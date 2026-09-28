/* 小笼洛包 · 图形启动页渲染冒烟测试（无需浏览器：极简 DOM 桩 + fetch 桩）
 *
 * 用法：node tests/verify_gui_launcher_render.js web/static/launcher.js
 * 覆盖两个场景：
 *   A) 首次启动：渲染两个模式卡片 → 点击选择 → 显示启动步骤 → 服务就绪 → 同一窗口进入正式界面；
 *   B) 已有服务在运行时重新打开启动页：仍然显示两个选项（不沿用上次选择、不自动进入界面），
 *      重新点击任意模式都会重新拉起服务。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const jsPath = process.argv[2];
if (!jsPath || !fs.existsSync(jsPath)) {
  console.error("缺少 launcher.js：" + jsPath);
  process.exit(2);
}
const SRC = fs.readFileSync(jsPath, "utf-8");

const FAILS = [];
const OKS = [];
function check(name, cond, extra) {
  if (cond) { OKS.push(name); console.log("[OK]   " + name); }
  else { FAILS.push(name + (extra ? " " + extra : "")); console.log("[FAIL] " + name + (extra ? " " + extra : "")); }
}

const htmlPath = path.join(path.dirname(jsPath), "..", "launcher.html");
const HTML = fs.existsSync(htmlPath) ? fs.readFileSync(htmlPath, "utf-8") : "";

/* ==================== 每个场景一个独立沙箱 ==================== */
function createSandbox() {
  const byId = new Map();
  const all = [];

  class El {
    constructor(tag) {
      this.tagName = String(tag || "div").toUpperCase();
      this.children = [];
      this.parentNode = null;
      this.dataset = {};
      this.style = {};
      this.type = "";
      this.title = "";
      this.onclick = null;
      this._text = "";
      this._html = "";
      const set = new Set();
      this.classList = {
        add: (...c) => c.forEach((x) => x && set.add(x)),
        remove: (...c) => c.forEach((x) => set.delete(x)),
        contains: (c) => set.has(c),
        toggle: (c, f) => { const on = f === undefined ? !set.has(c) : !!f; if (on) set.add(c); else set.delete(c); return on; },
      };
      Object.defineProperty(this, "className", {
        get: () => Array.from(set).join(" "),
        set: (v) => {
          set.clear();
          String(v || "").split(/\s+/).filter(Boolean).forEach((c) => set.add(c));
        },
      });
      all.push(this);
    }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
    remove() { if (this.parentNode) this.parentNode.removeChild(this); }
    setAttribute(k, v) { if (k === "id") { this.id = v; byId.set(v, this); } else if (k === "class") { this.className = v; } else { this[k] = v; } }
    getAttribute(k) { return this[k]; }
    querySelector(sel) { return queryAll(sel)[0] || null; }
    querySelectorAll(sel) { return queryAll(sel); }
    matches(sel) {
      return String(sel).split(",").some((raw) => {
        const part = raw.trim();
        if (!part) return false;
        const idMatch = part.match(/#([\w-]+)/);
        if (idMatch && this.id !== idMatch[1]) return false;
        const classes = (part.match(/\.([\w-]+)/g) || []).map((c) => c.slice(1));
        if (!classes.every((c) => this.classList.contains(c))) return false;
        const tag = part.match(/^([a-zA-Z]+)/);
        if (tag && this.tagName !== tag[1].toUpperCase()) return false;
        return true;
      });
    }
    set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
    set innerHTML(v) { this._html = v == null ? "" : String(v); if (!v) { this.children = []; this._text = ""; } }
    get innerHTML() { return this._html; }
  }

  function queryAll(sel) {
    const out = [];
    const walk = (node) => node.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
    Array.from(byId.values()).forEach((r) => { if (r.matches(sel)) out.push(r); walk(r); });
    all.forEach((e) => { if (e.matches(sel) && !out.includes(e)) out.push(e); });
    return out;
  }

  const document = {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new El(tag),
    querySelectorAll: (sel) => queryAll(sel),
    querySelector: (sel) => queryAll(sel)[0] || null,
    body: new El("body"),
  };

  // 页面骨架：按真实 HTML 的 id + class 初始化（初始隐藏状态也一致）
  const ID_RE = /<[^>]*id="([\w-]+)"[^>]*>/g;
  let m;
  while ((m = ID_RE.exec(HTML)) !== null) {
    const tag = (m[0].match(/^<([a-zA-Z]+)/) || [])[1] || "div";
    const cls = (m[0].match(/class="([^"]*)"/) || [])[1] || "";
    const e = new El(tag);
    e.id = m[1];
    e.className = cls;
    byId.set(m[1], e);
  }
  ["launch-modes", "launch-progress", "launch-steps", "launch-note", "launch-actions", "launch-sub"]
    .forEach((id) => {
      if (!byId.has(id)) {
        const e = new El("div");
        e.id = id;
        if (id === "launch-progress") e.className = "launch-progress hidden";
        byId.set(id, e);
      }
    });

  const responses = [];
  const calls = [];
  async function fetchStub(url, opts) {
    calls.push({ url, method: (opts && opts.method) || "GET", body: opts && opts.body });
    const next = responses.shift();
    if (!next) throw new Error("没有预设响应：" + url);
    if (next instanceof Error) throw next;
    return { json: async () => next };
  }

  const timers = [];
  const intervals = [];
  const api = {
    byId, all, calls, responses, timers, intervals, queryAll,
    pumpTimers: () => { const t = timers.splice(0); t.forEach((fn) => fn()); },
    window: { location: { href: "" } },
  };
  const stubbed = {
    console, document, window: api.window, fetch: fetchStub, Math, Date, JSON, Promise,
    Object, String, Number, Array,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => { intervals.length = 0; },
  };
  api.stubbed = stubbed;
  return api;
}

const tick = () => new Promise((r) => setImmediate(r));
const textOf = (el) => (el ? el.textContent : "");

async function runScenario(responses) {
  const sb = createSandbox();
  sb.responses.push(...responses);
  vm.runInContext(SRC, vm.createContext(sb.stubbed));
  await tick();
  await tick();
  return sb;
}

const MODES = [
  { id: "lite", label: "Lite 模式", tag: "最快启动", desc: "只加载语音合成（GPT-SoVITS）", need: "适合：已配置外部 API" },
  { id: "standard", label: "标准模式", tag: "全部本地服务", desc: "启动 Ollama + GPT-SoVITS", need: "适合：本地运行" },
];
const STATE_IDLE = { mode: "", starting: false, done: false, steps: [], tts: {} };

(async () => {
  /* ---------- 场景 A：首次启动 → 选择 Lite → 就绪 → 进入界面 ---------- */
  const A = await runScenario([{ ok: true, modes: MODES, state: STATE_IDLE }]);

  const cards = A.queryAll(".launch-card");
  check("A 启动页渲染出两个模式卡片（Lite / 标准）", cards.length === 2, "实际 " + cards.length);
  check("A 卡片显示模式名称与说明",
        textOf(cards[0]).includes("Lite 模式") && textOf(cards[0]).includes("只加载语音合成"),
        textOf(cards[0]));
  check("A 卡片带适用场景提示（第二个为标准模式）",
        textOf(cards[1]).includes("标准模式") && textOf(cards[1]).includes("本地运行"), textOf(cards[1]));
  check("A 未选择时不显示进度区", A.byId.get("launch-progress").classList.contains("hidden"));

  A.responses.push({
    ok: true, mode: "lite",
    state: {
      mode: "lite", starting: true, done: false, error: "",
      steps: [
        { id: "check", label: "检查运行环境", desc: "关键文件与依赖", status: "done", detail: "齐全" },
        { id: "config", label: "读取启动配置", desc: "launcher_config.json", status: "running", detail: "" },
        { id: "services", label: "启动语音合成（GPT-SoVITS）", desc: "", status: "pending", detail: "" },
      ],
      tts: {},
    },
  });
  cards[0].onclick();
  await tick();
  await tick();

  const startCall = A.calls.find((c) => String(c.url).includes("/api/launch/start"));
  check("A 点击选项即按该模式调用启动接口",
        !!startCall && startCall.method === "POST" && String(startCall.body).includes('"lite"'),
        JSON.stringify(startCall || {}));
  const stepRows = A.queryAll(".launch-step");
  check("A 启动过程中显示每个步骤及其状态",
        stepRows.length === 3 && textOf(stepRows[0]).includes("检查运行环境")
        && textOf(stepRows[1]).includes("读取启动配置"), "步骤数 " + stepRows.length);
  check("A 进行中的步骤有 running 标记（用于动画提示）",
        A.queryAll(".launch-step.running").length === 1,
        "实际 " + A.queryAll(".launch-step.running").length);
  check("A 轮询已开始（等待启动进度）", A.intervals.length >= 1, "定时器数 " + A.intervals.length);

  A.responses.push({
    ok: true,
    state: {
      mode: "lite", starting: false, done: true, error: "",
      steps: [
        { id: "check", label: "检查运行环境", status: "done", detail: "齐全" },
        { id: "config", label: "读取启动配置", status: "done", detail: "" },
        { id: "services", label: "启动语音合成（GPT-SoVITS）", status: "done", detail: "已拉起" },
        { id: "tts", label: "等待语音服务就绪", status: "done", detail: "" },
      ],
      tts: { ready: true, port: 20000 },
    },
  });
  await A.intervals[0]();
  await tick();
  await tick();

  check("A 服务拉起后提示已就绪（含实际端口）",
        textOf(A.byId.get("launch-note")).includes("服务已就绪")
        && textOf(A.byId.get("launch-note")).includes("20000"),
        textOf(A.byId.get("launch-note")));
  const goBtn = A.queryAll(".launch-go")[0];
  check("A 出现「进入对话界面」按钮", !!goBtn && textOf(goBtn).includes("进入对话界面"));
  check("A 启动完成后停止轮询（不再重复请求）", A.intervals.length === 0, "定时器数 " + A.intervals.length);

  A.pumpTimers();
  await tick();
  check("A 服务拉起后同一窗口进入正式界面（跳转 /）",
        A.window.location.href === "/", String(A.window.location.href));

  /* ---------- 场景 B：已有服务在运行时重新打开启动页 ---------- */
  const B = await runScenario([{
    ok: true, modes: MODES,
    state: {
      mode: "lite", starting: false, done: true, error: "",
      steps: [{ id: "services", label: "启动语音合成（GPT-SoVITS）", status: "done", detail: "" }],
      tts: { ready: true, port: 20000 },
    },
  }]);
  const cardsB = B.queryAll(".launch-card");
  check("B 已启动过时重新打开启动页仍然显示两个模式选项", cardsB.length === 2, "实际 " + cardsB.length);
  check("B 不会自动沿用上次选择直接进入界面（停在启动页）",
        B.window.location.href === "", String(B.window.location.href));
  check("B 提示可以重新选择模式",
        textOf(B.byId.get("launch-sub")).includes("重新选择"), textOf(B.byId.get("launch-sub")));
  check("B 进度区保持隐藏（等用户选择）", B.byId.get("launch-progress").classList.contains("hidden"));

  B.responses.push({ ok: true, mode: "standard", state: { mode: "standard", starting: true, done: false, steps: [], tts: {} } });
  cardsB[1].onclick();
  await tick();
  await tick();
  const startCallB = B.calls.find((c) => String(c.url).includes("/api/launch/start"));
  check("B 重新选择模式会按新选择重新拉起服务",
        !!startCallB && String(startCallB.body).includes('"standard"'),
        JSON.stringify(startCallB || {}));

  console.log("");
  if (FAILS.length) {
    console.log("通过 " + OKS.length + " 项，失败 " + FAILS.length + " 项：");
    FAILS.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log("通过 " + OKS.length + " 项，失败 0 项");
  console.log("图形启动页渲染冒烟测试通过。");
})();
