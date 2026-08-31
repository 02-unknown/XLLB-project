# memory_engine/retrieval.py
# 检索与召回流程（精简四步法）：全程不判断发言者，只做记忆召回。
#   1) L1 缓存闪电短路（输入哈希命中 → 直接返回完整结果）
#   2) L2 硬过滤（时间词 + 主题标签 → B-Tree 剪枝至 ≤200 候选）
#   3) 双路并行召回（向量点积 + BM25 关键词，取并集加权）
#   4) 置信度仲裁（高置信直出 / 模糊 LLM 复核 / 低置信返回空）
from __future__ import annotations

import threading

import memory_engine.config as cfg
from memory_engine import time_utils
from memory_engine import logging as me_log
from memory_engine import roles
from memory_engine import text_utils
from memory_engine.embedding import embed
from memory_engine.models import RetrievedMemory, RoleFact
from memory_engine import llm_recheck
from memory_engine.storage.l1_cache import L1Cache, cache_key


def _normalize(scores: list, relative: bool = False) -> list:
    """得分归一化到 [0,1]。

    - 向量路径（relative=False）：余弦本身已在 [0,1]，保持绝对相似度；
    - 关键词路径（relative=True）：BM25 无上界且绝对尺度极小，按候选集最大值相对缩放。
    """
    if not scores:
        return []
    if relative:
        mx = max(s for _, s in scores) or 1.0
        return [(fid, s / mx) for fid, s in scores]
    return [(fid, max(0.0, min(1.0, s))) for fid, s in scores]


def _merge(vec_scores, kw_scores, id_set) -> dict:
    """双路并集 + 综合基础分 Base_Score = 0.6*S_vec + 0.4*S_keyword。"""
    merged = {fid: {"vec": 0.0, "kw": 0.0} for fid in id_set}
    for fid, s in vec_scores:
        if fid in merged:
            merged[fid]["vec"] = s
    for fid, s in kw_scores:
        if fid in merged:
            merged[fid]["kw"] = s
    out = {}
    for fid, d in merged.items():
        if d["vec"] > 0 or d["kw"] > 0:
            out[fid] = cfg.VEC_WEIGHT * d["vec"] + cfg.KEYWORD_WEIGHT * d["kw"]
    return out


def _roles_set(role):
    """把 role（单个名字或名字列表）规整为集合。"""
    if not role:
        return set()
    if isinstance(role, (list, tuple, set)):
        return {str(r).strip() for r in role if str(r).strip()}
    return {str(role).strip()} if str(role).strip() else set()


def search(engine, user_input: str, top_k: int = cfg.TOP_K, now: float = None,
           include_raw: bool = False, role=None, extra_query: str = None) -> RetrievedMemory:
    """对外检索入口：输入用户话术，返回记忆片段 + facts_per_role + 置信度。

    返回 RetrievedMemory；无相关记忆时返回 route='none'、id='' 的空结果（不调用 LLM）。
    role：当前角色（单角色名或角色名列表，预留给多角色对话）——
    打分后对「参与者包含该角色」的事件加成（ROLE_BOOST），判断层优先输出与当前角色相关的事件。
    extra_query：省略式追问（如「星期三呢」）时由上层传入上一轮用户话术，
    仅参与向量/关键词相似度扩展，不影响时间解析与主题剪枝。
    """
    user_input = (user_input or "").strip()
    if not user_input:
        return RetrievedMemory(id="", route="none")

    me_log.debug(f"[检索] 输入: {user_input!r} | include_raw={include_raw} | 角色={role or '无'}"
                 + (f" | 追问扩展={extra_query!r}" if extra_query else ""))

    # ---------- 1) L1 缓存闪电短路 ----------
    tr = time_utils.parse_time_range(user_input, now)
    roles_set = _roles_set(role)
    role_key = ",".join(sorted(roles_set))
    l1_key = cache_key(user_input, top_k, tr.get("mode", "active"), include_raw, role_key, extra_query)
    me_log.debug(f"[检索] 时间解析: 年度={tr.get('year')} 季度={tr.get('quarter')} "
                 f"模式={tr.get('mode')} 缓存键={l1_key[:10]}…")
    cached = engine.l1.get(l1_key)
    if cached is not None:
        cached = dict(cached)
        cached["route"] = "l1"          # 标记为缓存直出
        me_log.debug(f"[检索] L1缓存命中 → 直接返回（来源：L1缓存）id={cached.get('id')}")
        return _render_memory(_from_dict(cached), engine.char_name)

    # ---------- 2) L2 硬过滤（mode 决定检索库：active / archive / both） ----------
    mode = tr.get("mode", "active")
    if mode == "both":
        dbs = [engine.active, engine.archive_db]
    elif mode == "archive":
        dbs = [engine.archive_db]
    else:
        dbs = [engine.active]
    db_names = {id(engine.active): "活跃库", id(engine.archive_db): "归档库"}
    src_index = "+".join(db_names.get(id(d), "?") for d in dbs)
    # 主题不做硬剪枝：同一事件在存储侧可能被归入别的主题，主题过滤会盲目剪掉正确候选；
    # 剪枝只依赖可靠的时间边界（+ 最近优先的候选窗口），主题信号交给双路打分承担。
    cand_ids = []
    for d in dbs:
        cand_ids += d.hard_filter(
            start_ts=tr["start"], end_ts=tr["end"],
            year=tr["year"], quarter=tr["quarter"],
        )
    cand_ids = list(dict.fromkeys(cand_ids))
    me_log.debug(f"[检索] 硬过滤: 来源库=L2-{src_index} 条件(year={tr.get('year')}, quarter={tr.get('quarter')}, "
                 f"topic=不限, 时间=[{tr.get('start')}, {tr.get('end')}]) → 候选 {len(cand_ids)} 条")
    if not cand_ids:
        me_log.debug("[检索] 候选集为空 → 无相关记忆（route=none；会话上下文由 pipeline 上下文检测直接提供）")
        result = RetrievedMemory(id="", route="none", base_score=0.0, confidence=0.0)
        engine.l1.set(l1_key, result.to_dict())
        return result

    # ---------- 3) 双路并行召回（向量覆盖活跃+归档；BM25 跨库合并） ----------
    # 查询规范化：星期X/礼拜X → 周X，保证与入库侧 searchable_text 的说法一致；
    # 省略句追问（extra_query）只扩展相似度输入，不参与时间解析。
    q_kw = text_utils.normalize_query_text(user_input)
    q_vec_text = q_kw
    if extra_query:
        q_vec_text = f"{text_utils.normalize_query_text(extra_query)} {q_kw}"
    vec = embed(q_vec_text)
    results = {}
    barrier = threading.Barrier(3)

    def path_vec():
        r = engine.vector.search(vec, set(cand_ids), top_k)
        results["vec"] = r
        try:
            barrier.wait()
        except Exception:
            pass

    def path_kw():
        kw = []
        for d in dbs:
            kw += d.bm25_search(q_kw, set(cand_ids), top_k)   # (id, score, thin)
        results["kw"] = kw
        try:
            barrier.wait()
        except Exception:
            pass

    try:
        t_vec = threading.Thread(target=path_vec, daemon=True)
        t_kw = threading.Thread(target=path_kw, daemon=True)
        t_vec.start()
        t_kw.start()
        barrier.wait(timeout=2.0)
    except Exception:
        pass
    vec_scores = _normalize(results.get("vec", []), relative=False)
    # BM25 已在库层按绝对尺度返回 [0,1]（FTS 归一化 + 2-gram 按查询潜力归一化），
    # 不再相对最大归一化：仅共享泛用填充词的弱重合不会被放大成高置信。
    kw_raw = []
    kw_meta = {}          # id -> (thin, 是否仅靠 1 个 2-gram 且无 FTS)
    for item in results.get("kw", []):
        fid, s, thin = item[0], item[1], bool(item[2]) if len(item) > 2 else False
        kw_raw.append((fid, s))
        if fid not in kw_meta:
            kw_meta[fid] = thin
        else:
            kw_meta[fid] = kw_meta[fid] and thin
    kw_scores = _normalize(kw_raw, relative=False)
    merged = _merge(vec_scores, kw_scores, cand_ids)

    # 共识校验 + 时间-内容确认：
    # 仅靠 1 个 2-gram（无 FTS 支持）命中的弱匹配，需要额外证据——
    #   1) 候选文本含查询的时间词（如「星期三呢」↔ 候选含「周三」）→ 时间-内容一致，
    #      结构性确认并加成（时间词本身即查询内容）；
    #   2) 否则须得到向量路径的语义确认（vec ≥ VEC_CONFIRM_MIN）；
    #   都不满足 → 视为泛用填充词巧合（如「你好呀」↔「好呀」），剔除。
    thin_ids = [fid for fid, thin in kw_meta.items() if thin and fid in merged]
    time_confirmed = set()
    if thin_ids:
        time_text = tr.get("time_text") or ""
        time_grams = {time_text[i:i + 2] for i in range(max(0, len(time_text) - 1))} if time_text else set()
        if time_grams:
            for d in dbs:
                texts = d.texts_of(thin_ids)
                for fid, t in texts.items():
                    tg = {t[j:j + 2] for j in range(max(0, len(t) - 1))}
                    if (time_text and time_text in t) or (time_grams & tg):
                        time_confirmed.add(fid)
    if kw_meta:
        vec_map = dict(vec_scores)
        for fid, thin in kw_meta.items():
            if not thin or fid not in merged:
                continue
            if fid in time_confirmed:
                merged[fid] += cfg.TIME_CONFIRM_BONUS
                me_log.debug(f"[检索] 时间-内容确认: id={fid} 含查询时间词{tr.get('time_text')!r} "
                             f"+{cfg.TIME_CONFIRM_BONUS} → {merged[fid]:.3f}")
            elif vec_map.get(fid, 0.0) < cfg.VEC_CONFIRM_MIN:
                me_log.debug(f"[检索] 弱匹配剔除: id={fid} 仅1个2-gram且向量未确认"
                             f"(vec={vec_map.get(fid, 0.0):.3f}) → 视为泛用填充词巧合")
                merged.pop(fid, None)

    me_log.debug(f"[检索] 双路召回: 向量Top5={_fmt(vec_scores[:5])}")
    me_log.debug(f"[检索] 双路召回: BM25 Top5={_fmt(kw_scores[:5])}")

    # 角色相关性加权（判断层优先输出与当前角色相关的事件；参与者以占位符存储，
    # 解析后含当前角色（或其（朋友）变体）即视为相关，角色预设改名不影响）
    if roles_set and merged:
        parts_map = {}
        for d in dbs:
            parts_map.update(d.participants_of(list(merged.keys())))
        boosted = []
        for fid in merged:
            if roles.involves(parts_map.get(fid) or [], roles_set, engine.char_name):
                merged[fid] = merged[fid] + cfg.ROLE_BOOST
                boosted.append(fid)
        if boosted:
            me_log.debug(f"[检索] 角色加成: 角色={','.join(sorted(roles_set))} +{cfg.ROLE_BOOST} → "
                         + ", ".join(f"{f[:10]}={m:.3f}" for f, m in merged.items() if f in boosted))

    me_log.debug(f"[检索] 双路召回: 综合Top5={_fmt(sorted(merged.items(), key=lambda x: x[1], reverse=True)[:5])}")
    if not merged:
        me_log.debug("[检索] 双路均无命中 → 无相关记忆（route=none）")
        result = RetrievedMemory(id="", route="none", base_score=0.0, confidence=0.0)
        engine.l1.set(l1_key, result.to_dict())
        return result

    ranked = sorted(merged.items(), key=lambda x: x[1], reverse=True)
    top1, top2 = ranked[0], (ranked[1] if len(ranked) > 1 else (None, 0.0))
    top1_score, top2_score = top1[1], top2[1]

    # ---------- 4) 置信度仲裁 ----------
    winner_id = None
    route = "direct"
    if top1_score < cfg.LOW_CONFIDENCE:
        # 情况C：低置信度降级 → 返回空（不调用 LLM）
        me_log.debug(f"[检索] 仲裁: Top1={top1_score:.4f} < 低置信阈值 {cfg.LOW_CONFIDENCE} → 情况C 返回空（不调LLM）")
        result = RetrievedMemory(id="", route="none", base_score=round(top1_score, 4), confidence=round(top1_score, 4))
        engine.l1.set(l1_key, result.to_dict())
        return result
    if (top1_score - top2_score) > cfg.CONFIDENCE_GAP:
        # 情况A：高置信度直出
        winner_id = top1[0]
        me_log.debug(f"[检索] 仲裁: Top1={top1_score:.4f} - Top2={top2_score:.4f} = {top1_score - top2_score:.4f} "
                     f"> 差距阈值 {cfg.CONFIDENCE_GAP} → 情况A 高置信直出（不碰LLM）")
    elif top1_score < cfg.RECHECK_MIN_SCORE:
        # 低分模糊：不值得消耗 LLM 复核，直接取 Top1（控制延迟）
        winner_id = top1[0]
        me_log.debug(f"[检索] 仲裁: 差距 {top1_score - top2_score:.4f} ≤ {cfg.CONFIDENCE_GAP} 且 "
                     f"Top1={top1_score:.4f} < 复核阈值 {cfg.RECHECK_MIN_SCORE} → 低分模糊直接取Top1（不调LLM）")
    else:
        # 情况B：模糊冲突 → 轻量级 LLM 复核（占比极低；默认关闭，失败则回退 Top1）
        route = "llm_recheck"
        cand_a = _get_from(engine, dbs, top1[0])
        cand_b = _get_from(engine, dbs, top2[0]) if top2[0] else None
        if cand_b is None:
            winner_id = top1[0]
            route = "direct"
        elif not cfg.LLM_RECHECK_ENABLED:
            # 插件「llm_recheck」关闭：不做网络复核，直接取 Top1（低延迟优先）
            winner_id = top1[0]
            route = "direct"
            me_log.debug(f"[检索] 仲裁: 差距 {top1_score - top2_score:.4f} ≤ {cfg.CONFIDENCE_GAP} → "
                         f"情况B 但LLM复核已禁用（llm_recheck=False）→ 直出Top1")
        else:
            me_log.debug(f"[检索] 仲裁: 差距 {top1_score - top2_score:.4f} ≤ {cfg.CONFIDENCE_GAP} → 情况B 触发LLM复核")
            picked = llm_recheck.recheck_binary(
                user_input,
                {"id": cand_a["id"], "summary": cand_a["semantic_context"]},
                {"id": cand_b["id"], "summary": cand_b["semantic_context"]},
                role_hint="、".join(sorted(roles_set)) if roles_set else "",
            )
            winner_id = picked or top1[0]
            me_log.debug(f"[检索] LLM复核结果: picked={picked} → 胜者={winner_id}")
            if picked is None:
                route = "direct"

    frag = _get_from(engine, dbs, winner_id)
    if frag is None:
        result = RetrievedMemory(id="", route="none")
        engine.l1.set(l1_key, result.to_dict())
        return result

    # 记录最近访问时间（供治理淘汰使用）
    try:
        for d in dbs:
            d.touch([frag["id"]])
            break
    except Exception:
        pass

    facts = {}
    for role, fd in frag["facts_per_role"].items():
        facts[role] = RoleFact.from_dict(fd) if isinstance(fd, dict) else fd
    raw_text = ""
    if include_raw:
        try:
            raw_text = engine.cold.read_by_id(frag["year"], frag["quarter"], frag["main_topic"], frag["id"]) or ""
        except Exception:
            raw_text = ""

    result = RetrievedMemory(
        id=frag["id"],
        full_summary=frag["full_summary"],
        participants=frag["participants"],
        facts_per_role=facts,
        base_score=round(top1_score, 4),
        confidence=round(top1_score, 4),
        route=route,
        candidates=[{"id": fid, "score": round(s, 4)} for fid, s in ranked[:3]],
        raw_text=raw_text,
    )
    result = _render_memory(result, engine.char_name)
    me_log.debug(f"[检索] 结果: id={result.id} 摘要={(result.full_summary or '')[:40]}… "
                 f"置信度={result.confidence} route={result.route} "
                 f"来源=L2-{src_index}{'+L3原文' if raw_text else ''}")
    engine.l1.set(l1_key, result.to_dict())
    return result


def _fmt(scores) -> str:
    try:
        return "[" + ", ".join(f"{fid[:8]}={s:.3f}" for fid, s in scores) + "]"
    except Exception:
        return str(scores)


def _get_from(engine, dbs, fragment_id):
    """从一组索引库中取片段（跨层检索时命中可能落在任一库）。"""
    for d in dbs:
        if d is None:
            continue
        row = d.get(fragment_id)
        if row is not None:
            return row
    return None


def _from_dict(d: dict) -> RetrievedMemory:
    facts = {}
    for role, fd in (d.get("facts_per_role") or {}).items():
        facts[role] = RoleFact.from_dict(fd)
    return RetrievedMemory(
        id=d.get("id", ""),
        full_summary=d.get("full_summary", ""),
        participants=d.get("participants") or [],
        facts_per_role=facts,
        base_score=d.get("base_score", 0.0),
        confidence=d.get("confidence", 0.0),
        route=d.get("route", "none"),
        candidates=d.get("candidates") or [],
        raw_text=d.get("raw_text", ""),
    )


def _render_memory(m: RetrievedMemory, char_name=None) -> RetrievedMemory:
    """把检索结果中的 {char} 占位符渲染回当前角色名（改名后历史记忆仍显示新名字）。"""
    if m is None or m.id == "":
        return m
    m.full_summary = roles.render(m.full_summary, char_name)
    m.raw_text = roles.render(m.raw_text, char_name)
    m.participants = roles.render_list(m.participants, char_name)
    out = {}
    for role, fd in (m.facts_per_role or {}).items():
        d = fd.to_dict() if isinstance(fd, RoleFact) else dict(fd or {})
        out[roles.render(role, char_name)] = RoleFact.from_dict(
            {k: roles.render(str(v), char_name) for k, v in d.items()})
    m.facts_per_role = out
    return m
