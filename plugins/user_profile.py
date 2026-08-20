# plugins/user_profile.py —— 官方插件：用户信息设置。
# 用户填写自己的信息后，这些信息会作为系统提示词的一部分注入。
# 为保持对话稳定，用户信息在“对话开始”（上下文为空）时快照一次，
# 对话进行中修改设置不会影响当前对话，只有新对话才会生效（即仅可在非对话期间使用）。
NAME = "用户信息设置"
VERSION = "1.0.0"
DESCRIPTION = "设置用户信息（称呼 / 偏好等），在对话开始前生效"
AUTHOR = "官方"
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
    if ctx.config.conversation_history:
        return False, "对话已开始，用户信息只能在对话开始前修改（请先清空对话）"
    return True, ""


def on_system_prompt(prompt, ctx):
    global _snapshot
    # 对话尚未开始（上下文为空）时刷新快照；对话进行中沿用快照，避免中途变更导致不稳定
    if not ctx.config.conversation_history:
        _snapshot = dict(ctx.manager.get_settings(NAME))
    segment = _build_prompt_segment(_snapshot)
    if segment:
        return prompt + segment
    return prompt
