# verify_music_player.py —— 音乐播放进度条 / 暂停跳转 + 「进入设置不打断播放」验证
#
# 本轮改动：
#   1) 音乐播放条新增可拖动进度条：显示播放位置与时长，点击 / 拖动跳转到任意时间，暂停 / 继续照常；
#   2) 「设置」改为当前页浮层（iframe），进入设置不再卸载主页 —— 调音量时音乐 / 语音合成继续播放，
#      设置页保存后把音量实时回传给主页立即生效。
#
# 验证方式（不需要真实浏览器 / 不启动真实服务进程）：
#   · 静态检查：页面元素、样式、事件与关键分支都在；
#   · 行为检查：verify_music_player.js（极简 DOM 桩 + 真实 app.js，模拟拖动 / 暂停 / 设置回传）；
#   · 接口检查：真实起本机 Web 服务，只读接口（/api/settings）+ 路由表。
#
#   venv\Scripts\python.exe tests\verify_music_player.py
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
STATIC = os.path.join(WEB, "static")

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


index_html = read(os.path.join(WEB, "index.html"))
settings_html = read(os.path.join(WEB, "settings.html"))
app_js = read(os.path.join(STATIC, "app.js"))
settings_js = read(os.path.join(STATIC, "settings.js"))
css = read(os.path.join(STATIC, "style.css"))


def block(src, selector_regex, limit=700):
    """截取某个 CSS 规则块，便于做「不包含」这类断言。"""
    m = re.search(selector_regex, src)
    return m.group(0)[:limit] if m else ""


def js_block(src, header, limit=4000):
    """按大括号配对截取一个 JS 函数 / 代码块（用于函数体内部的正反断言）。"""
    i = src.find(header)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth = 0
    for k in range(j, min(len(src), j + limit)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return src[i:j + limit]


# ==================== 1. 音乐播放条：结构 ====================
check("音乐条里有进度条 / 已播时间 / 总时长 / 拖动圆点",
      all(k in index_html for k in ('id="music-progress"', 'id="music-progress-fill"',
                                    'id="music-progress-knob"', 'id="music-time"'))
      and 'class="music-track"' in index_html)
check("进度条可聚焦（键盘也能操作）且标注了用途",
      'role="slider"' in index_html and 'tabindex="0"' in index_html
      and "拖动跳转播放位置" in index_html)
check("播放器仍是页面内的 <audio>（不换成浏览器原生控件条）",
      bool(re.search(r'<audio[^>]*id="music-audio"[^>]*>', index_html))
      and "controls" not in re.search(r'<audio[^>]*id="music-audio"[^>]*>', index_html).group(0))

# ==================== 2. 音乐播放条：样式（与语音进度条同一套观感） ====================
check("进度条样式：细线轨道 + 强调色填充，与语音进度条一致",
      bool(re.search(r"\.music-track \{[^}]*height: 3px", css))
      and bool(re.search(r"\.music-track > i \{[^}]*background: var\(--accent\)", css)))
check("拖动命中区域比 3px 轨道大（好点好拖）",
      bool(re.search(r"\.music-progress \{[^}]*height: 16px", css))
      and "touch-action: none" in block(css, r"\.music-progress \{[^}]*\}"))
check("拖动圆点只在悬停 / 拖动时出现", ".music-knob" in css and ".music-progress.dragging .music-knob" in css)
check("音乐进度条有自己的「滑动等待」动画（与语音进度条观感一致但互不共用）",
      bool(re.search(r"\.music-progress\.indeterminate \.music-track > i \{[^}]*music-wait-scan", css))
      and bool(re.search(r"@keyframes music-wait-scan", css)))
_css_rules = re.findall(r"[^{}]+\{[^}]*\}", css)
_music_rules = [r for r in _css_rules if ".music-progress" in r.split("{")[0]]
_speak_rules = [r for r in _css_rules if ".speak-progress" in r.split("{")[0]]
check("两条进度条没有合二为一：各有各的轨道 / 动画，元素也不共用",
      bool(_music_rules) and bool(_speak_rules)
      and all("speak-scan" not in r for r in _music_rules)
      and any("speak-scan" in r for r in _speak_rules)
      and all("music-wait-scan" not in r for r in _speak_rules)
      and 'id="music-progress"' in index_html and "speak-slot" in app_js
      and "music-progress-fill" in app_js)
check("音乐进度条的等待动画只由音乐自己的状态驱动（暂停时不显示）",
      "function musicWaiting()" in app_js
      and "bar.classList.toggle(\"indeterminate\", musicWaiting());" in app_js)
check("悬停指针与拖动时关闭过渡（跟手）",
      "cursor: pointer" in block(css, r"\.music-progress \{[^}]*\}")
      and ".music-progress.dragging .music-track > i { transition: none; }" in css)

# ==================== 3. 音乐播放条：行为 ====================
check("进度渲染：填充宽度 / 圆点位置 / 时间文本一起更新",
      "function renderMusicProgress" in app_js and "music-progress-fill" in app_js
      and "music-progress-knob" in app_js and "fmtClock" in app_js)
check("播放中随时间自动推进（timeupdate），拖动时不覆盖拖动预览",
      'musicAudio.addEventListener("timeupdate"' in app_js
      and "if (!musicSeeking) renderMusicProgress();" in app_js)
check("拖动 / 点击跳转：按下预览、移动跟随、松手才真正跳",
      '"pointerdown"' in app_js and '"pointermove"' in app_js and '"pointerup"' in app_js
      and "musicAudio.currentTime = musicDragRatio * dur;" in app_js)
check("拖动过程用指针捕获（拖到进度条外也不丢）", "setPointerCapture" in app_js)
check("拖到边界外按 0~100% 夹取", "Math.min(1, Math.max(0, x / rect.width))" in app_js)
check("拿不到时长时忽略拖动（不会跳到错误位置）",
      "const canSeek = () => musicDuration() > 0 && !musicStopping;" in app_js)
check("支持左右方向键前后跳 5 秒（无障碍）",
      '"ArrowRight"' in app_js and '"ArrowLeft"' in app_js and "musicAudio.currentTime + step" in app_js)
check("停止 / 换曲时进度条归零", "function resetMusicProgress" in app_js
      and app_js.count("resetMusicProgress();") >= 4)

# ==================== 4. 播放 / 暂停 / 停止与其它逻辑的适配 ====================
toggle_block = js_block(app_js, '$("btn-music-toggle").onclick = () =>')
stop_block = js_block(app_js, '$("btn-music-stop").onclick = async ()')
check("播放/暂停按钮：切换播放状态并刷新进度", "musicAudio.play()" in toggle_block
      and "musicAudio.pause()" in toggle_block and "renderMusicProgress()" in toggle_block)
check("主动暂停音乐时实时模式恢复聆听（暂停期间不再占着麦克风）",
      "if (currentMode === \"live\") { suspendLiveForMusic = false; setLiveIndicator(); }" in toggle_block)
check("聊天指令暂停 / 继续 / 停止三条路径都适配进度条",
      'result.music_control === "pause"' in app_js and 'result.music_control === "resume"' in app_js
      and 'result.music_control === "stop"' in app_js
      and app_js.count("suspendLiveForMusic = false; setLiveIndicator();") >= 3)
check("停止按钮：清空音源、收起音乐条、进度归零、通知后端",
      "musicAudio.src = \"\"" in stop_block and "resetMusicProgress()" in stop_block
      and 'classList.add("hidden")' in stop_block and "/api/music/stop" in stop_block)
check("程序性换曲 / 停止用 musicStopping 标记，避免和「用户主动暂停」混淆",
      "let musicStopping = false" in app_js and "musicStopping = true" in app_js
      and "musicStopping = false" in app_js)
check("语音合成与音乐互不干扰：停止语音只停语音播放器",
      "musicAudio" not in js_block(app_js, "function stopSpeech()")
      and "musicAudio" not in js_block(app_js, "function stopCurrentAudio()")
      and "musicAudio" not in js_block(app_js, '$("btn-stop-speech").onclick'))
check("音乐播放期间仍然禁止实时录音（保持原有逻辑）",
      'musicAudio.addEventListener("playing"' in app_js and "stopActiveRecording()" in app_js)

# ==================== 4.5 播放条出现时机 + 下载等待动画 ====================
_play_music_block = js_block(app_js, "async function playMusic(")
check("选好歌立刻显示播放条（在请求下载之前，用户马上能看到在准备哪首）",
      _play_music_block.index("showMusicBar(label)") < _play_music_block.index('api("/api/music/play"')
      and "setMusicLoading(true)" in _play_music_block)
check("下载期间进度条走「等待」动画，时间位显示「准备中…」",
      "function musicWaiting()" in app_js and 'time.textContent = musicLoading' in app_js
      and '"准备中…"' in app_js)
check("等待动画明确排除「暂停」状态（暂停时不能继续转圈）",
      "if (musicLoading) return true;" in app_js
      and "return !musicDuration() && !!musicAudio.src && !musicAudio.paused;" in app_js)
check("拿到时长 / 开始播放 / 出错都会退出等待动画",
      'musicAudio.addEventListener("loadedmetadata"' in app_js
      and "musicLoading = false; renderMusicProgress();" in app_js
      and 'musicAudio.addEventListener("error"' in app_js)
check("下载失败会收起播放条并复位（不会留一个空条在那转圈）",
      "音乐下载失败" in _play_music_block and '$("music-bar").classList.add("hidden")' in _play_music_block
      and "setMusicLoading(false)" in _play_music_block)
check("搜索结果按钮点过即显示「下载中…」并防重复点击",
      "b.disabled = true;" in app_js and '"下载中…"' in app_js)
check("用户暂停 / 停止时立即退出等待动画",
      "setMusicLoading(false)" in js_block(app_js, '$("btn-music-toggle").onclick')
      and "setMusicLoading(false)" in js_block(app_js, '$("btn-music-stop").onclick')
      and "setMusicLoading(false)" in js_block(app_js, 'result.music_control === "pause"'))

# ==================== 4.6 歌曲播完：立刻收起，不等结束语 ====================
_ended_block = js_block(app_js, 'musicAudio.addEventListener("ended"')
check("歌曲播完立刻收起播放条（收起动作在请求结束语之前）",
      _ended_block.index('$("music-bar").classList.add("hidden")')
      < _ended_block.index('api("/api/music/ended"')
      and "resetMusicProgress();" in _ended_block)
check("结束语仍在后台生成并播放（只是不再占着播放条）",
      'api("/api/music/ended"' in _ended_block and "playQueue(resp.audio)" in _ended_block)
check("结束语播完之前不恢复实时聆听（避免麦克风把结束语当用户说话）",
      _ended_block.index("playQueue(resp.audio)") < _ended_block.rindex("suspendLiveForMusic = false"))
check("下载与「即将播放」文本 / 语音并行（等较慢的一步，而不是相加）",
      "def _download()" in read(os.path.join(WEB, "server.py"))
      and "def _intro()" in read(os.path.join(WEB, "server.py"))
      and read(os.path.join(WEB, "server.py")).count('name="music-') >= 2)

# ==================== 5. 设置浮层：进入设置不再打断播放 ====================
check("设置入口改为浮层（有 打开 / 关闭 / 懒创建 三个函数）",
      "function openSettingsOverlay" in app_js and "function closeSettingsOverlay" in app_js
      and "function ensureSettingsOverlay" in app_js)
check("点「设置」不再整页跳转（拦掉默认跳转，href 仅作兜底）",
      "e.preventDefault();" in js_block(app_js, '$("btn-settings")')
      and 'href="/settings.html"' in index_html
      and not re.search(r"location\.(href|replace)\s*=\s*[\"']/settings\.html", app_js))
check("浮层用 iframe 装载设置页（带 embed 标记）",
      'const SETTINGS_URL = "/settings.html?embed=1";' in app_js
      and 'frame.id = "settings-frame"' in app_js)
_close_block = js_block(app_js, "function closeSettingsOverlay()")
check("关闭浮层只隐藏、不销毁（主页与 iframe 都不卸载，播放不受影响）",
      'classList.add("hidden")' in _close_block and ".remove()" not in _close_block
      and "removeChild" not in _close_block and "src = " not in _close_block,
      _close_block[-200:])
check("Esc 与点击浮层空白处都能关闭", "function settingsEsc" in app_js
      and "if (e.key === \"Escape\" && settingsOpen) closeSettingsOverlay();" in app_js
      and "if (e.target === ov) closeSettingsOverlay();" in app_js)
check("浮层样式：全屏遮罩 + 内嵌面板，层级低于确认弹窗",
      ".settings-overlay {" in css and ".settings-sheet {" in css
      and bool(re.search(r"\.settings-overlay \{[^}]*z-index: 9\d", css))
      and bool(re.search(r"\.modal-overlay \{[^}]*z-index: 100", css)))
check("图片主题下浮层仍保持 fixed 全屏（body.theme-image > * 会把它拉回文档流，必须显式覆盖）",
      bool(re.search(r"body\.theme-image > \.settings-overlay \{[^}]*position: fixed", css))
      and bool(re.search(r"body\.theme-image > \.modal-overlay \{[^}]*position: fixed", css)))
check("浮层只保留设置内容：嵌入时隐藏设置页自己的顶栏，并铺满整个窗口",
      "document.body.classList.add(\"embed\")" in settings_js
      and "body.embed .topbar { display: none; }" in css
      and bool(re.search(r"body\.embed \.settings-page \{ padding: 0; \}", css))
      and bool(re.search(r"body\.embed \.settings-page-inner \{[^}]*max-width: none", css))
      and bool(re.search(r"\.settings-sheet \{[^}]*width: 100%", css)))
check("播放时间与按钮之间留了间距（左侧间距不变，不会挤到标题）",
      bool(re.search(r"\.music-time \{[^}]*margin-right: 14px", css))
      and bool(re.search(r"\.music-progress \{[^}]*margin: 0 12px", css)))
check("背景图由设置页自己画（与主页同一张图、同一套画法），滑动中也不会变黑",
      bool(re.search(r"body\.embed\.theme-image::before,\s*\nbody\.embed\.theme-image::after \{ display: block; \}", css))
      and "settings-plate" not in css and "settings-plate" not in app_js
      and bool(re.search(r"\.settings-sheet \{[^}]*overflow: hidden", css)))
check("浮层不画自己的底色（只保留设置页那一层，不要外面那层黑底）",
      bool(re.search(r"\.settings-overlay \{[^}]*background: transparent", css))
      and bool(re.search(r"\.settings-overlay \{[^}]*padding: 0", css))
      and bool(re.search(r"\.settings-sheet \{[^}]*background: transparent", css))
      and bool(re.search(r"\.settings-sheet \{[^}]*border: 0", css))
      and "body.theme-image .settings-overlay { background:" not in css)
check("切换动画：主页内容左滑、设置页右滑入，只用 transform（GPU 合成）",
      bool(re.search(r"body\.settings-open > \.topbar,\s*\nbody\.settings-open > \.layout \{ transform: translateX\(-100%\);", css))
      and "@keyframes settings-slide-in" in css and "@keyframes settings-slide-out" in css
      and bool(re.search(r"\.settings-overlay:not\(\.hidden\) \.settings-sheet \{\s*\n\s*animation: settings-slide-in", css))
      and bool(re.search(r"\.settings-overlay\.closing \.settings-sheet \{\s*\n\s*animation: settings-slide-out", css))
      and bool(re.search(r"\.topbar, \.layout \{ transition: transform", css)))
check("关掉系统动效时不做动画（不硬上）",
      "prefers-reduced-motion: reduce" in css and "animation: none" in css)
check("动画由主页驱动：尊重系统动效设置，滑出结束后才隐藏",
      "function prefersReducedMotion()" in app_js
      and "prefersReducedMotion()" in js_block(app_js, "function closeSettingsOverlay()"))
check("开设置加 settings-open、关设置去掉并滑出后再隐藏",
      'document.body.classList.add("settings-open")' in app_js
      and 'document.body.classList.remove("settings-open")' in app_js
      and 'ov.classList.add("closing")' in app_js
      and 'ov.classList.add("hidden")' in js_block(app_js, "function closeSettingsOverlay()"))
check("浮层自带关闭按钮（顶栏隐藏后仍有可见的关闭入口）",
      'close.id = "settings-close"' in app_js and 'close.onclick = () => closeSettingsOverlay();' in app_js
      and ".settings-close {" in css and "close:" in app_js)
check("服务端不禁止内嵌（没有 X-Frame-Options / CSP frame-ancestors）",
      "X-Frame-Options" not in read(os.path.join(WEB, "server.py")))

# ==================== 6. 音量实时回传（设置页 → 主页） ====================
check("主页监听设置页回传：音量立即生效 / 其它设置同步界面 / 关闭浮层",
      'd.type === "xllb:volumes"' in app_js and 'd.type === "xllb:settings-saved"' in app_js
      and 'd.type === "xllb:close-settings"' in app_js)
check("回传消息校验来源（非同源消息一律忽略）",
      "if (e.origin && e.origin !== location.origin) return;" in app_js)
check("音量立即套用到音乐与正在播放的语音",
      "function applyVolumes" in app_js and "currentAudio.volume = ttsVolume" in app_js
      and "musicAudio.volume = musicVolume" in app_js)
check("关闭浮层后重新同步音量 / 主题 / 状态（设置改动能立刻看到）",
      "async function refreshAfterSettings" in app_js
      and "await loadVolumes();" in app_js and "await refreshTheme();" in app_js)
check("设置页识别「被浮层嵌入」并回传消息",
      "const EMBEDDED = (() =>" in settings_js and "function postToHost" in settings_js
      and "window.parent.postMessage(msg, location.origin)" in settings_js)
check("设置页保存后回传音量与保存事件",
      'postToHost({ type: "xllb:volumes", tts_volume: st.tts_volume, music_volume: st.music_volume });' in settings_js
      and 'postToHost({ type: "xllb:settings-saved" });' in settings_js
      and '"tts_volume" in patch || "music_volume" in patch' in settings_js)
check("嵌入时「返回对话」＝关闭浮层（不做跳转），Esc 也能关",
      'backLink.textContent = "← 关闭设置";' in settings_js
      and 'postToHost({ type: "xllb:close-settings" });' in settings_js
      and 'e.key !== "Escape"' in settings_js)
check("嵌入时先让弹窗处理 Esc（不抢确认弹窗的按键）",
      'document.querySelector(".modal-overlay:not(.hidden)")' in settings_js)

# ==================== 7. 行为测试（node + DOM 桩，跑真实 app.js） ====================
harness = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_music_player.js")
print()
print("---- 音乐播放器行为测试（node + DOM 桩 + 真实 app.js） ----")
if not os.path.exists(harness):
    check("行为测试脚本存在", False, harness)
else:
    rc = subprocess.call(["node", harness], cwd=ROOT)     # 直接继承输出，避免沙箱下的管道限制
    check("音乐播放器 / 设置浮层 行为测试通过", rc == 0, f"退出码 {rc}")

# ==================== 8. 接口层（真实起本机服务，只读） ====================
sys.path.insert(0, ROOT)
from web import server as web_server  # noqa: E402

routes = web_server.ROUTES
check("后端保留音乐停止接口（停止按钮 / 聊天指令都会用）", ("POST", "/api/music/stop") in routes)
check("后端保留音乐播放 / 播完接口", ("POST", "/api/music/play") in routes and ("POST", "/api/music/ended") in routes)
check("设置接口仍提供对话音量与音乐音量",
      ("GET", "/api/settings") in routes and ("POST", "/api/settings") in routes)

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {"X-XLLB-Client": "verify-music"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read()


def get_full(path, headers=None):
    """取回 (状态码, 响应头, 正文)，用于检查 Range / 206。"""
    req = urllib.request.Request(base + path, headers=headers or {"X-XLLB-Client": "verify-music"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# 造一个真实的媒体文件（放在 runtime 下，走 /runtime/ 这条音频路径）
import tempfile  # noqa: E402
media_dir = os.path.join(ROOT, "runtime", "_range_check")
os.makedirs(media_dir, exist_ok=True)
media_path = os.path.join(media_dir, "sample.wav")
with open(media_path, "wb") as f:
    f.write(bytes(range(256)) * 40)          # 10240 字节的伪音频，只验证分片传输
media_url = "/runtime/_range_check/sample.wav"
media_size = os.path.getsize(media_path)

try:
    status, body = get("/api/settings")
    st = json.loads(body.decode("utf-8")).get("settings", {})
    check("设置接口返回 tts_volume / music_volume",
          isinstance(st.get("tts_volume"), (int, float)) and isinstance(st.get("music_volume"), (int, float)),
          str({k: st.get(k) for k in ("tts_volume", "music_volume")}))
    status_html, home = get("/")
    home_text = home.decode("utf-8")
    check("主页（真实返回）确实带上了音乐进度条",
          'id="music-progress"' in home_text and 'id="music-time"' in home_text)
    check("主页仍加载 app.js / 生命周期脚本",
          "/static/app.js" in home_text and "/static/app_lifecycle.js" in home_text)
    status_settings, settings_page = get("/settings.html")
    check("设置页可正常打开（浮层 iframe 会加载它）",
          status_settings == 200 and "/static/settings.js" in settings_page.decode("utf-8"))
    try:
        get("/api/music/stop")
        check("音乐停止接口拒绝 GET（只接受 POST）", False)
    except urllib.error.HTTPError as e:
        check("音乐停止接口拒绝 GET（只接受 POST）", e.code in (404, 405), f"HTTP {e.code}")

    # ---------- 8.1 拖动跳转依赖的 HTTP Range（没有它浏览器会把拖动当成从头重载） ----------
    status, headers, body = get_full("/static/style.css")
    check("普通文件响应带 Accept-Ranges（浏览器据此知道可以跳转）",
          status == 200 and headers.get("Accept-Ranges") == "bytes", str(headers.get("Accept-Ranges")))
    full_css = body
    status, headers, body = get_full("/static/style.css", {"Range": "bytes=0-99", "X-XLLB-Client": "v"})
    check("Range 请求返回 206 且只回请求的那一段",
          status == 206 and len(body) == 100 and body == full_css[:100], f"{status} {len(body)}")
    check("206 带正确的 Content-Range 与 Accept-Ranges",
          headers.get("Content-Range") == f"bytes 0-99/{len(full_css)}"
          and headers.get("Accept-Ranges") == "bytes", str(headers.get("Content-Range")))
    status, headers, body = get_full("/static/style.css", {"Range": "bytes=100-", "X-XLLB-Client": "v"})
    check("开放式 Range（bytes=100-）返回剩余部分",
          status == 206 and len(body) == len(full_css) - 100 and body == full_css[100:], f"{status} {len(body)}")
    status, headers, body = get_full("/static/style.css", {"Range": "bytes=-50", "X-XLLB-Client": "v"})
    check("后缀 Range（bytes=-50）返回最后 50 字节",
          status == 206 and len(body) == 50 and body == full_css[-50:], f"{status} {len(body)}")
    status, headers, body = get_full("/static/style.css", {"Range": "bytes=99999999-", "X-XLLB-Client": "v"})
    check("越界 Range 返回 416 并说明总长（而不是从头重来）",
          status == 416 and headers.get("Content-Range") == f"bytes */{len(full_css)}",
          f"{status} {headers.get('Content-Range')}")
    status, headers, body = get_full(media_url)
    check("音频文件（/runtime/...）同样支持 Range",
          status == 200 and headers.get("Accept-Ranges") == "bytes")
    status, headers, body = get_full(media_url, {"Range": "bytes=1000-1099", "X-XLLB-Client": "v"})
    check("音频分片内容与源文件字节一致（拖动就是取这一段）",
          status == 206 and body == open(media_path, "rb").read()[1000:1100],
          f"{status} {len(body)}")
    check("Range 解析支持开放 / 后缀 / 越界各种写法",
          web_server.parse_range("bytes=0-", 100) == (0, 99)
          and web_server.parse_range("bytes=-10", 100) == (90, 99)
          and web_server.parse_range("bytes=10-200", 100) == (10, 99)
          and web_server.parse_range("bytes=100-", 100) is None
          and web_server.parse_range("", 100) is None)
finally:
    srv.shutdown()
    srv.server_close()
    try:
        os.remove(media_path)
        os.rmdir(media_dir)
    except OSError:
        pass

# ==================== 9. 下载后的音量均衡（LUFS，只做一次） ====================
from core import music as music_core  # noqa: E402
from core import plugin_manager as pm  # noqa: E402

plugin_src = read(os.path.join(ROOT, "plugins", "smooth_music.py"))
check("插件提供「下载后音量均衡」与目标响度两项设置",
      '"normalize"' in plugin_src and '"target_lufs"' in plugin_src
      and "下载后音量均衡" in plugin_src and "目标响度（LUFS）" in plugin_src)
check("插件在下载完成后调用均衡（失败也不影响播放）",
      "def _normalize(path)" in plugin_src and "music_core.normalize_if_enabled(path)" in plugin_src
      and "path = _normalize(path)" in plugin_src)
check("手动选歌（/api/music/play）走同一条均衡设置",
      "music.normalize_if_enabled(path)" in read(os.path.join(WEB, "server.py")))
check("均衡只做一次：处理后写标记，重复调用直接跳过",
      ".lufs.json" in read(os.path.join(ROOT, "core", "music.py"))
      and "已均衡过（跳过）" in read(os.path.join(ROOT, "core", "music.py")))
check("增益有上限（避免把底噪一起放大 / 测量异常时炸音）",
      "_MAX_GAIN_DB" in read(os.path.join(ROOT, "core", "music.py")))

# ==================== 9.5 角色相关性增强（只改判断提示词，不改搜索词） ====================
check("插件提供「角色相关性增强」开关",
      '"prefer_characters"' in plugin_src and "角色相关性增强" in plugin_src)
check("增强只作用在判断提示词上（搜索词仍是用户原话）",
      "if st.get(\"prefer_characters\")" in plugin_src
      and "prompt = prompt + \"\\n\" + extra" in plugin_src
      and "music_core.search_music(song)" in plugin_src)
check("提示词明确写了「用户明确指定时以用户为准」",
      "严格按用户的指定挑选" in plugin_src and "不要用角色相关性覆盖它" in plugin_src)
check("支持 /smooth char on|off 切换",
      'args[0] in ("char", "character")' in plugin_src and '"prefer_characters": on' in plugin_src)

# ==================== 9.6 停用音乐插件后不再自动选歌 ====================
from core import pipeline as pipeline_mod  # noqa: E402

_pipe_src = read(os.path.join(ROOT, "core", "pipeline.py"))
check("自动播放第一首由音乐插件的启用状态决定",
      "def _music_autoplay_enabled()" in _pipe_src
      and "if videos and _music_autoplay_enabled():" in _pipe_src)
check("插件停用时改为列出搜索结果让用户自己选",
      'result["action"] = "music_search"' in _pipe_src
      and "请选择一首播放" in _pipe_src)

_real_module, _real_enabled = pm.manager.plugin_module, pm.manager.is_enabled
try:
    pm.manager.plugin_module = lambda name: object()
    pm.manager.is_enabled = lambda name: False
    check("停用音乐插件后：点歌不再自动选择", pipeline_mod._music_autoplay_enabled() is False)
    pm.manager.is_enabled = lambda name: True
    check("启用音乐插件时：保持自动选择", pipeline_mod._music_autoplay_enabled() is True)
    pm.manager.plugin_module = lambda name: None
    check("插件文件不存在时：保留内置点歌能力（不影响原来的用法）",
          pipeline_mod._music_autoplay_enabled() is True)
finally:
    pm.manager.plugin_module, pm.manager.is_enabled = _real_module, _real_enabled

# 真跑一遍流水线（打桩掉网络 / 模型 / 语音）：停用插件时不再自动挑歌
_real_fn = {k: getattr(pipeline_mod, k) for k in
            ("confirm_music_intent", "generate_music_search_keyword", "search_music",
             "download_music", "music_intro_prompt")}
_real_get_settings = pm.manager.get_settings
try:
    pipeline_mod.confirm_music_intent = lambda text: True
    pipeline_mod.generate_music_search_keyword = lambda text: "测试歌"
    pipeline_mod.search_music = lambda kw: [{"title": "测试歌", "url": "https://example.invalid/1"}]
    pipeline_mod.download_music = lambda url, title="": os.path.join(ROOT, "runtime", "_gate_check.wav")
    pipeline_mod.music_intro_prompt = lambda text, kw: ("即将播放《测试歌》。", [])
    pm.manager.get_settings = lambda name: {}
    pm.manager.plugin_module = lambda name: object()

    pm.manager.is_enabled = lambda name: False
    _off = pipeline_mod.process_message("播放 测试歌")
    check("停用插件后：说一句点歌不再自动播放（改为列出结果）",
          _off.get("action") == "music_search" and not _off.get("music"), str(_off.get("action")))
    check("停用插件后：仍然把搜索结果列出来供用户自己挑",
          isinstance(_off.get("music_videos"), list) and len(_off["music_videos"]) == 1
          and "请选择一首播放" in str(_off.get("reply")), str(_off.get("reply")))

    pm.manager.is_enabled = lambda name: True
    _on = pipeline_mod.process_message("播放 测试歌")
    check("启用插件时：点歌照常自动播放第一首",
          bool(_on.get("music")) and str(_on["music"].get("url", "")).endswith(".wav"),
          str(_on.get("music")))
finally:
    for k, v in _real_fn.items():
        setattr(pipeline_mod, k, v)
    pm.manager.get_settings = _real_get_settings
    pm.manager.plugin_module, pm.manager.is_enabled = _real_module, _real_enabled


class _FakeCtx:
    """插件上下文桩：只记录判断提示词，避免真调用模型。"""

    class _Mgr:
        def __init__(self, settings):
            self.settings = settings

        def get_settings(self, name):
            return self.settings

        def save_settings(self, name, patch):
            self.settings.update(patch)
            return True

    class _Config:
        character_name = "洛天依"

    def __init__(self, settings):
        self.config = self._Config()
        self.manager = self._Mgr(settings)
        self.prompts = []

    def generate(self, prompt, num_predict=64, temperature=0.0, purpose="plugin"):
        self.prompts.append(prompt)
        return "2"


sys.path.insert(0, os.path.join(ROOT, "plugins"))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("smooth_music_verify",
                                               os.path.join(ROOT, "plugins", "smooth_music.py"))
smooth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smooth)

_names = smooth._character_names(_FakeCtx({}))
check("能取到本机角色名（当前角色 + 预设）", "洛天依" in _names, str(_names))
_hint = smooth._character_prompt(_FakeCtx({}))
check("角色提示词包含角色名与「用户明确指定时以用户为准」",
      "洛天依" in _hint and "明确指定" in _hint, _hint[:80])

_videos = [{"title": "与角色无关的歌", "url": "u1"}, {"title": "洛天依 角色曲", "url": "u2"}]
_ctx_on = _FakeCtx({"mode": "llm", "prefer_characters": True})
_picked_on = smooth._pick(_videos, "随便来一首", _ctx_on)
check("开启增强时：判断提示词里带上了角色相关性要求",
      _ctx_on.prompts and "洛天依" in _ctx_on.prompts[0] and "优先挑选" in _ctx_on.prompts[0],
      (_ctx_on.prompts or [""])[0][:120])
check("判断提示词里仍然保留原始搜索词（搜索词没有被改）",
      "随便来一首" in _ctx_on.prompts[0])
check("模型返回的序号仍被正确采用", _picked_on.get("url") == "u2", str(_picked_on))

_ctx_off = _FakeCtx({"mode": "llm", "prefer_characters": False})
smooth._pick(_videos, "随便来一首", _ctx_off)
check("关闭增强时：判断提示词里没有角色相关性要求",
      _ctx_off.prompts and "优先挑选" not in _ctx_off.prompts[0])

_ctx_default = _FakeCtx({"mode": "llm"})
_ctx_default.config.character_name = ""
smooth._pick(_videos, "随便来一首", _ctx_default)
check("没有角色信息时不会硬塞提示词", _ctx_default.prompts and "优先挑选" not in _ctx_default.prompts[0])

ffmpeg = music_core.ffmpeg_path()
if not ffmpeg:
    print("[跳过] 没有 ffmpeg，无法做响度均衡实测")
else:
    work = os.path.join(ROOT, "runtime", "_lufs_check")
    os.makedirs(work, exist_ok=True)
    sample = os.path.join(work, "tone.wav")
    quiet = os.path.join(work, "quiet.wav")
    marker = sample + ".lufs.json"
    quiet_marker = quiet + ".lufs.json"
    try:
        # 生成一段稳定的 440Hz 单音（响度约 -21.8 LUFS），再验证「测均值 → 对齐到目标」
        subprocess.call([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=44100",
                         "-c:a", "pcm_s16le", sample],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        check("测试音频生成成功（用于响度均衡实测）", os.path.isfile(sample) and os.path.getsize(sample) > 1000)
        before = music_core.measure_loudness(sample)
        check("能测出整首的整合响度（LUFS）", isinstance(before, float), str(before))
        applied, detail = music_core.normalize_loudness_once(sample, -14.0)
        check("均衡动作执行并给出说明", applied is True, detail)
        after = music_core.measure_loudness(sample)
        check("均衡后响度对齐到目标（±1 LUFS 内）",
              isinstance(after, float) and abs(after - (-14.0)) <= 1.0,
              f"{before} -> {after}")
        check("处理记录写进 .lufs.json（便于排查，也是「只做一次」的依据）",
              os.path.isfile(marker))
        again, detail2 = music_core.normalize_loudness_once(sample, -9.0)
        check("第二次调用直接跳过（即使目标改成别的值也不再重复处理）",
              again is False and "已均衡过" in detail2, detail2)
        after2 = music_core.measure_loudness(sample)
        check("重复调用没有改动文件（说明确实只运行一次）",
              isinstance(after2, float) and abs(after2 - after) <= 0.2, f"{after} -> {after2}")

        # 标记只对「当时那个文件」有效：文件被换掉后不能继续按「已均衡」跳过
        subprocess.call([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "sine=frequency=660:duration=2:sample_rate=44100",
                         "-af", "volume=-10dB", "-c:a", "pcm_s16le", sample],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reapplied, detail3 = music_core.normalize_loudness_once(sample, -14.0)
        check("文件被替换后旧标记自动失效（会重新均衡一次，不会漏处理）",
              reapplied is True, detail3)

        # 增益限幅：极小的音源不会被无上限放大（避免把底噪一起轰出来）
        subprocess.call([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
                         "-af", "volume=-35dB", "-c:a", "pcm_s16le", quiet],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        quiet_before = music_core.measure_loudness(quiet)
        applied_q, detail_q = music_core.normalize_loudness_once(quiet, -14.0)
        quiet_after = music_core.measure_loudness(quiet)
        checked_gain = (quiet_after - quiet_before) if (quiet_after and quiet_before) else None
        check("增益限幅生效（最多 ±24dB，极端音源不会被无限放大到目标）",
              applied_q is True and checked_gain is not None and checked_gain <= music_core._MAX_GAIN_DB + 0.5,
              f"{quiet_before} -> {quiet_after}（增益 {checked_gain:.1f}dB）")

        # 插件设置开关：关闭时完全不参与（用打桩读写，避免改到用户真实设置）
        real_enabled, real_settings = pm.manager.is_enabled, pm.manager.get_settings
        try:
            pm.manager.is_enabled = lambda name: True
            pm.manager.get_settings = lambda name: ({"normalize": False} if name == music_core.NORMALIZE_PLUGIN else {})
            check("插件里关闭均衡时：完全不做处理",
                  music_core.normalize_if_enabled(sample) is None)
            pm.manager.get_settings = lambda name: ({"normalize": True, "target_lufs": "-16"}
                                                    if name == music_core.NORMALIZE_PLUGIN else {})
            enabled, target = music_core.loudness_options()
            check("插件里开启均衡时：读出目标响度", enabled is True and target == -16.0, str((enabled, target)))
            pm.manager.is_enabled = lambda name: False
            check("插件被停用时：即使设置还开着也不再处理",
                  music_core.loudness_options() == (False, None))
        finally:
            pm.manager.is_enabled, pm.manager.get_settings = real_enabled, real_settings
    finally:
        for path in (sample, marker, quiet, quiet_marker):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass

# ==================== 汇总 ====================
print()
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("音乐播放进度条 / 设置浮层 验证全部通过。")
