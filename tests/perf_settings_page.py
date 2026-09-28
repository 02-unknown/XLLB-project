# perf_settings_page.py —— 设置页加载耗时剖析（只读，不起真实业务）
# 用法：venv\Scripts\python.exe perf_settings_page.py
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import plugin_manager  # noqa: E402

mgr = plugin_manager.manager


def timed(label, fn, repeat=3):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as e:
            print(f"  {label:34s} 失败: {e}")
            return None
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    flag = "  <== 慢" if best > 200 else ""
    print(f"  {label:34s} {best:8.1f} ms{flag}")
    return best


print("=" * 72)
print("A) 插件层：list_plugins() 内部各步骤")
print("=" * 72)
for p in mgr._plugins.values():
    t0 = time.perf_counter()
    schema = p.settings_schema()
    t1 = time.perf_counter()
    acts = p.actions()
    t2 = time.perf_counter()
    cmds = p.commands()
    t3 = time.perf_counter()
    tab = p.settings_tab(mgr.ctx)
    t4 = time.perf_counter()
    total = (t4 - t0) * 1000
    if total > 5:
        print(f"  {p.name:14s} schema {(t1-t0)*1000:7.1f} actions {(t2-t1)*1000:6.1f} "
              f"commands {(t3-t2)*1000:6.1f} tab {(t4-t3)*1000:6.1f} | 合计 {total:7.1f} ms")

tot = timed("manager.list_plugins()", lambda: mgr.list_plugins())

from web import server as web_server  # noqa: E402

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return r.read()


print()
print("=" * 72)
print("B) 接口层：设置页首屏串行访问耗时（取 3 次最快）")
print("=" * 72)
paths = [
    "/settings.html",
    "/api/settings",
    "/api/characters",
    "/api/voice_presets",
    "/api/history",
    "/api/plugins",
    "/api/models",
    "/api/characters/manage",
    "/static/settings.js",
    "/static/style.css",
]
total = 0.0
for path in paths:
    dt = timed(f"GET {path}", lambda p=path: get(p))
    total += dt or 0
print(f"\n  首屏合计（串行）≈ {total:.0f} ms")

print()
print("=" * 72)
print("C) 说明")
print("=" * 72)
print("  /api/models 依赖本地 Ollama，若服务未启动会等超时（前端已异步，不阻塞首屏渲染）。")

srv.shutdown()
srv.server_close()
