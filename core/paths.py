# core/paths.py —— 用户输入路径的规范化与解析（供需要读取本机文件的功能共用）。
#
# 使用场景：界面里让用户填「本机文件路径」时，用户常见的粘贴形式并不规范，例如
#   · Windows「复制文件地址」得到的是带引号的路径：        "E:\图片\a.jpg"
#   · 从聊天/记事本复制时带的是中文引号：                   “E:\图片\a.jpg”
#   · 浏览器复制的本地地址是 file URI：                     file:///E:/图片/a.jpg
#   · 前后带空格或换行（粘贴时常见）
# 这些形式 os.path.isfile() 都会判为不存在，表现为「填了路径却不生效」，
# 因此统一在这里清理，再交给各功能解析。
import os
import re
import urllib.parse

# 需要剥离的成对引号：ASCII 引号 + 中文/日文引号
_QUOTE_PAIRS = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』")]
_WS = " \t\r\n\u3000"   # 含全角空格


def normalize_user_path(raw):
    """清理用户粘贴的路径：去空白、去外层引号、file URI 还原为本地路径。

    只做「文本清理」，不判断文件是否存在、也不转成绝对路径（解析见 resolve_user_path）。
    """
    p = str(raw or "").strip(_WS)
    # file:///E:/a.jpg 或 file://E:/a.jpg → E:/a.jpg
    if p[:7].lower() == "file://":
        p = urllib.parse.unquote(p[7:])
        # file:///E:/a.jpg 去掉多余的前导斜杠（保留 \\server\share 形式的 UNC）
        p = re.sub(r"^/(?=[A-Za-z]:)", "", p)
    # 反复剥离外层成对引号（可能套了两层，如 "“E:\a.jpg”"）
    for _ in range(4):
        before = p
        p = p.strip(_WS)
        for left, right in _QUOTE_PAIRS:
            if len(p) >= 2 and p.startswith(left) and p.endswith(right):
                p = p[1:-1]
                break
        if p == before:
            break
    return p.strip(_WS)


def resolve_user_path(raw, base_dir=None):
    """在 normalize_user_path 的基础上解析成绝对路径。

    支持环境变量（%USERPROFILE%）与 ~；相对路径以 base_dir（默认项目根目录）为基准。
    """
    p = normalize_user_path(raw)
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(p))
    if not os.path.isabs(p):
        if base_dir is None:
            import core.config as config
            base_dir = config.PROJECT_ROOT
        p = os.path.join(base_dir, p)
    return os.path.normpath(p)


def is_existing_file(raw, base_dir=None):
    """规范化并解析后判断是否是一个存在的文件。"""
    p = resolve_user_path(raw, base_dir)
    return bool(p) and os.path.isfile(p)
