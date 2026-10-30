"""Redaction of credentials, codes and account identifiers.

Every secret the run touches is registered here. Logging, evidence bundles and
the manifest all pass text through `redact()` before it is written anywhere.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading

MASK = "[REDACTED]"

# Values shorter than this are not registered as literal secrets: masking every
# "a" or "1" in a log would destroy it. Codes are handled by the pattern below.
_MIN_LITERAL_LEN = 3

# Backstop for one-time codes we did not generate ourselves (e.g. a code that
# arrives by email or SMS and shows up in a page dump or an error message).
# Codes we generate are registered as literal secrets and masked anywhere.
# This pattern only masks a stand-alone 6-8 digit number when code-like words
# come shortly before it ("verification code is 123456", "otp=123456"), so
# order numbers, invoice ids and amounts on other portals stay readable.
_CODE_WORDS = (r"otp|totp|2fa|mfa|passcode|pin|one[- ]time|verification|verify|"
               r"security|auth(?:entication)?|sms|code")
_OTP_PATTERN = re.compile(
    rf"((?<![a-z])(?:{_CODE_WORDS})(?![a-z])"   # a code-like word
    r"(?:[^0-9a-z\n]{1,4}[a-z]+){0,3}"            # up to 3 more words ("code is", "was sent")
    r"[^0-9a-z\n]{1,6})"                          # separator: space, ":", "=", "is "
    r"(\d{6,8})(?![0-9a-z])",
    re.IGNORECASE)


# Signed-URL credentials (S3/Azure SAS/JWT style). Short-lived, but still bearer tokens.
_URL_SECRET = re.compile(
    r"([?&](?:sig|signature|token|access_token|jwt|code|x-amz-signature|x-amz-credential|"
    r"x-amz-security-token|skoid|sktid)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)


class Redactor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def add(self, *values: str | None) -> None:
        with self._lock:
            for value in values:
                if value and len(value) >= _MIN_LITERAL_LEN:
                    self._secrets.add(value)
                    # Usernames/emails also appear lower-cased in URLs and markup.
                    self._secrets.add(value.lower())

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = re.sub(re.escape(secret), MASK, text, flags=re.IGNORECASE)
        text = _URL_SECRET.sub(rf"\1{MASK}", text)
        return _OTP_PATTERN.sub(rf"\1{MASK}", text)

    def redact_obj(self, obj):
        """Redact every string inside a JSON-like structure.

        Use this instead of redacting serialized JSON: it leaves numbers alone
        (a 2 MB file size must stay a number, not become an invalid
        `[REDACTED]` token), and it sees secrets before JSON escapes their
        quotes or backslashes.
        """
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact(k) if isinstance(k, str) else k: self.redact_obj(v)
                    for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact_obj(v) for v in obj]
        return obj

    def redact_json(self, obj, **dumps_kwargs) -> str:
        """json.dumps(obj) with every string value redacted first."""
        plain = json.loads(json.dumps(obj, default=str))
        return json.dumps(self.redact_obj(plain), **dumps_kwargs)


# One process-wide instance: the CLI is single-process by design.
redactor = Redactor()


class RedactingFilter(logging.Filter):
    """Redacts the fully formatted message of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redactor.redact(message)
        record.args = None
        # Tracebacks are formatted later by the handler, after this filter, so
        # render and redact them here (exception messages can carry URLs/values).
        if record.exc_info:
            record.exc_text = redactor.redact(
                logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redactor.redact(record.stack_info)
        return True


# Which run the current asyncio task belongs to. Tasks copy their context when
# created, so concurrent jobs each see their own value.
current_run: contextvars.ContextVar[str] = contextvars.ContextVar("current_run", default="-")


class RunTagFilter(logging.Filter):
    """Adds %(run)s to records so interleaved console output stays readable."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run = current_run.get()
        return True


class OnlyRunFilter(logging.Filter):
    """Keeps a per-run log file free of other concurrent runs' lines."""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return current_run.get() == self.run_id


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(run)s] %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO, *, tag_runs: bool = False) -> None:
    """Console logging, redacted. Idempotent; safe to call more than once."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT if tag_runs else _FORMAT,
                                           "%H:%M:%S"))
    handler.addFilter(RunTagFilter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if getattr(h, "_bravebird_run", None)]
    root.addHandler(handler)
    root.setLevel(level)


def attach_run_log(path: str, run_id: str) -> logging.Handler:
    """Add a redacted file handler that only records lines from `run_id`."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT, "%H:%M:%S"))
    handler.addFilter(OnlyRunFilter(run_id))
    handler.addFilter(RedactingFilter())
    handler._bravebird_run = run_id  # type: ignore[attr-defined]
    logging.getLogger().addHandler(handler)
    return handler


def detach_run_log(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()
