# plugins/smooth_music.py —— 官方插件：更流畅的音乐播放。
# 启用后，说“播放/点歌 …”会直接播放第一个搜索结果，或在设置中选择“llm”让判断模型挑选最佳结果；
# 搜索结果中出现下载/解析报错时，可自动忽略该条并继续尝试剩余结果（默认开启）。
import os
import re

import core.config as config
from core import music as music_core

NAME = "更流畅的音乐播放"
VERSION = "1.0.0"
DESCRIPTION = "播放音乐时直接播放第一个搜索结果，或用判断模型挑选；可自动忽略报错结果"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    "mode": "first",        # first: 直接播放第一个；llm: 用判断模型挑选
    "ignore_errors": True,  # 忽略搜索/下载报错，自动尝试剩余结果
}

_PLAY_TRIGGERS = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱"]
_ADD_PHRASES = ["播放列表", "加入列表", "添加到播放列表", "队列", "排队"]
_SONG_SUFFIX = re.compile(r'(这首歌|那首歌|这首歌吗|音乐|一下|吧|啊|呀|呢|哦)$')


def settings_schema():
    return [
        {"key": "mode", "label": "选歌方式", "type": "select",
         "options": ["first", "llm"]},
        {"key": "ignore_errors", "label": "忽略搜索报错", "type": "checkbox"},
    ]


def _extract_song(user_text):
    text = user_text.strip()
    # 排除“加入播放列表”等队列用语，避免“播放”误命中
    if any(p in text for p in _ADD_PHRASES):
        return ""
    for t in _PLAY_TRIGGERS:
        if t in text:
            rest = text[text.index(t) + len(t):].strip()
            rest = rest.strip("，。！？,.!?；;：: ").strip()
            rest = _SONG_SUFFIX.sub('', rest).strip()
            return rest
    return ""


def _to_url(path):
    if not path:
        return None
    rel = os.path.relpath(path, config.RUNTIME_DIR).replace("\\", "/")
    return "/runtime/" + rel


def _pick(videos, song, ctx):
    """选择要播放的视频：first 直接取第一个；llm 用判断模型挑选。"""
    if len(videos) <= 1:
        return videos[0]
    if ctx.manager.get_settings(NAME).get("mode", "first") == "llm":
        lines = "\n".join(f"{i + 1}. {v['title']}" for i, v in enumerate(videos))
        prompt = (
            f"以下是从B站搜索「{song}」得到的歌曲结果：\n{lines}\n"
            f"请选择最符合「{song}」这一首的序号，只输出一个数字。"
        )
        result = ctx.generate(prompt, num_predict=16, purpose="music_pick")
        m = re.search(r'(\d+)', result or "")
        picked = None
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(videos):
                picked = videos[idx]
        if config.DEBUG_MODE:
            # 调试模式：在命令行输出判断模型的选歌结果并注明出处
            print(f"* (更流畅的音乐播放) 判断模型[{config.LLM_JUDGE_MODEL}@{config.LLM_JUDGE_BACKEND}] "
                  f"选歌结果: {result!r} -> {picked.get('title') if picked else '未命中'}")
        if picked is not None:
            return picked
    return videos[0]


def _intro(title, ctx):
    """生成“即将播放”的提示语（与主流程一致）。"""
    prompt = f"即将播放《{title}》，请用当前角色口吻说一句“即将播放...”的话，不要输出任何其他的无关内容。"
    reply = ctx.call_llm(title, extra_context=prompt)
    if not reply or "抱歉" in reply or "卡壳" in reply:
        return f"即将播放《{title}》。"
    return reply


def on_message(user_text, mode, ctx):
    if not any(t in user_text for t in _PLAY_TRIGGERS):
        return None
    song = _extract_song(user_text)
    if not song:
        return None
    videos = music_core.search_music(song)
    if not videos:
        return {"reply": f"没有找到「{song}」相关的歌曲。", "speak": True}

    ignore_errors = ctx.manager.get_settings(NAME).get("ignore_errors", True)

    candidates = list(videos)
    skipped = []
    while candidates:
        video = _pick(candidates, song, ctx)
        title = video.get("title", song)
        try:
            path = music_core.download_music(video.get("url", ""), title)
        except Exception:
            path = None

        if path:
            url = _to_url(path)
            return {"reply": _intro(title, ctx), "speak": True,
                    "music": {"url": url, "title": title}}

        # 该条结果下载/解析报错
        if not ignore_errors:
            return {"reply": f"《{title}》下载失败。", "speak": True}
        skipped.append(title)
        candidates = [v for v in candidates if v is not video]
        if config.DEBUG_MODE:
            print(f"* (更流畅的音乐播放) 忽略报错搜索结果: {title}")

    tail = f"（已忽略 {len(skipped)} 条报错结果）" if skipped else ""
    return {"reply": f"没有可播放的结果{tail}。", "speak": True}


def commands():
    return [{"name": "/smooth", "desc": "查看/切换流畅播放设置", "args": "[first|llm|ignore on|off]"}]


def on_command(command, args, ctx):
    if command != "/smooth":
        return None
    if args and args[0] in ("first", "llm"):
        ctx.manager.save_settings(NAME, {"mode": args[0]})
        return {"reply": f"流畅播放模式已切换为 {args[0]}。", "speak": True}
    if args and args[0] == "ignore" and len(args) > 1 and args[1] in ("on", "off"):
        on = args[1] == "on"
        ctx.manager.save_settings(NAME, {"ignore_errors": on})
        return {"reply": f"忽略搜索报错已{'开启' if on else '关闭'}。", "speak": True}

    st = ctx.manager.get_settings(NAME)
    mode = st.get("mode", "first")
    ignore = st.get("ignore_errors", True)
    return {"reply": f"当前：选歌方式={mode}，忽略报错={'开' if ignore else '关'}"
                     f"（/smooth first|llm 或 /smooth ignore on|off）", "speak": False}
