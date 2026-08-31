# memory_engine/governance.py
# 存储治理与生命周期维护：防止记录无限消耗存储空间。
#
#   usage()          各层空间统计（条数 / 磁盘占用 / 最旧最新）
#   enforce_quotas() 配额管理：活跃库超限→最旧迁归档；归档超限→最旧连同 L3 原文删除；
#                    L3 原文超过保留期→删除原文文件（保留索引摘要）
#   tidy()           整理：组内近似重复检测（向量余弦），合并 facts_per_role 与参与者
#   classify()       分类：按 主题 / 季度 / 参与者 重建分类索引（预留接口实现）
from __future__ import annotations

import os
import time

import memory_engine.config as cfg
from memory_engine import time_utils
from memory_engine.embedding import cosine
from memory_engine.models import MemoryFragment, RoleFact


# ==================== 空间统计 ====================
def _dir_size(path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _oldest_ts(db) -> float:
    rows = db.oldest_ids(1)
    if not rows:
        return 0.0
    r = db.get(rows[0])
    return (r or {}).get("ts", 0.0)


def usage(engine) -> dict:
    """各层空间统计（供 /memory usage 与审计）。"""
    active = engine.active
    archive = engine.archive_db
    cold = engine.cold
    return {
        "active_count": active.count(),
        "archive_count": archive.count(),
        "vector_count": engine.vector.size(),
        "cache_size": engine.l1.size(),
        "active_db_bytes": os.path.getsize(active.path) if os.path.isfile(active.path) else 0,
        "archive_db_bytes": os.path.getsize(archive.path) if os.path.isfile(archive.path) else 0,
        "cold_bytes": _dir_size(cold.root),
        "total_bytes": sum(
            os.path.getsize(p) if os.path.isfile(p) else 0
            for p in (active.path, archive.path)
        ) + _dir_size(cold.root),
        "oldest_ts": _oldest_ts(active) or _oldest_ts(archive),
        "l3_partitions": len(cold.partitions()),
    }


# ==================== 配额与降级治理 ====================
def _move_to_archive(engine, ids) -> int:
    """把 id 从活跃库迁到归档库（行 + 向量 + FTS，原文留在 L3 不动）。"""
    rows = engine.active.get_rows(ids)
    vecs = engine.active.get_vectors(ids)
    for row in rows:
        engine.archive_db.insert(_row_to_fragment(row))
    for fid, blob in vecs.items():
        engine.archive_db.insert_vector(fid, blob)
    engine.active.delete(ids)
    return len(ids)


def _quarter_age(now_year, now_quarter, y, q) -> int:
    """记录距今的季度数（用于判断是否触发聚合 / 压缩）。"""
    age = 0
    yy, qq = now_year, now_quarter
    while (yy, qq) != (y, q):
        yy, qq = time_utils.prev_quarter(yy, qq)
        age += 1
        if age > 400:
            break
    return age


def _compact_text(text, keep_ratio=None) -> str:
    """原文压缩为要点版（保留前 keep_ratio 比例 + 压缩标记），不删除。"""
    keep_ratio = keep_ratio if keep_ratio is not None else cfg.RAW_COMPACT_KEEP
    text = (text or "").strip()
    if len(text) <= 200:
        return text
    keep = max(60, int(len(text) * keep_ratio))
    return text[:keep].rstrip("，。； \n") + "……（原文已压缩，要点保留）"


def _aggregate_group(engine, db, rows, level: str, label=None):
    """把同一组多条记录降级为一条聚合片段（quarter=季度聚合 / year=年度聚合）。

    信息不删除：参与者取并集、facts_per_role 按角色拼接、各条原文压缩后并入聚合原文。
    label 可选 (year, quarter, topic) 覆盖聚合片段的标识（用于跨主题/跨季度合并）。
    """
    from memory_engine.embedding import embed
    from memory_engine.preprocessing import build_searchable_text, build_semantic_context

    year = rows[0]["year"]
    quarter = rows[0]["quarter"]
    topic = rows[0]["topic"]
    if label:
        year, quarter = label[0], label[1]
        if label[2]:
            topic = label[2]
    sub = next((r.get("sub_topic") for r in rows if r.get("sub_topic")), "")

    agg_id = f"agg_{year}_{quarter}_{topic}" if level == "quarter" else f"agg_{year}_{topic}"

    # 参与者并集
    parts = []
    for r in rows:
        for p in r.get("participants") or []:
            if p not in parts:
                parts.append(p)

    # 事实合并（同角色 action/result/stance 拼接去重）
    facts = {}
    for r in rows:
        for role, fd in (r.get("facts_per_role") or {}).items():
            f = facts.setdefault(role, RoleFact())
            d = fd if isinstance(fd, dict) else ({"raw": str(fd)} if fd else {})
            for key in ("action", "result", "stance", "raw"):
                v = str(d.get(key, "") or "").strip()
                if not v:
                    continue
                cur = getattr(f, key) or ""
                if v not in cur:
                    setattr(f, key, (cur + "；" + v) if cur else v)

    # 摘要（聚合标记 + 各条摘要拼接）
    tag = "【季度聚合】" if level == "quarter" else "【年度聚合】"
    summaries = [r.get("full_summary", "") for r in rows if r.get("full_summary")]
    summary = tag + "；".join(summaries)
    if len(summary) > cfg.SEMANTIC_CORE_MAX:
        summary = summary[:cfg.SEMANTIC_CORE_MAX].rstrip("，；。 ")

    searchable = build_searchable_text(topic, sub, parts, facts, summary)
    context = build_semantic_context(summary, facts, topic, sub)

    # 各条原文压缩后拼接为聚合原文（要点级，长期保留）
    raw_parts = []
    for r in rows:
        raw = engine.cold.read_by_id(r["year"], r["quarter"], r["topic"], r["id"])
        raw = raw or r.get("full_summary", "")
        raw_parts.append(f"[{r['year']}/{r['quarter']}] {_compact_text(raw)}")
    agg_raw = "\n".join(raw_parts)

    agg = MemoryFragment(
        id=agg_id, year=year, quarter=quarter,
        ts=max((r.get("ts") or 0.0) for r in rows),
        main_topic=topic, sub_topic=sub,
        participants=parts, facts_per_role=facts,
        searchable_text=searchable, semantic_context=context,
        full_summary=summary, raw_text=agg_raw,
    )
    engine.cold.write(agg)
    db.insert(agg)
    engine.vector.add(agg.id, embed(agg.semantic_context))

    # 原始行移除（要点已并入聚合片段；排除与聚合同 id 的旧行，避免误删新聚合）
    orig_ids = [r["id"] for r in rows]
    drop_ids = [i for i in orig_ids if i != agg.id]
    if drop_ids:
        db.delete(drop_ids)
        engine.vector.remove(drop_ids)
    for r in rows:
        if r["id"] == agg.id:
            continue
        engine.cold.remove_ids(r["year"], r["quarter"], r["topic"], [r["id"]])
    return agg


def _archive_rows(db):
    """归档库全部行的规范化 dict 列表。"""
    return [_normalize_row(r) for r in db.iter_rows()]


def _compact_raw(engine, ids, keep_ratio=None) -> int:
    """对指定记录做原文原位压缩（保留索引行与摘要，L3 原文变短）。"""
    if not ids:
        return 0
    id_set = set(ids)
    by_partition = {}
    for db in (engine.active, engine.archive_db):
        if db is None:
            continue
        for r in db.get_rows(ids):
            if r["id"] in id_set:
                by_partition.setdefault((r["year"], r["quarter"], r["topic"]), []).append(r["id"])
    total = 0
    for (y, q, t), part_ids in by_partition.items():
        text_map = {}
        for fid in part_ids:
            raw = engine.cold.read_by_id(y, q, t, fid)
            if raw and len(raw) > 200:
                text_map[fid] = _compact_text(raw, keep_ratio)
        if text_map:
            total += engine.cold.update_texts(y, q, t, text_map)
    return total


def enforce_quotas(engine, now=None, max_active=None, max_archive=None,
                   raw_retention_quarters=None, archive_retention_quarters=None,
                   aggregate_year_quarters=None, raw_compact_keep=None) -> dict:
    """配额与降级治理：数据只降级（归档 / 聚合 / 压缩），绝不直接删除。

    1) 活跃库超限 → 最旧记录迁入归档库；
    2) 归档库中超过「季度聚合」时长的组 → 季度聚合；超过「年度聚合」时长的组 → 年度聚合；
    3) 归档库仍超限 → 对最旧组继续聚合（季度 → 年度）直至达标；
    4) 超过「原文保留期」的记录 → L3 原文压缩为要点版（索引与摘要保留）。
    """
    now = now if now is not None else time.time()
    max_active = max_active if max_active is not None else cfg.ACTIVE_MAX_ENTRIES
    max_archive = max_archive if max_archive is not None else cfg.ARCHIVE_MAX_ENTRIES
    raw_ret = raw_retention_quarters if raw_retention_quarters is not None else cfg.RAW_RETENTION_QUARTERS
    agg_q = archive_retention_quarters if archive_retention_quarters is not None else cfg.ARCHIVE_MAX_QUARTERS
    agg_y = aggregate_year_quarters if aggregate_year_quarters is not None else cfg.AGGREGATE_YEAR_QUARTERS

    cy, cq = time_utils.year_quarter(now)
    report = {"moved_to_archive": 0, "quarter_aggregated": 0, "year_aggregated": 0, "raw_compacted": 0}

    # 1) 活跃库超限 → 最旧迁归档
    active_count = engine.active.count()
    if max_active and active_count > max_active:
        excess = active_count - max_active
        ids = engine.active.oldest_ids(excess)
        if ids:
            report["moved_to_archive"] = _move_to_archive(engine, ids)

    # 2) 归档库：按时长聚合（先季度聚合，再年度聚合）
    rows = _archive_rows(engine.archive_db)
    by_quarter = {}
    for r in rows:
        by_quarter.setdefault((r["year"], r["quarter"], r["topic"]), []).append(r)
    for key, group in by_quarter.items():
        if _quarter_age(cy, cq, key[0], key[1]) >= agg_q and len(group) > 1:
            _aggregate_group(engine, engine.archive_db, group, "quarter")
            report["quarter_aggregated"] += 1

    # 年度聚合（超过 agg_y 季度，且季度聚合后仍有多条同 年/主题 的）
    rows = _archive_rows(engine.archive_db)
    by_year = {}
    for r in rows:
        by_year.setdefault((r["year"], r["topic"]), []).append(r)
    for key, group in by_year.items():
        if _quarter_age(cy, cq, key[0], group[0]["quarter"]) >= agg_y and len(group) > 1:
            _aggregate_group(engine, engine.archive_db, group, "year")
            report["year_aggregated"] += 1

    # 3) 归档库仍超限 → 对最旧季度执行聚合降级（跨主题/跨季度合并）直至达标
    guard = 0
    while max_archive and engine.archive_db.count() > max_archive and guard < 200:
        guard += 1
        rows = _archive_rows(engine.archive_db)
        if not rows:
            break
        by_q = {}
        for r in rows:
            by_q.setdefault((r["year"], r["quarter"]), []).append(r)
        oldest_q = min(by_q.keys())
        group = by_q[oldest_q]
        if len(group) == 1:
            # 该季度仅一条：与次旧季度合并（标识用较新的一方）
            others = sorted(by_q.keys())[1:2]
            if not others:
                break
            nq = others[0]
            group = by_q[nq] + group
            _aggregate_group(engine, engine.archive_db, group, "quarter", label=(nq[0], nq[1], None))
            report["quarter_aggregated"] += 1
        else:
            _aggregate_group(engine, engine.archive_db, group, "quarter")
            report["quarter_aggregated"] += 1

    # 4) 原文保留期：超过时压缩 L3 原文（不删除索引与摘要）
    if raw_ret:
        ids = []
        for db in (engine.archive_db, engine.active):
            if db is None:
                continue
            for r in _archive_rows(db) if db is engine.archive_db else [_normalize_row(x) for x in db.iter_rows()]:
                if _quarter_age(cy, cq, r["year"], r["quarter"]) >= raw_ret:
                    ids.append(r["id"])
        if ids:
            report["raw_compacted"] = _compact_raw(engine, ids, raw_compact_keep)

    return report


# ==================== 整理（去重合并） ====================
def _normalize_row(row: dict) -> dict:
    """把 get_rows 返回的原始行（JSON 字符串列）解析为可用的 dict。"""
    import json
    out = dict(row)
    parts = out.get("participants")
    if isinstance(parts, str):
        try:
            out["participants"] = json.loads(parts or "[]")
        except Exception:
            out["participants"] = []
    facts = out.get("facts_per_role")
    if isinstance(facts, str):
        try:
            out["facts_per_role"] = json.loads(facts or "{}")
        except Exception:
            out["facts_per_role"] = {}
    return out


def _row_to_fragment(row):
    row = _normalize_row(row)
    facts = {}
    for role, fd in (row.get("facts_per_role") or {}).items():
        facts[role] = RoleFact.from_dict(fd) if isinstance(fd, dict) else fd
    return MemoryFragment(
        id=row["id"], year=row["year"], quarter=row["quarter"], ts=row.get("ts", 0.0),
        main_topic=row["topic"], sub_topic=row.get("sub_topic", ""),
        participants=row.get("participants") or [],
        facts_per_role=facts,
        searchable_text=row.get("searchable_text", ""),
        semantic_context=row.get("semantic_context", ""),
        full_summary=row.get("full_summary", ""),
    )


def tidy(engine, threshold: float = None, max_group: int = None) -> dict:
    """整理：在 (年/季度/主题) 组内检测近似重复（向量余弦 ≥ 阈值），合并后删除冗余。

    合并策略：保留语义上下文更完整的一条；participants 取并集；
    facts_per_role 取并集（缺失角色从被合并条补充）；full_summary 取更长者。
    """
    threshold = threshold if threshold is not None else cfg.TIDY_SIMILARITY
    max_group = max_group if max_group is not None else cfg.TIDY_MAX_GROUP
    report = {"scanned_groups": 0, "merged_pairs": 0, "removed_ids": []}

    for db in (engine.active, engine.archive_db):
        if db is None:
            continue
        cold_drops = {}   # (年/季度/主题) -> [被合并掉的 id]，批量移除冷存储原文
        partitions = db.partition_ids()
        for key, ids in partitions.items():
            if len(ids) < 2:
                continue
            if len(ids) > max_group * 3:
                continue   # 大组跳过，避免 O(n²)
            report["scanned_groups"] += 1
            rows = {r["id"]: _normalize_row(r) for r in db.get_rows(ids)}
            vecs = {fid: engine.vector.get_vector(fid) for fid in ids}
            done = set()
            for i in range(len(ids)):
                a = ids[i]
                if a in done or a not in vecs or vecs[a] is None:
                    continue
                for j in range(i + 1, len(ids)):
                    b = ids[j]
                    if b in done or b not in vecs or vecs[b] is None:
                        continue
                    if cosine(vecs[a], vecs[b]) >= threshold:
                        keep, drop = _pick_keep(rows[a], rows[b])
                        _merge_into(db, keep, drop)
                        engine.vector.remove([drop["id"]])
                        cold_drops.setdefault(
                            (keep["year"], keep["quarter"], keep["topic"]), []).append(drop["id"])
                        report["merged_pairs"] += 1
                        report["removed_ids"].append(drop["id"])
                        done.add(drop["id"])
        # 批量移除冷存储原文：避免每合并一对就重写一次整个分区文件（大库整理会慢 10 倍以上）
        for (y, q, t), drop_ids in cold_drops.items():
            try:
                engine.cold.remove_ids(y, q, t, drop_ids)
            except Exception:
                pass
    return report


def _pick_keep(row_a, row_b):
    """选择保留更完整的一条。"""
    la = len(row_a.get("semantic_context") or "")
    lb = len(row_b.get("semantic_context") or "")
    if lb > la:
        return row_b, row_a
    return row_a, row_b


def _merge_into(db, keep, drop):
    """把 drop 的参与者与事实并进 keep，删除 drop 的行与 FTS。"""
    keep_facts = dict(keep.get("facts_per_role") or {})
    drop_facts = dict(drop.get("facts_per_role") or {})
    for role, fd in drop_facts.items():
        if role not in keep_facts:
            keep_facts[role] = fd
    keep_parts = list(keep.get("participants") or [])
    for p in drop.get("participants") or []:
        if p not in keep_parts:
            keep_parts.append(p)
    frag = _row_to_fragment({**keep, "participants": keep_parts, "facts_per_role": keep_facts})
    db.insert(frag)
    db.delete([drop["id"]])


# ==================== 分类（分类记录的预留接口实现） ====================
def classify(engine) -> dict:
    """重建分类索引：每条记录登记 主题 / 季度 / 参与者 三个维度。

    对应“分类记录”的预留接口：list_categories / records_in_category 供上层按类取用。
    """
    engine.active.clear_categories()
    engine.archive_db.clear_categories()
    counts = {"topic": 0, "quarter": 0, "participant": 0}
    for db in (engine.active, engine.archive_db):
        if db is None:
            continue
        for raw_row in db.iter_rows():
            row = _normalize_row(raw_row)
            fid = row["id"]
            db.add_category(fid, "topic", row["topic"])
            db.add_category(fid, "quarter", f"{row['year']}/{row['quarter']}")
            for p in row.get("participants") or []:
                db.add_category(fid, "participant", p)
            counts["topic"] += 1
            counts["quarter"] += 1
            counts["participant"] += len(row.get("participants") or [])
    return {"rebuild": True, "entries": counts}
