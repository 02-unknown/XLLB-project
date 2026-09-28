# -*- coding: utf-8 -*-
"""问好/寒暄误命中修复验证 + 检索回归：
1) 泛用填充词（好呀/好好）不再把问好放大成高置信（绝对尺度归一化 + 弱匹配共识校验）；
2) 原有 16 项检索断言全部不回归。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False            # 只验证检索打分，不走判断模型
from memory_engine import time_utils

eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readwrite")            # 引擎默认只读：测试写入必须显式放开（门禁语义）
eng.set_char_name("洛天依")
eng.set_debug(True)

import datetime
NOW = datetime.datetime.now()
CY, CQ = time_utils.year_quarter(NOW.timestamp())
P1 = time_utils.prev_quarter(CY, CQ)
P2 = time_utils.prev_quarter(*P1)
P1Q, P2Q = int(P1[1][1]), int(P2[1][1])


def check(q, expect, role=None):
    r = eng.search(q, role=role)
    hit = (r.id in expect) if expect else (r.id == "")
    print("  %-26s 角色=%-12s -> conf=%.3f id=%s %s" % (
        q, "、".join(role) if isinstance(role, list) else (role or "无"),
        r.confidence, r.id or "∅", "✓" if hit else "✗"))
    return hit


print("=" * 78)
print("1) 问好 / 寒暄 / 泛用填充词：不再命中记忆")
print("=" * 78)
ok = True
ok &= check("嘿嘿，你好呀", set(), "洛天依（朋友）")     # 本次报告的问题
ok &= check("你好呀", set(), "洛天依")
ok &= check("早上好呀", set(), "洛天依")
ok &= check("哈哈", set(), "洛天依")
ok &= check("好好听", set(), "洛天依")                   # 与“好好庆祝”仅共享 好好

print()
print("=" * 78)
print("2) 原有 16 项检索断言（回归）")
print("=" * 78)


def today_fixture_ready():
    """「今天」类断言依赖夹具记录 chat_today_* 就是当天写入的。

    这两条是「当天会话」夹具：跨天之后它们不再属于“今天”，检索不到属于正常现象
    （不是回归 bug）。这里只读查询数据库里的时间戳判断，不改动任何数据。
    """
    import datetime
    import sqlite3
    try:
        db = eng.paths.active_db
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    except Exception:
        return False
    try:
        row = con.execute("SELECT ts FROM fragments WHERE id='chat_today_1'").fetchone()
    except Exception:
        return False
    finally:
        con.close()
    if not row or not row[0]:
        return False
    try:
        return datetime.date.fromtimestamp(float(row[0])) == datetime.date.today()
    except Exception:
        return False


if today_fixture_ready():
    ok &= check("今天下午去公园散步", {"chat_today_1"}, "洛天依")
    ok &= check("今天想喝奶茶", {"chat_today_2"}, ["洛天依", "洛天依（朋友）"])
    ok &= check("今天我们有什么安排吗", {"chat_today_1", "chat_today_2"}, "洛天依")
else:
    print("  [SKIP] 「今天」三项：夹具 chat_today_* 不是今天的记录（跨天后已不属于“今天”），跳过该组断言")
ok &= check("周末爬山野餐", {"chat_week_1"}, "洛天依")
ok &= check("周三晚上打游戏", {"chat_week_2"}, "洛天依")
ok &= check("星期三呢", {"chat_week_2"}, "洛天依")
ok &= check("明天呢", set(), "洛天依")
ok &= check("这个月学做小笼包", {"chat_month_1"}, "洛天依")
ok &= check("这个月生日", {"chat_month_2"}, "洛天依")
ok &= check("今年%d季度学吉他" % P1Q, {"chat_year_1"}, "洛天依")
ok &= check("今年%d季度游乐园" % P2Q, {"chat_year_2"}, "洛天依")
ok &= check("去年冬天看雪", {"chat_last_1"}, "洛天依")
ok &= check("去年海边旅行", {"chat_last_2"}, ["洛天依", "洛天依（朋友）"])
ok &= check("前年生日玩偶", {"chat_prev_1"}, "洛天依")
ok &= check("前年跨年烟花", {"chat_prev_2"}, "洛天依")
print("=" * 78)
print("验证结论: %s" % ("✓ 全部通过" if ok else "✗ 存在失败"))
