# web/confirm.py
# 危险操作的一次性确认令牌（confirm token）——纯标准库实现，供 Web 层使用。
#
# 定位（重要）：这不是用户认证系统，而是「防绕过界面确认 + 防跨站请求」的加固：
#   1) 危险操作（清空对话、删除记忆、插件重载/启停、二次确认动作、写入敏感设置）
#      必须先调 POST /api/confirm/prepare 拿到一次性令牌，再带令牌发出真正的请求；
#   2) 令牌是「一次性」的：通过校验后立即作废，重放同一个令牌一定失败；
#   3) 令牌绑定 op（操作类型）/ target（插件名、记忆 id 等资源名）/ client（客户端标识）
#      与过期时间，任何一个不匹配都拒绝；
#   4) 线程安全（threading.Lock）+ 容量上限（默认 200 条），避免内存无限增长。
#
# 它能防什么：脚本/第三方页面在用户不知情时直接调用危险接口（没有界面确认、没有令牌）、
#            跨站表单提交、以及拿到令牌后重放。
# 它防不了什么：本机用户自己（本地无认证时任何本地进程都能自己申请令牌）。
import secrets
import threading
import time

# 容量上限：超过后先清过期，再按签发时间淘汰最旧的记录
MAX_TOKENS = 200
# 默认有效期（秒）
DEFAULT_TTL = 120.0

_lock = threading.Lock()
# token -> {"op","target","client","issued","ttl"}
_tokens = {}


def issue(op: str, target: str = "", client: str = "", ttl: float = DEFAULT_TTL) -> str:
    """签发一个一次性确认令牌，返回令牌字符串。

    op：操作类型（如 history.clear / memory.delete / plugins.reload / plugin.action …）
    target：操作目标（插件名、记忆 id、`插件名:动作名` 等；无目标时用 "" 或 "*"）
    client：客户端标识（前端页面的 X-XLLB-Client 值），防止令牌被别的客户端拿去用
    ttl：有效期秒数（默认 120）
    """
    token = secrets.token_urlsafe(24)
    now = time.time()
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    with _lock:
        _purge_locked(now)
        _trim_locked()
        _tokens[token] = {
            "op": str(op or ""),
            "target": str(target or ""),
            "client": str(client or ""),
            "issued": now,
            "ttl": ttl,
        }
    return token


def consume(op: str, target: str = "", client: str = "", token: str = "") -> tuple:
    """校验并作废一个令牌，返回 (是否通过, 中文原因)。

    校验顺序：存在 → 未过期 → op / target / client 全部匹配 → 通过后立即删除（一次性）。
    """
    token = str(token or "").strip()
    if not token:
        return False, "需要先在界面确认（确认令牌缺失或已失效）"
    now = time.time()
    with _lock:
        _purge_locked(now)
        rec = _tokens.get(token)
        if rec is None:
            return False, "需要先在界面确认（确认令牌缺失或已失效）"
        if now - rec["issued"] > rec["ttl"]:
            _tokens.pop(token, None)
            return False, "确认令牌已过期，请重新点击该操作后确认"
        if rec["op"] != str(op or ""):
            return False, "确认令牌与当前操作不匹配（op 不符），请重新确认"
        if rec["target"] != str(target or ""):
            return False, "确认令牌与当前操作对象不匹配（target 不符），请重新确认"
        if rec["client"] != str(client or ""):
            return False, "确认令牌不属于当前客户端（client 不符），请重新确认"
        # 一次性：校验通过立刻作废，重放必失败
        _tokens.pop(token, None)
    return True, ""


def purge() -> int:
    """清理过期令牌，返回清理条数。"""
    with _lock:
        return _purge_locked(time.time())


def _purge_locked(now: float) -> int:
    """（需持锁）删除已过期令牌。"""
    stale = [t for t, rec in _tokens.items() if now - rec["issued"] > rec["ttl"]]
    for t in stale:
        _tokens.pop(t, None)
    return len(stale)


def _trim_locked() -> None:
    """（需持锁）容量上限保护：超出上限时淘汰最早签发的令牌。"""
    overflow = len(_tokens) - MAX_TOKENS + 1  # +1：为即将写入的新令牌留位置
    if overflow <= 0:
        return
    for token, _rec in sorted(_tokens.items(), key=lambda kv: kv[1]["issued"])[:overflow]:
        _tokens.pop(token, None)


def peek_count() -> int:
    """当前保留的令牌数量（供日志 / 测试观察容量控制是否生效）。"""
    with _lock:
        return len(_tokens)


def reset_for_test() -> None:
    """仅供测试：清空全部令牌。"""
    with _lock:
        _tokens.clear()
