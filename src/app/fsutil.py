"""Owner-only directories for everything sensitive the runner writes.

state/ holds the session cookies, the lock file and the job database (plus its
SQLite -wal/-shm side files, which are created later with default
permissions); runs/ holds downloaded private documents and evidence. Making
the directories 0700 protects every file inside them, whatever mode the file
itself was created with. Existing directories are tightened too, so a state/
created by an older version (0755) is fixed on the next run.
"""

from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path) -> Path:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if (path.stat().st_mode & 0o777) != DIR_MODE:
        os.chmod(path, DIR_MODE)
    return path


def private_file(path: Path) -> Path:
    os.chmod(path, FILE_MODE)
    return path
