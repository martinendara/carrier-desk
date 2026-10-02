"""The OTP flow: issue a code to the contact on file, then check it.

The service sends the code itself (via :class:`app.mail.Mailer`), so the
plaintext code never leaves this process. The only thing that may end up in the
conversation is the masked destination. A code is never written to an event, a
log line, or any agent-facing field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from app.carriers import directory
from app.events import EventSink, EventType, NullEventSink
from app.mail import MailError, Mailer, OutboundEmail, ResendMailer
from app.otp.security import WeakSecretError, generate_code, hash_code, mask_email, verify_code
from app.results import ResultCode
from app.sessions import RunSessionStore, SessionNotFound, utcnow
from app.sessions.models import OtpChallenge, RunSession

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RESENDS = 1

#: A short secret is a guessable secret, and this one is the only thing standing
#: between a leaked digest and a live code. Refuse to start rather than pretend.
MIN_SECRET_BYTES = 16

#: The email the code rides in. The expiry must match ``otp_ttl_seconds``.
OTP_EMAIL_SUBJECT = "Your HappyRobot Logistics verification code"
OTP_EMAIL_BODY = (
    "Your verification code is {code}. It expires in 5 minutes. "
    "If you didn't request it, you can ignore this email."
)


class DispatchStatus(str, Enum):
    """What happened when we tried to send a code."""

    SENT = "sent"
    #: No contact on file: the call goes to onboarding instead of an OTP.
    ROUTED_ONBOARDING = "routed_onboarding"
    RESEND_LIMIT_REACHED = "resend_limit_reached"
    ALREADY_VERIFIED = "already_verified"
    LOCKED = "locked"
    #: The email could not be delivered. Nothing was issued.
    SEND_FAILED = "send_failed"


class OtpFailure(str, Enum):
    """Why a verification did not succeed."""

    NO_CHALLENGE = "no_challenge"
    EXPIRED = "expired"
    LOCKED = "locked"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class OtpDispatch:
    """The result of a send or resend.

    There is no code and no address here: the service sends the email itself, so
    the plaintext code never leaves this process.
    """

    status: DispatchStatus
    run_id: str
    masked_destination: str | None = None

    @property
    def agent_view(self) -> dict[str, object]:
        """Everything the agent may say. No code, no full address."""
        if self.status is DispatchStatus.SENT:
            return {"masked_destination": self.masked_destination}
        return {"result_code": self.result_code}

    @property
    def result_code(self) -> str | None:
        if self.status is DispatchStatus.SENT:
            return None
        if self.status is DispatchStatus.ROUTED_ONBOARDING:
            return ResultCode.ROUTED_ONBOARDING.value
        return ResultCode.OTP_FAILED.value


@dataclass(frozen=True)
class OtpVerification:
    """The result of checking a code."""

    verified: bool
    attempts_left: int
    failure: OtpFailure | None = None

    @property
    def agent_view(self) -> dict[str, object]:
        return {"verified": self.verified, "attempts_left": self.attempts_left}

    @property
    def result_code(self) -> str | None:
        return None if self.verified else ResultCode.OTP_FAILED.value


class OtpService:
    """Issues and checks OTP codes, keyed by the call's `run_id`."""

    def __init__(
        self,
        store: RunSessionStore,
        *,
        secret: bytes,
        mailer: Mailer,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_resends: int = DEFAULT_MAX_RESENDS,
        lookup: Callable[[str], directory.CarrierContact | None] = directory.lookup,
        clock: Callable[[], datetime] = utcnow,
        sink: EventSink | None = None,
    ) -> None:
        if not secret or len(secret) < MIN_SECRET_BYTES:
            raise WeakSecretError(
                f"OTP HMAC secret must be at least {MIN_SECRET_BYTES} bytes"
            )
        self._store = store
        self._secret = secret
        self._mailer = mailer
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._max_resends = max_resends
        self._lookup = lookup
        self._clock = clock
        self._sink: EventSink = sink or NullEventSink()

    @classmethod
    def from_settings(
        cls, store: RunSessionStore, settings, *, sink: EventSink | None = None
    ) -> "OtpService":
        return cls(
            store,
            secret=settings.otp_hmac_secret.get_secret_value().encode("utf-8"),
            mailer=ResendMailer(
                settings.resend_api_key.get_secret_value(),
                timeout=settings.resend_timeout_seconds,
            ),
            ttl_seconds=settings.otp_ttl_seconds,
            max_attempts=settings.otp_max_attempts,
            max_resends=settings.otp_max_resends,
            sink=sink,
        )

    # --- sending --------------------------------------------------------

    def send(self, run_id: str, mc_number: str) -> OtpDispatch:
        """Issue a code for this MC, to the contact on file.

        Calling it twice for the same call behaves like a resend, so the
        one-resend budget cannot be bypassed by hammering the send endpoint.
        """
        session = self._store.get_or_create(run_id)
        mc = str(mc_number).strip()

        contact = self._lookup(mc)
        if contact is None:
            # Nothing on file means nothing to send to. The caller is not
            # refused — they are routed to a human, which is the whole point of
            # the onboarding path.
            logger.info("no contact on file for MC %s; routing to onboarding", mc)
            return OtpDispatch(
                status=DispatchStatus.ROUTED_ONBOARDING, run_id=session.run_id
            )

        session.mc_number = mc
        return self._issue(session, contact)

    def resend(self, run_id: str) -> OtpDispatch:
        """Send a fresh code, if the call still has its one resend."""
        session = self._store.require(run_id)
        if session.mc_number is None:
            raise SessionNotFound(f"run {run_id!r} has no MC on file to resend to")

        contact = self._lookup(session.mc_number)
        if contact is None:
            return OtpDispatch(
                status=DispatchStatus.ROUTED_ONBOARDING, run_id=session.run_id
            )
        return self._issue(session, contact)

    def _issue(self, session: RunSession, contact: directory.CarrierContact) -> OtpDispatch:
        existing = session.otp

        if existing is not None:
            if existing.verified_at is not None:
                return OtpDispatch(
                    status=DispatchStatus.ALREADY_VERIFIED, run_id=session.run_id
                )
            if existing.locked:
                # A lock is a security control. Issuing a new code would lift it
                # and hand the caller three more guesses.
                return OtpDispatch(status=DispatchStatus.LOCKED, run_id=session.run_id)
            if existing.resends_left <= 0:
                return OtpDispatch(
                    status=DispatchStatus.RESEND_LIMIT_REACHED, run_id=session.run_id
                )

        now = self._clock()
        code = generate_code()

        # Send first, store second: if the email fails, nothing was issued and
        # the resend budget is untouched.
        try:
            self._mailer.send(
                OutboundEmail(
                    to=contact.email,
                    subject=OTP_EMAIL_SUBJECT,
                    body=OTP_EMAIL_BODY.format(code=code),
                )
            )
        except MailError:
            logger.warning("OTP email for %s could not be sent", mask_email(contact.email))
            return OtpDispatch(status=DispatchStatus.SEND_FAILED, run_id=session.run_id)

        challenge = OtpChallenge(
            code_hmac=hash_code(self._secret, session.run_id, code),
            destination=contact.email,
            masked_destination=mask_email(contact.email),
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            max_attempts=self._max_attempts,
            # A resend does not refund attempts: the total number of guesses in
            # a call stays at three no matter how many codes were issued.
            attempts=existing.attempts if existing else 0,
            resends=(existing.resends + 1) if existing else 0,
            max_resends=self._max_resends,
        )
        session.otp = challenge
        session.touch()

        self._sink.emit(
            session.run_id,
            EventType.OTP_SENT,
            mc_number=session.mc_number,
            # Masked: an event log is internal, but it is still a place PII
            # accumulates for no benefit.
            destination=challenge.masked_destination,
            resend=existing is not None,
            expires_at=challenge.expires_at.isoformat(),
        )

        return OtpDispatch(
            status=DispatchStatus.SENT,
            run_id=session.run_id,
            masked_destination=challenge.masked_destination,
        )

    # --- verifying ------------------------------------------------------

    def verify(self, run_id: str, code: str) -> OtpVerification:
        """Check a code. Counts every attempt, valid-looking or not."""
        session = self._store.require(run_id)
        challenge = session.otp

        if challenge is None:
            return OtpVerification(verified=False, attempts_left=0, failure=OtpFailure.NO_CHALLENGE)

        if challenge.verified_at is not None:
            # Idempotent: a repeated verify on a verified session is not a new
            # attempt and does not consume anything.
            return OtpVerification(verified=True, attempts_left=challenge.attempts_left)

        if challenge.locked:
            return OtpVerification(verified=False, attempts_left=0, failure=OtpFailure.LOCKED)

        if challenge.is_expired(self._clock()):
            return OtpVerification(
                verified=False,
                attempts_left=challenge.attempts_left,
                failure=OtpFailure.EXPIRED,
            )

        # A code of the wrong shape counts as a failed attempt rather than being
        # rejected for free, so probing the expected length costs guesses.
        candidate = str(code).strip()
        if verify_code(self._secret, session.run_id, candidate, challenge.code_hmac):
            challenge.verified_at = self._clock()
            session.touch()
            self._sink.emit(session.run_id, EventType.OTP_VERIFIED, mc_number=session.mc_number)
            return OtpVerification(verified=True, attempts_left=challenge.attempts_left)

        challenge.attempts += 1
        if challenge.attempts >= challenge.max_attempts:
            challenge.locked = True
        session.touch()

        failure = OtpFailure.LOCKED if challenge.locked else OtpFailure.MISMATCH
        self._sink.emit(
            session.run_id,
            EventType.OTP_FAILED,
            mc_number=session.mc_number,
            reason=failure.value,
            attempts_left=challenge.attempts_left,
        )
        return OtpVerification(
            verified=False, attempts_left=challenge.attempts_left, failure=failure
        )
