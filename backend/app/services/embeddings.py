"""向量化。

**默认实现是本地确定性哈希向量，不是语义模型。** 理由：接真 embedding 需要模型
下载或云 API，对自托管是额外依赖；但"接口先定下来"比"先接一个模型"更重要——
上层（切块、检索、工具）完全不需要知道用的是哪种向量。

`local-hash-v1` 的做法：中文按字符二元组、英文按词做特征，用 SHA-256 把特征
映射到固定维度并累加，最后 L2 归一化。它是**词面**相似度，能把"预算 超支"和
"预算 控制"拉到一起，但不懂同义改写——这正是需要换成真模型的地方。

换模型的成本：实现一个 `EmbeddingProvider`（`embed` + `dimension`），在
`get_embedding_provider()` 里按配置返回即可；**已有向量的维度必须一致**，
所以换实现时要重建索引（`knowledge_bases.embedding_model` 记录了当时用的是哪个）。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.core.config import settings

DIMENSION = 1024

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]{2,}")


def lexical_features(text: str) -> set[str]:
    """词面特征（中文二元组 + 英文单词），用于检索前的硬门控。

    哈希向量在短文本上会有可观的随机相似度（实测无关查询也能到 0.14），
    单靠余弦阈值会把噪音当成命中。既然默认向量本身就是词面的，
    就再要求"至少共享一个词面特征"，把明显无关的结果直接拒掉。
    """
    lowered = (text or "").lower()
    chars = _CJK.findall(lowered)
    bigrams = {f"{chars[index]}{chars[index + 1]}" for index in range(len(chars) - 1)}
    return bigrams | set(_WORD.findall(lowered))


class EmbeddingProvider(Protocol):
    name: str

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _features(text: str) -> dict[str, float]:
    lowered = (text or "").lower()
    features: dict[str, float] = {}

    chars = _CJK.findall(lowered)
    for index in range(len(chars) - 1):
        bigram = f"{chars[index]}{chars[index + 1]}"
        features[bigram] = features.get(bigram, 0.0) + 1.0
    for char in chars:
        features[char] = features.get(char, 0.0) + 0.3
    for word in _WORD.findall(lowered):
        features[word] = features.get(word, 0.0) + 1.5
    return features


def _bucket(token: str) -> int:
    # 必须用稳定哈希：Python 内置 hash() 每个进程加盐，重启后向量就变了
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % DIMENSION


class LocalHashEmbedding:
    """确定性、无外部依赖的词袋哈希向量。"""

    name = "local-hash-v1"

    @property
    def dimension(self) -> int:
        return DIMENSION

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * DIMENSION
            for token, weight in _features(text).items():
                vector[_bucket(token)] += weight
            vectors.append(_normalize(vector))
        return vectors


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=False))


def get_embedding_provider() -> EmbeddingProvider:
    # 目前只有一种实现；接入真实模型时在这里按 settings 分支
    return LocalHashEmbedding()


def embedding_name() -> str:
    return get_embedding_provider().name


def max_chunks_per_query() -> int:
    return settings.knowledge_max_scan_chunks
