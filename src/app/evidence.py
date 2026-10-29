"""Failure evidence bundle.

On a final failure the run writes, under runs/<id>/evidence/:

    screenshot.png   account identifiers and every input field painted over
    page.html        DOM snapshot, redacted, hidden input values and CSRF tokens removed
    url.txt          current URL, redacted
    console.log      browser console + page errors, redacted
    failure.json     stage key, error, retry history, resolver attempts

Nothing is written unredacted: every text artifact goes through the redactor.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from .browser import ConsoleLog
from .redact import MASK, redactor
from .resolver import ResolveError

log = logging.getLogger(__name__)

_HIDDEN_VALUE = re.compile(r'(<input\b[^>]*type="hidden"[^>]*\bvalue=")[^"]*(")', re.IGNORECASE)
_HIDDEN_VALUE_REV = re.compile(r'(<input\b[^>]*\bvalue=")[^"]*("[^>]*type="hidden")', re.IGNORECASE)
_ANY_INPUT_VALUE = re.compile(r'(<(?:input|textarea)\b[^>]*\bvalue=")[^"]*(")', re.IGNORECASE)
# Known session-bound meta tags, plus any meta whose name looks like a
# token/nonce/hmac/session value (e.g. fetch-nonce), so new ones are caught too.
_CSRF_META = re.compile(r'(<meta\b[^>]*name="(?:csrf-token|request-id|html-safe-nonce|'
                        r'visitor-payload|visitor-hmac|user-login|octolytics-[\w-]+|'
                        r'[\w:-]*(?:nonce|token|hmac|csrf|session|payload)[\w:-]*)"'
                        r'[^>]*content=")[^"]*(")', re.IGNORECASE)


# Numeric account / private-repo ids: avatar URLs (/u/<user id>) and
# hovercard tags ("repository:<id>", "user:<id>").
_AVATAR_ID = re.compile(r"(avatars\.githubusercontent\.com/u/)\d+")
_SUBJECT_ID = re.compile(r"\b((?:repository|user|organization):)\d+")
_ID_ATTR = re.compile(r'(\bdata-[\w-]*(?:repository|user|owner|org)[\w-]*-id=")\d+(")', re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def scrub_html(html: str) -> str:
    html = _HIDDEN_VALUE.sub(rf"\1{MASK}\2", html)
    html = _HIDDEN_VALUE_REV.sub(rf"\1{MASK}\2", html)
    html = _ANY_INPUT_VALUE.sub(rf"\1{MASK}\2", html)
    html = _CSRF_META.sub(rf"\1{MASK}\2", html)
    html = _EMAIL.sub(MASK, html)
    html = _AVATAR_ID.sub(rf"\1{MASK}", html)
    html = _SUBJECT_ID.sub(rf"\1{MASK}", html)
    html = _ID_ATTR.sub(rf"\1{MASK}\2", html)
    return redactor.redact(html)


def _root_cause(exc: BaseException) -> BaseException:
    seen = set()
    while exc.__cause__ is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ResolveError):
            break
        exc = exc.__cause__
    return exc


async def write_bundle(out: Path, *, page: Page | None, console: ConsoleLog | None,
                       stage: str | None, error: BaseException, retry_events: list[dict],
                       identifiers: list[str]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    cause = _root_cause(error)
    attempts = [a.as_dict() for a in cause.attempts] if isinstance(cause, ResolveError) else []

    url = ""
    if page is not None and not page.is_closed():
        url = page.url
        try:
            masks = [page.locator("input, textarea, img.avatar, img[alt^='@'], "
                                  "img[class*='Avatar'], [data-testid='github-avatar']")]
            for ident in identifiers:
                masks.append(page.get_by_text(re.compile(re.escape(ident), re.IGNORECASE)))
            await page.screenshot(path=str(out / "screenshot.png"), full_page=True,
                                  mask=masks, mask_color="#000000")
        except Exception as exc:  # noqa: BLE001 - evidence must never mask the failure
            log.warning("Could not take screenshot: %s", exc)
        try:
            (out / "page.html").write_text(scrub_html(await page.content()), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save page HTML: %s", exc)

    (out / "url.txt").write_text(redactor.redact(url) + "\n", encoding="utf-8")
    lines = console.lines if console else []
    (out / "console.log").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    failure = {
        "stage": stage,
        "error": type(error).__name__,
        "message": str(error),
        "root_cause": type(cause).__name__,
        "retryable": bool(getattr(cause, "retryable", False)),
        "url": url,
        "retry_history": retry_events,
        "resolver": {"target": cause.target.key, "attempts": attempts}
        if isinstance(cause, ResolveError) else None,
    }
    text = redactor.redact(json.dumps(failure, indent=2, default=str))
    (out / "failure.json").write_text(text + "\n", encoding="utf-8")
    log.error("Evidence bundle written to %s", out)
    return out
