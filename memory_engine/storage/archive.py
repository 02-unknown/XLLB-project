# memory_engine/storage/archive.py
# 季度滚动归档：把「当前季度之前」的 L2 数据（SQLite 行 + 向量 + FTS 行）从活跃主库
# 迁移至归档历史库，保证活跃库始终维持在轻量级规模。
from __future__ import annotations

import json

import memory_engine.config as cfg
from memory_engine import time_utils
from memory_engine.storage.l2_sqlite import SqliteIndex


def run_quarterly_archive(active: SqliteIndex, archive: SqliteIndex, now: float = None) -> dict:
    """把早于当前季度的数据从 active 迁到 archive。返回 {moved, active_count, archive_count}。"""
    now = now if now is not None else time_utils.now_ts()
    cy, cq = time_utils.year_quarter(now)

    ids = active.ids_before(cy, cq)
    moved = 0
    if ids:
        rows = active.get_rows(ids)
        vec_blobs = active.get_vectors(ids)
        for row in rows:
            archive.insert(_row_to_fragment(row))
        for fid, blob in vec_blobs.items():
            archive.insert_vector(fid, blob)
        moved = len(ids)
        active.delete(ids)
    # 归档库也重建一次 FTS，保持一致性
    try:
        archive.rebuild_fts()
    except Exception:
        pass
    return {
        "moved": moved,
        "active_count": active.count(),
        "archive_count": archive.count(),
        "as_of": f"{cy}/{cq}",
    }


def _row_to_fragment(row):
    from memory_engine.models import MemoryFragment
    facts = {}
    for role, fd in json.loads(row["facts_per_role"] or "{}").items():
        from memory_engine.models import RoleFact
        facts[role] = RoleFact.from_dict(fd)
    return MemoryFragment(
        id=row["id"],
        year=row["year"],
        quarter=row["quarter"],
        ts=row["ts"],
        main_topic=row["topic"],
        sub_topic=row["sub_topic"],
        participants=json.loads(row["participants"] or "[]"),
        facts_per_role=facts,
        searchable_text=row["searchable_text"],
        semantic_context=row["semantic_context"],
        full_summary=row["full_summary"],
    )
