# plugins/export.py —— 把当前对话记录导出为 Markdown 文件。
import datetime
import os

import core.config as config

NAME = "对话导出"
VERSION = "1.0.0"
DESCRIPTION = "把当前对话记录导出为 Markdown：/export"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True


def on_command(command, args, ctx):
    if command != "/export":
        return None

    records = ctx.history()
    if not records:
        return {"reply": "当前没有可导出的对话记录。", "speak": False}

    out_dir = config.SAVE_WO_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"chat_{stamp}.md")

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 对话记录（{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）\n\n")
        for rec in records:
            f.write(f"**用户**：{rec['user']}\n\n**助手**：{rec['assistant']}\n\n---\n\n")

    return {"reply": f"已导出 {len(records)} 条对话到：{path}", "speak": False}


def commands():
    return [{"name": "/export", "desc": "导出对话为 Markdown", "args": ""}]
