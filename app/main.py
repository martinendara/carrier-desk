"""The FastAPI application.

Run it with the factory:

    uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000

There is no module-level `app = create_app()`. Building the container touches
configuration that must fail loudly when it is wrong — a missing OTP secret, for
instance — and doing that at import time would make this module unimportable in
exactly the situations where you want to import it and read the error.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.errors import install_error_handlers
from app.api.routes import build_api_router
from app.config import Settings, get_settings
from app.container import Container
from app.security import build_api_key_dependency

logger = logging.getLogger(__name__)

#: TEMPORARY diagnostic (DIAG). The tool endpoints, for the Generate-button
#: investigation. Remove `_diag_middleware` and its registration when done.
_DIAG_PATHS = frozenset(
    {
        "/carriers/verify",
        "/otp/send",
        "/otp/verify",
        "/loads/search",
        "/negotiations",
        "/negotiations/counter",
        "/bookings",
        "/handoff",
    }
)


async def _diag_middleware(request: Request, call_next):
    """Log entry (key presence, run_id) and exit (status, result_code)."""
    path = request.url.path
    if request.method != "POST" or path not in _DIAG_PATHS:
        return await call_next(request)

    run_id = None
    try:
        body = await request.body()
        if body:
            run_id = json.loads(body).get("run_id")
    except (json.JSONDecodeError, ValueError):
        run_id = "<unparseable>"

    key = "present" if request.headers.get("X-API-Key") else "absent"
    logger.warning("DIAG enter %s %s key=%s run_id=%r", request.method, path, key, run_id)

    response = await call_next(request)

    result_code = None
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict):
            result_code = payload.get("result_code")
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    logger.warning(
        "DIAG exit %s status=%d result_code=%r", path, response.status_code, result_code
    )
    return response

API_DESCRIPTION = """
Tools for the inbound carrier sales voice agent.

Every endpoint takes an `X-API-Key` header, and every request body carries a
`run_id` that the platform injects as a fixed parameter. The agent never sends
an `otp_session_id`, a `negotiation_id` or a round number: the server resolves
all of that from the `run_id`, so there is nothing the agent can drop, replay or
invent.

Responses are short and written to be spoken. A field that would not fit in one
breath does not belong in one.

Failures return `{error, say}`: a machine code and a sentence for the agent. No
internal detail is ever returned.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests pass their own settings."""
    settings = settings or get_settings()
    container = Container.build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "carrier-desk starting: tms_mode=%s budget=%.1fs events=%s",
            settings.tms_mode,
            settings.request_budget_seconds,
            settings.events_jsonl_path,
        )
        try:
            yield
        finally:
            container.close()

    app = FastAPI(
        title="carrier-desk",
        version="0.3.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.container = container

    # TEMPORARY diagnostic (DIAG) — remove when the Generate-button issue is
    # resolved.
    app.middleware("http")(_diag_middleware)

    install_error_handlers(app)
    app.include_router(
        build_api_router(
            build_api_key_dependency(settings.service_api_key.get_secret_value())
        )
    )

    @app.get("/health", tags=["health"], summary="Liveness. No auth.")
    def health() -> dict[str, str]:
        # Deliberately says nothing about configuration: this endpoint is
        # unauthenticated, and which TMS mode we are in is not public. The
        # revision is just a build marker so a deploy can be confirmed.
        return {"status": "ok", "version": settings.app_version}

    return app
