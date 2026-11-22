"""Redis 限流后端（用 fakeredis 当替身，无需真实 Redis 服务）。"""

import asyncio

import fakeredis.aioredis
import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.services import rate_limit
from app.services.rate_limit import RedisLimiter


def run(coro):
    return asyncio.run(coro)


def _limiter() -> RedisLimiter:
    return RedisLimiter(fakeredis.aioredis.FakeRedis(decode_responses=True))


def test_allows_requests_within_limit():
    limiter = _limiter()

    async def scenario():
        results = [await limiter.hit("k1", limit=3, window_seconds=60) for _ in range(3)]
        return results

    assert run(scenario()) == [None, None, None]


def test_rejects_after_limit_with_retry_after():
    limiter = _limiter()

    async def scenario():
        await limiter.hit("k", limit=2, window_seconds=60)
        await limiter.hit("k", limit=2, window_seconds=60)
        return await limiter.hit("k", limit=2, window_seconds=60)

    retry_after = run(scenario())
    assert retry_after is not None
    assert 0 < retry_after <= 61


def test_keys_are_isolated():
    limiter = _limiter()

    async def scenario():
        await limiter.hit("a", limit=1, window_seconds=60)
        blocked = await limiter.hit("a", limit=1, window_seconds=60)
        other = await limiter.hit("b", limit=1, window_seconds=60)
        return blocked, other

    blocked, other = run(scenario())
    assert blocked is not None
    assert other is None


def test_zero_limit_disables():
    limiter = _limiter()
    assert run(limiter.hit("k", limit=0, window_seconds=60)) is None


def test_window_expiry_frees_capacity():
    limiter = _limiter()

    async def scenario():
        await limiter.hit("k", limit=1, window_seconds=1)
        blocked = await limiter.hit("k", limit=1, window_seconds=1)
        await asyncio.sleep(1.1)
        return blocked, await limiter.hit("k", limit=1, window_seconds=1)

    blocked, after = run(scenario())
    assert blocked is not None
    assert after is None


def test_enforce_rate_limit_uses_redis_backend(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    rate_limit.set_rate_limiter_backend(_limiter())
    try:

        async def scenario():
            await rate_limit.enforce_rate_limit("key-x")
            await rate_limit.enforce_rate_limit("key-x")

        with pytest.raises(AppError) as excinfo:
            run(scenario())

        assert excinfo.value.code == ErrorCode.RATE_LIMITED
        assert excinfo.value.status_code == 429
        assert "Retry-After" in (excinfo.value.headers or {})
    finally:
        rate_limit.set_rate_limiter_backend(None)


def test_redis_backend_without_url_fails_loudly(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "")
    rate_limit.set_rate_limiter_backend(None)

    with pytest.raises(AppError) as excinfo:
        run(rate_limit.enforce_rate_limit("key-y"))

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR


def test_unknown_backend_fails_loudly(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_backend", "memcached")
    rate_limit.set_rate_limiter_backend(None)

    with pytest.raises(AppError) as excinfo:
        run(rate_limit.enforce_rate_limit("key-z"))

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
