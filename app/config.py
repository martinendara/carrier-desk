"""Settings loaded from the environment (and from `.env` during development).

Nothing in this repository hardcodes a host, a token or a key: every value
comes from the environment. `.env` is gitignored; `.env.example` documents the
variable names.
"""

from __future__ import annotations

from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime configuration.

    Defaults are the values CLAUDE.md section 4 commits to, so a missing
    variable degrades to the documented behaviour instead of crashing.
    """

    # env_file is resolved from the repository root so the settings load the
    # same way no matter which directory the process was started from.
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Legacy TMS -----------------------------------------------------
    tms_host: str = ""
    tms_port: int = 0
    # SecretStr keeps the token out of reprs, logs and tracebacks by accident.
    tms_token: SecretStr = SecretStr("")
    tms_connect_timeout_seconds: float = 3.0
    tms_read_deadline_seconds: float = 5.0
    tms_max_attempts: int = 3

    # --- carrier-desk HTTP API (phase 3) --------------------------------
    service_api_key: SecretStr = SecretStr("")

    # --- FMCSA (phase 2) ------------------------------------------------
    fmcsa_base_url: str = "https://mobile.fmcsa.dot.gov/qc/services"
    fmcsa_key: SecretStr = SecretStr("")
    fmcsa_timeout_seconds: float = 5.0

    # --- OTP (phase 2) --------------------------------------------------
    #: Key for the HMAC that protects stored codes. Generate with
    #: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    otp_hmac_secret: SecretStr = SecretStr("")
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 3
    otp_max_resends: int = 1

    # --- Negotiation (phase 2) ------------------------------------------
    negotiation_steps: str = "0.40,0.75"
    negotiation_urgent_steps: str = "0.60,0.90"
    negotiation_max_rounds: int = 3
    #: Offers are rounded down to this increment, and stay this far below the
    #: ceiling so a concession is always possible.
    negotiation_increment: int = 25
    negotiation_ceiling_margin: int = 25

    # --- Sessions (phase 2) ---------------------------------------------
    run_session_ttl_seconds: int = 3600

    @property
    def tms_token_value(self) -> str:
        """The TMS token as plain text. Call this only where it is needed."""
        return self.tms_token.get_secret_value()

    @property
    def negotiation_steps_tuple(self) -> tuple[Fraction, ...]:
        """Concession steps for a normal call."""
        return _parse_fractions("NEGOTIATION_STEPS", self.negotiation_steps)

    @property
    def negotiation_urgent_steps_tuple(self) -> tuple[Fraction, ...]:
        """Concession steps when the pickup is inside 24 hours."""
        return _parse_fractions("NEGOTIATION_URGENT_STEPS", self.negotiation_urgent_steps)


def _parse_fractions(name: str, raw: str) -> tuple[Fraction, ...]:
    """Parse a comma-separated list of decimals into exact rationals.

    `Fraction("0.40")` is 2/5 exactly. Going through `float` first would give
    0.4000000000000000222..., and this is money: a step that lands a hair below
    a multiple of 25 would silently lose a whole increment.
    """
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"{name} must contain at least one number")
    try:
        steps = tuple(Fraction(part) for part in parts)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{name} must be a comma-separated list of numbers") from exc
    for step in steps:
        if not 0 < step <= 1:
            raise ValueError(f"{name} entries must be between 0 and 1 exclusive")
    return steps


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
