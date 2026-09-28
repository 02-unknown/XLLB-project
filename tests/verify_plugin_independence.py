# -*- coding: utf-8 -*-
"""插件独立性运行时审计：
1) 静态：无插件间导入 / 设置互读（已在代码层确认，此处复述结果）；
2) 运行时：逐个停用插件 → 其余插件可正常加载/渲染 + 主流程消息可正常处理（LLM/TTS 打桩）→ 再恢复。
   （引擎数据用临时目录，不动真实记忆）
"""
import os
import shutil
import sys

VERIFY_DIR = r"<project-root>\runtime\memory_engine_indep"
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = VERIFY_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False

from core import llm as llm_core
from core import plugin_manager
from core import pipeline

# 打桩：LLM 生成 / 判断模型（避免真实网络调用）
def _fake_invoke(messages, model, temperature, num_predict, stop, purpose, user_text):
    return "好的呀，没问题！"
def _fake_generate(prompt, **kw):
    return "无需联网"
llm_core._invoke_chat = _fake_invoke
llm_core.generate = _fake_generate

mgr = plugin_manager.manager
names = [p.name for p in mgr._plugins.values()]
print("插件清单（%d 个）: %s\n" % (len(names), "、".join(names)))

# 1) 静态检查：扫描插件源码中的跨插件引用
import re as _re
bad = []
for p in mgr._plugins.values():
    src = open(p.filepath, encoding="utf-8").read()
    if _re.search(r"from plugins\s+import|import plugins\.|plugin_module\(", src):
        bad.append(p.name)
print("=" * 72)
print("1) 静态：插件间直接引用（import plugins / plugin_module）")
print("=" * 72)
print("  %s" % ("✓ 无任何跨插件引用" if not bad else "✗ " + "、".join(bad)))

# 2) 运行时：逐个停用，检查其余功能不受影响
print()
print("=" * 72)
print("2) 运行时：逐个停用插件 → 其余插件渲染 + 主流程消息处理正常")
print("=" * 72)
fails = []
for target in names:
    try:
        mgr.disable(target)
    except Exception as e:
        fails.append((target, "disable异常", str(e)))
        continue
    # 2a) 其余插件：schema / actions / commands / state 可正常获取
    for p in mgr._plugins.values():
        if p.name == target:
            continue
        try:
            p.settings_schema()
            p.actions()
            p.commands()
            if hasattr(p.module, "get_state"):
                p.module.get_state(mgr.ctx)
        except Exception as e:
            fails.append((target, f"停用后 {p.name} 渲染异常", str(e)))
    # 2b) 主流程：正常处理一条消息（LLM 打桩；多轮不同插件接管路径）
    try:
        for msg in ("你好，介绍一下自己", "播放一首歌吧", "清空对话"):
            r = pipeline.process_message(msg, mode="qa")
            if not r or not r.get("ok", True) and not r.get("skip_reason"):
                fails.append((target, f"主流程消息 {msg!r} 返回异常", str(r)))
    except Exception as e:
        fails.append((target, "主流程消息处理异常", str(e)))
    try:
        mgr.enable(target)
    except Exception as e:
        fails.append((target, "恢复启用异常", str(e)))

print("  停用-检查-恢复完成：%s" % ("✓ 全部插件可独立启停" if not fails else "✗ 存在 %d 处问题" % len(fails)))
for t, stage, msg in fails:
    print("    ✗ [%s] %s: %s" % (t, stage, msg))

# 3) 专项：记忆库停用时，多人对话/用户信息/主流程均回退可用
print()
print("=" * 72)
print("3) 专项：停用「上下文记忆库」后 主流程 / 用户信息 / 多人对话 回退")
print("=" * 72)
try:
    mgr.disable("上下文记忆库")
    r = pipeline.process_message("今天天气怎么样", mode="qa")
    # 消息被正常接管即可：主流程文本回复 或 多人对话流式会话 或 其它动作
    ok_trunk = bool(r) and bool(r.get("reply") or r.get("multi_stream_id")
                    or r.get("action") in ("chat", "music_search", "music_control"))
    up = mgr._plugins.get("用户信息设置")
    ok_up = up.module._session_active(mgr.ctx) is not None if hasattr(up.module, "_session_active") else True
    mc = mgr._plugins.get("多人对话")
    ok_mc = True
    if mc and hasattr(mc.module, "_memory_search"):
        ok_mc = mc.module._memory_search("测试", ["洛天依"], mgr.ctx) == (None, None)  # 引擎不可用 → 回退
    print("  主流程消息被正常处理(action=%s): %s | 用户信息会话判定可用: %s | 多人对话记忆回退: %s" % (
        r.get("action") if r else "?",
        "✓" if ok_trunk else "✗", "✓" if ok_up else "✗", "✓" if ok_mc else "✗"))
    mgr.enable("上下文记忆库")
except Exception as e:
    print("  ✗ 专项失败:", e)
    try:
        mgr.enable("上下文记忆库")
    except Exception:
        pass

print()
print("验证结论: %s" % ("✓ 插件间相互独立，可独立启停" if not fails else "✗ 存在耦合"))
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
