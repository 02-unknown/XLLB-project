# core/pipeline.py
# 对话处理流水线：把“传统问答（QA）”与“实时对话（Live）”两种模式共用的流程统一到一处，
# 供 Web 服务调用。返回结构化结果，音频由浏览器播放。
import threading

import core.config as config
from core import storage
from core.llm import (
    call_ollama,
    generate_search_keyword,
    generate_music_search_keyword,
    confirm_music_intent,
)
from core.judge import judge_need_online
from core.search import get_internet_info
from core.tts import synthesize
from core.music import search_music
from core import plugin_manager

_lock = threading.Lock()

SKIP_KEYWORDS = ["字幕", "by", "待续", "未完", "continued", "to be", "简体中文"]
MUSIC_TRIGGERS = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱","放","听","唱"]

# 音乐控制命令（使用明确短语，避免与实时对话的“暂停/继续”混淆）
MUSIC_PAUSE_WORDS = ["暂停音乐", "暂停播放"]
MUSIC_RESUME_WORDS = ["继续音乐", "继续播放", "恢复音乐"]
MUSIC_STOP_WORDS = ["停止音乐", "停止播放", "关闭音乐"]

# 实时对话模式的控制命令（对应旧版 live_mode.py）
LIVE_EXIT_WORDS = ["退出", "结束", "再见"]
LIVE_PAUSE_WORDS = ["暂停", "停下", "等一下"]
LIVE_RESUME_WORDS = ["继续", "恢复", "开始"]

FORCE_SEARCH_WORDS = ["上网查", "搜索", "帮我查", "查一下", "网上找"]


def _music_stop_reply():
    """生成“音乐已停止”的角色化提示文本（语音由 Web 层流式合成）。"""
    reply = call_ollama("音乐停止提示",
                        extra_context="音乐已停止，请用当前角色口吻说一句“音乐已停止”的话。")
    if not reply:
        reply = "音乐已停止。"
    return reply


def music_intro_prompt(user_text, keyword):
    """生成“即将播放...”的角色化提示并合成语音。"""
    prompt = f"即将播放《{keyword}》，请用当前角色口吻说一句“即将播放...”的话，不要输出任何其他的无关内容。"
    with _lock:
        reply = call_ollama(user_text, extra_context=prompt)
    if not reply:
        reply = f"即将播放《{keyword}》。"
    return reply, synthesize(reply)


def music_outro_prompt(song_name):
    """生成“播放完毕”的角色化提示并合成语音。"""
    prompt = f"歌曲《{song_name}》已播放完毕，请用当前角色口吻说一句“你已经播放...”的话。"
    with _lock:
        reply = call_ollama("播放结束提示", extra_context=prompt)
    if not reply:
        reply = f"歌曲《{song_name}》播放完毕啦。"
    return reply, synthesize(reply)


def _detect_live_control(user_text):
    """检测实时对话模式的控制指令，返回 'exit' | 'pause' | 'resume' | None。"""
    if any(w in user_text for w in LIVE_EXIT_WORDS):
        return "exit"
    if any(w in user_text for w in LIVE_PAUSE_WORDS):
        return "pause"
    if any(w in user_text for w in LIVE_RESUME_WORDS):
        return "resume"
    return None


def _base_result(user_text):
    return {
        "ok": True,
        "action": "chat",
        "user_text": user_text,
        "reply": "",
        "speak": False,          # 是否需要语音播报（Web 层据此流式合成）
        "need_online": False,
        "search_keyword": None,
        "music_keyword": None,
        "music_videos": [],
        "music_control": None,
        "skip_reason": None,
    }


def process_message(user_text, mode="qa"):
    """处理一条用户消息，返回结构化结果（供 Web 端渲染 / 播放）。

    mode:
      - "qa"  ：传统问答（按键 / 文本触发），支持点歌
      - "live"：实时对话（连续聆听），额外支持“退出 / 暂停 / 继续”等控制
    """
    user_text = (user_text or "").strip()
    result = _base_result(user_text)

    if not user_text:
        result["ok"] = False
        result["skip_reason"] = "空输入"
        return result

    # ========== 插件：命令（以 / 开头）与消息前置处理 ==========
    plugin_result = None
    if user_text.startswith("/"):
        plugin_result = plugin_manager.manager.handle_command(user_text, mode)
        if plugin_result is None:
            plugin_result = plugin_manager.manager.normalize({
                "reply": "未知命令，输入 /help 查看可用命令。",
                "speak": False,
            })
    else:
        plugin_result = plugin_manager.manager.handle_message(user_text, mode)

    if plugin_result is not None:
        plugin_result["user_text"] = user_text
        return plugin_result

    # 过滤疑似非对话输入（如视频字幕）
    if len(user_text) < 2 or any(kw in user_text for kw in SKIP_KEYWORDS):
        result["action"] = "skip"
        result["skip_reason"] = "疑似非对话输入"
        return result

    # ========== 音乐控制命令（先于实时控制，避免“暂停音乐”被误判） ==========
    if any(w in user_text for w in MUSIC_PAUSE_WORDS):
        result["action"] = "music_control"
        result["music_control"] = "pause"
        result["reply"] = "音乐已暂停。"
        result["speak"] = True
        return result

    if any(w in user_text for w in MUSIC_RESUME_WORDS):
        result["action"] = "music_control"
        result["music_control"] = "resume"
        result["reply"] = "音乐已继续。"
        result["speak"] = True
        return result

    if any(w in user_text for w in MUSIC_STOP_WORDS):
        result["action"] = "music_control"
        result["music_control"] = "stop"
        result["reply"] = _music_stop_reply()
        result["speak"] = True
        return result

    # ========== 实时对话控制（仅 live 模式） ==========
    if mode == "live":
        control = _detect_live_control(user_text)
        if control == "exit":
            result["action"] = "exit"
            result["reply"] = "好的，已退出实时对话。"
            return result
        if control == "pause":
            result["action"] = "live_pause"
            result["reply"] = "对话已暂停，点继续即可恢复。"
            return result
        if control == "resume":
            result["action"] = "live_resume"
            result["reply"] = "对话已继续。"
            return result

    # ========== 点歌意图检测 ==========
    if any(trigger in user_text for trigger in MUSIC_TRIGGERS) and confirm_music_intent(user_text):
        keyword = generate_music_search_keyword(user_text)
        videos = search_music(keyword)
        result["action"] = "music_search"
        result["music_keyword"] = keyword
        result["music_videos"] = videos
        result["reply"] = f"为你找到与「{keyword}」相关的歌曲，请选择一首播放。"
        return result

    # ========== 正常对话流程 ==========
    with _lock:
        need_online = judge_need_online(user_text)
        force_search = any(w in user_text for w in FORCE_SEARCH_WORDS)
        if force_search:
            need_online = True

        reply = None
        search_keyword = None
        if need_online:
            search_keyword = generate_search_keyword(user_text)
            online_info = get_internet_info(search_keyword)
            config.last_search_keyword = search_keyword
            reply = call_ollama(user_text, extra_context=online_info)
            if not reply:
                reply = "抱歉，我努力查询了但没能找到相关信息。"
        else:
            reply = call_ollama(user_text)

        if not reply:
            reply = "哎呀，我有点卡壳了，换个问题试试？"

        storage.append_history(user_text, reply)
        result["reply"] = reply
        result["need_online"] = need_online
        result["search_keyword"] = search_keyword
        result["speak"] = True

    # ========== 插件：消息后处理（可原地修改结果） ==========
    plugin_manager.manager.post_process(user_text, mode, result)
    return result
