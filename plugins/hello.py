# plugins/hello.py —— 最小示例插件，演示插件结构与钩子用法。
NAME = "示例问候"
VERSION = "1.0.0"
DESCRIPTION = "演示插件：说“你好啊”或输入 /hello 会得到问候"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True


def on_message(user_text, mode, ctx):
    """消息进入主流水线前调用；返回 dict 表示已处理。"""
    if user_text.strip() in ("你好啊", "你好呀", "hello"):
        return {"reply": "你好呀！我是插件，很高兴见到你～", "speak": True}
    return None


def commands():
    return [{"name": "/hello", "desc": "打个招呼", "args": ""}]


def on_command(command, args, ctx):
    if command == "/hello":
        return {"reply": "你好呀！我是插件，很高兴见到你～", "speak": True}
    return None
