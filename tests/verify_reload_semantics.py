# verify_reload_semantics.py
# 插件增量重载语义自测脚本（只使用标准库）。
#
# 覆盖点：
#   1) 只重载文件指纹发生变化的插件，未变化的插件完全不碰（不 on_unload / 不重新 exec / 不 on_load）
#   2) 新增插件文件 → 出现在 loaded
#   3) 删除插件文件 → 出现在 unloaded 且从 list_plugins() 消失
#   4) 重载前静默并等待在途钩子调用结束（waited_ms 与调用顺序）
#   5) 静默期的插件不再被分发消息钩子
#   6) force=True 保持旧的「全部重载」语义
#   7) list_plugins() 暴露 reload_policy / changing，且原有字段不丢
#
# 安全性：全程使用临时插件目录 runtime/reload_test_plugins，
#         不触碰真实 plugins/、plugins_state.json、plugins_settings.json。
#
# 运行：<project-root>\venv\Scripts\python.exe <project-root>\verify_reload_semantics.py
import hashlib
import os
import shutil
import sys
import threading
import time
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEMP_PLUGIN_DIR = os.path.join(PROJECT_ROOT, "runtime", "reload_test_plugins")
REAL_STATE_FILE = os.path.join(PROJECT_ROOT, "plugins_state.json")
REAL_SETTINGS_FILE = os.path.join(PROJECT_ROOT, "plugins_settings.json")

PASS_COUNT = 0
FAIL_COUNT = 0


def check(label, cond, detail=""):
    """打印一条断言结果（✓/✗）。"""
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  ✓ {label}" + (f"（{detail}）" if detail else ""))
    else:
        FAIL_COUNT += 1
        print(f"  ✗ {label}" + (f"（{detail}）" if detail else ""))
    return bool(cond)


def sha1_file(path):
    """计算文件内容 sha1（文件不存在时返回 "<不存在>"）。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except Exception:
        return "<不存在>"


PLUGIN_TEMPLATE = '''\
# 临时测试插件（由 verify_reload_semantics.py 生成，脚本结束会自动删除）
import time

NAME = {name!r}
VERSION = "{version}"
DESCRIPTION = "reload 语义测试插件"
AUTHOR = "verify_reload_semantics.py"
HOT_SWAP = True
SLOW = {slow!r}   # 是否响应 /slow（用于制造「在途钩子」）
RELOAD_POLICY = {{"preserve_context": True, "allow_during_chat": False,
                 "note": {name!r} + " 的测试策略"}}

CALLS = []   # 钩子调用记录：[(标签, 时间戳, 卸载原因), ...]


def _rec(tag, reason=""):
    CALLS.append((tag, time.time(), reason))


def on_load(settings, ctx):
    _rec("on_load", getattr(ctx, "unload_reason", ""))


def on_unload(ctx):
    _rec("on_unload", getattr(ctx, "unload_reason", ""))


def on_message(user_text, mode, ctx):
    _rec("on_message", getattr(ctx, "unload_reason", ""))
{message_body}


def on_command(cmd, args, ctx):
    if SLOW and cmd == "/slow":
        _rec("slow_begin", "")
        time.sleep(0.4)
        _rec("slow_end", "")
        return {{"reply": NAME + " 慢动作完成"}}
    return None
{extra}
'''

REPLY_BODY = '    return {"reply": NAME + " 已响应"}'
SILENT_BODY = "    return None"


def write_plugin(name, reply=False, slow=False, extra="", version="1.0.0"):
    """生成 / 覆盖一个临时插件文件。"""
    path = os.path.join(TEMP_PLUGIN_DIR, f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PLUGIN_TEMPLATE.format(
            name=name,
            version=version,
            slow=slow,
            message_body=REPLY_BODY if reply else SILENT_BODY,
            extra=extra,
        ))
    return path


def tags(module):
    """返回插件模块记录的钩子标签序列。"""
    return [c[0] for c in getattr(module, "CALLS", [])]


def tag_count(module, tag):
    """返回插件模块记录的某个钩子调用次数。"""
    return sum(1 for c in getattr(module, "CALLS", []) if c[0] == tag)


def first_ts(module, tag):
    """返回插件模块记录的某个钩子的首个时间戳（不存在时返回 None）。"""
    for c in getattr(module, "CALLS", []):
        if c[0] == tag:
            return c[1]
    return None


def wait_until(pred, timeout=3.0, interval=0.005):
    """轮询等待条件成立，返回是否成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def main():
    # ---------- 1. 把插件配置指向临时目录（必须在 import core.plugin_manager 之前） ----------
    import core.config as config

    original = {key: getattr(config, key) for key in
                ("PLUGINS_DIR", "PLUGINS_STATE_FILE", "PLUGINS_SETTINGS_FILE")}
    shutil.rmtree(TEMP_PLUGIN_DIR, ignore_errors=True)
    os.makedirs(TEMP_PLUGIN_DIR, exist_ok=True)
    config.PLUGINS_DIR = TEMP_PLUGIN_DIR
    config.PLUGINS_STATE_FILE = os.path.join(TEMP_PLUGIN_DIR, "test_state.json")
    config.PLUGINS_SETTINGS_FILE = os.path.join(TEMP_PLUGIN_DIR, "test_settings.json")

    # 真实状态文件的内容快照（结束时校验未被改动）
    real_state_before = sha1_file(REAL_STATE_FILE)
    real_settings_before = sha1_file(REAL_SETTINGS_FILE)

    import core.plugin_manager as pm

    print("=" * 68)
    print("插件重载语义验证（临时目录：runtime/reload_test_plugins）")
    print("=" * 68)

    # ---------- 2. 准备 A / B / C 三个临时插件并实例化管理器 ----------
    write_plugin("A")
    write_plugin("B", reply=True, slow=True)
    write_plugin("C")
    mgr = pm.PluginManager()
    print(f"\n[准备] 已实例化 PluginManager，加载插件：{sorted(mgr._plugins)}")
    check("初始加载 A/B/C 三个插件", set(mgr._plugins) == {"A", "B", "C"},
          f"实际 {sorted(mgr._plugins)}")
    check("初始 on_load 各调用 1 次",
          all(tag_count(mgr._plugins[n].module, "on_load") == 1 for n in ("A", "B", "C")))

    # ---------- 项1：只重载指纹变化的插件 ----------
    print("\n[项1] 只改 B 的文件内容 → 只应重载 B")
    a_mod = mgr._plugins["A"].module
    c_mod = mgr._plugins["C"].module
    a_snapshot = (tag_count(a_mod, "on_load"), tag_count(a_mod, "on_unload"))
    c_snapshot = (tag_count(c_mod, "on_load"), tag_count(c_mod, "on_unload"))
    write_plugin("B", reply=True, slow=True, extra="\n# 变更：触发 B 的增量重载\n", version="1.0.1")
    report = mgr.reload_report()
    check("reloaded == ['B']", report["reloaded"] == ["B"], f"实际 {report['reloaded']}")
    check("kept 包含 A 与 C", "A" in report["kept"] and "C" in report["kept"],
          f"实际 kept={report['kept']}")
    check("A/C 模块对象未被替换（未重新 exec）",
          mgr._plugins["A"].module is a_mod and mgr._plugins["C"].module is c_mod)
    check("A 的 on_unload / on_load 计数未增加",
          (tag_count(a_mod, "on_load"), tag_count(a_mod, "on_unload")) == a_snapshot,
          f"{a_snapshot} → {(tag_count(a_mod, 'on_load'), tag_count(a_mod, 'on_unload'))}")
    check("C 的 on_unload / on_load 计数未增加",
          (tag_count(c_mod, "on_load"), tag_count(c_mod, "on_unload")) == c_snapshot,
          f"{c_snapshot} → {(tag_count(c_mod, 'on_load'), tag_count(c_mod, 'on_unload'))}")
    check("B 的新模块 on_load 调用 1 次",
          tag_count(mgr._plugins["B"].module, "on_load") == 1)
    check("报告结构完整且 ok=True",
          report["ok"] is True and set(report) ==
          {"ok", "errors", "loaded", "reloaded", "unloaded", "kept", "waited_ms"},
          f"字段 {sorted(report)}")
    check("reload() 仍返回错误字符串列表", isinstance(mgr.reload(), list))

    b_mod = mgr._plugins["B"].module
    print("\n[项1b] 文件无变化时再次 reload → 谁都不碰")
    report = mgr.reload_report()
    check("reloaded 为空", report["reloaded"] == [] and report["loaded"] == [])
    check("kept 包含 A/B/C", set(report["kept"]) == {"A", "B", "C"}, f"实际 {report['kept']}")
    check("B 未被卸载（on_unload 计数为 0）", tag_count(b_mod, "on_unload") == 0)
    check("B 模块对象未被替换", mgr._plugins["B"].module is b_mod)

    # ---------- 项2：新增插件文件 ----------
    print("\n[项2] 新增插件 D 文件")
    write_plugin("D")
    report = mgr.reload_report()
    check("loaded == ['D']", report["loaded"] == ["D"], f"实际 {report['loaded']}")
    check("D 已进入管理表且 on_load 调用 1 次",
          "D" in mgr._plugins and tag_count(mgr._plugins["D"].module, "on_load") == 1)
    check("A/B/C 仍被跳过", set(report["kept"]) == {"A", "B", "C"}, f"实际 {report['kept']}")

    # ---------- 项6：force=True 全量重载 ----------
    print("\n[项6] force=True 应恢复全部重载语义")
    before = {n: mgr._plugins[n].module for n in ("A", "B", "C", "D")}
    report = mgr.reload_report(force=True)
    check("reloaded 包含 A/B/C/D", set(report["reloaded"]) == {"A", "B", "C", "D"},
          f"实际 {report['reloaded']}")
    check("所有插件模块对象都被替换",
          all(mgr._plugins[n].module is not before[n] for n in before))
    check("所有旧模块都收到 on_unload",
          all(tag_count(before[n], "on_unload") == 1 for n in before),
          f"计数 {[tag_count(before[n], 'on_unload') for n in before]}")
    check("所有新模块都被 on_load",
          all(tag_count(mgr._plugins[n].module, "on_load") == 1 for n in before))

    # ---------- 项3：删除插件文件 ----------
    print("\n[项3] 删除插件 C 文件")
    c_mod_before = mgr._plugins["C"].module
    os.remove(os.path.join(TEMP_PLUGIN_DIR, "C.py"))
    report = mgr.reload_report()
    check("unloaded == ['C']", report["unloaded"] == ["C"], f"实际 {report['unloaded']}")
    check("C 从管理表移除", "C" not in mgr._plugins)
    check("C 不再出现在 list_plugins()",
          "C" not in [item["name"] for item in mgr.list_plugins()])
    check("被删除的插件收到了 on_unload", tag_count(c_mod_before, "on_unload") == 1)
    check("A/B/D 仍被跳过", set(report["kept"]) == {"A", "B", "D"}, f"实际 {report['kept']}")

    # ---------- 项4：静默期等待在途调用 ----------
    print("\n[项4] 重载前静默并等待在途钩子（B 的 /slow 钩子 sleep 0.4s）")
    # 先改一次 B 的文件，保证这次 reload 确实要重载 B（旧模块仍在内存里继续服务）
    write_plugin("B", reply=True, slow=True, extra="\n# 项4：再次变更 B，触发重载\n", version="1.0.2")
    old_b = mgr._plugins["B"].module
    worker_result = {}

    def call_slow():
        worker_result["result"] = mgr.handle_command("/slow", "standard")

    worker = threading.Thread(target=call_slow, daemon=True)
    worker.start()
    entered = wait_until(lambda: "slow_begin" in tags(old_b), 3.0)
    check("慢钩子已开始执行", entered)
    report = mgr.reload_report()
    worker.join(3.0)
    check("reload 等待了在途调用（waited_ms >= 300）", report["waited_ms"] >= 300,
          f"waited_ms={report['waited_ms']}")
    check("本次只有 B 被重载", report["reloaded"] == ["B"], f"实际 {report['reloaded']}")
    check("等待期间其它插件未被牵连",
          set(report["kept"]) == {"A", "D"}, f"实际 kept={report['kept']}")
    order = tags(old_b)
    check("慢钩子先返回、on_unload 后调用",
          "slow_end" in order and "on_unload" in order
          and order.index("on_unload") > order.index("slow_end"),
          f"调用顺序 {order}")
    slow_end_ts = first_ts(old_b, "slow_end")
    unload_ts = first_ts(old_b, "on_unload")
    check("on_unload 时间戳晚于钩子返回时间戳",
          slow_end_ts is not None and unload_ts is not None and unload_ts > slow_end_ts,
          f"相差 {(unload_ts - slow_end_ts) * 1000:.1f}ms"
          if (slow_end_ts and unload_ts) else "缺少时间戳记录")
    check("慢钩子的返回值未被静默丢弃",
          isinstance(worker_result.get("result"), dict)
          and worker_result["result"].get("reply"), f"{worker_result.get('result')}")

    # ---------- 项5：静默期不再分发消息钩子 ----------
    print("\n[项5] 插件静默期间不再收到新消息")
    cur_b = mgr._plugins["B"].module
    msg_before = tag_count(cur_b, "on_message")
    with mgr._lock:
        mgr._plugins["B"].quiescing = True
    try:
        changing = [item["changing"] for item in mgr.list_plugins() if item["name"] == "B"]
        check("list_plugins() 中 B 的 changing 为 True", changing == [True], f"实际 {changing}")
        check("静默期 B 不在 _enabled_plugins() 中",
              "B" not in [p.name for p in mgr._enabled_plugins()])
        result = mgr.handle_message("你好", "standard")
        check("handle_message() 未分发到 B（返回 None）", result is None, f"实际 {result}")
        check("B 的 on_message 调用计数未增加",
              tag_count(cur_b, "on_message") == msg_before,
              f"{msg_before} → {tag_count(cur_b, 'on_message')}")
        check("静默期 get_state() 不会调用旧模块", mgr.get_state("B") == {})
    finally:
        with mgr._cond:
            mgr._plugins["B"].quiescing = False
    check("解除静默后 B 恢复接收消息",
          isinstance(mgr.handle_message("你好", "standard"), dict))

    # ---------- 项7：list_plugins() 新字段与既有字段 ----------
    print("\n[项7] list_plugins() 暴露 reload_policy / changing 且原有字段不丢")
    items = mgr.list_plugins()
    required = {"name", "version", "description", "author", "official", "hot_swap",
                "has_state", "enabled", "file", "commands", "actions",
                "settings_schema", "settings", "settings_tab",
                "reload_policy", "changing"}
    check("每项都包含全部既有字段与新增字段",
          all(required <= set(item) for item in items),
          f"缺失 {sorted(required - set(items[0])) if items else '无插件'}")
    check("reload_policy 均为 dict", all(isinstance(item["reload_policy"], dict) for item in items))
    check("changing 均为 bool", all(isinstance(item["changing"], bool) for item in items))
    check("插件声明的 RELOAD_POLICY 被正确暴露",
          all(item["reload_policy"].get("preserve_context") is True for item in items),
          f"示例 {items[0]['reload_policy'] if items else None}")
    check("未静默时 changing 为 False", all(item["changing"] is False for item in items))

    # ---------- 安全性：真实配置文件未被触碰 ----------
    print("\n[安全性] 真实 plugins_state.json / plugins_settings.json 未被改动")
    check("plugins_state.json 内容未变", sha1_file(REAL_STATE_FILE) == real_state_before)
    check("plugins_settings.json 内容未变", sha1_file(REAL_SETTINGS_FILE) == real_settings_before)

    # 恢复配置（异常路径也会在 finally 中恢复）
    for key, value in original.items():
        setattr(config, key, value)
    return 0


def cleanup():
    """清理临时插件目录与临时模块。"""
    try:
        import core.plugin_manager as pm  # noqa: F401  确保已导入
        for mod_name in [n for n in list(sys.modules) if n.startswith("_xllb_plugin_")]:
            sys.modules.pop(mod_name, None)
    except Exception:
        pass
    shutil.rmtree(TEMP_PLUGIN_DIR, ignore_errors=True)


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except Exception:
        traceback.print_exc()
        check("验证脚本未抛出异常", False, "见上方堆栈")
        exit_code = 1
    finally:
        cleanup()

    print("\n" + "=" * 68)
    print(f"通过 {PASS_COUNT} 项，失败 {FAIL_COUNT} 项")
    if FAIL_COUNT == 0 and exit_code == 0:
        print("验证结论: ✓ 全部通过")
        sys.exit(0)
    print("验证结论: ✗ 存在失败")
    sys.exit(1)
