# launcher.py
# 一键启动：拉起 Ollama（可选）、GPT-SoVITS API（可选），预加载 Whisper，
# 再启动 Web 界面。
# 各服务地址 / 脚本路径请在 launcher_config.json 中配置。
#
# 启动前会做关键组件预检（仅检查，不安装、不修改任何文件）：
#   关键文件 / 依赖缺失时，提示运行 setup\install.bat 并终止启动，
#   避免在组件不完整的情况下运行导致异常。
import importlib
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 关键文件 / 目录（缺失则无法运行）
CRITICAL_PATHS = [
    "core",
    "plugins",
    "web",
    os.path.join("web", "index.html"),
    os.path.join("web", "static"),
    "requirements.txt",
]
# 关键 Python 依赖（缺失则无法运行）
CRITICAL_DEPENDENCIES = ["requests", "numpy", "yt_dlp"]


def _preflight():
    """关键组件预检：缺失时提示运行安装程序并终止，绝不自动安装。"""
    missing = []
    for rel in CRITICAL_PATHS:
        if not os.path.exists(os.path.join(PROJECT_ROOT, rel)):
            missing.append(rel)
    for dep in CRITICAL_DEPENDENCIES:
        try:
            importlib.import_module(dep)
        except Exception:
            missing.append(f"依赖模块 {dep}")
    if missing:
        print("检测到关键组件缺失：")
        for item in missing:
            print(f"  - {item}")
        print("请先运行 setup\\install.bat 完成安装，然后再启动本程序。")
        print("（为避免损坏文件，本程序不会自动安装或修改任何组件。）")
        sys.exit(1)


def main():
    _preflight()

    import threading

    import core.config as config
    from core import services, models
    from web.server import serve

    print("=" * 40)
    print("小笼洛包 1.1 一键启动")
    print("=" * 40)

    # 1. 读取配置并启动外部服务（Ollama 可选、GPT-SoVITS 可选；不阻塞 Web）
    cfg = services.load_launcher_config()
    status = services.start_all()

    web = status.get("web") or cfg.get("web") or {}
    config.WEB_HOST = web.get("host", config.WEB_HOST)
    config.WEB_PORT = int(web.get("port", config.WEB_PORT))

    # 2. 提示 Ollama 加载状态
    ollama_status = status.get("ollama", "")
    if ollama_status in ("not_installed", "failed", "disabled"):
        print("提示：本次启动未加载 Ollama，将只能通过外部 API（OpenAI 兼容接口）调用大模型。")
        print("      可在 WebUI 的“模型与自动调优”设置中配置外部 API 后使用。")
    elif ollama_status == "started":
        print("Ollama 服务正在启动（后台等待就绪，不阻塞本程序）。")

    # 3. 后台预加载 Whisper（失败不影响运行）
    threading.Thread(target=models.init_models, daemon=True).start()

    # 4. 启动 Web 界面（端口被占用时自动顺延；浏览器在服务就绪后打开）
    serve(config.WEB_HOST, config.WEB_PORT, open_browser=web.get("auto_open_browser", True))


if __name__ == "__main__":
    main()
