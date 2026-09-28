# verify_quiet_skip.py —— 验证「疑似非对话输入」的静默处理。
#
# 需求：语音识别到「疑似非对话输入」（视频字幕、环境噪音、单字应答等）时，
#       识别出的内容与「已跳过」提示都不显示在聊天界面。
# 做法：
#   1) 后端：/api/chat 对这类输入返回 action=skip + skip_silent=true（前端据此撤回消息）；
#   2) 后端：跳过的输入不会被当成一轮对话记录（不会留下聊天记录 / 上下文）；
#   3) 前端：app.js 在 skip_silent 时删掉刚显示的用户消息、不显示跳过提示，并同步会话缓存；
#   4) 非静默跳过（其他原因）仍然显示提示，行为不变。
# 只读检查，可反复运行：
#   venv\Scripts\python.exe tests\verify_quiet_skip.py
import json
import os
import re
import sys
import threading
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


# ==================== 1. 前端：先判断再输出（非对话输入不显示） ====================
app_js = read(os.path.join(ROOT, "web", "static", "app.js"))
check("app.js 识别后端返回的 skip_silent 标记", "skip_silent" in app_js)
check("语音输入先请求后端判断，判定通过后才显示（不会先显示再撤回）",
      "if (voice) {" in app_js and 'setCapsuleState("chat", "正在生成回复…", "busy")' in app_js
      and "userMsg.remove()" not in app_js)
check("静默跳过时直接返回：不显示识别内容、也不显示跳过提示",
      "if (silentSkip) return result;" in app_js
      and 'addMessage("system", result.skip_reason ? "已跳过："' in app_js)
_voice_block = app_js[app_js.index("const silentSkip ="):app_js.index("if (!result.ok)")]
check("静默跳过不占用「已开始对话」状态（首次就是噪音时可继续切换模式）",
      _voice_block.index("if (silentSkip) return result;") < _voice_block.index("conversationStarted = true;"))
check("手打文字仍即时显示，并照常给出「已跳过」提示",
      'conversationStarted = true;\n    userMsg = addMessage("user", text);' in app_js
      and 'setCapsuleState("chat", "正在生成回复…", "busy");' in app_js
      and 'if (result.skip_silent && voice) return result;\n    addMessage("system", result.skip_reason' in app_js)

# ==================== 2. 后端：跳过原因与静默标记 ====================
from core import pipeline  # noqa: E402
from web import server as web_server  # noqa: E402

base_result = pipeline._base_result("测试")
check("结果结构里始终带 skip_silent 字段（默认 False）",
      "skip_silent" in base_result and base_result["skip_silent"] is False)

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def chat(text):
    body = json.dumps({"message": text, "mode": "qa"}).encode("utf-8")
    req = urllib.request.Request(base + "/api/chat", data=body, method="POST",
                                headers={"X-XLLB-Client": "skip-verify",
                                         "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


try:
    for bad, label in [("简体中文字幕组", "含关键词（字幕）"), ("嗯", "单字应答")]:
        r = chat(bad)
        check(f"「{label}」被判定为静默跳过（action=skip / skip_silent=true）",
              r.get("action") == "skip" and r.get("skip_silent") is True
              and r.get("skip_reason") == "疑似非对话输入",
              f"action={r.get('action')} silent={r.get('skip_silent')} reason={r.get('skip_reason')}")
        check(f"「{label}」不产生语音播报（speak=False 且无流式音频）",
              r.get("speak") is False and not r.get("stream_id"), str(r.get("stream_id"))[:40])

    # 跳过的输入不会进入历史 / 上下文（不会留下记录）
    from core import storage
    before = len(storage.get_history() or [])
    chat("未完待续")
    after = len(storage.get_history() or [])
    check("跳过的输入不会被记进聊天历史（刷新后也不会冒出来）", before == after,
          f"{before} -> {after}")

    # 非静默路径：命令走插件分支，不应被标成静默跳过
    r = chat("/help")
    check("普通输入 / 命令不会被误标为静默跳过", not r.get("skip_silent"),
          f"action={r.get('action')} silent={r.get('skip_silent')}")
finally:
    srv.shutdown()

# ==================== 3. 其它路径不受影响 ====================
check("非静默跳过仍保留「已跳过」提示逻辑（其他原因不受影响）",
      'addMessage("system", result.skip_reason ? "已跳过："' in app_js)
check("只有「疑似非对话输入」会被后端标记为静默跳过",
      'result["skip_silent"] = True' in read(os.path.join(ROOT, "core", "pipeline.py"))
      and '"skip_silent": False' in read(os.path.join(ROOT, "core", "pipeline.py")))

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("疑似非对话输入静默处理 验证全部通过。")
