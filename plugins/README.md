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

`mode` 为 `"qa"`（问答）或 `"live"`（实时）。
`meta` 为 `{"purpose", "model", "backend", "user_text"}`，`purpose` 可为
`chat` / `judge` / `search_keyword` / `music_keyword` / `music_intent` 等。

模块里可写 `OFFICIAL = True` 标记为官方插件（否则在 UI 中显示为“第三方”）。
可写 `HOT_SWAP = True` 标记为「热切换」插件（可随时在对话进行中启用 / 停用）；
不标记则建议仅在非对话期间切换（例如会影响提示词或模型的插件）。

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
