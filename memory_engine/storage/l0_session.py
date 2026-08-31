# memory_engine/storage/l0_session.py
# L0 会话缓存：最近 N 条对话回合的临时上下文（进程内内存，默认 20 条）。
# 职责：
#   - 上下文调用时优先在 L0 查询（按关键词重合度）；
#   - 追加回合时按窗口挤出最早条目（“超时的上下文历史”），由上层决定处置
#     （读写模式：早已按回合归档到 L2，无需再处理；只读模式：直接销毁）；
#   - 进程结束即销毁，不落盘。
from __future__ import annotations

import math
import re
import threading
import time

import memory_engine.config as cfg


def _split_words(text: str) -> list:
    """轻量分词：标点切分 + 2~16 字片段，供 L0 内关键词查询。"""
    parts = re.split(r"[，。！？；、,.!?;:\s“”\"'()（）\[\]【】<>《》\-—_~]+", text or "")
    out = []
    for p in parts:
        p = p.strip()
        if 2 <= len(p) <= 16:
            out.append(p)
    return out


def _bigrams(text: str) -> set:
    """字符 2-gram 集合（对中文近义/换说法鲁棒的相似度基础）。"""
    t = (text or "").strip()
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))}


class L0Session:
    """最近对话回合的临时缓存（进程内，不持久化）。"""

    def __init__(self, max_entries: int = None):
        self.max_entries = max_entries or cfg.L0_MAX_ENTRIES
        self._entries = []          # [{"role","content","ts","meta"}]
        self._lock = threading.RLock()
        self._stats = {"written": 0, "evicted": 0, "queried": 0}

    # ---------------- 写入 ----------------
    def append(self, role: str, content: str, meta: dict = None) -> list:
        """写入一条回合；返回被挤出 L0 窗口的条目列表（超时历史，由上层处置）。"""
        with self._lock:
            self._entries.append({
                "role": role,
                "content": content or "",
                "ts": time.time(),
                "meta": dict(meta or {}),
            })
            self._stats["written"] += 1
            evicted = []
            while len(self._entries) > self.max_entries:
                evicted.append(self._entries.pop(0))
                self._stats["evicted"] += 1
            return evicted

    # ---------------- 读取 ----------------
    def get_recent(self, n: int = None) -> list:
        """最近 n 条回合（按时间顺序，供 LLM 上下文组装）。"""
        with self._lock:
            n = n or self.max_entries
            return [dict(e) for e in self._entries[-n:]]

    def query_scored(self, text: str, limit: int = 5) -> list:
        """L0 内查询：返回 [(加权重合分, 条目)]，按字符 2-gram 的 IDF 加权重合度排序。

        对换说法鲁棒；泛用 2-gram（今天/什么/天有…在窗口内出现越多权重越低），
        实质内容词（奶茶/安排/周三…）权重更高——避免“昨天有什么新闻吗”仅因
        与“今天有什么安排吗”共享几个泛用词就被误判为上下文命中。
        """
        with self._lock:
            self._stats["queried"] += 1
            words = _split_words(text)
            if not words:
                return []
            qgrams = set()
            for w in words:
                qgrams |= _bigrams(w)
            if not qgrams:
                return []
            entries = self._entries
            n = len(entries) or 1
            grams_by = [_bigrams(e.get("content") or "") for e in entries]
            df = {g: 0 for g in qgrams}
            for gs in grams_by:
                for g in (gs & qgrams):
                    df[g] += 1
            idf = {g: math.log((n + 1) / (df[g] + 1)) + 1.0 for g in qgrams}
            scored = []
            for e, gs in zip(entries, grams_by):
                w = sum(idf[g] for g in (gs & qgrams))
                if w > 0:
                    scored.append((w, dict(e)))
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[:limit]

    def query(self, text: str, limit: int = 5) -> list:
        """L0 内查询：与输入关键词重合度排序（优先本地命中）。"""
        hits = [e for _, e in self.query_scored(text, limit)]
        if hits:
            try:
                from memory_engine import logging as me_log
                me_log.debug(f"[L0] 查询命中 {len(hits)} 条（来源L0）: "
                             + "；".join((h.get('content') or '')[:20] for h in hits))
            except Exception:
                pass
        return hits

    def all(self) -> list:
        with self._lock:
            return [dict(e) for e in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
