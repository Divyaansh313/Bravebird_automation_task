"""Browser lifecycle and console capture."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .redact import redactor


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


@asynccontextmanager
async def open_browser(*, headless: bool, storage_state: Path | None, downloads: Path):
    downloads.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, downloads_path=str(downloads))
        context = await browser.new_context(
            storage_state=str(storage_state) if storage_state else None,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        console = ConsoleLog()
        console.attach(page)
        try:
            yield BrowserSession(context, page, console)
        finally:
            await context.close()
            await browser.close()
