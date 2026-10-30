"""Saved state, input validation, the session check and the runner's last-resort error path.

Offline: GitHub responses are faked with page.route.
"""

import json
import os

import pytest
from pydantic import ValidationError

from app import runner as runner_mod
from app.config import Credentials, RunInput
from app.errors import FinalInputError, RetryableError
from app.flows import github
from app.resolver import Target, resolve
from app.runner import RunOptions, execute
from app.session import load_state, save_state

BASE = {"repo": "o/r", "form": {"tag": "v9", "title": "t"},
        "document": {"release_tag": "v1", "asset_name": "a.pdf"}}


# -- saved state ------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "not json at all",
    '{"cookies": "nope"}',
    '{"cookies": [{}], "origins": []}',  # parses, but Playwright would refuse it
    '{"cookies": [1], "origins": []}',
    '{"cookies": [{"name": "a", "value": "b", "domain": "x", "path": "/", "expires": "soon"}]}',
    '{"cookies": [], "origins": {}}',
    '{"cookies": [], "origins": [{"origin": "https://x", "localStorage": [1]}]}',
])
def test_malformed_state_is_treated_as_absent(tmp_path, content):
    path = tmp_path / "github.storage.json"
    path.write_text(content)
    assert load_state(path) is None


def test_valid_state_is_loaded(tmp_path):
    path = tmp_path / "github.storage.json"
    path.write_text(json.dumps({"cookies": [{
        "name": "a", "value": "b", "domain": ".x.test", "path": "/", "expires": -1,
        "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []}))
    assert load_state(path) == path


async def test_state_is_written_owner_only_from_the_start(tmp_path):
    class FakeContext:
        async def storage_state(self):
            return {"cookies": [], "origins": []}

    old = os.umask(0o022)
    try:
        path = tmp_path / "state" / "github.storage.json"
        await save_state(FakeContext(), path)
    finally:
        os.umask(old)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert load_state(path) == path


# -- input validation -------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"base_url": "http://github.com"},  # credentials over plain http
    {"base_url": "https://evil.example/login?next="},
    {"repo": "../.."},
    {"repo": "o/r/../../settings"},
    {"portal": "../../elsewhere"},  # the portal names the state file
])
def test_inputs_that_could_misdirect_credentials_or_files_are_rejected(override):
    with pytest.raises(ValidationError):
        RunInput.model_validate({**BASE, **override})


def test_ordinary_inputs_still_validate():
    RunInput.model_validate({**BASE, "repo": "Some-Owner/.github",
                             "base_url": "https://ghe.example.com:8443"})


# -- session check ----------------------------------------------------------

def _github_page(status: int, signed_in: bool):
    meta = '<meta name="user-login" content="someone">' if signed_in else ""
    return lambda route: route.fulfill(status=status, content_type="text/html",
                                       body=f"<html><head>{meta}</head><body>x</body></html>")


@pytest.mark.parametrize("status, signed_in, expected", [
    (200, True, True),
    (404, False, False),  # logged out: a private repo is a 404
    (200, False, False),
])
async def test_session_alive_needs_a_user_and_the_private_page(page, status, signed_in,
                                                               expected):
    await page.route("https://github.test/**", _github_page(status, signed_in))
    assert await github.session_alive(page, "https://github.test/o/r") is expected


async def test_signed_in_404_is_a_bad_repo_not_a_dead_session(page):
    """Don't throw away a working session (and log in again) over a typo in `repo`."""
    await page.route("https://github.test/**", _github_page(404, True))
    with pytest.raises(FinalInputError):
        await github.session_alive(page, "https://github.test/o/typo")


async def test_signed_in_server_error_is_retryable(page):
    await page.route("https://github.test/**", _github_page(503, True))
    with pytest.raises(RetryableError):
        await github.session_alive(page, "https://github.test/o/r")


async def test_cleanup_retry_is_a_no_op_once_the_draft_is_gone(page):
    seen = []

    async def handler(route):
        seen.append(route.request.url)
        await route.fulfill(status=404, body="gone")
    await page.route("https://github.test/**", handler)
    await github.delete_draft(page, "https://github.test/o/r", "t",
                              "https://github.test/o/r/releases/edit/untagged-abc",
                              check_first=True)
    assert seen == ["https://github.test/o/r/releases/edit/untagged-abc"]  # nothing else


# -- resolver ---------------------------------------------------------------

async def test_strategy_order_beats_frame_order(page):
    """A role match inside an iframe wins over a class match in the main frame."""
    await page.set_content(
        '<button class="go">Something else</button>'
        '<iframe name="inner" srcdoc="<button>Go</button>"></iframe>')
    await page.wait_for_function(
        "document.querySelector('iframe').contentDocument?.readyState === 'complete'")
    target = Target(key="t.go", role="button", name="Go", exact=True, css=(".go",))
    found = await resolve(page, target, timeout=3)
    assert found.strategy == "role" and found.frame != "main"


# -- runner: last-resort error path -----------------------------------------

async def test_unexpected_error_still_yields_a_failed_result_and_manifest(tmp_path,
                                                                          monkeypatch):
    async def broken_run(self):
        raise RuntimeError("bug in a step")
    monkeypatch.setattr(runner_mod.Runner, "run", broken_run)

    inputs = RunInput.model_validate({**BASE, "output_dir": str(tmp_path / "runs"),
                                      "state_dir": str(tmp_path / "state")})
    creds = Credentials(username="runner-user", password="runner-pass",
                        totp_secret="JBSWY3DPEHPK3PXP")
    result = await execute(inputs, creds, RunOptions())
    assert result.status == "failed" and result.exit_code == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "failed" and manifest["error"]["type"] == "RuntimeError"
