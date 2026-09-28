# -*- coding: utf-8 -*-
"""记忆管理页面后端验证（独立临时数据目录，不动真实记忆）：
1) 查看：按层级返回 L0/L1/L2(活跃/归档)/L3，主题渲染为当前角色名；
2) 关键词 / 日期筛选；
3) 新增（标准格式校验：拒绝非标准条目）；
4) 编辑 / 删除。
"""
import os
import shutil
import sys

VERIFY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime", "memory_engine_mv")
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
os.environ["MEMORY_ENGINE_DATA"] = VERIFY_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False

import web.server as srv
from memory_engine import service as mem_service

eng = srv._memory_engine()
eng.set_char_name("洛天依")
eng.set_mode("readwrite")
# 本脚本直接 import web.server，插件管理器会按用户真实设置注册门禁（可能是只读）；
# 这里显式登记为「插件已启用 + 完整权限」，等价于用户把上下文模式设为 readwrite
mem_service.register(eng, "readwrite")

# 管理接口现在统一要求客户端标识（防跨站）与危险操作令牌；脚本直调处理函数时手动构造
HEADERS = {"x-xllb-client": "verify-memview"}


def _token_for(op, target=""):
    """按新契约领取一次性确认令牌（服务端两步确认，不再依赖前端弹窗）。"""
    r = srv._api_confirm_prepare({"json": {"op": op, "target": target}, "headers": dict(HEADERS)})
    return __import__("json").loads(r[1].decode("utf-8")).get("token")

print("=" * 72)
print("1) 查看（按层级返回，主题渲染为当前角色名）")
print("=" * 72)
eng.ingest("洛天依：去年一起去海边旅行了。", fragment_id="mv_1", main_topic="旅行", participants=["洛天依"])
eng.l0.append("user", "今天想去海边吗", {})
r = srv._api_memory_view({"query": {}})
ok_view = r[0] == 200 and r[2].startswith("application/json")
body = __import__("json").loads(r[1].decode("utf-8"))
ok_layers = (len(body.get("active")) == 1
             and str(body["active"][0]["id"]).startswith("mv_1")
             and body["active"][0]["participants"] == ["洛天依"]
             and len(body.get("l0")) == 1 and len(body.get("cold")) >= 1
             and "l1" in body and "topics" in body)
print("  层级返回+渲染: %s（active=%d l0=%d cold=%d topics=%d）" % (
    "✓" if ok_view and ok_layers else "✗",
    len(body.get("active") or []), len(body.get("l0") or []), len(body.get("cold") or []), len(body.get("topics") or [])))

print()
print("=" * 72)
print("2) 关键词 / 日期筛选")
print("=" * 72)
r = srv._api_memory_view({"query": {"q": ["海边"]}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  关键词「海边」→ %d 条 %s" % (len(b.get("active") or []), "✓" if len(b.get("active")) == 1 else "✗"))
r = srv._api_memory_view({"query": {"q": ["奶茶"]}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  关键词「奶茶」→ %d 条 %s" % (len(b.get("active") or []), "✓" if not b.get("active") else "✗"))
import datetime
y = datetime.datetime.now().year
r = srv._api_memory_view({"query": {"date": [str(y)]}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  日期「%d」→ %d 条 %s" % (y, len(b.get("active") or []), "✓" if len(b.get("active")) == 1 else "✗"))
r = srv._api_memory_view({"query": {"date": ["1999"]}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  日期「1999」→ %d 条 %s" % (len(b.get("active") or []), "✓" if not b.get("active") else "✗"))

print()
print("=" * 72)
print("3) 新增（标准格式校验）")
print("=" * 72)
bad = srv._api_memory_view_add({"json": {"date": "2026/08/27", "participants": "洛天依", "content": "去公园散步"}})
print("  非标准日期拒绝: %s" % ("✓" if bad[0] == 400 else "✗"))
bad2 = srv._api_memory_view_add({"json": {"date": "2026-08-27", "participants": "", "content": "去公园散步"}})
print("  空参与者拒绝: %s" % ("✓" if bad2[0] == 400 else "✗"))
bad3 = srv._api_memory_view_add({"json": {"date": "2026-08-27", "participants": "洛天依", "content": "去公园散步", "topic": "八卦"}})
print("  非法主题拒绝: %s" % ("✓" if bad3[0] == 400 else "✗"))
ok = srv._api_memory_view_add({"json": {"date": "2026-08-27", "participants": "洛天依、洛天依（朋友）",
                                        "content": "今天下午三点约好一起去公园散步", "topic": "日常"}})
new_id = __import__("json").loads(ok[1].decode("utf-8")).get("id")
print("  标准条目新增: %s（id=%s）" % ("✓" if ok[0] == 200 and new_id else "✗", new_id))

print()
print("=" * 72)
print("4) 编辑 / 删除")
print("=" * 72)
upd = srv._api_memory_view_update({"json": {"id": new_id, "date": "2026-08-27", "participants": "洛天依",
                                            "content": "改成去爬山了", "topic": "旅行"}})
r = srv._api_memory_view({"query": {"q": ["爬山"]}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  编辑后内容更新: %s（查询「爬山」→ %d 条）" % ("✓" if upd[0] == 200 and len(b.get("active") or []) == 1 else "✗", len(b.get("active") or [])))
bad_upd = srv._api_memory_view_update({"json": {"id": new_id, "date": "昨天", "participants": "洛天依", "content": "x"}})
print("  编辑时非标准格式拒绝: %s" % ("✓" if bad_upd[0] == 400 else "✗"))
# 危险操作：无令牌必须被拒（不再依赖前端弹窗做确认）
dl_no_token = srv._api_memory_view_delete({"headers": dict(HEADERS), "json": {"id": new_id}})
dl_body = __import__("json").loads(dl_no_token[1].decode("utf-8"))
print("  删除无确认令牌被拒: %s（%s）" % (
    "✓" if dl_no_token[0] == 409 and dl_body.get("need_confirm") else "✗", dl_body.get("error")))
dl = srv._api_memory_view_delete({"headers": dict(HEADERS),
                                  "json": {"id": new_id, "token": _token_for("memory.delete", new_id)}})
r = srv._api_memory_view({"query": {}})
b = __import__("json").loads(r[1].decode("utf-8"))
print("  删除后: %s（active=%d 冷分区=%d，保留原有 mv_1）" % (
    "✓" if dl[0] == 200 and len(b.get("active") or []) == 1 else "✗",
    len(b.get("active") or []), len(b.get("cold") or [])))

print()
ok_all = (ok_view and ok_layers and len(b.get("active") or []) == 1
          and ok[0] == 200 and upd[0] == 200 and dl[0] == 200)
print("验证结论: %s" % ("✓ 全部通过" if ok_all else "✗ 存在失败"))
eng.close()
shutil.rmtree(VERIFY_DIR, ignore_errors=True)
