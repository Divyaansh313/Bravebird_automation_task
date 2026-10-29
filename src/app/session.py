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

log = logging.getLogger(__name__)


async def save_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    await context.storage_state(path=str(tmp))
    os.chmod(tmp, 0o600)  # cookies are credentials
    tmp.replace(path)  # atomic: a crash never leaves a half-written file
    log.info("Saved session state to %s", path)


def load_state(path: Path) -> Path | None:
    """Return the path if it holds parseable storage state, else None.

    A file that is missing, unreadable or not valid state JSON is treated as
    absent, so the run falls back to a full login instead of crashing.
    """
    if not path.exists():
        log.info("No saved session at %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
            raise TypeError("not a storage state object")
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Saved session at %s is unreadable (%s); ignoring it", path, exc)
        return None
    log.info("Loaded saved session from %s (%d cookies)", path, len(data["cookies"]))
    return path


def discard_state(path: Path) -> None:
    if path.exists():
        path.unlink()
        log.info("Discarded dead session state %s", path)
