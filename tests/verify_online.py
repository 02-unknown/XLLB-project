# -*- coding: utf-8 -*-
"""联网判断逻辑验证（离线，不产生网络调用）：
1) L0 IDF 打分：泛用词重合不再算实质命中；
2) _context_hit 决策：新闻类问题放行给联网判断，记忆类问题跳过联网；
3) judge_need_online 决策逻辑（stub 判断模型输出）。
只追加进程内 L0，不改动真实数据。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False            # 本脚本只验证联网判断与上下文命中，注入复核另测

import core.config as config
from core import pipeline, judge

eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readwrite")            # 引擎默认只读：测试写入必须显式放开（门禁语义）
# 本脚本不加载插件管理器，手动把引擎注册到记忆门禁（等价于「记忆插件已启用」），
# 否则核心路径按「插件未启用」处理，拿不到引擎
from memory_engine import service as _mem_service  # noqa: E402
_mem_service.register(eng, "readwrite")
eng.set_char_name(getattr(config, "character_name", "洛天依") or "洛天依")

# 模拟用户会话（L0 窗口，进程内）
eng.l0.append("user", "今天我们有什么安排吗", {})
eng.l0.append("assistant", "今天想喝奶茶，去楼下那家半糖少冰。", {})

print("=" * 72)
print("1) L0 IDF 加权重合分（泛用词权重低，实质内容词权重高）")
print("=" * 72)
for q in ["昨天有什么新闻吗", "那个奶茶店叫什么", "那周六呢"]:
    hits = eng.l0.query_scored(q, limit=2)
    line = "；".join("%.2f「%s」" % (s, (e.get("content") or "")[:16]) for s, e in hits) or "（无）"
    print("  %-16s -> %s" % (q, line))
    if hits:
        print("      ≥ L0_HIT_MIN_SCORE(%.1f) ? %s" % (cfg.L0_HIT_MIN_SCORE, hits[0][0] >= cfg.L0_HIT_MIN_SCORE))

print()
print("=" * 72)
print("2) _context_hit 决策（True=跳过联网；False=交给 judge_need_online）")
print("=" * 72)
for q in ["昨天有什么新闻吗", "就是大新闻有吗", "去年冬天看雪", "周末去爬山吗", "星期三呢"]:
    r = pipeline._context_hit(q, role=config.character_name)
    print("  %-16s -> %s" % (q, "跳过联网（True）" if r else "交给联网判断（False）"))

print()
print("=" * 72)
print("3) judge_need_online 决策逻辑（stub 判断模型输出）")
print("=" * 72)
cases = [("需要联网", True), ("需要联网。", True), ("无需联网", False),
         ("不需要联网", False), ("不用联网", False), ("", False)]
ok = True
for stub, expect in cases:
    judge.generate = lambda *a, **k: stub
    got = judge.judge_need_online("昨天有什么新闻吗")
    mark = "✓" if got == expect else "✗"
    ok = ok and got == expect
    print("  stub=%-12r -> %s %s" % (stub, got, mark))
print("  judge 决策逻辑: %s" % ("全部正确" if ok else "存在偏差"))
