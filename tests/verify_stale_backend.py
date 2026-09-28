# verify_stale_backend.py —— 验证「磁盘代码已更新，但端口上还挂着旧版本后台」时能自动换成新版本。
#
# 背景（真实踩到的问题）：修改代码后重新双击启动器，旧的后台进程还在 10999 上服务，
#   serve() 会直接复用旧进程 —— 界面与逻辑仍是旧的（GPT-SoVITS 依旧弹控制台、进度条没有数据），
#   用户看起来就像「改了没用」。
# 做法：
#   1) 构建标识：启动器本地算出的 build 必须和运行中服务 /api/status 返回的一致；
#   2) 旧版本（没有 build 字段，只有 /api/app/lifecycle）能被识别为「需要替换」；
#   3) 端口占用进程定位；只有确认「端口上就是本程序后台」时才结束它（否则一律不动）；
#   4) 启动器在 serve() 之前先做这个判断。
#   venv\Scripts\python.exe tests\verify_stale_backend.py
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
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


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        self.lines.append(("info", " ".join(str(x) for x in a)))

    def warn(self, *a):
        self.lines.append(("warn", " ".join(str(x) for x in a)))

    def error(self, *a):
        self.lines.append(("error", " ".join(str(x) for x in a)))


import launcher_gui  # noqa: E402
from web import server as web_server  # noqa: E402

# ==================== 1) 构建标识一致 + 旧版本识别 ====================
srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
try:
    with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
        status = json.loads(r.read().decode("utf-8"))
    server_build = status.get("build")
    check("运行中服务暴露了构建标识（/api/status.build）", bool(server_build), str(server_build))
    check("启动器本地算出的 build 与服务端一致（同一套算法）",
          launcher_gui._local_build() == server_build,
          f"{launcher_gui._local_build()} vs {server_build}")
    check("同版本后台被识别为「同版本」（不需要重启）",
          launcher_gui._running_build(base) == launcher_gui._local_build())
finally:
    srv.shutdown()

check("端口上什么都没有时返回 None（正常启动）",
      launcher_gui._running_build("http://127.0.0.1:1") is None)

src = open(os.path.join(ROOT, "launcher_gui.py"), encoding="utf-8").read()
check("启动器在 serve() 之前先比对构建标识、旧版本先结束",
      "_running_build(base_url)" in src and "if running != local:" in src
      and src.index("_running_build(base_url)") < src.index("serve(host, port"))
check("结束旧后台前先确认「端口上就是本程序后台」（trusted）",
      "trusted=True" in src and "def _stop_stale_backend(port, logger, trusted=False)" in src)

# 旧版本后台：只有 /api/app/lifecycle，没有 /api/status（读不到 build → 视为旧版本）
old_sock = socket.socket()
old_sock.bind(("127.0.0.1", 0))
old_port = old_sock.getsockname()[1]
old_sock.close()
OLD_SERVER = textwrap.dedent(f"""
    import http.server
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/app/lifecycle"):
                body = b'{{"ok": true}}'
                self.send_response(200)
            else:
                body = b'{{"ok": false}}'
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    http.server.ThreadingHTTPServer(("127.0.0.1", {old_port}), H).serve_forever()
""")
old_proc = subprocess.Popen([sys.executable, "-c", OLD_SERVER],
                            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                            | getattr(subprocess, "CREATE_NO_WINDOW", 0))
time.sleep(1.0)
try:
    got = launcher_gui._running_build(f"http://127.0.0.1:{old_port}")
    check("读不到 build 的旧后台返回空串（会被判定为旧版本，从而重启新版本）", got == "", repr(got))
finally:
    old_proc.terminate()
    time.sleep(0.3)

# ==================== 2) 端口占用进程定位 / 结束（替身进程真实验证） ====================
s = socket.socket()
s.bind(("127.0.0.1", 0))
hold_port = s.getsockname()[1]
s.close()

child_code = textwrap.dedent(f"""
    import socket, sys, time
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", {hold_port}))
    srv.listen(5)
    time.sleep(120)
""")
child = subprocess.Popen([sys.executable, "-c", child_code],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
time.sleep(1.2)

owner = launcher_gui._port_owner_pid(hold_port)
check("能定位监听端口的进程号（不依赖 Popen 句柄：venv 会再起真实解释器）",
      owner is not None and owner != os.getpid(), str(owner))

log = _Log()
refused = launcher_gui._stop_stale_backend(hold_port, log, trusted=False)
time.sleep(0.3)
check("未确认是本程序后台时不会结束任何进程（绝不误杀其它程序）",
      refused is False and launcher_gui._port_owner_pid(hold_port) is not None, str(log.lines[-1:]))

# 先确认本环境是否允许结束进程（部分受限环境会拒绝 taskkill，此时只能跳过该项）
probe_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                              | getattr(subprocess, "CREATE_NO_WINDOW", 0))
time.sleep(0.8)
can_kill, kill_msg = launcher_gui._kill_pid(probe_proc.pid)
time.sleep(0.4)
if not can_kill:
    print(f"[SKIP] 本环境不允许结束其它进程（taskkill 返回失败：{kill_msg[:80]}），"
          f"跳过「结束旧后台并释放端口」的实测（在普通 Windows 会话中该步骤有效）")
else:
    log2 = _Log()
    stopped = launcher_gui._stop_stale_backend(hold_port, log2, trusted=True)
    time.sleep(0.4)
    check("确认是本程序后台后能结束它并释放端口",
          stopped is True and launcher_gui._port_owner_pid(hold_port) is None, str(log2.lines[-1:]))
try:
    child.terminate()
except Exception:
    pass
try:
    probe_proc.terminate()
except Exception:
    pass

check("端口空闲时不会误判（返回 False 且不报错）",
      launcher_gui._stop_stale_backend(hold_port, _Log(), trusted=True) is False)

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("旧版本后台自动替换 验证全部通过。")
