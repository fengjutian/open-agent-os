"""服务端策略引擎。

安全姿态是 **deny-by-default**：没有任何 allow 规则命中，就拒绝。
模型只能「提出」调用请求，永远不能给自己授权——授权完全由这里的规则决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.entities import PolicyRule
from app.services.tool_offload import DEFAULT_ALLOWED_TOOLS

ALLOW = "allow"
DENY = "deny"
# 匹配任意工具名的通配规则。
ANY_TOOL = "*"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    effect: str | None = None
    rule_id: str | None = None


class PolicyEngine:
    def __init__(self, db: Session) -> None:
        self._db = db

    def evaluate(self, *, agent_id: str | None, tool_name: str) -> PolicyDecision:
        """按「优先级 → 适用范围 → deny 优先」的顺序选出生效规则。"""
        statements = (
            select(PolicyRule)
            .where(PolicyRule.enabled.is_(True))
            .where(or_(PolicyRule.agent_id == agent_id, PolicyRule.agent_id.is_(None)))
            .where(or_(PolicyRule.tool_name == tool_name, PolicyRule.tool_name == ANY_TOOL))
        )
        rules = self._db.scalars(statements).all()
        if not rules:
            # 平台内建的只读工具（如 artifact.read）在没有任何规则时默认放行：
            # 它是平台能力，不是用户注册的工具，要求每个人手工加规则才能用
            # 只会让人误以为功能坏了。**显式 deny 仍然生效**——只要有规则命中，
            # 就完全按规则裁决（上面已经走到这里说明没有规则命中）。
            if tool_name in DEFAULT_ALLOWED_TOOLS:
                return PolicyDecision(
                    allowed=True,
                    reason="平台内建只读工具，默认放行",
                    effect=ALLOW,
                )
            return PolicyDecision(
                allowed=False,
                reason="没有匹配的策略规则，按默认拒绝处理",
            )

        best = min(rules, key=self._rank)
        return PolicyDecision(
            allowed=best.effect == ALLOW,
            reason=f"命中策略规则 {best.id}（{best.effect}，priority={best.priority}）",
            effect=best.effect,
            rule_id=best.id,
        )

    @staticmethod
    def _rank(rule: PolicyRule) -> tuple[int, int, int, str]:
        # priority 大者优先；agent 专属规则优先于全局规则；同层 deny 优先于 allow。
        return (
            -rule.priority,
            0 if rule.agent_id else 1,
            0 if rule.effect == DENY else 1,
            rule.id,
        )
