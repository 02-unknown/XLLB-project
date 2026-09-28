# tests/ —— 验证脚本

这里的每个 `verify_*.py` 都是**可重复运行的功能验收脚本**：只读检查 + 真实跑接口 / 真实起服务，
自查一项功能是否按设计工作，末尾打印「通过 N 项，失败 M 项」并以退出码表示成败。
（`verify_*.js` 是给 Python 脚本调用的 DOM 桩 / 渲染冒烟脚本，由对应的 `verify_*.py` 唤起。）

## 一键跑全部

```bash
venv\Scripts\python.exe tests\run_all.py                 # 全部（跳过需要真实窗口的那个）
venv\Scripts\python.exe tests\run_all.py --all           # 连真实窗口脚本一起跑（会短暂弹出应用窗口）
venv\Scripts\python.exe tests\run_all.py verify_voice_flow verify_music_player   # 只跑指定几个
```

## 单个脚本

```bash
venv\Scripts\python.exe tests\verify_voice_flow.py
```

脚本内部一律用「本文件所在目录的上一级」当仓库根目录，因此**可以任意位置调用**，
不依赖当前工作目录，也不再写死 `<project-root>`。

## 主要脚本一览

| 脚本 | 覆盖内容 |
| --- | --- |
| `verify_voice_flow.py` | 语音/状态胶囊、播报进度条、生成期间不接收语音输入、流式 TTS 接口 |
| `verify_quiet_skip.py` | 语音噪音「先判断再输出」：不显示、不撤回、不写历史 |
| `verify_music_player.py`(+`.js`) | 音乐进度条/拖动跳转、Range 分片、LUFS 均衡、设置浮层滑动与状态胶囊状态机 |
| `verify_voice_ui_dom.py` | 真实 Edge 无头跑页面：胶囊常驻、麦克风录完音不再卡在「聆听」 |
| `verify_settings_page.py`(+2 个 js) | 设置页结构 / 接口 / 渲染冒烟 / 浮层嵌入模式 |
| `verify_app_close.py`、`verify_close_e2e.py` | 关窗口＝完整清理（窗口进程检查点、独立清理进程、限时收尾） |
| `verify_real_window_close.py` | 真实 Edge 窗口 + 真实 GPT-SoVITS：关窗后显存回落（默认跳过） |
| `verify_service_cleanup.py`、`verify_model_unload.py` | 服务进程与显存清理、模型卸载 |
| `verify_memory_*`、`verify_context.py`、`verify_archive_value.py` | 记忆门禁、归档价值、上下文检索 |
| `verify_plugin*.py` | 插件系统（启停 / 热重载 / 增量重载 / 独立性与可用性握手） |
| `verify_perf` 类（`perf_*.py`） | 设置页加载耗时、上下文压力测试（不参与 run_all 汇总） |

> 说明：部分脚本会临时启动本机服务或占用端口（如 10999），运行前请先关掉正在运行的程序实例；
> 脚本结束时会自行清理进程与临时文件。
