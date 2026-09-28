# -*- coding: utf-8 -*-
"""日常聊天场景测试上下文种子：当天/当周/当月/当年/去年/前年 × 单角色/多角色。

写入真实数据目录 runtime/memory_engine（保留，不删除）。所有事件按实际日期入库，
“去年/前年/今年/当天/当周/星期X”等相对词只在查询时按当前时间动态翻译。
主角色名以占位符 {char} 存储（用户可自由修改角色预设而不丢失记忆）。

运行前会彻底清空记忆库（含冷存储分区），保证无任何旧业务数据残留。
输出：每条 原文 + 分类程序整理结果（参与者 / 事实立场 / 主题 / 双路检索数据）。
"""
import os
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
from memory_engine import time_utils

cfg.LLM_RECHECK_ENABLED = False     # 测试确定性：不做 LLM 网络复核（应用内由插件设置控制）

# ==================== 彻底清库（数据 + 结构） ====================
eng = memory_engine.get_engine()
eng.init()
eng.close()
shutil.rmtree(cfg.DATA_DIR, ignore_errors=True)     # 物理删除旧库 / 向量 / 冷分区 / 日志
memory_engine.reset_engine()
eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readwrite")
eng.set_char_name("洛天依")                          # 写库时把“洛天依”替换为 {char} 占位符

NOW = datetime.now()
CY, CQ = time_utils.year_quarter(NOW.timestamp())
print("当前时间: %s（%s/%s）\n" % (NOW.strftime("%Y-%m-%d %H:%M"), CY, CQ))


def _ts(days_ago=0, hours_ago=0.3):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).timestamp()


def _weekday_ts(weekday, hour):
    """本周指定星期几（1=周一..7=周日）的指定时刻；已过去则取上周同日。"""
    mon = (NOW - timedelta(days=NOW.weekday())).replace(hour=hour, minute=0, second=0, microsecond=0)
    t = mon + timedelta(days=weekday - 1)
    if t > NOW:
        t -= timedelta(days=7)
    return t.timestamp()


def _q_ts(year, quarter, day=15):
    month = (quarter - 1) * 3 + 1
    return datetime(year, month, day, 10, 0).timestamp()


def _q(year, quarter):
    q = str(quarter).upper()
    if not q.startswith("Q"):
        q = "Q%s" % q
    return q


def ing(fid, text, ts, year, quarter, topic, sub="", parts=None, facts=None):
    r = eng.ingest(text, fragment_id=fid, ts=ts, year=year, quarter=_q(year, quarter),
                   main_topic=topic, sub_topic=sub, participants=parts, facts_per_role=facts,
                   chunks=False)
    assert r.ok, (fid, r.message)
    return r


P1 = time_utils.prev_quarter(CY, CQ)
P2 = time_utils.prev_quarter(*P1)
LAST = CY - 1
BEFORE = CY - 2
P1Q, P2Q = int(P1[1][1]), int(P2[1][1])

# ==================== 数据构造（日常聊天场景） ====================
rows = []

# ---- 当天 ----
rows.append(ing("chat_today_1", "洛天依：今天下午三点约好一起去公园散步，记得带遮阳伞哦，天气特别好。",
                _ts(0, 0.2), CY, CQ, "日常", "约定", ["洛天依"]))
rows.append(ing("chat_today_2", "洛天依：今天想喝奶茶。\n洛天依（朋友）：那就去楼下那家，半糖少冰。",
                _ts(0, 0.5), CY, CQ, "美食", "奶茶", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "想喝奶茶"}, "洛天依（朋友）": {"action": "推荐楼下那家", "result": "半糖少冰"}}))

# ---- 当周（确定性星期落点：周六爬山 / 周三打游戏，保证“星期三呢”可命中） ----
rows.append(ing("chat_week_1", "洛天依：这周末说好一起爬山，我准备了好多好吃的野餐。",
                _weekday_ts(6, 15), CY, CQ, "日常", "约定", ["洛天依"]))
rows.append(ing("chat_week_2", "洛天依：周三晚上约了打游戏。\n洛天依（朋友）：好呀，顺便试试新买的耳机。",
                _weekday_ts(3, 21), CY, CQ, "娱乐", "游戏", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "约打游戏"}, "洛天依（朋友）": {"action": "试新耳机"}}))

# ---- 当月 ----
rows.append(ing("chat_month_1", "洛天依：这个月在学做小笼包，已经练了三次，还差一点火候。",
                _ts(3, 0), CY, CQ, "美食", "厨艺", ["洛天依"]))
rows.append(ing("chat_month_2", "洛天依：这个月生日快到了。\n洛天依（朋友）：那要好好庆祝，给你准备惊喜！",
                _ts(6, 0), CY, CQ, "社交", "生日", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "生日将至"}, "洛天依（朋友）": {"action": "准备惊喜"}}))
rows.append(ing("chat_month_3", "洛天依：这个月体重目标达成，奖励自己一顿火锅。",
                _ts(5, 0), CY, CQ, "健康", "减肥", ["洛天依"]))

# ---- 当年（最近两个已结束季度） ----
rows.append(ing("chat_year_1", "洛天依：今年%d季度开始学吉他，已经会弹两首曲子了。" % P1Q,
                _q_ts(P1[0], P1Q), P1[0], P1[1], "爱好", "吉他", ["洛天依"]))
rows.append(ing("chat_year_2", "洛天依：今年%d季度一起去游乐园玩。\n洛天依（朋友）：超开心，拍了超多照片！" % P2Q,
                _q_ts(P2[0], P2Q), P2[0], P2[1], "旅行", "游乐园", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "一起去游乐园"}, "洛天依（朋友）": {"result": "拍了超多照片"}}))

# ---- 去年 ----
rows.append(ing("chat_last_1", "洛天依：去年冬天一起去看雪，堆了一个大雪人。",
                _q_ts(LAST, 1), LAST, "Q1", "回忆", "看雪", ["洛天依"]))
rows.append(ing("chat_last_2", "洛天依：去年暑假一起去海边旅行。\n洛天依（朋友）：晒黑了一圈但超快乐！",
                _q_ts(LAST, 3), LAST, "Q3", "旅行", "海边", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "去海边旅行"}, "洛天依（朋友）": {"result": "晒黑但快乐"}}))

# ---- 前年 ----
rows.append(ing("chat_prev_1", "洛天依：前年生日收到一只小玩偶，一直放在床头。",
                _q_ts(BEFORE, 4), BEFORE, "Q4", "回忆", "礼物", ["洛天依"]))
rows.append(ing("chat_prev_2", "洛天依：前年跨年一起倒数放烟花。\n洛天依（朋友）：好怀念啊！",
                _q_ts(BEFORE, 4, 28), BEFORE, "Q4", "回忆", "跨年", ["洛天依", "洛天依（朋友）"],
                {"洛天依": {"action": "跨年倒数放烟花"}, "洛天依（朋友）": {"stance": "很怀念"}}))

print("入库 %d 条。\n" % len(rows))

# ==================== 分类 ====================
cr = eng.classify_records()
print("分类登记: 主题 %d / 季度 %d / 参与者 %d\n" % (
    cr["entries"]["topic"], cr["entries"]["quarter"], cr["entries"]["participant"]))

# ==================== 季度归档 ====================
arc = eng.archive(now=NOW.timestamp())
print("季度归档: 迁入 %d 条 | 活跃 %d / 归档 %d\n" % (
    arc["moved"], arc["active_count"], arc["archive_count"]))

# ==================== 原文 + 分类整理结果 ====================
import json as _json
print("=" * 96)
print("原文 与 分类程序整理结果（参与者以 {char} 占位符存储，展示时渲染回当前角色名）")
print("=" * 96)
for ts, y, q, topic, sub, parts_raw, summary, layer, fid in sorted(
        [(r["ts"], r["year"], r["quarter"], r["topic"], r["sub_topic"], r["participants"],
          r["full_summary"], "归档", r["id"]) for r in eng.archive_db.iter_rows()]
        + [(r["ts"], r["year"], r["quarter"], r["topic"], r["sub_topic"], r["participants"],
            r["full_summary"], "活跃", r["id"]) for r in eng.active.iter_rows()],
        key=lambda x: x[0]):
    db = eng.archive_db if layer == "归档" else eng.active
    row = db.get(fid)
    parts = _json.loads(parts_raw) if isinstance(parts_raw, str) else parts_raw
    facts = _json.loads(row["facts_per_role"]) if isinstance(row["facts_per_role"], str) else row["facts_per_role"]
    print("【原文】%s（%s/%s %s）" % (datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), y, q, layer))
    print("        %s" % (summary or "")[:80])
    print("【整理】参与者存储=%s | 主主题=%s | 副主题=%s" % ("、".join(parts) if parts else "无", topic, sub or "-"))
    for role, fd in facts.items():
        bits = []
        for k in ("action", "result", "stance"):
            v = (fd.get(k) or "") if isinstance(fd, dict) else ""
            if v:
                bits.append("%s:%s" % (k, v))
        print("        %s → %s" % (role, "；".join(bits) if bits else "-"))
    print("        检索词(searchable_text): %s" % (row["searchable_text"] or "")[:70])
    print("        语义(semantic_context): %s" % (row["semantic_context"] or "")[:70])
    print()

# ==================== 检索验证 ====================
def check(q, expect, role=None, extra_query=None, now=None):
    r = eng.search(q, now=now or NOW.timestamp(), role=role, extra_query=extra_query)
    if expect:
        hit = r.id in expect
    else:
        hit = r.id == ""            # 期望空结果（如“明天呢”无安排）
    print("%-30s 角色=%-14s -> route=%-5s conf=%.3f id=%s %s" % (
        q, "、".join(role) if isinstance(role, list) else (role or "无"),
        r.route, r.confidence, r.id or "∅", "✓" if hit else "✗"))
    return hit

print("=" * 96)
print("检索验证（相对时间词按当前时间动态翻译；“星期三呢”等省略追问带上一轮扩展）")
print("=" * 96)
ok = True
ok &= check("今天下午去公园散步", {"chat_today_1"}, "洛天依")
ok &= check("今天想喝奶茶", {"chat_today_2"}, ["洛天依", "洛天依（朋友）"])
ok &= check("今天我们有什么安排吗", {"chat_today_1", "chat_today_2"}, "洛天依")
ok &= check("周末爬山野餐", {"chat_week_1"}, "洛天依")
ok &= check("周三晚上打游戏", {"chat_week_2"}, "洛天依")
ok &= check("星期三呢", {"chat_week_2"}, "洛天依")                      # 星期X 解析 → 本周三窗口
eng.l0.append("user", "今天我们有什么安排吗", {})                       # 模拟上一轮，省略句扩展
ok &= check("星期三呢", {"chat_week_2"}, "洛天依",
            extra_query=eng.followup_extra("星期三呢"))                 # 追问扩展路径
ok &= check("明天呢", set(), "洛天依")                                  # 明天无安排 → 优雅空结果
ok &= check("这个月学做小笼包", {"chat_month_1"}, "洛天依")
ok &= check("这个月生日", {"chat_month_2"}, "洛天依")
ok &= check("今年%d季度学吉他" % P1Q, {"chat_year_1"}, "洛天依")
ok &= check("今年%d季度游乐园" % P2Q, {"chat_year_2"}, "洛天依")
ok &= check("去年冬天看雪", {"chat_last_1"}, "洛天依")
ok &= check("去年海边旅行", {"chat_last_2"}, ["洛天依", "洛天依（朋友）"])
ok &= check("前年生日玩偶", {"chat_prev_1"}, "洛天依")
ok &= check("前年跨年烟花", {"chat_prev_2"}, "洛天依")
print("=" * 96)
print("检索全部命中期望: %s" % ("✓ 是" if ok else "✗ 否"))

# ==================== 角色改名演示（只存 {char}，改预设不丢记忆） ====================
print("=" * 96)
print("角色改名演示：记忆只存 {char}，把预设从“洛天依”改成“小笼包”后仍可命中并正确展示")
print("=" * 96)
eng.set_char_name("小笼包")
r = eng.search("今天下午去公园散步", role="小笼包", now=NOW.timestamp())
print("查询: 今天下午去公园散步 | 角色=小笼包 → id=%s conf=%.3f route=%s" % (r.id, r.confidence, r.route))
print("渲染后摘要: %s" % r.full_summary)
print("渲染后参与者: %s" % "、".join(r.participants))
for role, fd in (r.facts_per_role or {}).items():
    print("  %s → 动作:%s" % (role, getattr(fd, "action", "")))
eng.set_char_name("洛天依")
