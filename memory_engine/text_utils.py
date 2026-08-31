# memory_engine/text_utils.py
# 文本规范化与口语化省略句识别（与时间解析、主题分类解耦的纯文本工具）。
from __future__ import annotations

import re

# 星期说法归一：星期X / 礼拜X → 周X（与入库侧 searchable_text 的规范化保持一致，
# 保证「星期三」这类口语查询能用 2-gram「周三」命中存储的「周三晚上…」）。
_WEEKDAY_RE = re.compile(r"(?:星期|礼拜)([一二三四五六日天])")


def normalize_query_text(text: str) -> str:
    """查询文本规范化：把「星期三/礼拜三」统一成「周三」形式，便于关键词召回。"""
    t = (text or "").strip()
    if not t:
        return t
    t = _WEEKDAY_RE.sub(r"周\1", t)
    return t


def looks_like_followup(text: str) -> bool:
    """判断是否「省略式追问」（如「星期三呢」「那周六呢」「明天呢」）。

    这类输入依赖上一轮对话才能补全语义：检索时应带上最近一条用户回合做语义扩展，
    但时间解析仍以本句为准（「星期三呢」→ 本周三，而不是上轮的时间）。
    """
    t = (text or "").strip()
    if not t or len(t) > 12:
        return False
    if t.endswith(("呢", "呢？", "呢？？")):
        return True
    if len(t) <= 8 and t.startswith(("那", "然后", "那然后", "那再")):
        return True
    return False


# 常见功能字 / 语气字（问候、寒暄、语气应答等低信息量内容的基本字符）
# 含常用时间/话语字（今/明/昨/早…）：全部由这些字组成的短句才判为低信息量，
# 只要出现一个实义字（如「想喝奶茶」的奶/茶）就不会误判。
_FUNC_CHARS = set(
    "的了是在就这也那还不没有很真太和我你他她它们"
    "好哈嘿哎哦啊吧呀呢吗嘛啦哇哟嗯诶噢唉呗哼喂嗨咦咩"
    "谢早安拜见晚上今明天昨前周星"
)


def is_filler_text(text: str) -> bool:
    """低信息量判定：整句较短且全部由功能/语气字组成（如「你好」「哈哈」「好的」「谢谢」「嗯嗯」）。

    用于归档价值判断：问候、寒暄、语气应答不构成值得长期记住的记忆。
    通用字符类规则（非关键词表）；含实义字的句子（如「想喝奶茶」）不会被误判。
    """
    t = (text or "").strip()
    if not t:
        return True
    return len(t) <= 8 and all(c in _FUNC_CHARS for c in t)
