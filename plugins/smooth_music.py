# plugins/smooth_music.py —— 内置插件：更流畅的音乐播放。
# 启用后，说“播放/点歌 …”会直接播放第一个搜索结果，或在设置中选择“llm”让判断模型挑选最佳结果；
# 搜索结果中出现下载/解析报错时，可自动忽略该条并继续尝试剩余结果（默认开启）。
import os
import re

import core.config as config
from core import music as music_core
from core.runtime import runtime_url

NAME = "更流畅的音乐播放"
VERSION = "1.2.0"
DESCRIPTION = "播放音乐时直接播放第一个搜索结果，或用判断模型挑选；可自动忽略报错结果、下载后做音量均衡，并可优先挑选与角色相关的歌曲"
AUTHOR = "02"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    "mode": "first",            # first: 直接播放第一个；llm: 用判断模型挑选
    "ignore_errors": True,      # 忽略搜索/下载报错，自动尝试剩余结果
    "normalize": False,         # 下载完成后做一次音量均衡（整首对齐到目标响度）
    "target_lufs": "-14",       # 目标响度（LUFS）
    "prefer_characters": False,  # 角色相关性增强：挑选时优先与已有角色相关的曲目
}

_PLAY_TRIGGERS = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱"]
_ADD_PHRASES = ["播放列表", "加入列表", "添加到播放列表", "队列", "排队"]
_SONG_SUFFIX = re.compile(r'(这首歌|那首歌|这首歌吗|音乐|一下|吧|啊|呀|呢|哦)$')


def settings_schema():
    return [
        {"key": "mode", "label": "选歌方式", "type": "select",
         "options": ["first", "llm"]},
        {"key": "ignore_errors", "label": "忽略搜索报错", "type": "checkbox"},
        {"key": "normalize", "label": "下载后音量均衡", "type": "checkbox",
         "desc": "下载完成后按整首平均响度对齐到目标 LUFS，只处理一次（播放音量滑杆照常叠加）"},
        {"key": "target_lufs", "label": "目标响度（LUFS）", "type": "select",
         "options": ["-23", "-18", "-16", "-14", "-12", "-9"],
         "desc": "均衡的目标值：-14 接近主流流媒体，数值越大越响"},
        {"key": "prefer_characters", "label": "角色相关性增强", "type": "checkbox",
         "desc": "由判断模型挑选，并优先选与已有角色相关的曲目（不改搜索词；用户明确指定时以用户为准）"},
    ]


def _character_names(ctx):
    """本机已有的角色名（当前角色 + 全部预设），供「角色相关性增强」拼提示词。"""
    names = []
    try:
        cur = str(getattr(getattr(ctx, "config", None), "character_name", "") or "").strip()
        if cur:
            names.append(cur)
    except Exception:
        pass
    try:
        from core import character as character_core
        for name in character_core.list_presets() or []:
            name = str(name or "").strip()
            if name and name not in names:
                names.append(name)
    except Exception:
        pass
    return names


def _character_prompt(ctx):
    """角色相关性增强的提示词补充。

    只加在「判断 / 挑选」用的提示词里，**不动搜索词**（搜索仍然按用户说的来），
    并且明确写着：用户明确指定歌曲 / 歌手 / 风格时以用户为准。
    """
    names = _character_names(ctx)
    if not names:
        return ""
    return (
        "补充挑选偏好：本机的角色有：" + "、".join(names) + "。"
        "如果用户没有明确指定歌曲名、歌手或风格，请优先挑选与这些角色相关的曲目"
        "（该角色的角色曲 / 主题曲 / 同人曲，或由该角色演唱、翻唱、相关的作品）；"
        "如果用户已经明确指定了歌曲、歌手或风格，则严格按用户的指定挑选，不要用角色相关性覆盖它。"
    )


def _normalize(path):
    """下载完成后按设置做一次音量均衡（只处理一次；失败不影响播放）。"""
    try:
        info = music_core.normalize_if_enabled(path)
    except Exception as e:
        print(f"* (更流畅的音乐播放) 音量均衡失败（已忽略）：{e}")
        return path
    if info and config.DEBUG_MODE:
        print(f"* (更流畅的音乐播放) 音量均衡：{info.get('detail')}")
    return path


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


def _pick(videos, song, ctx):
    """选择要播放的视频：first 直接取第一个；llm 用判断模型挑选。

    任何异常（判断模型不可用 / 超时 / 解析失败）都安全回退到第一个结果，
    绝不中断点歌流程（否则会被当作未处理而回退手动选歌）。
    """
    if len(videos) <= 1:
        return videos[0]
    st = ctx.manager.get_settings(NAME)
    prefer = bool(st.get("prefer_characters"))
    # 角色相关性增强需要模型来挑（选歌方式是 first 时也走判断模型，否则设置了不生效）
    if st.get("mode", "first") == "llm" or prefer:
        try:
            lines = "\n".join(f"{i + 1}. {v['title']}" for i, v in enumerate(videos))
            prompt = (
                f"以下是从B站搜索「{song}」得到的歌曲结果：\n{lines}\n"
                f"请选择最符合「{song}」这一首的序号，只输出一个数字。"
            )
            # 角色相关性增强：只加了下面这段「挑选偏好」，搜索词仍是用户原话
            if prefer:
                extra = _character_prompt(ctx)
                if extra:
                    prompt = prompt + "\n" + extra
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
        except Exception as e:
            print(f"* (更流畅的音乐播放) llm选歌失败，回退第一首: {e}")
    return videos[0]


def _intro(title, ctx):
    """生成“即将播放”的提示语（与主流程一致）。

    播报类调用：不检索记忆、不写入上下文（record=False / use_context=False），
    避免歌曲标题污染 L0/L1/L2 缓存。
    """
    prompt = f"即将播放《{title}》，请用当前角色口吻说一句“即将播放...”的话，不要输出任何其他的无关内容。"
    reply = ctx.call_llm(title, extra_context=prompt, record=False, use_context=False)
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
            path = _normalize(path)
            url = runtime_url(path)
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
    return [{"name": "/smooth", "desc": "查看/切换流畅播放、音量均衡与角色相关性设置",
             "args": "[first|llm|ignore on|off|lufs on|off|<目标值>|char on|off]"}]


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
    if args and args[0] in ("char", "character") and len(args) > 1 and args[1] in ("on", "off"):
        on = args[1] == "on"
        ctx.manager.save_settings(NAME, {"prefer_characters": on})
        names = _character_names(ctx) if on else []
        tail = f"（当前角色：{'、'.join(names)}）" if names else ""
        return {"reply": f"角色相关性增强已{'开启' if on else '关闭'}。{tail}", "speak": True}
    if args and args[0] == "lufs":
        st = ctx.manager.get_settings(NAME)
        if len(args) > 1 and args[1] in ("on", "off"):
            ctx.manager.save_settings(NAME, {"normalize": args[1] == "on"})
            return {"reply": f"下载后音量均衡已{'开启' if args[1] == 'on' else '关闭'}。", "speak": True}
        if len(args) > 1:
            try:
                target = float(args[1])
            except ValueError:
                return {"reply": "目标响度要写成数字，例如 /smooth lufs -14", "speak": False}
            ctx.manager.save_settings(NAME, {"normalize": True, "target_lufs": str(target)})
            return {"reply": f"下载后音量均衡已开启，目标响度 {target:g} LUFS。", "speak": True}
        return {"reply": f"音量均衡：{'开启' if st.get('normalize') else '关闭'}"
                         f"（目标 {st.get('target_lufs', '-14')} LUFS）", "speak": False}

    st = ctx.manager.get_settings(NAME)
    mode = st.get("mode", "first")
    ignore = st.get("ignore_errors", True)
    lufs = f"{st.get('target_lufs', '-14')} LUFS" if st.get("normalize") else "关闭"
    char = "开启" if st.get("prefer_characters") else "关闭"
    return {"reply": f"当前：选歌方式={mode}，忽略报错={'开' if ignore else '关'}，音量均衡={lufs}，"
                     f"角色相关性={char}"
                     f"（/smooth first|llm 或 /smooth ignore on|off 或 /smooth lufs on|off|<目标值>"
                     f" 或 /smooth char on|off）",
            "speak": False}
