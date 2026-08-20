# plugins/time.py —— 本地时间/日期查询（无需调用大模型）。
import datetime

NAME = "时间日期"
VERSION = "1.0.0"
DESCRIPTION = "查询本地时间与日期：/time、/date，或说“现在几点”"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True

_TRIGGERS = ["现在几点", "现在时间", "几点了", "今天几号", "今天日期", "现在日期", "今天星期几"]


def on_message(user_text, mode, ctx):
    if any(t in user_text for t in _TRIGGERS):
        now = datetime.datetime.now()
        week = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
        reply = f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}，星期{week}。"
        return {"reply": reply, "speak": True}
    return None


def commands():
    return [
        {"name": "/time", "desc": "当前时间", "args": ""},
        {"name": "/date", "desc": "当前日期", "args": ""},
    ]


def on_command(command, args, ctx):
    now = datetime.datetime.now()
    if command == "/time":
        return {"reply": f"现在是 {now.strftime('%H:%M:%S')}。", "speak": True}
    if command == "/date":
        return {"reply": f"今天是 {now.strftime('%Y年%m月%d日')}。", "speak": True}
    return None
