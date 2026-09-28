# -*- coding: utf-8 -*-
"""上下文记忆库 v1.4.0 验证（独立临时数据目录，不动真实记忆）：
1) 设置项分组（高级选项 section 含 4 个容量/保留设置，普通设置保持平铺）+ 自然语言说明；
2) 查看各级缓存动作 / 命令：返回 L0/L1/L2/L3 报告；
3) 记忆初始化：二级确认（先警告不执行，确认动作才清空）；清空后 活跃/归档/冷存储/分类 全为 0。
"""
import os
import shutil
import sys

VERIFY_DIR = r"<project-root>\runtime\memory_engine_v14"
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = VERIFY_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False

import importlib.util
spec = importlib.util.spec_from_file_location("plugins.memory", r"<project-root>\plugins\memory.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

eng = memory_engine.get_engine()
eng.init()
eng.set_mode("readwrite")            # 引擎默认只读：测试写入必须显式放开（门禁语义）
eng.set_char_name("洛天依")

class FakeManager:
    def get_settings(self, name):
        return m.SETTINGS
class FakeCtx:
    config = __import__("core.config", fromlist=["x"])
    manager = FakeManager()
    def log(self, *a):
        print("[ctx]", *a)

ctx = FakeCtx()

print("=" * 72)
print("1) 设置分组：四个容量/保留设置进入「高级选项」，并带自然语言说明")
print("=" * 72)
schema = m.settings_schema()
flat_keys = [f.get("key") for f in schema if f.get("type") != "section"]
sec = [f for f in schema if f.get("type") == "section"]
print("  普通设置（平铺）: %s" % "、".join(flat_keys))
print("  分组: %s" % (sec[0]["label"] if sec else "无"))
sec_keys = [f["key"] for f in sec[0]["fields"]] if sec else []
print("  分组内设置: %s" % "、".join(sec_keys))
ok_group = (sec and sec[0]["key"] == "advanced" and set(sec_keys) == {"active_max", "archive_max", "raw_retention", "archive_retention"}
            and all(f.get("desc") for f in sec[0]["fields"]) and all(f.get("desc") for f in schema if f.get("type") != "section"))
print("  分组+说明完整: %s" % ("✓" if ok_group else "✗"))

print()
print("=" * 72)
print("2) 查看各级缓存（动作 + /memory cache 共用报告）")
print("=" * 72)
eng.ingest("洛天依：去年一起去海边旅行了。", fragment_id="v14_1", main_topic="旅行", participants=["洛天依"])
eng.l0.append("user", "今天想去海边吗", {})
eng.l0.append("assistant", "好呀，去年去的那次超开心。", {})
eng.search("去年海边旅行", role="洛天依")          # 填充 L1
report = m._cache_report(eng)
print(report)
ok_report = all(k in report for k in ("L0 会话缓存", "L1 检索热缓存", "L2 索引层", "L3 冷存储", "v14_1"))
print("  报告含 L0/L1/L2/L3 与样本: %s" % ("✓" if ok_report else "✗"))

print()
print("=" * 72)
print("3) 记忆初始化：二级确认（先警告不执行 → 确认动作才清空）")
print("=" * 72)
r1 = m.on_action("reset_memory", ctx)
print("  第一步返回确认标记: %s（confirm=%s，未删除）" % ("✓" if r1.get("confirm") == "reset_memory_do" else "✗", r1.get("confirm")))
print("  警告文本: %s" % (r1["reply"].splitlines()[0]))
still = eng.active.count() + eng.archive_db.count()
print("  第一步后记忆仍在: %s（活跃+归档=%d）" % ("✓" if still > 0 else "✗", still))
r2 = m.on_action("reset_memory_do", ctx)
print("  第二步执行: %s" % ("✓" if "完成" in r2["reply"] else "✗"))
cold_parts = eng.cold.partitions()
cats = eng.active.list_categories(limit=10) + eng.archive_db.list_categories(limit=10)
print("  清空后: 活跃=%d 归档=%d 冷分区=%d 分类=%d 向量=%d L1=%d L0=%d" % (
    eng.active.count(), eng.archive_db.count(), len(cold_parts), len(cats),
    eng.vector.size(), eng.l1.size(), eng.l0.size()))
ok_init = (eng.active.count() == 0 and eng.archive_db.count() == 0 and not cold_parts
           and not cats and eng.vector.size() == 0 and eng.l1.size() == 0 and eng.l0.size() == 0)
print("  全部清空: %s" % ("✓" if ok_init else "✗"))

print()
print("验证结论: %s" % ("✓ 全部通过" if (ok_group and ok_report and ok_init and still > 0) else "✗ 存在失败"))
eng.close()
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
