"""知识库：建库、导入文档、检索。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_principal, principal_actor, require_permission
from app.api.helpers import get_or_404
from app.core.config import settings
from app.core.security import READ, WRITE_AGENTS, Principal
from app.db.session import get_db
from app.models.entities import KnowledgeBase, KnowledgeDocument
from app.schemas.dto import (
    ErrorEnvelope,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    Page,
)
from app.services.audit import AuditService
from app.services.knowledge import add_document, delete_document, search_chunks

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_ERRORS = {
    401: {"model": ErrorEnvelope, "description": "未认证"},
    403: {"model": ErrorEnvelope, "description": "无权限"},
    404: {"model": ErrorEnvelope, "description": "资源不存在"},
    422: {"model": ErrorEnvelope, "description": "参数非法"},
}

router = APIRouter(prefix="/api/v1", tags=["knowledge"], dependencies=[Depends(get_principal)])


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    base = KnowledgeBase(name=payload.name, description=payload.description)
    db.add(base)
    db.flush()
    AuditService(db).record(
        action="knowledge.create",
        actor=principal_actor(principal),
        resource=f"knowledge_base:{base.id}",
        decision="allow",
        reason="创建知识库",
        detail={"name": base.name},
    )
    db.commit()
    db.refresh(base)
    return base


@router.get("/knowledge-bases", response_model=Page[KnowledgeBaseRead], responses=_ERRORS)
def list_knowledge_bases(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(KnowledgeBase)) or 0
    items = db.scalars(
        select(KnowledgeBase)
        .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[KnowledgeBaseRead](items=items, total=total, limit=limit, offset=offset)


@router.delete(
    "/knowledge-bases/{base_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS
)
def delete_knowledge_base(
    base_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    base = get_or_404(db, KnowledgeBase, base_id, "Knowledge base")
    AuditService(db).record(
        action="knowledge.delete",
        actor=principal_actor(principal),
        resource=f"knowledge_base:{base.id}",
        decision="allow",
        reason="删除知识库",
        detail={"name": base.name},
    )
    # 显式清理：SQLite 默认不强制外键，不能指望 ON DELETE CASCADE
    for document in db.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == base.id)
    ).all():
        delete_document(db, document)
    db.delete(base)
    db.commit()
    return None


@router.post(
    "/knowledge-bases/{base_id}/documents",
    response_model=KnowledgeDocumentRead,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def upload_document(
    base_id: str,
    payload: KnowledgeDocumentCreate,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    base = get_or_404(db, KnowledgeBase, base_id, "Knowledge base")
    document = add_document(db, knowledge_base=base, name=payload.name, content=payload.content)
    AuditService(db).record(
        action="knowledge.document.add",
        actor=principal_actor(principal),
        resource=f"knowledge_document:{document.id}",
        decision="allow",
        reason="导入文档并切块",
        detail={"knowledge_base_id": base.id, "chunk_count": document.chunk_count},
    )
    db.commit()
    db.refresh(document)
    return document


@router.get(
    "/knowledge-bases/{base_id}/documents",
    response_model=Page[KnowledgeDocumentRead],
    responses=_ERRORS,
)
def list_documents(
    base_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, KnowledgeBase, base_id, "Knowledge base")
    total = (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == base_id)
        )
        or 0
    )
    items = db.scalars(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.knowledge_base_id == base_id)
        .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[KnowledgeDocumentRead](items=items, total=total, limit=limit, offset=offset)


@router.delete(
    "/knowledge-bases/{base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
def remove_document(
    base_id: str,
    document_id: str,
    principal: Principal = Depends(require_permission(WRITE_AGENTS)),
    db: Session = Depends(get_db),
):
    get_or_404(db, KnowledgeBase, base_id, "Knowledge base")
    document = get_or_404(db, KnowledgeDocument, document_id, "Document")
    if document.knowledge_base_id != base_id:
        get_or_404(db, KnowledgeDocument, "missing", "Document")
    delete_document(db, document)
    AuditService(db).record(
        action="knowledge.document.delete",
        actor=principal_actor(principal),
        resource=f"knowledge_document:{document_id}",
        decision="allow",
        reason="删除文档",
        detail={"knowledge_base_id": base_id},
    )
    db.commit()
    return None


@router.post(
    "/knowledge-bases/{base_id}/search",
    response_model=KnowledgeSearchResponse,
    responses=_ERRORS,
)
def search(
    base_id: str,
    payload: KnowledgeSearchRequest,
    _principal: Principal = Depends(require_permission(READ)),
    db: Session = Depends(get_db),
):
    get_or_404(db, KnowledgeBase, base_id, "Knowledge base")
    hits = search_chunks(
        db,
        knowledge_base_id=base_id,
        query=payload.query,
        limit=min(payload.limit, settings.knowledge_top_k * 4),
    )
    return KnowledgeSearchResponse(
        hits=[KnowledgeSearchHit(**hit.to_dict()) for hit in hits],
        count=len(hits),
    )
