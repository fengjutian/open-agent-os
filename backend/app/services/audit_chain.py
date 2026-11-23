"""审计日志的哈希链。

每条审计记录都带 `(seq, prev_hash, entry_hash)`：

```text
entry_hash = sha256(prev_hash + canonical(entry))
canonical   = 按固定字段与顺序序列化的 JSON
```

于是「改一行」「删一行」「插一行」都会在验证时暴露：
改内容 → entry_hash 不匹配；删中间行 → 后续 prev_hash 断链且 seq 不连续；
插伪造行 → 同样断链（除非攻击者把它之后的所有行全部重算）。

**能力边界**：这只是「防篡改」不是「防重写」。拥有数据库完整写权限的人可以
重算整条链。真正的防篡改需要外部锚定（WORM 存储、透明日志、定期把链头写入
不可变介质）。我们只能保证「局部改动一定会被发现」。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.entities import AuditLog

logger = logging.getLogger(__name__)

# 参与哈希的字段。顺序不重要（sort_keys），但集合一旦变化就会让历史链失效，
# 因此这份清单属于对外契约的一部分。
_HASHED_FIELDS = (
    "id",
    "tenant_id",
    "seq",
    "run_id",
    "request_id",
    "actor",
    "action",
    "resource",
    "decision",
    "reason",
    "detail",
)


def _iso_utc(value: datetime | None) -> str | None:
    """统一成 naive-UTC 的 ISO 字符串。

    写入时 `created_at` 是 aware UTC，而 SQLite 读回来是 naive（PG 读回来又是 aware）。
    哈希必须对这两种形态得到同一个字符串，否则同一条记录「写时算一遍、验时算一遍」
    会得出不同结果——这正是最初校验失败的原因。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat()


def canonical_payload(entry: AuditLog) -> str:
    payload = {name: getattr(entry, name) for name in _HASHED_FIELDS}
    payload["created_at"] = _iso_utc(entry.created_at)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def compute_entry_hash(prev_hash: str | None, entry: AuditLog) -> str:
    material = f"{prev_hash or ''}|{canonical_payload(entry)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def link_entry(db: Session, entry: AuditLog) -> None:
    """给待插入的记录补上 seq / prev_hash / entry_hash。

    必须在 `db.add()` **之前**调用：一旦加入 session，查询会触发 autoflush，
    而那时 seq 还是 NULL 会直接违反非空约束。
    """
    last = db.scalar(
        select(AuditLog)
        .where(AuditLog.seq.is_not(None))
        .order_by(AuditLog.seq.desc())
        .limit(1)
    )
    entry.seq = (last.seq if last is not None and last.seq is not None else 0) + 1
    entry.prev_hash = last.entry_hash if last is not None else None
    entry.entry_hash = compute_entry_hash(entry.prev_hash, entry)


@dataclass(frozen=True)
class VerifyReport:
    valid: bool
    checked: int
    legacy: int = 0
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(db: Session) -> VerifyReport:
    """校验当前租户的哈希链。历史未链接行单独计数，不影响结论。"""
    both = db.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)).all()
    chained = sorted(
        (row for row in both if row.seq is not None), key=lambda row: row.seq or 0
    )
    legacy = len(both) - len(chained)

    prev_hash: str | None = None
    expected_seq = 1

    for row in chained:
        if row.seq != expected_seq:
            return VerifyReport(
                valid=False,
                checked=expected_seq - 1,
                legacy=legacy,
                broken_at=row.seq,
                reason=f"序号不连续：期望 {expected_seq}，实际 {row.seq}（可能有记录被删除）",
            )
        if (row.prev_hash or None) != (prev_hash or None):
            return VerifyReport(
                valid=False,
                checked=expected_seq - 1,
                legacy=legacy,
                broken_at=row.seq,
                reason="prev_hash 与上一条不匹配（链路断裂）",
            )
        if row.entry_hash != compute_entry_hash(prev_hash, row):
            return VerifyReport(
                valid=False,
                checked=expected_seq - 1,
                legacy=legacy,
                broken_at=row.seq,
                reason="entry_hash 不匹配（内容被修改）",
            )

        prev_hash = row.entry_hash
        expected_seq += 1

    return VerifyReport(valid=True, checked=len(chained), legacy=legacy)


# ----------------------------------------------------------------------
# 只追加保护：任何通过 ORM 的更新/删除都直接失败
# ----------------------------------------------------------------------


@event.listens_for(AuditLog, "before_update")
def _deny_update(mapper, connection, target) -> None:  # noqa: ANN001 - SQLAlchemy 事件
    raise RuntimeError("审计日志是只追加的：禁止 UPDATE")


@event.listens_for(AuditLog, "before_delete")
def _deny_delete(mapper, connection, target) -> None:  # noqa: ANN001 - SQLAlchemy 事件
    raise RuntimeError("审计日志是只追加的：禁止 DELETE")
