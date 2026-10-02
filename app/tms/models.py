"""Typed views over TMS records.

The parser (`app.tms.protocol`) returns flat ``{FIELD_NAME: "value"}``
dictionaries. This module turns those into objects, and is the only place that
knows which fields are required, which are numeric, and which must never leave
the server.

Note the deliberate asymmetry: :class:`LoadDetail` carries ``max_buy``, but
``public_dict()`` strips it. Any code that serialises a load for the voice
agent should use ``public_dict()`` so the ceiling cannot leak into the
conversation (CLAUDE.md principle 1).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.tms.protocol import TmsMalformed, parse_timestamp, to_int

#: Fields the LOAD_QUERY record must carry. Missing one makes the record
#: malformed rather than merely incomplete: a load without a rate cannot be
#: pitched, and silently defaulting it would put a made-up number in front of
#: a carrier.
REQUIRED_QUERY_FIELDS = (
    "LOAD_ID",
    "ORIG_CITY",
    "ORIG_STATE",
    "DEST_CITY",
    "DEST_STATE",
    "PICKUP_DT",
    "EQTYPE",
    "RATE",
    "STATUS",
)


def _require(record: dict[str, str], name: str) -> str:
    value = record.get(name)
    if value is None or value == "":
        raise TmsMalformed(f"record is missing required field {name}")
    return value


def _optional(record: dict[str, str], name: str) -> str | None:
    value = record.get(name)
    if value is None or value == "":
        return None
    return value


def _required_int(record: dict[str, str], name: str) -> int:
    return to_int(name, _require(record, name))


def _optional_int(record: dict[str, str], name: str) -> int | None:
    value = _optional(record, name)
    return None if value is None else to_int(name, value)


def _optional_timestamp(record: dict[str, str], name: str) -> datetime | None:
    value = _optional(record, name)
    return None if value is None else parse_timestamp(name, value)


class Load(BaseModel):
    """A load as returned by ``LOAD_QUERY``.

    ZIP codes stay strings: they are identifiers, and ``00123`` must not become
    ``123``.
    """

    model_config = ConfigDict(extra="ignore")

    load_id: str
    orig_city: str
    orig_state: str
    orig_zip: str | None = None
    dest_city: str
    dest_state: str
    dest_zip: str | None = None
    pickup_dt: datetime
    eqtype: str
    rate: int
    miles: int | None = None
    status: str

    #: Server fields we do not model yet. Kept so a new column is not silently
    #: dropped on the floor, and so it stays visible in the pitch payload.
    extra: dict[str, str] = Field(default_factory=dict, exclude=True)

    @classmethod
    def _known_fields(cls) -> set[str]:
        return set(REQUIRED_QUERY_FIELDS) | {"ORIG_ZIP", "DEST_ZIP", "MILES"}

    @classmethod
    def from_record(cls, record: dict[str, str]) -> "Load":
        """Build a :class:`Load`, or raise :class:`TmsMalformed`."""
        return cls(
            load_id=_require(record, "LOAD_ID"),
            orig_city=_require(record, "ORIG_CITY"),
            orig_state=_require(record, "ORIG_STATE"),
            orig_zip=_optional(record, "ORIG_ZIP"),
            dest_city=_require(record, "DEST_CITY"),
            dest_state=_require(record, "DEST_STATE"),
            dest_zip=_optional(record, "DEST_ZIP"),
            pickup_dt=parse_timestamp("PICKUP_DT", _require(record, "PICKUP_DT")),
            eqtype=_require(record, "EQTYPE"),
            rate=_required_int(record, "RATE"),
            miles=_optional_int(record, "MILES"),
            status=_require(record, "STATUS"),
            extra={k: v for k, v in record.items() if k not in cls._known_fields()},
        )

    @property
    def is_open(self) -> bool:
        """True while the load can still be booked on this token."""
        return self.status.strip().upper() == "OPEN"

    def public_dict(self) -> dict[str, object]:
        """Serialisable view safe to hand to the voice agent.

        `rate` (the posted rate) is excluded too: the price the agent may quote
        comes only from `open_negotiation`, never from a search or detail
        response. If the posted rate sits above the ceiling, quoting it would
        leak a number above what we will pay.
        """
        data = self.model_dump(exclude={"extra", "rate"}, mode="json")
        data.update(self.extra)
        return data


class LoadDetail(Load):
    """A load as returned by ``LOAD_GET``: everything in :class:`Load` plus the
    fields the server only discloses for a single load.

    ``MAX_BUY`` lives here and is excluded from :meth:`public_dict`.
    """

    delivery_dt: datetime | None = None
    weight: int | None = None
    commodity: str | None = None
    pieces: int | None = None
    dims: str | None = None
    notes: str = ""
    #: The ceiling the negotiation engine concedes toward. repr=False keeps it
    #: out of casual logging; public_dict() is what actually guarantees it
    #: never reaches the agent.
    max_buy: int | None = Field(default=None, repr=False)

    @classmethod
    def _known_fields(cls) -> set[str]:
        return super()._known_fields() | {
            "DELIVERY_DT",
            "WEIGHT",
            "COMMODITY",
            "PIECES",
            "DIMS",
            "NOTES",
            "MAX_BUY",
        }

    @classmethod
    def from_record(cls, record: dict[str, str]) -> "LoadDetail":
        base = Load.from_record(record)
        # Recompute the leftovers against *this* class's field list: the base
        # call would file MAX_BUY as "unknown" and hand it back in `extra`,
        # which public_dict() would then happily serialise.
        extra = {k: v for k, v in record.items() if k not in cls._known_fields()}
        return cls(
            **base.model_dump(exclude={"extra"}),
            delivery_dt=_optional_timestamp(record, "DELIVERY_DT"),
            weight=_optional_int(record, "WEIGHT"),
            commodity=_optional(record, "COMMODITY"),
            pieces=_optional_int(record, "PIECES"),
            dims=_optional(record, "DIMS"),
            # A blank NOTES field is all spaces on the wire; empty string is
            # the correct reading, not None.
            notes=record.get("NOTES", "").strip(),
            max_buy=_optional_int(record, "MAX_BUY"),
            extra=extra,
        )

    def public_dict(self) -> dict[str, object]:
        """Serialisable view with the buy ceiling removed.

        Belt and braces: the modelled field is dropped, and so is any unknown
        field the server might have spelled differently. This is the one
        invariant the whole design rests on (CLAUDE.md principle 1).
        """
        data = super().public_dict()
        for key in [key for key in data if key.lower() == "max_buy"]:
            data.pop(key)
        return data


class BookingStatus(str, Enum):
    """Outcome of a booking attempt."""

    BOOKED = "booked"
    #: The load is no longer OPEN but we cannot prove the booking reference is
    #: ours. A senior rep confirms it (CLAUDE.md section 4, LOAD_BOOK).
    BOOKED_UNCONFIRMED = "booked_unconfirmed"


class BookResult(BaseModel):
    """What ``LOAD_BOOK`` produced, including the ambiguous cases."""

    load_id: str
    status: BookingStatus
    booking_ref: str | None = None
    booked_at: datetime | None = None
    #: True when a transport fault forced us to resolve the outcome with a
    #: follow-up LOAD_GET instead of reading it off the booking response.
    ambiguous: bool = False

    @property
    def confirmed(self) -> bool:
        return self.status is BookingStatus.BOOKED and self.booking_ref is not None
