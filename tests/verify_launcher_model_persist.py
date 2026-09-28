# verify_launcher_model_persist.py —— 验证「模型 / API 设置下次启动沿用上次设置」
#
# 背景（本轮修复）：Lite 模式启动时会强制 `config.LLM_CHAT_MODEL = ""`，而插件（模型与自动调优）
# 在 web.server 导入时就已按已保存设置把模型名写回 config，于是启动顺序变成
# 「插件恢复设置 → Lite 再清空」，用户填好的模型名每次启动都被抹掉，下次启动不再是上次的设置。
#
# 本脚本只在本进程内改内存里的 config（不写任何文件、不启停插件），可反复运行：
#   venv\Scripts\python.exe tests\verify_launcher_model_persist.py
import importlib.util
import os
import sys
from types import SimpleNamespace

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


import core.config as config  # noqa: E402
import launcher  # noqa: E402


def _load_model_plugin():
    """直接加载插件模块（不注册进插件管理器，避免改动插件状态）。"""
    path = os.path.join(ROOT, "plugins", "model_manager.py")
    spec = importlib.util.spec_from_file_location("_mm_plugin_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubServices:
    """替身：不真的启动 GPT-SoVITS。"""

    def start_gpt_sovits(self, cfg):
        return False, {}

    def wait_ready(self, *a, **k):
        pass


class _StubModels:
    def init_models(self):
        pass


mm = _load_model_plugin()
saved = {
    "chat_backend": "openai",
    "judge_backend": "openai",
    "chat_model": "my-external-model",
    "judge_model": "my-judge-model",
    "chat_api_base": "https://example.invalid/v1",
    "chat_api_key": "sk-test",
    "judge_api_base": "https://example.invalid/v1",
    "judge_api_key": "sk-test",
}

# 1) 模拟「下次启动」：插件按已保存设置写入 config（插件在 web.server 导入时加载）
mm._apply(saved, config)
check("插件 on_load 会按已保存设置恢复模型 / 后端",
      config.LLM_CHAT_MODEL == "my-external-model"
      and config.LLM_JUDGE_MODEL == "my-judge-model"
      and config.LLM_CHAT_BACKEND == "openai")

# 2) 随后 Lite 模式启动服务 —— 已配置的模型必须保留
launcher._start_services("lite", _StubServices(), _StubModels(), {})
check("Lite 模式启动后仍保留已配置的生成模型（修复前会被清空）",
      config.LLM_CHAT_MODEL == "my-external-model", f"实际={config.LLM_CHAT_MODEL!r}")
check("Lite 模式启动后仍保留已配置的判断模型",
      config.LLM_JUDGE_MODEL == "my-judge-model", f"实际={config.LLM_JUDGE_MODEL!r}")
check("Lite 模式启动后 API 地址 / Key 未被清空",
      config.LLM_CHAT_API_BASE == "https://example.invalid/v1"
      and config.LLM_CHAT_API_KEY == "sk-test")

# 3) Lite 模式没有本地 Ollama：后端必须是外部 API
config.LLM_CHAT_BACKEND = "ollama"
config.LLM_JUDGE_BACKEND = "ollama"
launcher._start_services("lite", _StubServices(), _StubModels(), {})
check("Lite 模式把 ollama 后端强制为外部 API（openai）",
      config.LLM_CHAT_BACKEND == "openai" and config.LLM_JUDGE_BACKEND == "openai")

# 4) 从未配置过外部模型（仍是 Ollama 默认模型名）时留空，界面显示「未配置」而不是误导性的本机模型名
config.LLM_CHAT_MODEL = config.OLLAMA_MODEL
launcher._start_services("lite", _StubServices(), _StubModels(), {})
check("未配置外部模型时留空（不显示本机 Ollama 模型名）", config.LLM_CHAT_MODEL == "",
      f"实际={config.LLM_CHAT_MODEL!r}")

# 5) 静态检查：不再出现「无条件清空模型名」的写法
with open(os.path.join(ROOT, "launcher.py"), encoding="utf-8") as f:
    launcher_src = f.read()
check("launcher.py 清空模型名的写法被限制在「仍是 Ollama 默认模型名」的条件内",
      "config.LLM_CHAT_MODEL == config.OLLAMA_MODEL" in launcher_src
      and launcher_src.count('config.LLM_CHAT_MODEL = ""') == 1)

# 6) 静态检查：# 惰性 import 的坑——在函数里用 version.xxx 却只在别的函数里导入过
#    （launcher.py 为了先做环境预检才懒加载 core，模块顶层没有 version）
import ast  # noqa: E402

_tree = ast.parse(launcher_src)
_mod_names = set()
for _node in _tree.body:
    if isinstance(_node, (ast.Import, ast.ImportFrom)):
        for _a in _node.names:
            _mod_names.add(_a.asname or _a.name.split(".")[0])
_missing = []
for _fn in [n for n in ast.walk(_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    _local = set()
    for _n in ast.walk(_fn):
        if isinstance(_n, (ast.Import, ast.ImportFrom)):
            for _a in _n.names:
                _local.add(_a.asname or _a.name.split(".")[0])
    _uses = any(isinstance(_n, ast.Name) and _n.id == "version" for _n in ast.walk(_fn))
    if _uses and "version" not in _local and "version" not in _mod_names:
        _missing.append(_fn.name)
check("launcher.py 中引用 version 的函数都已导入 core.version（惰性导入作用域）",
      not _missing, "未导入的函数：" + ", ".join(_missing))

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("模型设置沿用（Lite 模式）验证全部通过。")
