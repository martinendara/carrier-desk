"""The tool endpoints.

Every route lives behind the API key dependency, installed once here on the
parent router rather than repeated per endpoint.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.api.routes import bookings, carriers, loads, negotiations, otp


def build_api_router(require_api_key: Callable[..., None]) -> APIRouter:
    """Assemble the router, with authentication applied to all of it."""
    router = APIRouter(dependencies=[Depends(require_api_key)])

    router.include_router(carriers.router)
    router.include_router(otp.router)
    router.include_router(loads.router)
    router.include_router(negotiations.router)
    router.include_router(bookings.router)

    return router
