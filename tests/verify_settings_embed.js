/* 小笼洛包 · 设置页「被主页浮层嵌入」时的行为测试（node + 极简 DOM 桩）
 *
 * 用法：node tests/verify_settings_embed.js
 * 由 verify_settings_page.py 调用。验证的是本轮改动：
 *   进入设置不再整页跳转（主页浮层 + iframe），所以设置页要认得「自己被嵌入」：
 *     1) 「返回对话」变成「关闭设置」，点击只回传关闭消息，不做跳转；
 *     2) 保存设置后回传「音量」与「已保存」消息，主页据此让音量即时生效（播放不中断）；
 *     3) 直接打开设置页（非嵌入）时不回传任何消息，行为与以前一致。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ==================== 极简 DOM 桩 ==================== */
class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
}

function matchPart(el, part) {
  part = String(part).trim();
  if (part.startsWith("#")) return el.id === part.slice(1);
  const cls = part.match(/^\.([\w-]+)$/);
  if (cls) return el.classList.contains(cls[1]);
  const tag = part.match(/^([a-zA-Z]+)$/);
  if (tag) return el.tagName === tag[1].toUpperCase();
  return false;
}

function descendants(root, out = []) {
  root.children.forEach((c) => { out.push(c); descendants(c, out); });
  return out;
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.classList = new ClassList();
    this.style = { setProperty() {}, removeProperty() {} };
    this._text = "";
    this._className = "";
    this._listeners = {};
    this.value = "";
    this.checked = false;
    this.title = "";
    this.rows = undefined;
  }
  get className() { return this._className; }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get id() { return this._id || ""; }
  set id(v) { this._id = v; }
  get textContent() {
    if (this._text) return this._text;
    return this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(v) { this._text = v == null ? "" : String(v); }
  get innerHTML() { return this._html || ""; }
  set innerHTML(v) { this._html = v == null ? "" : String(v); this.children = []; }
  get childNodes() { return this.children; }
  get options() { return this.children; }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((n) => n && this.appendChild(n)); }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  fire(type, evt) {
    const e = evt || {};
    if (typeof e.preventDefault !== "function") e.preventDefault = () => { e.defaultPrevented = true; };
    (this._listeners[type] || []).forEach((fn) => fn(e));
    return e;
  }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
  scrollIntoView() {}
  focus() {}
  blur() {}
  click() { if (typeof this.onclick === "function") return this.onclick(); }
}

function queryAll(root, selector) {
  const parts = String(selector).split(",").map((s) => s.trim()).filter(Boolean);
  return descendants(root).filter((el) => parts.some((p) => matchPart(el, p)));
}

const settingsSrc = fs.readFileSync(path.join(__dirname, "..", "web", "static", "settings.js"), "utf-8");
const memorySrc = fs.readFileSync(path.join(__dirname, "..", "web", "static", "memory_view.js"), "utf-8");
const htmlSrc = fs.readFileSync(path.join(__dirname, "..", "web", "settings.html"), "utf-8");
const htmlIds = [...new Set([...htmlSrc.matchAll(/id="([^"]+)"/g)].map((m) => m[1]))];
const modalIds = [...new Set([...memorySrc.matchAll(/id="([a-zA-Z0-9_-]+)"/g)].map((m) => m[1]))];

/* 造一个 settings.js 运行环境；embedded=true 时模拟「被主页 iframe 嵌入」 */
function buildContext(embedded) {
  const registry = new Map();
  const docListeners = {};
  const document = {
    body: new El("body"),
    createElement: (tag) => new El(tag),
    getElementById: (id) => registry.get(id) || null,
    querySelector: (sel) => queryAll(document.body, sel)[0] || null,
    querySelectorAll: (sel) => queryAll(document.body, sel),
    addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn); },
    hidden: false,
  };
  const body = document.body;
  htmlIds.concat(modalIds).forEach((id) => {
    const n = new El("div");
    n.id = id;
    n.id = id;
    registry.set(id, n);
    body.appendChild(n);
  });
  if (!registry.has("settings-nav")) {
    const nav = new El("aside");
    nav.id = "settings-nav";
    registry.set("settings-nav", nav);
    body.appendChild(nav);
  }
  // 与 settings.html 一致的返回链接（本轮改动会替换它的文案 / 行为）
  const back = new El("a");
  back.className = "btn ghost back-link";
  back.textContent = "← 返回对话";
  body.appendChild(back);

  const posts = [];
  const location = { hash: "", href: "http://127.0.0.1:10999/settings.html?embed=1" };
  const routes = {
    "/api/settings": { ok: true, settings: { tts_volume: 1, music_volume: 0.7, character_name: "A" } },
    "/api/characters": { ok: true, presets: [] },
    "/api/characters/manage": { ok: true, presets: [] },
    "/api/voice_presets": { ok: true, presets: [] },
    "/api/history": { ok: true, records: [] },
    "/api/plugins": { ok: true, plugins: [] },
    "/api/models": { ok: true, ollama_models: [] },
    "/api/tts/service": { ok: true, service: { ready: true, running: true, port: 20000 } },
    "/api/plugins/settings": { ok: true, settings: {} },
    "/api/confirm/prepare": { ok: true, token: "t", expires_in: 120 },
    "/api/usage": { ok: true },
    "/api/save": { ok: true },
    "/api/history/mark": { ok: true },
  };
  const fetchLog = [];
  const fetchStub = (url, opts) => {
    const clean = String(url).split("?")[0];
    const method = (opts && opts.method) || "GET";
    fetchLog.push(method + " " + clean);
    const data = clean === "/api/settings" && method === "POST"
      ? { ok: true, settings: Object.assign({}, routes["/api/settings"].settings, saveEcho) }
      : routes[clean];
    return Promise.resolve({
      json: () => Promise.resolve(data === undefined ? { ok: false, error: "未提供数据: " + clean } : data),
    });
  };
  let saveEcho = {};       // 模拟后端把刚保存的值写回来（saveSettings 会回传 state.settings）

  const winListeners = {};
  const ctx = {
    console, setTimeout, clearTimeout, Promise, JSON, Object, Array, String, Number, Boolean,
    Math, Date, RegExp, Error, Set, Map, encodeURIComponent, decodeURIComponent,
    fetch: fetchStub, document, location, sessionStorage: { getItem: () => null, setItem() {} },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn); },
    Image: function () {},
    Blob: function () {},
  };
  ctx.window = ctx;
  ctx.self = embedded ? { embedded: true } : ctx;      // 嵌入时 window.self !== window.top
  ctx.top = ctx;
  ctx.parent = { postMessage: (msg, origin) => posts.push({ msg, origin }) };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  return { ctx, posts, fetchLog, document, back, registry, setSaveEcho: (v) => { saveEcho = v; }, docListeners };
}

const fails = [];
const oks = [];
function check(name, cond, extra) {
  if (cond) { oks.push(name); console.log("[OK]   " + name); }
  else { fails.push(name + (extra ? " " + extra : "")); console.log("[FAIL] " + name + (extra ? " " + extra : "")); }
}

(async function main() {
  /* ---------- 1. 被浮层嵌入 ---------- */
  const embed = buildContext(true);
  vm.runInContext(memorySrc, embed.ctx, { filename: "memory_view.js" });
  vm.runInContext(settingsSrc, embed.ctx, { filename: "settings.js" });
  await new Promise((r) => setTimeout(r, 300));      // 等 init() 的异步链跑完

  check("嵌入时返回链接改成「关闭设置」", embed.back.textContent === "← 关闭设置", embed.back.textContent);
  check("嵌入时给 body 加 embed 标记（样式据此隐藏本页顶栏、让背景透出）",
        embed.document.body.classList.contains("embed"),
        embed.document.body.className);
  const clickEvt = embed.back.fire("click");
  check("点「关闭设置」不回传跳转、只回传关闭消息（主页据此关浮层）",
        clickEvt.defaultPrevented === true
        && embed.posts.some((p) => p.msg && p.msg.type === "xllb:close-settings"),
        JSON.stringify(embed.posts.map((p) => p.msg)));

  // 保存「声音设置」里的音乐音量：应同时回传音量与保存事件
  embed.setSaveEcho({ music_volume: 0.42 });
  await embed.ctx.saveSettings({ music_volume: 0.42 });
  const types = embed.posts.map((p) => p.msg && p.msg.type);
  check("保存音量后回传 xllb:volumes（主页据此立即改音量，不用等回到主页）",
        types.includes("xllb:volumes"), JSON.stringify(types));
  const volMsg = embed.posts.map((p) => p.msg).find((m) => m && m.type === "xllb:volumes");
  check("回传的音量数值取自保存结果",
        !!volMsg && volMsg.music_volume === 0.42, JSON.stringify(volMsg));
  check("保存后同时回传 xllb:settings-saved（其它设置同步界面）",
        types.includes("xllb:settings-saved"), JSON.stringify(types));
  check("回传消息带同源地址（主页按来源校验后才会采纳）",
        embed.posts.every((p) => !p.origin || p.origin === "http://127.0.0.1:10999"),
        JSON.stringify(embed.posts.map((p) => p.origin)));

  const beforeCount = embed.posts.length;
  await embed.ctx.saveSettings({ language: "zh" });          // 与音量无关的设置
  const newTypes = embed.posts.slice(beforeCount).map((p) => p.msg && p.msg.type);
  check("与音量无关的设置不会误发音量消息（只发保存事件）",
        !newTypes.includes("xllb:volumes") && newTypes.includes("xllb:settings-saved"),
        JSON.stringify(newTypes));

  /* ---------- 2. 直接打开设置页（非嵌入） ---------- */
  const standalone = buildContext(false);
  vm.runInContext(memorySrc, standalone.ctx, { filename: "memory_view.js" });
  vm.runInContext(settingsSrc, standalone.ctx, { filename: "settings.js" });
  await new Promise((r) => setTimeout(r, 300));

  check("非嵌入时返回链接保持原样（仍是整页返回对话）",
        standalone.back.textContent === "← 返回对话", standalone.back.textContent);
  standalone.setSaveEcho({ music_volume: 0.3 });
  await standalone.ctx.saveSettings({ music_volume: 0.3 });
  check("非嵌入（独立打开设置页）时不回传任何消息",
        standalone.posts.length === 0, JSON.stringify(standalone.posts));

  console.log("");
  if (fails.length) {
    console.log(`通过 ${oks.length} 项，失败 ${fails.length} 项：`);
    fails.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log(`通过 ${oks.length} 项，失败 0 项`);
  console.log("设置页嵌入模式 验证通过。");
})().catch((e) => {
  console.error("测试脚本异常：" + (e && e.stack ? e.stack : e));
  process.exit(1);
});
