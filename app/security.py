"""API key authentication for every tool endpoint.

The platform sends the key as `X-API-Key`. One dependency, applied to the whole
router, rather than a decorator per route: a check that has to be remembered is
a check that will eventually be forgotten, and this one is the only thing
between the internet and a service that books freight.
"""

from __future__ import annotations

import hmac
import logging
from typing import Callable

from fastapi import Header, HTTPException, Request, status

from app.api import phrasing

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"


def build_api_key_dependency(expected_key: str) -> Callable[..., None]:
    """A FastAPI dependency that rejects anything without the right key.

    The comparison is `compare_digest`, so the key cannot be recovered by timing
    a series of guesses.
    """

    def require_api_key(
        request: Request,
        x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
    ) -> None:
        if not expected_key:
            # No key configured means no key can be right. Refusing everything
            # is the only safe reading of an unconfigured guard.
            logger.error("SERVICE_API_KEY is not set; refusing every request")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "not_configured", "say": phrasing.INTERNAL},
            )

        if x_api_key is None or not hmac.compare_digest(x_api_key, expected_key):
            # Deliberately vague in the response: whether the header was missing
            # or wrong is not something an unauthenticated caller needs to know.
            # The log is more specific, because "the platform stopped sending a
            # key" and "someone is guessing" want different reactions.
            logger.warning(
                "rejected an unauthenticated request on %s: %s",
                request.url.path,
                "no X-API-Key header" if x_api_key is None else "wrong X-API-Key",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "say": phrasing.UNAUTHORIZED},
            )

    return require_api_key
