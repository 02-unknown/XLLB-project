# plugins/user_profile.py —— 内置插件：用户信息设置。
# 用户填写自己的信息后，这些信息会作为系统提示词的一部分注入。
# 为保持对话稳定，用户信息在“对话开始”（上下文为空）时快照一次，
# 对话进行中修改设置不会影响当前对话，只有新对话才会生效（即仅可在非对话期间使用）。
# 兼容两种上下文模式：
#   · 新模式（启用「上下文记忆库」）：会话上下文以记忆引擎 L0 为准；
#   · 老模式：会话上下文以 conversation_history 为准。
# 「对话是否已开始」统一按 引擎L0 / 历史镜像 任一非空判定。
NAME = "用户信息设置"
VERSION = "1.1.0"
DESCRIPTION = "设置用户信息（称呼 / 偏好等），在对话开始前生效"
AUTHOR = "02"
OFFICIAL = True

SETTINGS = {
    "name": "",        # 用户称呼
    "interests": "",   # 偏好 / 兴趣
    "notes": "",       # 其它补充信息
}

# 当前对话使用的用户信息快照（对话开始时刷新，对话中保持不变）
_snapshot = {}


def settings_schema():
    return [
        {"key": "name", "label": "称呼", "type": "text", "placeholder": "如：小明"},
        {"key": "interests", "label": "偏好/兴趣", "type": "text", "placeholder": "如：喜欢科幻、编程"},
        {"key": "notes", "label": "补充信息", "type": "text", "placeholder": "如：正在准备考研"},
    ]


def _session_active(ctx):
    """会话是否已开始（兼容新旧上下文模式）：新模式下看记忆引擎 L0，老模式看历史镜像。

    走记忆服务门禁：记忆插件停用 / 引擎挂起时不再读取记忆（回退历史镜像判断）。
    """
    try:
        from memory_engine import service as mem_service
        eng = mem_service.get_engine_for_read()
        if eng is not None and eng.l0 is not None and eng.l0.size() > 0:
            return True
    except Exception:
        pass
    return bool(ctx.config.conversation_history)


def _build_prompt_segment(settings):
    parts = []
    if settings.get("name"):
        parts.append(f"用户称呼为“{settings['name']}”")
    if settings.get("interests"):
        parts.append(f"用户的偏好/兴趣：{settings['interests']}")
    if settings.get("notes"):
        parts.append(f"关于用户的补充信息：{settings['notes']}")
    if not parts:
        return ""
    return "【关于用户的额外信息】" + "；".join(parts) + "。请在不打断对话的前提下自然地运用这些信息。"


def validate_settings(patch, ctx):
    """强制约束：用户信息只能在对话开始（模型启用）前修改。"""
    if _session_active(ctx):
        return False, "对话已开始，用户信息只能在对话开始前修改（请先清空对话）"
    return True, ""


def on_system_prompt(prompt, ctx):
    global _snapshot
    # 对话尚未开始（上下文为空）时刷新快照；对话进行中沿用快照，避免中途变更导致不稳定
    if not _session_active(ctx):
        _snapshot = dict(ctx.manager.get_settings(NAME))
    segment = _build_prompt_segment(_snapshot)
    if segment:
        return prompt + segment
    return prompt
