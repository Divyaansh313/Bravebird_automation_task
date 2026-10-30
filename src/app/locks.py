"""Per-account authentication lock.

Several jobs for the same account may run at once, but only one at a time may
check, refresh or write its saved session:

- two simultaneous logins would submit the same TOTP code, and GitHub rejects
  a code that was already used;
- two writers would race on the state file.

The lock is held from "load saved state" until the session is confirmed or
re-saved, then released so the rest of the jobs run in parallel. The job that
waited then loads the freshly saved state and skips the login entirely.

Works within one process (asyncio.Lock) and across processes (flock on a
sidecar .lock file), so a CLI run and the API server can share an account.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from pathlib import Path

from .errors import RetryableError
from .fsutil import private_dir

log = logging.getLogger(__name__)

# One asyncio.Lock per (event loop, state file): an asyncio.Lock belongs to the
# loop that first waits on it.
_process_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _process_lock(path: Path) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), str(path.resolve()))
    return _process_locks.setdefault(key, asyncio.Lock())


DEFAULT_TIMEOUT = 300.0  # seconds; a login takes ~5 s, so this means something is stuck


class LockTimeout(RetryableError):
    """Another run held this account's lock for too long (stuck or very slow)."""

    def __init__(self, state_path: Path, timeout: float):
        super().__init__(f"Timed out after {timeout:.0f}s waiting for the account lock "
                         f"on {state_path.name}")


class AccountLock:
    def __init__(self, state_path: Path, *, timeout: float | None = None, poll: float = 0.2):
        self.state_path = state_path
        self.timeout = timeout if timeout is not None else float(
            os.getenv("BRAVEBIRD_LOCK_TIMEOUT", DEFAULT_TIMEOUT))
        self.poll = poll
        self.lock_path = state_path.with_suffix(".lock")
        self._async_lock: asyncio.Lock | None = None
        self._fd: int | None = None
        self.waited: float = 0.0

    @property
    def held(self) -> bool:
        return self._fd is not None

    async def acquire(self) -> None:
        """Wait for the account, at most `timeout` seconds (LockTimeout after that).

        The OS lock is polled non-blocking rather than waited on in a thread, so
        the wait is bounded and can be cancelled cleanly.
        """
        t0 = time.perf_counter()
        deadline = time.monotonic() + self.timeout
        self._async_lock = _process_lock(self.state_path)
        try:
            await asyncio.wait_for(self._async_lock.acquire(), timeout=self.timeout)
        except TimeoutError:
            raise LockTimeout(self.state_path, self.timeout) from None
        try:
            private_dir(self.lock_path.parent)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:  # another process holds it
                        if time.monotonic() >= deadline:
                            raise LockTimeout(self.state_path, self.timeout) from None
                        await asyncio.sleep(self.poll)
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        except BaseException:
            self._async_lock.release()
            raise
        self.waited = time.perf_counter() - t0
        if self.waited > 0.05:
            log.info("Waited %.1fs for the account lock (another run was signing in)",
                     self.waited)

    def release(self) -> None:
        """Idempotent: safe to call from both the happy path and a finally block."""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self._async_lock.release()
