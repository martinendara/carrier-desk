"""Code generation, storage and masking. Pure functions, no state.

The two decisions that matter are here:

* **HMAC-SHA256, not a plain hash.** A 6-digit code is a million possibilities.
  An unsalted SHA-256 of one is a lookup table away from being read, and a
  leaked table would hand over every live code at once. Keying the digest with
  a server secret means a stolen table is useless without the secret.
* **The `run_id` is part of the message.** That binds a digest to the call it
  was issued for, so a digest captured from one call cannot be replayed against
  another even if the same six digits come up.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

CODE_DIGITS = 6


class WeakSecretError(RuntimeError):
    """The HMAC secret is missing or too short to be worth using."""


def generate_code(digits: int = CODE_DIGITS) -> str:
    """A uniformly random numeric code, zero-padded.

    `randbelow` rather than `random() % 10**digits`: the modulo of a float would
    bias the low codes, which is exactly the kind of weakness that never shows
    up in a demo and always shows up in an audit.
    """
    if digits < 4:
        raise ValueError("a code shorter than 4 digits is not worth generating")
    return f"{secrets.randbelow(10 ** digits):0{digits}d}"


def hash_code(secret: bytes, run_id: str, code: str) -> str:
    """The stored digest for `code` inside `run_id`."""
    if not secret:
        raise WeakSecretError("OTP HMAC secret is empty")
    message = f"{run_id}:{code}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_code(secret: bytes, run_id: str, code: str, expected_hmac: str) -> bool:
    """Constant-time comparison, so a wrong code cannot be found byte by byte."""
    if not expected_hmac:
        return False
    return hmac.compare_digest(hash_code(secret, run_id, code), expected_hmac)


def mask_email(email: str) -> str:
    """`m***@gmail.com`: enough for the agent to say where the code went.

    The full address is never spoken. A carrier who is not the owner of the
    mailbox should not learn it from the agent just by being on the call.
    """
    text = str(email).strip()
    if "@" not in text:
        return "***"
    local, _, domain = text.rpartition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def spoken_destination(masked: str) -> str:
    """A masked address, rendered the way a voice should say it.

    ``m***@gmail.com`` -> ``the one starting with M at Gmail``. The masked form
    already hides everything but the first letter and the domain, so this only
    rephrases what is safe to say out loud — the agent never reads asterisks.
    """
    text = str(masked).strip()
    if "@" not in text:
        return "the address we have on file"
    local, _, domain = text.rpartition("@")
    first = (local[:1] or "?").upper()
    domain_phrase = "Gmail" if domain.lower() == "gmail.com" else domain.removesuffix(".com")
    return f"the one starting with {first} at {domain_phrase}"
