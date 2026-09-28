# tests/run_all.py —— 一次跑完所有验证脚本，输出汇总。
#
# 用法（在仓库根目录执行）：
#   venv\Scripts\python.exe tests\run_all.py            # 跑全部（跳过需要真实窗口的重型脚本）
#   venv\Scripts\python.exe tests\run_all.py --all      # 连真实窗口脚本一起跑（会短暂弹出应用窗口）
#   venv\Scripts\python.exe tests\run_all.py verify_voice_flow verify_music_player   # 只跑指定几个
#
# 约定：每个 verify_*.py 自己打印「通过 N 项，失败 M 项」并以退出码表示成败；
# 本脚本只负责调度与汇总，不会修改任何业务代码或用户数据。
import os
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

# 需要真实打开应用窗口 / 需要独占端口的重型脚本：默认跳过（--all 时执行）
HEAVY = {"verify_real_window_close.py"}


def discover(patterns):
    names = []
    for n in sorted(os.listdir(TESTS_DIR)):
        if not n.startswith("verify_") or not n.endswith(".py"):
            continue
        if patterns and not any(p in n for p in patterns):
            continue
        names.append(n)
    return names


def run(name):
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([PY, os.path.join(TESTS_DIR, name)], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        code = proc.returncode
    except Exception as e:                      # noqa: BLE001
        out, code = f"运行异常：{e}", -1
    summary = ""
    for line in out.splitlines():
        if "通过" in line and "项" in line:
            summary = line.strip()
    fails = [ln.strip() for ln in out.splitlines() if ln.startswith("[FAIL]")]
    return {"name": name, "code": code, "summary": summary, "fails": fails,
            "cost": round(time.time() - t0, 1), "tail": out[-400:]}


def main(argv):
    patterns = [a for a in argv if not a.startswith("-")]
    include_all = "--all" in argv
    names = discover(patterns)
    if not names:
        print("没有找到要跑的验证脚本。")
        return 1

    print(f"共 {len(names)} 个验证脚本（python：{PY}）\n" + "-" * 68)
    results = []
    for name in names:
        if name in HEAVY and not include_all:
            print(f"{name:34s} 跳过（需要真实窗口；加 --all 可执行）")
            continue
        r = run(name)
        results.append(r)
        flag = "OK  " if r["code"] == 0 else "FAIL"
        print(f"{flag} {name:32s} {r['summary'] or '(无汇总输出)'}  [{r['cost']}s]")
        for f in r["fails"][:5]:
            print("      " + f)
        if r["code"] != 0 and not r["fails"]:
            print("      " + r["tail"].replace("\n", "\n      "))

    bad = [r for r in results if r["code"] != 0]
    print("-" * 68)
    print(f"共执行 {len(results)} 个脚本：通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for r in bad:
        print(f"  - {r['name']}（退出码 {r['code']}）{r['summary']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
