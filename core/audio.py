# core/audio.py
# 文本清洗、分句，以及音频（WAV）解码与语音转写。
import io
import re
import wave

import numpy as np

from core.config import SAMPLE_RATE
from core import models


# ==================== 文本清洗（保留英文数字的处理） ====================
def force_chinese_only(text):
    """清洗文本用于显示：移除括号、波浪线换句号、表情换逗号、移除英文、去除空格。"""
    if not text:
        return ""
    # 1. 循环移除嵌套括号（全角/半角）
    while True:
        new_text = re.sub(r'[（(][^）()]*[）)]', '', text)
        if new_text == text:
            break
        text = new_text

    # 2. 波浪线替换为句号
    text = text.replace('~', '。').replace('～', '。')

    # 3. 表情符号替换为逗号
    text = re.sub(r'[\U0001F300-\U0001F9FF\u2600-\u27BF\uFE0F]', '，', text)

    # 4. 移除所有英文字母（但保留数字）
    text = re.sub(r'[a-zA-Z]', '', text)

    # 5. 移除所有空白字符（中文不需要空格）
    text = re.sub(r'\s+', '', text)

    # 6. 只保留中文、数字、常用标点
    text = re.sub(r'[^\u4e00-\u9fff0-9，。！？…、；：]', '', text)

    # 7. 合并多余标点
    text = re.sub(r'([，。！？…、；：]){2,}', r'\1', text)

    return text.strip('，。')


def safe_for_tts(text):
    """最终清洗：移除括号、波浪线替换、表情替换、温度单位转换、移除英文、去除空格。"""
    if not text:
        return ""
    # 1. 循环移除嵌套括号
    while True:
        new_text = re.sub(r'[（(][^）()]*[）)]', '', text)
        if new_text == text:
            break
        text = new_text

    # 2. 波浪线替换为句号
    text = text.replace('~', '。').replace('～', '。')

    # 3. 表情符号替换为逗号
    text = re.sub(r'[\U0001F300-\U0001F9FF\u2600-\u27BF\uFE0F]', '，', text)

    # 4. 温度单位转换：°C、℃ 等 -> 度
    text = re.sub(r'°[Cc]|℃', '度', text)

    # 5. 移除所有英文字母
    text = re.sub(r'[a-zA-Z]', '', text)

    # 6. 移除所有空白
    text = re.sub(r'\s+', '', text)

    # 7. 只保留中文、数字、标点
    text = re.sub(r'[^\u4e00-\u9fff0-9，。！？…、；：]', '', text)

    # 8. 合并多余标点
    text = re.sub(r'([，。！？…、；：]){2,}', r'\1', text)

    return text.strip('，。')


def split_sentences(text, max_len=30):
    """把长文本按标点切成适合语音合成的小句。"""
    raw = re.split(r'(?<=[。！？…；])', text)
    sentences = []
    buffer = ""

    for seg in raw:
        seg = seg.strip()
        if not seg:
            continue
        if buffer and re.search(r'[。！？…；]$', buffer):
            sentences.append(buffer)
            buffer = ""
        while len(seg) > max_len:
            cut_pos = -1
            for m in re.finditer(r'[，、]', seg[:max_len]):
                cut_pos = m.end()
            if cut_pos == -1:
                chunk = seg[:max_len]
                last_space = max(chunk.rfind(' '), chunk.rfind('\t'))
                cut_pos = last_space + 1 if last_space > max_len // 2 else max_len
            sentences.append(seg[:cut_pos].strip())
            seg = seg[cut_pos:].lstrip()
        if seg:
            buffer = seg if not buffer else buffer + seg

    if buffer:
        sentences.append(buffer)

    for i, s in enumerate(sentences):
        if not re.search(r'[。！？…；，、]$', s):
            s += '。'
        sentences[i] = s
    return sentences


# ==================== 音频解码与转写 ====================
def decode_wav_bytes_to_float32(data):
    """把浏览器上传的 WAV 字节解码为 16k 单声道 float32 数组。

    浏览器录音的采样率可能是 44.1k/48k，这里统一重采样到 SAMPLE_RATE。
    """
    with wave.open(io.BytesIO(data), 'rb') as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 1:
        pcm = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        pcm = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的采样位宽: {sampwidth}")

    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1)

    # 重采样到目标采样率
    if framerate != SAMPLE_RATE and len(pcm) > 0:
        n_out = int(round(len(pcm) * SAMPLE_RATE / framerate))
        pcm = np.interp(
            np.linspace(0.0, len(pcm) - 1, n_out),
            np.arange(len(pcm)),
            pcm,
        ).astype(np.float32)
    return pcm


def transcribe_wav_bytes(data):
    """解码 WAV 字节并转写为中文文本。"""
    audio_np = decode_wav_bytes_to_float32(data)
    if audio_np is None or len(audio_np) < SAMPLE_RATE * 0.3:
        return ""
    return models.transcribe_audio(audio_np)
