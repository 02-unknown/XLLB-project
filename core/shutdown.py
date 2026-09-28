# core/shutdown.py
# 统一退出流程（shutdown manager）：按固定顺序收尾，避免「只依赖 daemon 线程自动结束」导致
# 归档任务丢失、数据库在半写状态下被进程退出打断、临时文件残留。
#
# 退出顺序（每一步都记录到报告与日志，失败不影响后续步骤继续执行）：
#   1) 停止接受新请求            —— 由 Web 层关闭 HTTP 服务（调用方传入 stop_server 回调）
#   2) 停止新归档任务            —— 记忆门禁进入「关闭中」：不再接受新的写入
#   3) 等待当前任务完成 / drain  —— 等待后台归档队列排空（超时则记录剩余任务数）
#   4) 关闭记忆引擎与数据库      —— service.shutdown() → engine.close()
#   5) 停止 TTS 流式任务         —— 由 Web 层清理其流式会话（调用方传入回调）
#   6) 清理运行时临时文件        —— runtime.cleanup_runtime()
import threading
import time
import traceback

_STATE_LOCK = threading.RLock()
_state = {"done": False, "running": False, "report": None}


def shutdown(reason: str = "normal", stop_server=None, extra_steps=None) -> dict:
    """执行统一退出流程；可安全重复调用（第二次直接返回上次报告）。

    reason：退出原因（normal / restart / error），写入日志与报告便于排查；
    stop_server：可选回调，用于停止接受新请求（关闭 HTTP 服务 / 停止监听）；
    extra_steps：可选 [(名称, 回调)] 列表，在「关闭引擎」之后、清理临时文件之前执行
                 （例如停止 TTS 流式会话、清理运行中的音乐下载）。
    """
    with _STATE_LOCK:
        if _state["done"]:
            return _state["report"] or {"reason": reason, "steps": [], "already": True}
        if _state["running"]:
            return {"reason": reason, "steps": [], "busy": True}
        _state["running"] = True

    report = {"reason": reason, "steps": [], "started": time.time()}

    def step(name, fn):
        t0 = time.time()
        ok, detail = True, ""
        if fn is not None:
            try:
                out = fn()
                if isinstance(out, str):
                    detail = out
                elif out is not None:
                    detail = str(out)
            except Exception as e:
                ok = False
                detail = f"{e}"
                traceback.print_exc()
        report["steps"].append({"name": name, "ok": ok, "ms": int((time.time() - t0) * 1000),
                                "detail": detail})
        _log(f"退出流程[{reason}] · {name}：{'完成' if ok else '失败'} {detail}")

    # 1) 停止接受新请求
    step("停止接受新请求", stop_server)

    # 2) 停止新的归档任务
    step("停止新的归档写入", _begin_memory_shutdown)

    # 3) 等待后台归档队列排空
    step("等待后台归档队列排空", _drain_memory)

    # 4) 关闭记忆引擎与数据库
    step("关闭记忆引擎与数据库", _close_memory)

    # 5) 其它组件（TTS 流式任务等）
    for name, fn in (extra_steps or []):
        step(name, fn)

    # 6) 清理运行时临时文件
    step("清理运行时临时文件", _cleanup_runtime)

    report["ms"] = int((time.time() - report["started"]) * 1000)
    with _STATE_LOCK:
        _state["done"] = True
        _state["running"] = False
        _state["report"] = report
    _log(f"退出流程[{reason}] 全部完成，耗时 {report['ms']}ms")
    return report


def _begin_memory_shutdown():
    """让记忆门禁进入关闭中：不再接受新的写入（不影响已排队的任务继续落库）。"""
    from memory_engine import service as mem_service
    st = mem_service.begin_shutdown("程序退出中")
    return f"插件启用={st.get('plugin_enabled')} 引擎{'可用' if st.get('ready') else '未初始化'}"


def _drain_memory():
    """等待后台归档队列排空（最多 ARCHIVE_DRAIN_TIMEOUT 秒），返回剩余任务数。"""
    from memory_engine import service as mem_service
    eng = mem_service.engine_ref()
    if eng is None:
        return "无引擎"
    pending = eng.drain_archives()
    if pending:
        return f"仍有 {pending} 条待归档任务未完成（已超时，任务随进程结束丢弃）"
    return "归档队列已排空"


def _close_memory():
    from memory_engine import service as mem_service
    eng = mem_service.engine_ref()
    if eng is None:
        return "无引擎"
    st = eng.status() if eng.is_ready() else {}
    mem_service.shutdown()          # 内部调用 engine.close()（关库前再 drain 一次）
    return (f"引擎已关闭（活跃 {st.get('active_count', 0)} 条 / "
            f"归档 {st.get('archive_count', 0)} 条，数据保留在磁盘）")


def _cleanup_runtime():
    from core import runtime
    runtime.cleanup_runtime()
    return "runtime 临时文件已清理"


def _log(msg: str) -> None:
    try:
        from core import logger
        logger.info(msg)
    except Exception:
        pass
    print(f"[shutdown] {msg}")


def report() -> dict:
    with _STATE_LOCK:
        return _state["report"] or {}


def is_done() -> bool:
    with _STATE_LOCK:
        return bool(_state["done"])


def is_running() -> bool:
    """退出流程是否正在执行（用于把「关闭监听 socket 引发的 OSError」当成正常收尾）。"""
    with _STATE_LOCK:
        return bool(_state["running"])


def reset_for_test() -> None:
    """仅供测试：重置退出流程状态。"""
    with _STATE_LOCK:
        _state.update({"done": False, "running": False, "report": None})
