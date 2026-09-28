# -*- coding: utf-8 -*-
"""「角色与语音」合并标签 验证（写入后恢复原配置，不影响用户设置）：

1) 数据接口：全部角色预设 + 当前勾选 + 声线映射 + 可选声线清单；
2) 可用性握手：多人对话插件通过 available() 声明「可用 / 不可用」——
   插件停用 → 不可用（单人输出）；启用但勾选不足 2 个 → 不可用；勾选满 2 个 → 可用；
3) 保存接口的安全约束：缺客户端标识 403、缺确认令牌 409、插件停用 409、带令牌成功；
4) 插件动作 manage_characters 指向设置页「角色与语音」（不再打开独立页面）。
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine.config as cfg
cfg.LLM_RECHECK_ENABLED = False

import web.server as srv
from core import plugin_manager

mgr = plugin_manager.manager
PLUGIN = "多人对话"
orig = dict(mgr.get_settings(PLUGIN))
was_enabled = mgr.is_enabled(PLUGIN)
OKS, FAILS = [], []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def body(resp):
    return json.loads(resp[1].decode("utf-8"))


HEADERS = {"x-xllb-client": "verify-roles"}


def token_for(op, target=""):
    r = srv._api_confirm_prepare({"json": {"op": op, "target": target}, "headers": dict(HEADERS)})
    return body(r).get("token")


def post(payload, headers=None):
    return srv._api_characters_manage_post({"json": payload, "headers": dict(headers or HEADERS)})


print("=" * 74)
print("1) 数据接口：角色预设 / 勾选 / 声线 / 可用性握手")
print("=" * 74)
mgr.enable(PLUGIN)
d = body(srv._api_characters_manage_get({}))
names = [p["name"] for p in d.get("presets", [])]
print("  角色预设：%s" % "、".join(names))
check("返回全部角色预设（含描述字段）",
      len(names) >= 2 and all("description" in p for p in d.get("presets", [])))
check("返回勾选 / 声线映射 / 可选声线清单",
      "selected" in d and "voices_map" in d and "voices" in d and "main_character" in d)
check("返回可用性握手字段（available / availability / min_roles / can_edit）",
      all(k in d for k in ("available", "availability", "min_roles", "can_edit", "plugin_enabled")))
print("  插件可用性：available=%s reason=%s" % (d.get("available"), d.get("reason")))
check("可用性信息来自插件自身声明（availability.enabled=True）",
      d["availability"].get("enabled") is True and "selected" in d["availability"])

print()
print("=" * 74)
print("2) 可用性握手：勾选不足 → 不可用；勾选满 2 个 → 可用")
print("=" * 74)
mgr.save_settings(PLUGIN, {"selected_characters": "", "character_voices": "{}",
                           "slot1_character": "", "slot2_character": "", "slot3_character": ""})
d0 = body(srv._api_characters_manage_get({}))
check("勾选不足 2 个 → available=False（单人输出）",
      d0["available"] is False and "至少" in (d0.get("reason") or ""), d0.get("reason"))
check("不可用时仍可编辑（插件已启用 → can_edit=True）", d0["can_edit"] is True)
first_voice = (d["voices"] or [""])[0]
sel = names[:2]
tok = token_for("characters.manage", PLUGIN)
r = post({"selected": sel, "voices": {sel[0]: first_voice}, "token": tok})
rb = body(r)
check("带令牌保存勾选成功", r[0] == 200 and rb.get("count") == 2, str(rb)[:120])
check("保存响应回传可用性（已激活）", rb.get("available") is True, str(rb.get("reason") or rb.get("note")))
d1 = body(srv._api_characters_manage_get({}))
check("勾选满 2 个 → available=True（多人对话激活）",
      d1["available"] is True and set(d1["selected"]) == set(sel), str(d1["selected"]))
check("声线映射已保存", d1["voices_map"].get(sel[0]) == first_voice, str(d1["voices_map"]))
check("勾选结果落盘到插件设置（selected_characters）",
      json.loads(mgr.get_settings(PLUGIN).get("selected_characters") or "[]") == sel)

print()
print("=" * 74)
print("3) 保存接口的安全约束（客户端标识 / 一次性令牌 / 插件启用状态）")
print("=" * 74)
r_no_client = srv._api_characters_manage_post({"json": {"selected": sel}, "headers": {}})
check("缺客户端标识 → 403", r_no_client[0] == 403, str(body(r_no_client))[:100])
r_no_token = post({"selected": sel})
check("缺确认令牌 → 409 且 need_confirm", r_no_token[0] == 409 and body(r_no_token).get("need_confirm"),
      str(body(r_no_token))[:120])
tok2 = token_for("characters.manage", PLUGIN)
post({"selected": sel, "token": tok2})                      # 正常消费掉
r_reuse = post({"selected": sel, "token": tok2})
check("令牌不能重复使用 → 409", r_reuse[0] == 409)
r_wrong_target = post({"selected": sel, "token": token_for("characters.manage", "其他插件")})
check("令牌目标不匹配 → 409", r_wrong_target[0] == 409)
mgr.disable(PLUGIN)
d_off = body(srv._api_characters_manage_get({}))
check("插件停用 → 可用性为不可用（单人输出）",
      d_off["available"] is False and d_off["plugin_enabled"] is False, d_off.get("reason"))
check("插件停用时不可编辑（can_edit=False）", d_off["can_edit"] is False)
r_off = post({"selected": sel, "token": token_for("characters.manage", PLUGIN)})
check("插件停用 → 保存被拒（409 + need_plugin）",
      r_off[0] == 409 and body(r_off).get("need_plugin") is True, str(body(r_off))[:120])
mgr.enable(PLUGIN)

print()
print("=" * 74)
print("4) 插件动作 / 设置文案指向设置页")
print("=" * 74)
spec = importlib.util.spec_from_file_location("plugins.multi_chat", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "multi_chat.py"))
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)
act = mc.on_action("manage_characters", mgr.ctx)
check("动作跳转到设置页「角色与语音」", bool(act) and act.get("page") == "/settings.html#characters", str(act))
av = mc.available(mgr.ctx)
check("插件 available() 返回结构化握手信息",
      isinstance(av, dict) and all(k in av for k in ("available", "selected", "min_roles", "note")), str(av))
schema = mc.settings_schema()
sec = [f for f in schema if f.get("type") == "section"]
check("插件设置文案已改为指向设置页（不再说独立页面）",
      bool(sec) and "角色与语音" in (sec[0].get("desc") or ""))
check("设置分组动作标签指向「角色与语音」",
      bool(sec) and "角色与语音" in (sec[0]["actions"][0]["label"] or ""))

print()
print("=" * 74)
print("5) 角色卡片：分类字段 / 修改设定 / LLM 提示词优化（写盘后复原）")
print("=" * 74)
from core import character as character_core
import os

d2 = body(srv._api_characters_manage_get({}))
first = d2["presets"][0]
check("角色条目带有分类字段（自定义要求 / 参考资料 / 完整提示词 / 字数 / 是否当前）",
      all(k in first for k in ("custom_req", "reference", "description", "prompt_len", "is_current", "summary")),
      str(sorted(first.keys())))
check("分类拆分可用（自定义要求非空，参考资料非空）",
      bool(first["custom_req"]) and bool(first["reference"]), f"{first['custom_req'][:20]} / {first['reference'][:20]}")

# LLM 优化：打桩生成模型，只验证链路与安全约束
from core import llm as llm_core
_orig_generate = llm_core.generate
llm_core.generate = lambda prompt, **kw: "优化后的角色设定：身份、性格、说话风格。"
r_opt_no_prompt = srv._api_characters_preset_optimize({"json": {"name": first["name"], "prompt": ""},
                                                       "headers": dict(HEADERS)})
check("LLM 优化：缺提示词 → 400", r_opt_no_prompt[0] == 400, str(body(r_opt_no_prompt))[:100])
r_opt = srv._api_characters_preset_optimize({"json": {"name": first["name"], "prompt": "旧提示词内容"},
                                             "headers": dict(HEADERS)})
ob = body(r_opt)
check("LLM 优化：返回优化结果（且不写盘）",
      r_opt[0] == 200 and ob.get("optimized", "").startswith("优化后的") and
      character_core.load_presets()[first["name"]].get("description") == first["description"],
      str(ob)[:120])
r_opt_no_client = srv._api_characters_preset_optimize({"json": {"name": first["name"], "prompt": "x"},
                                                       "headers": {}})
check("LLM 优化：缺客户端标识 → 403", r_opt_no_client[0] == 403)
llm_core.generate = _orig_generate

# 修改设定：缺令牌 409、带令牌成功、随后复原
orig_preset = dict(character_core.load_presets().get(first["name"]) or {})
r_save_no_token = srv._api_characters_preset_save({
    "json": {"name": first["name"], "prompt": "临时提示词", "custom_req": "临时要求", "reference": ""},
    "headers": dict(HEADERS)})
check("修改设定：缺确认令牌 → 409 + need_confirm",
      r_save_no_token[0] == 409 and body(r_save_no_token).get("need_confirm"), str(body(r_save_no_token))[:110])
r_save_unknown = srv._api_characters_preset_save({
    "json": {"name": "不存在的角色XYZ", "prompt": "x",
             "token": token_for("characters.preset", "不存在的角色XYZ")},
    "headers": dict(HEADERS)})
check("修改设定：未知角色 → 400", r_save_unknown[0] == 400, str(body(r_save_unknown))[:100])
r_save = srv._api_characters_preset_save({
    "json": {"name": first["name"], "prompt": "临时提示词：测试用", "custom_req": "临时要求", "reference": "临时资料",
             "token": token_for("characters.preset", first["name"])},
    "headers": dict(HEADERS)})
saved = character_core.load_presets().get(first["name"], {})
check("修改设定：带令牌保存成功且落盘",
      r_save[0] == 200 and saved.get("description") == "临时提示词：测试用", str(body(r_save))[:110])
d3 = body(srv._api_characters_manage_get({}))
now = next(p for p in d3["presets"] if p["name"] == first["name"])
check("保存后分类字段同步更新（自定义要求 / 参考资料）",
      now["custom_req"] == "临时要求" and now["reference"] == "临时资料",
      f"{now['custom_req']} / {now['reference']}")
# 复原用户原预设
character_core.save_preset(first["name"], orig_preset)
check("测试后已复原用户原角色预设",
      character_core.load_presets()[first["name"]].get("description") == first["description"])

# 新建角色：创建后清理（不残留测试文件）
tmp_name = "__测试角色_验证用__"
r_new = srv._api_characters_preset_save({
    "json": {"name": tmp_name, "prompt": "测试角色提示词", "create": True,
             "token": token_for("characters.create", tmp_name)},
    "headers": dict(HEADERS)})
check("新建角色：带令牌创建成功", r_new[0] == 200 and tmp_name in character_core.list_presets(),
      str(body(r_new))[:110])
r_dup = srv._api_characters_preset_save({
    "json": {"name": tmp_name, "prompt": "x", "create": True,
             "token": token_for("characters.create", tmp_name)},
    "headers": dict(HEADERS)})
check("新建角色：重名 → 400", r_dup[0] == 400, str(body(r_dup))[:100])
try:
    os.remove(os.path.join(character_core.config.PRESETS_DIR, f"{tmp_name}.json"))
except OSError:
    pass
check("新建角色测试文件已清理", tmp_name not in character_core.list_presets())

# 联网搜索补全设定：打桩搜索接口，验证链路与额度校验（不产生真实联网请求）
from core import search as search_core
_orig_search = search_core.search_tavily
_orig_usage = search_core.get_usage_info
search_core.get_usage_info = lambda: {"used": 1, "limit": 100, "remaining": 99}
search_core.search_tavily = lambda q, **kw: {"results": [{"content": "洛天依是中文虚拟歌手，声线温柔。"},
                                                       {"content": "常用说话风格：口语化、带语气词。"}]}
r_ref = srv._api_characters_preset_reference({"json": {"name": first["name"], "requirement": "朋友关系"},
                                              "headers": dict(HEADERS)})
rb2 = body(r_ref)
check("联网搜索补全设定：返回资料文本与剩余额度",
      r_ref[0] == 200 and "虚拟歌手" in (rb2.get("text") or "") and rb2.get("remaining") == 99, str(rb2)[:110])
r_ref_no_name = srv._api_characters_preset_reference({"json": {"name": ""}, "headers": dict(HEADERS)})
check("联网搜索补全设定：缺角色名 → 400", r_ref_no_name[0] == 400)
search_core.get_usage_info = lambda: {"used": 100, "limit": 100, "remaining": 0}
r_ref_no_quota = srv._api_characters_preset_reference({"json": {"name": "洛天依"}, "headers": dict(HEADERS)})
check("联网搜索补全设定：额度用尽 → 429", r_ref_no_quota[0] == 429, str(body(r_ref_no_quota))[:100])
search_core.search_tavily = _orig_search
search_core.get_usage_info = _orig_usage

# ---- 恢复用户原配置 ----
mgr.save_settings(PLUGIN, orig)
if not was_enabled:
    mgr.disable(PLUGIN)
print()
print("已恢复原插件配置。")
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("验证结论: ✓ 全部通过")
