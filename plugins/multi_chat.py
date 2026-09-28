# plugins/multi_chat.py —— 内置插件：多人对话。
# 启用后，普通对话由本插件接管：
#   - 先判断用户是否指定了角色（消息里出现已配置的人物名），若有则只有该角色回应；
#   - 否则随机抽取回应人数（1~3）与回应人；
#   - 生成方式可自选：
#       · 剧本模式（默认）：先由大模型生成“剧本”（每个角色的回应方向概要），
#         再按剧本顺序逐条独立调用大模型生成具体回复，最后用各自声线合成语音；
#         全程流式：每句生成完立即推送，上一句语音播放的同时生成下一句，压缩等待时间、减少幻觉。
#       · 单次模式：一次调用直接生成整段多人对话。
#       · 自然对话（接话式·推荐）：收到输入后先做 对话对象检测（可选 LLM 增强判断）→
#         歌曲检测（透传主干，LLM 复核）→ 上下文查询（记忆引擎）→ 联网搜索（判断模型裁决），
#         仅首位发言人注入检索/联网内容；首位按「指定角色 → 上下文相关角色 → 随机」确定，
#         其余按随机顺序逐个“接话”（可重复但同一角色不超过两次），逐句流式生成与播放，
#         每句独立显示、各自声线合成，结束按既有规则归档上下文。
# 兼容性说明：
#   - 音乐点歌 / 音乐控制 / 快捷设置 / 记事本 / 翻译 / 实时对话控制等指令会透传给
#     原流程与其它插件，避免功能冲突；
#   - 未配置足够人物（<2）时自动放行主流程，不影响正常对话；
#   - 各环节只调用主干（core / memory_engine），不直接调用其它插件；
#     记忆引擎不可用时回退最近对话，联网/歌曲检测失败时回退正常流程；
#   - 语音合成前后会保存并恢复全局声线配置（含服务器模型权重），不影响主对话音色。
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import core.config as config
from core import character as character_core
from core import llm as llm_core
from core import tts as tts_core
from core.runtime import runtime_url

NAME = "多人对话"
VERSION = "1.3.0"
DESCRIPTION = "角色管理页勾选参与角色；剧本/单次/自然对话（接话式）三种生成方式，各自声线合成"
AUTHOR = "02"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    # 新：角色管理页勾选结果（JSON 数组字符串，如 ["洛天依","乐正绫"]；空=未配置，回退旧槽位）
    "selected_characters": "",
    "character_voices": "{}",   # 每个角色的声线映射 JSON（{"洛天依": "声线名"}）
    # 旧（兼容保留）：人物 1~3 槽位
    "slot1_character": "",
    "slot1_voice": "",
    "slot2_character": "",
    "slot2_voice": "",
    "slot3_character": "",
    "slot3_voice": "",
    "max_lines": 5,             # 一场对话的语句总数上限（≤5）
    "auto_voice": True,         # 是否用各自声线合成语音（关闭则仅输出文本）
    "generation_mode": "script",  # script: 剧本模式（多次调用·流式）; single: 单次模式; natural: 自然对话（接话式·流式）
    "target_llm_judge": False,  # 增强判断（可选）：用 LLM 判断用户是否单独对某角色说话；关闭则只用传统名字匹配
}

# ==================== 透传保护（避免与其它功能冲突） ====================
_SKIP_WORDS = ["字幕", "by", "待续", "未完", "continued", "to be", "简体中文"]
_MUSIC_STRONG = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱"]
_MUSIC_WEAK = ["放", "听", "唱"]   # 弱音乐触发（单字，需 LLM 复核确认是否点歌）
_MUSIC_CONTROL = ["暂停音乐", "暂停播放", "继续音乐", "继续播放", "恢复音乐",
                  "停止音乐", "停止播放", "关闭音乐"]
_LIVE_CONTROL = ["退出", "结束", "再见", "暂停", "停下", "等一下", "继续", "恢复", "开始"]
_QUICK_SETTINGS = ["关闭联网", "关联网", "开启联网", "开联网", "清空对话", "清除对话", "清空上下文"]
_MEMO_VIEW = ["我的笔记", "我记了什么", "查看笔记", "备忘录"]
_MEMO_SAVE = re.compile(r'^(?:记住|记下|帮我记|帮我记住)\s*(.+)')

# 全局串行化：同一时间只允许一场多人对话（含流式后台任务），
# 避免并发消息在切换声线 / 切换模型权重时相互干扰。
_run_lock = threading.Lock()
_synth_lock = threading.Lock()   # 仅保护单句合成与声线切换
_tts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="multi-tts")

# ==================== 生命周期：声线防御性恢复 ====================
# 注意：不再强制关闭联网搜索 —— 自然对话模式按判断模型裁决决定是否联网搜索；
# 剧本/单次模式本身不发起联网；透传类消息（音乐/翻译/设置等）由主干按用户开关处理。
_voice_snapshot = None   # 最近一次多人对话合成前的全局声线快照


def on_load(settings, ctx):
    # 联网开关不再被本插件强制修改：自然对话的联网搜索按判断模型裁决执行
    pass


def on_unload(ctx):
    # 停用插件时取消进行中的多人对话流，避免聊天继续输出 / 播放
    _cancel_streams()
    # 防御性恢复全局声线（不阻塞：若流式任务仍在收尾，由其自身恢复）
    snap = _voice_snapshot
    if snap:
        try:
            if _run_lock.acquire(blocking=False):
                try:
                    _restore_snapshot(snap, ctx)
                finally:
                    _run_lock.release()
        except Exception:
            pass


# ==================== 设置表单 ====================
_schema_cache = {"ts": 0.0, "options": None}


def _schema_options():
    """人物与声线选项（带 5 秒缓存，避免插件管理页频繁扫描磁盘）。"""
    if _schema_cache["options"] is not None and time.time() - _schema_cache["ts"] < 5:
        return _schema_cache["options"]
    chars = [{"value": "", "label": "（未设置）"}] + [
        {"value": n, "label": n} for n in character_core.list_presets()
    ]
    voices = [{"value": "", "label": "默认（当前声线）"}] + [
        {"value": v["name"], "label": v["name"] + ("（" + (v.get("description") or "") + "）" if v.get("description") else "")}
        for v in tts_core.list_voice_presets()
    ]
    opts = (chars, voices)
    _schema_cache.update(ts=time.time(), options=opts)
    return opts


def settings_schema():
    _, voices = _schema_options()
    voice_names = "、".join(v["value"] for v in voices if v.get("value")) or "（未配置声线）"
    return [
        {"type": "section", "key": "characters",
         "label": "对话角色（在「设置 → 角色与语音」中勾选）",
         "desc": ("参与多人对话的角色在「设置 → 角色与语音」标签里勾选管理：那里会显示全部角色预设，"
                  "勾选即参与对话，并可为每个角色分别指定声线（推荐使用接话式·自然对话）。"
                  "至少勾选 2 个角色、且本插件处于启用状态时才会接管对话（否则为单人输出）；"
                  "未勾选时沿用旧版「人物1~3」槽位配置。"
                  f"可选声线：{voice_names}"),
         "actions": [
             {"name": "manage_characters", "label": "打开「角色与语音」设置",
              "desc": "跳到设置页的角色与语音标签，勾选参与对话的角色并指定声线"},
         ]},
        {"key": "generation_mode", "label": "生成方式", "type": "select",
         "options": [
             {"value": "script", "label": "剧本模式（多次调用·流式）"},
             {"value": "single", "label": "单次模式（一次生成）"},
             {"value": "natural", "label": "自然对话（接话式·流式·推荐）"},
         ]},
        {"key": "target_llm_judge", "label": "增强判断：用 LLM 识别用户是否单独对某角色说话（关闭则只用传统名字匹配）",
         "type": "checkbox"},
        {"key": "max_lines", "label": "对话语句上限", "type": "number", "placeholder": "2-5"},
        {"key": "auto_voice", "label": "用各自声线输出语音", "type": "checkbox"},
    ]


def on_action(action, ctx):
    if action == "manage_characters":
        # 打开设置页的「角色与语音」标签（全部角色预设 + 勾选参与角色 + 各角色声线）
        return {"page": "/settings.html#characters", "speak": False}
    return None


def available(ctx):
    """可用性声明（插件对外的握手信息）：告诉调用方现在能不能用多人对话。

    设置页的「角色与语音」据此决定是否激活多人对话区：
      · available=True  → 多人对话已激活（会接管对话，多角色输出）；
      · available=False → 当前为单人输出，并给出原因（插件未启用 / 勾选不足 2 个角色）。
    同时回传已勾选角色、最少角色数、生成方式，便于界面如实展示。
    """
    st = ctx.manager.get_settings(NAME)
    try:
        slots = _configured_slots(ctx)
    except Exception:
        slots = []
    selected = [name for name, _voice in slots]
    min_roles = 2
    base = {
        "enabled": True,
        "selected": selected,
        "min_roles": min_roles,
        "generation_mode": st.get("generation_mode", "natural"),
        "mode_label": {"script": "剧本模式", "single": "单次模式", "natural": "自然对话（接话式）"}
                      .get(st.get("generation_mode", "natural"), "自然对话（接话式）"),
    }
    if len(selected) < min_roles:
        base.update({
            "available": False,
            "reason": f"尚未勾选足够的参与角色（已选 {len(selected)} 个，至少需要 {min_roles} 个）",
            "note": "当前为单人输出",
        })
        return base
    base.update({
        "available": True,
        "reason": "",
        "note": f"多人对话已激活：{'、'.join(selected)}（{base['mode_label']}）",
    })
    return base


def _configured_slots(ctx):
    """返回参与对话的角色槽位 [(人物名, 声线名或"")]。

    优先使用角色管理页勾选的结果（selected_characters = JSON 数组，character_voices = 声线映射）；
    该设置为空字符串时（从未在页面保存过）回退到旧版「人物1~3」槽位，保证旧配置继续可用。
    """
    st = ctx.manager.get_settings(NAME)
    raw = (st.get("selected_characters") or "").strip()
    if raw:
        try:
            selected = json.loads(raw)
            if not isinstance(selected, list):
                selected = []
        except Exception:
            selected = [c.strip() for c in raw.split(",") if c.strip()]
        try:
            vmap = json.loads(st.get("character_voices") or "{}")
            if not isinstance(vmap, dict):
                vmap = {}
        except Exception:
            vmap = {}
        seen, slots = set(), []
        for c in selected:
            c = str(c).strip()
            if c and c not in seen:
                seen.add(c)
                slots.append((c, str(vmap.get(c, "") or "")))
        return slots
    slots = []
    for i in range(1, 4):
        c = (st.get(f"slot{i}_character") or "").strip()
        v = (st.get(f"slot{i}_voice") or "").strip()
        if c:
            slots.append((c, v))
    return slots


# ==================== 消息入口 ====================
def on_message(user_text, mode, ctx):
    text = (user_text or "").strip()
    # ---- 透传：以下情况不接管，保证正常功能不受影响 ----
    if not text or len(text) < 2 or any(k in text for k in _SKIP_WORDS):
        return None
    if any(w in text for w in _MUSIC_STRONG) or any(w in text for w in _MUSIC_CONTROL):
        _dbg(ctx, "歌曲检测: 强触发词/控制词 → 透传主干（音乐流程）")
        return None
    # 弱音乐触发（含“放/听/唱”单字，如“我想听周杰伦的歌”）：先做 LLM 点歌复核——
    # 确认为点歌则放行给音乐流程（更流畅的音乐播放 / 主流程音乐检测）；
    # 复核失败则回到多人对话（“歌曲llm复核失败回到原来流程”），复核异常时安全放行。
    if any(w in text for w in _MUSIC_WEAK):
        try:
            if llm_core.confirm_music_intent(text):
                _dbg(ctx, "歌曲检测: 弱触发 + LLM复核为点歌 → 透传主干（音乐流程）")
                return None
            _dbg(ctx, "歌曲检测: 弱触发 + LLM复核非点歌 → 继续多人对话")
        except Exception:
            _dbg(ctx, "歌曲检测: 弱触发复核异常 → 安全透传主干")
            return None
    if mode == "live" and any(w in text for w in _LIVE_CONTROL):
        return None
    if text in _QUICK_SETTINGS or text in _MEMO_VIEW or _MEMO_SAVE.match(text):
        return None
    if (text.startswith("翻译") and len(text) > 2) or (text.startswith("把") and "翻译成" in text):
        return None
    if text in ("退出", "结束", "再见"):
        return None

    slots = _configured_slots(ctx)
    if len(slots) < 2:
        # 未配置足够人物：放行主流程，避免启用后普通对话失效
        return None

    # 同一时间只允许一场多人对话；忙时给出提示而不是排队挂起
    if not _run_lock.acquire(blocking=False):
        return {"reply": "多人对话正在生成中，请稍等片刻。", "speak": False}

    try:
        settings = ctx.manager.get_settings(NAME)
        gmode = settings.get("generation_mode", "script")
        if gmode == "single":
            try:
                return _run_multi(text, slots, ctx)
            finally:
                _run_lock.release()
        if gmode == "natural":
            # 自然对话（接话式）：对象检测/歌曲透传/上下文/联网 → 流式逐句接话（worker 结束释放锁）
            try:
                sid = _start_natural(text, slots, ctx)
                return {"reply": "", "speak": False, "multi_stream_id": sid}
            except Exception as e:
                ctx.log("自然对话启动失败:", e)
                try:
                    _run_lock.release()
                except Exception:
                    pass
                return {"reply": "自然对话启动失败，请重试。", "speak": False}
        # 剧本模式：后台流式生成（worker 结束时释放锁）
        sid = _start_stream(text, slots, ctx)
        return {"reply": "", "speak": False, "multi_stream_id": sid}
    except Exception as e:
        try:
            _run_lock.release()
        except Exception:
            pass
        ctx.log("多人对话启动失败:", e)
        return {"reply": "多人对话启动失败，请重试。", "speak": False}


# ==================== 参与人选择（问题2：指定角色优先，否则随机人数与回应人） ====================
def _specified_characters(user_text, slots):
    """检测用户是否指定了某个已配置人物（消息中出现其名字）。返回 [(名, 声线)]。"""
    voice_of = dict(slots)
    found = []
    remaining = user_text
    for name in sorted((n for n, _ in slots), key=len, reverse=True):
        if name and name in remaining:
            found.append(name)
            remaining = remaining.replace(name, "", 1)   # 移除已命中名字，避免子串误配
    if not found:
        return []
    found.sort(key=lambda n: user_text.index(n))   # 按出现顺序
    return [(n, voice_of.get(n, "")) for n in found]


def _select_participants(user_text, slots, max_lines):
    """返回 (participants, total_lines)。

    - 用户指定了角色 → 只有该（些）角色回应，每人一句；
    - 否则随机抽取回应人数（1~3）与回应人，语句总数在人数与 max_lines 之间随机。
    """
    specified = _specified_characters(user_text, slots)
    if specified:
        return specified, len(specified)
    pool = list(slots)
    random.shuffle(pool)
    n = random.randint(1, min(3, len(pool)))
    participants = pool[:n]
    total = random.randint(n, max_lines) if max_lines >= n else n
    return participants, total


# ==================== 单次模式（一次生成整段） ====================
def _run_multi(user_text, slots, ctx):
    settings = ctx.manager.get_settings(NAME)
    try:
        max_lines = max(2, min(5, int(settings.get("max_lines", 5) or 5)))
    except Exception:
        max_lines = 5
    auto_voice = bool(settings.get("auto_voice", True))

    participants, total_lines = _select_participants(user_text, slots, max_lines)

    raw = _generate_dialogue(user_text, participants, total_lines, ctx)
    lines = _parse_dialogue(raw, [p[0] for p in participants])
    if len(lines) < 1:
        return {"reply": "多人对话生成失败，请换个说法试试。", "speak": False}

    _set_last(user_text, lines, [p[0] for p in participants])

    reply = "\n".join(f"{name}：{text}" for name, text in lines)
    _record_turn(ctx, user_text, reply, [p[0] for p in participants])

    multi_audio = []
    if auto_voice:
        path_lists = _synth_by_voice(lines, participants, ctx)
        for (name, text), paths in zip(lines, path_lists):
            multi_audio.append({"speaker": name, "text": text,
                                "audio": [_to_url(p) for p in paths if p]})
    else:
        for name, text in lines:
            multi_audio.append({"speaker": name, "text": text, "audio": []})

    return {"reply": reply, "speak": False, "multi_audio": multi_audio}


# ==================== 大模型生成 ====================
def _invoke(prompt, system, temperature, num_predict, purpose, user_text, ctx):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    try:
        return llm_core._invoke_chat(
            messages, config.LLM_CHAT_MODEL, temperature=temperature,
            num_predict=num_predict, stop=None, purpose=purpose, user_text=user_text,
        )
    except Exception as e:
        ctx.log(f"多人对话调用大模型失败（{purpose}）:", e)
        return ""


def _char_desc(name):
    presets = character_core.load_presets()
    return (presets.get(name) or {}).get("description", "")


def _recent_history(ctx, user_text=""):
    """最近对话上下文：统一由记忆引擎 L0 提供（上下文感知过滤，话题切换不残留）；
    引擎不可用时回退 conversation_history 镜像。"""
    hist = None
    try:
        from memory_engine import service as mem_service
        eng = mem_service.get_engine_for_read()   # 门禁：记忆插件停用 → 回退镜像历史
        if eng is not None:
            recent = eng.relevant_recent(user_text, 6) if (user_text or "").strip() else eng.l0.get_recent(6)
            hist = [{"role": t.get("role"), "content": t.get("content", "")} for t in recent]
    except Exception:
        hist = None
    if hist is None:
        hist = ctx.config.conversation_history[-6:]
    return "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{(m.get('content') or '')[:100]}"
        for m in hist if m.get("role") != "system"
    )


def _record_turn(ctx, user_text, reply, participants):
    """把一场多人对话回合记录到记忆引擎（统一上下文管理），并维护 conversation_history 镜像。"""
    try:
        from memory_engine import service as mem_service
        # 回合记录只写 L0 会话上下文：readonly 模式也允许（是否归档长期由引擎按模式判定）
        eng = mem_service.get_engine_for_turn()
        if eng is not None:
            eng.record_turn(user_text, reply, meta={
                "participants": list(participants or []),
                "main_topic": "多人对话",
                "role": config.character_name,
                "archive": True,
            })
            recent = [{"role": t["role"], "content": t["content"]} for t in eng.l0.get_recent()]
            ctx.config.conversation_history = [{"role": "system", "content": "多人对话会话"}] + recent
    except Exception:
        pass


def _memory_block(ctx, user_text, participants):
    """按参与角色检索相关长期记忆（判断层优先命中与这些角色相关的事件），返回提示词文本块。"""
    try:
        from memory_engine import get_engine
        eng = get_engine()
        if not eng.is_ready():
            return ""
        r = eng.search(user_text, role=list(participants or []))
        if not r.id:
            return ""
        parts = ["【相关历史记忆】" + (r.full_summary or "")]
        if r.participants:
            parts.append("参与者：" + "、".join(r.participants))
        return "\n".join(parts)
    except Exception:
        return ""


def _generate_dialogue(user_text, participants, total_lines, ctx):
    """单次模式：一次调用生成整段对话（角色直接对用户说话）。"""
    descs = []
    for name, _voice in participants:
        d = _char_desc(name)
        descs.append(f"{name}：{d}" if d else f"{name}：无特别描述")

    name_list = "、".join(f"「{n}」" for n, _ in participants)
    hist_text = _recent_history(ctx, user_text)
    mem_block = _memory_block(ctx, user_text, [p[0] for p in participants])
    prompt = (
        "你是一名多角色对话编剧。用户刚刚说了一句话，请让以下角色围绕这句话一起回应、陪伴用户，"
        "展开一段自然、生动、简短的对话。\n"
        "角色设定（只有这些角色可以发言）：\n" + "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descs)) + "\n"
        + (f"\n最近的对话上下文：\n{hist_text}\n" if hist_text else "")
        + (f"\n{mem_block}\n" if mem_block else "")
        + f"\n用户输入：{user_text}\n"
        "要求：\n"
        f"1. 共生成恰好 {total_lines} 句话，每句话严格按「角色名：说话内容」的格式独占一行；\n"
        f"2. 角色名必须与上面的名字完全一致，逐字使用，只允许使用 {name_list}；\n"
        "3. 禁止改写、缩写、替换或新增任何角色名（如把「洛天依」写成「天依」或换成其他人物）；\n"
        "4. 这是角色对用户的回应，不是角色之间的闲聊：每一句话都要直接对用户说，"
        "可以回应、问候、提问、安慰、调侃用户，内容必须紧扣用户的输入；\n"
        "5. 角色之间可以有少量互动（如顺着对方的话补充一句），但整段对话的核心对象始终是用户，"
        "不要变成角色们自说自话、把用户晾在一边；\n"
        "6. 每位参与角色至少说一句，先后顺序按自然对话推进；\n"
        "7. 每句话控制在 40 字以内，口语化，符合该角色的性格与说话风格；\n"
        "8. 只输出对话正文，不要输出任何解释、旁白、序号或引号；\n"
        "9. 使用简体中文。\n"
        "对话："
    )
    return _invoke(prompt, "你是一位中文多角色对话编剧。", 0.85, 1000,
                   "multi_dialogue", user_text, ctx)


def _generate_script(user_text, participants, total_lines, ctx):
    """剧本模式第 1 步：为每个回应角色生成一句「回应方向概要」。返回 [(名, 方向)]。"""
    descs = []
    for name, _voice in participants:
        d = _char_desc(name)
        descs.append(f"{name}：{d}" if d else f"{name}：无特别描述")

    name_list = "、".join(f"「{n}」" for n, _ in participants)
    hist_text = _recent_history(ctx, user_text)
    mem_block = _memory_block(ctx, user_text, [p[0] for p in participants])
    prompt = (
        "你是一名多角色对话编剧。用户说了一句话，请先为以下角色制定一份简短的回应剧本。\n"
        "角色（将按序依次回应用户）：\n" + "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descs)) + "\n"
        + (f"\n最近的对话上下文：\n{hist_text}\n" if hist_text else "")
        + (f"\n{mem_block}\n" if mem_block else "")
        + f"\n用户输入：{user_text}\n"
        "要求：\n"
        f"1. 为每个角色规划一句回应的内容方向（要点 / 意图，20 字以内）；\n"
        f"2. 每行严格按「角色名：方向」格式，角色名必须逐字使用 {name_list}；\n"
        "3. 每个角色恰好输出 1 行，同一角色只能出现一次；只有一个角色时也只输出 1 行；\n"
        "4. 方向要紧扣用户输入、直接对用户说话（回应、安慰、调侃、建议等），角色之间可以有承接；\n"
        "5. 只输出剧本，不要输出对话正文，不要任何解释。\n"
        "剧本："
    )
    raw = _invoke(prompt, "你是一位中文多角色对话编剧。", 0.7, 400,
                  "multi_script", user_text, ctx)
    return _parse_dialogue(raw, [p[0] for p in participants])


def _generate_line(user_text, name, direction, context_lines, ctx):
    """剧本模式第 2 步：按剧本顺序，为单个角色独立生成一句具体回复。"""
    desc = _char_desc(name)
    prev_text = "；".join(f"{n}说：{t}" for n, t in context_lines[-4:]) or "无"
    prompt = (
        f"你是「{name}」。" + (f"角色描述：{desc}。" if desc else "") + "\n"
        f"用户说：{user_text}\n"
        f"对话安排：{direction}\n"
        f"前面已经说的话：{prev_text}\n"
        "现在轮到你回应。请直接对用户说一句话（40 字以内），口语化、符合角色性格，"
        "紧扣用户输入与对话安排，可以回应或接上一句。"
        "只输出这句话的内容本身，不要角色名前缀、不要解释、不要引号、"
        "不要用第三人称称呼自己（如不要出现自己的名字）。"
    )
    raw = _invoke(prompt, f"你是「{name}」，正在直接与用户对话。", 0.8, 200,
                  "multi_line", user_text, ctx)
    raw = (raw or "").strip().strip('"').strip("“”").strip()
    m = re.match(r'^[^：:]{1,16}[：:]\s*(.+)', raw)
    if m:
        raw = m.group(1).strip()
    return raw


# ==================== 解析与匹配 ====================
def _match_speaker(spk, participants):
    if spk in participants:
        return spk
    # 模糊匹配仅在唯一命中时采用，避免相似人名（如「洛天依」与「洛天依（朋友）」）误配
    hits = [p for p in participants if spk in p or p in spk]
    return hits[0] if len(hits) == 1 else None


def _parse_dialogue(raw, participants):
    lines = []
    for ln in (raw or "").splitlines():
        ln = ln.strip().strip('"').strip("“”")
        if not ln:
            continue
        m = re.match(r'^([^：:]{1,16})[：:]\s*(.+)', ln)
        if not m:
            continue
        spk, content = m.group(1).strip(), m.group(2).strip()
        name = _match_speaker(spk, participants)
        if name and content and not re.match(r'^[（(]', content):
            lines.append((name, content))
        if len(lines) >= 5:
            break
    return lines


# ==================== 自然对话（接话式·流式） ====================
def _dbg(ctx, msg):
    """关键步骤调试输出：写入引擎运行日志 + 应用调试模式时打印控制台。"""
    try:
        from memory_engine import logging as me_log
        me_log.debug(f"[多人对话·自然] {msg}")
    except Exception:
        pass
    try:
        if getattr(config, "DEBUG_MODE", False):
            ctx.log(f"[多人对话·自然] {msg}")
    except Exception:
        pass


def _llm_detect_target(user_text, slots, ctx):
    """增强判断（可选）：用判断模型识别用户是否单独在与某一个角色说话（昵称/简称也认）。

    判断范围覆盖组内全部角色：用户可以指定任意一个角色，即使该角色上一轮没有发言。
    """
    names = "、".join(n for n, _ in slots)
    prompt = (
        f"以下是已配置的对话角色（组内全部角色）：{names}\n"
        f"用户说：{user_text}\n"
        "请判断用户是否在单独对某一个角色说话（可能使用昵称或简称）。\n"
        "注意：用户可以指定组内任意一个角色，包括上一轮没有发言的角色。\n"
        "如果明确指定了某一个角色，只输出该角色的完整名字（必须与上面列表完全一致）；\n"
        "如果没有明确指定，只输出“无”。\n"
        "只输出一个名字或“无”，不要任何解释。"
    )
    try:
        reply = (llm_core.generate(prompt, num_predict=16, temperature=0.0,
                                   purpose="multi_target", user_text=user_text) or "").strip()
        reply = reply.strip("。.！! \n")
        for n, _v in slots:
            if reply == n:
                return n
        # 容忍模型附带解释 / 引号
        for n, _v in slots:
            if n in reply:
                return n
    except Exception as e:
        ctx.log("多人对话增强判断失败:", e)
    return None


def _detect_target(user_text, slots, ctx):
    """对话对象检测：传统名字匹配优先；开启“增强判断”时未匹配到再用 LLM 判断。"""
    traditional = _specified_characters(user_text, slots)
    if traditional:
        _dbg(ctx, f"对话对象检测（传统名字匹配）: 命中 {traditional[0][0]}")
        return traditional
    settings = ctx.manager.get_settings(NAME)
    if settings.get("target_llm_judge", False):
        hit = _llm_detect_target(user_text, slots, ctx)
        if hit:
            _dbg(ctx, f"对话对象检测（LLM增强判断）: 命中 {hit}")
            return [(hit, dict(slots).get(hit, ""))]
        _dbg(ctx, "对话对象检测（LLM增强判断）: 未单独指定某角色")
    else:
        _dbg(ctx, "对话对象检测（传统名字匹配）: 未指定角色（增强判断未开启）")
    return []


def _memory_search(user_text, cast_names, ctx):
    """上下文查询（走记忆引擎，主干方案；引擎不可用时回退 None，由调用方兜底）。

    返回 (引擎或 None, 检索结果或 None)：引擎引用供上层调用注入复核等主干能力。
    """
    try:
        from memory_engine import service as mem_service
        eng = mem_service.get_engine_for_read()   # 门禁：记忆插件停用 / 引擎挂起 → 回退
        if eng is None:
            _dbg(ctx, "上下文查询: 记忆引擎不可用（插件停用或未初始化）→ 回退最近对话")
            return None, None
        try:
            eng.maybe_clear_stale()   # 纠错：长时间未活动（直接关窗退出后）先清理残留上下文
        except Exception:
            pass
        r = eng.search(user_text, role=cast_names, extra_query=eng.followup_extra(user_text))
        if r.id:
            _dbg(ctx, f"上下文查询命中: id={r.id} 置信度={r.confidence} route={r.route}"
                      f"（来源L1/L2/L3）参与者={'、'.join(r.participants)}")
            return eng, r
        _dbg(ctx, "上下文查询: 无相关长期记忆")
        return eng, None
    except Exception as e:
        _dbg(ctx, f"上下文查询异常（回退无记忆）: {e}")
    return None, None


def _online_info(user_text, ctx):
    """联网搜索（仅当上下文未命中时调用；判断模型裁决 + 主干搜索，失败回退 None）。"""
    try:
        from core.judge import judge_need_online
        from core.llm import generate_search_keyword
        from core.search import get_internet_info
        if not judge_need_online(user_text):
            _dbg(ctx, "联网判断: 无需联网")
            return None
        kw = generate_search_keyword(user_text)
        info = get_internet_info(kw)
        _dbg(ctx, f"联网判断: 需要联网 → 已获取信息（关键词={kw!r}，长度={len(info or '')}）")
        return info or None
    except Exception as e:
        _dbg(ctx, f"联网搜索异常（回退无联网信息）: {e}")
    return None


def _pick_first_speaker(cast, specified, memory, ctx):
    """首位发言人：① 用户单独指定 → 该角色；② 上下文相关角色（多角色相关时随机其一）；③ 随机。"""
    if specified:
        return specified[0]
    if memory and memory.participants:
        related = [s for s in cast if s[0] in memory.participants]
        if related:
            _dbg(ctx, f"首位发言人（上下文相关）: {'/'.join(n for n, _ in related)} → 随机取 {random.choice(related)[0]}")
            return random.choice(related)
    return random.choice(cast)


def _random_speakers(first, cast, total):
    """剩余发言人随机顺序：同一角色不超过两次，且不允许连续两次扮演相同角色。"""
    counts = {n: 0 for n, _ in cast}
    counts[first[0]] += 1
    rest = []
    prev = first[0]
    while len(rest) < total - 1:
        cands = [s for s in cast if counts[s[0]] < 2 and s[0] != prev]
        if not cands:
            # 其余候选都已达上限或只剩上一角色 → 提前结束，避免连续同角
            break
        s = random.choice(cands)
        counts[s[0]] += 1
        rest.append(s)
        prev = s[0]
    return rest


def _first_context_text(user_text, mem, online, recent_text):
    """首位发言人上下文块：相关记忆 + 联网信息 + 相关最近对话（合并注入，只注入首位）。

    最近的对话始终参与（经相关度过滤，话题切换不残留），避免被（哪怕是错误的）
    记忆命中挤掉真实对话上下文；联网信息与记忆同样只在该内容通过注入复核后保留。
    """
    blocks = []
    if online:
        blocks.append(f"从互联网查到的信息：\n{online}")
    if mem is not None:
        parts = ["相关历史记忆：", (mem.full_summary or "")]
        if mem.participants:
            parts.append("参与者：" + "、".join(mem.participants))
        blocks.append("\n".join(parts))
    if recent_text:
        blocks.append(f"最近的对话：\n{recent_text}")
    return "\n".join(blocks) + "\n" if blocks else ""


def _generate_natural_line(user_text, name, index, context_lines, first_context, ctx):
    """接话式单句生成：首位注入上下文/联网内容；后续根据前一句“接话”。"""
    desc = _char_desc(name)
    head = f"你是「{name}」。" + (f"角色描述：{desc}。" if desc else "")
    if index == 0:
        parts = [head, f"用户说：{user_text}"]
        if first_context:
            parts.append(first_context)
        parts += [
            "现在轮到你回应。请直接对用户说一句话（40字以内），口语化、符合角色性格，"
            "紧扣用户输入与上述内容。只输出这句话的内容本身，"
            "不要角色名前缀、不要解释、不要引号、不要用第三人称称呼自己（如不要出现自己的名字）。",
        ]
    else:
        prev_text = "；".join(f"{n}说：{t}" for n, t in context_lines[-4:]) or "无"
        parts = [
            head,
            f"用户说：{user_text}",
            f"前面已经说的话：{prev_text}",
            "现在轮到你接话。请接着上一句自然接话（可以回应、补充、调侃、追问），"
            "仍然直接对用户说，与前面的内容衔接自然。只输出这句话的内容本身（40字以内），"
            "口语化、符合角色性格，不要角色名前缀、不要解释、不要引号。",
        ]
    prompt = "\n".join(parts)
    raw = _invoke(prompt, f"你是「{name}」，正在直接与用户对话。", 0.8, 200,
                  "multi_line", user_text, ctx)
    raw = (raw or "").strip().strip('"').strip("“”").strip()
    m = re.match(r'^[^：:]{1,16}[：:]\s*(.+)', raw)
    if m:
        raw = m.group(1).strip()
    return raw


def _start_natural(user_text, slots, ctx):
    """启动自然对话（接话式）后台流式任务，返回 stream_id（调用方已持有 _run_lock）。"""
    sid = _new_stream()
    t = threading.Thread(target=_natural_worker, args=(sid, user_text, slots, ctx), daemon=True)
    t.start()
    return sid


def _natural_worker(sid, user_text, slots, ctx):
    """自然对话主流程：
    对话对象检测 → 回复数量随机 → 歌曲检测（已在入口透传）→ 上下文查询 → 联网搜索
    → 首位发言人（指定/上下文相关/随机）→ 其余随机顺序（重复≤2）→ 逐句“接话”生成+播放 → 归档。
    """
    global _voice_snapshot
    snapshot = None
    try:
        settings = ctx.manager.get_settings(NAME)
        try:
            max_lines = max(1, min(5, int(settings.get("max_lines", 5) or 5)))
        except Exception:
            max_lines = 5
        auto_voice = bool(settings.get("auto_voice", True))
        cast = list(slots)

        # 1) 对话对象检测（传统 + 可选 LLM 增强判断；判断范围=组内全部角色，含上一轮未发言者）
        specified = _detect_target(user_text, slots, ctx)
        _dbg(ctx, f"角色判断范围: 组内全部角色（{'、'.join(n for n, _ in cast)}，含上一轮未发言者）")

        # 2) 回复数量随机（≤max_lines；同一角色≤2次，且不允许连续两次扮演相同角色）
        n_replies = random.randint(1, min(max_lines, 2 * len(cast)))
        _dbg(ctx, f"回复数量（随机）: {n_replies} 句")

        # 3) 上下文查询（记忆引擎；不可用/未命中 → 无记忆）
        #    注入复核由上下文管理侧（记忆引擎 + 上下文记忆库插件设置）提供，走主干调用：
        #    内容与当前对话无关（判断模型输出 404 / 未明确 200）时阻止注入。
        eng_ref, mem = _memory_search(user_text, [n for n, _ in cast], ctx)
        if mem is not None and eng_ref is not None and not eng_ref.review_injection(user_text, mem.full_summary):
            _dbg(ctx, "记忆内容被注入复核阻止（404）→ 视为未命中，转联网搜索")
            mem = None
        # 相关最近对话始终参与首位上下文（经相关度过滤，话题切换不残留）
        try:
            fallback_hist = _recent_history(ctx, user_text)
        except Exception:
            fallback_hist = ""

        # 4) 联网搜索（仅当上下文未命中；联网内容同样经过注入复核）
        online = None
        if mem is None:
            online = _online_info(user_text, ctx)
            if online and eng_ref is not None and not eng_ref.review_injection(user_text, online):
                _dbg(ctx, "联网内容被注入复核阻止（404）→ 不注入首位")
                online = None

        # 5) 首位发言人（指定角色 → 上下文相关角色 → 随机）
        first = _pick_first_speaker(cast, specified, mem, ctx)
        _dbg(ctx, f"首位发言人: {first[0]}（{'用户指定' if specified else ('上下文相关' if (mem and first[0] in mem.participants) else '随机')}）")

        # 6) 其余发言人随机顺序（同一角色≤2次，不允许连续两次扮演相同角色）
        order = [first] + _random_speakers(first, cast, n_replies)
        _dbg(ctx, "发言人顺序: " + " → ".join(n for n, _ in order) + "（无连续同角，单角色≤2次）")

        # 7) 首位上下文块（联网 > 记忆 > 最近对话回退），只注入首位
        first_context = _first_context_text(user_text, mem, online, fallback_hist)

        snapshot = (
            config.REF_AUDIO_PATH, config.PROMPT_TEXT, config.CURRENT_VOICE_NAME,
            config.GPT_WEIGHTS_PATH, config.SOVITS_WEIGHTS_PATH,
        )
        _voice_snapshot = snapshot

        # 8) 逐句“接话”生成 + 各自声线合成（上一句语音播放期间生成下一句）
        context_lines = []
        prev_future = None
        prev_seq = None
        seq = 0
        voice_of = dict(cast)
        for name, _v in order:
            if _is_cancelled(sid):
                break
            line = _generate_natural_line(user_text, name, seq, context_lines, first_context, ctx)
            if not line:
                continue
            context_lines.append((name, line))
            _publish(sid, {"seq": seq, "speaker": name, "text": line, "audio": []})
            _dbg(ctx, f"第{seq + 1}/{len(order)}句已生成: {name}：{(line or '')[:30]}…")

            if prev_future is not None:
                paths = prev_future.result()
                _attach_audio(sid, prev_seq, [_to_url(p) for p in paths])

            if auto_voice:
                prev_future = _submit_tts(name, line, voice_of.get(name, ""), ctx)
            else:
                prev_future = None
            prev_seq = seq
            seq += 1

        if prev_future is not None:
            paths = prev_future.result()
            _attach_audio(sid, prev_seq, [_to_url(p) for p in paths])

        if not _is_cancelled(sid):
            speakers = list(dict.fromkeys(n for n, _ in order))
            _set_last(user_text, context_lines, speakers)
            reply = "\n".join(f"{n}：{t}" for n, t in context_lines)
            _record_turn(ctx, user_text, reply, speakers)
            _dbg(ctx, f"回合已归档（{len(context_lines)} 句，参与者={'、'.join(speakers)}），流程结束，等待下一次输入")
            _finish(sid, None)
    except Exception as e:
        ctx.log("自然对话流式生成异常:", e)
        _finish(sid, f"生成失败：{e}")
    finally:
        try:
            if snapshot:
                with _synth_lock:
                    _restore_snapshot(snapshot, ctx)
        except Exception:
            pass
        try:
            _run_lock.release()
        except Exception:
            pass


# ==================== 剧本模式：流式生成（后台任务 + 分段推送） ====================
_streams = {}        # sid -> {"segments": [...], "done": bool, "error": str|None, "ts": float}
_streams_lock = threading.Lock()
_STREAM_TTL = 600.0


def _prune_streams():
    now = time.time()
    with _streams_lock:
        stale = [s for s, v in _streams.items() if now - v["ts"] > _STREAM_TTL]
        for s in stale:
            _streams.pop(s, None)


def _new_stream():
    sid = uuid.uuid4().hex
    with _streams_lock:
        _streams[sid] = {"segments": [], "done": False, "error": None, "ts": time.time()}
    return sid


def _publish(sid, seg):
    with _streams_lock:
        v = _streams.get(sid)
        if v is None:
            return
        v["segments"].append(seg)
        v["ts"] = time.time()


def _attach_audio(sid, seq, urls):
    with _streams_lock:
        v = _streams.get(sid)
        if v is None:
            return
        for seg in v["segments"]:
            if seg.get("seq") == seq:
                seg["audio"] = [u for u in urls if u]
                break


def _finish(sid, error):
    with _streams_lock:
        v = _streams.get(sid)
        if v is None:
            return
        if not v.get("cancelled"):
            v["error"] = error
        v["done"] = True
        v["ts"] = time.time()


def _cancel_streams():
    """取消所有进行中的流（插件停用时调用）：标记 done + cancelled，worker 会在下一条前退出。"""
    with _streams_lock:
        for v in _streams.values():
            if not v.get("done"):
                v["done"] = True
                v["cancelled"] = True
                v["error"] = "多人对话已停止。"
                v["ts"] = time.time()


def _is_cancelled(sid):
    with _streams_lock:
        v = _streams.get(sid)
        return v is None or v.get("cancelled", False)


def poll_stream(sid):
    """供 Web 层轮询：返回 (segments, done, error)。"""
    _prune_streams()
    with _streams_lock:
        v = _streams.get(sid)
        if v is None:
            return [], True, "会话不存在或已过期"
        return list(v["segments"]), v["done"], v.get("error")


def _start_stream(user_text, slots, ctx):
    """启动后台流式生成，返回 stream_id（调用方已持有 _run_lock，worker 结束时释放）。"""
    sid = _new_stream()
    t = threading.Thread(target=_stream_worker, args=(sid, user_text, slots, ctx), daemon=True)
    t.start()
    return sid


def _stream_worker(sid, user_text, slots, ctx):
    """剧本模式后台任务：剧本 → 逐句生成（与上一句 TTS 并行）→ 逐段推送。"""
    global _voice_snapshot
    snapshot = None
    try:
        settings = ctx.manager.get_settings(NAME)
        try:
            max_lines = max(2, min(5, int(settings.get("max_lines", 5) or 5)))
        except Exception:
            max_lines = 5
        auto_voice = bool(settings.get("auto_voice", True))

        participants, total_lines = _select_participants(user_text, slots, max_lines)
        names = [p[0] for p in participants]
        voice_of = dict(participants)

        # 1) 剧本
        plan = _generate_script(user_text, participants, total_lines, ctx)
        # 只保留参与角色，且每个角色只保留最先出现的一条（剧本模式：每角色回应一句）
        deduped = []
        seen_names = set()
        for n, d in plan:
            if n in names and n not in seen_names:
                seen_names.add(n)
                deduped.append((n, d))
        plan = deduped
        if not plan:
            _finish(sid, "剧本生成失败，请换个说法重试。")
            return

        snapshot = (
            config.REF_AUDIO_PATH, config.PROMPT_TEXT, config.CURRENT_VOICE_NAME,
            config.GPT_WEIGHTS_PATH, config.SOVITS_WEIGHTS_PATH,
        )
        _voice_snapshot = snapshot

        # 2) 按剧本顺序逐句生成；上一句 TTS 与下一句 LLM 并行
        context_lines = []
        prev_future = None
        prev_seq = None
        seq = 0
        for name, direction in plan:
            if _is_cancelled(sid):
                break
            line = _generate_line(user_text, name, direction, context_lines, ctx)
            if not line:
                continue
            context_lines.append((name, line))
            _publish(sid, {"seq": seq, "speaker": name, "text": line, "audio": []})

            if prev_future is not None:
                paths = prev_future.result()
                _attach_audio(sid, prev_seq, [_to_url(p) for p in paths])

            if auto_voice:
                prev_future = _submit_tts(name, line, voice_of.get(name, ""), ctx)
            else:
                prev_future = None
            prev_seq = seq
            seq += 1

        if prev_future is not None:
            paths = prev_future.result()
            _attach_audio(sid, prev_seq, [_to_url(p) for p in paths])

        if not _is_cancelled(sid):
            _set_last(user_text, context_lines, names)
            reply = "\n".join(f"{n}：{t}" for n, t in context_lines)
            _record_turn(ctx, user_text, reply, names)
            _finish(sid, None)
    except Exception as e:
        ctx.log("多人对话流式生成异常:", e)
        _finish(sid, f"生成失败：{e}")
    finally:
        try:
            if snapshot:
                with _synth_lock:
                    _restore_snapshot(snapshot, ctx)
        except Exception:
            pass
        _run_lock.release()


def _submit_tts(name, line, voice, ctx):
    """后台合成一句（含声线切换），返回 Future（结果就绪后返回音频路径列表）。"""
    return _tts_pool.submit(_tts_job, name, line, voice, ctx)


def _tts_job(name, line, voice, ctx):
    with _synth_lock:
        return _synth_line_locked(name, line, voice, ctx)


def _synth_line_locked(name, line, voice, ctx):
    """在 _synth_lock 下用指定声线合成一句（不恢复声线，由调用方统一恢复）。"""
    try:
        if not tts_core.check_tts_api():
            return []
        if voice and voice != config.CURRENT_VOICE_NAME:
            try:
                tts_core.select_voice_preset_by_name(voice)
            except Exception as e:
                ctx.log(f"切换声线失败（{voice}）:", e)
        return tts_core.synthesize(line)
    except Exception as e:
        ctx.log("多人对话语音合成失败:", e)
        return []


# ==================== 单次模式的语音合成（整段预合成） ====================
def _synth_one(text, ctx):
    """合成单句文本，返回该句全部音频路径（一句可能被切成多个子句）。"""
    try:
        if not tts_core.check_tts_api():
            return []
        return tts_core.synthesize(text)
    except Exception as e:
        ctx.log("多人对话语音合成失败:", e)
        return []


def _restore_snapshot(snapshot, ctx):
    """恢复合成前的全局声线配置（含服务器模型权重）。

    若原声线没有单独权重（应用默认声线），服务器权重会切回其启动时加载的
    默认权重（读取 GPT-SoVITS tts_infer.yaml），避免主对话音色停留在
    多人对话的最后一个声线上。
    """
    try:
        (config.REF_AUDIO_PATH, config.PROMPT_TEXT, config.CURRENT_VOICE_NAME,
         config.GPT_WEIGHTS_PATH, config.SOVITS_WEIGHTS_PATH) = snapshot
        gpt, sovits = snapshot[3], snapshot[4]
        if not gpt or not sovits:
            dg, ds = tts_core.server_default_weights()
            if not gpt and dg:
                gpt = dg
            if not sovits and ds:
                sovits = ds
        if gpt:
            tts_core.switch_gpt_weights(gpt)
        if sovits:
            tts_core.switch_sovits_weights(sovits)
    except Exception as e:
        ctx.log("恢复声线失败:", e)


def _synth_by_voice(lines, participants, ctx):
    """按声线分组合成（相邻同声线合并，减少模型热切换次数），最后恢复原声线。"""
    global _voice_snapshot
    voice_of = dict(participants)
    groups = []  # [(声线名或"", [文本,...])]
    for name, text in lines:
        key = voice_of.get(name) or ""
        if groups and groups[-1][0] == key:
            groups[-1][1].append(text)
        else:
            groups.append((key, [text]))

    snapshot = (
        config.REF_AUDIO_PATH, config.PROMPT_TEXT, config.CURRENT_VOICE_NAME,
        config.GPT_WEIGHTS_PATH, config.SOVITS_WEIGHTS_PATH,
    )
    _voice_snapshot = snapshot
    applied = snapshot[2]
    results = []
    try:
        for key, texts in groups:
            target = key or snapshot[2]
            if target != applied:
                if key:
                    try:
                        tts_core.select_voice_preset_by_name(key)
                        applied = key
                    except Exception as e:
                        ctx.log(f"切换声线失败（{key}）:", e)
                else:
                    _restore_snapshot(snapshot, ctx)
                    applied = snapshot[2]
            for t in texts:
                results.append(_synth_one(t, ctx))
    finally:
        _restore_snapshot(snapshot, ctx)
    return results


def _to_url(path):
    """runtime 路径 → 可访问 URL（统一实现见 core/runtime.py；空路径给空串）。"""
    return runtime_url(path) or ""


# ==================== 状态展示 ====================
_last = {"user": "", "lines": [], "participants": []}


def _set_last(user, lines, participants):
    _last.update({"user": user, "lines": lines, "participants": participants})


def get_state(ctx):
    """插件管理页展示：当前参与人物 / 最近一场对话 / 是否生成中。"""
    with _streams_lock:
        streaming = any(not v["done"] for v in _streams.values())
    queue = []
    if _last.get("participants"):
        queue = [
            {"index": i + 1, "title": f"人物：{n}", "status": ""}
            for i, n in enumerate(_last["participants"])
        ]
    else:
        slots = _configured_slots(ctx)
        queue = [
            {"index": i + 1, "title": n + (f"（声线：{v}）" if v else "（默认声线）"), "status": ""}
            for i, (n, v) in enumerate(slots)
        ]
    st = ctx.manager.get_settings(NAME)
    return {
        "queue": queue,
        "last_user": _last.get("user", ""),
        "last_lines": len(_last.get("lines", [])),
        "streaming": streaming,
        "generation_mode": _mode_label(st.get("generation_mode", "script")),
        "saved": False,  # 预留：保存功能开放后置 True
    }


def _mode_label(mode):
    return {"script": "剧本模式（多次调用·流式）",
            "single": "单次模式（一次生成）",
            "natural": "自然对话（接话式·流式）"}.get(mode, str(mode))


# ==================== 命令 ====================
def commands():
    return [{"name": "/multi", "desc": "查看多人对话配置与状态", "args": ""}]


def on_command(command, args, ctx):
    if command != "/multi":
        return None
    slots = _configured_slots(ctx)
    if len(slots) < 2:
        return {"reply": "多人对话：请在「插件管理 → 多人对话 → 对话角色」中打开角色管理页，"
                         "勾选至少 2 位参与对话的角色。",
                "speak": False}
    lines = [f"{i + 1}. {n}" + (f"（声线：{v}）" if v else "（默认声线）") for i, (n, v) in enumerate(slots)]
    st = ctx.manager.get_settings(NAME)
    return {"reply": "多人对话已启用，参与人物：\n" + "\n".join(lines)
                     + f"\n语句上限：{st.get('max_lines', 5)} 句"
                     + f"\n生成方式：{_mode_label(st.get('generation_mode', 'script'))}"
                     + (f"\n增强判断：{'开启' if st.get('target_llm_judge', False) else '关闭'}"
                        if st.get('generation_mode', 'script') == 'natural' else ""),
            "speak": False}


# ==================== 多轮对话保存（预留接口，暂未启用） ====================
SAVE_FILE = os.path.join(config.RUNTIME_DIR, "multi_chat_history.json")


def save_dialogue(user_input, lines, participants, voice_map=None):
    """保存一场多人对话到本地文件。

    预留接口：基础功能测试通过后再开放（当前调用会明确报错，不会静默丢弃数据）。
    """
    raise NotImplementedError("多人对话保存功能暂未开放")
