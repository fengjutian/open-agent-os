"""请求限流与每日运行配额。

限流器是**进程内**滑动窗口：单实例够用；多副本部署时换成 Redis 即可，
对外契约（429 + `Retry-After` + `rate_limited` / `quota_exceeded`）保持不变。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.services.runs import runs_in_last_24h

# 超过这个数量的密钥桶就做一次清理，避免长期运行内存无限增长。
_PRUNE_THRESHOLD = 1024


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> float | None:
        """记一次请求；超限时返回建议的重试等待秒数。"""
        if limit <= 0:
            return None

        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            self._maybe_prune(now, cutoff)
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(0.0, bucket[0] + self.window_seconds - now)
            bucket.append(now)
            return None

    def _maybe_prune(self, now: float, cutoff: float) -> None:
        if len(self._hits) <= _PRUNE_THRESHOLD:
            return
        expired = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in expired:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowLimiter()


def reset_rate_limiter() -> None:
    """测试用：清空计数。"""
    _limiter.reset()


def enforce_rate_limit(key_id: str) -> None:
    retry_after = _limiter.hit(key_id, settings.rate_limit_per_minute)
    if retry_after is None:
        return

    wait = int(retry_after) + 1
    raise AppError(
        ErrorCode.RATE_LIMITED,
        f"请求过于频繁，请 {wait} 秒后重试",
        status_code=429,
        details={"limit": settings.rate_limit_per_minute, "window_seconds": 60},
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
