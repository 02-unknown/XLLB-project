# core/__init__.py
# 核心逻辑包：把原本散落在根目录的模块统一收纳，便于修改与复用。
import sys

# 统一 stdout/stderr 为 UTF-8，避免 Windows 控制台 GBK 编码导致 emoji 打印崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 各子模块职责单一：
#   config         —— 路径、默认值与全局运行时状态
#   models         —— Whisper 语音识别模型（懒加载）
#   audio          —— 文本清洗、分句、音频解码与转写
#   llm            —— Ollama 大模型客户端（对话 / 关键词 / 音乐意图）
#   judge          —— 是否需要联网的轻量判断
#   search         —— Tavily 联网搜索
#   character      —— 角色预设管理
#   tts            —— GPT-SoVITS 语音合成与语音预设
#   music          —— Bilibili 音乐搜索 / 下载
#   storage        —— 对话记录保存（文本 + 语音）
#   pipeline       —— 对话处理流水线（QA + Live 共用）
#   services       —— 外部服务生命周期管理
#   runtime        —— 运行时缓存清理
#   plugin_manager —— 插件系统（动态加载 / 启停 / 热重载）

__version__ = "1.3.0"
