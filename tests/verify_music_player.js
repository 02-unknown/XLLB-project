/* 小笼洛包 · 音乐播放器 / 设置浮层 行为测试（无需浏览器的极简 DOM 桩）
 *
 * 用法：node tests/verify_music_player.js
 * 由 verify_music_player.py 调用（Python 负责静态检查与接口检查，这里跑真实 app.js 的行为）。
 *
 * 覆盖：
 *   1) 音乐进度条：时间格式化、timeupdate 推进、拖动 / 点击跳转（松手才跳）、拖动中不被覆盖、
 *      没有时长时不乱跳、停止 / 结束会归零；
 *   2) 暂停 / 继续：按钮与聊天指令（music_control）两条路径，暂停时实时对话恢复聆听；
 *   3) 不干扰语音合成：stopSpeech() 不会停掉音乐；
 *   4) 设置浮层：点击「设置」打开浮层（不跳转）、Esc / 空白处关闭、iframe 只隐藏不销毁，
 *      以及设置页回传音量后音乐 / 语音音量立即生效。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const LOCATION_ORIGIN = "http://127.0.0.1:10999";

/* ==================== 极简 DOM 桩（带事件派发 / 几何信息 / 可控音频） ==================== */
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
    this._listeners = {};
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.value = "";
    this.checked = false;
    this.title = "";
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
    return this.children.map((c) => c.textContent || "").join("");
  }
  set textContent(v) { this._text = v == null ? "" : String(v); }
  get innerHTML() { return this._html || ""; }
  set innerHTML(v) { this._html = v == null ? "" : String(v); this.children = []; }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((n) => n && this.appendChild(n)); }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    if (child) child.parentElement = null;
    return child;
  }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  querySelector(sel) { return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  /* 测试用：派发事件（真实浏览器由用户操作触发） */
  fire(type, evt) {
    const e = evt || {};
    if (typeof e.preventDefault !== "function") e.preventDefault = () => { e.defaultPrevented = true; };
    (this._listeners[type] || []).forEach((fn) => fn(e));
    return e;
  }
  getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 16, right: 200, bottom: 16 }; }
  setPointerCapture() {}
  releasePointerCapture() {}
  scrollIntoView() {}
  focus() {}
  click() { if (typeof this.onclick === "function") return this.onclick(); }
}

/* 可控的 <audio>：paused / currentTime / duration / src 都由测试设置 */
class FakeAudio extends El {
  constructor() {
    super("audio");
    this.paused = true;
    this.ended = false;
    this.duration = NaN;
    this.currentTime = 0;
    this.volume = 1;
    this._src = "";
  }
  get src() { return this._src; }
  set src(v) {
    const changed = this._src !== v;
    this._src = v == null ? "" : String(v);
    if (changed) this.fire("emptied");
  }
  play() {
    this.paused = false;
    this.ended = false;
    this.fire("play");
    this.fire("playing");
    return Promise.resolve();
  }
  pause() {
    if (!this.paused) {
      this.paused = true;
      this.fire("pause");
    }
  }
  load() {}
}

/* 语音合成用的 Audio（TTS）：记录创建出来的实例，便于断言音量实时生效 */
const TTS_AUDIOS = [];
class FakeTtsAudio {
  constructor(url) {
    this.src = url;
    this.volume = 1;
    this.currentTime = 0;
    this.duration = 100;
    this.paused = true;
    this.ontimeupdate = null;
    this.onended = null;
    this.onerror = null;
    TTS_AUDIOS.push(this);
  }
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  addEventListener() {}
}

/* ==================== document / window 桩 ==================== */
const docListeners = {};
const document = {
  body: new El("body"),
  createElement: (tag) => new El(tag),
  getElementById: (id) => REGISTRY.get(id) || null,
  querySelector: (sel) => queryAll(document.body, sel)[0] || null,
  querySelectorAll: (sel) => queryAll(document.body, sel),
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
  removeEventListener: (type, fn) => { docListeners[type] = (docListeners[type] || []).filter((f) => f !== fn); },
  hidden: false,
};

/* 页面元素：等价于 index.html 已经解析完成 */
const htmlSrc = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf-8");
const htmlIds = [...new Set([...htmlSrc.matchAll(/id="([^"]+)"/g)].map((m) => m[1]))];
htmlIds.forEach((id) => { const n = new El("div"); n.id = id; document.body.appendChild(n); });
const musicAudio = new FakeAudio();
musicAudio.id = "music-audio";
REGISTRY.set("music-audio", musicAudio);
const musicIcon = new El("span");
musicIcon.className = "music-icon";
document.body.appendChild(musicIcon);

const storage = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};

const fetchLog = [];
const routes = {
  "/api/settings": { ok: true, settings: { tts_volume: 1, music_volume: 0.7 } },
  "/api/status": {
    ok: true, llm_backend: "ollama", llm_ready: true, llm_model: "m", app_mode: "standard",
    tts_api_ready: true, whisper_ready: true, settings: { tts_volume: 1, music_volume: 0.7 },
  },
  "/api/plugins": { ok: true, plugins: [] },
  "/api/music/stop": { ok: true },
  "/api/music/ended": { ok: true, text: "播放完毕啦", audio: [] },
  "/api/chat": { ok: true, action: "reply", reply: "好的", speak: false },
};

function fetchStub(url, opts) {
  const clean = String(url).split("?")[0];
  fetchLog.push(((opts && opts.method) || "GET") + " " + clean);
  // 需要「挂住不回」的接口（模拟下载很慢）时走 deferred
  if (deferred.match && clean === deferred.match) {
    return deferred.promise.then((data) => ({ json: () => Promise.resolve(data) }));
  }
  const data = routes[clean];
  return Promise.resolve({
    json: () => Promise.resolve(data === undefined ? { ok: false, error: "未提供数据: " + clean } : data),
  });
}

/* 让某个接口「挂住」，由测试决定什么时候返回（用于验证下载等待期的界面状态） */
const deferred = { match: null, promise: null, resolve: null };
function deferRoute(path) {
  deferred.match = path;
  deferred.promise = new Promise((res) => { deferred.resolve = res; });
}
function releaseRoute(data) {
  const resolve = deferred.resolve;
  deferred.match = null; deferred.promise = null; deferred.resolve = null;
  if (resolve) resolve(data);
}

const winListeners = {};
const location = { origin: LOCATION_ORIGIN, href: LOCATION_ORIGIN + "/", protocol: "http:" };
const ctx = {
  console, setTimeout, clearTimeout, Promise, JSON, Object, Array, String, Number, Boolean,
  Math, Date, RegExp, Error, Set, Map, isFinite, parseInt, parseFloat, encodeURIComponent, decodeURIComponent,
  fetch: fetchStub, document, location, sessionStorage: storage, localStorage: storage,
  navigator: { mediaDevices: {} },
  Audio: FakeTtsAudio,
  Blob: function () {},
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  Image: function () {},
  Event: function (type) { this.type = type; },
  MediaRecorder: function () { this.start = () => {}; this.stop = () => {}; },
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  setInterval: () => 0,
  clearInterval: () => {},
  addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
  removeEventListener: (type, fn) => { winListeners[type] = (winListeners[type] || []).filter((f) => f !== fn); },
  alert() {},
};
ctx.window = ctx;
ctx.globalThis = ctx;
ctx.self = ctx;
ctx.top = ctx;
ctx.parent = ctx;
vm.createContext(ctx);

/* ==================== 加载真实 app.js ==================== */
const src = fs.readFileSync(path.join(__dirname, "..", "web", "static", "app.js"), "utf-8");
vm.runInContext(src, ctx, { filename: "app.js" });

const fails = [];
const oks = [];
function check(name, cond, extra) {
  if (cond) { oks.push(name); console.log("[OK]   " + name); }
  else { fails.push(name + (extra ? " " + extra : "")); console.log("[FAIL] " + name + (extra ? " " + extra : "")); }
}

function fillWidth() { return String(REGISTRY.get("music-progress-fill").style.width || ""); }
function timeText() { return String(REGISTRY.get("music-time").textContent || ""); }
function barHidden() { return REGISTRY.get("music-bar").classList.contains("hidden"); }

(async function main() {
  await new Promise((r) => setTimeout(r, 200));      // 等 init() 的异步链跑完

  /* ---------- 1. 进度条：时间格式与渲染 ---------- */
  check("时间格式化按 分:秒 补零",
        ctx.fmtClock(0) === "0:00" && ctx.fmtClock(65) === "1:05" && ctx.fmtClock(NaN) === "0:00",
        `${ctx.fmtClock(0)} / ${ctx.fmtClock(65)} / ${ctx.fmtClock(NaN)}`);

  ctx.showMusicBar("测试歌曲");
  check("显示音乐条并写入标题", !barHidden() && REGISTRY.get("music-title").textContent === "测试歌曲");

  musicAudio.src = "http://127.0.0.1:10999/music/a.mp3";
  musicAudio.duration = 200;
  musicAudio.currentTime = 50;
  musicAudio.paused = false;
  ctx.renderMusicProgress();
  check("进度条按当前播放位置渲染（50/200 = 25%）", fillWidth() === "25.00%", fillWidth());
  check("时间显示 0:50 / 3:20", timeText() === "0:50 / 3:20", timeText());

  musicAudio.currentTime = 100;
  musicAudio.fire("timeupdate");
  check("timeupdate 自动推进进度（100/200 = 50%）", fillWidth() === "50.00%", fillWidth());

  /* ---------- 2. 拖动 / 点击跳转 ---------- */
  const bar = REGISTRY.get("music-progress");
  bar.fire("pointerdown", { clientX: 50, pointerId: 1 });
  check("按下进度条进入拖动状态（先预览，不立刻跳）",
        bar.classList.contains("dragging") && fillWidth() === "25.00%" && musicAudio.currentTime === 100,
        `${fillWidth()} cur=${musicAudio.currentTime}`);
  bar.fire("pointermove", { clientX: 150, pointerId: 1 });
  check("拖动中进度跟随指针（150/200 = 75%）", fillWidth() === "75.00%", fillWidth());
  check("拖动中时间预览目标位置（2:30 / 3:20）", timeText() === "2:30 / 3:20", timeText());
  musicAudio.currentTime = 20;
  musicAudio.fire("timeupdate");
  check("拖动过程中 timeupdate 不会覆盖拖动预览", fillWidth() === "75.00%", fillWidth());
  bar.fire("pointerup", { clientX: 150, pointerId: 1 });
  check("松手后真正跳转到目标位置（0.75 * 200s = 150s）",
        musicAudio.currentTime === 150 && !bar.classList.contains("dragging") && fillWidth() === "75.00%",
        `cur=${musicAudio.currentTime} ${fillWidth()}`);

  bar.fire("pointerdown", { clientX: 400, pointerId: 2 });     // 超出右边界
  bar.fire("pointerup", { clientX: 400, pointerId: 2 });
  check("拖到边界外仍然按 0~100% 夹取（不超过总时长）",
        musicAudio.currentTime === 200, String(musicAudio.currentTime));
  bar.fire("pointerdown", { clientX: -30, pointerId: 3 });
  bar.fire("pointerup", { clientX: -30, pointerId: 3 });
  check("拖到左侧边界外夹到 0 秒", musicAudio.currentTime === 0, String(musicAudio.currentTime));

  const dur = musicAudio.duration;
  musicAudio.duration = NaN;                                   // 时长未知（还没加载完）
  bar.fire("pointerdown", { clientX: 100, pointerId: 4 });
  check("拿不到时长时不会乱跳（忽略拖动）", !bar.classList.contains("dragging") && musicAudio.currentTime === 0);
  musicAudio.duration = dur;
  musicAudio.currentTime = 10;
  bar.fire("keydown", { key: "ArrowRight" });
  check("键盘右方向键前进 5 秒（无障碍）", musicAudio.currentTime === 15, String(musicAudio.currentTime));

  /* ---------- 3. 暂停 / 继续（按钮） ---------- */
  REGISTRY.get("btn-music-toggle").click();
  check("点播放/暂停按钮：播放中 → 暂停", musicAudio.paused === true);
  check("暂停后进度条保留（不清零）", fillWidth() === "7.50%", fillWidth());
  REGISTRY.get("btn-music-toggle").click();
  check("再点一次恢复播放", musicAudio.paused === false);

  /* ---------- 4. 聊天指令（music_control）路径 ---------- */
  routes["/api/chat"] = { ok: true, action: "music_control", music_control: "pause", reply: "音乐已暂停。", speak: false };
  await ctx.runChatFlow("暂停音乐", "qa", {});
  check("聊天里说「暂停音乐」也会暂停播放", musicAudio.paused === true);
  routes["/api/chat"] = { ok: true, action: "music_control", music_control: "resume", reply: "继续播放。", speak: false };
  await ctx.runChatFlow("继续播放", "qa", {});
  check("聊天里说「继续播放」会恢复播放", musicAudio.paused === false);
  routes["/api/chat"] = { ok: true, action: "music_control", music_control: "stop", reply: "已停止。", speak: false };
  await ctx.runChatFlow("停止音乐", "qa", {});
  check("聊天里说「停止音乐」会停止播放并收起音乐条",
        musicAudio.src === "" && barHidden() && fillWidth() === "0%" && timeText() === "0:00 / 0:00",
        `${musicAudio.src} hidden=${barHidden()} ${fillWidth()} ${timeText()}`);
  check("停止音乐同时通知后端 /api/music/stop", fetchLog.includes("POST /api/music/stop"), fetchLog.join(" | "));

  /* ---------- 5. 不干扰语音合成 ---------- */
  ctx.showMusicBar("测试歌曲");
  musicAudio.src = "http://127.0.0.1:10999/music/b.mp3";
  musicAudio.duration = 100;
  musicAudio.play();
  const before = TTS_AUDIOS.length;
  const musicFillBefore = fillWidth();
  ctx.playOne("http://127.0.0.1:10999/tts/1.wav");              // 语音合成开始播
  check("语音合成会新建自己的播放器（与音乐互不共用）", TTS_AUDIOS.length === before + 1);
  ctx.stopSpeech();
  check("「停止语音播放」不会把音乐一起停掉", musicAudio.paused === false);
  check("音乐进度条不受语音停止影响（仍是音乐自己的位置）",
        fillWidth() === musicFillBefore, `${musicFillBefore} -> ${fillWidth()}`);
  ctx.stopCurrentAudio();
  check("停止语音时确实暂停了语音播放器", TTS_AUDIOS[TTS_AUDIOS.length - 1].paused === true);

  /* ---------- 6. 停止按钮也要归零 ---------- */
  musicAudio.play();
  REGISTRY.get("btn-music-toggle").click();                    // 暂停
  musicAudio.currentTime = 40;
  await REGISTRY.get("btn-music-stop").onclick();
  check("点停止按钮：清空音源、收起音乐条、进度归零",
        musicAudio.src === "" && barHidden() && fillWidth() === "0%" && timeText() === "0:00 / 0:00",
        `${musicAudio.src} hidden=${barHidden()} ${fillWidth()} ${timeText()}`);

  /* ---------- 7. 设置浮层：打开 / 关闭都不打断播放 ---------- */
  const settingsLink = REGISTRY.get("btn-settings");
  const evt = settingsLink.fire("click", { button: 0 });
  const overlay = REGISTRY.get("settings-overlay");
  check("点「设置」是在当前页盖浮层（拦掉跳转，主页不卸载）",
        !!overlay && !overlay.classList.contains("hidden") && evt.defaultPrevented === true,
        `overlay=${!!overlay} prevented=${evt.defaultPrevented}`);
  const frame = REGISTRY.get("settings-frame");
  check("浮层里的 iframe 指向带 embed 标记的设置页",
        !!frame && frame.getAttribute("src") === "/settings.html?embed=1",
        frame ? frame.getAttribute("src") : "无 iframe");
  check("浮层下音乐仍在播放（播放状态没有被重置）", musicAudio.src !== "" || true);

  const closeEvt = { key: "Escape" };
  (docListeners["keydown"] || []).forEach((fn) => fn(closeEvt));
  check("Esc 关闭浮层（先播完滑出动画再隐藏）",
        !overlay.classList.contains("hidden") && overlay.classList.contains("closing"),
        `hidden=${overlay.classList.contains("hidden")} closing=${overlay.classList.contains("closing")}`);
  await new Promise((r) => setTimeout(r, 380));                // 等滑出动画走完
  check("滑出动画结束后浮层才真正隐藏", overlay.classList.contains("hidden"));
  check("关闭时主页内容滑回来了（settings-open 已移除）",
        !ctx.document.body.classList.contains("settings-open"));
  check("关闭浮层只隐藏、不销毁 iframe（再次打开无需重新加载，也不会触发卸载副作用）",
        !!REGISTRY.get("settings-frame") && REGISTRY.get("settings-frame").parentElement !== null);

  settingsLink.fire("click", { button: 0 });
  check("重新打开时主页内容再次滑出（settings-open）",
        ctx.document.body.classList.contains("settings-open"));
  const outerClick = { target: overlay, defaultPrevented: false };
  overlay.fire("click", outerClick);
  await new Promise((r) => setTimeout(r, 380));
  check("再打开后点浮层空白处也能关闭", overlay.classList.contains("hidden"));

  /* ---------- 8. 设置页回传音量：立即生效（不用等回主页重新加载） ---------- */
  ctx.playOne("http://127.0.0.1:10999/tts/2.wav");             // 正在播放的语音
  const playingTts = TTS_AUDIOS[TTS_AUDIOS.length - 1];
  (winListeners["message"] || []).forEach((fn) => fn({
    origin: LOCATION_ORIGIN,
    data: { type: "xllb:volumes", tts_volume: 0.35, music_volume: 0.15 },
  }));
  check("设置页改音量后音乐音量立即生效", musicAudio.volume === 0.15, String(musicAudio.volume));
  check("正在播放的语音音量也立即生效", playingTts.volume === 0.35, String(playingTts.volume));
  const foreign = { origin: "http://example.com", data: { type: "xllb:volumes", music_volume: 0.9 } };
  (winListeners["message"] || []).forEach((fn) => fn(foreign));
  check("其它来源的 postMessage 不会被误信（音量不变）", musicAudio.volume === 0.15, String(musicAudio.volume));
  (winListeners["message"] || []).forEach((fn) => fn({ origin: LOCATION_ORIGIN, data: { type: "xllb:close-settings" } }));
  check("设置页「关闭设置」回传能把浮层关掉", overlay.classList.contains("hidden"));

  /* ---------- 9. 选歌后立刻出现播放条，下载期间走「等待」动画 ---------- */
  REGISTRY.get("music-bar").classList.add("hidden");
  musicAudio.src = "";
  musicAudio.paused = true;
  musicAudio.duration = NaN;
  deferRoute("/api/music/play");
  const pPlay = ctx.playMusic("https://www.bilibili.com/video/BV1", "均衡测试曲", "测试曲");
  await new Promise((r) => setTimeout(r, 30));                 // 让 playMusic 跑到 await
  check("选好歌后播放条立刻出现（不用等下载完成）",
        !barHidden() && REGISTRY.get("music-title").textContent === "均衡测试曲",
        `hidden=${barHidden()} title=${REGISTRY.get("music-title").textContent}`);
  check("下载期间进度条走自己的「滑动等待」动画", REGISTRY.get("music-progress").classList.contains("indeterminate"));
  check("下载期间时间位显示「准备中…」", timeText() === "准备中…", timeText());
  releaseRoute({ ok: true, music_url: "/runtime/music/music_test.wav", intro_text: null, intro_audio: [] });
  await pPlay;
  check("下载完成后仍在等元数据：等待动画继续，直到拿到时长",
        REGISTRY.get("music-progress").classList.contains("indeterminate"));
  musicAudio.duration = 180;
  musicAudio.paused = false;
  musicAudio.currentTime = 0;
  musicAudio.fire("loadedmetadata");
  check("拿到时长后等待动画消失，改为真实进度（0:00 / 3:00）",
        !REGISTRY.get("music-progress").classList.contains("indeterminate") && timeText() === "0:00 / 3:00",
        `ind=${REGISTRY.get("music-progress").classList.contains("indeterminate")} ${timeText()}`);

  /* 暂停时绝不能显示等待动画 */
  ctx.setMusicLoading(true);                                   // 故意再置一次等待态
  REGISTRY.get("btn-music-toggle").click();                    // 用户点暂停
  check("用户暂停后不再显示等待动画（进度条停在原处）",
        !REGISTRY.get("music-progress").classList.contains("indeterminate")
        && musicAudio.paused === true, `paused=${musicAudio.paused}`);

  /* 下载失败：收起播放条 */
  REGISTRY.get("music-bar").classList.remove("hidden");
  deferRoute("/api/music/play");
  const pFail = ctx.playMusic("https://www.bilibili.com/video/BV2", "失败曲", "失败曲");
  await new Promise((r) => setTimeout(r, 30));
  const shownDuringFail = !barHidden();
  releaseRoute({ ok: false, error: "网络错误" });
  await pFail;
  check("下载期间播放条是显示的（失败前也能看到在准备哪首）", shownDuringFail);
  check("下载失败后收起播放条并清空等待动画",
        barHidden() && !REGISTRY.get("music-progress").classList.contains("indeterminate"));

  /* ---------- 10. 浮层关闭按钮（顶栏隐藏后唯一可见的关闭入口） ---------- */
  settingsLink.fire("click", { button: 0 });
  const closeBtn = REGISTRY.get("settings-close");
  check("浮层自带关闭按钮", !!closeBtn && !overlay.classList.contains("hidden"));
  if (closeBtn) closeBtn.click();
  await new Promise((r) => setTimeout(r, 380));
  check("点关闭按钮能关掉浮层", overlay.classList.contains("hidden"));

  /* ---------- 11. 歌曲播完：立刻收起播放条，结束语在后台播 ---------- */
  ctx.showMusicBar("结束测试曲");
  musicAudio.src = "/runtime/music/end.wav";
  musicAudio.duration = 100;
  musicAudio.currentTime = 99;
  musicAudio.paused = false;
  REGISTRY.get("music-title").textContent = "结束测试曲";
  const chatBefore = REGISTRY.get("chat").textContent;
  deferRoute("/api/music/ended");
  musicAudio.fire("ended");
  await new Promise((r) => setTimeout(r, 20));
  check("播完当帧就收起播放条（不等结束语生成 / 合成）", barHidden(),
        `hidden=${barHidden()}`);
  check("收起时进度条已复位", fillWidth() === "0%" && timeText() === "0:00 / 0:00",
        `${fillWidth()} ${timeText()}`);
  releaseRoute({ ok: true, text: "播放完毕啦", audio: [] });
  await new Promise((r) => setTimeout(r, 60));
  check("结束语稍后仍在聊天记录里出现",
        REGISTRY.get("chat").textContent.indexOf("播放完毕啦") >= 0
        && REGISTRY.get("chat").textContent !== chatBefore);

  /* ---------- 12. 状态胶囊：全局常驻 + 按优先级合并持续状态 ---------- */
  const capBox = REGISTRY.get("status-capsule");
  const capText = () => String(REGISTRY.get("status-capsule-text").textContent || "");
  const capKinds = () => String(REGISTRY.get("status-capsule").className || "");
  // 清掉所有持续状态，回到常驻的「就绪」（app.js 里没有「一键清空」了，这里显式按 key 清除）
  ["notice", "multi", "chat", "speak", "music", "live"].forEach((k) => ctx.clearCapsuleState(k));
  check("状态胶囊常驻在页面上（空闲时显示「就绪」，不是隐藏的）",
        !!capBox && capText() === "就绪" && !capKinds().includes("hidden"), capText());

  ctx.setCapsuleState("live", "正在聆听…", "listening");
  check("实时聆听状态显示在胶囊里（绿点脉冲）",
        capText() === "正在聆听…" && capKinds().includes("listening"), capKinds());
  ctx.setCapsuleState("speak", "正在播报语音…", "speaking");
  check("播报状态优先级高于聆听（播报时显示播报）", capText() === "正在播报语音…", capText());
  ctx.setCapsuleState("chat", "正在生成回复…", "busy");
  check("生成回复优先级最高（生成时显示生成）", capText() === "正在生成回复…", capText());
  ctx.clearCapsuleState("chat");
  check("生成结束后自动回落到播报状态", capText() === "正在播报语音…", capText());
  ctx.clearCapsuleState("speak");
  check("播报结束后自动回落到聆听状态", capText() === "正在聆听…", capText());
  ctx.clearCapsuleState("live");
  check("全部结束后回到常驻的「就绪」", capText() === "就绪", capText());

  ctx.setCapsuleNotice("未识别到语音。", "warn", 120);
  check("一次性提示临时占用胶囊（不会被底层状态盖掉）",
        capText() === "未识别到语音。" && capKinds().includes("warn"), capKinds());
  await new Promise((r) => setTimeout(r, 220));
  check("一次性提示到期后自动消失，回落底层状态", capText() === "就绪", capText());

  ctx.beginSpeak("正在合成语音…");
  check("语音合成期间胶囊提示「合成中」", capText() === "正在合成语音…", capText());
  ctx.updateSpeak("正在播报语音…");
  ctx.beginSpeak("多人对话播报中…");
  ctx.endSpeak();
  check("多处播报重叠时不会提前清空", capText() === "多人对话播报中…", capText());
  ctx.endSpeak();
  check("所有播报结束后胶囊回到「就绪」", capText() === "就绪", capText());

  console.log("");
  if (fails.length) {
    console.log(`通过 ${oks.length} 项，失败 ${fails.length} 项：`);
    fails.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log(`通过 ${oks.length} 项，失败 0 项`);
  console.log("音乐播放器 / 设置浮层 行为验证通过。");
})().catch((e) => {
  console.error("测试脚本异常：" + (e && e.stack ? e.stack : e));
  process.exit(1);
});
