# memory_engine/roles.py
# 角色名「占位符化」：长期记忆只存规范角色名（主角色用占位符 {char}），
# 用户自由修改角色预设（改名）后，历史记忆仍然参与检索、展示为当前角色名。
#
#   - 写入侧：把当前角色名替换为 {char}（参与者、事实、摘要、原文）；
#   - 读取侧：检索输出时把 {char} 渲染回当前角色名；
#   - 角色加成：参与者含 {char}（或解析后等于当前角色）即视为「与当前角色相关」。
from __future__ import annotations

CHAR_TOKEN = "{char}"


def resolve_char_name(explicit=None):
    """解析当前角色名：显式传入优先；否则懒读取应用角色配置（改预设即生效）。"""
    if explicit:
        return explicit
    try:
        import core.config as _config
        return getattr(_config, "character_name", None) or None
    except Exception:
        return None


def to_stored_text(text, char_name=None):
    """写入侧：把当前角色名替换为占位符（长记忆不绑定具体名字）。"""
    char_name = resolve_char_name(char_name)
    if char_name and text:
        return text.replace(char_name, CHAR_TOKEN)
    return text


def render(text, char_name=None):
    """读取侧：把占位符渲染回当前角色名（未识别角色名时原样保留）。"""
    char_name = resolve_char_name(char_name)
    if char_name and text and CHAR_TOKEN in text:
        return text.replace(CHAR_TOKEN, char_name)
    return text


def render_list(items, char_name=None):
    out = []
    for it in items or []:
        r = render(it, char_name)
        if r not in out:
            out.append(r)
    return out


def render_facts(facts_per_role, char_name=None):
    """渲染 facts_per_role：角色键与各字段统一替换占位符。"""
    out = {}
    for role, fd in (facts_per_role or {}).items():
        d = fd.to_dict() if hasattr(fd, "to_dict") else dict(fd or {})
        rendered = {}
        for k, v in d.items():
            rendered[k] = render(str(v), char_name)
        out[render(role, char_name)] = rendered
    return out


def involves(participants, role_names, char_name=None):
    """参与者是否与给定角色（一个或多个）相关。

    - 参与者含主角色占位符（含「{char}（朋友）」等变体）→ 与主角色相关；
    - 参与者渲染后与任一角色名相等 → 相关；
    - 仅其它具体人名（如朋友的独立名字）→ 不相关。
    """
    if not participants or not role_names:
        return False
    names = {str(r).strip() for r in role_names if str(r).strip()}
    char_name = resolve_char_name(char_name)
    for p in participants:
        p = str(p or "")
        if CHAR_TOKEN in p:
            return True
        rp = render(p, char_name)
        if rp in names:
            return True
    return False
