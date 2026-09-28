# verify_gui_launcher.py —— 验证「完全图形化启动程序」：启动页 + 模式选择 + 拉起服务 + 进入界面。
#
# 需求：不启动命令行，进入程序后直接打开图形界面；界面提供 Lite / 标准 两个选项，
#       点击后按所选模式拉起服务，再进入正式页面。
# 做法：
#   1) 静态检查：启动页 / 启动脚本 / 入口脚本的关键约定（无控制台、两处入口、进度轮询）；
#   2) 接口检查：/api/launch/state 返回模式清单，/api/launch/start 校验模式并驱动步骤；
#   3) 用替身服务后端跑完整流程，确认 Lite 只起语音合成、标准会起全部服务（不会真的启动外部进程）。
#   venv\Scripts\python.exe tests\verify_gui_launcher.py
import json
import os
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


def read(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding) as f:
        return f.read()


# ==================== 1. 入口与脚本（不出现命令行窗口） ====================
gui_py = os.path.join(ROOT, "launcher_gui.py")
vbs = os.path.join(ROOT, "启动.vbs")
bat = os.path.join(ROOT, "start.bat")
html = os.path.join(ROOT, "web", "launcher.html")
js = os.path.join(ROOT, "web", "static", "launcher.js")

check("图形启动器入口 launcher_gui.py 存在", os.path.exists(gui_py))
check("启动页 web/launcher.html 存在", os.path.exists(html))
check("启动页脚本 web/static/launcher.js 存在", os.path.exists(js))

gui_src = read(gui_py)
check("图形启动器用 web 服务提供启动页（不依赖控制台输入）",
      'serve(' in gui_src and "/launcher.html" in gui_src)
check("每次启动都打开启动页让人重新选择模式（不再沿用上次选择）",
      "_probe_launched" not in gui_src and 'url + "/launcher.html"' in gui_src
      and '"/api/launch/state"' in gui_src)
check("启动页在「已有服务在运行」时仍然显示两个选项并提示可重选",
      "已有服务在运行，可重新选择启动模式" in read(js) and "if (st.starting)" in read(js))
check("pythonw 无控制台：输出重定向到日志文件",
      "launcher_gui.log" in gui_src and "stdout" in gui_src and "stderr" in gui_src)
check("预检缺组件时不退出（窗口照开，由启动页显示原因）",
      "check_components" in gui_src and "sys.exit" not in gui_src)

check("无窗口启动脚本 启动.vbs 存在", os.path.exists(vbs))
with open(vbs, "rb") as f:
    vbs_bytes = f.read()
check("启动.vbs 为 UTF-16（wscript 中文不乱码）", vbs_bytes[:2] == b"\xff\xfe")
vbs_text = vbs_bytes.decode("utf-16")
check("启动.vbs 用 pythonw + 隐藏窗口(0) 启动图形启动器",
      "pythonw.exe" in vbs_text and "launcher_gui.py" in vbs_text
      and ", 0, False" in vbs_text)
check("启动.vbs 缺少 venv 时给出图形提示（不是命令行报错）",
      "MsgBox" in vbs_text and "install.bat" in vbs_text)

bat_text = read(bat)
check("start.bat 改为拉起图形启动器（pythonw，不再前台常驻控制台）",
      "pythonw.exe" in bat_text and "launcher_gui.py" in bat_text
      and 'start "" "%PYW%"' in bat_text and "exit /b 0" in bat_text
      and '"%PY%" launcher.py' not in bat_text)

# ==================== 2. 启动页（两个选项 = Lite / 标准） ====================
html_src = read(html)
check("启动页标题与图标齐全（窗口标题不带版本号）",
      "<title>小笼洛包</title>" in html_src and "app-icon.png" in html_src)
check("启动页包含模式选择容器与进度容器",
      'id="launch-modes"' in html_src and 'id="launch-progress"' in html_src)
check("启动页不写死模式文案（由后端模式清单渲染）",
      "Lite" not in html_src and "标准" not in html_src)

js_src = read(js)
check("启动页点击选项即调用 /api/launch/start（按选项拉起服务）",
      '"/api/launch/start"' in js_src and 'body: { mode }' in js_src)
check("启动页轮询 /api/launch/state 显示每个步骤状态",
      '"/api/launch/state"' in js_src and "POLL_MS" in js_src and "paint(" in js_src)
check("服务拉起后在同一窗口进入正式界面",
      'window.location.href = "/"' in js_src and "enterApp" in js_src)
check("启动失败时给出可操作提示（进入界面重试 / 看日志）",
      "launch-note bad" in js_src and "进入界面" in js_src)

# ==================== 3. 接口 + 完整流程（替身服务，不真的启动外部进程） ====================
import core.config as config  # noqa: E402
from core import launch_flow  # noqa: E402
from web import server as web_server  # noqa: E402


class FakeServices:
    """替身：记录被调用的启动动作，不启动任何真实进程。"""

    def __init__(self):
        self.calls = []
        self.ready = False

    def load_launcher_config(self):
        self.calls.append("load_launcher_config")
        return {"web": {"host": "127.0.0.1", "port": 10999},
                "gpt_sovits": {"enabled": True, "api_url": "http://127.0.0.1:20000",
                               "port": 20000, "start_timeout": 1}}

    def apply_api_urls(self, cfg):
        self.calls.append("apply_api_urls")

    def start_gpt_sovits(self, cfg):
        self.calls.append("start_gpt_sovits")
        return True, {"reason": "started", "port": 20000}

    def wait_ready(self, checker, timeout, interval=1.0, label=""):
        self.calls.append("wait_ready")

    def check_gpt_sovits(self, *a, **k):
        return True

    def gpt_sovits_status(self, *a, **k):
        return {"ready": True, "running": True, "port": 20000, "url": "http://127.0.0.1:20000",
                "last_error": ""}

    def start_all(self):
        self.calls.append("start_all")


class FakeModels:
    def __init__(self):
        self.calls = []

    def init_models(self):
        self.calls.append("init_models")


fake_svc = FakeServices()
fake_mdl = FakeModels()
launch_flow.set_backends(fake_svc, fake_mdl)
launch_flow.reset()

# 记录并恢复被启动流程改动的全局配置
keep = {k: getattr(config, k) for k in
        ("APP_MODE", "LLM_CHAT_BACKEND", "LLM_JUDGE_BACKEND", "LLM_CHAT_MODEL")}

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def req(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                              headers={"X-XLLB-Client": "launch-verify", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def wait_done(timeout=8.0):
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = json.loads(req("/api/launch/state")[1].decode("utf-8")).get("state") or {}
        if st.get("done") and not st.get("starting"):
            return st
        time.sleep(0.2)
    return st


try:
    status, body, _ = req("/launcher.html")
    text = body.decode("utf-8")
    check("GET /launcher.html 可访问且注入背景主题",
          status == 200 and 'id="launch-modes"' in text and "__THEME__" in text)

    status, body, _ = req("/static/launcher.js")
    check("GET /static/launcher.js 可访问", status == 200 and b"/api/launch/start" in body)

    # 图形启动器拉起且未选模式时，主页面(/ )先显示启动页；选完模式后回到对话界面
    config.LAUNCH_VIA_GUI = True
    launch_flow.reset()
    status, body, _ = req("/")
    check("未选模式时访问主页面会先看到启动页（不会落到没有服务的对话界面）",
          status == 200 and 'id="launch-modes"' in body.decode("utf-8"))
    with launch_flow._lock:
        launch_flow._state["done"] = True
    status, body, _ = req("/")
    check("选完模式后主页面恢复为对话界面",
          status == 200 and 'id="chat"' in body.decode("utf-8"))
    launch_flow.reset()
    config.LAUNCH_VIA_GUI = False
    status, body, _ = req("/")
    check("非图形启动器（app.py / 控制台版）访问主页面仍是对话界面",
          status == 200 and 'id="chat"' in body.decode("utf-8"))

    data = json.loads(req("/api/launch/state")[1].decode("utf-8"))
    ids = [m["id"] for m in data.get("modes") or []]
    check("模式清单正好是 Lite 与 标准（lite / standard）", ids == ["lite", "standard"], str(ids))
    check("每个模式都带界面所需文案（名称 / 说明 / 适用场景）",
          all(m.get("label") and m.get("desc") and m.get("need") for m in data["modes"]))
    check("未启动时状态为空（mode='' / done=False）",
          (data.get("state") or {}).get("mode") == "" and not (data.get("state") or {}).get("done"))

    status, body, _ = req("/api/launch/start", "POST", {"mode": "bogus"})
    check("未知模式被拒绝（400 + 明确原因）",
          status == 400 and "未知的启动模式" in body.decode("utf-8"), f"status={status}")

    # ---- Lite：只应启动语音合成 ----
    fake_svc.calls.clear()
    fake_mdl.calls.clear()
    status, body, _ = req("/api/launch/start", "POST", {"mode": "lite"})
    r = json.loads(body.decode("utf-8"))
    check("选择 Lite 后接口立即返回并给出步骤清单",
          status == 200 and r.get("ok") and len((r.get("state") or {}).get("steps") or []) >= 3)
    st = wait_done()
    check("Lite 流程执行完成（done=True 且无错误）",
          st.get("done") and not st.get("error"), str(st.get("error"))[:120])
    check("Lite 步骤全部成功（检查环境 / 读配置 / 起语音服务）",
          [s["status"] for s in st.get("steps", [])][:3] == ["done", "done", "done"],
          str([(s["id"], s["status"]) for s in st.get("steps", [])]))
    check("Lite 只启动语音合成（未调用 start_all / init_models）",
          "start_gpt_sovits" in fake_svc.calls and "start_all" not in fake_svc.calls
          and not fake_mdl.calls, f"services={fake_svc.calls} models={fake_mdl.calls}")
    check("Lite 模式写入运行模式（config.APP_MODE=lite）", config.APP_MODE == "lite",
          config.APP_MODE)
    check("状态里带上语音服务信息（端口 / 就绪）",
          (st.get("tts") or {}).get("port") == 20000, str(st.get("tts")))

    # ---- 标准：应启动全部服务 ----
    fake_svc.calls.clear()
    fake_mdl.calls.clear()
    status, body, _ = req("/api/launch/start", "POST", {"mode": "standard"})
    st = wait_done()
    check("选择标准模式后流程完成（done=True 且无错误）",
          st.get("done") and not st.get("error"), str(st.get("error"))[:120])
    check("标准模式启动全部服务（start_all + 语音识别 init_models）",
          "start_all" in fake_svc.calls and "init_models" in fake_mdl.calls,
          f"services={fake_svc.calls} models={fake_mdl.calls}")
    check("标准模式写入运行模式（config.APP_MODE=standard）", config.APP_MODE == "standard",
          config.APP_MODE)

    # ---- 幂等：启动中重复点击不会重复拉起 ----
    fake_svc.calls.clear()
    launch_flow.reset()
    with launch_flow._lock:   # 模拟“正在启动”
        launch_flow._state.update({"mode": "lite", "starting": True, "steps": [], "running": True})
    req("/api/launch/start", "POST", {"mode": "standard"})
    check("启动中重复点击不会重复拉起服务（幂等）", fake_svc.calls == [], str(fake_svc.calls))
finally:
    launch_flow.reset()
    launch_flow.set_backends(None, None)
    for k, v in keep.items():
        setattr(config, k, v)
    srv.shutdown()

# ==================== 4. 启动页渲染冒烟（node + DOM 桩，真实跑一遍 launcher.js） ====================
import subprocess  # noqa: E402

render_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_gui_launcher_render.js")
if os.path.exists(render_js):
    print("---- 启动页渲染冒烟测试（node + DOM 桩） ----")
    rc = subprocess.call(["node", render_js, js])
    check("启动页渲染冒烟测试通过（渲染两个模式 → 点击启动 → 进度 → 进入界面）", rc == 0)
else:
    check("启动页渲染冒烟脚本 verify_gui_launcher_render.js 存在", False, render_js)

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("图形化启动程序 验证全部通过。")
