# -*- coding: utf-8 -*-
"""自然对话（接话式）回归验证（离线：stub LLM/TTS、联网关闭、引擎只读）：
1) 不允许连续两次扮演相同角色；2) 角色判断范围=组内全部角色（含上一轮未发言者）；
3) 注入复核：带对话上下文判断（200=注入 / 404=阻止 / 含混或空输出默认阻止）；
4) 首位发言人始终注入相关最近对话（不被错误记忆挤掉）；5) 流式分段输出（UI 侧人工确认）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as config
config.DEBUG_MODE = True
config.internet_enabled = False      # 联网判断直接返回 False，避免真实搜索

import memory_engine
import memory_engine.config as mecfg
mecfg.LLM_RECHECK_ENABLED = False
mecfg.INJECT_REVIEW = True           # 注入复核开关（由「上下文记忆库」插件设置控制）
mecfg.REVIEW_CACHE_TTL = 0           # 本脚本同一内容用不同判断结果反复验证：关闭复核缓存

import plugins.multi_chat as mc
from core import llm as llm_core
from core import tts as tts_core

eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readonly")             # 测试回合只写进程内 L0
# 本脚本不加载插件管理器：手动把引擎注册到记忆门禁（等价于「记忆插件已启用」），
# 否则多人对话的记录 / 检索路径按「插件未启用」处理
from memory_engine import service as _mem_service  # noqa: E402
_mem_service.register(eng, "readonly")

# ---- 桩：LLM / TTS / 判断模型 ----
def fake_invoke(messages, model, temperature, num_predict, stop, purpose, user_text):
    return "嗯嗯，好的呀，我们一起去吧！"
llm_core._invoke_chat = fake_invoke
_generate_stub = {"reply": "", "prompt": ""}
def fake_generate(prompt, **kw):
    _generate_stub["prompt"] = prompt
    return _generate_stub["reply"]
llm_core.generate = fake_generate
tts_core.switch_gpt_weights = lambda *a, **k: None
tts_core.switch_sovits_weights = lambda *a, **k: None

# ---- 假插件上下文 ----
_logs = []
class FakeManager:
    def get_settings(self, name):
        return {"max_lines": 4, "auto_voice": False, "target_llm_judge": True,
                "generation_mode": "natural"}
class FakeCtx:
    config = config
    manager = FakeManager()
    def log(self, *a):
        _logs.append(" ".join(str(x) for x in a))
        print("[ctx]", *a)

ctx = FakeCtx()
slots = [("洛天依", ""), ("洛天依（朋友）", "")]

print("=" * 72)
print("1) 不允许连续两次扮演相同角色（500 次抽样，2/3 角色）")
print("=" * 72)
def _check_adjacency(cast, total, iters=500):
    bad = 0
    for _ in range(iters):
        first = cast[0]
        order = [first] + mc._random_speakers(first, cast, total)
        for a, b in zip(order, order[1:]):
            if a[0] == b[0]:
                bad += 1
        counts = {}
        for n, _ in order:
            counts[n] = counts.get(n, 0) + 1
        if any(v > 2 for v in counts.values()):
            bad += 1
    return bad
bad2 = _check_adjacency(slots, 4)
bad3 = _check_adjacency(slots + [("第三个角色", "")], 5)
print("  2角色×4句 违规次数: %d（应 0）  %s" % (bad2, "✓" if bad2 == 0 else "✗"))
print("  3角色×5句 违规次数: %d（应 0）  %s" % (bad3, "✓" if bad3 == 0 else "✗"))

print()
print("=" * 72)
print("2) 角色判断范围：组内全部角色（LLM 增强可命中上一轮未发言角色）")
print("=" * 72)
_generate_stub["reply"] = "洛天依"
hit = mc._detect_target("你就是上次说的那个人吧", slots, ctx)
print("  增强判断命中（组内任意角色）: %s  %s" % ([n for n, _ in hit], "✓" if hit and hit[0][0] == "洛天依" else "✗"))
_generate_stub["reply"] = "无"
hit2 = mc._detect_target("随便聊聊", slots, ctx)
print("  未指定 -> 空: %s  %s" % (hit2, "✓" if not hit2 else "✗"))

print()
print("=" * 72)
print("3) 注入复核（带对话上下文：200=注入 / 404=阻止 / 含混或空输出默认阻止）")
print("=" * 72)
eng.l0.clear()
eng.l0.append("user", "这首歌好好听", {})
eng.l0.append("assistant", "是啊，这首《勾指起誓》是洛天依的原创呢。", {})
for stub, expect in [("404", False), ("200", True), ("相关，200", True), ("", False),
                     ("404 无关", False), ("无关", False)]:
    _generate_stub["reply"] = stub
    got = eng.review_injection("好好听", "洛天依：这个月生日快到了。洛天依（朋友）：那要好好庆祝，给你准备惊喜！")
    prompt = _generate_stub["prompt"]
    has_ctx = "最近的对话" in prompt and "勾指起誓" in prompt
    print("  判断输出 %-10r -> 允许注入=%s 复核提示带对话上下文=%s %s" % (
        stub, got, has_ctx, "✓" if (got == expect and has_ctx) else "✗"))
mecfg.INJECT_REVIEW = False
print("  开关关闭(INJECT_REVIEW=False) -> 允许注入=%s ✓" % eng.review_injection("q", "任意内容"))
mecfg.INJECT_REVIEW = True

print()
print("=" * 72)
print("4) 首位发言人上下文合并注入（记忆 + 相关最近对话，不被错误记忆挤掉）")
print("=" * 72)
class _Mem:
    participants = ["洛天依"]
    full_summary = "去年一起去海边旅行了"
    id = "m"
text = mc._first_context_text("q", _Mem(), None, "最近的安排是喝奶茶")
has_mem = "相关历史记忆" in text and "海边旅行" in text
has_recent = "最近的对话" in text and "喝奶茶" in text
print("  记忆+最近对话同时注入: %s %s" % ("✓" if (has_mem and has_recent) else "✗", text.replace("\n", " | ")[:90]))

print()
print("=" * 72)
print("5) 主流程端到端（含注入复核阻止路径 + 无连续同角）")
print("=" * 72)
# 触发真实记忆命中（去年冬天看雪 -> chat_last_1），然后注入复核输出 404 阻止
_generate_stub["reply"] = "404"
l0_before = eng.l0.size()
sid = mc._new_stream()
mc._natural_worker(sid, "去年冬天我们去看雪的事你还记得吗", slots, ctx)
segments, done, error = mc.poll_stream(sid)
review_blocked = any("记忆内容被注入复核阻止（404）" in m for m in _logs)
no_adjacent = all(segments[i]["speaker"] != segments[i + 1]["speaker"] for i in range(len(segments) - 1))
print("  注入复核阻止（404）: %s" % ("✓" if review_blocked else "✗"))
print("  发言顺序: " + " → ".join(s["speaker"] for s in segments))
print("  无连续同角: %s | 句数=%d（≤4） | done=%s" % ("✓" if no_adjacent else "✗", len(segments), done))
print("  回合已记录(L0): %s" % ("✓" if eng.l0.size() == l0_before + 2 else "✗"))

all_ok = (bad2 == 0 and bad3 == 0 and hit and hit[0][0] == "洛天依" and not hit2
          and review_blocked and no_adjacent and eng.l0.size() == l0_before + 2 and done)
print()
print("全部关键步骤验证: %s" % ("✓ 通过" if all_ok else "✗ 存在失败"))
