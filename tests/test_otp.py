import logging

import pyotp

from app.otp import Challenge, TotpProvider, WrongFirstCodeProvider
from app.redact import MASK, redactor

SECRET = pyotp.random_base32()


class FakeClock:
    def __init__(self, t: float):
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


async def test_code_matches_rfc6238():
    clock = FakeClock(1_000_000_005.0)  # 25 s left in window
    provider = TotpProvider(SECRET, clock=clock, sleep=clock.sleep)
    code = await provider.get_code(Challenge())
    assert code == pyotp.TOTP(SECRET).at(clock.t)
    assert clock.slept == []


async def test_waits_when_window_nearly_over():
    clock = FakeClock(1_000_000_019.0)  # 1 s left in window (period boundary at ...020)
    provider = TotpProvider(SECRET, clock=clock, sleep=clock.sleep)
    await provider.get_code(Challenge())
    assert len(clock.slept) == 1 and clock.slept[0] > 1


async def test_retry_never_reuses_rejected_code():
    clock = FakeClock(1_000_000_005.0)
    provider = TotpProvider(SECRET, clock=clock, sleep=clock.sleep)
    first = await provider.get_code(Challenge(attempt=1))
    second = await provider.get_code(Challenge(attempt=2))
    assert first != second
    assert second == pyotp.TOTP(SECRET).at(clock.t)


async def test_simulated_wrong_code_then_real():
    clock = FakeClock(1_000_000_005.0)
    provider = WrongFirstCodeProvider(TotpProvider(SECRET, clock=clock, sleep=clock.sleep))
    wrong = await provider.get_code(Challenge(attempt=1))
    assert wrong != pyotp.TOTP(SECRET).at(1_000_000_005.0)
    right = await provider.get_code(Challenge(attempt=2))
    assert right == pyotp.TOTP(SECRET).at(clock.t)


async def test_codes_never_reach_logs(caplog):
    from app.redact import RedactingFilter

    caplog.handler.addFilter(RedactingFilter())
    clock = FakeClock(1_000_000_005.0)
    provider = TotpProvider(SECRET, clock=clock, sleep=clock.sleep)
    with caplog.at_level(logging.DEBUG):
        code = await provider.get_code(Challenge())
        logging.getLogger("t").info("leak attempt: %s", code)
    assert code not in caplog.text
    assert MASK in caplog.text
    assert redactor.redact(f"x {code} y") == f"x {MASK} y"
