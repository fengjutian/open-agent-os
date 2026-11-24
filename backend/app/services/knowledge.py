"""知识库：建库、导入文档、切块入库、检索。

检索是**全量扫描 + 余弦相似度**（仅限当前库，且受 `knowledge_max_scan_chunks`
限制）。这在小规模知识库上完全够用；要上规模就得换成向量索引（pgvector 或独立
向量库），届时只要替换 `search_chunks` 的实现，接口与工具不变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.models.entities import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.services.chunking import chunk_text
from app.services.embeddings import (
    cosine,
    embedding_name,
    get_embedding_provider,
    lexical_features,
)

logger = logging.getLogger(__name__)

KNOWLEDGE_TOOL_NAME = "knowledge_search"


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    document_id: str
    document_name: str
    seq: int
    text: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document_name,
            "seq": self.seq,
            "score": round(self.score, 4),
            "text": self.text,
        }


def add_document(
    db: Session,
    *,
    knowledge_base: KnowledgeBase,
    name: str,
    content: str,
) -> KnowledgeDocument:
    chunks = chunk_text(
        content,
        size=settings.knowledge_chunk_size,
        overlap=settings.knowledge_chunk_overlap,
    )
    if not chunks:
        raise AppError(ErrorCode.VALIDATION_ERROR, "文档内容为空，无法切块", status_code=422)

    document = KnowledgeDocument(
        knowledge_base_id=knowledge_base.id,
        name=name,
        content=content,
        chunk_count=len(chunks),
    )
    db.add(document)
    db.flush()

    provider = get_embedding_provider()
    vectors = provider.embed(chunks)
    for index, (text, vector) in enumerate(zip(chunks, vectors, strict=False)):
        db.add(
            KnowledgeChunk(
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                seq=index,
                text=text,
                embedding=vector,
            )
        )

    knowledge_base.embedding_model = provider.name
    knowledge_base.chunk_count = (knowledge_base.chunk_count or 0) + len(chunks)
    db.flush()
    return document


def search_chunks(
    db: Session,
    *,
    knowledge_base_id: str,
    query: str,
    limit: int | None = None,
) -> list[RetrievalHit]:
    query = (query or "").strip()
    if not query:
        return []

    top_k = max(1, min(limit or settings.knowledge_top_k, 20))
    rows = db.scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeChunk.document_id, KnowledgeChunk.seq)
        .limit(settings.knowledge_max_scan_chunks)
    ).all()
    if not rows:
        return []

    query_vector = get_embedding_provider().embed([query])[0]
    query_features = lexical_features(query)
    document_names = _document_names(db, {row.document_id for row in rows})

    scored: list[RetrievalHit] = []
    for row in rows:
        chunk_features = lexical_features(row.text)

        if settings.knowledge_ranking == "vector":
            # 语义/向量排序：仍要求至少共享一个词面特征，避免哈希噪声硬凑
            if query_features and not query_features & chunk_features:
                continue
            value = cosine(query_vector, list(row.embedding or []))
        else:
            # 词面覆盖率排序：命中比例越高越相关，可解释、不受向量维度噪声影响。
            # 这是默认值——本地哈希向量本质上就是词面特征，直接算覆盖率更准。
            if not chunk_features:
                continue
            overlap = len(query_features & chunk_features)
            if overlap == 0:
                continue
            value = overlap / max(1, len(query_features))

        scored.append(
            RetrievalHit(
                chunk_id=row.id,
                document_id=row.document_id,
                document_name=document_names.get(row.document_id, "未命名文档"),
                seq=row.seq,
                text=row.text,
                score=value,
            )
        )
    scored.sort(key=lambda hit: hit.score, reverse=True)
    # 分数为 0 说明一个字面特征都没命中，返回它们只是噪音
    return [hit for hit in scored[:top_k] if hit.score > 0]


def _document_names(db: Session, document_ids: set[str]) -> dict[str, str]:
    if not document_ids:
        return {}
    rows = db.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.name).where(
            KnowledgeDocument.id.in_(document_ids)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def delete_document(db: Session, document: KnowledgeDocument) -> None:
    db.query(KnowledgeChunk).filter(
        KnowledgeChunk.document_id == document.id
    ).delete(synchronize_session=False)
    knowledge_base = db.get(KnowledgeBase, document.knowledge_base_id)
    if knowledge_base is not None:
        knowledge_base.chunk_count = max(
            0, (knowledge_base.chunk_count or 0) - (document.chunk_count or 0)
        )
    db.delete(document)


def base_stats(db: Session, knowledge_base_id: str) -> tuple[int, int]:
    """返回 (文档数, 块数)。用于列表展示。"""
    documents = (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == knowledge_base_id)
        )
        or 0
    )
    chunks = (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_base_id == knowledge_base_id)
        )
        or 0
    )
    return documents, chunks


def embedding_label() -> str:
    return embedding_name()
