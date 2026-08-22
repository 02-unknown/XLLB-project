# launcher.py
# 小笼洛包 1.3 启动器（合并网页版与桌面版）。
# 启动时依次选择：
#   1. 界面方式：桌面版（内嵌窗口，需 pywebview）/ 网页版（浏览器）
#   2. 运行模式：Lite（仅加载语音合成，大模型只能用外部 API）/
#                标准（启动全部服务）
# 各服务地址 / 脚本路径请在 launcher_config.json 中配置。
#
# 启动前会做关键组件预检（仅检查，不安装、不修改任何文件）：
#   关键文件 / 依赖缺失时，提示运行 setup\install.bat 并终止启动，
#   避免在组件不完整的情况下运行导致异常。
import importlib
import os
import sys
import threading

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


def _log_dir():
    return os.path.join(PROJECT_ROOT, "runtime", "logs")


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
        try:
            from core import logger
            logger.init_log(_log_dir())
            logger.error("启动预检失败，关键组件缺失：" + "、".join(missing))
        except Exception:
            pass
        sys.exit(1)


def _ask_choice(title, options, default):
    """通用选项询问，返回选项序号字符串。"""
    print(title)
    for key, text in options:
        print(f"  {key}. {text}")
    while True:
        ans = input(f"请输入选项序号（直接回车默认 {default}）：").strip()
        if ans in [k for k, _ in options]:
            return ans
        if not ans:
            return default
        print("请输入有效的选项序号。")


def _choose_ui():
    """选择界面方式：桌面版（内嵌窗口）或 网页版（浏览器）。"""
    key = _ask_choice(
        "请选择界面方式：",
        [("1", "桌面版：内嵌窗口显示界面（推荐，无浏览器依赖）"),
         ("2", "网页版：使用浏览器打开界面")],
        "2")
    return "desktop" if key == "1" else "web"


def _choose_mode():
    """选择运行模式：Lite（仅语音合成 + 外部 API）或 标准（全部服务）。"""
    key = _ask_choice(
        "请选择运行模式：",
        [("1", "Lite 模式：仅加载语音合成（GPT-SoVITS），大模型只能使用外部 API"
               "（不启动 Ollama、不加载 Whisper）"),
         ("2", "标准模式：启动全部服务（Ollama + GPT-SoVITS + Whisper）")],
        "2")
    return "lite" if key == "1" else "standard"


def _start_services(mode, services, models, cfg):
    """按模式启动外部服务。"""
    if mode == "lite":
        import core.config as config
        print("已选择 Lite 模式：仅加载语音合成（GPT-SoVITS）。")
        print("提示：Lite 模式不启动 Ollama、不加载 Whisper，")
        print("      大模型只能使用外部 API（OpenAI 兼容接口）。")
        print("      请在 WebUI「设置 → 插件管理 → 模型与自动调优」确认 API 地址与 Key。")
        # 强制大模型走外部 API（插件加载后可能按设置覆盖，故在此再次强制）；
        # 清空生成模型名，避免界面显示本机 Ollama 模型名造成误导（配置外部模型后自动显示）
        config.APP_MODE = "lite"
        config.LLM_CHAT_BACKEND = "openai"
        config.LLM_JUDGE_BACKEND = "openai"
        config.LLM_CHAT_MODEL = ""
        started_gs = services.start_gpt_sovits(cfg)
        if started_gs:
            threading.Thread(
                target=lambda: services.wait_ready(
                    lambda: services.check_gpt_sovits(cfg),
                    cfg["gpt_sovits"].get("start_timeout", 60),
                    label="GPT-SoVITS API"),
                daemon=True).start()
    else:
        import core.config as config
        config.APP_MODE = "standard"
        print("已选择标准模式：启动全部服务（Ollama + GPT-SoVITS + Whisper）。")
        services.start_all()
        threading.Thread(target=models.init_models, daemon=True).start()


def _open_app_window(url):
    """用 Edge / Chrome 的应用模式打开独立窗口（无浏览器工具栏、无标签页）。"""
    import os
    import subprocess
    candidates = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    ]
    for template in candidates:
        exe = os.path.expandvars(template)
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, "--app=" + url])
                return True
            except Exception:
                continue
    return False


def _run_desktop(serve, config):
    """桌面版：优先内嵌 pywebview 窗口；失败则用 Edge/Chrome 应用模式
    独立窗口（Win10/11 自带，无需额外安装）；再失败回退普通浏览器。"""
    import time
    # Web 服务放入后台线程；就绪后通过回调拿到实际地址（可能是随机兜底端口）
    holder = {}

    def _on_ready(url):
        holder["url"] = url

    threading.Thread(
        target=lambda: serve(config.WEB_HOST, config.WEB_PORT,
                             open_browser=False, on_ready=_on_ready),
        daemon=True).start()
    url = None
    for _ in range(60):  # 最长约 30 秒
        if "url" in holder:
            url = holder["url"]
            break
        time.sleep(0.5)
    if url is None:
        url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"

    # 1) 内嵌 pywebview 窗口（可选组件；未安装或启动失败自动降级）
    try:
        import webview
    except Exception:
        webview = None
    if webview is not None:
        print(f"正在打开桌面窗口：{url}")
        try:
            webview.create_window("小笼洛包 1.3 桌面版", url,
                                  width=1280, height=820, resizable=True)
            webview.start()
            return
        except Exception as e:
            print(f"内嵌窗口启动失败（{e}），改用应用模式窗口。")
            try:
                from core import logger
                logger.warn(f"内嵌窗口启动失败：{e}，改用 Edge/Chrome 应用模式窗口")
            except Exception:
                pass
    else:
        print("[提示] 未安装桌面版内嵌窗口组件（pywebview），内嵌窗口不可用。")
        print("       桌面版将使用 Edge/Chrome 应用模式窗口打开（功能不受影响）。")
        try:
            from core import logger
            logger.warn("未安装 pywebview，内嵌窗口不可用，改用 Edge/Chrome 应用模式窗口")
        except Exception:
            pass

    # 2) Edge / Chrome 应用模式独立窗口
    print(f"正在打开桌面窗口（应用模式）：{url}")
    if _open_app_window(url):
        # 保持进程存活（Web 服务在后台线程运行）；Ctrl+C 退出
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n正在关闭服务...")
        return

    # 3) 最终回退：普通浏览器
    print("未找到可用的应用模式浏览器，改用普通浏览器打开界面。")
    serve(config.WEB_HOST, config.WEB_PORT, open_browser=True)


def main():
    import core.config as config
    from core import services, models, logger
    from web.server import serve

    logger.init_log(_log_dir())
    logger.info("=== 小笼洛包启动 ===")

    _preflight()

    print("=" * 50)
    print("小笼洛包 1.3")
    print("=" * 50)
    ui = _choose_ui()
    mode = _choose_mode()
    print(f"已选择：{'桌面版' if ui == 'desktop' else '网页版'} / "
          f"{'Lite 模式' if mode == 'lite' else '标准模式'}")
    print()
    logger.info(f"界面方式：{'桌面版' if ui == 'desktop' else '网页版'}；"
                f"运行模式：{'Lite' if mode == 'lite' else '标准'}")

    cfg = services.load_launcher_config()
    services.apply_api_urls(cfg)
    web = cfg.get("web") or {}
    config.WEB_HOST = web.get("host", config.WEB_HOST)
    config.WEB_PORT = int(web.get("port", config.WEB_PORT))
    logger.info(f"Web 配置：{config.WEB_HOST}:{config.WEB_PORT}")

    _start_services(mode, services, models, cfg)

    if ui == "desktop":
        _run_desktop(serve, config)
    else:
        print("已选择网页版：使用浏览器打开界面。")
        serve(config.WEB_HOST, config.WEB_PORT, open_browser=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            from core import logger
            logger.init_log(_log_dir())
            logger.error("主程序异常退出：\n" + traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)
