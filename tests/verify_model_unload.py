# verify_model_unload.py —— 验证「关闭程序时会卸载模型权重，不再占用硬件」。
#
# 需求：现有关闭逻辑只停进程，不会卸载已加载的模型权重 → 显卡 / 内存仍被占着。
# 做法：
#   1) 起一个「假 Ollama 服务」：/api/ps 先返回两个已加载模型，收到 keep_alive=0 后返回空；
#   2) 调用 services.unload_ollama_models()，检查它逐个调用 /api/generate 且 keep_alive=0，
#      并在复核时确认模型已卸载；
#   3) 检查 stop_all() 的顺序（先卸权重 → 再停进程 → 清遗留 → 释放进程内模型 → 复核端口 / 显存）；
#   4) 检查进程内模型（Whisper）提供 release()，且 stop_all 会调用它。
#   venv\Scripts\python.exe tests\verify_model_unload.py
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


MODELS = ["qwen3.5:9b", "glm-4-flash"]
state = {"loaded": list(MODELS), "posts": []}


class FakeOllama(BaseHTTPRequestHandler):
    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/ps"):
            self._send({"models": [{"name": n} for n in state["loaded"]]})
        else:
            self._send({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        state["posts"].append({"path": self.path, "payload": payload})
        if self.path.startswith("/api/generate") and payload.get("keep_alive") == 0:
            name = payload.get("model")
            if name in state["loaded"]:
                state["loaded"].remove(name)
        self._send({"done": True})

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

from core import models as models_mod  # noqa: E402
from core import services  # noqa: E402

try:
    unloaded, still, err = services.unload_ollama_models({"ollama": {"api_url": base}})
    check("能列出已加载模型并逐个卸载（返回已卸载列表）",
          unloaded == MODELS, f"unloaded={unloaded} err={err}")
    check("卸载请求带 keep_alive=0（官方卸载方式，释放显存/内存）",
          len(state["posts"]) == len(MODELS)
          and all(p["path"].startswith("/api/generate") and p["payload"].get("keep_alive") == 0
                  for p in state["posts"]),
          str(state["posts"])[:160])
    check("每个模型的卸载请求都带上了模型名",
          sorted(p["payload"].get("model") for p in state["posts"]) == sorted(MODELS),
          str([p["payload"].get("model") for p in state["posts"]]))
    check("复核后确认权重已卸载（/api/ps 为空）", still == [], str(still))
finally:
    srv.shutdown()

# 服务不可用时要安全跳过（不抛异常，给出原因）
unloaded2, still2, err2 = services.unload_ollama_models({"ollama": {"api_url": "http://127.0.0.1:1"}})
check("Ollama 未运行时安全跳过并说明原因",
      unloaded2 == [] and still2 == [] and "未获取到已加载模型" in err2, err2[:100])

# ---------- stop_all 顺序与复核 ----------
src = open(os.path.join(ROOT, "core", "services.py"), encoding="utf-8").read()
check("stop_all 先卸载模型权重、再停止服务进程、最后复核",
      src.index("unload_ollama_models(timeout=") < src.index("stop_gpt_sovits()")
      < src.index("_kill_orphan_services(logger=") < src.index("verify_services_stopped()"))
check("清理全过程受总超时约束（不会把关闭流程拖住）",
      "deadline_seconds" in src and "_left()" in src and "清理遗留进程时间已到" in src)
check("还会按端口占用者清理（兼容手动启动的整合包）",
      "def _kill_service_port_owners" in src and "def port_owner_pid" in src)
check("stop_all 会释放进程内模型（Whisper）",
      "release_local_models()" in src and callable(getattr(models_mod, "release", None)))
check("stop_all 会复核端口是否已释放（避免进程没退干净）",
      "_wait_ports_free" in src and "端口仍被占用" in src)
check("stop_all 报告显存占用前后对比（有 N 卡时给出实际数字）",
      "_gpu_memory" in src and "显存占用：" in src)
check("报告里写明「权重随进程退出释放」",
      "权重随进程退出释放" in src)

# ---------- 进程内模型 release ----------
ready_before = models_mod.is_ready()
freed = models_mod.release()
check("Whisper release() 可安全调用（未加载时返回 False，不报错）",
      freed in (True, False) and models_mod.is_ready() is False,
      f"before={ready_before} freed={freed}")

report = services.stop_all()
check("stop_all 始终返回可读报告（字符串）", isinstance(report, str) and report, report[:80])
check("报告里给出可读结论（停止 / 卸载 / 清理 / 复核 / 无需停止）",
      any(k in report for k in ("已停止", "已卸载", "已释放", "已清理", "复核", "没有需要停止")),
      report[:160])

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("关闭时卸载模型权重 验证全部通过。")
