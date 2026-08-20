# plugins/quick_settings.py —— 快捷设置插件：用语音/命令快速切换常用开关。
import core.config as config
from core import storage

NAME = "快捷设置"
VERSION = "1.0.0"
DESCRIPTION = "快速切换联网、清空对话：说“关闭联网”或 /net、/clear"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True


def on_message(user_text, mode, ctx):
    c = ctx.config
    if user_text in ("关闭联网", "关联网"):
        c.internet_enabled = False
        return {"reply": "联网搜索已关闭。", "speak": True}
    if user_text in ("开启联网", "开联网"):
        c.internet_enabled = True
        return {"reply": "联网搜索已开启。", "speak": True}
    if user_text in ("清空对话", "清除对话", "清空上下文"):
        storage.clear_history()
        return {"reply": "对话上下文已清空。", "speak": True}
    return None


def commands():
    return [
        {"name": "/net", "desc": "切换联网搜索开关", "args": "[on|off]"},
        {"name": "/clear", "desc": "清空对话上下文", "args": ""},
    ]


def on_command(command, args, ctx):
    c = ctx.config
    if command == "/net":
        if args and args[0] in ("on", "off"):
            c.internet_enabled = (args[0] == "on")
        else:
            c.internet_enabled = not c.internet_enabled
        return {"reply": f"联网搜索已{'开启' if c.internet_enabled else '关闭'}。", "speak": True}
    if command == "/clear":
        storage.clear_history()
        return {"reply": "对话上下文已清空。", "speak": True}
    return None
