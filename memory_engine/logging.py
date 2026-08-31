# memory_engine/logging.py
# 引擎日志与调试输出：
#   - 运行日志：始终写入 memory_engine.log（文件），记录关键节点的运行结果；
#   - 调试输出：进入调试模式后（引擎 set_debug / 应用 config.DEBUG_MODE）额外在控制台
#     打印检索过程（搜索来源、候选、得分、置信度、仲裁等），便于开发者使用。
from __future__ import annotations

import logging
import os

import memory_engine.config as cfg

_LOGGER_NAME = "memory_engine"
_configured = False
_debug = False


def _ensure():
    global _configured
    if _configured:
        return
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        os.makedirs(os.path.dirname(cfg.LOG_FILE) or ".", exist_ok=True)
        fh = logging.FileHandler(cfg.LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass
    _configured = True


def set_debug(enabled: bool) -> None:
    global _debug
    _debug = bool(enabled)


def is_debug() -> bool:
    """调试模式：引擎开关 或 应用自身的 DEBUG_MODE（进入应用调试模式即联动显示）。"""
    if _debug:
        return True
    try:
        from core import config as app_config
        return bool(getattr(app_config, "DEBUG_MODE", False))
    except Exception:
        return False


def info(msg: str) -> None:
    _ensure()
    logging.getLogger(_LOGGER_NAME).info(msg)


def debug(msg: str) -> None:
    """调试级别：写入日志文件；调试模式下同时打印到控制台。"""
    _ensure()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.debug(msg)
    if is_debug():
        print(f"[memory-engine] {msg}")


def warn(msg: str) -> None:
    _ensure()
    logging.getLogger(_LOGGER_NAME).warning(msg)
    if is_debug():
        print(f"[memory-engine][warn] {msg}")


def log_file() -> str:
    return cfg.LOG_FILE
