"""FMCSA authority check.

**These field names are recorded, not guessed.** The payload in
`tests/fixtures/fmcsa_carrier_recorded.json` came back from the live API on
2026-10-01 — MC 123456, the first real response this service ever saw — and
every key read below is a key that response carries. What it settled is written
next to each constant; what it left open (the authority date, and therefore
`authority_age_days`) is in `PROGRESS.md`.

Two things that response changed, beyond the names:

* **`outOfService` does not exist.** The nearest field is `oosDate`, a date,
  where null means no order on file.
* **`statusCode` is not the authority.** The recorded response carries
  `statusCode: "A"` next to `commonAuthorityStatus: "I"` — an active carrier
  record whose operating authority is inactive. Eligibility reads the two
  authority statuses instead. See DECISIONS.md §45.

**It fails closed.** A timeout, a 5xx, a 403, a body we cannot read, a record
missing a field the decision needs: all of them mean `eligible=False`. A carrier
who cannot be verified is not booked through this flow; the call goes to a
human. See DECISIONS.md.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.fmcsa.models import AuthorityCheck, IneligibilityReason

logger = logging.getLogger(__name__)

#: MC numbers are digits only, 1-8 of them (CLAUDE.md §5).
_MC_RE = re.compile(r"^\d{1,8}$")

DEFAULT_TIMEOUT_SECONDS = 5.0

#: The authority record is enrichment on a call the carrier is already on, so
#: it gets a shorter leash than the lookup the verdict depends on: a slow
#: second opinion is not worth a pause in the conversation.
AUTHORITY_TIMEOUT_SECONDS = 2.0

#: How much of a payload goes on one log line. Long enough for a real carrier
#: record with its nested lists, short enough that a log viewer stays readable.
PAYLOAD_LOG_LIMIT = 2000

#: Fields dropped before anything is written down, in a log or an event. The
#: registry publishes them, but nothing here needs them, and the cheapest way
#: to be sure a tax id cannot leak is to take it out at the edge.
REDACTED_FIELDS = frozenset({"ein"})

# --- field names, taken from the recorded response ----------------------
#
# A tuple even for a single name: `_first` also skips a blank value, and a
# second spelling can be added later without touching the rule that reads it.

#: "B MARRON LOGISTICS LLC".
_LEGAL_NAME_KEYS = ("legalName",)
#: 3177404 — an **integer** in the response, where the documentation describes
#: a string. Both are read, because both are plausibly out there.
_DOT_KEYS = ("dotNumber",)
#: "Y".
_ALLOWED_KEYS = ("allowedToOperate",)
#: Operating authority, which is what a broker has to check and what a carrier
#: needs in order to haul for one. Either kind will do: a carrier may run on
#: common authority or on contract authority.
_COMMON_AUTHORITY_KEYS = ("commonAuthorityStatus",)
_CONTRACT_AUTHORITY_KEYS = ("contractAuthorityStatus",)
#: A date, not a flag. Null means no out-of-service order on file.
_OOS_DATE_KEYS = ("oosDate",)
#: BI&PD — the liability cover the carrier is required to carry. `Required` is
#: what FMCSA says the carrier must hold; `OnFile` is what has actually been
#: filed with it.
_BIPD_REQUIRED_KEYS = ("bipdInsuranceRequired",)
_BIPD_ON_FILE_KEYS = ("bipdInsuranceOnFile",)

_TRUE_FLAGS = {"y", "yes", "true", "1"}
_FALSE_FLAGS = {"n", "no", "false", "0"}

#: An authority status meaning the authority is in force. "I" is inactive and
#: "N" is not held; anything unrecognised is not an active authority either.
_ACTIVE_AUTHORITY = {"a"}


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


def _active_authority(value: Any) -> bool:
    """True when an authority status means the authority is in force."""
    return isinstance(value, str) and value.strip().lower() in _ACTIVE_AUTHORITY


def _has_out_of_service_order(value: Any) -> bool:
    """A date on `oosDate` is an order that was issued.

    Absent and null are read the same way: no order on file is the normal
    state, and treating a missing field as "unknown" here would send every
    carrier to a human — which is the failure this parser has just come out of.
    """
    return value is not None


def _insurance_not_on_file(carrier: dict[str, Any]) -> bool:
    """FMCSA states liability cover is required and shows nothing filed.

    "u" on the requirement is *not* a rejection: FMCSA is saying the
    requirement is unstated, so there is nothing to hold the carrier to. On the
    filing side anything that is not a clear yes counts as missing — an
    unknown filing against a stated requirement is not a yes.
    """
    required = _flag(_first(carrier, _BIPD_REQUIRED_KEYS))
    on_file = _flag(_first(carrier, _BIPD_ON_FILE_KEYS))
    return required is True and on_file is not True


def _without_redacted(value: Any) -> Any:
    """A copy of a payload with the fields we never write down removed."""
    if isinstance(value, dict):
        return {
            key: ("***" if key in REDACTED_FIELDS else _without_redacted(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_redacted(item) for item in value]
    return value


def _flat_fields(carrier: dict[str, Any]) -> dict[str, Any]:
    """The record's own scalar fields, nested containers and secrets removed."""
    return {
        key: ("***" if key in REDACTED_FIELDS else value)
        for key, value in carrier.items()
        if not isinstance(value, (dict, list))
    }


def _find_carrier(payload: Any) -> dict[str, Any] | None:
    """Dig the carrier object out of the wrapper around it.

    Still tolerant on purpose. The response we recorded was logged as the
    carrier object — the wrapper is what our own parser stripped on the way in,
    so the shape around it (`content` as a list, as an object, or absent) is
    the one thing this response did not settle.
    """
    if isinstance(payload, list):
        for item in payload:
            found = _find_carrier(item)
            if found is not None:
                return found
        return None

    if not isinstance(payload, dict):
        return None

    for wrapper in ("content", "carrier"):
        if wrapper in payload:
            found = _find_carrier(payload[wrapper])
            if found is not None:
                return found

    if any(key in payload for key in (*_LEGAL_NAME_KEYS, *_DOT_KEYS, *_ALLOWED_KEYS)):
        return payload
    return None


def describe_payload(payload: Any, secret: str = "") -> str:
    """One bounded, single-line, secret-free view of what FMCSA actually sent.

    Field names **and** values, on purpose. The names are what we got wrong;
    the values are what settles a guess — a status code only means something
    next to the code it carries.

    Public registry data (a carrier's name and address), not a credential —
    but the tax id is stripped all the same, the web key is redacted in case a
    body ever echoed it, and the line is capped so one pathological record
    cannot flood a log.
    """
    try:
        text = json.dumps(_without_redacted(payload), default=str, ensure_ascii=True)
    except (TypeError, ValueError):  # a shape json cannot serialise at all
        text = repr(payload)

    if secret:
        text = text.replace(secret, "***")

    if len(text) > PAYLOAD_LOG_LIMIT:
        text = f"{text[:PAYLOAD_LOG_LIMIT]}...[+{len(text) - PAYLOAD_LOG_LIMIT} chars]"

    return text


class FmcsaClient:
    """Looks a carrier up by MC number and decides whether it can be booked."""

    def __init__(
        self,
        base_url: str,
        web_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._web_key = web_key
        self._timeout = timeout
        self._client = client

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

        check = self._interpret(mc, payload)

        if check.authority_active and check.dot_number:
            self.authority_record(check.dot_number)

        return check

    # --- transport ------------------------------------------------------

    def _get(self, path: str, *, timeout: float | None = None) -> Any:
        """One GET against FMCSA, web key attached. Raises on anything but 2xx.

        A 404 raises here rather than being handled: which of the callers wants
        to read a missing record as an answer and which as a failure is their
        decision, not this one's.
        """
        url = f"{self._base_url}{path}"
        headers = {"Accept": "application/json"}
        params = {"webKey": self._web_key}
        timeout = self._timeout if timeout is None else timeout

        if self._client is not None:
            response = self._client.get(url, params=params, headers=headers, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, params=params, headers=headers)

        response.raise_for_status()
        return response.json()

    def _fetch(self, mc: str) -> Any:
        # A 404 here means FMCSA does not know the MC, which `check()` reads as
        # an answer rather than an outage.
        return self._get(f"/carriers/docket-number/{mc}")

    def authority_record(self, dot_number: str) -> Any | None:
        """Fetch a carrier's own authority record and log it.

        **Reconnaissance.** `authority_age_days` needs the date the authority
        was granted, and the carrier payload has none — the last open
        `TODO: verify`. This endpoint is the candidate source, and its field
        names are as unverified as the carrier payload's were. So it is read
        the same way out of it: print the record once, from a region that can
        reach FMCSA, and take the names off it.

        Nothing parses the result yet, and nothing may fail because of it: the
        call is bounded, a failure is swallowed and logged, and the verdict the
        carrier is waiting on is already decided by the time this runs. Once a
        date is parsed out of here it becomes the source of the age, and this
        line can be deleted.
        """
        try:
            record = self._get(
                f"/carriers/{dot_number}/authority", timeout=AUTHORITY_TIMEOUT_SECONDS
            )
        except httpx.HTTPStatusError as exc:
            # 404 is the interesting one: it is what a wrong path looks like.
            logger.warning(
                "FMCSA authority lookup for DOT %s answered %s. "
                "TODO: verify the path and the field names.",
                dot_number,
                exc.response.status_code,
            )
            return None
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "FMCSA authority lookup for DOT %s failed: %s", dot_number, type(exc).__name__
            )
            return None

        logger.warning(
            "FMCSA authority record for DOT %s. TODO: verify the field names, then parse "
            "the grant date into authority_age_days. payload=%s",
            dot_number,
            describe_payload(record, self._web_key),
        )
        return record

    # --- interpretation -------------------------------------------------

    def _interpret(self, mc: str, payload: Any) -> AuthorityCheck:
        carrier = _find_carrier(payload)
        if carrier is None:
            # FMCSA answered 200 with a shape that holds no carrier we can
            # find. Logged with the body: this is either an MC that does not
            # exist, or a response shape we have not seen, and the two are
            # indistinguishable from here without looking.
            logger.warning(
                "FMCSA answered for MC %s but carried no carrier object. "
                "TODO: verify the response shape. payload=%s",
                mc,
                describe_payload(payload, self._web_key),
            )
            return AuthorityCheck(
                eligible=False, mc_number=mc, reason=IneligibilityReason.NOT_FOUND
            )

        legal_name = _first(carrier, _LEGAL_NAME_KEYS)
        dot_number = _first(carrier, _DOT_KEYS)
        allowed = _flag(_first(carrier, _ALLOWED_KEYS))
        common_authority = _first(carrier, _COMMON_AUTHORITY_KEYS)
        contract_authority = _first(carrier, _CONTRACT_AUTHORITY_KEYS)
        oos_date = _first(carrier, _OOS_DATE_KEYS)

        context = {
            "mc_number": mc,
            "legal_name": str(legal_name).strip() if legal_name is not None else None,
            "dot_number": str(dot_number).strip() if dot_number is not None else None,
            # None, always: the carrier payload carries no authority date, so
            # there is nothing to measure an age from. Onboarding reads unknown
            # as Review, which is the honest answer, not a guess that the
            # authority is settled. The source is still to be found —
            # PROGRESS.md carries it as the last open `TODO: verify`.
            "authority_age_days": None,
            "raw": _flat_fields(carrier),
        }

        # Order follows CLAUDE.md §5 — found, allowed, active, not out of
        # service — with insurance appended: the first three are statements
        # about the authority itself, and this one is a filing that lags it.
        if allowed is None or common_authority is None or contract_authority is None:
            # The line that settles a wrong field name: which keys the record
            # actually carries, and what they hold. `top_level` is printed
            # separately because it survives truncation.
            logger.warning(
                "FMCSA payload for %s did not carry the fields we need "
                "(allowedToOperate/commonAuthorityStatus/contractAuthorityStatus). "
                "top_level=%s payload=%s",
                mc,
                sorted(str(key) for key in carrier),
                describe_payload(carrier, self._web_key),
            )
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.INCOMPLETE_RESPONSE, **context
            )

        if not allowed:
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.NOT_ALLOWED_TO_OPERATE, **context
            )

        if not (_active_authority(common_authority) or _active_authority(contract_authority)):
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.AUTHORITY_INACTIVE, **context
            )

        # Past here the carrier holds an authority. Everything below is about
        # whether it may be used, and `check()` reads this flag to decide
        # whether a second lookup — the authority's own record — is worth the
        # round trip.
        context["authority_active"] = True

        if _has_out_of_service_order(oos_date):
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.OUT_OF_SERVICE, **context
            )

        if _insurance_not_on_file(carrier):
            return AuthorityCheck(
                eligible=False, reason=IneligibilityReason.INSURANCE_NOT_ON_FILE, **context
            )

        return AuthorityCheck(eligible=True, **context)

    @staticmethod
    def _unavailable(mc: str, status_code: int | None = None) -> AuthorityCheck:
        if status_code is not None:
            logger.warning("FMCSA returned %s for MC %s", status_code, mc)
        return AuthorityCheck(
            eligible=False,
            mc_number=mc,
            reason=IneligibilityReason.VERIFICATION_UNAVAILABLE,
        )
