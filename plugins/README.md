# 插件开发指南

插件目录（`plugins/`）下的每个 `.py` 文件都是一个插件，第三方开发者可以：

- **新增**：把一个 `.py` 文件丢进 `plugins/`，在 Web UI 里点“重新加载插件”即可生效；
- **修改**：直接编辑文件后点“重新加载插件”（热重载，无需重启）；
- **删除**：删除文件后点“重新加载插件”；
- **启停**：在 Web UI 的“插件管理”里开关某个插件，无需删除文件。

## 最小插件示例

```python
NAME = "示例插件"
VERSION = "1.0.0"
DESCRIPTION = "这是一个最小示例"
AUTHOR = "你的名字"

def on_message(user_text, mode, ctx):
    if user_text == "你好啊":
        return {"reply": "你好呀！我是插件。", "speak": True}
    return None   # 返回 None 表示不处理，交给主流程

def commands():
    return [{"name": "/hello", "desc": "打个招呼", "args": ""}]

def on_command(command, args, ctx):
    if command == "/hello":
        return {"reply": "你好呀！", "speak": True}
    return None
```

## 钩子一览

| 钩子 | 说明 |
| --- | --- |
| `on_load(settings, ctx)` | 插件加载 / 启用时调用，`settings` 为合并后的设置 |
| `on_unload(ctx)` | 插件卸载 / 停用时调用 |
| `on_message(user_text, mode, ctx)` | 消息进入主流水线前调用；返回 `dict` 表示处理完毕 |
| `on_command(command, args, ctx)` | 处理以 `/` 开头的命令；返回 `dict` 或 `None` |
| `commands()` | 返回命令列表：`[{"name": "/xx", "desc": "...", "args": "..."}]` |
| `after_message(user_text, mode, result, ctx)` | 主流水线处理完后调用，可原地修改 `result` |
| `pre_llm(messages, meta, ctx)` | 每次大模型调用前调用；返回 `{"reply": ...}` 可短路本次调用 |
| `post_llm(reply, meta, ctx)` | 每次大模型调用后调用；返回新的回复文本 |
| `on_system_prompt(prompt, ctx)` | 在系统提示词上追加内容，返回修改后的完整提示词 |
| `actions()` | 返回动作列表：`[{"name": "...", "label": "...", "desc": "..."}]` |
| `on_action(name, ctx)` | 处理 Web UI 一键动作，返回结果 `dict` |
| `settings_tab(ctx)` | （可选）声明「设置」页左侧标签：`{"label", "order", "type", "page", "desc"}` |
| `available(ctx)` | （可选）可用性声明：告诉其它功能「我现在能不能用」，如 `{"available": False, "reason": "..."}` |
| `ctx.unload_reason` | `on_unload` 期间可读：`"disable"`（被停用）/ `"reload"`（被热重载），据此决定是否保留自身状态 |

> **贡献浮层窗口**：动作除返回 `reply` 外还可以返回 `{"modal": "名字"}`，
> 前端会在 `window.XLLB_MODALS` 中查找同名函数并调用（例如「上下文记忆库」的
> `{"modal": "memory_manager"}` 会弹出记忆管理浮层窗口），不需要新开页面。
> 约定：脚本自行 `window.XLLB_MODALS = window.XLLB_MODALS || {}` 后注册，并保证重复调用只打开一个窗口。

> 声明了 `actions()` 或设置分组里的 `actions` 时，界面会按「标签 + 说明 — 按钮」逐行渲染，
> 因此动作的 `label` 可以写完整（不需要为了排版而缩写），`desc` 会作为说明显示在左侧。

`mode` 为 `"qa"`（问答）或 `"live"`（实时）。
`meta` 为 `{"purpose", "model", "backend", "user_text"}`，`purpose` 可为
`chat` / `judge` / `search_keyword` / `music_keyword` / `music_intent` 等。

模块里可写 `OFFICIAL = True` 标记为官方插件（否则在 UI 中显示为“第三方”）。
可写 `HOT_SWAP = True` 标记为「热切换」插件（可随时在对话进行中启用 / 停用）；
不标记则建议仅在非对话期间切换（例如会影响提示词或模型的插件）。
还可写 `RELOAD_POLICY`（字典）声明自己的重载语义，供 UI 提示与插件管理器参考：

```python
RELOAD_POLICY = {
    "preserve_context": True,     # 重载时保留自身会话上下文（如记忆引擎的 L0）
    "allow_during_chat": True,    # 是否允许对话进行中重载
    "note": "重载只重启插件代码，保留引擎实例与 L0 会话上下文",
}
```

### 重载与停用语义（重要）

- **重载是增量的**：只有文件内容发生变化的插件才会 `on_unload` → 重新加载 → `on_load`；
  未变化的插件完全不会被触碰（不卸载、不重新执行、不回调 `on_load`）。
- 重载 / 停用某个插件前，管理器会先让该插件**静默**（停止向它分发新的消息、命令、动作与 LLM 钩子），
  并等待它的**在途钩子调用**结束（最多 5 秒）后再调用 `on_unload`，避免「旧模块正在被调用却被替换」。
- 通过 `ctx.unload_reason` 区分「停用」与「重载」：需要保留运行时状态（数据库连接、会话缓存）的插件，
  应在 `on_unload` 里只停止自己的后台线程、保留状态，把真正的资源释放留给进程退出
  （应用退出会调用统一退出流程，插件可在 `on_unload` 里判断 `ctx.unload_reason` 为空/`"exit"` 时彻底关闭）。
- 静默期间（`list_plugins()` 里 `changing=True`）前端应避免对该插件执行动作 / 保存设置。

## 可用性声明：和别的功能对接（`available(ctx)`）

插件如果需要被别人「确认能不能用」（例如「多人对话」要告诉设置页的角色管理：勾选满 2 个角色才算激活），
可以实现 `available(ctx)`，返回统一结构：

```python
def available(ctx):
    slots = _configured_slots(ctx)
    if len(slots) < 2:
        return {"available": False, "reason": "尚未勾选足够的参与角色（至少 2 个）",
                "note": "当前为单人输出", "selected": [], "min_roles": 2}
    return {"available": True, "reason": "", "note": f"已激活：{'、'.join(slots)}",
            "selected": slots, "min_roles": 2}
```

调用方统一通过 `core.plugin_manager.manager.plugin_available(插件名)` 获取，管理器会补齐这些规则：

- 插件不存在 / **未启用** / 正在静默重载 → 一律 `available=False` 并给出中文原因（不会误激活）；
- 插件未实现 `available()` → 视为可用（向后兼容）；
- 插件抛异常 → `available=False` + 失败原因（不会把异常抛给调用方）。

这样「能力是否生效」由插件自己声明、由管理器统一裁决，调用方（如设置页）只需按结果展示
「已激活 / 单人输出」，不需要了解插件内部细节。

## 返回结果格式

钩子返回的 `dict` 支持（都有默认值）：

```python
{"reply": "回复文本", "speak": True, "action": "chat"}
```

`speak=True` 时回复会走流式语音合成。

## 插件设置（带 UI 表单）

插件可声明 `SETTINGS`（默认值）、`settings_schema()`（表单字段）与
`on_settings_changed(settings, ctx)`（保存后回调），Web UI 会自动渲染表单：

```python
SETTINGS = {"api_key": "", "model": "gpt-4o-mini"}

def settings_schema():
    return [
        {"key": "model", "label": "模型", "type": "text"},
        {"key": "api_key", "label": "API Key", "type": "password"},
    ]

def on_settings_changed(settings, ctx):
    # 用户保存后调用
    ctx.config.SOME_VALUE = settings["api_key"]
```

`type` 支持 `text` / `password` / `number` / `select` / `datalist`
（`select` 与 `datalist` 需提供 `options`；`datalist` 是可输入 + 下拉选择的组合）。
字段也可写成折叠分组：`{"type": "section", "label": "高级选项", "fields": [...], "actions": [...]}`。

插件参数设置统一出现在 Web UI 的「设置」页左侧栏（每个有设置的插件自动生成一个同名标签），
「插件管理」标签只负责启用 / 停用，不再承载参数设置。

## 在「设置」页新增自己的标签（可选）

插件默认会得到一个同名标签（渲染上面的设置表单 / 动作 / 状态）；
如果想自定义标签名称、顺序，或想让标签指向插件自带的页面，可声明 `SETTINGS_TAB`
（字典）或 `settings_tab(ctx)`（函数，返回字典，优先级更高）：

```python
SETTINGS_TAB = {"label": "音乐", "order": 55}

# 自带页面的标签：右侧给出入口按钮，点击打开该页面
def settings_tab(ctx):
    return {"label": "音乐", "type": "page", "page": "/music.html",
            "desc": "管理音乐播放列表与音源"}
```

| 字段 | 说明 |
| --- | --- |
| `label` | 左侧标签文字，默认取插件名 |
| `icon` | 保留字段：当前版本左侧栏为纯文字标签，图标不会显示（写法保留以兼容后续版本） |
| `order` | 排序，越小越靠前；内置分类为 10/20/30/40/50，插件标签默认 60 |
| `type` | `inline`（默认，渲染插件的设置 / 动作 / 状态）或 `page`（插件自带页面） |
| `page` | `type="page"` 时的页面地址（放在 `web/` 目录下即可被服务，如 `/music.html`） |
| `desc` | 右侧内容顶部的说明文字 |

停止（未启用）的插件仍会显示标签，但标签上会标记「已停用」，参数保存后启用即生效。
`settings_tab(ctx)` 会在每次列表 / 刷新时调用，应保持“无副作用、快速返回”。

## 上下文 ctx

`ctx` 提供受控的能力接口：

- `ctx.config` —— 全局配置模块（可读可写运行时状态）；
- `ctx.manager` —— 插件管理器（如 `ctx.manager.save_settings(name, patch)` 持久化设置）；
- `ctx.history()` —— 当前对话记录；
- `ctx.call_llm(text, extra_context="")` —— 调用生成模型对话；
- `ctx.generate(prompt, ...)` —— 调用判断模型做提示词生成；
- `ctx.judge(user_text)` —— 判断是否需要联网；
- `ctx.search(query)` —— Tavily 联网搜索；
- `ctx.music_search(query)` —— B 站音乐搜索；
- `ctx.synthesize(text)` —— 合成语音，返回音频路径列表；
- `ctx.list_models()` —— 列出本地 Ollama 模型；
- `ctx.check_backend()` —— 当前模型后端是否可用；
- `ctx.log(*args)` —— 打印日志。

> 插件是本地可信代码（可 `import` 任意模块），`ctx` 只是便捷封装。
> 应保持“无副作用、快速返回”，长时间任务请自行开线程；加载失败不影响主程序。
