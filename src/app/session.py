"""Saved browser state (cookies + localStorage).

Saving is done right after a successful login. Loading and dead-session
detection live here too so the flow only asks "is this state usable?".
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from playwright.async_api import BrowserContext

from .fsutil import private_dir

log = logging.getLogger(__name__)

_SAME_SITE = {"Strict", "Lax", "None"}


async def save_state(context: BrowserContext, path: Path) -> None:
    private_dir(path.parent)  # also tightens a state/ created as 0755 earlier
    state = await context.storage_state()
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    # Created owner-only from the start: cookies are credentials, so there is
    # no moment where the file is readable by others.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    tmp.replace(path)  # atomic: a crash never leaves a half-written file
    log.info("Saved session state to %s", path)


def _check_shape(data: object) -> None:
    """Raise TypeError unless `data` is storage state Playwright will accept.

    Mirrors Playwright's own validation, so a file that parses as JSON but has
    the wrong shape is treated as absent instead of crashing the browser launch.
    """
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        raise TypeError("not a storage state object")
    for c in data["cookies"]:
        if not isinstance(c, dict) or not all(
                isinstance(c.get(k), str) for k in ("name", "value", "domain", "path")):
            raise TypeError("malformed cookie")
        if "expires" in c and (isinstance(c["expires"], bool)
                               or not isinstance(c["expires"], int | float)):
            raise TypeError("malformed cookie expiry")
        if any(k in c and not isinstance(c[k], bool) for k in ("httpOnly", "secure")):
            raise TypeError("malformed cookie flags")
        if "sameSite" in c and c["sameSite"] not in _SAME_SITE:
            raise TypeError("malformed cookie sameSite")
    origins = data.get("origins", [])
    if not isinstance(origins, list):
        raise TypeError("malformed origins")
    for o in origins:
        if not isinstance(o, dict) or not isinstance(o.get("origin"), str):
            raise TypeError("malformed origin")
        items = o.get("localStorage", [])
        if not isinstance(items, list) or not all(
                isinstance(i, dict) and isinstance(i.get("name"), str)
                and isinstance(i.get("value"), str) for i in items):
            raise TypeError("malformed localStorage")


def load_state(path: Path) -> Path | None:
    """Return the path if it holds usable storage state, else None.

    A file that is missing, unreadable, not JSON or not shaped like storage
    state is treated as absent, so the run falls back to a full login instead
    of crashing.
    """
    if not path.exists():
        log.info("No saved session at %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _check_shape(data)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Saved session at %s is unreadable (%s); ignoring it", path, exc)
        return None
    log.info("Loaded saved session from %s (%d cookies)", path, len(data["cookies"]))
    return path


def discard_state(path: Path) -> None:
    if path.exists():
        path.unlink()
        log.info("Discarded dead session state %s", path)
