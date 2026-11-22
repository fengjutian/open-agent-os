"""Skill 管理：可复用的指令包。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import count_rows, get_or_404
from app.core.errors import AppError, ErrorCode
from app.core.security import READ, WRITE_SKILLS, Principal
from app.db.session import get_db
from app.models.entities import Skill
from app.schemas.dto import ErrorEnvelope, Page, SkillCreate, SkillRead, SkillUpdate
from app.services.audit import AuditService

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    409: {"model": ErrorEnvelope, "description": "资源冲突"},
}

router = APIRouter(prefix="/api/v1", tags=["skills"], dependencies=[Depends(get_principal)])


@router.post(
    "/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_skill(
    payload: SkillCreate,
    principal: Principal = Depends(require_permission(WRITE_SKILLS)),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Skill).where(Skill.name == payload.name)) is not None:
        raise AppError(ErrorCode.CONFLICT, f"技能 {payload.name} 已存在", status_code=409)

    skill = Skill(**payload.model_dump())
    db.add(skill)
    AuditService(db).record(
        action="skill.create",
        actor=principal_actor(principal),
        resource=f"skill:{payload.name}",
        decision="allow",
        reason="创建技能",
        detail={"instructions_chars": len(payload.instructions)},
    )
    db.commit()
    db.refresh(skill)
    return skill


@router.get("/skills", response_model=Page[SkillRead])
def list_skills(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    total = count_rows(db, Skill)
    items = db.scalars(
        select(Skill).order_by(Skill.name).limit(limit).offset(offset)
    ).all()
    return Page[SkillRead](items=items, total=total, limit=limit, offset=offset)


@router.get("/skills/{skill_id}", response_model=SkillRead, responses=_ERRORS)
def get_skill(
    skill_id: str,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    return get_or_404(db, Skill, skill_id, "Skill")


@router.patch("/skills/{skill_id}", response_model=SkillRead, responses=_ERRORS)
def update_skill(
    skill_id: str,
    payload: SkillUpdate,
    principal: Principal = Depends(require_permission(WRITE_SKILLS)),
    db: Session = Depends(get_db),
):
    skill = get_or_404(db, Skill, skill_id, "Skill")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(skill, key, value)

    AuditService(db).record(
        action="skill.update",
        actor=principal_actor(principal),
        resource=f"skill:{skill.name}",
        decision="allow",
        reason="更新技能",
        detail={"changed_fields": sorted(changes)},
    )
    db.commit()
    db.refresh(skill)
    return skill
