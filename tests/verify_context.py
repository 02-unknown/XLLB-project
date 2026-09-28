# -*- coding: utf-8 -*-
"""上下文感知 + 真正清空上下文 验证：
1) 话题切换（酸菜鱼做法 → 新歌推荐）后，旧话题回合不再注入 LLM 上下文；
   同话题延续保留；省略式追问保留窗口（通用方案：IDF 加权重合度过滤，无主题词表）。
2) storage.clear_history() 真正清除记忆引擎 L0/L1（不只是显示）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False            # 本脚本只验证相关度过滤与清空，注入复核另测

import core.config as config
from core import storage

eng = memory_engine.get_engine()
eng.init()
eng.set_char_name("洛天依")

print("=" * 72)
print("1) 上下文感知：话题切换后旧话题回合不再注入")
print("=" * 72)
eng.l0.clear()
eng.l0.append("user", "酸菜鱼怎么做才好吃呀", {})
eng.l0.append("assistant", "先把酸菜炒香，再下鱼片，最后放醋和花椒。", {})

ctx = eng.assemble_context("有没有新歌推荐", role="洛天依")
recent = [t.get("content") for t in ctx.get("recent", [])]
leak = any(("酸菜" in c or "花椒" in c) for c in recent)
print("  查询「有没有新歌推荐」→ 注入回合: %s" % (recent if recent else "（无，全新回答）"))
print("  旧话题（酸菜鱼）被过滤: %s" % ("✓" if not leak else "✗ 仍残留"))

ctx2 = eng.assemble_context("酸菜鱼还要放醋吗", role="洛天依")
recent2 = [t.get("content") for t in ctx2.get("recent", [])]
keep = any("酸菜" in c for c in recent2)
print("  查询「酸菜鱼还要放醋吗」→ 注入回合: %s" % recent2)
print("  同话题回合被保留: %s" % ("✓" if keep else "✗ 被误删"))

eng.l0.clear()
eng.l0.append("user", "今天我们有什么安排吗", {})
eng.l0.append("assistant", "今天想喝奶茶，去楼下那家半糖少冰。", {})
ctx3 = eng.assemble_context("那周六呢", role="洛天依")
recent3 = [t.get("content") for t in ctx3.get("recent", [])]
print("  省略追问「那周六呢」→ 注入回合: %s" % recent3)
print("  省略追问保留窗口: %s" % ("✓" if len(recent3) == 2 else "✗"))

print()
print("=" * 72)
print("2) 清空上下文：storage.clear_history 真正清除 L0/L1")
print("=" * 72)
eng.l0.clear()
eng.l0.append("user", "酸菜鱼怎么做", {})
eng.l0.append("assistant", "先把酸菜炒香。", {})
eng.search("酸菜鱼", role="洛天依")     # 填充 L1 缓存
print("  清空前: L0=%d L1=%d conversation_history=%d" % (
    eng.l0.size(), eng.l1.size(), len(config.conversation_history)))
storage.clear_history()
print("  清空后: L0=%d L1=%d conversation_history=%d" % (
    eng.l0.size(), eng.l1.size(), len(config.conversation_history)))
ok_clear = eng.l0.size() == 0 and eng.l1.size() == 0 and len(config.conversation_history) == 0
print("  L0/L1/镜像 全部清除: %s" % ("✓" if ok_clear else "✗"))

print()
all_ok = (not leak and keep and len(recent3) == 2 and ok_clear)
print("验证结论: %s" % ("✓ 全部通过" if all_ok else "✗ 存在失败"))
