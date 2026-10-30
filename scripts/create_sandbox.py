"""Create the test sandbox in YOUR OWN GitHub account, plus matching input files.

    uv run python scripts/create_sandbox.py              # create (asks first)
    uv run python scripts/create_sandbox.py --yes        # no prompt
    uv run python scripts/create_sandbox.py --dry-run    # show what it would do
    make sandbox                                         # same as --yes

What it builds (same shape as the author's sandbox, so the flow works unchanged):

  <you>/bravebird-portal-sandbox   PRIVATE repo, so documents need a login
    branches:  <default branch>, staging        (staging = the Target choice)
    releases:  v1.0.0  with statement-v1.0.0.pdf
               v1.1.0  with statement-v1.1.0.pdf

and writes inputs/local_v1.json and inputs/local_v2.json pointing at it
(git-ignored). Run them with:

    uv run bravebird --input inputs/local_v1.json

Safe to re-run: anything that already exists is kept and skipped.
Needs the GitHub CLI signed in to the same account as PORTAL_USERNAME:
`gh auth login` (default scopes are enough to create; deleting the sandbox
later needs `gh auth refresh -s delete_repo`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASES = [("v1.0.0", "2026-10-01", "Sandbox release 1"),
            ("v1.1.0", "2026-10-30", "Sandbox release 2")]
TARGET_BRANCH = "staging"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


class Sandbox:
    def __init__(self, repo: str, gh: Runner, dry_run: bool):
        self.repo = repo
        self.gh = gh
        self.dry_run = dry_run

    def _do(self, what: str, args: list[str]) -> None:
        if self.dry_run:
            print(f"  would {what}:  gh {' '.join(args)}")
            return
        r = self.gh(args)
        if r.returncode != 0:
            raise SystemExit(f"Failed to {what}: {r.stderr.strip() or r.stdout.strip()}")
        print(f"  {what}: done")

    def repo_exists(self) -> bool:
        return self.gh(["repo", "view", self.repo, "--json", "name"]).returncode == 0

    def default_branch(self) -> str:
        r = self.gh(["api", f"repos/{self.repo}", "--jq", ".default_branch"])
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "main"

    def branch_exists(self, name: str) -> bool:
        return self.gh(["api", f"repos/{self.repo}/branches/{name}", "--jq", ".name"]
                       ).returncode == 0

    def release_assets(self, tag: str) -> list[str] | None:
        r = self.gh(["release", "view", tag, "-R", self.repo, "--json", "assets",
                     "--jq", "[.assets[].name]"])
        return json.loads(r.stdout) if r.returncode == 0 else None

    def ensure(self, pdfs: dict[str, Path]) -> str:
        if self.repo_exists():
            print(f"  repo {self.repo}: exists, keeping it")
        else:
            self._do(f"create private repo {self.repo}",
                     ["repo", "create", self.repo, "--private", "--add-readme",
                      "--description", "Sandbox for the Bravebird automation task"])
        default = "main" if self.dry_run and not self.repo_exists() else self.default_branch()

        if not self.dry_run and self.branch_exists(TARGET_BRANCH):
            print(f"  branch {TARGET_BRANCH}: exists")
        else:
            sha = "<sha of default branch>" if self.dry_run else self.gh(
                ["api", f"repos/{self.repo}/git/refs/heads/{default}", "--jq", ".object.sha"]
            ).stdout.strip()
            self._do(f"create branch {TARGET_BRANCH}",
                     ["api", f"repos/{self.repo}/git/refs", "-f",
                      f"ref=refs/heads/{TARGET_BRANCH}", "-f", f"sha={sha}"])

        for tag, _date, notes in RELEASES:
            asset = f"statement-{tag}.pdf"
            existing = None if self.dry_run else self.release_assets(tag)
            if existing is None:
                self._do(f"create release {tag} with {asset}",
                         ["release", "create", tag, str(pdfs[tag]), "-R", self.repo,
                          "--title", tag, "--notes", notes, "--target", default])
            elif asset not in existing:
                self._do(f"upload {asset} to {tag}",
                         ["release", "upload", tag, str(pdfs[tag]), "-R", self.repo])
            else:
                print(f"  release {tag}: exists with {asset}")
        return default


async def make_pdfs(out: Path) -> dict[str, Path]:
    """Small one-page statements, generated locally with the project's Playwright."""
    from playwright.async_api import async_playwright

    paths = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        for tag, date, _notes in RELEASES:
            await page.set_content(
                f"<h1>Bravebird Sandbox Statement</h1><p>Release {tag}</p>"
                f"<p>Date: {date}</p><p>Test document for the automation assignment.</p>")
            paths[tag] = out / f"statement-{tag}.pdf"
            await page.pdf(path=str(paths[tag]))
        await browser.close()
    return paths


def input_files(repo: str, default_branch: str) -> dict[str, dict]:
    """Two inputs that differ only in data: different tag, target branch and document."""
    base = {"portal": "github", "base_url": "https://github.com", "repo": repo}
    return {
        "local_v1.json": {**base,
                          "form": {"tag": "v2.0.0-rc1", "target": TARGET_BRANCH,
                                   "title": "Bravebird automated draft",
                                   "notes": "Drafted by the Bravebird automation run.",
                                   "label": "Pre-release"},
                          "document": {"release_tag": "v1.0.0",
                                       "asset_name": "statement-v1.0.0.pdf"}},
        "local_v2.json": {**base,
                          "form": {"tag": "v3.0.0-beta", "target": default_branch,
                                   "title": "Second automated draft",
                                   "notes": "Different inputs, same code.", "label": "None"},
                          "document": {"release_tag": "v1.1.0",
                                       "asset_name": "statement-v1.1.0.pdf"}},
    }


def main(argv: list[str] | None = None, gh: Runner = run_gh) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="bravebird-portal-sandbox", help="Repository name")
    ap.add_argument("--owner", help="Account to create it in (default: the gh login)")
    ap.add_argument("--inputs-dir", type=Path, default=ROOT / "inputs")
    ap.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan, change nothing")
    a = ap.parse_args(argv)

    if gh is run_gh and not shutil.which("gh"):
        print("The GitHub CLI (gh) is required: https://cli.github.com, then `gh auth login`.")
        return 2
    owner = a.owner
    if not owner:
        who = gh(["api", "user", "--jq", ".login"])
        if who.returncode != 0 or not who.stdout.strip():
            print("gh is not signed in. Run `gh auth login` with the account from your .env.")
            return 2
        owner = who.stdout.strip()
    repo = f"{owner}/{a.name}"

    print(f"Sandbox: {repo} (private){'  [dry run]' if a.dry_run else ''}")
    if not (a.yes or a.dry_run) and input(
            "Create or complete it in this account? [y/N] ").strip().lower() != "y":
        print("Nothing changed.")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        pdfs = ({tag: Path(tmp) / f"statement-{tag}.pdf" for tag, *_ in RELEASES}
                if a.dry_run else asyncio.run(make_pdfs(Path(tmp))))
        default = Sandbox(repo, gh, a.dry_run).ensure(pdfs)

    for name, data in input_files(repo, default).items():
        path = a.inputs_dir / name
        if a.dry_run:
            print(f"  would write {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
            continue
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

    if not a.dry_run:
        print("\nReady. Make sure PORTAL_USERNAME in .env is this same account, then:\n"
              "  uv run bravebird --input inputs/local_v1.json\n"
              "  uv run bravebird --input inputs/local_v2.json   # different document\n"
              f"Remove it later: gh repo delete {repo}  (needs: gh auth refresh -s delete_repo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
