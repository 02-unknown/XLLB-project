# -*- coding: utf-8 -*-
"""归档价值判断 + 记忆页展开详情 验证（独立临时数据目录）：
1) is_filler_text：问候/寒暄/语气应答判为低信息量，实义句不误判；
2) record_turn：问候回合跳过长期归档（L0 仍记录），实质回合正常归档；
3) LLM 深度复核（stub）：1=归档 / 0=不归档；
4) 记忆页：view 含 searchable 详情字段，L3 分区详情接口可读取。
"""
import json
import os
import shutil
import sys

VERIFY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "memory_engine_av")
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = VERIFY_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.ARCHIVE_VALUE_CHECK = True
cfg.LLM_VALUE_CHECK = False
from memory_engine import text_utils

eng = memory_engine.get_engine()
eng.init()
eng.set_char_name("洛天依")
eng.set_mode("readwrite")

print("=" * 72)
print("1) is_filler_text（低信息量判定，通用字符类规则）")
print("=" * 72)
ok_filler = True
for t, expect in [("你好", True), ("哈哈", True), ("好的", True), ("谢谢", True), ("嗯嗯", True),
                  ("早上好呀", True), ("晚安", True), ("想喝奶茶", False),
                  ("我们周三晚上去打游戏吧", False), ("去年去海边旅行了", False), ("今天天气怎么样", False)]:
    got = text_utils.is_filler_text(t)
    ok_filler &= (got == expect)
    print("  %-22r -> %-5s %s" % (t, got, "✓" if got == expect else "✗"))

print()
print("=" * 72)
print("2) record_turn 归档价值判断（后台异步：问候跳过长期归档，L0 即时记录）")
print("=" * 72)
before = eng.active.count()
r1 = eng.record_turn("你好", "你好呀！今天过得怎么样？", meta={"role": "洛天依", "archive": True})
l0_immediate = eng.l0.size()          # L0 同步即时写入
eng._drain_archives()                  # 等待后台归档（含价值判断）处理完毕
ok_greet = eng.active.count() == before and l0_immediate == 2 and r1.get("archived") is True
print("  问候回合: 即时返回(L0=%d) 后台处理后活跃=%d（应不归档）%s" % (
    l0_immediate, eng.active.count(), "✓" if ok_greet else "✗"))
r2 = eng.record_turn("我们周三晚上一起去打游戏吧", "好呀，顺便试试新买的耳机！", meta={"role": "洛天依", "archive": True})
eng._drain_archives()
ok_real = eng.active.count() == before + 1
print("  实质回合: 后台归档后活跃=%d（应归档）%s" % (eng.active.count(), "✓" if ok_real else "✗"))
print("  L0 仍记录问候（会话内保留）: %s" % ("✓" if eng.l0.size() == 4 else "✗"))

print()
print("=" * 72)
print("3) LLM 深度复核（stub 判断模型：1=归档 / 0=不归档）")
print("=" * 72)
from core import llm as llm_core
stub = {"r": "1"}
llm_core.generate = lambda *a, **k: stub["r"]
cfg.LLM_VALUE_CHECK = True
ok_llm1 = eng._archive_value("今天天气怎么样呀", "今天有雨，记得带伞哦。") is True
print("  判断=1 → 值得归档: %s" % ("✓" if ok_llm1 else "✗"))
stub["r"] = "0"
ok_llm0 = eng._archive_value("今天心情怎么样", "还不错，谢谢关心。") is False
print("  判断=0 → 不值得归档: %s" % ("✓" if ok_llm0 else "✗"))
cfg.LLM_VALUE_CHECK = False

print()
print("=" * 72)
print("4) 记忆页展开详情：view 含 searchable，L3 分区详情接口可读")
print("=" * 72)
import web.server as srv
r = srv._api_memory_view({"query": {}})
b = json.loads(r[1].decode("utf-8"))
has_searchable = any("searchable" in f for f in b.get("active") or [])
print("  view 含 searchable 详情字段: %s" % ("✓" if has_searchable else "✗"))
parts = b.get("cold") or []
print("  冷分区数: %d" % len(parts))
ok_cold = True
for p in parts[:2]:
    r2 = srv._api_memory_view_cold({"query": {"partition": [p["partition"]]}})
    b2 = json.loads(r2[1].decode("utf-8"))
    ok_cold &= r2[0] == 200 and isinstance(b2.get("entries"), list)
print("  分区详情接口返回条目列表: %s" % ("✓" if ok_cold else "✗"))
r3 = srv._api_memory_view_cold({"query": {"partition": ["bad"]}})
print("  非法分区被拒绝: %s" % ("✓" if r3[0] == 400 else "✗"))

print()
ok_all = (ok_filler and ok_greet and ok_real and ok_llm1 and ok_llm0
          and has_searchable and ok_cold and r3[0] == 400)
print("验证结论: %s" % ("✓ 全部通过" if ok_all else "✗ 存在失败"))
eng.close()
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
