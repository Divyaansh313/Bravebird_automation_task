"""Offline resolver runner and HTML mutator. No network access at all.

    uv run bravebird-offline resolve --page form tests/fixtures/github_release_form.html
    uv run bravebird-offline mutate --all in.html out.html
    uv run bravebird-offline check            # every fixture x every mutation

`resolve` loads saved HTML into a browser with all requests blocked, runs every
target for that page through the resolver and prints the winning strategy.
Exit code 1 if any target is not found.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright

from .flows import github
from .redact import configure_logging
from .resolver import ResolveError, Target, resolve

FIXTURES = Path("tests/fixtures")

PAGE_TARGETS: dict[str, tuple[str, list[Target]]] = {
    "login": ("github_login.html", [github.USERNAME, github.PASSWORD, github.SIGN_IN]),
    "repo": ("github_repo.html", [github.REPO_RELEASES_LINK]),
    "releases": ("github_releases.html", [github.DRAFT_NEW_LINK, github.release_link("v1.0.0")]),
    "form": ("github_release_form.html", [
        github.TAG_BUTTON, github.TARGET_BUTTON, github.TITLE, github.NOTES,
        github.release_label("Pre-release"), github.SAVE_DRAFT,
    ]),
    "release": ("github_release_tag.html", [
        github.asset_link("v1.0.0", "statement-v1.0.0.pdf"),
    ]),
}

# Each mutation runs in the page and rewrites the live DOM.
MUTATIONS: dict[str, str] = {
    # Every class name replaced with a meaningless one.
    "classes": """() => {
        let n = 0;
        document.querySelectorAll('[class]').forEach(el => {
            el.setAttribute('class', [...el.classList].map(() => 'x' + (n++).toString(36)).join(' '));
        });
    }""",
    # Children of every container inside <main>/<form> in reverse order.
    "reorder": """() => {
        const roots = document.querySelectorAll('main, form, main *');
        roots.forEach(p => {
            if (!p.children || p.children.length < 2 || p.tagName === 'SELECT') return;
            [...p.children].reverse().forEach(c => p.appendChild(c));
        });
    }""",
    # Every interactive element wrapped in an extra <div>.
    "wrapper": """() => {
        document.querySelectorAll('input, textarea, button, a, select').forEach(el => {
            const w = document.createElement('div');
            w.className = 'added-wrapper';
            el.parentNode.insertBefore(w, el);
            w.appendChild(el);
        });
    }""",
    # Whole body moved into an iframe (srcdoc, so still offline).
    # Copy rewrite: every visible string becomes "Item N" and naming attributes
    # go, so role/label/nearby-text strategies can't match and the structural
    # and id/class fallbacks have to carry the lookup.
    "relabel": """() => {
        let n = 0;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        for (const t of nodes) {
            const p = t.parentElement;
            if (!p || ['SCRIPT', 'STYLE', 'TEXTAREA'].includes(p.tagName)) continue;
            if (t.nodeValue.trim()) t.nodeValue = ' Item ' + (n++) + ' ';
        }
        for (const attr of ['aria-label', 'aria-labelledby', 'aria-describedby', 'title',
                            'placeholder', 'alt']) {
            document.querySelectorAll('[' + attr + ']').forEach(el => el.removeAttribute(attr));
        }
        document.querySelectorAll('input[type=submit], input[type=button]')
            .forEach(el => el.value = 'Item ' + (n++));
    }""",
    "iframe": """() => {
        const html = '<!doctype html><html><head>' + document.head.innerHTML +
                     '</head><body>' + document.body.innerHTML + '</body></html>';
        document.body.innerHTML = '';
        const f = document.createElement('iframe');
        f.setAttribute('name', 'mutated-frame');
        f.style.width = '1280px'; f.style.height = '2000px'; f.style.border = '0';
        f.srcdoc = html;
        document.body.appendChild(f);
        return new Promise(r => f.addEventListener('load', () => r()));
    }""",
}
ALL_MUTATIONS = ["classes", "reorder", "wrapper", "relabel", "iframe"]  # iframe last: wraps

# Combined variants. "relabel" and "classes" together remove both the words and
# the class names at once, which is a full redesign rather than a mutation, so
# the copy rewrite is combined with the structural changes that keep classes.
COMBOS: dict[str, list[str]] = {
    "structural": ["classes", "reorder", "wrapper", "iframe"],
    "copy+structure": ["reorder", "wrapper", "relabel", "iframe"],
}
VARIANTS: list[list[str]] = [[], *[[m] for m in ALL_MUTATIONS], *COMBOS.values()]


async def _offline_page(pw) -> tuple:
    browser = await pw.chromium.launch()
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await page.route("**/*", lambda r: r.continue_()
                     if r.request.url.startswith(("about:", "data:")) else r.abort())
    return browser, page


async def apply_mutations(page: Page, names: list[str]) -> None:
    for name in names:
        await page.evaluate(MUTATIONS[name])


async def resolve_all(page: Page, targets: list[Target], timeout: float = 3.0) -> list[dict]:
    rows = []
    for t in targets:
        try:
            r = await resolve(page, t, timeout=timeout)
            rows.append({"target": t.key, "found": True, "strategy": r.strategy,
                         "query": r.query, "frame": r.frame,
                         "failed_before_win": len(r.attempts) - 1})
        except ResolveError as e:
            rows.append({"target": t.key, "found": False, "strategy": None, "query": None,
                         "frame": None, "failed_before_win": len(e.attempts)})
    return rows


def _print(rows: list[dict], header: str) -> None:
    print(f"\n{header}")
    for r in rows:
        mark = "OK  " if r["found"] else "MISS"
        where = f"{r['strategy']:<9} [{r['frame']}] {r['query']}" if r["found"] else "-"
        print(f"  {mark} {r['target']:<22} {where}")


async def cmd_resolve(html: Path, page_key: str, mutations: list[str]) -> int:
    targets = PAGE_TARGETS[page_key][1]
    async with async_playwright() as pw:
        browser, page = await _offline_page(pw)
        await page.set_content(html.read_text(encoding="utf-8"))
        await apply_mutations(page, mutations)
        rows = await resolve_all(page, targets)
        await browser.close()
    _print(rows, f"{html} (page={page_key}, mutations={'+'.join(mutations) or 'none'})")
    return 0 if all(r["found"] for r in rows) else 1


async def cmd_mutate(src: Path, dst: Path, mutations: list[str]) -> int:
    async with async_playwright() as pw:
        browser, page = await _offline_page(pw)
        await page.set_content(src.read_text(encoding="utf-8"))
        await apply_mutations(page, mutations)
        dst.write_text(await page.content(), encoding="utf-8")
        await browser.close()
    print(f"wrote {dst} ({'+'.join(mutations)})")
    return 0


async def cmd_check() -> int:
    variants = VARIANTS
    failures = 0
    async with async_playwright() as pw:
        browser, page = await _offline_page(pw)
        for key, (fixture, targets) in PAGE_TARGETS.items():
            html = (FIXTURES / fixture).read_text(encoding="utf-8")
            for muts in variants:
                await page.set_content(html)
                await apply_mutations(page, muts)
                rows = await resolve_all(page, targets)
                failures += sum(not r["found"] for r in rows)
                _print(rows, f"{key} + {'+'.join(muts) or 'original'}")
        await browser.close()
    print(f"\n{'ALL FOUND' if not failures else f'{failures} MISSING'}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bravebird-offline", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="Resolve a page's targets in saved HTML")
    r.add_argument("html", type=Path)
    r.add_argument("--page", choices=sorted(PAGE_TARGETS), required=True)
    r.add_argument("--mutate", action="append", default=[], choices=ALL_MUTATIONS + ["all"])
    m = sub.add_parser("mutate", help="Write a mutated copy of saved HTML")
    m.add_argument("src", type=Path)
    m.add_argument("dst", type=Path)
    m.add_argument("--mutate", action="append", default=[], choices=ALL_MUTATIONS)
    m.add_argument("--all", action="store_true", help="Apply every mutation")
    sub.add_parser("check", help="Every fixture against every mutation")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    configure_logging(logging.INFO if a.verbose else logging.WARNING)

    if a.cmd == "resolve":
        muts = ALL_MUTATIONS if "all" in a.mutate else a.mutate
        return asyncio.run(cmd_resolve(a.html, a.page, muts))
    if a.cmd == "mutate":
        return asyncio.run(cmd_mutate(a.src, a.dst, ALL_MUTATIONS if a.all else a.mutate))
    return asyncio.run(cmd_check())


if __name__ == "__main__":
    sys.exit(main())
