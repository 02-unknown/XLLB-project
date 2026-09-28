# verify_close_e2e.py —— 端到端验证「关闭窗口 → 真的卸载模型权重 / 停掉本地服务」。
#
# 为什么要这个测试：只有真起一次 GPT-SoVITS（并模拟窗口关闭），才能证明「关闭时执行了清理」，
#   而不是只验证函数存在。测试在独立进程里跑（那个进程会被 os._exit 结束），
#   测完把结论写在 runtime\_e2e_close_status.json 供本脚本核对。
# 注意：会真实启动 GPT-SoVITS（随后立即结束它），请勿在他人使用语音合成时运行。
#   venv\Scripts\python.exe tests\verify_close_e2e.py
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
OKS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


STATUS = os.path.join(ROOT, "runtime", "_e2e_close_status.json")
CHILD = os.path.join(ROOT, "runtime", "_e2e_close.py")
LOG = os.path.join(ROOT, "runtime", "logs", "xiaolongluo.log")

from core import services  # noqa: E402

if os.path.exists(STATUS):
    os.remove(STATUS)

log_size_before = os.path.getsize(LOG) if os.path.exists(LOG) else 0
gpu_before = services._gpu_memory()
print(f"启动前：显存 {gpu_before}，服务进程 {services.list_service_processes()}")

proc = subprocess.Popen([sys.executable, CHILD])
deadline = time.time() + 90
stage = ""
while time.time() < deadline:
    if os.path.exists(STATUS):
        try:
            data = json.load(open(STATUS, encoding="utf-8"))
            stage = data.get("stage", "")
        except Exception:
            data = {}
        if stage in ("closing", "still-alive-unexpected", "error"):
            break
    if proc.poll() is not None and stage == "closing":
        break
    time.sleep(0.5)

# 等它把清理做完（宽限期 2 秒 + 清理时间）
for _ in range(40):
    if proc.poll() is not None:
        break
    time.sleep(0.5)
exited = proc.poll() is not None
if not exited:
    try:
        proc.terminate()
    except Exception:
        pass
time.sleep(1.5)

data = json.load(open(STATUS, encoding="utf-8")) if os.path.exists(STATUS) else {}
check("测试脚本完成启动并发出关闭信标", data.get("stage") in ("closing", "still-alive-unexpected"),
      str(data.get("stage")))
check("GPT-SoVITS 在测试中确实被启动过（否则测试无意义）",
      data.get("gpt_sovits_started") is True or "started" in str(data.get("gpt_sovits_info")),
      str(data.get("gpt_sovits_info"))[:120])
check("发出关闭信标后程序自己退出了（执行完清理）", exited, f"stage={data.get('stage')}")

left = services.list_service_processes()
gpu_after = services._gpu_memory()
check("关闭后没有 GPT-SoVITS / Ollama 进程残留", left == [], str(left))
check("关闭后 GPT-SoVITS 端口已释放", services.port_available(services.gpt_sovits_port()) is True)
check("关闭后显存已回落（或不高于关闭前）",
      bool(gpu_after) and (gpu_before is None or gpu_after[0] <= gpu_before[0] + 50),
      f"{gpu_before} -> {gpu_after}")

log_tail = ""
if os.path.exists(LOG):
    with open(LOG, encoding="utf-8", errors="replace") as f:
        f.seek(log_size_before)
        log_tail = f.read()
check("日志里能看到「开始卸载模型权重并停止本地服务」",
      "开始卸载模型权重并停止本地服务" in log_tail)
check("日志里能看到停止服务的结果（含卸载 / 停止 / 复核）",
      any(k in log_tail for k in ("已卸载 Ollama 模型权重", "已停止本程序启动的 GPT-SoVITS", "复核")),
      log_tail[-300:])
check("日志里记录了程序退出", "窗口已关闭，程序退出" in log_tail)

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("关闭窗口 → 卸载权重 / 停止服务 端到端验证通过。")
