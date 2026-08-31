# core/pipeline.py
# 对话处理流水线：把“传统问答（QA）”与“实时对话（Live）”两种模式共用的流程统一到一处，
# 供 Web 服务调用。返回结构化结果，音频由浏览器播放。
import os
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
from core.music import search_music, download_music
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


def _music_url(path):
    """把 runtime 下的音乐文件路径转成可访问 URL。"""
    if not path:
        return None
    rel = os.path.relpath(path, config.RUNTIME_DIR).replace("\\", "/")
    return "/runtime/" + rel


def _music_stop_reply():
    """生成“音乐已停止”的角色化提示文本（语音由 Web 层流式合成）。"""
    reply = call_ollama("音乐停止提示",
                        extra_context="音乐已停止，请用当前角色口吻说一句“音乐已停止”的话。",
                        record=False, use_context=False)
    if not reply:
        reply = "音乐已停止。"
    return reply


def music_intro_prompt(user_text, keyword):
    """生成“即将播放...”的角色化提示并合成语音。"""
    prompt = f"即将播放《{keyword}》，请用当前角色口吻说一句“即将播放...”的话，不要输出任何其他的无关内容。"
    with _lock:
        # record=False / use_context=False：播报不写上下文、不做记忆检索，避免污染
        reply = call_ollama(user_text, extra_context=prompt, record=False, use_context=False)
    if not reply:
        reply = f"即将播放《{keyword}》。"
    return reply, synthesize(reply)


def music_outro_prompt(song_name):
    """生成“播放完毕”的角色化提示并合成语音。"""
    prompt = f"歌曲《{song_name}》已播放完毕，请用当前角色口吻说一句“你已经播放...”的话。"
    with _lock:
        reply = call_ollama("播放结束提示", extra_context=prompt, record=False, use_context=False)
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

    # ========== 清空对话命令（真正清除上下文：L0 会话缓存 + 显示 + 镜像历史） ==========
    if user_text in ("清空对话", "清除对话", "清空上下文"):
        storage.clear_history()
        result["reply"] = "已清空当前对话与上下文，开始新话题吧。"
        result["speak"] = True
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

    # ========== 点歌意图检测（置于最前：先于联网与上下文检测） ==========
    # 歌曲关键词触发 → 跳过联网与上下文检测，进入歌曲播放检测流程；
    # LLM 复核失败（confirm_music_intent 为 False）→ 回到原流程继续。
    # 命中后自动播放第一首（与“更流畅的音乐播放”插件行为一致），避免回退手动选歌；
    # 仅当搜索无结果或下载失败时才回退手动选歌列表。
    if any(trigger in user_text for trigger in MUSIC_TRIGGERS) and confirm_music_intent(user_text):
        keyword = generate_music_search_keyword(user_text)
        videos = search_music(keyword)
        if videos:
            video = videos[0]
            title = video.get("title", keyword)
            try:
                path = download_music(video.get("url", ""), title)
            except Exception:
                path = None
            if path:
                intro, _ = music_intro_prompt(keyword, keyword)
                result["reply"] = intro
                result["speak"] = True
                result["music"] = {"url": _music_url(path), "title": title}
                return result
        # 无结果 / 下载失败 → 回退手动选歌
        result["action"] = "music_search"
        result["music_keyword"] = keyword
        result["music_videos"] = videos
        result["reply"] = f"为你找到与「{keyword}」相关的歌曲，请选择一首播放。"
        return result

    # ========== 正常对话流程：上下文检测优先，无命中再联网 ==========
    with _lock:
        reply = None
        search_keyword = None
        need_online = False
        force_search = any(w in user_text for w in FORCE_SEARCH_WORDS)

        # 1) 上下文检测（记忆引擎：L1/L2/L3 长期记忆 + L0 会话记忆，按当前角色加权）
        #    命中 → 直接用上下文回答，跳过联网（避免双源注入上下文混乱）
        if not force_search and _context_hit(user_text, role=config.character_name):
            reply = call_ollama(user_text)
        else:
            # 2) 无相关上下文（或显式要求联网）→ 联网检测
            need_online = force_search or judge_need_online(user_text)
            if need_online:
                search_keyword = generate_search_keyword(user_text)
                online_info = get_internet_info(search_keyword)
                config.last_search_keyword = search_keyword
                # 注入复核（与多人对话同套逻辑）：联网内容与当前对话无关时不注入
                if online_info and not _review_content(user_text, online_info):
                    online_info = ""
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


def _context_hit(user_text, role=None):
    """上下文检测：判断「是否已具备足够上下文，无需联网」。

    通用原则（不依赖关键词表）：
      1) 长期记忆命中（L1/L2/L3 检索到相关事件）→ 强上下文，跳过联网；
      2) 省略式追问（如“那周六呢”“然后呢”，一般语言模式）且 L0 最近回合有
         实质相关内容 → 延续对话，跳过联网；
      3) 其余一律交给 judge_need_online 仲裁——包括“昨天有什么新闻吗”这类
         索取实时外部信息的问题（仅与 L0 泛用词重合不再构成跳过联网的理由）。
    role：当前角色（判断层优先命中与当前角色相关的事件）。
    """
    try:
        from memory_engine import get_engine
        from memory_engine import logging as me_log
        import memory_engine.config as mecfg
        eng = get_engine()
        if not eng.is_ready():
            return False
        # 纠错机制：长时间未活动（直接关窗退出后）先清理残留上下文
        try:
            eng.maybe_clear_stale()
        except Exception:
            pass
        # 1) 长期记忆命中（省略句追问自动带上上一轮用户话术做语义扩展；
        #    命中内容先经注入复核，无关记忆不构成“跳过联网”的理由）
        try:
            extra = eng.followup_extra(user_text)
            r = eng.search(user_text, role=role, extra_query=extra)
            if r.id:
                if _review_content(user_text, r.full_summary):
                    me_log.debug(f"[上下文检测] 长期记忆命中（来源L1/L2/L3）: id={r.id} "
                                 f"置信度={r.confidence} → 跳过联网")
                    return True
                me_log.debug(f"[上下文检测] 记忆被注入复核阻止（404/未确认）id={r.id} → 继续联网判断")
        except Exception:
            pass
        # 2) 省略式追问 + L0 实质相关回合（IDF 加权重合分达标）→ 延续对话
        try:
            if eng.followup_extra(user_text):
                hits = eng.l0.query_scored(user_text, limit=1)
                if hits and hits[0][0] >= mecfg.L0_HIT_MIN_SCORE:
                    me_log.debug(f"[上下文检测] L0会话记忆命中（来源L0）: "
                                 f"{(hits[0][1].get('content') or '')[:30]}… → 跳过联网")
                    return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def _review_content(user_text, content):
    """注入复核（主干封装，单人/多人共用）：记忆/联网内容注入前用引擎复核相关性。

    内容与当前对话无关（判断模型输出 404 / 未明确 200）时返回 False；
    引擎不可用 / 复核关闭 / 判断异常时放行（返回 True，不阻塞主流程）。
    """
    try:
        from memory_engine import get_engine
        from memory_engine import logging as me_log
        eng = get_engine()
        if not eng.is_ready():
            return True
        ok = eng.review_injection(user_text, content)
        if not ok:
            me_log.debug(f"[上下文检测] 内容被注入复核阻止（404/未确认）: {str(content)[:30]}…")
        return ok
    except Exception:
        return True
