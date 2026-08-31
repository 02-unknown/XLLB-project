# memory_engine/storage/l1_cache.py
# L1 热缓存：驻留内存的 LRU + TTL（默认 30 分钟）。
# 键为「输入哈希」，值为完整检索结果（含 facts_per_role），命中时直接短路全部检索。
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

import memory_engine.config as cfg


def cache_key(user_input: str, top_k: int, mode: str = "active", include_raw: bool = False,
              role: str = "", extra_query: str = "") -> str:
    raw = f"{user_input}|{top_k}|{mode}|{include_raw}|{role}|{extra_query}"
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()


class L1Cache:
    def __init__(self, ttl: float = None, max_items: int = None):
        self.ttl = ttl if ttl is not None else cfg.CACHE_TTL
        self.max_items = max_items or cfg.CACHE_MAX_ITEMS
        self._data = OrderedDict()          # key -> (expire_ts, value)
        self._stats = {"hit": 0, "miss": 0, "evict": 0}

    def get(self, key: str):
        now = time.time()
        item = self._data.get(key)
        if item is None:
            self._stats["miss"] += 1
            return None
        expire_ts, value = item
        if expire_ts < now:
            self._data.pop(key, None)
            self._stats["miss"] += 1
            return None
        # LRU：移到末尾
        self._data.move_to_end(key)
        self._stats["hit"] += 1
        return value

    def set(self, key: str, value):
        now = time.time()
        if key in self._data:
            self._data.pop(key)
        self._data[key] = (now + self.ttl, value)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)
            self._stats["evict"] += 1

    def delete(self, key: str):
        self._data.pop(key, None)

    def clear(self):
        self._data.clear()

    def size(self) -> int:
        return len(self._data)

    def stats(self) -> dict:
        return dict(self._stats)
