import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import InfrastructureUnavailableError, UpstreamError, UpstreamMeteringError
from app.core.responses import ClosingStreamingResponse
from app.db.models import (
    IdempotencyStatus,
    ModelCatalog,
    PriceVersion,
    Reservation,
    ReservationStatus,
    Tenant,
)
from app.infra.rate_limit import RateLease
from app.schemas.gateway import ChatCompletionRequest
from app.services.gateway import GatewayService
from app.services.idempotency import claim_request
from app.services.ledger import create_tenant_accounts, ensure_platform_accounts, manual_recharge


class AllowLimiter:
    def __init__(self) -> None:
        self.released = False
        self.release_count = 0

    async def acquire(self, tenant_id: str, estimated_tokens: int) -> RateLease:
        return RateLease(tenant_id, "lease")

    async def renew(self, lease: RateLease) -> bool:
        return True

    async def release(self, lease: RateLease) -> None:
        self.released = True
        self.release_count += 1


class NonStreamClient:
    async def completion(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["stream"] is False
        assert kwargs["request_timeout"] == 120
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class NegativeUsageClient:
    async def completion(self, **kwargs: Any) -> Any:
        response = {
            "id": "chatcmpl-negative-usage",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": -1, "completion_tokens": 5},
        }
        if not kwargs["stream"]:
            return response

        async def chunks():
            yield response

        return chunks()


class StreamClient:
    async def completion(self, **kwargs: Any) -> Any:
        assert kwargs["stream"] is True

        async def chunks():
            yield {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}
            yield {"id": "chunk-2", "choices": [{"delta": {"content": " world"}}]}

        return chunks()


class APIConnectionError(Exception):
    pass


class ConnectionFailureClient:
    def __init__(self) -> None:
        self.calls = 0

    async def completion(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise APIConnectionError("connection reset after dispatch")


class OverageClient:
    def __init__(self) -> None:
        self.calls = 0

    async def completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": "chatcmpl-overage",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10_000, "completion_tokens": 10_000},
        }


class RenewalFailLimiter(AllowLimiter):
    async def renew(self, lease: RateLease) -> bool:
        return False


class SlowStreamClient:
    async def completion(self, **kwargs: Any) -> Any:
        async def chunks():
            yield {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}
            await asyncio.sleep(10)

        return chunks()


class RenewTrackingLimiter(AllowLimiter):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()
        self.renew_count = 0

    async def renew(self, lease: RateLease) -> bool:
        self.renew_count += 1
        self.renewed.set()
        return True


class BlockingFirstIterator:
    def __init__(self, release_first: asyncio.Event) -> None:
        self.release_first = release_first
        self.sent = False
        self.close_count = 0

    def __aiter__(self) -> "BlockingFirstIterator":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.sent:
            raise StopAsyncIteration
        await self.release_first.wait()
        self.sent = True
        return {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}

    async def aclose(self) -> None:
        self.close_count += 1


class FirstChunkBlockingClient:
    def __init__(self) -> None:
        self.release_first = asyncio.Event()
        self.calls = 0
        self.request_timeout: float | None = None
        self.iterator = BlockingFirstIterator(self.release_first)

    async def completion(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.request_timeout = kwargs["request_timeout"]
        return self.iterator


class CompletionBlockingClient:
    def __init__(self) -> None:
        self.release_completion = asyncio.Event()
        self.completion_started = asyncio.Event()

    async def completion(self, **kwargs: Any) -> Any:
        self.completion_started.set()
        await self.release_completion.wait()

        async def chunks():
            yield {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}

        return chunks()


class CancellableIterator:
    def __init__(self, waiting_for_second: asyncio.Event) -> None:
        self.waiting_for_second = waiting_for_second
        self.first = True
        self.close_count = 0

    def __aiter__(self) -> "CancellableIterator":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.first:
            self.first = False
            return {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}
        self.waiting_for_second.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.close_count += 1


class CancellableStreamClient:
    def __init__(self) -> None:
        self.waiting_for_second = asyncio.Event()
        self.iterator = CancellableIterator(self.waiting_for_second)

    async def completion(self, **kwargs: Any) -> Any:
        return self.iterator


class CancelledCloseIterator:
    def __init__(self) -> None:
        self.sent = False
        self.close_count = 0

    def __aiter__(self) -> "CancelledCloseIterator":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return {"id": "chunk-1", "choices": [{"delta": {"content": "hello"}}]}

    async def aclose(self) -> None:
        self.close_count += 1
        raise asyncio.CancelledError


class CancelledCloseStreamClient:
    def __init__(self) -> None:
        self.iterator = CancelledCloseIterator()

    async def completion(self, **kwargs: Any) -> Any:
        return self.iterator


class CloseTrackingBody:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.close_count = 0

    def __aiter__(self) -> "CloseTrackingBody":
        return self

    async def __anext__(self) -> bytes:
        return await self.inner.__anext__()

    async def aclose(self) -> None:
        self.close_count += 1
        await self.inner.aclose()


class MalformedUsageIterator:
    def __init__(self) -> None:
        self.sent = False
        self.close_count = 0

    def __aiter__(self) -> "MalformedUsageIterator":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return {"id": "chunk-1", "usage": "invalid"}

    async def aclose(self) -> None:
        self.close_count += 1


class MalformedUsageClient:
    def __init__(self) -> None:
        self.iterator = MalformedUsageIterator()

    async def completion(self, **kwargs: Any) -> Any:
        return self.iterator


class HeartbeatTrackingGateway(GatewayService):
    def __init__(self, settings: Settings, limiter: AllowLimiter, client: Any) -> None:
        super().__init__(settings, limiter, client)
        self.heartbeat_stopped = asyncio.Event()

    async def _renew_lease(
        self,
        lease: RateLease,
        failure: asyncio.Future[BaseException],
    ) -> None:
        try:
            await super()._renew_lease(lease, failure)
        finally:
            self.heartbeat_stopped.set()


async def seed_gateway(session: AsyncSession):
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    await create_tenant_accounts(session, tenant.id)
    await ensure_platform_accounts(session)
    model = ModelCatalog(
        public_name="enterprise-chat", litellm_model="openai/test", route_order=0
    )
    session.add(model)
    await session.flush()
    session.add(
        PriceVersion(
            model_id=model.id,
            input_micro_cny_per_million=1_000_000,
            output_micro_cny_per_million=2_000_000,
            effective_at=datetime.now(UTC),
        )
    )
    await manual_recharge(session, tenant.id, 10_000_000, str(uuid4()))
    await session.commit()
    return tenant


async def add_fallback(session: AsyncSession) -> None:
    model = ModelCatalog(
        public_name="enterprise-chat", litellm_model="openai/fallback", route_order=1
    )
    session.add(model)
    await session.flush()
    session.add(
        PriceVersion(
            model_id=model.id,
            input_micro_cny_per_million=1_000_000,
            output_micro_cny_per_million=2_000_000,
            effective_at=datetime.now(UTC),
        )
    )
    await session.commit()


def request(stream: bool) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="enterprise-chat",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=100,
        stream=stream,
    )


def safe_settings(**overrides: object) -> Settings:
    return Settings(
        admin_token="a" * 32,
        api_key_hmac_secret="b" * 32,
        **overrides,
    )


@pytest.mark.parametrize(
    ("prompt", "completion"),
    [
        ("10", 5),
        (10.0, 5),
        (True, 5),
        (10, False),
        (10, -1),
    ],
)
def test_usage_rejects_non_integer_boolean_and_negative_values(
    prompt: object,
    completion: object,
) -> None:
    assert (
        GatewayService._usage(
            {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_nonstream_proxy_mock_settles_usage(session: AsyncSession) -> None:
    tenant = await seed_gateway(session)
    body = request(False)
    claim = await claim_request(session, tenant.id, "idem-nonstream", "hash", body.model, False)
    limiter = AllowLimiter()
    service = GatewayService(safe_settings(), limiter, NonStreamClient())
    response = await service.complete_nonstream(session, tenant.id, claim, body)
    assert response["choices"][0]["message"]["content"] == "ok"
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.SETTLED
    assert limiter.released


@pytest.mark.asyncio
async def test_nonstream_negative_usage_conservatively_settles(session: AsyncSession) -> None:
    tenant = await seed_gateway(session)
    body = request(False)
    claim = await claim_request(
        session, tenant.id, "idem-negative-nonstream", "hash", body.model, False
    )
    limiter = AllowLimiter()
    service = GatewayService(safe_settings(), limiter, NegativeUsageClient())

    response = await service.complete_nonstream(session, tenant.id, claim, body)

    assert response["id"] == "chatcmpl-negative-usage"
    assert claim.request.usage_json is None
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED
    assert limiter.release_count == 1


@pytest.mark.asyncio
async def test_stream_cancel_conservatively_settles_ceiling(session: AsyncSession) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-stream", "hash", body.model, True)
    limiter = AllowLimiter()
    service = GatewayService(safe_settings(), limiter, StreamClient())
    stream = await service.prepare_stream(session, tenant.id, claim, body)
    first = await stream.__anext__()
    assert b"hello" in first
    await stream.aclose()
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED
    assert limiter.released


@pytest.mark.asyncio
async def test_stream_negative_usage_conservatively_settles(session: AsyncSession) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-negative-stream", "hash", body.model, True
    )
    limiter = AllowLimiter()
    service = GatewayService(safe_settings(), limiter, NegativeUsageClient())
    stream = await service.prepare_stream(session, tenant.id, claim, body)

    chunks = [chunk async for chunk in stream]

    assert chunks[-1] == b"data: [DONE]\n\n"
    assert claim.request.usage_json is None
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED
    assert limiter.release_count == 1


@pytest.mark.asyncio
async def test_connection_error_charges_ceiling_and_stops_fallback(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    await add_fallback(session)
    body = request(False)
    claim = await claim_request(session, tenant.id, "idem-connection", "hash", body.model, False)
    client = ConnectionFailureClient()
    service = GatewayService(safe_settings(), AllowLimiter(), client)

    with pytest.raises(UpstreamError, match="uncertain"):
        await service.complete_nonstream(session, tenant.id, claim, body)

    assert client.calls == 1
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_usage_over_reservation_is_recorded_without_retry(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    await add_fallback(session)
    body = request(False)
    claim = await claim_request(session, tenant.id, "idem-overage", "hash", body.model, False)
    client = OverageClient()
    service = GatewayService(safe_settings(), AllowLimiter(), client)

    with pytest.raises(UpstreamMeteringError, match="reserved ceiling"):
        await service.complete_nonstream(session, tenant.id, claim, body)

    assert client.calls == 1
    assert claim.record.status == IdempotencyStatus.COMPLETED
    assert claim.request.status == "COMPLETED_WITH_OVERAGE"
    assert claim.request.usage_json is not None
    assert claim.request.usage_json["overage"] is True
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_stream_renewal_failure_terminates_and_conservatively_settles(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-renewal", "hash", body.model, True)
    limiter = RenewalFailLimiter()
    service = GatewayService(
        safe_settings(concurrency_lease_seconds=1, request_timeout_seconds=0.5),
        limiter,
        SlowStreamClient(),
    )
    stream = await service.prepare_stream(session, tenant.id, claim, body)
    assert b"hello" in await stream.__anext__()

    with pytest.raises(InfrastructureUnavailableError, match="renewal failed"):
        await asyncio.wait_for(stream.__anext__(), timeout=2)

    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED
    assert limiter.released


@pytest.mark.asyncio
async def test_stream_renews_lease_while_waiting_for_stream_creation(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-create-renew", "hash", body.model, True)
    limiter = RenewTrackingLimiter()
    client = CompletionBlockingClient()
    service = GatewayService(
        safe_settings(concurrency_lease_seconds=1, request_timeout_seconds=0.8),
        limiter,
        client,
    )

    prepare = asyncio.create_task(service.prepare_stream(session, tenant.id, claim, body))
    await asyncio.wait_for(client.completion_started.wait(), timeout=0.2)
    await asyncio.wait_for(limiter.renewed.wait(), timeout=0.7)
    assert not prepare.done()

    client.release_completion.set()
    stream = await prepare
    assert b"hello" in await stream.__anext__()
    await stream.aclose()
    assert limiter.release_count == 1


@pytest.mark.asyncio
async def test_stream_renews_lease_while_waiting_for_first_chunk(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-first-renew", "hash", body.model, True)
    limiter = RenewTrackingLimiter()
    client = FirstChunkBlockingClient()
    service = GatewayService(
        safe_settings(concurrency_lease_seconds=1, request_timeout_seconds=0.8),
        limiter,
        client,
    )

    prepare = asyncio.create_task(service.prepare_stream(session, tenant.id, claim, body))
    await asyncio.wait_for(limiter.renewed.wait(), timeout=0.7)
    assert not prepare.done()
    assert limiter.renew_count >= 1

    client.release_first.set()
    stream = await prepare
    assert client.request_timeout == 0.8
    assert b"hello" in await stream.__anext__()
    await stream.aclose()
    assert client.iterator.close_count == 1
    assert limiter.release_count == 1


@pytest.mark.asyncio
async def test_stream_first_chunk_timeout_settles_and_releases(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    await add_fallback(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-first-timeout", "hash", body.model, True)
    limiter = AllowLimiter()
    client = FirstChunkBlockingClient()
    service = GatewayService(
        safe_settings(concurrency_lease_seconds=1, request_timeout_seconds=0.05),
        limiter,
        client,
    )
    original_finish = service._finish
    finish_calls = 0

    async def counted_finish(*args: Any, **kwargs: Any) -> None:
        nonlocal finish_calls
        finish_calls += 1
        await original_finish(*args, **kwargs)

    service._finish = counted_finish  # type: ignore[method-assign]
    with pytest.raises(UpstreamError, match="uncertain"):
        await service.prepare_stream(session, tenant.id, claim, body)

    assert client.calls == 1
    assert client.iterator.close_count == 1
    assert finish_calls == 1
    assert limiter.release_count == 1
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_stream_renewal_failure_before_first_chunk_settles_without_fallback(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    await add_fallback(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-renew-before-first", "hash", body.model, True
    )
    limiter = RenewalFailLimiter()
    client = FirstChunkBlockingClient()
    service = GatewayService(
        safe_settings(concurrency_lease_seconds=1, request_timeout_seconds=0.8),
        limiter,
        client,
    )
    original_finish = service._finish
    finish_calls = 0

    async def counted_finish(*args: Any, **kwargs: Any) -> None:
        nonlocal finish_calls
        finish_calls += 1
        await original_finish(*args, **kwargs)

    service._finish = counted_finish  # type: ignore[method-assign]
    with pytest.raises(UpstreamError, match="uncertain"):
        await service.prepare_stream(session, tenant.id, claim, body)

    assert client.calls == 1
    assert client.iterator.close_count == 1
    assert finish_calls == 1
    assert limiter.release_count == 1
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_stream_task_cancellation_completes_finish_and_lease_release(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(session, tenant.id, "idem-cancel-cleanup", "hash", body.model, True)
    limiter = AllowLimiter()
    client = CancellableStreamClient()
    service = GatewayService(safe_settings(), limiter, client)
    stream = await service.prepare_stream(session, tenant.id, claim, body)
    assert b"hello" in await stream.__anext__()

    original_finish = service._finish
    finish_started = asyncio.Event()
    allow_finish = asyncio.Event()
    finish_completed = asyncio.Event()
    finish_calls = 0

    async def delayed_finish(*args: Any, **kwargs: Any) -> None:
        nonlocal finish_calls
        finish_calls += 1
        finish_started.set()
        await allow_finish.wait()
        await original_finish(*args, **kwargs)
        finish_completed.set()

    service._finish = delayed_finish  # type: ignore[method-assign]
    consumer = asyncio.create_task(stream.__anext__())
    await asyncio.wait_for(client.waiting_for_second.wait(), timeout=1)
    consumer.cancel()
    await asyncio.wait_for(finish_started.wait(), timeout=1)
    assert not consumer.done()
    consumer.cancel()
    await asyncio.sleep(0)
    assert not consumer.done()

    allow_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert finish_completed.is_set()
    assert finish_calls == 1
    assert client.iterator.close_count == 1
    assert limiter.release_count == 1
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_upstream_aclose_cancelled_error_does_not_cancel_cleanup(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-upstream-close-cancel", "hash", body.model, True
    )
    limiter = AllowLimiter()
    client = CancelledCloseStreamClient()
    service = HeartbeatTrackingGateway(safe_settings(), limiter, client)
    stream = await service.prepare_stream(session, tenant.id, claim, body)

    await stream.aclose()

    assert client.iterator.close_count == 1
    assert service.heartbeat_stopped.is_set()
    assert limiter.release_count == 1
    assert claim.record.status == IdempotencyStatus.COMPLETED
    assert claim.request.status == "COMPLETED"
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_response_start_send_cancellation_closes_unstarted_stream(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-start-send-cancel", "hash", body.model, True
    )
    limiter = AllowLimiter()
    client = CancellableStreamClient()
    service = HeartbeatTrackingGateway(safe_settings(), limiter, client)
    stream = await service.prepare_stream(session, tenant.id, claim, body)
    tracked_body = CloseTrackingBody(stream)
    response = ClosingStreamingResponse(tracked_body, media_type="text/event-stream")
    start_sends = 0
    body_sends = 0

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        nonlocal start_sends, body_sends
        if message["type"] == "http.response.start":
            start_sends += 1
            raise asyncio.CancelledError
        if message["type"] == "http.response.body":
            body_sends += 1

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
    }
    with pytest.raises(asyncio.CancelledError):
        await response(scope, receive, send)
    await stream.aclose()

    assert start_sends == 1
    assert body_sends == 0
    assert tracked_body.close_count == 1
    assert client.iterator.close_count == 1
    assert service.heartbeat_stopped.is_set()
    assert limiter.release_count == 1
    assert claim.record.status == IdempotencyStatus.COMPLETED
    assert claim.request.status == "COMPLETED"
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_response_send_cancellation_closes_prepared_stream(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-send-cancel", "hash", body.model, True
    )
    limiter = AllowLimiter()
    client = CancellableStreamClient()
    service = HeartbeatTrackingGateway(safe_settings(), limiter, client)
    stream = await service.prepare_stream(session, tenant.id, claim, body)
    tracked_body = CloseTrackingBody(stream)
    response = ClosingStreamingResponse(tracked_body, media_type="text/event-stream")
    body_sends = 0

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        nonlocal body_sends
        if message["type"] == "http.response.body" and message.get("more_body"):
            body_sends += 1
            raise asyncio.CancelledError

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
    }
    with pytest.raises(asyncio.CancelledError):
        await response(scope, receive, send)

    assert body_sends == 1
    assert tracked_body.close_count == 1
    assert client.iterator.close_count == 1
    assert service.heartbeat_stopped.is_set()
    assert limiter.release_count == 1
    assert claim.record.status == IdempotencyStatus.COMPLETED
    assert claim.request.status == "COMPLETED"
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED


@pytest.mark.asyncio
async def test_malformed_first_chunk_usage_conservatively_settles(
    session: AsyncSession,
) -> None:
    tenant = await seed_gateway(session)
    body = request(True)
    claim = await claim_request(
        session, tenant.id, "idem-malformed-usage", "hash", body.model, True
    )
    limiter = AllowLimiter()
    client = MalformedUsageClient()
    service = HeartbeatTrackingGateway(safe_settings(), limiter, client)
    stream = await service.prepare_stream(session, tenant.id, claim, body)

    chunks = [chunk async for chunk in stream]

    assert chunks[-1] == b"data: [DONE]\n\n"
    assert client.iterator.close_count == 1
    assert service.heartbeat_stopped.is_set()
    assert limiter.release_count == 1
    assert claim.record.status == IdempotencyStatus.COMPLETED
    assert claim.request.status == "COMPLETED"
    reservation = await session.scalar(
        select(Reservation).where(Reservation.gateway_request_id == claim.request.id)
    )
    assert reservation is not None
    assert reservation.status == ReservationStatus.CONSERVATIVE_SETTLED
