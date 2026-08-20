# plugins/translate.py —— 翻译插件：说“翻译 …”或 /translate。
import re

NAME = "翻译"
VERSION = "1.0.0"
DESCRIPTION = "翻译文本：说“翻译 hello”或 /translate 内容（中英自动判断）"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True


def _translate(text, ctx):
    if not text:
        return "用法：/translate 内容，或说“翻译 内容”"
    if re.search(r'[\u4e00-\u9fff]', text):
        prompt = f"请把下面的中文翻译成英文，只输出译文，不要解释：\n{text}"
    else:
        prompt = f"请把下面的内容翻译成简体中文，只输出译文，不要解释：\n{text}"
    result = ctx.generate(prompt, num_predict=256, temperature=0.2, purpose="translate")
    return result or "翻译失败，请稍后重试。"


def on_message(user_text, mode, ctx):
    if user_text.startswith("翻译") and len(user_text) > 2:
        return {"reply": _translate(user_text[2:].strip(), ctx), "speak": True}
    if user_text.startswith("把") and "翻译成" in user_text:
        return {"reply": _translate(user_text, ctx), "speak": True}
    return None


def commands():
    return [{"name": "/translate", "desc": "翻译文本", "args": "内容"}]


def on_command(command, args, ctx):
    if command == "/translate":
        return {"reply": _translate(" ".join(args), ctx), "speak": True}
    return None
