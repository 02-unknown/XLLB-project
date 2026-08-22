# core/services.py
# 外部服务生命周期管理：检测 / 启动 / 等待 Ollama 与 GPT-SoVITS API。
# 地址、启动命令、脚本路径等均可在项目根目录的 launcher_config.json 中配置。
# 说明：
#   - Ollama 为可选组件：已安装则尝试启动；未安装或启动失败时跳过，
#     不阻塞启动流程（此时将只能通过外部 API 调用大模型）。
#   - GPT-SoVITS 为可选组件：缺失时不加载，不影响文字对话。
#   - 服务就绪等待在后台线程进行，不阻塞 Web 界面启动。
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import requests

import core.config as config

CONFIG_FILE = os.path.join(config.PROJECT_ROOT, "launcher_config.json")

DEFAULTS = {
    "ollama": {
        "enabled": True,
        "command": "ollama",          # 可改为 ollama.exe 的完整路径
        "api_url": "http://localhost:11434",
        "start_timeout": 30,
    },
    "gpt_sovits": {
        "enabled": True,
        # GPT-SoVITS api_v2.py 路径（相对项目根目录，便于打包/再部署）
        "api_script": "gpt_sovits/api_v2.py",
        "python": "",                  # 留空则自动使用 api_script 同级的 runtime/python.exe
        "api_url": "http://127.0.0.1:9880",
        "start_timeout": 60,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 10999,
        "auto_open_browser": True,
    },
}


def _resolve_path(path):
    """把相对路径解析为基于项目根目录的绝对路径；已是绝对路径则原样返回。"""
    if not path:
        return path
    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(p):
        p = os.path.join(config.PROJECT_ROOT, p)
    return os.path.normpath(p)


def _deep_merge(base, override):
    """递归合并两个字典，override 优先。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_launcher_config():
    """读取 launcher_config.json，缺失时返回默认值。"""
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:
            print(f"[警告] 读取 launcher_config.json 失败，使用默认配置：{e}")
    return cfg


def apply_api_urls(cfg):
    """把启动器配置中的服务地址同步到全局配置。"""
    ollama_url = (cfg.get("ollama", {}).get("api_url") or "").rstrip("/")
    if ollama_url:
        config.OLLAMA_TAGS_API = ollama_url + "/api/tags"
        config.OLLAMA_CHAT_API = ollama_url + "/api/chat"
        config.OLLAMA_GENERATE_API = ollama_url + "/api/generate"

    gs_url = (cfg.get("gpt_sovits", {}).get("api_url") or "").rstrip("/")
    if gs_url:
        config.GPT_SOVITS_BASE = gs_url
        config.GPT_SOVITS_API = gs_url + "/tts"


# ==================== 就绪检测 ====================
def check_ollama(cfg=None):
    """检测 Ollama 服务是否就绪（服务在运行且已拉取模型）。"""
    cfg = cfg or load_launcher_config()
    url = (cfg["ollama"]["api_url"] or "").rstrip("/") + "/api/tags"
    try:
        resp = requests.get(url, timeout=2)
        if resp.status_code == 200:
            return bool(resp.json().get("models"))
    except Exception:
        pass
    return False


def check_gpt_sovits(cfg=None):
    """检测 GPT-SoVITS API 是否就绪。"""
    cfg = cfg or load_launcher_config()
    url = (cfg["gpt_sovits"]["api_url"] or "").rstrip("/") + "/docs"
    try:
        return requests.get(url, timeout=2).status_code == 200
    except Exception:
        return False


def _ollama_command(cfg):
    """返回可用的 ollama 可执行文件路径；未安装时返回 None。"""
    command = (cfg.get("ollama", {}) or {}).get("command", "ollama")
    return shutil.which(command)


# ==================== 启动 ====================
def _create_flags():
    return subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0


def _gpt_sovits_python(cfg):
    """推断运行 api_v2.py 的 Python 解释器路径。"""
    script = _resolve_path(cfg["gpt_sovits"].get("api_script", ""))
    if cfg["gpt_sovits"].get("python"):
        return _resolve_path(cfg["gpt_sovits"]["python"])
    if script:
        api_dir = os.path.dirname(script)
        bundled = os.path.join(api_dir, "runtime", "python.exe")
        if os.path.exists(bundled):
            return bundled
    return sys.executable


def start_ollama(cfg=None):
    """尝试启动 ollama serve。

    返回状态字符串：
      "running"       - 已在运行
      "started"       - 本次启动成功
      "not_installed" - 未安装 ollama 程序（跳过加载）
      "failed"        - 启动失败（跳过加载）
      "disabled"      - 配置中禁用了 ollama
    未安装或启动失败时不会阻塞流程，仅提示将只能使用外部 API。
    """
    cfg = cfg or load_launcher_config()
    if not cfg["ollama"].get("enabled", True):
        print("Ollama 已在配置中禁用，本次启动不加载 Ollama（将只能通过外部 API 调用大模型）。")
        return "disabled"
    if check_ollama(cfg):
        print("Ollama 服务已在运行。")
        return "running"

    exe = _ollama_command(cfg)
    if not exe:
        print("未检测到 Ollama 程序，本次启动不加载 Ollama。")
        print("提示：不加载 Ollama 时，将只能通过外部 API（OpenAI 兼容接口）调用大模型。")
        return "not_installed"

    print("正在启动 Ollama 服务...")
    try:
        subprocess.Popen([exe, "serve"], creationflags=_create_flags())
        return "started"
    except Exception as e:
        print(f"启动 Ollama 失败：{e}")
        print("本次启动不加载 Ollama，将只能通过外部 API（OpenAI 兼容接口）调用大模型。")
        return "failed"


def start_gpt_sovits(cfg=None):
    """启动 GPT-SoVITS API；缺失或启动失败时跳过，不阻塞流程。"""
    cfg = cfg or load_launcher_config()
    if not cfg["gpt_sovits"].get("enabled", True):
        return False
    if check_gpt_sovits(cfg):
        return False
    script = _resolve_path(cfg["gpt_sovits"].get("api_script", ""))
    if not script or not os.path.exists(script):
        print(f"未检测到 GPT-SoVITS API 脚本：{script}")
        print("本次启动不加载语音合成（GPT-SoVITS），文字对话不受影响。")
        print("如需语音合成，请先补齐 GPT-SoVITS 整合包并重新运行本程序。")
        return False
    python = _gpt_sovits_python(cfg)
    api_dir = os.path.dirname(script)
    print(f"正在启动 GPT-SoVITS API（{python}）...")
    try:
        subprocess.Popen([python, script], cwd=api_dir, creationflags=_create_flags())
    except Exception as e:
        print(f"启动 GPT-SoVITS API 失败：{e}")
        print("本次启动不加载语音合成（GPT-SoVITS），文字对话不受影响。")
        return False
    return True


# ==================== 等待就绪（后台线程，不阻塞 Web） ====================
def wait_ready(checker, timeout, interval=1.0, label=""):
    start = time.time()
    while time.time() - start < timeout:
        if checker():
            print(f"[就绪] {label} 已就绪。")
            return True
        time.sleep(interval)
    print(f"[提示] {label} 在 {timeout}s 内未就绪，相关功能可能暂不可用。")
    return False


def start_all():
    """启动所有外部服务（就绪等待在后台进行），返回状态字典。"""
    cfg = load_launcher_config()
    apply_api_urls(cfg)

    ollama_status = start_ollama(cfg)
    gs_started = start_gpt_sovits(cfg)

    # 就绪检测放到后台线程，避免阻塞 Web 界面启动
    def _wait(label, checker, timeout, need_wait):
        if not need_wait:
            return
        wait_ready(checker, timeout, label=label)

    threading.Thread(
        target=_wait,
        args=("Ollama",
              lambda: check_ollama(cfg),
              cfg["ollama"].get("start_timeout", 30),
              ollama_status in ("started", "running")),
        daemon=True,
    ).start()
    threading.Thread(
        target=_wait,
        args=("GPT-SoVITS API",
              lambda: check_gpt_sovits(cfg),
              cfg["gpt_sovits"].get("start_timeout", 60),
              gs_started),
        daemon=True,
    ).start()

    return {
        "ollama": ollama_status,          # not_installed / started / running / failed / disabled
        "ollama_ready": check_ollama(cfg),
        "gpt_sovits_ready": check_gpt_sovits(cfg),
        "web": cfg.get("web", DEFAULTS["web"]),
    }
