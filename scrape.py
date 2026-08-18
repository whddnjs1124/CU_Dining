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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = "https://dining.columbia.edu"
SOURCE = f"{BASE}/content/john-jay-dining-hall"  # any dining page carries the full payload
ROOT = Path(__file__).parent
OUT = ROOT / "dining.json"
FIXTURES = ROOT / "fixtures"
NY = ZoneInfo("America/New_York")
# navigator.webdriver is true without this, which is the loudest automation
# tell there is. Measured: the flag flips it to false.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Columbia lists 16. Well below that means the payload broke, not that a café
# closed; well above any plausible shrinkage so a real closure never wedges us.
MIN_LOCATIONS = 8

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


def open_page(pw):
    """A browser that doesn't advertise itself as automation.

    Cloudflare went from challenging roughly one scrape in fifteen to
    challenging every one from the GitHub runner. Two signals were measurably
    wrong: `navigator.webdriver` was true, and the user agent said
    `HeadlessChrome`.

    The UA is derived from the browser's own rather than hardcoded. A fixed
    macOS string would contradict `navigator.platform` on a Linux runner, and
    a pinned Chrome version goes stale against the engine actually running --
    both are tells in their own right.
    """
    browser = pw.chromium.launch(args=LAUNCH_ARGS)
    probe = browser.new_page()
    ua = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    probe.close()
    context = browser.new_context(
        user_agent=ua,
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": 1280, "height": 900},
    )
    return browser, context.new_page()


def die(message):
    """Exit non-zero, and make the reason legible from outside the run.

    Actions logs need authentication to read, so a bare exit shows up only as
    "Process completed with exit code 1" and the cause has to be inferred from
    how long the step took. ::error:: becomes an annotation, which is public.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message}")
    sys.exit(message)


def load(page, attempts=3):
    """Read the inline globals, retrying past a Cloudflare wall.

    The interstitial arrives instead of the real page: `goto` succeeds,
    `dining_nodes` never appears, and the wait below times out. Retrying in the
    *same* browser context is what makes this work -- the challenge page runs
    its JS and banks a clearance cookie, so a later attempt can sail through.
    """
    for attempt in range(1, attempts + 1):
        page.goto(SOURCE, wait_until="domcontentloaded", timeout=60000)
        try:
            # networkidle never settles (long-poll connections), so give the
            # inline bootstrap a fixed window instead.
            page.wait_for_function("typeof dining_nodes !== 'undefined'", timeout=30000)
            break
        except PlaywrightTimeout:
            blocked = "just a moment" in page.title().lower()
            why = "Cloudflare challenge" if blocked else f"no dining_nodes (title: {page.title()!r})"
            if attempt == attempts:
                die(f"{why} after {attempts} attempts. Not writing.")
            # Back off further each time: five seconds was not long enough to
            # outlast a challenge that had decided to stick around.
            pause = 5000 * 2 ** (attempt - 1)
            print(f"attempt {attempt}: {why}, retrying in {pause // 1000}s...", file=sys.stderr)
            page.wait_for_timeout(pause)

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
    """Refuse to publish a broken scrape. Stale-but-correct beats half-right.

    Deliberately a fixed floor rather than a ratchet against the last run. A
    ratchet reads stricter but wedges permanently the first time Columbia
    retires a location: 16 -> 15 would fail, and because the failure means we
    never write, every later scrape compares against 16 too and also fails.
    The payload arrives as one JSON blob, so "half the locations parsed" is
    not a real failure mode anyway -- what we are catching is a blob that
    stopped arriving or stopped being what we think it is.

    Note what is NOT checked: whether anything is actually open. Every
    location closed is normal for months at a time over summer and winter
    break, and Columbia keeps the hours blocks in place while closed. Do not
    "improve" this into an open-for-business check -- see test_scrape.py.
    """
    n = len(data["locations"])
    if n < MIN_LOCATIONS:
        die(f"only {n} locations, expected at least {MIN_LOCATIONS}. Not writing.")
    if not any(l["open_hours_fields"] for l in data["locations"]):
        die("no location carries any hours block at all. Not writing.")


def recon(url):
    """Dump every non-asset response, then save rendered HTML to fixtures/."""
    FIXTURES.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser, page = open_page(p)
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
        browser, page = open_page(p)
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
