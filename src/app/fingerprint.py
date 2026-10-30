"""Browser fingerprint consistency ("hardened" profile).

Out of the box, headless Chromium announces itself: `HeadlessChrome` in the
user agent and client hints, `navigator.webdriver === true`, and a language
list that disagrees with the Accept-Language header. Portals use exactly these
to flag sessions, which then get extra challenges or blocks.

The hardened profile makes the browser look like what it is (a real Chromium
build of a known version) without lying about anything else:

- runs the full Chromium build in new-headless mode (real plugins, real APIs)
  instead of the stripped-down headless shell
- user agent says "Chrome" (as every Chromium build does) and the UA client
  hints say "Chromium", same version, same platform
- `navigator.webdriver` is false (automation-controlled flag disabled)
- locale, `navigator.languages` and Accept-Language agree
- optional timezone, so it can match a proxy's exit region

It does not spoof hardware, canvas/WebGL output or other devices: the aim is
internal consistency for automating your own accounts, not impersonation.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false,
                                                             configurable: true });
  } catch (e) {}
})();
"""


@dataclass(frozen=True)
class Profile:
    user_agent: str
    accept_language: str
    locale: str
    platform: str  # client-hint platform: macOS | Windows | Linux
    os_token: str  # the "(...)" part of the UA string
    major: str
    full_version: str

    def context_options(self, timezone: str | None) -> dict:
        opts = {
            "user_agent": self.user_agent,
            "locale": self.locale,
            "extra_http_headers": {"Accept-Language": self.accept_language},
        }
        if timezone:
            opts["timezone_id"] = timezone
        return opts

    def ua_metadata(self) -> dict:
        # What a real Chromium build reports. No "Google Chrome" brand: this is
        # Chromium, and the profile doesn't claim to be anything else.
        brands = [{"brand": "Chromium", "version": self.major},
                  {"brand": "Not.A/Brand", "version": "99"}]
        return {
            "brands": brands,
            "fullVersionList": [{**b, "version": self.full_version if b["version"] != "99"
                                 else "99.0.0.0"} for b in brands],
            "fullVersion": self.full_version,
            "platform": self.platform,
            "platformVersion": platform_version(self.platform),
            "architecture": "arm" if platform.machine() in ("arm64", "aarch64") else "x86",
            "bitness": "64",
            "model": "",
            "mobile": False,
            "wow64": False,
        }


def platform_version(ch_platform: str) -> str:
    """The OS version the way Chrome's Sec-CH-UA-Platform-Version reports it.

    macOS: the real version, three parts ("26.6.2", "15.5.0").
    Windows: Chrome reports "10.0.0" for Windows 10 and "13.0.0"+ for 11; build
    22000 is the first Windows 11 build.
    Linux: Chrome sends an empty string.
    """
    if ch_platform == "macOS":
        ver = platform.mac_ver()[0] or "15.0"
        parts = (ver.split(".") + ["0", "0"])[:3]
        return ".".join(parts)
    if ch_platform == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
        except ValueError:
            build = 0
        return "15.0.0" if build >= 22000 else "10.0.0"
    return ""


def build_profile(browser_version: str, locale: str = "en-US") -> Profile:
    system = platform.system()
    os_token, ch_platform = {
        "Darwin": ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),  # Chrome freezes this token
        "Windows": ("Windows NT 10.0; Win64; x64", "Windows"),
    }.get(system, ("X11; Linux x86_64", "Linux"))
    major = browser_version.split(".", 1)[0]
    # Real Chrome sends a reduced UA: only the major version is meaningful.
    ua = (f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{major}.0.0.0 Safari/537.36")
    lang = locale.split("-", 1)[0]
    accept = f"{locale},{lang};q=0.9" if lang != locale else locale
    return Profile(ua, accept, locale, ch_platform, os_token, major, browser_version)


async def apply_to_page(context: BrowserContext, page: Page, profile: Profile) -> None:
    """Client hints need a CDP override per page (Playwright sets only the UA string)."""
    cdp = await context.new_cdp_session(page)
    await cdp.send("Emulation.setUserAgentOverride", {
        "userAgent": profile.user_agent,
        "acceptLanguage": profile.accept_language,
        "platform": {"macOS": "MacIntel", "Windows": "Win32"}.get(profile.platform,
                                                                   "Linux x86_64"),
        "userAgentMetadata": profile.ua_metadata(),
    })


async def harden(browser: Browser, context: BrowserContext, profile: Profile) -> None:
    await context.add_init_script(INIT_SCRIPT)

    async def on_page(page: Page) -> None:
        await apply_to_page(context, page, profile)

    context.on("page", on_page)
