"""Durable job queue in front of the runner.

- Jobs live in SQLite (stdlib), so a restart loses nothing: jobs that were
  running when the process died go back to "queued" on start-up.
- N workers pull jobs in submission order (FIFO by insertion rowid). A
  per-portal limit stops one portal from taking every worker, so several
  portals run side by side fairly.
- Each job runs `runner.execute()` in its own asyncio task; logging, the
  break-step setting and the account lock are per run, so jobs don't interfere.
- Job id == run id == run directory name.

Statuses: queued -> running -> ok | failed | needs_human ; queued -> cancelled
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Credentials, RunInput
from .fsutil import private_dir
from .redact import redactor
from .runner import RunOptions, RunResult, execute, new_run_id

log = logging.getLogger(__name__)

TERMINAL = {"ok", "failed", "needs_human", "cancelled"}
Executor = Callable[[RunInput, Credentials, RunOptions], Awaitable[RunResult]]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    status: str
    portal_key: str
    label: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    input: dict
    options: dict
    result: dict | None
    note: str | None

    def public(self) -> dict:
        """API view. Everything leaving the process goes through the redactor."""
        return json.loads(redactor.redact_json(asdict(self)))


class JobStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        portal_key TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        input_json TEXT NOT NULL,
        options_json TEXT NOT NULL,
        result_json TEXT,
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
    """

    def __init__(self, path: Path):
        private_dir(path.parent)  # also covers the SQLite -wal/-shm side files
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(self.SCHEMA)
        self._lock = threading.Lock()
        path.chmod(0o600)

    def _row(self, r: sqlite3.Row) -> Job:
        return Job(id=r["id"], status=r["status"], portal_key=r["portal_key"], label=r["label"],
                   created_at=r["created_at"], started_at=r["started_at"],
                   finished_at=r["finished_at"], input=json.loads(r["input_json"]),
                   options=json.loads(r["options_json"]),
                   result=json.loads(r["result_json"]) if r["result_json"] else None,
                   note=r["note"])

    def create(self, inputs: RunInput, options: RunOptions, label: str | None = None) -> Job:
        job_id = new_run_id()
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, status, portal_key, label, created_at, input_json,"
                " options_json) VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (job_id, portal_key(inputs), label, _now(),
                 inputs.model_dump_json(), json.dumps(asdict(options))))
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
            return {r["status"]: r["n"] for r in rows}

    def claim_next(self, portal_busy: Callable[[str], bool]) -> Job | None:
        """Atomically move the oldest runnable queued job to running."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY rowid"
            ).fetchall()
            for r in rows:
                if portal_busy(r["portal_key"]):
                    continue
                cur = self._db.execute(
                    "UPDATE jobs SET status = 'running', started_at = ? "
                    "WHERE id = ? AND status = 'queued'", (_now(), r["id"]))
                if cur.rowcount == 1:
                    return self._row(self._db.execute(
                        "SELECT * FROM jobs WHERE id = ?", (r["id"],)).fetchone())
        return None

    def finish(self, job_id: str, status: str, result: dict | None, note: str | None = None):
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, result_json = ?, note = ? "
                "WHERE id = ?",
                (status, _now(), json.dumps(result, default=str) if result else None, note,
                 job_id))

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ?, note = 'cancelled' "
                "WHERE id = ? AND status = 'queued'", (_now(), job_id))
        return cur.rowcount == 1

    def recover(self) -> int:
        """Jobs left 'running' by a crash or restart go back to the queue."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL, "
                "note = 'requeued after restart' WHERE status = 'running'")
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()


def portal_key(inputs: RunInput) -> str:
    """Jobs with the same key share an account and its saved session."""
    return f"{inputs.portal}|{inputs.base_url}|{inputs.state_path}"


class JobQueue:
    def __init__(self, store: JobStore, creds: Credentials, *, workers: int = 2,
                 per_portal: int = 2, executor: Executor = execute, poll: float = 1.0):
        self.store = store
        self.creds = creds
        self.workers = max(1, workers)
        self.per_portal = max(1, per_portal)
        self.executor = executor
        self.poll = poll
        self._running: Counter[str] = Counter()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._claim_lock = asyncio.Lock()

    def _portal_busy(self, key: str) -> bool:
        return self._running[key] >= self.per_portal

    async def start(self) -> None:
        if n := self.store.recover():
            log.warning("Re-queued %d job(s) that were running at shutdown", n)
        self._tasks = [asyncio.create_task(self._worker(i), name=f"worker-{i}")
                       for i in range(self.workers)]
        log.info("Job queue started: %d worker(s), %d per portal", self.workers,
                 self.per_portal)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def submit(self, inputs: RunInput, options: RunOptions, label: str | None = None) -> Job:
        job = self.store.create(inputs, options, label)
        self._wake.set()
        log.info("Queued job %s (%s)", job.id, label or inputs.portal)
        return job

    async def _worker(self, n: int) -> None:
        while True:
            async with self._claim_lock:
                job = self.store.claim_next(self._portal_busy)
                if job:
                    self._running[job.portal_key] += 1
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll)
                except TimeoutError:
                    pass
                continue
            try:
                await self._run(job)
            finally:
                self._running[job.portal_key] -= 1
                self._wake.set()  # a portal slot freed up

    async def _run(self, job: Job) -> None:
        try:
            # Inside the try: a stored job that no longer validates must fail
            # that job, not kill the worker and leave the job "running".
            inputs = RunInput.model_validate(job.input)
            options = RunOptions(**{**job.options, "run_id": job.id})
            result = await self.executor(inputs, self.creds, options)
        except asyncio.CancelledError:
            self.store.finish(job.id, "queued", None, "interrupted; will rerun")
            raise
        except Exception as exc:
            log.exception("Job %s crashed", job.id)
            self.store.finish(job.id, "failed", {"error": {"type": type(exc).__name__,
                                                           "message": redactor.redact(str(exc))}})
            return
        self.store.finish(job.id, result.status, {
            "status": result.status, "exit_code": result.exit_code,
            "session": result.session, "run_dir": str(result.run_dir),
            "files": [Path(f).name for f in result.files], "error": result.error,
        })
        log.info("Job %s finished: %s", job.id, result.status)
