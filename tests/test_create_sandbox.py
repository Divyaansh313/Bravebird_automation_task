"""scripts/create_sandbox.py against a fake `gh`: plan, idempotency, inputs."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app.config import RunInput

SPEC = importlib.util.spec_from_file_location(
    "create_sandbox", Path(__file__).parent.parent / "scripts" / "create_sandbox.py")
sandbox = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sandbox)


class FakeGh:
    """Remembers what exists; records every mutating call."""

    def __init__(self, repo_exists=False, branches=(), releases=None, default="main"):
        self.repo_exists = repo_exists
        self.branches = set(branches)
        self.releases = dict(releases or {})
        self.default = default
        self.writes: list[list[str]] = []

    def __call__(self, args):
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        fail = subprocess.CompletedProcess(args, 1, "", "not found")
        cmd = " ".join(args)
        if args[:2] == ["api", "user"]:
            return ok("reviewer\n")
        if args[:2] == ["repo", "view"]:
            return ok("{}") if self.repo_exists else fail
        if args[:2] == ["repo", "create"]:
            self.writes.append(args)
            self.repo_exists = True
            return ok()
        if args[0] == "api" and args[1].endswith("/git/refs") and "-f" in args:
            self.writes.append(args)
            self.branches.add("staging")
            return ok()
        if args[0] == "api" and "/git/refs/heads/" in args[1]:
            return ok("abc123\n")
        if args[0] == "api" and "/branches/" in args[1]:
            return ok("staging") if args[1].rsplit("/", 1)[1] in self.branches else fail
        if args[0] == "api" and "--jq" in args and ".default_branch" in cmd:
            return ok(self.default + "\n")
        if args[:2] == ["release", "view"]:
            tag = args[2]
            return ok(json.dumps(self.releases[tag])) if tag in self.releases else fail
        if args[:2] in (["release", "create"], ["release", "upload"]):
            self.writes.append(args)
            tag = args[2]
            self.releases.setdefault(tag, []).append(f"statement-{tag}.pdf")
            return ok()
        raise AssertionError(f"unexpected gh call: {cmd}")


@pytest.fixture
def no_pdfs(monkeypatch, tmp_path):
    async def fake_pdfs(out):
        return {tag: tmp_path / f"statement-{tag}.pdf" for tag, *_ in sandbox.RELEASES}
    monkeypatch.setattr(sandbox, "make_pdfs", fake_pdfs)


def test_creates_everything_in_a_fresh_account(tmp_path, no_pdfs):
    gh = FakeGh(default="main")
    assert sandbox.main(["--yes", "--inputs-dir", str(tmp_path)], gh=gh) == 0
    kinds = [" ".join(w[:2]) for w in gh.writes]
    assert kinds == ["repo create", "api repos/reviewer/bravebird-portal-sandbox/git/refs",
                     "release create", "release create"]
    assert "--private" in gh.writes[0]

    v1 = json.loads((tmp_path / "local_v1.json").read_text())
    v2 = json.loads((tmp_path / "local_v2.json").read_text())
    assert v1["repo"] == "reviewer/bravebird-portal-sandbox"
    assert v1["form"]["target"] == "staging"
    assert v2["form"]["target"] == "main"  # the account's real default branch
    RunInput.model_validate(v1)  # generated inputs are valid run inputs
    RunInput.model_validate(v2)


def test_rerun_changes_nothing(tmp_path, no_pdfs):
    gh = FakeGh(repo_exists=True, branches={"master", "staging"}, default="master",
                releases={"v1.0.0": ["statement-v1.0.0.pdf"],
                          "v1.1.0": ["statement-v1.1.0.pdf"]})
    assert sandbox.main(["--yes", "--inputs-dir", str(tmp_path)], gh=gh) == 0
    assert gh.writes == []
    assert json.loads((tmp_path / "local_v2.json").read_text())["form"]["target"] == "master"


def test_missing_asset_is_uploaded(tmp_path, no_pdfs):
    gh = FakeGh(repo_exists=True, branches={"main", "staging"},
                releases={"v1.0.0": [], "v1.1.0": ["statement-v1.1.0.pdf"]})
    sandbox.main(["--yes", "--inputs-dir", str(tmp_path)], gh=gh)
    assert [w[:3] for w in gh.writes] == [["release", "upload", "v1.0.0"]]


def test_dry_run_writes_nothing(tmp_path, capsys):
    gh = FakeGh()
    assert sandbox.main(["--dry-run", "--inputs-dir", str(tmp_path)], gh=gh) == 0
    assert gh.writes == [] and not list(tmp_path.iterdir())
    assert "would create private repo" in capsys.readouterr().out


def test_declining_the_prompt_changes_nothing(tmp_path, monkeypatch, no_pdfs):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    gh = FakeGh()
    assert sandbox.main(["--inputs-dir", str(tmp_path)], gh=gh) == 1
    assert gh.writes == []
