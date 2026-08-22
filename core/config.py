# core/config.py
# 路径、默认值以及全局运行时状态。
# 所有路径均基于项目根目录推导，避免把绝对路径写死在代码里。
import os

# ========== 项目根目录 ==========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ========== 路径配置 ==========
WHISPER_MODEL_DIR = os.path.join(PROJECT_ROOT, "models", "whisper")

# ========== 大模型设置（可由官方“模型与自动调优”插件 / Web UI 热切换） ==========
OLLAMA_MODEL = "qwen3.5:9b"        # Ollama 默认模型（向后兼容）
LLM_CHAT_BACKEND = "ollama"        # 生成模型的运行后端："ollama" 或 "openai"
LLM_JUDGE_BACKEND = "ollama"       # 判断模型的后端："ollama" 或 "openai"（可单独设置）
LLM_CHAT_MODEL = OLLAMA_MODEL      # 生成 / 对话模型
LLM_JUDGE_MODEL = OLLAMA_MODEL     # 判断模型（联网判断、关键词、音乐意图）
LLM_API_BASE = ""                  # OpenAI 兼容 API 地址，如 https://api.openai.com/v1
LLM_API_KEY = ""                   # API Key（留空则尝试使用环境变量 OPENAI_API_KEY）

OLLAMA_CHAT_API = "http://localhost:11434/api/chat"
OLLAMA_GENERATE_API = "http://localhost:11434/api/generate"
OLLAMA_TAGS_API = "http://localhost:11434/api/tags"
GPT_SOVITS_API = "http://127.0.0.1:9880/tts"
GPT_SOVITS_BASE = "http://127.0.0.1:9880"

# 默认语音参数（会被预设覆盖）
REF_AUDIO_PATH = os.path.join(
    PROJECT_ROOT, "voice_presets", "luoty_01",
    "my_voice.wav_0000000000_0000173120.wav",
).replace("\\", "/")
PROMPT_TEXT = "见字如唔，此刻的我呢，正坐在老v帮忙订的火车上"
TTS_MODEL_NAME = "GPT-SoVITS 自定义音色"
CURRENT_VOICE_NAME = "默认"
GPT_WEIGHTS_PATH = ""
SOVITS_WEIGHTS_PATH = ""

# 角色默认值（默认使用“洛天依”预设，取代原“小薇”）
DEFAULT_CHARACTER = "洛天依"
DEFAULT_INFLUENCE_MIN = 5
DEFAULT_INFLUENCE_MAX = 8

# 数据 / 预设 / 输出目录
PRESETS_DIR = os.path.join(PROJECT_ROOT, "presets")
VOICE_PRESETS_DIR = os.path.join(PROJECT_ROOT, "voice_presets")
USAGE_FILE = os.path.join(PROJECT_ROOT, "tavily_usage.json")
SAVE_AU_DIR = os.path.join(PROJECT_ROOT, "save-au")
SAVE_WO_DIR = os.path.join(PROJECT_ROOT, "save-wo")

# 运行时生成目录（语音合成结果、临时音乐）
RUNTIME_DIR = os.path.join(PROJECT_ROOT, "runtime")
TTS_OUTPUT_DIR = os.path.join(RUNTIME_DIR, "tts")
MUSIC_OUTPUT_DIR = os.path.join(RUNTIME_DIR, "music")

# 插件目录（第三方开发者可在此放入 / 修改 / 删除插件）
PLUGINS_DIR = os.path.join(PROJECT_ROOT, "plugins")
PLUGINS_STATE_FILE = os.path.join(PROJECT_ROOT, "plugins_state.json")
PLUGINS_SETTINGS_FILE = os.path.join(PROJECT_ROOT, "plugins_settings.json")

# ffmpeg 路径（相对项目根目录，便于打包；若不存在则回退到 PATH 或旧默认路径）
FFMPEG_PATH = os.path.join(PROJECT_ROOT, "ffmpeg", "bin", "ffmpeg.exe")

# 联网搜索（可通过“联网搜索设置”插件在 WebUI 配置）
SEARCH_PROVIDER = "tavily"      # tavily | bing（不推荐，无需Key） | custom
TAVILY_API_KEY = ""             # 请填写你自己的 Tavily API Key（或在 WebUI“联网搜索设置”中配置）
TAVILY_MONTHLY_LIMIT = 1000
SEARCH_CUSTOM_URL = ""          # 自定义搜索 API 地址（如 https://api.example.com/search）
SEARCH_CUSTOM_KEY = ""          # 自定义搜索 API Key（可选）

# 录音参数（浏览器端录音沿用这些阈值做静音检测）
SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 1.2
MAX_RECORD_SEC = 10
MAX_HISTORY = 20

# 联网触发词（保留，供需要时做启发式兜底）
SEARCH_TRIGGER_PHRASES = [
    "不知道", "不太清楚", "无法回答", "我暂时不知道",
    "我有点卡壳了", "抱歉，我", "不清楚",
    "做不到", "无法", "不能上网", "没法", "没办法",
    "让我上网查", "上网查一下", "查一查", "我查一下",
    "不擅长", "没学过", "网络", "不能", "记不清", "上网"
]

# ========== Web 服务 ==========
WEB_HOST = "127.0.0.1"
WEB_PORT = 10999

# ========== 全局运行时状态 ==========
APP_MODE = "standard"       # 运行模式：standard（全部服务）/ lite（仅语音合成 + 外部 API）
DEBUG_MODE = False
tts_volume = 1.0
music_volume = 0.7
tavily_used_count = 0
internet_enabled = True
influence_min = DEFAULT_INFLUENCE_MIN
influence_max = DEFAULT_INFLUENCE_MAX
character_name = DEFAULT_CHARACTER
conversation_history = []      # 发给大模型的对话历史
history_records = []           # 展示 / 保存用的人类可读记录
last_search_keyword = ""       # 最近一次联网搜索关键词

# 保存计数器
save_counter = 0


def increment_save_counter():
    """保存记录时递增序号，保证文件名不冲突。"""
    global save_counter
    save_counter += 1
    return save_counter


def ensure_dirs():
    """确保运行时需要的目录存在。"""
    for d in (PRESETS_DIR, VOICE_PRESETS_DIR, SAVE_AU_DIR, SAVE_WO_DIR,
              TTS_OUTPUT_DIR, MUSIC_OUTPUT_DIR, PLUGINS_DIR):
        os.makedirs(d, exist_ok=True)
