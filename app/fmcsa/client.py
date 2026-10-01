"""FMCSA authority check.

**Every field name read here is `TODO: verify`.** FMCSA blocks the whole domain
from Argentina (403), so no real response has been recorded yet; the shapes this
parses come from the public API documentation and from the fixture in
`tests/fixtures/fmcsa_*.json`, which is synthetic. The parser therefore reads
keys defensively — several spellings, several date formats — and reports
`incomplete_response` rather than guessing when a signal is missing.

**It fails closed.** A timeout, a 5xx, a 403, a body we cannot read: all of them
mean `eligible=False`. A carrier who cannot be verified is not booked through
this flow; the call goes to a human. See DECISIONS.md.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Callable

import httpx

from app.fmcsa.models import AuthorityCheck, IneligibilityReason

logger = logging.getLogger(__name__)

#: MC numbers are digits only, 1-8 of them (CLAUDE.md §5).
_MC_RE = re.compile(r"^\d{1,8}$")

DEFAULT_TIMEOUT_SECONDS = 5.0

#: Candidate key spellings, most likely first. TODO: verify against a real
#: response and delete the losers.
_LEGAL_NAME_KEYS = ("legalName", "legal_name", "name")
_DOT_KEYS = ("dotNumber", "dot_number", "usdotNumber")
_ALLOWED_KEYS = ("allowedToOperate", "allowed_to_operate")
_OUT_OF_SERVICE_KEYS = ("outOfService", "out_of_service", "oos")
_STATUS_KEYS = ("statusCode", "status_code", "status")
_AUTHORITY_DATE_KEYS = (
    "addDate",
    "authDate",
    "authorityDate",
    "originalActionDate",
    "mcs150Date",
)

_TRUE_FLAGS = {"y", "yes", "true", "1", "a"}
_FALSE_FLAGS = {"n", "no", "false", "0"}

#: `statusCode` meaning an active authority. TODO: verify.
_ACTIVE_STATUS = {"a", "active"}


class McValidationError(ValueError):
    """The MC we were asked to verify is not shaped like an MC."""


def validate_mc(mc_number: str) -> str:
    """Digits only, 1-8 characters. Raises rather than calling FMCSA."""
    text = str(mc_number).strip()
    if not _MC_RE.match(text):
        raise McValidationError("MC number must be 1-8 digits")
    return text


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """The first key that is present and not blank, or None."""
    for key in keys:
        if key in payload:
            value = payload[key]
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            return value
    return None


def _flag(value: Any) -> bool | None:
    """Read a yes/no signal written as Y/N, true/false, 1/0 or a bool.

    Returns None when the value is present but unrecognisable, so the caller can
    tell "absent" from "answered in a way we do not understand" — both fail
    closed, but they are different events to log.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_FLAGS:
            return True
        if text in _FALSE_FLAGS:
            return False
    return None


def _find_carrier(payload: Any) -> dict[str, Any] | None:
    """Dig the carrier object out of whatever wrapper FMCSA used today."""
    if isinstance(payload, list):
        for item in payload:
            found = _find_carrier(item)
            if found is not None:
                return found
        return None

    if not isinstance(payload, dict):
        return None

    # The documented shape is {"content": {"carrier": {...}}} or the same with
    # "content" as a list. Accept a bare carrier object too.
    for wrapper in ("content", "carrier"):
        if wrapper in payload:
            found = _find_carrier(payload[wrapper])
            if found is not None:
                return found

    if any(key in payload for key in (*_LEGAL_NAME_KEYS, *_DOT_KEYS, *_ALLOWED_KEYS)):
        return payload
    return None


def _parse_date(value: Any) -> date | None:
    """Parse the several date shapes FMCSA is known to emit. TODO: verify."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # ISO-ish, with or without a time part.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for pattern in ("%Y%m%d", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


class FmcsaClient:
    """Looks a carrier up by MC number and decides whether it can be booked."""

    def __init__(
        self,
        base_url: str,
        web_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._web_key = web_key
        self._timeout = timeout
        self._client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc).date())

    @classmethod
    def from_settings(cls, settings, **overrides) -> "FmcsaClient":
        return cls(
            settings.fmcsa_base_url,
            settings.fmcsa_key.get_secret_value(),
            **overrides,
        )

    # --- public ---------------------------------------------------------

    def check(self, mc_number: str) -> AuthorityCheck:
        """Verify a carrier. Never raises for a remote failure: fails closed."""
        mc = validate_mc(mc_number)

        try:
            payload = self._fetch(mc)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # A 404 is an answer: FMCSA does not know this MC. That is a
                # rejection, not an outage, and the two route differently.
                return AuthorityCheck(
                    eligible=False, mc_number=mc, reason=IneligibilityReason.NOT_FOUND
                )
            return self._unavailable(mc, exc.response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            # Timeout, DNS, TLS, connection reset, and the 403 that FMCSA serves
            # to the whole domain from outside the US. ValueError covers a 200
            # whose body is not JSON — an HTML error page, say, which is exactly
            # what a proxy or a block page returns.
            logger.warning("FMCSA lookup for %s failed: %s", mc, type(exc).__name__)
            return self._unavailable(mc)

        return self._interpret(mc, payload)

    # --- transport ------------------------------------------------------

    def _fetch(self, mc: str) -> Any:
        url = f"{self._base_url}/carriers/docket-number/{mc}"
        headers = {"Accept": "application/json"}

        if self._client is not None:
            response = self._client.get(
                url, params={"webKey": self._web_key}, headers=headers, timeout=self._timeout
            )
        else:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    url, params={"webKey": self._web_key}, headers=headers
                )

        if response.status_code == 404:
            raise httpx.HTTPStatusError(
                "carrier not found", request=response.request, response=response
            )
        response.raise_for_status()
        return response.json()

    # --- interpretation -------------------------------------------------

    def _interpret(self, mc: str, payload: Any) -> AuthorityCheck:
        carrier = _find_carrier(payload)
        if carrier is None:
            return AuthorityCheck(
                eligible=False, mc_number=mc, reason=IneligibilityReason.NOT_FOUND
            )

        legal_name = _first(carrier, _LEGAL_NAME_KEYS)
        dot_number = _first(carrier, _DOT_KEYS)
        allowed = _flag(_first(carrier, _ALLOWED_KEYS))
        out_of_service = _flag(_first(carrier, _OUT_OF_SERVICE_KEYS))
        status = _first(carrier, _STATUS_KEYS)

        age_days = self._authority_age_days(carrier)

        common = {
            "mc_number": mc,
            "legal_name": str(legal_name).strip() if legal_name else None,
            "dot_number": str(dot_number).strip() if dot_number else None,
            "authority_age_days": age_days,
            "raw": {k: v for k, v in carrier.items() if not isinstance(v, (dict, list))},
        }

        # Order follows CLAUDE.md §5: found, allowed, active, not out of service.
        if allowed is None or out_of_service is None or status is None:
            logger.warning(
                "FMCSA payload for %s did not carry the fields we need "
                "(allowedToOperate/outOfService/statusCode). TODO: verify key names.",
                mc,
            )
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.INCOMPLETE_RESPONSE, **common
            )

        if not allowed:
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.NOT_ALLOWED_TO_OPERATE, **common
            )

        if str(status).strip().lower() not in _ACTIVE_STATUS:
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.AUTHORITY_INACTIVE, **common
            )

        if out_of_service:
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.OUT_OF_SERVICE, **common
            )

        return AuthorityCheck(eligible=True, **common)

    def _authority_age_days(self, carrier: dict[str, Any]) -> int | None:
        granted = _parse_date(_first(carrier, _AUTHORITY_DATE_KEYS))
        if granted is None:
            return None
        return (self._clock() - granted).days

    @staticmethod
    def _unavailable(mc: str, status_code: int | None = None) -> AuthorityCheck:
        if status_code is not None:
            logger.warning("FMCSA returned %s for MC %s", status_code, mc)
        return AuthorityCheck(
            eligible=False,
            mc_number=mc,
            reason=IneligibilityReason.VERIFICATION_UNAVAILABLE,
        )
