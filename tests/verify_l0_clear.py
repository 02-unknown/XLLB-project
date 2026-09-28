# verify_l0_clear.py —— 验证「上下文记忆库 → 清理 L0 对话缓存」一键清理。
#
# 需求：上下文插件新增清理缓存选项，一键清理 L0 的对话缓存。
# 做法：用替身引擎直接调用插件的 clear_l0 动作，检查：
#   · 高级选项里确实有这个动作（可一键执行、无需二级确认）；
#   · 动作会清空 L0 会话缓存 / L1 检索热缓存，并调用引擎的 clear_context()（含代次与待归档队列处理）；
#   · 长期记忆（L2 活跃 / 归档）完全不受影响；
#   · 结果里如实报告清理前后的数量，异常时给出可读错误。
#   venv\Scripts\python.exe tests\verify_l0_clear.py
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
OKS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def _load_memory_plugin():
    path = os.path.join(ROOT, "plugins", "memory.py")
    spec = importlib.util.spec_from_file_location("_memory_plugin_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


memory = _load_memory_plugin()

# ---------- 1) 动作已登记在「高级选项」里 ----------
schema = memory.settings_schema()
advanced = next((s for s in schema if s.get("key") == "advanced"), {})
names = [a.get("name") for a in (advanced.get("actions") or [])]
check("高级选项里新增了 clear_l0 动作（一键清理 L0 对话缓存）", "clear_l0" in names, str(names))
entry = next((a for a in advanced.get("actions") or [] if a.get("name") == "clear_l0"), {})
check("动作有清晰的中文标签与说明（说明只清缓存、不动长期记忆）",
      "对话缓存" in (entry.get("label") or "") and "长期记忆" in (entry.get("desc") or ""),
      str(entry)[:140])
check("动作名不以 _do 结尾（属于一键操作，不需要二级确认）",
      not str(entry.get("name") or "").endswith("_do"))

from web import server as web_server  # noqa: E402
check("服务端不会把该动作当成二次确认动作",
      web_server._is_confirm_action(memory.NAME, "clear_l0") is False)


# ---------- 2) 用替身引擎执行动作 ----------
class FakeL0:
    def __init__(self, n):
        self.n = n
        self.cleared = 0

    def size(self):
        return self.n

    def clear(self):
        self.cleared += 1
        self.n = 0


class FakeL1:
    def __init__(self, n):
        self.n = n

    def size(self):
        return self.n

    def clear(self):
        self.n = 0


class FakeEngine:
    def __init__(self):
        self.l0 = FakeL0(7)
        self.l1 = FakeL1(3)
        self.cleared_context = 0
        self.active_count = 12      # 长期记忆（活跃区）
        self.archive_count = 5      # 长期记忆（归档区）

    def clear_context(self):
        self.cleared_context += 1
        self.l0.clear()
        self.l1.clear()


eng = FakeEngine()
memory._get_engine = lambda ctx: eng
r = memory.on_action("clear_l0", None)
reply = (r or {}).get("reply", "")
check("动作执行成功并返回说明", bool(reply) and (r or {}).get("speak") is False, reply[:120])
check("清空了 L0 会话缓存", eng.l0.size() == 0 and eng.l0.cleared == 1)
check("同时清空了 L1 检索热缓存", eng.l1.size() == 0)
check("调用了引擎的 clear_context（含会话代次 / 待归档队列处理）", eng.cleared_context == 1)
check("结果里报告清理前后的数量（7 → 0 / 3 → 0）",
      "7" in reply and "0" in reply and "L0" in reply and "L1" in reply, reply[:160])
check("长期记忆完全不受影响（活跃 / 归档数量不变）",
      eng.active_count == 12 and eng.archive_count == 5)


class BrokenEngine(FakeEngine):
    def clear_context(self):
        raise RuntimeError("引擎已关闭")


memory._get_engine = lambda ctx: BrokenEngine()
r2 = memory.on_action("clear_l0", None)
check("引擎异常时给出可读错误而不是抛异常",
      "失败" in (r2 or {}).get("reply", "") and "引擎已关闭" in (r2 or {}).get("reply", ""),
      str(r2)[:120])

memory._get_engine = lambda ctx: None
r3 = memory.on_action("clear_l0", None)
check("引擎不可用时安全提示", "不可用" in (r3 or {}).get("reply", ""), str(r3)[:80])

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("清理 L0 对话缓存 验证全部通过。")
