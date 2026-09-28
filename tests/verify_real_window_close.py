# verify_real_window_close.py —— 真实场景验证：打开图形窗口 → 启动服务 → 关掉窗口 → 检查完整清理。
#
# 这就是「关掉图形化窗口」这个检查点的验收脚本：真的用 pythonw 启动图形启动器（等同双击 启动.vbs），
# 真的打开浏览器应用窗口，真的按 Lite 模式拉起 GPT-SoVITS，然后结束该窗口进程（＝点 X 关窗），
# 最后检查：后台退出、服务进程清零、端口释放、显存回落、日志写明完整清理过程。
#
# 注意：会在桌面上真实弹出一个应用窗口，并真实启动一次语音合成服务（随后自动清理）。
#   venv\Scripts\python.exe tests\verify_real_window_close.py
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "runtime", "logs", "xiaolongluo.log")
PYW = os.path.join(ROOT, "venv", "Scripts", "pythonw.exe")
sys.path.insert(0, ROOT)

from core import services  # noqa: E402

FAILS = []
OKS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def log_size():
    return os.path.getsize(LOG) if os.path.exists(LOG) else 0


def log_tail(since):
    if not os.path.exists(LOG):
        return ""
    with open(LOG, encoding="utf-8", errors="replace") as f:
        f.seek(since)
        return f.read()


def post(path, body=None, timeout=25):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request("http://127.0.0.1:10999" + path, data=data, method="POST",
                                headers={"X-XLLB-Client": "real-window-verify",
                                         "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def backend_alive():
    try:
        with urllib.request.urlopen("http://127.0.0.1:10999/api/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def app_windows():
    """应用窗口句柄（可见顶层窗口，标题含应用名）—— 用来真的「点 X 关窗」。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if "小笼洛包" in buf.value:
                    found.append(hwnd)
        return True

    user32.EnumWindows.argtypes = [cb_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(cb_type(_cb), 0)
    return found


def close_app_windows(timeout=12.0):
    """对应用窗口发 WM_CLOSE（等价于点标题栏的 X），必要时退回结束窗口进程。

    返回 (是否已全部关闭, 关闭前窗口数)。注意：启动出来的进程可能因为 Edge 把窗口
    交给「已有浏览器进程」而立刻退出，所以关窗必须按窗口本身来做。
    """
    import ctypes
    wins = app_windows()
    n_before = len(wins)
    if not wins:
        return False, 0
    for hwnd in wins:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
    end = time.time() + timeout
    while time.time() < end:
        if not app_windows():
            return True, n_before
        time.sleep(0.5)
    # 窗口不理会 WM_CLOSE（被脚本拦住 / 卡住）：退回结束窗口所属进程
    for hwnd in app_windows():
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            subprocess.run(["taskkill", "/PID", str(pid.value), "/F"], capture_output=True, timeout=20)
    end = time.time() + 8
    while time.time() < end:
        if not app_windows():
            return True, n_before
        time.sleep(0.5)
    return False, n_before


if backend_alive():
    print("检测到 10999 已有实例在运行：请先关闭它（或重启电脑后）再运行本脚本。")
    sys.exit(2)

print("=" * 68)
start_gpu = services._gpu_memory()          # 启动前的基线（机器上可能还有别的程序占显存）
print("前置：服务进程", services.list_service_processes(), "显存", start_gpu)
since = log_size()

print("\n1) 用 pythonw 启动图形启动器（等同双击 启动.vbs）")
subprocess.Popen([PYW, os.path.join(ROOT, "launcher_gui.py")], cwd=ROOT)

print("2) 等窗口打开并确认开始监视窗口进程（最多 40 秒）")
window_pid = None
end = time.time() + 40
while time.time() < end:
    m = re.search(r"已开始监视图形窗口进程（PID (\d+)）", log_tail(since))
    if m:
        window_pid = int(m.group(1))
        break
    time.sleep(1.0)
check("启动后确实开始监视图形窗口进程（关窗口检查点已就绪）", window_pid is not None,
      log_tail(since)[-200:])
if window_pid is None:
    print("\n没有拿到窗口进程号，后续步骤无法继续（窗口没打开？Edge/Chrome 是否可用？）")
    sys.exit(1)

print(f"   窗口进程 PID = {window_pid}")

print("3) 按 Lite 模式真正拉起服务")
try:
    post("/api/launch/start", {"mode": "lite"})
except Exception as e:
    print("   启动接口异常：", e)
time.sleep(12)
before_procs = services.list_service_processes()
before_gpu = services._gpu_memory()
check("服务真的被拉起来了（否则测试无意义）",
      any(p["kind"] == "gpt_sovits" for p in before_procs), str(before_procs))
check("窗口打开后程序没有误判「窗口已关闭」而自己退出（Edge 把窗口交给已有浏览器进程的场景）",
      backend_alive())

print("4) 关闭窗口（对应用窗口发 WM_CLOSE，等价于点 X 关窗）")
closed_ok, win_count = close_app_windows()
print(f"   关闭前应用窗口数 = {win_count}，已全部关闭 = {closed_ok}")
if not closed_ok:
    # 兜底：按启动进程结束（正常情况下上面就该成功）
    subprocess.run(["taskkill", "/PID", str(window_pid), "/F"], capture_output=True, text=True, timeout=20)

print("5) 等待完整清理（最多 45 秒）")
end = time.time() + 45
while time.time() < end and backend_alive():
    time.sleep(1.0)
time.sleep(3)
tail = log_tail(since)
after_procs = services.list_service_processes()
after_gpu = services._gpu_memory()

check("关窗口后后台进程自己退出了", not backend_alive())
check("关闭图形窗口（等价于点 X）成功", closed_ok, f"剩余窗口 {len(app_windows())}")
check("关窗口后没有 GPT-SoVITS / Ollama 残留", after_procs == [], str(after_procs))
check("关窗口后 GPT-SoVITS 端口已释放", services.port_available(services.gpt_sovits_port()) is True)
check("关窗口后显存回落到启动前水平（本程序加载的权重已释放）",
      bool(after_gpu) and bool(start_gpu) and after_gpu[0] <= start_gpu[0] + 400,
      f"启动前 {start_gpu} / 拉服务后 {before_gpu} / 关窗后 {after_gpu}")
check("日志写明「检测到图形窗口已关闭」", "检测到图形窗口已关闭" in tail)
check("日志写明「已启动独立清理进程」", "已启动独立清理进程" in tail)
check("日志写明卸载 / 停止 / 复核结果",
      any(k in tail for k in ("已卸载 Ollama 模型权重", "已停止本程序启动的 GPT-SoVITS",
                              "已复核：本地服务进程与端口均已清理")), tail[-300:])
check("日志写明「窗口已关闭，程序退出」", "窗口已关闭，程序退出" in tail)

print("\n本次运行日志摘录：")
for line in tail.splitlines():
    if any(k in line for k in ("监视", "关闭程序", "检测到图形窗口", "独立清理", "清理本地服务",
                               "窗口已关闭", "显存占用", "退出流程[window")):
        print("   ", line[:165])

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("真实关窗口 → 完整清理 验证通过。")
