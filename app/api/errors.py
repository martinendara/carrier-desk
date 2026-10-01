"""One mapping from failure to something an agent can say out loud.

Every exception that can leave a route lands here. The rules:

* the response carries a short machine code and a spoken line — never the
  exception text, never a stack trace, never anything about the TMS;
* the internal detail goes to the log, where a human can find it and a carrier
  cannot;
* a failure that is the caller's fault (422) is separated from one that is ours
  (5xx), because the platform may want to retry ours and not theirs.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import phrasing
from app.fmcsa import McValidationError
from app.negotiation import (
    CeilingUnavailable,
    NegotiationAlreadyClosed,
    NegotiationError,
    NoActiveNegotiation,
)
from app.sessions import (
    LoadNotShown,
    NegotiationLimitReached,
    NotVerified,
    SessionError,
    SessionNotFound,
)
from app.tms.protocol import TmsBudgetExhausted, TmsFault, TmsInvalidInput

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A failure we have already decided how to describe."""

    def __init__(
        self,
        status_code: int,
        code: str,
        say: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.say = say
        #: Extra response headers, e.g. `Retry-After` on a 429.
        self.headers = headers or {}
        super().__init__(code)


def to_api_error(exc: Exception) -> ApiError:
    """Map a domain exception onto a spoken line.

    Ordered most specific first. Anything not listed is a bug on our side and
    becomes a 500 with a generic sentence — the exception text never travels.
    """
    if isinstance(exc, NotVerified):
        return ApiError(status.HTTP_403_FORBIDDEN, "not_verified", phrasing.NOT_VERIFIED)

    if isinstance(exc, SessionNotFound):
        return ApiError(status.HTTP_404_NOT_FOUND, "session_not_found", phrasing.SESSION_NOT_FOUND)

    if isinstance(exc, LoadNotShown):
        return ApiError(status.HTTP_409_CONFLICT, "load_not_shown", phrasing.LOAD_NOT_SHOWN)

    if isinstance(exc, NegotiationLimitReached):
        return ApiError(status.HTTP_409_CONFLICT, "negotiation_limit", phrasing.NEGOTIATION_LIMIT)

    if isinstance(exc, NoActiveNegotiation):
        return ApiError(status.HTTP_409_CONFLICT, "no_negotiation", phrasing.NO_NEGOTIATION)

    if isinstance(exc, NegotiationAlreadyClosed):
        return ApiError(status.HTTP_409_CONFLICT, "negotiation_closed", phrasing.NEGOTIATION_CLOSED)

    if isinstance(exc, CeilingUnavailable):
        return ApiError(status.HTTP_409_CONFLICT, "load_unavailable", phrasing.LOAD_UNAVAILABLE)

    if isinstance(exc, TmsBudgetExhausted):
        # 504: we gave up on the caller's clock. Retrying the whole request is
        # reasonable, which is why this is not a 500.
        return ApiError(status.HTTP_504_GATEWAY_TIMEOUT, "tms_timeout", phrasing.TMS_TIMEOUT)

    if isinstance(exc, TmsInvalidInput):
        # Must precede the TmsFault check below: this is our own bad request, not
        # a broken upstream, and the two say different things to a carrier.
        return ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request", phrasing.INVALID_REQUEST)

    if isinstance(exc, TmsFault):
        return ApiError(status.HTTP_502_BAD_GATEWAY, "tms_unavailable", phrasing.TMS_UNAVAILABLE)

    if isinstance(exc, McValidationError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_mc", phrasing.MC_INVALID)

    if isinstance(exc, ValueError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_request", phrasing.INVALID_REQUEST)

    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", phrasing.INTERNAL
    )


def describe_validation_errors(errors: list[dict]) -> str:
    """Field paths and error kinds, one per failed field. Never the values.

    Pydantic hands back a list of dicts that includes the offending input under
    `"input"`. That key is not read here: this line goes to a log, and the field
    name is what identifies the mistake anyway — "body.mc_number: string_type"
    is the whole diagnosis, and the value that arrived is the caller's.

    The same rule applies to `msg`, which pydantic builds from the constraint
    rather than from the input ("Input should be a valid string"), and to our
    own validators, whose messages name the field and the rule and never echo
    what they rejected.
    """
    described = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<body>"
        kind = error.get("type", "unknown")
        message = str(error.get("msg", "")).strip()
        described.append(f"{location}: {kind} ({message})" if message else f"{location}: {kind}")
    return "; ".join(described) or "<no detail>"


#: Domain exception bases that must never reach the generic handler. Registering
#: the bases rather than every leaf means a new exception type added under one of
#: them is mapped automatically instead of escaping as a 500 — and the generic
#: `Exception` handler is the one Starlette re-raises, so anything that lands
#: there surfaces as a stack trace rather than a sentence.
MAPPED_EXCEPTIONS = (
    TmsFault,
    SessionError,
    NegotiationError,
    McValidationError,
    ValueError,
)


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers. Called once, from `create_app`."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.info("api error %s on %s", exc.code, request.url.path)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "say": exc.say},
            headers=exc.headers,
        )

    def make_handler(name: str):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            mapped = to_api_error(exc)
            # The detail goes to the log. The response gets a sentence.
            logger.warning(
                "%s on %s: %s: %s", mapped.code, request.url.path, type(exc).__name__, exc
            )
            return JSONResponse(
                status_code=mapped.status_code,
                content={"error": mapped.code, "say": mapped.say},
            )

        handler.__name__ = name
        return handler

    for index, exception_type in enumerate(MAPPED_EXCEPTIONS):
        app.add_exception_handler(exception_type, make_handler(f"_mapped_{index}"))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # The field names and reasons are logged, not returned: the agent gets a
        # sentence it can say, and whoever is debugging gets the detail.
        #
        # This is the line that answers "the platform's test panel returns 422
        # and we cannot see which field it is sending wrong". It names the
        # field and the kind of problem, and nothing else — the value that
        # failed is in `exc.errors()` too, and is deliberately dropped.
        logger.warning(
            "invalid request on %s: %s",
            request.url.path,
            describe_validation_errors(exc.errors()),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "invalid_request", "say": phrasing.INVALID_REQUEST},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        logger.warning("http %s on %s", exc.status_code, request.url.path)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "say": phrasing.INTERNAL},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full traceback to the log. The agent hears a sentence.
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error", "say": phrasing.INTERNAL},
        )
