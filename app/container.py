"""Everything the API needs, built once and held on `app.state`.

Building the wiring in one place keeps the route handlers about the
conversation rather than about construction, and it makes the whole service
constructible from a `Settings` object — which is how the tests point it at the
in-process fake TMS instead of the live one.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.config import REPO_ROOT, Settings
from app.events import EventSink, InMemoryEventSink, JsonlEventSink
from app.fmcsa import FmcsaClient
from app.handoff import HandoffQueue
from app.negotiation import NegotiationEngine
from app.otp import OtpService
from app.rate_limit import SlidingWindowLimiter
from app.sessions import RunSessionStore
from app.tms.client import TmsClient
from app.tms.fake_server import FakeTms, start_fake_tms
from app.twin import TwinWriter

logger = logging.getLogger(__name__)

#: Sentinel for `EVENTS_JSONL_PATH` meaning "keep events in memory".
MEMORY_SINK = "memory"

SECONDS_PER_HOUR = 3600.0


def _enforce_production_guards(settings: Settings) -> None:
    """Refuse to start in production with a configuration that cannot work.

    Each of these is a misconfiguration that produces a service which looks
    alive and is not:
    a fake TMS quotes freight that does not exist; an empty API key answers 503
    to every tool; an empty OTP secret cannot issue a code.

    Refusing at startup turns all three into a container that will not come up,
    which is the failure everyone notices. Checked in this order so the message
    names the most consequential problem first.
    """
    if not settings.is_production:
        return

    if settings.tms_mode.strip().lower() == "fake":
        raise ConfigurationError(
            "TMS_MODE=fake serves recorded fixtures and must not run with "
            "ENV=production. Set TMS_MODE=live, or unset ENV."
        )

    if settings.reviewer_mode:
        # Ranked second because it is the same shape of mistake as the fake TMS:
        # both are switches that are right for an evaluation and wrong for a
        # carrier. This one is a verification bypass — a fixed code, no email,
        # for one known MC — so anyone who reads the code can become that
        # carrier. The empty-secret checks below only make the service fail;
        # this one makes it succeed at the wrong thing, which is worse.
        raise ConfigurationError(
            "REVIEWER_MODE is on and ENV=production. It issues a fixed OTP for "
            f"MC {settings.reviewer_mc or '<unset>'} with no email, which is an "
            "evaluation tool and not a production credential. Turn it off, or "
            "unset ENV."
        )

    if not settings.service_api_key.get_secret_value():
        # Left to fail at request time it would answer 503 to every tool, which
        # from the platform's side is indistinguishable from an outage.
        raise ConfigurationError(
            "SERVICE_API_KEY is empty and ENV=production. Every request would be "
            "refused. Set it to the key the platform sends as X-API-Key."
        )

    if not settings.otp_hmac_secret.get_secret_value():
        # OtpService refuses an empty secret on its own, but that failure names
        # the class rather than the environment.
        raise ConfigurationError(
            "OTP_HMAC_SECRET is empty and ENV=production. No code could be "
            "issued. Generate one with: python -c \"import secrets; "
            'print(secrets.token_urlsafe(32))"'
        )

    if not settings.resend_api_key.get_secret_value():
        raise ConfigurationError(
            "RESEND_API_KEY is empty and ENV=production. The OTP email could "
            "not be sent. Set it to the key for Resend's HTTP API."
        )


def _validate_reviewer_mode(settings: Settings) -> None:
    """Refuse a reviewer mode that cannot be told apart from a bypass.

    Checked in **every** environment, not just production: the deploy runs
    `ENV=development` and uses reviewer mode on purpose, so a rule that only
    applied to production would not protect the instance that actually has it
    switched on.

    The empty-code case is the one that matters. `reviewer_otp` defaults to the
    empty string, and the service issues whatever it is given as the fixed code
    — so `REVIEWER_MODE=true` with `REVIEWER_OTP` unset would accept an empty
    string as the correct code for the reviewer MC. That is a verification
    bypass with no secret in it at all, and it is one careless env var away.
    """
    if not settings.reviewer_mode:
        return

    if not settings.reviewer_mc.strip():
        raise ConfigurationError(
            "REVIEWER_MODE is on but REVIEWER_MC is empty. There is no carrier "
            "to issue the fixed code to. Set REVIEWER_MC, or turn REVIEWER_MODE off."
        )

    otp = settings.reviewer_otp.get_secret_value().strip()
    if len(otp) != 6 or not otp.isdigit():
        # The carrier reads a six-digit code back, so anything else is either a
        # typo or a weaker secret than the flow assumes.
        raise ConfigurationError(
            "REVIEWER_MODE is on but REVIEWER_OTP is not six digits. It is the "
            "code the reviewer MC is issued, and an empty one would be accepted "
            "as-is. Set it to six digits, or turn REVIEWER_MODE off."
        )


def build_event_sink(settings: Settings) -> EventSink:
    """JSONL on disk, or in memory for the tests."""
    if settings.events_jsonl_path.strip().lower() == MEMORY_SINK:
        return InMemoryEventSink()

    path = Path(settings.events_jsonl_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return JsonlEventSink(path)


class ConfigurationError(RuntimeError):
    """The configuration is internally consistent but dangerous. Refuse to start."""


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
    otp_verify_limiter: SlidingWindowLimiter
    #: Serialises booking so two concurrent requests for the same run_id do not
    #: both call LOAD_BOOK. Coarse (one lock for all calls) on purpose: the POC
    #: is single-instance and low-traffic, and correctness beats throughput here.
    booking_lock: threading.Lock = field(default_factory=threading.Lock)
    #: Set when TMS_MODE=fake. We own it and must stop it on the way out.
    fake_tms: FakeTms | None = None

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        _enforce_production_guards(settings)
        _validate_reviewer_mode(settings)

        store = RunSessionStore(ttl_seconds=settings.run_session_ttl_seconds)
        sink = build_event_sink(settings)
        if settings.twin_enabled:
            sink = TwinWriter(
                sink,
                base_url=settings.twin_base_url,
                api_key=settings.hr_platform_api_key.get_secret_value(),
                table=settings.twin_table,
                enabled=True,
                timeout=settings.twin_timeout_seconds,
                max_attempts=settings.twin_max_attempts,
            )

        fake = None
        if settings.tms_mode.strip().lower() == "fake":
            # The real client over a real socket, against a server in this
            # process: the deadline, framing and retry code all still run.
            logger.warning("TMS_MODE=fake: serving loads from the recorded fixtures")
            if settings.tms_fake_fault_rate > 0:
                # Said out loud because it changes what a demo proves: every
                # TMS call has a real chance of failing, so the retry ladder and
                # the ambiguous-booking recovery are exercised rather than
                # assumed. Safe because LOAD_BOOK stays idempotent per call
                # (app/api/routes/bookings.py) and an unresolved booking is
                # never reported as `booked`.
                logger.warning(
                    "TMS_FAKE_FAULT_RATE=%.2f: the fake TMS will fail that share "
                    "of operational requests on purpose",
                    settings.tms_fake_fault_rate,
                )
            fake = start_fake_tms(fault_rate=settings.tms_fake_fault_rate)
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
            # The live client refuses an empty host/port/token with a bare
            # ``ValueError`` that does not name the environment variable. A
            # deploy missing TMS_* then crash-loops at startup, which Render
            # reports as "timed out waiting for a health check" — a hung
            # service, not the misconfiguration it is. Raise the same
            # ConfigurationError the production guard uses, naming the variable,
            # so the log points at exactly what is missing. Checked always, not
            # just in production: live mode needs these in every environment.
            if not settings.tms_host:
                raise ConfigurationError(
                    "TMS_HOST is empty and TMS_MODE is not 'fake'. The service "
                    "cannot reach the legacy TMS. Set TMS_HOST, TMS_PORT and "
                    "TMS_TOKEN, or TMS_MODE=fake."
                )
            if not settings.tms_port:
                raise ConfigurationError(
                    "TMS_PORT is empty and TMS_MODE is not 'fake'. The service "
                    "cannot reach the legacy TMS. Set TMS_PORT, or TMS_MODE=fake."
                )
            if not settings.tms_token_value:
                raise ConfigurationError(
                    "TMS_TOKEN is empty and TMS_MODE is not 'fake'. The service "
                    "cannot authenticate to the legacy TMS. Set TMS_TOKEN, or "
                    "TMS_MODE=fake."
                )
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
            handoffs=HandoffQueue(
                retention_seconds=settings.handoff_retention_seconds
            ),
            otp_verify_limiter=SlidingWindowLimiter(
                limit=settings.otp_verify_rate_limit_per_hour,
                window_seconds=SECONDS_PER_HOUR,
            ),
            fake_tms=fake,
        )

    def close(self) -> None:
        if self.fake_tms is not None:
            self.fake_tms.stop()
            self.fake_tms = None
        if isinstance(self.sink, TwinWriter):
            self.sink.close()
