"""Skill：可复用的指令包。

注入策略：

- 只在拼上下文时并入 system prompt，因此天然受 `CONTEXT_MAX_CHARS` 约束；
- 另有 `SKILL_MAX_CHARS` 专门限制技能内容本身，避免少数技能挤占全部历史预算；
- 缺失或停用的技能**跳过而不是报错**，让历史 Agent 不会因为技能被改而不可用。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.models.entities import Agent, Skill

logger = logging.getLogger(__name__)

TRUNCATED_MARKER = "\n…（技能内容已截断）\n"


def assert_skills_exist(db: Session, names: list[str] | None) -> None:
    """创建/更新 Agent 时校验引用，避免挂上不存在的技能。"""
    if not names:
        return

    found = set(db.scalars(select(Skill.name).where(Skill.name.in_(names))).all())
    missing = [name for name in names if name not in found]
    if missing:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"技能不存在: {', '.join(missing)}",
            status_code=422,
            details={"missing": missing},
        )


def compose_system_prompt(db: Session, agent: Agent) -> tuple[str, list[str]]:
    """把 Agent 的基础 prompt 与技能指令拼成最终 system prompt。

    返回 (system_prompt, 实际生效的技能名)。
    """
    base = agent.system_prompt or ""
    names = list(agent.skill_names or [])
    if not names:
        return base, []

    skills = {
        skill.name: skill
        for skill in db.scalars(select(Skill).where(Skill.name.in_(names))).all()
    }

    budget = max(0, settings.skill_max_chars)
    sections: list[str] = []
    applied: list[str] = []
    used = 0

    for name in names:
        skill = skills.get(name)
        if skill is None:
            logger.warning("skill missing, skipped", extra={"fields": {"skill": name}})
            continue
        if not skill.enabled:
            logger.info("skill disabled, skipped", extra={"fields": {"skill": name}})
            continue

        block = f"## Skill: {skill.name}\n{skill.instructions.strip()}\n"
        remaining = budget - used
        if remaining <= len(TRUNCATED_MARKER) + 40:
            break

        if len(block) > remaining:
            block = block[: remaining - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER
            sections.append(block)
            applied.append(name)
            break

        sections.append(block)
        applied.append(name)
        used += len(block)

    if not sections:
        return base, []
    return f"{base}\n\n" + "\n".join(sections), applied
