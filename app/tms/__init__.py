"""Adapter for the legacy TMS (raw TCP, line protocol).

Layering, from the outside in:

    client.TmsClient  -- sockets, deadlines, retries, fault classification
    models.Load       -- typed records built from parsed dictionaries
    protocol          -- pure bytes <-> dicts: encoding, parsing, exceptions

Only `client.TmsClient` touches the network. Everything below it is pure, so
the wire format can be tested without a server.
"""

from app.tms.client import TmsClient
from app.tms.models import BookResult, Load, LoadDetail
from app.tms.protocol import (
    TmsBudgetExhausted,
    TmsError,
    TmsFault,
    TmsInvalidInput,
    TmsMalformed,
    TmsPartialResponse,
    TmsTimeout,
)

__all__ = [
    "BookResult",
    "Load",
    "LoadDetail",
    "TmsBudgetExhausted",
    "TmsClient",
    "TmsError",
    "TmsFault",
    "TmsInvalidInput",
    "TmsMalformed",
    "TmsPartialResponse",
    "TmsTimeout",
]
