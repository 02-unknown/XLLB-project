#!/usr/bin/env python3
# setup/install.py —— 小笼洛包 1.0 一键安装部署程序。
# 功能：
#   1) 检查并安装 Python 依赖（优先使用项目 venv）；
#   2) 创建/检查运行时目录；
#   3) 可选安装 Whisper 语音识别模型（不装则提示语音识别不可用）；
#   4) 检查 GPT-SoVITS 语音合成（外部整合包，无法自动安装，仅检查路径）；
#   5) 检查 ffmpeg（点歌转码需要）。
# 本程序只新建/安装缺失内容，不会修改项目已有的代码文件。
import json
import os
import shutil
import subprocess
import sys

# 统一 stdout/stderr 为 UTF-8，避免 Windows 控制台 GBK 编码导致 emoji 打印崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(PROJECT_ROOT, "venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
REQ_FILE = os.path.join(PROJECT_ROOT, "requirements.txt")
WHISPER_DIR = os.path.join(PROJECT_ROOT, "models", "whisper")
RUNTIME_DIRS = [os.path.join(PROJECT_ROOT, "runtime", "tts"),
                os.path.join(PROJECT_ROOT, "runtime", "music")]
LAUNCHER_CFG = os.path.join(PROJECT_ROOT, "launcher_config.json")
FFMPEG_DEFAULT = r"D:\ffmpeg-8.0.1-essentials_build\bin\ffmpeg.exe"

# 项目必需文件（缺失则视为项目不完整）
ESSENTIAL = ["app.py", "launcher.py", "requirements.txt",
             "core", "web", "plugins", "presets", "voice_presets"]


def ask(question, default="y"):
    """交互提问，返回 bool。"""
    hint = "Y/n" if default == "y" else "y/N"
    while True:
        ans = input(f"{question} [{hint}]: ").strip().lower()
        if not ans:
            ans = default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def section(title):
    print("\n" + "=" * 52)
    print(f"  {title}")
    print("=" * 52)


def find_python():
    """优先使用项目 venv 的 python，否则找系统 python。"""
    if os.path.exists(VENV_PY):
        return VENV_PY
    for cmd in ("python", "py"):
        p = shutil.which(cmd)
        if p:
            return p
    return None


def run(cmd):
    print(">>>", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)


def ensure_venv(system_python):
    if os.path.exists(VENV_PY):
        print("✅ 已存在虚拟环境 venv。")
        return VENV_PY
    print("创建虚拟环境 venv ...")
    run([system_python, "-m", "venv", VENV_DIR])
    return VENV_PY


def check_essential():
    missing = [p for p in ESSENTIAL if not os.path.exists(os.path.join(PROJECT_ROOT, p))]
    if missing:
        print("⚠️ 项目文件缺失：", ", ".join(missing))
        print("   请确认 setup 文件夹位于项目根目录下，且项目文件完整。")
        return False
    print("✅ 项目文件完整。")
    return True


def install_deps(python):
    if not os.path.exists(REQ_FILE):
        print("⚠️ 未找到 requirements.txt，跳过依赖安装。")
        return
    section("1. 安装 Python 依赖")
    print(f"使用解释器：{python}")
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "-r", REQ_FILE])
    print("✅ 依赖安装完成。")


def ensure_runtime():
    section("2. 运行时目录")
    for d in RUNTIME_DIRS:
        os.makedirs(d, exist_ok=True)
        print("✅", d)
    print("✅ 运行时目录就绪。")


def check_whisper():
    section("3. 语音识别（Whisper）")
    model_bin = os.path.join(WHISPER_DIR, "model.bin")
    if os.path.exists(model_bin):
        print("✅ Whisper 模型已存在，语音识别可用。")
        return True

    print("未检测到 Whisper 模型（models/whisper/model.bin 缺失）。")
    print("是否现在下载 faster-whisper-small 模型（约 500MB，需联网）？")
    if not ask("下载 Whisper 语音识别模型", "y"):
        print("⚠️ 已跳过语音识别安装：语音识别功能将【不可用】（文字对话不受影响）。")
        return False
    try:
        code = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download('Systran/faster-whisper-small', local_dir={WHISPER_DIR!r})"
        )
        run([sys.executable, "-c", code])
        if os.path.exists(model_bin):
            print("✅ Whisper 模型下载完成，语音识别可用。")
            return True
        print("⚠️ Whisper 模型下载后文件不完整，语音识别可能不可用。")
        return False
    except Exception as e:
        print(f"❌ Whisper 模型下载失败：{e}")
        print("⚠️ 语音识别将【不可用】（可在之后手动下载模型到 models/whisper）。")
        return False


def check_tts():
    section("4. 语音合成（GPT-SoVITS）")
    # 读取 launcher_config.json 中的 api_script 路径（相对路径基于项目根目录解析）
    api_script = ""
    if os.path.exists(LAUNCHER_CFG):
        try:
            with open(LAUNCHER_CFG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api_script = (cfg.get("gpt_sovits", {}) or {}).get("api_script", "") or ""
            if api_script and not os.path.isabs(api_script):
                api_script = os.path.join(PROJECT_ROOT, api_script)
            api_script = os.path.normpath(api_script)
        except Exception as e:
            print(f"⚠️ 读取 launcher_config.json 失败：{e}")

    if api_script and os.path.exists(api_script):
        print("✅ GPT-SoVITS API 脚本已找到：")
        print("   ", api_script)
        print("   语音合成可用（需先启动该 API）。")
        return True

    print("未检测到 GPT-SoVITS 整合包（api_v2.py）。")
    print("注意：GPT-SoVITS 为外部整合包，本安装程序【无法自动下载】。")
    if not ask("是否继续（跳过语音合成检查）", "n"):
        print("⚠️ 已跳过语音合成：语音合成功能将【不可用】。")
        return False

    # 用户选择了检查但路径不存在：询问是否手动提供路径（不改文件，仅提示）
    print("请在 launcher_config.json 的 gpt_sovits.api_script 中配置正确的路径后重新运行本程序。")
    print("⚠️ 语音合成将【不可用】，直到配置正确并启动 GPT-SoVITS API。")
    return False


def check_ffmpeg():
    section("5. ffmpeg（点歌转码）")
    ok = bool(shutil.which("ffmpeg")) or os.path.exists(FFMPEG_DEFAULT)
    if ok:
        print("✅ ffmpeg 已就绪，点歌功能可用。")
    else:
        print("⚠️ 未检测到 ffmpeg（PATH 或默认路径）。")
        print("   点歌下载转码将【不可用】；其余功能不受影响。")
        print(f"   安装后请更新 core/config.py 的 FFMPEG_PATH（当前默认：{FFMPEG_DEFAULT}）。")
    return ok


# ==================== Ollama（必需项） ====================
def find_ollama():
    """查找 ollama 可执行文件。"""
    p = shutil.which("ollama")
    if p:
        return p
    local = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    if os.path.exists(local):
        return local
    return None


def install_ollama():
    """安装 Ollama（必需项）；用户拒绝则退出安装。返回 ollama 路径或 None。"""
    section("0. Ollama（本地大模型，必需）")
    existing = find_ollama()
    if existing:
        print(f"✅ 已检测到 Ollama：{existing}")
        return existing

    print("未检测到 Ollama。Ollama 是运行本地大模型的必需组件。")
    if not ask("是否自动安装 Ollama？（拒绝将退出安装）", "y"):
        print("❌ 已拒绝安装 Ollama，安装程序退出。")
        sys.exit(1)

    # 方式一：winget
    try:
        subprocess.check_call([
            "winget", "install", "--id", "Ollama.Ollama", "-e",
            "--silent", "--accept-package-agreements", "--accept-source-agreements",
        ], timeout=900)
        if find_ollama():
            print("✅ Ollama 安装成功。")
            return find_ollama()
    except Exception as e:
        print(f"winget 安装失败，尝试下载安装包：{e}")

    # 方式二：下载官方安装包并静默安装
    try:
        import urllib.request
        exe = os.path.join(os.environ.get("TEMP", "."), "OllamaSetup.exe")
        print("正在下载 Ollama 安装包（约 1GB，需联网）...")
        urllib.request.urlretrieve("https://ollama.com/download/OllamaSetup.exe", exe)
        subprocess.check_call([exe, "/VERYSILENT", "/NORESTART"], timeout=900)
        if find_ollama():
            print("✅ Ollama 安装成功。")
            return find_ollama()
    except Exception as e:
        print(f"❌ Ollama 自动安装失败：{e}")

    print("❌ Ollama 安装未完成。请手动安装：https://ollama.com/download 后重新运行本程序。")
    sys.exit(1)


def pull_ollama_model(ollama):
    """可选：拉取默认模型。"""
    if ask("是否拉取默认模型 qwen3.5:9b（约 5GB，需联网）？", "y"):
        try:
            subprocess.check_call([ollama, "pull", "qwen3.5:9b"], timeout=3600)
            print("✅ 默认模型拉取完成。")
            return True
        except Exception as e:
            print(f"⚠️ 模型拉取失败：{e}（可稍后手动执行 ollama pull qwen3.5:9b）")
            return False
    print("⚠️ 已跳过模型拉取：请在启动前执行 ollama pull qwen3.5:9b。")
    return False


def main():
    print("=" * 52)
    print("  小笼洛包 1.0 · 一键安装部署")
    print("=" * 52)
    print(f"项目根目录：{PROJECT_ROOT}")

    python = find_python()
    if not python:
        print("❌ 未检测到 Python。请先安装 Python 3.9+ 并加入 PATH，再运行本程序。")
        sys.exit(1)

    # 检查版本
    ver = subprocess.check_output([python, "--version"], text=True).strip()
    print(f"使用 Python：{ver}")

    if not check_essential():
        sys.exit(1)

    # 0. Ollama（必需）：未安装则自动安装，拒绝则退出
    ollama = install_ollama()
    pull_ollama_model(ollama)

    # 1. 创建/使用 venv 并安装依赖
    python = ensure_venv(python)
    install_deps(python)

    # 2. 运行时目录
    ensure_runtime()

    # 3. 语音识别（可选）
    check_whisper()

    # 4. 语音合成（可选，外部整合包）
    check_tts()

    # 5. ffmpeg
    check_ffmpeg()

    print("\n" + "=" * 52)
    print("  安装部署完成")
    print("=" * 52)
    print("下一步：")
    print("  1) 编辑 launcher_config.json 确认 GPT-SoVITS 路径（如需语音合成）；")
    print("  2) 双击项目根目录的 start.bat（或运行 venv\\Scripts\\python.exe launcher.py）启动。")
    print("未安装语音识别/语音合成时，相关语音功能不可用，但文字对话与联网等功能正常。")


if __name__ == "__main__":
    main()
