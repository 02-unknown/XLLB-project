# plugins/model_manager.py —— 官方插件：模型 / API 设置 + 一键自动调优。
# 生成模型与判断模型可分别设置后端与模型名；Ollama 后端会列出已安装模型供选择。
import os
import platform
import subprocess

import core.config as config

NAME = "模型与自动调优"
VERSION = "1.0.0"
DESCRIPTION = "模型 / API 设置（生成与判断可分开）+ 一键自动调优"
AUTHOR = "官方"
OFFICIAL = True

SETTINGS = {
    "chat_backend": "ollama",
    "judge_backend": "ollama",
    "chat_model": "qwen3.5:9b",
    "judge_model": "qwen3.5:9b",
    "api_base": "",
    "api_key": "",
}


def _apply(settings, c):
    c.LLM_CHAT_BACKEND = settings.get("chat_backend", "ollama")
    c.LLM_JUDGE_BACKEND = settings.get("judge_backend", "ollama")
    c.LLM_CHAT_MODEL = settings.get("chat_model", c.LLM_CHAT_MODEL)
    c.LLM_JUDGE_MODEL = settings.get("judge_model", c.LLM_JUDGE_MODEL)
    c.LLM_API_BASE = (settings.get("api_base") or "").strip()
    c.LLM_API_KEY = (settings.get("api_key") or "").strip()


def on_load(settings, ctx):
    _apply(settings, ctx.config)
    # 后台预热 Ollama 模型列表，避免首次打开设置时卡顿
    try:
        from core import llm
        llm.warm_ollama_models()
    except Exception:
        pass


def on_settings_changed(settings, ctx):
    _apply(settings, ctx.config)


def _model_field(key, label, model, backend):
    # 注意：settings_schema() 会在每次列出插件时被调用，禁止在此做网络请求。
    # Ollama 可用模型由前端通过 /api/models 单独拉取（options_source 标记）。
    if backend == "ollama":
        return {"key": key, "label": label, "type": "datalist",
                "options": [model] if model else [], "options_source": "ollama_models"}
    return {"key": key, "label": label, "type": "text", "placeholder": "如 gpt-4o-mini"}


def settings_schema():
    return [
        {"key": "chat_backend", "label": "生成后端", "type": "select", "options": ["ollama", "openai"]},
        {"key": "judge_backend", "label": "判断后端", "type": "select", "options": ["ollama", "openai"]},
        _model_field("chat_model", "生成模型", config.LLM_CHAT_MODEL, config.LLM_CHAT_BACKEND),
        _model_field("judge_model", "判断模型", config.LLM_JUDGE_MODEL, config.LLM_JUDGE_BACKEND),
        {"key": "api_base", "label": "API Base URL", "type": "text", "placeholder": "https://api.openai.com/v1"},
        {"key": "api_key", "label": "API Key", "type": "password", "placeholder": "留空则读取环境变量 OPENAI_API_KEY"},
    ]


# ==================== 命令 ====================
def commands():
    return [
        {"name": "/model", "desc": "查看或切换生成模型", "args": "[模型名]"},
        {"name": "/judge", "desc": "查看或切换判断模型", "args": "[模型名]"},
        {"name": "/backend", "desc": "切换生成后端", "args": "ollama|openai"},
        {"name": "/jbackend", "desc": "切换判断后端", "args": "ollama|openai"},
        {"name": "/autotune", "desc": "一键检测并推荐模型配置", "args": "[apply]"},
        {"name": "/sysinfo", "desc": "查看系统信息", "args": ""},
    ]


def on_command(command, args, ctx):
    c = ctx.config
    if command == "/model":
        if args:
            c.LLM_CHAT_MODEL = args[0]
            ctx.manager.save_settings(NAME, {"chat_model": args[0]})
            return {"reply": f"生成模型已切换为 {args[0]}", "speak": True}
        return {"reply": _status(c), "speak": False}
    if command == "/judge":
        if args:
            c.LLM_JUDGE_MODEL = args[0]
            ctx.manager.save_settings(NAME, {"judge_model": args[0]})
            return {"reply": f"判断模型已切换为 {args[0]}", "speak": True}
        return {"reply": f"判断模型：{c.LLM_JUDGE_MODEL}（{c.LLM_JUDGE_BACKEND}）", "speak": False}
    if command == "/backend":
        if args and args[0] in ("ollama", "openai"):
            c.LLM_CHAT_BACKEND = args[0]
            ctx.manager.save_settings(NAME, {"chat_backend": args[0]})
            return {"reply": f"生成后端已切换为 {args[0]}", "speak": True}
        return {"reply": "用法：/backend ollama 或 /backend openai", "speak": False}
    if command == "/jbackend":
        if args and args[0] in ("ollama", "openai"):
            c.LLM_JUDGE_BACKEND = args[0]
            ctx.manager.save_settings(NAME, {"judge_backend": args[0]})
            return {"reply": f"判断后端已切换为 {args[0]}", "speak": True}
        return {"reply": "用法：/jbackend ollama 或 /jbackend openai", "speak": False}
    if command == "/sysinfo":
        return {"reply": _sysinfo(), "speak": False}
    if command == "/autotune":
        r = _detect()
        if args and args[0] == "apply":
            applied = _apply_rec(r, ctx)
            return {"reply": _report(r) + "\n\n" + applied, "speak": False}
        return {"reply": _report(r), "speak": False}
    return None


def _status(c):
    shared = (c.LLM_CHAT_BACKEND == "ollama" and c.LLM_JUDGE_BACKEND == "ollama"
              and c.LLM_CHAT_MODEL == c.LLM_JUDGE_MODEL)
    s = f"生成：{c.LLM_CHAT_MODEL}（{c.LLM_CHAT_BACKEND}）\n判断：{c.LLM_JUDGE_MODEL}（{c.LLM_JUDGE_BACKEND}）"
    if shared:
        s += "\n（生成与判断为同一模型，Ollama 只会加载一次，节省显存）"
    return s


# ==================== 动作（UI 一键按钮） ====================
def actions():
    return [
        {"name": "autotune", "label": "一键自动调优", "desc": "检测系统并应用推荐配置"},
        {"name": "detect", "label": "仅检测系统", "desc": "只检测，不修改配置"},
    ]


def on_action(name, ctx):
    if name == "detect":
        return {"reply": _report(_detect()), "speak": False}
    if name == "autotune":
        r = _detect()
        applied = _apply_rec(r, ctx)
        return {"reply": _report(r) + "\n\n" + applied, "speak": False}
    return None


# ==================== 检测与推荐 ====================
def _vram_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        lines = [l for l in out.decode("utf-8", "ignore").strip().splitlines() if l.strip()]
        return int(float(lines[0])) if lines else 0
    except Exception:
        return 0


def _ram_gb():
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        return 0.0


def _sysinfo():
    vram, ram, cpu = _vram_mb(), _ram_gb(), (os.cpu_count() or 0)
    return f"系统：{platform.system()} {platform.release()}（{platform.machine()}）\n显存：{vram}MB，内存：{ram}GB，CPU：{cpu} 核"


def _detect():
    vram, ram, cpu = _vram_mb(), _ram_gb(), (os.cpu_count() or 0)
    if vram >= 10000:
        rec = {"chat_backend": "ollama", "judge_backend": "ollama", "chat_model": "qwen3.5:9b", "judge_model": "qwen3.5:3b",
               "note": "显存充足（≥10G），可流畅运行 9B 模型"}
    elif vram >= 6000:
        rec = {"chat_backend": "ollama", "judge_backend": "ollama", "chat_model": "qwen3.5:7b", "judge_model": "qwen3.5:3b",
               "note": "显存中等（6~10G），推荐 7B 模型"}
    elif vram >= 3000:
        rec = {"chat_backend": "ollama", "judge_backend": "ollama", "chat_model": "qwen3.5:3b", "judge_model": "qwen3.5:3b",
               "note": "显存较小（3~6G），推荐 3B 小模型"}
    elif ram >= 16:
        rec = {"chat_backend": "ollama", "judge_backend": "ollama", "chat_model": "qwen3.5:3b", "judge_model": "qwen3.5:3b",
               "note": "无独显但内存充足，可用 CPU 跑 3B 小模型"}
    else:
        rec = {"chat_backend": "openai", "judge_backend": "openai", "chat_model": "", "judge_model": "",
               "note": "无独显且内存有限，建议切换 API 后端（请在下方填 API 地址与 Key）"}
    return {"vram_mb": vram, "ram_gb": ram, "cpu": cpu, "recommend": rec}


def _report(r):
    rec = r["recommend"]
    lines = [
        f"系统检测：显存 {r['vram_mb']}MB，内存 {r['ram_gb']}GB，CPU {r['cpu']} 核",
        f"建议：{rec['note']}",
    ]
    if rec["chat_backend"] == "ollama":
        lines.append(f"推荐：生成 {rec['chat_model']}，判断 {rec['judge_model']}（本地 Ollama）")
    else:
        lines.append("推荐：切换 OpenAI 兼容 API 后端")
    lines.append("点“一键自动调优”应用，或输入 /autotune apply")
    return "\n".join(lines)


def _apply_rec(r, ctx):
    rec = r["recommend"]
    # 只持久化配置字段，不写入展示用的 note
    patch = {k: rec.get(k) for k in ("chat_backend", "judge_backend", "chat_model", "judge_model")}
    try:
        ctx.manager.save_settings(NAME, patch)
        return "已应用推荐配置。"
    except Exception:
        _apply(rec, ctx.config)
        return "已应用推荐配置（直接写入运行时）。"
