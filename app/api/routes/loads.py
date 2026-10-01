"""Load search and detail.

Every load in a response goes through `public_dict()`, which is where `MAX_BUY`
is dropped (CLAUDE.md principle 1). Nothing in this module has to remember to
strip it, and nothing in it can forget to.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.api import phrasing
from app.api.deps import get_container, get_deadline, require_verified, session_for
from app.api.schemas import LoadDetailResponse, LoadSearchRequest, LoadSearchResponse
from app.container import Container
from app.deadline import Deadline
from app.events import EventType
from app.results import ResultCode
from app.tms.client import FILTER_FIELDS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["loads"])

#: The brief caps a pitch at three loads. Fixing it here rather than exposing it
#: means the agent cannot widen its own search.
MAX_RESULTS = 3

#: Request field -> TMS filter name. The TMS name is not always the obvious one.
_FILTER_MAP = {
    "orig_city": "ORIG_CITY",
    "orig_state": "ORIG_STATE",
    "orig_zip": "ORIG_ZIP",
    "dest_city": "DEST_CITY",
    "dest_state": "DEST_STATE",
    "dest_zip": "DEST_ZIP",
    "equipment": "EQTYPE",
    "pickup_date": "PICKUP_DT",
}


@router.post("/loads/search", response_model=LoadSearchResponse)
def search_loads(
    payload: LoadSearchRequest,
    container: Container = Depends(get_container),
    deadline: Deadline = Depends(get_deadline),
) -> LoadSearchResponse:
    """Find up to three loads, and remember them as shown for this call."""
    session = require_verified(session_for(container, payload.run_id))

    filters = {
        _FILTER_MAP[field]: value
        for field, value in payload.model_dump(exclude={"run_id"}).items()
        if value not in (None, "") and field in _FILTER_MAP
    }

    loads = container.tms.query_loads(max_results=MAX_RESULTS, deadline=deadline, **filters)

    # Recorded before the response is built: what the carrier was shown is what
    # this call may later negotiate, and it is decided here and nowhere else.
    for load in loads:
        session.record_shown_load(load.load_id)

    # Emitted even when the count is zero: "we looked and found nothing" is the
    # fact a rep needs, and it is the one most easily lost.
    container.sink.emit(
        payload.run_id,
        EventType.SEARCH_PERFORMED,
        filters=filters,
        results=len(loads),
    )

    if not loads:
        return LoadSearchResponse(
            loads=[], count=0, result_code=ResultCode.NO_MATCH.value, say=phrasing.NO_MATCH
        )

    return LoadSearchResponse(
        loads=[load.public_dict() for load in loads],
        count=len(loads),
        say=phrasing.LOADS_FOUND.format(count=len(loads), s="" if len(loads) == 1 else "s"),
    )


@router.get("/loads/{load_id}", response_model=LoadDetailResponse)
def load_detail(
    load_id: str,
    run_id: str = Query(..., description="Fixed parameter injected by the platform."),
    container: Container = Depends(get_container),
    deadline: Deadline = Depends(get_deadline),
) -> LoadDetailResponse:
    """The detail needed to pitch one load.

    Gated by OTP like the search, even though CLAUDE.md §8 lists only search,
    negotiation and booking. Pitching freight to an unverified caller is the
    thing verification exists to prevent, so the stricter reading is the one
    implemented (see DECISIONS.md).

    Fetching the detail does **not** add the load to the shown list: if it did,
    "you may only negotiate what you were shown" would collapse into "you may
    negotiate anything you can name", and load ids are guessable.
    """
    require_verified(session_for(container, run_id))

    load = container.tms.get_load(load_id, deadline=deadline)
    container.sink.emit(
        run_id, EventType.LOAD_PITCHED, load_id=load.load_id, rate=load.rate
    )

    return LoadDetailResponse(load=load.public_dict(), say=_describe(load))


def _describe(load) -> str:
    """One sentence the agent can read out about this load."""
    return (
        f"That one picks up in {load.orig_city}, {load.orig_state} "
        f"and delivers to {load.dest_city}, {load.dest_state}. "
        f"The rate is {load.rate} dollars."
    )


# Keeps the filter names honest: if the TMS client stops accepting one, the
# mapping above is wrong and this fails at import rather than at call time.
assert set(_FILTER_MAP.values()) <= FILTER_FIELDS, "load filter map is out of date"
