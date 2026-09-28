# verify_voice_ui_dom.py —— 真实浏览器（Edge 无头）里跑真实页面，验证这一版的两处界面改动：
#   1) 实时状态胶囊：页面上确实有这个胶囊、默认隐藏；语音请求期间显示「识别中…」，结束后收掉；
#   2) 语音输入「先判断再输出」：噪音不显示任何消息（不会先显示再撤回），手打同样内容仍给「已跳过」；
#   3) 对照组：正常回复照常显示用户消息与回答。
#
# 做法：起真实服务（只把 /api/chat 换成固定答案，其余接口都是真的）+ 用 Edge 无头加载一个探针页，
# 探针页在同一个源里 iframe 打开真实 index.html，直接调用页面里的 runChatFlow 并检查真实 DOM。
#
#   venv\Scripts\python.exe tests\verify_voice_ui_dom.py
import json
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from web import server as web_server  # noqa: E402

PROBE = os.path.join(ROOT, "web", "_dom_voice_probe.html")
EDGE_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
]

OKS = []
FAILS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


PROBE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>voice-ui-probe</title></head>
<body>
<iframe id="frame" src="/index.html" style="width:1100px;height:760px"></iframe>
<pre id="result">PENDING</pre>
<script>
const out = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function finish() {
  out.done = true;
  document.getElementById("result").textContent = "RESULT:" + JSON.stringify(out);
}

(async () => {
  try {
    const frame = document.getElementById("frame");
    const w = frame.contentWindow;
    // 等 iframe 真正导航完成（不能复用导航前拿到的 document：那是 about:blank 的旧文档），
    // 并等真实 app.js 跑起来（runChatFlow 是页面里的全局函数）。
    let d = null;
    for (let i = 0; i < 150; i++) {
      d = frame.contentDocument;
      if (d && d.readyState === "complete" && d.getElementById("chat")
          && typeof w.runChatFlow === "function") break;
      await sleep(100);
    }
    w.__probe = true;

    out.booted = typeof w.runChatFlow === "function";
    const cap = d.getElementById("status-capsule");
    out.capsule_present = !!cap;
    out.capsule_hidden_default = !!(cap && cap.classList.contains("hidden"));
    const chat = d.getElementById("chat");
    const capText = () => (d.getElementById("status-capsule-text") || {}).textContent || "";
    const capHidden = () => !cap || cap.classList.contains("hidden");
    out.capsule_text_idle = capText();          // 常驻：空闲时应该是「就绪」
    const bubbles = () => chat.querySelectorAll(".msg").length;
    const userBubbles = () => chat.querySelectorAll(".msg.user").length;

    // 场景 1：语音噪音（后端判定为静默跳过）——「先判断再输出」，页面不该出现任何消息
    out.user_before_noise = userBubbles();
    out.msgs_before_noise = bubbles();
    const p1 = w.runChatFlow("__noise__", "qa", { voice: true });
    out.capsule_text_during_voice = capText();
    out.capsule_visible_during_voice = !capHidden();
    out.noise_added_sync = bubbles() - out.msgs_before_noise;
    await p1;
    await sleep(120);
    out.noise_added = bubbles() - out.msgs_before_noise;
    out.noise_user_added = userBubbles() - out.user_before_noise;
    out.capsule_hidden_after_noise = capHidden();
    out.noise_text_shown = chat.textContent.indexOf("__noise__") >= 0;

    // 场景 2：同样内容改成手打 —— 用户主动发送，仍应给「已跳过」提示
    const before2 = bubbles();
    await w.runChatFlow("__noise__", "qa", {});
    out.typed_added = bubbles() - before2;
    out.typed_skip_hint = /已跳过/.test(chat.textContent);

    // 场景 3：对照组，正常回复
    const before3 = bubbles();
    const before3user = userBubbles();
    await w.runChatFlow("__hello__", "qa", {});
    out.hello_user_added = userBubbles() - before3user;
    out.hello_added = bubbles() - before3;
    out.hello_reply_shown = chat.textContent.indexOf("你好呀") >= 0;
    out.hello_capsule_hidden = capHidden();

    // 场景 4：多人对话流式生成 —— 生成期间显示「等待」进度条，生成结束必须收掉（不能一直等）
    // 注意：无头浏览器开了虚拟时间，定时器会被瞬间跳过，所以中间状态用 MutationObserver 记录，
    // 而不是靠轮询（轮询可能全部在真实网络返回之前就跑完了）。
    const before4 = bubbles();
    const before4asst = chat.querySelectorAll(".msg.assistant").length;
    out.multi_seen_wait_bar = false;
    out.multi_seen_capsule = false;
    const note = () => {
      if (chat.querySelector(".speak-progress.indeterminate")) out.multi_seen_wait_bar = true;
      if (capText().indexOf("生成中") >= 0) out.multi_seen_capsule = true;
    };
    const mo = new MutationObserver(note);
    mo.observe(chat, { childList: true, subtree: true, attributes: true, characterData: true });
    const mo2 = new MutationObserver(note);
    if (cap) mo2.observe(cap, { childList: true, subtree: true, attributes: true, characterData: true });
    await w.runChatFlow("__multi__", "qa", {});
    await sleep(300);
    mo.disconnect();
    mo2.disconnect();
    out.multi_wait_bars_left = chat.querySelectorAll(".speak-progress.indeterminate").length;
    out.multi_bubbles_added = chat.querySelectorAll(".msg.assistant").length - before4asst;
    out.multi_text_shown = chat.textContent.indexOf("角色A：第一句") >= 0
                           && chat.textContent.indexOf("角色B：第二句") >= 0;
    out.multi_capsule_hidden = capHidden();
    out.multi_msgs_added = bubbles() - before4;

    // 场景 5：问答模式按一次麦克风录完音 —— 胶囊不能一直停在「聆听」
    try {
      Object.defineProperty(w.navigator.mediaDevices, "getUserMedia", {
        configurable: true,
        value: async () => {
          const ac = new w.AudioContext();
          w.__probeCtx = ac;
          return ac.createMediaStreamDestination().stream;
        },
      });
      w.setMode("qa");
      await sleep(100);
      const pMic = w.onMicClick();                 // 开始录音
      await sleep(700);
      out.mic_capsule_during = capText();
      w.onMicClick();                              // 再点一次：停止录音
      await Promise.race([Promise.resolve(pMic).catch(() => {}), sleep(4000)]);
      await sleep(600);
      out.mic_capsule_after = capText();
      out.mic_capsule_class_after = String(cap.className || "");
      await sleep(2600);                           // 等一次性提示到期
      out.mic_capsule_settled = capText();
      out.mic_capsule_class_settled = String(cap.className || "");
    } catch (e) {
      out.mic_error = String(e && e.message ? e.message : e);
    }
  } catch (e) {
    out.error = String(e && e.stack ? e.stack : e);
  }
  finish();
})();
</script>
</body></html>
"""


def _find_edge():
    for tpl in EDGE_CANDIDATES:
        exe = os.path.expandvars(tpl)
        if os.path.exists(exe):
            return exe
    return None


def _stub_chat(req):
    """把 /api/chat 换成固定答案，其余流程（HTTP / 前端）都是真的。"""
    msg = (req.get("json", {}) or {}).get("message", "")
    if msg == "__noise__":
        return web_server._ok(action="skip", skip_silent=True, skip_reason="疑似非对话输入",
                              reply="", speak=False)
    if msg == "__multi__":
        return web_server._ok(action="reply", reply="多人对话生成中…", speak=False,
                              multi_stream_id="probe-multi")
    return web_server._ok(action="reply", reply="你好呀，我在。", speak=False, tts_total=1)


_MULTI_STEPS = [
    {"segments": [{"seq": 0, "speaker": "角色A", "text": "第一句", "audio": []}], "done": False},
    {"segments": [{"seq": 1, "speaker": "角色B", "text": "第二句", "audio": []}], "done": False},
    {"segments": [], "done": True},
]
_multi_calls = {"n": 0}


def _stub_multi_poll(req):
    """模拟多人对话「边生成边返回」：第一次故意慢一点，好让前端确实显示出「生成中」等待条。"""
    n = _multi_calls["n"]
    _multi_calls["n"] = n + 1
    if n == 0:
        time.sleep(0.8)
    step = _MULTI_STEPS[n] if n < len(_MULTI_STEPS) else {"segments": [], "done": True}
    return web_server._ok(segments=step["segments"], done=step["done"], error=None)


def main():
    edge = _find_edge()
    if not edge:
        print("找不到 Edge / Chrome，跳过真实浏览器验证。")
        return 2

    with open(PROBE, "w", encoding="utf-8") as f:
        f.write(PROBE_HTML)

    original = web_server.ROUTES.get(("POST", "/api/chat"))
    original_multi = web_server.ROUTES.get(("GET", "/api/multi_chat/poll"))
    web_server.ROUTES[("POST", "/api/chat")] = _stub_chat
    web_server.ROUTES[("GET", "/api/multi_chat/poll")] = _stub_multi_poll
    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/_dom_voice_probe.html"
    try:
        out = _run_headless(edge, url)
    finally:
        if original is not None:
            web_server.ROUTES[("POST", "/api/chat")] = original
        if original_multi is not None:
            web_server.ROUTES[("GET", "/api/multi_chat/poll")] = original_multi
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
        try:
            os.remove(PROBE)
        except Exception:
            pass

    m = re.search(r"RESULT:(\{.*?\})\s*</pre>", out, re.S)
    if not m:
        print("没能从浏览器拿到结果，原始输出片段：")
        print(out[-1500:])
        return 1
    data = json.loads(m.group(1))
    print("浏览器实测数据：", json.dumps(data, ensure_ascii=False)[:600], "\n")

    check("页面成功加载真实 app.js（探针能调到 runChatFlow）", data.get("booted") is True, str(data.get("error"))[:300])
    check("状态胶囊元素存在", data.get("capsule_present") is True)
    check("状态胶囊全局常驻：默认就显示「就绪」，不是隐藏的",
          data.get("capsule_hidden_default") is False
          and str(data.get("capsule_text_idle")) == "就绪", str(data.get("capsule_text_idle")))
    check("语音请求期间胶囊显示「正在生成回复…」（转写已在录音结束时完成）",
          "正在生成回复" in str(data.get("capsule_text_during_voice")), str(data.get("capsule_text_during_voice")))
    check("语音请求期间胶囊可见（浮在对话记录上方）", data.get("capsule_visible_during_voice") is True)
    check("噪音语音：先判断再输出 —— 流程结束后没有多出任何消息",
          data.get("noise_added") == 0, str(data.get("noise_added")))
    check("噪音语音：没有插入用户气泡（不会闪一下再撤回）",
          data.get("noise_user_added") == 0, str(data.get("noise_user_added")))
    check("噪音语音：识别内容从未出现在对话记录里", data.get("noise_text_shown") is False)
    check("噪音语音结束后胶囊回落（仍然常驻，不是隐藏）",
          data.get("capsule_hidden_after_noise") is False)
    check("手打同样内容仍给出「已跳过」提示",
          data.get("typed_skip_hint") is True and data.get("typed_added", 0) >= 1,
          f"added={data.get('typed_added')}")
    check("正常回复：用户消息照常显示", data.get("hello_user_added") == 1, str(data.get("hello_user_added")))
    check("正常回复：回答照常显示", data.get("hello_reply_shown") is True and data.get("hello_added", 0) >= 2,
          f"added={data.get('hello_added')}")
    check("正常回复结束后胶囊回落到常驻状态（不再显示播报 / 生成）",
          data.get("hello_capsule_hidden") is False)
    check("多人对话生成期间确实显示了「等待」进度条", data.get("multi_seen_wait_bar") is True)
    check("多人对话生成期间胶囊提示「生成中」", data.get("multi_seen_capsule") is True)
    check("多人对话生成结束后「等待」进度条被收掉（不会一直显示等待）",
          data.get("multi_wait_bars_left") == 0, str(data.get("multi_wait_bars_left")))
    check("多人对话每个角色一个气泡（占位气泡被接手，不再多一个空泡）",
          data.get("multi_bubbles_added") == 2 and data.get("multi_msgs_added") == 3,
          f"assistant={data.get('multi_bubbles_added')} 全部={data.get('multi_msgs_added')}")
    check("多人对话两个人的台词都显示出来",
          data.get("multi_text_shown") is True)
    check("多人对话结束后胶囊回落到常驻状态", data.get("multi_capsule_hidden") is False)
    check("问答模式录音期间胶囊显示聆听",
          "聆听" in str(data.get("mic_capsule_during")), str(data.get("mic_capsule_during")))
    check("问答模式录完音后胶囊不再停在「聆听」（本次修复）",
          data.get("mic_error") is None
          and "聆听" not in str(data.get("mic_capsule_after"))
          and "listening" not in str(data.get("mic_capsule_class_after")),
          f"{data.get('mic_capsule_after')} / {data.get('mic_capsule_class_after')} / {data.get('mic_error')}")
    check("问答模式录音结束后胶囊最终回到常驻的「就绪」",
          data.get("mic_capsule_settled") == "就绪"
          and "listening" not in str(data.get("mic_capsule_class_settled")),
          f"{data.get('mic_capsule_settled')} / {data.get('mic_capsule_class_settled')}")

    print()
    if FAILS:
        print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"通过 {len(OKS)} 项，失败 0 项")
    print("真实浏览器里的状态胶囊 / 先判断再输出 验证通过。")
    return 0


def _run_headless(edge, url):
    profile = os.path.join(ROOT, "runtime", "_dom_voice_profile")
    cmd = [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           f"--user-data-dir={profile}", "--virtual-time-budget=20000", "--dump-dom", url]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    raw = proc.stdout or b""
    # 清掉本次用的临时浏览器配置目录（不留在仓库里）
    import shutil
    for _ in range(5):
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile):
            break
        time.sleep(1.0)
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return str(raw)


if __name__ == "__main__":
    sys.exit(main())
