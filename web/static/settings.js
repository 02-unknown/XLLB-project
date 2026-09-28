/* 小笼洛包 · 统一「设置」页前端逻辑
 *
 * 结构：左侧分类标签（内置分类 + 插件声明的标签），右侧展示该标签下的具体内容。
 * 约定：
 *   - 所有参数设置（含插件参数）都集中在本页；
 *   - 「插件管理」标签只保留插件的启用 / 停用 / 重新加载能力；
 *   - 插件可声明 SETTINGS_TAB / settings_tab(ctx) 在左侧栏新增自己的标签
 *     （type="inline" 渲染设置表单；type="page" 给出插件自带页面的入口）。
 */
"use strict";

const $ = (id) => document.getElementById(id);

/* 是否被主页的「设置浮层」用 iframe 嵌入（?embed=1）。
 * 嵌入时的两点不同：
 *   1) 「返回对话」变成「关闭浮层」，不做页面跳转；
 *   2) 保存设置后把结果回传给主页，让音量 / 主题即时生效（主页始终没被卸载，
 *      所以调音量时正在播放的音乐与语音合成不会中断）。 */
const EMBEDDED = (() => {
  try { return window.self !== window.top; } catch (e) { return true; }
})();

function postToHost(msg) {
  if (!EMBEDDED) return;
  try { window.parent.postMessage(msg, location.origin); } catch (e) { /* 忽略 */ }
}

/* 嵌入时给 body 打个标记：样式据此去掉本页自己的顶栏、让背景透明
   （浮层里只保留左侧分类 + 右侧内容；背景图由浮层的「背景图底板」负责画，
   本页再画一张会在滑动时错位）。 */
if (EMBEDDED) {
  try { document.body.classList.add("embed"); } catch (e) { /* 忽略 */ }
}

/* 客户端标识：页面加载时生成一次并缓存到 sessionStorage。
 * 所有请求都带 X-XLLB-Client：服务端据此做「防跨站」校验（跨站表单提交无法附带自定义头），
 * 并把它绑定到危险操作的一次性确认令牌上（令牌不能被别的客户端拿去用）。 */
const CLIENT_ID = (() => {
  const KEY = "xllb-client-id";
  try {
    let v = sessionStorage.getItem(KEY);
    if (!v) {
      v = "xllb-client-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      sessionStorage.setItem(KEY, v);
    }
    return v;
  } catch (e) {
    return "xllb-client-" + Math.random().toString(36).slice(2, 10);
  }
})();

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  // 统一附加客户端标识（与 Content-Type 并存，不冲突）
  opts.headers = { "X-XLLB-Client": CLIENT_ID, ...(opts.headers || {}) };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Blob)) {
    opts.headers = { "Content-Type": "application/json", ...opts.headers };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
}

/* 危险操作：先领一次性确认令牌，再带令牌发真正的请求（静默两步，不弹窗）。
 * 服务端用令牌防止「绕过界面确认」的调用；令牌只能用一次、且与 op/target 绑定。 */
async function apiWithConfirm(path, body, op, target) {
  const prep = await api("/api/confirm/prepare", { method: "POST", body: { op, target: target || "" } });
  if (!prep || !prep.ok || !prep.token) {
    return prep || { ok: false, error: "无法获取确认令牌（服务未就绪）" };
  }
  return api(path, { method: "POST", body: { ...(body || {}), token: prep.token } });
}

/* 普通动作名不需要令牌，但请求体内含敏感键（api_key / model / backend …）的
 * 插件设置保存需要令牌：统一走 apiWithConfirm 静默两步。 */
function hasSensitiveKeys(settings) {
  const re = /(^|_)(api_key|key)$|model|backend/i;
  return Object.keys(settings || {}).some((k) => re.test(k));
}

/* ==================== 轻量提示 ==================== */
let _toastEl = null;
function toast(text) {
  if (!_toastEl) {
    _toastEl = document.createElement("div");
    _toastEl.className = "msg system";
    _toastEl.style.cssText = "position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:99;display:none;max-width:90%;";
    document.body.appendChild(_toastEl);
  }
  _toastEl.textContent = text;
  _toastEl.style.display = "block";
  clearTimeout(_toastEl._t);
  _toastEl._t = setTimeout(() => { _toastEl.style.display = "none"; }, 2600);
}

/* ==================== DOM 工具 ==================== */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/* 一个内容区块：小标题 +（可选）说明 + 卡片（卡片内为若干「标签 / 说明 / 控件」横向行） */
function section(title, desc) {
  const box = el("div", "set-section");
  if (title) box.appendChild(el("div", "set-section-title", title));
  if (desc) box.appendChild(el("div", "set-section-desc", desc));
  const card = el("div", "set-card");
  box.appendChild(card);
  box.rows = card;
  return box;
}

/* 一行设置：左侧标签 + 说明，右侧控件 */
function row(label, desc, control) {
  const line = el("div", "set-row");
  const main = el("div", "set-row-main");
  main.appendChild(el("div", "set-row-label", label));
  if (desc) main.appendChild(el("div", "set-row-desc", desc));
  line.appendChild(main);
  if (control) {
    const ctl = el("div", "set-row-ctl");
    (Array.isArray(control) ? control : [control]).forEach((c) => c && ctl.appendChild(c));
    line.appendChild(ctl);
  }
  return line;
}

function button(label, cls, onclick) {
  const b = el("button", "btn" + (cls ? " " + cls : ""), label);
  if (onclick) b.onclick = onclick;
  return b;
}

function textInput(value, placeholder) {
  const i = document.createElement("input");
  i.type = "text";
  i.value = value == null ? "" : value;
  if (placeholder) i.placeholder = placeholder;
  return i;
}

function numberInput(value, min, max) {
  const i = document.createElement("input");
  i.type = "number";
  if (min != null) i.min = String(min);
  if (max != null) i.max = String(max);
  i.value = value == null ? "" : String(value);
  return i;
}

function selectInput(options, value) {
  const s = document.createElement("select");
  (options || []).forEach((opt) => {
    const o = document.createElement("option");
    const v = (opt && typeof opt === "object") ? opt.value : opt;
    const t = (opt && typeof opt === "object") ? (opt.label || opt.value) : opt;
    o.value = v;
    o.textContent = t;
    o.selected = String(v) === String(value);
    s.appendChild(o);
  });
  return s;
}

function checkInput(checked, onchange) {
  const i = document.createElement("input");
  i.type = "checkbox";
  i.checked = !!checked;
  if (onchange) i.onchange = onchange;
  return i;
}

/* 音量滑杆：拖动时实时显示数值，松手后保存 */
function sliderInput(value, onCommit) {
  const wrap = el("div", "set-slider");
  const input = document.createElement("input");
  input.type = "range";
  input.min = "0";
  input.max = "1";
  input.step = "0.05";
  input.value = String(value);
  const val = el("span", "value", Number(value || 0).toFixed(2));
  input.oninput = () => { val.textContent = Number(input.value).toFixed(2); };
  input.onchange = () => onCommit(Number(input.value));
  wrap.appendChild(input);
  wrap.appendChild(val);
  return wrap;
}

/* ==================== 确认弹窗（二级确认：风险警告 + 二次确认） ==================== */
function showConfirmModal(title, text, onConfirm) {
  let overlay = $("confirm-overlay");
  if (!overlay) {
    overlay = el("div", "modal-overlay hidden");
    overlay.id = "confirm-overlay";
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">${escapeHtml(title)}</div>
      <div class="modal-text"></div>
      <div class="modal-actions">
        <button class="btn" data-act="no">否，取消</button>
        <button class="btn danger" data-act="yes">是，继续</button>
      </div>
    </div>`;
  overlay.querySelector(".modal-text").textContent = text || "确定继续吗？";
  overlay.classList.remove("hidden");
  overlay.querySelector('[data-act="no"]').onclick = () => overlay.classList.add("hidden");
  overlay.querySelector('[data-act="yes"]').onclick = () => {
    overlay.classList.add("hidden");
    try { onConfirm && onConfirm(); } catch (e) { /* 忽略 */ }
  };
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 动作长输出：渲染在当前标签内容底部（而不是塞进小提示气泡） */
function renderActionOutput(pluginName, text) {
  const host = $("settings-body") || document.body;
  let box = $("action-output-" + pluginName);
  if (!box) {
    box = el("div", "action-output");
    box.id = "action-output-" + pluginName;
    host.appendChild(box);
  }
  box.textContent = text || "";
  box.scrollIntoView({ block: "nearest" });
}

/* ==================== 背景主题（由「背景设置」插件控制） ==================== */
function applyBackgroundTheme(bgPlugin) {
  const s = (bgPlugin && bgPlugin.settings) || {};
  const mode = s.mode || "dark";
  const body = document.body;
  body.classList.remove("theme-light", "theme-image");
  body.style.removeProperty("--bg-image");
  body.style.removeProperty("--bg-strength");
  if (mode === "light") {
    body.classList.add("theme-light");
  } else if (mode === "image") {
    body.classList.add("theme-image");
    const img = String(s.image || "").trim();
    if (img) {
      if (/^(https?:|data:|\/)/i.test(img)) {
        body.style.setProperty("--bg-image", `url("${img}")`);
      } else {
        body.style.setProperty("--bg-image", "url('/api/background/image')");
      }
    }
    body.style.setProperty("--bg-strength", String(s.strength != null ? s.strength : 0.8));
  }
}

/* ==================== Ollama 模型列表（用于可输入的模型下拉） ==================== */
let ollamaModelsCache = null;

async function fetchOllamaModels() {
  if (ollamaModelsCache === null) {
    try {
      const m = await api("/api/models");
      ollamaModelsCache = m.ollama_models || [];
    } catch (e) {
      ollamaModelsCache = [];
    }
  }
  return ollamaModelsCache;
}

// 模型列表到达后就地补充下拉选项，不重建整个表单（避免丢失正在编辑的内容）
function updateModelDatalists(models) {
  document.querySelectorAll("input[list^='dl-']").forEach((input) => {
    const dl = document.getElementById(input.getAttribute("list"));
    if (!dl) return;
    const existing = [...dl.options].map((o) => o.value);
    [...new Set(models)].forEach((m) => {
      if (!existing.includes(m)) {
        const o = document.createElement("option");
        o.value = m;
        dl.appendChild(o);
      }
    });
  });
}

/* ==================== 全局状态 ==================== */
// 内置分类标签（插件标签由插件的 settings_tab / 设置声明自动生成；左侧栏只用文字标签，不带图标）
const BUILTIN_TABS = [
  { key: "general", label: "通用设置", desc: "音量、角色表达、功能开关与联网额度", order: 10 },
  { key: "characters", label: "角色与语音", desc: "对话角色预设、自定义角色、语音预设，以及多人对话的参与角色", order: 20 },
  { key: "history", label: "对话记录", desc: "保存 / 清空对话记录", order: 40 },
  { key: "plugins", label: "插件管理", desc: "只负责插件的启用 / 停用与重新加载；插件参数请点击左侧插件标签", order: 50 },
];

const state = {
  settings: null,
  characters: [],
  voices: [],
  history: [],
  plugins: [],
  models: [],
  roster: null,      // /api/characters/manage：角色预设 + 勾选 + 声线 + 多人对话可用性
  ttsService: null,  // /api/tts/service：语音合成服务状态（端口 / 就绪 / 失败原因）
  tab: "",
};

// 旧标签别名：语音合成标签已合并进「角色与语音」，老链接（#voice）继续可用
const TAB_ALIAS = { voice: "characters", roles: "characters", plugins_manage: "plugins" };

function normalizeTabKey(key) {
  const k = String(key || "").replace(/^#/, "");
  return TAB_ALIAS[k] || k;
}

/* ==================== 标签集合 ==================== */
function allTabs() {
  const tabs = BUILTIN_TABS.map((t) => ({ ...t, builtin: true }));
  (state.plugins || []).forEach((p) => {
    const meta = p.settings_tab || {};
    const hasInline = (p.settings_schema && p.settings_schema.length) || (p.actions && p.actions.length) || p.has_state;
    const isPage = meta.type === "page" && !!meta.page;
    // 既没有可渲染内容、也没有声明标签的插件，只出现在「插件管理」列表里
    if (!hasInline && !isPage && !meta.label) return;
    tabs.push({
      key: "plugin:" + p.name,
      label: meta.label || p.name,
      desc: meta.desc || p.description || (isPage ? "插件自带页面" : "插件设置"),
      order: meta.order != null ? meta.order : 60,
      plugin: p,
      page: isPage ? meta.page : "",
      builtin: false,
    });
  });
  tabs.sort((a, b) => (a.order - b.order) || String(a.label).localeCompare(String(b.label), "zh"));
  return tabs;
}

function readHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  if (!raw) return "";
  try { return decodeURIComponent(raw); } catch (e) { return raw; }
}

/* ==================== 左侧标签栏 ==================== */
function renderNav() {
  const nav = $("settings-nav");
  nav.innerHTML = "";
  const tabs = allTabs();
  let pluginGroupDone = false;
  tabs.forEach((tab) => {
    if (!tab.builtin && !pluginGroupDone) {
      nav.appendChild(el("div", "settings-nav-group plugin-group", "插件设置"));
      pluginGroupDone = true;
    }
    const item = el("button", "settings-nav-item" + (tab.builtin ? "" : " plugin"));
    item.classList.toggle("active", tab.key === state.tab);
    item.appendChild(el("span", "sn-label", tab.label));
    if (tab.plugin && !tab.plugin.enabled) item.appendChild(el("span", "sn-badge", "已停用"));
    item.onclick = () => openTab(tab.key);
    nav.appendChild(item);
  });
}

/* ==================== 右侧内容 ==================== */
function openTab(key) {
  const tabs = allTabs();
  const wanted = normalizeTabKey(key);
  const target = tabs.find((t) => t.key === wanted) || tabs[0];
  if (!target) return;
  state.tab = target.key;
  if (readHash() !== target.key) {
    try { location.hash = target.key; } catch (e) { /* 忽略 */ }
  }
  setHead(target);
  renderNav();
  renderContent(target);
}

function setHead(tab) {
  const title = $("settings-tab-title");
  title.innerHTML = "";
  title.appendChild(el("span", null, tab.label));
  // 插件标签加一个「插件」标记，和自带设置区分开
  if (tab.plugin) title.appendChild(el("span", "sm-badge", "插件"));
  $("settings-tab-desc").textContent = tab.desc || "";
}

function renderContent(tab) {
  const body = $("settings-body");
  body.innerHTML = "";
  if (tab.plugin) {
    if (tab.page) renderPageTab(body, tab);
    else renderPluginTab(body, tab.plugin);
    return;
  }
  if (tab.key === "general") return renderGeneral(body);
  if (tab.key === "characters") return renderCharacters(body);
  if (tab.key === "history") return renderHistory(body);
  if (tab.key === "plugins") return renderPluginsTab(body);
}

/* ---------- 通用设置 ---------- */
function renderGeneral(body) {
  const st = state.settings || {};

  const audio = section("音频");
  audio.rows.appendChild(row("对话音量", "角色语音的播放音量", sliderInput(st.tts_volume, async (v) => {
    await saveSettings({ tts_volume: v });
  })));
  audio.rows.appendChild(row("音乐音量", "音乐播放的音量", sliderInput(st.music_volume, async (v) => {
    await saveSettings({ music_volume: v });
  })));
  body.appendChild(audio);

  const infl = section("角色表达", "每次回复随机取一个影响力值，数值越大角色表达越主动、越丰富。");
  const minI = numberInput(st.influence_min, 1, 10);
  const maxI = numberInput(st.influence_max, 1, 10);
  minI.classList.add("set-num-sm");
  maxI.classList.add("set-num-sm");
  infl.rows.appendChild(row("影响力范围", "需满足 1 ≤ 最小 ≤ 最大 ≤ 10", [
    el("span", "set-inline-label", "最小"), minI,
    el("span", "set-inline-label", "最大"), maxI,
    button("应用范围", "", async () => {
      const r = await saveSettings({ influence_min: Number(minI.value), influence_max: Number(maxI.value) });
      if (r) toast("已应用影响力范围。");
    }),
  ]));
  body.appendChild(infl);

  const sw = section("功能开关");
  sw.rows.appendChild(row("联网搜索", "角色需要时自动联网搜索（消耗 Tavily 额度）", checkInput(st.internet_enabled, async (e) => {
    await saveSettings({ internet_enabled: e.target.checked });
  })));
  sw.rows.appendChild(row("调试模式", "输出更详细的日志，便于排查问题", checkInput(st.debug_mode, async (e) => {
    await saveSettings({ debug_mode: e.target.checked });
  })));
  body.appendChild(sw);

  const tav = st.tavily || {};
  const usage = section("联网额度");
  usage.rows.appendChild(row("Tavily 额度", "已用 / 上限（剩余）",
    el("span", "value", `${tav.used != null ? tav.used : "—"} / ${tav.limit != null ? tav.limit : "—"}（剩 ${tav.remaining != null ? tav.remaining : "—"}）`)));
  const usedInput = numberInput(tav.used, 0);
  usedInput.classList.add("set-num-sm");
  usage.rows.appendChild(row("重置计数", "把已用次数改为指定值", [
    usedInput,
    button("重置计数", "", async () => {
      const r = await api("/api/usage", { method: "POST", body: { used: Number(usedInput.value) } });
      if (!r || !r.ok) { toast("重置失败：" + ((r && r.error) || "未知错误")); return; }
      await reloadSettings();
      toast("已重置联网额度计数。");
      openTab("general");
    }),
  ]));
  body.appendChild(usage);

  // 版本号来自根目录 version.txt（界面与启动脚本都读同一份文件，不再写死）
  const about = section("关于");
  about.rows.appendChild(row("版本", "读取自根目录 version.txt",
    el("span", "value", st.version || "—")));
  body.appendChild(about);
}


/* ---------- 角色与语音（角色卡片 + 语音预设；不再保留重复的预设/自定义表单） ---------- */
function renderCharacters(body) {
  const st = state.settings || {};

  /* --- 角色（每个角色一张卡片：勾选参与多人对话 + 分类设定 + 修改 / LLM 优化） --- */
  const roles = section("角色", "勾选即参与多人对话（至少 2 个才会接管对话）；点卡片右侧可展开分类设定、修改提示词或用大模型优化。");
  roles.rows.appendChild(renderRosterStatus());
  roles.rows.appendChild(renderRosterBar());
  const list = el("div", "role-list");
  list.id = "role-list";
  roles.rows.appendChild(list);
  body.appendChild(roles);
  renderRoleList();

  /* --- 语音预设（单人对话使用的音色；多人对话里各角色的声线在角色卡片内设置） --- */
  const voice = section("语音预设", "这里是单人对话使用的音色；多人对话中各角色的声线在角色卡片里分别指定。");
  voice.rows.appendChild(renderTtsServiceRow());
  voice.rows.appendChild(row("当前预设", "", el("span", "value", st.current_voice_name || "—")));
  const voiceSel = selectInput(state.voices.map((v) => ({
    value: v.name, label: `${v.name}${v.description ? " — " + v.description : ""}`,
  })), st.current_voice_name);
  voice.rows.appendChild(row("预设列表", "预设目录下的音色配置", [
    voiceSel,
    button("默认配置", "", async () => {
      const r = await api("/api/voice_preset", { method: "POST", body: { mode: "default" } });
      if (!r || !r.ok) { toast("切换失败：" + ((r && r.error) || "未知错误")); return; }
      await reloadSettings();
      openTab("characters");
      refreshStatusBar();
      toast("已切换为默认语音配置。");
    }),
    button("应用预设", "primary", async () => {
      const r = await api("/api/voice_preset", { method: "POST", body: { mode: "preset", name: voiceSel.value } });
      if (!r || !r.ok) { toast("应用失败：" + ((r && r.error) || "未知错误")); return; }
      await reloadSettings();
      openTab("characters");
      refreshStatusBar();
      toast(`已应用语音预设「${voiceSel.value}」。`);
    }),
  ]));
  const newVoice = textInput("", "新预设名称");
  voice.rows.appendChild(row("新建预设", "创建后请把参考音频（.wav）、prompt.txt 与权重文件放入生成的目录。", [
    newVoice,
    button("创建新预设", "", async () => {
      if (!newVoice.value.trim()) { toast("请先填写预设名称。"); return; }
      const r = await api("/api/voice_preset", { method: "POST", body: { mode: "create", name: newVoice.value } });
      if (!r || !r.ok) { toast("创建失败：" + ((r && r.error) || "未知错误")); return; }
      await reloadVoices();
      openTab("characters");
      toast(r.message || "已创建语音预设。");
    }),
  ]));
  body.appendChild(voice);
}

/* 语音合成服务（GPT-SoVITS）状态行：
   端口可能不是 9880 —— Windows 上该端口可能被系统保留（Hyper-V/WSL/Docker 预留），
   程序会自动改用可用端口；这里显示实际端口，并提供「重启语音服务」与「查看日志」。 */
function renderTtsServiceRow() {
  const svc = state.ttsService || {};
  const ready = !!svc.ready;
  const info = el("div", "tts-status" + (ready ? " on" : ""));
  info.appendChild(el("span", "tts-status-title", ready ? "语音服务：就绪" : "语音服务：未就绪"));
  const bits = [];
  if (svc.port) bits.push(`端口 ${svc.port}`);
  if (svc.pid) bits.push(`PID ${svc.pid}`);
  if (svc.running && !ready) bits.push("正在加载模型…");
  if (!svc.configured_port_available && svc.port) bits.push("该端口不可监听（已被系统保留或占用）");
  info.appendChild(el("span", "tts-status-desc", bits.join("　")));
  const btns = el("div", "p-btns");
  btns.appendChild(button("重启语音服务", "", async () => {
    toast("正在重启语音服务（模型加载需要一会儿）…");
    const r = await api("/api/tts/service", { method: "POST", body: { action: "restart" } });
    if (!r || !r.ok) { toast("重启失败：" + ((r && r.error) || "未知错误")); return; }
    state.ttsService = r.service || state.ttsService;
    toast(r.message || "已处理。");
    openTab("characters");
  }));
  btns.appendChild(button("查看日志", "", async () => {
    const r = await api("/api/tts/service");
    const s = (r && r.service) || {};
    if (s.log_file) toast(`日志文件：${s.log_file}`);
    else toast("暂无日志路径");
  }));
  if (svc.last_error) info.appendChild(el("div", "tts-status-err", svc.last_error));
  info.appendChild(btns);
  return info;
}

/* 多人对话握手状态：由插件 available() 声明「已激活 / 单人输出」及原因 */
function renderRosterStatus() {
  const info = state.roster || {};
  const availability = info.availability || {};
  const enabled = !!info.plugin_enabled;
  const active = !!info.available;
  const status = el("div", "role-status" + (active ? " on" : ""));
  status.appendChild(el("span", "role-status-title", active ? "多人对话：已激活" : "多人对话：单人输出"));
  status.appendChild(el("span", "role-status-desc",
    enabled ? (availability.note || availability.reason || "")
            : (availability.reason || "「多人对话」插件未启用")));
  const statusBtns = el("div", "p-btns");
  statusBtns.appendChild(button("刷新状态", "", async () => {
    await reloadRoster();
    openTab("characters");
  }));
  if (!enabled) {
    const tab = allTabs().find((t) => t.key === "plugin:多人对话");
    statusBtns.appendChild(button("打开插件设置", "", () => openTab(tab ? tab.key : "plugins")));
  }
  status.appendChild(statusBtns);
  return status;
}

/* 角色区工具栏：新建角色 / 全选 / 全不选 / 保存勾选 / 已勾选计数 */
function renderRosterBar() {
  const info = state.roster || {};
  const enabled = !!info.plugin_enabled;
  const bar = el("div", "page-head role-bar");
  bar.appendChild(button("新建角色", "", () => openRoleDialog({ create: true })));
  if (enabled) {
    bar.appendChild(button("全选", "", () => setAllRoles(true)));
    bar.appendChild(button("全不选", "", () => setAllRoles(false)));
    bar.appendChild(button("保存勾选", "primary", saveRosterSelection));
  }
  const count = el("span", "count role-count");
  count.id = "role-count";
  bar.appendChild(count);
  return bar;
}

/* 角色卡片列表 */
function renderRoleList() {
  const list = $("role-list");
  if (!list) return;
  const info = state.roster || {};
  const enabled = !!info.plugin_enabled;
  const items = info.presets || [];
  const selected = new Set(info.selected || []);
  const voices = info.voices || [];
  const vmap = info.voices_map || {};
  list.innerHTML = "";
  if (!items.length) {
    list.appendChild(el("div", "role-empty", "（暂无角色预设）点上方「新建角色」创建第一个角色。"));
    updateRoleCount();
    return;
  }
  items.forEach((p) => list.appendChild(createRoleCard(p, {
    enabled, selected: selected.has(p.name), voices, voice: vmap[p.name] || "",
  })));
  updateRoleCount();
}

function createRoleCard(p, opt) {
  const card = el("div", "role-item" + (opt.selected ? " on" : ""));
  card.dataset.name = p.name;

  const head = el("div", "role-head");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = opt.selected;
  cb.dataset.name = p.name;
  cb.disabled = !opt.enabled;
  cb.title = opt.enabled ? "勾选后参与多人对话" : "「多人对话」插件未启用";
  cb.onchange = () => {
    card.classList.toggle("on", cb.checked);
    updateRoleCount();
  };
  head.appendChild(cb);
  head.appendChild(el("span", "role-name", p.name));
  if (p.is_current) head.appendChild(el("span", "role-tag", "当前角色"));
  head.appendChild(el("span", "role-summary", p.summary || "（无设定）"));

  const btns = el("div", "p-btns");
  if (!p.is_current) {
    btns.appendChild(button("设为当前", "", async () => {
      const r = await api("/api/character", { method: "POST", body: { mode: "preset", preset_name: p.name } });
      if (!r || !r.ok) { toast("切换失败：" + ((r && r.error) || "未知错误")); return; }
      await reloadSettings();
      await reloadRoster();
      openTab("characters");
      refreshStatusBar();
      toast(`已把「${p.name}」设为当前角色。`);
    }));
  }
  btns.appendChild(button("修改设定", "", () => openRoleDialog({ preset: p })));
  btns.appendChild(button("LLM 优化", "", () => openRoleDialog({ preset: p, optimize: true })));
  head.appendChild(btns);

  const detail = el("div", "role-detail hidden");
  const rows = el("div", "role-detail-rows");
  rows.appendChild(detailRow("自定义要求", p.custom_req, ""));
  rows.appendChild(detailRow("参考资料", p.reference, ""));
  rows.appendChild(detailRow("完整提示词", p.description, `共 ${p.prompt_len || 0} 字`));
  const voiceSel = document.createElement("select");
  voiceSel.className = "select";
  voiceSel.dataset.voice = p.name;
  [{ value: "", label: "（默认声线）" }].concat(opt.voices.map((v) => ({ value: v, label: v })))
    .forEach((o) => {
      const op = document.createElement("option");
      op.value = o.value;
      op.textContent = o.label;
      op.selected = String(opt.voice || "") === o.value;
      voiceSel.appendChild(op);
    });
  voiceSel.disabled = !opt.selected;
  voiceSel.onchange = () => { /* 与勾选一起保存 */ };
  const voiceRow = row("对话声线", "多人对话中该角色的音色", voiceSel);
  rows.appendChild(voiceRow);
  detail.appendChild(rows);

  const toggle = el("button", "role-toggle", "展开详情");
  toggle.onclick = () => {
    const hidden = detail.classList.toggle("hidden");
    toggle.textContent = hidden ? "展开详情" : "收起详情";
  };
  head.appendChild(toggle);

  card.appendChild(head);
  card.appendChild(detail);
  return card;
}

/* 详情行：左侧分类标签 + 右侧内容（超长可滚动，避免卡片过长） */
function detailRow(label, text, hint) {
  const line = el("div", "set-row");
  line.appendChild(el("div", "set-row-label", label));
  const body = el("div", "role-text" + (String(text || "").trim() ? "" : " empty"),
    String(text || "").trim() || "（无）");
  line.appendChild(body);
  if (hint) line.appendChild(el("span", "set-row-hint", hint));
  return line;
}

function setAllRoles(checked) {
  const list = $("role-list");
  if (!list) return;
  list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    if (cb.disabled || cb.checked === checked) return;
    cb.checked = checked;
    cb.dispatchEvent(new Event("change"));
  });
}

function updateRoleCount() {
  const list = $("role-list");
  const count = $("role-count");
  if (!list || !count) return;
  const n = list.querySelectorAll('input[type="checkbox"]:checked').length;
  count.textContent = `已勾选 ${n} 个${n < 2 ? "（至少 2 个才会接管对话）" : ""}`;
}

/* 保存勾选（含各角色声线）：危险写操作，带客户端标识 + 一次性确认令牌 */
async function saveRosterSelection() {
  const list = $("role-list");
  if (!list) return;
  const chosen = [];
  const voices = {};
  list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    if (!cb.checked) return;
    const name = cb.dataset.name;
    chosen.push(name);
    const card = cb.closest(".role-item");
    const sel = card ? card.querySelector("select[data-voice]") : null;
    voices[name] = sel ? sel.value : "";
  });
  const r = await apiWithConfirm("/api/characters/manage",
    { selected: chosen, voices }, "characters.manage", "多人对话");
  if (!r || !r.ok) {
    toast("保存失败：" + ((r && r.error) || "未知错误"));
    return;
  }
  await reloadRoster();
  openTab("characters");
  toast(r.available
    ? `已保存：多人对话已激活（${chosen.length} 个角色）`
    : `已保存 ${chosen.length} 个角色；${r.reason || "仍未激活，当前为单人输出"}`);
}

/* ---------- 角色设定对话框（修改设定 / 新建角色 / LLM 优化提示词） ----------
   注意：调用方式统一为 openRoleDialog({preset?, create?, optimize?})。 */
function openRoleDialog(opts = {}) {
  const create = !!opts.create;
  const preset = opts.preset || {};
  let overlay = $("role-dialog-overlay");
  if (overlay) overlay.remove();
  overlay = el("div", "modal-overlay");
  overlay.id = "role-dialog-overlay";
  const box = el("div", "modal-box role-dialog");
  box.appendChild(el("div", "modal-title", create ? "新建角色" : `修改角色设定：${preset.name || ""}`));
  box.appendChild(el("div", "modal-text",
    create ? "填写角色名与设定即可创建；也可以点「联网搜索补全设定」自动抓取角色资料。"
           : "可直接编辑提示词（就是实际发给模型的人物设定），或用大模型把网页残留整理成清晰条目。"));

  const fields = el("div", "role-dialog-fields");
  const nameI = textInput(create ? "" : (preset.name || ""), "角色名（可附加括号说明）");
  if (!create) nameI.disabled = true;
  fields.appendChild(row("角色名", create ? "新角色的名称（保存后不可改名）" : "角色名不可修改（如需改名请新建）", nameI));
  const reqI = textInput(preset.custom_req || "", "自定义要求，如：自称阿绫，与用户是朋友关系");
  fields.appendChild(row("自定义要求", "", reqI));
  const refI = document.createElement("textarea");
  refI.className = "textarea";
  refI.rows = 4;
  refI.value = preset.reference || "";
  refI.placeholder = "参考资料（角色背景、说话风格等，可留空；也可用下方联网搜索自动补全）";
  fields.appendChild(row("参考资料", "", refI));
  const promptI = document.createElement("textarea");
  promptI.className = "textarea";
  promptI.rows = 8;
  promptI.value = preset.description || "";
  promptI.placeholder = "提示词全文（实际发给模型的人物设定）";
  fields.appendChild(row("提示词全文", "保存后立即生效；建议保留人物身份与关键设定", promptI));
  box.appendChild(fields);

  const note = el("div", "role-dialog-note");
  box.appendChild(note);

  // 联网搜索补全设定：明显的主按钮（消耗 1 次 Tavily 额度，结果填进「参考资料」）
  const tools = el("div", "role-dialog-tools");
  tools.appendChild(button("联网搜索补全设定", "primary", async () => {
    const name = nameI.value.trim();
    if (!name) { note.textContent = "请先填写角色名，再联网搜索资料。"; nameI.focus(); return; }
    note.textContent = "正在联网搜索角色资料（消耗 1 次 Tavily 额度）…";
    const r = await api("/api/characters/preset/reference",
      { method: "POST", body: { name, requirement: reqI.value.trim() } });
    if (!r || !r.ok) { note.textContent = (r && r.error) || "联网搜索失败"; return; }
    refI.value = r.text || "";
    note.textContent = `已获取 ${(r.text || "").length} 字资料，已填入「参考资料」；` +
      (r.remaining != null ? `剩余额度 ${r.remaining} 次。` : "");
  }));
  tools.appendChild(button("LLM 优化提示词", "", async () => {
    if (!promptI.value.trim()) {
      note.textContent = promptI.value.trim() ? "" : "请先填写提示词（或先点「联网搜索补全设定」再整理成条目）。";
      return;
    }
    note.textContent = "正在用大模型优化提示词，请稍候…";
    const r = await api("/api/characters/preset/optimize",
      { method: "POST", body: { name: nameI.value.trim(), prompt: promptI.value } });
    if (!r || !r.ok) { note.textContent = (r && r.error) || "优化失败（请检查生成模型是否可用）"; return; }
    promptI.value = r.optimized || promptI.value;
    note.textContent = `已优化（模型：${r.model || "默认"}），请检查后点「保存」。`;
  }));
  box.appendChild(tools);

  const actions = el("div", "modal-actions");
  actions.appendChild(button("取消", "", () => overlay.remove()));
  actions.appendChild(button("保存", "primary", async () => {
    const name = nameI.value.trim();
    if (!name) { note.textContent = "请填写角色名。"; nameI.focus(); return; }
    const prompt = promptI.value.trim();
    const customReq = reqI.value.trim();
    const reference = refI.value.trim();
    if (!prompt && !customReq) { note.textContent = "请至少填写「自定义要求」或「提示词全文」。"; return; }
    note.textContent = "正在保存…";
    const r = await apiWithConfirm("/api/characters/preset/save",
      { name, prompt, custom_req: customReq, reference, create },
      create ? "characters.create" : "characters.preset", name);
    if (!r || !r.ok) { note.textContent = (r && r.error) || "保存失败"; return; }
    overlay.remove();
    await reloadCharacters();
    await reloadRoster();
    openTab("characters");
    toast(create ? `已创建角色「${name}」。` : `已保存角色「${name}」的设定。`);
  }));
  box.appendChild(actions);

  overlay.appendChild(box);
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
  if (opts.optimize) tools.querySelectorAll("button")[1].click();
  else (create ? nameI : promptI).focus();
}

/* ---------- 对话记录 ---------- */
function renderHistory(body) {
  const ops = section("操作");
  ops.rows.appendChild(row("刷新", "重新读取当前对话记录", button("刷新", "", async () => {
    await reloadHistory();
    toast("已刷新对话记录。");
  })));
  ops.rows.appendChild(row("保存已标记", "保存列表中勾选的记录到长期记忆", button("保存已标记", "primary", async () => {
    const indexes = [...document.querySelectorAll("#history-list input:checked")].map((c) => c.dataset.index);
    if (!indexes.length) { toast("请先勾选要保存的记录。"); return; }
    await api("/api/history/mark", { method: "POST", body: { indexes } });
    const r = await api("/api/save", { method: "POST", body: { mode: "marked" } });
    toast(r && r.ok ? `已保存 ${r.result.count} 条记录。` : "保存失败");
    await reloadHistory();
    openTab("history");
  })));
  ops.rows.appendChild(row("保存最新", "保存最近一条对话记录", button("保存最新", "", async () => {
    const r = await api("/api/save", { method: "POST", body: { mode: "latest" } });
    toast(r && r.ok ? "已保存最新记录。" : "保存失败");
  })));
  ops.rows.appendChild(row("清空", "清空当前对话记录（不影响长期记忆库）", button("清空", "danger", () => {
    showConfirmModal("清空对话记录", "将清空当前对话记录与上下文，确定继续吗？", async () => {
      const r = await apiWithConfirm("/api/history/clear", {}, "history.clear", "*");
      if (!r || !r.ok) { toast(`清空失败：${(r && r.error) || "未知错误"}`); return; }
      await reloadHistory();
      openTab("history");
      toast("已清空对话记录。");
    });
  })));
  body.appendChild(ops);

  const list = section("记录列表", "勾选后点「保存已标记」写入长期记忆。");
  const box = el("div", "history-list");
  box.id = "history-list";
  list.rows.appendChild(box);
  body.appendChild(list);
  renderHistoryList();
}

function renderHistoryList() {
  const list = $("history-list");
  if (!list) return;
  list.innerHTML = "";
  const records = state.history || [];
  if (!records.length) {
    list.appendChild(el("div", "p-desc", "暂无对话记录"));
    return;
  }
  records.forEach((rec) => {
    const div = el("div", "history-item");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = rec.saved;
    cb.dataset.index = rec.index;
    const bodyEl = el("div", "h-body");
    bodyEl.innerHTML = `<b>#${rec.index}</b> 用户：${escapeHtml(rec.user)}<br>助手：${escapeHtml(rec.assistant)}`;
    div.appendChild(cb);
    div.appendChild(bodyEl);
    list.appendChild(div);
  });
}

/* ---------- 插件管理（只保留启停能力） ---------- */
/* 重载结果提示：已重载 N 个、跳过 M 个未变化插件，以及本次是否会清理临时上下文。
 * 判断依据是插件自己声明的 reload_policy.preserve_context（不谎报）：
 * 只有「本次真正被卸载/重新加载」且该策略为 false 的插件才可能丢掉临时上下文。 */
function reloadReportText(r) {
  const report = (r && r.report) || {};
  const plugins = (r && r.plugins) || [];
  const reloaded = (report.loaded || []).length + (report.reloaded || []).length;
  const kept = (report.kept || []).length;
  const changed = new Set([...(report.loaded || []), ...(report.reloaded || [])]);
  const risky = plugins.filter((p) => changed.has(p.name)
    && p.reload_policy && p.reload_policy.preserve_context === false).map((p) => p.name);
  const ctx = risky.length
    ? `本次会影响临时上下文（${risky.join("、")}）`
    : "本次不影响临时上下文";
  return `已重载 ${reloaded} 个，跳过 ${kept} 个未变化插件；${ctx}`;
}

function renderPluginsTab(body) {
  const ops = section("插件管理", "这里只负责插件的启用 / 停用与重新加载；插件参数设置请点击左侧对应的插件标签。");
  const count = el("span", "value", `${(state.plugins || []).filter((p) => p.enabled).length} / ${(state.plugins || []).length} 个已启用`);
  ops.rows.appendChild(row("重新加载插件", "扫描 plugins/ 目录，增量热重载；未变化的插件会被跳过（不影响其临时上下文）", [
    count,
    button("重新加载插件", "", async () => {
      const r = await apiWithConfirm("/api/plugins/reload", {}, "plugins.reload", "*");
      if (!r || !r.ok) { toast(`插件加载失败：${(r && r.error) || "未知错误"}`); return; }
      toast(reloadReportText(r));
      await reloadPlugins();
      await reloadRoster();   // 重载后刷新多人对话的可用性状态
      openTab("plugins");
    }),
  ]));
  body.appendChild(ops);

  const list = section("插件列表", "「参数设置 →」跳到左侧该插件的标签页。");
  const box = el("div", "plugin-list");
  box.id = "plugin-list";
  list.rows.appendChild(box);
  body.appendChild(list);
  renderPluginList();
}

function renderPluginList() {
  const list = $("plugin-list");
  if (!list) return;
  list.innerHTML = "";
  const plugins = state.plugins || [];
  if (!plugins.length) {
    list.innerHTML = '<div class="p-desc">暂无插件（把 .py 文件放入 plugins/ 目录后点“重新加载插件”）。</div>';
    return;
  }
  const tabs = allTabs();
  plugins.forEach((p) => {
    const div = el("div", "plugin-item" + (p.enabled ? "" : " disabled"));
    const head = el("div", "p-head");
    const nameEl = el("span", "p-name", p.name + " ");
    nameEl.appendChild(el("span", "p-version", `v${p.version}`));
    // 不再区分「官方 / 第三方」：这个标签位改显示插件作者名（作者留空时不显示，保持空白）
    const author = String(p.author || "").trim();
    if (author) {
      const ab = el("span", "p-badge author", author);
      ab.title = "插件作者";
      nameEl.appendChild(ab);
    }
    if (p.hot_swap) {
      const hs = el("span", "p-badge hot-swap", "热切换");
      hs.title = "可在对话进行中随时启用 / 停用";
      nameEl.appendChild(hs);
    }
    head.appendChild(nameEl);
    const btns = el("div", "p-btns");
    const tab = tabs.find((t) => t.key === "plugin:" + p.name);
    if (tab) {
      btns.appendChild(button("参数设置 →", "", () => openTab(tab.key)));
    }
    btns.appendChild(button(p.enabled ? "停用" : "启用", "p-toggle", async () => {
      await togglePlugin(p, !p.enabled);
    }));
    head.appendChild(btns);
    div.appendChild(head);

    if (p.description) div.appendChild(el("div", "p-desc", p.description));
    if (p.commands && p.commands.length) {
      div.appendChild(el("div", "p-cmds", "命令：" + p.commands.map((c) => c.name || c).join("  ")));
    }
    list.appendChild(div);
  });
}

/* ---------- 插件标签（参数设置 / 动作 / 状态） ---------- */
function renderPluginTab(body, p) {
  const status = section("插件状态", p.description || "");
  status.rows.appendChild(row(p.enabled ? "已启用" : "已停用",
    p.enabled ? "参数修改后立即生效" : "停用状态下参数不生效，启用后立即生效",
    button(p.enabled ? "停用插件" : "启用插件", p.enabled ? "" : "primary", async () => {
      await togglePlugin(p, !p.enabled);
    })));
  status.rows.appendChild(row("版本", "", el("span", "value", `v${p.version}${p.author ? "　作者：" + p.author : ""}`)));
  body.appendChild(status);

  if (p.settings_schema && p.settings_schema.length) {
    const box = section("参数设置", "修改后点「保存设置」生效。");
    const form = el("div", "plugin-settings set-form");
    p.settings_schema.forEach((field) => schemaField(form, field, p, state.models));
    form.appendChild(el("div", "set-form-actions")).appendChild(button("保存设置", "primary", async () => {
      const patch = {};
      form.querySelectorAll("input,select").forEach((input) => {
        let v;
        if (input.type === "checkbox") v = input.checked;
        else {
          v = input.value;
          if (input.type === "number") v = Number(v);
        }
        patch[input.dataset.key] = v;
      });
      const payload = { name: p.name, settings: patch };
      // 含敏感键（api_key / model / backend …）时服务端要求确认令牌：这里静默两步，不弹窗
      const r = hasSensitiveKeys(patch)
        ? await apiWithConfirm("/api/plugins/settings", payload, "plugins.settings", p.name)
        : await api("/api/plugins/settings", { method: "POST", body: payload });
      if (!r || !r.ok) { toast(`保存失败：${(r && r.error) || "未知错误"}`); return; }
      toast(`已保存「${p.name}」设置。`);
      await reloadPlugins();
      openTab("plugin:" + p.name);
    }));
    box.rows.appendChild(form);
    body.appendChild(box);
    wireApiAutofill(form);
  }

  if (p.actions && p.actions.length) {
    const box = section("快捷操作");
    p.actions.forEach((a) => box.rows.appendChild(row(a.label || a.name, a.desc || "", actionButton(p, a))));
    body.appendChild(box);
  }

  if (p.has_state) {
    const box = section("运行状态");
    const stateBox = el("div", "plugin-state");
    stateBox.id = `plugin-state-${p.name}`;
    box.rows.appendChild(stateBox);
    body.appendChild(box);
    refreshPluginState(p.name);
  }
}

/* ---------- 插件自带页面标签 ---------- */
function renderPageTab(body, tab) {
  const box = section("插件页面", tab.desc || "该标签由插件提供，内容在插件自带的页面中展示。");
  box.rows.appendChild(row(tab.label, tab.page, button("打开页面", "primary", () => { location.href = tab.page; })));
  body.appendChild(box);
}

/* ==================== 设置字段渲染（插件 settings_schema） ==================== */
function schemaField(container, field, p, models) {
  if (field.type === "section") {
    // 折叠分组：默认收起（带动作按钮的分组默认展开，避免入口被藏起来）
    const details = el("details", "p-section");
    if (field.actions && field.actions.length) details.open = true;
    details.appendChild(el("summary", null, field.label || "高级选项"));
    if (field.desc) details.appendChild(el("div", "p-hint", field.desc));
    const inner = el("div", "p-sec-body");
    (field.fields || []).forEach((sub) => schemaField(inner, sub, p, models));
    if (field.actions && field.actions.length) {
      // 动作按「标签 + 说明 — 按钮」成行排列：与设置项同一套版式，
      // 不再用等宽拉伸按钮（长文案会折行错位，排版难看）
      field.actions.forEach((a) => inner.appendChild(row(a.label || a.name, a.desc || "", actionButton(p, a))));
    }
    details.appendChild(inner);
    container.appendChild(details);
    return;
  }
  container.appendChild(row(field.label || field.key, field.desc, buildInput(field, p, container, models)));
}

function buildInput(field, p, form, models) {
  const val = (p.settings && p.settings[field.key] !== undefined) ? p.settings[field.key] : "";
  let input;

  if (field.type === "select") {
    input = selectInput(field.options || [], val);
    if (field.key.endsWith("_backend")) {
      // 生成 / 判断后端切换时，联动模型输入框（ollama 用下拉候选，openai 手填）
      input.onchange = () => {
        const modelKey = field.key === "chat_backend" ? "chat_model" : "judge_model";
        const modelInput = form.querySelector(`input[data-key="${modelKey}"]`);
        if (!modelInput) return;
        const dlId = `dl-${p.name}-${modelKey}`;
        if (input.value === "openai") {
          modelInput.removeAttribute("list");
          modelInput.placeholder = "如 gpt-4o-mini";
          const dl = document.getElementById(dlId);
          if (dl) dl.innerHTML = "";
        } else {
          modelInput.setAttribute("list", dlId);
          modelInput.placeholder = "";
          let dl = document.getElementById(dlId);
          if (!dl) {
            dl = document.createElement("datalist");
            dl.id = dlId;
            form.appendChild(dl);
          }
          dl.innerHTML = "";
          const cur = modelInput.value;
          [...new Set([cur, ...(models || [])])].filter(Boolean).forEach((m) => {
            const o = document.createElement("option");
            o.value = m;
            dl.appendChild(o);
          });
        }
      };
    }
  } else if (field.type === "checkbox") {
    input = checkInput(!!val);
  } else if (field.type === "datalist") {
    input = textInput(val, field.placeholder);
    input.setAttribute("list", `dl-${p.name}-${field.key}`);
    const dl = document.createElement("datalist");
    dl.id = `dl-${p.name}-${field.key}`;
    let opts = field.options || [];
    if (field.options_source === "ollama_models") {
      opts = opts.concat((models || []).filter((m) => !opts.includes(m)));
    }
    opts.forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt;
      dl.appendChild(o);
    });
    form.appendChild(dl);
  } else if (field.type === "number") {
    input = numberInput(val);
  } else {
    input = textInput(val, field.placeholder);
    if (field.type === "password") input.type = "password";
  }
  input.dataset.key = field.key;
  return input;
}

// 生成 / 判断外部 API 自动互填：一边填写时，另一边为空则自动填入同值（可再单独修改）
function wireApiAutofill(form) {
  const pairs = [
    ["chat_api_base", "judge_api_base"],
    ["chat_api_key", "judge_api_key"],
    ["judge_api_base", "chat_api_base"],
    ["judge_api_key", "chat_api_key"],
  ];
  pairs.forEach(([srcKey, dstKey]) => {
    const src = form.querySelector(`[data-key="${srcKey}"]`);
    const dst = form.querySelector(`[data-key="${dstKey}"]`);
    if (!src || !dst) return;
    const fill = () => { if (src.value && !dst.value) dst.value = src.value; };
    src.addEventListener("input", fill);
    fill();
  });
}

/* ==================== 插件动作 / 状态 ==================== */
/* 插件可以要求打开「浮层窗口」而不是跳转页面：动作返回 {"modal": "名字"}，
   前端在 window.XLLB_MODALS 里查找同名浮层（如记忆管理由 memory_view.js 注册）。 */
function openPluginModal(name) {
  const modals = window.XLLB_MODALS || {};
  const open = modals[name];
  if (typeof open === "function") {
    open();
    return true;
  }
  toast(`插件想打开的「${name}」浮层不可用（对应脚本未加载）`);
  return false;
}

function actionButton(p, a) {
  const b = button(a.label || a.name, "", async () => {
    const r = await api("/api/plugins/action", { method: "POST", body: { name: p.name, action: a.name } });
    if (r && r.modal) { openPluginModal(r.modal); return; }
    if (r && r.page) { location.href = r.page; return; }
    if (r && r.confirm) {
      // 二级确认：先弹风险警告，选「是」后带服务端签发的一次性令牌调用最终动作。
      // 令牌可用一次、与 op/target(插件名:动作名) 绑定；直连最终动作会被服务端 409 拒绝。
      showConfirmModal(a.label || "请确认", r.reply || "确定继续吗？", async () => {
        const body = { name: p.name, action: r.confirm };
        const token = r.confirm_token;
        if (token) body.token = token;
        const r2 = await api("/api/plugins/action", { method: "POST", body });
        if (r2 && r2.need_confirm) {
          // 令牌缺失或已过期：明确提示需要重新点击该动作（不静默失败）
          renderActionOutput(p.name, `注意：${(r2 && r2.error) || "确认令牌缺失或已失效"}\n请重新点击「${a.label || a.name}」再次确认。`);
          return;
        }
        if (r2 && r2.reply) renderActionOutput(p.name, r2.reply);
        else toast((r2 && r2.error) || "已执行。");
      });
      return;
    }
    if (r && r.need_confirm) {
      renderActionOutput(p.name, `注意：${r.error || "确认令牌缺失或已失效"}\n请重新点击「${a.label || a.name}」再次确认。`);
      return;
    }
    if (r && r.reply) {
      if (r.reply.length > 100 || r.reply.includes("\n")) renderActionOutput(p.name, r.reply);
      else toast(r.reply);
    } else if (r && r.music) toast(`已在主页触发播放：「${r.music.title || ""}」`);
    else if (r && r.music_control === "stop") toast("已触发停止播放");
    else if (r && r.stream_id) toast("已触发语音播放");
    else toast((r && r.error) || "已执行。");
  });
  if (a.desc) b.title = a.desc;
  return b;
}

const STATE_LABELS = { saved: "已保存", last_user: "最近提问", last_lines: "最近行数", streaming: "生成中" };

async function refreshPluginState(name) {
  const box = document.getElementById(`plugin-state-${name}`);
  if (!box) return;
  try {
    const r = await api(`/api/plugins/state?name=${encodeURIComponent(name)}`);
    renderPluginState(box, r.state || {});
  } catch (e) { /* 忽略 */ }
}

function renderPluginState(box, st) {
  box.innerHTML = "";
  const queue = st && st.queue;
  if (queue && queue.length) {
    const list = el("div", "q-list");
    queue.forEach((q) => {
      const item = el("div", "q-item" + (q.status === "playing" ? " playing" : ""));
      const mark = q.status === "playing" ? "▶ " : (q.index != null ? `${q.index}. ` : "");
      item.textContent = mark + (q.title || "");
      list.appendChild(item);
    });
    box.appendChild(list);
  }
  const extras = Object.keys(st || {}).filter((k) => {
    const v = st[k];
    return k !== "queue" && v !== "" && v != null && typeof v !== "object";
  });
  if (extras.length) {
    const info = el("div", "p-desc");
    info.textContent = extras.map((k) => {
      const v = st[k];
      const text = v === true ? "是" : v === false ? "否" : v;
      return `${STATE_LABELS[k] || k}：${text}`;
    }).join("　");
    box.appendChild(info);
  }
  if (!box.childNodes.length) box.appendChild(el("div", "p-desc", "暂无运行状态"));
}

/* ==================== 数据加载 ==================== */
async function reloadSettings() {
  const r = await api("/api/settings");
  if (r && r.settings) state.settings = r.settings;
  refreshStatusBar();
}

/* 语音合成服务状态（端口 / 就绪 / 上次失败原因） */
async function reloadTtsService() {
  try {
    const r = await api("/api/tts/service");
    if (r && r.ok && r.service) state.ttsService = r.service;
  } catch (e) { /* 忽略 */ }
}

async function reloadCharacters() {
  const r = await api("/api/characters");
  state.characters = (r && r.presets) || [];
}

async function reloadVoices() {
  const r = await api("/api/voice_presets");
  state.voices = (r && r.presets) || [];
}

async function reloadHistory() {
  const r = await api("/api/history");
  state.history = (r && r.records) || [];
}

async function reloadRoster() {
  // 角色与语音标签的数据：全部角色预设 + 勾选 + 声线 + 多人对话插件的可用性声明
  try {
    const r = await api("/api/characters/manage");
    state.roster = (r && r.ok) ? r : (state.roster || {});
  } catch (e) {
    state.roster = state.roster || {};
  }
  return state.roster;
}

async function reloadPlugins() {
  const r = await api("/api/plugins");
  state.plugins = (r && r.plugins) || [];
  const bg = state.plugins.find((p) => p.name === "背景设置");
  applyBackgroundTheme(bg);
}

async function reloadModels() {
  state.models = await fetchOllamaModels();
}

async function togglePlugin(p, enable) {
  // 插件启停是危险操作：先领一次性令牌再执行（op=plugins.enable / plugins.disable）
  const r = await apiWithConfirm(`/api/plugins/${enable ? "enable" : "disable"}`, { name: p.name },
    enable ? "plugins.enable" : "plugins.disable", p.name);
  if (!r || !r.ok) { toast(`操作失败：${(r && r.error) || "未知错误"}`); return; }
  state.plugins = r.plugins || state.plugins;
  toast(`已${enable ? "启用" : "停用"}「${p.name}」。`);
  await reloadRoster();   // 插件启停会影响「角色与语音」里多人对话的可用性
  openTab(state.tab || "plugins");
  reloadModels().then(() => updateModelDatalists(state.models || []));
}

async function saveSettings(patch) {
  const r = await api("/api/settings", { method: "POST", body: patch });
  if (!r || !r.ok) { toast(`保存失败：${(r && r.error) || "未知错误"}`); return null; }
  state.settings = r.settings || state.settings;
  refreshStatusBar();
  // 回传主页：音量立即生效（不用等回到主页重新加载），其它设置让主页同步界面
  const st = state.settings || {};
  if (patch && ("tts_volume" in patch || "music_volume" in patch)) {
    postToHost({ type: "xllb:volumes", tts_volume: st.tts_volume, music_volume: st.music_volume });
  }
  postToHost({ type: "xllb:settings-saved" });
  return r.settings;
}

function refreshStatusBar() {
  const bar = $("status-bar");
  if (!bar) return;
  const st = state.settings || {};
  bar.innerHTML = "";
  bar.appendChild(el("span", "badge", `角色 · ${st.character_name || "—"}`));
  bar.appendChild(el("span", "badge", `语音 · ${st.current_voice_name || "—"}`));
}

/* ==================== 初始化 ==================== */
// 首帧占位：接口返回前先把框架画出来，避免进入设置页时先看到一片空白
function renderShellLoading(initialKey) {
  const key = normalizeTabKey(initialKey);
  const tab = BUILTIN_TABS.find((t) => t.key === key) || BUILTIN_TABS[0];
  state.tab = tab.key;
  setHead(tab);
  renderNav();
  const body = $("settings-body");
  body.innerHTML = "";
  body.appendChild(el("div", "set-loading", "正在加载设置…"));
}

async function init() {
  const initial = normalizeTabKey(readHash() || "general");
  renderShellLoading(initial);
  await Promise.all([reloadSettings(), reloadCharacters(), reloadVoices(), reloadHistory(),
                     reloadPlugins(), reloadRoster(), reloadTtsService()]);
  renderNav();
  openTab(initial);
  // 模型列表异步到达后就地补充下拉候选（Ollama 忙时不阻塞页面）
  reloadModels().then((models) => { if (models && models.length) updateModelDatalists(models); });
}

window.addEventListener("hashchange", () => {
  const key = normalizeTabKey(readHash());
  if (key && key !== state.tab) openTab(key);
});

/* 被主页浮层嵌入时：「返回对话」＝关闭浮层（不做跳转，避免把主页连同播放一起卸载） */
const backLink = document.querySelector(".back-link");
if (backLink && EMBEDDED) {
  backLink.textContent = "← 关闭设置";
  backLink.title = "关闭设置，返回对话";
  backLink.addEventListener("click", (e) => {
    e.preventDefault();
    postToHost({ type: "xllb:close-settings" });
  });
}

/* 嵌入时按 Esc 也关闭浮层（焦点在设置页里时，主页收不到这个按键）；
   有弹窗打开时先让弹窗自己处理，不抢它的 Esc。 */
document.addEventListener("keydown", (e) => {
  if (!EMBEDDED || e.key !== "Escape") return;
  if (document.querySelector(".modal-overlay:not(.hidden)")) return;
  postToHost({ type: "xllb:close-settings" });
});

init();
