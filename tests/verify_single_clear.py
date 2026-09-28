# -*- coding: utf-8 -*-
"""单人对话检索逻辑 + 残留清理纠错机制 验证（离线：stub 判断模型、联网关闭、引擎只读）：
1) 单人对话记忆注入前经过注入复核（无关记忆被拦：assemble_context 不注入、_context_hit 不跳过联网）；
2) 同回合复核缓存（相同内容只复核一次，避免重复调用）；
3) 纠错机制：长时间未活动（直接关窗退出后重新使用）自动清理 L0/L1 与镜像历史。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as config
config.DEBUG_MODE = True
config.internet_enabled = False

import memory_engine
import memory_engine.config as mecfg
mecfg.LLM_RECHECK_ENABLED = False
mecfg.INJECT_REVIEW = True

from core import llm as llm_core
from core import pipeline

eng = memory_engine.get_engine()
eng.init()
eng.set_char_name(getattr(config, "character_name", "洛天依") or "洛天依")
eng.set_mode("readonly")             # 测试回合只写进程内 L0
# 本脚本不加载插件管理器：手动把引擎注册到记忆门禁（等价于「记忆插件已启用」），
# 否则核心路径按「插件未启用」处理，拿不到引擎
from memory_engine import service as _mem_service  # noqa: E402
_mem_service.register(eng, "readonly")

_calls = {"n": 0}
_generate_stub = {"reply": "200"}
def fake_generate(prompt, **kw):
    _calls["n"] += 1
    return _generate_stub["reply"]
llm_core.generate = fake_generate

print("=" * 72)
print("1) 单人对话记忆注入复核（与多人对话同套逻辑）")
print("=" * 72)
eng.l0.clear()
eng.l0.append("user", "今天有什么安排吗", {})
eng.l0.append("assistant", "今天想喝奶茶。", {})

_generate_stub["reply"] = "404"
ctx = eng.assemble_context("去年冬天我们去看雪的事还记得吗", role="洛天依")
mem = ctx.get("memory")
print("  复核404 → assemble_context 不注入记忆: %s (memory=%s)" % (
    "✓" if mem is None else "✗", mem.id if mem else "None"))

_generate_stub["reply"] = "404"
r = pipeline._context_hit("去年冬天看雪", role=config.character_name)
print("  复核404 → _context_hit 不跳过联网（交给判断）: %s (返回 %s)" % ("✓" if r is False else "✗", r))
_generate_stub["reply"] = "200"
eng._review_cache.clear()            # 清掉上一条目的复核缓存，验证“200→跳过联网”
r2 = pipeline._context_hit("去年冬天看雪", role=config.character_name)
print("  复核200 → _context_hit 跳过联网: %s (返回 %s)" % ("✓" if r2 is True else "✗", r2))

print()
print("=" * 72)
print("2) 同回合复核缓存（相同内容只复核一次）")
print("=" * 72)
_calls["n"] = 0
_generate_stub["reply"] = "200"
eng.review_injection("q1", "相同内容abc")
eng.review_injection("q1", "相同内容abc")
eng.review_injection("q2", "其他内容xyz")
print("  判断模型调用次数 = %d（相同内容1次 + 不同内容1次，应为 2）  %s" % (
    _calls["n"], "✓" if _calls["n"] == 2 else "✗"))

print()
print("=" * 72)
print("3) 纠错机制：长时间未活动自动清理残留上下文")
print("=" * 72)
mecfg.STALE_CONTEXT_TIMEOUT = 1      # 1 秒（测试用）
eng.l0.clear()
eng.l0.append("user", "残留旧对话", {})
eng.search("残留旧对话", role="洛天依")          # 填充 L1
config.conversation_history.append({"role": "user", "content": "残留镜像"})
eng._last_active = time.time() - 10              # 模拟已离开很久
cleared = eng.maybe_clear_stale()
ok_stale = (cleared and eng.l0.size() == 0 and eng.l1.size() == 0
            and len(config.conversation_history) == 0)
print("  超时清理: 触发=%s | L0=%d L1=%d mirror=%d  %s" % (
    cleared, eng.l0.size(), eng.l1.size(), len(config.conversation_history),
    "✓" if ok_stale else "✗"))
eng.l0.append("user", "新对话", {})
cleared2 = eng.maybe_clear_stale()               # 刚活动过 → 不清理
print("  未超时不清理: 触发=%s | L0=%d  %s" % (
    cleared2, eng.l0.size(), "✓" if (not cleared2 and eng.l0.size() == 1) else "✗"))
mecfg.STALE_CONTEXT_TIMEOUT = 30 * 60

print()
all_ok = (mem is None and r is False and r2 is True
          and _calls["n"] == 2 and ok_stale and not cleared2 and eng.l0.size() == 1)
print("验证结论: %s" % ("✓ 全部通过" if all_ok else "✗ 存在失败"))
