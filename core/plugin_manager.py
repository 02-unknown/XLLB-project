# core/plugin_manager.py
# 轻量插件系统：扫描 plugins/ 目录，动态加载 / 重载 / 启停插件，
# 并为第三方插件开放丰富的扩展点与上下文能力。
#
# 插件约定（详见 plugins/README.md）：
#   - 一个 .py 文件即一个插件，可定义 NAME/VERSION/DESCRIPTION/AUTHOR；
#   - 可选钩子：on_load / on_unload / on_message / on_command / commands /
#               after_message / pre_llm / post_llm；
#   - 可选设置：SETTINGS（默认值字典）+ settings_schema() + on_settings_changed()。
import importlib.util
import json
import os
import sys
import threading
import traceback

import core.config as config
from core import storage


class _Context:
    """传递给插件的上下文，暴露受控的能力接口。"""

    def __init__(self):
        self.config = config

    @staticmethod
    def history():
        return storage.get_history()

    @staticmethod
    def call_llm(text, extra_context=""):
        from core import llm
        return llm.call_ollama(text, extra_context=extra_context)

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


class PluginManager:
    """插件管理器（进程内单例）。"""

    def __init__(self):
        self._lock = threading.RLock()
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
            print(f"⚠️ 保存插件状态失败: {e}")

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
            print(f"⚠️ 保存插件设置失败: {e}")

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
        modname = f"_chatbot_plugin_{base}"
        spec = importlib.util.spec_from_file_location(modname, filepath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        return module

    def _reload_locked(self):
        # 触发卸载钩子
        for p in self._plugins.values():
            self._call(p.module, "on_unload", self.ctx)

        new_plugins = {}
        errors = []
        for filepath in self._plugin_files():
            base = os.path.splitext(os.path.basename(filepath))[0]
            try:
                module = self._load_module(filepath)
                name = getattr(module, "NAME", base)
                new_plugins[name] = Plugin(name, filepath, module)
            except Exception as e:
                traceback.print_exc()
                errors.append(f"{base}: {e}")

        self._plugins = new_plugins
        self._state = {k: v for k, v in self._state.items() if k in self._plugins}
        self._settings = {k: v for k, v in self._settings.items() if k in self._plugins}
        self._save_state()

        # 触发加载钩子（传入合并后的设置）
        for name, p in self._plugins.items():
            if self.is_enabled(name):
                self._call(p.module, "on_load", self.get_settings(name), self.ctx)

        print(f"🔌 已加载 {len(self._plugins)} 个插件。")
        if errors:
            print(f"⚠️ {len(errors)} 个插件加载失败: {errors}")
        return errors

    def reload(self):
        with self._lock:
            return self._reload_locked()

    def _call(self, module, hook, *args):
        fn = getattr(module, hook, None)
        if not fn:
            return None
        try:
            return fn(*args)
        except Exception as e:
            print(f"插件 {getattr(module, 'NAME', '?')} 的 {hook} 出错: {e}")
            return None

    # ==================== 启停 ====================
    def is_enabled(self, name):
        return self._state.get(name, True)

    def enable(self, name):
        with self._lock:
            if name not in self._plugins:
                raise ValueError(f"插件不存在: {name}")
            self._state[name] = True
            module = self._plugins[name].module
            settings = self.get_settings(name)
        # 文件写入与钩子放到锁外，避免阻塞其它插件钩子
        self._save_state()
        self._call(module, "on_load", settings, self.ctx)
        return True

    def disable(self, name):
        with self._lock:
            if name not in self._plugins:
                raise ValueError(f"插件不存在: {name}")
            self._state[name] = False
            module = self._plugins[name].module
        self._save_state()
        self._call(module, "on_unload", self.ctx)
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
        self._call(module, "on_settings_changed", merged, self.ctx)
        return merged

    # ==================== 列表 ====================
    def list_plugins(self):
        result = []
        for name, p in self._plugins.items():
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
                "settings_schema": p.settings_schema(),
                "settings": self.get_settings(name) if p.settings_schema() else {},
            }
            result.append(item)
        return result

    # ==================== 钩子：消息 / 命令 ====================
    def _enabled_plugins(self):
        with self._lock:
            return [p for p in self._plugins.values() if self.is_enabled(p.name)]

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
        # 透传插件自定义字段（如 music 播放指令 / music_plugin 归属）
        for key in ("music", "music_plugin"):
            if key in result:
                out[key] = result[key]
        return out

    def get_state(self, name):
        """返回插件的动态状态（供前端展示，如播放列表队列）。"""
        p = self._plugins.get(name)
        if not p:
            return {}
        return self._call(p.module, "get_state", self.ctx) or {}


# 进程内单例
manager = PluginManager()
