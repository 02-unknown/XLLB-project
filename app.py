# app.py
# 小笼洛包 —— 统一入口。
# 启动本地 Web 界面（默认 http://127.0.0.1:10999）。
#
# 用法：
#   python app.py                 # 默认端口
#   python app.py --port 9000     # 自定义端口
import argparse
import sys

# Windows 控制台默认 GBK，无法输出 emoji/部分 Unicode；统一改为 UTF-8，避免打印崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import core.config as config
from core import llm, tts, models, services
from web.server import run


def main():
    parser = argparse.ArgumentParser(description="小笼洛包 Web 界面")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    args = parser.parse_args()

    config.ensure_dirs()

    # 应用 launcher_config.json 中的服务地址（与 launcher.py 保持一致）
    services.apply_api_urls(services.load_launcher_config())

    host = args.host or config.WEB_HOST
    port = args.port or config.WEB_PORT

    print("=" * 40)
    print("小笼洛包 1.1")
    print("=" * 40)

    # 依赖探测（不阻塞，仅提示）
    print("检查 Ollama 服务...")
    if llm.check_ollama():
        print("Ollama 服务已就绪。")
    else:
        print("未检测到 Ollama 服务，请确认已运行 `ollama serve` 并拉取模型。")

    print("检查 GPT-SoVITS API...")
    if tts.check_tts_api():
        print("GPT-SoVITS API 已就绪。")
    else:
        print("GPT-SoVITS API 未就绪，语音合成将不可用（文字对话仍可用）。")

    # 后台预热 Whisper 模型（失败也不影响启动）
    try:
        import threading
        threading.Thread(target=models.init_models, daemon=True).start()
    except Exception:
        pass

    run(host, port)


if __name__ == "__main__":
    main()
