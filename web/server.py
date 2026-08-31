# web/server.py
# 基于标准库 http.server 的轻量 Web 服务（零额外依赖），提供页面与 JSON API。
import atexit
import json
import mimetypes
import os
import random
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
    plugin_manager,
    logger,
)

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")
INDEX_FILE = os.path.join(WEB_DIR, "index.html")

_http_lock = threading.Lock()

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
def _runtime_url(abs_path):
    """把 runtime 下的绝对路径转换为可访问的 URL。"""
    rel = os.path.relpath(abs_path, config.RUNTIME_DIR).replace("\\", "/")
    return "/runtime/" + rel


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


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


# ==================== 状态 / 设置 ====================
def _settings_snapshot():
    return {
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


def _status_snapshot():
    return {
        "llm_ready": llm.check_backend(),
        "judge_ready": llm.check_judge_backend(),
        # Ollama 状态必须反映"实际使用"：仅当生成/判断后端为 ollama 且服务可用时才为真
        "ollama_used": llm.ollama_used(),
        "ollama_ready": llm.ollama_used() and llm.check_ollama(),
        "app_mode": config.APP_MODE,
        "llm_backend": config.LLM_CHAT_BACKEND,
        "llm_judge_backend": config.LLM_JUDGE_BACKEND,
        "llm_model": config.LLM_CHAT_MODEL,
        "tts_api_ready": tts.check_tts_api(),
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


def _api_history_clear(_req):
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
    """若结果需要语音播报，为其创建流式合成任务并返回 stream_id。"""
    result["stream_id"] = None
    if result.get("speak") and result.get("reply"):
        sid = uuid.uuid4().hex
        with _streams_lock:
            _streams[sid] = (tts.TtsStreamer(result["reply"]), time.time())
        result["stream_id"] = sid
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
        return _ok(audio=None, done=True)

    streamer, _ts = entry
    path, done = streamer.get(timeout=25)
    if done:
        with _streams_lock:
            _streams.pop(sid, None)
        return _ok(audio=None, done=True)
    if path is None:
        return _ok(audio=None, done=False)
    return _ok(audio=_runtime_url(path), done=False)


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

    path = music.download_music(url, title)
    if not path:
        return _error("音乐下载失败")

    intro_text, intro_audio = pipeline.music_intro_prompt(title or keyword, keyword)
    return _ok(
        music_url=_runtime_url(path),
        intro_text=intro_text,
        intro_audio=_urls(intro_audio),
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


def _api_plugins_reload(_req):
    errors = plugin_manager.manager.reload()
    return _ok(plugins=plugin_manager.manager.list_plugins(), errors=errors)


def _api_plugins_enable(req):
    name = req.get("json", {}).get("name", "")
    try:
        plugin_manager.manager.enable(name)
    except ValueError as e:
        return _error(e)
    return _ok(plugins=plugin_manager.manager.list_plugins())


def _api_plugins_disable(req):
    name = req.get("json", {}).get("name", "")
    try:
        plugin_manager.manager.disable(name)
    except ValueError as e:
        return _error(e)
    return _ok(plugins=plugin_manager.manager.list_plugins())


def _api_plugins_settings(req):
    data = req.get("json", {})
    name = data.get("name", "")
    settings = data.get("settings", {})
    try:
        merged = plugin_manager.manager.save_settings(name, settings)
    except ValueError as e:
        return _error(e)
    return _ok(settings=merged)


def _api_plugins_action(req):
    data = req.get("json", {})
    name = data.get("name", "")
    action = data.get("action", "")
    try:
        result = plugin_manager.manager.run_action(name, action)
    except ValueError as e:
        return _error(e)
    if result is None:
        return _error("动作不存在")
    return _json(_attach_tts_stream(result))


def _api_plugins_state(req):
    name = (req.get("query", {}).get("name") or [""])[0]
    return _ok(state=plugin_manager.manager.get_state(name))


# ==================== 记忆管理页面（查看 / 查询 / 编辑 / 增删） ====================
def _memory_engine():
    """惰性获取记忆引擎（不可用时返回 None）。"""
    try:
        from memory_engine import get_engine
        eng = get_engine()
        if not eng.is_ready():
            eng.init()
        return eng if eng.is_ready() else None
    except Exception:
        return None


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
    eng = _memory_engine()
    if eng is None:
        return _error("记忆库不可用（请先启用「上下文记忆库」插件）")
    from memory_engine import roles
    from memory_engine.preprocessing import TOPIC_NAMES
    q = (req.get("query", {}).get("q") or [""])[0].strip()
    drange = _parse_view_date((req.get("query", {}).get("date") or [""])[0])

    def _match(frag):
        if q and q not in (frag.get("searchable_text") or "") and q not in (frag.get("full_summary") or ""):
            return False
        if drange:
            ts = frag.get("ts") or 0
            if not (drange[0] <= ts < drange[1]):
                return False
        return True

    active, archive = [], []
    for db, out in ((eng.active, active), (eng.archive_db, archive)):
        if db is None:
            continue
        for r in db.iter_rows():
            if not _match(r):
                continue
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
    return _ok(l0=l0, l1=l1, active=active, archive=archive, cold=cold, topics=TOPIC_NAMES)


def _api_memory_view_cold(req):
    """L3 冷存储分区详情（点开展示）：partition = 年/季度/主题。"""
    eng = _memory_engine()
    if eng is None:
        return _error("记忆库不可用")
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
    return _ok(entries=entries)


def _api_memory_view_add(req):
    eng = _memory_engine()
    if eng is None:
        return _error("记忆库不可用")
    ok, msg, f = _validate_memory_entry(req.get("json", {}))
    if not ok:
        return _error(msg)
    try:
        r = eng.ingest(f["content"], ts=f["ts"], year=f["year"], quarter=f["quarter"],
                       main_topic=f["topic"] or None, participants=f["participants"])
        return _ok(id=r.fragment_id, message="已新增记忆")
    except Exception as e:
        return _error(f"新增失败：{e}")


def _api_memory_view_update(req):
    eng = _memory_engine()
    if eng is None:
        return _error("记忆库不可用")
    data = req.get("json", {})
    fid = str(data.get("id") or "").strip()
    ok, msg, f = _validate_memory_entry(data)
    if not ok:
        return _error(msg)
    if not fid:
        return _error("缺少记忆 id")
    old = None
    for db in (eng.active, eng.archive_db):
        row = db.get(fid)
        if row:
            old = row
            break
    if old is None:
        return _error("要编辑的记忆不存在（可能已被归档/合并）")
    try:
        # 删除旧记录（索引 + 向量 + 冷存储原文），再以同 id 重新入库（chunks=False 保证 id 不变）
        for db in (eng.active, eng.archive_db):
            db.delete([fid])
        eng.vector.remove([fid])
        eng.cold.remove_ids(old["year"], old["quarter"], old["main_topic"], [fid])
        r = eng.ingest(f["content"], fragment_id=fid, ts=f["ts"], year=f["year"], quarter=f["quarter"],
                       main_topic=f["topic"] or None, participants=f["participants"], chunks=False)
        return _ok(id=r.fragment_id, message="已更新记忆")
    except Exception as e:
        return _error(f"更新失败：{e}")


def _api_memory_view_delete(req):
    eng = _memory_engine()
    if eng is None:
        return _error("记忆库不可用")
    fid = str((req.get("json", {}) or {}).get("id") or "").strip()
    if not fid:
        return _error("缺少记忆 id")
    old = None
    for db in (eng.active, eng.archive_db):
        row = db.get(fid)
        if row:
            old = row
            break
    if old is None:
        return _error("要删除的记忆不存在")
    try:
        for db in (eng.active, eng.archive_db):
            db.delete([fid])
        eng.vector.remove([fid])
        eng.cold.remove_ids(old["year"], old["quarter"], old["main_topic"], [fid])
        return _ok(message="已删除记忆")
    except Exception as e:
        return _error(f"删除失败：{e}")


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


def _api_background_image(_req):
    """提供“背景设置”插件配置的本地图片（浏览器无法直接加载本地文件路径，
    由本端点读取并返回；http(s)/data:/以 / 开头的地址由前端直接加载）。"""
    try:
        s = plugin_manager.manager.get_settings("背景设置")
    except Exception:
        return _error("背景设置插件不可用", 500)
    img = ((s or {}).get("image") or "").strip()
    if not img:
        return _error("未配置背景图片", 404)
    if img.lower().startswith(("http://", "https://", "data:", "/")):
        return _error("网络/内置图片由前端直接加载", 400)
    p = os.path.expandvars(os.path.expanduser(img))
    if not os.path.isabs(p):
        p = os.path.join(config.PROJECT_ROOT, p)
    if not os.path.isfile(p):
        return _error(f"图片文件不存在：{img}", 404)
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
    ("GET", "/api/memory/view"): _api_memory_view,
    ("GET", "/api/memory/view/cold"): _api_memory_view_cold,
    ("POST", "/api/memory/view/add"): _api_memory_view_add,
    ("POST", "/api/memory/view/update"): _api_memory_view_update,
    ("POST", "/api/memory/view/delete"): _api_memory_view_delete,
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
    return 200, body, ctype


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
    """生成内联脚本：在页面解析完成前同步应用背景主题并预加载背景图。"""
    data = json.dumps(_theme_embed(), ensure_ascii=False).replace("<", "\\u003c")
    return (
        "<script>window.__THEME__=" + data + ";"
        "(function(){var t=window.__THEME__&&window.__THEME__.settings||{};"
        "var m=t.mode||\"dark\",b=document.body;"
        "if(m===\"light\"){b.classList.add(\"theme-light\");}"
        "else if(m===\"image\"){b.classList.add(\"theme-image\");"
        "var i=String(t.image||\"\").trim(),src=\"\";"
        "if(i){if(/^(https?:|data:|\\/)/i.test(i)){src=i;"
        "b.style.setProperty(\"--bg-image\",'url(\"'+i+'\"');}"
        "else{src=\"/api/background/image\";"
        "b.style.setProperty(\"--bg-image\",\"url('/api/background/image')\");}"
        "new Image().src=src;}"
        "b.style.setProperty(\"--bg-strength\",String(t.strength!=null?t.strength:0.8));"
        "}})();</script>"
    )


def _inject_theme(result):
    """把背景主题内联脚本注入到 HTML 的 </body> 前。"""
    if result is None:
        return None
    status, body, ctype = result
    if b"</body>" in body:
        body = body.replace(b"</body>", _theme_inline_script().encode("utf-8") + b"</body>", 1)
    return status, body, ctype


def _serve_static(path):
    if path in ("/", "/index.html"):
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
    server_version = "XiaolongluoWebUI/1.7"

    def _dispatch(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

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
        return result

    def _respond(self, result):
        status, body, ctype = result
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
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


def serve(host=None, port=None, open_browser=True, on_ready=None):
    """启动 Web 界面：确保目录、创建服务器（端口被占时改用随机端口）、自检并打开界面。

    on_ready: 可选回调，服务绑定并自检通过后以实际地址调用（桌面版窗口用）。
    """
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()
        runtime.cleanup_runtime()


def run(host=None, port=None):
    """兼容入口：不自动打开浏览器。"""
    serve(host, port, open_browser=False)
