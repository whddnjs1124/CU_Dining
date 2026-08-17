"""Regression check on the parser. Not coverage -- one job: notice when
Columbia changes their payload and the scraper starts quietly producing
nothing.

Run: pytest
"""
import json
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
