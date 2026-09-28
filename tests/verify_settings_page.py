# verify_settings_page.py —— 统一「设置」页验证（结构 / 静态资源 / 接口 / 前后端 id 对齐）
#
# 只做只读检查（不修改任何设置与插件状态），可反复运行：
#   venv\Scripts\python.exe tests\verify_settings_page.py
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
STATIC = os.path.join(WEB, "static")

FAILS = []
OKS = []


def check(name, cond, extra=""):
    if cond:
        OKS.append(name)
        print(f"[OK]   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"[FAIL] {name} {extra}")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def ids_in_html(text):
    return set(re.findall(r'id="([^"]+)"', text))


def ids_assigned_in_js(text):
    found = set(re.findall(r'\.id\s*=\s*[`"\']([^`"\'$]+)[`"\']', text))
    found |= set(re.findall(r'id="([^"$]+)"', text))          # 模板字符串里的 id="..."
    found |= set(re.findall(r"id='([^'$]+)'", text))
    return found


def dollar_ids(text):
    return set(re.findall(r'\$\("([^"]+)"\)', text))


# ==================== 1. 结构：文件与入口 ====================
settings_html = read(os.path.join(WEB, "settings.html"))
settings_js = read(os.path.join(STATIC, "settings.js"))
memory_js = read(os.path.join(STATIC, "memory_view.js"))
memory_html = read(os.path.join(WEB, "memory_view.html"))
index_html = read(os.path.join(WEB, "index.html"))
app_js = read(os.path.join(STATIC, "app.js"))
plugins_html = read(os.path.join(WEB, "plugins.html"))

check("web/settings.html 存在且含左侧标签栏 / 右侧内容区",
      'id="settings-nav"' in settings_html and 'id="settings-body"' in settings_html
      and 'id="settings-tab-title"' in settings_html)
check("settings.html 引用了 settings.js", "/static/settings.js" in settings_html)
check("首页只保留一个「设置」入口（无插件管理入口、无内嵌设置侧栏）",
      "/settings.html" in index_html and "/plugins.html" not in index_html
      and 'id="settings-pane"' not in index_html and "settings-pane" not in index_html)
check("app.js 不再包含旧设置侧栏逻辑（本轮新增的是浮层函数 openSettingsOverlay 等，不算旧侧栏）",
      not re.search(r"settings-pane|loadSettings|loadHistory|loadVoicePresets|loadCharacters"
                    r"|\bopenSettings\b|\bcloseSettings\b|refreshPluginState", app_js))
check("plugins.html 已改为跳转到设置页", "/settings.html#plugins" in plugins_html)
check("旧 plugins.js 已删除", not os.path.exists(os.path.join(STATIC, "plugins.js")))
check("角色管理入口已合并到设置页（旧页面变跳转页、独立脚本已删除）",
      'href="/plugins.html"' not in read(os.path.join(WEB, "characters.html"))
      and "/settings.html#characters" in read(os.path.join(WEB, "characters.html"))
      and not os.path.exists(os.path.join(STATIC, "characters.js"))
      and 'href="/plugins.html"' not in read(os.path.join(WEB, "memory_view.html"))
      and "/settings.html" in read(os.path.join(WEB, "memory_view.html")))

# id 对齐：settings.js / app.js 里 $("...") 引用的 id 必须存在（HTML 或同文件内动态创建）
s_ids = ids_in_html(settings_html) | ids_assigned_in_js(settings_js)
missing = sorted(i for i in dollar_ids(settings_js) if i not in s_ids)
check("settings.js 的 $() id 全部有来源", not missing, f"缺失: {missing}")

a_ids = ids_in_html(index_html) | ids_assigned_in_js(app_js)
missing_a = sorted(i for i in dollar_ids(app_js) if i not in a_ids)
check("app.js 的 $() id 全部有来源", not missing_a, f"缺失: {missing_a}")

# 设置页样式与主题适配
css = read(os.path.join(STATIC, "style.css"))
check("style.css 已删除旧 settings-pane 样式并包含新设置页样式",
      "settings-pane" not in css and ".settings-nav-item" in css and ".set-row" in css)
check("全站字体统一（表单控件继承字体，不再回退系统默认）",
      "button, input, select, textarea" in css and "font-family: inherit" in css)
check("左侧标签栏为纯文字（无图标元素）",
      "sn-icon" not in settings_js and "sn-icon" not in css)
check("全站共用统一页面骨架（.page / .page-inner）与控件样式（.input/.select/.textarea）",
      ".page-inner" in css and ".input, .select, .textarea" in css)
check("图片主题在 body 级统一定义半透明 surface（所有页面跟随背景主题）",
      bool(re.search(r"body\.theme-image \{[\s\S]*?--surface: rgba", css)))


def _no_inline_style(name):
    html = read(os.path.join(WEB, name))
    return "<style" not in html and 'class="page"' in html and "page-inner" in html


check("角色管理页 / 记忆管理页 / 跳转页使用统一骨架且无内联样式",
      _no_inline_style("characters.html") and _no_inline_style("memory_view.html")
      and _no_inline_style("plugins.html"))


__img_block = re.search(r"/\* 图片模式：所有页面外壳[\s\S]*?/\* 图片模式下：顶栏", css)
check("图片主题下设置页不做大面积背景模糊（避免掉帧）",
      bool(__img_block) and "backdrop-filter:" not in __img_block.group(0))
__blur_block = re.search(r"/\* 图片模式下：仅聊天区少量元素保留磨砂玻璃[\s\S]*?\n\}", css)
__css_rules = re.sub(r"/\*[\s\S]*?\*/", "", css).split("}")
check("设置页 / 记忆页的列表卡片不再叠加背景模糊（长列表滚动不掉帧）",
      bool(__blur_block) and not any(
          any(k in r for k in (".plugin-item", ".history-item", ".mv-item", ".mv-form", ".mv-spec", ".role-item"))
          and "backdrop-filter" in r for r in __css_rules))
check("记忆条目使用 content-visibility 优化长列表渲染",
      bool(re.search(r"\.mv-item \{[^}]*content-visibility: auto", css)))
server_src = read(os.path.join(WEB, "server.py"))
check("背景图支持浏览器缓存（ETag + 304 校验）",
      "_bg_image_etag" in server_src and "If-None-Match" in server_src and "304" in server_src)

# ==================== 2. 插件标签钩子（Plugin.settings_tab） ====================
sys.path.insert(0, ROOT)
from core import plugin_manager as pm  # noqa: E402
from types import SimpleNamespace  # noqa: E402

p_dict = pm.Plugin("字典标签", "x.py", SimpleNamespace(SETTINGS_TAB={"label": "音乐", "icon": "🎵", "order": "55", "type": "page", "page": "/m.html"}))
tab = p_dict.settings_tab(None)
check("SETTINGS_TAB 字典被正确解析",
      tab == {"label": "音乐", "icon": "🎵", "page": "/m.html", "type": "page", "order": 55}, str(tab))

p_fn = pm.Plugin("函数标签", "y.py", SimpleNamespace(settings_tab=lambda ctx: {"label": "翻译", "type": "?", "order": "abc"}))
tab2 = p_fn.settings_tab(None)
check("settings_tab(ctx) 函数钩子优先且字段被清洗",
      tab2.get("label") == "翻译" and tab2.get("type") == "inline" and tab2.get("order") == 60, str(tab2))

p_fn_bad = pm.Plugin("异常标签", "z.py", SimpleNamespace(settings_tab=lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))))
check("settings_tab 抛异常时安全降级为空", p_fn_bad.settings_tab(None) == {})

plugins = pm.manager.list_plugins()
check("list_plugins() 每项都带 settings_tab 字段", all("settings_tab" in p for p in plugins))
setting_plugins = [p["name"] for p in plugins if p["settings_schema"] or p["actions"] or p["has_state"]]
print(f"       有设置/动作/状态的插件（将生成标签）：{setting_plugins}")

# ==================== 3. HTTP 层（真实起服务，只读接口） ====================
from web import server as web_server  # noqa: E402

# 主题内联脚本必须是合法 JS（否则首帧不跟随背景主题）
__theme_html = web_server._theme_inline_script()
__theme_m = re.search(r"<script>([\s\S]*?)</script>", __theme_html)
__theme_tmp = os.path.join(ROOT, "runtime", "_theme_inline_check.js")
__theme_js = ("var window={},document={body:{classList:{add:function(){}},"
              "style:{setProperty:function(){}}}};\nfunction Image(){}\n")
if __theme_m:
    with open(__theme_tmp, "w", encoding="utf-8") as f:
        f.write(__theme_js + __theme_m.group(1))
    __theme_rc = subprocess.call(["node", "--check", __theme_tmp])
    try:
        os.remove(__theme_tmp)
    except OSError:
        pass
else:
    __theme_rc = 1
check("背景主题内联脚本语法正确（进入页面首帧即跟随主题）", __theme_rc == 0)
check("内联脚本按配置的主题分支应用（light / image / dark）",
      "theme-light" in __theme_html and "theme-image" in __theme_html)

# ==================== 2.5 主题变量 / 版本号 / 图标（本轮修复项） ====================
style_css = read(os.path.join(STATIC, "style.css"))
_root_block = re.search(r":root\s*\{([\s\S]*?)\}", style_css)
_light_block = re.search(r"body\.theme-light\s*\{([\s\S]*?)\}", style_css)
check(":root 的 --surface/--surface-2 用具体颜色（写成 var(--panel) 会被 :root 解析成深色后被继承）",
      bool(_root_block) and "--surface: #" in _root_block.group(1)
      and "--surface-2: #" in _root_block.group(1))
check("浅色主题显式覆盖 --surface / --surface-2（修复浅色下左侧栏与输入框仍是深色）",
      bool(_light_block) and "--surface:" in _light_block.group(1)
      and "--surface-2:" in _light_block.group(1))
check("主题类互斥：内联脚本先移除另一个主题类再按配置添加",
      "remove('theme-light','theme-image')" in __theme_html.replace('"', "'"))

_version_file = os.path.join(ROOT, "version.txt")
check("version.txt 存在且含版本号（版本号只写在这一个文件里）",
      os.path.exists(_version_file) and read(_version_file).strip() != "")
from core import version as version_mod  # noqa: E402
_ver = version_mod.read_version()
check(f"core.version 能从文件读到版本号（当前：{_ver}）", _ver not in ("", "unknown"))
_server_py = read(os.path.join(WEB, "server.py"))
check("web/server.py 不再写死版本号（服务标识来自 core.version）",
      "version.server_token()" in _server_py and not re.search(r'XiaolongluoWebUI/\d', _server_py))
check("启动入口不再写死版本号（app.py / launcher.py 用 version.version_label()）",
      "version.version_label()" in read(os.path.join(ROOT, "app.py"))
      and "version.version_label()" in read(os.path.join(ROOT, "launcher.py")))
check("start.bat 从 version.txt 读取版本号", "version.txt" in read(os.path.join(ROOT, "start.bat")))
check("页面标题不再带版本号（图1 处不再显示版本号）",
      all(re.search(r"<title>[^<]*\d+\.\d+", t) is None
          for t in (index_html, settings_html, plugins_html, memory_html,
                    read(os.path.join(WEB, "characters.html")))))
check("「通用设置」显示版本号（来自接口 settings.version）",
      'row("版本"' in settings_js and "st.version" in settings_js)
_all_pages = (index_html, settings_html, plugins_html, memory_html,
              read(os.path.join(WEB, "characters.html")))
check("所有页面都引用应用图标（窗口 / 任务栏图标）",
      all("app-icon.png" in t for t in _all_pages))
check("应用图标文件存在（app-icon.png + favicon.ico）",
      os.path.exists(os.path.join(STATIC, "app-icon.png"))
      and os.path.exists(os.path.join(STATIC, "favicon.ico")))
check("外部 API 报错不再限定某家服务的模型名（已无 DeepSeek 专用提示）",
      "deepseek" not in read(os.path.join(ROOT, "core", "llm.py")).lower()
      and "deepseek" not in read(os.path.join(ROOT, "plugins", "model_manager.py")).lower())

# 插件列表标签位：取消「官方 / 第三方」标签，改显示插件作者（现有插件统一签名 02）
check("插件列表不再使用「官方 / 第三方」标签（标签位改为作者名）",
      '"p-badge author"' in settings_js and "p.official" not in settings_js)
check("样式表里官方 / 第三方标签样式已替换为作者样式",
      ".p-badge.author" in style_css and ".p-badge.official" not in style_css
      and ".p-badge.third" not in style_css)
_plugin_dir = os.path.join(ROOT, "plugins")
_plugin_files = sorted(f for f in os.listdir(_plugin_dir) if f.endswith(".py"))
_unsigned = [f for f in _plugin_files if 'AUTHOR = "02"' not in read(os.path.join(_plugin_dir, f))]
check(f"现有插件全部签名为 02（共 {len(_plugin_files)} 个）", not _unsigned, str(_unsigned))

srv = web_server.create_server("127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


try:
    status, body, ctype = get("/settings.html")
    text = body.decode("utf-8")
    check("GET /settings.html 可访问且注入了背景主题", status == 200 and 'id="settings-nav"' in text and "__THEME__" in text)
    check("主题脚本注入在 <body> 起始处（内容解析前应用，首帧不闪）",
          text.index("__THEME__") < text.index('id="settings-nav"'))

    # 设置页确实带上当前配置的主题模式（跟随「背景设置」插件）
    plist_now = json.loads(get("/api/plugins")[1].decode("utf-8")).get("plugins", [])
    bg_mode = next((p["settings"].get("mode") for p in plist_now if p["name"] == "背景设置"), "dark")
    check(f"设置页 HTML 注入的主题模式与配置一致（{bg_mode}）",
          f'"mode": "{bg_mode}"' in text.replace("'", '"'))

    # 插件接口每项都带作者字段（界面标签位用它显示作者名，作者为空则留白）
    check("插件接口每项都带作者字段（供标签位显示）",
          bool(plist_now) and all((p.get("author") or "").strip() for p in plist_now),
          str([(p.get("name"), p.get("author")) for p in plist_now[:4]]))

    for page, marker in (("/characters.html", "/settings.html#characters"), ("/memory_view.html", "/settings.html#plugin:"),
                         ("/plugins.html", "/settings.html#plugins")):
        st2, bd2, _ = get(page)
        txt2 = bd2.decode("utf-8")
        check(f"GET {page} 可访问且使用统一页面骨架",
              st2 == 200 and marker in txt2 and 'class="page"' in txt2 and "<style" not in txt2)

    # 角色与语音：标签已合并（voice 标签消失、旧 #voice 链接被别名到 characters）
    check("设置页标签已合并为「角色与语音」（不再有独立语音标签）",
          '"characters", label: "角色与语音"' in settings_js.replace("'", '"')
          and 'key: "voice"' not in settings_js)
    check("旧 #voice 链接被别名到角色与语音", "voice: \"characters\"" in settings_js)
    check("多人对话区按插件可用性握手渲染（available / can_edit）",
          "available" in settings_js and "plugin_enabled" in settings_js and "role-item" in settings_js)
    # 相同功能只保留一种设置方式：顶部重复的「当前角色 / 自定义角色」表单已移除
    check("已移除与角色卡片重复的「当前角色 / 自定义角色」表单",
          'section("当前角色"' not in settings_js and '"自定义角色"' not in settings_js
          and '"加载预设"' not in settings_js and '"应用自定义"' not in settings_js)
    check("角色卡片支持分类详情 + 收起（role-detail / 展开详情）",
          "role-detail" in settings_js and "展开详情" in settings_js and "detailRow" in settings_js)
    check("角色卡片提供「修改设定」与「LLM 优化」入口",
          "修改设定" in settings_js and "LLM 优化" in settings_js
          and "/api/characters/preset/save" in settings_js
          and "/api/characters/preset/optimize" in settings_js)
    check("角色卡片可新建角色 / 设为当前角色",
          "新建角色" in settings_js and "设为当前" in settings_js)
    # 插件设置与自带设置的区分（左侧分隔线 + 右侧「插件」标记）
    check("插件设置分组与自带设置已有视觉区分（分隔线 + 插件标记）",
          "plugin-group" in settings_js and ".settings-nav-group.plugin-group" in css
          and "sm-badge" in settings_js and ".settings-main-head .sm-badge" in css)
    # 插件动作改为成行排版（不再用等宽拉伸按钮，避免长文案折行错位）
    check("插件动作按「标签 + 说明 — 按钮」成行排版",
          'el("div", "btn-row")' not in settings_js)
    # 浮层窗口：插件动作可通过 {"modal": ...} 打开注册的浮层（记忆管理不再单独开页面）
    check("插件动作支持打开浮层窗口（openPluginModal / XLLB_MODALS）",
          "openPluginModal" in settings_js and "r.modal" in settings_js
          and "XLLB_MODALS" in settings_js)
    check("设置页加载了记忆管理浮层脚本",
          "/static/memory_view.js" in settings_html and "memory_manager" in memory_js)
    check("记忆管理已是浮层窗口（不再有自己的页面骨架）",
          "memory-modal" in memory_js and "openMemoryManager" in memory_js
          and "memory-modal-body" in memory_js)
    check("记忆管理旧页面已改为跳转页", "/settings.html#plugin:" in memory_html)
    # 新建角色对话框：角色名可输入 + 显眼的联网搜索按钮
    check("新建角色对话框按选项对象调用（create 模式生效）",
          "openRoleDialog({ create: true })" in settings_js
          and "function openRoleDialog(opts = {})" in settings_js)
    check("对话框提供显眼的「联网搜索补全设定」按钮",
          "联网搜索补全设定" in settings_js and "/api/characters/preset/reference" in settings_js
          and ".role-dialog-tools" in css)

    status, body, _ = get("/")
    check("GET / 首页无设置侧栏", status == 200 and "settings-pane" not in body.decode("utf-8"))

    status, body, ctype = get("/plugins.html")
    check("GET /plugins.html 返回跳转页", status == 200 and "/settings.html#plugins" in body.decode("utf-8"))

    status, body, ctype = get("/static/settings.js")
    check("GET /static/settings.js 可访问", status == 200 and "javascript" in ctype, ctype)

    for path in ("/api/settings", "/api/characters", "/api/voice_presets", "/api/history",
                 "/api/plugins", "/api/characters/manage", "/api/models"):
        status, body, _ = get(path)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            check(f"GET {path} 返回 JSON", False, str(e))
            continue
        check(f"GET {path} 返回 JSON 且 ok", status == 200 and data.get("ok") is True)

    data = json.loads(get("/api/plugins")[1].decode("utf-8"))
    plist = data.get("plugins", [])
    check("接口返回的插件都带 settings_tab", all("settings_tab" in p for p in plist))

    # 记忆管理页：分页 + 统计（数据多时不再一次性返回全库 / 渲染整页）
    mv = json.loads(get("/api/memory/view?limit=20")[1].decode("utf-8"))
    check("记忆查看接口支持分页并返回总数（limit / totals / has_more）",
          all(k in mv for k in ("limit", "offset", "totals", "has_more")) and mv.get("limit") == 20,
          str({k: mv.get(k) for k in ("limit", "totals", "has_more")}))
    check("角色与语音接口返回分类字段与握手信息",
          all(k in json.loads(get("/api/characters/manage")[1].decode("utf-8"))
              for k in ("presets", "available", "availability", "can_edit", "min_roles")))
    check("停用插件也会返回设置（供标签页展示）",
          all("settings" in p for p in plist))

    st = json.loads(get("/api/settings")[1].decode("utf-8"))["settings"]
    check("设置快照含设置页所需字段",
          all(k in st for k in ("character_name", "current_voice_name", "influence_min", "influence_max",
                                "tts_volume", "music_volume", "internet_enabled", "debug_mode", "tavily")))

    try:
        get("/static/plugins.js")
        check("旧 /static/plugins.js 已下线", False, "仍可访问")
    except urllib.error.HTTPError as e:
        check("旧 /static/plugins.js 已下线", e.code == 404)

    # 背景图缓存：第二次带 If-None-Match 应得 304（进页面不再重传整张图）
    try:
        with urllib.request.urlopen(base + "/api/background/image", timeout=10) as r:
            img_etag = r.headers.get("ETag")
            img_cc = r.headers.get("Cache-Control", "")
            img_len = len(r.read())
        check("背景图响应带 ETag 且每次都校验（no-cache：换图后不会被旧缓存挡住）",
              bool(img_etag) and "no-cache" in img_cc and "max-age" not in img_cc,
              f"ETag={img_etag} CC={img_cc}")
        req = urllib.request.Request(base + "/api/background/image")
        req.add_header("If-None-Match", img_etag)
        try:
            with urllib.request.urlopen(req, timeout=10) as r2:
                check("背景图命中缓存返回 304", r2.status == 304, f"状态 {r2.status}")
        except urllib.error.HTTPError as e:
            check("背景图命中缓存返回 304", e.code == 304, f"状态 {e.code}")
    except urllib.error.HTTPError as e:
        # 用户当前主题不是「本地图片」时没有背景图，跳过该项（不算失败）
        print(f"[SKIP] 背景图缓存检查（无本地背景图：HTTP {e.code}）")

    # ==================== 4. 渲染冒烟测试（真实接口数据 + 极简 DOM 桩） ====================
    fixture = {
        "settings": st,
        "characters": json.loads(get("/api/characters")[1].decode("utf-8")).get("presets", []),
        "voices": json.loads(get("/api/voice_presets")[1].decode("utf-8")).get("presets", []),
        "history": json.loads(get("/api/history")[1].decode("utf-8")).get("records", []),
        "plugins": json.loads(get("/api/plugins")[1].decode("utf-8")).get("plugins", []),
        "models": json.loads(get("/api/models")[1].decode("utf-8")).get("ollama_models", []),
        "manage": json.loads(get("/api/characters/manage")[1].decode("utf-8")),
    }
finally:
    srv.shutdown()
    srv.server_close()

fixture_path = os.path.join(ROOT, "runtime", "_settings_fixture.json")
os.makedirs(os.path.dirname(fixture_path), exist_ok=True)
with open(fixture_path, "w", encoding="utf-8") as f:
    json.dump(fixture, f, ensure_ascii=False)

render_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_settings_render.js")
print()
print("---- 设置页渲染冒烟测试（node + DOM 桩） ----")
try:
    # 继承标准输出，避免管道（沙箱下子进程管道可能被拒绝）
    rc = subprocess.call(["node", render_js, fixture_path], cwd=ROOT)
    check("settings.js 渲染冒烟测试通过", rc == 0, f"退出码 {rc}")
    # 嵌入模式（主页设置浮层）：返回链接改成关闭浮层、保存后把音量回传给主页
    embed_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_settings_embed.js")
    rc2 = subprocess.call(["node", embed_js], cwd=ROOT)
    check("设置页嵌入模式（浮层）行为测试通过", rc2 == 0, f"退出码 {rc2}")
finally:
    try:
        os.remove(fixture_path)
    except OSError:
        pass

# ==================== 汇总 ====================
print()
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("统一设置页验证全部通过。")
