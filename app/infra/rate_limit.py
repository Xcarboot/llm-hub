from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.core.errors import InfrastructureUnavailableError

ACQUIRE_SCRIPT = """
local rpm_key = KEYS[1]
local tpm_key = KEYS[2]
local concurrent_key = KEYS[3]
local now = tonumber(ARGV[1])
local lease_until = tonumber(ARGV[2])
local lease_id = ARGV[3]
local rpm_limit = tonumber(ARGV[4])
local tpm_limit = tonumber(ARGV[5])
local concurrency_limit = tonumber(ARGV[6])
local estimated_tokens = tonumber(ARGV[7])
redis.call('ZREMRANGEBYSCORE', concurrent_key, '-inf', now)
local rpm = tonumber(redis.call('GET', rpm_key) or '0')
local tpm = tonumber(redis.call('GET', tpm_key) or '0')
local concurrent = tonumber(redis.call('ZCARD', concurrent_key))
if rpm + 1 > rpm_limit
  or tpm + estimated_tokens > tpm_limit
  or concurrent + 1 > concurrency_limit then
  return 0
end
redis.call('INCR', rpm_key)
redis.call('EXPIRE', rpm_key, 120)
redis.call('INCRBY', tpm_key, estimated_tokens)
redis.call('EXPIRE', tpm_key, 120)
redis.call('ZADD', concurrent_key, lease_until, lease_id)
redis.call('EXPIRE', concurrent_key, math.ceil((lease_until - now) / 1000) + 60)
return 1
"""

RENEW_SCRIPT = """
local concurrent_key = KEYS[1]
local now = tonumber(ARGV[1])
local lease_until = tonumber(ARGV[2])
local lease_id = ARGV[3]
if redis.call('ZSCORE', concurrent_key, lease_id) == false then
  return 0
end
redis.call('ZADD', concurrent_key, lease_until, lease_id)
redis.call('EXPIRE', concurrent_key, math.ceil((lease_until - now) / 1000) + 60)
return 1
"""


@dataclass(frozen=True)
class RateLease:
    tenant_id: str
    lease_id: str


class RedisRateLimiter:
    def __init__(
        self,
        redis: Any,
        rpm_limit: int,
        tpm_limit: int,
        concurrency_limit: int,
        lease_seconds: int,
    ) -> None:
        self.redis = redis
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.concurrency_limit = concurrency_limit
        self.lease_seconds = lease_seconds

    async def acquire(self, tenant_id: str, estimated_tokens: int) -> RateLease | None:
        import time

        now = int(time.time() * 1000)
        minute = now // 60_000
        lease_id = str(uuid4())
        keys = [
            f"rl:{tenant_id}:rpm:{minute}",
            f"rl:{tenant_id}:tpm:{minute}",
            f"rl:{tenant_id}:concurrent",
        ]
        try:
            allowed = await self.redis.eval(
                ACQUIRE_SCRIPT,
                3,
                *keys,
                now,
                now + self.lease_seconds * 1000,
                lease_id,
                self.rpm_limit,
                self.tpm_limit,
                self.concurrency_limit,
                estimated_tokens,
            )
        except Exception as exc:
            raise InfrastructureUnavailableError("rate limiter unavailable") from exc
        if int(allowed) != 1:
            return None
        return RateLease(tenant_id=tenant_id, lease_id=lease_id)

    async def renew(self, lease: RateLease) -> bool:
        import time

        now = int(time.time() * 1000)
        try:
            renewed = await self.redis.eval(
                RENEW_SCRIPT,
                1,
                f"rl:{lease.tenant_id}:concurrent",
                now,
                now + self.lease_seconds * 1000,
                lease.lease_id,
            )
        except Exception as exc:
            raise InfrastructureUnavailableError("rate limiter unavailable during renewal") from exc
        return int(renewed) == 1

    async def release(self, lease: RateLease) -> None:
        try:
            await self.redis.zrem(f"rl:{lease.tenant_id}:concurrent", lease.lease_id)
        except Exception as exc:
            raise InfrastructureUnavailableError("rate limiter unavailable during release") from exc
