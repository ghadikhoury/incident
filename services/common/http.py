"""Calls to other services. Forwards the trace id and turns failures into ServiceErrors."""

import httpx

from common.context import current
from common.errors import ServiceError

_client = httpx.Client()


def call(method: str, url: str, *, dependency: str, timeout: float, **kwargs) -> httpx.Response:
    """Call another service.

    Returns the response for 2xx/4xx (callers decide what a 4xx means).
    Raises ServiceError for timeouts, connection failures and 5xx responses.
    """
    ctx = current()
    headers = {"x-trace-id": ctx.trace_id} if ctx else {}
    try:
        response = _client.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except httpx.TimeoutException as exc:
        raise ServiceError(
            504, "DependencyTimeout", f"{dependency} did not respond within {timeout}s"
        ) from exc
    except httpx.TransportError as exc:
        raise ServiceError(
            503, "DependencyUnavailable", f"cannot connect to {dependency}: {exc}"
        ) from exc
    if response.status_code >= 500:
        raise ServiceError(
            502, "DependencyError", f"{dependency} returned HTTP {response.status_code}"
        )
    return response
