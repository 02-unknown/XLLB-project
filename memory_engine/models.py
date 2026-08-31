# memory_engine/models.py
# 数据模型与输出契约（纯数据结构，不涉及存储与检索逻辑）。
from __future__ import annotations

from typing import Any, Optional


class RoleFact:
    """单个角色在该事件中的「动作 / 结果 / 立场」事实。"""

    __slots__ = ("action", "result", "stance", "raw")

    def __init__(self, action: str = "", result: str = "", stance: str = "", raw: str = ""):
        self.action = action or ""
        self.result = result or ""
        self.stance = stance or ""
        self.raw = raw or ""

    def to_dict(self) -> dict:
        d = {}
        if self.action:
            d["action"] = self.action
        if self.result:
            d["result"] = self.result
        if self.stance:
            d["stance"] = self.stance
        if self.raw:
            d["raw"] = self.raw
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RoleFact":
        d = d or {}
        return cls(
            action=str(d.get("action", "") or ""),
            result=str(d.get("result", "") or ""),
            stance=str(d.get("stance", "") or ""),
            raw=str(d.get("raw", "") or ""),
        )

    def __repr__(self):
        return f"RoleFact({self.to_dict()})"


class MemoryFragment:
    """一条入库的记忆片段（三级物理存储共用同一逻辑模型）。"""

    __slots__ = (
        "id", "year", "quarter", "ts", "main_topic", "sub_topic",
        "participants", "facts_per_role", "searchable_text",
        "semantic_context", "full_summary", "raw_text",
    )

    def __init__(
        self,
        id: str,
        year: int,
        quarter: str,
        ts: float,
        main_topic: str = "综合",
        sub_topic: str = "",
        participants: Optional[list] = None,
        facts_per_role: Optional[dict] = None,
        searchable_text: str = "",
        semantic_context: str = "",
        full_summary: str = "",
        raw_text: str = "",
    ):
        self.id = id
        self.year = int(year)
        self.quarter = quarter if quarter and str(quarter).upper().startswith("Q") else f"Q{((int(ts) // (91 * 86400)) % 4) + 1}"
        self.ts = float(ts)
        self.main_topic = main_topic or "综合"
        self.sub_topic = sub_topic or ""
        self.participants = list(participants or [])
        self.facts_per_role = facts_per_role or {}
        self.searchable_text = searchable_text
        self.semantic_context = semantic_context
        self.full_summary = full_summary
        self.raw_text = raw_text

    def facts_dict(self) -> dict:
        """facts_per_role 的 JSON 安全形式（RoleFact → dict）。"""
        out = {}
        for role, fact in (self.facts_per_role or {}).items():
            out[role] = fact.to_dict() if isinstance(fact, RoleFact) else dict(fact or {})
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "year": self.year,
            "quarter": self.quarter,
            "ts": self.ts,
            "main_topic": self.main_topic,
            "sub_topic": self.sub_topic,
            "participants": list(self.participants),
            "facts_per_role": self.facts_dict(),
            "searchable_text": self.searchable_text,
            "semantic_context": self.semantic_context,
            "full_summary": self.full_summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryFragment":
        import time as _time
        from datetime import datetime as _dt
        _cur = _dt.fromtimestamp(_time.time()).year
        facts = {}
        for role, fd in (d.get("facts_per_role") or {}).items():
            facts[role] = RoleFact.from_dict(fd)
        return cls(
            id=str(d.get("id")),
            year=int(d.get("year", _cur)),      # 缺省年份用当前年，不写死
            quarter=str(d.get("quarter", "Q1")),
            ts=float(d.get("ts", 0.0)),
            main_topic=str(d.get("main_topic", "综合")),
            sub_topic=str(d.get("sub_topic", "") or ""),
            participants=list(d.get("participants") or []),
            facts_per_role=facts,
            searchable_text=str(d.get("searchable_text", "") or ""),
            semantic_context=str(d.get("semantic_context", "") or ""),
            full_summary=str(d.get("full_summary", "") or ""),
            raw_text=str(d.get("raw_text", "") or ""),
        )


class RetrievedMemory:
    """检索输出契约：上层「接话」流水线的最终交付物。

    只包含记忆本身与置信度，不含发言顺序 / 发言者判断。
    """

    __slots__ = (
        "id", "full_summary", "participants", "facts_per_role",
        "base_score", "confidence", "route", "candidates", "raw_text",
    )

    def __init__(
        self,
        id: str,
        full_summary: str = "",
        participants: Optional[list] = None,
        facts_per_role: Optional[dict] = None,
        base_score: float = 0.0,
        confidence: float = 0.0,
        route: str = "none",
        candidates: Optional[list] = None,
        raw_text: str = "",
    ):
        self.id = id
        self.full_summary = full_summary
        self.participants = list(participants or [])
        self.facts_per_role = facts_per_role or {}
        self.base_score = float(base_score)
        self.confidence = float(confidence)
        self.route = route          # l1 | direct | llm_recheck | none
        self.candidates = list(candidates or [])
        self.raw_text = raw_text

    def to_dict(self) -> dict:
        facts = {}
        for role, fd in (self.facts_per_role or {}).items():
            facts[role] = fd.to_dict() if isinstance(fd, RoleFact) else dict(fd or {})
        return {
            "id": self.id,
            "full_summary": self.full_summary,
            "participants": list(self.participants),
            "facts_per_role": facts,
            "base_score": round(self.base_score, 4),
            "confidence": round(self.confidence, 4),
            "route": self.route,
            "candidates": self.candidates,
            "raw_text": self.raw_text,
        }


class IngestResult:
    __slots__ = ("fragment_id", "year", "quarter", "main_topic", "participants", "l3_ref", "ok", "message")

    def __init__(self, fragment_id="", year=0, quarter="", main_topic="", participants=None,
                 l3_ref="", ok=True, message=""):
        self.fragment_id = fragment_id
        self.year = year
        self.quarter = quarter
        self.main_topic = main_topic
        self.participants = list(participants or [])
        self.l3_ref = l3_ref
        self.ok = ok
        self.message = message

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fragment_id": self.fragment_id,
            "year": self.year,
            "quarter": self.quarter,
            "main_topic": self.main_topic,
            "participants": self.participants,
            "l3_ref": self.l3_ref,
            "message": self.message,
        }
