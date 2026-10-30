"""Second-factor code providers.

The flow only depends on the `CodeProvider` protocol:

    async def get_code(challenge: Challenge) -> str

so a TOTP secret, an IMAP poll or an SMS-forwarding inbox are interchangeable.
Codes are registered with the redactor and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import pyotp

from .redact import redactor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Challenge:
    """What the portal is asking for."""

    kind: str = "totp"  # totp | email | sms
    attempt: int = 1  # 2 means the previous code was rejected
    portal: str = ""


class CodeProvider(Protocol):
    async def get_code(self, challenge: Challenge) -> str: ...


class TotpProvider:
    """RFC 6238 codes from a base32 secret.

    - If the current 30 s window is about to end, waits for the next one so the
      code does not expire on the way to the server.
    - Never returns the same code twice (GitHub rejects a code that was already
      used, and a rejected one won't start working): waits for the next window
      instead. This also covers a whole login being retried within one window.
    """

    def __init__(self, secret: str, *, min_validity: float = 3.0, clock=time.time, sleep=None):
        self._totp = pyotp.TOTP(secret)
        self._min_validity = min_validity
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._last_code: str | None = None

    def _seconds_left(self) -> float:
        interval = self._totp.interval
        return interval - (self._clock() % interval)

    def peek(self) -> str:
        """The current code, without using it up (for the wrong-code simulator)."""
        return self._totp.at(self._clock())

    async def get_code(self, challenge: Challenge) -> str:
        left = self._seconds_left()
        if left < self._min_validity:
            log.info("TOTP window ends in %.1fs; waiting for the next one", left)
            await self._sleep(left + 0.5)

        code = self._totp.at(self._clock())
        if code == self._last_code:
            left = self._seconds_left()
            log.info("Code for this window was already used; waiting %.1fs for a fresh one",
                     left)
            await self._sleep(left + 0.5)
            code = self._totp.at(self._clock())

        self._last_code = code
        redactor.add(code)
        log.info("Generated %s code (attempt %d)", challenge.kind, challenge.attempt)
        return code


class WrongFirstCodeProvider:
    """Wraps a provider and returns a wrong code on the first attempt.

    Used by `--simulate-wrong-code` to demonstrate the retry path live.
    """

    def __init__(self, inner: TotpProvider):
        self._inner = inner

    async def get_code(self, challenge: Challenge) -> str:
        if challenge.attempt == 1:
            # Peek instead of get_code: the real code stays unused, so attempt 2
            # can send it straight away instead of waiting for the next window.
            real = self._inner.peek()
            wrong = f"{(int(real) + 1) % 1_000_000:06d}"
            redactor.add(wrong)
            log.info("Simulating a wrong code on attempt 1")
            return wrong
        return await self._inner.get_code(challenge)
