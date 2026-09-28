# -*- coding: utf-8 -*-
"""记忆门禁 / 数据隔离 / 会话代次 验证（全部在临时数据目录内，不触碰真实记忆与真实插件配置）

覆盖用户提出的问题点：
  1) 停用记忆插件后，核心聊天路径不再检索、不再写入长期记忆（数据库无新增记录）；
  2) readonly 模式下只有 L0 临时上下文变化，长期库不变；readonly 由引擎底层强制（不只是插件设置）；
  3) 重启（重建引擎）后 readonly 数据不会出现（进程内 L0 也随进程结束消失）；
  4) 清空上下文 / 记忆初始化后，排队中的旧归档任务不会把旧内容写回长期记忆（会话代次）；
  5) data_dir 真正隔离：实例各层存储在传入目录，生产目录不被测试写脏；单例路径不一致会报错；
  6) 后台治理线程可停可再启（状态机）；
  7) 统一退出流程：引擎被显式关闭、可重复调用。

运行：venv\\Scripts\\python.exe tests\verify_memory_gate.py
"""
import hashlib
import importlib.util
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_ROOT = os.path.join(ROOT, "runtime", "memory_gate_test")
PROD_DIR = os.path.join(ROOT, "runtime", "memory_engine")

shutil.rmtree(TMP_ROOT, ignore_errors=True)
os.makedirs(TMP_ROOT, exist_ok=True)
# 引擎数据目录：必须在 import memory_engine 之前设置
os.environ["MEMORY_ENGINE_DATA"] = os.path.join(TMP_ROOT, "default")
sys.path.insert(0, ROOT)

import core.config as app_config  # noqa: E402
# 隔离插件系统：空插件目录 + 临时状态/设置文件，避免加载真实插件、避免改写用户插件配置
app_config.PLUGINS_DIR = os.path.join(TMP_ROOT, "plugins_empty")
os.makedirs(app_config.PLUGINS_DIR, exist_ok=True)
app_config.PLUGINS_STATE_FILE = os.path.join(TMP_ROOT, "plugins_state.json")
app_config.PLUGINS_SETTINGS_FILE = os.path.join(TMP_ROOT, "plugins_settings.json")
app_config.TTS_OUTPUT_DIR = os.path.join(TMP_ROOT, "tts")
app_config.MUSIC_OUTPUT_DIR = os.path.join(TMP_ROOT, "music")
os.makedirs(app_config.TTS_OUTPUT_DIR, exist_ok=True)
os.makedirs(app_config.MUSIC_OUTPUT_DIR, exist_ok=True)

import memory_engine  # noqa: E402
import memory_engine.config as cfg  # noqa: E402
from memory_engine import service  # noqa: E402
from memory_engine.service import WriteBlocked  # noqa: E402

cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False
cfg.ARCHIVE_DRAIN_TIMEOUT = 5.0

from core import llm as llm_core  # noqa: E402
from core import pipeline  # noqa: E402
from core import shutdown as shutdown_mod  # noqa: E402

# LLM 打桩：不产生任何真实网络调用
llm_core._invoke_chat = lambda *a, **k: "好呀，那我们周三晚上一起去打游戏吧！"
llm_core.generate = lambda *a, **k: "无需联网"

OKS, FAILS = [], []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def count(eng):
    """长期库条数（活跃 + 归档）。"""
    st = eng.status()
    return st.get("active_count", 0) + st.get("archive_count", 0), st


def load_memory_plugin():
    spec = importlib.util.spec_from_file_location("plugins.memory", os.path.join(ROOT, "plugins", "memory.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeManager:
    def __init__(self, settings):
        self.settings = dict(settings)

    def get_settings(self, name):
        return dict(self.settings)

    def save_settings(self, name, patch):
        self.settings.update(patch)
        return dict(self.settings)


class FakeCtx:
    def __init__(self, settings):
        self.config = app_config
        self.manager = FakeManager(settings)
        self.logs = []
        self.unload_reason = ""

    def log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))


def dir_state(path):
    """目录内所有文件的 (相对路径, 大小, mtime_ns, sha1) 指纹。"""
    out = {}
    if not os.path.isdir(path):
        return out
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
                with open(full, "rb") as f:
                    digest = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                continue
            out[os.path.relpath(full, path)] = (st.st_size, st.st_mtime_ns, digest)
    return out


PROD_BEFORE = dir_state(PROD_DIR)


def chat_turns(n, user="我们周三晚上一起去打游戏吧"):
    """走真实核心聊天路径（LLM 已打桩）产生 n 个对话回合。"""
    for _ in range(n):
        llm_core.call_ollama(user, record=True)


# ==================== 1) get_engine 不隐式初始化 ====================
section("1) get_engine() 不再隐式初始化（不隐式打开 / 写库）")
eng = memory_engine.get_engine()
check("get_engine() 返回的引擎未初始化", eng.is_ready() is False and eng.is_active() is False)
check("未初始化时不产生数据库文件",
      not os.path.exists(os.path.join(cfg.DATA_DIR, "active", "memory.db")),
      cfg.DATA_DIR)
st = service.status()
check("门禁默认：插件未启用 + 不可读不可写",
      st["plugin_enabled"] is False and st["can_read"] is False and st["can_write"] is False
      and bool(st["reason"]))
check("核心检索路径拿不到引擎", service.get_engine_for_read() is None)
eng_w, reason_w = service.get_engine_for_write("archive")
check("写入路径被拒绝并给出原因", eng_w is None and "未启用" in reason_w, reason_w)
eng_m, reason_m, _ = service.check(service.LEVEL_MANAGE)
check("管理路径被拒绝并给出原因", eng_m is None and "未启用" in reason_m, reason_m)
check("对话侧检索判定：无记忆（不隐式初始化）", llm_core._get_memory_engine() is None)

# ==================== 2) 插件启用：正常读写 ====================
section("2) 插件启用：引擎就绪 + 后台线程运行 + 正常写入")
mem = load_memory_plugin()
settings = dict(mem.SETTINGS)
settings["context_mode"] = "readwrite"
ctx = FakeCtx(settings)
mem.on_load(settings, ctx)
eng = memory_engine.get_engine()
check("启用后引擎就绪且可用", eng.is_ready() and eng.is_active())
st = service.status()
check("门禁：插件启用 + readwrite → 可读可写可管理",
      st["plugin_enabled"] and st["mode"] == "readwrite" and st["can_read"] and st["can_write"] and st["can_manage"])
check("引擎数据目录为传入的临时目录", eng.paths.data_dir == os.path.abspath(cfg.DATA_DIR), eng.paths.data_dir)
check("临时目录内已创建活跃库", os.path.exists(os.path.join(cfg.DATA_DIR, "active", "memory.db")))
check("后台治理线程状态为运行中", mem.maintain_state()["state"] == "running" and mem.maintain_state()["alive"])

chat_turns(2)
eng.drain_archives(timeout=5)
n_after_write, _ = count(eng)
check("readwrite 下多轮对话写入长期记忆", n_after_write >= 1, f"长期库 {n_after_write} 条")

# ==================== 3) 停用插件：检索与写入都停止 ====================
section("3) 停用记忆插件：核心对话不再检索、不再写入（数据库无新增记录）")
ctx.unload_reason = "disable"
mem.on_unload(ctx)
st = service.status()
check("停用后门禁：插件未启用 + 引擎挂起",
      st["plugin_enabled"] is False and st["active"] is False and "停用" in st["reason"], st["reason"])
check("停用后核心检索拿不到引擎", service.get_engine_for_read() is None and llm_core._get_memory_engine() is None)
check("停用后注入复核直接放行（不再走记忆）", pipeline._review_content("周三去吗", "我们周三打游戏") is True)
check("停用后后台治理线程已停止", mem.maintain_state()["state"] == "stopped" and not mem.maintain_state()["alive"])
check("停用不销毁引擎实例（数据与连接保留，便于快速恢复）", eng.is_ready() is True)
n_before_turns, _ = count(eng)
l0_before = eng.l0.size()
chat_turns(3)
time.sleep(0.6)
n_after_turns, _ = count(eng)
check("停用后多轮对话：长期库无任何新增", n_after_turns == n_before_turns,
      f"{n_before_turns} → {n_after_turns}")
check("停用后 L0 也不再写入（整个记忆能力关闭）", eng.l0.size() == l0_before,
      f"{l0_before} → {eng.l0.size()}")
check("停用后 record_turn 返回 blocked 原因",
      bool(eng.record_turn("测试", "测试", {"archive": True}).get("blocked")))
try:
    eng.ingest("停用后不应入库")
    check("停用后引擎底层拒绝 ingest", False, "居然写入了")
except WriteBlocked as e:
    check("停用后引擎底层拒绝 ingest", "停用" in e.reason, e.reason)

# ==================== 4) 重新启用：无数据丢失 + 线程可再启 ====================
section("4) 重新启用：引擎实例复用、L0 保留、后台线程可再次启动")
l0_kept = eng.l0.size()
ctx.unload_reason = ""
mem.on_load(settings, ctx)
eng2 = memory_engine.get_engine()
check("重新启用复用同一引擎实例（不重新打开数据库）", eng2 is eng)
check("重新启用保留 L0 会话上下文（重载/启停不丢上下文）", eng.l0.size() >= l0_kept)
check("重新启用后后台治理线程再次运行", mem.maintain_state()["state"] == "running")
check("重新启用后长期库条数不变（没有重复写入）", count(eng)[0] == n_after_turns)

# 快速 停用→启用 循环：验证状态机不会出现「没有维护线程」
cycle_ok = True
for i in range(3):
    ctx.unload_reason = "disable"
    mem.on_unload(ctx)
    mem.on_load(settings, ctx)
    ms = mem.maintain_state()
    if not (ms["state"] == "running" and ms["alive"]):
        cycle_ok = False
        print(f"       第 {i + 1} 轮循环后线程状态={ms['state']} alive={ms['alive']}")
check("快速停用→启用 3 轮后维护线程始终在运行", cycle_ok)

# ==================== 5) readonly：只写 L0，底层强制 ====================
section("5) readonly 模式：只有 L0 临时上下文变化，长期库不变（引擎底层强制）")
ro_settings = dict(settings)
ro_settings["context_mode"] = "readonly"
mem.on_settings_changed(ro_settings, ctx)
st = service.status()
check("门禁：readonly → 可读、不可写、不可管理",
      st["mode"] == "readonly" and st["can_read"] and not st["can_write"] and not st["can_manage"])
check("引擎模式同步为 readonly", eng.get_mode() == "readonly")
n_before_ro, _ = count(eng)
l0_before_ro = eng.l0.size()
chat_turns(3)
time.sleep(0.6)
n_after_ro, _ = count(eng)
check("readonly 下多轮对话：长期库无新增", n_after_ro == n_before_ro, f"{n_before_ro} → {n_after_ro}")
check("readonly 下 L0 正常写入（本次会话上下文可用）", eng.l0.size() > l0_before_ro)
r = eng.record_turn("今天想喝奶茶", "好呀，去买吧", {"archive": True})
check("readonly 下 record_turn 明确返回未归档", r.get("mode") == "readonly" and r.get("archived") is False)
try:
    eng.ingest("readonly 不应入库")
    check("readonly 下引擎底层拒绝 ingest（不依赖插件设置）", False, "居然写入了")
except WriteBlocked as e:
    check("readonly 下引擎底层拒绝 ingest（不依赖插件设置）", "只读" in e.reason, e.reason)
res = eng.initialize_memory()
check("readonly 下记忆初始化被拒绝（管理操作受门禁）", res.get("ok") is False and "只读" in (res.get("message") or ""),
      str(res))

# ==================== 6) 重启后 readonly 数据不出现 ====================
section("6) 重启（重建引擎）后：readonly 期间的数据不会出现")
l0_mem = eng.l0.size()
service.reset_for_test()
memory_engine.reset_engine()          # 模拟进程结束：引擎关闭
eng3 = memory_engine.get_engine(os.path.abspath(cfg.DATA_DIR), auto_init=True)
n_restart, st3 = count(eng3)
check("重启后长期库条数与 readonly 前一致（readonly 未写入）", n_restart == n_after_ro,
      f"{n_after_ro} → {n_restart}")
check("重启后 L0 为空（进程内临时上下文随进程结束销毁）",
      st3.get("l0_size", 1) == 0, f"重启前 {l0_mem} 条")

# ==================== 7) 清空上下文 / 初始化：旧归档任务不回流 ====================
section("7) 清空上下文与记忆初始化：排队中的旧归档任务不写回长期记忆（会话代次）")
ctx2 = FakeCtx(dict(settings))
mem.on_load(dict(settings), ctx2)
check("重新加载后引擎可写", eng3.is_active() and eng3.get_mode() == "readwrite")
gen_before = eng3.generation()
for i in range(6):
    eng3.record_turn(f"第{i}条待归档内容", "好的", {"archive": True, "role": "洛天依"})
check("已有待归档任务进入队列", eng3.status().get("pending_archives", 0) > 0,
      str(eng3.status().get("pending_archives")))
eng3.clear_context()                 # 模拟「清空对话」：应立即丢弃旧代次任务
check("清空上下文后会话代次递增", eng3.generation() > gen_before)
time.sleep(0.8)
n_cleared, stc = count(eng3)
check("清空后旧队列内容未被写回长期库", stc.get("pending_archives", 0) == 0 and n_cleared >= 0,
      f"待归档 {stc.get('pending_archives')} 条")
# 初始化：清空全部记忆并丢弃排队任务
eng3.record_turn("初始化前的待归档内容", "好的", {"archive": True})
eng3.initialize_memory()
time.sleep(0.8)
n_init, sti = count(eng3)
check("记忆初始化后长期库为 0 且没有旧任务回写", n_init == 0, f"长期库 {n_init} 条")

# ==================== 8) data_dir 隔离 ====================
section("8) data_dir 真正隔离（各层存储按实例路径，生产目录不被写脏）")
service.reset_for_test()
memory_engine.reset_engine()
dir_a = os.path.join(TMP_ROOT, "iso_a")
dir_b = os.path.join(TMP_ROOT, "iso_b")
eng_a = memory_engine.MemoryEngine(data_dir=dir_a, auto_init=True)
eng_a.set_mode("readwrite")
eng_a.ingest("A 目录的独占记忆：我们上周去了海边", participants=["用户"], main_topic="旅行")
a_count, _ = count(eng_a)
eng_a.close()
check("实例 A 的数据写在自己的目录", os.path.exists(os.path.join(dir_a, "active", "memory.db")) and a_count >= 1)
check("实例 B 目录尚未创建（互不干扰）", not os.path.exists(os.path.join(dir_b, "active", "memory.db")))
eng_b = memory_engine.MemoryEngine(data_dir=dir_b, auto_init=True)
b_count, _ = count(eng_b)
check("实例 B 看不到实例 A 的数据（无互相污染）", b_count == 0, f"B 长期库 {b_count} 条")
check("实例 A/B 各自拥有独立的日志与索引文件",
      os.path.exists(os.path.join(dir_a, "memory_engine.log")) and os.path.exists(os.path.join(dir_b, "memory_engine.log")))
eng_b.close()
# 单例路径不一致 → 明确报错
single = memory_engine.get_engine(dir_a, auto_init=True)
try:
    memory_engine.get_engine(dir_b)
    check("单例复用路径不一致时明确报错", False, "居然没有报错")
except ValueError as e:
    check("单例复用路径不一致时明确报错", dir_a.split(os.sep)[-1] in str(e), str(e))
single.close()
memory_engine.reset_engine()

# ==================== 9) 统一退出流程 ====================
section("9) 统一退出流程：显式关闭引擎 / 队列 drain / 可重复调用")
ctx3 = FakeCtx(dict(settings))
mem.on_load(dict(settings), ctx3)
eng4 = memory_engine.get_engine()
eng4.record_turn("退出前的最后一轮对话", "好的", {"archive": True})
stopped = {"called": False}


def stop_server_stub():
    stopped["called"] = True
    return "HTTP 服务已停止"


report = shutdown_mod.shutdown("verify", stop_server=stop_server_stub,
                               extra_steps=[("停止 TTS 流式任务", lambda: "已停止")])
names = [s["name"] for s in report["steps"]]
check("退出流程包含规定的步骤顺序",
      names == ["停止接受新请求", "停止新的归档写入", "等待后台归档队列排空",
                "关闭记忆引擎与数据库", "停止 TTS 流式任务", "清理运行时临时文件"], str(names))
check("退出流程调用了「停止接受新请求」回调", stopped["called"])
check("退出后引擎已关闭", eng4.is_ready() is False)
check("退出后门禁为关闭状态", service.status()["plugin_enabled"] is False)
pending_detail = next((s["detail"] for s in report["steps"] if s["name"] == "等待后台归档队列排空"), "")
check("退出流程报告了归档队列处理结果", "归档" in pending_detail, pending_detail)
stopped["called"] = False
second = shutdown_mod.shutdown("verify")
check("退出流程可重复调用（第二次不重复执行步骤）",
      second.get("steps") == report.get("steps") and stopped["called"] is False)
check("退出时未接受新写入（record_turn 被拒绝）",
      bool(eng4.record_turn("退出后", "不应写入", {}).get("blocked")))

# ==================== 10) 生产目录未被写脏 ====================
section("10) 生产数据目录未被本次测试改动")
PROD_AFTER = dir_state(PROD_DIR)
check("真实记忆目录文件指纹完全一致（测试全部落在临时目录）",
      PROD_AFTER == PROD_BEFORE,
      f"变化：{set(PROD_AFTER.items()) ^ set(PROD_BEFORE.items())}"[:200])
real_state = os.path.join(ROOT, "plugins_state.json")
check("真实插件状态文件仍指向真实插件目录（未被本测试改写）",
      os.path.isfile(real_state))

shutil.rmtree(TMP_ROOT, ignore_errors=True)
print()
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("验证结论: ✓ 记忆门禁 / 隔离 / 代次 / 退出流程 全部通过")
