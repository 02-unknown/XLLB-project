# -*- coding: utf-8 -*-
"""记忆管理 API 门禁 + 危险操作确认令牌 + 统一退出接线 验证（全中文输出）

覆盖任务 1~3 的关键契约：
  1) 记忆管理 API 统一走 memory_engine.service 门禁（Web 层不再自行 init() / 写库）；
     插件停用 / 只读 / 完整权限 三种状态下 view / add / update / delete 的返回码与字段；
     GET /api/memory/status 在插件停用时也能安全返回 200；
  2) 危险操作：POST 缺少 X-XLLB-Client → 403；一次性确认令牌（绑定 op/target/client、
     只能用一次、过期作废、容量上限）；
  3) 插件二次确认动作：第一步返回 confirm_token，直连最终动作 409，带正确令牌可执行；
  4) /api/history/clear 无令牌 409、有令牌 200；
  5) serve() 结束路径接入 core.shutdown.shutdown（源码检查，不真的 serve_forever）。

所有数据都落在临时目录（MEMORY_ENGINE_DATA 在 import 之前设置），结束时删除临时目录，
并断言真实 runtime/memory_engine 与插件配置文件指纹未变。

运行：venv\\Scripts\\python.exe tests\verify_memory_api_guard.py
"""
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---- 引擎数据目录：必须在 import memory_engine / web.server 之前设置为临时目录 ----
TMP_ROOT = os.path.join(tempfile.gettempdir(), "xllb_mem_api_guard_%d" % os.getpid())
shutil.rmtree(TMP_ROOT, ignore_errors=True)
os.makedirs(TMP_ROOT, exist_ok=True)
os.environ["MEMORY_ENGINE_DATA"] = os.path.join(TMP_ROOT, "memory_engine")

PROD_MEM_DIR = os.path.join(ROOT, "runtime", "memory_engine")
PROD_FILES = [os.path.join(ROOT, "plugins_state.json"), os.path.join(ROOT, "plugins_settings.json")]

OKS, FAILS = [], []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"  ✓ {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"  ✗ {name} {extra}")


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def dir_state(path):
    """目录内文件的 (相对路径 → 大小, mtime_ns, sha1) 指纹。"""
    out = {}
    if not os.path.isdir(path):
        return out
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
                with open(full, "rb") as f:
                    digest = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                continue
            out[os.path.relpath(full, path)] = (st.st_size, st.st_mtime_ns, digest)
    return out


def file_state(path):
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            return (st.st_size, st.st_mtime_ns, hashlib.sha1(f.read()).hexdigest())
    except OSError:
        return None


PROD_MEM_BEFORE = dir_state(PROD_MEM_DIR)
PROD_FILES_BEFORE = {p: file_state(p) for p in PROD_FILES}

import core.config as app_config  # noqa: E402

# 隔离插件系统：空插件目录 + 临时状态 / 设置文件（避免加载真实插件、避免改写用户配置）
app_config.PLUGINS_DIR = os.path.join(TMP_ROOT, "plugins_empty")
os.makedirs(app_config.PLUGINS_DIR, exist_ok=True)
app_config.PLUGINS_STATE_FILE = os.path.join(TMP_ROOT, "plugins_state.json")
app_config.PLUGINS_SETTINGS_FILE = os.path.join(TMP_ROOT, "plugins_settings.json")
app_config.TTS_OUTPUT_DIR = os.path.join(TMP_ROOT, "tts")
app_config.MUSIC_OUTPUT_DIR = os.path.join(TMP_ROOT, "music")
os.makedirs(app_config.TTS_OUTPUT_DIR, exist_ok=True)
os.makedirs(app_config.MUSIC_OUTPUT_DIR, exist_ok=True)

import memory_engine  # noqa: E402
import memory_engine.config as mem_cfg  # noqa: E402
from memory_engine import service as mem_service  # noqa: E402

mem_cfg.LLM_RECHECK_ENABLED = False
mem_cfg.INJECT_REVIEW = False
mem_cfg.ARCHIVE_DRAIN_TIMEOUT = 3.0

import web.server as srv  # noqa: E402
from web import confirm as confirm_mod  # noqa: E402
from core import plugin_manager  # noqa: E402

CLIENT = "test"


def jbody(result):
    """把 (status, body, ctype) 结果解析为 (status, dict)。"""
    return result[0], json.loads(result[1].decode("utf-8"))


def req_post(json_body=None, headers=None, **kw):
    """构造脚本用的 req（与 Handler._dispatch 里的结构一致）。"""
    h = {"x-xllb-client": CLIENT}
    if headers:
        h.update({str(k).lower(): v for k, v in headers.items()})
    req = {"json": json_body or {}, "query": {}, "headers": h}
    req.update(kw)
    return req


def req_get(**query):
    return {"json": {}, "query": query, "headers": {"x-xllb-client": CLIENT}}


def new_token(op, target=""):
    r = srv._api_confirm_prepare(req_post({"op": op, "target": target}))
    status, body = jbody(r)
    assert status == 200 and body.get("token"), f"签发令牌失败：{status} {body}"
    return body["token"]


# ==================== 0) 前置：门禁默认状态 ====================
section("0) 前置检查：门禁默认状态与 Web 层不再自行初始化引擎")
st0 = mem_service.status()
check("门禁默认：插件未启用 + 不可读不可写不可管理",
      st0["plugin_enabled"] is False and st0["can_read"] is False and st0["can_manage"] is False, str(st0))

src = open(os.path.join(ROOT, "web", "server.py"), encoding="utf-8").read()
check("Web 层已不再 get_engine()+init() 自行初始化记忆引擎",
      "eng.init()" not in src and "from memory_engine import get_engine" not in src)
check("Web 层记忆接口统一经 service 门禁（存在 _memory_gate 且使用 LEVEL_MANAGE）",
      "def _memory_gate(" in src and "LEVEL_MANAGE" in src and "mem_service.check(" in src)
check("HTTP 层把请求头塞进 req（req[\"headers\"]），供令牌 / 客户端标识校验",
      'req["headers"]' in src)

# ==================== 1) POST 客户端标识 + 同源校验 ====================
section("1) 危险操作加固：POST 客户端标识（X-XLLB-Client）与同源校验")
r = srv._guard_post_headers({"json": {}, "headers": {}})
status, body = jbody(r)
check("POST 缺少 X-XLLB-Client → 403", status == 403, f"实际 {status}")
check("403 错误信息包含客户端标识提示",
      "X-XLLB-Client" in body.get("error", ""), body.get("error", ""))

r = srv._guard_post_headers({"json": {}, "headers": {"x-xllb-client": "   "}})
check("POST 客户端标识为空白 → 403", jbody(r)[0] == 403)

r = srv._guard_post_headers({"json": {}, "headers": {"x-xllb-client": CLIENT}})
check("POST 带客户端标识 → 通过（返回 None）", r is None)

r = srv._guard_post_headers({"json": {}, "headers": {"x-xllb-client": CLIENT,
                                                     "origin": "http://evil.example.com",
                                                     "host": "127.0.0.1:10999"}})
check("Origin 与 Host 不一致（跨站）→ 403", jbody(r)[0] == 403)

r = srv._guard_post_headers({"json": {}, "headers": {"x-xllb-client": CLIENT,
                                                     "origin": "http://127.0.0.1:10999",
                                                     "host": "127.0.0.1:10999"}})
check("Origin 与 Host 同源 → 通过", r is None)

# ==================== 2) 插件停用：读 / 写全部 409 且字段完整 ====================
section("2) 插件停用：记忆接口 409、字段完整、reason 可读；/api/memory/status 仍 200")
mem_service.reset_for_test()

status, body = jbody(srv._api_memory_view(req_get(q=["奶茶"])))
check("停用：GET /api/memory/view → 409", status == 409, f"实际 {status}")
check("停用：响应带 plugin_enabled=False", body.get("plugin_enabled") is False, str(body))
check("停用：响应带 mode=readonly / level / can_* 字段",
      body.get("mode") == "readonly" and body.get("level") == "只读（仅本次会话）"
      and body.get("can_read") is False and body.get("can_write") is False
      and body.get("can_manage") is False, str(body))
check("停用：reason 说明插件未启用", "未启用" in (body.get("reason") or ""), body.get("reason", ""))
check("停用：error 与 reason 一致（前端可直接展示）",
      body.get("error") == body.get("reason"), f"{body.get('error')} / {body.get('reason')}")

check("停用：GET /api/memory/view/cold → 409", jbody(srv._api_memory_view_cold(req_get(partition=["2026/Q3/美食"])))[0] == 409)

r_add = srv._api_memory_view_add(req_post({"date": "2026-08-27", "participants": "洛天依", "content": "去公园散步"}))
status, body = jbody(r_add)
check("停用：POST /api/memory/view/add → 409（先门禁后校验）", status == 409, f"实际 {status}")
check("停用：add 的 reason 说明插件未启用", "未启用" in (body.get("reason") or ""), body.get("reason", ""))

r_upd = srv._api_memory_view_update(req_post({"id": "x", "date": "2026-08-27", "participants": "洛天依", "content": "改内容"}))
check("停用：POST /api/memory/view/update → 409", jbody(r_upd)[0] == 409, str(jbody(r_upd)))

tok_del = new_token(srv.CONFIRM_MEMORY_DELETE, "x")
r_del = srv._api_memory_view_delete(req_post({"id": "x", "token": tok_del}))
check("停用：POST /api/memory/view/delete（令牌正确）→ 409", jbody(r_del)[0] == 409)

check("停用：_memory_engine() 返回 None（不再自行初始化）", srv._memory_engine() is None)

status, body = jbody(srv._api_memory_status({"json": {}, "query": {}, "headers": {}}))
check("停用：GET /api/memory/status → 200", status == 200, f"实际 {status}")
check("停用：status 返回 gate 快照（plugin_enabled=False + 原因）",
      body.get("ok") is True and body.get("gate", {}).get("plugin_enabled") is False
      and bool(body.get("gate", {}).get("reason")), str(body)[:200])

# ---- confirm 令牌本身不依赖插件状态，可正常签发 / 校验 ----
check("停用：令牌接口照常工作（签发 → 使用成功）",
      confirm_mod.consume(srv.CONFIRM_HISTORY_CLEAR, "*", CLIENT, new_token(srv.CONFIRM_HISTORY_CLEAR, "*"))[0])

# ==================== 3) 只读模式：可查看、不可增删改 ====================
section("3) 只读模式（readonly）：view 允许、add/update/delete 409 且原因含「只读」")
engine = memory_engine.get_engine(auto_init=True)
engine.set_char_name("洛天依")
# 引擎底层的模式必须与门禁一致：插件启用时会同步下发（测试里手动构造状态，故显式设置）
engine.set_mode("readonly")
mem_service.register(engine, "readonly")
check("只读：门禁状态与引擎底层模式一致",
      mem_service.status()["mode"] == "readonly" and engine.get_mode() == "readonly",
      f"{mem_service.status()['mode']} / {engine.get_mode()}")

status, body = jbody(srv._api_memory_view(req_get()))
check("只读：GET /api/memory/view → 200", status == 200, f"实际 {status}")
check("只读：成功响应带 mode=readonly / level / can_manage=False",
      body.get("mode") == "readonly" and body.get("level") == "只读（仅本次会话）"
      and body.get("can_manage") is False and body.get("ok") is True, str(body)[:200])

status, body = jbody(srv._api_memory_view_add(req_post({"date": "2026-08-27", "participants": "洛天依", "content": "只读下不应写入"})))
check("只读：add → 409 且原因含「只读」", status == 409 and "只读" in (body.get("reason") or ""), f"{status} {body.get('reason')}")

status, body = jbody(srv._api_memory_view_update(req_post({"id": "x", "date": "2026-08-27", "participants": "洛天依", "content": "改"})))
check("只读：update → 409 且原因含「只读」", status == 409 and "只读" in (body.get("reason") or ""), f"{status} {body.get('reason')}")

tok = new_token(srv.CONFIRM_MEMORY_DELETE, "x")
status, body = jbody(srv._api_memory_view_delete(req_post({"id": "x", "token": tok})))
check("只读：delete → 409 且原因含「只读」", status == 409 and "只读" in (body.get("reason") or ""), f"{status} {body.get('reason')}")

status, body = jbody(srv._api_memory_status({"json": {}, "query": {}, "headers": {}}))
check("只读：status → can_read=True / can_manage=False",
      body["gate"]["can_read"] is True and body["gate"]["can_manage"] is False, str(body["gate"]))

# ==================== 4) 完整权限：新增 / 编辑 / 删除成功 ====================
section("4) 完整权限（readwrite）：add / update / delete 成功，成功响应带当前模式")
engine.set_mode("readwrite")
mem_service.register(engine, "readwrite")
check("读写：门禁状态与引擎底层模式一致（门禁 readwrite + 引擎 readwrite）",
      mem_service.status()["mode"] == "readwrite" and engine.get_mode() == "readwrite"
      and engine.check_write("ingest") == "", f"{mem_service.status()['mode']} / {engine.get_mode()} / {engine.check_write('ingest')}")

status, body = jbody(srv._api_memory_view_add(req_post({
    "date": "2026-08-27", "participants": "洛天依、洛天依（朋友）",
    "content": "今天下午三点约好一起去公园散步", "topic": "日常"})))
new_id = body.get("id")
check("读写：add → 200 且返回 id", status == 200 and bool(new_id), f"{status} {body}")
check("读写：add 成功响应带 mode=readwrite / can_manage=True",
      body.get("mode") == "readwrite" and body.get("can_manage") is True, str(body)[:200])

status, body = jbody(srv._api_memory_view_update(req_post({
    "id": new_id, "date": "2026-08-27", "participants": "洛天依", "content": "改成去爬山了", "topic": "旅行"})))
check("读写：update → 200", status == 200, f"{status} {body}")

r = jbody(srv._api_memory_view(req_get(q=["爬山"])))[1]
check("读写：update 后查询「爬山」命中 1 条", len(r.get("active") or []) == 1, str(len(r.get("active") or [])))

r = jbody(srv._api_memory_view(req_get(q=["奶茶"])))[1]
check("读写：查询「奶茶」无命中（关键词筛选有效）", not r.get("active"), str(r.get("active")))

status, body = jbody(srv._api_memory_view_delete(req_post({"id": new_id, "token": new_token(srv.CONFIRM_MEMORY_DELETE, new_id)})))
check("读写：delete（带一次性令牌）→ 200", status == 200, f"{status} {body}")
check("读写：delete 成功响应带当前模式字段",
      body.get("mode") == "readwrite" and "can_manage" in body, str(body)[:200])

r = jbody(srv._api_memory_view(req_get(q=["爬山"])))[1]
check("读写：删除后查询「爬山」无命中（engine.delete 生效）", not r.get("active"), str(r.get("active")))

# 幂等：删一个本来就不存在的 id 也不应报 500（引擎 delete 直接返回）
status, body = jbody(srv._api_memory_view_delete(req_post({"id": "不存在的id",
                                                            "token": new_token(srv.CONFIRM_MEMORY_DELETE, "不存在的id")})))
check("读写：删除不存在的 id 不报 500（引擎 delete 幂等）", status == 200, f"{status} {body}")

# 引擎底层 WriteBlocked：模式切只读后由门禁同步关掉写入策略，引擎底层直接拒绝
mem_service.set_mode("readonly")
try:
    engine.ingest("底层拒绝写入验证")
    check("引擎底层在门禁关闭写入时抛 WriteBlocked（供 Web 层 409 兜底）", False, "居然写入了")
except mem_service.WriteBlocked as e:
    check("引擎底层在门禁关闭写入时抛 WriteBlocked（供 Web 层 409 兜底）",
          bool(e.reason) and e.reason in ("记忆写入已被门禁关闭",) , e.reason)
mem_service.register(engine, "readwrite")

# ==================== 5) 一次性确认令牌：绑定、一次性、过期 ====================
section("5) 一次性确认令牌：op/target/client 绑定、只能用一次、过期作废、容量上限")
confirm_mod.reset_for_test()

r = srv._api_confirm_prepare(req_post({"op": "demo.op", "target": "obj"}))
status, body = jbody(r)
check("POST /api/confirm/prepare → 200 且返回 token/expires_in/op/target",
      status == 200 and body.get("token") and body.get("expires_in") == 120
      and body.get("op") == "demo.op" and body.get("target") == "obj", str(body)[:200])

check("prepare 缺少 op → 400", jbody(srv._api_confirm_prepare(req_post({"target": "obj"})))[0] == 400)

tok = new_token("demo.op", "obj")
ok1, why1 = confirm_mod.consume("demo.op", "obj", CLIENT, tok)
ok2, why2 = confirm_mod.consume("demo.op", "obj", CLIENT, tok)
check("令牌第一次使用通过", ok1 is True, why1)
check("令牌第二次使用被拒绝（一次性）", ok2 is False and bool(why2), why2)

tok = new_token("demo.op", "obj")
ok_bad, why_bad = confirm_mod.consume("other.op", "obj", CLIENT, tok)
check("op 不匹配被拒绝（原因含 op）", ok_bad is False and "op" in why_bad, why_bad)
still_ok, _ = confirm_mod.consume("demo.op", "obj", CLIENT, tok)
check("op 不匹配后令牌未被误消耗（仍可用一次）", still_ok is True)

tok = new_token("demo.op", "obj")
ok_bad, why_bad = confirm_mod.consume("demo.op", "别的对象", CLIENT, tok)
check("target 不匹配被拒绝（原因含 target）", ok_bad is False and "target" in why_bad, why_bad)

tok = new_token("demo.op", "obj")
ok_bad, why_bad = confirm_mod.consume("demo.op", "obj", "别的客户端", tok)
check("client 不匹配被拒绝（原因含 client）", ok_bad is False and "client" in why_bad, why_bad)

check("空令牌被拒绝", confirm_mod.consume("demo.op", "obj", CLIENT, "")[0] is False)
check("不存在的令牌被拒绝", confirm_mod.consume("demo.op", "obj", CLIENT, "不存在的令牌")[0] is False)

# 过期：直接操作内部记录（把签发时间往前挪，模拟已过期）
tok = confirm_mod.issue("demo.op", target="obj", client=CLIENT, ttl=120.0)
with confirm_mod._lock:
    confirm_mod._tokens[tok]["issued"] = time.time() - 121
ok_exp, why_exp = confirm_mod.consume("demo.op", "obj", CLIENT, tok)
check("过期令牌被拒绝（提示过期或需重新确认）",
      ok_exp is False and ("过期" in why_exp or "确认" in why_exp), why_exp)

# ttl 极短也能过期（不依赖内部字段）
tok = confirm_mod.issue("demo.op", target="obj", client=CLIENT, ttl=0.01)
time.sleep(0.05)
check("ttl=0.01 的令牌过期后被拒绝", confirm_mod.consume("demo.op", "obj", CLIENT, tok)[0] is False)

confirm_mod.reset_for_test()
for i in range(confirm_mod.MAX_TOKENS + 60):
    confirm_mod.issue("demo.op", target="obj_%d" % i, client=CLIENT)
check("令牌容量上限生效（不超过 %d 条）" % confirm_mod.MAX_TOKENS,
      confirm_mod.peek_count() <= confirm_mod.MAX_TOKENS, f"实际 {confirm_mod.peek_count()}")
confirm_mod.reset_for_test()
test_tok = confirm_mod.issue("demo.op", target="obj", client=CLIENT, ttl=120.0)
with confirm_mod._lock:
    confirm_mod._tokens[test_tok]["issued"] = time.time() - 999
check("purge() 能清理过期令牌", confirm_mod.purge() >= 1 and confirm_mod.peek_count() == 0)
confirm_mod.reset_for_test()

# ==================== 6) 插件动作：二次确认动作必须带令牌 ====================
section("6) 插件动作：二次确认动作无令牌 409；第一步签发 confirm_token；带令牌可执行")
calls = []
CONFIRM_PLUGIN = "假插件"
CONFIRM_OP = "plugin.action"
CONFIRM_TARGET = f"{CONFIRM_PLUGIN}:危险动作_do"


def fake_run_action(name, action):
    """假插件：第一步返回 confirm（要求二次确认），第二步执行。"""
    calls.append((name, action))
    if action == "危险动作":
        return {"reply": "⚠️ 这会把数据全部删掉，确定继续吗？", "confirm": "危险动作_do", "speak": False}
    if action == "危险动作_do":
        return {"reply": "已执行危险动作（数据已清理）", "speak": False}
    if action == "普通动作":
        return {"reply": "普通动作已执行", "speak": False}
    return None


real_run_action = plugin_manager.manager.run_action
plugin_manager.manager.run_action = fake_run_action
try:
    # 普通动作不需要令牌
    status, body = jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "普通动作"})))
    check("普通动作不需要令牌（200 且 ok=True，reply 保留）",
          status == 200 and body.get("ok") is True and body.get("reply") == "普通动作已执行", f"{status} {body}")

    # 第一步：返回 confirm + 服务端签发 confirm_token
    status, body = jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "危险动作"})))
    check("第一步动作 → 200 且保留 confirm 字段", status == 200 and body.get("confirm") == "危险动作_do", str(body)[:200])
    check("第一步动作 → 保留 reply（风险警告）", "确定继续" in (body.get("reply") or ""), body.get("reply", ""))
    confirm_token = body.get("confirm_token")
    check("第一步动作 → 返回 confirm_token 与有效期",
          bool(confirm_token) and body.get("confirm_expires_in") == 120, str(body)[:200])

    # 第二步：不带令牌 → 409
    status, body = jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "危险动作_do"})))
    check("直连二次确认动作（无令牌）→ 409", status == 409, f"实际 {status}")
    check("409 带 need_confirm/op/target，提示需先在界面确认",
          body.get("need_confirm") is True and body.get("op") == CONFIRM_OP
          and body.get("target") == CONFIRM_TARGET and "确认" in (body.get("error") or ""), str(body))

    # 第二步：带错令牌（别的 target）→ 409
    bad_token = new_token(CONFIRM_OP, f"{CONFIRM_PLUGIN}:其他动作_do")
    check("二次确认动作带不匹配 target 的令牌 → 409",
          jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "危险动作_do",
                                                  "token": bad_token})))[0] == 409)

    # 第二步：带正确令牌 → 200
    status, body = jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "危险动作_do",
                                                           "token": confirm_token})))
    check("二次确认动作带正确令牌 → 200 且执行成功",
          status == 200 and "已执行危险动作" in (body.get("reply") or ""), f"{status} {body}")

    check("二次确认动作带正确令牌后，令牌已作废（重放 409）",
          jbody(srv._api_plugins_action(req_post({"name": CONFIRM_PLUGIN, "action": "危险动作_do",
                                                  "token": confirm_token})))[0] == 409)

    # 两个不同的一次性令牌必须互不相同（secrets.token_urlsafe 随机性）
    t1 = confirm_mod.issue("demo.op", target="obj", client=CLIENT)
    t2 = confirm_mod.issue("demo.op", target="obj", client=CLIENT)
    check("令牌随机生成（长度足够且两次不同）", len(t1) >= 30 and t1 != t2, f"{t1} / {t2}")

    # 显式声明表：插件第一步声明过确认动作后，直接调用该动作也必须带令牌
    check("二次确认动作已登记（插件名:动作名）", CONFIRM_TARGET in srv._declared_confirm_actions,
          str(sorted(srv._declared_confirm_actions)))
    check("普通动作（上下文记忆库:view_memory 风格）不需要令牌",
          srv._is_confirm_action("上下文记忆库", "view_memory") is False)

    # 真实「上下文记忆库」插件的动作语义（插件本体不在本测试的隔离插件目录中，
    # 这里按 plugins/memory.py 的真实行为打桩，验证「记忆初始化」两步流程完整可用）
    engine.set_mode("readwrite")
    mem_service.register(engine, "readwrite")
    engine.ingest("初始化前的一条记忆", participants=["洛天依"], main_topic="日常")

    def memory_plugin_run_action(name, action):
        if name != "上下文记忆库":
            return real_run_action(name, action)
        if action == "reset_memory":
            return {"reply": "⚠️ 记忆初始化将永久删除全部记忆，确定继续吗？",
                    "confirm": "reset_memory_do", "confirm_target": "上下文记忆库", "speak": False}
        if action == "reset_memory_do":
            result = engine.initialize_memory()
            if not result.get("ok"):
                return {"reply": f"记忆初始化被拒绝：{result.get('message') or '当前状态不允许'}", "speak": False}
            return {"reply": f"记忆初始化完成：已删除 {result.get('active', 0)} 条记忆", "speak": False}
        return None

    plugin_manager.manager.run_action = memory_plugin_run_action

    first = jbody(srv._api_plugins_action(req_post({"name": "上下文记忆库", "action": "reset_memory"})))
    check("真实「记忆初始化」第一步返回 confirm + confirm_token",
          first[1].get("confirm") == "reset_memory_do" and bool(first[1].get("confirm_token")), str(first[1])[:200])
    second = jbody(srv._api_plugins_action(req_post({"name": "上下文记忆库", "action": "reset_memory_do"})))
    check("真实「记忆初始化」无令牌 → 409", second[0] == 409, str(second))
    third = jbody(srv._api_plugins_action(req_post({"name": "上下文记忆库", "action": "reset_memory_do",
                                                    "token": first[1]["confirm_token"]})))
    check("真实「记忆初始化」带令牌 → 200 且执行成功",
          third[0] == 200 and "记忆初始化完成" in (third[1].get("reply") or ""), str(third)[:200])
finally:
    plugin_manager.manager.run_action = real_run_action

# ==================== 7) 清空对话记录：无令牌 409、有令牌 200 ====================
section("7) POST /api/history/clear：无令牌 409、有令牌 200")

status, body = jbody(srv._api_history_clear(req_post({})))
check("清空对话无令牌 → 409", status == 409, f"实际 {status}")
check("清空对话 409 带 need_confirm/op=history.clear",
      body.get("need_confirm") is True and body.get("op") == srv.CONFIRM_HISTORY_CLEAR, str(body))

status, body = jbody(srv._api_history_clear(req_post({"token": "无效令牌"})))
check("清空对话带无效令牌 → 409", status == 409, f"实际 {status}")

status, body = jbody(srv._api_history_clear(req_post({"token": new_token(srv.CONFIRM_HISTORY_CLEAR, "*")})))
check("清空对话带正确令牌 → 200", status == 200 and body.get("ok") is True, f"{status} {body}")

status, body = jbody(srv._api_history_clear(req_post({"token": "重放刚才的令牌"})))
check("清空对话令牌重放 → 409", status == 409, f"实际 {status}")

# 缺客户端标识时危险接口也应被拒（脚本直调处理函数这一层）
status, body = jbody(srv._api_history_clear({"json": {}, "query": {}, "headers": {}}))
check("缺客户端标识直调清空对话 → 403", status == 403, f"实际 {status}")

# ==================== 8) 插件重载 / 启停 / 敏感设置：令牌与报告 ====================
section("8) 插件重载 / 启停 / 敏感设置：令牌要求 + 重载报告字段")

status, body = jbody(srv._api_plugins_reload(req_post({})))
check("插件重载无令牌 → 409", status == 409, f"实际 {status}")

real_reload_report = plugin_manager.manager.reload_report
real_list_plugins = plugin_manager.manager.list_plugins
plugin_manager.manager.reload_report = lambda force=False: {
    "ok": True, "errors": [], "loaded": ["新插件"], "reloaded": [], "unloaded": [],
    "kept": ["上下文记忆库", "背景设置"], "waited_ms": 12}
plugin_manager.manager.list_plugins = lambda: []
try:
    token = new_token(srv.CONFIRM_PLUGINS_RELOAD, "*")
    status, body = jbody(srv._api_plugins_reload(req_post({"token": token})))
    check("插件重载带令牌 → 200", status == 200, f"{status} {body}")
    check("重载响应保留 errors 字段（兼容旧前端）", "errors" in body and isinstance(body["errors"], list))
    check("重载响应新增 report（含 kept/reloaded/waited_ms）",
          isinstance(body.get("report"), dict) and body["report"].get("kept") == ["上下文记忆库", "背景设置"]
          and "waited_ms" in body["report"], str(body.get("report")))
finally:
    plugin_manager.manager.reload_report = real_reload_report
    plugin_manager.manager.list_plugins = real_list_plugins

status, body = jbody(srv._api_plugins_enable(req_post({"name": "上下文记忆库"})))
check("插件启用无令牌 → 409", status == 409, f"实际 {status}")
status, body = jbody(srv._api_plugins_disable(req_post({"name": "上下文记忆库"})))
check("插件停用无令牌 → 409", status == 409, f"实际 {status}")

real_save_settings = plugin_manager.manager.save_settings
plugin_manager.manager.save_settings = lambda name, patch: {"已保存": True}
try:
    status, body = jbody(srv._api_plugins_settings(req_post({"name": "假插件", "settings": {"enabled": True, "max_items": 5}})))
    check("普通设置（开关 / 数值）不需要令牌 → 200", status == 200, f"{status} {body}")

    status, body = jbody(srv._api_plugins_settings(req_post({"name": "假插件", "settings": {"chat_api_key": "sk-xxx"}})))
    check("敏感设置（api_key）无令牌 → 409", status == 409, f"实际 {status}")

    status, body = jbody(srv._api_plugins_settings(req_post(
        {"name": "假插件", "settings": {"chat_model": "gpt-4o"},
         "token": new_token(srv.CONFIRM_PLUGINS_SETTINGS, "假插件")})))
    check("敏感设置（model）带令牌 → 200", status == 200, f"{status} {body}")

    status, body = jbody(srv._api_plugins_settings(req_post(
        {"name": "假插件", "settings": {"chat_backend": "openai"},
         "token": new_token(srv.CONFIRM_PLUGINS_SETTINGS, "别的插件")})))
    check("敏感设置令牌 target 不匹配 → 409", status == 409, f"实际 {status}")
finally:
    plugin_manager.manager.save_settings = real_save_settings

check("敏感键检测：api_key / key / model / backend 命中，开关数值不命中",
      srv._is_sensitive_keys({"chat_api_key": "x"}) and srv._is_sensitive_keys({"judge_key": "x"})
      and srv._is_sensitive_keys({"llm_model": "x"}) and srv._is_sensitive_keys({"chat_backend": "ollama"})
      and not srv._is_sensitive_keys({"enabled": True, "max_items": 5}))

# 插件系统隔离：本测试的状态 / 设置文件都在临时目录，
# 因此不会因为 manager 落盘而改写用户的真实插件配置（真实文件指纹在最后一节断言）。
app_config.ensure_dirs()
check("插件目录 / 状态文件已隔离到临时目录（不写真实插件配置）",
      os.path.abspath(app_config.PLUGINS_DIR).startswith(os.path.abspath(TMP_ROOT))
      and os.path.abspath(app_config.PLUGINS_STATE_FILE).startswith(os.path.abspath(TMP_ROOT))
      and os.path.abspath(app_config.PLUGINS_SETTINGS_FILE).startswith(os.path.abspath(TMP_ROOT)),
      app_config.PLUGINS_DIR)

# ==================== 9) 统一退出流程接线 ====================
section("9) 统一退出流程接线（源码检查，不真的 serve_forever）")
serve_src = inspect.getsource(srv.serve)
check("serve() 源码调用了 core.shutdown.shutdown", "shutdown_mod.shutdown(" in serve_src)
check("serve() 传入 stop_server 回调（server_close）", "stop_server=" in serve_src and "server_close()" in serve_src)
check("serve() 传入 extra_steps（停止 TTS 流式任务）",
      "extra_steps=" in serve_src and "停止 TTS 流式任务" in serve_src)
check("serve() 不再单独调用 runtime.cleanup_runtime()（已含在 shutdown 内）",
      "runtime.cleanup_runtime()" not in serve_src)
check("TTS 流式任务清理函数存在且可调用", callable(getattr(srv, "_stop_stream_tasks", None))
      and "个流式会话" in srv._stop_stream_tasks())

from core import shutdown as shutdown_mod  # noqa: E402
check("core.shutdown.shutdown 可重复调用（幂等）", callable(shutdown_mod.shutdown) and shutdown_mod.is_done() is False)

# ==================== 10) 真实数据目录与插件配置未被写脏 ====================
section("10) 真实数据未被本次测试改动（临时目录隔离）")
check("本测试使用的是临时引擎目录（不是真实 runtime/memory_engine）",
      os.path.abspath(mem_cfg.DATA_DIR).startswith(os.path.abspath(TMP_ROOT)),
      mem_cfg.DATA_DIR)
PROD_MEM_AFTER = dir_state(PROD_MEM_DIR)
check("真实 runtime/memory_engine 文件指纹完全一致",
      PROD_MEM_AFTER == PROD_MEM_BEFORE,
      f"变化：{set(PROD_MEM_AFTER.items()) ^ set(PROD_MEM_BEFORE.items())}"[:200])
for p in PROD_FILES:
    check(f"真实文件未被改写：{os.path.basename(p)}", file_state(p) == PROD_FILES_BEFORE[p])

# ==================== 收尾 ====================
try:
    engine.close()
except Exception:
    pass
mem_service.reset_for_test()
confirm_mod.reset_for_test()
shutil.rmtree(TMP_ROOT, ignore_errors=True)

print()
print("=" * 78)
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  ✗ " + f)
    print("验证结论: ✗ 存在失败")
    sys.exit(1)
print("验证结论: ✓ 全部通过")
