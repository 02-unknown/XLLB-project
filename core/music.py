# core/music.py
# Bilibili 音乐搜索与下载（音频由浏览器播放）。
import hashlib
import json
import os
import re
import subprocess
import time

import yt_dlp

import core.config as config

# 「更流畅的音乐播放」插件里保存音量均衡设置（下载后均衡一次，之后不再参与任何音量调整）
NORMALIZE_PLUGIN = "更流畅的音乐播放"
DEFAULT_TARGET_LUFS = -14.0
_MAX_GAIN_DB = 24.0        # 增益限幅：避免把底噪一起放大或在异常测量上炸音

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
    legacy = r""
    if os.path.exists(legacy):
        return legacy
    return None


def ffmpeg_path():
    """公开的 ffmpeg 路径（响度均衡 / 诊断用）。"""
    return _ffmpeg_location()


def _silent_run(args, timeout=300):
    """静默执行外部命令（不弹控制台窗口），返回 (returncode, 输出文本)。"""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stderr or "") + (proc.stdout or "")
    except Exception as e:
        return -1, str(e)


def measure_loudness(path, ffmpeg=None):
    """量出整首的整合响度（LUFS，Integrated）。

    这就是「算均值」那一步：用 ffmpeg 的 ebur128 扫整首取平均响度，
    而不是播放时逐帧动态压限。失败（没装 ffmpeg / 文件损坏）返回 None。
    """
    exe = ffmpeg or _ffmpeg_location()
    if not exe or not path or not os.path.isfile(path):
        return None
    rc, text = _silent_run([exe, "-hide_banner", "-nostats", "-i", path,
                            "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    matches = re.findall(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", text, re.M)
    if not matches:
        if rc != 0:
            print(f"响度检测失败（ffmpeg 退出码 {rc}）：{text[-300:]}")
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _lufs_marker(path):
    return path + ".lufs.json"


def _write_lufs_marker(marker, path, measured, target, gain, note):
    try:
        st = os.stat(path)
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"file": os.path.basename(path), "measured_lufs": round(measured, 2),
                       "target_lufs": round(float(target), 2), "gain_db": round(gain, 2),
                       "size": st.st_size, "mtime": round(st.st_mtime, 3),
                       "note": note, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _marker_matches(marker, path):
    """标记是否对应「当前这个文件」。

    避免留下的旧标记让新下载的文件被误判成「已经均衡过」（文件被单独删掉 / 换掉时）。
    """
    try:
        with open(marker, "r", encoding="utf-8") as f:
            data = json.load(f)
        st = os.stat(path)
        return int(data.get("size", -1)) == st.st_size and abs(float(data.get("mtime", -1)) - st.st_mtime) < 1.5
    except Exception:
        return False


def normalize_loudness_once(path, target_lufs=DEFAULT_TARGET_LUFS, tolerance=0.5):
    """把音频整体响度对齐到目标 LUFS —— **只做一次**，结果直接写回文件。

    流程（「下载后算一次均值」的初始化式处理，不是播放时的动态调整）：
      1) ebur128 量出整首整合响度；
      2) 与目标求差得到固定增益（限幅 ±24dB）；
      3) 整首套用该增益并覆盖原文件；
      4) 在文件旁写 .lufs.json 标记 —— 以后不再重复处理；音量滑杆照常叠加在上面。
    返回 (是否处理过, 说明文字)。
    """
    if not path or not os.path.isfile(path):
        return False, "文件不存在"
    marker = _lufs_marker(path)
    if os.path.isfile(marker) and _marker_matches(marker, path):
        return False, "已均衡过（跳过）"
    exe = _ffmpeg_location()
    if not exe:
        return False, "未找到 ffmpeg"
    try:
        target = float(target_lufs)
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_LUFS

    measured = measure_loudness(path, exe)
    if measured is None:
        return False, "响度检测失败"
    gain = target - measured
    if abs(gain) <= max(0.0, float(tolerance)):
        _write_lufs_marker(marker, path, measured, target, 0.0, "已达标")
        return False, f"响度已达标（{measured:.1f} LUFS）"
    gain = max(-_MAX_GAIN_DB, min(_MAX_GAIN_DB, gain))

    tmp = path + ".norm.tmp.wav"
    rc, text = _silent_run([exe, "-y", "-hide_banner", "-nostats", "-i", path,
                            "-af", f"volume={gain:.2f}dB", tmp])
    if rc != 0 or not os.path.isfile(tmp):
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, f"均衡失败（ffmpeg 退出码 {rc}）：{text[-200:]}"
    try:
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"写入失败：{e}"
    _write_lufs_marker(marker, path, measured, target, gain, "已均衡")
    return True, f"{measured:.1f} → {target:.0f} LUFS（增益 {gain:+.1f}dB）"


def loudness_options():
    """读取插件里的音量均衡设置：返回 (是否开启, 目标 LUFS)。

    插件停用 / 不存在时视为关闭 —— 选项属于该插件，插件关掉就不该再改下载下来的文件。
    """
    try:
        from core import plugin_manager as pm
        if not pm.manager.is_enabled(NORMALIZE_PLUGIN):
            return False, None
        st = pm.manager.get_settings(NORMALIZE_PLUGIN) or {}
    except Exception:
        return False, None
    if not st.get("normalize"):
        return False, None
    try:
        target = float(st.get("target_lufs", DEFAULT_TARGET_LUFS))
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_LUFS
    return True, target


def normalize_if_enabled(path):
    """按插件设置做一次响度均衡（关闭 / 不可用时什么都不做）。

    任何异常都不影响播放：均衡失败只是回到未处理状态（文件保持原样）。
    """
    try:
        enabled, target = loudness_options()
        if not enabled:
            return None
        ok, detail = normalize_loudness_once(path, target)
        if config.DEBUG_MODE:
            print(f"* (音乐音量均衡) {os.path.basename(path)}: {detail}")
        return {"applied": ok, "detail": detail}
    except Exception as e:
        print(f"音乐音量均衡异常（已忽略）：{e}")
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
