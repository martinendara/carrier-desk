"""Email delivery via Resend's HTTP API.

The service sends the OTP code itself rather than handing it back to the
platform. The plaintext code therefore never transits the platform, which closes
the one place a code could previously leak (the tool result). See DECISIONS.md.

Resend's free tier only delivers to the account owner's own address, which is
fine for the demo. Production would swap the provider (SMS, or the client's
mail provider) behind the same ``Mailer`` interface.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

RESEND_URL = "https://api.resend.com/emails"
RESEND_FROM = "HappyRobot Logistics <otp@notifications.clicon.app>"


class MailError(RuntimeError):
    """The email could not be sent."""


@dataclass(frozen=True)
class OutboundEmail:
    """One email to deliver. The body may contain a code; it is not stored."""

    to: str
    subject: str
    body: str


class Mailer:
    """Sends one email. Raises :class:`MailError` on failure."""

    def send(self, email: OutboundEmail) -> None:
        raise NotImplementedError


class ResendMailer(Mailer):
    """Resend's HTTP API, with a short timeout and one retry."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 5.0,
        max_attempts: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client = client

    def send(self, email: OutboundEmail) -> None:
        last: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                self._post(email)
                return
            except httpx.HTTPError as exc:
                last = exc
        raise MailError("email could not be sent") from last

    def _post(self, email: OutboundEmail) -> None:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "from": RESEND_FROM,
            "to": [email.to],
            "subject": email.subject,
            "text": email.body,
        }
        if self._client is not None:
            response = self._client.post(
                RESEND_URL, json=payload, headers=headers, timeout=self._timeout
            )
        else:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(RESEND_URL, json=payload, headers=headers)
        response.raise_for_status()
