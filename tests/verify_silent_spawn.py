# verify_silent_spawn.py —— 验证本地服务子进程「静默启动」：整棵进程树都不会出现命令行窗口。
#
# 背景（用户实际看到的现象）：
#   · 只用 CREATE_NO_WINDOW / SW_HIDE 时，从无控制台的 pythonw 启动的子进程仍会带控制台；
#   · 用 DETACHED_PROCESS 让子进程完全无控制台后，子进程再开的「孙进程」会各自新建控制台窗口
#     —— 这正是「大量命令行窗口跳出又迅速消失」的原因。
# 正确做法（本脚本实测）：
#   · 子进程用 CREATE_NO_WINDOW（拥有「无窗口控制台」），孙进程继承它 → 整棵树都没有窗口；
#   · 另外给本进程分配一个隐藏控制台（ensure_hidden_console）作为兜底。
# 做法：生成探针脚本，用 pythonw 运行；探针按真实方式启动子进程，子进程再启动一个孙进程，
#       然后用 EnumWindows 枚举「可见窗口」，检查子/孙进程都没有可见窗口。
#   venv\Scripts\python.exe tests\verify_silent_spawn.py
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
OKS = []
SKIPS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def skip(name, why):
    SKIPS.append(name)
    print(f"[SKIP] {name}（{why}）")


PROBE = os.path.join(ROOT, "runtime", "_silent_probe.py")
RESULT = os.path.join(ROOT, "runtime", "_silent_probe.json")
GC_PID = os.path.join(ROOT, "runtime", "_silent_probe_gc.pid")

PROBE_SRC = r'''
# 探针：由 pythonw 运行（自身没有控制台），检查子进程与孙进程有没有可见的控制台窗口
import ctypes, json, os, subprocess, sys, time
from ctypes import wintypes
sys.path.insert(0, r"__ROOT__")
from core.services import _silent_kwargs, ensure_hidden_console

ensure_hidden_console()          # 真实启动时的做法：给本进程一个隐藏控制台（兜底）
py = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.exists(py):
    py = sys.executable

# 孙进程代码：由它自己写出自己的进程号（避免中间层转发失败）
gc_code = "import os, time\nopen(r'__GC__', 'w').write(str(os.getpid()))\ntime.sleep(12)\n"
# 中间层：用被测参数启动；它自己再启动一个「不加任何参数」的孙进程
middle = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', " + repr(gc_code) + "])\ntime.sleep(13)\n"
child = subprocess.Popen([py, "-c", middle], **_silent_kwargs())
time.sleep(3.5)

user32 = ctypes.windll.user32
pids = set()
def _cb(hwnd, lparam):
    if user32.IsWindowVisible(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(int(pid.value))
    return True
user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_cb), 0)

gc_pid = 0
try:
    gc_pid = int(open(r"__GC__").read().strip())
except Exception:
    gc_pid = 0

out = {
    "silent_kwargs": sorted(_silent_kwargs().keys()),
    "visible_window_total": len(pids),
    "self_pid": os.getpid(),
    "self_visible": os.getpid() in pids,
    "child_pid": child.pid,
    "grandchild_pid": gc_pid,
    "child_visible": child.pid in pids,
    "grandchild_visible": gc_pid in pids,
}
for p in (child,):
    try:
        p.terminate()
    except Exception:
        pass
if gc_pid:
    try:
        os.kill(gc_pid, 9)
    except Exception:
        pass
with open(r"__RESULT__", "w", encoding="utf-8") as f:
    json.dump(out, f)
'''.replace("__ROOT__", ROOT.replace("\\", "/")).replace("__GC__", GC_PID.replace("\\", "/")) \
   .replace("__RESULT__", RESULT.replace("\\", "/"))


def main():
    from core.services import _silent_kwargs

    kw = _silent_kwargs()
    flags = kw.get("creationflags", 0)
    check("子进程用 CREATE_NO_WINDOW（拥有无窗口控制台，孙进程可继承）",
          bool(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0)), str(flags))
    check("不再使用 DETACHED_PROCESS（它会让孙进程各自新建控制台窗口）",
          not (flags & getattr(subprocess, "DETACHED_PROCESS", 0)), str(flags))
    check("附带隐藏窗口 startupinfo（双保险）",
          kw.get("startupinfo") is not None
          and bool(kw["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
          and kw["startupinfo"].wShowWindow == subprocess.SW_HIDE)

    services_src = open(os.path.join(ROOT, "core", "services.py"), encoding="utf-8").read()
    check("提供「隐藏控制台」兜底（ensure_hidden_console）并在启动服务前调用",
          "def ensure_hidden_console" in services_src
          and services_src.count("ensure_hidden_console()") >= 2)
    check("GPT-SoVITS 启动走静默参数", "cwd=api_dir" in services_src and "**_silent_kwargs()" in services_src)
    check("GPT-SoVITS 优先用 pythonw.exe 启动（解释器自身就不带控制台）",
          "pythonw.exe" in services_src and "def _gpt_sovits_python" in services_src)
    from core import services as services_mod
    chosen = services_mod._gpt_sovits_python(services_mod.load_launcher_config())
    check("按当前配置选出的解释器确实是 pythonw.exe（实测）",
          os.path.basename(chosen).lower() == "pythonw.exe" and os.path.exists(chosen), chosen)
    check("Ollama 启动走静默参数（输出写入 logs/ollama.log）",
          re.search(r'Popen\(\[exe, "serve"\][^)]*\*\*_silent_kwargs\(\)', services_src, re.S) is not None
          and "ollama.log" in services_src)
    check("已经没有任何地方再新建控制台窗口（CREATE_NEW_CONSOLE 已移除）",
          "CREATE_NEW_CONSOLE" not in services_src)

    # 真实检测：用 pythonw 跑探针（子进程 + 孙进程都不应有可见窗口）
    os.makedirs(os.path.join(ROOT, "runtime"), exist_ok=True)
    for path in (RESULT, GC_PID):
        if os.path.exists(path):
            os.remove(path)
    with open(PROBE, "w", encoding="utf-8") as f:
        f.write(PROBE_SRC)

    pythonw = os.path.join(ROOT, "venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    proc = subprocess.Popen([pythonw, PROBE])
    deadline = time.time() + 40
    data = None
    while time.time() < deadline:
        if os.path.exists(RESULT):
            try:
                with open(RESULT, encoding="utf-8") as f:
                    data = json.load(f)
                break
            except Exception:
                pass
        time.sleep(0.3)
    try:
        proc.terminate()
    except Exception:
        pass
    for path in (PROBE, RESULT, GC_PID):
        try:
            os.remove(path)
        except OSError:
            pass

    check("探针已产出检测结果（pythonw 下真实启动子进程与孙进程）", isinstance(data, dict), str(data))
    if isinstance(data, dict):
        if data.get("visible_window_total", 0) > 0:
            OKS.append("桌面存在可见窗口 → 窗口枚举检测有效")
            print(f"[OK]   窗口枚举检测有效（当前可见窗口进程数 {data['visible_window_total']}）")
        else:
            skip("窗口枚举检测有效性", "当前会话没有可见窗口，无法用对照验证（仍检查子/孙进程）")
        check("程序自身的隐藏控制台也是隐藏的（不会自己弹一个窗口）",
              data.get("self_visible") is False, f"self_pid={data.get('self_pid')}")
        check("子进程没有可见的控制台窗口（不再出现空白命令行）",
              data.get("child_visible") is False, f"child_pid={data.get('child_pid')}")
        check("孙进程（服务自己再启动的进程）同样没有可见窗口（不再大量跳出）",
              data.get("grandchild_visible") is False and data.get("grandchild_pid"),
              f"grandchild_pid={data.get('grandchild_pid')}")

    print()
    if FAILS:
        print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
        for f_ in FAILS:
            print("  -", f_)
        return 1
    print(f"通过 {len(OKS)} 项，失败 0 项" + (f"（跳过 {len(SKIPS)} 项）" if SKIPS else ""))
    print("本地服务静默启动 验证全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
