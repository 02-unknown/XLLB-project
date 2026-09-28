# core/version.py —— 应用版本号统一入口。
# 版本号只写在项目根目录的 version.txt 里（代码里不再出现硬编码版本号），
# 命令行、Web 服务标识、界面标题与「设置 → 通用设置」都从这里读取同一份值。
import re
from pathlib import Path

# 项目根目录（core/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "version.txt"
APP_NAME = "小笼洛包"
# 读取失败时的兜底值：宁可显示 unknown，也不要退回某个写死的版本号
FALLBACK = "unknown"

_cache = None


def _parse(text):
    """取文件里第一行有效内容；支持行内注释与 v 前缀。"""
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return re.sub(r"^[vV]\s*", "", line).strip()
    return ""


def read_version(refresh=False):
    """读取 version.txt 里的版本号（带缓存；文件缺失或为空时返回 unknown）。"""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    value = FALLBACK
    try:
        value = _parse(VERSION_FILE.read_text(encoding="utf-8-sig")) or FALLBACK
    except OSError:
        value = FALLBACK
    _cache = value
    return value


def version_label():
    """用于打印 / 窗口标题：小笼洛包 1.8.0-preview"""
    return f"{APP_NAME} {read_version()}"


def version_info():
    """供 Web 接口返回：版本号 + 来源文件（便于排查读的是哪份文件）。"""
    return {"version": read_version(), "app_name": APP_NAME, "source": str(VERSION_FILE)}


def server_token():
    """HTTP Server 头用的服务标识（去掉不能出现在头里的字符）。"""
    safe = re.sub(r"[^0-9A-Za-z._+-]", "", read_version()) or FALLBACK
    return f"XiaolongluoWebUI/{safe}"
