"""Builders shared by the phase 2 tests."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.mail import MailError, OutboundEmail
from app.tms.models import LoadDetail

#: Matches the recorded LOAD_GET sample: RATE 2073 under MAX_BUY 2528.
RECORDED_LOAD_ID = "LD00896"
RECORDED_RATE = 2073
RECORDED_CEILING = 2528


def make_load(
    *,
    load_id: str = RECORDED_LOAD_ID,
    rate: int = RECORDED_RATE,
    max_buy: int | None = RECORDED_CEILING,
    pickup_dt: datetime | None = None,
) -> LoadDetail:
    """A LoadDetail shaped like the live server returns one."""
    return LoadDetail(
        load_id=load_id,
        orig_city="Fort Lauderdale",
        orig_state="FL",
        orig_zip="33301",
        dest_city="Pittsburgh",
        dest_state="PA",
        dest_zip="15219",
        pickup_dt=pickup_dt or datetime(2026, 10, 6, 22, 18, tzinfo=timezone.utc),
        eqtype="DRY_VAN",
        rate=rate,
        miles=989,
        status="OPEN",
        max_buy=max_buy,
    )


class FakeMailer:
    """Records sent emails; fails on demand. The code is in the body."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[OutboundEmail] = []
        self.fail = fail

    def send(self, email: OutboundEmail) -> None:
        if self.fail:
            raise MailError("resend refused")
        self.sent.append(email)

    @property
    def last_code(self) -> str | None:
        if not self.sent:
            return None
        match = re.search(r"\d{6}", self.sent[-1].body)
        return match.group(0) if match else None
