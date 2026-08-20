# core/storage.py
# 对话记录保存（文本 + 可选语音合成）与历史管理。
import os
import time

import requests

import core.config as config
from core.audio import safe_for_tts
from core.tts import load_voice_presets


def _resolve_ref_audio_path():
    """从当前语音预设中解析参考音频路径，返回绝对路径。"""
    voice_name = config.CURRENT_VOICE_NAME
    ref_path = None
    if voice_name == "默认":
        ref_path = os.path.abspath(config.REF_AUDIO_PATH)
    else:
        for name, cfg in load_voice_presets():
            if name == voice_name:
                ref_path = cfg.get("ref_audio_path")
                if ref_path:
                    ref_path = os.path.abspath(ref_path)
                break
    if not ref_path:
        ref_path = os.path.abspath(config.REF_AUDIO_PATH)
    return ref_path


def _synthesize_record_audio(text, ref_path):
    """把记录文本合成为音频，返回 wav 文件路径或 None。"""
    safe = safe_for_tts(text)
    if not safe:
        return None
    payload = {
        "text": safe,
        "text_lang": "zh",
        "ref_audio_path": ref_path,
        "prompt_text": config.PROMPT_TEXT,
        "prompt_lang": "zh",
    }
    resp = requests.post(config.GPT_SOVITS_API, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.content


def save_marked_records():
    """把标记为已保存的记录输出到 save-au / save-wo。返回结果摘要。"""
    marked = [rec for rec in config.history_records if rec.get('saved', False)]
    result = {"count": len(marked), "items": []}
    if not marked:
        return result

    os.makedirs(config.SAVE_AU_DIR, exist_ok=True)
    os.makedirs(config.SAVE_WO_DIR, exist_ok=True)

    ref_path = _resolve_ref_audio_path()
    ref_ok = os.path.exists(ref_path)

    for rec in marked:
        idx = config.history_records.index(rec) + 1
        item = {"index": idx, "text": True, "audio": False}

        txt_file = os.path.join(config.SAVE_WO_DIR, f"record_{idx}.txt")
        with open(txt_file, "w", encoding="utf-8") as f:
            f.write(f"用户: {rec['user']}\n助手: {rec['assistant']}")

        if ref_ok:
            try:
                audio = _synthesize_record_audio(rec['assistant'], ref_path)
                if audio:
                    wav_file = os.path.join(config.SAVE_AU_DIR, f"record_{idx}.wav")
                    with open(wav_file, "wb") as f:
                        f.write(audio)
                    item["audio"] = True
            except Exception as e:
                print(f"保存记录 {idx} 语音失败: {e}")
        result["items"].append(item)
    return result


def quick_save_latest():
    """保存最新的一条对话记录（文本 + 可选语音）。返回结果摘要。"""
    if not config.history_records:
        return {"count": 0, "items": []}

    latest = config.history_records[-1]
    idx = config.increment_save_counter()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    voice_model = config.CURRENT_VOICE_NAME.replace(" ", "_")
    char_name = config.character_name.replace(" ", "_")
    base_name = f"{char_name}_{voice_model}_{timestamp}_{idx}"

    os.makedirs(config.SAVE_AU_DIR, exist_ok=True)
    os.makedirs(config.SAVE_WO_DIR, exist_ok=True)

    txt_file = os.path.join(config.SAVE_WO_DIR, f"{base_name}.txt")
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(f"用户: {latest['user']}\n助手: {latest['assistant']}")

    item = {"name": base_name, "text": True, "audio": False}
    ref_path = os.path.abspath(config.REF_AUDIO_PATH)
    if os.path.exists(ref_path):
        try:
            audio = _synthesize_record_audio(latest['assistant'], ref_path)
            if audio:
                wav_file = os.path.join(config.SAVE_AU_DIR, f"{base_name}.wav")
                with open(wav_file, "wb") as f:
                    f.write(audio)
                item["audio"] = True
        except Exception as e:
            print(f"语音保存失败: {e}")
    return {"count": 1, "items": [item]}


# ==================== 历史记录管理 ====================
def append_history(user_text, reply):
    config.history_records.append({'user': user_text, 'assistant': reply, 'saved': False})
    if len(config.history_records) > config.MAX_HISTORY:
        for i, rec in enumerate(config.history_records):
            if not rec.get('saved', False):
                del config.history_records[i]
                break
        else:
            del config.history_records[0]


def get_history():
    return [
        {"index": i + 1, "user": rec["user"], "assistant": rec["assistant"],
         "saved": bool(rec.get("saved", False))}
        for i, rec in enumerate(config.history_records)
    ]


def mark_records(indexes):
    """按 1-based 序号标记要保存的记录，返回成功标记的数量。"""
    marked = 0
    for idx in indexes:
        i = int(idx) - 1
        if 0 <= i < len(config.history_records):
            config.history_records[i]['saved'] = True
            marked += 1
    return marked


def clear_history():
    config.history_records.clear()
    config.conversation_history.clear()
