"""Fingerprint profile, proxy routing, CAPTCHA detection, resolver load-deferral.

All offline: pages are served by Playwright routes or by a fake proxy that
answers requests itself and never connects anywhere.
"""

import asyncio
import base64
import os

import pytest

from app import captcha
from app.browser import open_browser
from app.config import ConfigError, ProxyConfig, RunInput
from app.redact import redactor
from app.resolver import Target, resolve

PROBE = """() => ({
  ua: navigator.userAgent,
  webdriver: navigator.webdriver,
  brands: (navigator.userAgentData?.brands || []).map(b => b.brand),
  platform: navigator.userAgentData?.platform,
  languages: navigator.languages,
})"""


async def _probe(profile: str, tmp_path) -> dict:
    async with open_browser(headless=True, storage_state=None, downloads=tmp_path / "dl",
                            profile=profile) as s:
        # https origin so userAgentData (secure-context only) is available
        await s.page.route("https://fp.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html", body="<p>fingerprint probe</p>"))
        await s.page.goto("https://fp.test/")
        info = await s.page.evaluate(PROBE)
        info["accept_language"] = None

        seen = {}

        async def capture(route):
            seen["al"] = route.request.headers.get("accept-language")
            seen["ua"] = route.request.headers.get("user-agent")
            await route.fulfill(status=200, body="ok")
        await s.page.route("https://fp.test/hdr", capture)
        await s.page.evaluate("fetch('/hdr')")
        await asyncio.sleep(0.2)
        info["accept_language"], info["header_ua"] = seen.get("al"), seen.get("ua")
        return info


async def test_hardened_profile_is_consistent_and_not_headless(tmp_path):
    info = await _probe("hardened", tmp_path)
    assert "Headless" not in info["ua"] and "Chrome/" in info["ua"]
    assert info["webdriver"] is False
    assert info["brands"] and not any("Headless" in b for b in info["brands"])
    assert info["languages"][0] == "en-US"
    assert info["accept_language"].startswith("en-US")
    assert info["header_ua"] == info["ua"]  # JS and network agree


async def test_default_profile_shows_the_problem_we_fix(tmp_path):
    info = await _probe("default", tmp_path)
    assert "HeadlessChrome" in info["ua"] and info["webdriver"] is True


# -- proxy ------------------------------------------------------------------

class FakeProxy:
    """Answers absolute-form HTTP requests itself; demands Basic auth if set."""

    def __init__(self, user=None, password=None):
        self.requests: list[str] = []
        self.auth = (base64.b64encode(f"{user}:{password}".encode()).decode()
                     if user else None)

    async def handle(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        line, *headers = head.split("\r\n")
        hdrs = {h.split(":", 1)[0].lower(): h.split(":", 1)[1].strip()
                for h in headers if ":" in h}
        if self.auth and hdrs.get("proxy-authorization") != f"Basic {self.auth}":
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                         b'Proxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\n\r\n')
        else:
            self.requests.append(line)
            body = b"<html><body><h1>served by fake proxy</h1></body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


@pytest.mark.parametrize("with_auth", [False, True])
async def test_traffic_goes_through_the_configured_proxy(tmp_path, with_auth):
    creds = ("proxy-user", "proxy-pass-123") if with_auth else (None, None)
    async with FakeProxy(*creds) as proxy:
        cfg = ProxyConfig.from_url(
            f"http://{'proxy-user:proxy-pass-123@' if with_auth else ''}127.0.0.1:{proxy.port}")
        async with open_browser(headless=True, storage_state=None, downloads=tmp_path / "dl",
                                proxy=cfg, profile="hardened") as s:
            await s.page.goto("http://portal.test/statements")
            assert await s.page.locator("h1").inner_text() == "served by fake proxy"
            assert s.proxy_server == f"http://127.0.0.1:{proxy.port}"
        assert any("http://portal.test/statements" in r for r in proxy.requests)
    if with_auth:
        assert redactor.redact("pw=proxy-pass-123") == "pw=[REDACTED]"


def test_proxy_routing_order(monkeypatch):
    base = {"repo": "o/r", "form": {"tag": "v1", "title": "t"},
            "document": {"release_tag": "v1", "asset_name": "a.pdf"}}
    monkeypatch.delenv("BRAVEBIRD_PROXY", raising=False)
    assert RunInput.model_validate(base).effective_proxy() is None

    monkeypatch.setenv("BRAVEBIRD_PROXY", "http://default:8080")
    assert RunInput.model_validate(base).effective_proxy().server == "http://default:8080"

    monkeypatch.setenv("BRAVEBIRD_PROXY_EU", "socks5://u:p%40ss@eu.example:1080")
    eu = RunInput.model_validate({**base, "proxy_route": "eu"}).effective_proxy()
    assert eu.server == "socks5://eu.example:1080"
    assert eu.username == "u" and eu.password.get_secret_value() == "p@ss"

    explicit = RunInput.model_validate({**base, "proxy_route": "eu",
                                        "proxy": {"server": "http://explicit:1"}})
    assert explicit.effective_proxy().server == "http://explicit:1"

    with pytest.raises(ConfigError):
        RunInput.model_validate({**base, "proxy_route": "missing"}).effective_proxy()
    assert "BRAVEBIRD_PROXY_MISSING" not in os.environ


# -- CAPTCHA -----------------------------------------------------------------

@pytest.mark.parametrize("html, kind", [
    ('<div class="g-recaptcha" style="width:300px;height:80px"></div>', "recaptcha"),
    ('<iframe src="https://hcaptcha.com/x" width="300" height="80"></iframe>', "hcaptcha"),
    ('<div class="cf-turnstile" style="width:300px;height:65px"></div>', "turnstile"),
    ('<iframe src="https://octocaptcha.com/" width="300" height="300"></iframe>',
     "github-octocaptcha"),
    ("<h1>Please verify you are human</h1>", "interstitial"),
    ("<h1>Sign in to GitHub</h1><input name=login>", None),
    ('<div class="g-recaptcha" hidden></div>', None),  # hidden widgets don't count
])
async def test_captcha_detection(page, html, kind):
    await page.set_content(html)
    assert await captcha.detect(page) == kind


async def test_captcha_stops_headless_runs(page):
    await page.set_content('<div class="g-recaptcha" style="width:300px;height:80px"></div>')
    with pytest.raises(captcha.CaptchaRequired) as err:
        await captcha.check(page, headless=True, wait_seconds=60)
    assert err.value.kind == "recaptcha" and not err.value.retryable


async def test_captcha_waits_for_a_human_when_headed(page):
    await page.set_content('<div id="c" class="g-recaptcha" style="width:300px;height:80px">'
                           '</div>')
    # The "human" solves it after 0.5 s.
    await page.evaluate("setTimeout(() => document.getElementById('c').remove(), 500)")
    await captcha.check(page, headless=False, wait_seconds=5, poll=0.1)
    assert await captcha.detect(page) is None


async def test_captcha_wait_times_out(page):
    await page.set_content('<div class="g-recaptcha" style="width:300px;height:80px"></div>')
    with pytest.raises(captcha.CaptchaRequired):
        await captcha.check(page, headless=False, wait_seconds=0.3, poll=0.1)


# -- resolver: weak strategies must not win while the page is loading ---------

async def test_weak_strategy_does_not_win_while_page_is_loading(tmp_path):
    async with open_browser(headless=True, storage_state=None, downloads=tmp_path / "dl",
                            profile="default") as s:
        page = s.page

        async def slow_image(route):
            await asyncio.sleep(1.5)  # keeps document.readyState at "interactive"
            await route.fulfill(status=200, content_type="image/gif", body=b"GIF89a")

        await page.route("https://slow.test/img.gif", slow_image)
        await page.route("https://slow.test/", lambda r: r.fulfill(
            status=200, content_type="text/html", body="""
            <p>Releases</p><a href="/wrong">some other link</a>
            <img src="/img.gif">
            <script>setTimeout(() => { const a = document.createElement('a');
              a.href = '/releases'; a.textContent = 'Releases'; document.body.append(a); }, 400);
            </script>"""))
        await page.goto("https://slow.test/", wait_until="domcontentloaded")
        target = Target(key="t", role="link", name="Releases", exact=True,
                        near_text=("Releases",), near_tag="a")
        r = await resolve(page, target, timeout=5)
        assert r.strategy == "role"
        assert await r.locator.get_attribute("href") == "/releases"
