"""GitHub portal flow: login with password + TOTP."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .. import captcha
from ..config import Credentials
from ..errors import AuthError, FinalInputError, RetryableError, TwoFactorError
from ..fsutil import private_file
from ..otp import Challenge, CodeProvider
from ..resolver import Target, exists, resolve

log = logging.getLogger(__name__)

# --- Targets ---------------------------------------------------------------

USERNAME = Target(
    key="login.username",
    role="textbox", name=re.compile(r"username or email", re.IGNORECASE),
    labels=(re.compile(r"username or email", re.IGNORECASE),),
    near_text=("Username or email address",),
    structure=('form input[autocomplete="username"]', 'form input[name="login"]'),
    css=("#login_field", ".js-login-field"),
)
PASSWORD = Target(
    key="login.password",
    role="textbox", name=re.compile(r"^password$", re.IGNORECASE),  # password inputs expose no role
    labels=(re.compile(r"^password$", re.IGNORECASE),),
    near_text=("Password",),
    structure=('form input[type="password"]',),
    css=("#password", ".js-password-field"),
)
SIGN_IN = Target(
    key="login.submit",
    role="button", name="Sign in", exact=True,
    structure=('form input[type="submit"][name="commit"]', 'form [type="submit"]'),
    css=(".js-sign-in-button",),
)
OTP_FIELD = Target(
    key="2fa.code",
    role="textbox", name=re.compile(r"authentication code|one-time|verification code", re.IGNORECASE),
    labels=(re.compile(r"authentication code", re.IGNORECASE),),
    near_text=("Authentication code",),
    structure=('input[autocomplete="one-time-code"]', 'form input[inputmode="numeric"]'),
    css=("#app_totp", 'input[name="app_otp"]', "#otp"),
)
OTP_VERIFY = Target(
    key="2fa.verify",
    role="button", name=re.compile(r"^verify$", re.IGNORECASE),
    structure=('form:has(input[autocomplete="one-time-code"]) [type="submit"]',),
    css=('button[type="submit"].btn-primary',),
)
USE_AUTH_APP = Target(  # shown when GitHub defaults to passkey / mobile prompt
    key="2fa.use_app",
    role="link", name=re.compile(r"authenticator app", re.IGNORECASE),
    near_text=("authenticator app",),
    near_tag="a",
    css=('a[href*="two-factor/app"]',),
)
OTP_FAILED_TEXT = re.compile(r"two-factor authentication failed|authentication code is (?:invalid|incorrect)", re.IGNORECASE)
BAD_PASSWORD_TEXT = re.compile(r"incorrect username or password", re.IGNORECASE)


# --- Page state probes ------------------------------------------------------

async def logged_in_user(page: Page) -> str | None:
    """The login GitHub renders into every page for a signed-in user, or None."""
    try:
        value = await page.evaluate(
            "() => document.querySelector('meta[name=\"user-login\"]')?.content || ''"
        )
    except PlaywrightError:  # page mid-navigation
        return None
    return value or None


async def account_identifiers(page: Page) -> list[str]:
    """Login handle and display name of the signed-in user, for redaction.

    Both are account identifiers: they appear in URLs, embedded JSON and the
    avatar menu, so evidence and logs must mask them.
    """
    try:
        found = await page.evaluate("""() => {
            const out = [];
            const login = document.querySelector('meta[name="user-login"]')?.content;
            if (login) out.push(login);
            // The user menu button carries data-login; its avatar alt is the display name.
            const sel = (login ? `[data-login="${CSS.escape(login)}"] img[alt], ` : '') +
                        'header img.avatar-user, img.avatar-user';
            for (const img of document.querySelectorAll(sel)) {
                const alt = (img.getAttribute('alt') || '').replace(/^@/, '').trim();
                if (alt) out.push(alt);
            }
            return out;
        }""")
    except PlaywrightError:
        return []
    return sorted({v for v in found if v})


def _on_two_factor(page: Page) -> bool:
    return "/sessions/two-factor" in page.url


async def _wait_for(page: Page, predicate, *, timeout: float, what: str) -> str:
    """Poll `predicate(page)` until it returns a non-empty outcome string."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = await predicate(page)
        if outcome:
            return outcome
        await asyncio.sleep(0.3)
    raise RetryableError(f"Timed out after {timeout:.0f}s waiting for {what} (url={page.url})")


# --- Login --------------------------------------------------------------------

async def login(page: Page, base_url: str, creds: Credentials, provider: CodeProvider,
                on_captcha=None) -> str:
    """Full login: password, then TOTP with one retry. Returns the signed-in login.

    `on_captcha(page)` is awaited if a CAPTCHA appears after the password step;
    it either raises (no human available) or returns once a human solved it.
    """
    await page.goto(f"{base_url}/login", wait_until="domcontentloaded")

    if user := await logged_in_user(page):
        log.info("Already signed in")
        return user

    await (await resolve(page, USERNAME)).locator.fill(creds.username.get_secret_value())
    await (await resolve(page, PASSWORD)).locator.fill(creds.password.get_secret_value())
    await (await resolve(page, SIGN_IN)).locator.click()

    async def after_password(p: Page) -> str:
        if _on_two_factor(p):
            return "2fa"
        if "/sessions/verified-device" in p.url:
            return "device"
        if await logged_in_user(p):
            return "done"
        if await p.get_by_text(BAD_PASSWORD_TEXT).filter(visible=True).count():
            return "bad_password"
        if await captcha.detect(p):
            return "captcha"
        return ""

    outcome = await _wait_for(page, after_password, timeout=30, what="the post-password page")
    if outcome == "captcha":
        if on_captcha is None:
            raise captcha.CaptchaRequired(await captcha.detect(page) or "unknown", page.url)
        await on_captcha(page)
        outcome = await _wait_for(page, after_password, timeout=30,
                                  what="the page after the CAPTCHA")
    if outcome == "bad_password":
        raise AuthError("Portal rejected the username or password")
    if outcome == "device":
        raise AuthError("Portal asked for email device verification; enable TOTP 2FA on the account")
    if outcome == "2fa":
        await _two_factor(page, provider)

    user = await logged_in_user(page)
    if not user:
        raise RetryableError(f"Login finished but no signed-in user was found (url={page.url})")
    log.info("Signed in")
    return user


async def _submit_and_wait(page: Page, action, timeout: float) -> bool:
    """Run `action` and wait for the navigation it causes. False if none happened."""
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout * 1000):
            await action()
        return True
    except PlaywrightTimeoutError:
        return False


async def _two_factor(page: Page, provider: CodeProvider, max_attempts: int = 2) -> None:
    await page.wait_for_load_state("domcontentloaded")
    if not await exists(page, OTP_FIELD, timeout=3):
        # Passkey or GitHub Mobile prompt first: switch to the authenticator app.
        log.info("2FA page is not the authenticator form; switching to it")
        await (await resolve(page, USE_AUTH_APP, timeout=10)).locator.click()
        await page.wait_for_load_state("domcontentloaded")

    for attempt in range(1, max_attempts + 1):
        code = await provider.get_code(Challenge(kind="totp", attempt=attempt, portal="github"))
        field = (await resolve(page, OTP_FIELD)).locator
        await field.fill("")

        # GitHub auto-submits on the 6th digit. If it does not, submit explicitly.
        submitted = await _submit_and_wait(page, lambda f=field, c=code: f.fill(c), timeout=6)
        if not submitted:
            async def click_verify(f=field) -> None:
                if await exists(page, OTP_VERIFY):
                    await (await resolve(page, OTP_VERIFY)).locator.click()
                else:
                    await f.press("Enter")
            if not await _submit_and_wait(page, click_verify, timeout=20):
                raise RetryableError("2FA form did not submit")

        # The page is freshly loaded, so any error text belongs to this attempt.
        if not _on_two_factor(page):
            log.info("2FA accepted on attempt %d", attempt)
            return
        rejected = await page.get_by_text(OTP_FAILED_TEXT).filter(visible=True).count() > 0
        log.warning("2FA code %s on attempt %d of %d",
                    "rejected" if rejected else "not accepted", attempt, max_attempts)

    raise TwoFactorError(f"2FA code rejected {max_attempts} times")


# =============================================================================
# Navigation → form → document
# =============================================================================

REPO_RELEASES_LINK = Target(
    key="nav.releases",
    role="link", name="Releases", exact=True,
    structure=('a[href$="/releases"]',),  # no near_text: the next <a> after it is a release
    css=(".BorderGrid a[href$='/releases']",),
)
DRAFT_NEW_LINK = Target(
    key="nav.draft_new",
    role="link", name="Draft a new release", exact=True,
    structure=('a[href$="/releases/new"]',),
    css=("a.btn[href$='/releases/new']",),
)
FORM_RELEASES_LINK = Target(  # breadcrumb back to the list from the form
    key="nav.form_to_releases",
    role="link", name="Releases", exact=True,
    structure=('main a[href$="/releases"]',),
)
TAG_BUTTON = Target(
    key="form.tag.open",
    role="button", name=re.compile(r"\bTag:", re.IGNORECASE),  # unanchored: survives reorder
    structure=('main [aria-haspopup]:has-text("Tag:")',
               'main :is(button, [role="button"]):has-text("Tag:")',
               # wording-independent: survives a copy rewrite
               '#tag-list [data-testid="anchor-button"][aria-haspopup]'),
    css=("#ref-picker-releases-tag",),
)
TAG_FILTER = Target(
    key="form.tag.filter",
    role="textbox", name="Filter tags", exact=True,
    labels=("Search or create a new tag",),
    structure=('[role="dialog"] input[type="text"]',),
)
CREATE_TAG_BUTTON = Target(
    key="form.tag.create",
    role="button", name="Create new tag", exact=True,
    structure=('[role="dialog"] button:has-text("Create new tag")',),
)
CREATE_TAG_CONFIRM = Target(
    key="form.tag.confirm",
    role="button", name="Create", exact=True,
    structure=('[role="dialog"]:has-text("Create new tag") button:text-is("Create")',),
)
TARGET_BUTTON = Target(  # the dependent field: only rendered for a NEW tag
    key="form.target.open",
    role="button", name=re.compile(r"\bTarget:", re.IGNORECASE),  # GitHub renders a <summary>
    structure=('main [role="group"] :is(button, summary, [role="button"]):has-text("Target:")',
               'details:has([data-menu-button]) > summary:has-text("Target:")',
               # wording-independent: the menu summary in the tag-state container
               '[data-existing-id] details > summary[aria-haspopup="menu"]'),
)
BRANCH_FILTER = Target(
    key="form.target.filter",
    role="textbox", name="Filter branches", exact=True,
    labels=("Filter branches…",),
)
TITLE = Target(
    key="form.title",
    role="textbox", name="Enter release title", exact=True,
    labels=("Title", re.compile(r"release title", re.IGNORECASE)),
    near_text=("Release title",),
    structure=('main form input[type="text"][placeholder="Title"]',),
    css=("#release_name", 'input[name="release[name]"]'),
)
NOTES = Target(
    key="form.notes",
    role="textbox", name="Release description", exact=True,
    labels=(re.compile(r"release description", re.IGNORECASE),),
    near_text=("Release description",), near_tag="textarea",
    structure=("main form textarea",),
    css=("#release_body", 'textarea[name="release[body]"]'),
)
SAVE_DRAFT = Target(
    key="form.save_draft",
    role="button", name="Save draft", exact=True,
    structure=('main form button:has-text("Save draft")',),
    css=("button.js-save-draft",),
)
DUPLICATE_TAG_TEXT = re.compile(r"duplicate tag name", re.IGNORECASE)


LABEL_VALUES = {"None": "none", "Pre-release": "prerelease", "Latest": "latest"}


def release_label(label: str) -> Target:
    """Release label radio: by its visible name, or by the form value it submits."""
    value = LABEL_VALUES[label]
    return Target(
        key="form.label",
        role="radio", name=label, exact=True,
        labels=(label,),
        structure=(
            f'input[type="radio"][name="release[release_label_option]"][value="{value}"]',),
    )


def _option(key: str, role: str, name: str) -> Target:
    return Target(key=key, role=role, name=name, exact=True,
                  structure=(f'[role="{role}"]:text-is("{name}")',))


def release_link(tag: str) -> Target:
    return Target(
        key="doc.release_link",
        role="link", name=tag, exact=True,
        structure=(f'a[href$="/releases/tag/{tag}"]',),
    )


def asset_link(tag: str, asset: str) -> Target:
    return Target(
        key="doc.asset_link",
        role="link", name=asset, exact=True,
        near_text=(asset,), near_tag="a",
        structure=(f'a[href$="/releases/download/{tag}/{asset}"]',),
        css=(f'a[href*="/download/"][href$="/{asset}"]',),
    )


def delete_button(title: str, slug: str) -> Target:
    """Delete button of one specific release.

    Titles can repeat (a leftover draft from an earlier failed run), so the
    role match may be ambiguous; the resolver then moves on to the structural
    selector scoped to this draft's own <section id="release-<slug>">.
    """
    scope = f'section[id="release-{slug}"]'
    return Target(
        key="cleanup.delete",
        role="button", name=f"Delete: {title}", exact=True,
        structure=(f'{scope} button[data-show-dialog-id^="delete-release"]',
                   f'{scope} :is(button, [role="button"])[aria-label^="Delete"]'),
        css=(f'{scope} button.Button--danger',),
    )


DELETE_CONFIRM = Target(
    key="cleanup.confirm",
    role="button", name="Delete this release", exact=True,
    structure=('[role="dialog"] button:has-text("Delete this release")',),
)


def _picker_value(text: str, prefix: str) -> str:
    """'Target: staging' -> 'staging', whatever order the pieces render in."""
    return " ".join(text.replace(prefix, " ").split())


async def _click(page: Page, target: Target, **kw) -> None:
    await (await resolve(page, target, **kw)).locator.click()


async def _settle(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded")


async def session_alive(page: Page, repo_url: str) -> bool:
    """Positive check, not just "did we get redirected to /login".

    Alive only if GitHub renders a signed-in user AND the private repo page
    loads (logged out, a private repo is a 404, not a redirect).

    A signed-in user who still gets a 404 means the repo in the input is wrong
    or not shared with this account. That is a final input error: throwing
    away a working session and logging in again would not fix it. Other
    statuses (GitHub 5xx, rate limiting) are retryable.
    """
    response = await page.goto(repo_url, wait_until="domcontentloaded")
    user = await logged_in_user(page)
    status = response.status if response else 0
    log.info("Session check: signed_in=%s status=%s", bool(user), status)
    if not user or "/login" in page.url:
        return False
    if status == 200:
        return True
    if status == 404:
        raise FinalInputError(f"Repository not reachable with this account: {repo_url}")
    raise RetryableError(f"Session check got HTTP {status} for {repo_url}")


async def navigate_to_form(page: Page, repo_url: str) -> None:
    """repo page → Releases → Draft a new release, by clicking like a user."""
    if page.url.rstrip("/") != repo_url.rstrip("/"):
        await page.goto(repo_url, wait_until="domcontentloaded")
    await page.wait_for_load_state("load")
    if "Page not found" in await page.title():
        raise FinalInputError(f"Repository not reachable with this account: {repo_url}")
    await _click(page, REPO_RELEASES_LINK)
    await page.wait_for_url(re.compile(r"/releases/?(\?.*)?$"), timeout=20_000)
    await _click(page, DRAFT_NEW_LINK)
    await page.wait_for_url(re.compile(r"/releases/new"), timeout=20_000)
    await _settle(page)


async def fill_release_form(page: Page, form) -> dict:
    """Fill the draft form. Returns what was filled, for the manifest.

    Dependency: the Target branch picker exists only when the tag is new.
    An existing tag hides it and turns the form into "duplicate tag", which
    is a final input error rather than something to retry.
    """
    filled: dict = {"tag": form.tag}

    # The pickers are client-side widgets: a click before the page's scripts
    # have loaded does nothing. Wait for load, and re-click if no dialog opens.
    await page.wait_for_load_state("load")
    await _click(page, TAG_BUTTON)
    if not await exists(page, TAG_FILTER, timeout=3):
        log.info("Tag dialog did not open; clicking again")
        await _click(page, TAG_BUTTON)
    await (await resolve(page, TAG_FILTER)).locator.fill(form.tag)
    await asyncio.sleep(0.6)  # list filters as you type
    if await exists(page, _option("form.tag.existing", "menuitemradio", form.tag), timeout=1.0):
        raise FinalInputError(
            f"Tag {form.tag!r} already exists; the form needs a new tag "
            "(the Target field only appears for new tags)")
    await _click(page, CREATE_TAG_BUTTON)
    await _click(page, CREATE_TAG_CONFIRM)
    filled["tag_mode"] = "new"

    # Dependent field: wait for Target to appear because the tag is new.
    target_btn = await resolve(page, TARGET_BUTTON, timeout=10)
    filled["target_shown"] = True
    if form.target:
        current = _picker_value(await target_btn.locator.inner_text(), "Target:")
        if current != form.target:
            await target_btn.locator.click()
            branch = _option("form.target.branch", "menuitemradio", form.target)
            if not await exists(page, branch, timeout=3):
                # Long branch lists are paged: filter, then let the list settle.
                await (await resolve(page, BRANCH_FILTER)).locator.fill(form.target)
                await asyncio.sleep(0.8)
                if not await exists(page, branch, timeout=5):
                    raise FinalInputError(f"Branch {form.target!r} does not exist")
            await _click(page, branch)  # selecting closes the menu; Escape would cancel it

        async def target_value(p: Page) -> str:
            btn = await resolve(p, TARGET_BUTTON)
            now = _picker_value(await btn.locator.inner_text(), "Target:")
            return now if now == form.target else ""
        filled["target"] = await _wait_for(page, target_value, timeout=5,
                                           what=f"Target to show {form.target!r}")

    await (await resolve(page, TITLE)).locator.fill(form.title)
    filled["title"] = form.title
    if form.notes:
        await (await resolve(page, NOTES)).locator.fill(form.notes)
        filled["notes_chars"] = len(form.notes)

    label = release_label(form.label)
    await (await resolve(page, label)).locator.check()
    filled["label"] = form.label

    if await page.get_by_text(DUPLICATE_TAG_TEXT).filter(visible=True).count():
        raise FinalInputError(f"Portal reports duplicate tag {form.tag!r}")
    return filled


async def save_draft(page: Page) -> str:
    """Submit as draft. Returns the draft's edit URL."""
    await _click(page, SAVE_DRAFT)
    await page.wait_for_url(re.compile(r"/releases/edit/"), timeout=30_000)
    await _settle(page)
    log.info("Draft saved")
    return page.url


_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def safe_download_path(downloads_dir: Path, suggested: str | None, *, fallback: str) -> Path:
    """Server-chosen file name -> path that can only land inside downloads_dir.

    The name comes from the portal (Content-Disposition), so it is untrusted:
    directory parts are dropped, odd characters replaced, and hidden or empty
    names fall back to the expected asset name.
    """
    name = Path((suggested or "").replace("\\", "/")).name
    name = _UNSAFE_CHARS.sub("_", name).strip(" .")
    if not name:
        name = _UNSAFE_CHARS.sub("_", Path(fallback).name).strip(" .") or "download.bin"
    root = downloads_dir.resolve()
    dest = (root / name).resolve()
    if dest.parent != root:
        raise FinalInputError(f"Refusing to save a download outside {downloads_dir}")
    return dest


async def capture_asset(page: Page, repo_url: str, tag: str, asset: str,
                        downloads_dir) -> tuple[str, str | None, str]:
    """Releases → <tag> → click asset (native download).

    Returns (saved path, sha256 digest shown by the portal, source URL).
    """
    if not re.search(r"/releases/?(\?.*)?$", page.url):
        if "/releases/" in page.url and await exists(page, FORM_RELEASES_LINK, timeout=2):
            await _click(page, FORM_RELEASES_LINK)
        else:
            await page.goto(f"{repo_url}/releases", wait_until="domcontentloaded")
        await page.wait_for_url(re.compile(r"/releases/?(\?.*)?$"), timeout=20_000)

    await _click(page, release_link(tag))
    await page.wait_for_url(re.compile(rf"/releases/tag/{re.escape(tag)}$"), timeout=20_000)
    await _settle(page)

    link = (await resolve(page, asset_link(tag, asset))).locator
    expected = None
    try:
        row = await link.locator("xpath=ancestor::li[1]").inner_text(timeout=2_000)
        if m := re.search(r"sha256:([0-9a-f]{64})", row):
            expected = m.group(1)
    except PlaywrightError:
        log.info("No digest shown for %s", asset)

    async with page.expect_download(timeout=60_000) as info:
        await link.click()
    download = await info.value
    if failure := await download.failure():
        raise RetryableError(f"Download failed: {failure}")
    dest = safe_download_path(downloads_dir, download.suggested_filename, fallback=asset)
    await download.save_as(dest)
    private_file(dest)  # a private statement: owner-only
    log.info("Downloaded %s", dest.name)
    # Record the portal link, not the signed CDN redirect (its query is a bearer token).
    href = await link.get_attribute("href") or ""
    return str(dest), expected, urljoin(page.url, href)


def draft_slug(draft_url: str) -> str:
    """.../releases/edit/untagged-7ced8d22 -> untagged-7ced8d22"""
    return draft_url.rstrip("/").rsplit("/", 1)[-1]


async def draft_gone(page: Page, draft_url: str) -> bool:
    """True if the draft's edit page is a 404, i.e. the draft no longer exists."""
    response = await page.goto(draft_url, wait_until="domcontentloaded")
    return response is not None and response.status == 404


async def delete_draft(page: Page, repo_url: str, title: str, draft_url: str, *,
                       check_first: bool = False) -> None:
    """Remove the draft this run created (confirmation dialog quirk).

    GitHub's release list can lag a few seconds behind a just-saved draft, so
    always load a fresh list, and reload with a short backoff until this
    draft's own section appears.

    `check_first` makes a retry idempotent: if an earlier attempt already
    deleted the draft, its edit page is a 404 and there is nothing to do.
    """
    if check_first and await draft_gone(page, draft_url):
        log.info("Draft already deleted")
        return
    slug = draft_slug(draft_url)
    section = page.locator(f'section[id="release-{slug}"]')
    for attempt in range(4):
        await page.goto(f"{repo_url}/releases", wait_until="domcontentloaded")
        if await section.count():
            break
        log.info("Draft not listed yet (attempt %d); reloading", attempt + 1)
        await asyncio.sleep(1.5 * (attempt + 1))
    else:
        raise RetryableError("This run's draft never appeared in the release list")
    btn = await resolve(page, delete_button(title, slug))
    if btn.strategy != "structure":
        # A unique role match is fine only if it sits inside this draft's section.
        inside = await btn.locator.evaluate(
            "(el, id) => !!el.closest('[id=\"' + id + '\"]')", f"release-{slug}")
        if not inside:
            raise FinalInputError("Delete button found, but not for this run's draft")
    await btn.locator.click()
    await _click(page, DELETE_CONFIRM)
    # We are already on /releases, so waiting for that URL proves nothing.
    # Wait for this draft's section to go away instead.
    try:
        await section.wait_for(state="detached", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        raise RetryableError("Draft still listed after confirming the delete") from exc
    log.info("Draft deleted")
