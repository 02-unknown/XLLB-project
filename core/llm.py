# core/llm.py
# 大模型客户端：统一封装 Ollama（本地）与 OpenAI 兼容 API 两种后端，
# 支持生成模型与判断模型分别配置，并为插件系统提供 pre/post LLM 钩子。
import os
import random
import re
import threading
import time

import requests

import core.config as config
from core.character import build_system_prompt
from core.audio import force_chinese_only
from core import plugin_manager


# ==================== 后端探测 ====================
def check_ollama():
    """探测 Ollama 服务是否就绪（复用模型列表缓存，避免频繁请求 /api/tags）。"""
    return bool(list_ollama_models())


def ollama_used():
    """Ollama 是否被当前配置实际使用（生成或判断后端任一为 ollama）。

    Lite 模式强制使用外部 API 后，即使本机 Ollama 服务在运行，
    也不应把 Ollama 视为"可用"或展示其模型列表。
    """
    return (config.LLM_CHAT_BACKEND == "ollama"
            or config.LLM_JUDGE_BACKEND == "ollama")


def _backend_ready(backend):
    if backend == "openai":
        key = config.LLM_API_KEY or os.environ.get("OPENAI_API_KEY", "")
        return bool(config.LLM_API_BASE) and bool(key)
    return check_ollama()


def check_backend():
    """探测生成模型后端是否可用。"""
    return _backend_ready(config.LLM_CHAT_BACKEND)


def check_judge_backend():
    """探测判断模型后端是否可用。"""
    return _backend_ready(config.LLM_JUDGE_BACKEND)


# Ollama 模型列表缓存：避免每次打开插件设置都同步请求 /api/tags 造成卡顿。
_models_cache = {"ts": 0.0, "data": []}
_models_lock = threading.Lock()


def list_ollama_models(ttl=30.0):
    """列出本地 Ollama 已安装的模型名（带 TTL 缓存，失败时回退旧缓存）。"""
    with _models_lock:
        if time.time() - _models_cache["ts"] < ttl:
            return list(_models_cache["data"])
    try:
        resp = requests.get(config.OLLAMA_TAGS_API, timeout=2)
        if resp.status_code == 200:
            data = [m.get("name", "") for m in resp.json().get("models", [])]
        else:
            data = list(_models_cache["data"])
    except Exception:
        data = list(_models_cache["data"])
    with _models_lock:
        _models_cache["ts"] = time.time()
        _models_cache["data"] = data
    return list(data)


def warm_ollama_models():
    """后台预热模型列表缓存，避免首次打开插件设置时阻塞。"""
    threading.Thread(target=list_ollama_models, daemon=True).start()


# ==================== 后端底层请求 ====================
def _ollama_chat(messages, model, temperature, num_predict, stop):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "enable_thinking": False,
        },
    }
    if stop:
        payload["options"]["stop"] = list(stop)
    resp = requests.post(config.OLLAMA_CHAT_API, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message", {})
    reply = msg.get("content", "").strip()
    if not reply and "thinking" in msg:
        parts = re.split(r'\n\s*\n', msg["thinking"])
        for part in reversed(parts):
            clean = force_chinese_only(part)
            if len(clean) > 4:
                reply = clean
                break
    return reply


def _ollama_generate(prompt, model, temperature, num_predict, stop):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            "enable_thinking": False,
        },
    }
    if stop:
        payload["options"]["stop"] = list(stop)
    resp = requests.post(config.OLLAMA_GENERATE_API, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _openai_chat(messages, model, temperature, num_predict, stop):
    base = (config.LLM_API_BASE or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 API Base URL")
    payload = {"model": model, "messages": messages, "temperature": temperature}
    # 兼容 OpenAI 风格的接口：不传 stop / max_tokens。
    # 部分模型（含推理型）会先输出说明再给出答案，stop 会在首个句号/换行处截断，
    # max_tokens 过小会截掉答案；因此让模型生成到自然结束，再由调用方做稳健提取。
    headers = {"Content-Type": "application/json"}
    api_key = config.LLM_API_KEY or os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(base + "/chat/completions", json=payload, headers=headers, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content", "").strip()


def _raw_chat(messages, model, backend, temperature, num_predict, stop):
    if backend == "openai":
        return _openai_chat(messages, model, temperature, num_predict, stop)
    return _ollama_chat(messages, model, temperature, num_predict, stop)


def _raw_generate(prompt, model, backend, temperature, num_predict, stop):
    if backend == "openai":
        return _openai_chat([{"role": "user", "content": prompt}], model, temperature, num_predict, stop)
    return _ollama_generate(prompt, model, temperature, num_predict, stop)


# ==================== 带插件钩子的调用 ====================
def _invoke_chat(messages, model, temperature, num_predict, stop, purpose, user_text):
    backend = config.LLM_CHAT_BACKEND
    meta = {"purpose": purpose, "model": model, "backend": backend, "user_text": user_text}
    short = plugin_manager.manager.pre_llm(messages, meta)
    if short is not None:
        reply = str(short.get("reply", ""))
    else:
        try:
            reply = _raw_chat(messages, model, backend, temperature, num_predict, stop)
        except Exception as e:
            print("LLM 错误:", e)
            reply = "抱歉，我出了点问题。"
    reply = plugin_manager.manager.post_llm(reply, meta)
    return reply or ""


def generate(prompt, model=None, num_predict=64, temperature=0.0, stop=("\n", "。"),
             purpose="generate", user_text=""):
    """以“判断模型”执行一次提示词生成（供联网判断、关键词、音乐意图等使用）。"""
    model = model or config.LLM_JUDGE_MODEL
    backend = config.LLM_JUDGE_BACKEND
    meta = {"purpose": purpose, "model": model, "backend": backend, "user_text": user_text}
    short = plugin_manager.manager.pre_llm([{"role": "user", "content": prompt}], meta)
    if short is not None:
        reply = str(short.get("reply", ""))
    else:
        try:
            reply = _raw_generate(prompt, model, backend, temperature, num_predict, stop)
        except Exception as e:
            print(f"生成失败({purpose}): {e}")
            reply = ""
    reply = plugin_manager.manager.post_llm(reply, meta)
    return reply or ""


# ==================== 主对话 ====================
def call_ollama(user_message, extra_context=""):
    """主对话（使用生成模型），维护全局对话历史并清洗输出。"""
    current_influence = random.randint(config.influence_min, config.influence_max)
    system_prompt = build_system_prompt(config.character_name, current_influence)

    if not config.conversation_history or config.conversation_history[0]["role"] != "system":
        config.conversation_history.insert(0, {"role": "system", "content": system_prompt})
    else:
        config.conversation_history[0]["content"] = system_prompt

    messages_for_request = config.conversation_history.copy()

    if extra_context:
        user_prompt = (
            f"用户刚才问：{user_message}\n\n"
            f"以下是系统从互联网查到的信息：\n{extra_context}\n\n"
            f"请直接基于以上信息回答问题，用中文、以{config.character_name}的口吻，简洁回答。不要输出任何“需要联网”的标记。"
        )
        messages_for_request.append({"role": "user", "content": user_prompt})
    else:
        messages_for_request.append({"role": "user", "content": user_message})

    reply = _invoke_chat(
        messages_for_request, config.LLM_CHAT_MODEL,
        temperature=0.3 if extra_context else 0.7,
        num_predict=-1, stop=None, purpose="chat", user_text=user_message,
    )

    if config.DEBUG_MODE:
        print("* (llm)真实原始回复:\n", reply)

    reply = force_chinese_only(reply)
    reply = re.sub(r'[*_#]', '', reply)
    reply = re.sub(r'(.)\1{2,}', r'\1', reply)

    if not re.search(r'[\u4e00-\u9fff]', reply) or len(reply) < 3:
        reply = "哎呀，我有点卡壳了，换个问题试试？"

    config.conversation_history.append({"role": "user", "content": user_message})
    config.conversation_history.append({"role": "assistant", "content": reply})

    if len(config.conversation_history) > 21:
        config.conversation_history = [config.conversation_history[0]] + config.conversation_history[-(21 - 1):]

    return reply


call_llm = call_ollama  # 兼容别名


def _clean_keyword(text):
    """从（可能包含说明的）模型输出中稳健提取关键词：
    取最后一行（推理模型通常把最终答案放最后），去掉“关键词：”前缀并清洗。"""
    text = (text or "").strip()
    if not text:
        return ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        text = lines[-1]
    text = re.sub(r'^(关键词|keyword|搜索关键词)\s*[:：]?\s*', '', text)
    text = re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9 ]', '', text)
    return text.strip()


def generate_search_keyword(user_text):
    recent_user_msgs = [m["content"] for m in config.conversation_history[-3:] if m["role"] == "user"]
    context = "；".join(recent_user_msgs) if recent_user_msgs else user_text
    prompt = (
        f"对话历史：{context}\n\n"
        f"当前问题：{user_text}\n"
        f"请根据对话历史，为当前问题生成一个简洁的搜索关键词（短语），"
        f"用于搜索引擎查询。例如，如果历史中是“上海天气”，当前问题“那苏州呢”，"
        f"则关键词应为“苏州天气”。只输出关键词本身，不要任何解释。"
    )
    keyword = _clean_keyword(generate(prompt, num_predict=-1, purpose="search_keyword", user_text=user_text))
    return keyword if keyword else user_text


def generate_music_search_keyword(user_text):
    prompt = (
        f"你是一个音乐搜索助手。用户想听一首歌，可能包含口语化表达或错误。\n"
        f"请提取歌曲名称及可能的歌手名，生成一个简洁的搜索关键词，用于在B站搜索音乐视频。\n"
        f"【重要】除非你认为有非常大的可能是输入错误，否则禁止修改用户输入。\n"
        f"示例：\n"
        f"用户输入：给我唱一首 想见你 想见你 想见你 → 关键词：想见你想见你想见你\n"
        f"用户输入：放一首周金人的晴天 → 关键词：周杰伦 晴天\n"
        f"用户输入：播放 晴天 → 关键词：晴天\n"
        f"只能输出简体中文或可能的英文单词。\n"
        f"只输出关键词本身，不要任何解释。\n\n"
        f"用户输入：{user_text}\n"
        f"关键词："
    )
    keyword = _clean_keyword(generate(prompt, num_predict=-1, purpose="music_keyword", user_text=user_text))
    return keyword if keyword else user_text


_STRONG_MUSIC_TRIGGERS = ["放一首", "播放", "唱一首", "点歌", "来一首", "我要听", "给我放", "给我唱"]


def confirm_music_intent(user_text):
    """用判断模型复核是否点歌；模型无响应时按强触发词兜底，保证 API 后端下点歌仍可用。"""
    prompt = (
        f"请判断以下用户输入是否在请求播放或点播一首歌曲（例如“放一首XXX”、“播放XXX”、“唱XXX”）。\n"
        f"如果是点歌请求，请只回复“是”；否则只回复“否”。\n\n"
        f"用户输入：{user_text}\n"
        f"回复："
    )
    result = (generate(prompt, num_predict=-1, purpose="music_intent", user_text=user_text) or "").strip()
    if config.DEBUG_MODE:
        print(f"* (llm) 音乐意图复核结果: {result}")

    if result:
        # 在整个输出中查找结论，兼容模型先说明再给答案的情况
        if "否" in result or "不是" in result or "不算" in result:
            return False
        if "是" in result:
            return True
        return False
    # 判断模型未返回有效结果时，用强触发词兜底
    return any(w in user_text for w in _STRONG_MUSIC_TRIGGERS)
