"""Command-line entry points.

    uv run bravebird --input inputs/github_v1.json [--headed] [--simulate-wrong-code]
                     [--break-step TARGET_KEY]
    uv run bravebird-batch inputs/github_v1.json inputs/github_v2.json [--concurrency 2]

Exit codes: 0 ok, 1 automation failure, 2 configuration error, 3 needs a human
(CAPTCHA). For a batch, the worst exit code of all runs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from .config import ConfigError, load_credentials, load_input
from .redact import configure_logging
from .runner import BREAKABLE, RunOptions, execute

log = logging.getLogger("bravebird")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env-file", default=".env", help="Credentials file (default: .env)")
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    p.add_argument("--simulate-wrong-code", action="store_true",
                   help="Send a wrong 2FA code first to exercise the retry path")
    p.add_argument("--break-step", metavar="TARGET_KEY", choices=BREAKABLE,
                   help="Make one element impossible to find, to exercise the failure path. "
                        f"One of: {', '.join(BREAKABLE)}")
    p.add_argument("-v", "--verbose", action="store_true")


def _options(a: argparse.Namespace) -> RunOptions:
    return RunOptions(headed=a.headed, simulate_wrong_code=a.simulate_wrong_code,
                      break_step=a.break_step)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravebird", description="Run one portal job.")
    p.add_argument("--input", required=True, help="Path to the run input JSON")
    _common(p)
    a = p.parse_args(argv)
    configure_logging(logging.DEBUG if a.verbose else logging.INFO)
    try:
        inputs = load_input(a.input)
        creds = load_credentials(a.env_file)
        result = asyncio.run(execute(inputs, creds, _options(a)))
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    return result.exit_code


async def _batch(paths: list[str], concurrency: int, a: argparse.Namespace) -> int:
    creds = load_credentials(a.env_file)
    inputs = [load_input(path) for path in paths]  # validate all before starting any
    sem = asyncio.Semaphore(concurrency)

    async def one(path: str, inp):
        async with sem:
            return path, await execute(inp, creds, _options(a))

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one(path, inp) for path, inp in zip(paths, inputs,
                                                                          strict=True)))
    wall = time.perf_counter() - t0
    print(f"\n{'input':<28} {'status':<12} {'session':<30} files")
    for path, r in results:
        print(f"{path:<28} {r.status:<12} {r.session or '-':<30} {len(r.files)}  ({r.run_dir})")
    print(f"\n{len(results)} run(s), concurrency {concurrency}, wall time {wall:.1f}s")
    return max(r.exit_code for _, r in results)


def batch_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravebird-batch",
                                description="Run several input files at once.")
    p.add_argument("inputs", nargs="+", help="Input JSON files")
    p.add_argument("--concurrency", type=int, default=2, help="Parallel browsers (default 2)")
    _common(p)
    a = p.parse_args(argv)
    configure_logging(logging.DEBUG if a.verbose else logging.INFO, tag_runs=True)
    try:
        return asyncio.run(_batch(a.inputs, max(1, a.concurrency), a))
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
