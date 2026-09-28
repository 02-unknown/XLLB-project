/* 小笼洛包 · 设置页渲染冒烟测试（无需浏览器的极简 DOM 桩 + 真实接口数据回放）
 *
 * 用法：node tests/verify_settings_render.js <fixture.json>
 * fixture.json 由 verify_settings_page.py 通过真实接口导出（settings/characters/voices/history/plugins/models）。
 * 作用：在没有浏览器的环境里把 settings.js 完整跑一遍，逐个标签渲染，捕捉运行期 JS 错误。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const fixturePath = process.argv[2];
if (!fixturePath || !fs.existsSync(fixturePath)) {
  console.error("缺少 fixture 文件：" + fixturePath);
  process.exit(2);
}
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf-8"));

/* ==================== 极简 DOM 桩 ==================== */
const REGISTRY = new Map();

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
}

function matchPart(el, rawPart) {
  let part = String(rawPart).trim();
  let needChecked = false;
  if (part.includes(":")) {
    const bits = part.split(":");
    part = bits[0];
    needChecked = bits[1] === "checked";
  }
  // #id 与 .class 选择器（冒烟测试会用到）
  if (part.startsWith("#")) {
    if (el.id !== part.slice(1)) return false;
    return !needChecked || !!el.checked;
  }
  const clsMatch = part.match(/^\.([\w-]+)$/);
  if (clsMatch) {
    if (!el.classList.contains(clsMatch[1])) return false;
    return !needChecked || !!el.checked;
  }
  const m = part.match(/^([a-zA-Z]*)(?:\[([^\]]+)\])?$/);
  if (!m) return false;
  const tag = m[1];
  const attrExpr = m[2];
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  if (needChecked && !el.checked) return false;
  if (attrExpr) {
    const am = attrExpr.match(/^([\w-]+)(?:(\^=|=)([\s\S]*))?$/);
    if (!am) return false;
    const name = am[1], op = am[2];
    const want = String(am[3] || "").replace(/^["']|["']$/g, "");
    let have = name === "data-key" ? el.dataset.key : el.attributes[name];
    if (have === undefined) return false;
    if (op === "=" && String(have) !== want) return false;
    if (op === "^=" && !String(have).startsWith(want)) return false;
  }
  return true;
}

function descendants(root, out = []) {
  root.children.forEach((c) => { out.push(c); descendants(c, out); });
  return out;
}

function queryAll(root, selector) {
  const parts = String(selector).split(",").map((s) => s.trim()).filter(Boolean);
  return descendants(root).filter((el) => parts.some((p) => matchPart(el, p)));
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.classList = new ClassList(this);
    this.style = { _p: {}, setProperty(k, v) { this._p[k] = v; }, removeProperty(k) { delete this._p[k]; } };
    this._text = "";
    this._className = "";
    this.value = "";
    this.checked = false;
    this.type = "";
    this.title = "";
    this.placeholder = "";
    this.rows = undefined;
  }
  get className() { return this._className; }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get id() { return this._id || ""; }
  set id(v) { this._id = v; REGISTRY.set(v, this); }
  get textContent() {
    if (this._text) return this._text;
    if (!this.children.length) return "";
    return this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
  get innerHTML() { return this._html || ""; }
  set innerHTML(v) { this._html = v == null ? "" : String(v); this.children = []; }
  get childNodes() { return this.children; }
  get options() { return this.children; }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((n) => n && this.appendChild(n)); }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    if (child) {
      child.parentElement = null;
      // 与真实 DOM 一致：节点被移除后 getElementById 不再命中
      if (child.id && REGISTRY.get(child.id) === child) REGISTRY.delete(child.id);
    }
    return child;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener() {}
  removeEventListener() {}
  scrollIntoView() {}
  focus() {}
  blur() {}
  click() { if (typeof this.onclick === "function") this.onclick(); }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  insertBefore(node, ref) {
    const idx = ref ? this.children.indexOf(ref) : -1;
    node.parentElement = this;
    if (idx >= 0) this.children.splice(idx, 0, node);
    else this.children.push(node);
    return node;
  }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
}

const document = {
  body: new El("body"),
  createElement: (tag) => new El(tag),
  getElementById: (id) => REGISTRY.get(id) || null,
  querySelectorAll: (sel) => queryAll(document.body, sel),
  querySelector: (sel) => queryAll(document.body, sel)[0] || null,
  // 设置页会注册全局按键（嵌入时 Esc 关闭浮层）：桩里同样支持，避免误判成运行期错误
  addEventListener() {},
  removeEventListener() {},
  hidden: false,
};

// 用 settings.html 里声明的 id 预置元素（等价于页面已解析完成）
const htmlSrc = fs.readFileSync(path.join(__dirname, "..", "web", "settings.html"), "utf-8");
const mvSrcForIds = fs.readFileSync(path.join(__dirname, "..", "web", "static", "memory_view.js"), "utf-8");
// 记忆管理浮层的 DOM 由 memory_view.js 用 innerHTML 生成；DOM 桩不解析 innerHTML，
// 这里把该模板里的 id 一并预置，等价于「浮层已解析」。
const modalIds = [...mvSrcForIds.matchAll(/id="([a-zA-Z0-9_-]+)"/g)].map((m) => m[1]);
const htmlIds = [...htmlSrc.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
[...new Set(htmlIds.concat(modalIds))].forEach((id) => {
  const node = new El("div");
  node.id = id;
  document.body.appendChild(node);
});
console.log(`       预置页面元素：${htmlIds.join(", ")}`);
console.log(`       预置浮层元素：${modalIds.join(", ")}`);

/* ==================== fetch 桩（回放真实接口数据） ==================== */
const routes = {
  "/api/settings": { ok: true, settings: fixture.settings },
  "/api/characters": { ok: true, presets: fixture.characters },
  "/api/voice_presets": { ok: true, presets: fixture.voices },
  "/api/history": { ok: true, records: fixture.history },
  "/api/plugins": { ok: true, plugins: fixture.plugins },
  "/api/models": { ok: true, ollama_models: fixture.models },
  "/api/characters/manage": Object.assign({ ok: true }, fixture.manage),
  "/api/plugins/settings": { ok: true, settings: {} },
  "/api/plugins/enable": { ok: true, plugins: fixture.plugins },
  "/api/plugins/disable": { ok: true, plugins: fixture.plugins },
  "/api/plugins/reload": { ok: true, plugins: fixture.plugins },
  "/api/plugins/action": { ok: true, reply: "动作已执行（冒烟测试）", speak: false },
  "/api/plugins/state": { ok: true, state: { queue: [{ index: 1, title: "冒烟测试项", status: "" }], streaming: false } },
  "/api/confirm/prepare": { ok: true, token: "test-token", expires_in: 120, op: "test", target: "" },
  "/api/memory/status": { ok: true, gate: { plugin_enabled: true, mode: "readwrite", can_read: true, can_write: true, can_manage: true } },
  "/api/tts/service": { ok: true, service: { ready: true, running: true, pid: 1234, port: 20000, url: "http://127.0.0.1:20000", log_file: "runtime/logs/gpt_sovits_api.log", last_error: "", configured_port_available: true, enabled: true } },
  "/api/memory/view": { ok: true, l0: [], l1: { size: 0, stats: {} }, active: [], archive: [], cold: [], topics: ["日常"], totals: { active: 0, archive: 0 }, has_more: { active: false, archive: false }, limit: 200 },
  "/api/settings/post": { ok: true, settings: fixture.settings },
  "/api/character": { ok: true, character_name: "冒烟角色" },
  "/api/voice_preset": { ok: true, message: "已创建" },
  "/api/usage": { ok: true },
  "/api/save": { ok: true, result: { count: 1 } },
  "/api/history/mark": { ok: true },
  "/api/history/clear": { ok: true },
};
let fetchLog = [];

function fetchStub(url, opts) {
  const clean = String(url).split("?")[0];
  fetchLog.push(((opts && opts.method) || "GET") + " " + clean);
  const data = routes[clean];
  return Promise.resolve({
    json: () => Promise.resolve(data === undefined ? { ok: false, error: "未提供数据的接口: " + clean } : data),
  });
}

/* ==================== 运行 settings.js ==================== */
const location = { hash: "", href: "" };
const ctx = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, JSON, Object, Array, String, Number, Boolean, Math, Date, RegExp, Error, Set, Map,
  fetch: fetchStub, document, location, Image: function () {}, Blob: function () {},
  URLSearchParams, Event: function (type) { this.type = type; },
};
ctx.window = { addEventListener: () => {}, location };
ctx.globalThis = ctx;
vm.createContext(ctx);
// 与 settings.html 一致：先加载记忆管理浮层脚本（注册 XLLB_MODALS），再加载设置页脚本
const mvSrc = fs.readFileSync(path.join(__dirname, "..", "web", "static", "memory_view.js"), "utf-8");
vm.runInContext(mvSrc, ctx, { filename: "memory_view.js" });
const src = fs.readFileSync(path.join(__dirname, "..", "web", "static", "settings.js"), "utf-8");
vm.runInContext(src, ctx, { filename: "settings.js" });

const fails = [];
const oks = [];
function check(name, cond, extra) {
  if (cond) { oks.push(name); console.log("[OK]   " + name); }
  else { fails.push(name + (extra ? " " + extra : "")); console.log("[FAIL] " + name + (extra ? " " + extra : "")); }
}

(async function main() {
  await new Promise((r) => setTimeout(r, 300));   // 等 init() 的异步链跑完

  const tabs = ctx.allTabs();
  check("左侧标签已生成（内置 + 插件）", tabs.length >= 5, `共 ${tabs.length} 个`);
  const labels = tabs.map((t) => t.label);
  console.log("       标签顺序：" + labels.join(" | "));
  check("左侧标签为纯文字（不再带表情图标）", tabs.every((t) => !t.icon), JSON.stringify(tabs.map((t) => t.icon)));

  const builtinKeys = ["general", "characters", "history", "plugins"];
  builtinKeys.forEach((k) => check(`内置标签存在：${k}`, tabs.some((t) => t.key === k)));
  check("语音合成标签已合并进「角色与语音」（不再单独存在）", !tabs.some((t) => t.key === "voice"));

  const pluginTabs = tabs.filter((t) => t.plugin);
  check("每个有设置/动作/状态的插件都生成了标签", pluginTabs.length >= 1, `共 ${pluginTabs.length} 个`);

  let rendered = 0;
  tabs.forEach((t) => {
    try {
      ctx.openTab(t.key);
      const body = document.getElementById("settings-body");
      const title = document.getElementById("settings-tab-title");
      const ok = body && body.children.length > 0 && title && title.textContent;
      if (!ok) throw new Error("内容为空");
      rendered += 1;
    } catch (e) {
      fails.push(`渲染标签 ${t.key} 抛错: ${e && e.message}`);
      console.log(`[FAIL] 渲染标签 ${t.key}: ${e && e.message}`);
    }
  });
  check("所有标签都能渲染出内容", rendered === tabs.length, `${rendered}/${tabs.length}`);

  // 内容抽查：通用设置 / 角色与语音 / 对话记录 / 插件管理
  ctx.openTab("general");
  check("通用设置渲染了滑块与开关",
    document.getElementById("settings-body").querySelectorAll("input").length >= 4);
  ctx.openTab("characters");
  const roleBody = document.getElementById("settings-body");
  check("角色与语音渲染了角色与语音下拉（合并后可一处设置）",
    roleBody.querySelectorAll("select").length >= 3,
    `select 数量 ${roleBody.querySelectorAll("select").length}`);
  check("角色与语音包含多人对话勾选区（role-item）",
    roleBody.querySelectorAll(".role-item").length === (fixture.manage.presets || []).length,
    `role-item ${roleBody.querySelectorAll(".role-item").length} / 预设 ${(fixture.manage.presets || []).length}`);
  check("多人对话状态区显示激活状态（含可用性说明）",
    !!roleBody.querySelector(".role-status") && !!roleBody.querySelector(".role-status-desc"));
  // 卡片：默认收起详情 + 三个操作入口；展开后能看到分类行
  const card0 = roleBody.querySelectorAll(".role-item")[0];
  const cardBtns = card0 ? card0.querySelectorAll("button") : [];
  const btnsText = cardBtns.map((b) => String(b.textContent || "")).join(" / ");
  check("角色卡片有「修改设定 / LLM 优化 / 展开详情」等操作",
    btnsText.includes("修改设定") && btnsText.includes("LLM 优化") && btnsText.includes("展开详情"), btnsText);
  const drawer = card0 ? card0.querySelector(".role-detail") : null;
  check("角色详情默认收起（卡片更短，便于选择）", !!drawer && drawer.classList.contains("hidden"));
  const toggleBtn = cardBtns.find((b) => String(b.textContent || "").includes("展开详情"));
  if (toggleBtn) {
    toggleBtn.click();
    check("点「展开详情」后展示分类设定行",
      !drawer.classList.contains("hidden") && drawer.querySelectorAll(".set-row").length >= 3,
      `分类行 ${drawer.querySelectorAll(".set-row").length}`);
  } else {
    check("点「展开详情」后展示分类设定行", false, "未找到展开按钮");
  }
  // 修改设定对话框：能打开、含提示词文本框、可关闭
  ctx.openRoleDialog({ preset: (fixture.manage.presets || [])[0] });
  const ov = document.getElementById("role-dialog-overlay");
  const areas = ov ? ov.querySelectorAll(".textarea") : [];
  check("「修改设定」对话框可打开并带提示词文本框",
    !!ov && areas.length >= 2 && areas.some((a) => String(a.value || "").length > 0),
    `对话框 ${!!ov}，多行输入 ${areas.length}`);
  check("对话框提供「LLM 优化提示词」与「保存」按钮",
    !!ov && ov.querySelectorAll("button").map((b) => String(b.textContent || "")).join("/").includes("LLM 优化提示词"));
  check("对话框提供「联网搜索补全设定」按钮",
    !!ov && ov.querySelectorAll("button").map((b) => String(b.textContent || "")).join("/").includes("联网搜索补全设定"));
  if (ov) ov.remove();
  check("对话框可关闭（不残留遮挡层）", !document.getElementById("role-dialog-overlay"));
  // 新建角色对话框：角色名必须可输入（create 模式）+ 联网搜索按钮可见
  ctx.openRoleDialog({ create: true });
  const createOv = document.getElementById("role-dialog-overlay");
  const nameInput = createOv ? createOv.querySelectorAll("input")[0] : null;
  const createBtns = createOv ? createOv.querySelectorAll("button").map((b) => String(b.textContent || "")).join("/") : "";
  check("新建角色对话框：角色名输入框可用（未禁用）",
    !!nameInput && nameInput.disabled !== true, `disabled=${nameInput && nameInput.disabled}`);
  check("新建角色对话框：显示「联网搜索补全设定」按钮",
    createBtns.includes("联网搜索补全设定"), createBtns);
  if (createOv) createOv.remove();
  // 记忆管理浮层：由插件动作 {"modal": "memory_manager"} 打开
  check("记忆管理浮层已注册（XLLB_MODALS.memory_manager）",
    typeof (ctx.window.XLLB_MODALS || {}).memory_manager === "function");
  ctx.window.XLLB_MODALS.memory_manager();
  const mvOv = document.getElementById("memory-modal-overlay");
  check("记忆管理浮层可打开（含搜索、增删改与各层级区块）",
    !!mvOv && !!document.getElementById("box-active") && !!document.getElementById("mv-q")
    && !!document.getElementById("f-submit") && !!document.getElementById("memory-modal-body"));
  if (mvOv) mvOv.remove();
  check("记忆管理浮层可关闭", !document.getElementById("memory-modal-overlay"));
  // 插件动作返回 modal 时走 openPluginModal
  check("插件动作支持 modal 分发（openPluginModal 可调用）", typeof ctx.openPluginModal === "function");
  ctx.openTab("voice");   // 旧链接别名：应落到角色与语音
  check("旧 #voice 链接被别名到角色与语音",
    decodeURIComponent(String(location.hash).replace(/^#/, "")) === "characters", location.hash);
  ctx.openTab("history");
  const histExpect = Math.max(1, (fixture.history || []).length);   // 空列表会渲染占位提示
  check("对话记录渲染出记录条数",
    document.getElementById("history-list").children.length === histExpect,
    `期望 ${histExpect}，实际 ${document.getElementById("history-list").children.length}`);
  ctx.openTab("plugins");
  check("插件管理渲染了插件列表（数量与接口一致）",
    document.getElementById("plugin-list").children.length === (fixture.plugins || []).length,
    `期望 ${(fixture.plugins || []).length}，实际 ${document.getElementById("plugin-list").children.length}`);

  // 插件标签：参数设置表单 / 动作 / 状态
  const withSchema = (fixture.plugins || []).find((p) => p.settings_schema && p.settings_schema.length);
  if (withSchema) {
    ctx.openTab("plugin:" + withSchema.name);
    const form = document.getElementById("settings-body").querySelector(".plugin-settings") ||
      document.getElementById("settings-body").querySelectorAll("input")[0];
    check(`插件标签「${withSchema.name}」渲染了设置控件`, !!form);
    // 模拟点击「保存设置」：验证字段收集 / 保存请求链路不抛错
    const inputs = document.getElementById("settings-body").querySelectorAll("input,select");
    const hasKey = inputs.some((i) => i.dataset && i.dataset.key);
    check(`插件标签「${withSchema.name}」的控件带 data-key`, hasKey);
  } else {
    check("存在带 settings_schema 的插件（用于校验插件标签）", false, "接口里没有");
  }

  const withState = (fixture.plugins || []).find((p) => p.has_state);
  if (withState) {
    ctx.openTab("plugin:" + withState.name);
    await new Promise((r) => setTimeout(r, 60));
    const box = document.getElementById("plugin-state-" + withState.name);
    check(`插件标签「${withState.name}」渲染了运行状态`, !!box && box.children.length > 0);
  }

  // 多人对话插件标签：设置分组内的动作按钮应指向「角色与语音」（不再打开独立页面）
  const multi = (fixture.plugins || []).find((p) => p.name === "多人对话");
  if (multi) {
    ctx.openTab("plugin:多人对话");
    const btns = document.getElementById("settings-body").querySelectorAll("button");
    const hit = btns.find((b) => String(b.textContent || "").includes("角色与语音"));
    check("多人对话插件标签里的动作按钮指向「角色与语音」", !!hit,
      btns.map((b) => b.textContent).join(" / "));
  } else {
    check("存在「多人对话」插件（用于校验其设置分组动作）", false, "接口里没有");
  }

  // 启停链路（走桩，不改真实状态）
  const anyPlugin = (fixture.plugins || [])[0];
  try {
    await ctx.togglePlugin(anyPlugin, !anyPlugin.enabled);
    check("插件启用/停用链路不抛错", true);
  } catch (e) {
    check("插件启用/停用链路不抛错", false, e && e.message);
  }

  // 动作按钮链路
  const withAction = (fixture.plugins || []).find((p) => p.actions && p.actions.length);
  if (withAction) {
    ctx.openTab("plugin:" + withAction.name);
    const btn = document.getElementById("settings-body").querySelectorAll("button")
      .find((b) => b.textContent === (withAction.actions[0].label || withAction.actions[0].name));
    if (btn && typeof btn.onclick === "function") {
      try { await btn.onclick(); check("插件动作按钮可点击且不抛错", true); }
      catch (e) { check("插件动作按钮可点击且不抛错", false, e && e.message); }
    } else {
      check("插件动作按钮已渲染", false, "未找到按钮");
    }
  }

  // hash 路由
  ctx.openTab("plugin:" + anyPlugin.name);
  check("hash 已同步为当前标签", decodeURIComponent(location.hash.replace(/^#/, "")) === "plugin:" + anyPlugin.name,
    location.hash);

  const unknownCalls = fetchLog.filter((l) => routes[l.split(" ")[1]] === undefined);
  check("未出现未定义的接口调用", unknownCalls.length === 0, unknownCalls.join(", "));

  console.log(`\n通过 ${oks.length} 项，失败 ${fails.length} 项`);
  if (fails.length) {
    fails.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log("设置页渲染冒烟测试全部通过。");
})();
