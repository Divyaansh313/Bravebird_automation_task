"""Redaction of credentials, codes and account identifiers.

Every secret the run touches is registered here. Logging, evidence bundles and
the manifest all pass text through `redact()` before it is written anywhere.
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "[REDACTED]"

# Values shorter than this are not registered as literal secrets: masking every
# "a" or "1" in a log would destroy it. Codes are handled by the pattern below.
_MIN_LITERAL_LEN = 3

# Stand-alone 6-8 digit numbers look like one-time codes. Masked everywhere as a
# backstop in case a code ends up in a URL, a page dump or an exception message.
# Digit runs inside hex hashes or ids (letters on either side) are left alone.
_OTP_PATTERN = re.compile(r"(?<![0-9A-Za-z])\d{6,8}(?![0-9A-Za-z])")


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
        return _OTP_PATTERN.sub(MASK, text)


# One process-wide instance: the CLI is single-process by design.
redactor = Redactor()


class RedactingFilter(logging.Filter):
    """Redacts the fully formatted message of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redactor.redact(message)
        record.args = None
        return True


def configure_logging(level: int = logging.INFO, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
