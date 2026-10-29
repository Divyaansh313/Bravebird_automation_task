"""Run manifest: inputs, pages visited, stage timings, captured files.

Written on every run, successful or not, as runs/<id>/manifest.json.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .redact import redactor
from .stages import StageTracker


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PageVisit:
    url: str
    at: str


@dataclass
class CapturedFile:
    path: str
    name: str
    bytes: int
    sha256: str
    method: str  # native_download | new_tab | print_pdf
    source_url: str
    expected_sha256: str | None = None

    @property
    def verified(self) -> bool | None:
        return None if self.expected_sha256 is None else self.sha256 == self.expected_sha256


@dataclass
class Manifest:
    run_id: str
    portal: str
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    status: str = "running"  # ok | failed
    session: str | None = None  # reused | full_login | full_login_after_dead_session
    inputs: dict = field(default_factory=dict)
    pages_visited: list[PageVisit] = field(default_factory=list)
    form: dict = field(default_factory=dict)
    files: list[CapturedFile] = field(default_factory=list)
    retries: list[dict] = field(default_factory=list)
    error: dict | None = None

    def visit(self, url: str) -> None:
        if not self.pages_visited or self.pages_visited[-1].url != url:
            self.pages_visited.append(PageVisit(url, now_iso()))

    def write(self, path: Path, stages: StageTracker) -> None:
        data = asdict(self)
        data["files"] = [{**asdict(f), "verified": f.verified} for f in self.files]
        data["stages"] = [
            {"key": r.key, "status": r.status, "seconds": r.seconds, "note": r.note}
            for r in stages.records
        ]
        data["total_seconds"] = round(sum(r.seconds or 0 for r in stages.records), 3)
        path.write_text(redactor.redact(json.dumps(data, indent=2, default=str)) + "\n",
                        encoding="utf-8")
