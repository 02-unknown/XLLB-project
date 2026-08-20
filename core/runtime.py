# core/runtime.py
# 运行时生成文件（合成语音、临时音乐）的清理，避免缓存无限累积。
import os
import time

import core.config as config


def cleanup_runtime(max_age_seconds=None):
    """清理 runtime 目录下的生成文件。

    max_age_seconds 为 None 时删除全部；否则仅删除超过该时长的文件。
    返回被删除的文件数量。
    """
    removed = 0
    for directory in (config.TTS_OUTPUT_DIR, config.MUSIC_OUTPUT_DIR):
        if not os.path.isdir(directory):
            continue
        now = time.time()
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if not os.path.isfile(path):
                    continue
                if max_age_seconds is None or now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed
