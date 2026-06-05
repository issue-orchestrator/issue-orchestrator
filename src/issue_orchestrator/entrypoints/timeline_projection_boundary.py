"""HTTP boundary helpers for timeline-derived view-model projection failures."""

from __future__ import annotations

from functools import wraps
import logging
from logging import Logger
from typing import Any, Awaitable, Callable, Mapping, TypeAlias

from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..view_models.lifecycle_projection import LifecycleProjectionError

TimelineProjectionBoundaryError: TypeAlias = LifecycleProjectionError | ValidationError
logger = logging.getLogger(__name__)

TIMELINE_PROJECTION_BOUNDARY_EXCEPTIONS: tuple[type[Exception], ...] = (
    LifecycleProjectionError,
    ValidationError,
)


def timeline_projection_failed_response(
    *,
    logger: Logger,
    exc: Exception,
    route: str,
    issue_number: int | str | None = None,
    run_id: int | None = None,
) -> JSONResponse:
    """Return a typed API failure for invalid timeline projection state."""
    logger.error(
        "Timeline projection failed for %s issue=%s run=%s: %s",
        route,
        issue_number,
        run_id,
        exc,
        exc_info=True,
    )
    content: dict[str, Any] = {
        "error": "timeline_projection_failed",
        "detail": str(exc),
        "exception_type": type(exc).__name__,
        "route": route,
    }
    if issue_number is not None:
        content["issue_number"] = issue_number
    if run_id is not None:
        content["run_id"] = run_id
    return JSONResponse(status_code=500, content=content)


def timeline_projection_endpoint(
    route: str,
    *,
    issue_number_key: str | None = "issue_number",
    run_id_key: str | None = "run_id",
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Scope projection failures to a specific timeline-derived endpoint."""

    def decorate(endpoint: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(endpoint)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return await endpoint(*args, **kwargs)
            except TIMELINE_PROJECTION_BOUNDARY_EXCEPTIONS as exc:
                return timeline_projection_failed_response(
                    logger=logger,
                    exc=exc,
                    route=route,
                    issue_number=_path_value(kwargs, issue_number_key),
                    run_id=_path_int(kwargs, run_id_key),
                )

        return wrapped

    return decorate


def _path_value(kwargs: Mapping[str, Any], key: str | None) -> int | str | None:
    if key is None:
        return None
    value = kwargs.get(key)
    if isinstance(value, (int, str)):
        return value
    return None


def _path_int(kwargs: Mapping[str, Any], key: str | None) -> int | None:
    value = _path_value(kwargs, key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
