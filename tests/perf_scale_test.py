# -*- coding: utf-8 -*-
"""上下文管理插件压力测试（独立临时数据目录，测试完自动清理，不影响真实库）：
A) 灌入 5000 条记忆（写入吞吐）；
B) 检索延迟（20 条混合查询，LLM 复核/注入复核关闭）；
C) 每回合异步归档：200 个对话回合 record_turn 的“返回延迟”（应毫秒级，不受归档影响）
   + 后台归档吞吐（价值判断 + L2/L3 写入）；
D) 插件治理动作耗时：govern（季度归档/配额/聚合/压缩）+ tidy（去重）+ classify（分类索引）；
E) 内存 / 磁盘占用。
"""
import os
import shutil
import sys
import time

PERF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "memory_engine_perf")
shutil.rmtree(PERF_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = PERF_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False
cfg.INJECT_REVIEW = False
cfg.LLM_VALUE_CHECK = False
cfg.ARCHIVE_VALUE_CHECK = True

eng = memory_engine.get_engine()
eng.init()
eng.set_char_name("洛天依")
eng.set_mode("readwrite")

topics = ["日常", "美食", "旅行", "娱乐", "爱好", "学习", "社交", "回忆", "健康", "日程"]
acts = ["去公园散步", "喝奶茶", "爬山野餐", "打游戏", "学吉他", "复习考试",
        "朋友聚会", "看雪", "跑步锻炼", "上班开会", "做小笼包", "看电影"]
places = ["楼下咖啡馆", "海边", "游乐园", "天台", "图书馆", "火锅店"]
N = 5000

print("=" * 78)
print("A) 灌入 %d 条记忆（写入吞吐）" % N)
print("=" * 78)
import datetime as _dt
now = _dt.datetime.now()
t0 = time.perf_counter()
for i in range(N):
    q_off = i % 12
    y_off = q_off // 4
    q_idx = (q_off % 4) + 1
    y = now.year - y_off
    month = (q_idx - 1) * 3 + 1
    day = (i % 27) + 1
    ts = _dt.datetime(y, month, day, 8 + i % 10, 0).timestamp()
    text = ("洛天依：%d年%d月%d日我们去%s%s。\n洛天依（朋友）：%s，真不错。"
            % (y, month, day, places[i % 6], acts[i % 12], acts[i % 12]))
    eng.ingest(text, fragment_id="p%05d" % i, ts=ts, year=y,
               quarter="Q%d" % q_idx, main_topic=topics[i % 10],
               participants=["洛天依", "洛天依（朋友）"], chunks=False)
t1 = time.perf_counter()
print("  灌入 %d 条耗时 %.1fs（%.0f 条/s）\n" % (N, t1 - t0, N / (t1 - t0)))

print("=" * 78)
print("B) 检索延迟（20 条混合查询，复核全关）")
print("=" * 78)
queries = [
    "今天我们有什么安排吗", "星期三呢", "去年冬天看雪", "这个月生日",
    "前年海边旅行", "今年Q2学吉他", "周末爬山野餐", "去年夏天去哪玩了",
    "最近一次喝奶茶", "上周三晚上干嘛了", "上个月学的什么", "今天想喝奶茶",
    "去年跨年怎么过的", "今年去过游乐园吗", "前年冬天堆雪人", "这个月在学什么",
    "去年生日收到什么礼物", "上周有没有去公园", "下周三有什么安排", "去年看雪了吗",
]
for q in queries[:3]:
    eng.search(q, role="洛天依")          # 预热（向量惰性加载）
times = []
for q in queries:
    t0 = time.perf_counter()
    eng.search(q, role="洛天依")
    times.append((time.perf_counter() - t0) * 1000)
times.sort()
print("  平均 %.2f ms | 中位 %.2f ms | 最大 %.2f ms | 最小 %.2f ms\n" % (
    sum(times) / len(times), times[len(times) // 2], times[-1], times[0]))

print("=" * 78)
print("C) 每回合异步归档（200 回合 record_turn：返回延迟应毫秒级，归档后台执行）")
print("=" * 78)
turns = 200
t0 = time.perf_counter()
turn_times = []
for i in range(turns):
    s = time.perf_counter()
    eng.record_turn(
        "我们第%d次约好周末去爬山野餐" % i,
        "好呀，我带好吃的野餐！" if i % 2 else "没问题，准时到！",
        meta={"role": "洛天依", "archive": True})
    turn_times.append((time.perf_counter() - s) * 1000)
ret = time.perf_counter() - t0
t0 = time.perf_counter()
eng._drain_archives()                      # 等待后台归档（价值判断 + L2/L3 写入）
drain = time.perf_counter() - t0
tt = sorted(turn_times)
print("  200 回合返回总耗时 %.0f ms | 平均 %.2f ms | 最大 %.2f ms（归档异步，不阻塞）" % (
    ret * 1000, sum(tt) / len(tt), tt[-1]))
print("  后台归档处理 %d 回合耗时 %.1f s（%.0f 回合/s，含价值判断 + 写入）" % (
    turns, drain, turns / drain))
print("  归档后活跃片段：%d（= 灌入5000 + 归档%d）%s\n" % (
    eng.active.count(), turns, "✓" if eng.active.count() == N + turns else "✗"))

print("=" * 78)
print("D) 插件治理动作耗时（上下文整理：govern + tidy + classify）")
print("=" * 78)
t0 = time.perf_counter(); g = eng.govern(); tg = time.perf_counter() - t0
t0 = time.perf_counter(); t = eng.tidy_records(); tt2 = time.perf_counter() - t0
t0 = time.perf_counter(); c = eng.classify_records(); tc = time.perf_counter() - t0
print("  govern   %.1f s（归档迁移 %s 条 | 聚合 %s 组 | 原文压缩 %s 条）" % (
    tg, g.get("archive", {}).get("moved", 0),
    g.get("quotas", {}).get("quarter_aggregated", 0),
    g.get("quotas", {}).get("raw_compacted", 0)))
print("  tidy     %.1f s（合并 %s 对近似重复）" % (tt2, t.get("merged_pairs", 0)))
print("  classify %.1f s（主题/季度/参与者索引重建）" % tc)

print()
print("=" * 78)
print("E) 内存 / 磁盘占用")
print("=" * 78)
st = eng.status()
u = eng.usage()
print("  活跃 %d / 归档 %d | 向量驻留 %d/%d | L1缓存 %d 项" % (
    st["active_count"], st["archive_count"], st["vector_count"],
    cfg.VECTOR_MEMORY_MAX, st["cache_size"]))
print("  磁盘：活跃库 %.2f MB | 归档库 %.2f MB | L3冷存 %.2f MB | 合计 %.2f MB" % (
    u["active_db_bytes"] / 1048576, u["archive_db_bytes"] / 1048576,
    u["cold_bytes"] / 1048576, u["total_bytes"] / 1048576))

eng.close()
shutil.rmtree(PERF_DIR, ignore_errors=True)
print("\n压测临时目录已清理")
