"""Bravebird portal runner.

    uv run bravebird --input inputs/github_v1.json [--headed] [--simulate-wrong-code]
                     [--break-step TARGET_KEY]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from . import resolver
from .browser import BrowserSession, open_browser
from .config import ConfigError, Credentials, RunInput, load_credentials, load_input
from .errors import AutomationError
from .evidence import write_bundle
from .flows import github
from .manifest import CapturedFile, Manifest, now_iso, sha256_file
from .otp import CodeProvider, TotpProvider, WrongFirstCodeProvider
from .redact import configure_logging, redactor
from .retry import RetryLog, RetryPolicy, with_retry
from .session import discard_state, load_state, save_state
from .stages import StageTracker

log = logging.getLogger("bravebird")

BREAKABLE = sorted({
    "login.username", "login.password", "login.submit", "2fa.code",
    "nav.releases", "nav.draft_new", "form.tag.open", "form.tag.filter",
    "form.target.open", "form.title", "form.notes", "form.save_draft",
    "doc.release_link", "doc.asset_link", "cleanup.delete",
})


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bravebird", description=__doc__.splitlines()[0])
    p.add_argument("--input", required=True, help="Path to the run input JSON")
    p.add_argument("--env-file", default=".env", help="Credentials file (default: .env)")
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    p.add_argument("--simulate-wrong-code", action="store_true",
                   help="Send a wrong 2FA code first to exercise the retry path")
    p.add_argument("--break-step", metavar="TARGET_KEY", choices=BREAKABLE,
                   help="Make one element impossible to find, to exercise the failure path. "
                        f"One of: {', '.join(BREAKABLE)}")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


class Runner:
    def __init__(self, inputs: RunInput, creds: Credentials, provider: CodeProvider,
                 run_dir: Path):
        self.inputs = inputs
        self.creds = creds
        self.provider = provider
        self.run_dir = run_dir
        self.stages = StageTracker()
        self.retries = RetryLog()
        self.manifest = Manifest(run_id=run_dir.name, portal=inputs.portal,
                                 inputs=inputs.model_dump(mode="json"))
        self.session: BrowserSession | None = None
        self.identifiers: list[str] = [creds.username.get_secret_value()]

    async def _stage(self, key: str, fn, policy: RetryPolicy | None = None):
        async with self.stages.stage(key) as rec:
            result = await with_retry(key, fn, policy=policy, history=self.retries)
            attempts = sum(1 for e in self.retries.events if e["stage"] == key) + 1
            if attempts > 1:
                rec.note = f"succeeded on attempt {attempts}"
            return result

    def _track_pages(self, page: Page) -> None:
        def on_nav(frame) -> None:
            if frame == page.main_frame and frame.url.startswith("http"):
                self.manifest.visit(frame.url)
        page.on("framenavigated", on_nav)

    async def run(self) -> None:
        i = self.inputs
        state = load_state(i.state_path)
        async with open_browser(headless=i.headless, storage_state=state,
                                downloads=self.run_dir / "downloads") as session:
            self.session = session
            self._track_pages(session.page)
            try:
                await self._steps(session.page, had_state=state is not None)
            except AutomationError as exc:
                # Capture evidence while the page is still open.
                await write_bundle(
                    self.run_dir / "evidence", page=session.page, console=session.console,
                    stage=self.stages.current, error=exc, retry_events=self.retries.events,
                    identifiers=self.identifiers)
                await self._best_effort_cleanup(session.page)
                raise

    async def _best_effort_cleanup(self, page: Page) -> None:
        """After a failure, remove a draft this run already saved. Never raises."""
        if not self.manifest.form.get("draft_url") or self.inputs.form.keep_draft:
            return
        try:
            await github.delete_draft(page, self.inputs.repo_url, self.inputs.form.title,
                                      self.manifest.form["draft_url"])
            self.manifest.form["cleanup_after_failure"] = "deleted"
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the real failure
            self.manifest.form["cleanup_after_failure"] = f"failed: {type(exc).__name__}"
            log.warning("Could not delete the draft after failure: %s", exc)

    async def _steps(self, page: Page, *, had_state: bool) -> None:
        i = self.inputs
        await self._authenticate(page, had_state=had_state)

        await self._stage("nav.to_form", lambda _a: github.navigate_to_form(page, i.repo_url))

        async def fill(attempt: int) -> dict:
            if attempt > 1:  # start again from a clean form
                await page.goto(f"{i.repo_url}/releases/new", wait_until="domcontentloaded")
            return await github.fill_release_form(page, i.form)
        self.manifest.form = await self._stage("form.fill", fill)

        async def submit(_attempt: int) -> str:
            if "/releases/edit/" in page.url:  # a previous attempt already saved it
                return page.url
            return await github.save_draft(page)
        self.manifest.form["draft_url"] = await self._stage("form.submit", submit,
                                                            RetryPolicy(attempts=2))

        async def capture(_attempt: int) -> CapturedFile:
            path, expected, src = await github.capture_asset(
                page, i.repo_url, i.document.release_tag, i.document.asset_name,
                self.run_dir / "downloads")
            p = Path(path)
            return CapturedFile(path=str(p), name=p.name, bytes=p.stat().st_size,
                                sha256=sha256_file(p), method="native_download",
                                source_url=src, expected_sha256=expected)
        captured = await self._stage("document.capture", capture)
        self.manifest.files.append(captured)
        if captured.verified is False:
            log.warning("Downloaded file digest does not match the portal's digest")

        if not i.form.keep_draft:
            await self._stage("cleanup.draft",
                              lambda _a: github.delete_draft(page, i.repo_url, i.form.title,
                                                             self.manifest.form["draft_url"]))

    async def _authenticate(self, page: Page, *, had_state: bool) -> None:
        i = self.inputs
        if had_state:
            async with self.stages.stage("session.check") as rec:
                # Check on the private repo page we need next anyway: one navigation,
                # and two signals (signed-in user + private content visible).
                alive = await github.session_alive(page, i.repo_url)
                rec.note = "alive" if alive else "dead"
                log.info("Saved session is %s", rec.note)
            if alive:
                self.manifest.session = "reused"
                await self._register_identifiers(page)
                return
            # Stale cookies can make the portal serve odd pages; start clean.
            await self.session.context.clear_cookies()
            discard_state(i.state_path)

        await self._stage("login", lambda _a: github.login(page, i.base_url, self.creds,
                                                           self.provider))
        async with self.stages.stage("session.save"):
            await save_state(self.session.context, i.state_path)
        self.manifest.session = "full_login_after_dead_session" if had_state else "full_login"
        await self._register_identifiers(page)

    async def _register_identifiers(self, page: Page) -> None:
        ids = await github.account_identifiers(page)
        redactor.add(*ids)
        self.identifiers.extend(i for i in ids if i not in self.identifiers)
        log.info("Registered %d account identifier(s) for redaction", len(ids))


async def run(args: argparse.Namespace) -> int:
    inputs = load_input(args.input)
    if args.headed:
        inputs.headless = False
    creds = load_credentials(args.env_file)
    run_dir = inputs.output_dir / datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(logging.DEBUG if args.verbose else logging.INFO, str(run_dir / "run.log"))
    log.info("Run directory: %s", run_dir)

    if args.break_step:
        resolver.BROKEN_TARGETS.add(args.break_step)
        log.warning("Deliberately breaking target %s", args.break_step)

    provider: CodeProvider = TotpProvider(creds.totp_secret.get_secret_value())
    if args.simulate_wrong_code:
        provider = WrongFirstCodeProvider(provider)

    runner = Runner(inputs, creds, provider, run_dir)
    try:
        await runner.run()
        runner.manifest.status = "ok"
        return 0
    except AutomationError as exc:
        runner.manifest.status = "failed"
        runner.manifest.error = {"stage": runner.stages.current, "type": type(exc).__name__,
                                 "message": str(exc), "evidence": str(run_dir / "evidence")}
        log.error("Final failure in stage %s: %s", runner.stages.current, exc)
        return 1
    finally:
        runner.manifest.finished_at = now_iso()
        runner.manifest.retries = runner.retries.events
        manifest_path = run_dir / "manifest.json"
        runner.manifest.write(manifest_path, runner.stages)
        log.info("Manifest written to %s (status=%s, session=%s, files=%d)", manifest_path,
                 runner.manifest.status, runner.manifest.session, len(runner.manifest.files))


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parse(argv)
    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
