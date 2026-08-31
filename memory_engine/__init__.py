# memory_engine/__init__.py
# 面向多角色长文本的分层语义检索引擎 —— 对外统一入口。
#
# 职责边界：纯检索器。不判断“谁该发言”，只负责：
#   1) 毫秒级返回与用户输入最相关的历史记忆片段；
#   2) 附带该片段内按角色拆解好的 facts_per_role 事实立场字典；
#   3) 供上层“接话”流水线直接取用。
#
# 对外接口（预留，供上层与插件调用）：
#   engine = memory_engine.get_engine()          # 获取进程内单例
#   engine.init() / engine.close()               # 生命周期
#   engine.ingest(raw_text, ...)                 # 写入（含多角色事实拆解与三级落盘）
#   engine.search(user_input, ...)               # 检索（L1→硬过滤→双路→仲裁）
#   engine.archive()                             # 季度滚动归档
#   engine.rebuild_fts()                         # FTS5 每日重建
#   engine.status()                              # 各层状态统计
from __future__ import annotations

import hashlib
import os
import queue
import threading

import memory_engine.config as cfg
from memory_engine import ingest as ingest_ops
from memory_engine import retrieval
from memory_engine import roles
from memory_engine import text_utils
from memory_engine import time_utils
from memory_engine import logging as me_log
from memory_engine import llm_recheck
from memory_engine.storage import archive as archive_ops
from memory_engine.storage.l0_session import L0Session
from memory_engine.storage.l1_cache import L1Cache
from memory_engine.storage.l2_sqlite import SqliteIndex
from memory_engine.storage.l2_vector import VectorIndex
from memory_engine.storage.l3_cold import ColdStorage
from memory_engine import governance

__version__ = "1.7.0"


class MemoryEngine:
    """分层语义检索引擎（进程内单例，可安全重复 init/close）。

    上下文统一管理：
      - L0：最近 N 条对话回合的临时缓存（进程内），上下文调用时优先查询；
      - L1/L2/L3：长期记忆（归档、聚合、压缩），支持跨年度检索；
      - 双模式：readwrite=完整权限（生成后记录归档）；readonly=只读（仅写 L0，进程结束销毁）。
    """

    def __init__(self, data_dir: str = None, auto_init: bool = True):
        self.data_dir = data_dir or cfg.DATA_DIR
        self._init_done = False
        self._lock = threading.RLock()
        self._char_name = None          # 显式角色名；None 时懒读取应用角色配置
        self.active = None          # L2 活跃主库（SQLite + FTS5）
        self.archive_db = None         # L2 归档历史库
        self.vector = None          # L2 向量索引（numpy-flat，LanceDB 预留）
        self.cold = None            # L3 冷存储（Parquet/JSONL）
        self.l1 = None              # L1 热缓存（LRU + 30min TTL）
        self.l0 = None              # L0 会话缓存（最近对话回合，进程内临时）
        self._mode = cfg.CONTEXT_MODE   # readwrite | readonly
        self._fts_rebuilt_today = False
        self._last_active = time_utils.now_ts()   # 纠错机制：最近一次会话活动时间
        self._review_cache = {}                   # 注入复核结果缓存 {key: (expire, bool)}
        self._archive_queue = queue.Queue()       # 后台归档队列（价值判断 + 写入在输出结束后异步执行）
        self._archive_worker = None               # 后台归档线程
        if auto_init:
            self.init()

    @property
    def char_name(self):
        """当前角色名：显式设置优先，否则懒读取应用角色配置（改预设即生效）。"""
        return self._char_name or roles.resolve_char_name(None)

    def set_char_name(self, name: str) -> None:
        """显式设置当前角色名（写库占位符化与检索渲染使用）。"""
        self._char_name = (name or "").strip() or None

    # ---------------- 生命周期 ----------------
    def init(self):
        with self._lock:
            if self._init_done:
                return
            os.makedirs(cfg.ACTIVE_DB and os.path.dirname(cfg.ACTIVE_DB), exist_ok=True)
            os.makedirs(os.path.dirname(cfg.ARCHIVE_DB), exist_ok=True)
            self.active = SqliteIndex(cfg.ACTIVE_DB)
            self.archive_db = SqliteIndex(cfg.ARCHIVE_DB)
            self.vector = VectorIndex([self.active, self.archive_db])   # 双库加载，支持跨年度向量检索
            self.vector.load()
            self.cold = ColdStorage(cfg.COLD_DIR)
            self.l1 = L1Cache()
            self.l0 = L0Session(cfg.L0_MAX_ENTRIES)
            self._init_done = True

    def close(self):
        """停止引擎：释放连接与缓存（数据保留在磁盘）。"""
        with self._lock:
            # 先处理完后台归档队列（优雅退出不丢失待归档回合），再关闭数据库
            try:
                self._drain_archives()
            except Exception:
                pass
            self._init_done = False
            for db in (self.active, self.archive_db):
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
            self.active = self.archive_db = None
            self.vector = None
            self.cold = None
            self.l1 = None

    def is_ready(self) -> bool:
        return self._init_done

    # ---------------- 写入 ----------------
    def ingest(self, raw_text: str, **kwargs):
        kwargs.setdefault("char_name", self.char_name)
        return ingest_ops.ingest(self, raw_text, **kwargs)

    def delete(self, fragment_id: str) -> bool:
        return ingest_ops.delete(self, fragment_id)

    # ---------------- 检索 ----------------
    def search(self, user_input: str, top_k: int = cfg.TOP_K, now: float = None,
               include_raw: bool = False, role=None, extra_query: str = None):
        return retrieval.search(self, user_input, top_k=top_k, now=now,
                                include_raw=include_raw, role=role, extra_query=extra_query)

    def followup_extra(self, user_text: str) -> str:
        """省略式追问（如「星期三呢」）时，取最近一条用户回合作为语义扩展。

        仅当本句看起来像省略追问且 L0 里有上一轮用户话术时返回扩展文本；否则返回 None。
        """
        if not text_utils.looks_like_followup(user_text) or self.l0 is None:
            return None
        for t in reversed(self.l0.get_recent(self.l0.max_entries)):
            if t.get("role") == "user":
                content = (t.get("content") or "").strip()
                if content:
                    return content
        return None

    def _review_context(self) -> str:
        """复核用的最近对话上下文（最近 4 条回合，每条截断）。"""
        try:
            if self.l0 is not None:
                recent = self.l0.get_recent(4)
                lines = []
                for t in recent:
                    if t.get("role") in ("user", "assistant"):
                        who = "用户" if t.get("role") == "user" else "助手"
                        lines.append(f"{who}：{(t.get('content') or '')[:60]}")
                return "\n".join(lines)
        except Exception:
            pass
        return ""

    def review_injection(self, user_input: str, content: str) -> bool:
        """注入复核（上下文管理能力）：检索/联网内容与当前对话无关时阻止注入。

        自动带上最近对话回合作为判断上下文（判断模型据此判断“用户正在聊什么”，
        避免只见字面重合就误放行）；数字协议：200=相关可注入；404=无关阻止；
        含混/空输出默认阻止。同一回合内相同内容只复核一次（短时缓存）。
        开关由「上下文记忆库」插件设置（cfg.INJECT_REVIEW）控制；关闭时直接放行。
        """
        if not cfg.INJECT_REVIEW or not content or not str(content).strip():
            return True
        context = self._review_context()
        key = hashlib.md5(f"{user_input}|{str(content)[:500]}|{context[:200]}".encode("utf-8", "ignore")).hexdigest()
        now = time_utils.now_ts()
        hit = self._review_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            result = llm_recheck.review_injection(user_input, content, context)
        except Exception:
            result = True
        self._review_cache[key] = (now + cfg.REVIEW_CACHE_TTL, result)
        if len(self._review_cache) > 64:   # 防膨胀：顺手清理过期项
            self._review_cache = {k: v for k, v in self._review_cache.items() if v[0] > now}
        return result

    def maybe_clear_stale(self) -> bool:
        """纠错机制：长时间未活动（用户直接关窗退出后重新使用）时清理残留上下文。

        用户直接关闭所有窗口退出时进程未走优雅关闭，L0/L1 会话缓存与镜像历史可能残留；
        下次活动前若距上次活动超过 STALE_CONTEXT_TIMEOUT，视为新会话并全部清空。
        返回是否执行了清理。timeout=0 时关闭该机制。
        """
        timeout = cfg.STALE_CONTEXT_TIMEOUT
        now = time_utils.now_ts()
        stale = timeout > 0 and (now - self._last_active) > timeout
        self._last_active = now
        if not stale:
            return False
        try:
            self.clear_context()
        except Exception:
            pass
        # 同步清空核心会话镜像（由 L0 重建；直接清理避免旧历史被判断/回退读到）
        try:
            import core.config as _core_cfg
            try:
                _core_cfg.conversation_history.clear()
            except Exception:
                pass
        except Exception:
            pass
        me_log.info(f"[纠错] 距上次活动超过 {timeout}s，判定为新会话，已清理 L0/L1 与镜像历史残留")
        return True

    # ---------------- 上下文统一管理（L0 + 双模式） ----------------
    def set_mode(self, mode: str) -> str:
        """切换上下文使用模式：readwrite（完整权限，生成后记录归档）/ readonly（只读，仅写 L0）。"""
        mode = "readwrite" if str(mode).lower() in ("readwrite", "读写", "rw") else "readonly"
        with self._lock:
            self._mode = mode
        me_log.info(f"[模式] 上下文模式切换为 {mode}")
        return self._mode

    def set_debug(self, enabled: bool) -> None:
        """开启 / 关闭引擎调试输出（控制台打印检索过程；日志文件始终记录）。"""
        me_log.set_debug(bool(enabled))
        me_log.debug(f"[调试] 引擎调试模式 {'开启' if enabled else '关闭'}")

    def get_mode(self) -> str:
        return self._mode

    def clear_context(self) -> None:
        """清空 L0 会话缓存（切换角色 / 清空对话时调用）。"""
        if self.l0 is not None:
            self.l0.clear()
        if self.l1 is not None:
            self.l1.clear()
        me_log.debug("[上下文] L0/L1 缓存已清空")

    def _relevant_recent(self, user_input: str, entries: list) -> list:
        """按「当前输入与回合的 IDF 加权重合度」过滤最近回合（上下文感知）。

        通用统计方案（无主题词表）：话题切换（如从“酸菜鱼做法”转到“新歌推荐”）时，
        与旧话题只有泛用词重合（或无重合）的回合自动被过滤，避免模型把旧话题内容
        带进新回答；同一话题的延续（“酸菜鱼还要放什么”）因内容词重合被保留。
        """
        try:
            scored = self.l0.query_scored(user_input, limit=cfg.L0_MAX_ENTRIES)
            keep_keys = {(e.get("role"), e.get("content"), e.get("ts"))
                         for s, e in scored if s >= cfg.L0_CONTEXT_MIN_SCORE}
            kept = [e for e in entries
                    if (e.get("role"), e.get("content"), e.get("ts")) in keep_keys]
            return kept
        except Exception:
            return entries

    def relevant_recent(self, user_input: str, max_turns: int = None) -> list:
        """返回与当前输入相关的最近回合（上下文感知过滤），供上层注入上下文。

        省略式追问（如“那周六呢”“然后呢”）保留最近窗口以维持延续性；
        其余按 IDF 加权重合度过滤，话题切换时自动丢弃无关旧回合。
        """
        entries = self.l0.get_recent(max_turns or cfg.L0_MAX_ENTRIES) if self.l0 else []
        if not entries or text_utils.looks_like_followup(user_input):
            return entries
        return self._relevant_recent(user_input, entries)

    def assemble_context(self, user_input: str, max_recent: int = None,
                         max_memories: int = 1, role=None) -> dict:
        """生成前调用：组装上下文。

        优先在 L0 查询最近对话回合；再检索 L2/L3 相关长期记忆（跨年度，按当前角色加权）。
        省略式追问（如「星期三呢」）自动带上上一轮用户话术做语义扩展；
        上下文感知：非省略句按与当前输入的相关度过滤旧回合（话题切换不残留）；
        注入复核：记忆在注入前先经过相关性判断（200=注入 / 404=阻止），与多人对话同套逻辑。
        返回 {"recent": [...], "memory": RetrievedMemory|None}。
        """
        self.maybe_clear_stale()   # 纠错：长时间未活动（直接关窗退出后）先清理残留上下文
        recent_all = self.l0.get_recent(max_recent or cfg.L0_MAX_ENTRIES) if self.l0 else []
        recent = self.relevant_recent(user_input, len(recent_all) or None) if recent_all else []
        me_log.debug(f"[上下文] L0查询（来源L0）: 最近 {len(recent_all)} 条 → 与当前问题相关 {len(recent)} 条"
                     + (f"（省略式追问保留窗口）" if text_utils.looks_like_followup(user_input) else ""))
        memory = None
        try:
            r = self.search(user_input, role=role, extra_query=self.followup_extra(user_input))
            if r.id and self.review_injection(user_input, r.full_summary):
                memory = r
            elif r.id:
                me_log.debug(f"[上下文] 记忆被注入复核阻止（404/未确认）→ 不注入: id={r.id}")
        except Exception:
            memory = None
        if memory is not None:
            me_log.debug(f"[上下文] 长期记忆（来源L1/L2/L3）: id={memory.id} route={memory.route} "
                         f"置信度={memory.confidence} 摘要={(memory.full_summary or '')[:40]}…")
        else:
            me_log.debug("[上下文] 无相关长期记忆（仅使用L0最近回合）")
        return {"recent": recent, "memory": memory}

    def record_turn(self, user_text: str, reply: str, meta: dict = None) -> dict:
        """生成后调用：记录一个对话回合（L0 必写）。

        - readwrite（完整权限）：生成后记录并归档到 L2（按回合入库，供长期记忆检索）；
        - readonly（只读）：仅写入 L0 临时缓存，不做归档等后续操作；L0 随进程结束销毁。
        返回 {"l0_size", "archived", "mode"}。
        """
        if not self._init_done or self.l0 is None:
            return {"l0_size": 0, "archived": False, "mode": self._mode}
        meta = dict(meta or {})
        evicted = self.l0.append("user", user_text or "", meta)
        evicted += self.l0.append("assistant", reply or "", meta)
        archived = False
        if (self._mode == "readwrite" and meta.get("archive", True)
                and (user_text or "").strip() and (reply or "").strip()):
            # 归档（含价值判断）放入后台队列：在“回合输出结束后”异步执行，
            # 不阻塞当前回合返回、不增加任何可见延迟；L0 会话上下文已即时写入。
            try:
                self._archive_queue.put((user_text or "", reply or "", meta))
                self._start_archive_worker()
                archived = True
            except Exception:
                archived = False
        me_log.debug(f"[回合记录] 模式={self._mode} L0={self.l0.size()}/{self.l0.max_entries} "
                     f"归档={'后台队列' if archived else '否'} 挤出={len(evicted)} 条"
                     f"（超时历史{'，读写模式已提交后台归档' if evicted else ''}）")
        return {"l0_size": self.l0.size(), "archived": archived, "mode": self._mode}

    # ---------------- 后台归档（输出结束后异步执行，价值判断 + 长期写入） ----------------
    def _start_archive_worker(self):
        if self._archive_worker is None or not self._archive_worker.is_alive():
            self._archive_worker = threading.Thread(
                target=self._archive_loop, daemon=True, name="memory-archive")
            self._archive_worker.start()

    def _archive_loop(self):
        while True:
            try:
                item = self._archive_queue.get()
            except Exception:
                break
            if item is None:
                break
            try:
                self._archive_turn(*item)
            except Exception:
                pass
            finally:
                try:
                    self._archive_queue.task_done()
                except Exception:
                    pass

    def _archive_turn(self, user_text, reply, meta):
        """后台归档一个回合：归档核心是【用户输入】（用户陈述的事件/约定/偏好/经历），
        角色回复只作为补充检索词进入 searchable_text，避免把模型生成的客套话当成记忆。
        参与者：参与角色（多人对话传槽位名）+ 用户；事实以「用户 → 动作」记录。"""
        if cfg.ARCHIVE_VALUE_CHECK and not self._archive_value(user_text, reply):
            me_log.debug("[回合记录] 归档价值判断：低信息量回合（问候/寒暄等），跳过长期归档")
            return
        try:
            role = str(meta.get("role") or self.char_name or "用户").strip()
            participants = [str(p).strip() for p in (meta.get("participants") or [role]) if str(p).strip()]
            if "用户" not in participants:
                participants = ["用户"] + participants
            # 事实：以用户输入为记忆核心（避免把助手回复的客套话归档成“记忆”）
            facts = {"用户": {"action": (user_text or "").strip()[:80]}}
            main_topic = meta.get("main_topic")
            if not main_topic:
                from memory_engine.preprocessing import guess_topic
                main_topic = guess_topic((user_text or "") + " " + (reply or ""))[0]
            r = self.ingest(
                f"{user_text}\n{reply}",
                participants=participants,
                facts_per_role=facts,
                main_topic=main_topic,
                sub_topic=meta.get("sub_topic") or "",
                ts=meta.get("ts"),
                year=meta.get("year"),
                quarter=meta.get("quarter"),
                full_summary=(user_text or "")[:cfg.SEMANTIC_CORE_MAX],
            )
            me_log.debug(f"[回合记录] 后台归档完成: id={r.fragment_id} 主题={r.main_topic} 摘要=用户输入")
        except Exception:
            pass

    def _drain_archives(self, timeout: float = 15.0) -> None:
        """等待后台归档队列处理完毕（优雅关闭 / 测试用，最多等 timeout 秒）。"""
        deadline = time_utils.now_ts() + timeout
        while time_utils.now_ts() < deadline:
            try:
                if self._archive_queue.unfinished_tasks <= 0:
                    break
            except Exception:
                break
            import time as _t
            _t.sleep(0.05)

    def _archive_value(self, user_text: str, reply: str) -> bool:
        """归档价值判断：只保留有长期记忆价值的回合。

        结构信号（零成本，始终生效）：用户消息为空 / 整句为功能语气字
        （问候、寒暄、语气应答，如「你好」「哈哈」「好的」「谢谢」）→ 不归档；
        可选 LLM 深度复核（cfg.LLM_VALUE_CHECK，默认关闭）对边界情况复核（1=保留 / 0=丢弃）。
        """
        user = (user_text or "").strip()
        reply = (reply or "").strip()
        if not user or not reply:
            return False
        if text_utils.is_filler_text(user):
            return False
        if cfg.LLM_VALUE_CHECK:
            try:
                return llm_recheck.value_judge(user, reply)
            except Exception:
                return True
        return True

    def on_run_start(self) -> dict:
        """每次运行开始：清理 L0 残留（进程内），并执行存储治理（超时历史移入下一层）。"""
        report = {"l0_cleared": False, "govern": None}
        if self.l0 is not None:
            if self.l0.size() > 0:
                n = self.l0.size()
                self.l0.clear()
                report["l0_cleared"] = True
                me_log.info(f"[治理] 运行开始：清理 L0 残留 {n} 条")
        try:
            report["govern"] = self.govern()
        except Exception as e:
            report["govern"] = {"error": str(e)}
        me_log.info(f"[治理] 运行开始完成: L0清理={report['l0_cleared']} 治理={report['govern']}")
        return report

    def on_run_end(self) -> dict:
        """每次运行结束：销毁 L0（readonly 不归档；readwrite 已按回合归档），并执行治理。"""
        report = {"l0_destroyed": False, "govern": None}
        try:
            self._drain_archives()   # 先等后台归档完成，再销毁 L0 与治理
        except Exception:
            pass
        if self.l0 is not None:
            if self.l0.size() > 0:
                self.l0.clear()
                report["l0_destroyed"] = True
        try:
            report["govern"] = self.govern()
        except Exception as e:
            report["govern"] = {"error": str(e)}
        me_log.info(f"[治理] 运行结束完成: L0销毁={report['l0_destroyed']} 治理={report['govern']}")
        return report

    # ---------------- 生命周期维护 ----------------
    def archive(self, now: float = None) -> dict:
        """季度滚动归档：把早于当前季度的 L2 数据迁至归档库。"""
        if not self._init_done:
            return {"error": "引擎未初始化"}
        return archive_ops.run_quarterly_archive(self.active, self.archive_db, now=now)

    # ---------------- 存储治理（防止无限消耗） ----------------
    def usage(self) -> dict:
        """各层空间统计（条数 / 磁盘占用 / 最旧记录时间）。"""
        if not self._init_done:
            return {"ready": False}
        return governance.usage(self)

    def govern(self, now: float = None, **overrides) -> dict:
        """一键治理：季度归档 + 配额降级（聚合/压缩）+ FTS 重建。

        overrides 支持 max_active / max_archive / raw_retention_quarters /
        archive_retention_quarters / aggregate_year_quarters / raw_compact_keep
        （0 表示不限制 / 不触发）。数据只降级保存，绝不直接删除。
        """
        if not self._init_done:
            return {"error": "引擎未初始化"}
        now = now if now is not None else time_utils.now_ts()
        report = {"archive": self.archive(now=now)}
        report["quotas"] = governance.enforce_quotas(
            self, now=now,
            max_active=overrides.get("max_active"),
            max_archive=overrides.get("max_archive"),
            raw_retention_quarters=overrides.get("raw_retention_quarters"),
            archive_retention_quarters=overrides.get("archive_retention_quarters"),
            aggregate_year_quarters=overrides.get("aggregate_year_quarters"),
            raw_compact_keep=overrides.get("raw_compact_keep"),
        )
        report["fts"] = self.rebuild_fts()
        me_log.info(f"[治理] govern 完成: 归档={report['archive'].get('moved', 0)} 迁入, "
                    f"季度聚合={report['quotas'].get('quarter_aggregated', 0)}, "
                    f"年度聚合={report['quotas'].get('year_aggregated', 0)}, "
                    f"原文压缩={report['quotas'].get('raw_compacted', 0)}, "
                    f"FTS={report['fts'].get('rows', 0)} 行")
        return report

    # ---------------- 整理 / 分类（预留接口） ----------------
    def store_record(self, record: dict, raw_text: str = "") -> dict:
        """存储：以结构化记录入库（预留接口）。

        record 支持 {id, year, quarter, ts, main_topic, sub_topic, participants,
        facts_per_role, full_summary}；缺失字段自动从 raw_text 提取。
        """
        from memory_engine.preprocessing import make_fragment
        if not record and not raw_text:
            return {"ok": False, "message": "记录内容为空"}
        text = raw_text or record.get("full_summary") or ""
        # 角色名占位符化（与 ingest 一致）：写入侧只存规范角色名
        from memory_engine import roles as _roles
        _cn = self.char_name
        text = _roles.to_stored_text(text, _cn)
        participants = [_roles.to_stored_text(p, _cn) for p in (record.get("participants") or [])]
        facts = {}
        for r, fd in (record.get("facts_per_role") or {}).items():
            d = fd.to_dict() if hasattr(fd, "to_dict") else dict(fd or {})
            facts[_roles.to_stored_text(r, _cn)] = {
                k: _roles.to_stored_text(str(v), _cn) for k, v in d.items()
            }
        frag = make_fragment(
            text,
            fragment_id=record.get("id"),
            ts=record.get("ts"),
            year=record.get("year"),
            quarter=record.get("quarter"),
            main_topic=record.get("main_topic"),
            sub_topic=record.get("sub_topic"),
            participants=participants,
            facts_per_role=facts,
            full_summary=_roles.to_stored_text(record.get("full_summary"), _cn),
        )
        l3_ref = self.cold.write(frag)
        self.active.insert(frag)
        from memory_engine.embedding import embed
        self.vector.add(frag.id, embed(frag.semantic_context))
        return {"ok": True, "fragment_id": frag.id, "l3_ref": l3_ref,
                "main_topic": frag.main_topic, "participants": frag.participants}

    def tidy_records(self, threshold: float = None, max_group: int = None) -> dict:
        """整理：检测并合并近似重复记录（预留接口）。"""
        if not self._init_done:
            return {"error": "引擎未初始化"}
        return governance.tidy(self, threshold=threshold, max_group=max_group)

    def classify_records(self) -> dict:
        """分类：重建 主题 / 季度 / 参与者 分类索引（预留接口）。"""
        if not self._init_done:
            return {"error": "引擎未初始化"}
        return governance.classify(self)

    def list_categories(self, cat_type: str = None, limit: int = 100) -> list:
        """分类清单（预留接口的查询侧）。"""
        if not self._init_done:
            return []
        out = self.active.list_categories(cat_type, limit)
        out += self.archive_db.list_categories(cat_type, limit)
        return out

    def records_in_category(self, cat_type: str, cat_value: str, limit: int = 50) -> list:
        """按分类取记录 id（预留接口的查询侧）。"""
        if not self._init_done:
            return []
        out = self.active.records_in_category(cat_type, cat_value, limit)
        out += self.archive_db.records_in_category(cat_type, cat_value, limit)
        return out

    def rebuild_fts(self) -> dict:
        """FTS5 索引重建（每日凌晨定时任务调用）。"""
        if not self._init_done:
            return {"error": "引擎未初始化"}
        n = self.active.rebuild_fts()
        try:
            self.archive_db.rebuild_fts()
        except Exception:
            pass
        try:
            with open(cfg.FTS_REBUILD_MARKER, "w", encoding="utf-8") as f:
                f.write(time_utils.year_quarter(time_utils.now_ts())[0].__str__() + "_" + str(int(time_utils.now_ts())))
        except Exception:
            pass
        self._fts_rebuilt_today = True
        return {"rebuilt": True, "rows": n}

    def maybe_rebuild_fts(self) -> bool:
        """每日首次调用时重建一次 FTS5（按标记文件判断）。"""
        if self._fts_rebuilt_today:
            return False
        try:
            if os.path.isfile(cfg.FTS_REBUILD_MARKER):
                with open(cfg.FTS_REBUILD_MARKER, "r", encoding="utf-8") as f:
                    marker = f.read().strip()
                import datetime
                today = datetime.date.today().isoformat()
                if marker.startswith(today):
                    self._fts_rebuilt_today = True
                    return False
        except Exception:
            pass
        self.rebuild_fts()
        return True

    def clear_all(self) -> None:
        """清空全部数据（活跃库 + 归档库 + 向量 + 缓存），仅供测试 / 管理使用。"""
        with self._lock:
            if self.active is not None:
                self.active.delete(self.active.ids_before(9999, "Q5"))
            if self.archive_db is not None:
                self.archive_db.delete(self.archive_db.ids_before(9999, "Q5"))
            if self.vector is not None:
                self.vector.clear()
            if self.l1 is not None:
                self.l1.clear()

    def initialize_memory(self) -> dict:
        """记忆初始化：删除全部记忆（L0/L1/L2 索引 + 分类索引 + L3 冷存储原文），不可恢复。

        供「上下文记忆库 → 记忆初始化」管理操作使用（前端二级确认后调用）。
        """
        if not self._init_done:
            return {"ok": False, "message": "引擎未初始化"}
        with self._lock:
            counts = {
                "active": self.active.count() if self.active else 0,
                "archive": self.archive_db.count() if self.archive_db else 0,
            }
            for db in (self.active, self.archive_db):
                if db is not None:
                    db.delete(db.ids_before(9999, "Q5"))
                    db.clear_categories()
            if self.vector is not None:
                self.vector.clear()
            if self.l1 is not None:
                self.l1.clear()
            if self.l0 is not None:
                self.l0.clear()
            if self.cold is not None:
                self.cold.clear()
            self._review_cache.clear()
            self._fts_rebuilt_today = False
        me_log.info(f"[初始化] 记忆库已全部清空（活跃 {counts['active']} 条 / 归档 {counts['archive']} 条 + 冷存储原文）")
        return {"ok": True, **counts}

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        if not self._init_done:
            return {"ready": False}
        try:
            return {
                "ready": True,
                "active_count": self.active.count(),
                "archive_count": self.archive_db.count(),
                "vector_count": self.vector.size(),
                "vector_backend": self.vector.backend(),
                "cache_size": self.l1.size(),
                "cache_stats": self.l1.stats(),
                "l0_size": self.l0.size() if self.l0 else 0,
                "l0_stats": self.l0.stats() if self.l0 else {},
                "context_mode": self._mode,
                "debug": me_log.is_debug(),
                "log_file": cfg.LOG_FILE,
                "l3_partitions": self.cold.partitions(),
                "fts_enabled": self.active.fts_enabled,
            }
        except Exception as e:
            return {"ready": False, "error": str(e)}


_engine = None
_engine_lock = threading.Lock()


def get_engine(data_dir: str = None) -> MemoryEngine:
    """获取进程内单例（重复调用返回同一实例；close 后可再次 init）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = MemoryEngine(data_dir=data_dir, auto_init=True)
    return _engine


def reset_engine() -> None:
    """仅供测试：销毁单例。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
        _engine = None
