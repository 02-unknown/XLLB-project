# -*- coding: utf-8 -*-
"""读取真实数据目录，输出最终录入的上下文 时间×事件 表（供用户亲自测试参考）。

角色名以占位符 {char} 存储（可自由改名不丢记忆），展示时渲染回当前角色名。
相对时间标签（当年/去年/前年）按当前年份动态计算，不写死。
"""
import os
import json
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_engine
import memory_engine.config as cfg
from memory_engine import roles
from memory_engine import time_utils

cfg.LLM_RECHECK_ENABLED = False     # 展示脚本确定性：不做 LLM 网络复核

eng = memory_engine.get_engine()
eng.init()
eng.set_char_name("洛天依")          # 展示用当前角色名（可改成你预设的新名字试试）
CY = datetime.now().year

rows = []
for db, layer in ((eng.active, "活跃"), (eng.archive_db, "归档")):
    for r in db.iter_rows():
        rows.append((r["ts"], r["year"], r["quarter"], r["topic"], r["sub_topic"],
                     r["participants"], r["full_summary"], layer, r["id"]))

rows.sort(key=lambda x: x[0])
print("=" * 100)
print("最终录入的测试上下文（%d 条，数据保留于 runtime/memory_engine，未删除）" % len(rows))
print("=" * 100)
print("%-22s %-8s %-6s %-6s %-8s %-20s %s" % (
    "时间", "季度", "主题", "副题", "层", "参与角色", "事件摘要"))
print("-" * 100)
for ts, y, q, topic, sub, parts, summary, layer, fid in rows:
    t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    # 相对时间标签按当前年份动态计算，不写死
    if y == CY:
        dim = "当年"
    elif y == CY - 1:
        dim = "去年"
    elif y == CY - 2:
        dim = "前年"
    else:
        dim = str(y)
    role_txt = "、".join(roles.render_list(json.loads(parts), eng.char_name)) if parts else "-"
    summary_show = roles.render(summary, eng.char_name)
    print("%-22s %-8s %-6s %-6s %-8s %-20s %s" % (
        t + "（" + dim + "）", q, topic, sub or "-", layer, role_txt[:18], (summary_show or "")[:34]))
    _ = fid

print("=" * 100)
print("分类索引（主题/季度/参与者 维度；参与者以占位符形式登记）")
cats = eng.list_categories(limit=60)
for ct in ("topic", "quarter", "participant"):
    items = [(c["cat_value"], c["count"]) for c in cats if c["cat_type"] == ct]
    print("  %-12s %s" % (ct, "，".join("%s×%d" % (v, n) for v, n in items[:24])))

print("=" * 100)
print("多角色事实示例（chat_last_2 · 去年海边旅行，已渲染回当前角色名）")
frag = eng.archive_db.get("chat_last_2") or eng.active.get("chat_last_2")
if frag:
    for role, fd in frag["facts_per_role"].items():
        d = fd if isinstance(fd, dict) else {}
        print("  %s → 动作:%s | 结果:%s | 立场:%s" % (
            roles.render(role, eng.char_name),
            roles.render(d.get("action", ""), eng.char_name),
            roles.render(d.get("result", ""), eng.char_name),
            roles.render(d.get("stance", ""), eng.char_name)))

print("=" * 100)
print("角色改名演示（记忆只存 {char}，预设改名后仍命中并正确展示）")
print("-" * 100)
for new_name in ("小笼包", "洛天依"):
    eng.set_char_name(new_name)
    r = eng.search("今天下午去公园散步", role=new_name)
    print("角色=%s → id=%s conf=%.3f route=%s" % (new_name, r.id or "∅", r.confidence, r.route))
    if r.id:
        print("    摘要: %s" % r.full_summary)
        print("    参与者: %s" % "、".join(r.participants))
