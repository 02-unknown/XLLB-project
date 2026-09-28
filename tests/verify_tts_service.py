# -*- coding: utf-8 -*-
"""GPT-SoVITS 语音服务启动修复 验证

覆盖本次修复（原问题：GPT-SoVITS 加载完模型就自己退出）：
  1) 端口可监听性检测：Windows「系统保留段」内的端口（如 9880 落在 9834-9933 排除段）
     bind 会失败 → port_available() 必须能识别；
  2) 端口自动回退：配置端口不可用时自动选一个可监听端口，并把结果写回 launcher_config.json；
  3) 启动输出落到日志文件（不再开一个出错就消失的控制台窗口）；
  4) 启动后立刻退出时给出明确原因（读日志尾部判断端口 / CUDA 等问题）；
  5) 状态查询 / 重启 / 停止接口（供 Web UI 的「重启语音服务」使用）；
  6) 真实端到端（可选，--e2e）：用自动选出的端口真的把 api_v2.py 起起来，
     等 /docs 返回 200，然后停止。

用法：
  venv\\Scripts\\python.exe tests\verify_tts_service.py          # 只跑快速检查（不启动模型）
  venv\\Scripts\\python.exe tests\verify_tts_service.py --e2e    # 额外做一次真实启动（需 1-2 分钟）
"""
import json
import os
import shutil
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import services  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "launcher_config.json")
OKS, FAILS = [], []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def free_port():
    """让系统分配一个当前空闲端口（用于「占用端口」场景）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


cfg_before = json.load(open(CFG, encoding="utf-8")) if os.path.exists(CFG) else {}

# ==================== 1) 端口可监听性检测 ====================
section("1) 端口可监听性检测（能识别系统保留 / 被占用的端口）")
hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
busy_port = free_port()
hold.bind(("127.0.0.1", busy_port))
hold.listen(1)
check(f"被占用的端口 {busy_port} 判定为不可用", services.port_available(busy_port) is False)
hold.close()
check(f"关闭后可重新监听 {busy_port}", services.port_available(busy_port) is True)
check("端口 20000 可监听（备选端口在动态端口段之外）", services.port_available(20000) is True)

# ==================== 2) 端口解析与自动回退 ====================
section("2) 端口解析 / 自动回退（含写回配置）")
check("默认端口解析自 api_url（9880）", services.gpt_sovits_port({"gpt_sovits": {"api_url": "http://127.0.0.1:9880"}}) == 9880)
check("显式 port 优先于 api_url",
      services.gpt_sovits_port({"gpt_sovits": {"api_url": "http://127.0.0.1:9880", "port": 21000}}) == 21000)
check("地址跟随 port（保证监听与访问一致）",
      services.gpt_sovits_url({"gpt_sovits": {"api_url": "http://127.0.0.1:9880", "port": 21000}})
      == "http://127.0.0.1:21000")
# 占用配置端口 → 解析器应换端口
tmp_cfg = {"gpt_sovits": {"api_url": f"http://127.0.0.1:{busy_port}", "port": busy_port}}
hold2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
hold2.bind(("127.0.0.1", busy_port))
hold2.listen(1)
port2, why2 = services.resolve_gpt_sovits_port(tmp_cfg)
hold2.close()
check("配置端口被占用时换到可用端口且给出原因",
      port2 != busy_port and services.port_available(port2) and "无法监听" in why2, f"{port2} / {why2}")
# 本机 9880 落在 Windows 保留段（9834-9933）：若确实不可监听，解析器必须避开它
if not services.port_available(9880):
    port3, why3 = services.resolve_gpt_sovits_port({"gpt_sovits": {"api_url": "http://127.0.0.1:9880", "port": 9880}})
    check("本机 9880 被系统保留 → 自动改用其它端口（这正是「自己关闭」的根因）",
          port3 != 9880 and services.port_available(port3), f"{port3} / {why3}")
else:
    print("[SKIP] 本机 9880 可监听（未落在保留段），跳过该场景")

# ==================== 3) 启动失败时的日志与诊断 ====================
section("3) 启动输出落盘 + 立刻退出时给出明确原因")
orig_script = services.DEFAULTS["gpt_sovits"]["api_script"]
proc_hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
proc_hold.bind(("127.0.0.1", busy_port))
proc_hold.listen(1)
# 写一个「模拟端口被占」的假脚本：尝试 bind 主机上被占用的端口后会抛错退出
fake_dir = os.path.join(ROOT, "runtime", "_tts_test")
os.makedirs(fake_dir, exist_ok=True)
fake_py = os.path.join(fake_dir, "fake_api.py")
with open(fake_py, "w", encoding="utf-8") as f:
    f.write(
        "import socket, sys\n"
        "port = int(sys.argv[sys.argv.index('-p') + 1])\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', port))   # 端口被占用 → 抛错退出\n"
        "print('bind ok', port)\n"
    )
fake_cfg = {
    "gpt_sovits": {
        "enabled": True,
        "api_script": os.path.relpath(fake_py, ROOT).replace("\\", "/"),
        "python": sys.executable,
        "api_url": f"http://127.0.0.1:{busy_port}",
        "port": None,
        "start_timeout": 5,
    },
    "ollama": dict(services.DEFAULTS["ollama"]),
    "web": dict(services.DEFAULTS["web"]),
}
started, info = services.start_gpt_sovits(fake_cfg, auto_port=False)
check("启动后立刻退出时返回 exited + 原因", started is False and info.get("reason") == "exited", str(info)[:140])
check("失败原因里带上了日志尾部（便于排查）", bool(info.get("log_tail")), str(info.get("log_tail"))[:120])
tail = services.tail_gpt_sovits_log(30)
check("启动输出已写入日志文件（不再随控制台窗口消失）",
      "=====" in tail and os.path.basename(fake_py) in tail, tail[-120:])
check("退出错误被识别为端口类问题",
      ("Errno" in str(info.get("error", "")) or "端口" in str(info.get("error", ""))
       or "in use" in str(info.get("error", "")).lower()), str(info.get("error"))[:120])
proc_hold.close()

# ==================== 4) 状态 / 重启 / 停止 ====================
section("4) 状态查询与停止（Web UI「重启语音服务」依赖）")
st = services.gpt_sovits_status()
check("状态包含 ready/running/port/url/log_file/configured_port_available",
      all(k in st for k in ("ready", "running", "port", "url", "log_file", "configured_port_available")), str(st)[:140])
check("未启动时 running 为假", st["running"] is False)
check("停止接口可安全调用（无进程时返回 True）", services.stop_gpt_sovits() is True)

# ==================== 4.5) 静默启动（不弹命令行窗口） ====================
section("4.5) 子进程静默启动（图形界面的前提：不出现命令行窗口）")
with open(os.path.join(ROOT, "core", "services.py"), encoding="utf-8") as f:
    services_src = f.read()
check("静默标志使用 DETACHED_PROCESS + CREATE_NO_WINDOW（子进程完全不带控制台）",
      "DETACHED_PROCESS" in services_src and "CREATE_NO_WINDOW" in services_src
      and "def _silent_kwargs" in services_src)
check("GPT-SoVITS 子进程带静默参数（此前会弹出一个空白命令行窗口）",
      "cwd=api_dir" in services_src and "**_silent_kwargs()" in services_src)
check("Ollama 子进程同样静默（不再 CREATE_NEW_CONSOLE）",
      "CREATE_NEW_CONSOLE" not in services_src and 'Popen([exe, "serve"]' in services_src
      and '**_silent_kwargs()' in services_src)
check("GPT-SoVITS 输出仍重定向到日志文件（静默后不丢日志）",
      'stderr=subprocess.STDOUT' in services_src and "gpt_sovits_api.log" in services_src)
check("（真实控制台检测见 verify_silent_spawn.py）退出时能停掉本地服务",
      "def stop_all" in services_src and "def stop_ollama" in services_src)

# ==================== 5) 真实端到端（可选） ====================
if "--e2e" in sys.argv:
    section("5) 真实启动 GPT-SoVITS（自动端口）并等待 /docs 就绪")
    real_cfg = services.load_launcher_config()
    started, info = services.start_gpt_sovits(real_cfg)
    print("  启动结果：", info.get("reason"), "端口", info.get("port"))
    got = False
    deadline = time.time() + 180
    while time.time() < deadline:
        if services.check_gpt_sovits():
            got = True
            break
        time.sleep(3)
    st2 = services.gpt_sovits_status()
    check("GPT-SoVITS 在自动选择的端口上真实就绪（/docs 200）", got,
          f"port={st2.get('port')} log={services.tail_gpt_sovits_log(12)[-400:]}")
    check("就绪端口可用（可监听，或正被本服务监听）",
          bool(st2.get("port")) and (st2.get("ready") or services.port_available(st2.get("port"))),
          str(st2.get("port")))
    check("不再使用被系统保留的端口", bool(st2.get("port")) and st2["port"] != 9880, str(st2.get("port")))
    check("launcher_config.json 已记录实际端口", bool(services.load_launcher_config()["gpt_sovits"].get("port")),
          str(services.load_launcher_config()["gpt_sovits"].get("port")))
    services.stop_gpt_sovits()
    print("  已停止测试启动的语音服务。")
else:
    section("5) 真实端到端（已跳过）")
    print("  需要真实验证时加 --e2e 参数（会启动模型，约 1-2 分钟）。")

# ==================== 收尾：清理测试脚本；恢复配置里的 api_script ====================
shutil.rmtree(fake_dir, ignore_errors=True)
try:
    cfg_now = json.load(open(CFG, encoding="utf-8"))
    if "--e2e" not in sys.argv and cfg_now != cfg_before:
        # 非 e2e 模式不应改动配置；若被改了就还原
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cfg_before, f, ensure_ascii=False, indent=2)
        print("  已还原 launcher_config.json（快速模式不改配置）")
except Exception:
    pass

print()
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("验证结论: ✓ 全部通过")
