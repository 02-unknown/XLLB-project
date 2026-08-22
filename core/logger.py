# core/logger.py
# 轻量运行日志：记录运行状态与报错，带大小轮转与保留上限，避免日志无限增长。
# 用法：
#   from core import logger
#   logger.init_log(os.path.join(config.RUNTIME_DIR, "logs"))
#   logger.info("...") / logger.warn("...") / logger.error("...")
# 日志文件：runtime/logs/xiaolongluo.log
#   - 单文件超过 2MB 自动轮转（xiaolongluo.log -> .1 -> .2 -> .3）；
#   - 最多保留 4 个文件（约 8MB），更旧的自动删除；
# 所有写入均容错（失败静默忽略），绝不影响主程序运行。
import os
import threading
import time

LOG_DIR = None
_CURRENT = None
_LOCK = threading.Lock()
_MAX_BYTES = 2 * 1024 * 1024   # 单文件上限 2MB
_MAX_BACKUPS = 3               # 最多保留 3 个轮转副本（共 4 个文件）
LOG_NAME = "xiaolongluo.log"


def init_log(log_dir):
    """初始化日志目录（幂等；重复调用会切换到最新目录）。"""
    global LOG_DIR, _CURRENT
    try:
        os.makedirs(log_dir, exist_ok=True)
        LOG_DIR = log_dir
        _CURRENT = os.path.join(log_dir, LOG_NAME)
        _cleanup_old()
    except Exception:
        pass


def _rotate():
    if not LOG_DIR:
        return
    try:
        base = os.path.join(LOG_DIR, LOG_NAME)
        for i in range(_MAX_BACKUPS - 1, 0, -1):
            src = f"{base}.{i}"
            dst = f"{base}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(base):
            os.replace(base, f"{base}.1")
    except Exception:
        pass


def _cleanup_old():
    if not LOG_DIR:
        return
    try:
        base = os.path.join(LOG_DIR, LOG_NAME)
        for i in range(_MAX_BACKUPS + 1, 50):
            p = f"{base}.{i}"
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass


def log(level, msg):
    try:
        with _LOCK:
            if not _CURRENT:
                return
            if os.path.exists(_CURRENT) and os.path.getsize(_CURRENT) > _MAX_BYTES:
                _rotate()
                _cleanup_old()
            line = "[%s] [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
            with open(_CURRENT, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def info(msg):
    log("INFO", msg)


def warn(msg):
    log("WARN", msg)


def error(msg):
    log("ERROR", msg)
