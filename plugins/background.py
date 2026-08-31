# plugins/background.py —— 背景设置插件：自定义界面背景（浅色 / 深色 / 自定义图片）。
# 前端根据本插件的设置即时应用主题，无需重启；与其它功能无冲突。
import core.config as config

NAME = "背景设置"
VERSION = "1.0.0"
DESCRIPTION = "自定义界面背景：浅色 / 深色 / 自定义图片，保存后即时生效"
AUTHOR = "官方"
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
         "placeholder": "本地路径（如 D:\\pic.png）或 http(s) 链接"},
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
