"""Settings loaded from the environment (and from `.env` during development).

Nothing in this repository hardcodes a host, a token or a key: every value
comes from the environment. `.env` is gitignored; `.env.example` documents the
variable names.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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

    # --- Negotiation (phase 2) ------------------------------------------
    negotiation_steps: str = "0.40,0.75,1.00"

    @property
    def tms_token_value(self) -> str:
        """The TMS token as plain text. Call this only where it is needed."""
        return self.tms_token.get_secret_value()

    @property
    def negotiation_steps_tuple(self) -> tuple[float, ...]:
        """Concession steps parsed from the comma-separated env value."""
        raw = [part.strip() for part in self.negotiation_steps.split(",")]
        steps = tuple(float(part) for part in raw if part)
        if not steps:
            raise ValueError("NEGOTIATION_STEPS must contain at least one number")
        return steps


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
