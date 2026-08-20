# core/character.py
# 角色预设管理：保存 / 加载 / 生成系统提示词 / 切换角色。
import json
import os
import re

import core.config as config
from core.search import search_tavily


def save_preset(name, data):
    """保存角色预设到 presets 文件夹。"""
    os.makedirs(config.PRESETS_DIR, exist_ok=True)
    filepath = os.path.join(config.PRESETS_DIR, f"{name}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_presets():
    """加载所有角色预设，返回字典 {名称: 数据}。"""
    os.makedirs(config.PRESETS_DIR, exist_ok=True)
    presets = {}
    for f in os.listdir(config.PRESETS_DIR):
        if f.endswith('.json'):
            name = f[:-5]
            filepath = os.path.join(config.PRESETS_DIR, f)
            try:
                with open(filepath, 'r', encoding='utf-8') as fp:
                    presets[name] = json.load(fp)
            except Exception:
                pass
    return presets


def build_system_prompt(character, influence):
    base = (
        f"你必须使用纯中文回答。你现在是{character}。"
        f"请严格模仿{character}的说话风格、语气和用词。"
    )
    presets = load_presets()
    if character in presets:
        desc = presets[character].get("description", "")
        if desc:
            base += f"以下是对你的角色更具体的描述：{desc}"

    base += (
        f"当前角色影响力强度为{influence}（1-10）。数值越高，你越应突出{character}的典型特征，"
        f"包括但不限于：语气强度、口头禅、表情符号使用频率、拟声词、角色标志性动作描述等。"
        f"请根据当前影响力自动调整说话方式，但不要输出任何调整过程的思考。"
    )

    base += (
        f"基于事实回答，不知道则回答不知道，严格禁止编造、歪曲事实。"
        f"绝对不要输出任何思考过程、内部标记或英文单词。"
        f"【强制】将所有输出中的英文翻译成简体中文。"
        f"你的输出将直接用于语音合成，所以只输出最终的简体中文回答。"
    )
    # 让插件（如“用户信息”）在系统提示词上追加内容（惰性导入，避免循环依赖）
    from core import plugin_manager
    return plugin_manager.manager.on_system_prompt(base)


def list_presets():
    """返回角色预设名称列表，供 Web 界面展示。"""
    return sorted(load_presets().keys())


def _build_custom_description(raw_input, custom_req, use_search):
    """根据用户输入构造角色描述（对应旧版交互式 configure_personality 的自定义分支）。"""
    match = re.match(r'^([^（(]+)[（(]([^）)]*)[）)]$', raw_input)
    if match:
        search_name = match.group(1).strip()
        save_name = raw_input
    else:
        search_name = raw_input
        save_name = raw_input

    extra_info = ""
    if use_search:
        search_parts = [search_name]
        if custom_req:
            search_parts.append(custom_req)
        query = " ".join(search_parts) + " 角色扮演 说话风格 性格特点"
        data = search_tavily(query)
        if data:
            if isinstance(data, str):
                extra_info = data
            else:
                snippets = [r.get("content", "") for r in data.get("results", [])[:3] if r.get("content")]
                if snippets:
                    extra_info = "；".join(snippets[:2])

    parts = []
    if custom_req:
        parts.append(f"自定义要求：{custom_req}")
    if extra_info:
        parts.append(f"搜索结果：{extra_info}")
    return save_name, "；".join(parts) if parts else ""


def apply_character(mode, preset_name=None, raw_input=None, custom_req="", use_search=False, save=False):
    """切换角色。mode: 'default' | 'preset' | 'custom'。

    返回 (角色名, 描述)。切换后清空历史，避免旧角色残留。
    """
    description = ""

    if mode == "preset":
        presets = load_presets()
        if not preset_name or preset_name not in presets:
            raise ValueError("指定的角色预设不存在")
        config.character_name = preset_name
        description = presets[preset_name].get("description", "")

    elif mode == "custom":
        if not raw_input:
            raise ValueError("角色名称不能为空")
        save_name, description = _build_custom_description(raw_input, custom_req, use_search)
        config.character_name = save_name
        if save:
            save_preset(save_name, {"description": description})

    else:  # default
        config.character_name = config.DEFAULT_CHARACTER

    config.conversation_history.clear()
    config.history_records.clear()
    return config.character_name, description
