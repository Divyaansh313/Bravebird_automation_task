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


def test_evidence_html_masks_any_nonce_or_token_meta():
    html = ('<meta name="fetch-nonce" content="v2:abcdef12-3456">'
            '<meta name="some-new-session-token" content="zzz999">'
            '<meta name="viewport" content="width=device-width">')
    out = scrub_html(html)
    assert "abcdef12" not in out and "zzz999" not in out
    assert "width=device-width" in out  # harmless metadata is kept


def test_evidence_html_masks_numeric_account_and_repo_ids():
    html = ('<img src="https://avatars.githubusercontent.com/u/12345678?v=4">'
            '<meta name="hovercard-subject-tag" content="repository:9876543210">'
            '<div data-upload-repository-id="9876543210"></div>')
    out = scrub_html(html)
    assert "12345678" not in out and "9876543210" not in out


def test_json_redaction_keeps_numbers_and_valid_json():
    """A 1-99 MB file size is a 7-8 digit number: it must stay a number."""
    import json

    data = {"bytes": 2_345_678, "note": "code 123456 sent", "nested": [{"n": 12345678}]}
    out = json.loads(redactor.redact_json(data))
    assert out["bytes"] == 2_345_678 and out["nested"][0]["n"] == 12345678
    assert out["note"] == f"code {MASK} sent"


def test_json_redaction_masks_secrets_that_json_would_escape():
    redactor.add('pa"ss\\word-x9')
    assert 'pa\\"ss' not in redactor.redact_json({"m": 'login with pa"ss\\word-x9 failed'})


def test_log_tracebacks_are_redacted(tmp_path):
    import logging

    from app.redact import RedactingFilter

    redactor.add("traceback-secret-value")
    handler = logging.FileHandler(tmp_path / "t.log")
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("bravebird.test.tb")
    log.addHandler(handler)
    try:
        raise RuntimeError("failed for traceback-secret-value")
    except RuntimeError:
        log.exception("boom")
    finally:
        log.removeHandler(handler)
        handler.close()
    text = (tmp_path / "t.log").read_text()
    assert "RuntimeError" in text and "traceback-secret-value" not in text


@pytest.mark.parametrize("text, masked", [
    ("Your verification code is 482913.", True),
    ("GET /login?otp=123456&x=1", True),
    ("2FA code was 99887766", True),
    ("pin: 1234567", True),
    ("Order 1234567 total 2500000 bytes", False),
    ("Invoice number 88776655 due", False),
    ("statement for account 12345678", False),
])
def test_digit_backstop_needs_code_context(text, masked):
    out = redactor.redact(text)
    assert (MASK in out) is masked, out


def test_generated_codes_are_masked_without_context():
    redactor.add("731905")  # what TotpProvider does with every code it hands out
    assert redactor.redact("value 731905 here") == f"value {MASK} here"


@pytest.mark.parametrize("html", [
    '<meta content="v2:secret-nonce" name="fetch-nonce">',          # content first
    "<meta name='csrf-token' content='secret-nonce'>",              # single quotes
    '<meta http-equiv="x-session-id" content="secret-nonce">',      # http-equiv
])
def test_meta_scrub_ignores_attribute_order_and_quotes(html):
    assert "secret-nonce" not in scrub_html(html)
