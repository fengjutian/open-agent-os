"""文本切块。

原则：**按语义边界切，切不动了才按长度硬切**。段落 → 句子 → 硬切。
块之间保留少量重叠，避免答案正好落在切口两侧时检索不到。
"""

from __future__ import annotations

import re

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[。！？!?；;\n])")


def _split_sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE.split(text) if part.strip()]


def chunk_text(text: str, *, size: int = 800, overlap: int = 100) -> list[str]:
    """把长文本切成不超过 `size` 字符的块，块间重叠 `overlap` 字符。"""
    size = max(100, size)
    overlap = max(0, min(overlap, size // 2))

    normalized = (text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= size:
        return [normalized]

    # 先按段落粗分，再把过长的段落按句子拆，句子还长就硬切
    units: list[str] = []
    for paragraph in _PARAGRAPH.split(normalized):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            units.append(paragraph)
            continue
        for sentence in _split_sentences(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= size:
                units.append(sentence)
            else:
                for start in range(0, len(sentence), size):
                    units.append(sentence[start : start + size])

    chunks: list[str] = []
    buffer = ""
    for unit in units:
        if not buffer:
            buffer = unit
            continue
        if len(buffer) + 1 + len(unit) <= size:
            buffer = f"{buffer}\n{unit}"
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap and len(buffer) > overlap else ""
            buffer = f"{tail}\n{unit}" if tail else unit
    if buffer.strip():
        chunks.append(buffer)
    return chunks
