# Bravebird Automation Task

A single-process Playwright CLI that:

1. logs into **GitHub** with a password and a **TOTP second factor** (no human types the code),
2. **reuses the saved session** on later runs and falls back to a full login when it is dead,
3. clicks **three pages deep** (repo → Releases → Draft a new release) to a **5-field form with a
   dependent field**, fills it and saves a draft,
4. captures a **document via native download** (a release asset PDF) and verifies its sha256,
5. writes a **JSON manifest** of inputs, pages visited, stage timings and captured files.

All inputs come from a JSON file; a different release, document or form value needs no code change.

## Status

| Requirement | State |
|---|---|
| 1. Second-factor login, code obtained automatically | ✅ TOTP, wrong-code retry verified live |
| 2. Session reuse + fallback to full login | ✅ Reuse, corrupted cookies and unreadable file verified live |
| 3. ≥ 3 pages deep to a form with ≥ 3 fields, one dependent | ✅ 5 fields; Target only exists for a new tag |
| 4. Document capture | ✅ Native download, sha256 checked against the portal's digest |
| 5. JSON manifest | ✅ Written on success *and* failure |
| Resilient locators, offline | ✅ 5 pages × 6 mutation variants, all found |
| Retry + evidence bundle | ✅ Bounded backoff, redacted bundle (see `examples/failure-bundle`) |

**Portal:** GitHub (TOTP 2FA). One site covers everything.
**Test data:** private repo `Divyaansh313/bravebird-portal-sandbox`, branches `master` and `staging`,
releases `v1.0.0` and `v1.1.0` with one PDF each. Private, so nothing is reachable without logging in.

## Requirements

| Tool | Version | Purpose |
|---|---|---|
| macOS / Linux | — | Tested on macOS 26 (Apple Silicon) |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.13 | Python + dependency manager |
| Python | 3.12 | Runtime (uv installs it) |
| Chromium | Playwright-managed | The browser |
| [gh](https://cli.github.com/) (optional) | ≥ 2.100 | Only for publishing / sandbox setup |

Python dependencies are pinned in `uv.lock`: `playwright`, `pyotp`, `pydantic`, `python-dotenv`;
dev: `pytest`, `pytest-asyncio`, `ruff`.

## Setup (one command)

```bash
# once, if you don't have uv:
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="$HOME/.local/bin:$PATH"

uv sync && uv run playwright install chromium
cp .env.example .env    # then fill it in
```

## Environment variables

`.env` is git-ignored. See `.env.example`.

| Variable | Description |
|---|---|
| `PORTAL_USERNAME` | GitHub username or email of **your own** account |
| `PORTAL_PASSWORD` | Its password |
| `TOTP_SECRET` | Base32 TOTP secret: the "setup key" shown under the QR code when you add an authenticator app (Settings → Password and authentication) |

To point the run at your own account, also change `repo` in the input JSON to a repo you own that
has a release with an asset and a second branch.

## Usage

```bash
uv run bravebird --input inputs/github_v1.json                 # headless run
uv run bravebird --input inputs/github_v1.json --headed        # watch it
uv run bravebird --input inputs/github_v2.json                 # different document, no code change
uv run bravebird --input inputs/github_v1.json --simulate-wrong-code   # exercise the 2FA retry
uv run bravebird --input inputs/github_v1.json --break-step form.title # exercise the failure path

uv run pytest                                                  # 49 offline tests
uv run bravebird-offline check                                 # every fixture × every mutation
uv run bravebird-offline resolve --page form path/to/saved.html   # resolver on any saved HTML
uv run bravebird-offline mutate --all in.html out.html         # write a mutated copy
```

Exit codes: `0` success · `1` automation failure (evidence written) · `2` configuration error.
`--break-step` accepts any target key, e.g. `login.username`, `nav.releases`, `form.title`,
`form.target.open`, `doc.asset_link` (`--help` lists them all).

### Input JSON

```json
{
  "portal": "github",
  "base_url": "https://github.com",
  "repo": "Divyaansh313/bravebird-portal-sandbox",
  "form": {
    "tag": "v2.0.0-rc1",
    "target": "staging",
    "title": "Bravebird automated draft",
    "notes": "Drafted by the Bravebird automation run.",
    "label": "Pre-release"
  },
  "document": {
    "release_tag": "v1.0.0",
    "asset_name": "statement-v1.0.0.pdf"
  }
}
```

Optional: `form.keep_draft` (default `false`, the draft is deleted at the end), `headless`
(default `true`), `state_dir` (`state/`), `output_dir` (`runs/`). Unknown keys are rejected so a
typo fails fast with exit code 2.

### Outputs

```
state/github.storage.json          saved session (cookies + localStorage), mode 600, git-ignored
runs/<timestamp>/run.log           redacted log
runs/<timestamp>/manifest.json     inputs, pages visited, stages + timings, files, retries, error
runs/<timestamp>/downloads/        captured document(s)
runs/<timestamp>/evidence/         only on failure: screenshot, page.html, url, console, failure.json
```

Committed samples: [`examples/successful-run/`](examples/successful-run) (first run from no saved
state) and [`examples/failure-bundle/`](examples/failure-bundle) (`--break-step doc.asset_link`).

## What a run does

| Stage | What happens |
|---|---|
| `session.check` | Load saved state, open the private repo page, decide alive or dead |
| `login` | Only if no or dead session: username → password → TOTP (retry once) |
| `session.save` | Atomic write of storage state, mode 600 |
| `nav.to_form` | repo page → click **Releases** → click **Draft a new release** |
| `form.fill` | Tag (new) → **Target** branch (dependent) → Title → Description → Release label |
| `form.submit` | **Save draft**, which lands on `/releases/edit/untagged-…` |
| `document.capture` | Releases → click the release → click the asset → `download` event → sha256 check |
| `cleanup.draft` | Delete this run's draft through the confirmation dialog |

Measured live (see manifests): full login ≈ 16 s total, reused session ≈ 12 s total (auth stage
0.8 s instead of 3.4 s).

## Project layout

```
src/app/
  cli.py          `bravebird` entry point, Runner, stage orchestration
  config.py       .env credentials + input JSON models (pydantic)
  otp.py          CodeProvider protocol, TotpProvider, wrong-code simulator
  session.py      save / load / discard browser state
  resolver.py     multi-strategy element resolver
  flows/github.py GitHub targets and steps (login, navigation, form, download, cleanup)
  retry.py        bounded exponential backoff, retryable vs final
  evidence.py     failure bundle, HTML scrubbing, masked screenshot
  manifest.py     run manifest
  redact.py       secret registry, logging filter, URL-token masking
  stages.py       stage keys and timings
  offline.py      `bravebird-offline`: resolver against saved HTML, mutations
  browser.py      browser lifecycle, console capture
tests/            offline tests; fixtures are sanitized copies of the real pages
scripts/          sanitize_fixture.py (turn a saved page into a safe fixture)
inputs/           example input files
examples/         one successful run, one failure bundle
```

## Design answers

### How the code arrives, and what happens when it is wrong

The flow depends only on `async def get_code(challenge) -> str` (`src/app/otp.py`). The real
implementation, `TotpProvider`, computes RFC 6238 codes from `TOTP_SECRET` with `pyotp`, so there is
no human and no inbox to poll. An IMAP or SMS provider would be another class with the same method.

- If the current 30 s window has under 3 s left, it waits for the next window so the code doesn't
  expire in transit.
- GitHub auto-submits on the 6th digit. The flow waits for that form navigation; if none happens
  it clicks **Verify**. Because it waits for a fresh page load, a "failed" message left over from
  the previous attempt can't be mistaken for this attempt's result.
- **Wrong code → exactly one retry.** On attempt 2 the provider never reuses the rejected code; it
  waits for the next window (≈ 13 s in the verified run). A second rejection raises
  `TwoFactorError`, a final failure, so we never hammer the account into a lockout.
- If GitHub shows a passkey or GitHub Mobile prompt first, the flow switches to the authenticator app.
- **Never logged.** Each code is registered with the redactor when created. As a backstop, any
  stand-alone 6–8 digit number is masked in every log, manifest and evidence file.

`--simulate-wrong-code` proves it live: *rejected on attempt 1 → fresh code → accepted on attempt 2*.

### How a dead session is detected

Not by "did we get redirected to `/login`". After loading saved state the run opens the **private
sandbox repo** and requires **both**:

1. `<meta name="user-login">` is filled. GitHub renders it only for a signed-in user, whatever the URL.
2. The response is **200**. Logged out, a private repo is a **404**, not a redirect.

Outcomes:

- **Missing, unreadable or wrong-shape state file** → treated as absent → full login.
- **Valid file, dead cookies** (tested by scrambling every cookie value) → detected → cookies
  cleared from the browser, state file deleted, full login, new state saved. Clearing matters:
  with garbage cookies still present, GitHub served a page without the login form.
- State is written atomically (temp file + rename) with mode `600`.

This check also makes reuse faster: it happens on the page the flow needs next anyway.

### Locator strategy order, and why

Each element is a `Target` describing it several independent ways. `resolve()` tries them in this
order, in the main frame and then every iframe, and logs which strategy won:

1. **Role + accessible name**: what assistive tech sees. It survives class renames, wrappers and
   reordering, because the accessibility tree is what GitHub keeps stable.
2. **Label / placeholder**: the text a user reads next to the field.
3. **Nearby text**: visible text followed by the element (generated XPath, not hand-written).
4. **Structure**: shape in the page, e.g. `section[id="release-<slug>"] button[data-show-dialog-id^="delete-release"]`.
5. **id / class**: last resort, because build tooling churns these the most (GitHub's classes
   are hashed, like `prc-Button-ButtonBase-9n-Xk`).

Rules that made it robust in practice:

- **Visible elements only.** GitHub's login page has a hidden honeypot input, and the form page
  has hidden status templates such as "Duplicate tag name". Counting hidden elements caused false
  failures during development.
- **Unique or keep looking.** A strategy matching several elements is recorded as *ambiguous* and
  the resolver moves on. Example: two drafts with the same title make the role match ambiguous, so
  the section-scoped structural selector wins and deletes the right one. An ambiguous match is
  used only if nothing unique turns up before the timeout.
- **Unanchored names for composite labels.** The Target control is `<summary><i>Target:</i>
  <span>master</span></summary>`. Reordering its children changes the name to "master Target:",
  so the name pattern matches "Target:" anywhere.
- Every attempt (strategy, query, frame, result) is kept and written to `failure.json`.

**Offline proof:** `uv run bravebird-offline check` loads sanitized copies of all 5 pages the flow
touches, blocks all network, applies **renamed classes**, **reordered siblings**, an **added
wrapper div**, an **iframe**, and all four together, then resolves every target: **all found**.
`tests/test_resolver_mutations.py` runs the same matrix in pytest, plus a harsher case where ids
are renamed too (so `<label for>` breaks); later strategies still find every field.

### Portal quirks handled

| Quirk | Where | How |
|---|---|---|
| **Dependent field** | Release form | The **Target** branch picker only exists after a *new* tag is created in the tag dialog. The flow creates the tag, waits for Target to appear, selects the branch, then waits until the control shows it. An existing tag hides Target (GitHub says "Duplicate tag name"), which is a final input error. |
| **Download event** | Release page | `expect_download` around the asset click. Saved under `downloads/`, sha256 compared with the digest GitHub shows. The signed CDN URL is not recorded, because its query string is a bearer token. |
| **Modal dialogs** | Tag picker, "Create new tag", "Delete this release?" | Resolved by role inside the dialog and confirmed explicitly. |
| **iframe** | Resolver | Every frame is searched; proven by the iframe mutation. |
| **Auto-submitting 2FA field** | Login | Waits for the navigation the 6th digit triggers; falls back to Verify / Enter. |
| **Menus that re-render while filtering** | Branch picker | Clicks the branch directly; filters only when it isn't listed, then lets the list settle. Pressing Escape after selecting cancels the choice, so we don't. |

### Which failures retry

| Retried (bounded: 3 attempts, 1 s → 2 s → 4 s backoff, ±30 % jitter, capped at 8 s) | Final, never retried |
|---|---|
| `ResolveError` (element not there yet) | `AuthError`: wrong password, unexpected device verification, repo not reachable |
| Playwright timeouts and navigation errors | `TwoFactorError`: code rejected twice |
| Our own `RetryableError` (e.g. Target didn't update, download failed) | `FinalInputError`: existing tag, unknown branch |
| | `ConfigError`: missing env vars, invalid input JSON (exit 2) |

Retries are per stage, and each stage is safe to repeat: `form.fill` reloads a clean form,
`form.submit` (2 attempts) first checks whether the previous attempt already saved, so it never
creates a duplicate draft. When attempts run out the error becomes `RetriesExhausted` (final).

On a final failure the run writes `runs/<id>/evidence/`:

- `screenshot.png`: full page with every input, avatar and account name painted over
- `page.html`: DOM snapshot, scrubbed of hidden input values, CSRF/analytics meta, field values,
  emails, account identifiers
- `url.txt`, `console.log`: redacted
- `failure.json`: stage key, error type, root cause, retryable flag, retry history, and the
  resolver's attempts for the element that failed

It then deletes the draft if this run already saved one, so a failed run leaves the sandbox clean,
and still writes `manifest.json` with `status: failed`.

**Redaction:** password, username, TOTP secret, every generated code, and the login handle and
display name read from the page after sign-in are all registered with one redactor. Every log line
and every file written goes through it, along with masking for stand-alone 6–8 digit numbers and
signed-URL tokens (`sig`, `jwt`, `token`, …). Before committing `examples/`, a scan for the real
credentials, session cookie values, CSRF tokens, signed-URL tokens and emails found nothing.

### What I'd change first for a hundred portals

1. **Portal definitions as data plus small plugins.** Today `flows/github.py` holds both targets
   and steps. I'd split each portal into a declarative target file (YAML/JSON) and a small step
   script, so most portals need no Python and target fixes don't need a deploy.
2. **A locator health loop.** Record which strategy won for each target on every run. When a target
   drifts from `role` down to `css`, flag it before it breaks, and auto-save the page as a new
   offline fixture.
3. **More code providers behind the same interface:** IMAP (with message-id dedupe), an SMS
   gateway webhook, and per-portal secrets in a vault instead of `.env`.
4. **A job queue in front of the runner** with per-portal concurrency limits, so one slow portal
   or rate limit can't starve the rest, plus a browser pool and per-portal proxy/locale settings.
5. **Per-account session store** (encrypted, with TTL) instead of one local file, and a nightly
   canary run per portal that reports failures through the same evidence bundle.
