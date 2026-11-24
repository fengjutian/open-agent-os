"""受控工具调用网关。

严格按顺序执行，任何一步不通过都不会进入下一步：

1. **Tool Registry** —— 工具必须已注册且启用；
2. **JSON Schema** —— 参数必须符合工具声明的契约；
3. **Policy Engine** —— 服务端策略必须放行（默认拒绝）；
4. **Audit** —— 决策先落审计，再执行（失败也留痕）；
5. **Execution** —— 只执行内建白名单实现。

每次调用都会写一条 ``ToolInvocation``，并至少写一条 ``AuditLog``。
网关自己负责提交，保证审计记录不会因为上层回滚而丢失。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.telemetry import record_tool, span
from app.core.tenancy import current_tenant
from app.models.entities import ToolInvocation, now_utc
from app.services.artifacts import ArtifactStore
from app.services.audit import AuditService, agent_actor
from app.services.policy import PolicyEngine
from app.services.tool_executor import execute_tool
from app.services.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_DENIED = "denied"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ToolCallOutcome:
    invocation_id: str
    tool_name: str
    status: str
    allowed: bool
    reason: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class ToolGateway:
    def __init__(self, db: Session) -> None:
        self._db = db

    async def invoke(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolCallOutcome:
        """受控调用的入口：套一层 span 与指标，主体逻辑在 _invoke。"""
        with span(
            "tool.call", **{"tool.name": tool_name, "tenant.id": current_tenant()}
        ) as current:
            outcome = await self._invoke(
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            current.set_attribute("tool.status", outcome.status)
            current.set_attribute("tool.allowed", outcome.allowed)
            record_tool(status=outcome.status)
            return outcome

    async def _invoke(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolCallOutcome:
        audit = AuditService(self._db)
        invocation = ToolInvocation(
            run_id=run_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
            status=STATUS_DENIED,
            started_at=now_utc(),
        )
        self._db.add(invocation)
        self._db.flush()

        registry = ToolRegistry(self._db)

        # ---- 第 1~3 步：存在性 → 参数契约 → 服务端策略 ----
        try:
            tool = registry.get_enabled(tool_name)
            registry.validate_arguments(tool, arguments)
            decision = PolicyEngine(self._db).evaluate(agent_id=agent_id, tool_name=tool_name)
            if not decision.allowed:
                raise AppError(
                    ErrorCode.POLICY_DENIED,
                    "策略拒绝该工具调用",
                    status_code=403,
                    details={"reason": decision.reason},
                )
        except AppError as exc:
            return self._denied(invocation, audit, exc, run_id=run_id, agent_id=agent_id)

        # ---- 第 4 步：放行决策先落审计 ----
        audit.record(
            action="tool.invoke",
            actor=agent_actor(agent_id),
            resource=f"tool:{tool_name}",
            decision="allow",
            reason=decision.reason,
            detail={
                "run_id": run_id,
                "conversation_id": conversation_id,
                "argument_keys": sorted(arguments),
                "permissions": tool.permissions,
            },
        )

        # ---- 第 5 步：执行 ----
        try:
            result = await execute_tool(
                tool,
                arguments,
                invocation_id=invocation.id,
                run_id=run_id,
                artifact_store=ArtifactStore(self._db),
            )
        except AppError as exc:
            invocation.status = STATUS_FAILED
            invocation.error_code = exc.code
            invocation.error_message = exc.message
            invocation.finished_at = now_utc()
            audit.record(
                action="tool.execute.failed",
                actor=agent_actor(agent_id),
                resource=f"tool:{tool_name}",
                decision=None,
                reason=exc.message,
                detail={"run_id": run_id, "code": exc.code},
            )
            self._db.commit()
            return ToolCallOutcome(
                invocation_id=invocation.id,
                tool_name=tool_name,
                status=STATUS_FAILED,
                allowed=True,
                reason=decision.reason,
                error_code=exc.code,
                error_message=exc.message,
            )

        invocation.status = STATUS_SUCCEEDED
        invocation.result = result
        invocation.finished_at = now_utc()
        self._db.commit()

        logger.info(
            "tool invoked",
            extra={
                "fields": {
                    "run_id": run_id,
                    "tool": tool_name,
                    "status": STATUS_SUCCEEDED,
                    "agent_id": agent_id,
                }
            },
        )
        return ToolCallOutcome(
            invocation_id=invocation.id,
            tool_name=tool_name,
            status=STATUS_SUCCEEDED,
            allowed=True,
            reason=decision.reason,
            result=result,
        )

    def _denied(
        self,
        invocation: ToolInvocation,
        audit: AuditService,
        exc: AppError,
        *,
        run_id: str,
        agent_id: str | None,
    ) -> ToolCallOutcome:
        invocation.status = STATUS_DENIED
        invocation.error_code = exc.code
        invocation.error_message = exc.message
        invocation.finished_at = now_utc()
        audit.record(
            action="tool.invoke",
            actor=agent_actor(agent_id),
            resource=f"tool:{invocation.tool_name}",
            decision="deny",
            reason=exc.message,
            detail={
                "run_id": run_id,
                "code": exc.code,
                # 参数内容不落审计，只记录键名，避免把敏感数据写进日志。
                "argument_keys": sorted(invocation.arguments or {}),
            },
        )
        self._db.commit()

        logger.info(
            "tool invocation denied",
            extra={
                "fields": {
                    "run_id": run_id,
                    "tool": invocation.tool_name,
                    "code": exc.code,
                    "agent_id": agent_id,
                }
            },
        )
        return ToolCallOutcome(
            invocation_id=invocation.id,
            tool_name=invocation.tool_name,
            status=STATUS_DENIED,
            allowed=False,
            reason=exc.message,
            error_code=exc.code,
            error_message=exc.message,
        )
