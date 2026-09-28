# verify_voice_flow.py —— 验证「合成语音进度条」与「生成期间不接收语音输入」。
#
# 需求：
#   1) 聊天界面在合成语音期间用一条很细的横线进度条占据「重播」按钮的位置，
#      合成完成后进度条被按钮取代；拿不到进度就不做（这里进度来自总句数）。
#   2) 多人对话生成/播报期间不再接收用户语音输入（此前实时模式会在生成过程中继续聆听 → 用户插话）。
# 做法：
#   · 服务端：真实跑一遍流式合成接口（合成函数用替身，不启动 GPT-SoVITS），
#     检查 /api/chat 返回总句数、/api/tts/next 逐句返回 produced/total、结束后 done；
#   · 前端：静态断言关键接线（进度条占位、完成后换成按钮、实时循环与麦克风在忙时拒绝输入、
#     多人对话分支被等待）。
#   venv\Scripts\python.exe tests\verify_voice_flow.py
import json
import os
import re
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


# ==================== 1) 服务端：总句数 + 逐句进度 ====================
from core import tts  # noqa: E402
from web import server as web_server  # noqa: E402

TEXT3 = "今天天气不错。我们出去走走吧。顺便买点水果。"
sentences = tts.tts_sentences(TEXT3)
check(f"分句函数可预先得到总句数（{len(sentences)} 句）", len(sentences) == 3, str(sentences))

# 合成函数替身：不启动 GPT-SoVITS，直接产出一个存在的文件
_real_gen = tts.generate_audio_for_sentence
_fake_files = []


def _fake_generate(sentence):
    path = os.path.join(ROOT, "runtime", f"_tts_progress_{len(_fake_files)}.wav")
    with open(path, "wb") as f:
        f.write(b"RIFF0000WAVE")
    _fake_files.append(path)
    return path


tts.generate_audio_for_sentence = _fake_generate
try:
    streamer = tts.TtsStreamer(TEXT3)
    check("TtsStreamer 创建时即可算出总句数（界面据此渲染进度条）", streamer.total == 3, str(streamer.total))
    p0 = streamer.progress()
    check("进度初始为 0/总句数 且未完成",
          p0["produced"] == 0 and p0["total"] == 3 and p0["done"] is False, str(p0))

    result = {"speak": True, "reply": TEXT3}
    web_server._attach_tts_stream(result)
    check("需要播报的回复带上 tts_total（供界面显示进度）",
          result.get("tts_total") == 3 and bool(result.get("stream_id")), str(result)[:120])

    no_audio = {"speak": True, "reply": "hello world"}
    web_server._attach_tts_stream(no_audio)
    check("没有可合成内容时不创建流（界面不会停在永远不动的进度条上）",
          no_audio.get("stream_id") is None and no_audio.get("tts_total") == 0, str(no_audio))
    silent = {"speak": False, "reply": TEXT3}
    web_server._attach_tts_stream(silent)
    check("不需要播报时不创建流", silent.get("stream_id") is None and silent.get("tts_total") == 0)

    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        res = {"speak": True, "reply": TEXT3}
        web_server._attach_tts_stream(res)
        sid = res["stream_id"]
        totals, produced, audio = [], [], 0
        deadline = time.time() + 20
        while time.time() < deadline:
            r = get("/api/tts/next?id=" + sid)
            totals.append(r.get("total"))
            produced.append(r.get("produced"))
            if r.get("audio"):
                audio += 1
            if r.get("done"):
                break
            time.sleep(0.05)
        check("流式接口每一步都返回 总句数 / 已完成句数（界面据此推进进度条）",
              all(t == 3 for t in totals) and produced == sorted(produced) and produced[-1] == 3,
              f"total={totals} produced={produced}")
        check("逐句返回音频（进度与音频同步到达）", audio == 3, f"音频段数 {audio}")
        check("完成后 done=True 且进度等于总句数",
              r.get("done") is True and r.get("produced") == 3, str(r))
        r2 = get("/api/tts/next?id=" + sid)
        check("已结束的流再次查询直接返回 done（不重复合成）",
              r2.get("done") is True and r2.get("total") == 0, str(r2))
    finally:
        srv.shutdown()
finally:
    tts.generate_audio_for_sentence = _real_gen
    for p in _fake_files:
        try:
            os.remove(p)
        except OSError:
            pass

# ==================== 2) 前端：进度条占位与替换 ====================
app_js = read(os.path.join(ROOT, "web", "static", "app.js"))
css = read(os.path.join(ROOT, "web", "static", "style.css"))

check("存在「合成进度占位」组件（进度条 + 完成后换成重播按钮）",
      "function speakSlot(getUrls, total, hasStream)" in app_js and "function finish()" in app_js)
check("进度条在合成开始前就占据按钮的位置（先插进度条，不是先插按钮）",
      re.search(r"const slot = speakSlot\(\(\) => collected, result\.tts_total, !!result\.stream_id\);\s*\n\s*wrap\.appendChild\(slot\.node\);",
                app_js) is not None)
check("只要有流式合成就会显示进度条（旧版后台没有总句数也能显示）",
      "if (hasStream || expect > 0) showBar();" in app_js
      and app_js.count("!!result.stream_id)") >= 3)
check("新进度取法：第一句到达前用不确定动画（单句回答也能看到进度在动）",
      "speak-progress indeterminate" in app_js and ".speak-progress.indeterminate" in css
      and "@keyframes speak-scan" in css)
check("新进度取法：按「已合成句数 - 1 + 当前句播放比例」推进（单句 = 播放比例 0→100%）",
      "(have + ratio) / total_" in app_js and "function setProgress(evt)" in app_js)
check("播放器上报当前音频播放比例（ontimeupdate → onRatio）",
      "function playOne(url, onRatio)" in app_js and "audio.ontimeupdate" in app_js
      and "audio.currentTime / dur" in app_js)
check("playStream 把 已合成句数 / 总句数 / 播放比例 一起上报",
      "async function playStream(streamId, onUrl, onProgress)" in app_js
      and "onProgress({ have, total, ratio" in app_js)
check("合成完成后进度条被「重播」按钮取代（含被打断的情况）",
      "speechPromise.then(() => slot.finish(), () => slot.finish())" in app_js)
check("没有流时直接显示按钮（不会留下进度条）", "} else {\n    slot.finish();" in app_js)
check("进度条样式是一条很细的横线（3px，占据按钮位置，轨道用明显的 --border）",
      ".msg .speak-progress" in css and "height: 3px" in css and ".msg .speak-slot" in css
      and "background: var(--border)" in css)
check("多人对话生成期间也显示进度条（生成中就能看到）",
      "const waitSlot = speakSlot(() => [], 0, true);" in app_js
      and "waitWrap.remove()" in app_js)
check("拿不到总句数/时长时不会把进度条写死为 0（保持不确定动画）",
      "if (!bar || !expect) return;" in app_js)

# ==================== 2.5) 状态胶囊（全局常驻；持续状态都在这里，一次性提示仍走聊天记录） ====================
index_html = read(os.path.join(ROOT, "web", "index.html"))
check("页面里有状态胶囊元素（浮层，默认就显示「就绪」）",
      'id="status-capsule"' in index_html and 'id="status-capsule-text"' in index_html
      and 'class="status-capsule"' in index_html and "hidden" not in index_html.split('id="status-capsule"')[0][-80:])
check("胶囊浮在对话记录上方、标题栏下方靠左（绝对定位，不占聊天排版）",
      ".status-capsule" in css and "position: absolute" in css and "top: 10px" in css
      and "left: 16px" in css and "border-radius: 999px" in css)
check("胶囊按状态换颜色 / 动画（聆听脉冲、处理中闪烁、播报、音乐、暂停、警告）",
      ".status-capsule.listening" in css and "cap-pulse" in css
      and ".status-capsule.busy" in css and "cap-blink" in css
      and ".status-capsule.speaking" in css and ".status-capsule.music" in css
      and ".status-capsule.paused" in css and ".status-capsule.warn" in css)
check("胶囊是全局常驻的状态机：按优先级合并持续状态，空闲回落「就绪」",
      "const CAPSULE_STATES = new Map()" in app_js and "function renderCapsule()" in app_js
      and "const CAPSULE_IDLE = { text: \"就绪\"" in app_js
      and "const CAPSULE_PRIORITY = { notice: 60, multi: 45, chat: 40, speak: 30, music: 25, live: 20 }" in app_js
      and "renderCapsule();               // 状态胶囊常驻" in app_js)
check("实时模式的状态都走胶囊（开启 / 聆听 / 未识别 / 暂停 / 继续 / 停止）",
      'setCapsuleState("live", "实时对话开启' in app_js
      and 'setCapsuleState("live", "正在聆听…' in app_js
      and 'setCapsuleNotice("未识别到语音。", "warn")' in app_js
      and 'setCapsuleState("live", "对话已暂停' in app_js
      and 'setCapsuleState("live", "已继续' in app_js
      and 'setCapsuleNotice("实时对话已停止"' in app_js)
check("这些状态不再写进聊天记录（不刷屏、不占历史）",
      'addMessage("system", "正在聆听' not in app_js
      and 'addMessage("system", "未识别到语音' not in app_js
      and 'addMessage("system", "实时对话开启' not in app_js
      and 'addMessage("system", "实时对话已停止' not in app_js)
check("问答模式同样常驻：识别 / 生成回复放胶囊（不再往聊天记录塞「思考中…」）",
      'setCapsuleState("chat", "识别中…", "busy")' in app_js
      and 'setCapsuleState("chat", "正在生成回复…", "busy")' in app_js
      and 'addMessage("system", "思考中…")' not in app_js)
check("问答模式录完音不再一直挂着「聆听」（录音结束就清掉实时状态）",
      re.search(r"mr\.onstop = async \(\) => \{\s*\n\s*cleanup\(\);\s*\n\s*//[^\n]*\n\s*clearCapsuleState\(\"live\"\);",
                app_js) is not None)
check("转写期间提示「识别中…」，转写结束就交还给调用方（不会残留）",
      'setCapsuleState("chat", "识别中…", "busy");      // 正在转写' in app_js
      and 'clearCapsuleState("chat");                       // 转写阶段结束' in app_js)
check("语音合成 / 播报的持续状态也在胶囊里（合成中 → 播报中，队列重叠用计数收口）",
      'beginSpeak("正在合成语音…")' in app_js and 'updateSpeak("正在播报语音…")' in app_js
      and "function endSpeak()" in app_js and "let speakBusyCount = 0" in app_js)
check("音乐的准备 / 播放 / 暂停同样在胶囊里",
      'setCapsuleState("music", "音乐准备中…", "busy")' in app_js
      and '"正在播放：" + name' in app_js and 'setCapsuleState("music", "音乐已暂停", "paused")' in app_js)
check("一次性提示（清空上下文、跳过、出错、下载失败）仍然写进聊天记录，避免漏看",
      'addMessage("system", "音乐下载失败：" + (resp.error || ""))' in app_js
      and 'addMessage("system", result.skip_reason ? "已跳过："' in app_js
      and 'addMessage("system", "请求失败：" + e)' in app_js
      and 'addMessage("system", result.skip_reason || result.error || "处理失败")' in app_js)
check("一轮结束后胶囊恢复为「聆听中」（实时模式）",
      "chatBusyCount === 0 && typeof liveActive" in app_js)

# ==================== 2.6) 先判断再输出（语音非对话输入不再闪现） ====================
_flow = app_js[app_js.index("async function runChatFlow"):app_js.index("async function sendMessage")]
check("语音输入不再先显示用户气泡（先请求后端判断）",
      "if (voice) {" not in _flow.split("let result;")[0]
      and _flow.index("setCapsuleState(\"chat\", \"正在生成回复…\"") < _flow.index("addMessage(\"user\", text)"))
check("判定为真实对话后才显示用户气泡",
      "userMsg = addMessage(\"user\", text);         // 判定为真实对话：这时才显示" in _flow)
check("静默跳过时直接返回（什么都没显示，不闪不撤）",
      "if (silentSkip) return result;" in _flow)
check("手打文字仍即时显示（用户主动输入）",
      "conversationStarted = true;\n    userMsg = addMessage(\"user\", text);" in _flow
      and 'setCapsuleState("chat", "正在生成回复…", "busy")' in _flow)

# ==================== 2.7) 多人对话的进度条 ====================
_block = app_js[app_js.index("async function streamMultiDialogue"):app_js.index("function renderVideoList")]
check("多人对话的每个气泡都有进度条（不是只有重播按钮）",
      "const slot = speakSlot(() => segUrls[seq] || []" in _block)
check("多人对话整段音频分支也给每个气泡挂进度条",
      app_js.count("const slot = speakSlot(") >= 3)
check("进度按该句播放比例推进，播完换成重播按钮",
      "slot.progress({ have: total - urls.length + i + 1, total, ratio })" in _block
      and "slot.finish();" in _block)
check("「生成中」的等待进度条在所有结束路径都会移除（不会一直等待）",
      "const stopWait = () =>" in _block and "} finally {\n    stopWait();" in _block
      and "stopWait();                        // 生成结束" in _block)
check("多人对话生成时用胶囊提示「多人对话生成中…」",
      'setCapsuleState("multi", "多人对话生成中…", "busy")' in _block
      and 'clearCapsuleState("multi");' in _block)

# ==================== 3) 前端：生成期间不接收语音输入 ====================
check("存在「本轮是否还没结束」的忙标记（计数式，容忍重叠）",
      "let chatBusyCount = 0;" in app_js and "function chatBusy() { return chatBusyCount > 0; }" in app_js
      and "chatBusyCount += 1;" in app_js and "chatBusyCount -= 1;" in app_js)
check("实时模式在上一轮结束前不开始新的录音（不再插话）",
      re.search(r"if \(chatBusy\(\)\) \{ await sleep\(150\); continue; \}", app_js) is not None)
check("问答模式麦克风在忙时拒绝新的语音输入并在胶囊里提示",
      re.search(r"async function onMicClick\(\) \{\s*\n\s*//[^\n]*\n\s*if \(chatBusy\(\)\) \{ setCapsuleNotice\(",
                app_js) is not None)
check("多人对话（剧本/流式）分支被等待到生成+播报结束",
      "await streamMultiDialogue(result.multi_stream_id, result.reply);" in app_js
      and "await multiDrain();" in app_js)
check("多人对话（整段音频）分支同样等待播完",
      "speechPromise = multiDrain();   // 等所有角色的语音播完，本轮才算结束" in app_js)
check("等待播放队列排空有超时保护（异常时不会永久卡住）",
      "async function multiDrain(maxMs = 180000)" in app_js and "Date.now() < deadline" in app_js)
check("handleMessage 用 try/finally 保证忙标记一定被清掉",
      re.search(r"async function handleMessage\(text, mode, opts\) \{\s*\n\s*chatBusyCount \+= 1;\s*\n\s*try \{", app_js) is not None
      and "} finally {\n    chatBusyCount -= 1;" in app_js)

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("语音合成进度条 / 生成期间不接收语音输入 验证全部通过。")
