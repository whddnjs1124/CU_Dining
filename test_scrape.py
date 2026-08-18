"""Regression check on the parser. Not coverage -- one job: notice when
Columbia changes their payload and the scraper starts quietly producing
nothing.

Run: pytest
"""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from scrape import build, build_menus, check, load

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def live():
    """Real payload captured from dining.columbia.edu (`python scrape.py`)."""
    return json.loads((FIXTURES / "globals.json").read_text())


def test_all_locations_survive_the_build(live):
    data = build(live)
    assert len(data["locations"]) == 16
    names = [l["name"] for l in data["locations"]]
    assert "John Jay Dining Hall" in names
    assert "Ferris Booth Commons" in names


def test_titles_are_unescaped(live):
    names = [l["name"] for l in build(live)["locations"]]
    assert "Chef Mike's Sub Shop" in names, "Drupal's &#039; leaked through"
    assert not any("&#" in n for n in names)


def test_every_location_has_hours(live):
    for loc in build(live)["locations"]:
        assert loc["open_hours_fields"], f"{loc['name']} has no hours"
        assert loc["url"].startswith("https://dining.columbia.edu")


def test_halls_and_retail_are_both_present(live):
    types = {l["type"] for l in build(live)["locations"]}
    assert types == {"dining_hall", "retail"}


def test_collapsed_scrape_is_rejected(live):
    data = build(live)
    data["locations"] = data["locations"][:5]
    with pytest.raises(SystemExit):
        check(data)


def test_scrape_with_no_hours_anywhere_is_rejected(live):
    data = build(live)
    for loc in data["locations"]:
        loc["open_hours_fields"] = []
    with pytest.raises(SystemExit):
        check(data)


def test_a_retired_location_does_not_wedge_the_scraper(live):
    """Columbia closing one café must not stop every future scrape.

    A floor tied to the previous run's count would reject 15 forever: the
    rejection means nothing is written, so the next run compares against 16
    again and also fails. Nobody would notice until the stale banner appeared.
    """
    data = build(live)
    data["locations"] = data["locations"][:-1]
    check(data)


def test_everything_closed_is_a_valid_scrape(live):
    """Every location shut is the normal state for months at a time.

    Over summer and winter break Columbia closes the halls but keeps their
    hours blocks in place. Validation counts locations and hours blocks, never
    whether anything is open -- turning this into an open-for-business check
    would silently freeze the site every break.
    """
    data = build(live)
    for loc in data["locations"]:
        for block in loc["open_hours_fields"]:
            block["days"] = []
            block["displayed_hours"] = [{"title": "Closed for Summer"}]
    check(data)


# menu_data is empty over summer break (Fall menus start 2026-09-04), so the
# menu path is exercised against a synthetic payload shaped like the real one.
# Replace this with a captured fixture once the term starts.
SYNTHETIC = {
    "terms": {
        "types": {"6": {"name": "Breakfast", "tid": "6"}},
        "stations": {"24": {"name": "Main Line", "tid": "24"},
                     "10": {"name": "Chef Mike&#039;s Kitchen", "tid": "10"}},
        "dietary_prefs": {"20": {"name": "Vegan", "tid": "20"},
                          "23": {"name": "Gluten Free", "tid": "23"}},
        "ingredients": {"3": {"name": "Eggs", "tid": "3"}},
    },
    "menus": [{
        "nid": "17590",
        "locations": ["840"],
        "date_range_fields": [{
            "date_from": "2026-10-29T05:00:00",
            "date_to": "2026-10-29T10:59:00",
            "menu_type": ["6"],
            "stations": [
                {"station": ["24"], "meals": [
                    {"title": "Scrambled Eggs", "ingredients": ["3"], "dietary_prefs": ["23"]},
                    {"title": "Vegetable Medley", "dietary_prefs": ["20", "23"]},
                ]},
                {"station": ["10"], "meals": [{"title": "Iced Coffee Bar"}]},
            ],
        }],
    }],
}


def test_menu_windows_flatten_with_names_resolved():
    out = build_menus(SYNTHETIC["menus"], SYNTHETIC["terms"])
    assert len(out) == 1
    w = out[0]
    assert w["location_nids"] == ["840"]
    assert w["meal"] == "Breakfast"
    assert [s["name"] for s in w["stations"]] == ["Main Line", "Chef Mike's Kitchen"]

    eggs = w["stations"][0]["items"][0]
    assert eggs["title"] == "Scrambled Eggs"
    assert eggs["allergens"] == ["Eggs"]
    assert eggs["dietary"] == ["Gluten Free"]
    assert w["stations"][0]["items"][1]["dietary"] == ["Vegan", "Gluten Free"]


# --- what got us blocked ------------------------------------------------------

@pytest.fixture(scope="module")
def browsers(playwright, browser):
    """A page opened the way a scrape opens one, plus a plain default page to
    compare it against."""
    from scrape import open_page
    scraper_browser, page = open_page(playwright)
    yield page, browser.new_page()
    scraper_browser.close()


@pytest.fixture
def scraper_page(browsers):
    return browsers[0]


def test_the_browser_does_not_announce_itself_as_automation(scraper_page):
    """This is what took the scraper down for four hours on 2026-08-18.

    Cloudflare went from challenging the occasional run to blocking every one.
    navigator.webdriver was true and the UA said HeadlessChrome; both are
    trivially readable from the page. Nothing caught it because nothing looked.
    """
    assert scraper_page.evaluate("navigator.webdriver") is False
    assert "Headless" not in scraper_page.evaluate("navigator.userAgent")


def test_the_user_agent_is_the_real_one_with_headless_removed(browsers):
    """Not a hardcoded string. A fixed macOS UA contradicts navigator.platform
    on a Linux runner, and a pinned Chrome version drifts from the engine
    actually running -- each is a tell on its own. The only edit allowed is
    dropping the word Headless.
    """
    scraper, plain = browsers
    expected = plain.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    assert scraper.evaluate("navigator.userAgent") == expected


def test_failures_say_why_where_the_logs_cannot_be_read(monkeypatch, capsys):
    """Actions logs need auth, so a bare exit reads as 'exit code 1' and the
    cause has to be guessed from how long the step took -- which is how the
    Cloudflare block was actually diagnosed. ::error:: lands in the public
    annotations instead."""
    from scrape import die
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(SystemExit):
        die("Cloudflare challenge after 3 attempts. Not writing.")
    assert "::error::Cloudflare challenge" in capsys.readouterr().out


def test_local_runs_do_not_emit_workflow_commands(monkeypatch, capsys):
    from scrape import die
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit):
        die("nope")
    assert "::error::" not in capsys.readouterr().out


class FakePage:
    """Stands in for a Playwright page that hits Cloudflare `blocked` times.

    Real runs failed at exactly 30s -- goto succeeded, dining_nodes never
    arrived -- so the interstitial is modelled as the wait timing out rather
    than the navigation failing.
    """

    def __init__(self, blocked, payload):
        self.blocked, self.payload, self.loads, self.waits = blocked, payload, 0, 0

    def goto(self, *a, **k):
        self.loads += 1

    def wait_for_function(self, *a, **k):
        if self.loads <= self.blocked:
            raise PlaywrightTimeout("timed out")

    def title(self):
        return "Just a moment..." if self.loads <= self.blocked else "John Jay | Columbia Dining"

    def wait_for_timeout(self, ms):
        self.waits += 1

    def evaluate(self, _):
        return self.payload


@pytest.fixture
def payload(live):
    return {"nodes": json.dumps({"locations": live["locations"]}),
            "terms": json.dumps(live["terms"]),
            "menus": json.dumps(live["menus"]),
            "tz_offset": live["tz_offset"]}


def test_load_retries_past_a_cloudflare_challenge(payload, live):
    page = FakePage(blocked=1, payload=payload)
    assert len(load(page)["locations"]) == len(live["locations"])
    assert page.loads == 2, "should have reloaded once"
    assert page.waits == 1, "should have paused before retrying"


def test_load_survives_two_challenges(payload):
    page = FakePage(blocked=2, payload=payload)
    load(page)
    assert page.loads == 3


def test_load_gives_up_rather_than_writing_nothing(payload):
    """Exiting non-zero is the point: the workflow then skips the commit and
    yesterday's correct data stays published."""
    page = FakePage(blocked=99, payload=payload)
    with pytest.raises(SystemExit) as e:
        load(page)
    assert "Cloudflare" in str(e.value)
    assert page.loads == 3, "should not hammer the site"


def test_a_clean_load_does_not_retry(payload):
    page = FakePage(blocked=0, payload=payload)
    load(page)
    assert page.loads == 1 and page.waits == 0


# --- the workflow's own invariants -------------------------------------------
# Checked as text rather than parsed YAML: three specific lines are not worth a
# dependency, and each of these three was an actual outage.

@pytest.fixture
def workflow():
    return (Path(__file__).parent / ".github/workflows/scrape.yml").read_text()


def test_a_wedged_run_cannot_hold_the_queue(workflow):
    """One run sat in progress for 53 minutes with others stacking up behind
    it. A healthy scrape takes about a minute."""
    match = re.search(r"timeout-minutes:\s*(\d+)", workflow)
    assert match, "the job needs a timeout"
    assert int(match.group(1)) <= 15


def test_a_newer_run_supersedes_a_struggling_one(workflow):
    assert re.search(r"cancel-in-progress:\s*true", workflow), \
        "queueing behind a stuck run is how the 53-minute hang happened"


def test_the_cadence_stays_gentle(workflow):
    """Half-hourly produced 185 commits with zero content change and got the
    runner blocked. Columbia publishes menus months ahead; there is nothing to
    catch on a tighter loop."""
    cron = re.search(r'cron:\s*"([^"]+)"', workflow)
    assert cron, "no cron schedule found"
    minute, hour = cron.group(1).split()[:2]
    assert "," not in minute and "/" not in minute, f"more than once an hour: {cron.group(1)}"
    step = re.match(r"\*/(\d+)", hour)
    assert step and int(step.group(1)) >= 3, f"cadence tighter than 3h: {cron.group(1)}"


# The committed dining.json is what the site actually serves, so it is worth
# checking directly -- a bad hand-edit or a half-written file would otherwise
# only show up as a blank page.
@pytest.fixture
def published():
    return json.loads((Path(__file__).parent / "dining.json").read_text())


def test_published_json_has_the_shape_the_page_expects(published):
    assert published["locations"] and isinstance(published["menus"], list)
    for loc in published["locations"]:
        assert loc["nid"].isdigit()
        assert loc["type"] in ("dining_hall", "retail")
        assert loc["name"] and loc["url"].startswith("https://dining.columbia.edu")
        for block in loc["open_hours_fields"]:
            assert "date_from" in block and "date_to" in block


def test_published_timestamp_is_new_york(published):
    assert published["updated_at"].endswith(("-04:00", "-05:00"))


def test_published_hours_are_four_digit_clock_times(published):
    for loc in published["locations"]:
        for block in loc["open_hours_fields"]:
            for day in block.get("days", []):
                if not isinstance(day, dict):
                    continue          # empty list is how a break block looks
                for windows in day.values():
                    for w in windows:
                        for key in ("hours_from", "hours_to"):
                            assert w[key].isdigit() and len(w[key]) <= 4, w


def test_published_menus_point_at_real_locations(published):
    nids = {l["nid"] for l in published["locations"]}
    for menu in published["menus"]:
        assert set(menu["location_nids"]) <= nids


def test_unknown_meal_fields_do_not_crash():
    menus = [{"locations": ["840"], "date_range_fields": [{
        "date_from": "x", "date_to": "y", "menu_type": [],
        "stations": [{"station": [], "meals": [{"some_new_field": ["99"], "nutrition": {}}]}],
    }]}]
    out = build_menus(menus, SYNTHETIC["terms"])
    assert out[0]["stations"][0]["items"] == [{"title": ""}]
    assert out[0]["meal"] is None
