"""Load search and detail.

Every load in a response goes through `public_dict()`, which is where `MAX_BUY`
is dropped (CLAUDE.md principle 1). Nothing in this module has to remember to
strip it, and nothing in it can forget to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api import phrasing
from app.api.deps import get_container, get_deadline, require_verified, session_for
from app.api.schema_sample import SCHEMA_SAMPLE_RUN_ID, sample_search_loads
from app.api.schemas import LoadDetailResponse, LoadSearchRequest, LoadSearchResponse
from app.api.search_normalization import build_search_filters
from app.container import Container
from app.deadline import Deadline
from app.events import EventType
from app.results import ResultCode

router = APIRouter(tags=["loads"])

#: The brief caps a pitch at three loads. Fixing it here rather than exposing it
#: means the agent cannot widen its own search.
MAX_RESULTS = 3


@router.post("/loads/search", response_model=LoadSearchResponse)
def search_loads(
    payload: LoadSearchRequest,
    container: Container = Depends(get_container),
    deadline: Deadline = Depends(get_deadline),
) -> LoadSearchResponse:
    """Find up to three loads (full detail), and remember them as shown."""
    if payload.run_id == SCHEMA_SAMPLE_RUN_ID:
        return sample_search_loads()

    session = require_verified(session_for(container, payload.run_id))

    filters = build_search_filters(
        equipment=payload.equipment,
        orig_city=payload.orig_city,
        orig_state=payload.orig_state,
        orig_zip=payload.orig_zip,
        dest_city=payload.dest_city,
        dest_state=payload.dest_state,
        dest_zip=payload.dest_zip,
        pickup_date=payload.pickup_date,
    )

    summaries = container.tms.query_loads(max_results=MAX_RESULTS, deadline=deadline, **filters)
    # The pitch needs the full record, not the summary: fetch each load's detail
    # so search alone carries delivery_dt, weight, commodity, pieces, dims and
    # notes. rate and MAX_BUY are stripped by public_dict().
    loads = [container.tms.get_load(load.load_id, deadline=deadline) for load in summaries]

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

    # One event per load actually put in front of the carrier. This is the
    # pitch: the search response *is* what the agent reads out (DECISIONS §56
    # moved the detail into it), so a load returned here is a load pitched.
    # Before this, `load_pitched` came only from `GET /loads/{load_id}` — a
    # route the platform no longer calls — which left the count at zero for
    # every load in Twin.
    for load in loads:
        container.sink.emit(
            payload.run_id, EventType.LOAD_PITCHED, load_id=load.load_id, rate=load.rate
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
    """One sentence the agent can read out about this load.

    No rate: the price comes from `open_negotiation`, not the pitch.
    """
    return (
        f"That one picks up in {load.orig_city}, {load.orig_state} "
        f"and delivers to {load.dest_city}, {load.dest_state}."
    )
