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
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

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
        # 监听端口：留空则取 api_url 里的端口。若该端口被系统保留 / 被占用（Windows 上
        # 常见于 Hyper-V / WSL / Docker 预留的排除段），启动时会自动换一个可用端口并写回本文件。
        "port": None,
        "start_timeout": 60,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 10999,
        "auto_open_browser": True,
    },
}

# GPT-SoVITS 运行时状态（进程 / 端口 / 日志），供状态查询与重启使用
_GS_LOCK = threading.RLock()
_GS_STATE = {
    "process": None,
    "port": None,
    "url": "",
    "log_file": "",
    "last_error": "",
    "started_at": 0.0,
}
# 备选端口：优先避开 Windows 动态端口段（本机 1024-15000）与常见预留段
_GS_PORT_CANDIDATES = [20000, 20100, 20200, 21000, 22000, 23000, 24000]
# Ollama 进程状态（只停止「本程序启动的」Ollama，用户自己启动的不动）
_OLLAMA_LOCK = threading.Lock()
_OLLAMA_STATE = {"process": None}
# 最近一次 stop_all 的结果（用于跳过退出流程里的重复清理）
_LAST_STOP = {"ts": 0.0, "clean": False}
# GPT-SoVITS 就绪探测缓存（本地探测可能被防火墙拖到超时，缓存后轮询几乎零成本）
_GS_READY_CACHE = {"ts": 0.0, "ready": False, "ttl": 3.0}


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
    """把启动器配置中的服务地址同步到全局配置。

    GPT-SoVITS 的地址优先用 `gpt_sovits.port`（端口不可用时会自动改写并写回配置），
    没有 port 时才用 api_url——保证「实际监听端口」与「程序访问地址」始终一致。
    """
    ollama_url = (cfg.get("ollama", {}).get("api_url") or "").rstrip("/")
    if ollama_url:
        config.OLLAMA_TAGS_API = ollama_url + "/api/tags"
        config.OLLAMA_CHAT_API = ollama_url + "/api/chat"
        config.OLLAMA_GENERATE_API = ollama_url + "/api/generate"

    gs_url = gpt_sovits_url(cfg)
    if gs_url:
        config.GPT_SOVITS_BASE = gs_url
        config.GPT_SOVITS_API = gs_url + "/tts"


def gpt_sovits_url(cfg=None):
    """返回 GPT-SoVITS API 的基地址（以 port 为准，port 缺省时用 api_url）。"""
    cfg = cfg or load_launcher_config()
    gs = cfg.get("gpt_sovits", {}) or {}
    try:
        if gs.get("port"):
            return f"http://127.0.0.1:{int(gs['port'])}"
    except Exception:
        pass
    return (gs.get("api_url") or "").rstrip("/")


def gpt_sovits_port(cfg=None):
    """返回 GPT-SoVITS 当前端口（优先 port，其次从 api_url 解析）。"""
    cfg = cfg or load_launcher_config()
    gs = cfg.get("gpt_sovits", {}) or {}
    try:
        if gs.get("port"):
            return int(gs["port"])
    except Exception:
        pass
    try:
        return int(urllib.parse.urlparse(gs.get("api_url") or "").port or 9880)
    except Exception:
        return 9880


def port_available(port, host="127.0.0.1"):
    """检测端口能否被本机监听（Windows 上「系统保留端口」会 bind 失败 → 返回 False）。

    这是本次修复的关键：此前 GPT-SoVITS 固定用 9880，而该端口可能落在
    Hyper-V / WSL / Docker 预留的排除段里，bind 会报 WSAEACCES(10013)，
    进程加载完模型就立刻退出（表现为「GPT-SoVITS 自己关闭」）。

    注意：这里**不**设置 SO_REUSEADDR —— Windows 下若两个套接字都开了它，
    第二个 bind 仍会「成功」，会掩盖「端口已被占用」的真实情况；
    不设置时，被占用的端口与系统保留端口都会如实失败。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, int(port)))
        return True
    except Exception:
        return False


def resolve_gpt_sovits_port(cfg=None):
    """挑选一个真正可用的 GPT-SoVITS 监听端口。

    返回 (port, reason)：reason 为空表示沿用配置端口；否则说明为什么换了端口。
    """
    cfg = cfg or load_launcher_config()
    preferred = gpt_sovits_port(cfg)
    if port_available(preferred):
        return preferred, ""
    candidates = [p for p in _GS_PORT_CANDIDATES if p != preferred]
    candidates += [random.randint(21000, 29000) for _ in range(6)]
    for port in candidates:
        if port_available(port):
            return port, f"端口 {preferred} 无法监听（被系统保留或被占用）"
    return preferred, f"端口 {preferred} 不可用，且未找到可用备选端口"


def _save_launcher_config(cfg):
    """写回 launcher_config.json（用于记录自动选择的服务端口）。"""
    try:
        data = copy.deepcopy(cfg)
        data.get("gpt_sovits", {}).pop("port_note", None)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[警告] 写入 launcher_config.json 失败：{e}")
        return False


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


def check_gpt_sovits(cfg=None, use_cache=True, timeout=0.6):
    """检测 GPT-SoVITS API 是否就绪（按实际端口探测 /docs）。

    本地服务探测用短超时（0.6s）+ 结果短时缓存：部分机器上防火墙会**丢弃**到本地
    未监听端口的连接（表现为一直等到超时，而不是立刻 connection refused），
    而前端每 5 秒轮询一次 /api/status，若不缓存会被这个探测反复拖慢。
    """
    now = time.time()
    if use_cache and (now - _GS_READY_CACHE["ts"]) < _GS_READY_CACHE["ttl"]:
        return _GS_READY_CACHE["ready"]
    url = gpt_sovits_url(cfg) + "/docs"
    ready = False
    try:
        ready = requests.get(url, timeout=timeout).status_code == 200
    except Exception:
        ready = False
    _GS_READY_CACHE.update(ts=now, ready=ready)
    return ready


def invalidate_ready_cache():
    """清空就绪探测缓存（启动/停止服务后调用，保证状态立刻刷新）。"""
    _GS_READY_CACHE.update(ts=0.0, ready=False)


def _ollama_command(cfg):
    """返回可用的 ollama 可执行文件路径；未安装时返回 None。"""
    command = (cfg.get("ollama", {}) or {}).get("command", "ollama")
    return shutil.which(command)


# ==================== 启动 ====================
def ensure_hidden_console():
    """给本进程准备一个「隐藏控制台」（Windows）。

    为什么需要：父进程完全没有控制台时，子进程——以及子进程再开的孙进程——每次创建控制台程序
    都会各自新分配一个控制台窗口，用户就会看到命令行窗口不断跳出又迅速消失。
    这里先给本进程分配一个控制台并立刻隐藏，之后所有子进程都继承它，就不会再出现新窗口
    （stdout/stderr 早已重定向到日志文件，所以隐藏的控制台不会影响日志）。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        if not k32.GetConsoleWindow():
            k32.AllocConsole()
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            u32.ShowWindow(hwnd, 0)      # SW_HIDE
            return True
    except Exception:
        pass
    return False


def _silent_kwargs():
    """静默启动子进程所需的 Popen 参数（Windows 下不出现任何命令行窗口）。

    实测结论（见 verify_silent_spawn.py）：
      · CREATE_NO_WINDOW：子进程拥有一个「没有窗口的控制台」，它再开的子进程会继承这个控制台，
        因此整棵进程树都不会出现窗口 —— 这是正确的选择；
      · DETACHED_PROCESS：子进程完全没有控制台，它再开的子进程会各自新建控制台窗口
        （这正是「大量命令行窗口跳出又消失」的原因），因此不能再用；
      · 另外通过 ensure_hidden_console() 给本进程一个隐藏控制台作为兜底。
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def _gpt_sovits_python(cfg):
    """推断运行 api_v2.py 的 Python 解释器路径。

    优先使用同目录下的 pythonw.exe（GUI 子系统解释器）：它本身不带控制台，
    配合 _silent_kwargs 的 CREATE_NO_WINDOW 与 ensure_hidden_console，整棵进程树都不会出现窗口。
    找不到 pythonw.exe 时回退到 python.exe。
    """
    if cfg["gpt_sovits"].get("python"):
        chosen = _resolve_path(cfg["gpt_sovits"]["python"])
    else:
        chosen = ""
        script = _resolve_path(cfg["gpt_sovits"].get("api_script", ""))
        if script:
            bundled = os.path.join(os.path.dirname(script), "runtime", "python.exe")
            if os.path.exists(bundled):
                chosen = bundled
        if not chosen:
            chosen = sys.executable
    # python.exe → pythonw.exe（同目录存在时优先，避免控制台窗口）
    try:
        base = os.path.basename(chosen)
        if base.lower() == "python.exe":
            candidate = os.path.join(os.path.dirname(chosen), "pythonw.exe")
            if os.path.exists(candidate):
                return candidate
    except Exception:
        pass
    return chosen


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
        ensure_hidden_console()          # 子进程继承隐藏控制台，孙进程也不会新开窗口
        # 静默启动（不弹命令行窗口）；输出仍写入日志文件，便于排查
        log_dir = os.path.join(config.RUNTIME_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_fp = open(os.path.join(log_dir, "ollama.log"), "a", encoding="utf-8", errors="replace")
        log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动：{exe} serve =====\n")
        log_fp.flush()
        proc = subprocess.Popen([exe, "serve"], stdout=log_fp, stderr=subprocess.STDOUT,
                                **_silent_kwargs())
        with _OLLAMA_LOCK:
            _OLLAMA_STATE["process"] = proc
        return "started"
    except Exception as e:
        print(f"启动 Ollama 失败：{e}")
        print("本次启动不加载 Ollama，将只能通过外部 API（OpenAI 兼容接口）调用大模型。")
        return "failed"


def start_gpt_sovits(cfg=None, auto_port=True):
    """启动 GPT-SoVITS API；缺失或启动失败时跳过，不阻塞流程。

    关键修复（旧实现的问题）：固定使用 9880 端口，而该端口可能落在 Windows
    「系统保留 / 排除」段（Hyper-V、WSL、Docker 预留）里 —— 此时 api_v2.py 加载完模型
    会因 bind 报 WSAEACCES(10013) 立刻退出，控制台窗口一闪而过（用户看到的就是
    「GPT-SoVITS 自己关闭」）。现在：
      1) 启动前先做端口可用性检测，不可用则自动换到可用端口并写回 launcher_config.json；
      2) 把 api_v2.py 的 stdout/stderr 重定向到 runtime/logs/gpt_sovits_api.log（不再丢日志）；
      3) 启动后短暂观察，若进程立刻退出，就读日志尾部并打印明确原因。
    返回 (started: bool, info: dict)。
    """
    cfg = cfg or load_launcher_config()
    ensure_hidden_console()          # 子进程继承隐藏控制台，孙进程也不会新开窗口
    with _GS_LOCK:
        if not cfg["gpt_sovits"].get("enabled", True):
            return False, {"reason": "disabled"}
        if check_gpt_sovits(cfg):
            return False, {"reason": "running", "port": gpt_sovits_port(cfg)}
        script = _resolve_path(cfg["gpt_sovits"].get("api_script", ""))
        if not script or not os.path.exists(script):
            print(f"未检测到 GPT-SoVITS API 脚本：{script}")
            print("本次启动不加载语音合成（GPT-SoVITS），文字对话不受影响。")
            print("如需语音合成，请先补齐 GPT-SoVITS 整合包并重新运行本程序。")
            return False, {"reason": "no_script"}
        python = _gpt_sovits_python(cfg)

        # 1) 选一个真正可用的端口（避开被系统保留 / 占用的端口）
        port = gpt_sovits_port(cfg)
        if auto_port:
            port, why = resolve_gpt_sovits_port(cfg)
            if why:
                print(f"[提示] {why}，已自动改用端口 {port}（可写入 launcher_config.json 固化）。")
                cfg.setdefault("gpt_sovits", {})["port"] = port
                cfg["gpt_sovits"]["api_url"] = f"http://127.0.0.1:{port}"
                cfg["gpt_sovits"]["port_note"] = why
                _save_launcher_config(cfg)
        apply_api_urls(cfg)

        # 2) 输出重定向到日志文件（不再开一个出错就消失的控制台窗口）
        log_dir = os.path.join(config.RUNTIME_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "gpt_sovits_api.log")
        api_dir = os.path.dirname(script)
        print(f"正在启动 GPT-SoVITS API（{python}，端口 {port}）...")
        print(f"        运行日志：{log_file}")
        try:
            log_fp = open(log_file, "a", encoding="utf-8", errors="replace")
            log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动："
                         f"{python} {os.path.basename(script)} -p {port} =====\n")
            log_fp.flush()
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            proc = subprocess.Popen([python, script, "-a", "127.0.0.1", "-p", str(port)],
                                    cwd=api_dir, stdout=log_fp, stderr=subprocess.STDOUT, env=env,
                                    **_silent_kwargs())
        except Exception as e:
            print(f"启动 GPT-SoVITS API 失败：{e}")
            print("本次启动不加载语音合成（GPT-SoVITS），文字对话不受影响。")
            return False, {"reason": "spawn_failed", "error": str(e)}
        with _GS_LOCK:
            _GS_STATE.update({"process": proc, "port": port,
                              "url": gpt_sovits_url(cfg), "log_file": log_file,
                              "last_error": "", "started_at": time.time()})
        invalidate_ready_cache()

    # 3) 短暂观察：立刻退出说明启动失败（端口被占 / 依赖缺失 / 权重异常等）
    time.sleep(2.5)
    if proc.poll() is not None:
        tail = tail_gpt_sovits_log(8)
        hint = ""
        if "10013" in tail or "Errno 13" in tail or "访问权限" in tail:
            hint = f"端口 {port} 无法监听（可能被系统保留或被占用）"
        elif "Address already in use" in tail or "10048" in tail:
            hint = f"端口 {port} 已被其它程序占用"
        elif "CUDA" in tail and ("error" in tail.lower() or "no kernel image" in tail):
            hint = "CUDA 初始化失败（可把 tts_infer.yaml 的 custom.device 改为 cpu 后重试）"
        reason = f"GPT-SoVITS 启动后立刻退出（退出码 {proc.returncode}）" + (f"：{hint}" if hint else "")
        print(f"[错误] {reason}")
        print(f"        日志尾部：\n{tail}")
        with _GS_LOCK:
            _GS_STATE.update({"process": None, "last_error": reason})
        return False, {"reason": "exited", "error": reason, "port": port, "log_tail": tail}
    return True, {"reason": "started", "port": port, "log_file": log_file}


def stop_gpt_sovits(timeout=8.0):
    """停止本程序启动的 GPT-SoVITS 进程（用于重启 / 退出清理）。"""
    with _GS_LOCK:
        proc = _GS_STATE.get("process")
        _GS_STATE["process"] = None
    invalidate_ready_cache()
    if proc is None:
        return True
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()
        return True
    except Exception:
        return False


def stop_ollama(timeout=5.0):
    """停止本程序启动的 Ollama 进程（用户自己启动的 Ollama 不动，返回 False 表示没停）。"""
    with _OLLAMA_LOCK:
        proc = _OLLAMA_STATE.get("process")
        _OLLAMA_STATE["process"] = None
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()
        return True
    except Exception:
        return False


def _ollama_base_url(cfg=None):
    """Ollama 服务基地址（跟随 launcher_config.json 里的 api_url）。"""
    cfg = cfg or load_launcher_config()
    base = ((cfg.get("ollama", {}) or {}).get("api_url") or "").strip().rstrip("/")
    if base:
        return base
    try:                     # 兜底：从已应用的接口地址反推
        return config.OLLAMA_TAGS_API.rsplit("/api/", 1)[0].rstrip("/")
    except Exception:
        return "http://localhost:11434"


def unload_ollama_models(cfg=None, timeout=3.0):
    """让 Ollama 卸载已加载的模型权重（keep_alive=0），释放显存 / 内存。

    为什么需要：直接结束 ollama 进程时，已加载的模型权重不一定立刻从显存释放
    （尤其是 ollama app / 服务方式运行时），会造成「关掉了程序但显卡还被占着」。
    这里先通过官方接口显式卸载：GET /api/ps 列出已加载模型 → 逐个 POST /api/generate
    带 keep_alive=0，最后再复核一次 /api/ps。
    超时给得很短：Ollama 忙的时候不能把退出流程拖住（拖住就等于「关了没清理」）。
    返回 (已卸载的模型列表, 复核后仍加载的模型列表, 错误说明)。
    """
    base = _ollama_base_url(cfg)
    unloaded, still, err = [], [], ""
    try:
        resp = requests.get(base + "/api/ps", timeout=timeout)
        resp.raise_for_status()
        models = (resp.json() or {}).get("models") or []
    except Exception as e:
        return [], [], f"未获取到已加载模型（{e}）"
    for m in models:
        name = (m or {}).get("name") or (m or {}).get("model") or ""
        if not name:
            continue
        try:
            requests.post(base + "/api/generate", json={"model": name, "keep_alive": 0},
                          timeout=timeout)
            unloaded.append(name)
        except Exception as e:
            err = str(e)
    try:                     # 复核：确认真的卸载掉
        resp = requests.get(base + "/api/ps", timeout=timeout)
        if resp.status_code == 200:
            still = [((m or {}).get("name") or (m or {}).get("model") or "")
                     for m in ((resp.json() or {}).get("models") or [])]
            still = [s for s in still if s]
    except Exception:
        pass
    return unloaded, still, err


def release_local_models():
    """卸载「本程序进程内」加载的模型权重（语音识别 Whisper）。"""
    freed = []
    try:
        from core import models as models_mod
        if models_mod.is_ready() and models_mod.release():
            freed.append("Whisper 语音识别")
    except Exception:
        pass
    return freed


def _gpu_memory(timeout=3.0):
    """显存占用（MB）：返回 (used, total) 或 None（无 NVIDIA 显卡 / 取不到）。

    超时给得短：nvidia-smi 在显卡繁忙时可能长时间不返回，不能拖住退出流程。
    """
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        first = out.strip().splitlines()[0] if out.strip() else ""
        used, total = [int(x.strip()) for x in first.split(",")[:2]]
        return used, total
    except Exception:
        return None


def _wait_ports_free(ports, timeout=6.0):
    """等待端口释放；返回仍未释放的端口列表。"""
    import time
    deadline = time.time() + timeout
    pending = [p for p in ports if p]
    while pending and time.time() < deadline:
        pending = [p for p in pending if not port_available(p)]
        if pending:
            time.sleep(0.3)
    return pending


def stop_all(include_orphans=True, verify=True, force=False, deadline_seconds=15.0):
    """停止本地服务并卸载模型权重（关闭程序时调用），返回可读报告。

    顺序（先「卸载权重」再「停进程」，避免关掉程序后显存 / 内存还被占着）：
      1) 让 Ollama 卸载已加载模型（/api/ps + keep_alive=0）——显式释放权重；
      2) 停「本进程启动的」GPT-SoVITS（权重随进程退出释放）与 Ollama；
      3) include_orphans：结束所有同类服务进程（含父进程已消失的遗留进程，按可执行文件路径识别）；
      4) 卸载本程序进程内的 Whisper 权重；
      5) verify：复核「进程 + 端口」是否真的都清干净，没清干净就再结束一次；
         并给出显存占用前后对比，便于确认硬件已释放。

    **全部步骤都受 deadline_seconds 约束**：任何一步卡住（例如 nvidia-smi 不返回、
    某个进程杀不掉）都会立即收尾并返回，绝不把退出流程拖住 —— 拖住就等于「关了没清理」。
    """
    now = time.time()
    if not force and _LAST_STOP.get("clean") and now - _LAST_STOP.get("ts", 0.0) < 3.0:
        return "刚刚已完成本地服务清理（跳过重复执行）"

    deadline = time.time() + max(3.0, float(deadline_seconds))

    def _left():
        return max(0.0, deadline - time.time())

    try:
        from core import logger as _logger
    except Exception:
        _logger = None

    def _note(msg):
        if _logger is not None:
            try:
                _logger.info(f"清理本地服务：{msg}")
            except Exception:
                pass

    _note(f"开始（PID {os.getpid()}，超时 {int(deadline_seconds)}s）")
    lines = []
    gpu_before = _gpu_memory(timeout=min(2.5, _left())) if _left() > 1 else None

    # 1) 显式卸载 Ollama 已加载的模型权重
    if _left() > 1:
        t0 = time.time()
        try:
            unloaded, still, err = unload_ollama_models(timeout=min(3.0, _left()))
            if unloaded:
                lines.append("已卸载 Ollama 模型权重：" + "、".join(unloaded)
                             + (f"（仍有 {len(still)} 个未释放）" if still else "（显存已释放）"))
            elif err:
                lines.append(f"Ollama 模型卸载跳过：{err}")
        except Exception as e:
            lines.append(f"Ollama 模型卸载失败：{e}")
        _note(f"卸载 Ollama 权重完成，用时 {int((time.time() - t0) * 1000)}ms")

    # 2) 停本程序启动的服务进程（只有确实由本进程启动的才记「已停止」）
    if _left() > 0.5:
        t0 = time.time()
        try:
            with _GS_LOCK:
                owned_gs = _GS_STATE.get("process") is not None
        except Exception:
            owned_gs = False
        try:
            if stop_gpt_sovits() and owned_gs:
                lines.append("已停止本程序启动的 GPT-SoVITS（权重随进程退出释放）")
            if stop_ollama():
                lines.append("已停止本程序启动的 Ollama")
        except Exception as e:
            lines.append(f"停止本程序服务失败：{e}")
        _note(f"停止自有服务完成，用时 {int((time.time() - t0) * 1000)}ms")

    # 3) 结束所有同类服务进程（含遗留进程：GPT-SoVITS 的父进程可能早已退出）
    remaining_before = list_service_processes() if _left() > 0.5 else []
    if include_orphans and remaining_before and _left() > 1:
        t0 = time.time()
        try:
            killed = _kill_orphan_services(logger=_logger, budget=_left())
            if _left() > 1:
                extra = _kill_service_port_owners(logger=_logger)
                for k, v in extra.items():
                    killed[k] = killed.get(k, 0) + v
            if killed:
                lines.append("已结束遗留服务进程：" + "、".join(f"{n}×{c}" for n, c in killed.items()))
            else:
                lines.append("遗留服务进程未能结束："
                             + "、".join(f'{p["name"]}({p["pid"]})' for p in remaining_before))
        except Exception as e:
            lines.append(f"清理遗留服务进程失败：{e}")
        _note(f"清理遗留进程完成，用时 {int((time.time() - t0) * 1000)}ms")

    # 4) 进程内模型（Whisper）
    try:
        freed = release_local_models()
        if freed:
            lines.append("已释放进程内模型：" + "、".join(freed))
    except Exception:
        pass

    # 5) 复核 + 再清一次（保证「关闭不完全」不会再发生）
    if verify and _left() > 1:
        try:
            _wait_ports_free([gpt_sovits_port(), 11434], timeout=min(2.0, _left()))
        except Exception:
            pass
        st = verify_services_stopped()
        if st["processes"] or st["ports"]:
            again = _kill_orphan_services(logger=_logger, budget=_left())
            try:
                _wait_ports_free([gpt_sovits_port(), 11434], timeout=min(2.0, _left()))
            except Exception:
                pass
            st = verify_services_stopped()
            if again:
                lines.append("复核时又结束了残留进程：" + "、".join(f"{n}×{c}" for n, c in again.items()))
            if st["processes"]:
                lines.append("复核发现仍有服务进程存活：" + "、".join(st["processes"]))
            if st["ports"]:
                lines.append("复核发现端口仍被占用：" + "、".join(str(p) for p in st["ports"]))
        else:
            lines.append("已复核：本地服务进程与端口均已清理")

    if _left() > 1:
        gpu_after = _gpu_memory(timeout=min(2.5, _left()))
        if gpu_before and gpu_after:
            lines.append(f"显存占用：{gpu_before[0]}MB → {gpu_after[0]}MB（共 {gpu_after[1]}MB）")
            if gpu_after[0] and gpu_after[0] >= gpu_before[0] - 50 and not list_service_processes():
                lines.append("显存未明显下降，但本地服务进程已全部退出（占用可能来自其它程序）")
    _note(f"结束，总用时 {int((time.time() - (deadline - deadline_seconds)) * 1000)}ms")

    _LAST_STOP["ts"] = time.time()
    _LAST_STOP["clean"] = not verify_services_stopped()["processes"] if verify else False
    return "；".join(lines) if lines else "没有需要停止的本地服务"


def _snapshot_processes():
    """用 Windows 进程快照列出进程（名称 / PID / 父 PID），无需额外权限。

    比 tasklist 可靠（部分环境 tasklist 会被拒绝访问），用于识别遗留的服务进程。
    返回 [{"pid": int, "ppid": int, "name": str}, ...]；非 Windows 返回 []。
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    rows = []
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)      # TH32CS_SNAPPROCESS
        if not snap or snap == -1:
            return []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                rows.append({"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                             "name": entry.szExeFile})
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return rows
    return rows


def _list_processes():
    """(名称, 进程号) 列表（保留旧接口，内部改用进程快照）。"""
    return [(p["name"], p["pid"]) for p in _snapshot_processes()]


def _is_our_gpt_sovits(name, pid):
    """是否是「本项目的 GPT-SoVITS 语音服务进程」（按可执行文件路径判断）。"""
    if name.lower() not in ("python.exe", "pythonw.exe"):
        return False
    img = _process_image_path(pid)
    if not img:
        return False
    gs_dir = os.path.normcase(os.path.join(config.PROJECT_ROOT, "gpt_sovits"))
    return os.path.normcase(img).startswith(gs_dir)


def _is_ollama_process(name):
    """是否是 Ollama 相关进程（服务 / 模型 runner / 桌面托盘）。"""
    return name.lower() in ("ollama.exe", "ollama_llama_server.exe", "ollama app.exe")


def list_service_processes():
    """列出本地服务相关进程（只读诊断，不结束任何进程）。

    返回 [{"pid", "ppid", "name", "kind", "image"}]，kind ∈ {"gpt_sovits", "ollama"}。
    """
    me = os.getpid()
    out = []
    for p in _snapshot_processes():
        if p["pid"] == me:
            continue
        kind = ""
        if _is_our_gpt_sovits(p["name"], p["pid"]):
            kind = "gpt_sovits"
        elif _is_ollama_process(p["name"]):
            kind = "ollama"
        if kind:
            out.append({"pid": p["pid"], "ppid": p["ppid"], "name": p["name"], "kind": kind,
                        "image": _process_image_path(p["pid"])})
    return out


def _wait_pid_gone(pid, timeout=5.0):
    """等待进程真正消失（taskkill 之后进程不会瞬间从进程表消失）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def kill_process(pid, timeout=4.0, logger=None, taskkill_timeout=4.0):
    """结束进程（连同子进程）：taskkill /T 与 TerminateProcess 双管齐下，并等待真正退出。

    之前只用 taskkill 且**立刻**检查进程表，会出现「其实已经杀掉、但报告说没杀掉」，
    也可能真的失败却没人知道；现在两种方式都试、都等结果，并把失败原因记进日志。
    所有等待都有上限：退出流程里不能因为一个杀不掉的进程把整个关闭拖住。
    """
    import subprocess
    try:
        pid = int(pid)
    except Exception:
        return False
    if not _pid_alive(pid):
        return True
    msgs = []
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True,
                           timeout=taskkill_timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((r.stderr or "") + (r.stdout or "")).strip().replace("\n", " ")[:160]
        msgs.append(f"taskkill rc={r.returncode} {out}")
    except Exception as e:
        msgs.append(f"taskkill 异常：{e}")
    if _wait_pid_gone(pid, min(1.5, timeout)):
        return True

    # 方式二：直接 TerminateProcess
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            k32.TerminateProcess(handle, 1)
            k32.CloseHandle(handle)
            msgs.append("TerminateProcess 已调用")
        else:
            msgs.append(f"OpenProcess 失败（错误码 {k32.GetLastError()}）")
    except Exception as e:
        msgs.append(f"TerminateProcess 异常：{e}")

    ok = _wait_pid_gone(pid, timeout)
    if not ok and logger is not None:
        try:
            logger.warn(f"结束进程 {pid} 失败：" + "；".join(msgs))
        except Exception:
            pass
    return ok


def _kill_pid(pid, timeout=15):
    """兼容旧调用名。"""
    return kill_process(pid, timeout=timeout)


def _pid_alive(pid):
    """进程是否还活着（用进程快照判断：不依赖 tasklist，受限环境也准确）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    return any(p["pid"] == pid for p in _snapshot_processes())


def _kill_orphan_services(only_gpt_sovits=False, logger=None, budget=None):
    """清理遗留的服务进程；返回 {进程名: 数量}（只统计真正结束掉的）。

    budget：本次清理允许花费的秒数上限（退出流程里必须限时，不能被杀不掉的进程拖住）。
    """
    import time
    deadline = time.time() + float(budget) if budget else None
    killed = {}
    for p in list_service_processes():
        if only_gpt_sovits and p["kind"] != "gpt_sovits":
            continue
        if deadline is not None and time.time() >= deadline:
            if logger is not None:
                try:
                    logger.warn(f"清理遗留进程时间已到，剩余未处理：{p['name']}({p['pid']})")
                except Exception:
                    pass
            break
        per = 4.0
        if deadline is not None:
            per = max(1.0, min(per, deadline - time.time()))
        if kill_process(p["pid"], timeout=per, logger=logger, taskkill_timeout=min(4.0, per)):
            key = p["name"]
            killed[key] = killed.get(key, 0) + 1
        elif logger is not None:
            try:
                logger.warn(f"未能结束遗留服务进程 {p['name']}({p['pid']})")
            except Exception:
                pass
    return killed


def sweep_gpt_sovits_orphans(logger=None, budget=None):
    """只清理「本项目的 GPT-SoVITS」遗留进程（启动器启动时用，绝不动 Ollama）。"""
    return _kill_orphan_services(only_gpt_sovits=True, logger=logger, budget=budget)


def port_owner_pid(port):
    """返回监听指定端口的进程号（找不到返回 None）。"""
    import re
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=6,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line, re.I)
        if m and int(m.group(1)) == int(port):
            return int(m.group(2))
    return None


def _kill_service_port_owners(logger=None):
    """按端口占用者清理：个别遗留服务可能不是从项目路径启动的（例如手动启动的整合包），
    只要它占着我们的服务端口（GPT-SoVITS / Ollama），退出时就一并结束；
    非 Python / Ollama 占用端口的进程一律不动。
    """
    killed = {}
    me = os.getpid()
    ports = set()
    try:
        ports.add(gpt_sovits_port())
    except Exception:
        pass
    ports.add(11434)
    for port in ports:
        pid = port_owner_pid(port)
        if not pid or pid == me:
            continue
        img = _process_image_path(pid) or ""
        name = os.path.basename(img).lower()
        if not (name.startswith("python") or name.startswith("ollama")):
            continue
        if kill_process(pid, logger=logger):
            key = name or str(pid)
            killed[key] = killed.get(key, 0) + 1
    return killed


def verify_services_stopped():
    """复核本地服务是否真的都退出了：返回 {"processes": [...], "ports": [...]}。"""
    processes = [f'{p["name"]}({p["pid"]})' for p in list_service_processes()]
    try:
        ports = [p for p in (gpt_sovits_port(), 11434) if not port_available(p)]
    except Exception:
        ports = []
    return {"processes": processes, "ports": ports}


def _gpu_processes(timeout=3.0):
    """占用 GPU 的进程列表（nvidia-smi，取不到返回空列表）。"""
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        rows = []
        for line in out.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2:
                rows.append({"pid": parts[0], "name": parts[1],
                             "memory": parts[2] if len(parts) > 2 else ""})
        return rows
    except Exception:
        return []


def _process_image_path(pid):
    """取进程可执行文件路径（取不到返回空串）。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value or ""
            return ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""


def restart_gpt_sovits(cfg=None):
    """重启 GPT-SoVITS（先停后起），返回 (started, info)。"""
    cfg = cfg or load_launcher_config()
    stop_gpt_sovits()
    time.sleep(1.0)
    # 重新读取配置：上一轮可能已经把端口改成可用值
    cfg = load_launcher_config()
    apply_api_urls(cfg)
    return start_gpt_sovits(cfg)


def tail_gpt_sovits_log(lines=20):
    """读取 GPT-SoVITS 运行日志尾部（供启动诊断与界面展示）。"""
    path = _GS_STATE.get("log_file") or os.path.join(config.RUNTIME_DIR, "logs", "gpt_sovits_api.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().splitlines()
        return "\n".join(content[-max(1, int(lines)):])
    except Exception:
        return ""


def gpt_sovits_status(cfg=None):
    """GPT-SoVITS 服务状态（供 Web UI 展示与「重启语音服务」使用）。"""
    cfg = cfg or load_launcher_config()
    with _GS_LOCK:
        proc = _GS_STATE.get("process")
        port = _GS_STATE.get("port") or gpt_sovits_port(cfg)
        log_file = _GS_STATE.get("log_file") or os.path.join(config.RUNTIME_DIR, "logs", "gpt_sovits_api.log")
        last_error = _GS_STATE.get("last_error", "")
    running = bool(proc is not None and proc.poll() is None)
    ready = check_gpt_sovits(cfg)
    return {
        "ready": ready,
        "running": running,
        "pid": (proc.pid if running else None),
        "port": port,
        "url": gpt_sovits_url(cfg),
        "log_file": log_file,
        "last_error": ("" if ready else last_error),
        "configured_port_available": port_available(port),
        "enabled": bool(cfg.get("gpt_sovits", {}).get("enabled", True)),
    }


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
    gs_started, gs_info = start_gpt_sovits(cfg)
    if gs_info.get("port"):
        # 自动换端口后，用新端口重新同步地址（就绪探测与后续 TTS 请求都走新端口）
        apply_api_urls(load_launcher_config())

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
              lambda: check_gpt_sovits(),
              cfg["gpt_sovits"].get("start_timeout", 60),
              gs_started),
        daemon=True,
    ).start()

    return {
        "ollama": ollama_status,          # not_installed / started / running / failed / disabled
        "ollama_ready": check_ollama(cfg),
        "gpt_sovits_ready": check_gpt_sovits(),
        "gpt_sovits": gpt_sovits_status(),
        "web": cfg.get("web", DEFAULTS["web"]),
    }
