"""请求限流与每日运行配额。

限流器后端可切换，语义一致（滑动窗口 + 超限返回 `Retry-After`）：

- `memory`（默认）：进程内滑动窗口，单实例够用、零依赖；
- `redis`：多个副本共享计数，需要 `REDIS_URL`。

配额（每日运行数）始终基于数据库里的 Run 记录统计，因此与限流后端无关。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Protocol
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.services.runs import runs_in_last_24h

WINDOW_SECONDS = 60
REDIS_KEY_PREFIX = "agent-os:ratelimit:"

# 超过这个数量的密钥桶就做一次清理，避免长期运行内存无限增长。
_PRUNE_THRESHOLD = 1024


class RateLimiterBackend(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> float | None:
        """记一次请求；超限时返回建议的重试等待秒数。"""
        ...


class InMemoryLimiter:
    """进程内滑动窗口。"""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> float | None:
        if limit <= 0:
            return None

        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            self._maybe_prune(cutoff)
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(0.0, bucket[0] + window_seconds - now)
            bucket.append(now)
            return None

    def _maybe_prune(self, cutoff: float) -> None:
        if len(self._hits) <= _PRUNE_THRESHOLD:
            return
        expired = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in expired:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisLimiter:
    """基于 Redis ZSET 的滑动窗口，多副本共享。

    用 ZSET 而不是固定窗口 INCR：滑动窗口不会在窗口边界出现两倍突发。
    pipeline(transaction=True) 让「清理 + 记账 + 计数 + 续期」成为一个原子操作。
    """

    def __init__(self, client) -> None:  # noqa: ANN001 - redis.asyncio.Redis
        self._client = client

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> float | None:
        if limit <= 0:
            return None

        now = time.time()  # 跨进程共享，必须用墙上时钟
        name = f"{REDIS_KEY_PREFIX}{key}"
        member = f"{now:.6f}:{uuid4().hex}"

        pipe = self._client.pipeline(transaction=True)
        pipe.zremrangebyscore(name, 0, now - window_seconds)
        pipe.zadd(name, {member: now})
        pipe.zcard(name)
        pipe.expire(name, window_seconds)
        results = await pipe.execute()

        count = int(results[2])
        if count <= limit:
            return None

        # 超限：把刚记的这条撤掉，避免持续请求把窗口越撑越满
        await self._client.zrem(name, member)
        oldest = await self._client.zrange(name, 0, 0, withscores=True)
        if oldest:
            return max(0.0, float(oldest[0][1]) + window_seconds - now)
        return float(window_seconds)


_backend: RateLimiterBackend | None = None
_backend_signature: tuple[str, str] | None = None
# 显式注入的后端优先于配置：测试与自定义部署都靠它。
_forced = False


def set_rate_limiter_backend(backend: RateLimiterBackend | None) -> None:
    """测试或自定义部署时替换后端；传 None 恢复按配置构造。"""
    global _backend, _backend_signature, _forced
    _backend = backend
    _backend_signature = None
    _forced = backend is not None


def _build_backend() -> RateLimiterBackend:
    signature = (settings.rate_limit_backend, settings.redis_url)
    global _backend, _backend_signature

    if _forced and _backend is not None:
        return _backend
    if _backend is not None and _backend_signature == signature:
        return _backend

    if settings.rate_limit_backend == "redis":
        if not settings.redis_url:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "RATE_LIMIT_BACKEND=redis 但未配置 REDIS_URL",
                status_code=500,
            )
        from redis.asyncio import Redis

        _backend = RedisLimiter(Redis.from_url(settings.redis_url, decode_responses=True))
    elif settings.rate_limit_backend == "memory":
        _backend = InMemoryLimiter()
    else:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"未知的 RATE_LIMIT_BACKEND: {settings.rate_limit_backend}",
            status_code=500,
        )

    _backend_signature = signature
    return _backend


def reset_rate_limiter() -> None:
    """测试用：清空计数。"""
    backend = _backend
    if isinstance(backend, InMemoryLimiter):
        backend.reset()


async def enforce_rate_limit(key_id: str) -> None:
    backend = _build_backend()
    retry_after = await backend.hit(
        key_id, limit=settings.rate_limit_per_minute, window_seconds=WINDOW_SECONDS
    )
    if retry_after is None:
        return

    wait = int(retry_after) + 1
    raise AppError(
        ErrorCode.RATE_LIMITED,
        f"请求过于频繁，请 {wait} 秒后重试",
        status_code=429,
        details={"limit": settings.rate_limit_per_minute, "window_seconds": WINDOW_SECONDS},
        headers={"Retry-After": str(wait)},
    )


def enforce_run_quota(db: Session, key_id: str | None) -> None:
    limit = settings.daily_run_quota
    if limit <= 0 or key_id is None:
        return

    used = runs_in_last_24h(db, key_id=key_id)
    if used < limit:
        return

    raise AppError(
        ErrorCode.QUOTA_EXCEEDED,
        f"24 小时内 Agent 运行次数已达上限（{limit} 次）",
        status_code=429,
        details={"limit": limit, "used": used, "window_hours": 24},
        headers={"Retry-After": "3600"},
    )
