# core/music.py
# Bilibili 音乐搜索与下载（音频由浏览器播放）。
import hashlib
import os

import yt_dlp

import core.config as config

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _ffmpeg_location():
    """返回 ffmpeg 路径：依次检查项目相对路径、系统 PATH、旧默认路径。"""
    import shutil
    # 1) 项目内相对路径（便于打包）
    if os.path.exists(config.FFMPEG_PATH):
        return config.FFMPEG_PATH
    # 2) 系统 PATH
    p = shutil.which("ffmpeg")
    if p:
        return p
    # 3) 旧默认路径（兼容已有部署）
    legacy = r"D:\ffmpeg-8.0.1-essentials_build\bin\ffmpeg.exe"
    if os.path.exists(legacy):
        return legacy
    return None


def search_music(query, max_results=5):
    """在 B 站搜索音乐视频，返回 [{title, url}]。"""
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "format": "bestaudio/best",
        "proxy": "",
        "http_headers": {"User-Agent": _USER_AGENT, "Referer": "https://www.bilibili.com/"},
        "default_search": "bilisearch",
        "noplaylist": True,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"bilisearch{max_results}:{query}", download=False)
            entries = info.get("entries", []) if info else []
            if not entries:
                entries = [info] if info else []

            videos = []
            for e in entries:
                if e is None:
                    continue
                url = e.get("webpage_url", "")
                if "/video/" in url and "bilibili.com/video/" in url:
                    videos.append({"title": e.get("title") or "未知标题", "url": url})
                if len(videos) >= max_results:
                    break
            return videos
    except Exception as e:
        print(f"音乐搜索失败: {e}")
        return []


def download_music(video_url, title=""):
    """下载视频音频并转为 WAV，返回稳定命名的文件路径。"""
    os.makedirs(config.MUSIC_OUTPUT_DIR, exist_ok=True)
    key = hashlib.md5((video_url or "").encode("utf-8")).hexdigest()[:12]
    outtmpl = os.path.join(config.MUSIC_OUTPUT_DIR, f"music_{key}.%(ext)s")
    final_wav = os.path.join(config.MUSIC_OUTPUT_DIR, f"music_{key}.wav")

    # 已缓存过则直接返回
    if os.path.exists(final_wav):
        return final_wav

    ydl_opts = {
        "quiet": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "ffmpeg_location": _ffmpeg_location(),
        "proxy": "",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)
        if os.path.exists(final_wav):
            return final_wav
        return None
    except Exception as e:
        print(f"音乐下载失败: {e}")
        return None


def cleanup_music(path=None):
    """删除临时音乐文件。path 为空时清空整个音乐目录。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            return
        if os.path.isdir(config.MUSIC_OUTPUT_DIR):
            for f in os.listdir(config.MUSIC_OUTPUT_DIR):
                try:
                    os.remove(os.path.join(config.MUSIC_OUTPUT_DIR, f))
                except Exception:
                    pass
    except Exception as e:
        print(f"清理音乐文件失败: {e}")
