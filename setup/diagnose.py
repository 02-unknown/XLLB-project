#!/usr/bin/env python3
# setup/diagnose.py —— 小笼洛包 运行环境自检工具（只读，不修改、不安装任何文件）。
# 用法：双击 setup\诊断.bat，或在命令行运行 python setup\diagnose.py
# 作用：逐项检查运行环境，定位"网页无法打开 / 服务无法启动"的原因，并给出处理建议。
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []  # (检查项, 状态, 说明)


def report(item, ok, detail=""):
    RESULTS.append((item, ok, detail))
    print(f"[{'OK' if ok else 'FAIL'}] {item}" + (f" - {detail}" if detail else ""))


def http_get(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except Exception as e:
        return None


def port_open(host, port, timeout=1):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def port_owner(port):
    """尽力获取占用端口的进程名（Windows）。"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], timeout=10, text=True, errors="replace")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                pid = parts[4]
                try:
                    info = subprocess.check_output(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        timeout=5, text=True, errors="replace")
                    name = info.split(",")[0].strip('"') if info.strip() else pid
                except Exception:
                    name = pid
                return f"{name} (PID {pid})"
    except Exception:
        pass
    return None


def check_python(cmd):
    try:
        out = subprocess.check_output([cmd, "--version"], stderr=subprocess.STDOUT,
                                      timeout=10, text=True)
        return "python" in out.lower(), out.strip()
    except Exception:
        return False, ""


def main():
    print("=" * 56)
    print("小笼洛包 运行环境自检")
    print("=" * 56)
    print(f"项目根目录：{PROJECT_ROOT}\n")

    # 1. 项目完整性
    print("--- 1. 项目文件完整性 ---")
    essential = ["core", "plugins", "web", os.path.join("web", "index.html"),
                 os.path.join("web", "static"), "requirements.txt",
                 "app.py", "launcher.py"]
    for rel in essential:
        report(f"文件 {rel}", os.path.exists(os.path.join(PROJECT_ROOT, rel)))

    # 2. 虚拟环境与依赖
    print("\n--- 2. 虚拟环境与 Python 依赖 ---")
    venv_py = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        ok, ver = check_python(venv_py)
        report("venv 虚拟环境", ok, ver if ok else "解释器不可用")
        try:
            subprocess.check_output(
                [venv_py, "-c", "import requests, numpy, yt_dlp"], timeout=30)
            report("依赖 requests/numpy/yt_dlp", True)
        except Exception:
            report("依赖 requests/numpy/yt_dlp", False,
                   "依赖不完整，请运行 setup\\install.bat 重新安装")
    else:
        report("venv 虚拟环境", False, "未创建，请先运行 setup\\install.bat")

    # 3. Web 服务
    print("\n--- 3. Web 界面（默认 127.0.0.1:10999） ---")
    web_host, web_port = "127.0.0.1", 10999
    cfg_path = os.path.join(PROJECT_ROOT, "launcher_config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            web_host = (cfg.get("web", {}) or {}).get("host", web_host)
            web_port = int((cfg.get("web", {}) or {}).get("port", web_port))
        except Exception:
            pass
    listening = port_open(web_host, web_port)
    report(f"端口 {web_port} 监听", listening,
           (f"被占用：{port_owner(web_port)}" if (not listening and port_owner(web_port))
            else ("服务未运行（启动 start.bat 后此处应为 OK）" if not listening else "")))
    if listening:
        st = http_get(f"http://{web_host}:{web_port}/")
        report(f"Web 页面访问 http://{web_host}:{web_port}/", st == 200,
               f"HTTP {st}" if st else "页面响应异常")
    else:
        report("Web 页面访问", False, "服务未运行，请先启动 start.bat")

    # 4. Ollama
    print("\n--- 4. Ollama（可选） ---")
    exe = shutil.which("ollama")
    report("Ollama 程序", bool(exe), exe or "未安装（可只用外部 API 调用大模型）")
    if port_open("127.0.0.1", 11434):
        st = http_get("http://127.0.0.1:11434/api/tags")
        report("Ollama 服务 (11434)", st == 200, f"HTTP {st}" if st else "API 异常")
    else:
        report("Ollama 服务 (11434)", False, "未运行（未安装或未启动；不影响 Web 界面与外部 API 模式）")

    # 5. GPT-SoVITS
    print("\n--- 5. GPT-SoVITS（可选） ---")
    api_script = ""
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api_script = (cfg.get("gpt_sovits", {}) or {}).get("api_script", "") or ""
            if api_script and not os.path.isabs(api_script):
                api_script = os.path.join(PROJECT_ROOT, api_script)
        except Exception:
            pass
    report("GPT-SoVITS api_v2.py", bool(api_script) and os.path.exists(api_script),
           api_script or "未配置（语音合成不可用，文字对话不受影响）")
    if port_open("127.0.0.1", 9880):
        st = http_get("http://127.0.0.1:9880/docs")
        report("GPT-SoVITS 服务 (9880)", st == 200, f"HTTP {st}" if st else "API 异常")
    else:
        report("GPT-SoVITS 服务 (9880)", False, "未运行（语音合成暂不可用，文字对话不受影响）")

    # 6. 系统 Python（判断安装程序能否补装）
    print("\n--- 6. 系统 Python（用于安装程序补装） ---")
    found = False
    for cmd in ("python", "py"):
        ok, ver = check_python(cmd)
        if ok:
            report(f"命令 {cmd}", True, ver)
            found = True
            break
    if not found:
        bundled = os.path.join(PROJECT_ROOT, "gpt_sovits", "runtime", "python.exe")
        if os.path.exists(bundled):
            report("系统 Python", False, "未找到可用系统 Python，将使用 gpt_sovits 自带运行时补装")
        else:
            report("系统 Python", False,
                   "未找到可用系统 Python 且包内无 gpt_sovits 运行时；"
                   "请安装 Python 3.9+（勾选 Add to PATH）后运行 setup\\install.bat")

    # 7. 汇总
    print("\n" + "=" * 56)
    failed = [r for r in RESULTS if not r[1]]
    if not failed:
        print("全部检查通过：环境正常，请双击 start.bat 启动后浏览器会自动打开。")
    else:
        print(f"发现 {len(failed)} 项异常：")
        for item, ok, detail in failed:
            print(f"  - {item}" + (f"：{detail}" if detail else ""))
        print("处理建议：")
        print("  1) venv/依赖异常 -> 双击 setup\\install.bat 完成安装（需联网，已使用国内镜像）；")
        print("  2) Web 未运行     -> 双击 start.bat 启动，浏览器应自动打开；")
        print("  3) 端口被占用     -> 关闭占用程序后重试；")
        print("  4) 浏览器打不开   -> 检查系统代理/防火墙，或手动访问诊断中给出的地址；")
        print("  5) 若仍有问题，请把本窗口输出完整截图反馈。")
    print("=" * 56)


if __name__ == "__main__":
    main()
