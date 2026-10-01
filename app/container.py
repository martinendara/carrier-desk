"""Everything the API needs, built once and held on `app.state`.

Building the wiring in one place keeps the route handlers about the
conversation rather than about construction, and it makes the whole service
constructible from a `Settings` object — which is how the tests point it at the
in-process fake TMS instead of the live one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import REPO_ROOT, Settings
from app.events import EventSink, InMemoryEventSink, JsonlEventSink
from app.fmcsa import FmcsaClient
from app.handoff import HandoffQueue
from app.negotiation import NegotiationEngine
from app.otp import OtpService
from app.sessions import RunSessionStore
from app.tms.client import TmsClient
from app.tms.fake_server import FakeTms, start_fake_tms

logger = logging.getLogger(__name__)

#: Sentinel for `EVENTS_JSONL_PATH` meaning "keep events in memory".
MEMORY_SINK = "memory"


def build_event_sink(settings: Settings) -> EventSink:
    """JSONL on disk, or in memory for the tests."""
    if settings.events_jsonl_path.strip().lower() == MEMORY_SINK:
        return InMemoryEventSink()

    path = Path(settings.events_jsonl_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return JsonlEventSink(path)


@dataclass
class Container:
    """The constructed service."""

    settings: Settings
    store: RunSessionStore
    sink: EventSink
    tms: TmsClient
    otp: OtpService
    negotiation: NegotiationEngine
    fmcsa: FmcsaClient
    handoffs: HandoffQueue
    #: Set when TMS_MODE=fake. We own it and must stop it on the way out.
    fake_tms: FakeTms | None = None

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        store = RunSessionStore(ttl_seconds=settings.run_session_ttl_seconds)
        sink = build_event_sink(settings)

        fake = None
        if settings.tms_mode.strip().lower() == "fake":
            # The real client over a real socket, against a server in this
            # process: the deadline, framing and retry code all still run.
            logger.warning("TMS_MODE=fake: serving loads from the recorded fixtures")
            fake = start_fake_tms()
            tms = TmsClient(
                host="127.0.0.1",
                port=fake.port,
                token=settings.tms_fake_token,
                connect_timeout=settings.tms_connect_timeout_seconds,
                read_deadline=settings.tms_read_deadline_seconds,
                max_attempts=settings.tms_max_attempts,
                total_budget=settings.request_budget_seconds,
            )
        else:
            tms = TmsClient.from_settings(settings)

        return cls(
            settings=settings,
            store=store,
            sink=sink,
            tms=tms,
            otp=OtpService.from_settings(store, settings, sink=sink),
            negotiation=NegotiationEngine.from_settings(store, settings, sink=sink),
            fmcsa=FmcsaClient.from_settings(
                settings, timeout=settings.fmcsa_timeout_seconds
            ),
            handoffs=HandoffQueue(),
            fake_tms=fake,
        )

    def close(self) -> None:
        if self.fake_tms is not None:
            self.fake_tms.stop()
            self.fake_tms = None
