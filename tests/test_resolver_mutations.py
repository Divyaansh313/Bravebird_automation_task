"""Every flow target, on every saved page, under every mutation the reviewers apply
(renamed classes, reordered siblings, an added wrapper div, an iframe) plus a copy
rewrite ("relabel") that removes every visible word and naming attribute, so the
structural and id/class fallbacks are exercised too.
Fully offline: the browser blocks all network requests.
"""

import pytest

from app.offline import FIXTURES, PAGE_TARGETS, VARIANTS, apply_mutations, resolve_all


@pytest.mark.parametrize("page_key", sorted(PAGE_TARGETS))
@pytest.mark.parametrize("mutations", VARIANTS, ids=lambda m: "+".join(m) or "original")
async def test_targets_survive_mutation(page, page_key, mutations):
    fixture, targets = PAGE_TARGETS[page_key]
    await page.set_content((FIXTURES / fixture).read_text(encoding="utf-8"))
    await apply_mutations(page, mutations)
    rows = await resolve_all(page, targets, timeout=3)
    missing = [r["target"] for r in rows if not r["found"]]
    assert not missing, f"{page_key} + {mutations}: not found {missing}"
    if "iframe" in mutations:
        assert all(r["frame"] != "main" for r in rows), "should be found inside the iframe"
    if "relabel" in mutations:
        # No words left to match, so only wording-independent strategies can win.
        assert {r["strategy"] for r in rows} <= {"structure", "css"}, rows


async def test_renamed_ids_and_labels_fall_through_to_later_strategies(page):
    """Harsher than the brief: ids gone too, so <label for> links break."""
    html = (FIXTURES / "github_login.html").read_text(encoding="utf-8")
    html = html.replace('id="login_field"', 'id="a1"').replace('id="password"', 'id="a2"')
    await page.set_content(html)
    await apply_mutations(page, ["classes", "wrapper"])
    _, targets = PAGE_TARGETS["login"]
    rows = await resolve_all(page, targets, timeout=3)
    assert all(r["found"] for r in rows)
    by_key = {r["target"]: r for r in rows}
    assert by_key["login.username"]["strategy"] != "role"  # name came from <label for=id>
