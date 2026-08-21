#!/usr/bin/env python3
# setup/install.py —— 小笼洛包 1.0 安装部署程序（可反复运行，用于补装缺失组件）。
# 功能：
#   0) 本地大模型 Ollama（可选：不装则只能通过外部 API 调用大模型）；
#   1) 检查并安装 Python 依赖（优先使用项目 venv）；
#   2) 创建/检查运行时目录；
#   3) 可选安装 Whisper 语音识别模型（不装则语音识别不可用）；
#   4) 检查 GPT-SoVITS 语音合成（外部整合包，仅检查路径）；
#   5) 检查 ffmpeg（点歌转码需要）。
# 说明：
#   - 每个组件都会先检测是否已安装，已安装则跳过；
#   - 未安装的组件会逐个询问是否安装（手动确认）；
#   - 全部已安装时输出“所有组件均已安装完成”提示（重复打开同样会显示该提示，不会闪退）。
# 本程序只新建/安装缺失内容，不会修改项目已有的代码文件。
# bilibili@我叫清少（UID:478929333） 重写
import json
import os
import shutil
import subprocess
import sys

# 统一 stdout/stderr 为 UTF-8，避免 Windows 控制台 GBK 编码导致中文乱码。
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
ESSENTIAL = ["app.py", "launcher.py", "requirements.txt", "core", "web", "plugins"]
# 以下为可缺失项：缺失仅提示（程序会自动创建 / 由用户自行提供）
WARN_OPTIONAL = ["presets", "voice_presets"]


def ask(question, default="n"):
    """交互提问，返回 bool。默认按传入的 default（y/n）。"""
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


def _is_real_python(python):
    """校验解释器确实可执行（排除微软商店占位符等假 Python）。"""
    try:
        out = subprocess.check_output([python, "--version"], stderr=subprocess.STDOUT,
                                      timeout=10, text=True)
        return "python" in out.lower()
    except Exception:
        return False


def find_python():
    """优先使用项目 venv 的 python，否则找系统 python（校验真实可用）。

    若系统 Python 缺失或为微软商店占位符，则回退使用 gpt_sovits 自带的 Python 完成安装。
    """
    if os.path.exists(VENV_PY) and _is_real_python(VENV_PY):
        return VENV_PY
    for cmd in ("python", "py"):
        p = shutil.which(cmd)
        if p and _is_real_python(p):
            return p
    # 兜底：gpt_sovits 自带的 Python（若存在且真实可用）
    bundled = os.path.join(PROJECT_ROOT, "gpt_sovits", "runtime", "python.exe")
    if os.path.exists(bundled) and _is_real_python(bundled):
        print("[警告] 未检测到系统 Python，将使用 gpt_sovits 自带的 Python 完成安装。")
        return bundled
    return None


def run(cmd):
    print(">>>", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)


def ensure_venv(system_python):
    if os.path.exists(VENV_PY):
        print("  [OK] 已存在虚拟环境 venv。")
        return VENV_PY
    print("  创建虚拟环境 venv ...")
    run([system_python, "-m", "venv", VENV_DIR])
    return VENV_PY


def find_ollama():
    """检测 Ollama 是否已安装。"""
    if shutil.which("ollama"):
        return True
    # 常见安装路径兜底
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
              r"C:\Program Files\Ollama\ollama.exe"):
        if os.path.exists(p):
            return True
    return False


def check_essential():
    missing = [p for p in ESSENTIAL if not os.path.exists(os.path.join(PROJECT_ROOT, p))]
    if missing:
        print("[错误] 项目文件缺失：", ", ".join(missing))
        print("       请确认 setup 文件夹位于项目根目录下，且项目文件完整。")
        return False
    for p in WARN_OPTIONAL:
        if not os.path.exists(os.path.join(PROJECT_ROOT, p)):
            print(f"[警告] 未找到 {p}/ 目录（程序会自动创建；语音相关功能可能受限）。")
    print("[OK] 项目文件完整。")
    return True


def install_deps(python):
    if not os.path.exists(REQ_FILE):
        print("[警告] 未找到 requirements.txt，跳过依赖安装。")
        return False
    print("  安装 Python 依赖 ...")
    print(f"  使用解释器：{python}")
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "-r", REQ_FILE])
    print("  [OK] 依赖安装完成。")
    return True


def ensure_runtime():
    for d in RUNTIME_DIRS:
        os.makedirs(d, exist_ok=True)
        print("  [OK]", d)
    print("  [OK] 运行时目录就绪。")


def check_whisper_installed():
    return os.path.exists(os.path.join(WHISPER_DIR, "model.bin"))


def install_whisper(python):
    print("  正在下载 faster-whisper-small 模型（约 500MB，需联网）...")
    try:
        code = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download('Systran/faster-whisper-small', local_dir={WHISPER_DIR!r})"
        )
        run([python, "-c", code])
        if check_whisper_installed():
            print("  [OK] Whisper 模型下载完成，语音识别可用。")
            return True
        print("[警告] Whisper 模型下载后文件不完整，语音识别可能不可用。")
        return False
    except Exception as e:
        print(f"[错误] Whisper 模型下载失败：{e}")
        print("[警告] 语音识别将不可用（可稍后重新运行本程序，或手动下载模型到 models/whisper）。")
        return False


def check_tts_installed():
    """返回 (是否找到 api_script, api_script 绝对路径)。"""
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
            print(f"[警告] 读取 launcher_config.json 失败：{e}")
    return os.path.exists(api_script), api_script


def check_ffmpeg():
    return bool(shutil.which("ffmpeg")) or os.path.exists(FFMPEG_DEFAULT)


# ==================== 各模块（均先检测已安装，再手动确认） ====================
def module_ollama():
    section("0. 本地大模型（Ollama，可选）")
    if find_ollama():
        print("  [OK] 已安装 Ollama，跳过。")
        return True
    print("  [状态] 未安装 Ollama。")
    print("  说明：Ollama 用于在本地运行大模型。")
    print("        若不安装 Ollama，将只能通过外部 API（OpenAI 兼容接口）调用大模型。")
    if not ask("  是否安装 Ollama？", "n"):
        print("  已跳过：本机将只能通过外部 API 调用大模型。")
        return False
    # 方式一：winget
    try:
        subprocess.check_call([
            "winget", "install", "--id", "Ollama.Ollama", "-e",
            "--silent", "--accept-package-agreements", "--accept-source-agreements",
        ], timeout=900)
        if find_ollama():
            print("  [OK] Ollama 安装成功。")
            return True
    except Exception as e:
        print(f"[警告] winget 安装失败，尝试下载安装包：{e}")
    # 方式二：下载官方安装包并静默安装
    try:
        import urllib.request
        exe = os.path.join(os.environ.get("TEMP", "."), "OllamaSetup.exe")
        print("  正在下载 Ollama 安装包（约 1GB，需联网）...")
        urllib.request.urlretrieve("https://ollama.com/download/OllamaSetup.exe", exe)
        subprocess.check_call([exe, "/VERYSILENT", "/NORESTART"], timeout=900)
        if find_ollama():
            print("  [OK] Ollama 安装成功。")
            return True
    except Exception as e:
        print(f"[错误] Ollama 自动安装失败：{e}")

    print("[警告] Ollama 安装未完成。请手动安装：https://ollama.com/download 后重新运行本程序。")
    print("       或使用外部 API（OpenAI 兼容接口）调用大模型。")
    return False


def module_python_deps(python):
    section("1. Python 依赖")
    if python and os.path.exists(VENV_PY):
        try:
            subprocess.check_output(
                [VENV_PY, "-c", "import requests, numpy, yt_dlp"], timeout=30)
            print("  [OK] 虚拟环境与依赖已就绪，跳过。")
            return True
        except Exception:
            pass
    if not python:
        print("[错误] 未检测到可用的 Python，无法安装依赖。")
        print("       请安装 Python 3.9+ 并加入 PATH，或确认 gpt_sovits 运行时完整后再试。")
        return False
    print("  [状态] 虚拟环境或依赖不完整。")
    if not ask("  是否创建虚拟环境并安装依赖？", "y"):
        print("  已跳过：程序将无法运行。")
        return False
    python = ensure_venv(python)
    return install_deps(python)


def module_whisper(python):
    section("2. 语音识别（Whisper，可选）")
    if check_whisper_installed():
        print("  [OK] Whisper 模型已存在，语音识别可用。")
        return True
    print("  [状态] 未检测到 Whisper 模型（models/whisper/model.bin 缺失）。")
    print("  说明：语音识别用于“语音输入”功能；不安装则语音输入不可用（文字对话不受影响）。")
    if not python:
        print("[错误] 未检测到可用的 Python，无法下载模型。")
        return False
    if not ask("  是否下载 Whisper 语音识别模型（约 500MB）？", "n"):
        print("  已跳过：语音识别功能不可用（可稍后重新运行本程序补装）。")
        return False
    return install_whisper(python)


def module_tts():
    section("3. 语音合成（GPT-SoVITS，外部整合包）")
    found, api_script = check_tts_installed()
    if found:
        print("  [OK] GPT-SoVITS API 脚本已找到：")
        print("       ", api_script)
        gs_dir = os.path.dirname(api_script)
        missing_tools = [m for m in ("audio_sr", "AP_BWE_main", "i18n")
                         if not os.path.exists(os.path.join(gs_dir, "tools", m))]
        if missing_tools:
            print("[警告] GPT-SoVITS 的 tools 目录不完整，缺少：", ", ".join(missing_tools))
            print("       请补齐 tools/audio_sr 等模块（参考官方 GPT-SoVITS 仓库），否则 api_v2.py 可能无法启动。")
            return False
        print("       语音合成可用（需先启动该 API）。")
        return True
    print("  [状态] 未检测到 GPT-SoVITS 整合包（api_v2.py）。")
    print("  说明：语音合成（GPT-SoVITS）为外部整合包，本安装程序无法自动下载，需要你手动准备。")
    if not ask("  是否跳过语音合成安装？", "y"):
        print("  已选择处理语音合成：请手动准备 GPT-SoVITS 整合包，")
        print("  并在 launcher_config.json 的 gpt_sovits.api_script 中配置正确的 api_v2.py 路径。")
    else:
        print("  已跳过：语音合成功能暂不可用。")
    print("  语音合成将不可用，直到配置正确并启动 GPT-SoVITS API。")
    return False


def module_ffmpeg():
    section("4. ffmpeg（点歌转码，可选）")
    if check_ffmpeg():
        print("  [OK] ffmpeg 已就绪，点歌功能可用。")
        return True
    print("  [状态] 未检测到 ffmpeg（PATH 或默认路径）。")
    print("  说明：ffmpeg 仅用于点歌下载转码；缺失不影响其余功能。")
    print("        安装 ffmpeg 后可重新运行本程序确认（无需其它操作）。")
    return False


def main():
    print("=" * 52)
    print("  小笼洛包 1.0 · 安装部署")
    print("=" * 52)
    print(f"项目根目录：{PROJECT_ROOT}")

    if not check_essential():
        sys.exit(1)

    python = find_python()
    if python:
        try:
            ver = subprocess.check_output([python, "--version"], stderr=subprocess.STDOUT,
                                          timeout=10, text=True).strip()
            print(f"使用 Python：{ver}")
        except Exception as e:
            print(f"[警告] Python 版本校验失败：{e}")

    # 各模块：先检测已安装，未安装则手动确认
    results = {}
    results["Ollama"] = module_ollama()
    results["Python依赖"] = module_python_deps(python)
    results["语音识别(Whisper)"] = module_whisper(python)
    results["语音合成(GPT-SoVITS)"] = module_tts()
    results["ffmpeg"] = module_ffmpeg()

    section("运行时目录")
    ensure_runtime()

    print()
    print("=" * 52)
    missing = [name for name, ok in results.items() if not ok]
    if not missing:
        print("  所有组件均已安装完成。")
    else:
        print("  安装流程结束。尚未安装：", "、".join(missing))
        print("  说明：可稍后重新运行本程序进行补装；缺失项对应的功能将不可用。")
    print("=" * 52)
    print("下一步：双击项目根目录的 start.bat（或运行 venv\\Scripts\\python.exe launcher.py）启动。")


if __name__ == "__main__":
    main()
