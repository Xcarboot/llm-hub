from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.routes import (
    admin,
    customer_account,
    customer_auth,
    customer_payments,
    gateway,
    health,
)
from app.core.config import settings
from app.core.errors import GatewayError
from app.core.logging import configure_logging
from app.infra.rate_limit import RedisRateLimiter

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.assert_safe()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.rate_limiter = RedisRateLimiter(
        redis,
        settings.rpm_limit,
        settings.tpm_limit,
        settings.concurrency_limit,
        settings.concurrency_lease_seconds,
    )
    yield
    await redis.aclose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Admin-Token"],
    )


@app.middleware("http")
async def request_size_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": {"code": "invalid_length"}})
        if length > settings.max_request_bytes or length < 0:
            return JSONResponse(status_code=413, content={"error": {"code": "request_too_large"}})
    body = await request.body()
    if len(body) > settings.max_request_bytes:
        return JSONResponse(status_code=413, content={"error": {"code": "request_too_large"}})
    response = await call_next(request)
    if request.url.path.startswith(("/portal/", "/admin/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    del request
    # Pydantic errors include the raw password, activation token or request body in input/ctx.
    # Never return those values, or arbitrary validator messages which may interpolate them.
    errors = [
        {"type": item["type"], "loc": item["loc"], "msg": "Invalid request field"}
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


app.include_router(health.router)
app.include_router(gateway.router)
app.include_router(admin.router)
app.include_router(customer_auth.router)
app.include_router(customer_account.router)
app.include_router(customer_payments.router)
