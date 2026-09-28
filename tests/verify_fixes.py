# -*- coding: utf-8 -*-
"""修复验证（独立临时数据目录，不影响真实 runtime/memory_engine）：
1) 短查询向量分不再≈0；2) record_turn 不再产生假“回复”角色；3) 时间解析边界；
4) 真实库无业务词残留；5) assemble_context 省略句扩展。
"""
import os
import shutil
import sys

VERIFY_DIR = r"<project-root>\runtime\memory_engine_verify"
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = VERIFY_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False            # 本脚本只验证检索与上下文，注入复核另测
from memory_engine import time_utils
from memory_engine import text_utils
from memory_engine.embedding import embed, cosine

eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readwrite")            # 引擎默认只读：测试写入必须显式放开（门禁语义）
eng.set_char_name("洛天依")
eng.set_debug(True)

print("=" * 78)
print("1) 短查询向量分（非负加权哈希修复后应 > 0，不再 0.000）")
print("=" * 78)
q = "今天我们有什么安排吗"
frag = ("洛天依：今天想喝奶茶。\n洛天依（朋友）：那就去楼下那家，半糖少冰。")
from memory_engine.preprocessing import make_fragment
f = make_fragment(frag, fragment_id="t_vec", participants=["洛天依", "洛天依（朋友）"],
                  facts_per_role={"洛天依": {"action": "想喝奶茶"}})
sim = cosine(embed(text_utils.normalize_query_text(q)), embed(f.semantic_context))
print("向量相似度(今天… vs 今天想喝奶茶): %.4f  %s" % (sim, "✓" if sim > 0.01 else "✗"))
sim2 = cosine(embed("星期三呢"), embed("洛天依：周三晚上约了打游戏。"))
print("向量相似度(星期三呢 vs 周三晚上打游戏): %.4f  %s" % (sim2, "✓" if sim2 > 0.005 else "✗"))

print()
print("=" * 78)
print("2) record_turn：不再产生假“回复”角色，参与者以 {char} 存储")
print("=" * 78)
r = eng.record_turn("今天下午一起去散步吧", "好呀，去公园走走，记得带伞。",
                    meta={"role": "洛天依", "main_topic": "日常", "archive": True})
print("record_turn ->", r)
eng._drain_archives()   # 等待后台归档完成（归档为异步执行）
row = None
for db in (eng.active, eng.archive_db):
    for rr in db.iter_rows():
        if "散步" in (rr["full_summary"] or ""):   # 摘要=用户输入（含“散步”）
            row = rr
            break
if row is None:
    print("✗ 未找到归档的回合片段")
else:
    import json
    parts = json.loads(row["participants"])
    facts = json.loads(row["facts_per_role"])
    print("片段:", row["id"], "| 参与者:", parts, "| 主题:", row["topic"])
    print("事实角色:", list(facts.keys()))
    bad = "回复" in parts or any("回复" in k for k in facts)
    summary_is_user = ("散步" in (row["full_summary"] or ""))   # 摘要=用户输入，而非助手回复
    print("假角色检查: %s %s" % ("✗ 存在“回复”假角色" if bad else "✓ 无假角色",
                                "| 占位符存储 ✓" if any("{char}" in p for p in parts) else "| ✗ 无占位符"))
    print("归档核心=用户输入: %s %s" % ("✓" if summary_is_user else "✗", "| 参与者含用户 ✓" if "用户" in parts else "| ✗ 无用户"))

print()
print("=" * 78)
print("3) 时间解析边界（星期X / 明天 / 前天 / 周末）")
print("=" * 78)
import datetime
now = datetime.datetime.now().timestamp()
for t in ["星期三呢", "这周三", "下周三", "上周三", "明天", "后天", "前天", "周末", "周杰伦的歌"]:
    tr = time_utils.parse_time_range(t, now)
    tag = ""
    if tr["start"] is not None:
        s = datetime.datetime.fromtimestamp(tr["start"]).strftime("%m-%d %H:%M")
        e = datetime.datetime.fromtimestamp(tr["end"]).strftime("%m-%d %H:%M")
        tag = "→ %s ~ %s" % (s, e)
    print("  %-10s mode=%-8s year=%s %s" % (t, tr["mode"], tr["year"], tag))

print()
print("=" * 78)
print("4) 真实库业务词残留检查（runtime/memory_engine 当前数据）")
print("=" * 78)
import sqlite3
bad_words = ["业绩", "战略", "营收", "预算", "促销", "华东", "华南", "华北", "华中",
             "研发", "人事", "离职", "招聘", "扩张", "收缩", "销售额", "利润"]
found = []
for db_path in (r"<project-root>\runtime\memory_engine\active\memory.db",
                r"<project-root>\runtime\memory_engine\archive\memory.db"):
    if not os.path.isfile(db_path):
        continue
    con = sqlite3.connect(db_path)
    try:
        for (text,) in con.execute("SELECT searchable_text FROM fragments"):
            for w in bad_words:
                if w in (text or ""):
                    found.append((db_path.split("memory_engine")[1], w))
    except Exception:
        pass
    con.close()
print("残留业务词: %s" % ("✗ " + str(found) if found else "✓ 无"))

print()
print("=" * 78)
print("5) assemble_context 省略句扩展（L0 上一轮 → 星期三呢 命中本周三片段）")
print("=" * 78)
# 先在临时库放入一条“本周三”的片段（与种子数据同规则：本周三晚打游戏）
from datetime import datetime, timedelta
now_dt = datetime.now()
mon = (now_dt - timedelta(days=now_dt.weekday())).replace(hour=21, minute=0, second=0, microsecond=0)
wed = mon + timedelta(days=2)
if wed > now_dt:
    wed -= timedelta(days=7)
eng.ingest("洛天依：周三晚上约了打游戏。", fragment_id="t_wed", ts=wed.timestamp(),
           year=wed.year, quarter="Q%d" % ((wed.month - 1) // 3 + 1),
           main_topic="娱乐", participants=["洛天依"])
eng.l0.append("user", "今天我们有什么安排吗", {})
eng.l0.append("assistant", "今天想喝奶茶，去楼下那家半糖少冰。", {})
ctx = eng.assemble_context("星期三呢", role="洛天依")
mem = ctx.get("memory")
print("memory id=%s conf=%s route=%s" % (mem.id if mem else "∅",
                                         mem.confidence if mem else "-",
                                         mem.route if mem else "-"))
print("  %s" % ("✓ 省略句扩展命中" if mem and mem.id else "✗ 未命中"))
print("recent 回合数:", len(ctx.get("recent", [])))

print()
eng.close()
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
print("临时验证目录已清理")
