"""Drives the real page in a headless browser.

Exists mainly so CI runs the in-page assertion suite. Those assertions live in
index.html and only fire when a human opens `?selftest`, which means a
frontend regression would otherwise ship without anyone noticing.

The rest are checkpoint tests: they pin down what the page does today against
the real dining.json, so the next change has something to fail against.

Run: pytest
"""
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
SPRING_TERM = "2026-03-02T12:40:00-05:00"   # a Monday with everything running
LATE_NIGHT = "2026-03-03T02:00:00-05:00"    # a Tuesday at 2am
SUMMER = "2026-08-10T12:40:00-04:00"        # a Monday over the break


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def site():
    handler = functools.partial(Quiet, directory=str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A page that fails the test if it logs an error."""
    pg = browser.new_page(viewport={"width": 420, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.errors = errors
    yield pg
    pg.close()


def visit(page, url, when=None):
    page.goto(f"{url}?now={when}" if when else url)
    page.wait_for_selector(".card", timeout=15000)
    assert not page.errors, page.errors
    return page


@pytest.fixture
def dining():
    return json.loads((ROOT / "dining.json").read_text())


# --- the suite that lives inside the page ------------------------------------

def test_in_page_assertions_all_pass(page, site):
    page.goto(f"{site}?selftest")
    page.wait_for_selector("#selftest")
    lines = page.inner_text("#selftest").splitlines()
    failed = [l for l in lines if l.startswith("FAIL")]
    assert not failed, "\n".join(failed)
    # Guards against the suite being gutted or silently failing to run.
    assert len(lines) >= 40, f"only {len(lines)} assertions ran"
    assert not page.errors, page.errors


# --- rendering against the committed data ------------------------------------

def test_every_location_gets_a_card(page, site, dining):
    visit(page, site, SPRING_TERM)
    assert page.locator(".card").count() == len(dining["locations"])


def test_both_groups_are_labelled(page, site):
    visit(page, site, SPRING_TERM)
    headings = page.eval_on_selector_all(".list h2", "es => es.map(e => e.textContent)")
    assert headings == ["Dining halls", "Cafés & retail"]


def test_ribbon_draws_a_row_per_serving_location(page, site):
    visit(page, site, SPRING_TERM)
    rows = page.locator(".row:not(.glab)").count()
    assert rows >= 10, f"only {rows} ribbon rows on a full term day"
    assert page.locator(".bar").count() >= rows


def test_open_locations_sort_first(page, site):
    visit(page, site, SPRING_TERM)
    states = page.eval_on_selector_all(
        ".list:first-of-type .card", "es => es.map(e => e.className.split(' ')[1])")
    rank = {"open": 0, "closing": 1, "opening": 2, "closed": 3, "break": 4}
    assert states == sorted(states, key=lambda s: rank[s])


def test_late_night_keeps_the_marker_on_the_dial(page, site):
    """The regression that started this: at 2am the dial used to go blank."""
    visit(page, site, LATE_NIGHT)
    assert page.locator(".now").count() == 1
    jj = page.locator(".card", has_text="JJ'S PLACE").first
    assert "Open until 10:00 AM" in jj.inner_text()


def test_break_banner_names_the_reopening(page, site):
    visit(page, site, SUMMER)
    banner = page.inner_text("#banner")
    assert "closed for the break" in banner
    assert "August 26" in banner


def test_break_cards_carry_a_reopening_date(page, site):
    visit(page, site, SUMMER)
    jj = page.locator(".card", has_text="JJ'S PLACE").first
    assert "Closed for break" in jj.inner_text()
    assert "Reopens Aug 26" in jj.inner_text()


# --- states the committed data cannot reach on its own -----------------------

def rerender(page, mutate):
    """Re-run render() with a doctored payload, for states real data lacks."""
    return page.evaluate(
        """async ([url, body]) => {
             const data = await (await fetch(url)).json();
             (new Function('data', body))(data);
             render(data);
             return true;
           }""", ["dining.json", mutate])


def test_stale_data_raises_a_warning(page, site):
    visit(page, site, SPRING_TERM)
    rerender(page, "data.updated_at = new Date(Date.now() - 9*3600e3).toISOString();")
    banner = page.inner_text("#banner")
    assert "hours ago" in banner and "out of date" in banner


def test_fresh_data_raises_no_warning(page, site):
    visit(page, site, SPRING_TERM)
    rerender(page, "data.updated_at = new Date().toISOString();")
    assert "out of date" not in page.inner_text("#banner")


def test_menus_render(page, site):
    """The menu path cannot be checked against live data until the term starts
    (menu_data is empty over break), so it is exercised here with a payload
    shaped like the real one. Field names inside a dish are still unverified.
    """
    visit(page, site, SPRING_TERM)
    rerender(page, """
        const nid = data.locations.find(l => l.name.includes('John Jay')).nid;
        data.menus = [{
          location_nids: [nid],
          date_from: '2026-03-02T16:00:00', date_to: '2026-03-02T22:00:00',
          meal: 'Lunch',
          stations: [{ name: 'Main Line', items: [
            { title: 'Peri Peri Chicken', dietary: ['Halal'], allergens: [] },
            { title: 'Quinoa', dietary: ['Vegan'] }]}],
        }];
    """)
    card = page.locator(".card", has_text="JOHN JAY").first
    card.locator("summary").click()
    text = card.inner_text().lower()   # headings are uppercased in CSS
    assert "lunch" in text and "main line" in text
    assert "peri peri chicken" in text and "quinoa" in text
    assert "halal" in text and "vegan" in text
    # date_from is UTC, so the heading must show New York time, not 16:00.
    assert "11:00 am" in text


def test_no_menu_means_no_disclosure(page, site):
    visit(page, site, SUMMER)
    assert page.locator(".card details").count() == 0


def test_location_names_are_escaped(page, site):
    """Names come from Columbia's CMS; they must not be able to inject markup."""
    visit(page, site, SPRING_TERM)
    rerender(page, "data.locations[0].name = '<img src=x onerror=alert(1)>Hax';")
    assert page.locator("#out img").count() == 0, "markup in a name became an element"
    # Cards are ordered by status, so find it by content rather than position.
    hacked = page.locator(".card", has_text="Hax").first
    assert "<img" in hacked.inner_text().lower(), "the name should render as literal text"
    assert not page.errors, page.errors


# --- installable to the home screen ------------------------------------------

@pytest.fixture
def manifest():
    return json.loads((ROOT / "manifest.webmanifest").read_text())


def test_manifest_declares_a_standalone_app(manifest):
    assert manifest["short_name"] == "CU Dining"
    assert manifest["display"] == "standalone"
    # Pages serves this from /CU_Dining/, so absolute paths would 404.
    assert not manifest["start_url"].startswith("/")
    assert not manifest["scope"].startswith("/")


def test_declared_icons_exist_at_their_declared_size(manifest):
    import struct
    for icon in manifest["icons"]:
        path = ROOT / icon["src"]
        assert path.exists(), f"{icon['src']} is declared but missing"
        w, h = struct.unpack(">II", path.read_bytes()[16:24])
        assert f"{w}x{h}" == icon["sizes"], f"{icon['src']} is {w}x{h}, declared {icon['sizes']}"


def test_a_maskable_icon_is_offered(manifest):
    assert any("maskable" in i.get("purpose", "") for i in manifest["icons"])


def test_ios_home_screen_icon_is_linked(page, site):
    """iOS ignores the manifest icons and uses apple-touch-icon."""
    visit(page, site, SPRING_TERM)
    href = page.get_attribute('link[rel="apple-touch-icon"]', "href")
    assert href and (ROOT / href).exists()


def test_page_reaches_under_the_notch_and_pads_it_back(page, site):
    """Installed, the page runs full-bleed; without the insets the masthead
    would sit under the notch."""
    visit(page, site, SPRING_TERM)
    assert "viewport-fit=cover" in page.get_attribute('meta[name="viewport"]', "content")
    # env() resolves to 0 in a normal tab, so the computed value proves
    # nothing; read the rule itself.
    rule = page.evaluate("""() => [...document.styleSheets[0].cssRules]
        .filter(r => r.selectorText === '.wrap').map(r => r.style.padding)[0]""")
    for side in ("top", "right", "bottom", "left"):
        assert f"safe-area-inset-{side}" in rule, f"{side} inset missing from .wrap"


def test_service_worker_never_prefers_cache_for_the_data():
    """A cached menu shown as current is the failure this project exists to
    avoid, so dining.json must be fetched first and only fall back."""
    sw = (ROOT / "sw.js").read_text()
    handler = sw[sw.index("endsWith('/dining.json')"):]
    assert handler.index("fetch(request)") < handler.index("caches.match"), \
        "dining.json must be network-first"


def test_service_worker_expires_old_caches():
    assert "caches.delete" in (ROOT / "sw.js").read_text()


def test_the_page_still_works_with_no_network(browser, site):
    """The point of installing it: usable in the Butler stacks.

    Worth testing for real rather than by reading sw.js -- the first version
    of this cached nothing, because it cloned the response inside a callback
    that ran after the body had already been handed to the page.
    """
    ctx = browser.new_context()
    pg = ctx.new_page()
    try:
        pg.goto(f"{site}?now={SPRING_TERM}")
        pg.wait_for_selector(".card")
        pg.evaluate("navigator.serviceWorker.ready")
        pg.reload()                       # the first load is never controlled
        pg.wait_for_selector(".card")
        # Being active is not the same as controlling this page, and an
        # uncontrolled page's dining.json fetch is never intercepted, so the
        # cache below would stay empty. Wait for control before relying on it.
        pg.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
        pg.wait_for_function(
            "caches.open('data-v1').then(c => c.keys()).then(k => k.length > 0)", timeout=15000)

        ctx.set_offline(True)
        offline = ctx.new_page()
        offline.goto(f"{site}?now={SPRING_TERM}")
        offline.wait_for_selector(".card", timeout=10000)
        assert offline.locator(".card").count() == 16
        assert offline.locator(".row:not(.glab)").count() > 0, "the dial should render too"
    finally:
        ctx.close()


# --- layout ------------------------------------------------------------------

@pytest.mark.parametrize("width", [320, 390, 768, 1200])
def test_no_horizontal_overflow(browser, site, width):
    pg = browser.new_page(viewport={"width": width, "height": 900})
    pg.goto(f"{site}?now={SPRING_TERM}")
    pg.wait_for_selector(".card")
    scroll, client = pg.evaluate(
        "[document.body.scrollWidth, document.documentElement.clientWidth]")
    pg.close()
    assert scroll <= client, f"{width}px viewport scrolls horizontally"


def test_ribbon_names_never_take_three_lines(page, site):
    """Row height is fixed, so a third line would knock the bars out of line."""
    visit(page, site, SPRING_TERM)
    tall = page.eval_on_selector_all(".row:not(.glab) .rname", """
        es => es.filter(e => e.getBoundingClientRect().height > 30)
                .map(e => e.textContent)""")
    assert not tall, tall
