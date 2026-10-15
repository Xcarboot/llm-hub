from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.db.models import (
    GatewayRequest,
    IdempotencyRecord,
    IdempotencyStatus,
)


@dataclass
class IdempotencyClaim:
    request: GatewayRequest
    record: IdempotencyRecord
    replay_response: dict[str, Any] | None = None


async def claim_request(
    session: AsyncSession,
    tenant_id: UUID,
    key: str,
    payload_hash: str,
    model: str,
    streaming: bool,
) -> IdempotencyClaim:
    existing = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ConflictError("Idempotency-Key was already used with a different payload")
        if existing.status == IdempotencyStatus.PROCESSING:
            raise ConflictError("request with this Idempotency-Key is still processing")
        if existing.streaming:
            raise ConflictError("streaming responses cannot be replayed")
        if existing.status == IdempotencyStatus.COMPLETED and existing.response_json is not None:
            request = await session.get(GatewayRequest, existing.gateway_request_id)
            assert request is not None
            return IdempotencyClaim(request, existing, existing.response_json)
        raise ConflictError("request with this Idempotency-Key previously failed")

    request = GatewayRequest(
        tenant_id=tenant_id,
        model=model,
        stream=streaming,
        payload_hash=payload_hash,
        status="PROCESSING",
    )
    session.add(request)
    await session.flush()
    record = IdempotencyRecord(
        tenant_id=tenant_id,
        idempotency_key=key,
        gateway_request_id=request.id,
        payload_hash=payload_hash,
        status=IdempotencyStatus.PROCESSING,
        streaming=streaming,
    )
    session.add(record)
    await session.flush()
    return IdempotencyClaim(request, record)


async def complete_request(
    session: AsyncSession,
    record: IdempotencyRecord,
    request: GatewayRequest,
    response: dict[str, Any] | None,
    usage: dict[str, Any] | None,
) -> None:
    record.status = IdempotencyStatus.COMPLETED
    record.response_json = response
    record.completed_at = datetime.now(UTC)
    request.status = "COMPLETED"
    request.usage_json = usage
    await session.flush()


async def fail_request(
    session: AsyncSession, record: IdempotencyRecord, request: GatewayRequest, code: str
) -> None:
    record.status = IdempotencyStatus.FAILED
    record.completed_at = datetime.now(UTC)
    request.status = "FAILED"
    request.error_code = code
    await session.flush()
