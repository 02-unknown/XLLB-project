#!/usr/bin/env python3
# launcher_gui.py —— 完全图形化的启动程序（不出现命令行窗口）。
#
# 流程：
#   1) 启动本机 Web 服务（此时只显示启动页，不加载任何对话界面数据）；
#   2) 用 Edge / Chrome 的「应用模式」窗口打开启动页（无地址栏 / 标签页）；
#   3) 在启动页里选择 Lite 或 标准 模式 → 按所选模式拉起服务（进度实时显示）；
#   4) 服务拉起后，同一个窗口直接进入正式对话界面。
#
# 启动方式（都不会留下命令行窗口）：
#   双击「启动.vbs」（推荐，完全无窗口）
#   venv\Scripts\pythonw.exe launcher_gui.py
# 需要看完整启动输出时仍可用控制台版：venv\Scripts\python.exe launcher.py
import os
import sys
import threading

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_ROOT, "runtime", "logs")


def _redirect_console_output():
    """pythonw 没有控制台：把 stdout/stderr 落到日志文件，避免 print 报错或信息丢失。

    日志按大小滚动一次（超过 2MB 时把旧文件改名为 .1），避免长期运行无限增长。
    """
    stream = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "launcher_gui.log")
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                backup = path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
        except OSError:
            pass
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        class _Null:
            def write(self, *_args):
                return 0

            def flush(self):
                pass

        stream = _Null()
    for name in ("stdout", "stderr"):
        try:
            setattr(sys, name, stream)
        except Exception:
            pass


def _probe_running(url, timeout=1.5):
    """服务是否已有实例在运行（用于启动页上的提示文案，不再用它跳过模式选择）。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/launch/state", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        st = data.get("state") or {}
        return bool(st.get("done") or st.get("starting"))
    except Exception:
        return False


def _probe_json(url, path, timeout=2.0):
    """读取本机服务的一个接口；失败返回 None。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _running_build(url):
    """运行中的后台构建标识；旧版本（没有该字段）返回空串，连不上返回 None。"""
    data = _probe_json(url, "/api/status")
    if not isinstance(data, dict):
        data = _probe_json(url, "/api/app/lifecycle")
        if data is None:
            return None
        return ""      # 有服务但读不到 build：视为旧版本
    return str(data.get("build") or "")


def _local_build():
    """当前磁盘代码的构建标识（与 web/server.py 的算法一致）。"""
    import hashlib
    parts = []
    for rel in ("core", "web", "plugins"):
        base = os.path.join(PROJECT_ROOT, rel)
        for root, _dirs, names in os.walk(base):
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                    parts.append(f"{os.path.relpath(path, PROJECT_ROOT)}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    continue
    for rel in ("launcher.py", "launcher_gui.py", "version.txt"):
        path = os.path.join(PROJECT_ROOT, rel)
        try:
            st = os.stat(path)
            parts.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            continue
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _port_owner_pid(port):
    """返回监听指定端口的进程号（找不到返回 None）。"""
    import re
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line, re.I)
        if m and int(m.group(1)) == int(port):
            return int(m.group(2))
    return None


def _process_image(pid):
    """取进程可执行文件路径（仅查询，失败返回空串）。"""
    if os.name != "nt":
        return ""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value or ""
        return ""
    except Exception:
        return ""
    finally:
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass


def _is_python_process(pid):
    """该进程是不是 Python（用于日志与最终确认；取不到信息时返回 None 表示未知）。"""
    img = _process_image(pid)
    if img:
        return "python" in os.path.basename(img).lower()
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return None
    low = out.lower()
    if "python" in low:
        return True
    if not out.strip() or "error" in low or "拒绝" in out or "信息" in out:
        return None
    return False


def _kill_pid(pid):
    """结束进程树；返回 (是否成功, 输出信息)。"""
    import subprocess
    try:
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F", "/T"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, str(e)
    msg = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, msg


def _stop_stale_backend(port, logger, trusted=False):
    """结束端口上仍在运行的旧版本后台（磁盘代码已更新，旧进程还在服务）。

    trusted=True 表示调用方已经通过本项目接口确认「端口上就是本程序的后台」
    （例如它应答了 /api/status 或 /api/app/lifecycle）——这是结束它的主要依据；
    进程名检查只作为日志信息，取不到也不影响（部分环境禁止查询其它进程）。
    trusted=False 时不会结束任何进程，避免误杀别人的程序。
    """
    import time
    pid = _port_owner_pid(port)
    if not pid:
        logger.warn("检测到端口上的旧后台，但未能定位进程号，改为自动改用其它端口启动")
        return False
    if not trusted:
        logger.warn(f"端口 {port} 被其它程序占用（PID {pid}），不结束它，将改用其它端口")
        return False
    img = _process_image(pid)
    logger.info(f"准备结束端口 {port} 上的旧后台：PID {pid}"
                + (f"（{os.path.basename(img)}）" if img else ""))
    ok, msg = _kill_pid(pid)
    if not ok:
        logger.error(f"结束旧后台失败（taskkill 返回失败）：{msg[:200]}")
    for _ in range(20):        # 等端口释放（最多约 5 秒）
        if _port_owner_pid(port) is None:
            logger.info(f"已结束旧版本后台（PID {pid}），将启动新版本")
            return True
        time.sleep(0.25)
    logger.warn("旧后台仍占用端口，本次将改用其它端口启动")
    return False


def _open_window(url, size=(1280, 860)):
    """用 Edge / Chrome 的应用模式打开窗口；都没有时退回默认浏览器（仍是图形界面）。

    返回 (是否成功, 浏览器进程对象或 None)：带独立配置目录启动时，这个进程就是我们的窗口，
    它退出＝窗口关闭 —— 后端据此做完整清理（不依赖页面 JS）。
    """
    try:
        from launcher import _open_app_window
        ok, proc = _open_app_window(url, size=size, profile_dir=os.path.join(PROJECT_ROOT, "runtime", "edge-app-profile"))
        if ok:
            return True, proc
    except Exception:
        pass
    try:
        import webbrowser
        return bool(webbrowser.open(url)), None
    except Exception:
        return False, None


# 关窗口检查点的参数
WINDOW_APPEAR_SECONDS = 25.0    # 等窗口出现的最长时间（要等服务就绪后才打得开）
WINDOW_GONE_CHECKS = 2          # 连续几次「看不到窗口」才算关窗（避开页面跳转的瞬时抖动）
WINDOW_POLL_SECONDS = 0.25      # 等窗口出现时的轮询间隔
WINDOW_TITLE_HINT = "小笼洛包"   # core.version.APP_NAME：启动页 / 主界面 / 设置页标题都含它


def _window_title_hint():
    try:
        from core import version
        return version.APP_NAME or WINDOW_TITLE_HINT
    except Exception:
        return WINDOW_TITLE_HINT


def _app_window_count(title_hint=None):
    """当前有几个「应用窗口」（可见顶层窗口，标题含应用名）。

    为什么要按窗口判断，而不是只看启动出来的进程：Edge 在「同一配置目录已经有浏览器进程」
    时会把手交出去 —— 启动出来的进程立刻退出，但窗口其实开着。只按进程判定就会
    **窗口刚打开程序就自动退出**（真实踩过：上次异常退出留下了浏览器进程）。

    返回 -1 表示无法判断（非 Windows / 枚举失败），调用方应退回按进程判定。
    """
    if os.name != "nt":
        return -1
    hint = title_hint or _window_title_hint()
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = []

        def _cb(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd):
                    n = user32.GetWindowTextLengthW(hwnd)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(hwnd, buf, n + 1)
                        if hint in buf.value:
                            found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows.argtypes = [cb_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumWindows(cb_type(_cb), 0)
        return len(found)
    except Exception:
        return -1


def _backend_active_recently(seconds=1.5):
    """后端最近还收到心跳 / 请求吗？（说明页面还活着 → 窗口没被关）"""
    import time
    try:
        from web import app_lifecycle
        st = app_lifecycle.state()
        last = max(float(st.get("last_beat") or 0.0), float(st.get("last_request") or 0.0))
        return last > 0 and (time.time() - last) <= float(seconds)
    except Exception:
        return False


def _notify_window_closed(logger, message):
    """记一条日志并触发完整清理退出（已开始退出则不重复触发）。"""
    try:
        from web import app_lifecycle
        if app_lifecycle.state().get("shutdown_called"):
            return
        logger.info(message)
        app_lifecycle.shutdown_now("window-process-exit")
    except Exception as e:
        logger.error(f"窗口关闭处理失败：{e}")


def _watch_window_process(proc, logger):
    """把关掉图形窗口当作检查点：窗口一关就完整清理并退出程序。

    两条路都用上，先准后稳：
      1) 能枚举到窗口 → 只按「窗口还在不在」判定（进程怎么变都不影响），最准；
      2) 枚举不到（非 Windows / 老环境）→ 按「启动进程退出」判定，
         但先排除「进程把手交给已有浏览器进程」的假关窗（此时页面还在心跳）。
    """
    import time

    def _shutting_down():
        try:
            from web import app_lifecycle
            return bool(app_lifecycle.state().get("shutdown_called"))
        except Exception:
            return False

    # ---------- 第一阶段：确认窗口真的出现过 ----------
    seen = False
    unusable = False
    deadline = time.time() + WINDOW_APPEAR_SECONDS
    while time.time() < deadline:
        if _shutting_down():
            return
        n = _app_window_count()
        if n > 0:
            seen = True
            break
        if n < 0:
            unusable = True
            break
        time.sleep(WINDOW_POLL_SECONDS)

    # ---------- 第二阶段：按窗口存在性监视 ----------
    if seen:
        miss = 0
        while True:
            if _shutting_down():
                return
            n = _app_window_count()
            if n < 0:
                logger.warn("窗口枚举失败，改由页面信标 / 空闲兜底判定关窗")
                return
            if n == 0:
                miss += 1
                if miss >= WINDOW_GONE_CHECKS:
                    _notify_window_closed(
                        logger, "检测到图形窗口已关闭（窗口已不存在），开始完整清理并退出程序")
                    return
            else:
                miss = 0
            time.sleep(1.0)

    # ---------- 兜底：按启动进程是否退出判定 ----------
    if not unusable:
        logger.warn("未确认到应用窗口出现，改用「窗口进程退出」判定关窗")
    try:
        proc.wait()
    except Exception:
        return
    time.sleep(1.0)
    if _shutting_down():
        return
    if _backend_active_recently(1.5):
        # 进程退了但页面还在心跳：说明窗口被交给了已有浏览器进程，窗口还开着，
        # 交给页面关闭信标 / 空闲兜底判定，别把还在用的程序关掉。
        logger.info("窗口进程已交给已有浏览器进程（页面仍在心跳），改由页面信标 / 空闲兜底判定关窗")
        return
    _notify_window_closed(logger, "检测到图形窗口已关闭（窗口进程退出），开始完整清理并退出程序")


def main():
    _redirect_console_output()

    from core import logger
    logger.init_log(LOG_DIR)

    # 给本进程准备一个隐藏控制台：之后启动的所有子进程（以及子进程再开的进程）都继承它，
    # 不会再逐个新建命令行窗口（用户看到「大量窗口跳出又消失」的根因）。
    try:
        from core import services as _services
        _services.ensure_hidden_console()
    except Exception:
        pass

    # 组件预检：缺组件不退出（还要把窗口打开，让启动页把原因显示出来）
    try:
        import launcher as launcher_mod
        missing = launcher_mod.check_components()
        if missing:
            logger.error("图形启动器预检：关键组件缺失：" + "、".join(missing))
    except Exception as e:
        logger.warn(f"图形启动器预检异常：{e}")

    import core.config as config
    from core import services
    from web.server import serve

    # 标记本次运行由图形启动器拉起：在用户选择模式之前，主页面先显示启动页
    config.LAUNCH_VIA_GUI = True

    cfg = services.load_launcher_config()
    services.apply_api_urls(cfg)
    web = cfg.get("web") or {}
    host = web.get("host", config.WEB_HOST)
    port = int(web.get("port", config.WEB_PORT))

    # 关键：磁盘上的代码更新后，端口上可能还挂着「旧版本的后台」进程。
    # 之前遇到这种情况 serve() 会直接复用旧进程（界面还是旧逻辑，改了代码也不生效），
    # 这里先比对构建标识，是旧版本就结束它，本次启动全新后台。
    base_url = f"http://{host}:{port}"
    running = _running_build(base_url)
    replaced = False
    if running is not None:
        local = _local_build()
        if running != local:
            logger.warn(f"检测到旧版本后台仍在运行（build={running or '未知'}，当前={local}），正在结束它")
            # trusted=True：上面已经通过本项目接口确认「端口上就是本程序的后台」
            replaced = _stop_stale_backend(port, logger, trusted=True)
        else:
            logger.info(f"已有同版本后台在运行（build={running}），直接复用它")

    # 启动前清理遗留的本地服务：
    # 上一次运行如果没能完整退出（旧版本没有清理逻辑 / 强杀），服务会一直占着显存与端口。
    try:
        from core import services as services_mod
        left = services_mod.list_service_processes()
        if left:
            logger.warn("启动前发现遗留服务进程："
                        + "、".join(f"{p['kind']}:{p['name']}({p['pid']})" for p in left))
        # 被替换掉的旧后台说明上一次关闭没清理干净：顺手把已加载的 Ollama 权重卸掉
        if replaced or running is None:
            unloaded, still, err = services_mod.unload_ollama_models()
            if unloaded:
                logger.warn("启动前卸载残留的 Ollama 模型权重：" + "、".join(unloaded))
        if left:
            killed = services_mod.sweep_gpt_sovits_orphans(logger=logger)
            if killed:
                logger.warn("启动前清理遗留 GPT-SoVITS：" + str(killed))
            else:
                logger.warn("启动前未能结束遗留 GPT-SoVITS："
                            + "、".join(f'{p["name"]}({p["pid"]})' for p in left))
    except Exception as e:
        logger.warn(f"启动前清理遗留服务失败：{e}")

    def _on_ready(url):
        # 每次启动都打开启动页让人重新选择模式（Lite / 标准），不再沿用上一次的选择：
        # 之前「已启动过就直接进界面」会让后续启动跳过选择、固定用第一次的模式。
        # 整段都包起来：窗口/监视出问题也不能让后台进程崩掉（否则清理会被一起带走）。
        try:
            target = url + "/launcher.html"
            logger.info(f"图形启动器：打开窗口 {target}"
                        + ("（已有服务在运行）" if _probe_running(url) else ""))
            ok, proc = _open_window(target)
            logger.info(f"图形启动器：窗口打开结果 ok={ok} pid={getattr(proc, 'pid', None)}")
            if not ok:
                logger.error("图形启动器：未能打开窗口（未找到 Edge / Chrome，也无法调用默认浏览器）")
                return
            # 把关窗口作为检查点：窗口进程退出 → 完整清理 + 退出程序（不依赖页面脚本）
            if proc is not None:
                threading.Thread(target=_watch_window_process, args=(proc, logger), daemon=True).start()
                logger.info(f"已开始监视图形窗口进程（PID {getattr(proc, 'pid', '?')}）")
            else:
                logger.warn("无法取得窗口进程句柄（改用默认浏览器打开）：关闭检测依赖页面心跳")
        except Exception as e:
            import traceback
            logger.error(f"打开窗口失败：{e}\n{traceback.format_exc()[:800]}")

    logger.info("图形启动器：正在启动本地服务")
    try:
        serve(host, port, open_browser=False, on_ready=_on_ready)
    finally:
        # 若正在执行「关窗口 → 完整清理」，主线程先等它收尾完成再退出，
        # 否则主线程结束会把清理的最后几步与日志截断（服务其实已清干净，但看不到完整记录）。
        try:
            from web import app_lifecycle
            if app_lifecycle.wait_finished(25.0):
                return
        except Exception:
            pass
        try:
            from core import shutdown as shutdown_mod
            shutdown_mod.shutdown("launcher-gui-exit")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "launcher_gui_error.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise
