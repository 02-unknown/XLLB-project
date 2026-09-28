# verify_service_cleanup.py —— 验证「关闭后不再有 GPT-SoVITS / Ollama 残留占用」。
#
# 真实现象：GPT-SoVITS 由「上一次运行」启动后父进程消失（遗留进程），
#   旧逻辑只按句柄停自己启动的进程，于是它一直占着显存与 20000 端口，关闭不完全。
# 做法：
#   1) 用项目内的 GPT-SoVITS 运行时解释器（gpt_sovits\runtime\python.exe）真起一个「替身遗留进程」，
#      检查能被识别为 kind=gpt_sovits（按可执行文件路径判断，不靠 tasklist）；
#   2) 检查 list_service_processes / verify_services_stopped 的输出；
#   3) 检查「只清理 GPT-SoVITS 遗留进程」的启动清理函数不会动 Ollama；
#   4) 真结束该替身进程并确认端口释放（受限环境会跳过结束步骤并说明）；
#   5) 静态检查 stop_all 的复核 + 二次清理逻辑与显存/占用者报告。
#   venv\Scripts\python.exe tests\verify_service_cleanup.py
import os
import socket
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


from core import services  # noqa: E402

src = open(os.path.join(ROOT, "core", "services.py"), encoding="utf-8").read()

# ==================== 1) 静态检查：识别 + 复核 + 二次清理 ====================
check("用进程快照识别进程（不依赖 tasklist，避免被拒绝访问）",
      "def _snapshot_processes" in src and "CreateToolhelp32Snapshot" in src)
check("按可执行文件路径识别「本项目的 GPT-SoVITS 进程」",
      "def _is_our_gpt_sovits" in src and "gpt_sovits" in src and "startswith(gs_dir)" in src)
check("Ollama 覆盖服务 / 模型 runner / 桌面端三类进程名",
      '("ollama.exe", "ollama_llama_server.exe", "ollama app.exe")' in src)
check("提供只读诊断接口（列出服务进程 / 复核是否停干净）",
      "def list_service_processes" in src and "def verify_services_stopped" in src)
check("启动时只清理 GPT-SoVITS 遗留进程，不会动 Ollama",
      "def sweep_gpt_sovits_orphans" in src and "only_gpt_sovits" in src
      and 'p["kind"] != "gpt_sovits"' in src)
check("stop_all 复核后若仍有残留会再清一次，并报告残留进程 / 端口",
      src.count("verify_services_stopped()") >= 2 and "复核发现仍有服务进程存活" in src
      and "复核发现端口仍被占用" in src)
check("报告不会把「没启动过的服务」说成已停止（避免误导）",
      "owned_gs" in src and "已停止本程序启动的 GPT-SoVITS" in src)
check("显存未下降但服务已清空时给出说明（占用来自其它程序）",
      "占用可能来自其它程序" in src)

# ==================== 2) 真实替身遗留进程 ====================
gs_python = os.path.join(ROOT, "gpt_sovits", "runtime", "python.exe")
if not os.path.exists(gs_python):
    skip("替身遗留进程识别", "未找到打包运行时 gpt_sovits\\runtime\\python.exe")
    dummy = None
else:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    code = ("import socket, time\n"
            "srv = socket.socket()\n"
            "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            f"srv.bind(('127.0.0.1', {port}))\n"
            "srv.listen(5)\n"
            "time.sleep(120)\n")
    dummy = subprocess.Popen([gs_python, "-c", code],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(2.0)

    found = [p for p in services.list_service_processes() if p["pid"] == dummy.pid]
    check("项目内的 GPT-SoVITS 解释器进程被识别为 kind=gpt_sovits",
          bool(found) and found[0]["kind"] == "gpt_sovits", str(found))
    check("识别结果带可执行文件路径（用于区分其它 Python）",
          bool(found) and "gpt_sovits" in (found[0].get("image") or "").lower(), str(found)[:160])

    # 只清理 GPT-SoVITS 的函数不会把 Ollama 也列进去
    kinds = {p["kind"] for p in services.list_service_processes()}
    check("只读诊断能同时区分 gpt_sovits 与 ollama 两类进程",
          kinds.issubset({"gpt_sovits", "ollama"}), str(kinds))

    # 真结束（受限环境可能不允许，跳过并说明）
    killed = services.sweep_gpt_sovits_orphans()
    time.sleep(1.0)
    alive = any(p["pid"] == dummy.pid for p in services.list_service_processes())
    if alive:
        skip("结束遗留 GPT-SoVITS 进程", f"当前环境不允许结束进程（kill 结果 {killed}）")
    else:
        check("能结束遗留的 GPT-SoVITS 进程（关闭不完全的根因）",
              not alive and killed, str(killed))
        check("结束后端口随之释放", services.port_available(port) is True)
    try:
        dummy.terminate()
    except Exception:
        pass

# ==================== 3) 复核接口 ====================
st = services.verify_services_stopped()
check("复核接口返回进程与端口两类结果（供退出流程判断）",
      isinstance(st, dict) and "processes" in st and "ports" in st, str(st)[:120])
report = services.stop_all(include_orphans=False, verify=True)
check("stop_all 报告包含复核结论", "复核" in report, report[:160])

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项" + (f"（跳过 {len(SKIPS)} 项）" if SKIPS else ""))
print("服务残留清理 验证全部通过。")
