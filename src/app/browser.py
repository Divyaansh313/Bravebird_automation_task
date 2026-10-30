"""Browser lifecycle, proxy routing, fingerprint profile and console capture."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from . import fingerprint
from .config import ProxyConfig
from .fsutil import private_dir
from .redact import redactor

log = logging.getLogger(__name__)


@dataclass
class ConsoleLog:
    lines: list[str] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("console", lambda m: self.lines.append(redactor.redact(f"[{m.type}] {m.text}")))
        page.on("pageerror", lambda e: self.lines.append(redactor.redact(f"[pageerror] {e}")))


@dataclass
class BrowserSession:
    context: BrowserContext
    page: Page
    console: ConsoleLog
    profile: str = "default"
    user_agent: str | None = None
    proxy_server: str | None = None


@asynccontextmanager
async def open_browser(*, headless: bool, storage_state: Path | None, downloads: Path,
                       proxy: ProxyConfig | None = None, profile: str = "hardened",
                       timezone: str | None = None):
    private_dir(downloads)
    if proxy and proxy.password:
        redactor.add(proxy.password.get_secret_value())
    if proxy and proxy.username:
        redactor.add(proxy.username)

    launch: dict = {"headless": headless, "downloads_path": str(downloads)}
    if proxy:
        launch["proxy"] = proxy.playwright()
    if profile == "hardened":
        launch["args"] = fingerprint.LAUNCH_ARGS
        launch["channel"] = "chromium"  # full build in new-headless mode, not the shell

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)
        ctx_opts: dict = {
            "storage_state": str(storage_state) if storage_state else None,
            "accept_downloads": True,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        fp = None
        if profile == "hardened":
            fp = fingerprint.build_profile(browser.version)
            ctx_opts.update(fp.context_options(timezone))
        elif timezone:
            ctx_opts["timezone_id"] = timezone
        context = await browser.new_context(**ctx_opts)
        if fp:
            await fingerprint.harden(browser, context, fp)
        page = await context.new_page()
        if fp:  # the "page" handler covers later pages; make sure the first is done
            await fingerprint.apply_to_page(context, page, fp)
        console = ConsoleLog()
        console.attach(page)
        log.info("Browser: chromium %s, profile=%s, proxy=%s", browser.version, profile,
                 proxy.server if proxy else "none")
        try:
            yield BrowserSession(context, page, console, profile,
                                 fp.user_agent if fp else None,
                                 proxy.server if proxy else None)
        finally:
            await context.close()
            await browser.close()
