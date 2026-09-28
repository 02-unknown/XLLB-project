# web/app_lifecycle.py —— 关闭窗口 = 完全关闭程序（图形启动器模式）。
#
# 三层检测，任意一层命中都会完整退出（卸载模型权重 + 停止本地服务 + 收尾 + 清理缓存）：
#   1) 窗口进程检查点：界面窗口由启动器带独立配置目录打开，后端持有该进程句柄，
#      进程一退出（点 X 关窗）立即退出 —— 不依赖页面脚本，最可靠；
#   2) 关闭信标：页面真正关闭时（pagehide 且非 bfcache 恢复）POST /api/app/window-closed；
#   3) 兜底空闲：既没有心跳、也没有任何 HTTP 请求超过 IDLE_SECONDS，判定窗口已消失。
#
# 清理本身交给**独立进程**执行（见 _spawn_cleanup_helper）：主进程即使中途退出/被杀，
# 清理仍会完成，不会出现「关了程序但 GPT-SoVITS / Ollama 还占着显存」。
# 只在「图形启动器拉起的实例」里启用（config.LAUNCH_VIA_GUI），
# 用 app.py / 控制台版启动时不会自动退出，避免影响调试与常驻用法。
import os
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 心跳间隔由前端决定；服务端只判断「多久没动静算窗口已经没了」
CLOSE_GRACE_SECONDS = 2.0      # 收到关闭信标后的宽限期（页面跳转/刷新会在这期间重新心跳）
IDLE_SECONDS = 90.0            # 兜底：完全没有心跳、也没有任何请求（旧版页面 / 浏览器被强杀）
TICK_SECONDS = 1.0
HARD_EXIT_SECONDS = 20.0       # 退出流程硬超时：超时强制结束进程，绝不留着服务占资源
CLEANUP_JOIN_SECONDS = 15.0    # 「卸载权重 + 停服务」这一步最多等多久，超时就继续收尾退出

_lock = threading.RLock()
_state = {
    "enabled": False,
    "seen_ever": False,
    "last_beat": 0.0,
    "last_request": 0.0,
    "close_signaled_at": 0.0,
    "beats": 0,
    "requests": 0,
    "closing": False,
    "shutdown_called": False,
    "shutdown_reason": "",
    "finished": False,
}
_shutdown_hook = None          # 测试可注入；默认走 core.shutdown + 退出进程
_stop_server = None            # 退出流程第一步：停止接受新请求（由 web 层注入）
_extra_steps = []              # 退出流程的额外步骤（由 web 层注入，例如停止 TTS 流式任务）
_thread = None


def enable(enabled=True, stop_server=None, extra_steps=None):
    """启用/停用「关闭窗口即退出程序」（仅图形启动器模式下启用）。"""
    global _thread, _stop_server, _extra_steps
    with _lock:
        _state["enabled"] = bool(enabled)
        if stop_server is not None:
            _stop_server = stop_server
        if extra_steps is not None:
            _extra_steps = list(extra_steps)
        started = _thread is not None and _thread.is_alive()
    if enabled and not started:
        _thread = threading.Thread(target=_watchdog, name="app-lifecycle", daemon=True)
        _thread.start()
    return _state["enabled"]


def is_enabled():
    with _lock:
        return bool(_state["enabled"])


def beat():
    """页面心跳：说明至少还有一个窗口开着。"""
    with _lock:
        now = time.time()
        _state["last_beat"] = now
        _state["last_request"] = now
        _state["seen_ever"] = True
        _state["beats"] += 1
        _state["close_signaled_at"] = 0.0        # 说明只是页面跳转/刷新，取消待关闭
        _state["closing"] = False
    return state()


def touch():
    """任何 HTTP 请求都算「窗口还活着」：接口每几秒就被轮询一次。

    这样即使页面是旧版本（没有心跳脚本），只要界面还开着就一定有请求；
    窗口关掉后请求停止，看门狗仍能判定「窗口已关闭」并完整退出程序。
    """
    with _lock:
        now = time.time()
        _state["last_request"] = now
        _state["seen_ever"] = True
        _state["requests"] += 1
    return None


def window_closed():
    """窗口关闭信标（pagehide 且非 bfcache 恢复时上报）。"""
    with _lock:
        _state["close_signaled_at"] = time.time()
    return state()


def state():
    with _lock:
        st = dict(_state)
    st["now"] = time.time()
    return st


def set_shutdown_hook(fn):
    """注入退出动作（仅测试使用；传 None 恢复默认）。"""
    global _shutdown_hook
    with _lock:
        _shutdown_hook = fn
    return True


def shutdown_now(reason="user-confirm"):
    """立即执行完整退出（供「完全退出程序」按钮与关窗口看门狗共用）。"""
    with _lock:
        if _state["shutdown_called"]:
            return False
        _state["shutdown_called"] = True
        _state["shutdown_reason"] = reason
    _do_shutdown(reason)
    return True


def _suspend_gap_detected():
    """检测「系统刚从睡眠 / 休眠中恢复」。

    长时间没动静也可能是整机睡了一觉（此时浏览器定时器全部停摆），不能当成「窗口关了」。
    做法：比较两个系统计时器的差值——GetTickCount64 含睡眠时间，
    QueryUnbiasedInterruptTime 不含；两者差值突然变大即说明刚睡醒。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        up = float(k32.GetTickCount64()) / 1000.0
        unbiased = ctypes.c_ulonglong()
        if not k32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return False
        gap = up - float(unbiased.value) / 1e7
        prev = _SUSPEND.get("gap")
        _SUSPEND["gap"] = gap
        return prev is not None and (gap - prev) > 5.0
    except Exception:
        return False


_SUSPEND = {"gap": None}


def _watchdog():
    """看门狗：判断窗口是否真的都关了，是的话执行退出。"""
    while True:
        time.sleep(TICK_SECONDS)
        now = time.time()
        if _suspend_gap_detected():
            # 刚从睡眠恢复：把存活时间刷新一下，避免误判成「窗口已关闭」
            with _lock:
                _state["last_beat"] = now
                _state["last_request"] = now
                _state["close_signaled_at"] = 0.0
            continue
        with _lock:
            if not _state["enabled"] or _state["shutdown_called"]:
                continue
            if not _state["seen_ever"]:
                continue                      # 窗口还没连上来过，不能判定
            # 「窗口还活着」的证据：心跳 或 任何 HTTP 请求（取较新的那个）
            last = max(_state["last_beat"], _state["last_request"])
            closed_at = _state["close_signaled_at"]
            reason = ""
            if closed_at and now - closed_at >= CLOSE_GRACE_SECONDS and now - last >= CLOSE_GRACE_SECONDS:
                # 收到了「窗口关闭」且之后一直没有动静 → 窗口确实关了
                reason = "window-closed"
            elif now - last >= IDLE_SECONDS:
                # 完全没有动静很久（旧版页面没有心跳脚本 / 浏览器被强杀）：兜底退出，
                # 避免「窗口早关了但服务与显存还一直占着」
                reason = "window-idle"
            if not reason:
                continue
            _state["shutdown_called"] = True
            _state["shutdown_reason"] = reason
        _do_shutdown(reason)


def _spawn_cleanup_helper():
    """启动一个**独立进程**做「卸载模型权重 + 停服务 + 复核」。

    为什么用独立进程：清理期间主进程可能因为其它原因退出（例如关闭监听 socket 引发异常、
    用户强杀窗口），如果清理跑在主进程的线程里就会被一起终止，表现为「关了程序但服务/显存还在」。
    独立进程不受主进程退出影响，一定能把它做完，并把过程写进日志。
    """
    import subprocess
    import sys
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from core import logger, services;\n"
        "logger.init_log(r'%s');\n"
        "logger.info('独立清理进程：开始');\n"
        "r = services.stop_all(force=True);\n"
        "logger.info('独立清理进程：' + str(r));\n"
    ) % (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "runtime", "logs"))
    try:
        return subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None


def _do_shutdown(reason):
    """执行退出：先让「独立清理进程」卸载模型权重并停止本地服务，再做统一收尾，最后结束进程。

    三层保障：
      1) 独立进程负责清理 —— 主进程即使中途退出/被杀，清理也会完成；
      2) 主进程限时等待（CLEANUP_JOIN_SECONDS），不因为清理慢而卡住关闭；
      3) 硬超时（HARD_EXIT_SECONDS）强制结束主进程。
    """
    hook = None
    with _lock:
        hook = _shutdown_hook
    if hook is not None:
        try:
            hook(reason)
        except Exception:
            pass
        return

    def _log(msg, level="info"):
        try:
            from core import logger
            getattr(logger, level)(msg)
        except Exception:
            pass

    # 硬超时兜底：防止个别步骤卡死导致「关了但没清理」
    try:
        timer = threading.Timer(HARD_EXIT_SECONDS, _force_exit, args=(reason,))
        timer.daemon = True
        timer.start()
    except Exception:
        pass

    _log(f"关闭程序（{reason}）：开始卸载模型权重并停止本地服务")
    t0 = time.time()
    helper = _spawn_cleanup_helper()
    if helper is not None:
        _log(f"已启动独立清理进程（PID {helper.pid}），等待其完成（最多 {int(CLEANUP_JOIN_SECONDS)} 秒）")
        try:
            helper.wait(timeout=CLEANUP_JOIN_SECONDS)
            _log(f"独立清理进程已完成（用时 {int((time.time() - t0) * 1000)}ms）")
        except Exception:
            _log(f"独立清理进程仍在运行（超过 {int(CLEANUP_JOIN_SECONDS)} 秒），"
                 f"它会在后台继续完成清理；本进程现在继续退出收尾", "warn")
    else:
        # 起不了独立进程时退回本进程内清理（限时）
        holder = {}

        def _pre_stop():
            try:
                holder["detail"] = _stop_services()
            except Exception as e:
                holder["error"] = str(e)

        try:
            worker = threading.Thread(target=_pre_stop, name="stop-services", daemon=True)
            worker.start()
            worker.join(CLEANUP_JOIN_SECONDS)
            if worker.is_alive():
                _log(f"停止本地服务超时（已等待 {int(CLEANUP_JOIN_SECONDS)} 秒），继续退出收尾", "warn")
            elif holder.get("error"):
                _log(f"停止本地服务失败：{holder['error']}", "error")
            else:
                _log(f"关闭程序（{reason}）：{holder.get('detail') or '已处理'}")
        except Exception as e:
            _log(f"停止本地服务异常：{e}", "error")

    try:
        from core import shutdown as shutdown_mod
        steps = list(_extra_steps) + [("停止本地服务（GPT-SoVITS / Ollama）", _stop_services)]
        # shutdown() 自己会逐步写日志，这里不再重复打印每一步
        shutdown_mod.shutdown(reason, stop_server=_stop_server, extra_steps=steps)
    except Exception as e:
        _log(f"统一退出流程异常：{e}", "error")

    _log(f"窗口已关闭，程序退出（{reason}）")
    with _lock:
        _state["finished"] = True
    os._exit(0)


def wait_finished(timeout=20.0):
    """等待退出流程真正收尾完成（供启动器主线程在退出前等一下，避免把日志 / 清理截断）。

    返回 True 表示已收尾（马上会 os._exit）；False 表示没有在退出或已超时。
    """
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        with _lock:
            if _state["finished"]:
                return True
            started = bool(_state["shutdown_called"])
        if not started:
            return False
        time.sleep(0.2)
    return False


def _force_exit(reason):
    """硬超时兜底：强制退出（尽量先记一条日志）。"""
    try:
        from core import logger
        logger.warn(f"退出流程超时（{reason}），强制结束进程")
    except Exception:
        pass
    os._exit(0)


def _stop_services():
    from core import services
    return services.stop_all()


def reset_for_test():
    """仅测试使用：恢复初始状态并停掉看门狗判定。"""
    global _thread, _stop_server, _extra_steps
    with _lock:
        _state.update({
            "enabled": False, "seen_ever": False, "last_beat": 0.0, "last_request": 0.0,
            "close_signaled_at": 0.0, "beats": 0, "requests": 0,
            "closing": False, "shutdown_called": False, "shutdown_reason": "",
        })
        _thread = None
        _stop_server = None
        _extra_steps = []
