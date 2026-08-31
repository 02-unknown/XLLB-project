# memory_engine/storage/l2_vector.py
# L2 向量检索层：与 SQLite 通过 id 强关联。
# 默认实现为内置 numpy 平面向量索引（零外部依赖、支持 id 预过滤）；
# 预留 LanceDB 后端接口（lancedb 可用时自动切换，无需改动上层代码）。
from __future__ import annotations

import threading

try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    _HAS_NUMPY = False

import memory_engine.config as cfg
from memory_engine.embedding import cosine

try:
    import lancedb  # noqa: F401
    _HAS_LANCEDB = True
except Exception:
    _HAS_LANCEDB = False


class VectorIndex:
    """向量索引统一接口：add / remove / search(vec, id_filter, top_k) -> [(id, score)]。

    支持从多个 SQLite 库加载向量（活跃库 + 归档库），实现跨年度检索的向量预过滤；
    新增写入默认落在主库（第一个）。
    """

    def __init__(self, sqlite_indexes, dim: int = None, memory_max: int = None):
        self.dim = dim or cfg.VECTOR_DIM
        if isinstance(sqlite_indexes, (list, tuple)):
            self.indexes = list(sqlite_indexes)
        else:
            self.indexes = [sqlite_indexes]
        self.sqlite = self.indexes[0]   # 主库（写入）
        self.memory_max = memory_max if memory_max is not None else cfg.VECTOR_MEMORY_MAX
        self._lock = threading.RLock()
        self._vecs = {}          # id -> np.ndarray（LRU 有界缓存，超出上限淘汰最久未用）
        self._loaded = False
        self._backend = "numpy-flat"

    # ---------------- 初始化 / 加载 ----------------
    def load(self):
        """启动时预载主库（活跃）向量到内存（受 memory_max 上限约束）；归档向量惰性加载。"""
        if self._loaded:
            return
        with self._lock:
            if _HAS_LANCEDB:
                self._backend = "lancedb(预留)"
                self._loaded = True
                return
            blobs = self.indexes[0].get_vectors()
            for fid, blob in blobs.items():
                self._put(fid, self._blob_to_arr(blob))
            self._loaded = True

    @staticmethod
    def _blob_to_arr(blob):
        arr = np.frombuffer(blob, dtype=np.float32)
        return arr

    def _put(self, fid, arr):
        """写入 LRU 缓存（最新放末尾，超出上限淘汰最旧）。"""
        if arr is None or arr.size < self.dim:
            return
        arr = arr[:self.dim]
        if fid in self._vecs:
            self._vecs.pop(fid)
        self._vecs[fid] = arr
        while self.memory_max and len(self._vecs) > self.memory_max:
            self._vecs.pop(next(iter(self._vecs)))

    # ---------------- 写入 ----------------
    def add(self, fragment_id: str, vec) -> None:
        with self._lock:
            if _HAS_NUMPY:
                arr = np.asarray(vec, dtype=np.float32).reshape(-1)[:self.dim]
                if arr.size < self.dim:
                    pad = np.zeros(self.dim - arr.size, dtype=np.float32)
                    arr = np.concatenate([arr, pad])
            else:  # pragma: no cover
                arr = list(vec)
            self._put(fragment_id, arr)
            try:
                self.sqlite.insert_vector(fragment_id, np.asarray(arr, dtype=np.float32).tobytes())
            except Exception:
                pass

    def remove(self, ids) -> None:
        with self._lock:
            for fid in ids:
                self._vecs.pop(fid, None)

    def _load_missing(self, ids):
        """把不在内存中的候选向量从各库批量惰性加载进 LRU 缓存。"""
        missing = [k for k in ids if k not in self._vecs]
        if not missing:
            return
        for idx in self.indexes:
            try:
                blobs = idx.get_vectors(missing)
            except Exception:
                continue
            for fid, blob in blobs.items():
                try:
                    self._put(fid, self._blob_to_arr(blob))
                except Exception:
                    pass

    def get_vector(self, fragment_id: str):
        """取单个向量（整理去重时用于相似度比较）。"""
        with self._lock:
            return self._vecs.get(fragment_id)

    def clear(self) -> None:
        with self._lock:
            self._vecs.clear()
            try:
                self.sqlite.conn.execute("DELETE FROM vectors")
                self.sqlite.conn.commit()
            except Exception:
                pass

    # ---------------- 检索 ----------------
    def search(self, vec, id_filter=None, top_k: int = cfg.TOP_K) -> list:
        """向量点积检索（预过滤限定候选集，缺失向量惰性加载）。返回 [(id, score)]，score∈[0,1]。"""
        with self._lock:
            if id_filter is not None:
                keys = [k for k in id_filter if k in self._vecs]
                if len(keys) < len(id_filter):
                    self._load_missing(id_filter)
                    keys = [k for k in id_filter if k in self._vecs]
            else:
                keys = list(self._vecs.keys())
            if not keys:
                return []
            # 最近使用标记：把本次命中的 id 移到 LRU 末尾
            for k in keys:
                self._vecs[k] = self._vecs.pop(k)
            if _HAS_NUMPY:
                matrix = np.stack([self._vecs[k] for k in keys]).astype(np.float32)
                q = np.asarray(vec, dtype=np.float32).reshape(-1)[:self.dim]
                qn = np.linalg.norm(q)
                if qn > 1e-9:
                    q = q / qn
                scores = matrix @ q
                scores = np.clip(scores, 0.0, 1.0)
                order = np.argsort(-scores)[:top_k]
                return [(keys[i], float(scores[i])) for i in order]
            # 纯 Python 兜底
            scored = [(k, max(0.0, min(1.0, cosine(vec, self._vecs[k])))) for k in keys]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def size(self) -> int:
        with self._lock:
            return len(self._vecs)

    def backend(self) -> str:
        return self._backend
