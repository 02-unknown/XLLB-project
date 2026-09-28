# core/plugin_manager.py
# 轻量插件系统：扫描 plugins/ 目录，动态加载 / 重载 / 启停插件，
# 并为第三方插件开放丰富的扩展点与上下文能力。
#
# 插件约定（详见 plugins/README.md）：
#   - 一个 .py 文件即一个插件，可定义 NAME/VERSION/DESCRIPTION/AUTHOR；
#   - 可选钩子：on_load / on_unload / on_message / on_command / commands /
#               after_message / pre_llm / post_llm；
#   - 可选设置：SETTINGS（默认值字典）+ settings_schema() + on_settings_changed()。
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import traceback

import core.config as config
from core import storage


class _Context:
    """传递给插件的上下文，暴露受控的能力接口。"""

    def __init__(self):
        self.config = config
        # 插件卸载原因：on_unload 调用期间为 "reload"（被重载）或 "disable"（被停用），
        # 其余时间为空串。插件可据此区分处理（例如记忆插件在 reload 时保留会话上下文）。
        self.unload_reason = ""

    @staticmethod
    def history():
        return storage.get_history()

    @staticmethod
    def call_llm(text, extra_context="", record=True, use_context=True):
        from core import llm
        return llm.call_ollama(text, extra_context=extra_context,
                               record=record, use_context=use_context)

    @staticmethod
    def generate(prompt, num_predict=64, temperature=0.0, purpose="plugin"):
        from core import llm
        return llm.generate(prompt, num_predict=num_predict, temperature=temperature, purpose=purpose)

    @staticmethod
    def judge(user_text):
        from core import judge
        return judge.judge_need_online(user_text)

    @staticmethod
    def search(query):
        from core import search
        return search.search_tavily(query)

    @staticmethod
    def music_search(query):
        from core import music
        return music.search_music(query)

    @staticmethod
    def synthesize(text):
        from core import tts
        return tts.synthesize(text)

    @staticmethod
    def list_models():
        from core import llm
        return llm.list_ollama_models()

    @staticmethod
    def check_backend():
        from core import llm
        return llm.check_backend()

    @staticmethod
    def log(*args):
        print("[plugin]", *args)


class Plugin:
    """单个插件的运行时封装。"""

    def __init__(self, name, filepath, module):
        self.name = name
        self.filepath = filepath
        self.module = module
        self.version = getattr(module, "VERSION", "0.0.0")
        self.description = getattr(module, "DESCRIPTION", "")
        self.author = getattr(module, "AUTHOR", "")
        # ---- 增量重载 / 静默期相关的运行时状态 ----
        self.fingerprint = None      # 最近一次加载时插件文件的内容指纹
        self.inflight = 0            # 在途的插件钩子调用数（进入 +1，退出 -1）
        self.inflight_tids = {}      # 线程 id -> 该线程的在途调用数（避免钩子内自等待）
        self.quiescing = False       # 是否处于静默期（静默期间不再分发新请求）
        self.enabled_applied = False  # on_load 是否已对该插件生效（用于识别启用状态变化）

    def commands(self):
        fn = getattr(self.module, "commands", None)
        if not fn:
            return []
        try:
            return list(fn()) or []
        except Exception:
            return []

    def settings_schema(self):
        fn = getattr(self.module, "settings_schema", None)
        if not fn:
            return []
        try:
            return list(fn()) or []
        except Exception:
            return []

    def actions(self):
        fn = getattr(self.module, "actions", None)
        if not fn:
            return []
        try:
            return list(fn()) or []
        except Exception:
            return []

    def reload_policy(self):
        """插件声明的重载策略（可选，缺省为空字典）。

        例如：

            RELOAD_POLICY = {"preserve_context": True, "allow_during_chat": False,
                             "note": "重载时保留 L0 会话上下文"}

        仅作为元信息暴露给前端，插件管理器不解释其内容。
        """
        try:
            return dict(getattr(self.module, "RELOAD_POLICY", {}) or {})
        except Exception:
            return {}

    def settings_tab(self, ctx=None):
        """插件在「设置」页左侧栏声明的分类标签（可选，用于第三方插件接入设置界面）。

        两种写法都支持：

            SETTINGS_TAB = {"label": "音乐", "order": 60}

            def settings_tab(ctx):
                return {"label": "音乐", "type": "page", "page": "/music.html"}

        - `type="inline"`（默认）：右侧渲染该插件的设置表单 / 动作 / 状态；
        - `type="page"`：右侧给出入口按钮，点击打开 `page` 指定的页面（插件自带页面）；
        - `order` 越小越靠前；未声明时 Web UI 按插件的设置/动作自动生成一个同名标签。
        - `icon` 为保留字段：当前设置页左侧栏是纯文字标签，不显示图标。
        """
        meta = getattr(self.module, "SETTINGS_TAB", None)
        fn = getattr(self.module, "settings_tab", None)
        if callable(fn):
            try:
                out = fn(ctx)
                if isinstance(out, dict) and out:
                    meta = out
            except Exception:
                pass
        if not isinstance(meta, dict):
            return {}
        tab = {}
        for key in ("label", "icon", "desc", "page"):
            if isinstance(meta.get(key), str):
                tab[key] = meta[key]
        tab["type"] = "page" if meta.get("type") == "page" else "inline"
        try:
            tab["order"] = int(meta.get("order", 60))
        except Exception:
            tab["order"] = 60
        return tab


class PluginManager:
    """插件管理器（进程内单例）。"""

    def __init__(self):
        self._lock = threading.RLock()
        # 在途调用等待 / 通知复用 _lock（可重入锁，Condition 会正确处理递归计数）
        self._cond = threading.Condition(self._lock)
        # 串行化 reload / enable / disable，避免插件替换与启停相互穿插
        self._reload_lock = threading.RLock()
        self._plugins = {}          # name -> Plugin
        self._state = {}            # name -> bool (enabled)
        self._settings = {}         # name -> {key: value}
        self.ctx = _Context()
        self.ctx.manager = self     # 让插件可持久化自己的设置
        self._load_state()
        self._load_settings()
        self._reload_locked()

    # ==================== 状态 / 设置持久化 ====================
    def _load_state(self):
        try:
            with open(config.PLUGINS_STATE_FILE, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except Exception:
            self._state = {}

    def _save_state(self):
        try:
            with open(config.PLUGINS_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存插件状态失败: {e}")

    def _load_settings(self):
        try:
            with open(config.PLUGINS_SETTINGS_FILE, "r", encoding="utf-8") as f:
                self._settings = json.load(f)
        except Exception:
            self._settings = {}

    def _save_settings(self):
        try:
            with open(config.PLUGINS_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存插件设置失败: {e}")

    # ==================== 加载 / 重载 ====================
    def _plugin_files(self):
        os.makedirs(config.PLUGINS_DIR, exist_ok=True)
        files = []
        for name in sorted(os.listdir(config.PLUGINS_DIR)):
            if not name.endswith(".py") or name.startswith("_") or name == "__init__.py":
                continue
            files.append(os.path.join(config.PLUGINS_DIR, name))
        return files

    def _load_module(self, filepath):
        base = os.path.splitext(os.path.basename(filepath))[0]
        modname = f"_xllb_plugin_{base}"
        spec = importlib.util.spec_from_file_location(modname, filepath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _file_fingerprint(filepath):
        """计算插件文件的内容指纹：sha1(文件字节) + mtime_ns + size。

        指纹相同即认为插件文件没有被改动，重载时会完全跳过该插件
        （不调用 on_unload、不重新 exec 模块、不调用 on_load）。
        文件不可读时返回空串（视为已变化，交由加载流程报错）。
        """
        try:
            st = os.stat(filepath)
            with open(filepath, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()
            return f"{digest}:{st.st_mtime_ns}:{st.st_size}"
        except Exception:
            return ""

    # ==================== 静默期 / 在途调用 ====================
    def _plugin_of_locked(self, module):
        """按模块对象反查插件（模块已被替换或不属于本管理器时返回 None）。"""
        for p in self._plugins.values():
            if p.module is module:
                return p
        return None

    def _pending_inflight_locked(self, plugin):
        """除当前线程自身以外的在途调用数。

        减去自身是为了避免插件在自己的钩子里触发重载 / 停用时自我等待到超时。
        """
        return plugin.inflight - plugin.inflight_tids.get(threading.get_ident(), 0)

    def _exit_inflight(self, plugin):
        """退出一次插件钩子调用：在途计数 -1，归零时唤醒等待静默的线程。"""
        with self._cond:
            tid = threading.get_ident()
            left = plugin.inflight_tids.get(tid, 0) - 1
            if left > 0:
                plugin.inflight_tids[tid] = left
            else:
                plugin.inflight_tids.pop(tid, None)
            plugin.inflight = max(0, plugin.inflight - 1)
            if plugin.inflight == 0:
                self._cond.notify_all()

    def _quiesce_and_wait(self, plugin, timeout=5.0):
        """把插件标记为静默（不再分发新请求），并等待其在途钩子调用结束。

        返回实际等待秒数（即使超时也继续重载，只打印警告）。
        """
        started = time.monotonic()
        with self._cond:
            plugin.quiescing = True
            while self._pending_inflight_locked(plugin) > 0:
                remain = timeout - (time.monotonic() - started)
                if remain <= 0:
                    print(f"警告：插件 {plugin.name} 仍有在途钩子调用未结束，"
                          f"等待 {timeout:.0f} 秒超时，继续重载（可能与其执行中的钩子竞争）。")
                    break
                self._cond.wait(remain)
        return max(0.0, time.monotonic() - started)

    def _clear_quiescing(self, plugin):
        """解除静默，允许继续向该插件分发请求。"""
        with self._cond:
            plugin.quiescing = False
            if plugin.inflight == 0:
                self._cond.notify_all()

    def _unload_plugin(self, plugin, reason):
        """调用 on_unload，并在 ctx 上标记卸载原因（"reload" / "disable"）。"""
        self.ctx.unload_reason = reason
        try:
            self._call(plugin.module, "on_unload", self.ctx, _internal=True)
        finally:
            self.ctx.unload_reason = ""
        plugin.enabled_applied = False

    def _forget_plugin(self, name):
        """把插件从管理表 / 状态 / 设置中移除（文件被删除或加载失败时）。"""
        with self._lock:
            p = self._plugins.pop(name, None)
            if p is not None:
                p.quiescing = False
            self._state.pop(name, None)
            self._settings.pop(name, None)

    def _migrate_key_locked(self, old_name, new_name):
        """插件改名（模块 NAME 变化）时迁移状态与设置。"""
        if old_name == new_name:
            return
        if old_name in self._state:
            self._state[new_name] = self._state.pop(old_name)
        if old_name in self._settings:
            self._settings[new_name] = self._settings.pop(old_name)
        self._plugins.pop(old_name, None)

    # ==================== 重载 ====================
    def reload(self, force=False):
        """重载插件，返回错误字符串列表（保持旧接口兼容）。

        默认只重载发生变化的插件；force=True 时恢复旧的「全部重载」语义。
        """
        return list(self.reload_report(force=force).get("errors", []))

    def reload_report(self, force=False):
        """重载插件并返回结构化报告。

        报告字段：ok / errors / loaded / reloaded / unloaded / kept / waited_ms。

        默认（force=False）为增量重载，只处理：
          - 文件新增的插件        → 加载，对启用者调用 on_load（计入 loaded）
          - 文件指纹变化的插件    → 卸载旧模块 → 重新加载 → 对启用者调用 on_load（计入 reloaded）
          - 文件被删除的插件      → 卸载并从管理表移除（计入 unloaded）
          - 启用状态发生变化的插件 → 只补 / 撤 on_load、on_unload，不重新加载模块
        指纹未变的插件完全不碰（计入 kept）：不 on_unload、不重新 exec、不 on_load，
        因此持有会话上下文 / 数据库连接 / 后台线程的插件不会被破坏。
        """
        report = {"ok": True, "errors": [], "loaded": [], "reloaded": [],
                  "unloaded": [], "kept": [], "waited_ms": 0}
        waited = 0.0
        with self._reload_lock:
            if force:
                waited += self._force_reload_locked(report)
            else:
                waited += self._incremental_reload_locked(report)
        report["waited_ms"] = int(round(waited * 1000))
        report["ok"] = not report["errors"]
        print(f"插件重载完成：新增 {len(report['loaded'])}，重载 {len(report['reloaded'])}，"
              f"卸载 {len(report['unloaded'])}，跳过 {len(report['kept'])}，"
              f"等待在途调用 {report['waited_ms']}ms。")
        if report["errors"]:
            print(f"{len(report['errors'])} 个插件加载失败: {report['errors']}")
        return report

    def _incremental_reload_locked(self, report):
        """增量重载实现（调用方需已持有 _reload_lock）。"""
        waited = 0.0
        files = self._plugin_files()
        disk = {}
        for filepath in files:
            disk[os.path.normcase(os.path.abspath(filepath))] = filepath
        by_file = {}
        for name, p in self._plugins.items():
            by_file[os.path.normcase(os.path.abspath(p.filepath))] = p

        # 1) 文件已被删除的插件：静默 → 卸载 → 移出管理表
        for name, p in list(self._plugins.items()):
            if os.path.normcase(os.path.abspath(p.filepath)) in disk:
                continue
            waited += self._quiesce_and_wait(p)
            try:
                self._unload_plugin(p, "reload")
            finally:
                self._clear_quiescing(p)
            self._forget_plugin(name)
            report["unloaded"].append(name)

        # 2) 对磁盘上的插件文件分类：新增 / 指纹变化 / 未变化
        added, changed, kept = [], [], []
        for key, filepath in disk.items():
            p = by_file.get(key)
            if p is None:
                added.append(filepath)
                continue
            fingerprint = self._file_fingerprint(filepath)
            if fingerprint and fingerprint == p.fingerprint:
                kept.append(p)
            else:
                changed.append((p, filepath, fingerprint))

        # 3) 新增的插件文件：加载并对启用者调用 on_load
        for filepath in added:
            base = os.path.splitext(os.path.basename(filepath))[0]
            try:
                module = self._load_module(filepath)
            except Exception as e:
                traceback.print_exc()
                report["errors"].append(f"{base}: {e}")
                continue
            name = getattr(module, "NAME", base)
            p = Plugin(name, filepath, module)
            p.fingerprint = self._file_fingerprint(filepath)
            with self._lock:
                self._plugins[name] = p
            if self.is_enabled(name):
                self._call(p.module, "on_load", self.get_settings(name), self.ctx)
                p.enabled_applied = True
            report["loaded"].append(name)

        # 4) 指纹变化的插件：静默 → 等待在途调用 → on_unload → 重新加载 → on_load
        for p, filepath, fingerprint in changed:
            base = os.path.splitext(os.path.basename(filepath))[0]
            old_name = p.name
            new_p = None
            waited += self._quiesce_and_wait(p)
            try:
                self._unload_plugin(p, "reload")
                try:
                    module = self._load_module(filepath)
                except Exception as e:
                    traceback.print_exc()
                    report["errors"].append(f"{base}: {e}")
                    self._forget_plugin(old_name)
                    continue
                name = getattr(module, "NAME", base)
                new_p = Plugin(name, filepath, module)
                new_p.fingerprint = fingerprint or self._file_fingerprint(filepath)
                with self._lock:
                    self._migrate_key_locked(old_name, name)
                    self._plugins[name] = new_p
                if self.is_enabled(name):
                    self._call(new_p.module, "on_load", self.get_settings(name), self.ctx)
                    new_p.enabled_applied = True
                report["reloaded"].append(name)
            finally:
                self._clear_quiescing(p)
                if new_p is not None:
                    self._clear_quiescing(new_p)

        # 5) 指纹未变但启用状态变化的插件：只补 / 撤钩子，不重新加载模块
        for p in kept:
            if self.is_enabled(p.name) and not p.enabled_applied:
                self._call(p.module, "on_load", self.get_settings(p.name), self.ctx)
                p.enabled_applied = True
                report["loaded"].append(p.name)
            elif (not self.is_enabled(p.name)) and p.enabled_applied:
                self._unload_plugin(p, "disable")
                report["unloaded"].append(p.name)
            else:
                report["kept"].append(p.name)

        self._save_state()
        return waited

    def _force_reload_locked(self, report):
        """旧语义的全量重载：先全部 on_unload，再重新加载所有插件文件。"""
        waited = 0.0
        for p in list(self._plugins.values()):
            waited += self._quiesce_and_wait(p)
            self._unload_plugin(p, "reload")

        new_plugins = {}
        for filepath in self._plugin_files():
            base = os.path.splitext(os.path.basename(filepath))[0]
            try:
                module = self._load_module(filepath)
                name = getattr(module, "NAME", base)
                p = Plugin(name, filepath, module)
                p.fingerprint = self._file_fingerprint(filepath)
                new_plugins[name] = p
                report["reloaded"].append(name)
            except Exception as e:
                traceback.print_exc()
                report["errors"].append(f"{base}: {e}")

        with self._lock:
            old_names = set(self._plugins)
            self._plugins = new_plugins
            self._state = {k: v for k, v in self._state.items() if k in self._plugins}
            self._settings = {k: v for k, v in self._settings.items() if k in self._plugins}
        self._save_state()

        # 触发加载钩子（传入合并后的设置）
        for name, p in self._plugins.items():
            if self.is_enabled(name):
                self._call(p.module, "on_load", self.get_settings(name), self.ctx)
                p.enabled_applied = True
        report["unloaded"] = sorted(n for n in old_names if n not in new_plugins)
        return waited

    def _reload_locked(self):
        """启动时的全量加载入口（等价于 reload(force=True)），保持旧名称兼容。"""
        return self.reload(force=True)

    def _call(self, module, hook, *args, _internal=False):
        """调用插件钩子，并维护该插件的在途调用计数。

        - 插件处于静默期（quiescing）时拒绝新的钩子调用（返回 None），
          避免「旧模块正在被调用」的同时被替换；
        - _internal=True 供管理器自身的 on_unload / on_load 调用使用，
          这类调用不受静默期拒绝影响；
        - 无论正常返回还是抛出异常，退出时都会把计数减回去。
        """
        fn = getattr(module, hook, None)
        if not fn:
            return None
        plugin = None
        with self._cond:
            plugin = self._plugin_of_locked(module)
            if plugin is not None:
                if plugin.quiescing and not _internal:
                    return None
                plugin.inflight += 1
                tid = threading.get_ident()
                plugin.inflight_tids[tid] = plugin.inflight_tids.get(tid, 0) + 1
        try:
            return fn(*args)
        except Exception as e:
            print(f"插件 {getattr(module, 'NAME', '?')} 的 {hook} 出错: {e}")
            return None
        finally:
            if plugin is not None:
                self._exit_inflight(plugin)

    # ==================== 启停 ====================
    def is_enabled(self, name):
        return self._state.get(name, True)

    def enable(self, name):
        with self._reload_lock:
            with self._lock:
                if name not in self._plugins:
                    raise ValueError(f"插件不存在: {name}")
                self._state[name] = True
                p = self._plugins[name]
                settings = self.get_settings(name)
            # 文件写入与钩子放到锁外，避免阻塞其它插件钩子
            self._save_state()
            self._call(p.module, "on_load", settings, self.ctx)
            p.enabled_applied = True
        return True

    def disable(self, name):
        with self._reload_lock:
            with self._lock:
                if name not in self._plugins:
                    raise ValueError(f"插件不存在: {name}")
                self._state[name] = False
                p = self._plugins[name]
            self._save_state()
            # 静默该插件并等待在途钩子结束，避免停用与执行中的调用竞争
            self._quiesce_and_wait(p)
            try:
                self._unload_plugin(p, "disable")
            finally:
                self._clear_quiescing(p)
        return True

    # ==================== 设置 ====================
    def get_settings(self, name):
        p = self._plugins.get(name)
        defaults = getattr(p.module, "SETTINGS", {}) if p else {}
        stored = self._settings.get(name, {})
        merged = dict(defaults)
        merged.update(stored if isinstance(stored, dict) else {})
        return merged

    def save_settings(self, name, patch):
        if not isinstance(patch, dict):
            raise ValueError("设置必须是字典")

        with self._lock:
            if name not in self._plugins:
                raise ValueError(f"插件不存在: {name}")
            module = self._plugins[name].module
            # 插件可声明 validate_settings 拒绝在特定状态下修改（如对话期间）
            validator = getattr(module, "validate_settings", None)
            if validator:
                try:
                    ok, msg = validator(patch, self.ctx)
                except Exception:
                    ok, msg = True, ""
                if not ok:
                    raise ValueError(msg)
            current = dict(self._settings.get(name, {}))
            current.update(patch)
            self._settings[name] = current
            merged = dict(getattr(module, "SETTINGS", {}))
            merged.update(current)

        self._save_settings()
        # 插件钩子返回的字典是该插件对设置的「校验 / 规范化」结果（例如背景设置会在
        # 非图片模式下清空图片路径、把非法模式改回深色）。此前返回值被丢弃，规范化结果
        # 只作用于界面、不会落盘，下次启动又读回未规范化的旧值；这里把它合并回存储再保存。
        normalized = self._call(module, "on_settings_changed", merged, self.ctx)
        if isinstance(normalized, dict):
            with self._lock:
                stored = dict(self._settings.get(name, {}))
                # 只接受「本次提交的键 + 原本已存的键」：不引入插件默认值，
                # 插件主动 pop 掉的旧字段（如旧版 opacity / overlay）会随之被清除
                allowed = set(stored) | set(patch)
                cleaned = {k: v for k, v in normalized.items() if k in allowed}
                changed = cleaned != stored
                if changed:
                    self._settings[name] = cleaned
            if changed:
                self._save_settings()
            merged = dict(getattr(module, "SETTINGS", {}))
            merged.update(cleaned)
        return merged

    # ==================== 列表 ====================
    def list_plugins(self):
        result = []
        for name, p in self._plugins.items():
            schema = p.settings_schema()
            item = {
                "name": name,
                "version": p.version,
                "description": p.description,
                "author": p.author,
                "official": bool(getattr(p.module, "OFFICIAL", False)),
                "hot_swap": bool(getattr(p.module, "HOT_SWAP", False)),
                "has_state": bool(getattr(p.module, "get_state", None)),
                "enabled": self.is_enabled(name),
                "file": os.path.basename(p.filepath),
                "commands": p.commands(),
                "actions": p.actions(),
                "settings_schema": schema,
                "settings": self.get_settings(name) if schema else {},
                # 「设置」页左侧栏分类标签（插件可用 SETTINGS_TAB / settings_tab() 自定义，
                # 也可以自带页面：{"type": "page", "page": "/xxx.html"}）
                "settings_tab": p.settings_tab(self.ctx),
                # 插件声明的重载策略（可选，缺省 {}）与当前是否正在静默 / 重载中，
                # 供前端判断这次重载会不会清理临时上下文。
                "reload_policy": p.reload_policy(),
                "changing": bool(p.quiescing),
            }
            result.append(item)
        return result

    # ==================== 钩子：消息 / 命令 ====================
    def _enabled_plugins(self):
        with self._lock:
            # 跳过处于静默期的插件：静默期间不再向其分发新的消息 / 命令 / 动作 / LLM 钩子
            return [p for p in self._plugins.values()
                    if self.is_enabled(p.name) and not p.quiescing]

    def handle_command(self, text, mode):
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return None
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/help", "/插件", "/plugins"):
            return self._help_result()

        for plugin in self._enabled_plugins():
            result = self._call(plugin.module, "on_command", cmd, args, self.ctx)
            if result:
                return self.normalize(result)
        return None

    def handle_message(self, user_text, mode):
        for plugin in self._enabled_plugins():
            result = self._call(plugin.module, "on_message", user_text, mode, self.ctx)
            if result:
                return self.normalize(result)
        return None

    def post_process(self, user_text, mode, result):
        for plugin in self._enabled_plugins():
            self._call(plugin.module, "after_message", user_text, mode, result, self.ctx)
        return result

    def on_system_prompt(self, prompt):
        """允许插件在系统提示词上追加内容（如用户信息），返回修改后的提示词。"""
        for plugin in self._enabled_plugins():
            out = self._call(plugin.module, "on_system_prompt", prompt, self.ctx)
            if isinstance(out, str):
                prompt = out
        return prompt

    def run_action(self, plugin_name, action_name):
        """执行插件的指定动作（供 Web UI 一键触发），返回标准化结果或 None。"""
        p = self._plugins.get(plugin_name)
        if not p or not self.is_enabled(plugin_name):
            raise ValueError(f"插件不存在或未启用: {plugin_name}")
        result = self._call(p.module, "on_action", action_name, self.ctx)
        return self.normalize(result) if result else None

    # ==================== 钩子：大模型调用 ====================
    def pre_llm(self, messages, meta):
        """返回 dict（{"reply": ...}）表示短路；None 表示继续正常调用。"""
        for plugin in self._enabled_plugins():
            result = self._call(plugin.module, "pre_llm", messages, meta, self.ctx)
            if isinstance(result, dict):
                return result
        return None

    def post_llm(self, reply, meta):
        """逐个执行后处理，返回（可能被修改的）回复文本。"""
        for plugin in self._enabled_plugins():
            out = self._call(plugin.module, "post_llm", reply, meta, self.ctx)
            if isinstance(out, str):
                reply = out
        return reply

    # ==================== 工具 ====================
    def _help_result(self):
        lines = ["可用命令："]
        for p in self._enabled_plugins():
            for c in p.commands():
                name = c.get("name", "") if isinstance(c, dict) else str(c)
                desc = c.get("desc", "") if isinstance(c, dict) else ""
                args = c.get("args", "") if isinstance(c, dict) else ""
                line = f"  {name} {args}".strip()
                if desc:
                    line += f" — {desc}"
                lines.append(line)
        lines.append("  也可以直接用自然语言提问。")
        return self.normalize({"reply": "\n".join(lines), "speak": False})

    def normalize(self, result):
        if not isinstance(result, dict):
            return None
        out = {
            "ok": True,
            "action": result.get("action", "chat"),
            "user_text": result.get("user_text", ""),
            "reply": result.get("reply", ""),
            "speak": bool(result.get("speak", False)),
            "need_online": False,
            "search_keyword": None,
            "music_keyword": None,
            "music_videos": [],
            "music_control": result.get("music_control"),
            "skip_reason": None,
            "from_plugin": True,
        }
        # 透传插件自定义字段（如 music 播放指令 / music_plugin 归属 / 多人对话音频 / 流式会话 /
        # page 打开新页面 / modal 打开浮层窗口（如记忆管理）/ confirm 二级确认（前端确认后调用 confirm 指定的执行动作））
        for key in ("music", "music_plugin", "multi_audio", "multi_stream_id", "page", "modal", "confirm"):
            if key in result:
                out[key] = result[key]
        return out

    def get_state(self, name):
        """返回插件的动态状态（供前端展示，如播放列表队列）。"""
        p = self._plugins.get(name)
        if not p:
            return {}
        return self._call(p.module, "get_state", self.ctx) or {}

    def plugin_available(self, name):
        """询问某个插件「现在可用吗」——插件之间能力对接的统一握手（可用性声明）。

        插件可实现 `available(ctx)` 返回：
            {"available": bool, "reason": "不可用原因", "note": "补充说明", ...自定义字段}
        例如「多人对话」用它告诉设置页的角色管理：勾选满 2 个角色才算可用（否则单人输出）。
        统一规则：插件不存在 / 未启用 / 正在静默重载 → 一律不可用；
        插件未实现 available() → 视为可用（向后兼容）。
        """
        p = self._plugins.get(name)
        if not p:
            return {"available": False, "enabled": False, "reason": f"插件不存在：{name}"}
        if not self.is_enabled(name):
            return {"available": False, "enabled": False,
                    "reason": f"「{name}」插件未启用（可在「插件管理」中启用）"}
        if getattr(p, "quiescing", False):
            return {"available": False, "enabled": True,
                    "reason": f"「{name}」插件正在重载，请稍后再试"}
        fn = getattr(p.module, "available", None)
        if not callable(fn):
            return {"available": True, "enabled": True, "reason": ""}
        try:
            out = fn(self.ctx)
        except Exception as e:
            return {"available": False, "enabled": True, "reason": f"可用性检查失败：{e}"}
        if not isinstance(out, dict):
            out = {"available": bool(out)}
        out["enabled"] = True
        out["available"] = bool(out.get("available"))
        out.setdefault("reason", "" if out["available"] else "插件声明当前不可用")
        return out

    def plugin_module(self, name):
        """返回指定插件的模块对象（供 Web 层调用插件能力），不存在返回 None。"""
        p = self._plugins.get(name)
        return p.module if p else None


# 进程内单例
manager = PluginManager()
