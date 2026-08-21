# core/tts.py
# GPT-SoVITS 语音合成、语音预设管理与模型热切换。
# 注意：Web 端由浏览器播放音频，因此这里只负责“合成”并返回文件路径，不再用 pygame 播放。
import os
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import core.config as config
from core.audio import safe_for_tts, force_chinese_only, split_sentences

# 并发生成句子的并发度：提前把后续句子的合成请求排队，减少上下句之间的等待。
TTS_CONCURRENCY = 2


def check_tts_api():
    """探测 GPT-SoVITS API 是否就绪。"""
    try:
        resp = requests.get(f"{config.GPT_SOVITS_BASE}/docs", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def generate_audio_for_sentence(sentence):
    """调用 GPT-SoVITS API 合成单句音频，返回绝对文件路径。"""
    safe_sentence = safe_for_tts(sentence)
    if not safe_sentence:
        print(f"句子清洗后为空，跳过合成: {repr(sentence)}")
        return None

    char_name = re.sub(r'[（(][^）)]*[）)]', '', config.character_name)
    print(f"{char_name}:{safe_sentence}")

    ref_audio_abs = os.path.abspath(config.REF_AUDIO_PATH)
    payload = {
        "text": safe_sentence,
        "text_lang": "zh",
        "ref_audio_path": ref_audio_abs,
        "prompt_text": config.PROMPT_TEXT,
        "prompt_lang": "zh",
    }

    try:
        resp = requests.post(config.GPT_SOVITS_API, json=payload, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"生成音频失败: {e}")
        try:
            print("API 响应内容:", resp.text)
        except Exception:
            pass
        return None

    os.makedirs(config.TTS_OUTPUT_DIR, exist_ok=True)
    audio_file = os.path.join(
        config.TTS_OUTPUT_DIR,
        f"sentence_{int(time.time() * 1000)}_{random.randint(0, 9999)}.wav",
    )
    with open(audio_file, "wb") as f:
        f.write(resp.content)
    return audio_file


def iter_audio(text):
    """逐句合成并逐句产出音频路径（生成器）。

    使用有界并发（TTS_CONCURRENCY）提前排队后续句子的合成请求，
    第一句生成完即可开始播放，同时后续句子已在合成中，显著减少上下句衔接等待。
    """
    if not text or not text.strip():
        return
    clean = force_chinese_only(text)
    if not clean:
        return

    sentences = [s for s in split_sentences(clean) if s and re.search(r"[\u4e00-\u9fff]", s)]
    if not sentences:
        return

    if config.DEBUG_MODE:
        print("* (tts)分句结果:")
        for i, s in enumerate(sentences):
            print(f"{i + 1}. {s}")

    # 单句直接合成；多句用线程池并发生成，按顺序 yield（保持播放顺序）
    if len(sentences) == 1:
        path = generate_audio_for_sentence(sentences[0])
        if path:
            yield path
        return

    workers = min(TTS_CONCURRENCY, len(sentences))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(generate_audio_for_sentence, s) for s in sentences]
        for fut in futures:
            try:
                path = fut.result()
            except Exception as e:
                print(f"句子合成异常: {e}")
                path = None
            if path:
                yield path


def synthesize(text):
    """一次性合成整段文本，返回音频路径列表（非流式，供保存等场景使用）。"""
    return list(iter_audio(text))


class TtsStreamer:
    """在后台线程逐句合成，供 Web 层按需拉取（长轮询）。"""

    _DONE = object()

    def __init__(self, text):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(text,), daemon=True)
        self._thread.start()

    def _run(self, text):
        try:
            for path in iter_audio(text):
                self._queue.put(path)
        finally:
            self._queue.put(self._DONE)

    def get(self, timeout=30.0):
        """返回 (audio_path, done)；超时未就绪时返回 (None, False)。"""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None, False
        if item is self._DONE:
            return None, True
        return item, False


# ==================== 语音预设管理（自动识别文件夹） ====================
def scan_voice_preset_folder(folder_path):
    """扫描单个预设文件夹，返回配置字典。至少需要一个 .wav 参考音频。"""
    cfg = {
        "ref_audio_path": "",
        "prompt_text": "",
        "gpt_weights": "",
        "sovits_weights": "",
        "description": os.path.basename(folder_path),
    }

    files = os.listdir(folder_path)
    wav_files = [f for f in files if f.lower().endswith(".wav")]
    if wav_files:
        cfg["ref_audio_path"] = os.path.join(folder_path, wav_files[0]).replace("\\", "/")

    if "prompt.txt" in files:
        try:
            with open(os.path.join(folder_path, "prompt.txt"), "r", encoding="utf-8") as f:
                cfg["prompt_text"] = f.read().strip()
        except Exception:
            pass

    gpt_files = [f for f in files if f.lower().endswith(".ckpt")]
    if gpt_files:
        cfg["gpt_weights"] = os.path.join(folder_path, gpt_files[0]).replace("\\", "/")

    sovits_files = [f for f in files if f.lower().endswith(".pth")]
    if sovits_files:
        cfg["sovits_weights"] = os.path.join(folder_path, sovits_files[0]).replace("\\", "/")

    info_files = [f for f in files if f.lower() == "info.txt"]
    if info_files:
        try:
            with open(os.path.join(folder_path, "info.txt"), "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if first_line:
                    cfg["description"] = first_line
        except Exception:
            pass

    return cfg if cfg["ref_audio_path"] else None


def load_voice_presets():
    """自动扫描 voice_presets 文件夹，返回预设列表 [(名称, 配置)]。"""
    presets = []
    os.makedirs(config.VOICE_PRESETS_DIR, exist_ok=True)
    for folder in os.listdir(config.VOICE_PRESETS_DIR):
        folder_path = os.path.join(config.VOICE_PRESETS_DIR, folder)
        if os.path.isdir(folder_path):
            cfg = scan_voice_preset_folder(folder_path)
            if cfg:
                presets.append((folder, cfg))
    return presets


def _default_voice_config():
    return {
        "ref_audio_path": config.REF_AUDIO_PATH,
        "prompt_text": config.PROMPT_TEXT,
        "gpt_weights": config.GPT_WEIGHTS_PATH,
        "sovits_weights": config.SOVITS_WEIGHTS_PATH,
        "description": "默认音色",
    }


def list_voice_presets():
    """返回语音预设概览列表，供 Web 界面展示。"""
    return [
        {"name": name, "description": cfg.get("description", name)}
        for name, cfg in load_voice_presets()
    ]


def apply_voice_preset(preset_name, cfg):
    """应用选中的语音预设：更新全局变量，并通过 API 切换模型。"""
    config.REF_AUDIO_PATH = cfg.get("ref_audio_path", config.REF_AUDIO_PATH)
    config.PROMPT_TEXT = cfg.get("prompt_text", config.PROMPT_TEXT)
    config.CURRENT_VOICE_NAME = preset_name
    config.GPT_WEIGHTS_PATH = cfg.get("gpt_weights", "")
    config.SOVITS_WEIGHTS_PATH = cfg.get("sovits_weights", "")
    print(f"已加载语音预设：{preset_name}")

    if config.GPT_WEIGHTS_PATH:
        switch_gpt_weights(config.GPT_WEIGHTS_PATH)
    if config.SOVITS_WEIGHTS_PATH:
        switch_sovits_weights(config.SOVITS_WEIGHTS_PATH)
    return preset_name


def select_voice_preset_by_name(name):
    """按名称选择语音预设。name == '默认' 或空表示使用默认配置。"""
    if not name or name == "默认":
        return apply_voice_preset("默认", _default_voice_config())
    for preset_name, cfg in load_voice_presets():
        if preset_name == name:
            return apply_voice_preset(preset_name, cfg)
    raise ValueError(f"未找到语音预设：{name}")


def create_voice_preset(name):
    """创建新的语音预设文件夹，返回其路径。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    folder_path = os.path.join(config.VOICE_PRESETS_DIR, name)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


# ==================== GPT-SoVITS 模型热切换 ====================
def switch_gpt_weights(weights_path):
    abs_path = os.path.abspath(weights_path)
    try:
        resp = requests.get(f"{config.GPT_SOVITS_BASE}/set_gpt_weights?weights_path={abs_path}", timeout=30)
        if resp.status_code == 200:
            print("GPT 模型已切换")
        else:
            print(f"GPT 模型切换失败: {resp.text}")
    except Exception as e:
        print(f"GPT 模型切换出错: {e}")


def switch_sovits_weights(weights_path):
    abs_path = os.path.abspath(weights_path)
    try:
        resp = requests.get(f"{config.GPT_SOVITS_BASE}/set_sovits_weights?weights_path={abs_path}", timeout=30)
        if resp.status_code == 200:
            print("SoVITS 模型已切换")
        else:
            print(f"SoVITS 模型切换失败: {resp.text}")
    except Exception as e:
        print(f"SoVITS 模型切换出错: {e}")
