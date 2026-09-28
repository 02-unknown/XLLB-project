# -*- coding: utf-8 -*-
"""统一前端视觉验收：去表情符号 / 统一内联 SVG 图标 / 统一字号与对齐。

只读校验，不修改任何文件，也不依赖任何第三方库。
用法：venv\\Scripts\\python.exe tests\verify_ui_icons.py
"""
import os
import re
import sys

try:  # 统一 UTF-8 输出：管道捕获时不会因为控制台 GBK 变成乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
STATIC = os.path.join(WEB, "static")

# 本次「统一视觉」涉及的文件
FILES = [
    os.path.join(WEB, "index.html"),
    os.path.join(WEB, "memory_view.html"),
    os.path.join(WEB, "settings.html"),
    os.path.join(STATIC, "app.js"),
    os.path.join(STATIC, "memory_view.js"),
    os.path.join(STATIC, "settings.js"),
]
CSS_PATH = os.path.join(STATIC, "style.css")
ICON_SOURCE = os.path.join(STATIC, "app.js")
ICON_PAGES = [os.path.join(WEB, "index.html"), os.path.join(WEB, "memory_view.html"),
              os.path.join(WEB, "settings.html")]

OKS, FAILS = [], []


def rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def check(desc, ok, detail=""):
    if ok:
        OKS.append(desc)
        print(f"  ✓ {desc}" + (f"（{detail}）" if detail else ""))
    else:
        FAILS.append(desc)
        print(f"  ✗ {desc}" + (f"（{detail}）" if detail else ""))
    return ok


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def code_points(text):
    """把文本里的非 ASCII 符号转成码点描述（避免打印出表情符号导致控制台编码报错）"""
    out = []
    for ch in sorted(set(text), key=ord):
        if ord(ch) > 0x2000:
            out.append(f"U+{ord(ch):04X}")
    return out


# ==================== 1. 表情符号清理 ====================
head("1) 表情符号清理（任务给定正则 + 媒体符号 U+23E9-U+23FA）")

# 任务要求的范围，额外把 U+23E9-U+23FA（播放/暂停/停止/沙漏等媒体表情）也纳入扫描
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u23E9-\u23FA]")
# 允许保留的排版符号（非 emoji）
ALLOWED = set("→←▶✕")

texts = {p: read(p) for p in FILES}
all_clean = True
for path in FILES:
    bad = sorted({c for c in texts[path] if EMOJI_RE.match(c) and c not in ALLOWED})
    if bad:
        all_clean = False
        check(f"{rel(path)} 不含表情符号", False, "残留：" + " ".join(code_points("".join(bad))))
    else:
        check(f"{rel(path)} 不含表情符号", True)

check("六个文件全部清理完毕（无 emoji 码点）", all_clean)

# 点名清理过的字符：逐个确认已消失
BANNED_CHARS = ["\U0001F399", "\U0001F3A4", "\U0001F4AC", "\u2699", "\U0001F3B5", "\U0001F507",
                "\U0001F5D1", "\U0001F50A", "\U0001F310", "\u23ED", "\u23EF", "\u23F3", "\u23F8",
                "\u23F9", "\U0001F9E0", "\u270E", "\u26A0", "\uFF0B"]
leftover = []
for ch in BANNED_CHARS:
    where = [rel(p) for p, t in texts.items() if ch in t]
    if where:
        leftover.append(f"U+{ord(ch):04X}->{where}")
check("主标题麦克风 / 问答 / 实时 / 设置 / 音乐 / 录音 / 停止 / 清空 / 重播 / 联网 / 记忆等表情均已删除",
      not leftover, "; ".join(leftover))

check("settings.js 的两处 ⚠️ 提示已改为「注意：」",
      "\u26A0" not in texts[os.path.join(STATIC, "settings.js")]
      and texts[os.path.join(STATIC, "settings.js")].count("注意：") >= 2)

# ==================== 2. 统一内联 SVG 图标 ====================
head("2) 统一内联 SVG 图标（同一套风格 / 按钮注入 / 渲染 18×18）")

app_js = texts[ICON_SOURCE]
texts[CSS_PATH] = read(CSS_PATH)
css = texts[CSS_PATH]

# 2.1 ICONS 图标集与 setIcon 注入函数
has_icons = "const ICONS = {" in app_js
has_seticon = bool(re.search(r"function setIcon\(el, name\)[\s\S]*?innerHTML = ICONS\[name\]", app_js))
check("app.js 定义统一图标集 ICONS 与注入函数 setIcon()", has_icons and has_seticon)

icon_block = ""
if has_icons:
    icon_block = app_js[app_js.index("const ICONS = {"):]
    icon_block = icon_block[:icon_block.index("function setIcon")]
icon_keys = []
for m in re.finditer(r'(?:"([a-z][a-z-]*)"|([a-z][a-z-]*))\s*:\s*\'<svg', icon_block):
    icon_keys.append(m.group(1) or m.group(2))
check("图标集包含麦克风 / 停止播放 / 清空 / 播放 / 暂停 / 停止 / 重播",
      {"mic", "speaker-off", "trash", "play", "pause", "stop", "speaker"}.issubset(set(icon_keys)),
      "现有：" + ", ".join(icon_keys))

# 2.2 三个必查 id（外加音乐条两个按钮）都套用 .btn.icon 并注入了图标
BUTTON_ICONS = {
    "btn-mic": "mic",
    "btn-stop-speech": "speaker-off",
    "btn-clear": "trash",
    "btn-music-toggle": "play",
    "btn-music-stop": "stop",
}
index_html = texts[os.path.join(WEB, "index.html")]


def icon_injected(btn_id, icon_name):
    """按钮的图标是否由 setIcon() 注入（兼容按播放状态切换图标的三元写法）"""
    for m in re.finditer(r'setIcon\(\s*"%s"\s*,\s*([^)]*)\)' % re.escape(btn_id), app_js):
        if re.search(r"""["']%s["']""" % re.escape(icon_name), m.group(1)):
            return True
    return False


missing_btn, missing_icon = [], []
for btn_id, icon_name in BUTTON_ICONS.items():
    m = re.search(r"<button[^>]*id=\"%s\"[^>]*>" % re.escape(btn_id), index_html)
    cls = ""
    if m:
        cm = re.search(r'class="([^"]*)"', m.group(0))
        cls = cm.group(1) if cm else ""
    if not (m and "btn" in cls.split() and "icon" in cls.split()):
        missing_btn.append(btn_id)
    if not icon_injected(btn_id, icon_name):
        missing_icon.append(btn_id)
check("按钮 btn-mic / btn-stop-speech / btn-clear / 音乐条两个按钮都套用 .btn.icon",
      not missing_btn, "缺失：" + ", ".join(missing_btn) if missing_btn else "5 个按钮样式统一")
check("btn-mic / btn-stop-speech / btn-clear 的图标均由 ICONS 注入（未在 HTML 里写死图标字符）",
      not missing_icon, "缺失：" + ", ".join(missing_icon) if missing_icon else "图标一一对应")
check("动态生成的「重播」按钮复用同一图标集（ICONS.speaker）",
      "ICONS.speaker" in app_js and "重播" in app_js and "\U0001F50A" not in app_js)

# 2.3 所有内联 SVG 的风格属性必须完全一致
svg_tags = []
for path in FILES + [CSS_PATH]:
    for m in re.finditer(r"<svg\b[^>]*>", texts.get(path, read(path))):
        svg_tags.append((rel(path), m.group(0)))
signatures = {}
for where, tag in svg_tags:
    attrs = tuple(sorted(re.findall(r'([a-zA-Z-]+)="([^"]*)"', tag)))
    signatures.setdefault(attrs, []).append(where)
REQUIRED_ATTRS = {"viewBox": "0 0 24 24", "fill": "none", "stroke": "currentColor",
                  "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round"}
style_ok = len(signatures) == 1
if style_ok:
    attrs = dict(next(iter(signatures)))
    style_ok = all(attrs.get(k) == v for k, v in REQUIRED_ATTRS.items())
check("所有内联 SVG 使用同一组风格属性（viewBox 24×24 / fill none / stroke currentColor / stroke-width 1.7 / 圆头圆角）",
      style_ok, f"共 {len(svg_tags)} 个 <svg>，属性组合 {len(signatures)} 种")
check("图标数量覆盖全部按钮（麦克风/停止播放/清空/播放/暂停/停止/重播/音符）",
      len(svg_tags) >= 8, f"实际 {len(svg_tags)} 个")

# 2.4 尺寸规则与布局规则
css_body = re.sub(r"/\*[\s\S]*?\*/", "", css)


def css_rules(selector):
    """取出所有选择器列表里含该选择器的规则体（已去注释）"""
    bodies = []
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css_body):
        sels = [s.strip() for s in m.group(1).split(",")]
        if selector in sels:
            bodies.append(m.group(2))
    return "\n".join(bodies)


btn_icon = css_rules(".btn.icon")
check(".btn.icon 固定尺寸规则（36×36 + padding:0 + 居中不压扁）",
      all(k in btn_icon for k in ("width: 36px", "height: 36px", "padding: 0",
                                  "display: inline-flex", "align-items: center",
                                  "justify-content: center")))

ico_rule_sel = [m.group(1) for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css_body)
                if ".btn.icon svg" in m.group(1) and ".btn .ico" in m.group(1)]
ico_bodies = [m.group(2) for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css_body)
              if ".btn.icon svg" in m.group(1) and ".btn .ico" in m.group(1)]
global_ico = css_rules(".ico")
check("style.css 含 .btn.icon svg / .btn .ico 的 18×18 尺寸规则",
      bool(ico_bodies) and all(k in "".join(ico_bodies) for k in ("width: 18px", "height: 18px", "display: block")),
      "；".join(s.strip() for s in ico_rule_sel))
check("图标位 .ico 在按钮之外（重播 / 音乐条）同样按 18×18 渲染",
      all(k in global_ico for k in ("width: 18px", "height: 18px", "display: block")))

# ==================== 3. 对齐规则 ====================
head("3) 对齐规则（顶栏 / 输入区 / 音乐条 / 图标按钮间距）")

for sel, name in ((".topbar", "顶栏"), (".composer", "输入区"), (".music-controls", "音乐条按钮组")):
    body = css_rules(sel)
    check(f"{sel} 声明 align-items: center（{name}垂直居中）", "align-items: center" in body)

check("输入区按钮与文本框居中对齐（.composer 不再用 flex-end）",
      "align-items: center" in css_rules(".composer") and "flex-end" not in css_rules(".composer"))
check("图标按钮间距统一 8px（输入区 / 音乐条）",
      "gap: 8px" in css_rules(".composer") and "gap: 8px" in css_rules(".music-controls"))
check("模式切换器与「设置」按钮等高（32px 控件刻度：--ctl-h）",
      "min-height: calc(var(--ctl-h) - 2px)" in css_rules(".mode-toggle .mode-btn")
      and "min-height: var(--ctl-h)" in css_rules(".btn"))

# ==================== 4. 字号 / 行高统一 ====================
head("4) 字号与行高统一（只用 :root 里的 --fs-* 变量）")

for var in ("--fs-xs", "--fs-sm", "--fs-md", "--fs-base", "--fs-lg"):
    check(f"style.css 顶部定义刻度变量 {var}", re.search(r"%s:\s*\d+px;" % re.escape(var), css_body) is not None)

used_vars = sorted(set(re.findall(r"font-size:\s*var\((--fs-[a-z]+)\)", css_body)))
check("所有字号都引用 --fs-* 变量", len(used_vars) >= 4, "用到：" + ", ".join(used_vars))

print("       硬编码 font-size 扫描结果（任务涉及的全部文件）：")
hard_bad = []
for path in FILES + [CSS_PATH]:
    text = texts.get(path) or read(path)
    hits = []
    for m in re.finditer(r"font-size:\s*([^;]+);", text):
        val = m.group(1).strip()
        if "px" in val:
            line = text[:m.start()].count("\n") + 1
            line_text = text.splitlines()[line - 1]
            # 白名单：品牌 logo 字号（图标/emoji 尺寸例外）
            ok = ".brand .logo" in line_text
            hits.append((val, line, ok, line_text.strip()))
            if not ok:
                hard_bad.append(f"{rel(path)}:{line} {val}")
    if hits:
        for val, line, ok, line_text in hits:
            flag = "白名单" if ok else "违规"
            print(f"         {rel(path)}:{line}  font-size: {val}  [{flag}] {line_text}")
    else:
        print(f"         {rel(path)}  无硬编码 px 字号（全部来自变量）")
check("本次涉及文件不再有硬编码 font-size: Npx（白名单：.brand .logo 图标尺寸）", not hard_bad,
      "；".join(hard_bad))

line_heights = sorted(set(v.strip() for v in re.findall(r"line-height:\s*([^;]+);", css_body)))
ALLOWED_LH = {"1", "1.3", "1.6", "1.65", "inherit", "var(--ctl-h)"}
check("行高统一为 1.6 / 1.65（例外仅：胶囊徽章 1、品牌标题 1.3、控件高度）",
      set(line_heights).issubset(ALLOWED_LH), "现有行高：" + ", ".join(line_heights))
check("正文类文本的行高已收敛到 1.6~1.65（无 1.7x 旧值）",
      not any(re.match(r"^1\.[7-9]", v) or re.match(r"^[2-9]", v) for v in line_heights))

# 层级检查：页面标题 --fs-lg + 600、小节标题 --fs-sm、说明文字 --fs-sm + --muted
check("页面标题层级（.brand .title / .sm-title 用 --fs-lg + 600 字重）",
      "font-size: var(--fs-lg)" in css_rules(".brand .title")
      and "font-weight: 600" in css_rules(".brand .title")
      and "font-size: var(--fs-lg)" in css_rules(".settings-main-head .sm-title"))
check("小节标题层级（记忆管理 h2 用 --fs-sm + --muted）",
      "font-size: var(--fs-sm)" in css_rules(".mv-sec > h2")
      and "color: var(--muted)" in css_rules(".mv-sec > h2"))
check("正文层级（记忆条目卡片 .mv-item 用 --fs-base，卡片内 meta 为 --fs-xs 次要信息）",
      "font-size: var(--fs-base)" in css_rules(".mv-item")
      and "font-size: var(--fs-xs)" in css_rules(".mv-item .meta"))
check("说明文字层级（欢迎语 --fs-sm + --muted）",
      "font-size: var(--fs-sm)" in css_rules(".welcome")
      and "color: var(--muted)" in css_rules(".welcome"))
check("按钮文字层级（.btn 用 --fs-base）", "font-size: var(--fs-base)" in css_rules(".btn"))

# ==================== 5. 无外链图标库 / 字体 ====================
head("5) 不引入任何外链图标库 / 图标字体")
EXT_RE = re.compile(r"@import|@font-face|iconfont|cdn\.|unpkg|jsdelivr|googleapis", re.I)
ext_bad = [rel(p) for p in FILES + [CSS_PATH] if EXT_RE.search(texts.get(p) or read(p))]
check("六个文件均无外链图标库 / 图标字体（图标全部内联 SVG）", not ext_bad, "；".join(ext_bad))

# ==================== 汇总 ====================
print()
print("=" * 72)
print(f"通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  - " + f)
    print("验证结论: ✗ 存在未通过项")
    sys.exit(1)
print("验证结论: ✓ 全部通过")
