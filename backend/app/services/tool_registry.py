"""工具注册表：工具存在性、启用状态与 JSON Schema 校验。

调用顺序的第一、二步都在这里：**先确认工具真的注册过，再校验参数符合契约**。
"""

from __future__ import annotations

from typing import Any

import jsonschema
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.models.entities import ToolDefinition

# 参数没有任何结构声明时，退化为「必须是对象」，避免 schema 为空导致校验形同虚设。
_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False}


def build_validator(schema: dict[str, Any]) -> jsonschema.protocols.Validator:
    """按 schema 里声明的 $schema 选择校验器，默认 Draft 2020-12。"""
    validator_cls = jsonschema.validators.validator_for(
        schema, default=jsonschema.Draft202012Validator
    )
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def assert_valid_schema(schema: dict[str, Any]) -> None:
    """注册工具时校验 schema 本身是否合法。"""
    if not isinstance(schema, dict):
        raise AppError(ErrorCode.VALIDATION_ERROR, "input_schema 必须是 JSON 对象", status_code=422)
    try:
        build_validator(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"input_schema 不是合法的 JSON Schema: {exc.message}",
            status_code=422,
        ) from exc


class ToolRegistry:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_by_name(self, name: str) -> ToolDefinition | None:
        return self._db.scalar(select(ToolDefinition).where(ToolDefinition.name == name))

    def get_enabled(self, name: str) -> ToolDefinition:
        """第 1 步：工具存在性与启用状态。"""
        tool = self.get_by_name(name)
        if tool is None:
            raise AppError(ErrorCode.TOOL_NOT_FOUND, f"工具 {name} 未注册", status_code=404)
        if not tool.enabled:
            raise AppError(ErrorCode.TOOL_DISABLED, f"工具 {name} 已停用", status_code=403)
        return tool

    def list_enabled(self) -> list[ToolDefinition]:
        return list(
            self._db.scalars(
                select(ToolDefinition)
                .where(ToolDefinition.enabled.is_(True))
                .order_by(ToolDefinition.name)
            ).all()
        )

    @staticmethod
    def validate_arguments(tool: ToolDefinition, arguments: dict[str, Any]) -> None:
        """第 2 步：参数必须符合工具声明的 JSON Schema。"""
        schema = tool.input_schema or _EMPTY_OBJECT_SCHEMA
        validator = build_validator(schema)

        error = jsonschema.exceptions.best_match(validator.iter_errors(arguments))
        if error is not None:
            raise AppError(
                ErrorCode.TOOL_ARGUMENTS_INVALID,
                f"工具参数不符合 input_schema: {error.message}",
                status_code=422,
                details={"path": list(error.absolute_path), "validator": error.validator},
            )
