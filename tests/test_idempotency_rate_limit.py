from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InfrastructureUnavailableError
from app.db.models import Tenant
from app.infra.rate_limit import RENEW_SCRIPT, RateLease, RedisRateLimiter
from app.services.idempotency import claim_request


@pytest.mark.asyncio
async def test_idempotency_payload_conflict(session: AsyncSession) -> None:
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    await claim_request(session, tenant.id, "same-key", "hash-a", "model", False)
    await session.commit()
    with pytest.raises(ConflictError, match="different payload"):
        await claim_request(session, tenant.id, "same-key", "hash-b", "model", False)


class BrokenRedis:
    async def eval(self, *args: object) -> int:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_rate_limit_fails_closed_when_redis_is_down() -> None:
    limiter = RedisRateLimiter(BrokenRedis(), 10, 1000, 2, 30)  # type: ignore[arg-type]
    with pytest.raises(InfrastructureUnavailableError):
        await limiter.acquire("tenant", 10)


class MissingLeaseRedis:
    def __init__(self) -> None:
        self.script: str | None = None

    async def eval(self, script: str, *args: object) -> int:
        self.script = script
        return 0


@pytest.mark.asyncio
async def test_renew_missing_lease_fails_closed_with_atomic_lua() -> None:
    redis = MissingLeaseRedis()
    limiter = RedisRateLimiter(redis, 10, 1000, 2, 30)
    renewed = await limiter.renew(RateLease("tenant", "missing"))
    assert renewed is False
    assert redis.script == RENEW_SCRIPT
    assert "ZSCORE" in RENEW_SCRIPT


@pytest.mark.asyncio
async def test_renew_fails_closed_when_redis_is_down() -> None:
    limiter = RedisRateLimiter(BrokenRedis(), 10, 1000, 2, 30)  # type: ignore[arg-type]
    with pytest.raises(InfrastructureUnavailableError, match="renewal"):
        await limiter.renew(RateLease("tenant", "lease"))
