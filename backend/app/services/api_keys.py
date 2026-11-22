"""API Key 的创建、校验与首次启动引导。"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.security import (
    ROLES,
    Principal,
    generate_api_key,
    hash_api_key,
    key_prefix,
    principal_for,
)
from app.models.entities import ApiKey, now_utc

logger = logging.getLogger(__name__)


class ApiKeyService:
    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # 创建与校验
    # ------------------------------------------------------------------

    def create(self, *, name: str, role: str) -> tuple[ApiKey, str]:
        """创建密钥并返回 (记录, 明文)。明文只在此刻可见。"""
        if role not in ROLES:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"未知角色 {role}", status_code=422)
        if self._db.scalar(select(ApiKey).where(ApiKey.name == name)) is not None:
            raise AppError(ErrorCode.CONFLICT, f"API Key 名称 {name} 已存在", status_code=409)

        raw = generate_api_key()
        record = ApiKey(
            name=name,
            key_prefix=key_prefix(raw),
            key_hash=hash_api_key(raw),
            role=role,
            enabled=True,
        )
        self._db.add(record)
        self._db.commit()
        self._db.refresh(record)
        return record, raw

    def authenticate(self, raw: str) -> Principal | None:
        record = self._db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw)))
        if record is None or not record.enabled:
            return None

        record.last_used_at = now_utc()
        self._db.commit()
        return principal_for(key_id=record.id, name=record.name, role=record.role)

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def list_all(self, *, limit: int, offset: int) -> tuple[list[ApiKey], int]:
        total = self._db.scalar(select(func.count()).select_from(ApiKey)) or 0
        items = self._db.scalars(
            select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id).limit(limit).offset(offset)
        ).all()
        return list(items), total

    def set_enabled(self, key_id: str, enabled: bool) -> ApiKey:
        record = self._db.get(ApiKey, key_id)
        if record is None:
            raise AppError(ErrorCode.NOT_FOUND, "API Key not found", status_code=404)
        if not enabled:
            self._ensure_not_last_admin(record)
        record.enabled = enabled
        self._db.commit()
        self._db.refresh(record)
        return record

    def _ensure_not_last_admin(self, record: ApiKey) -> None:
        """防止把最后一个启用的 admin 密钥停用，导致谁也进不来。"""
        if record.role != "admin":
            return
        others = self._db.scalars(
            select(ApiKey).where(
                ApiKey.role == "admin",
                ApiKey.enabled.is_(True),
                ApiKey.id != record.id,
            )
        ).all()
        if not others:
            raise AppError(
                ErrorCode.CONFLICT,
                "不能停用最后一个可用的 admin 密钥",
                status_code=409,
            )

    # ------------------------------------------------------------------
    # 引导
    # ------------------------------------------------------------------

    def ensure_bootstrap_admin(self, bootstrap: str) -> str | None:
        """确保存在一个 admin 密钥。

        返回明文表示「刚刚自动生成了，需要打印一次」；返回 None 表示无需告知。
        """
        if bootstrap:
            if self._db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(bootstrap))) is None:
                self._db.add(
                    ApiKey(
                        name=f"bootstrap-admin-{key_prefix(bootstrap)}",
                        key_prefix=key_prefix(bootstrap),
                        key_hash=hash_api_key(bootstrap),
                        role="admin",
                        enabled=True,
                    )
                )
                self._db.commit()
            return None

        has_admin = self._db.scalar(
            select(ApiKey).where(ApiKey.role == "admin", ApiKey.enabled.is_(True))
        )
        if has_admin is not None:
            return None

        raw = generate_api_key()
        self._db.add(
            ApiKey(
                name=f"bootstrap-admin-{key_prefix(raw)}",
                key_prefix=key_prefix(raw),
                key_hash=hash_api_key(raw),
                role="admin",
                enabled=True,
            )
        )
        self._db.commit()
        return raw
