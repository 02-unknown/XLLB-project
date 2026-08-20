# plugins/memo.py —— 记事本 / 记忆插件：让助手帮你记住事情。
import json
import os
import re

NAME = "记事本"
VERSION = "1.0.0"
DESCRIPTION = "让助手记住事情：说“记住 …”或“我记了什么”，或 /memo"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True


def _path(ctx):
    return os.path.join(ctx.config.PROJECT_ROOT, "memo_notes.json")


def _load(ctx):
    try:
        with open(_path(ctx), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(ctx, notes):
    try:
        with open(_path(ctx), "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        ctx.log("保存笔记失败:", e)


def _render(notes):
    if not notes:
        return "你还没有任何笔记。"
    return "你的笔记：\n" + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(notes))


def on_message(user_text, mode, ctx):
    m = re.match(r'^(?:记住|记下|帮我记|帮我记住)\s*(.+)$', user_text)
    if m:
        notes = _load(ctx)
        notes.append(m.group(1).strip())
        _save(ctx, notes)
        return {"reply": f"已记住：{m.group(1).strip()}（共 {len(notes)} 条）", "speak": True}

    if user_text in ("我的笔记", "我记了什么", "查看笔记", "备忘录"):
        return {"reply": _render(_load(ctx)), "speak": False}
    return None


def commands():
    return [{"name": "/memo", "desc": "查看笔记", "args": "[clear]"}]


def on_command(command, args, ctx):
    if command != "/memo":
        return None
    if args and args[0] == "clear":
        _save(ctx, [])
        return {"reply": "笔记已清空。", "speak": False}
    return {"reply": _render(_load(ctx)), "speak": False}
