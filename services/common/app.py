"""Builds a FastAPI app with the request logging and tracing every service shares."""

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.context import RequestContext, current, reset_current, set_current
from common.errors import ServiceError
from common.logs import get_logger, setup_logging

TRACE_HEADER = "x-trace-id"
QUIET_PATHS = {"/health"}  # polled constantly; logging them would drown out real traffic


def create_app(service: str, **kwargs) -> FastAPI:
    setup_logging(service)
    log = get_logger()
    app = FastAPI(title=service, **kwargs)

    @app.exception_handler(ServiceError)
    async def handle_service_error(request: Request, exc: ServiceError):
        ctx = current()
        if ctx:
            ctx.error_type = exc.error_type
            ctx.error_message = exc.message
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error_type, "message": exc.message},
        )

    @app.middleware("http")
    async def observe(request: Request, call_next):
        ctx = RequestContext(trace_id=request.headers.get(TRACE_HEADER) or uuid.uuid4().hex)
        token = set_current(ctx)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("unhandled error")
                ctx.error_type = ctx.error_type or "InternalServerError"
                ctx.error_message = ctx.error_message or "unhandled exception"
                response = JSONResponse(
                    status_code=500,
                    content={"error": ctx.error_type, "message": ctx.error_message},
                )
            response.headers[TRACE_HEADER] = ctx.trace_id
            if request.url.path not in QUIET_PATHS:
                _log_request(log, request, response.status_code, start, ctx)
            return response
        finally:
            reset_current(token)

    return app


def _log_request(log, request: Request, status_code: int, start: float, ctx: RequestContext):
    route = request.scope.get("route")
    fields = {
        "method": request.method,
        "endpoint": getattr(route, "path", request.url.path),
        "status_code": status_code,
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        "error_type": ctx.error_type,
        "error_message": ctx.error_message,
    }
    level = "error" if status_code >= 500 else "warning" if status_code >= 400 else "info"
    getattr(log, level)("request", extra={"fields": fields})
