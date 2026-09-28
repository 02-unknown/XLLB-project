# verify_settings_hook_persist.py —— 验证「插件对设置的校验 / 规范化会真正落盘」
#
# 背景（本轮修复）：PluginManager.save_settings() 先落盘、再调用插件的 on_settings_changed，
# 且丢弃其返回值，于是插件对设置的规范化（如背景设置切到浅色时清空图片路径、非法模式改回深色、
# 强度限制到 0-1）只作用于界面，不会写进 plugins_settings.json，下次启动又读回未规范化的旧值。
#
# 本脚本会临时改动插件设置文件，结束时按字节原样恢复（与 verify_characters_page.py 同一做法）：
#   venv\Scripts\python.exe tests\verify_settings_hook_persist.py
import io
import json
import os
import sys

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
from core import plugin_manager as pm  # noqa: E402

PATH = config.PLUGINS_SETTINGS_FILE
with open(PATH, "rb") as f:
    ORIGINAL = f.read()
ORIGINAL_JSON = json.loads(ORIGINAL.decode("utf-8"))
manager = pm.manager
NAME = "背景设置"

try:
    # 1) 切到浅色：图片路径必须被插件清空，并且这个清空要落盘
    out = manager.save_settings(NAME, {
        "mode": "light",
        "image": "D:\\some\\stale\\bg.png",
        "strength": 1,
    })
    with open(PATH, "r", encoding="utf-8") as f:
        on_disk = json.load(f).get(NAME, {})
    check("切到浅色后图片路径被清空且写入文件（规范化落盘）",
          on_disk.get("mode") == "light" and on_disk.get("image") == "", str(on_disk))
    check("返回给界面的设置也是规范化后的值",
          out.get("mode") == "light" and out.get("image") == "", str(out))

    # 2) 非法模式回退为深色
    manager.save_settings(NAME, {"mode": "rainbow"})
    with open(PATH, "r", encoding="utf-8") as f:
        on_disk = json.load(f).get(NAME, {})
    check("非法背景模式被改回深色并落盘", on_disk.get("mode") == "dark", str(on_disk))

    # 3) 背景强度被限制在 0-1
    manager.save_settings(NAME, {"strength": 5.5})
    with open(PATH, "r", encoding="utf-8") as f:
        on_disk = json.load(f).get(NAME, {})
    check("背景强度被限制到 0-1 并落盘", on_disk.get("strength") == 1.0, str(on_disk))

    # 4) 只改一个键时，其它插件设置不被牵连
    before_names = set(json.loads(open(PATH, encoding="utf-8").read()).keys())
    check("保存单个插件设置不会影响其它插件的键",
          before_names >= set(ORIGINAL_JSON.keys()) - {NAME} | {NAME}, str(sorted(before_names)))
finally:
    # 原样恢复（字节级），并把内存里的设置重新读回
    with io.open(PATH, "wb") as f:
        f.write(ORIGINAL)
    manager._load_settings()

with open(PATH, "rb") as f:
    restored = f.read()
check("插件设置文件已按字节原样恢复", restored == ORIGINAL)
check("内存里的插件设置与恢复后的文件一致",
      manager._settings.get(NAME, {}).get("mode") == ORIGINAL_JSON.get(NAME, {}).get("mode"))

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("插件设置规范化（落盘）验证全部通过。")
