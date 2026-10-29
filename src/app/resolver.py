"""Resilient element resolution.

A `Target` describes an element several independent ways. The resolver tries
them in a fixed order, most semantic first:

    1. role + accessible name   (what assistive tech sees; survives markup churn)
    2. label / placeholder      (what the user reads next to a field)
    3. nearby text              (visible text followed by the element)
    4. structure                (position in the form, attribute shape)
    5. id / class               (last resort; the most volatile)

Every frame on the page is searched, main frame first, so an element that
moves into an iframe is still found. Each strategy's outcome is recorded; the
winner is logged and the full attempt list goes into failure evidence.

Nothing here talks to the network, so it runs offline against saved HTML.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from .errors import RetryableError

log = logging.getLogger(__name__)

TextMatch = str | re.Pattern[str]

STRATEGY_ORDER = ("role", "label", "near_text", "structure", "css")

# Target keys to sabotage (set by --break-step). Every strategy for these keys
# looks for something that cannot exist, so the failure path runs for real.
BROKEN_TARGETS: set[str] = set()


@dataclass(frozen=True)
class Target:
    key: str  # stable name used in logs and evidence, e.g. "login.password"
    role: str | None = None
    name: TextMatch | None = None  # accessible name for the role strategy
    exact: bool = False
    labels: tuple[TextMatch, ...] = ()  # <label> text or placeholder
    near_text: tuple[str, ...] = ()  # visible text that precedes the element
    near_tag: str = "input"  # element to look for after near_text
    structure: tuple[str, ...] = ()  # structural CSS: "form input[type=password]"
    css: tuple[str, ...] = ()  # id/class fallbacks: "#password"


@dataclass
class Attempt:
    strategy: str
    query: str
    frame: str
    result: str  # "unique" | "none" | "ambiguous(n)" | "error: ..."

    def as_dict(self) -> dict[str, str]:
        return {"strategy": self.strategy, "query": self.query, "frame": self.frame,
                "result": self.result}


@dataclass
class Resolved:
    locator: Locator
    strategy: str
    query: str
    frame: str
    attempts: list[Attempt] = field(default_factory=list)


class ResolveError(RetryableError):
    """Retryable: the element may appear after a reload."""

    def __init__(self, target: Target, attempts: list[Attempt]):
        self.target = target
        self.attempts = attempts
        super().__init__(f"Could not resolve {target.key!r} after {len(attempts)} attempts")


def _xpath_literal(text: str) -> str:
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    parts = text.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"


def _describe(match: TextMatch) -> str:
    return f"/{match.pattern}/" if isinstance(match, re.Pattern) else repr(match)


Factory = Callable[[Frame], Locator]


def _candidates(t: Target) -> list[tuple[str, str, Factory]]:
    out: list[tuple[str, str, Factory]] = []
    if t.role:
        def by_role(f: Frame, t=t) -> Locator:
            if t.name is None:
                return f.get_by_role(t.role)  # type: ignore[arg-type]
            return f.get_by_role(t.role, name=t.name, exact=t.exact)  # type: ignore[arg-type]
        q = f"role={t.role}" + (f" name={_describe(t.name)}" if t.name is not None else "")
        out.append(("role", q, by_role))
    for label in t.labels:
        out.append(("label", f"label={_describe(label)}",
                    lambda f, v=label: f.get_by_label(v)))
        out.append(("label", f"placeholder={_describe(label)}",
                    lambda f, v=label: f.get_by_placeholder(v)))
    for text in t.near_text:
        lit = _xpath_literal(text)
        xpath = (f"xpath=//*[not(self::script) and not(self::style)]"
                 f"[contains(normalize-space(.), {lit}) and not(*[contains(normalize-space(.), {lit})])]"
                 f"/following::{t.near_tag}[not(@type='hidden') and not(@hidden)][1]")
        out.append(("near_text", f"near {text!r} -> {t.near_tag}", lambda f, x=xpath: f.locator(x)))
    for sel in t.structure:
        out.append(("structure", sel, lambda f, s=sel: f.locator(s)))
    for sel in t.css:
        out.append(("css", sel, lambda f, s=sel: f.locator(s)))
    return out


def _frame_name(frame: Frame) -> str:
    if frame.parent_frame is None:
        return "main"
    return frame.name or frame.url or "iframe"


async def _try(factory: Factory, frame: Frame) -> tuple[str, Locator | None]:
    try:
        loc = factory(frame).filter(visible=True)
        n = await loc.count()
    except PlaywrightError as exc:
        return f"error: {str(exc).splitlines()[0][:120]}", None
    if n == 1:
        return "unique", loc
    if n == 0:
        return "none", None
    return f"ambiguous({n})", loc.first


async def resolve(page: Page, target: Target, *, timeout: float = 10.0,
                  poll: float = 0.25) -> Resolved:
    """Find exactly one visible element for `target`, or raise ResolveError.

    Polls until `timeout` because elements may still be rendering. A strategy
    that matches several elements is recorded but only used if no strategy
    finds a unique match by the deadline.
    """
    if target.key in BROKEN_TARGETS:
        target = _sabotaged(target)
    candidates = _candidates(target)
    deadline = time.monotonic() + timeout
    while True:
        attempts: list[Attempt] = []
        fallback: Resolved | None = None
        for frame in page.frames:  # main frame first, then iframes
            fname = _frame_name(frame)
            for strategy, query, factory in candidates:
                result, loc = await _try(factory, frame)
                attempts.append(Attempt(strategy, query, fname, result))
                if result == "unique" and loc is not None:
                    _log_win(target, strategy, query, fname, attempts)
                    return Resolved(loc, strategy, query, fname, attempts)
                if result.startswith("ambiguous") and fallback is None and loc is not None:
                    fallback = Resolved(loc, strategy, query, fname, [])
        if time.monotonic() >= deadline:
            if fallback is not None:
                fallback.attempts = attempts
                log.warning("resolved %s via %s %s [%s] (ambiguous, took first)",
                            target.key, fallback.strategy, fallback.query, fallback.frame)
                return fallback
            raise ResolveError(target, attempts)
        await asyncio.sleep(poll)


def _sabotaged(t: Target) -> Target:
    nope = "__bravebird_broken__"
    return Target(key=t.key, role=t.role or "button", name=nope, exact=True, labels=(nope,),
                  near_text=(nope,), near_tag=t.near_tag, structure=(f"form #{nope}",),
                  css=(f"#{nope}",))


def _log_win(target: Target, strategy: str, query: str, frame: str,
             attempts: list[Attempt]) -> None:
    tried = len(attempts) - 1
    log.info("resolved %s via %s %s [%s]%s", target.key, strategy, query, frame,
             f" after {tried} failed attempt(s)" if tried else "")


async def exists(page: Page, target: Target, *, timeout: float = 0.0) -> bool:
    try:
        await resolve(page, target, timeout=timeout)
        return True
    except ResolveError:
        return False
