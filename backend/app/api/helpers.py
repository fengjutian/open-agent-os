"""路由层共用的小工具。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode


def get_or_404(db: Session, model, ident: str, label: str):
    item = db.get(model, ident)
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found", status_code=404)
    return item


def count_rows(db: Session, model, *conditions) -> int:
    statement = select(func.count()).select_from(model)
    if conditions:
        statement = statement.where(*conditions)
    return db.scalar(statement) or 0
