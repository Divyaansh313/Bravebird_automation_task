"""Failure taxonomy: retryable failures are transient, final ones are not."""


class AutomationError(Exception):
    retryable = False


class RetryableError(AutomationError):
    """Transient: timeouts, navigation hiccups, a page still rendering."""

    retryable = True


class FinalError(AutomationError):
    """Retrying cannot help: bad credentials, rejected 2FA twice, blocked account."""


class AuthError(FinalError):
    pass


class TwoFactorError(FinalError):
    pass


class FinalInputError(FinalError):
    """The inputs ask for something the portal will not accept (e.g. existing tag)."""
