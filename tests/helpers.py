"""Builders shared by the phase 2 tests."""

from __future__ import annotations

from datetime import datetime, timezone

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
