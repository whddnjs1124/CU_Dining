"""Regression check on the parser. Not coverage -- one job: notice when
Columbia changes their payload and the scraper starts quietly producing
nothing.

Run: pytest
"""
import json
from pathlib import Path

import pytest

from scrape import build, build_menus, check

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


def test_partial_scrape_is_rejected(live):
    data = build(live)
    data["locations"] = data["locations"][:5]
    with pytest.raises(SystemExit):
        check(data)


def test_hours_only_scrape_is_rejected(live):
    data = build(live)
    for loc in data["locations"]:
        loc["open_hours_fields"] = []
    with pytest.raises(SystemExit):
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


def test_unknown_meal_fields_do_not_crash():
    menus = [{"locations": ["840"], "date_range_fields": [{
        "date_from": "x", "date_to": "y", "menu_type": [],
        "stations": [{"station": [], "meals": [{"some_new_field": ["99"], "nutrition": {}}]}],
    }]}]
    out = build_menus(menus, SYNTHETIC["terms"])
    assert out[0]["stations"][0]["items"] == [{"title": ""}]
    assert out[0]["meal"] is None
