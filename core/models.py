# core/models.py
# Whisper 语音识别模型的懒加载与线程安全封装。
import threading
from core.config import WHISPER_MODEL_DIR

_model = None
_ready = False
_error = None
_lock = threading.Lock()


def init_models():
    """惰性加载 Whisper 模型（进程内只加载一次）。"""
    global _model, _ready, _error
    if _ready or _error is not None:
        return
    with _lock:
        if _ready or _error is not None:
            return
        try:
            from faster_whisper import WhisperModel
            print("加载 Whisper 模型...")
            _model = WhisperModel(WHISPER_MODEL_DIR, device="cpu", compute_type="int8")
            _ready = True
            print("Whisper 模型已就绪。")
        except Exception as e:  # 模型文件缺失等情况不应导致整个程序崩溃
            _error = str(e)
            print(f"Whisper 模型加载失败（语音识别不可用）: {e}")


def is_ready():
    return _ready


def last_error():
    return _error


def transcribe_audio(audio_np):
    """把 numpy(float32, 16k 单声道) 音频转写为中文文本。"""
    init_models()
    if not _ready:
        raise RuntimeError(_error or "Whisper 模型未就绪")
    with _lock:
        segments, _ = _model.transcribe(
            audio_np,
            language="zh",
            beam_size=3,
            initial_prompt="以下是普通话的句子，请使用简体中文输出。",
        )
        return " ".join([seg.text for seg in segments]).strip()
