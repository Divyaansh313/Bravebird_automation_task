from pathlib import Path

import pytest
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        pg = await browser.new_page()
        # Offline: block anything that tries to reach the network.
        await pg.route("**/*", lambda route: route.abort()
                       if not route.request.url.startswith(("about:", "data:")) else route.continue_())
        yield pg
        await browser.close()


@pytest.fixture
def login_html() -> str:
    return (FIXTURES / "github_login.html").read_text(encoding="utf-8")
