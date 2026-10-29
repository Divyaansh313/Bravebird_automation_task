"""Stage timing. Each stage key also tells evidence bundles where a run failed."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class StageRecord:
    key: str
    started_at: float
    seconds: float | None = None
    status: str = "running"  # running | ok | failed | skipped
    note: str | None = None


@dataclass
class StageTracker:
    records: list[StageRecord] = field(default_factory=list)
    current: str | None = None

    @asynccontextmanager
    async def stage(self, key: str):
        rec = StageRecord(key, time.time())
        self.records.append(rec)
        self.current = key
        t0 = time.perf_counter()
        log.info("stage %s: start", key)
        try:
            yield rec
        except BaseException:
            rec.status = "failed"
            raise
        else:
            if rec.status == "running":
                rec.status = "ok"
        finally:
            rec.seconds = round(time.perf_counter() - t0, 3)
            log.info("stage %s: %s in %.2fs", key, rec.status, rec.seconds)
