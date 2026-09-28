# core/launch_flow.py —— 图形启动器的启动流程（选模式 → 拉起服务 → 进入正式界面）。
#
# 界面（web/launcher.html）通过 /api/launch/start 选择模式，通过 /api/launch/state 看进度：
#   config  ：读取并应用 launcher_config.json（服务地址 / 脚本路径）
#   services：按模式拉起服务（Lite 只起 GPT-SoVITS；标准再起 Ollama 与 Whisper）
#   tts     ：后台等待语音服务就绪（不阻塞进入界面，页面上显示“加载中/就绪”）
# 设计要点：
#   · 幂等：重复点击 / 重复打开启动页不会重复拉起（正在启动时直接返回当前状态）；
#   · 非致命：缺组件、启动失败都只记在步骤状态里，绝不 sys.exit（进程还要继续提供界面）；
#   · 不阻塞：只有「拉起服务」这一步需要等待，模型加载在后台线程里完成。
import copy
import threading
import time

import core.config as config
import core.models as models
import core.services as services

# 两种模式（界面按此渲染，顺序即展示顺序）
MODES = {
    "lite": {
        "label": "Lite 模式",
        "tag": "最快启动",
        "desc": "只加载语音合成（GPT-SoVITS），大模型走外部 API。不启动 Ollama、不加载语音识别。",
        "need": "适合：已配置外部 API（地址 / Key / 模型名），只想快速进入语音对话。",
    },
    "standard": {
        "label": "标准模式",
        "tag": "全部本地服务",
        "desc": "启动 Ollama（本地大模型）+ GPT-SoVITS（语音合成），并在后台加载 Whisper 语音识别。",
        "need": "适合：本地运行、不依赖外部 API；需已安装 Ollama 与语音识别模型。",
    },
}

# 每个模式的步骤（id, 展示名, 说明）
_STEP_PLANS = {
    "lite": [
        ("check", "检查运行环境", "关键文件与依赖"),
        ("config", "读取启动配置", "launcher_config.json"),
        ("services", "启动语音合成（GPT-SoVITS）", "仅本地语音服务"),
        ("tts", "等待语音服务就绪", "后台加载模型，可先进入界面"),
    ],
    "standard": [
        ("check", "检查运行环境", "关键文件与依赖"),
        ("config", "读取启动配置", "launcher_config.json"),
        ("services", "启动本地服务（Ollama + GPT-SoVITS）", "Whisper 语音识别随后台加载"),
        ("tts", "等待语音服务就绪", "后台加载模型，可先进入界面"),
    ],
}

_lock = threading.Lock()
# 可注入的服务后端（验证脚本用替身，避免真的启动外部进程）
_backends = {"services": services, "models": models}


def set_backends(services_mod=None, models_mod=None):
    """注入服务后端（仅测试使用；传 None 表示恢复默认）。"""
    with _lock:
        _backends["services"] = services_mod or services
        _backends["models"] = models_mod or models


def _blank_state():
    return {
        "mode": "",
        "starting": False,   # 步骤是否还在执行（启动中再次点击不会重复拉起）
        "running": False,    # 服务是否仍在后台就绪等待
        "done": False,       # 服务已拉起，可以进入正式界面
        "error": "",
        "started_at": 0.0,
        "finished_at": 0.0,
        "steps": [],
        "tts": {},
        "app_mode": config.APP_MODE,
        "url": "/",
    }


_state = _blank_state()


def modes():
    """模式清单（供界面渲染）。"""
    return [{"id": key, **info} for key, info in MODES.items()]


def state():
    """当前启动状态（深拷贝，避免调用方改到内部状态）。"""
    with _lock:
        return copy.deepcopy(_state)


def reset():
    """清空状态（测试 / 重新选择模式时用）。"""
    global _state
    with _lock:
        _state = _blank_state()
        return copy.deepcopy(_state)


def is_done():
    with _lock:
        return bool(_state["done"])


def start(mode):
    """按模式开始启动流程；返回最新状态。正在启动中则原样返回（幂等）。"""
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError("未知的启动模式：%s" % (mode or "(空)"))
    with _lock:
        if _state["starting"]:
            return copy.deepcopy(_state)
        _state.update({
            "mode": mode,
            "starting": True,
            "running": True,
            "done": False,
            "error": "",
            "started_at": time.time(),
            "finished_at": 0.0,
            "steps": [{"id": sid, "label": label, "desc": desc, "status": "pending", "detail": ""}
                      for sid, label, desc in _STEP_PLANS[mode]],
            "tts": {},
            "app_mode": config.APP_MODE,
        })
    threading.Thread(target=_run, args=(mode,), daemon=True).start()
    return state()


def _run(mode):
    """后台执行步骤：任一步失败即停止并记录原因（不影响界面可访问）。"""
    try:
        for step in _STEP_PLANS[mode]:
            sid = step[0]
            _set_step(sid, "running", "")
            try:
                detail = _execute(sid, mode)
            except Exception as e:
                _set_step(sid, "failed", str(e)[:200])
                _fail(f"「{step[1]}」失败：{e}")
                return
            _set_step(sid, "done", detail or "")
            if sid == "services":
                # 服务已拉起：允许进入正式界面，后面的就绪等待在后台继续
                with _lock:
                    _state["done"] = True
                    _state["starting"] = False
                    _state["finished_at"] = time.time()
    except Exception as e:
        _fail(str(e)[:200])


def _fail(reason):
    with _lock:
        _state["error"] = reason
        _state["starting"] = False
        _state["running"] = False
        _state["done"] = True   # 失败也要放行界面（用户可在设置里重试 / 看日志）
        _state["finished_at"] = time.time()
    try:
        from core import logger
        logger.error("图形启动器：启动流程失败：" + reason)
    except Exception:
        pass


def _set_step(step_id, status, detail=""):
    with _lock:
        for s in _state["steps"]:
            if s["id"] == step_id:
                s["status"] = status
                s["detail"] = detail or s["detail"]
                break


def _execute(step_id, mode):
    """执行单个步骤，返回该步骤的补充说明（detail）。"""
    if step_id == "check":
        return _step_check()
    if step_id == "config":
        return _step_config()
    if step_id == "services":
        return _step_services(mode)
    if step_id == "tts":
        return _step_tts_wait(mode)
    return ""


def _step_check():
    """环境预检：缺组件不退出（图形启动器在进程内运行），只把问题写在步骤里。"""
    try:
        import launcher as launcher_mod
        missing = launcher_mod.check_components()
    except Exception as e:
        return f"预检跳过：{e}"
    if missing:
        raise RuntimeError("关键组件缺失：" + "、".join(missing) + "（请先运行 setup\\install.bat）")
    return "关键文件与依赖齐全"


def _step_config():
    """读取并应用服务地址配置（Web 端口已由当前服务绑定，这里不再改写）。"""
    with _lock:
        svc = _backends["services"]
    cfg = svc.load_launcher_config()
    svc.apply_api_urls(cfg)
    return "启动配置已应用"


def _step_services(mode):
    """真正拉起服务：复用启动器里的模式逻辑（Lite / 标准）。"""
    with _lock:
        svc = _backends["services"]
        mdl = _backends["models"]
    cfg = svc.load_launcher_config()
    svc.apply_api_urls(cfg)
    import launcher as launcher_mod
    launcher_mod.apply_mode_and_start(mode, svc, mdl, cfg)
    if mode == "lite":
        return "已按 Lite 模式拉起语音合成（未启动 Ollama / 语音识别）"
    return "已按标准模式拉起本地服务（语音识别后台加载中）"


def _step_tts_wait(mode):
    """后台等待语音服务就绪：只把状态写进 state，不阻塞进入界面。"""
    with _lock:
        svc = _backends["services"]

    def _watch():
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                st = svc.gpt_sovits_status()
            except Exception:
                st = {}
            with _lock:
                _state["tts"] = {
                    "ready": bool(st.get("ready")),
                    "running": bool(st.get("running")),
                    "port": st.get("port"),
                    "url": st.get("url"),
                    "last_error": st.get("last_error") or "",
                    "checked_at": time.time(),
                }
                if st.get("ready"):
                    _state["running"] = False
                    return
            time.sleep(2.0)
        with _lock:
            _state["running"] = False

    threading.Thread(target=_watch, daemon=True).start()
    try:
        st = svc.gpt_sovits_status()
        port = st.get("port")
    except Exception:
        port = None
    return (f"语音服务监听端口 {port}，正在后台加载模型" if port else "语音服务正在后台加载模型")
