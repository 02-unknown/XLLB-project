# memory_engine/service.py
# 记忆服务门禁：所有记忆访问（核心对话检索/写入、插件、Web 管理接口）的唯一判定入口。
#
# 为什么需要它：核心聊天路径会直接 import memory_engine，如果各处自行 get_engine() 并隐式初始化，
# 就会出现「插件已停用但记忆仍在读写」「readonly 仍可能按 readwrite 归档」这类绕过插件生命周期的问题。
# 这里把三件事收口到一处：
#
#   1) 插件是否启用：由「上下文记忆库」插件在 on_load / on_unload 时 register / unregister；
#   2) 当前上下文模式（readonly / readwrite）：readonly 在引擎底层同样强制（不只是插件设置）；
#   3) 访问级别（read=查看与检索 / write=自动归档 / manage=人工增删改与初始化），
#      每一级都返回结构化拒绝原因，Web API 与前端据此如实告知用户「当前模式 / 为什么被拒绝」。
#
# 缺省状态（没有插件注册时，例如纯引擎单元测试）为「未启用 + readonly」：
# 任何写入都必须显式通过 set_mode("readwrite") 或插件启用后注册来放开。
from __future__ import annotations

import threading

PLUGIN_NAME = "上下文记忆库"
MODE_READONLY = "readonly"
MODE_READWRITE = "readwrite"

# 访问级别
LEVEL_READ = "read"        # 查看 / 检索（L0 上下文 + 长期记忆）
LEVEL_TURN = "turn"        # 记录对话回合（只写 L0 会话上下文；readonly 同样允许）
LEVEL_WRITE = "write"      # 自动归档写入长期记忆
LEVEL_MANAGE = "manage"    # 人工管理：新增 / 编辑 / 删除 / 初始化

_lock = threading.RLock()
_state = {
    "plugin_enabled": False,
    "engine": None,
    "mode": MODE_READONLY,
    # 预留的管理权限位：接入用户/角色体系时在此判定「当前用户是否可管理记忆」，
    # 目前单用户本地应用默认放开（仍受插件启用状态与 readonly 模式限制）。
    "manage_allowed": True,
    "suspended": True,
    "reason": "记忆插件未启用",
}


class WriteBlocked(RuntimeError):
    """引擎在底层拒绝写入时抛出（插件停用 / 只读模式 / 未初始化）。"""

    def __init__(self, reason: str, level: str = LEVEL_WRITE):
        super().__init__(reason)
        self.reason = reason
        self.level = level


def _engine():
    return _state.get("engine")


def register(engine, mode: str = None) -> dict:
    """「上下文记忆库」插件启用（on_load）时注册引擎与模式，解除挂起。"""
    with _lock:
        _state["engine"] = engine
        _state["plugin_enabled"] = True
        _state["suspended"] = False
        _state["reason"] = ""
        if mode:
            _state["mode"] = MODE_READWRITE if str(mode).lower() == MODE_READWRITE else MODE_READONLY
        _apply_policy_locked()
    # 解除引擎自身的挂起（幂等）：插件停用期间引擎处于 suspend 状态
    if engine is not None:
        try:
            engine.resume(_state["mode"])
        except Exception:
            pass
    return status()


def unregister(reason: str = "记忆插件已停用") -> dict:
    """「上下文记忆库」插件停用（on_unload）时注销：核心检索与写入立即停止。

    只挂起引擎（suspend），不销毁数据、不关闭数据库连接，便于再次启用后立即恢复。
    """
    with _lock:
        _state["plugin_enabled"] = False
        _state["suspended"] = True
        _state["reason"] = reason or "记忆插件已停用"
        eng = _state.get("engine")
    if eng is not None:
        try:
            eng.suspend(_state["reason"])
        except Exception:
            pass
    return status()


def set_mode(mode: str) -> dict:
    """更新上下文模式（插件保存设置时调用），只读模式在引擎底层同样强制。"""
    with _lock:
        _state["mode"] = MODE_READWRITE if str(mode).lower() == MODE_READWRITE else MODE_READONLY
        if _state["plugin_enabled"]:
            _state["reason"] = ""
        _apply_policy_locked()
        return status()


def set_manage_allowed(allowed: bool) -> dict:
    """设置「当前用户是否可管理记忆」（权限体系接入点）。"""
    with _lock:
        _state["manage_allowed"] = bool(allowed)
        _apply_policy_locked()
        return status()


def _apply_policy_locked() -> None:
    """把门禁状态下发给引擎：引擎自身也会强制（不依赖调用方守规矩）。"""
    eng = _state.get("engine")
    if eng is None:
        return
    write_ok = _state["plugin_enabled"] and not _state["suspended"] and _state["mode"] == MODE_READWRITE
    manage_ok = write_ok and _state["manage_allowed"]
    try:
        eng.apply_policy(write=write_ok, manage=manage_ok)
    except Exception:
        pass


def status() -> dict:
    """门禁状态快照（供 Web API / 前端展示「当前模式与可用能力」）。"""
    with _lock:
        eng = _state["engine"]
        ready = bool(eng is not None and _call_bool(eng, "is_ready"))
        active = bool(eng is not None and _call_bool(eng, "is_active"))
        mode = _state["mode"]
        can_read = bool(_state["plugin_enabled"] and active)
        can_write = bool(can_read and mode == MODE_READWRITE)
        can_manage = bool(can_write and _state["manage_allowed"])
        return {
            "plugin_enabled": _state["plugin_enabled"],
            "ready": ready,
            "active": active,
            "suspended": _state["suspended"],
            "mode": mode,
            "level": "完整权限" if mode == MODE_READWRITE else "只读（仅本次会话）",
            "can_read": can_read,
            "can_write": can_write,
            "can_manage": can_manage,
            "manage_allowed": _state["manage_allowed"],
            "reason": _state["reason"] or "",
        }


def _call_bool(obj, name: str) -> bool:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def check(level: str = LEVEL_READ):
    """按访问级别判定：返回 (engine|None, reason, status)。

    reason 为空字符串表示允许；否则是给用户看的中文拒绝原因。
    """
    with _lock:
        st = status()
        eng = _state["engine"]
    if not st["plugin_enabled"]:
        return None, "记忆插件未启用（可在「设置 → 插件管理」中启用「上下文记忆库」）", st
    if eng is None or not st["ready"]:
        return None, "记忆引擎未初始化", st
    if st["suspended"]:
        return None, st["reason"] or "记忆引擎已挂起", st
    if level == LEVEL_READ or level == LEVEL_TURN:
        # 回合记录（LEVEL_TURN）只写进程内 L0 会话上下文，readonly 下依然允许；
        # 是否归档到长期记忆由引擎按 mode 自行判定，这里不需要额外限制。
        return eng, "", st
    if st["mode"] != MODE_READWRITE:
        if level == LEVEL_WRITE:
            return None, "当前为只读模式：对话只写入本次会话上下文，不写入长期记忆", st
        return None, "当前为只读模式：只允许查看，不允许新增 / 修改 / 删除长期记忆", st
    if level == LEVEL_MANAGE and not st["manage_allowed"]:
        return None, "当前账号没有记忆管理权限", st
    return eng, "", st


def get_engine_for_read():
    """核心检索路径使用：允许时返回引擎，否则 None（绝不隐式初始化）。"""
    eng, _reason, _st = check(LEVEL_READ)
    return eng


def get_engine_for_write(action: str = "archive"):
    """长期归档写入路径使用：返回 (engine|None, reason)，reason 为空表示允许。"""
    return check(LEVEL_WRITE)[:2]


def get_engine_for_turn():
    """对话回合记录路径使用（只写 L0 会话上下文，readonly 也允许）：返回 engine | None。"""
    return check(LEVEL_TURN)[0]


def get_engine_for_manage(action: str = "manage"):
    """管理操作（Web 记忆管理接口）使用：返回 (engine|None, reason, status)。"""
    return check(LEVEL_MANAGE)


def shutdown() -> None:
    """进程退出：注销并关闭引擎（由统一的 shutdown 流程调用）。"""
    with _lock:
        eng = _state["engine"]
        _state.update({"plugin_enabled": False, "suspended": True,
                       "reason": "服务已关闭", "engine": None})
    if eng is not None:
        try:
            eng.close()
        except Exception:
            pass


def begin_shutdown(reason: str = "程序退出中") -> dict:
    """进入关闭流程：不再接受「新的」写入任务，但已排队的归档仍会写完后才关库。"""
    with _lock:
        eng = _state["engine"]
        _state["reason"] = reason
    if eng is not None:
        try:
            eng.stop_accepting(reason)
        except Exception:
            pass
    return status()


def engine_ref():
    """返回当前引擎实例（可能未初始化 / 已挂起），供退出流程 drain 与关闭、以及
    「清理进程内临时缓存」这类不涉及门禁语义的操作使用。

    门禁尚未绑定引擎时（插件未加载 / 核心脚本场景）回退到进程内单例：
    这些调用只做临时缓存清理与关闭，不做检索或写库，因此不构成对插件启停的绕过。
    """
    with _lock:
        eng = _state["engine"]
    if eng is not None:
        return eng
    try:
        from memory_engine import get_engine
        return get_engine()
    except Exception:
        return None


def plugin_enabled() -> bool:
    with _lock:
        return bool(_state["plugin_enabled"])


def current_mode() -> str:
    with _lock:
        return _state["mode"]


def reset_for_test() -> None:
    """仅供测试：清空门禁状态（不关闭引擎）。"""
    with _lock:
        _state.update({"plugin_enabled": False, "engine": None, "mode": MODE_READONLY,
                       "manage_allowed": True, "suspended": True, "reason": "记忆插件未启用"})
