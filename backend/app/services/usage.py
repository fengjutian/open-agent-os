"""用量报表：按时间桶与维度聚合运行与工具调用。

**为什么在 Python 里聚合，而不是 SQL `GROUP BY`**：时间分桶在 SQLite 与 PostgreSQL
上用的函数不同（`date` / `date_trunc`），时区处理也不一致；放在 Python 里两端行为
一样，也才做得成「没有数据的那天也要出现在折线图上」这种展示逻辑。
窗口内只取需要的几列，不加载整行实体。

**没有 token 计数**：模型网关目前不解析 `usage`，所以口径是字符数与调用次数，
不是 token。接入真实计费数据前，这个报表只能回答「用得多不多」，不能回答「花了多少钱」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Agent, Run, ToolInvocation

logger = logging.getLogger(__name__)

# 时间桶粒度与分解维度
BUCKETS = ("day", "week", "month")
DIMENSIONS = ("agent", "model", "tool")

# 窗口上限：再长就没人在界面上看了，而且要扫的行也太多
MAX_DAYS = 366


@dataclass
class UsageBucket:
    """一段时间或一个维度上的用量汇总。"""

    key: str
    runs: int = 0
    succeeded: int = 0
    failed: int = 0
    running: int = 0
    tool_calls: int = 0
    prompt_chars: int = 0
    answer_chars: int = 0
    errors: int = 0
    duration_total_ms: int = 0
    duration_count: int = 0

    @property
    def avg_duration_ms(self) -> int:
        if not self.duration_count:
            return 0
        return self.duration_total_ms // self.duration_count


@dataclass
class ToolStat:
    """单个工具的调用情况。"""

    key: str
    calls: int = 0
    errors: int = 0
    duration_total_ms: int = 0
    duration_count: int = 0
    runs: int = 0


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 读出来的是 naive 时间，统一按 UTC 处理再比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bucket_key(moment: datetime, bucket: str) -> str:
    if bucket == "day":
        return moment.date().isoformat()
    if bucket == "week":
        monday = moment.date() - timedelta(days=moment.weekday())
        iso = monday.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return moment.strftime("%Y-%m")


def _bucket_keys(days: int, bucket: str) -> list[str]:
    """列出窗口内所有桶（含没有数据的天），供折线图补零。"""
    now = now_utc()
    start = now - timedelta(days=days)
    keys: list[str] = []

    if bucket == "day":
        cursor = start.date()
        last = now.date()
        while cursor <= last:
            keys.append(cursor.isoformat())
            cursor += timedelta(days=1)
    elif bucket == "week":
        cursor = start.date() - timedelta(days=start.weekday())
        last = now.date() - timedelta(days=now.weekday())
        while cursor <= last:
            iso = cursor.isocalendar()
            keys.append(f"{iso.year}-W{iso.week:02d}")
            cursor += timedelta(days=7)
    else:
        cursor = start.date().replace(day=1)
        last = now.date().replace(day=1)
        while cursor <= last:
            keys.append(cursor.strftime("%Y-%m"))
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    start = _as_utc(started_at)
    end = _as_utc(finished_at)
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    if delta < 0:
        return 0
    return int(delta * 1000)


def _collect_runs(db: Session, since: datetime):
    """取窗口内的运行摘要列。列级查询同样受 ORM 租户过滤约束。"""
    return db.execute(
        select(
            Run.started_at,
            Run.finished_at,
            Run.status,
            Run.model,
            Run.agent_id,
            Run.tool_calls,
            Run.prompt_chars,
            Run.answer_chars,
        ).where(Run.started_at >= since)
    ).all()


def _collect_tools(db: Session, since: datetime):
    return db.execute(
        select(
            ToolInvocation.tool_name,
            ToolInvocation.run_id,
            ToolInvocation.status,
            ToolInvocation.error_code,
            ToolInvocation.started_at,
            ToolInvocation.finished_at,
        ).where(ToolInvocation.started_at >= since)
    ).all()


def _apply_run(bucket: UsageBucket, row) -> None:
    bucket.runs += 1
    status = row[2]
    if status == "succeeded":
        bucket.succeeded += 1
    elif status == "failed":
        bucket.failed += 1
        bucket.errors += 1
    else:
        bucket.running += 1
    bucket.tool_calls += row[5] or 0
    bucket.prompt_chars += row[6] or 0
    bucket.answer_chars += row[7] or 0
    duration = _duration_ms(row[0], row[1])
    if duration is not None:
        bucket.duration_total_ms += duration
        bucket.duration_count += 1


def overview(db: Session, *, days: int) -> UsageBucket:
    """窗口内的总量指标。

    `tool_calls` 取运行记录上的累计值（与按 Agent / 模型分解的口径一致）；
    按工具分解用的是 `tool_invocations` 表，两者的窗口过滤条件不同
    （运行按开始时间、调用按调用时间），跨窗口边界的运行会让两边差几条。
    """
    since = now_utc() - timedelta(days=days)
    total = UsageBucket(key="total")
    for row in _collect_runs(db, since):
        _apply_run(total, row)
    return total


def timeline(db: Session, *, days: int, bucket: str) -> list[UsageBucket]:
    """按时间桶的序列，空桶补零。"""
    since = now_utc() - timedelta(days=days)
    buckets = {key: UsageBucket(key=key) for key in _bucket_keys(days, bucket)}
    for row in _collect_runs(db, since):
        started = _as_utc(row[0])
        if started is None:
            continue
        key = _bucket_key(started, bucket)
        target = buckets.get(key)
        if target is None:
            # 窗口边界上可能落在列表之外（按本地日历切桶），忽略即可
            continue
        _apply_run(target, row)
    return list(buckets.values())


def breakdown(db: Session, *, days: int, dimension: str) -> list[UsageBucket]:
    """按 Agent / 模型 / 工具分解。

    各维度的口径不完全一样，接口文档里写明了：
    - `agent` / `model`：runs 与字符数来自运行记录，tool_calls 取运行上累计的值；
    - `tool`：runs 是「用过该工具的运行数」，tool_calls 是该工具的调用次数，
      字符数不适用（工具调用本身不产生 prompt/answer）。
    """
    since = now_utc() - timedelta(days=days)

    if dimension == "tool":
        return _breakdown_by_tool(db, since)
    return _breakdown_by_runs(db, since, dimension)


def _breakdown_by_runs(db: Session, since: datetime, dimension: str) -> list[UsageBucket]:
    buckets: dict[str, UsageBucket] = {}
    for row in _collect_runs(db, since):
        if dimension == "agent":
            raw = row[4]
            fallback = "(未指定 Agent)"
        else:
            raw = row[3]
            fallback = "(未知模型)"
        key = raw or fallback
        bucket = buckets.setdefault(key, UsageBucket(key=key))
        _apply_run(bucket, row)
    return sorted(buckets.values(), key=lambda item: item.runs, reverse=True)


def _breakdown_by_tool(db: Session, since: datetime) -> list[UsageBucket]:
    stats: dict[str, ToolStat] = {}
    runs_by_tool: dict[str, set[str]] = {}
    for row in _collect_tools(db, since):
        name = row[0] or "(未命名工具)"
        stat = stats.setdefault(name, ToolStat(key=name))
        stat.calls += 1
        runs_by_tool.setdefault(name, set()).add(row[1] or "")
        if row[2] == "failed" or row[3]:
            stat.errors += 1
        duration = _duration_ms(row[4], row[5])
        if duration is not None:
            stat.duration_total_ms += duration
            stat.duration_count += 1

    buckets: list[UsageBucket] = []
    for name, stat in stats.items():
        bucket = UsageBucket(key=name)
        bucket.tool_calls = stat.calls
        bucket.errors = stat.errors
        bucket.runs = len(runs_by_tool.get(name, ()))
        bucket.duration_total_ms = stat.duration_total_ms
        bucket.duration_count = stat.duration_count
        buckets.append(bucket)
    return sorted(buckets, key=lambda item: item.tool_calls, reverse=True)


def agent_labels(db: Session, agent_ids: set[str]) -> dict[str, str]:
    """Agent id → 名称，给前端展示用。"""
    if not agent_ids:
        return {}
    rows = db.execute(
        select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def active_dimensions(db: Session, *, days: int) -> tuple[int, int, int]:
    """窗口内用到的 Agent 数、模型数、工具数。"""
    since = now_utc() - timedelta(days=days)
    agents = (
        db.scalar(
            select(func.count(func.distinct(Run.agent_id))).where(
                Run.started_at >= since
            )
        )
        or 0
    )
    models = (
        db.scalar(
            select(func.count(func.distinct(Run.model))).where(Run.started_at >= since)
        )
        or 0
    )
    tools = (
        db.scalar(
            select(func.count(func.distinct(ToolInvocation.tool_name))).where(
                ToolInvocation.started_at >= since
            )
        )
        or 0
    )
    return int(agents), int(models), int(tools)


def now_utc() -> datetime:
    return datetime.now(UTC)
