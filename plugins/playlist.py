# plugins/playlist.py —— 官方插件：更优的播放列表。
# 取代单曲播放栏：支持多曲排队、每首开头可选生成回复、播放结束自动清空缓存并生成回复。
import os
import re
import threading

import core.config as config
from core import music as music_core

NAME = "更优的播放列表"
VERSION = "1.0.0"
DESCRIPTION = "完整播放列表：多曲排队、每首开头可生成回复、结束自动清空缓存并生成回复"
AUTHOR = "官方"
OFFICIAL = True

SETTINGS = {
    "reply_each_start": True,   # 每首开头生成回复
    "auto_clear": True,         # 结束后自动清空缓存
    "auto_reply_end": True,     # 结束后自动生成回复
}

_queue = []            # [{"title": str, "url": str}]
_playing_index = -1
_lock = threading.Lock()

_PLAY_TRIGGERS = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱"]
_ADD_TRIGGERS = ["加入播放列表", "添加到播放列表", "加入列表", "排队"]
_STOP_WORDS = ["停止音乐", "停止播放", "关闭音乐", "清空播放列表", "清除播放列表", "清空列表"]


def settings_schema():
    return [
        {"key": "reply_each_start", "label": "每首开头生成回复", "type": "checkbox"},
        {"key": "auto_clear", "label": "结束后自动清空缓存", "type": "checkbox"},
        {"key": "auto_reply_end", "label": "结束后自动生成回复", "type": "checkbox"},
    ]


# ==================== 内部工具 ====================
def _opts(ctx):
    return ctx.manager.get_settings(NAME)


def _to_url(path):
    if not path:
        return None
    rel = os.path.relpath(path, config.RUNTIME_DIR).replace("\\", "/")
    return "/runtime/" + rel


def ctx_log(*args):
    print("[playlist]", *args)


def _download(url, title):
    try:
        return _to_url(music_core.download_music(url, title))
    except Exception as e:
        ctx_log(e)
        return None


def _extract_song(user_text):
    """从用户输入中提取歌曲名（返回空字符串表示无法提取）。"""
    text = user_text.strip()
    # 先匹配“加入列表”类，避免“播放”误命中“加入播放列表”中的“播放”
    for t in _ADD_TRIGGERS + _PLAY_TRIGGERS:
        if t in text:
            rest = text[text.index(t) + len(t):].strip()
            rest = rest.strip("，。！？,.!?；;：: ").strip()
            rest = re.sub(r'(这首歌|那首歌|这首歌吗|音乐|列表|一下|吧|啊|呀|呢|哦)$', '', rest).strip()
            return rest
    return ""


def _intro(title, ctx):
    prompt = f"即将播放《{title}》，请用当前角色口吻说一句“即将播放...”的话，不要输出任何其他的无关内容。"
    reply = ctx.call_llm(title, extra_context=prompt)
    if not reply or "抱歉" in reply or "卡壳" in reply:
        return f"即将播放《{title}》。"
    return reply


def _outro(ctx):
    prompt = "播放列表已全部播放完毕，请用当前角色口吻说一句结束语。"
    reply = ctx.call_llm("播放列表结束提示", extra_context=prompt)
    if not reply or "抱歉" in reply or "卡壳" in reply:
        return "播放列表播放完毕啦。"
    return reply


def _clear_cache():
    try:
        music_core.cleanup_music()
    except Exception:
        pass


def _snapshot():
    return {
        "queue": [
            {"index": i + 1, "title": q["title"], "status": "playing" if i == _playing_index else "pending"}
            for i, q in enumerate(_queue)
        ],
        "playing_index": _playing_index,
        "total": len(_queue),
    }


def _queue_text():
    if not _queue:
        return "播放列表为空。"
    lines = ["当前播放列表："]
    for i, q in enumerate(_queue):
        mark = "▶" if i == _playing_index else f"{i + 1}."
        lines.append(f"  {mark} {q['title']}")
    return "\n".join(lines)


# ==================== 核心流程 ====================
def _add_song(query, ctx):
    """搜索并把第一首匹配加入队列，返回 (title, url)。"""
    global _queue
    query = (query or "").strip()
    if not query:
        return None, None
    videos = music_core.search_music(query)
    if not videos:
        return None, None
    v = videos[0]
    with _lock:
        _queue.append({"title": v.get("title", query), "url": v.get("url", "")})
    return v.get("title", query), v.get("url", "")


def _play_first_if_idle(ctx):
    """若尚未在播放，则开始播放队列第一首，返回结果字典；否则返回 None。"""
    global _playing_index
    with _lock:
        if _playing_index >= 0:
            return None
        if not _queue:
            return {"reply": "播放列表为空。", "speak": False}
        _playing_index = 0
        title, url = _queue[0]["title"], _queue[0]["url"]

    audio_url = _download(url, title)
    opts = _opts(ctx)
    if audio_url is None:
        with _lock:
            _playing_index = -1
        return {"reply": f"《{title}》下载失败，请稍后重试。", "speak": True}

    if opts.get("reply_each_start", True):
        return {"reply": _intro(title, ctx), "speak": True,
                "music": {"url": audio_url, "title": title}, "music_plugin": NAME}
    return {"reply": f"正在播放：{title}", "speak": False,
            "music": {"url": audio_url, "title": title}, "music_plugin": NAME}


def _play_next(ctx):
    """播放下一首；队列播放完毕时执行收尾（清空缓存 + 生成回复）。"""
    global _queue, _playing_index
    with _lock:
        _playing_index += 1
        if _playing_index < len(_queue):
            title, url = _queue[_playing_index]["title"], _queue[_playing_index]["url"]
        else:
            title, url = None, None

    if title is None:
        # 播放结束
        opts = _opts(ctx)
        if opts.get("auto_clear", True):
            _clear_cache()
        with _lock:
            _queue.clear()
            _playing_index = -1
        if opts.get("auto_reply_end", True):
            return {"reply": _outro(ctx), "speak": True, "playlist_done": True}
        return {"reply": "播放列表播放完毕。", "speak": False, "playlist_done": True}

    audio_url = _download(url, title)
    opts = _opts(ctx)
    if audio_url is None:
        return {"reply": f"《{title}》下载失败，播放下一首。", "speak": True, "music": None}

    if opts.get("reply_each_start", True):
        return {"reply": _intro(title, ctx), "speak": True,
                "music": {"url": audio_url, "title": title}, "music_plugin": NAME}
    return {"reply": f"正在播放：{title}", "speak": False,
            "music": {"url": audio_url, "title": title}, "music_plugin": NAME}


# ==================== 钩子 ====================
def on_message(user_text, mode, ctx):
    global _queue, _playing_index
    # 停止 / 清空
    if any(w in user_text for w in _STOP_WORDS):
        with _lock:
            _queue.clear()
            _playing_index = -1
        _clear_cache()
        return {"action": "music_control", "music_control": "stop",
                "reply": "音乐已停止，播放列表已清空。", "speak": True}

    # 加入列表（不自动播放）
    if any(t in user_text for t in _ADD_TRIGGERS):
        song = _extract_song(user_text)
        if not song:
            return None
        title, _ = _add_song(song, ctx)
        if title is None:
            return {"reply": f"没有找到「{song}」相关的歌曲。", "speak": True}
        return {"reply": f"已加入播放列表：{title}\n{_queue_text()}", "speak": True}

    # 播放请求（加入队列并开始播放）
    if any(t in user_text for t in _PLAY_TRIGGERS):
        song = _extract_song(user_text)
        if not song:
            return None
        title, _ = _add_song(song, ctx)
        if title is None:
            return {"reply": f"没有找到「{song}」相关的歌曲。", "speak": True}
        result = _play_first_if_idle(ctx)
        if result is not None:
            return result
        return {"reply": f"已加入播放列表：{title}\n{_queue_text()}", "speak": True}

    return None


def commands():
    return [
        {"name": "/playlist", "desc": "查看播放列表", "args": ""},
        {"name": "/add", "desc": "添加歌曲到播放列表", "args": "歌曲名"},
        {"name": "/playnext", "desc": "播放下一首", "args": ""},
        {"name": "/clearplaylist", "desc": "清空播放列表", "args": ""},
    ]


def on_command(command, args, ctx):
    global _queue, _playing_index
    if command == "/playlist":
        return {"reply": _queue_text(), "speak": False}
    if command == "/add":
        song = " ".join(args)
        if not song:
            return {"reply": "用法：/add 歌曲名", "speak": False}
        title, _ = _add_song(song, ctx)
        if title is None:
            return {"reply": f"没有找到「{song}」相关的歌曲。", "speak": True}
        result = _play_first_if_idle(ctx)
        if result is not None:
            return result
        return {"reply": f"已加入播放列表：{title}\n{_queue_text()}", "speak": True}
    if command == "/playnext":
        return _play_next(ctx)
    if command == "/clearplaylist":
        with _lock:
            _queue.clear()
            _playing_index = -1
        _clear_cache()
        return {"action": "music_control", "music_control": "stop",
                "reply": "播放列表已清空。", "speak": True}
    return None


def actions():
    return [
        {"name": "play", "label": "播放", "desc": "播放 / 继续播放列表"},
        {"name": "next", "label": "下一首", "desc": "跳过当前，播放下一首"},
        {"name": "clear", "label": "清空", "desc": "清空播放列表并停止"},
    ]


def on_action(name, ctx):
    global _queue, _playing_index
    if name == "play":
        if not _queue:
            return {"reply": "播放列表为空，请先添加歌曲。", "speak": False}
        return _play_first_if_idle(ctx)
    if name == "next":
        return _play_next(ctx)
    if name == "clear":
        with _lock:
            _queue.clear()
            _playing_index = -1
        _clear_cache()
        return {"action": "music_control", "music_control": "stop",
                "reply": "播放列表已清空。", "speak": True}
    return None


def get_state(ctx):
    return _snapshot()
