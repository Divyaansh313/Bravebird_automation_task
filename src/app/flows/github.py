"""GitHub portal flow: login with password + TOTP."""

from __future__ import annotations

import asyncio
import logging
import re
import time

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..config import Credentials
from ..errors import AuthError, RetryableError, TwoFactorError
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

async def login(page: Page, base_url: str, creds: Credentials, provider: CodeProvider) -> str:
    """Full login: password, then TOTP with one retry. Returns the signed-in login."""
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
        return ""

    outcome = await _wait_for(page, after_password, timeout=30, what="the post-password page")
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

