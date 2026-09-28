# web/server.py
# 基于标准库 http.server 的轻量 Web 服务（零额外依赖），提供页面与 JSON API。
#
# ==================== 危险操作加固（请先读这里） ====================
# 本层的三件加固措施，定位是「防止绕过界面确认 + 防跨站请求」，**不是**用户认证：
#
#   1) 客户端标识：所有 POST 请求必须带自定义请求头 `X-XLLB-Client: <客户端 id>`。
#      自定义请求头会让跨站表单提交（浏览器只发简单请求）直接失败；
#      缺失或为空 → 403「缺少客户端标识（X-XLLB-Client），已拒绝该请求」。
#   2) 同源校验：带 `Origin` 头且其 host 与 `Host` 头不一致 → 403（防跨站脚本代发）。
#   3) 一次性确认令牌（web/confirm.py）：清空对话 / 删除记忆 / 插件重载启停 /
#      二次确认动作 / 写入敏感设置，必须先 `POST /api/confirm/prepare` 领令牌，
#      再带 `token` 发出真正的请求；令牌与 op/target/client 绑定、只能用一次。
#
# 认证挂载点：`_authorize(req)`。当前默认放行本地请求；将来接入账号体系时在
# `_authorize` 内校验会话 / 角色，返回 (False, "原因") 即拒绝，所有危险接口都会自动生效。
#
# 这些机制能防：第三方页面在用户不知情时调用危险接口、跨站表单、令牌重放。
# 防不了：本机用户自己（本地无账号体系时，任何本机进程都能自行申请令牌）。
import atexit
import json
import mimetypes
import os
import random
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core.config as config
from core import (
    pipeline,
    models,
    llm,
    tts,
    music,
    search,
    character,
    storage,
    runtime,
    services,
    plugin_manager,
    logger,
    version,
    paths,
    launch_flow,
)
from web import confirm as confirm_mod

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")
INDEX_FILE = os.path.join(WEB_DIR, "index.html")

_http_lock = threading.Lock()

# 客户端标识请求头（前后端约定：前端 api() 统一携带）
CLIENT_HEADER = "x-xllb-client"

# 需要一次性确认令牌的操作类型（op 名与前端约定，不要随意改）
CONFIRM_HISTORY_CLEAR = "history.clear"
CONFIRM_MEMORY_DELETE = "memory.delete"
CONFIRM_PLUGINS_RELOAD = "plugins.reload"
CONFIRM_PLUGINS_ENABLE = "plugins.enable"
CONFIRM_PLUGINS_DISABLE = "plugins.disable"
CONFIRM_PLUGIN_ACTION = "plugin.action"
CONFIRM_PLUGINS_SETTINGS = "plugins.settings"
CONFIRM_APP_SHUTDOWN = "app.shutdown"      # 完全退出程序（二级确认：停止服务 + 清理 + 关界面）

# 「二次确认动作」的显式声明表：`插件名:动作名`。
# 插件第一步动作返回 confirm 字段时，服务端会在这里登记确认动作名，
# 于是「直接调用最终动作」也会被要求携带令牌（插件不声明也能靠 `_do` 后缀兜底）。
_declared_confirm_actions = set()

# 敏感设置键：命中即要求令牌（避免每次保存普通开关都弹窗）
_SENSITIVE_KEY_RE = re.compile(r"(^|_)(api_key|key)$|model|backend", re.IGNORECASE)

# 流式 TTS 注册表：stream_id -> (TtsStreamer, 创建时间)
_streams = {}
_streams_lock = threading.Lock()
_STREAM_TTL = 600  # 10 分钟内未取完的流视为失效

# 运行时缓存清理间隔（秒）与最大保留时长（秒）
_CLEANUP_INTERVAL = 300
_CACHE_MAX_AGE = 600


def _start_cleanup_loop():
    """后台周期清理 runtime 缓存，避免长期运行累积。"""
    def _loop():
        while True:
            time.sleep(_CLEANUP_INTERVAL)
            try:
                runtime.cleanup_runtime(max_age_seconds=_CACHE_MAX_AGE)
                _prune_streams()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


def _prune_streams():
    now = time.time()
    with _streams_lock:
        stale = [sid for sid, (_, ts) in _streams.items() if now - ts > _STREAM_TTL]
        for sid in stale:
            _streams.pop(sid, None)


# ==================== 工具函数 ====================
# runtime 绝对路径 → 可访问 URL（唯一实现在 core/runtime.py，各层共用）
_runtime_url = runtime.runtime_url


def _urls(paths):
    return [_runtime_url(p) for p in paths]


def _json(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _ok(**kw):
    kw.setdefault("ok", True)
    return _json(kw)


def _error(message, status=400):
    return _json({"ok": False, "error": str(message)}, status)


# ==================== 危险操作加固：认证挂载点 / 客户端标识 / 令牌 ====================
def _authorize(req):
    """认证挂载点：返回 (是否允许, 中文原因)。

    当前版本为单用户本地应用，默认放行（仅记录客户端标识，不做鉴权）。
    接入账号体系时在这里校验会话 / 角色：返回 (False, "未登录或权限不足") 即拒绝，
    所有危险接口都会先经过这里，无需逐个改动业务代码。
    """
    client = _client_id(req)
    if not client:
        # 到这里说明连客户端标识都没有（HTTP 层已拦），这里再兜一层，便于脚本直调处理函数
        return False, "缺少客户端标识（X-XLLB-Client），已拒绝该请求"
    return True, ""


def _client_id(req):
    """从 req["headers"] 读取客户端标识（大小写不敏感）。"""
    headers = (req or {}).get("headers") or {}
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == CLIENT_HEADER:
            return str(value or "").strip()
    return ""


def _guard_post_headers(req):
    """POST 进入处理函数前的统一检查：客户端标识 + 同源校验。

    返回 None 表示通过；否则返回已构造好的错误响应（403）。
    """
    if not _client_id(req):
        return _error("缺少客户端标识（X-XLLB-Client），已拒绝该请求", 403)
    headers = (req or {}).get("headers") or {}
    if isinstance(headers, dict):
        origin = host = ""
        for key, value in headers.items():
            k = str(key).lower()
            if k == "origin":
                origin = str(value or "").strip()
            elif k == "host":
                host = str(value or "").strip()
        if origin:
            origin_host = urllib.parse.urlparse(origin).netloc.lower()
            # 只比较 host:port，兼容默认端口省略（如 http://127.0.0.1 与 127.0.0.1:80）
            if origin_host != host.lower():
                return _error("请求来源与访问地址不一致（同源校验失败），已拒绝该请求", 403)
    return None


def _require_confirm(req, op, target=""):
    """校验一次性确认令牌（危险操作的第二步请求）。

    返回 None 表示通过；否则返回 409 错误响应（带 need_confirm / op / target）。
    """
    token = str((req or {}).get("json", {}).get("token") or "").strip()
    ok, reason = confirm_mod.consume(op, target=target, client=_client_id(req), token=token)
    if ok:
        return None
    return _json({"ok": False, "error": reason, "need_confirm": True, "op": op, "target": target}, 409)


def _require_authorized(req):
    """危险接口先过认证挂载点：不允许时返回 403。"""
    allowed, reason = _authorize(req)
    if allowed:
        return None
    return _error(reason or "未授权", 403)


def _gate_error(reason, st, status=409):
    """记忆门禁拒绝响应：如实告知「当前模式 + 为什么被拒绝」（HTTP 409）。"""
    st = st or {}
    return _json({
        "ok": False,
        "error": reason,
        "plugin_enabled": bool(st.get("plugin_enabled")),
        "mode": st.get("mode") or "readonly",
        "level": st.get("level") or "只读（仅本次会话）",
        "can_read": bool(st.get("can_read")),
        "can_write": bool(st.get("can_write")),
        "can_manage": bool(st.get("can_manage")),
        "reason": reason,
    }, status)


def _memory_gate(level):
    """记忆门禁统一入口：返回 (engine|None, 拒绝响应|None, 状态快照)。

    引擎一律从 `memory_engine.service` 取，Web 层不再自行 init() / 写库。
    """
    from memory_engine import service as mem_service
    eng, reason, st = mem_service.check(level)
    if eng is None:
        return None, _gate_error(reason or "记忆不可用", st), st
    return eng, None, st


def _memory_read_level():
    """读类记忆接口的访问级别。"""
    from memory_engine import service as mem_service
    return mem_service.LEVEL_READ


def _memory_manage_level():
    """增删改（人工管理）记忆接口的访问级别。"""
    from memory_engine import service as mem_service
    return mem_service.LEVEL_MANAGE


def _write_blocked_class():
    """引擎底层拒绝写入时抛出的异常类（延迟导入，避免循环依赖）。"""
    from memory_engine.service import WriteBlocked
    return WriteBlocked


def _memory_ok(st, **kw):
    """记忆接口成功响应：附带当前模式字段，供前端显示「当前模式」。"""
    st = st or {}
    kw.setdefault("mode", st.get("mode") or "readonly")
    kw.setdefault("level", st.get("level") or "只读（仅本次会话）")
    kw.setdefault("can_manage", bool(st.get("can_manage")))
    kw.setdefault("can_read", bool(st.get("can_read")))
    kw.setdefault("can_write", bool(st.get("can_write")))
    kw.setdefault("plugin_enabled", bool(st.get("plugin_enabled")))
    return _ok(**kw)


def _is_sensitive_keys(settings):
    """patch 中是否含敏感键（api_key / key / model / backend）：命中才要求确认令牌。"""
    if not isinstance(settings, dict):
        return False
    return any(_SENSITIVE_KEY_RE.search(str(k)) for k in settings.keys())


def _is_confirm_action(name, action):
    """该动作是否属于「二次确认动作」：显式声明过，或名字以 _do 结尾。

    显式声明来源：插件第一步动作返回 confirm 字段时，服务端登记 confirm 指定的动作名。
    """
    key = f"{name}:{action}"
    if key in _declared_confirm_actions:
        return True
    return str(action or "").endswith("_do")


def _register_confirm_action(name, action):
    """登记「二次确认动作」，使直接调用最终动作也必须携带令牌。"""
    if name and action:
        _declared_confirm_actions.add(f"{name}:{action}")


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


# ==================== 状态 / 设置 ====================
def _settings_snapshot():
    return {
        "version": version.read_version(),   # 来自根目录 version.txt（界面不再写死版本号）
        "character_name": config.character_name,
        "influence_min": config.influence_min,
        "influence_max": config.influence_max,
        "tts_volume": config.tts_volume,
        "music_volume": config.music_volume,
        "internet_enabled": config.internet_enabled,
        "debug_mode": config.DEBUG_MODE,
        "current_voice_name": config.CURRENT_VOICE_NAME,
        "ollama_model": config.OLLAMA_MODEL,
        "llm_chat_backend": config.LLM_CHAT_BACKEND,
        "llm_judge_backend": config.LLM_JUDGE_BACKEND,
        "llm_chat_model": config.LLM_CHAT_MODEL,
        "llm_judge_model": config.LLM_JUDGE_MODEL,
        "tavily": search.get_usage_info(),
    }


def _build_id():
    """代码构建标识：按关键源码文件的最新修改时间 + 大小算一个短哈希。

    用途：磁盘上的代码更新后，旧进程仍在端口上服务时（「改了代码但界面还是旧的」），
    图形启动器可以据此识别「后台是旧版本」并结束它重开，而不是继续复用旧进程。
    """
    import hashlib
    parts = []
    for rel in ("core", "web", "plugins"):
        base = os.path.join(config.PROJECT_ROOT, rel)
        for root, _dirs, names in os.walk(base):
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                    parts.append(f"{os.path.relpath(path, config.PROJECT_ROOT)}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    continue
    for rel in ("launcher.py", "launcher_gui.py", "version.txt"):
        path = os.path.join(config.PROJECT_ROOT, rel)
        try:
            st = os.stat(path)
            parts.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            continue
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


_BUILD_ID = _build_id()


def _status_snapshot():
    # 语音服务状态只探一次（/api/status 被前端每 5 秒轮询，避免重复探测 /docs）
    tts_service = services.gpt_sovits_status()
    return {
        "llm_ready": llm.check_backend(),
        "judge_ready": llm.check_judge_backend(),
        # Ollama 状态必须反映"实际使用"：仅当生成/判断后端为 ollama 且服务可用时才为真
        "ollama_used": llm.ollama_used(),
        "ollama_ready": llm.ollama_used() and llm.check_ollama(),
        "app_mode": config.APP_MODE,
        "version": version.read_version(),
        "build": _BUILD_ID,
        "llm_backend": config.LLM_CHAT_BACKEND,
        "llm_judge_backend": config.LLM_JUDGE_BACKEND,
        "llm_model": config.LLM_CHAT_MODEL,
        "tts_api_ready": bool(tts_service.get("ready")),
        "tts_service": tts_service,
        "whisper_ready": models.is_ready(),
        "whisper_error": models.last_error(),
        "settings": _settings_snapshot(),
    }


def _apply_settings(data):
    changed = []
    if "influence_min" in data:
        v = int(data["influence_min"])
        if 1 <= v <= config.influence_max <= 10:
            config.influence_min = v
            changed.append("influence_min")
        else:
            raise ValueError("影响力范围需满足 1 ≤ 最小值 ≤ 最大值 ≤ 10")
    if "influence_max" in data:
        v = int(data["influence_max"])
        if 1 <= config.influence_min <= v <= 10:
            config.influence_max = v
            changed.append("influence_max")
        else:
            raise ValueError("影响力范围需满足 1 ≤ 最小值 ≤ 最大值 ≤ 10")
    if "tts_volume" in data:
        config.tts_volume = max(0.0, min(1.0, float(data["tts_volume"])))
        changed.append("tts_volume")
    if "music_volume" in data:
        config.music_volume = max(0.0, min(1.0, float(data["music_volume"])))
        changed.append("music_volume")
    if "internet_enabled" in data:
        config.internet_enabled = bool(data["internet_enabled"])
        changed.append("internet_enabled")
    if "debug_mode" in data:
        config.DEBUG_MODE = bool(data["debug_mode"])
        changed.append("debug_mode")
    return changed


# ==================== API 处理函数 ====================
def _api_status(_req):
    return _ok(**_status_snapshot())


def _api_settings_get(_req):
    return _ok(settings=_settings_snapshot())


def _api_settings_post(req):
    try:
        changed = _apply_settings(req.get("json", {}))
    except ValueError as e:
        return _error(e)
    return _ok(changed=changed, settings=_settings_snapshot())


def _api_characters_get(_req):
    return _ok(presets=character.list_presets(), current=config.character_name)


def _api_character_post(req):
    data = req.get("json", {})
    mode = data.get("mode", "default")
    try:
        name, desc = character.apply_character(
            mode,
            preset_name=data.get("preset_name"),
            raw_input=data.get("raw_input"),
            custom_req=data.get("custom_req", ""),
            use_search=bool(data.get("use_search")),
            save=bool(data.get("save")),
        )
    except ValueError as e:
        return _error(e)
    return _ok(character_name=name, description=desc)


def _api_voice_presets_get(_req):
    return _ok(presets=tts.list_voice_presets(), current=config.CURRENT_VOICE_NAME)


def _api_voice_preset_post(req):
    data = req.get("json", {})
    mode = data.get("mode", "preset")
    name = data.get("name", "")
    try:
        if mode == "create":
            folder = tts.create_voice_preset(name)
            return _ok(folder=folder, message=f"已创建文件夹：{folder}，请放入参考音频 (.wav)、prompt.txt 与权重文件。")
        if mode == "default":
            tts.select_voice_preset_by_name("默认")
        else:
            tts.select_voice_preset_by_name(name)
    except ValueError as e:
        return _error(e)
    return _ok(current=config.CURRENT_VOICE_NAME)


def _api_history_get(_req):
    return _ok(records=storage.get_history())


def _api_history_mark(req):
    data = req.get("json", {})
    indexes = data.get("indexes", [])
    marked = storage.mark_records([int(i) for i in indexes])
    return _ok(marked=marked)


def _api_history_clear(req):
    """清空对话记录：危险操作，必须带一次性确认令牌（op=history.clear）。"""
    denied = _require_authorized(req)
    if denied:
        return denied
    denied = _require_confirm(req, CONFIRM_HISTORY_CLEAR, "*")
    if denied:
        return denied
    storage.clear_history()
    return _ok()


def _api_save(req):
    data = req.get("json", {})
    mode = data.get("mode", "marked")
    if mode == "latest":
        result = storage.quick_save_latest()
    else:
        result = storage.save_marked_records()
    return _ok(result=result)


def _api_usage_get(_req):
    return _ok(**search.get_usage_info())


def _api_usage_post(req):
    data = req.get("json", {})
    try:
        info = search.reset_usage(data.get("used"))
    except ValueError as e:
        return _error(e)
    return _ok(**info)


def _attach_tts_stream(result):
    """若结果需要语音播报，为其创建流式合成任务并返回 stream_id。

    同时带上总句数 tts_total：界面在合成期间用「细横线进度条」占据播放按钮的位置，
    用 已合成句数 / 总句数 显示进度，合成完成后进度条再换成「重播」按钮。
    没有任何可合成的句子时不创建流（避免界面停在一个永远不动的进度条上）。
    """
    result["stream_id"] = None
    result["tts_total"] = 0
    if not (result.get("speak") and result.get("reply")):
        return result
    streamer = tts.TtsStreamer(result["reply"])
    if streamer.total <= 0:
        return result
    sid = uuid.uuid4().hex
    with _streams_lock:
        _streams[sid] = (streamer, time.time())
    result["stream_id"] = sid
    result["tts_total"] = streamer.total
    return result


def _api_chat(req):
    data = req.get("json", {})
    message = data.get("message", "")
    mode = data.get("mode", "qa")
    result = pipeline.process_message(message, mode=mode)
    return _json(_attach_tts_stream(result))


def _api_tts_next(req):
    sid = (req.get("query", {}).get("id") or [""])[0]
    with _streams_lock:
        entry = _streams.get(sid)
    if entry is None:
        return _ok(audio=None, done=True, produced=0, total=0)

    streamer, _ts = entry
    path, done = streamer.get(timeout=25)
    prog = streamer.progress()
    if done:
        with _streams_lock:
            _streams.pop(sid, None)
        return _ok(audio=None, done=True, produced=prog["total"], total=prog["total"])
    if path is None:
        return _ok(audio=None, done=False, produced=prog["produced"], total=prog["total"])
    return _ok(audio=_runtime_url(path), done=False,
               produced=prog["produced"], total=prog["total"])


def _api_transcribe(req):
    wav_bytes = req.get("body", b"")
    if not wav_bytes:
        return _error("未收到音频数据")
    try:
        from core.audio import transcribe_wav_bytes
        text = transcribe_wav_bytes(wav_bytes)
        return _ok(text=text)
    except Exception as e:
        return _json({"ok": False, "text": "", "error": str(e)})


def _api_music_search(req):
    q = (req.get("query", {}).get("q") or [""])[0]
    if not q:
        return _error("缺少搜索关键词")
    return _ok(videos=music.search_music(q))


def _api_music_play(req):
    data = req.get("json", {})
    url = data.get("url", "")
    title = data.get("title", "")
    keyword = data.get("keyword", title)
    if not url:
        return _error("缺少视频地址")

    # 下载与「即将播放」文本 / 语音并行：总等待时间取两者中较慢的，而不是相加
    holder = {}

    def _download():
        try:
            holder["path"] = music.download_music(url, title)
        except Exception as e:
            holder["download_error"] = str(e)

    def _intro():
        try:
            holder["intro"] = pipeline.music_intro_prompt(title or keyword, keyword)
        except Exception as e:
            holder["intro_error"] = str(e)

    workers = [threading.Thread(target=_download, name="music-download", daemon=True),
               threading.Thread(target=_intro, name="music-intro", daemon=True)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    path = holder.get("path")
    if not path:
        return _error("音乐下载失败")

    # 插件里开了「下载后音量均衡」时，下载完成就均衡一次（与语音点歌走同一条设置）
    level = None
    try:
        level = music.normalize_if_enabled(path)
    except Exception as e:
        logger.warn(f"音乐音量均衡失败（已忽略）：{e}")

    intro_text, intro_audio = holder.get("intro") or ("", [])
    return _ok(
        music_url=_runtime_url(path),
        intro_text=intro_text,
        intro_audio=_urls(intro_audio),
        loudness=level,
    )


def _api_music_ended(req):
    data = req.get("json", {})
    song_name = data.get("title", "")
    text, audio = pipeline.music_outro_prompt(song_name)
    return _ok(text=text, audio=_urls(audio))


def _api_music_stop(_req):
    music.cleanup_music()
    return _ok()


# ==================== 插件管理 ====================
def _api_plugins_get(_req):
    return _ok(plugins=plugin_manager.manager.list_plugins())


def _api_plugins_reload(req):
    """重新加载插件：危险操作，必须带令牌。

    返回结构化报告（保留既有 errors 字段）：本次重载了哪些、跳过了哪些未变化插件、
    以及是否可能影响临时上下文（由插件声明的 reload_policy.preserve_context 判断）。
    """
    denied = _require_authorized(req)
    if denied:
        return denied
    denied = _require_confirm(req, CONFIRM_PLUGINS_RELOAD, "*")
    if denied:
        return denied
    report = plugin_manager.manager.reload_report()
    return _ok(plugins=plugin_manager.manager.list_plugins(),
               errors=list(report.get("errors") or []),
               report=report)


def _api_plugins_enable(req):
    name = req.get("json", {}).get("name", "")
    denied = _require_authorized(req)
    if denied:
        return denied
    denied = _require_confirm(req, CONFIRM_PLUGINS_ENABLE, name)
    if denied:
        return denied
    try:
        plugin_manager.manager.enable(name)
    except ValueError as e:
        return _error(e)
    return _ok(plugins=plugin_manager.manager.list_plugins())


def _api_plugins_disable(req):
    name = req.get("json", {}).get("name", "")
    denied = _require_authorized(req)
    if denied:
        return denied
    denied = _require_confirm(req, CONFIRM_PLUGINS_DISABLE, name)
    if denied:
        return denied
    try:
        plugin_manager.manager.disable(name)
    except ValueError as e:
        return _error(e)
    return _ok(plugins=plugin_manager.manager.list_plugins())


def _api_plugins_settings(req):
    """保存插件设置：含敏感键（api_key / model / backend …）时必须带令牌。

    「敏感键」指会影响计费、外部服务地址或模型选择的参数；普通开关与数值不要求令牌，
    避免每次保存都弹窗。
    """
    data = req.get("json", {})
    name = data.get("name", "")
    settings = data.get("settings", {})
    if _is_sensitive_keys(settings):
        denied = _require_authorized(req)
        if denied:
            return denied
        denied = _require_confirm(req, CONFIRM_PLUGINS_SETTINGS, name)
        if denied:
            return denied
    try:
        merged = plugin_manager.manager.save_settings(name, settings)
    except ValueError as e:
        return _error(e)
    return _ok(settings=merged)


def _api_plugins_action(req):
    """执行插件动作。

    两级语义：
      · 第一步：动作返回 confirm 字段（插件声明「需要二次确认」）时，服务端为
        confirm 指定的最终动作签发一次性令牌，随响应返回 confirm_token（保留原 confirm / reply）。
      · 第二步：调用「二次确认动作」（显式声明过，或动作名以 _do 结尾）必须带该令牌，
        否则 409「需要先在界面确认（确认令牌缺失或已失效）」；直接调用最终动作绕不过去。
      · 普通动作不需要令牌。
    """
    data = req.get("json", {})
    name = data.get("name", "")
    action = data.get("action", "")
    if _is_confirm_action(name, action):
        denied = _require_authorized(req)
        if denied:
            return denied
        denied = _require_confirm(req, CONFIRM_PLUGIN_ACTION, f"{name}:{action}")
        if denied:
            return denied
    try:
        result = plugin_manager.manager.run_action(name, action)
    except ValueError as e:
        return _error(e)
    if result is None:
        return _error("动作不存在")
    payload = _attach_tts_stream(result)
    payload.setdefault("ok", True)  # 保持既有字段 ok 不丢
    # 第一步：插件要求二次确认 → 登记并签发令牌（前端弹窗选「是」时带回）
    confirm_action = str(payload.get("confirm") or "").strip()
    if confirm_action:
        target = f"{name}:{confirm_action}"
        _register_confirm_action(name, confirm_action)
        payload["confirm_token"] = confirm_mod.issue(
            CONFIRM_PLUGIN_ACTION, target=target, client=_client_id(req))
        payload["confirm_expires_in"] = int(confirm_mod.DEFAULT_TTL)
    return _json(payload)


def _api_plugins_state(req):
    name = (req.get("query", {}).get("name") or [""])[0]
    return _ok(state=plugin_manager.manager.get_state(name))


def _api_confirm_prepare(req):
    """签发一次确认令牌：危险操作第二步请求必须带上它。

    请求体：{"op": "...", "target": "..."}（op/target 会原样回显，便于前端核对）。
    """
    denied = _require_authorized(req)
    if denied:
        return denied
    data = req.get("json", {}) or {}
    op = str(data.get("op") or "").strip()
    target = str(data.get("target") or "").strip()
    if not op:
        return _error("缺少操作类型（op）")
    token = confirm_mod.issue(op, target=target, client=_client_id(req))
    return _ok(token=token, expires_in=int(confirm_mod.DEFAULT_TTL), op=op, target=target)


def _api_memory_status(_req):
    """记忆能力状态：只问门禁（不触碰引擎），插件停用时也能安全调用。"""
    from memory_engine import service as mem_service
    return _ok(gate=mem_service.status())


# ==================== 记忆管理页面（查看 / 查询 / 编辑 / 增删） ====================
def _memory_engine(level=None):
    """惰性获取记忆引擎：一律经 `memory_engine.service` 门禁，Web 层不再自行 init() / 写库。

    被拒绝（插件停用 / 只读 / 未初始化）时返回 None —— 调用方请改用 `_memory_gate()`，
    以便拿到结构化的拒绝原因与当前模式。
    """
    from memory_engine import service as mem_service
    eng, _reason, _st = mem_service.check(level or mem_service.LEVEL_READ)
    return eng


def _parse_view_date(s):
    """解析日期筛选 / 录入：YYYY / YYYY-MM / YYYY-MM-DD → (start_ts, end_ts, year, quarter) 或 None。"""
    s = (s or "").strip()
    if not s:
        return None
    import re as _re
    m = _re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", s)
    if not m:
        return None
    from datetime import datetime as _dt
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else None
    d = int(m.group(3)) if m.group(3) else None
    if d is not None:
        start = _dt(y, mo, d)
        return start.timestamp(), start.timestamp() + 86400, y, f"Q{(mo - 1) // 3 + 1}"
    if mo is not None:
        start = _dt(y, mo, 1)
        end = _dt(y + (1 if mo == 12 else 0), (1 if mo == 12 else mo + 1), 1)
        return start.timestamp(), end.timestamp(), y, f"Q{(mo - 1) // 3 + 1}"
    return _dt(y, 1, 1).timestamp(), _dt(y + 1, 1, 1).timestamp(), y, None


def _validate_memory_entry(data):
    """校验记忆条目（标准格式）。返回 (ok, 错误信息, 规整字段或 None)。

    标准格式：时间 YYYY[-MM[-DD]]；参与者用中文顿号/逗号分隔；主题可选（标准主题或留空自动归类）；内容 2~1000 字。
    """
    import re as _re
    from memory_engine.preprocessing import TOPIC_NAMES
    date = str(data.get("date") or "").strip()
    participants = str(data.get("participants") or "").strip()
    content = str(data.get("content") or "").strip()
    topic = str(data.get("topic") or "").strip()
    if not _re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", date):
        return False, "时间格式不正确：请使用 2026 或 2026-08 或 2026-08-27 的形式。", None
    parts = [p.strip() for p in _re.split(r"[、,，]", participants) if p.strip()]
    if not parts:
        return False, "参与者不能为空：请用中文顿号分隔，如：洛天依、洛天依（朋友）。", None
    if len(content) < 2 or len(content) > 1000:
        return False, "内容长度需在 2~1000 字之间。", None
    if topic and topic not in TOPIC_NAMES:
        return False, ("主题不在标准范围内（可选：%s；或留空自动归类）。" % "、".join(TOPIC_NAMES)), None
    d = _parse_view_date(date)
    return True, "", {
        "ts": d[0], "year": d[2], "quarter": d[3],
        "participants": parts, "topic": topic, "content": content,
    }


def _api_memory_view(req):
    eng, denied, st = _memory_gate(_memory_read_level())
    if denied:
        return denied
    from memory_engine import roles
    from memory_engine.preprocessing import TOPIC_NAMES
    query = req.get("query", {}) or {}
    q = (query.get("q") or [""])[0].strip()
    drange = _parse_view_date((query.get("date") or [""])[0])
    try:
        limit = int((query.get("limit") or ["200"])[0])
    except Exception:
        limit = 200
    try:
        offset = int((query.get("offset") or ["0"])[0])
    except Exception:
        offset = 0
    limit = max(20, min(500, limit))
    offset = max(0, offset)

    active, archive = [], []
    totals = {"active": 0, "archive": 0}
    ts_from = drange[0] if drange else None
    ts_to = drange[1] if drange else None
    # 分页 + SQL 侧过滤：不再一次性把整库拉进内存与前端 DOM（管理页「数据一多就卡」的主因）
    for key, db, out in (("active", eng.active, active), ("archive", eng.archive_db, archive)):
        if db is None:
            continue
        try:
            rows, total = db.query_rows(text=q, ts_from=ts_from, ts_to=ts_to, limit=limit, offset=offset)
        except Exception:
            rows, total = [], 0
        totals[key] = total
        for r in rows:
            out.append({
                "id": r["id"], "year": r["year"], "quarter": r["quarter"],
                "topic": r["topic"], "ts": r["ts"],
                "participants": roles.render_list(json.loads(r["participants"] or "[]"), eng.char_name),
                "summary": roles.render(r.get("full_summary") or "", eng.char_name),
                "searchable": roles.render((r.get("searchable_text") or "")[:200], eng.char_name),
            })
    l0 = []
    if eng.l0:
        for t in eng.l0.get_recent(eng.l0.max_entries):
            c = t.get("content") or ""
            if q and q not in c:
                continue
            l0.append({"role": t.get("role"), "content": c, "ts": t.get("ts")})
    l1 = {"size": eng.l1.size() if eng.l1 else 0, "stats": eng.l1.stats() if eng.l1 else {}}
    cold = []
    if eng.cold:
        for p in eng.cold.partitions():
            cold.append({"partition": str(p)})
    return _memory_ok(st, l0=l0, l1=l1, active=active, archive=archive, cold=cold, topics=TOPIC_NAMES,
                      limit=limit, offset=offset, totals=totals,
                      has_more={"active": totals["active"] > offset + len(active),
                                "archive": totals["archive"] > offset + len(archive)})


def _api_memory_view_cold(req):
    """L3 冷存储分区详情（点开展示）：partition = 年/季度/主题。"""
    eng, denied, st = _memory_gate(_memory_read_level())
    if denied:
        return denied
    from memory_engine import roles
    parts = (req.get("query", {}).get("partition") or [""])[0].strip().split("/")
    if len(parts) != 3:
        return _error("分区格式应为 年/季度/主题，如 2026/Q3/美食")
    try:
        year, quarter, topic = int(parts[0]), parts[1].upper(), parts[2]
    except Exception:
        return _error("分区格式不正确")
    entries = eng.cold.read_partition(year, quarter, topic) if eng.cold else []
    for e in entries:
        e["full_text"] = roles.render(e.get("full_text") or "", eng.char_name)
    return _memory_ok(st, entries=entries)


def _api_memory_view_add(req):
    """新增记忆：人工管理操作，走 manage 级门禁。"""
    eng, denied, st = _memory_gate(_memory_manage_level())
    if denied:
        return denied
    ok, msg, f = _validate_memory_entry(req.get("json", {}))
    if not ok:
        return _error(msg)
    try:
        r = eng.ingest(f["content"], ts=f["ts"], year=f["year"], quarter=f["quarter"],
                       main_topic=f["topic"] or None, participants=f["participants"])
        return _memory_ok(st, id=r.fragment_id, message="已新增记忆")
    except _write_blocked_class() as e:
        # 引擎底层再次拒绝（并发状态变化），把引擎给的原因原样返回
        return _gate_error(getattr(e, "reason", str(e)), st)
    except Exception as e:
        return _error(f"新增失败：{e}")


def _api_memory_view_update(req):
    """编辑记忆：人工管理操作，走 manage 级门禁。"""
    eng, denied, st = _memory_gate(_memory_manage_level())
    if denied:
        return denied
    data = req.get("json", {})
    fid = str(data.get("id") or "").strip()
    ok, msg, f = _validate_memory_entry(data)
    if not ok:
        return _error(msg)
    if not fid:
        return _error("缺少记忆 id")
    old = None
    for db in (eng.active, eng.archive_db):
        if db is None:
            continue
        row = db.get(fid)
        if row:
            old = row
            break
    if old is None:
        return _error("要编辑的记忆不存在（可能已被归档/合并）")
    try:
        # 删除旧记录（索引 + 向量 + 冷存储原文），再以同 id 重新入库（chunks=False 保证 id 不变）
        for db in (eng.active, eng.archive_db):
            if db is not None:
                db.delete([fid])
        eng.vector.remove([fid])
        eng.cold.remove_ids(old["year"], old["quarter"], old["main_topic"], [fid])
        r = eng.ingest(f["content"], fragment_id=fid, ts=f["ts"], year=f["year"], quarter=f["quarter"],
                       main_topic=f["topic"] or None, participants=f["participants"], chunks=False)
        return _memory_ok(st, id=r.fragment_id, message="已更新记忆")
    except _write_blocked_class() as e:
        return _gate_error(getattr(e, "reason", str(e)), st)
    except Exception as e:
        return _error(f"更新失败：{e}")


def _api_memory_view_delete(req):
    """删除记忆：危险操作，必须带一次性确认令牌（op=memory.delete，target=记忆 id）。

    多层清理（活跃/归档/向量/冷存储）收敛到引擎的公开方法 `engine.delete(id)`，
    由引擎统一受 manage 门禁保护，Web 层不再自己拼删除逻辑。
    """
    data = req.get("json", {}) or {}
    fid = str(data.get("id") or "").strip()
    if not fid:
        return _error("缺少记忆 id")
    denied = _require_authorized(req)
    if denied:
        return denied
    denied = _require_confirm(req, CONFIRM_MEMORY_DELETE, fid)
    if denied:
        return denied
    eng, denied, st = _memory_gate(_memory_manage_level())
    if denied:
        return denied
    try:
        eng.delete(fid)
        return _memory_ok(st, message="已删除记忆", id=fid)
    except _write_blocked_class() as e:
        return _gate_error(getattr(e, "reason", str(e)), st)
    except Exception as e:
        return _error(f"删除失败：{e}")


# ==================== 角色与语音（设置页标签：角色预设 + 语音 + 多人对话勾选） ====================
MULTI_CHAT_PLUGIN = "多人对话"


def _api_characters_manage_get(_req):
    """设置页「角色与语音」数据：全部角色预设、当前勾选、各角色声线、可选声线清单。

    多人对话是否「可用」由插件自己声明（`available()` 握手）：
    只有插件处于启用状态、且勾选了足够角色（≥2）时才 available=True（多角色输出），
    否则前端按「单人输出」展示，并如实给出原因。
    """
    from core import character as character_core
    from core import tts as tts_core
    presets = character_core.load_presets()
    cur_name = getattr(config, "character_name", "")
    items = []
    for n, d in presets.items():
        d = d or {}
        desc = str(d.get("description") or "")
        seg = _split_preset_text(desc)
        # 分类字段优先用保存时显式写入的值；老预设没有这两个字段，则从提示词里解析
        custom_req = str(d.get("custom_req") or seg["custom_req"])
        reference = str(d.get("reference") or seg["reference"])
        try:
            updated = int(os.path.getmtime(os.path.join(config.PRESETS_DIR, f"{n}.json")))
        except OSError:
            updated = 0
        items.append({
            "name": n,
            "description": desc,
            "summary": (custom_req or desc.replace("\n", " "))[:60],
            "custom_req": custom_req,
            "reference": reference,
            "extra": seg["extra"],
            "prompt_len": len(desc),
            "updated_at": updated,
            "is_current": (n == cur_name),
        })
    items.sort(key=lambda x: x["name"])
    try:
        st = plugin_manager.manager.get_settings(MULTI_CHAT_PLUGIN)
    except Exception:
        st = {}
    selected = []
    raw = (st.get("selected_characters") or "").strip()
    if raw:
        try:
            selected = [str(c).strip() for c in json.loads(raw) if str(c).strip()]
        except Exception:
            selected = [c.strip() for c in raw.split(",") if c.strip()]
    else:
        # 回退旧槽位（尚未在设置页保存过勾选）
        selected = [(st.get(f"slot{i}_character") or "").strip() for i in range(1, 4)]
        selected = [c for c in selected if c]
    try:
        vmap = json.loads(st.get("character_voices") or "{}")
        if not isinstance(vmap, dict):
            vmap = {}
    except Exception:
        vmap = {}
    voices = [v.get("name", "") for v in tts_core.list_voice_presets() if v.get("name")]
    # 插件可用性握手（统一由插件管理器裁决：未启用 / 正在重载 → 不可用）
    try:
        availability = plugin_manager.manager.plugin_available(MULTI_CHAT_PLUGIN)
    except Exception as e:
        availability = {"available": False, "enabled": False, "reason": f"可用性检查失败：{e}"}
    enabled = bool(availability.get("enabled"))
    return _ok(presets=items, selected=selected, voices_map=vmap, voices=voices,
               main_character=getattr(config, "character_name", ""),
               plugin_enabled=enabled,
               available=bool(availability.get("available")),
               availability=availability,
               min_roles=int(availability.get("min_roles") or 2),
               reason=availability.get("reason") or "",
               note=availability.get("note") or "",
               can_edit=enabled,
               from_page=bool((st.get("selected_characters") or "").strip()))


def _api_characters_manage_post(req):
    """保存勾选结果到「多人对话」插件设置（selected_characters / character_voices）。

    安全约束（与其它危险写操作一致）：
      1) 必须带客户端标识（HTTP 层已校验，脚本直调时这里再过一次）；
      2) 一次性确认令牌（op=characters.manage，target=多人对话）；
      3) 插件未启用 / 正在重载时拒绝写入（避免写进一个不生效的插件）。
    """
    denied = _require_authorized(req)
    if denied:
        return denied
    try:
        availability = plugin_manager.manager.plugin_available(MULTI_CHAT_PLUGIN)
    except Exception as e:
        return _error(f"多人对话插件不可用：{e}", 409)
    if not availability.get("enabled"):
        return _json({"ok": False, "error": availability.get("reason") or "多人对话插件未启用",
                      "plugin_enabled": False, "need_plugin": True}, 409)
    denied = _require_confirm(req, "characters.manage", MULTI_CHAT_PLUGIN)
    if denied:
        return denied

    from core import character as character_core
    data = req.get("json", {})
    selected = [str(c).strip() for c in (data.get("selected") or []) if str(c).strip()]
    presets = set(character_core.list_presets())
    unknown = [c for c in selected if c not in presets]
    if unknown:
        return _error("存在未知角色预设：%s（请先在「角色与语音」中创建该角色预设）" % "、".join(unknown))
    vmap = data.get("voices") or {}
    if not isinstance(vmap, dict):
        vmap = {}
    patch = {
        "selected_characters": json.dumps(selected, ensure_ascii=False),
        "character_voices": json.dumps({k: str(v or "") for k, v in vmap.items() if k in selected},
                                       ensure_ascii=False),
    }
    try:
        plugin_manager.manager.save_settings(MULTI_CHAT_PLUGIN, patch)
    except ValueError as e:
        return _error(e)
    # 保存后立刻回读插件可用性：前端据此显示「已激活 / 仍为单人输出」
    try:
        after = plugin_manager.manager.plugin_available(MULTI_CHAT_PLUGIN)
    except Exception:
        after = {}
    return _ok(selected=selected, count=len(selected),
               available=bool(after.get("available")),
               availability=after,
               reason=after.get("reason") or "",
               note=after.get("note") or "")


def _split_preset_text(text: str) -> dict:
    """把角色预设的提示词文本拆成分类片段（供卡片按行分类展示）。

    历史预设的 description 由「自定义要求：…；搜索结果：…」拼成，这里做无损拆分：
    能识别的段分别落到 custom_req / reference，其余原样归入 extra。
    """
    text = str(text or "").strip()
    out = {"custom_req": "", "reference": "", "extra": ""}
    if not text:
        return out
    for seg in re.split(r"[；;]\s*(?=(?:自定义要求|搜索结果)[:：])", text):
        s = seg.strip()
        if s.startswith("自定义要求：") or s.startswith("自定义要求:"):
            out["custom_req"] += ("" if not out["custom_req"] else "；") + s.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif s.startswith("搜索结果：") or s.startswith("搜索结果:"):
            out["reference"] += ("" if not out["reference"] else "；") + s.split("：", 1)[-1].split(":", 1)[-1].strip()
        else:
            out["extra"] += ("" if not out["extra"] else "；") + s
    return out


def _api_characters_preset_save(req):
    """保存角色预设的提示词（修改设定 / 新建角色）。

    安全约束：客户端标识（HTTP 层已校验）+ 一次性确认令牌（op=characters.preset，target=角色名）。
    """
    denied = _require_authorized(req)
    if denied:
        return denied
    from core import character as character_core
    data = req.get("json", {})
    name = str(data.get("name") or "").strip()
    create = bool(data.get("create"))
    if not name:
        return _error("角色名不能为空")
    if len(name) > 40:
        return _error("角色名过长（最多 40 字）")
    presets = character_core.load_presets()
    if create and name in presets:
        return _error(f"角色预设「{name}」已存在")
    if not create and name not in presets:
        return _error(f"角色预设「{name}」不存在")
    denied = _require_confirm(req, "characters.create" if create else "characters.preset", name)
    if denied:
        return denied

    prompt = str(data.get("prompt") or "").strip()
    custom_req = str(data.get("custom_req") or "").strip()
    reference = str(data.get("reference") or "").strip()
    presets = character_core.load_presets()
    prev = dict(presets.get(name) or {})
    prev_desc = str(prev.get("description") or "")
    # 提示词全文未改动、只改了「自定义要求 / 参考资料」时，按历史格式重建提示词，
    # 保证这两个分类字段真的会影响实际发给模型的设定（而不是只改展示）
    if prompt == prev_desc and (custom_req or reference):
        seg = _split_preset_text(prev_desc)
        parts = []
        if custom_req:
            parts.append(f"自定义要求：{custom_req}")
        if reference:
            parts.append(f"搜索结果：{reference}")
        if seg["extra"]:
            parts.append(seg["extra"])
        prompt = "；".join(parts)
    if not prompt and not custom_req:
        return _error("提示词不能为空（至少保留人物设定或自定义要求）")
    if len(prompt) > 20000:
        return _error("提示词过长（最多 20000 字）")
    payload = dict(prev)
    payload.update({"description": prompt, "custom_req": custom_req, "reference": reference,
                    "updated_at": int(time.time())})
    try:
        character_core.save_preset(name, payload)
    except Exception as e:
        return _error(f"保存失败：{e}", 500)
    logger.info(f"角色预设已保存：{name}（{'新建' if create else '修改'}，提示词 {len(prompt)} 字）")
    return _ok(name=name, created=create, prompt_len=len(prompt))


def _api_characters_preset_optimize(req):
    """用 LLM 优化角色提示词（只返回优化结果，不写盘；应用仍需保存令牌）。"""
    denied = _require_authorized(req)
    if denied:
        return denied
    data = req.get("json", {})
    name = str(data.get("name") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return _error("请先填写需要优化的提示词")
    if len(prompt) > 20000:
        return _error("提示词过长（最多 20000 字）")
    try:
        from core import llm as llm_core
        ordered = (
            "你是角色扮演提示词的编辑。请在【不改变角色身份与事实】的前提下优化下面这段角色设定提示词：\n"
            "1) 去掉网页残留（导航、脚注、表格、外链文字、重复空白）；\n"
            "2) 归纳为结构清晰的中文条目（身份、性格、说话风格、与用户的关系、禁忌）；\n"
            "3) 保留原有专有名词与关键设定，不要新增虚构事实；\n"
            "4) 只输出优化后的提示词正文，不要任何解释、标题或 Markdown 代码块符号。\n\n"
            f"角色名：{name or '（未命名）'}\n原始提示词：\n{prompt}"
        )
        optimized = llm_core.generate(ordered, num_predict=1400, temperature=0.3,
                                      purpose="prompt_optimize")
        optimized = (optimized or "").strip()
        if not optimized:
            return _error("优化失败：模型没有返回内容（请检查生成模型是否可用）", 502)
        return _ok(name=name, original=prompt, optimized=optimized,
                   model=getattr(config, "LLM_CHAT_MODEL", ""))
    except Exception as e:
        return _error(f"优化失败：{e}", 502)


def _api_characters_preset_reference(req):
    """联网搜索角色资料（供「联网搜索补全设定」按钮把结果填进「参考资料」）。"""
    denied = _require_authorized(req)
    if denied:
        return denied
    data = req.get("json", {})
    name = str(data.get("name") or "").strip()
    requirement = str(data.get("requirement") or "").strip()
    if not name:
        return _error("请先填写角色名")
    try:
        from core import search as search_core
        query = " ".join([p for p in (name, requirement) if p]) + " 角色扮演 说话风格 性格特点 背景设定"
        info = search_core.get_usage_info()
        if not info.get("remaining"):
            return _error("联网额度已用完，请到「通用设置 → 联网额度」重置，或手工填写参考资料", 429)
        data_out = search_core.search_tavily(query)
    except Exception as e:
        return _error(f"联网搜索失败：{e}", 502)
    text = ""
    if isinstance(data_out, str):
        text = data_out
    elif isinstance(data_out, dict):
        snippets = [r.get("content", "") for r in (data_out.get("results") or [])[:3] if r.get("content")]
        text = "；".join(snippets[:2])
    text = " ".join(str(text).split())
    if not text:
        return _error("联网搜索没有返回可用内容（请检查「联网搜索设置」里的 API Key）", 502)
    logger.info(f"角色资料联网搜索完成：{name}（{len(text)} 字）")
    return _ok(name=name, text=text[:4000], remaining=search_core.get_usage_info().get("remaining"))


def _api_tts_service_get(_req):
    """语音合成服务（GPT-SoVITS）状态：就绪 / 端口 / 日志路径 / 上次失败原因。

    端口可能不是 9880：Windows 上 9880 可能落在系统保留段（Hyper-V/WSL/Docker 预留），
    程序会自动换到可用端口，这里如实回显实际端口。
    """
    from core import services
    return _ok(service=services.gpt_sovits_status())


def _api_tts_service_post(req):
    """启动 / 重启 / 停止语音合成服务（本地服务，非破坏性操作）。"""
    from core import services
    action = str((req.get("json", {}) or {}).get("action") or "restart")
    cfg = services.load_launcher_config()
    if action == "stop":
        ok = services.stop_gpt_sovits()
        return _ok(action=action, stopped=bool(ok), service=services.gpt_sovits_status(cfg))
    if action == "start":
        started, info = services.start_gpt_sovits(services.load_launcher_config())
    else:
        started, info = services.restart_gpt_sovits(cfg)
    services.apply_api_urls(services.load_launcher_config())
    service = services.gpt_sovits_status()
    reason = info.get("reason", "")
    message = {
        "started": "语音服务已启动（模型加载需要一会儿，状态会自动刷新）。",
        "running": "语音服务已在运行。",
        "exited": f"语音服务启动后立刻退出：{info.get('error') or '请查看日志'}",
        "no_script": "未找到 GPT-SoVITS 的 api_v2.py，无法启动语音服务。",
        "disabled": "配置里已禁用 GPT-SoVITS。",
        "spawn_failed": f"启动失败：{info.get('error') or '未知错误'}",
    }.get(reason, "已处理。")
    if reason == "exited" and info.get("log_tail"):
        message += "\n日志尾部：\n" + info["log_tail"][-600:]
    return _ok(action=action, started=bool(started), reason=reason, message=message, service=service)


def _api_launch_state(_req):
    """图形启动器：当前启动进度（所选模式 / 每个步骤状态 / 语音服务是否就绪）。

    启动页在等待期间轮询本接口；无论是否启动过都能安全查询。
    """
    from core import launch_flow
    st = launch_flow.state()
    return _ok(state=st, modes=launch_flow.modes(), app_mode=config.APP_MODE)


def _api_launch_start(req):
    """图形启动器：按所选模式拉起服务（lite = 仅语音合成；standard = 全部本地服务）。

    非破坏性本地操作，不需要确认令牌；重复点击是幂等的（启动中直接返回当前状态）。
    """
    from core import launch_flow
    data = req.get("json", {}) or {}
    mode = str(data.get("mode") or "").strip().lower()
    try:
        st = launch_flow.start(mode)
    except ValueError as e:
        return _error(str(e), 400)
    label = (launch_flow.MODES.get(mode) or {}).get("label", mode)
    return _ok(state=st, mode=mode, message=f"已开始按「{label}」启动服务。")


def _api_app_beat(_req):
    """界面心跳：只要还有窗口在，就持续上报（用于判断「窗口关了 = 程序该退出」）。"""
    from web import app_lifecycle
    return _ok(lifecycle=app_lifecycle.beat())


def _api_app_window_closed(_req):
    """窗口关闭信标：宽限期内没有新的心跳就执行完整退出（含停止本地服务）。"""
    from web import app_lifecycle
    return _ok(lifecycle=app_lifecycle.window_closed())


def _api_app_lifecycle(_req):
    """当前生命周期状态（排查「关窗口后进程是否还在」时看这里）。"""
    from web import app_lifecycle
    return _ok(lifecycle=app_lifecycle.state())


def _api_app_shutdown(req):
    """完全退出程序（二级确认后执行）：卸载模型权重 + 停止本地服务 + 完整收尾 + 结束进程。

    界面上不再放按钮（关闭方式改为点窗口 X 时由浏览器确认，见 web/app_lifecycle.py）；
    本接口保留给脚本 / 自动化 / 外部工具使用：先请求确认令牌（/api/confirm/prepare，
    op=app.shutdown），再带令牌调用本接口。先返回结果再在后台执行退出，保证调用方收到回复。
    """
    from web import app_lifecycle
    denied = _require_confirm(req, CONFIRM_APP_SHUTDOWN, "*")
    if denied:
        return denied
    # 保证退出流程包含 TTS 流式任务清理（即使不是图形启动器模式）
    app_lifecycle.enable(app_lifecycle.is_enabled(),
                         extra_steps=[("停止 TTS 流式任务", _stop_stream_tasks)])

    def _work():
        time.sleep(0.4)      # 让 HTTP 响应先发出去
        app_lifecycle.shutdown_now("user-confirm")

    threading.Thread(target=_work, daemon=True).start()
    return _ok(message="已开始退出：正在停止本地服务并清理缓存，随后关闭界面。",
               steps=["停止接受新请求", "停止归档并关闭记忆引擎", "停止 TTS 流式任务",
                      "停止本地服务（GPT-SoVITS / Ollama）", "清理运行时缓存"])


def _api_multi_chat_poll(req):
    """轮询「多人对话」剧本模式的流式生成结果（分段返回，客户端按 seq 去重）。"""
    sid = (req.get("query", {}).get("id") or [""])[0]
    mod = plugin_manager.manager.plugin_module("多人对话")
    if not mod or not callable(getattr(mod, "poll_stream", None)):
        return _error("多人对话插件不可用", 404)
    try:
        segments, done, error = mod.poll_stream(sid)
    except Exception as e:
        return _error(f"轮询失败: {e}", 500)
    return _ok(segments=segments, done=done, error=error)


def _api_models_get(_req):
    shared = (
        config.LLM_CHAT_BACKEND == "ollama"
        and config.LLM_JUDGE_BACKEND == "ollama"
        and config.LLM_CHAT_MODEL == config.LLM_JUDGE_MODEL
    )
    # 仅当实际使用 Ollama 时才返回其模型列表（Lite 模式/外部 API 下返回空，
    # 避免界面显示本机 Ollama 模型名造成误导）
    return _ok(
        chat_backend=config.LLM_CHAT_BACKEND,
        judge_backend=config.LLM_JUDGE_BACKEND,
        chat_model=config.LLM_CHAT_MODEL,
        judge_model=config.LLM_JUDGE_MODEL,
        shared=shared,
        ollama_models=llm.list_ollama_models() if llm.ollama_used() else [],
    )


# 背景图片内存缓存（避免每次页面加载都重读磁盘，key=路径+mtime+大小）
_bg_image_cache = {"key": None, "body": None, "ctype": None}


def _bg_image_path():
    """返回「背景设置」插件配置的本地背景图绝对路径；未配置 / 网络图 / 文件不存在时返回 None。

    路径统一走 core.paths 规范化：用户粘贴的路径常带引号（"D:\\a.jpg"、“D:\\a.jpg”）
    或 file:/// 前缀，直接判断会当成文件不存在、背景图返回 404。
    """
    try:
        s = plugin_manager.manager.get_settings("背景设置")
    except Exception:
        return None
    img = ((s or {}).get("image") or "").strip()
    if not img or img.lower().startswith(("http://", "https://", "data:", "/")):
        return None
    p = paths.resolve_user_path(img, config.PROJECT_ROOT)
    return p if p and os.path.isfile(p) else None


def _bg_image_etag():
    """背景图 ETag（路径 + 修改时间 + 大小）。

    背景图只有 1 个固定地址，浏览器默认会被 no-store 强制每次重新下载整张图
    （进设置页 / 返回对话都要重来一遍，图片大时明显卡顿）。这里给出 ETag，
    浏览器可命中缓存、变化时再取新图。
    """
    p = _bg_image_path()
    if not p:
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    return f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'


def _api_background_image(_req):
    """提供“背景设置”插件配置的本地图片（浏览器无法直接加载本地文件路径，
    由本端点读取并返回；http(s)/data:/以 / 开头的地址由前端直接加载）。"""
    try:
        plugin_manager.manager.get_settings("背景设置")
    except Exception:
        return _error("背景设置插件不可用", 500)
    p = _bg_image_path()
    if not p:
        return _error("未配置本地背景图片（或文件不存在）", 404)
    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    key = None
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
    except Exception:
        key = None
    if key and _bg_image_cache.get("key") == key:
        return 200, _bg_image_cache["body"], _bg_image_cache["ctype"]
    try:
        with open(p, "rb") as f:
            body = f.read()
    except Exception as e:
        return _error(f"读取图片失败：{e}", 500)
    if key:
        _bg_image_cache.update(key=key, body=body, ctype=ctype)
    return 200, body, ctype


# ==================== 路由表 ====================
ROUTES = {
    ("GET", "/api/status"): _api_status,
    ("GET", "/api/settings"): _api_settings_get,
    ("POST", "/api/settings"): _api_settings_post,
    ("GET", "/api/characters"): _api_characters_get,
    ("POST", "/api/character"): _api_character_post,
    ("GET", "/api/background/image"): _api_background_image,
    ("GET", "/api/voice_presets"): _api_voice_presets_get,
    ("POST", "/api/voice_preset"): _api_voice_preset_post,
    ("GET", "/api/history"): _api_history_get,
    ("POST", "/api/history/mark"): _api_history_mark,
    ("POST", "/api/history/clear"): _api_history_clear,
    ("POST", "/api/save"): _api_save,
    ("GET", "/api/usage"): _api_usage_get,
    ("POST", "/api/usage"): _api_usage_post,
    ("POST", "/api/chat"): _api_chat,
    ("GET", "/api/tts/next"): _api_tts_next,
    ("GET", "/api/tts/service"): _api_tts_service_get,
    ("POST", "/api/tts/service"): _api_tts_service_post,
    ("GET", "/api/launch/state"): _api_launch_state,
    ("POST", "/api/launch/start"): _api_launch_start,
    ("POST", "/api/app/beat"): _api_app_beat,
    ("POST", "/api/app/window-closed"): _api_app_window_closed,
    ("GET", "/api/app/lifecycle"): _api_app_lifecycle,
    ("POST", "/api/app/shutdown"): _api_app_shutdown,
    ("POST", "/api/transcribe"): _api_transcribe,
    ("GET", "/api/music/search"): _api_music_search,
    ("POST", "/api/music/play"): _api_music_play,
    ("POST", "/api/music/ended"): _api_music_ended,
    ("POST", "/api/music/stop"): _api_music_stop,
    ("GET", "/api/plugins"): _api_plugins_get,
    ("POST", "/api/plugins/reload"): _api_plugins_reload,
    ("POST", "/api/plugins/enable"): _api_plugins_enable,
    ("POST", "/api/plugins/disable"): _api_plugins_disable,
    ("POST", "/api/plugins/settings"): _api_plugins_settings,
    ("POST", "/api/plugins/action"): _api_plugins_action,
    ("GET", "/api/plugins/state"): _api_plugins_state,
    ("POST", "/api/confirm/prepare"): _api_confirm_prepare,
    ("GET", "/api/memory/status"): _api_memory_status,
    ("GET", "/api/memory/view"): _api_memory_view,
    ("GET", "/api/memory/view/cold"): _api_memory_view_cold,
    ("POST", "/api/memory/view/add"): _api_memory_view_add,
    ("POST", "/api/memory/view/update"): _api_memory_view_update,
    ("POST", "/api/memory/view/delete"): _api_memory_view_delete,
    ("GET", "/api/characters/manage"): _api_characters_manage_get,
    ("POST", "/api/characters/manage"): _api_characters_manage_post,
    ("POST", "/api/characters/preset/save"): _api_characters_preset_save,
    ("POST", "/api/characters/preset/optimize"): _api_characters_preset_optimize,
    ("POST", "/api/characters/preset/reference"): _api_characters_preset_reference,
    ("GET", "/api/multi_chat/poll"): _api_multi_chat_poll,
    ("GET", "/api/models"): _api_models_get,
}


# ==================== 静态文件 ====================
def _safe_join(base, rel):
    """防止路径穿越。"""
    rel = rel.lstrip("/").replace("\\", "/")
    target = os.path.abspath(os.path.join(base, rel))
    base_abs = os.path.abspath(base)
    if target == base_abs or target.startswith(base_abs + os.sep):
        return target
    return None


def _serve_file(path):
    if not path or not os.path.isfile(path):
        return None
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        body = f.read()
    # 带 Accept-Ranges：音频要能「拖动跳转」（浏览器靠 Range 请求取指定片段，
    # 不支持 Range 时 Chromium 会把拖动当成从头重新加载，表现就是「一跳就回到开头」）
    return 200, body, ctype, {"Accept-Ranges": "bytes"}


def parse_range(header, size):
    """解析单段 Range 请求头，返回闭区间 (start, end)。

    只支持最常见的 `bytes=a-b` / `bytes=a-` / `bytes=-n`；不合法或越界返回 None
    （调用方按 416 处理）。多段 Range（逗号分隔）不支持，按整段返回。
    """
    try:
        text = (header or "").strip()
        if not text.startswith("bytes=") or "," in text or size <= 0:
            return None
        spec = text[len("bytes="):].strip()
        m = re.match(r"^(\d*)-(\d*)$", spec)
        if not m:
            return None
        first, last = m.group(1), m.group(2)
        if not first and not last:
            return None
        if not first:                       # bytes=-n：最后 n 字节
            n = int(last)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(first)
        if start >= size:
            return None
        end = int(last) if last else size - 1
        if end < start:
            return None
        return start, min(end, size - 1)
    except Exception:
        return None


def _apply_range(range_header, result):
    """把整段文件响应切成 206 分片（音频拖动跳转 / 断点续传用）。

    不支持 Range 的调用方（普通页面 / JSON）原样返回；Range 不合法时按 416 回复并
    带上 `Content-Range: bytes */总长`，浏览器据此知道资源本身是可跳转的。
    """
    if not range_header or not result:
        return result
    if len(result) == 4:
        status, body, ctype, extra = result
    else:
        status, body, ctype = result
        extra = {}
    if status != 200 or not extra.get("Accept-Ranges") or not body:
        return result
    size = len(body)
    span = parse_range(range_header, size)
    if span is None:
        headers = {"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"}
        return 416, b"", ctype, headers
    start, end = span
    headers = dict(extra)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return 206, body[start:end + 1], ctype, headers


# ==================== 背景主题内联注入 ====================
def _theme_embed():
    """返回“背景设置”插件的当前设置，供页面内联脚本同步应用主题，
    避免页面加载时先显示默认深色再切换（进入/退出页面时的闪烁）。"""
    try:
        for p in plugin_manager.manager.list_plugins():
            if p.get("name") == "背景设置":
                return {"name": "背景设置", "settings": p.get("settings") or {}}
    except Exception:
        pass
    return {}


def _theme_inline_script():
    """生成内联脚本：在页面解析完成前同步应用背景主题并预加载背景图。

    注意：这里是在拼 JS 源码，字符串引号必须成对（此前 url() 的引号拼错会导致
    整段脚本语法错误 → 页面首帧不跟随主题）。下面的写法统一用单引号 + JSON.stringify
    处理 URL，避免再有嵌套引号问题。
    """
    data = json.dumps(_theme_embed(), ensure_ascii=False).replace("<", "\\u003c")
    return (
        "<script>window.__THEME__=" + data + ";\n"
        "(function(){"
        "var t=(window.__THEME__||{}).settings||{};"
        "var m=t.mode||'dark';var b=document.body;"
        # 主题类互斥：先清掉可能残留的另一个主题类，避免浅色与图片主题同时生效
        # （两个类都命中时 --surface/--surface-2 会取到图片主题的深色值，左侧栏与输入框发黑）
        "b.classList.remove('theme-light','theme-image');"
        "if(m==='light'){b.classList.add('theme-light');}"
        "else if(m==='image'){b.classList.add('theme-image');"
        "var i=String(t.image||'').trim(),src='';"
        "if(i){"
        "if(/^(https?:|data:|\\/)/i.test(i)){src=i;b.style.setProperty('--bg-image','url('+JSON.stringify(i)+')');}"
        "else{src='/api/background/image';b.style.setProperty('--bg-image',\"url('/api/background/image')\");}"
        "new Image().src=src;}"
        "b.style.setProperty('--bg-strength',String(t.strength!=null?t.strength:0.8));}"
        "})();</script>"
    )


_BODY_OPEN_RE = re.compile(rb"<body[^>]*>", re.IGNORECASE)


def _inject_theme(result):
    """把背景主题内联脚本注入到 <body> 起始处（在内容解析前即应用主题，
    避免首帧先闪一下默认主题再切成背景图）。"""
    if result is None:
        return None
    # 文件响应可能是 4 元组（多带 Accept-Ranges 这类头），注入时原样保留
    if len(result) == 4:
        status, body, ctype, extra = result
    else:
        status, body, ctype = result
        extra = None
    script = _theme_inline_script().encode("utf-8")
    m = _BODY_OPEN_RE.search(body)
    if m:
        body = body[:m.end()] + script + body[m.end():]
    elif b"</body>" in body:
        body = body.replace(b"</body>", script + b"</body>", 1)
    if extra is not None:
        return status, body, ctype, extra
    return status, body, ctype


def _serve_static(path):
    if path in ("/", "/index.html"):
        # 图形启动器拉起、但还没选模式时：主页面先显示启动页，避免直接落到没有服务的对话界面
        if getattr(config, "LAUNCH_VIA_GUI", False) and not launch_flow.is_done():
            return _inject_theme(_serve_file(os.path.join(WEB_DIR, "launcher.html")))
        return _inject_theme(_serve_file(INDEX_FILE))
    if path.endswith(".html"):
        return _inject_theme(_serve_file(_safe_join(WEB_DIR, path.lstrip("/"))))
    if path.startswith("/static/"):
        return _serve_file(_safe_join(STATIC_DIR, path[len("/static/"):]))
    if path.startswith("/runtime/"):
        return _serve_file(_safe_join(config.RUNTIME_DIR, path[len("/runtime/"):]))
    return None


# ==================== HTTP 处理器 ====================
class Handler(BaseHTTPRequestHandler):
    server_version = version.server_token()

    def _dispatch(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 任何请求都算「窗口还活着」：旧版页面没有心跳脚本时，靠这个判断窗口是否已关闭
        try:
            from web import app_lifecycle
            app_lifecycle.touch()
        except Exception:
            pass

        if path.startswith("/api/"):
            key = (self.command, path)
            handler = ROUTES.get(key)
            if handler is None:
                return _error("接口不存在", 404)
            body = _read_body(self) if self.command == "POST" else b""
            req = {"query": query, "body": body}
            if self.command == "POST":
                try:
                    req["json"] = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    req["json"] = {}
            # 请求头也放进 req：令牌校验 / 客户端标识 / 同源校验都从 req["headers"] 读，
            # 这样脚本直接调用 handler（绕过 HTTP 层）时也能构造 req 进行测试。
            req["headers"] = {k.lower(): v for k, v in self.headers.items()}
            if self.command == "POST":
                denied = _guard_post_headers(req)
                if denied is not None:
                    return denied
            try:
                return handler(req)
            except Exception as e:
                traceback.print_exc()
                try:
                    logger.error(f"API 接口异常 [{self.command} {path}]：{e}\n{traceback.format_exc()}")
                except Exception:
                    pass
                return _error(f"服务器内部错误: {e}", 500)

        # 静态文件
        result = _serve_static(path)
        if result is None:
            return _error("页面不存在", 404)
        return _apply_range(self.headers.get("Range"), result)

    def _respond(self, result):
        extra = {}
        if len(result) == 4:
            status, body, ctype, extra = result
        else:
            status, body, ctype = result
        # 背景图带 ETag：命中则 304（不重传整张图）。
        # 这里用 no-cache（每次使用前都带 If-None-Match 校验）而不是 max-age：
        # 背景图地址固定是 /api/background/image，换图后 URL 不变，
        # 若给 max-age 浏览器会在缓存有效期内直接用旧图，表现为「改了路径不生效」。
        etag = None
        if status == 200 and urllib.parse.urlparse(self.path).path == "/api/background/image":
            etag = _bg_image_etag()
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache, must-revalidate")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if etag:
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache, must-revalidate")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._respond(self._dispatch())

    def do_POST(self):
        self._respond(self._dispatch())

    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志，避免刷屏


class _NoReuseServer(ThreadingHTTPServer):
    """关闭 SO_REUSEADDR：Windows 下复用地址会导致同一端口被重复绑定，
    使端口冲突检测与顺延失效（两个实例抢占同一端口）。"""

    allow_reuse_address = False


def create_server(host=None, port=None):
    """创建 HTTP 服务器（端口策略：默认端口 -> 随机端口 -> 系统分配）。

    优先绑定默认端口（10999）；若被占用，则依次尝试随机高位端口；
    随机端口也全部失败时，绑定 0 让操作系统分配一个保证空闲的端口。
    始终返回实际绑定的服务器，实际端口见 server.server_address[1]。
    """
    host = host or config.WEB_HOST
    port = port or config.WEB_PORT
    candidates = [port]
    for _ in range(3):
        candidates.append(random.randint(20000, 60000))
    candidates.append(0)  # 由操作系统分配空闲端口（保证成功）
    last_error = None
    for candidate in candidates:
        try:
            return _NoReuseServer((host, candidate), Handler)
        except OSError as e:
            last_error = e
    raise last_error


def _stop_stream_tasks():
    """停止 TTS 流式任务（统一退出流程的一个步骤）。

    _streams 里保存的是 TtsStreamer 与其创建时间；若流式对象提供了停止方法就调用它，
    没有就清空注册表并记录日志（已创建的音频文件由 runtime 清理步骤负责删除）。
    """
    stopped, no_stop = 0, 0
    with _streams_lock:
        entries = list(_streams.items())
        _streams.clear()
    for sid, entry in entries:
        streamer = entry[0] if isinstance(entry, (tuple, list)) and entry else None
        stop = getattr(streamer, "stop", None) or getattr(streamer, "close", None)
        if callable(stop):
            try:
                stop()
                stopped += 1
                continue
            except Exception:
                no_stop += 1
                continue
        no_stop += 1
    detail = f"清理 {len(entries)} 个流式会话（调用停止方法 {stopped} 个，仅清空引用 {no_stop} 个）"
    try:
        logger.info(f"退出流程 · 停止 TTS 流式任务：{detail}")
    except Exception:
        pass
    return detail


def serve(host=None, port=None, open_browser=True, on_ready=None):
    """启动 Web 界面：确保目录、创建服务器（端口被占时改用随机端口）、自检并打开界面。

    on_ready: 可选回调，服务绑定并自检通过后以实际地址调用（桌面版窗口用）。
    """
    from core import shutdown as shutdown_mod
    config.ensure_dirs()
    _start_cleanup_loop()
    atexit.register(runtime.cleanup_runtime)
    logger.init_log(os.path.join(config.RUNTIME_DIR, "logs"))
    host = host or config.WEB_HOST
    port = port or config.WEB_PORT

    # 若目标端口上已有本程序的服务在运行，则直接打开浏览器/窗口，不重复启动
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as resp:
            if resp.status == 200:
                url = f"http://{host}:{port}"
                print(f"检测到 Web 界面已在运行：{url}（不重复启动）。")
                if on_ready is not None:
                    on_ready(url)
                elif open_browser:
                    try:
                        import webbrowser
                        webbrowser.open(url)
                    except Exception:
                        pass
                return
    except Exception:
        pass

    try:
        server = create_server(host, port)
    except OSError:
        print("Web 端口（含随机兜底端口）均无法绑定，无法启动 Web 界面。")
        print("请关闭占用端口的程序后重试，或在 launcher_config.json 的 web.port 中更换端口。")
        raise
    actual_host, actual_port = server.server_address[0], server.server_address[1]
    url = f"http://{actual_host}:{actual_port}"
    if actual_port != port:
        print(f"端口 {port} 已被占用，已改用端口 {actual_port}。")
        logger.warn(f"端口 {port} 已被占用，已改用端口 {actual_port}")
    print(f"小笼洛包 Web 界面已启动：{url}")
    logger.info(f"Web 界面已启动：{url}")

    def _bootstrap():
        # 等服务开始接受请求后：本地自检 + 打开浏览器 / 通知就绪
        time.sleep(0.3)
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                print(f"本地访问自检：HTTP {resp.status}，访问正常。")
                logger.info(f"本地访问自检：HTTP {resp.status}")
        except Exception as e:
            print(f"本地访问自检失败：{e}")
            print("提示：若浏览器仍无法打开，请运行 setup\\诊断.bat 检查环境（代理/防火墙/端口占用）。")
            logger.error(f"本地访问自检失败：{e}")
        if on_ready is not None:
            on_ready(url)
        elif open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass

    threading.Thread(target=_bootstrap, daemon=True).start()

    # 图形启动器模式：接入「关闭窗口 = 完全关闭程序」（心跳 + 关闭信标 + 看门狗）
    try:
        from web import app_lifecycle
        if getattr(config, "LAUNCH_VIA_GUI", False):
            app_lifecycle.enable(
                True,
                stop_server=lambda: server.server_close(),
                extra_steps=[("停止 TTS 流式任务", _stop_stream_tasks)],
            )
            logger.info("已启用「关闭窗口即退出程序」（图形启动器模式）")
    except Exception as e:
        logger.warn(f"启用窗口关闭检测失败：{e}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    except OSError:
        # 退出流程会关闭监听 socket，serve_forever 的 select 随之报错，属正常收尾；
        # 只有「既没在退出、也没退完」的 OSError 才是真异常。
        if not (shutdown_mod.is_done() or shutdown_mod.is_running()):
            raise
    finally:
        # 统一退出流程：停止接受新请求 → 停止新归档 → 等归档排空 → 关记忆引擎 →
        # 停 TTS 流式任务 → 停止本地服务 → 清理运行时临时文件。
        # server_close() 只关闭监听 socket，正在处理的请求由线程池收尾。
        shutdown_mod.shutdown(
            "normal",
            stop_server=lambda: server.server_close(),
            extra_steps=[("停止 TTS 流式任务", _stop_stream_tasks),
                         ("停止本地服务（GPT-SoVITS / Ollama）", services.stop_all)],
        )


def run(host=None, port=None):
    """兼容入口：不自动打开浏览器。"""
    serve(host, port, open_browser=False)
