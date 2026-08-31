# memory_engine/embedding.py
# 向量化模块：默认使用本地 n-gram 哈希向量（确定性、零依赖、毫秒级），
# 并预留 set_embedder() 接口，后续可无缝替换为 Ollama / 外部 API 的真实嵌入模型。
from __future__ import annotations

import hashlib

try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    _HAS_NUMPY = False

import memory_engine.config as cfg


def _stable_hash(text: str) -> int:
    """进程无关的稳定哈希（内置 hash() 对 str 每次进程随机，不可用）。"""
    return int.from_bytes(hashlib.md5(text.encode("utf-8", "ignore")).digest()[:8], "big")


def _local_embed(text: str):
    """字符 n-gram 哈希向量（2/3/4-gram，非负加权累加，L2 归一化）。

    设计要点（聊天场景短查询）：
      - 非负累加（不取 ± 符号）：带符号哈希在高维稀疏下，长片段里共享 n-gram 所在
        维度极易被其它 n-gram 抵消为 0，导致「今天」这类共享词余弦≈0；
        非负计数保证共享 n-gram 一定贡献正值，短查询与片段间的相似度稳定可判。
      - 按 n-gram 长度加权（2-gram×1.0 / 3-gram×1.5 / 4-gram×2.0）：
        更长的 n-gram 更具区分度，突出实质性匹配词。
    维度取自 config.VECTOR_DIM（默认 512）。
    """
    dim = cfg.VECTOR_DIM
    if _HAS_NUMPY:
        vec = np.zeros(dim, dtype=np.float32)
    else:  # pragma: no cover
        vec = [0.0] * dim
    norm_text = (text or "").strip().lower()
    if not norm_text:
        return vec
    weights = {2: 1.0, 3: 1.5, 4: 2.0}
    for n in (2, 3, 4):
        w = weights[n]
        for i in range(max(0, len(norm_text) - n + 1)):
            h = _stable_hash(norm_text[i:i + n])
            idx = h % dim
            if _HAS_NUMPY:
                vec[idx] += w
            else:  # pragma: no cover
                vec[idx] += w
    if _HAS_NUMPY:
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec = vec / norm
    else:  # pragma: no cover
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 1e-9:
            vec = [v / norm for v in vec]
    return vec


_embedder = _local_embed


def set_embedder(fn):
    """替换向量化实现（预留）：fn(text) -> 向量（任意支持点积的对象）。"""
    global _embedder
    _embedder = fn


def embed(text: str):
    return _embedder(text)


def cosine(a, b) -> float:
    """向量点积相似度（向量均已归一化时即余弦）。"""
    try:
        if _HAS_NUMPY and hasattr(a, "shape"):
            return float(np.dot(a, b))
        return float(sum(x * y for x, y in zip(a, b)))
    except Exception:
        return 0.0
