# 小笼洛包

基于本地大语言模型（Ollama）的聊天软件，支持文字 / 语音对话、联网搜索、语音合成（GPT-SoVITS）、
B 站点歌、角色与语音预设管理、对话记录保存等功能。输入输出界面已统一集成到 **Web UI**。

## 目录结构

```
Chat-bot/
├── start.bat               # 一键启动脚本（推荐）
├── launcher.py             #   一键启动逻辑：拉起 Ollama / GPT-SoVITS / Whisper / Web
├── app.py                  #   仅启动 Web 服务（假设外部服务已运行）
├── launcher_config.json    #   服务地址 / 启动脚本路径配置
├── requirements.txt        #   完整依赖清单
├── core/                   # 核心逻辑（模块化后端，可独立复用）
│   ├── config.py           #   路径、默认值、全局状态
│   ├── models.py           #   Whisper 语音识别模型
│   ├── audio.py            #   文本清洗 / 分句 / 音频解码转写
│   ├── llm.py              #   Ollama 大模型客户端
│   ├── judge.py            #   是否联网判断
│   ├── search.py           #   Tavily 联网搜索
│   ├── character.py        #   角色预设管理
│   ├── tts.py              #   GPT-SoVITS 语音合成与预设
│   ├── music.py            #   B 站音乐搜索 / 下载
│   ├── storage.py          #   对话记录保存
│   ├── services.py         #   外部服务生命周期管理（启动 / 等待就绪）
│   ├── runtime.py          #   运行时缓存清理
│   ├── plugin_manager.py   #   插件系统（动态加载 / 启停 / 热重载）
│   └── pipeline.py         #   对话处理流水线（QA + Live 共用）
├── web/                    # Web UI 层
│   ├── server.py           #   HTTP 服务 + JSON API（标准库实现）
│   ├── index.html          #   页面
│   └── static/             #   style.css / app.js
├── plugins/                # 插件（第三方开发者可增删改，Web UI 热重载）
├── presets/                # 角色预设（*.json）
├── voice_presets/          # 语音预设（参考音频 + 权重）
├── models/whisper/         # Whisper 模型文件
├── runtime/                # 运行时生成（合成语音、临时音乐，自动创建）
├── save-au/  save-wo/      # 保存的对话（音频 / 文本）
└── legacy/                 # 归档：重构前的 CLI 代码与测试脚本
```

## 快速开始

### 一键启动（推荐）

双击 `start.bat`，或在命令行运行：

```bash
venv\Scripts\python.exe launcher.py
```

它会自动：启动 Ollama → 启动 GPT-SoVITS API → 预加载 Whisper → 打开浏览器进入 Web 界面。

> 首次使用请先编辑 `launcher_config.json`，把 `gpt_sovits.api_script` 改成你自己的
> `api_v2.py` 路径（默认已填入示例路径），并按需调整端口 / 地址。

### 仅启动 Web（服务已手动运行时）

```bash
venv\Scripts\python.exe app.py
venv\Scripts\python.exe app.py --port 9000
```

### 安装依赖

```bash
venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 使用说明

界面顶部可切换两种模式：

- ** 问答模式**：输入文字或点击 🎤 单次录音（静音自动停止）。
- ** 实时模式**：点击 🎤 后连续聆听，逐句对话；说“退出”结束、“暂停”暂停、“继续”恢复。

设置面板可管理：角色（默认 / 加载预设 / 自定义 + 联网搜特点）、语音预设、影响力范围、音量、
联网开关、调试模式、Tavily 额度、对话记录（查看 / 标记 / 保存 / 清空）。

点歌：直接说“播放 晴天”“来一首 想见你”等，从列表中选择即可播放；
“暂停音乐 / 继续音乐 / 停止音乐”可控制播放。

### 播放与缓存

- **流式语音播放**：回复语音按句流式合成，第一句生成后即开始播放，后续句子边生成边播放，降低等待感。
- **实时模式与音乐**：音乐播放期间彻底禁用语音识别（避免拾取音乐声与音频设备抢占），音乐播放结束后自动恢复。
- **缓存自动清理**：合成语音与临时音乐存放在 `runtime/`，程序退出时自动清空，运行中也会定期清理过期文件。

## 插件系统

`plugins/` 目录下的每个 `.py` 文件都是一个插件，支持**热加载**（无需重启）：

- 在 Web UI 的「🔌 插件管理」中查看、启用 / 停用插件，点“重新加载插件”即可让新增 / 修改的插件生效；
- 插件可声明**设置表单**（在 UI 中直接填写并保存）、可拦截 / 改写每一次大模型调用（`pre_llm` / `post_llm`）；
- 第三方开发者把插件文件放进 `plugins/` 即可扩展功能，详见 `plugins/README.md`。

内置插件（在 UI 中以「官方 / 第三方」标注区分）：

| 插件 | 说明 |
| --- | --- |
| 模型与自动调优（官方） | 模型 / API 设置 + 一键自动调优；生成与判断的后端、模型可分别设置 |
| 用户信息设置（官方） | 设置用户称呼 / 偏好等，实时融入系统提示词 |
| 记事本 | “记住 …”“我记了什么”或 `/memo` |
| 翻译 | “翻译 …”或 `/translate`（中英自动判断） |
| 快捷设置 | “关闭联网”/“开启联网”/“清空对话”或 `/net`、`/clear` |
| 示例问候 | 说“你好啊”或 `/hello`（插件开发模板） |
| 时间日期 | “现在几点”或 `/time`、`/date` |
| 对话导出 | `/export` 导出为 Markdown |

输入 `/help` 可查看所有可用命令。

### 模型与自动调优

支持两种大模型后端（Ollama / OpenAI 兼容 API），**生成模型与判断模型的后端、模型名均可分别设置**：

- **Ollama（本地）**：模型下拉框会**列出已安装的 Ollama 模型**供选择（如 `qwen3.5:9b`、`qwen3.5:2b`）；
- **OpenAI 兼容 API**：填 API Base URL 与 Key（或读环境变量 `OPENAI_API_KEY`），即可用任意兼容服务；
- **一键自动调优**：检测显存 / 内存 / CPU，自动推荐并应用合适的模型配置（显存不足时提示改用更小模型或 API）；
- **省显存**：当生成模型与判断模型相同时，Ollama 只会加载一次模型权重（`/model` 会提示“仅加载一次”）。

也可用命令：`/model <模型名>`、`/judge <模型名>`、`/backend ollama|openai`、`/jbackend ollama|openai`、`/autotune [apply]`、`/sysinfo`。

### 前置依赖

- **Ollama**：本地大模型服务，默认模型 `qwen3.5:9b`（在 `core/config.py` 修改 `OLLAMA_MODEL`）。
- **GPT-SoVITS API**：语音合成服务（`launcher_config.json` 配置 `api_script`，默认端口 9880）。
- **Whisper 模型**：`models/whisper` 下的 faster-whisper 模型文件。
- **ffmpeg**：点歌下载转码需要（路径见 `core/config.py` 的 `FFMPEG_PATH`）。

## Web API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat` | 发送消息（`mode` 可传 `qa`/`live`），返回回复文本与语音 URL |
| POST | `/api/transcribe` | 上传 WAV 音频，返回识别文本 |
| GET | `/api/status` | 服务就绪状态（Ollama / TTS / Whisper） |
| GET/POST | `/api/settings` | 读取 / 修改设置 |
| GET/POST | `/api/character` | 读取 / 切换角色 |
| GET/POST | `/api/voice_preset` | 读取 / 应用 / 创建语音预设 |
| GET/POST | `/api/history` | 读取 / 标记 / 清空对话记录 |
| POST | `/api/save` | 保存对话（文本 + 语音） |
| GET/POST | `/api/usage` | Tavily 额度查询 / 重置 |
| GET | `/api/music/search` | B 站音乐搜索 |
| POST | `/api/music/play` | 下载并返回音乐音频 URL |
| POST | `/api/music/ended` | 音乐播放完毕的角色提示 |
| POST | `/api/music/stop` | 停止并清理音乐 |
| GET | `/api/plugins` | 插件列表（名称 / 版本 / 命令 / 启停状态） |
| POST | `/api/plugins/reload` | 热重载所有插件（扫描 plugins/ 目录） |
| POST | `/api/plugins/enable` | 启用插件 |
| POST | `/api/plugins/disable` | 停用插件 |
| POST | `/api/plugins/settings` | 保存插件设置 |
| GET | `/api/tts/next?id=` | 流式 TTS：长轮询拉取下一句语音 |

## 常见问题

- **语音合成失败**：确认 GPT-SoVITS API 已启动（看 `launcher_config.json` 的 `api_script` 是否正确），
  且 `voice_presets` 中的参考音频路径正确。
- **语音识别失败**：确认 `models/whisper` 模型完整，浏览器已授权麦克风。
- **联网搜索失败**：检查 `core/config.py` 的 `TAVILY_API_KEY` 与额度。
- **点歌下载失败**：确认 `ffmpeg` 路径正确且网络可访问 B 站。

qq交流群：743918078
