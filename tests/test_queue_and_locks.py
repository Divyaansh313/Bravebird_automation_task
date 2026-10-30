import asyncio
import logging

from app.config import Credentials, RunInput
from app.jobs import JobQueue, JobStore
from app.locks import AccountLock
from app.redact import attach_run_log, current_run, detach_run_log
from app.runner import RunOptions, RunResult

CREDS = Credentials(username="u-queue", password="p", totp_secret="JBSWY3DPEHPK3PXP")


def run_input(portal: str, tmp_path) -> RunInput:
    # Only "github" is implemented; a different host stands in for another portal.
    return RunInput.model_validate({
        "base_url": f"https://{portal}.example", "repo": "o/r",
        "state_dir": str(tmp_path / "state"),
        "output_dir": str(tmp_path / "runs"),
        "form": {"tag": "v1", "title": "t"},
        "document": {"release_tag": "v1", "asset_name": "a.pdf"}})


async def test_workers_run_portals_in_parallel_with_a_per_portal_cap(tmp_path):
    running: dict[str, int] = {}
    peak: dict[str, int] = {}
    overall = {"now": 0, "peak": 0}

    async def executor(inputs, creds, options):
        portal = inputs.base_url.split("//")[1].split(".")[0]
        running[portal] = running.get(portal, 0) + 1
        overall["now"] += 1
        peak[portal] = max(peak.get(portal, 0), running[portal])
        overall["peak"] = max(overall["peak"], overall["now"])
        await asyncio.sleep(0.2)
        running[portal] -= 1
        overall["now"] -= 1
        return RunResult(options.run_id, "ok", 0, tmp_path, tmp_path / "m.json")

    store = JobStore(tmp_path / "jobs.db")
    queue = JobQueue(store, CREDS, workers=3, per_portal=1, executor=executor, poll=0.05)
    await queue.start()
    for portal in ("alpha", "alpha", "alpha", "beta", "beta"):
        queue.submit(run_input(portal, tmp_path), RunOptions())
    for _ in range(100):
        if store.counts() == {"ok": 5}:
            break
        await asyncio.sleep(0.05)
    await queue.stop()
    assert store.counts() == {"ok": 5}
    assert peak == {"alpha": 1, "beta": 1}  # per-portal cap respected
    assert overall["peak"] == 2  # but the two portals ran side by side


async def test_running_jobs_are_requeued_after_a_crash(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create(run_input("alpha", tmp_path), RunOptions())
    assert store.claim_next(lambda _k: False).id == job.id  # now "running"
    store.close()

    reopened = JobStore(tmp_path / "jobs.db")  # simulated restart
    assert reopened.recover() == 1
    again = reopened.get(job.id)
    assert again.status == "queued" and again.note == "requeued after restart"


async def test_crashing_executor_marks_job_failed_and_worker_survives(tmp_path):
    calls = []

    async def executor(inputs, creds, options):
        calls.append(options.run_id)
        if len(calls) == 1:
            raise RuntimeError("boom with u-queue inside")
        return RunResult(options.run_id, "ok", 0, tmp_path, tmp_path / "m.json")

    store = JobStore(tmp_path / "jobs.db")
    queue = JobQueue(store, CREDS, workers=1, executor=executor, poll=0.05)
    await queue.start()
    a = queue.submit(run_input("alpha", tmp_path), RunOptions())
    b = queue.submit(run_input("alpha", tmp_path), RunOptions())
    for _ in range(100):
        if store.get(b.id).status == "ok":
            break
        await asyncio.sleep(0.05)
    await queue.stop()
    assert store.get(a.id).status == "failed"
    assert store.get(b.id).status == "ok"


async def test_account_lock_serialises_same_account_only(tmp_path):
    order: list[str] = []

    async def hold(path, name, seconds):
        lock = AccountLock(path)
        await lock.acquire()
        order.append(f"{name}+")
        await asyncio.sleep(seconds)
        order.append(f"{name}-")
        lock.release()
        lock.release()  # idempotent

    same = tmp_path / "state/github.storage.json"
    await asyncio.gather(hold(same, "a", 0.2), hold(same, "b", 0.01))
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])

    order.clear()
    other = tmp_path / "state/other.storage.json"
    await asyncio.gather(hold(same, "a", 0.2), hold(other, "b", 0.01))
    assert order[:2] == ["a+", "b+"]  # different accounts don't wait for each other


async def test_per_run_log_files_do_not_mix(tmp_path):
    log = logging.getLogger("bravebird.test")
    logging.getLogger().setLevel(logging.INFO)

    async def run(run_id: str):
        token = current_run.set(run_id)
        handler = attach_run_log(str(tmp_path / f"{run_id}.log"), run_id)
        try:
            for n in range(5):
                log.info("hello from %s #%d", run_id, n)
                await asyncio.sleep(0.01)
        finally:
            detach_run_log(handler)
            current_run.reset(token)

    await asyncio.gather(run("run-a"), run("run-b"))
    a = (tmp_path / "run-a.log").read_text()
    b = (tmp_path / "run-b.log").read_text()
    assert a.count("run-a") == 5 and "run-b" not in a
    assert b.count("run-b") == 5 and "run-a" not in b


async def test_job_whose_stored_input_no_longer_validates_fails_cleanly(tmp_path):
    async def executor(inputs, creds, options):
        return RunResult(options.run_id, "ok", 0, tmp_path, tmp_path / "m.json")

    store = JobStore(tmp_path / "jobs.db")
    bad = store.create(run_input("alpha", tmp_path), RunOptions())
    with store._lock:  # e.g. written by an older version with a different schema
        store._db.execute("UPDATE jobs SET input_json = '{}' WHERE id = ?", (bad.id,))
    queue = JobQueue(store, CREDS, workers=1, executor=executor, poll=0.05)
    await queue.start()
    good = queue.submit(run_input("alpha", tmp_path), RunOptions())
    for _ in range(100):
        if store.get(good.id).status == "ok":
            break
        await asyncio.sleep(0.05)
    await queue.stop()
    assert store.get(bad.id).status == "failed"
    assert store.get(good.id).status == "ok"  # the worker survived
