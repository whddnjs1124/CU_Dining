"""Shared Playwright setup.

Both test files drive a browser, and the sync API cannot be started twice in
one session, so the driver lives here and they borrow it.
"""
import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="session")
def playwright():
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright):
    b = playwright.chromium.launch()
    yield b
    b.close()
