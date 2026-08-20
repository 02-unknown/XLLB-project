# launcher.py
# 一键启动：依次拉起 Ollama、GPT-SoVITS API，预加载 Whisper，再启动 Web 界面。
# 各服务地址 / 脚本路径请在 launcher_config.json 中配置。
import sys
import threading
import webbrowser

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import core.config as config
from core import services, models
from web.server import run


def main():
    print("=" * 40)
    print("小笼洛包 1.0 · 一键启动")
    print("=" * 40)

    # 1. 读取配置并启动外部服务（Ollama、GPT-SoVITS）
    cfg = services.load_launcher_config()
    status = services.start_all()

    web = status.get("web") or cfg.get("web") or {}
    config.WEB_HOST = web.get("host", config.WEB_HOST)
    config.WEB_PORT = int(web.get("port", config.WEB_PORT))

    # 2. 后台预加载 Whisper（失败不影响运行）
    threading.Thread(target=models.init_models, daemon=True).start()

    # 3. 就绪后自动打开浏览器
    if web.get("auto_open_browser", True):
        url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # 4. 启动 Web 界面
    run(config.WEB_HOST, config.WEB_PORT)


if __name__ == "__main__":
    main()
