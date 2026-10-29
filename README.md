# Bravebird Automation Task

A single-process Playwright CLI that logs into a real portal with a second factor (TOTP),
reuses the saved session on later runs, navigates to a nested form, captures a document,
and writes a JSON manifest of the run.

> Status: environment set up. Implementation in progress.

## Requirements

| Tool | Version | Purpose |
|---|---|---|
| macOS / Linux | — | Tested on macOS 26 (Apple Silicon) |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.13 | Python version + dependency manager |
| Python | 3.12 | Runtime (installed by uv) |
| Chromium | Playwright-managed | Browser driven by the script |
| [gh](https://cli.github.com/) (optional) | ≥ 2.100 | Only used to publish this repo; not needed to run the script |

Python dependencies (pinned in `uv.lock`):

- `playwright`: browser automation
- `pyotp`: TOTP code generation for the second factor
- `pydantic`: input JSON validation and manifest schema
- `python-dotenv`: loads credentials from `.env`
- dev: `pytest`, `pytest-asyncio`, `ruff`

## Setup

Install uv once (if you don't have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.zshrc to make it permanent
```

Then, from the repo root, run one command:

```bash
uv sync && uv run playwright install chromium
```

`uv sync` fetches Python 3.12 if needed, creates `.venv/`, and installs the locked dependencies.

Check it worked:

```bash
uv run python -c "import playwright, pyotp; print('ok')"
```

## Environment variables

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.

| Variable | Description |
|---|---|
| `PORTAL_USERNAME` | Login username / email for your own account |
| `PORTAL_PASSWORD` | Account password |
| `TOTP_SECRET` | Base32 TOTP secret (the "setup key" shown when enabling an authenticator app) |

## Usage

_TBD: run command, input JSON format, and output layout._

## Design answers

_To be completed alongside the implementation._

- **How the code arrives, and what happens when it is wrong:** TBD
- **How a dead session is detected:** TBD
- **Locator strategy order, and why:** TBD
- **Portal quirks handled:** TBD
- **Which failures retry:** TBD
- **What I'd change first for a hundred portals:** TBD
