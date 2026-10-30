"""CAPTCHA detection and human-in-the-loop handling.

Policy: never solve CAPTCHAs automatically and never send them to a solving
service. A CAPTCHA means the portal wants a human, so the run either

  - stops with CaptchaRequired (final, evidence written, job marked
    "needs_human" by the queue), or
  - when headed and captcha_wait_seconds > 0, waits for the person at the
    screen to solve it, then carries on.

Detection looks at frames and markup for the common providers plus generic
"verify you are human" interstitials.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .errors import FinalError

log = logging.getLogger(__name__)

# frame URL / element selector -> provider name
FRAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"google\.com/recaptcha|recaptcha\.net"), "recaptcha"),
    (re.compile(r"hcaptcha\.com"), "hcaptcha"),
    (re.compile(r"challenges\.cloudflare\.com"), "turnstile"),
    (re.compile(r"arkoselabs\.com|funcaptcha\.com"), "arkose"),
    (re.compile(r"octocaptcha\.com"), "github-octocaptcha"),
]
ELEMENT_SELECTORS: list[tuple[str, str]] = [
    ("iframe[src*='recaptcha'], .g-recaptcha, #g-recaptcha-response", "recaptcha"),
    ("iframe[src*='hcaptcha'], .h-captcha", "hcaptcha"),
    ("iframe[src*='challenges.cloudflare.com'], .cf-turnstile", "turnstile"),
    ("iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], #arkose-iframe", "arkose"),
    ("iframe[src*='octocaptcha'], .octocaptcha-frame, #captcha-container-nux", "github-octocaptcha"),
]
TEXT_PATTERN = re.compile(
    r"verify (?:that )?you(?:'re| are) (?:a )?human|are you a robot|complete the captcha|"
    r"security check to continue|please solve this puzzle", re.IGNORECASE)


class CaptchaRequired(FinalError):
    def __init__(self, kind: str, url: str):
        self.kind = kind
        super().__init__(f"CAPTCHA ({kind}) requires a human at {url}")


async def detect(page: Page) -> str | None:
    """Return the CAPTCHA provider shown on the page, or None."""
    for frame in page.frames:
        for pattern, kind in FRAME_PATTERNS:
            if frame.url and pattern.search(frame.url):
                return kind
    try:
        for selector, kind in ELEMENT_SELECTORS:
            if await page.locator(selector).filter(visible=True).count():
                return kind
        if await page.get_by_text(TEXT_PATTERN).filter(visible=True).count():
            return "interstitial"
    except PlaywrightError:  # page navigating
        return None
    return None


async def check(page: Page, *, headless: bool, wait_seconds: int, poll: float = 2.0) -> None:
    """Raise CaptchaRequired if a CAPTCHA is showing and no human can solve it."""
    kind = await detect(page)
    if not kind:
        return
    if headless or wait_seconds <= 0:
        raise CaptchaRequired(kind, page.url)

    log.warning("CAPTCHA (%s) on screen: waiting up to %ds for a human to solve it",
                kind, wait_seconds)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        if not await detect(page):
            log.info("CAPTCHA cleared by a human; continuing")
            return
    raise CaptchaRequired(kind, page.url)
