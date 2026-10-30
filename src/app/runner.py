"""One portal run, callable from the CLI, the batch runner and the job queue.

    result = await execute(inputs, creds, RunOptions(...))

Safe to run several at once in one process: logging, --break-step and the
account lock are all per run.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from . import captcha
from .browser import BrowserSession, open_browser
from .config import Credentials, RunInput
from .errors import AutomationError, RetryableError
from .evidence import write_bundle
from .flows import github
from .fsutil import private_dir
from .locks import AccountLock
from .manifest import CapturedFile, Manifest, now_iso, sha256_file
from .otp import CodeProvider, TotpProvider, WrongFirstCodeProvider
from .redact import attach_run_log, current_run, detach_run_log, redactor
from .resolver import BROKEN_TARGETS
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


@dataclass
class RunOptions:
    headed: bool = False
    simulate_wrong_code: bool = False
    break_step: str | None = None
    run_id: str | None = None  # the queue passes its job id


@dataclass
class RunResult:
    run_id: str
    status: str  # ok | failed | needs_human
    exit_code: int
    run_dir: Path
    manifest_path: Path
    session: str | None = None
    files: list[str] = field(default_factory=list)
    error: dict | None = None


def new_run_id() -> str:
    # Timestamp for humans + 4 random hex chars so parallel runs never collide.
    return f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}-{secrets.token_hex(2)}"


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

    # -- helpers -----------------------------------------------------------

    async def _stage(self, key: str, fn, policy: RetryPolicy | None = None):
        page = self.session.page if self.session else None

        async def guarded(attempt: int):
            try:
                return await fn(attempt)
            except (RetryableError, PlaywrightError):
                # A CAPTCHA looks like "element not found"; retrying won't help.
                if page is not None:
                    await captcha.check(page, headless=self.inputs.headless,
                                        wait_seconds=self.inputs.captcha_wait_seconds)
                raise

        async with self.stages.stage(key) as rec:
            result = await with_retry(key, guarded, policy=policy, history=self.retries)
            attempts = sum(1 for e in self.retries.events if e["stage"] == key) + 1
            if attempts > 1:
                rec.note = f"succeeded on attempt {attempts}"
            return result

    def _track_pages(self, page: Page) -> None:
        def on_nav(frame) -> None:
            if frame == page.main_frame and frame.url.startswith("http"):
                self.manifest.visit(frame.url)
        page.on("framenavigated", on_nav)

    async def _on_captcha(self, page: Page) -> None:
        await captcha.check(page, headless=self.inputs.headless,
                            wait_seconds=self.inputs.captcha_wait_seconds)

    # -- run ---------------------------------------------------------------

    async def run(self) -> None:
        i = self.inputs
        lock = AccountLock(i.state_path)
        async with self.stages.stage("account.lock") as rec:
            await lock.acquire()
            rec.note = f"waited {lock.waited:.1f}s" if lock.waited > 0.05 else None
        try:
            state = load_state(i.state_path)  # read only while holding the lock
            proxy = i.effective_proxy()
            async with open_browser(headless=i.headless, storage_state=state,
                                    downloads=self.run_dir / "downloads", proxy=proxy,
                                    profile=i.fingerprint, timezone=i.timezone) as session:
                self.session = session
                self.manifest.browser = {"profile": session.profile,
                                         "user_agent": session.user_agent,
                                         "proxy": session.proxy_server}
                self._track_pages(session.page)
                try:
                    await self._authenticate(session.page, had_state=state is not None)
                    lock.release()  # session is valid and saved; let other runs in
                    await self._steps(session.page)
                except Exception as exc:  # AutomationError, or a bug / unexpected error
                    if isinstance(exc, captcha.CaptchaRequired):
                        self.manifest.captcha = exc.kind
                    await write_bundle(
                        self.run_dir / "evidence", page=session.page, console=session.console,
                        stage=self.stages.current, error=exc, retry_events=self.retries.events,
                        identifiers=self.identifiers)
                    await self._best_effort_cleanup(session.page)
                    raise
        finally:
            lock.release()

    async def _best_effort_cleanup(self, page: Page) -> None:
        """After a failure, remove a draft this run already saved. Never raises."""
        if not self.manifest.form.get("draft_url") or self.inputs.form.keep_draft:
            return
        # --break-step sabotages a target to demo the failure path; it must not
        # also stop us from cleaning up (e.g. --break-step cleanup.delete).
        unbroken = BROKEN_TARGETS.set(frozenset())
        try:
            await github.delete_draft(page, self.inputs.repo_url, self.inputs.form.title,
                                      self.manifest.form["draft_url"], check_first=True)
            self.manifest.form["cleanup_after_failure"] = "deleted"
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the real failure
            self.manifest.form["cleanup_after_failure"] = f"failed: {type(exc).__name__}"
            log.warning("Could not delete the draft after failure: %s", exc)
        finally:
            BROKEN_TARGETS.reset(unbroken)

    async def _steps(self, page: Page) -> None:
        i = self.inputs
        await self._stage("nav.to_form", lambda _a: github.navigate_to_form(page, i.repo_url))

        async def fill(attempt: int) -> dict:
            if attempt > 1:  # start again from a clean form
                await page.goto(f"{i.repo_url}/releases/new", wait_until="domcontentloaded")
            return await github.fill_release_form(page, i.form)
        self.manifest.form = await self._stage("form.fill", fill)

        async def submit(attempt: int) -> str:
            if attempt > 1:
                # The previous click may have saved the draft with the redirect
                # still on its way. Give it time before clicking Save again.
                try:
                    await page.wait_for_url(re.compile(r"/releases/edit/"), timeout=10_000)
                except PlaywrightTimeoutError:
                    pass
            if "/releases/edit/" in page.url:  # a previous attempt already saved it
                return page.url
            return await github.save_draft(page)
        self.manifest.form["draft_url"] = await self._stage("form.submit", submit,
                                                            RetryPolicy(attempts=2))

        async def capture(_attempt: int) -> CapturedFile:
            path, expected, src = await github.capture_asset(
                page, i.repo_url, i.document.release_tag, i.document.asset_name,
                self.run_dir / "downloads")
            # Relative to the run directory's parent, like every other path we log:
            # an absolute path would leak the local home directory.
            p = self.run_dir / "downloads" / Path(path).name
            digest = sha256_file(p)
            if expected and digest != expected:
                raise RetryableError("Downloaded file's sha256 does not match the digest "
                                     "the portal shows (truncated or wrong file)")
            return CapturedFile(path=str(p), name=p.name, bytes=p.stat().st_size,
                                sha256=digest, method="native_download",
                                source_url=src, expected_sha256=expected)
        captured = await self._stage("document.capture", capture)
        self.manifest.files.append(captured)

        if not i.form.keep_draft:
            await self._stage("cleanup.draft",
                              lambda a: github.delete_draft(page, i.repo_url, i.form.title,
                                                            self.manifest.form["draft_url"],
                                                            check_first=a > 1))

    async def _authenticate(self, page: Page, *, had_state: bool) -> None:
        i = self.inputs
        if had_state:
            async with self.stages.stage("session.check") as rec:
                # Check on the private repo page we need next anyway: one navigation,
                # and two signals (signed-in user + private content visible).
                alive = await with_retry(
                    "session.check", lambda _a: github.session_alive(page, i.repo_url),
                    policy=RetryPolicy(attempts=2), history=self.retries)
                rec.note = "alive" if alive else "dead"
                log.info("Saved session is %s", rec.note)
            if alive:
                self.manifest.session = "reused"
                await self._register_identifiers(page)
                return
            # Stale cookies can make the portal serve odd pages; start clean.
            await self.session.context.clear_cookies()
            discard_state(i.state_path)

        await self._stage("login", lambda _a: github.login(
            page, i.base_url, self.creds, self.provider, on_captcha=self._on_captcha))
        async with self.stages.stage("session.save"):
            await save_state(self.session.context, i.state_path)
        self.manifest.session = "full_login_after_dead_session" if had_state else "full_login"
        await self._register_identifiers(page)

    async def _register_identifiers(self, page: Page) -> None:
        ids = await github.account_identifiers(page)
        redactor.add(*ids)
        self.identifiers.extend(i for i in ids if i not in self.identifiers)
        log.info("Registered %d account identifier(s) for redaction", len(ids))


async def execute(inputs: RunInput, creds: Credentials, options: RunOptions) -> RunResult:
    """Run one job end to end. The result says how it went.

    Never raises for a failed run, including unexpected errors (status "failed",
    exit code 1, manifest written); only cancellation and Ctrl-C propagate.
    """
    if options.headed:
        inputs = inputs.model_copy(update={"headless": False})
    inputs.effective_proxy()  # fail fast (ConfigError) on a bad proxy route, before any I/O
    run_id = options.run_id or new_run_id()
    run_dir = inputs.output_dir / run_id
    private_dir(inputs.output_dir)  # runs/ holds private documents and evidence
    private_dir(run_dir)

    run_token = current_run.set(run_id)
    broken_token = BROKEN_TARGETS.set(
        frozenset({options.break_step}) if options.break_step else frozenset())
    handler = attach_run_log(str(run_dir / "run.log"), run_id)
    log.info("Run directory: %s", run_dir)
    if options.break_step:
        log.warning("Deliberately breaking target %s", options.break_step)

    provider: CodeProvider = TotpProvider(creds.totp_secret.get_secret_value())
    if options.simulate_wrong_code:
        provider = WrongFirstCodeProvider(provider)

    runner = Runner(inputs, creds, provider, run_dir)
    manifest_path = run_dir / "manifest.json"
    exit_code = 1
    try:
        await runner.run()
        runner.manifest.status = "ok"
        exit_code = 0
    except AutomationError as exc:
        needs_human = isinstance(exc, captcha.CaptchaRequired)
        runner.manifest.status = "needs_human" if needs_human else "failed"
        exit_code = 3 if needs_human else 1
        runner.manifest.error = {"stage": runner.stages.current, "type": type(exc).__name__,
                                 "message": str(exc), "evidence": str(run_dir / "evidence")}
        log.error("Final failure in stage %s: %s", runner.stages.current, exc)
    except Exception as exc:  # a bug or unexpected error: still report it
        runner.manifest.status = "failed"
        exit_code = 1
        runner.manifest.error = {"stage": runner.stages.current, "type": type(exc).__name__,
                                 "message": str(exc), "evidence": str(run_dir / "evidence")}
        log.error("Unexpected failure in stage %s", runner.stages.current, exc_info=exc)
    finally:
        runner.manifest.finished_at = now_iso()
        runner.manifest.retries = runner.retries.events
        runner.manifest.write(manifest_path, runner.stages)
        log.info("Manifest written to %s (status=%s, session=%s, files=%d)", manifest_path,
                 runner.manifest.status, runner.manifest.session, len(runner.manifest.files))
        detach_run_log(handler)
        BROKEN_TARGETS.reset(broken_token)
        current_run.reset(run_token)

    return RunResult(
        run_id=run_id, status=runner.manifest.status, exit_code=exit_code, run_dir=run_dir,
        manifest_path=manifest_path, session=runner.manifest.session,
        files=[f.path for f in runner.manifest.files],
        error=redactor_safe(runner.manifest.error))


def redactor_safe(error: dict | None) -> dict | None:
    if not error:
        return None
    return {k: redactor.redact(str(v)) for k, v in error.items()}
