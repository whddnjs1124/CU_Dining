#!/usr/bin/env python3
"""Columbia Dining scraper -> dining.json. See docs/HLD.md.

The site sits behind a Cloudflare JS challenge, so a real browser engine is
required -- plain HTTP clients get 403 "Just a moment...".

Every dining page ships the whole dataset inline as three JS globals
(`dining_nodes`, `dining_terms`, `menu_data`), so one page load is the entire
scrape. We read those instead of parsing DOM: it is the same JSON their own
AngularJS app consumes, and it survives CSS/markup churn.
"""
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = "https://dining.columbia.edu"
SOURCE = f"{BASE}/content/john-jay-dining-hall"  # any dining page carries the full payload
ROOT = Path(__file__).parent
OUT = ROOT / "dining.json"
FIXTURES = ROOT / "fixtures"
NY = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Locations that serve meals; everything else is coffee/retail with hours only.
DINING_HALLS = {
    "Chef Don's Pizza Pi featuring Blue Java", "Chef Mike's Sub Shop",
    "Faculty House 2nd Floor", "Faculty House 4th Floor", "Ferris Booth Commons",
    "Grace Dodge Dining Hall", "JJ's Place", "John Jay Dining Hall",
    "Johnny's Food Truck", "Robert F. Smith Dining Hall", "The Fac Shack",
}

READ_GLOBALS = """() => ({
    nodes: typeof dining_nodes !== 'undefined' ? dining_nodes : null,
    terms: typeof dining_terms !== 'undefined' ? dining_terms : null,
    menus: typeof menu_data   !== 'undefined' ? menu_data   : null,
    tz_offset: typeof window.timezoneOffset !== 'undefined' ? window.timezoneOffset : 0,
})"""


def text(v):
    """Drupal double-escapes titles: 'Chef Mike&#039;s' -> 'Chef Mike's'."""
    return html.unescape(v).strip() if isinstance(v, str) else v


def names(tids, terms):
    return [text(terms[str(t)]["name"]) for t in tids or [] if str(t) in terms]


def load(page):
    page.goto(SOURCE, wait_until="domcontentloaded", timeout=60000)
    # networkidle never settles (long-poll connections), so give the inline
    # bootstrap a fixed window instead.
    page.wait_for_function("typeof dining_nodes !== 'undefined'", timeout=30000)
    raw = page.evaluate(READ_GLOBALS)
    return {
        "locations": json.loads(raw["nodes"])["locations"],
        "terms": json.loads(raw["terms"]),
        "menus": json.loads(raw["menus"]),
        "tz_offset": raw["tz_offset"],
    }


def build_item(meal, terms):
    """One dish. Field names beyond `title` are inferred defensively.

    ponytail: the meal-level shape is unverified -- menu_data is empty over
    summer break and only repopulates for the Fall term (first node is dated
    2026-09-04). Verify against the live site then; until then this must not
    crash on unexpected keys.
    """
    item = {"title": text(meal.get("title") or meal.get("name") or "")}
    for key, val in meal.items():
        if not isinstance(val, list) or not val:
            continue
        k = key.lower()
        if "dietary" in k or "pref" in k:
            item["dietary"] = names(val, terms.get("dietary_prefs", {}))
        elif "allerg" in k or "ingredient" in k:
            item["allergens"] = names(val, terms.get("ingredients", {}))
    return item


def build_menus(menus, terms):
    """Flatten menu_data into (location, meal window, stations, items)."""
    out = []
    for node in menus:
        for window in node.get("date_range_fields", []):
            out.append({
                "location_nids": [str(n) for n in node.get("locations", [])],
                "date_from": window.get("date_from"),
                "date_to": window.get("date_to"),
                "meal": (names(window.get("menu_type"), terms.get("types", {})) or [None])[0],
                "stations": [{
                    "name": (names(s.get("station"), terms.get("stations", {})) or [None])[0],
                    "items": [build_item(m, terms) for m in s.get("meals", [])],
                } for s in window.get("stations", [])],
            })
    return out


def build(raw):
    terms = raw["terms"]
    locations = []
    for loc in raw["locations"]:
        title = text(loc["title"])
        path = loc.get("path") or ""
        locations.append({
            "nid": str(loc["nid"]),
            "name": title,
            "type": "dining_hall" if title in DINING_HALLS else "retail",
            "url": BASE + path if path else BASE,
            "description": text(loc.get("description", "")),
            "crowd_id": loc.get("crowd_id") or None,
            # Passed through verbatim: date ranges, per-weekday HHMM windows,
            # human-readable strings, and holiday exclusions. The frontend
            # derives open/closed from this so status is right even when the
            # snapshot is stale.
            "open_hours_fields": loc.get("open_hours_fields", []),
        })
    locations.sort(key=lambda l: l["name"])
    return {
        "updated_at": datetime.now(timezone.utc).astimezone(NY).isoformat(timespec="seconds"),
        "timezone_offset": raw["tz_offset"],
        "locations": locations,
        "menus": build_menus(raw["menus"], terms),
    }


def check(data):
    """Refuse to publish a partial scrape. Stale-but-correct beats half-right."""
    n = len(data["locations"])
    floor = 16
    if OUT.exists():
        floor = max(floor, len(json.loads(OUT.read_text())["locations"]))
    if n < floor:
        sys.exit(f"scrape looks partial: {n} locations, expected >= {floor}. Not writing.")
    if not any(l["open_hours_fields"] for l in data["locations"]):
        sys.exit("no location has any hours. Not writing.")


def recon(url):
    """Dump every non-asset response, then save rendered HTML to fixtures/."""
    FIXTURES.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        seen = []
        page.on("response", seen.append)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        print(f"{page.title()}\n")
        for r in seen:
            ct = r.headers.get("content-type", "").split(";")[0]
            if any(a in ct for a in ("image/", "font/", "text/css", "javascript")):
                continue
            print(f"{r.status} {ct:<24} {r.url[:130]}")
            if "json" in ct:
                try:
                    print(f"    {r.text()[:800]}\n")
                except Exception as e:
                    print(f"    <unavailable: {e}>\n")
        name = (urlparse(url).path.strip("/").replace("/", "_") or "home") + ".html"
        (FIXTURES / name).write_text(page.content())
        print(f"\nsaved fixtures/{name}")
        browser.close()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        raw = load(page)
        browser.close()

    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / "globals.json").write_text(json.dumps(raw, indent=1))

    data = build(raw)
    check(data)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    halls = sum(1 for l in data["locations"] if l["type"] == "dining_hall")
    print(f"{len(data['locations'])} locations ({halls} halls), "
          f"{len(data['menus'])} menu windows -> {OUT.name}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--recon":
        recon(sys.argv[2])
    elif len(sys.argv) > 1:
        sys.exit("usage: scrape.py [--recon <url>]")
    else:
        main()
