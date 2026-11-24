"""定时任务：cron 表达式解析与调度。

设计取舍：

- **表达式按 UTC 解释**。多时区需求用「偏移量换算表达式」也能做，但容易出错，
  不如先把语义钉死；需要本地时间时由调用方自己换算。
- **先推进、后执行**。命中任务时先把 `next_run_at` 算到下下次并提交，再真正跑。
  这样即使执行过程崩溃，也不会在下一个 tick 重复触发同一条任务。
- **进程内轮询**，不引入外部调度器。单实例够用；多实例部署会重复执行，
  需要外部分布式锁（这一点写在文档里，不假装支持）。

支持的标准语法：`*`、数字、范围 `a-b`、步长 `*/n` 与 `a-b/n`、列表 `a,b,c`，
以及 `@hourly` / `@daily` / `@weekly` / `@monthly` 别名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# 各字段取值范围（cron 的周：0=周日，6=周六）
_RANGES: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

# 预报未来多久以内一定能找到触发时间；找不到说明表达式退化了（例如 2/30）
_MAX_LOOKAHEAD_DAYS = 366 * 5


class CronError(ValueError):
    """表达式非法。"""


@dataclass(frozen=True)
class Schedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _parse_field(raw: str, low: int, high: int) -> tuple[frozenset[int], bool]:
    """返回 (取值集合, 是否受限)。"""
    values: set[int] = set()
    restricted = False

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"空的字段片段：{raw!r}")

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"非法步长：{step_text!r}")
            step = int(step_text)
            restricted = True

        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            if not start_text.isdigit() or not end_text.isdigit():
                raise CronError(f"非法范围：{part!r}")
            start, end = int(start_text), int(end_text)
            restricted = True
        elif part.isdigit():
            start = end = int(part)
            restricted = True
        else:
            raise CronError(f"无法解析的字段片段：{part!r}")

        if start < low or end > high or start > end:
            raise CronError(f"取值超出范围 [{low}, {high}]：{part!r}")

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError("字段没有任何取值")
    return frozenset(values), restricted


def parse_schedule(expression: str) -> Schedule:
    text = (expression or "").strip()
    if not text:
        raise CronError("表达式不能为空")

    normalized = _ALIASES.get(text.lower(), text)
    parts = normalized.split()
    if len(parts) != 5:
        raise CronError(f"需要 5 个字段（分 时 日 月 周），实际 {len(parts)} 个")

    parsed = [_parse_field(part, low, high) for part, (low, high) in zip(parts, _RANGES, strict=False)]
    return Schedule(
        minutes=parsed[0][0],
        hours=parsed[1][0],
        days_of_month=parsed[2][0],
        months=parsed[3][0],
        days_of_week=parsed[4][0],
        dom_restricted=parsed[2][1],
        dow_restricted=parsed[4][1],
    )


def _day_matches(schedule: Schedule, day: datetime) -> bool:
    if day.month not in schedule.months:
        return False

    dom_ok = day.day in schedule.days_of_month
    # cron 里 0=周日；Python 的 isoweekday 是 1=周一…7=周日
    dow_ok = (day.isoweekday() % 7) in schedule.days_of_week

    # 标准语义：日与周都被限定时，满足其一即可；只有一个受限时，用它判断。
    if schedule.dom_restricted and schedule.dow_restricted:
        return dom_ok or dow_ok
    if schedule.dom_restricted:
        return dom_ok
    if schedule.dow_restricted:
        return dow_ok
    return True


def next_run(schedule: Schedule, after: datetime) -> datetime:
    """严格晚于 `after` 的下一个触发时间（UTC）。"""
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)

    start = (after + timedelta(minutes=1)).astimezone(UTC).replace(second=0, microsecond=0)
    hours = sorted(schedule.hours)
    minutes = sorted(schedule.minutes)

    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if _day_matches(schedule, day):
            for hour in hours:
                for minute in minutes:
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)

    raise CronError("未来 5 年内没有匹配的时间，请检查表达式")


def next_run_expression(expression: str, after: datetime) -> datetime:
    return next_run(parse_schedule(expression), after)
