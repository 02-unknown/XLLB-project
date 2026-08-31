# memory_engine/ingest.py
# 数据写入与入库流程：分级预处理 → 多角色事实拆解 → 双路检索数据生成 → 分层物理落盘。
from __future__ import annotations

import memory_engine.config as cfg
from memory_engine import preprocessing
from memory_engine import roles
from memory_engine.embedding import embed
from memory_engine.models import IngestResult


def _normalize_char(raw_text, participants, facts_per_role, full_summary, char_name):
    """写入侧角色名占位符化：把当前角色名替换为 {char}，长记忆不绑定具体名字。"""
    char_name = roles.resolve_char_name(char_name)
    if not char_name:
        return raw_text, participants, facts_per_role, full_summary
    text = roles.to_stored_text(raw_text, char_name)
    parts = [roles.to_stored_text(p, char_name) for p in (participants or [])]
    facts = {}
    for r, fd in (facts_per_role or {}).items():
        d = fd.to_dict() if hasattr(fd, "to_dict") else dict(fd or {})
        facts[roles.to_stored_text(r, char_name)] = {
            k: roles.to_stored_text(str(v), char_name) for k, v in d.items()
        }
    summary = roles.to_stored_text(full_summary, char_name) if full_summary else full_summary
    return text, parts, facts, summary


def ingest(
    engine,
    raw_text: str,
    fragment_id: str = None,
    ts: float = None,
    year: int = None,
    quarter: str = None,
    main_topic: str = None,
    sub_topic: str = None,
    participants: list = None,
    facts_per_role: dict = None,
    full_summary: str = None,
    chunks: bool = True,
    char_name: str = None,
) -> IngestResult:
    """入库一条（或一段超长文本切成的多条）记忆。

    返回 IngestResult（多片段时取首片段概要）。接口参数与 preprocessing.make_fragment 对齐，
    供上层流水线直接调用；facts_per_role 支持 {角色: {"action":..., "result":..., "stance":...}}。
    char_name：当前角色名；传入后写入侧把该名字替换为占位符 {char}（角色预设可自由改名）。
    """
    if not raw_text or not str(raw_text).strip():
        return IngestResult(ok=False, message="内容为空")

    raw_text, participants, facts_per_role, full_summary = _normalize_char(
        raw_text, participants, facts_per_role, full_summary, char_name)

    frags = preprocessing.split_fragments(raw_text, fragment_id=fragment_id, ts=ts, year=year,
                                          quarter=quarter, main_topic=main_topic, sub_topic=sub_topic,
                                          participants=participants, facts_per_role=facts_per_role,
                                          full_summary=full_summary) if chunks else [
        preprocessing.make_fragment(raw_text, fragment_id=fragment_id, ts=ts, year=year,
                                    quarter=quarter, main_topic=main_topic, sub_topic=sub_topic,
                                    participants=participants, facts_per_role=facts_per_role,
                                    full_summary=full_summary)
    ]

    first = None
    for f in frags:
        # L3 冷存储：原始完整长文本按 年/季度/主题 归档（仅 id + full_text 映射）
        l3_ref = engine.cold.write(f)
        # L2 索引持久层：主表 + FTS5（searchable_text）+ 向量（semantic_context）
        engine.active.insert(f)
        engine.vector.add(f.id, embed(f.semantic_context))
        if first is None:
            first = IngestResult(
                fragment_id=f.id, year=f.year, quarter=f.quarter, main_topic=f.main_topic,
                participants=f.participants, l3_ref=l3_ref, ok=True, message=f"已入库 {len(frags)} 条",
            )
    return first or IngestResult(ok=False, message="入库失败")


def delete(engine, fragment_id: str) -> bool:
    """按 id 删除一条记忆（含索引与向量）。"""
    engine.active.delete([fragment_id])
    engine.vector.remove([fragment_id])
    return True
