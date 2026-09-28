# verify_app_close.py —— 验证「关闭窗口 = 完全关闭程序」（图形启动器模式）。
#
# 需求：点窗口右上角 X 时，不能只关掉显示界面，必须完整退出（停止本地服务并结束进程）。
# 做法：
#   1) 服务端生命周期：心跳 / 关闭信标 / 宽限期看门狗（用替身退出动作，不会真的把测试进程关掉）；
#   2) 页面之间跳转、刷新会立刻重新心跳，不会被误判成关闭；
#   3) 只在「图形启动器拉起的实例」里启用（app.py / 控制台版不会自动退出）；
#   4) 退出动作包含「停止本地服务（GPT-SoVITS / Ollama）」，且只停自己启动的进程；
#   5) 各页面都引入了心跳脚本。
#   venv\Scripts\python.exe tests\verify_app_close.py
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
OKS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ==================== 1) 页面接线 ====================
life_js = read(os.path.join(ROOT, "web", "static", "app_lifecycle.js"))
check("存在共用的窗口生命周期脚本（心跳 + 关闭信标）",
      "/api/app/beat" in life_js and "/api/app/window-closed" in life_js)
check("只在真正关闭时上报（pagehide 且非 bfcache 恢复）",
      'addEventListener("pagehide"' in life_js and "e.persisted" in life_js)
check("页面恢复（pageshow）/ 显隐切换也会补心跳（避免误判）",
      'addEventListener("pageshow"' in life_js and "visibilitychange" in life_js)
for page in ("index.html", "settings.html", "launcher.html"):
    html = read(os.path.join(ROOT, "web", page))
    check(f"{page} 引入了窗口生命周期脚本", "/static/app_lifecycle.js" in html)

server_src = read(os.path.join(ROOT, "web", "server.py"))
check("只有图形启动器模式才启用「关窗口即退出」（调试用 app.py 不受影响）",
      'getattr(config, "LAUNCH_VIA_GUI", False)' in server_src and "app_lifecycle.enable(" in server_src)
check("正常退出流程也包含「停止本地服务（GPT-SoVITS / Ollama）」",
      "停止本地服务（GPT-SoVITS / Ollama）" in server_src)

# ==================== 2) 生命周期状态机 ====================
from core import services  # noqa: E402
from web import app_lifecycle as life  # noqa: E402
from web import server as web_server  # noqa: E402

life.reset_for_test()
calls = []
life.set_shutdown_hook(lambda reason: calls.append(reason))
# 缩短判定时间，测试不需要等 8 秒
life.CLOSE_GRACE_SECONDS = 1.2
life.TICK_SECONDS = 0.15
life.enable(True)

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def post(path):
    req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                 headers={"X-XLLB-Client": "close-verify",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


try:
    status, data = post("/api/app/beat")
    st = data.get("lifecycle") or {}
    check("心跳接口可用并记录「窗口开着」",
          status == 200 and st.get("seen_ever") is True and st.get("beats", 0) >= 1, str(st)[:120])

    # 页面跳转：先收到关闭信标，紧接着新页面心跳 → 不能退出
    post("/api/app/window-closed")
    time.sleep(0.4)
    post("/api/app/beat")
    time.sleep(1.6)
    check("页面之间跳转/刷新（信标后立刻有心跳）不会被误判为关闭",
          calls == [] and (get("/api/app/lifecycle")["lifecycle"].get("shutdown_called") is False),
          str(calls))

    # 真正关闭：信标之后不再有心跳 → 宽限期后执行退出
    post("/api/app/window-closed")
    deadline = time.time() + 6
    while time.time() < deadline and not calls:
        time.sleep(0.15)
    check("窗口真正关闭后（宽限期内无心跳）执行完整退出", calls == ["window-closed"], str(calls))
    st = get("/api/app/lifecycle")["lifecycle"]
    check("退出只执行一次（重复信标不会再触发）", st.get("shutdown_called") is True, str(st)[:120])

    post("/api/app/window-closed")
    time.sleep(1.5)
    check("退出动作不会重复执行", calls == ["window-closed"], str(calls))
finally:
    life.set_shutdown_hook(None)
    life.reset_for_test()
    srv.shutdown()

# ==================== 2.5) 兜底：完全没有动静（旧版页面 / 浏览器被强杀）也会退出 ====================
life.reset_for_test()
idle_calls = []
life.set_shutdown_hook(lambda reason: idle_calls.append(reason))
life.CLOSE_GRACE_SECONDS = 1.0
life.IDLE_SECONDS = 1.5
life.TICK_SECONDS = 0.15
life.enable(True)
life.beat()                     # 窗口连上来过一次
deadline = time.time() + 6
while time.time() < deadline and not idle_calls:
    time.sleep(0.15)
check("长时间没有任何请求（旧页面直接关掉）也会兜底完整退出",
      idle_calls == ["window-idle"], str(idle_calls))
life.reset_for_test()

# 有请求持续进来时不会误退出（界面开着就不该关）
life.reset_for_test()
alive_calls = []
life.set_shutdown_hook(lambda reason: alive_calls.append(reason))
life.CLOSE_GRACE_SECONDS = 1.0
life.IDLE_SECONDS = 1.5
life.TICK_SECONDS = 0.15
life.enable(True)
for _ in range(14):             # 约 2.1 秒内持续有请求
    life.touch()
    time.sleep(0.15)
check("界面开着（持续有请求）时不会误判退出", alive_calls == [], str(alive_calls))
life.set_shutdown_hook(None)
life.reset_for_test()

# ==================== 2.8) 把关窗口当检查点：窗口进程退出即清理（不依赖页面脚本） ====================
launcher_gui_src = read(os.path.join(ROOT, "launcher_gui.py"))
launcher_src = read(os.path.join(ROOT, "launcher.py"))
check("打开窗口时会带上独立配置目录（保证窗口进程可被跟踪）",
      "profile_dir=" in launcher_gui_src and "--user-data-dir=" in launcher_src
      and "edge-app-profile" in launcher_gui_src)
check("启动后会监视窗口进程，窗口关闭即完整清理",
      "def _watch_window_process" in launcher_gui_src
      and 'shutdown_now("window-process-exit")' in launcher_gui_src
      and "_watch_window_process" in launcher_gui_src)
# Edge 在「同一配置目录已有浏览器进程」时会把窗口交给它：启动出来的进程立刻退出，但窗口开着。
# 只按进程号判定就会出现「窗口刚打开程序就自己退出」，所以必须按窗口是否存在判定，并兜住这种交接。
check("关窗判定看「窗口是否还在」（不是只看启动进程退没退）",
      "def _app_window_count" in launcher_gui_src
      and "EnumWindows" in launcher_gui_src
      and "_app_window_count()" in launcher_gui_src
      and "WINDOW_GONE_CHECKS" in launcher_gui_src)
check("窗口被交给已有浏览器进程时不会误判关窗（页面还在心跳就交给信标/空闲兜底）",
      "def _backend_active_recently" in launcher_gui_src
      and "窗口进程已交给已有浏览器进程" in launcher_gui_src)

import importlib  # noqa: E402
launcher_gui = importlib.import_module("launcher_gui")


class _FakeProc:
    pid = 4242

    def wait(self):
        return 0


class _FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        self.lines.append("info " + " ".join(str(x) for x in a))

    def warn(self, *a):
        self.lines.append("warn " + " ".join(str(x) for x in a))

    def error(self, *a):
        self.lines.append("error " + " ".join(str(x) for x in a))


life.reset_for_test()
proc_calls = []
life.set_shutdown_hook(lambda reason: proc_calls.append(reason))
life.enable(True)
log = _FakeLogger()
_orig_count = launcher_gui._app_window_count
_orig_active = launcher_gui._backend_active_recently
_seq = {"n": [1, 0, 0, 0]}          # 先看到窗口，之后窗口消失

launcher_gui._app_window_count = lambda *a, **k: (_seq["n"].pop(0) if _seq["n"] else 0)
launcher_gui._watch_window_process(_FakeProc(), log)
check("窗口消失即执行完整清理（reason=window-process-exit）",
      proc_calls == ["window-process-exit"], str(proc_calls) + " " + str(log.lines[-2:]))
check("窗口关闭的日志里写明了检查点",
      any("图形窗口已关闭" in line for line in log.lines), str(log.lines[-3:]))

# 关键回归：Edge 把窗口交给「已有的浏览器进程」时，启动进程立刻退出，但窗口还在 —— 不能当成关窗。
life.reset_for_test()
proc_calls_handoff = []
life.set_shutdown_hook(lambda reason: proc_calls_handoff.append(reason))
life.enable(True)
log_handoff = _FakeLogger()
launcher_gui._app_window_count = lambda *a, **k: -1            # 枚举不到窗口 → 退回按进程判定
launcher_gui._backend_active_recently = lambda seconds=1.5: True
launcher_gui._watch_window_process(_FakeProc(), log_handoff)
check("窗口进程退出但页面仍在心跳（＝窗口被交接）时不清理退出",
      proc_calls_handoff == [], str(proc_calls_handoff) + " " + str(log_handoff.lines[-2:]))
check("这种情况会写明「已交给已有浏览器进程」",
      any("交给已有浏览器进程" in line for line in log_handoff.lines), str(log_handoff.lines[-3:]))

# 反过来：进程退出且页面没有动静 → 仍然按关窗处理
life.reset_for_test()
proc_calls_plain = []
life.set_shutdown_hook(lambda reason: proc_calls_plain.append(reason))
life.enable(True)
launcher_gui._backend_active_recently = lambda seconds=1.5: False
launcher_gui._watch_window_process(_FakeProc(), _FakeLogger())
launcher_gui._app_window_count = _orig_count
launcher_gui._backend_active_recently = _orig_active
check("窗口进程退出且页面无心跳时照常清理退出",
      proc_calls_plain == ["window-process-exit"], str(proc_calls_plain))

# 已经在退出流程里时不会重复触发
life.reset_for_test()
proc_calls2 = []
life.set_shutdown_hook(lambda reason: proc_calls2.append(reason))
life.enable(True)
with life._lock:
    life._state["shutdown_called"] = True
launcher_gui._watch_window_process(_FakeProc(), _FakeLogger())
check("已经退出中时不再重复触发清理", proc_calls2 == [], str(proc_calls2))
life.set_shutdown_hook(None)
life.reset_for_test()

# ==================== 3) 退出时停止本地服务（只停自己启动的） ====================
check("services.stop_all 存在（退出流程用它停掉本地服务）", callable(getattr(services, "stop_all", None)))
check("未启动过服务时 stop_all 安全返回", isinstance(services.stop_all(), str))

# 用替身进程验证 stop_ollama 只停「本程序启动的」进程
dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
with services._OLLAMA_LOCK:
    services._OLLAMA_STATE["process"] = dummy
stopped = services.stop_ollama(timeout=5)
time.sleep(0.4)
check("stop_ollama 会结束本程序启动的 Ollama 进程", stopped is True and dummy.poll() is not None,
      f"stopped={stopped} poll={dummy.poll()}")
check("再次调用不会误报（进程已清理）", services.stop_ollama() is False)
with services._OLLAMA_LOCK:
    services._OLLAMA_STATE["process"] = None

# ==================== 4) 退出时清理「遗留的」服务进程 ====================
check("stop_all 支持清理上一次运行遗留的服务进程",
      "include_orphans" in read(os.path.join(ROOT, "core", "services.py"))
      and callable(getattr(services, "_kill_orphan_services", None))
      and callable(getattr(services, "_list_processes", None)))
orphan_src = read(os.path.join(ROOT, "core", "services.py"))
check("清理范围只限「项目内 GPT-SoVITS 解释器 + ollama 进程」，不会泛杀 Python",
      "def _is_our_gpt_sovits" in orphan_src and "def _is_ollama_process" in orphan_src
      and 'startswith(gs_dir)' in orphan_src
      and '"ollama app.exe"' in orphan_src and '"ollama_llama_server.exe"' in orphan_src
      and 'p["pid"] == me' in orphan_src)
check("stop_all 汇总结果里会写明停掉了哪些服务（含权重释放与显存对比）",
      "权重随进程退出释放" in orphan_src and "显存占用：" in orphan_src
      and "没有需要停止的本地服务" in orphan_src)

# ==================== 5) 关闭方式：无二级确认；跳转不算关闭；请求也算存活 ====================
settings_js = read(os.path.join(ROOT, "web", "static", "settings.js"))
css = read(os.path.join(ROOT, "web", "static", "style.css"))
app_js = read(os.path.join(ROOT, "web", "static", "app.js"))
life_src = read(os.path.join(ROOT, "web", "app_lifecycle.py"))
check("设置页里不再放置「完全退出」按钮",
      "完全退出" not in settings_js and "app-exiting" not in settings_js and ".app-exiting" not in css)
check("关闭时不再有二级确认（点 X 直接关闭）",
      "beforeunload" not in life_js and "XLLB_CLOSE_GUARD" not in life_js
      and "XLLB_CLOSE_GUARD" not in app_js and "XLLB_CLOSE_GUARD" not in settings_js)
check("页面内跳转会明确告知服务端（不算关闭）：设置链接 / 返回对话 / 启动页进入界面",
      "window.XLLB_NAV" in life_js and "XLLB_NAV.allow()" in app_js
      and "XLLB_NAV" in read(os.path.join(ROOT, "web", "settings.html"))
      and "XLLB_NAV.allow()" in read(os.path.join(ROOT, "web", "static", "launcher.js")))
check("跳转时不发「窗口已关闭」信标（只补一次心跳）",
      "if (navAllowed) { beat(); return; }" in life_js)
check("任何 HTTP 请求都算「窗口还活着」（旧页面关掉后也能被兜底清理）",
      "def touch()" in life_src)
check("服务端提供 touch()，并在请求分发里调用",
      "def touch()" in life_src and "app_lifecycle.touch()" in server_src)
check("存活判定同时看「心跳」和「请求」（取较新的那个）",
      'max(_state["last_beat"], _state["last_request"])' in life_src)
check("长时间毫无动静会兜底退出（旧版页面 / 浏览器被强杀）",
      '"window-idle"' in life_src and "IDLE_SECONDS" in life_src)
check("关闭宽限期足够短（关掉窗口后很快开始退出）", "CLOSE_GRACE_SECONDS = 2.0" in life_src)
check("没有心跳/请求时的兜底超时也足够短（最多一分半）",
      "IDLE_SECONDS = 90.0" in life_src)
check("整机睡眠 / 唤醒不会被误判成「窗口关闭」",
      "_suspend_gap_detected" in life_src and "QueryUnbiasedInterruptTime" in life_src
      and "GetTickCount64" in life_src)
_st = (life._SUSPEND.get("gap") or 0.0) - 100        # 模拟刚睡醒（计时器差值突然变大）
life._SUSPEND["gap"] = _st
check("睡眠恢复检测可用（差值突变时能识别出来）", life._suspend_gap_detected() is True)
life._SUSPEND["gap"] = None
check("清理跑在独立进程里（主进程中途退出也不会漏清）",
      "def _spawn_cleanup_helper" in life_src and "subprocess.Popen([sys.executable" in life_src
      and "独立清理进程" in life_src)
check("独立清理进程完成后主进程才继续收尾（限时等待 + 超时继续）",
      "helper.wait(timeout=CLEANUP_JOIN_SECONDS)" in life_src
      and "它会在后台继续完成清理" in life_src)
check("关闭监听 socket 引发的 OSError 被当作正常收尾（不会把清理一起带走）",
      "shutdown_mod.is_running()" in server_src and "def is_running" in read(os.path.join(ROOT, "core", "shutdown.py")))
check("打开窗口/监视失败不会让后台进程崩掉",
      "打开窗口失败" in launcher_gui_src and "整段都包起来" in launcher_gui_src)

# 静态检查：函数里用到的模块级名字必须真的导入过（曾经因为 launcher_gui.py 忘了 import threading，
# 监视窗口进程的线程根本没启动 → 关窗口不清理，只在日志里留下一行 “name 'threading' is not defined”）
import builtins as _builtins  # noqa: E402
import symtable  # noqa: E402

_missing_names = []
for _path in ("launcher_gui.py", "launcher.py"):
    _src = read(os.path.join(ROOT, _path))
    _st = symtable.symtable(_src, _path, "exec")
    _module_names = {s.get_name() for s in _st.get_symbols()}

    def _scan(table, path=_path):
        for child in table.get_children():
            for sym in child.get_symbols():
                name = sym.get_name()
                if (sym.is_global() and not sym.is_assigned()
                        and name not in _module_names and not hasattr(_builtins, name)):
                    _missing_names.append(f"{path}:{child.get_name()}:{name}")
            _scan(child)

    _scan(_st)
check("启动器里没有「用了但没导入」的模块名（正是关窗口不清理的根因）",
      not _missing_names, str(sorted(set(_missing_names))[:6]))
check("服务端退出接口保留（供脚本 / 自动化调用，界面上不暴露按钮）",
      'CONFIRM_APP_SHUTDOWN = "app.shutdown"' in server_src
      and "_require_confirm(req, CONFIRM_APP_SHUTDOWN" in server_src)

# 接口行为：无令牌 409；带令牌后执行完整退出（用替身退出动作，不会真的退出测试进程）
life.reset_for_test()
calls2 = []
life.set_shutdown_hook(lambda reason: calls2.append(reason))
life.CLOSE_GRACE_SECONDS = 1.2
life.TICK_SECONDS = 0.15
life.enable(True)

srv2 = web_server.create_server("127.0.0.1", 0)
port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
base2 = f"http://127.0.0.1:{port2}"


def post2(path, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(base2 + path, data=data, method="POST",
                                 headers={"X-XLLB-Client": "close-verify",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


try:
    status, data = post2("/api/app/shutdown")
    check("没有确认令牌时退出接口被拒（409 + need_confirm）",
          status == 409 and data.get("need_confirm") is True, f"status={status} {str(data)[:100]}")

    from web import confirm as confirm_mod
    token = confirm_mod.issue("app.shutdown", target="*", client="close-verify")
    status, data = post2("/api/app/shutdown", {"token": token})
    check("带确认令牌后开始退出并返回清理步骤",
          status == 200 and data.get("ok") and len(data.get("steps") or []) >= 4, str(data)[:140])
    deadline = time.time() + 5
    while time.time() < deadline and not calls2:
        time.sleep(0.1)
    check("确认后真的执行了完整退出（停止服务 + 清理 + 结束进程）",
          calls2 == ["user-confirm"], str(calls2))
finally:
    life.set_shutdown_hook(None)
    life.reset_for_test()
    srv2.shutdown()

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("关闭窗口即完全退出 验证全部通过。")
