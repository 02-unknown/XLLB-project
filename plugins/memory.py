# plugins/memory.py —— 官方插件：上下文记忆库（分层语义检索）。
# 启用：初始化 memory_engine（L1 热缓存 / L2 SQLite+FTS5+向量索引 / L3 冷存储），
#       并执行每日一次的 FTS5 增量重建。
# 停用：安全关闭引擎（数据保留在磁盘，再次启用即可恢复）。
# 命令：/memory status | store <文本> | search <查询> | archive | clear
# 说明：本插件只负责「检索 / 存入 / 输出」接口的生命周期与测试入口，
#       不挂接 on_message，不参与发言者判断，不改变任何现有对话行为。
import traceback

import core.config as config

NAME = "上下文记忆库"
VERSION = "1.4.1"
DESCRIPTION = "多角色长文本分层语义检索：入库/检索/输出接口预留，/memory 命令测试"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    "llm_recheck": False,         # 模糊冲突时是否允许轻量级 LLM 复核（默认关闭：低延迟优先；开启会多一次 LLM 网络调用）
    "inject_review": True,        # 注入复核：检索/联网内容注入上下文前用 LLM 判断相关性（200=注入 / 404=阻止）
    "value_check": True,          # 归档价值判断：只保留有长期记忆价值的回合（问候/寒暄等低信息量不归档）
    "llm_value_check": False,     # 归档价值判断是否启用 LLM 深度复核（每回合多一次判断模型调用）
    "stale_timeout": 30,          # 纠错机制：距上次活动超过该分钟数（直接关窗退出后重新使用）自动清理 L0/L1 残留（0=关闭）
    "auto_maintain": True,        # 启用期间后台定期执行存储治理（归档/配额/原文保留）
    "context_mode": "readwrite",  # 上下文使用模式：readwrite=完整权限（生成后记录归档）| readonly=只读（仅写L0，进程结束销毁）
    "debug": False,               # 引擎调试模式（控制台输出检索过程；应用调试模式开启时自动联动）
    "active_max": 0,              # 活跃库片段上限（0=引擎默认 20000）
    "archive_max": 0,             # 归档库片段上限（0=引擎默认 100000）
    "raw_retention": 0,           # L3 原文压缩季度数（0=引擎默认 8）
    "archive_retention": 0,       # 季度聚合季度数（0=引擎默认 12）
}

_engine = None
_maintain_stop = None            # 后台治理线程停止事件
_maintain_thread = None


def _get_engine(ctx):
    global _engine
    if _engine is None:
        try:
            import memory_engine
            _engine = memory_engine.get_engine()
            _engine.init()
            _apply_engine_config(ctx)
        except Exception as e:
            ctx.log("上下文记忆库初始化失败:", e)
            traceback.print_exc()
            return None
    return _engine


def _apply_engine_config(ctx):
    """把插件设置（LLM 复核开关、注入复核、归档价值判断、残留清理、上下文模式、调试模式）应用到引擎。"""
    import memory_engine.config as mecfg
    st = ctx.manager.get_settings(NAME)
    mecfg.LLM_RECHECK_ENABLED = bool(st.get("llm_recheck", True))
    mecfg.INJECT_REVIEW = bool(st.get("inject_review", True))
    mecfg.ARCHIVE_VALUE_CHECK = bool(st.get("value_check", True))
    mecfg.LLM_VALUE_CHECK = bool(st.get("llm_value_check", False))
    try:
        mecfg.STALE_CONTEXT_TIMEOUT = max(0, int(float(st.get("stale_timeout") or 30))) * 60
    except Exception:
        mecfg.STALE_CONTEXT_TIMEOUT = 30 * 60
    try:
        _engine.set_mode(st.get("context_mode", "readwrite"))
    except Exception:
        pass
    try:
        _engine.set_debug(bool(st.get("debug", False)) or bool(getattr(config, "DEBUG_MODE", False)))
    except Exception:
        pass


def _maintain_loop(ctx):
    """后台治理线程：按间隔执行 govern（归档 + 配额 + 原文保留 + FTS 重建）。"""
    import memory_engine.config as mecfg
    interval = mecfg.AUTO_MAINTAIN_INTERVAL
    while not _maintain_stop.wait(interval):
        try:
            eng = _get_engine(ctx)
            if eng is None:
                continue
            st = ctx.manager.get_settings(NAME)
            if not st.get("auto_maintain", True):
                continue
            overrides = {}
            if int(st.get("active_max") or 0) > 0:
                overrides["max_active"] = int(st["active_max"])
            if int(st.get("archive_max") or 0) > 0:
                overrides["max_archive"] = int(st["archive_max"])
            if int(st.get("raw_retention") or 0) > 0:
                overrides["raw_retention_quarters"] = int(st["raw_retention"])
            if int(st.get("archive_retention") or 0) > 0:
                overrides["archive_retention_quarters"] = int(st["archive_retention"])
            eng.govern(**overrides)
        except Exception:
            traceback.print_exc()


def on_load(settings, ctx):
    import memory_engine.config as mecfg
    mecfg.LLM_RECHECK_ENABLED = bool(settings.get("llm_recheck", True))
    mecfg.INJECT_REVIEW = bool(settings.get("inject_review", True))
    mecfg.ARCHIVE_VALUE_CHECK = bool(settings.get("value_check", True))
    mecfg.LLM_VALUE_CHECK = bool(settings.get("llm_value_check", False))
    try:
        mecfg.STALE_CONTEXT_TIMEOUT = max(0, int(float(settings.get("stale_timeout") or 30))) * 60
    except Exception:
        mecfg.STALE_CONTEXT_TIMEOUT = 30 * 60
    eng = _get_engine(ctx)
    if eng is not None:
        try:
            eng.set_mode(settings.get("context_mode", "readwrite"))
        except Exception:
            pass
        try:
            eng.set_debug(bool(settings.get("debug", False)) or bool(getattr(config, "DEBUG_MODE", False)))
        except Exception:
            pass
        try:
            eng.maybe_rebuild_fts()   # 每日一次的 FTS5 增量重建
        except Exception:
            pass
        try:
            eng.on_run_start()        # 运行开始：清理 L0 残留 + 存储治理
        except Exception:
            pass
    # 启动后台治理线程（仅一次）
    global _maintain_stop, _maintain_thread
    if _maintain_thread is None or not _maintain_thread.is_alive():
        import threading
        _maintain_stop = threading.Event()
        _maintain_thread = threading.Thread(
            target=_maintain_loop, args=(ctx,), daemon=True, name="memory-maintain")
        _maintain_thread.start()


def on_unload(ctx):
    global _engine, _maintain_stop
    if _maintain_stop is not None:
        _maintain_stop.set()
        _maintain_stop = None
    if _engine is not None:
        try:
            _engine.on_run_end()      # 运行结束：销毁 L0 + 存储治理
        except Exception:
            pass
        try:
            _engine.close()
        except Exception:
            pass
        _engine = None


def on_settings_changed(settings, ctx):
    import memory_engine.config as mecfg
    mecfg.LLM_RECHECK_ENABLED = bool(settings.get("llm_recheck", True))
    mecfg.INJECT_REVIEW = bool(settings.get("inject_review", True))
    mecfg.ARCHIVE_VALUE_CHECK = bool(settings.get("value_check", True))
    mecfg.LLM_VALUE_CHECK = bool(settings.get("llm_value_check", False))
    try:
        mecfg.STALE_CONTEXT_TIMEOUT = max(0, int(float(settings.get("stale_timeout") or 30))) * 60
    except Exception:
        mecfg.STALE_CONTEXT_TIMEOUT = 30 * 60
    try:
        eng = _get_engine(ctx)
        eng.set_mode(settings.get("context_mode", "readwrite"))
        eng.set_debug(bool(settings.get("debug", False)) or bool(getattr(config, "DEBUG_MODE", False)))
    except Exception:
        pass


def settings_schema():
    return [
        {"key": "llm_recheck", "label": "模糊冲突时允许 LLM 复核",
         "desc": "两条记忆得分接近时，是否让大模型再判断一次谁更贴合。开启会更准一点，但每次都会多一次联网调用，变慢。默认关闭。", "type": "checkbox"},
        {"key": "inject_review", "label": "注入复核（防止无关内容混进回答）",
         "desc": "检索到记忆或联网内容后，先让大模型判断它是否和当前话题相关；不相关（输出 404）就不注入。默认开启。", "type": "checkbox"},
        {"key": "value_check", "label": "归档价值判断（只记住有意义的对话）",
         "desc": "把对话写入长期记忆前先判断值不值得记：像“你好”“哈哈”“谢谢”这类问候寒暄、语气应答不会进入长期记忆（只在本次会话里出现），避免以后检索时错误命中。默认开启。", "type": "checkbox"},
        {"key": "llm_value_check", "label": "归档价值判断：LLM 深度复核",
         "desc": "在结构判断之外，再让大模型判断每回合是否值得记住（更准确，但每次对话都会多一次联网调用，变慢）。默认关闭。", "type": "checkbox"},
        {"key": "stale_timeout", "label": "残留上下文自动清理间隔（分钟）",
         "desc": "如果上次聊天已经过去很久（比如直接关掉窗口隔天再回来），自动清空上次的临时对话记忆，避免串味。0 表示关闭。默认 30 分钟。", "type": "number"},
        {"key": "auto_maintain", "label": "后台定期自动整理存储",
         "desc": "启用后会自动把旧记忆归档、合并压缩，保证越用越久也不占太多空间。建议保持开启。", "type": "checkbox"},
        {"key": "debug", "label": "引擎调试模式",
         "desc": "开启后把检索的每一步过程打印到控制台并写入运行日志，方便排查问题。一般用户无需开启。", "type": "checkbox"},
        {"key": "context_mode", "label": "上下文使用模式",
         "desc": "完整权限：每次对话都会记入长期记忆，之后能回忆起来；只读：只用本次会话的临时记忆，关掉程序就清空，不会积累。", "type": "select",
         "options": [
             {"value": "readwrite", "label": "完整权限（记录并长期保存）"},
             {"value": "readonly", "label": "只读（仅本次会话，关掉即清空）"},
         ]},
        {"type": "section", "key": "advanced", "label": "高级选项",
         "desc": "下面的设置决定记忆库的容量上限和保留策略；下面的操作属于高风险操作，全部收在这里避免误触。普通用户保持默认即可。",
         "fields": [
             {"key": "active_max", "label": "近期记忆区容量上限（条）",
              "desc": "记忆库分两个区：近期区放最近常用的记忆。超过这个数后，最旧的会自动移到长期归档区，不会丢。0 = 用默认值（2 万条）。", "type": "number"},
             {"key": "archive_max", "label": "长期归档区容量上限（条）",
              "desc": "长期归档区放搬过来的旧记忆。超过这个数后，最旧的内容会按季度合并成摘要，只留要点、不删除。0 = 用默认值（10 万条）。", "type": "number"},
             {"key": "raw_retention", "label": "原始对话全文保留时长（季度）",
              "desc": "记下来的对话原文能完整保留多久。超过这个时长后，原文会自动压缩成“要点版”，只留开头和关键信息，省空间。0 = 用默认值（8 个季度，约两年）。", "type": "number"},
             {"key": "archive_retention", "label": "归档内容合并成季度摘要的时长（季度）",
              "desc": "旧记忆在归档区放多久后会合并成一条季度总览（同一季度的多条记成一条）。合并后仍能检索到，只是细节变少。0 = 用默认值（12 个季度，约三年）。", "type": "number"},
         ],
         "actions": [
             {"name": "view_memory", "label": "查看 / 管理记忆", "desc": "在新页面按层级查看全部记忆，支持关键词 / 日期查询，可手动编辑、新增、删除"},
             {"name": "tidy_memory", "label": "上下文整理（归档 / 去重 / 分类）", "desc": "立即执行一次归档整理：季度归档、近似重复合并、分类索引重建（数据只降级不删除）"},
             {"name": "reset_memory", "label": "记忆初始化（清空全部记忆）", "desc": "二级确认：先弹风险警告，选择“是”后才执行删除"},
         ]},
    ]


def actions():
    # 高风险 / 管理类操作已移入「高级选项」分组内（settings_schema 的 section.actions），
    # 不在插件顶部暴露按钮，避免误触。
    return []


def on_action(action, ctx):
    eng = _get_engine(ctx)
    if eng is None:
        return {"reply": "记忆库不可用。", "speak": False}
    if action == "view_memory":
        # 打开记忆管理页面（新页面显示，按层级快速定位 + 查询 + 编辑增删）
        return {"page": "/memory_view.html", "speak": False}
    if action == "inspect_caches":
        return {"reply": _cache_report(eng), "speak": False}
    if action == "tidy_memory":
        # 一键上下文整理：季度归档 + 配额降级 + 近似重复合并 + 分类索引重建（数据只降级不删除）
        try:
            g = eng.govern()
            t = eng.tidy_records()
            c = eng.classify_records()
            a = g.get("archive", {})
            q = g.get("quotas", {})
            return {"reply": (
                "上下文整理完成（数据只降级，不删除）：\n"
                f"· 季度归档：迁移 {a.get('moved', 0)} 条（活跃 {a.get('active_count', 0)} / 归档 {a.get('archive_count', 0)}）\n"
                f"· 聚合/压缩：季度聚合 {q.get('quarter_aggregated', 0)} 组，年度聚合 {q.get('year_aggregated', 0)} 组，"
                f"原文压缩 {q.get('raw_compacted', 0)} 条\n"
                f"· 近似重复合并：{t.get('merged_pairs', 0)} 对\n"
                f"· 分类索引重建：主题 {c.get('entries', {}).get('topic', 0)} / "
                f"季度 {c.get('entries', {}).get('quarter', 0)} / 参与者 {c.get('entries', {}).get('participant', 0)}"),
                    "speak": False}
        except Exception as e:
            return {"reply": f"上下文整理失败：{e}", "speak": False}
    if action == "reset_memory":
        # 第一步：仅返回风险警告（由前端弹窗展示“是/否”选项，选“是”后调用 reset_memory_do）
        return {"reply": _RESET_WARNING, "confirm": "reset_memory_do", "speak": False}
    if action == "reset_memory_do":
        try:
            r = eng.initialize_memory()
            return {"reply": ("记忆初始化完成：已删除全部记忆"
                              f"（活跃 {r.get('active', 0)} 条 / 归档 {r.get('archive', 0)} 条 + 冷存储原文）。"),
                    "speak": False}
        except Exception as e:
            return {"reply": f"记忆初始化失败：{e}", "speak": False}
    return None


_RESET_WARNING = (
    "⚠️ 风险警告：记忆初始化将【永久删除】全部记忆！\n"
    "包括：\n"
    "· 近期记忆区与长期归档区的所有记录（无法恢复）\n"
    "· 原始对话全文（冷存储）\n"
    "· 各级缓存（L0 会话 / L1 检索缓存）\n\n"
    "此操作不可撤销。确定要继续吗？"
)


def _cache_report(eng):
    """各级缓存内容报告（供「查看各级缓存」动作与 /memory cache 命令）。"""
    from memory_engine import roles
    lines = ["【L0 会话缓存 · 最近对话（进程内临时）】"]
    if eng.l0 and eng.l0.size():
        for t in reversed(eng.l0.get_recent(6)):
            who = "用户" if t.get("role") == "user" else "助手"
            lines.append("  %s：%s" % (who, roles.render((t.get("content") or "")[:44], eng.char_name)))
        if eng.l0.size() > 6:
            lines.append("  … 共 %d 条" % eng.l0.size())
    else:
        lines.append("  （空）")

    lines.append("【L1 检索热缓存（最近查询结果，30 分钟）】")
    st = eng.l1.stats() if eng.l1 else {}
    lines.append("  缓存项：%d 项（命中 %d / 未中 %d）" % (
        eng.l1.size() if eng.l1 else 0, st.get("hit", 0), st.get("miss", 0)))

    lines.append("【L2 索引层（活跃区 / 归档区）】")
    lines.append("  活跃片段 %d 条｜归档片段 %d 条" % (
        eng.active.count() if eng.active else 0, eng.archive_db.count() if eng.archive_db else 0))
    for db, name in ((eng.active, "活跃"), (eng.archive_db, "归档")):
        if db is None:
            continue
        rows = list(db.iter_rows())
        for r in rows[-3:]:
            lines.append("  %s %s [%s/%s %s] %s" % (
                name, r["id"], r["year"], r["quarter"], r["topic"],
                roles.render((r.get("full_summary") or "")[:30], eng.char_name)))

    parts = eng.cold.partitions() if eng.cold else []
    lines.append("【L3 冷存储（原始全文分区）】")
    lines.append("  分区 %d 个%s" % (len(parts), ("：" + "、".join(str(p) for p in parts[:8])) if parts else ""))
    return "\n".join(lines)


def commands():
    return [
        {"name": "/memory", "desc": "上下文记忆库：状态/入库/检索/缓存/模式/治理/整理/分类", "args": "status|store 文本|search 查询|cache|mode readwrite|readonly|l0|usage|govern|tidy|classify|archive|clear"},
    ]


def on_command(command, args, ctx):
    if command != "/memory":
        return None
    eng = _get_engine(ctx)
    if eng is None:
        return {"reply": "上下文记忆库初始化失败，请查看日志。", "speak": False}

    sub = args[0] if args else "status"

    if sub == "status":
        return {"reply": _status_text(eng), "speak": False}

    if sub == "mode":
        if len(args) < 2 or args[1] not in ("readwrite", "readonly", "读写", "只读"):
            return {"reply": f"当前上下文模式：{eng.get_mode()}（用法：/memory mode readwrite|readonly）", "speak": False}
        mode = "readwrite" if args[1] in ("readwrite", "读写") else "readonly"
        eng.set_mode(mode)
        ctx.manager.save_settings(NAME, {"context_mode": mode})
        return {"reply": f"上下文模式已切换为：{'完整权限（生成后记录归档）' if mode == 'readwrite' else '只读（仅写L0，进程结束销毁）'}", "speak": False}

    if sub == "debug":
        if len(args) >= 2 and args[1] in ("on", "off"):
            on = args[1] == "on"
            eng.set_debug(on)
            ctx.manager.save_settings(NAME, {"debug": on})
            return {"reply": f"引擎调试模式已{'开启' if on else '关闭'}（检索过程将输出到控制台与日志）。", "speak": False}
        return {"reply": f"引擎调试模式：{'开启' if eng.status().get('debug') else '关闭'}"
                         f"（用法：/memory debug on|off）\n运行日志：{eng.status().get('log_file')}", "speak": False}

    if sub == "l0":
        l0 = eng.l0
        recent = l0.get_recent(8) if l0 else []
        lines = [f"L0 会话缓存：{l0.size() if l0 else 0} 条（窗口上限 {l0.max_entries if l0 else 20}）",
                 f"模式：{eng.get_mode()}"]
        for t in reversed(recent):
            who = "用户" if t.get("role") == "user" else "助手"
            lines.append(f"  {who}：{(t.get('content') or '')[:40]}")
        return {"reply": "\n".join(lines), "speak": False}

    if sub == "cache":
        return {"reply": _cache_report(eng), "speak": False}

    if sub == "store":
        text = " ".join(args[1:]).strip()
        if not text:
            return {"reply": "用法：/memory store <要入库的文本>", "speak": False}
        try:
            r = eng.ingest(text)
            return {"reply": (f"已入库：{r.fragment_id}\n主题：{r.main_topic}｜{r.year}/{r.quarter}\n"
                              f"参与者：{'、'.join(r.participants) if r.participants else '无'}\n"
                              f"L3：{r.l3_ref}"), "speak": False}
        except Exception as e:
            return {"reply": f"入库失败：{e}", "speak": False}

    if sub == "search":
        q = " ".join(args[1:]).strip()
        if not q:
            return {"reply": "用法：/memory search <查询>", "speak": False}
        try:
            r = eng.search(q)
            if not r.id:
                return {"reply": f"未找到相关记忆（route={r.route}，得分 {r.base_score}）。", "speak": False}
            lines = [f"命中：{r.id}（route={r.route}，置信 {r.confidence}）",
                     f"摘要：{r.full_summary}",
                     f"参与者：{'、'.join(r.participants) if r.participants else '无'}"]
            for role, fd in (r.facts_per_role or {}).items():
                bits = []
                if fd.action:
                    bits.append(f"动作：{fd.action}")
                if fd.result:
                    bits.append(f"结果：{fd.result}")
                if fd.stance:
                    bits.append(f"立场：{fd.stance}")
                if not bits and fd.raw:
                    bits.append(fd.raw)
                lines.append(f"  {role} → {'；'.join(bits)}")
            return {"reply": "\n".join(lines), "speak": False}
        except Exception as e:
            return {"reply": f"检索失败：{e}", "speak": False}

    if sub == "archive":
        try:
            r = eng.archive()
            return {"reply": (f"季度归档完成：迁移 {r.get('moved', 0)} 条\n"
                              f"活跃库 {r.get('active_count')} 条｜归档库 {r.get('archive_count')} 条"), "speak": False}
        except Exception as e:
            return {"reply": f"归档失败：{e}", "speak": False}

    if sub == "usage":
        try:
            u = eng.usage()
            return {"reply": _usage_text(u), "speak": False}
        except Exception as e:
            return {"reply": f"空间统计失败：{e}", "speak": False}

    if sub == "govern":
        try:
            st = ctx.manager.get_settings(NAME)
            overrides = {}
            if int(st.get("active_max") or 0) > 0:
                overrides["max_active"] = int(st["active_max"])
            if int(st.get("archive_max") or 0) > 0:
                overrides["max_archive"] = int(st["archive_max"])
            if int(st.get("raw_retention") or 0) > 0:
                overrides["raw_retention_quarters"] = int(st["raw_retention"])
            if int(st.get("archive_retention") or 0) > 0:
                overrides["archive_retention_quarters"] = int(st["archive_retention"])
            r = eng.govern(**overrides)
            a = r["archive"]
            q = r["quotas"]
            return {"reply": (f"治理完成（数据只降级，不删除）：\n"
                              f"归档：迁移 {a.get('moved', 0)} 条"
                              f"（活跃 {a.get('active_count')} / 归档 {a.get('archive_count')}）\n"
                              f"聚合：季度聚合 {q.get('quarter_aggregated', 0)} 组，"
                              f"年度聚合 {q.get('year_aggregated', 0)} 组\n"
                              f"压缩：原文压缩 {q.get('raw_compacted', 0)} 条\n"
                              f"FTS：重建 {r['fts'].get('rows', 0)} 行"), "speak": False}
        except Exception as e:
            return {"reply": f"治理失败：{e}", "speak": False}

    if sub == "tidy":
        try:
            r = eng.tidy_records()
            return {"reply": (f"整理完成：扫描 {r.get('scanned_groups', 0)} 组，"
                              f"合并近似重复 {r.get('merged_pairs', 0)} 对，"
                              f"清理 {len(r.get('removed_ids', []))} 条冗余。"), "speak": False}
        except Exception as e:
            return {"reply": f"整理失败：{e}", "speak": False}

    if sub == "classify":
        try:
            r = eng.classify_records()
            c = r.get("entries", {})
            cats = eng.list_categories(limit=30)
            topic = [f"{x['cat_value']}×{x['count']}" for x in cats if x["cat_type"] == "topic"][:8]
            lines = [f"分类完成：主题 {c.get('topic', 0)}、季度 {c.get('quarter', 0)}、参与者 {c.get('participant', 0)} 条登记"]
            if topic:
                lines.append("主题分布：" + "，".join(topic))
            return {"reply": "\n".join(lines), "speak": False}
        except Exception as e:
            return {"reply": f"分类失败：{e}", "speak": False}

    if sub == "clear":
        try:
            eng.clear_all()
            return {"reply": "记忆库已清空（活跃库 + 归档库 + 向量 + 缓存）。", "speak": False}
        except Exception as e:
            return {"reply": f"清空失败：{e}", "speak": False}

    return {"reply": _status_text(eng) + "\n子命令：status | store 文本 | search 查询 | archive | clear", "speak": False}


def _status_text(eng) -> str:
    try:
        st = eng.status()
    except Exception as e:
        return f"记忆库状态获取失败：{e}"
    if not st.get("ready"):
        return "上下文记忆库：未初始化。"
    lines = [
        "上下文记忆库状态：",
        f"上下文模式：{st.get('context_mode', 'readwrite')}",
        f"调试模式：{'开启' if st.get('debug') else '关闭'}（日志：{st.get('log_file', '')}）",
        f"L0 会话缓存：{st.get('l0_size', 0)} 条（已挤出 {st.get('l0_stats', {}).get('evicted', 0)}）",
        f"活跃库片段：{st['active_count']} 条",
        f"归档库片段：{st['archive_count']} 条",
        f"向量索引：{st['vector_count']} 条（{st['vector_backend']}）",
        f"L1 缓存：{st['cache_size']} 项（命中 {st['cache_stats'].get('hit', 0)} / 未中 {st['cache_stats'].get('miss', 0)}）",
        f"L3 冷存储分区：{len(st['l3_partitions'])} 个",
        f"FTS5：{'启用' if st.get('fts_enabled') else '不可用（自动降级 LIKE）'}",
    ]
    return "\n".join(lines)


def _usage_text(u) -> str:
    mb = 1024 * 1024
    lines = [
        "记忆库空间统计：",
        f"活跃片段 {u.get('active_count', 0)} 条｜归档片段 {u.get('archive_count', 0)} 条｜向量 {u.get('vector_count', 0)} 条",
        f"活跃库文件：{u.get('active_db_bytes', 0) / mb:.2f} MB",
        f"归档库文件：{u.get('archive_db_bytes', 0) / mb:.2f} MB",
        f"L3 冷存储：{u.get('cold_bytes', 0) / mb:.2f} MB（{u.get('l3_partitions', 0)} 个分区）",
        f"合计占用：{u.get('total_bytes', 0) / mb:.2f} MB",
    ]
    return "\n".join(lines)


def get_state(ctx):
    """插件管理页展示：记忆库统计。"""
    eng = _get_engine(ctx)
    queue = []
    if eng is None:
        queue = [{"index": 1, "title": "记忆库未初始化", "status": ""}]
    else:
        try:
            st = eng.status()
            items = [
                f"活跃片段 {st['active_count']} 条",
                f"归档片段 {st['archive_count']} 条",
                f"向量 {st['vector_count']} 条",
                f"L1 缓存 {st['cache_size']} 项",
                f"L3 分区 {len(st['l3_partitions'])} 个",
            ]
            queue = [{"index": i + 1, "title": t, "status": ""} for i, t in enumerate(items)]
        except Exception:
            queue = [{"index": 1, "title": "状态读取失败", "status": ""}]
    return {"queue": queue, "saved": False}
