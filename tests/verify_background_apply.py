# verify_background_apply.py —— 验证「自定义背景图片：改路径 → 保存 → 真正生效」整条链路。
#
# 背景（本轮修复）：用户从「复制文件地址」/聊天窗口粘贴的路径常带引号（"D:\a.jpg" 或 “D:\a.jpg”），
# 又或带 file:/// 前缀，直接判断会当成文件不存在 → /api/background/image 返回 404、界面不显示这张图，
# 表现为「改了路径并保存但没有生效」；此外背景图地址固定不变，若响应带 max-age 浏览器会继续用旧图。
#
# 本脚本会临时改动插件设置文件，结束时按字节原样恢复：
#   venv\Scripts\python.exe tests\verify_background_apply.py
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request

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
import core.paths as paths  # noqa: E402
from core import plugin_manager as pm  # noqa: E402
from web import server as web_server  # noqa: E402

SETTINGS = config.PLUGINS_SETTINGS_FILE
with open(SETTINGS, "rb") as f:
    ORIGINAL = f.read()
ORIGINAL_BG = json.loads(ORIGINAL.decode("utf-8")).get("背景设置", {})

IMG1 = os.path.join(ROOT, "111.png")
IMG2 = os.path.join(ROOT, "web", "static", "app-icon.png")

# ==================== 1. 路径规范化 ====================
check("ASCII 引号路径被清理", paths.normalize_user_path(f'"{IMG1}"') == IMG1)
check("中文引号路径被清理", paths.normalize_user_path(f"“{IMG1}”") == IMG1)
check("单引号 / 前后空白被清理", paths.normalize_user_path(f"  '{IMG1}'  ") == IMG1)
check("嵌套两层引号也能清理", paths.normalize_user_path(f'"“{IMG1}”"') == IMG1)
check("file:/// 本地地址被还原为路径",
      paths.normalize_user_path("file:///" + IMG1.replace("\\", "/").lstrip("/")) == IMG1.replace("\\", "/"))
check("普通路径原样保留", paths.normalize_user_path(IMG1) == IMG1)
check("空值 / None 安全返回空串",
      paths.normalize_user_path(None) == "" and paths.normalize_user_path("   ") == "")
check("is_existing_file 对带引号的真实文件判定为存在",
      paths.is_existing_file(f"“{IMG1}”") and not paths.is_existing_file(f"“{IMG1}.missing”"))

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def req(path, method="GET", body=None, extra=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-XLLB-Client": "bg-verify", "Content-Type": "application/json"}
    headers.update(extra or {})
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def save(**settings):
    st, b, _ = req("/api/plugins/settings", "POST", {"name": "背景设置", "settings": settings})
    return st, json.loads(b.decode("utf-8"))


try:
    # ==================== 2. 保存带引号的路径 ====================
    st, r = save(mode="image", image=f"“{IMG1}”", strength=0.9)
    stored = pm.manager.get_settings("背景设置")
    check("保存带中文引号的路径：接口成功", st == 200 and r.get("ok"), str(r)[:160])
    check("保存后存储值已去掉引号（界面再打开就是干净路径）",
          stored.get("image") == IMG1, str(stored))
    with open(SETTINGS, encoding="utf-8") as f:
        on_disk = json.load(f).get("背景设置", {})
    check("去引号后的路径已落盘", on_disk.get("image") == IMG1, str(on_disk))

    status, body, headers = req("/api/background/image")
    check("保存后能取到背景图（200 且内容一致）",
          status == 200 and body == open(IMG1, "rb").read(), f"status={status} len={len(body)}")
    check("背景图响应带 ETag", bool(headers.get("ETag")))
    check("背景图不再给 max-age（换图后不会被浏览器缓存挡住）",
          "max-age" not in (headers.get("Cache-Control") or ""), headers.get("Cache-Control"))

    # ==================== 3. 修复前已经存下的带引号路径也要能用 ====================
    with pm.manager._lock:
        pm.manager._settings.setdefault("背景设置", {})["image"] = f"“{IMG1}”"
    status, body, _ = req("/api/background/image")
    check("历史上已存的带引号路径无需重存也能取到图（解析时同样清理引号）",
          status == 200 and body == open(IMG1, "rb").read(), f"status={status}")

    # ==================== 4. 换图：内容与 ETag 都要变 ====================
    st, r = save(mode="image", image=IMG2, strength=0.9)
    etag1 = headers.get("ETag")
    status, body, headers2 = req("/api/background/image", extra={"If-None-Match": etag1 or ""})
    check("换成另一张图后返回新图内容",
          status == 200 and body == open(IMG2, "rb").read(), f"status={status} len={len(body)}")
    check("换图后 ETag 变化（旧缓存不会命中 304）", headers2.get("ETag") != etag1)
    status, body, _ = req("/api/background/image", extra={"If-None-Match": headers2.get("ETag") or ""})
    check("同一张图重复请求仍走 304（不重传整张图）", status == 304 and body == b"")

    # ==================== 5. 文件不存在的路径：保存成功但接口明确报错 ====================
    missing = os.path.join(ROOT, "runtime", "_no_such_image.png")
    st, r = save(mode="image", image=f'"{missing}"', strength=0.9)
    status, body, _ = req("/api/background/image")
    check("不存在的本地图片返回 404 且说明原因（不再静默无图）",
          status == 404 and "不存在" in body.decode("utf-8"), f"status={status}")

    # ==================== 6. 运行状态：填了路径却不生效时能一眼看出 ====================
    st, b, _ = req("/api/plugins/state?name=" + urllib.parse.quote("背景设置"))
    state = json.loads(b.decode("utf-8")).get("state") or {}
    titles = " ".join(q.get("title", "") for q in (state.get("queue") or []))
    check("「运行状态」会提示本地图片不存在", "不存在" in titles, titles[:200])
    save(mode="image", image=IMG1, strength=0.9)
    st, b, _ = req("/api/plugins/state?name=" + urllib.parse.quote("背景设置"))
    titles = " ".join(q.get("title", "") for q in (json.loads(b.decode("utf-8")).get("state") or {}).get("queue") or [])
    check("「运行状态」会确认本地图片可用", "本地图片可用" in titles, titles[:200])

    # ==================== 7. 切回浅色 / 深色时清空图片路径 ====================
    save(mode="light", image=IMG1, strength=0.9)
    check("切到浅色后图片路径被清空", pm.manager.get_settings("背景设置").get("image") == "",
          str(pm.manager.get_settings("背景设置")))
finally:
    with io.open(SETTINGS, "wb") as f:
        f.write(ORIGINAL)
    pm.manager._load_settings()
    srv.shutdown()

with open(SETTINGS, "rb") as f:
    restored = f.read()
check("插件设置文件已按字节原样恢复", restored == ORIGINAL)
check("内存设置与恢复后的文件一致",
      pm.manager.get_settings("背景设置").get("mode") == ORIGINAL_BG.get("mode"))

print()
if FAILS:
    print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项：")
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print(f"通过 {len(OKS)} 项，失败 0 项")
print("背景图片路径保存 / 生效 验证全部通过。")
