"""Lock timeout, owner-only output directories, platform version."""

import asyncio
import fcntl
import os
import stat

import pytest

from app.fingerprint import platform_version
from app.fsutil import private_dir
from app.jobs import JobStore
from app.locks import AccountLock, LockTimeout
from app.session import save_state


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


async def test_lock_wait_is_bounded_within_a_process(tmp_path):
    path = tmp_path / "state/github.storage.json"
    holder = AccountLock(path, timeout=5)
    await holder.acquire()
    with pytest.raises(LockTimeout):
        await AccountLock(path, timeout=0.3, poll=0.05).acquire()
    holder.release()
    late = AccountLock(path, timeout=1)
    await late.acquire()  # free again once released
    late.release()


async def test_lock_wait_is_bounded_across_processes(tmp_path):
    path = tmp_path / "state/github.storage.json"
    path.parent.mkdir(parents=True)
    # Another process holding the OS lock: a separate open file description.
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lock = AccountLock(path, timeout=0.3, poll=0.05)
        with pytest.raises(LockTimeout) as err:
            await lock.acquire()
        assert err.value.retryable
        assert not lock.held
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def test_lock_wait_can_be_cancelled(tmp_path):
    path = tmp_path / "state/github.storage.json"
    holder = AccountLock(path, timeout=5)
    await holder.acquire()
    waiter = asyncio.create_task(AccountLock(path, timeout=30).acquire())
    await asyncio.sleep(0.1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.release()


def test_private_dir_creates_and_tightens(tmp_path):
    fresh = private_dir(tmp_path / "a/b")
    assert mode(fresh) == 0o700
    old = tmp_path / "old-state"
    old.mkdir(mode=0o755)
    os.chmod(old, 0o755)
    private_dir(old)
    assert mode(old) == 0o700  # a 0755 dir from an older version gets fixed


async def test_session_and_jobs_dirs_are_owner_only(tmp_path):
    class FakeContext:
        async def storage_state(self):
            return {"cookies": [], "origins": []}

    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    os.chmod(state, 0o755)
    await save_state(FakeContext(), state / "github.storage.json")
    assert mode(state) == 0o700
    assert mode(state / "github.storage.json") == 0o600

    JobStore(tmp_path / "queue/jobs.db").close()
    assert mode(tmp_path / "queue") == 0o700


def test_platform_version_shapes():
    mac = platform_version("macOS")
    assert len(mac.split(".")) == 3 and all(p.isdigit() for p in mac.split("."))
    assert platform_version("Windows") in ("10.0.0", "15.0.0")
    assert platform_version("Linux") == ""
