import pytest

from app.errors import AuthError, RetryableError
from app.evidence import scrub_html
from app.redact import MASK, redactor
from app.retry import RetriesExhausted, RetryLog, RetryPolicy, with_retry

FAST = RetryPolicy(attempts=3, base_delay=0.01, max_delay=0.02)


async def test_retryable_failure_is_retried_then_succeeds():
    calls = []

    async def flaky(attempt):
        calls.append(attempt)
        if attempt < 3:
            raise RetryableError("not yet")
        return "ok"

    log = RetryLog()
    assert await with_retry("s", flaky, policy=FAST, history=log) == "ok"
    assert calls == [1, 2, 3]
    assert [e["retryable"] for e in log.events] == [True, True]


async def test_final_failure_is_not_retried():
    calls = []

    async def bad_password(attempt):
        calls.append(attempt)
        raise AuthError("wrong password")

    with pytest.raises(AuthError):
        await with_retry("login", bad_password, policy=FAST)
    assert calls == [1]


async def test_retries_are_bounded():
    async def always(attempt):
        raise RetryableError("still broken")

    with pytest.raises(RetriesExhausted) as err:
        await with_retry("s", always, policy=FAST)
    assert "3 attempts" in str(err.value)
    assert not err.value.retryable  # exhausted counts as final


def test_backoff_grows_and_is_capped():
    p = RetryPolicy(base_delay=1, max_delay=8, jitter=0)
    assert [p.delay(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 8, 8]


def test_signed_url_tokens_are_masked():
    url = "https://cdn.example/f?sp=r&sig=AbC%2Fd&jwt=eyJ.x.y&rscd=attachment"
    out = redactor.redact(url)
    assert "AbC" not in out and "eyJ" not in out
    assert out.count(MASK) == 2 and "sp=r" in out and "rscd=attachment" in out


def test_hashes_survive_but_bare_codes_do_not():
    digest = "9149114fcc0135f66e89e5971295b768ba875039b4a39a705d0b494d62e6e235"
    assert redactor.redact(f"sha256:{digest}") == f"sha256:{digest}"
    assert redactor.redact("code 123456 sent") == f"code {MASK} sent"


def test_registered_identifiers_are_masked_case_insensitively():
    redactor.add("Some-Account-Name")
    assert redactor.redact("github.com/some-account-name/repo") == f"github.com/{MASK}/repo"


def test_evidence_html_drops_tokens_and_field_values():
    html = ('<meta name="csrf-token" content="tok123abc">'
            '<input type="hidden" name="authenticity_token" value="secretvalue">'
            '<input type="text" name="login" value="typed-user">')
    out = scrub_html(html)
    for leaked in ("tok123abc", "secretvalue", "typed-user"):
        assert leaked not in out


def test_evidence_html_masks_emails():
    out = scrub_html('<span>contact: someone.name+tag@example.co.uk</span>')
    assert "someone.name" not in out and "example.co.uk" not in out
