"""Per-request context (trace id + error info), visible anywhere while a request is handled."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RequestContext:
    trace_id: str
    error_type: str | None = None
    error_message: str | None = None


_current: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def current() -> RequestContext | None:
    return _current.get()


def set_current(ctx: RequestContext):
    return _current.set(ctx)


def reset_current(token) -> None:
    _current.reset(token)
