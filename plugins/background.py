# plugins/background.py —— 背景设置插件：自定义界面背景（浅色 / 深色 / 自定义图片）。
# 前端根据本插件的设置即时应用主题，无需重启；与其它功能无冲突。
import os

import core.config as config
import core.paths as paths

NAME = "背景设置"
VERSION = "1.0.0"
DESCRIPTION = "自定义界面背景：浅色 / 深色 / 自定义图片，保存后即时生效"
AUTHOR = "02"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    "mode": "dark",      # light（浅色）/ dark（深色，默认）/ image（自定义图片）
    "image": "",         # 自定义图片：本地路径（如 D:\pic.png 或 runtime/backgrounds/xxx.png），或 http(s)/data:/以 / 开头的内置地址
    "strength": 0.8,     # 背景强度（0-1）：越大图片越清晰，同时自动调节遮罩保证文字可读
}


def settings_schema():
    return [
        {"key": "mode", "label": "背景模式", "type": "select", "options": [
            {"value": "dark", "label": "深色（默认）"},
            {"value": "light", "label": "浅色"},
            {"value": "image", "label": "自定义图片"},
        ]},
        {"key": "image", "label": "图片路径/地址", "type": "text",
         "placeholder": "本地路径（如 D:\\pic.png）或 http(s) 链接",
         "desc": "支持直接粘贴带引号的路径（Win「复制文件地址」）与 file:/// 地址，保存时会自动清理；"
                 "本地图片必须是存在的文件，否则不显示（可在下方「运行状态」确认）"},
        {"key": "strength", "label": "背景强度(0-1)", "type": "number",
         "placeholder": "0.8（越大图片越清晰，自动调节遮罩保证文字可读）"},
    ]


def on_settings_changed(settings, ctx):
    """校验并规范化设置，避免非法值破坏界面。"""
    mode = settings.get("mode", "dark")
    if mode not in ("light", "dark", "image"):
        settings["mode"] = "dark"
    if mode != "image":
        settings["image"] = ""
    else:
        # 用户常直接粘贴「复制文件地址」得到的带引号路径（含中文引号 / file:/// 前缀），
        # 不清理的话 os.path.isfile() 判定为不存在 → 背景图 404，表现为「改了路径不生效」
        settings["image"] = paths.normalize_user_path(settings.get("image"))
    # 合并为单一“背景强度”：兼容旧版 opacity/overlay 设置
    strength = settings.get("strength")
    if strength is None:
        try:
            strength = float(settings.get("opacity", 0.8))
        except (TypeError, ValueError):
            strength = 0.8
    try:
        settings["strength"] = max(0.0, min(1.0, float(strength)))
    except (TypeError, ValueError):
        settings["strength"] = 0.8
    settings.pop("opacity", None)
    settings.pop("overlay", None)
    return settings


def get_state(ctx):
    """设置页「运行状态」：当前模式 + 本地图片是否真的可用（填了路径却不生效时一眼看出原因）。"""
    try:
        s = ctx.manager.get_settings(NAME) or {}
    except Exception:
        s = {}
    mode = s.get("mode", "dark")
    mode_txt = {"light": "浅色", "dark": "深色", "image": "自定义图片"}.get(mode, mode)
    info = [f"当前模式：{mode_txt}　背景强度：{s.get('strength', 0.8)}"]
    img = str(s.get("image") or "").strip()
    if mode != "image":
        info.append("自定义图片未启用（切换到「自定义图片」模式并填入路径后生效）")
    elif not img:
        info.append("已选择自定义图片模式，但还没有填写图片路径")
    elif img.lower().startswith(("http://", "https://", "data:")):
        info.append(f"使用网络图片地址：{img}")
    else:
        p = paths.resolve_user_path(img)
        if p and os.path.isfile(p):
            size = os.path.getsize(p)
            info.append(f"本地图片可用：{p}（{size // 1024} KB）")
        else:
            info.append(f"本地图片不存在，界面不会显示这张图：{p or img}")
    return {"queue": [{"index": i + 1, "title": t, "status": ""} for i, t in enumerate(info)]}
