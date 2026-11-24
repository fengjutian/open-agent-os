"""V0.2 工具治理 API：注册表、策略规则、受控调用与审计查询。

治理结论在响应体中表达（``status`` / ``allowed``），HTTP 状态码只表示请求本身是否被处理——
被策略拒绝不是 API 错误，而是一次成功的治理决策。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import count_rows, get_or_404
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.request_context import run_id_var
from app.core.security import INVOKE_TOOLS, READ, WRITE_POLICY, WRITE_TOOLS, Principal
from app.db.session import get_db
from app.models.entities import (
    Agent,
    AuditLog,
    Conversation,
    PolicyRule,
    ToolArtifact,
    ToolDefinition,
    ToolInvocation,
)
from app.schemas.dto import (
    AgentToolRead,
    AuditLogRead,
    AuditVerifyResult,
    ErrorEnvelope,
    McpSyncResult,
    Page,
    PolicyRuleCreate,
    PolicyRuleRead,
    ToolArtifactRead,
    ToolCreate,
    ToolInvocationRead,
    ToolInvokeRequest,
    ToolInvokeResponse,
    ToolRead,
    ToolUpdate,
)
from app.services.artifacts import stored_path
from app.services.audit import AuditService
from app.services.audit_chain import verify_chain
from app.services.mcp_client import McpError, McpStdioClient
from app.services.policy import PolicyEngine
from app.services.tool_gateway import ToolGateway
from app.services.tool_registry import ToolRegistry, assert_valid_schema

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_TOOL_ERRORS = {
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "资源冲突"},
}
_INVOKE_ERRORS = {
    **_TOOL_ERRORS,
    501: {"model": ErrorEnvelope, "description": "执行器不可用"},
    504: {"model": ErrorEnvelope, "description": "工具执行超时"},
}

router = APIRouter(prefix="/api/v1", tags=["tools"], dependencies=[Depends(get_principal)])


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


def _assert_mcp_config(payload: ToolCreate) -> None:
    """MCP 工具必须引用运维声明过的服务器，不能自带命令行。"""
    if payload.execution_type != "mcp":
        return

    config = payload.config or {}
    server_name = config.get("server")
    if not server_name:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "execution_type=mcp 时必须提供 config.server",
            status_code=422,
        )
    if server_name not in settings.mcp_servers:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"MCP 服务器 {server_name} 未在 MCP_SERVERS 中声明（只能由运维通过环境变量配置）",
            status_code=422,
        )


_EXECUTOR_RUNTIMES = frozenset({"shell", "python"})
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
_COMMAND_ARGUMENT_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _is_string_type(value: object) -> bool:
    return value == "string" or (isinstance(value, list) and "string" in value)


def _assert_executor_config(payload: ToolCreate) -> None:
    """隔离执行器工具只能在 config 里声明「运行时 + 镜像」，不能携带命令。

    命令必须来自调用参数：这样它才会经过 JSON Schema 校验、策略裁决与审计，
    而不是变成一条注册时就写死的任意命令通道。
    """
    if payload.execution_type != "executor":
        return

    config = payload.config or {}
    forbidden = sorted({"command", "argv", "cmd"} & set(config))
    if forbidden:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"executor 工具的 config 不允许携带命令字段（{', '.join(forbidden)}）；命令必须来自调用参数",
            status_code=422,
        )

    runtime = config.get("runtime")
    if runtime not in _EXECUTOR_RUNTIMES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "execution_type=executor 时 config.runtime 必须是 shell 或 python",
            status_code=422,
        )

    image = config.get("image")
    if image is not None and (not isinstance(image, str) or not _IMAGE_PATTERN.match(image)):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "config.image 不是合法的镜像引用（只允许字母、数字和 . _ - / : @，且不能以 - 开头）",
            status_code=422,
        )

    argument_name = config.get("command_argument", "command")
    if not isinstance(argument_name, str) or not _COMMAND_ARGUMENT_PATTERN.match(argument_name):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "config.command_argument 必须匹配 ^[a-z_][a-z0-9_]{0,63}$",
            status_code=422,
        )

    schema = payload.input_schema or {}
    properties = schema.get("properties")
    property_schema = properties.get(argument_name) if isinstance(properties, dict) else None
    if not isinstance(property_schema, dict) or not _is_string_type(property_schema.get("type")):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"executor 工具的 input_schema 必须把 {argument_name} 声明为 string",
            status_code=422,
        )
    required = schema.get("required")
    if not isinstance(required, list) or argument_name not in required:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"executor 工具的 input_schema 必须把 {argument_name} 列为必填",
            status_code=422,
        )


def mcp_tool_name(server_name: str, remote_name: str) -> str:
    """把「服务器名 + 远程工具名」压成合法的本地工具名。"""
    raw = f"{server_name}.{remote_name}".lower()
    cleaned = re.sub(r"[^a-z0-9_.-]", "-", raw).strip("-.")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"mcp-{cleaned}"
    return cleaned[:64]


@router.post("/tools", response_model=ToolRead, status_code=status.HTTP_201_CREATED, responses=_TOOL_ERRORS)
def create_tool(
    payload: ToolCreate,
    principal: Principal = Depends(require_permission(WRITE_TOOLS)),
    db: Session = Depends(get_db),
):
    registry = ToolRegistry(db)
    if registry.get_by_name(payload.name) is not None:
        raise AppError(ErrorCode.CONFLICT, f"工具 {payload.name} 已存在", status_code=409)

    # 注册时就校验 schema 本身合法，避免把坏契约存进库。
    assert_valid_schema(payload.input_schema)
    _assert_mcp_config(payload)
    _assert_executor_config(payload)

    tool = ToolDefinition(**payload.model_dump())
    db.add(tool)
    AuditService(db).record(
        action="tool.register",
        actor=principal_actor(principal),
        resource=f"tool:{payload.name}",
        decision="allow",
        reason="注册工具定义",
        detail={
            "execution_type": payload.execution_type,
            "permissions": payload.permissions,
            "timeout_seconds": payload.timeout_seconds,
        },
    )
    db.commit()
    db.refresh(tool)
    return tool


@router.get("/tools", response_model=Page[ToolRead])
def list_tools(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    total = count_rows(db, ToolDefinition)
    items = db.scalars(
        select(ToolDefinition).order_by(ToolDefinition.name).limit(limit).offset(offset)
    ).all()
    return Page[ToolRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/tools/{tool_id}", response_model=ToolRead, responses=_TOOL_ERRORS)
def get_tool(
    tool_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    return get_or_404(db, ToolDefinition, tool_id, "Tool")


@router.patch("/tools/{tool_id}", response_model=ToolRead, responses=_TOOL_ERRORS)
def update_tool(
    tool_id: str,
    payload: ToolUpdate,
    principal: Principal = Depends(require_permission(WRITE_TOOLS)),
    db: Session = Depends(get_db),
):
    tool = get_or_404(db, ToolDefinition, tool_id, "Tool")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(tool, key, value)
    AuditService(db).record(
        action="tool.update",
        actor=principal_actor(principal),
        resource=f"tool:{tool.name}",
        decision="allow",
        reason="更新工具定义",
        detail={"changed_fields": sorted(changes)},
    )
    db.commit()
    db.refresh(tool)
    return tool


# ---------------------------------------------------------------------------
# 策略规则
# ---------------------------------------------------------------------------


@router.post(
    "/policy-rules",
    response_model=PolicyRuleRead,
    status_code=status.HTTP_201_CREATED,
    responses=_TOOL_ERRORS,
)
def create_policy_rule(
    payload: PolicyRuleCreate,
    principal: Principal = Depends(require_permission(WRITE_POLICY)),
    db: Session = Depends(get_db),
):
    if payload.agent_id is not None:
        get_or_404(db, Agent, payload.agent_id, "Agent")

    rule = PolicyRule(**payload.model_dump())
    db.add(rule)
    AuditService(db).record(
        action="policy.create",
        actor=principal_actor(principal),
        resource=f"tool:{payload.tool_name}",
        decision=payload.effect,
        reason=payload.note or "创建策略规则",
        detail={
            "agent_id": payload.agent_id,
            "priority": payload.priority,
            "effect": payload.effect,
        },
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/policy-rules", response_model=Page[PolicyRuleRead])
def list_policy_rules(
    agent_id: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    conditions = [PolicyRule.agent_id == agent_id] if agent_id else []
    total = count_rows(db, PolicyRule, *conditions)
    statement = select(PolicyRule)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(PolicyRule.priority.desc(), PolicyRule.id).limit(limit).offset(offset)
    ).all()
    return Page[PolicyRuleRead](items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Agent 视角的可用工具 + 受控调用
# ---------------------------------------------------------------------------


@router.get(
    "/agents/{agent_id}/tools",
    response_model=Page[AgentToolRead],
    responses=_TOOL_ERRORS,
)
def list_agent_tools(
    agent_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")

    total = count_rows(db, ToolDefinition)
    tools = db.scalars(
        select(ToolDefinition).order_by(ToolDefinition.name).limit(limit).offset(offset)
    ).all()

    engine = PolicyEngine(db)
    items = []
    for tool in tools:
        if not tool.enabled:
            items.append(
                AgentToolRead(
                    name=tool.name,
                    description=tool.description,
                    execution_type=tool.execution_type,
                    enabled=False,
                    allowed=False,
                    reason="工具已停用",
                )
            )
            continue
        decision = engine.evaluate(agent_id=agent_id, tool_name=tool.name)
        items.append(
            AgentToolRead(
                name=tool.name,
                description=tool.description,
                execution_type=tool.execution_type,
                enabled=True,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
    return Page[AgentToolRead](items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/agents/{agent_id}/tools/{tool_name}/invoke",
    response_model=ToolInvokeResponse,
    responses=_INVOKE_ERRORS,
)
async def invoke_agent_tool(
    agent_id: str,
    tool_name: str,
    payload: ToolInvokeRequest,
    _principal: Principal = Depends(require_permission(INVOKE_TOOLS)),
    db: Session = Depends(get_db),
):
    get_or_404(db, Agent, agent_id, "Agent")
    if payload.conversation_id is not None:
        get_or_404(db, Conversation, payload.conversation_id, "Conversation")

    run_id = f"run_{uuid4().hex}"
    token = run_id_var.set(run_id)
    try:
        outcome = await ToolGateway(db).invoke(
            run_id=run_id,
            tool_name=tool_name,
            arguments=payload.arguments,
            agent_id=agent_id,
            conversation_id=payload.conversation_id,
        )
    finally:
        run_id_var.reset(token)

    stored_artifacts = db.scalars(
        select(ToolArtifact)
        .where(ToolArtifact.invocation_id == outcome.invocation_id)
        .order_by(ToolArtifact.path)
    ).all()

    return ToolInvokeResponse(
        run_id=run_id,
        invocation_id=outcome.invocation_id,
        tool_name=outcome.tool_name,
        status=outcome.status,
        allowed=outcome.allowed,
        reason=outcome.reason,
        result=outcome.result,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        artifacts=[ToolArtifactRead.model_validate(item) for item in stored_artifacts],
    )


# ---------------------------------------------------------------------------
# 审计与调用记录
# ---------------------------------------------------------------------------


@router.get("/tool-invocations", response_model=Page[ToolInvocationRead])
def list_tool_invocations(
    run_id: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    conditions = []
    if run_id:
        conditions.append(ToolInvocation.run_id == run_id)
    if tool_name:
        conditions.append(ToolInvocation.tool_name == tool_name)

    total = count_rows(db, ToolInvocation, *conditions)
    statement = select(ToolInvocation)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(ToolInvocation.started_at.desc(), ToolInvocation.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ToolInvocationRead](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/tool-invocations/{invocation_id}/artifacts",
    response_model=Page[ToolArtifactRead],
    responses=_TOOL_ERRORS,
)
def list_tool_artifacts(
    invocation_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """列出一次调用的产物索引（不返回文件内容）。"""
    get_or_404(db, ToolInvocation, invocation_id, "ToolInvocation")

    conditions = [ToolArtifact.invocation_id == invocation_id]
    total = count_rows(db, ToolArtifact, *conditions)
    items = db.scalars(
        select(ToolArtifact)
        .where(*conditions)
        .order_by(ToolArtifact.path)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ToolArtifactRead](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=FileResponse,
    responses={404: {"model": ErrorEnvelope, "description": "产物不存在"}},
)
def download_artifact(
    artifact_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """下载产物文件。

    始终以 `application/octet-stream` + attachment 返回，避免把用户可控内容
    当 HTML 在同一 origin 下渲染。
    """
    artifact = get_or_404(db, ToolArtifact, artifact_id, "Artifact")
    path = stored_path(artifact)
    if path is None:
        raise AppError(
            ErrorCode.ARTIFACT_NOT_FOUND,
            "产物文件不存在或已被清理",
            status_code=404,
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=Path(artifact.path).name,
    )


@router.get("/audit-logs/verify", response_model=AuditVerifyResult)
def verify_audit_logs(
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    """校验当前租户的审计哈希链，指出首个断裂点。"""
    report = verify_chain(db)
    return AuditVerifyResult(
        valid=report.valid,
        checked=report.checked,
        legacy=report.legacy,
        broken_at=report.broken_at,
        reason=report.reason,
    )


@router.get("/audit-logs", response_model=Page[AuditLogRead])
def list_audit_logs(
    run_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    conditions = []
    if run_id:
        conditions.append(AuditLog.run_id == run_id)
    if action:
        conditions.append(AuditLog.action == action)

    total = count_rows(db, AuditLog, *conditions)
    statement = select(AuditLog)
    if conditions:
        statement = statement.where(*conditions)
    items = db.scalars(
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    ).all()
    return Page[AuditLogRead](items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@router.post(
    "/mcp/servers/{server_name}/sync",
    response_model=McpSyncResult,
    responses=_TOOL_ERRORS,
)
async def sync_mcp_server(
    server_name: str,
    principal: Principal = Depends(require_permission(WRITE_TOOLS)),
    db: Session = Depends(get_db),
):
    """把某个 MCP 服务器上的工具同步进注册表。

    同步进来的工具依然完整走「注册表 → Schema → Policy → 审计 → 执行」，
    而且 MCP 服务器本身只能由运维在 `MCP_SERVERS` 里声明。
    """
    server = settings.mcp_servers.get(server_name)
    if server is None:
        raise AppError(ErrorCode.NOT_FOUND, f"MCP 服务器 {server_name} 未配置", status_code=404)

    timeout = min(settings.mcp_timeout_seconds, server.timeout_seconds)
    client = McpStdioClient(
        command=server.command, args=server.args, env=server.env, timeout=timeout
    )
    try:
        await asyncio.wait_for(client.start(), timeout=timeout)
        remote_tools = await asyncio.wait_for(client.list_tools(), timeout=timeout)
    except TimeoutError as exc:
        raise AppError(ErrorCode.TOOL_TIMEOUT, "列举 MCP 工具超时", status_code=504) from exc
    except McpError as exc:
        raise AppError(
            ErrorCode.MCP_ERROR, f"连接 MCP 服务器失败：{exc.message}", status_code=502
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(client.close())

    registry = ToolRegistry(db)
    audit = AuditService(db)
    result = McpSyncResult(server=server_name)
    pending: list[ToolDefinition] = []

    for remote in remote_tools:
        name = mcp_tool_name(server_name, remote.name)
        try:
            assert_valid_schema(remote.input_schema)
        except AppError as exc:
            result.skipped.append(f"{remote.name}: {exc.message}")
            continue

        tool = registry.get_by_name(name)
        action = "tool.update" if tool is not None else "tool.register"
        if tool is None:
            tool = ToolDefinition(name=name)
            db.add(tool)

        tool.description = remote.description
        tool.input_schema = remote.input_schema
        tool.execution_type = "mcp"
        tool.config = {"server": server_name, "tool": remote.name}
        audit.record(
            action=action,
            actor=principal_actor(principal),
            resource=f"tool:{name}",
            decision="allow",
            reason=f"同步 MCP 服务器 {server_name}",
            detail={"server": server_name, "remote_tool": remote.name},
        )
        pending.append(tool)

    db.commit()
    result.registered = [ToolRead.model_validate(tool) for tool in pending]
    return result
