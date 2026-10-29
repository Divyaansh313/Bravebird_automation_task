"""Resolver against the saved GitHub login page, offline.

The full mutation suite (renamed classes, reordered siblings, wrapper div,
iframe) lands with the resolver hardening step; these cover the basics.
"""

import pytest

from app.flows.github import PASSWORD, SIGN_IN, USERNAME
from app.resolver import ResolveError, Target, resolve


async def test_login_fields_resolve_semantically(page, login_html):
    await page.set_content(login_html)
    user = await resolve(page, USERNAME, timeout=2)
    pwd = await resolve(page, PASSWORD, timeout=2)
    btn = await resolve(page, SIGN_IN, timeout=2)
    assert user.strategy == "role"
    assert pwd.strategy == "role"
    assert btn.strategy == "role"
    assert await user.locator.get_attribute("name") == "login"
    assert await pwd.locator.get_attribute("type") == "password"


async def test_honeypot_field_is_never_picked(page, login_html):
    await page.set_content(login_html)
    for target in (USERNAME, PASSWORD):
        resolved = await resolve(page, target, timeout=2)
        assert await resolved.locator.get_attribute("name") != "required_field_07b9"


async def test_falls_back_when_ids_and_labels_change(page, login_html):
    html = (login_html.replace('id="login_field"', 'id="user_x"')
            .replace('for="login_field"', 'for="nothing"')
            .replace('class="form-control  js-login-field"', 'class="zz"'))
    await page.set_content(html)
    resolved = await resolve(page, USERNAME, timeout=2)
    assert await resolved.locator.get_attribute("name") == "login"


async def test_failure_records_every_attempt(page):
    await page.set_content("<form><p>nothing here</p></form>")
    target = Target(key="x", role="textbox", name="Missing", labels=("Missing",),
                    near_text=("Missing",), structure=("form input",), css=("#missing",))
    with pytest.raises(ResolveError) as err:
        await resolve(page, target, timeout=0.3)
    strategies = {a.strategy for a in err.value.attempts}
    assert strategies == {"role", "label", "near_text", "structure", "css"}
    assert all(a.result == "none" for a in err.value.attempts)
