"""Run / RunEvent 记录。

此前 run_id 只活在日志里：能 grep，却不能按 id 回溯一次运行做过什么。
现在每次运行都有主体记录 + 追加式事件流。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Run, RunEvent, now_utc

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


class RunRecorder:
    """按序写入事件，并在结束时结算 Run。"""

    def __init__(self, db: Session, run: Run) -> None:
        self._db = db
        self.run = run
        self._seq = 0

    def event(self, event_type: str, **payload: Any) -> None:
        self._seq += 1
        self._db.add(
            RunEvent(run_id=self.run.id, seq=self._seq, type=event_type, payload=payload)
        )

    def succeed(
        self,
        *,
        iterations: int,
        tool_calls: int,
        answer_chars: int,
        prompt_chars: int,
    ) -> None:
        self.run.status = STATUS_SUCCEEDED
        self.run.iterations = iterations
        self.run.tool_calls = tool_calls
        self.run.answer_chars = answer_chars
        self.run.prompt_chars = prompt_chars
        self.run.finished_at = now_utc()
        self.event(
            "run.completed",
            iterations=iterations,
            tool_calls=tool_calls,
            answer_chars=answer_chars,
            prompt_chars=prompt_chars,
        )


def finalize_failure(
    db: Session,
    *,
    run_id: str,
    error_code: str,
    error_message: str,
    fallback: dict[str, Any],
) -> None:
    """记录失败。

    运行期间工具调用会自行提交（真实副作用不应被撤销），因此这里不能假设
    Run 行还没落库——存在就更新，不存在才插入。
    """
    db.rollback()

    run = db.get(Run, run_id)
    if run is None:
        run = Run(id=run_id, status=STATUS_FAILED, started_at=now_utc(), **fallback)
        db.add(run)

    run.status = STATUS_FAILED
    run.error_code = error_code
    run.error_message = error_message
    run.finished_at = now_utc()

    next_seq = (
        db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0
    ) + 1
    db.add(
        RunEvent(
            run_id=run_id,
            seq=next_seq,
            type="run.failed",
            payload={"error_code": error_code, "error_message": error_message},
        )
    )
    db.commit()


def runs_in_last_24h(db: Session, *, key_id: str | None) -> int:
    if key_id is None:
        return 0
    since = now_utc() - timedelta(days=1)
    return (
        db.scalar(
            select(func.count()).select_from(Run).where(Run.key_id == key_id, Run.started_at >= since)
        )
        or 0
    )
