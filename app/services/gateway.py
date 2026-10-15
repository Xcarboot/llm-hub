import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    InfrastructureUnavailableError,
    UpstreamError,
    UpstreamMeteringError,
)
from app.db.models import ModelCatalog, PriceVersion, Reservation
from app.infra.rate_limit import RateLease
from app.schemas.gateway import ChatCompletionRequest
from app.services.idempotency import IdempotencyClaim, complete_request, fail_request
from app.services.ledger import mark_dispatched, release, reserve, settle
from app.services.pricing import calculate_cost_micro_cny, estimate_input_tokens

logger = logging.getLogger(__name__)


class _LifecycleStream:
    """Own cleanup even when the event generator has never started."""

    def __init__(
        self,
        body: AsyncGenerator[bytes, None],
        cleanup: Callable[[], Awaitable[None]],
    ) -> None:
        self._body = body
        self._cleanup = cleanup

    def __aiter__(self) -> "_LifecycleStream":
        return self

    async def __anext__(self) -> bytes:
        return await self._body.__anext__()

    async def aclose(self) -> None:
        cleanup_error: BaseException | None = None
        try:
            await self._cleanup()
        except BaseException as exc:
            cleanup_error = exc

        try:
            await self._body.aclose()
        except BaseException as exc:
            if cleanup_error is None:
                raise
            logger.error(
                "failed to close event stream after lifecycle cleanup",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)


class CompletionClient(Protocol):
    async def completion(self, **kwargs: Any) -> Any: ...


class Limiter(Protocol):
    async def acquire(self, tenant_id: str, estimated_tokens: int) -> RateLease | None: ...

    async def renew(self, lease: RateLease) -> bool: ...

    async def release(self, lease: RateLease) -> None: ...


class GatewayService:
    def __init__(self, settings: Settings, limiter: Limiter, client: CompletionClient) -> None:
        self.settings = settings
        self.limiter = limiter
        self.client = client

    async def _routes_and_reservation(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        claim: IdempotencyClaim,
        request: ChatCompletionRequest,
    ) -> tuple[list[tuple[ModelCatalog, PriceVersion]], Reservation, RateLease]:
        models = (
            await session.scalars(
                select(ModelCatalog)
                .where(ModelCatalog.public_name == request.model, ModelCatalog.active.is_(True))
                .order_by(ModelCatalog.route_order)
            )
        ).all()
        if not models or models[0].route_order != 0:
            raise UpstreamError("requested model is not configured")
        if len(models) > 2:
            raise RuntimeError("MVP supports one primary and one fallback only")

        routes: list[tuple[ModelCatalog, PriceVersion]] = []
        now = datetime.now(UTC)
        for model in models:
            price = await session.scalar(
                select(PriceVersion)
                .where(PriceVersion.model_id == model.id, PriceVersion.effective_at <= now)
                .order_by(PriceVersion.effective_at.desc())
                .limit(1)
            )
            if price is None:
                raise InfrastructureUnavailableError("active route has no effective price")
            routes.append((model, price))

        input_tokens = estimate_input_tokens(
            request.messages, self.settings.token_estimate_safety_factor
        )
        estimated_total_tokens = input_tokens + request.max_tokens
        lease = await self.limiter.acquire(str(tenant_id), estimated_total_tokens)
        if lease is None:
            raise InfrastructureUnavailableError("rate limit exceeded")

        costs = [
            calculate_cost_micro_cny(
                input_tokens,
                request.max_tokens,
                price.input_micro_cny_per_million,
                price.output_micro_cny_per_million,
            )
            for _, price in routes
        ]
        ceiling = max(costs)
        snapshot: dict[str, object] = {
            str(model.id): {
                "price_version_id": str(price.id),
                "input_rate": price.input_micro_cny_per_million,
                "output_rate": price.output_micro_cny_per_million,
                "worst_case_micro_cny": cost,
            }
            for (model, price), cost in zip(routes, costs, strict=True)
        }
        try:
            reservation = await reserve(
                session,
                tenant_id,
                claim.request.id,
                ceiling,
                snapshot,
                self.settings.reservation_ttl_seconds,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            await self._release_lease_safely(lease)
            raise
        return routes, reservation, lease

    async def _mark_dispatch(self, session: AsyncSession, reservation_id: UUID) -> None:
        try:
            await mark_dispatched(session, reservation_id)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    @staticmethod
    def _definitely_no_charge(exc: BaseException) -> bool:
        if getattr(exc, "no_charge", False) is True:
            return True
        return type(exc).__name__ in {
            "AuthenticationError",
            "BadRequestError",
            "NotFoundError",
        }

    @staticmethod
    def _response_dict(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if hasattr(response, "model_dump"):
            return dict(response.model_dump())
        raise UpstreamError("upstream returned an unsupported response")

    @staticmethod
    def _usage(data: dict[str, Any]) -> tuple[int, int] | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if (
            not isinstance(prompt, int)
            or isinstance(prompt, bool)
            or prompt < 0
            or not isinstance(completion, int)
            or isinstance(completion, bool)
            or completion < 0
        ):
            return None
        return prompt, completion

    async def _finish(
        self,
        session: AsyncSession,
        claim: IdempotencyClaim,
        reservation: Reservation,
        price: PriceVersion,
        response: dict[str, Any] | None,
        usage: tuple[int, int] | None,
        *,
        force_conservative: bool = False,
    ) -> None:
        conservative = force_conservative or usage is None
        amount = reservation.amount_micro_cny
        usage_json: dict[str, Any] | None = None
        if usage is not None:
            amount = calculate_cost_micro_cny(
                usage[0],
                usage[1],
                price.input_micro_cny_per_million,
                price.output_micro_cny_per_million,
            )
            usage_json = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
            if amount > reservation.amount_micro_cny:
                usage_json.update(
                    {
                        "overage": True,
                        "calculated_cost_micro_cny": amount,
                        "reservation_ceiling_micro_cny": reservation.amount_micro_cny,
                    }
                )
                await settle(
                    session,
                    reservation.id,
                    reservation.amount_micro_cny,
                    price.id,
                    conservative=True,
                )
                await complete_request(
                    session, claim.record, claim.request, None, usage_json
                )
                claim.request.status = "COMPLETED_WITH_OVERAGE"
                claim.request.error_code = "upstream_usage_over_reservation"
                await session.commit()
                raise UpstreamMeteringError(
                    "upstream usage exceeded the reserved ceiling; ceiling charged"
                )
            if conservative:
                amount = reservation.amount_micro_cny
        await settle(session, reservation.id, amount, price.id, conservative=conservative)
        await complete_request(session, claim.record, claim.request, response, usage_json)
        await session.commit()

    async def _release_failed(
        self,
        session: AsyncSession,
        claim: IdempotencyClaim,
        reservation: Reservation,
        code: str,
    ) -> None:
        await release(session, reservation.id, code)
        await fail_request(session, claim.record, claim.request, code)
        await session.commit()

    async def _release_lease_safely(self, lease: RateLease) -> None:
        try:
            await self.limiter.release(lease)
        except Exception:
            logger.exception("failed to release concurrency lease")

    async def _await_stream_cleanup(self, cleanup: asyncio.Task[None]) -> None:
        """Wait for owned cleanup even if the request task is cancelled repeatedly."""
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        if cancellation is not None:
            if not cleanup.cancelled() and cleanup.exception() is not None:
                logger.error(
                    "stream cleanup failed while request task was cancelled",
                    exc_info=cleanup.exception(),
                )
            raise cancellation
        await cleanup

    async def _close_stream_safely(self, iterator: Any | None) -> None:
        if iterator is None:
            return
        close = getattr(iterator, "aclose", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(), timeout=self.settings.request_timeout_seconds)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.exception("failed to close upstream stream")

    async def _stream_cleanup(
        self,
        heartbeat: asyncio.Task[None],
        lease: RateLease,
        iterator: AsyncIterator[Any] | None,
        finalize: Callable[[], Awaitable[None]] | None,
    ) -> None:
        try:
            await self._close_stream_safely(iterator)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if finalize is not None:
                await finalize()
        finally:
            if not heartbeat.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release_lease_safely(lease)

    async def _await_stream_step(
        self,
        operation: Awaitable[Any],
        renewal_failure: asyncio.Future[BaseException],
        *,
        timeout_message: str,
        renewal_message: str,
        close_result_on_failure: bool = False,
    ) -> Any:
        operation_task = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait(
                {operation_task, renewal_failure},
                timeout=self.settings.request_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_failure in done:
                raise InfrastructureUnavailableError(renewal_message) from renewal_failure.result()
            if operation_task not in done:
                raise TimeoutError(timeout_message)
            return operation_task.result()
        except BaseException:
            operation_task.cancel()
            result = (await asyncio.gather(operation_task, return_exceptions=True))[0]
            if close_result_on_failure and not isinstance(result, BaseException):
                await self._close_stream_safely(result)
            raise

    async def _renew_lease(
        self,
        lease: RateLease,
        failure: asyncio.Future[BaseException],
    ) -> None:
        interval = self.settings.concurrency_lease_seconds / 3
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.limiter.renew(lease)
                    if not renewed:
                        raise InfrastructureUnavailableError(
                            "concurrency lease disappeared during streaming"
                        )
                except Exception as exc:
                    if not failure.done():
                        failure.set_result(exc)
                    return
        except asyncio.CancelledError:
            raise

    async def complete_nonstream(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        claim: IdempotencyClaim,
        request: ChatCompletionRequest,
    ) -> dict[str, Any]:
        routes, reservation, lease = await self._routes_and_reservation(
            session, tenant_id, claim, request
        )
        try:
            await self._mark_dispatch(session, reservation.id)
            last_error: BaseException | None = None
            for model, price in routes:
                candidate_ceiling = int(
                    reservation.pricing_snapshot[str(model.id)]["worst_case_micro_cny"]
                )
                if candidate_ceiling > reservation.amount_micro_cny:
                    continue
                try:
                    response = await self.client.completion(
                        model=model.litellm_model,
                        messages=[message.model_dump() for message in request.messages],
                        max_tokens=request.max_tokens,
                        temperature=request.temperature,
                        stream=False,
                        request_timeout=self.settings.request_timeout_seconds,
                    )
                    data = self._response_dict(response)
                    await self._finish(
                        session, claim, reservation, price, data, self._usage(data)
                    )
                    return data
                except UpstreamMeteringError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if not self._definitely_no_charge(exc):
                        await self._finish(
                            session,
                            claim,
                            reservation,
                            price,
                            None,
                            None,
                            force_conservative=True,
                        )
                        raise UpstreamError(
                            "upstream outcome is uncertain; ceiling charged"
                        ) from exc
            await self._release_failed(session, claim, reservation, "upstream_no_charge_failure")
            raise UpstreamError(
                "all upstream routes failed before producing billable output"
            ) from last_error
        finally:
            await self._release_lease_safely(lease)

    async def prepare_stream(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        claim: IdempotencyClaim,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[bytes]:
        routes, reservation, lease = await self._routes_and_reservation(
            session, tenant_id, claim, request
        )
        try:
            await self._mark_dispatch(session, reservation.id)
        except BaseException:
            lease_cleanup = asyncio.create_task(self._release_lease_safely(lease))
            await self._await_stream_cleanup(lease_cleanup)
            raise

        renewal_failure: asyncio.Future[BaseException] = (
            asyncio.get_running_loop().create_future()
        )
        heartbeat = asyncio.create_task(self._renew_lease(lease, renewal_failure))
        cleanup_task: asyncio.Task[None] | None = None
        settlement_started = False
        finished = False

        async def cleanup(
            iterator: AsyncIterator[Any] | None,
            finalize: Callable[[], Awaitable[None]] | None,
        ) -> None:
            nonlocal cleanup_task, settlement_started, finished
            if cleanup_task is None:
                settlement_started = finalize is not None

                async def owned_cleanup() -> None:
                    nonlocal finished
                    try:
                        await self._stream_cleanup(heartbeat, lease, iterator, finalize)
                    finally:
                        finished = True

                cleanup_task = asyncio.create_task(owned_cleanup())
            await self._await_stream_cleanup(cleanup_task)

        selected: tuple[PriceVersion, AsyncIterator[Any], dict[str, Any]] | None = None
        active_iterator: AsyncIterator[Any] | None = None
        attempted_price: PriceVersion | None = None
        last_error: BaseException | None = None
        try:
            for model, price in routes:
                candidate_ceiling = int(
                    reservation.pricing_snapshot[str(model.id)]["worst_case_micro_cny"]
                )
                if candidate_ceiling > reservation.amount_micro_cny:
                    continue
                attempted_price = price
                try:
                    raw_stream = await self._await_stream_step(
                        self.client.completion(
                            model=model.litellm_model,
                            messages=[message.model_dump() for message in request.messages],
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                            stream=True,
                            stream_options={"include_usage": True},
                            request_timeout=self.settings.request_timeout_seconds,
                        ),
                        renewal_failure,
                        timeout_message="upstream timed out creating stream",
                        renewal_message=(
                            "streaming concurrency lease renewal failed while creating stream"
                        ),
                        close_result_on_failure=True,
                    )
                    active_iterator = raw_stream
                    active_iterator = raw_stream.__aiter__()
                    first = self._response_dict(
                        await self._await_stream_step(
                            active_iterator.__anext__(),
                            renewal_failure,
                            timeout_message="upstream timed out before first byte",
                            renewal_message=(
                                "streaming concurrency lease renewal failed before first byte"
                            ),
                        )
                    )
                    selected = (price, active_iterator, first)
                    break
                except Exception as exc:
                    last_error = exc
                    if not renewal_failure.done() and self._definitely_no_charge(exc):
                        await self._close_stream_safely(active_iterator)
                        active_iterator = None
                        continue
                    await cleanup(
                        active_iterator,
                        partial(
                            self._finish,
                            session,
                            claim,
                            reservation,
                            price,
                            None,
                            None,
                            force_conservative=True,
                        ),
                    )
                    raise UpstreamError(
                        "upstream outcome is uncertain; ceiling charged"
                    ) from exc
            if selected is None:
                await cleanup(
                    active_iterator,
                    lambda: self._release_failed(
                        session, claim, reservation, "upstream_no_charge_failure"
                    ),
                )
                raise UpstreamError("all upstream routes failed before first byte") from last_error
        except BaseException:
            if cleanup_task is None:
                if attempted_price is None:
                    await cleanup(active_iterator, None)
                else:
                    await cleanup(
                        active_iterator,
                        lambda: self._finish(
                            session,
                            claim,
                            reservation,
                            attempted_price,
                            None,
                            None,
                            force_conservative=True,
                        ),
                    )
            raise

        price, iterator, first = selected

        def conservative_finalize() -> Awaitable[None]:
            return self._finish(
                session,
                claim,
                reservation,
                price,
                None,
                None,
                force_conservative=True,
            )

        async def event_stream() -> AsyncGenerator[bytes, None]:
            try:
                usage = self._usage(first)
                if renewal_failure.done():
                    raise InfrastructureUnavailableError(
                        "streaming concurrency lease renewal failed"
                    ) from renewal_failure.result()
                yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()
                while True:
                    if renewal_failure.done():
                        raise InfrastructureUnavailableError(
                            "streaming concurrency lease renewal failed"
                        ) from renewal_failure.result()
                    next_chunk: asyncio.Future[Any] = asyncio.ensure_future(
                        iterator.__anext__()
                    )
                    try:
                        done, _ = await asyncio.wait(
                            {next_chunk, renewal_failure},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except BaseException:
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
                        raise
                    if renewal_failure in done:
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
                        raise InfrastructureUnavailableError(
                            "streaming concurrency lease renewal failed"
                        ) from renewal_failure.result()
                    try:
                        raw_chunk = next_chunk.result()
                    except StopAsyncIteration:
                        break
                    chunk = self._response_dict(raw_chunk)
                    usage = self._usage(chunk) or usage
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                if renewal_failure.done():
                    raise InfrastructureUnavailableError(
                        "streaming concurrency lease renewal failed"
                    ) from renewal_failure.result()
                await cleanup(
                    iterator,
                    lambda: self._finish(session, claim, reservation, price, None, usage),
                )
                yield b"data: [DONE]\n\n"
            finally:
                if cleanup_task is None and not finished and not settlement_started:
                    await cleanup(iterator, conservative_finalize)

        return _LifecycleStream(
            event_stream(),
            lambda: cleanup(iterator, conservative_finalize),
        )
