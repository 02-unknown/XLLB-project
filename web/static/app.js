/* 小笼洛包 Web 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);

/* 客户端标识：危险接口（改设置 / 启停插件 / 清空对话等）要求 POST 带此头，
   用于防跨站请求（前端脚本无法让第三方页面带上自定义请求头）。同一标签页内保持一致。 */
const CLIENT_ID = (() => {
  try {
    let id = sessionStorage.getItem("xllb-client");
    if (!id) {
      id = "xllb-client-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      sessionStorage.setItem("xllb-client", id);
    }
    return id;
  } catch (e) {
    return "xllb-client-local";
  }
})();
const CLIENT_HEADER = { "X-XLLB-Client": CLIENT_ID };

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  opts.headers = { ...CLIENT_HEADER, ...(options.headers || {}) };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Blob)) {
    opts.headers = { "Content-Type": "application/json", ...opts.headers };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ==================== 统一内联 SVG 图标 ====================
   全站只用这一套图标（不引任何外链图标库 / 字体）：24×24 视图框、无填充描边、
   线宽 1.7、圆头圆角，实际渲染统一 18×18（见 style.css 的 .btn.icon svg / .ico）。
   按钮不写死图标字符，统一由 setIcon() 注入，保证线条风格与尺寸完全一致。 */
const ICONS = {
  // 麦克风：录音按钮
  mic: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>',
  // 喇叭静音：停止播放语音
  "speaker-off": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="m15.5 10 4.5 4.5"/><path d="m20 10-4.5 4.5"/></svg>',
  // 垃圾桶：清空对话与上下文
  trash: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/><path d="m6.5 7 .9 12.1A1.9 1.9 0 0 0 9.3 21h5.4a1.9 1.9 0 0 0 1.9-1.9L17.5 7"/><path d="M10.5 11.5v5.5"/><path d="M13.5 11.5v5.5"/></svg>',
  // 播放
  play: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 5.2 18.2 12 8 18.8z"/></svg>',
  // 暂停（播放中显示）
  pause: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 5.5v13"/><path d="M14.5 5.5v13"/></svg>',
  // 方块：停止
  stop: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5"/></svg>',
  // 喇叭：重播
  speaker: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="M15.3 9.4a3.8 3.8 0 0 1 0 5.2"/><path d="M18.1 6.9a7.4 7.4 0 0 1 0 10.2"/></svg>',
  // 音符：音乐条
  music: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V6.4l10-2v11.2"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="15.6" r="2.5"/></svg>',
  // 叉号：关闭浮层
  close: '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6.5 6.5 11 11"/><path d="m17.5 6.5-11 11"/></svg>',
};

/* 把指定图标注入到元素（按钮 / 图标位）：支持元素或 id 字符串 */
function setIcon(el, name) {
  const node = typeof el === "string" ? $(el) : el;
  if (node && ICONS[name]) node.innerHTML = ICONS[name];
}

/* ==================== 状态栏 ==================== */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    // 模型徽章：按实际后端显示（Ollama / 外部API），不再无条件显示 Ollama 状态
    const backendLabel = s.llm_backend === "ollama" ? "Ollama" : "外部API";
    setBadge("status-model", s.llm_ready, `模型 · ${backendLabel} · ${s.llm_model || "未配置"}`);
    setBadge("status-mode", true, s.app_mode === "lite" ? "模式 · Lite（仅外部API）" : "模式 · 标准");
    setBadge("status-tts", s.tts_api_ready, s.tts_api_ready ? "TTS · 就绪" : "TTS · 未就绪");
    setBadge("status-whisper", s.whisper_ready, s.whisper_ready ? "Whisper · 就绪" : "Whisper · 未加载");
    return s.settings;
  } catch (e) {
    console.error(e);
    return null;
  }
}

function setBadge(id, ok, text) {
  const el = $(id);
  el.textContent = text;
  el.className = "badge " + (ok ? "ok" : "bad");
}

/* ==================== 模式切换 ==================== */
let currentMode = "qa";
let conversationStarted = false;   // 是否已开始对话（用于限制模式切换）

function setMode(mode) {
  // 实时模式只能在开始对话之前切换；开始对话后需先“清空对话”
  if (conversationStarted && mode !== currentMode) {
    addMessage("system", "对话已开始，请先点「清空」再切换模式。");
    return;
  }
  currentMode = mode;
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  if (mode === "live") {
    $("input").placeholder = "实时模式：说“退出”结束，“暂停”暂停，“继续”恢复…";
  } else {
    $("input").placeholder = "输入消息，回车发送…";
    stopLive();
  }
}

/* ==================== 背景主题（由“背景设置”插件控制） ==================== */
async function refreshTheme() {
  try {
    const r = await api("/api/plugins");
    const bg = (r.plugins || []).find((p) => p.name === "背景设置");
    applyBackgroundTheme(bg);
  } catch (e) { /* 忽略 */ }
}

// 应用背景主题（由“背景设置”插件控制：浅色 / 深色 / 自定义图片）
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
      // 本地路径（浏览器无法直接加载）交给后端端点提供；http(s)/data:/以 / 开头的地址直接使用
      if (/^(https?:|data:|\/)/i.test(img)) {
        body.style.setProperty("--bg-image", `url("${img}")`);
      } else {
        body.style.setProperty("--bg-image", "url('/api/background/image')");
      }
    }
    body.style.setProperty("--bg-strength", String(s.strength != null ? s.strength : 0.8));
  }
  // dark 为默认主题，不加额外类
}

/* 插件设置 / 状态面板已统一迁移到设置页 settings.html（settings.js） */

/* ==================== 聊天渲染 ==================== */
function timeLabel() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function addMessage(role, text, extra) {
  const chat = $("chat");
  const div = document.createElement("div");
  div.className = "msg " + role;

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = timeLabel();
  div.appendChild(time);

  const body = document.createElement("span");
  body.className = "body";
  body.textContent = text || "";
  div.appendChild(body);

  if (extra) {
    const wrap = document.createElement("div");
    wrap.className = "actions";
    wrap.appendChild(extra);
    div.appendChild(wrap);
  }
  chat.appendChild(div);
  $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
  saveChatSession();
  return div;
}

/* ==================== 状态胶囊（全局常驻） ====================
   浮在对话记录上方、标题栏下方靠左，**一直显示当前持续性状态**（问答模式同样常驻）：
     · 持续性状态放这里：实时对话聆听 / 识别 / 生成回复 / 合成播报 / 多人对话生成 / 音乐准备与播放 / 暂停；
     · 一次性提示（清空上下文、跳过、出错…）仍然写进聊天记录，避免被忽略漏看。
   不同状态用不同的 key 占位，按优先级取最高的那个显示，互不抢占（例如正在播报时开始新的一轮生成，
   优先级更高的「生成回复」会顶上来，播报结束后自动回落到聆听 / 就绪）。 */
const CAPSULE_STATES = new Map();       // key -> {text, kind, priority}
const CAPSULE_PRIORITY = { notice: 60, multi: 45, chat: 40, speak: 30, music: 25, live: 20 };
const CAPSULE_IDLE = { text: "就绪", kind: "" };
let capsuleNoticeToken = 0;

function renderCapsule() {
  const box = $("status-capsule");
  const label = $("status-capsule-text");
  if (!box || !label) return;
  let best = null;
  CAPSULE_STATES.forEach((s) => { if (!best || s.priority > best.priority) best = s; });
  const cur = best || CAPSULE_IDLE;
  label.textContent = cur.text;
  box.className = "status-capsule" + (cur.kind ? " " + cur.kind : "");
}

function setCapsuleState(key, text, kind, priority) {
  if (!text) { clearCapsuleState(key); return; }
  CAPSULE_STATES.set(key, {
    text: text, kind: kind || "",
    priority: priority != null ? priority : (CAPSULE_PRIORITY[key] || 10),
  });
  renderCapsule();
}

function clearCapsuleState(key) {
  CAPSULE_STATES.delete(key);
  renderCapsule();
}

/* 一次性状态（未识别到语音 / 已停止…）：短暂提示后自动消失，回落到底层状态 */
function setCapsuleNotice(text, kind, ms) {
  const token = ++capsuleNoticeToken;
  setCapsuleState("notice", text, kind);
  setTimeout(() => { if (token === capsuleNoticeToken) clearCapsuleState("notice"); }, ms || 2500);
}

/* 语音合成 / 播报期间的持续状态（可能有队列重叠，用计数收口） */
let speakBusyCount = 0;
function beginSpeak(text) {
  speakBusyCount += 1;
  setCapsuleState("speak", text || "正在播报语音…", "speaking");
}
function updateSpeak(text) {
  if (speakBusyCount > 0) setCapsuleState("speak", text, "speaking");
}
function endSpeak() {
  speakBusyCount = Math.max(0, speakBusyCount - 1);
  if (speakBusyCount === 0) clearCapsuleState("speak");
}

/* ==================== 设置浮层（进入设置不再打断播放） ====================
   以前点「设置」是整页跳转到 settings.html：主页被卸载，正在播放的音乐 / 语音合成会立刻中断，
   而音量设置恰好就在设置页里（等于「一进去就什么都听不到了」，调不了音量）。
   现在改成在当前页面上盖一层浮层，里面用 iframe 装设置页：主页始终不被卸载，
   音乐 / 语音 / 实时对话继续跑，音量改动由设置页实时回传并立即生效。
   iframe 关闭时只隐藏不销毁：再进来是瞬时的，也不会产生「页面卸载」带来的副作用。 */
let settingsOpen = false;
const SETTINGS_URL = "/settings.html?embed=1";

function ensureSettingsOverlay() {
  let ov = $("settings-overlay");
  if (ov) return ov;
  ov = document.createElement("div");
  ov.className = "settings-overlay hidden";
  ov.id = "settings-overlay";
  const sheet = document.createElement("div");
  sheet.className = "settings-sheet";
  const frame = document.createElement("iframe");
  frame.id = "settings-frame";
  frame.title = "设置";
  frame.setAttribute("src", SETTINGS_URL);
  // 设置页里点「插件页面」这类链接是在窗口内跳转：提前告诉生命周期脚本「这是页面跳转，不是关窗口」，
  // 免得浮层里的跳转被当成关窗而触发完整退出（同源才能拿到 iframe 的 document）。
  frame.addEventListener("load", () => {
    try {
      const doc = frame.contentDocument;
      if (!doc) return;
      doc.addEventListener("click", (e) => {
        const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
        const w = frame.contentWindow;
        if (a && !a.target && w && w.XLLB_NAV) w.XLLB_NAV.allow();
      }, true);
    } catch (err) { /* 跨域 / 拿不到 document 时忽略（本地同源正常可用） */ }
  });
  sheet.appendChild(frame);
  // 设置页自己的顶栏在嵌入模式下是隐藏的（浮层只保留左侧分类 + 右侧内容），
  // 所以这里给浮层提供一个关闭按钮（另外 Esc / 点空白处也能关）
  const close = document.createElement("button");
  close.className = "settings-close";
  close.id = "settings-close";
  close.title = "关闭设置（Esc）";
  close.setAttribute("aria-label", "关闭设置");
  close.onclick = () => closeSettingsOverlay();
  setIcon(close, "close");
  sheet.appendChild(close);
  ov.appendChild(sheet);
  document.body.appendChild(ov);
  // 点浮层空白处关闭（点设置内容区域不关闭）
  ov.addEventListener("click", (e) => { if (e.target === ov) closeSettingsOverlay(); });
  return ov;
}

function prefersReducedMotion() {
  try { return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
  catch (e) { return false; }
}

function openSettingsOverlay() {
  const ov = ensureSettingsOverlay();
  ov.classList.remove("closing");
  ov.classList.remove("hidden");
  document.body.classList.add("settings-open");     // 主页内容向左滑出
  settingsOpen = true;
  document.addEventListener("keydown", settingsEsc);
}

function closeSettingsOverlay() {
  const ov = $("settings-overlay");
  if (!settingsOpen && (!ov || ov.classList.contains("hidden"))) return;   // 已经关了就别重复播动画
  settingsOpen = false;
  document.removeEventListener("keydown", settingsEsc);
  document.body.classList.remove("settings-open");   // 主页内容滑回来
  const finish = () => {
    if (ov) {
      ov.classList.add("hidden");
      ov.classList.remove("closing");
    }
  };
  if (ov && !prefersReducedMotion()) {
    ov.classList.add("closing");                     // 设置页向右滑出
    setTimeout(finish, 300);
  } else {
    finish();
  }
  refreshAfterSettings();      // 设置里可能改了音量 / 主题：回到主页立刻同步
}

function settingsEsc(e) {
  if (e.key === "Escape" && settingsOpen) closeSettingsOverlay();
}

/* 设置页保存后回传：音量立即生效（不用等回到主页重新加载），其它设置同步界面 */
window.addEventListener("message", (e) => {
  if (e.origin && e.origin !== location.origin) return;
  const d = e.data || {};
  if (d.type === "xllb:volumes") applyVolumes(d.tts_volume, d.music_volume);
  else if (d.type === "xllb:settings-saved") refreshAfterSettings();
  else if (d.type === "xllb:close-settings") closeSettingsOverlay();
});

/* 音量：对话语音（含正在播放的那一句）与音乐立即套用 */
function applyVolumes(tts, music) {
  if (typeof tts === "number") {
    ttsVolume = tts;
    if (currentAudio) { try { currentAudio.volume = ttsVolume; } catch (e) { /* 忽略 */ } }
  }
  if (typeof music === "number") {
    musicVolume = music;
    try { musicAudio.volume = musicVolume; } catch (e) { /* 忽略 */ }
  }
}

async function refreshAfterSettings() {
  await loadVolumes();
  try { await refreshTheme(); } catch (e) { /* 忽略 */ }
  try { await refreshStatus(); } catch (e) { /* 忽略 */ }
}

/* 点「设置」打开浮层（不再整页跳转）：href 保留，方便中键 / 右键新标签打开 */
const toSettings = $("btn-settings");
if (toSettings) {
  toSettings.addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;   // 让浏览器处理新标签打开
    e.preventDefault();
    openSettingsOverlay();
  });
}

/* ==================== 会话保持（进入/退出插件管理页后聊天与上下文不丢失） ==================== */
const CHAT_STORAGE_KEY = "xllb-chat-session";

function saveChatSession() {
  try {
    sessionStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify({
      html: $("chat").innerHTML,
      started: conversationStarted,
      mode: currentMode,
    }));
  } catch (e) { /* 忽略 */ }
}

function restoreChatSession() {
  try {
    const raw = sessionStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    if (!data || !data.html) return;
    $("chat").innerHTML = data.html;
    conversationStarted = !!data.started;
    if (data.mode === "qa" || data.mode === "live") currentMode = data.mode;
    // 清理遗留在页面里的“思考中…”占位（页面被中途切走时会残留）
    const stale = [...document.querySelectorAll("#chat .msg.system")].filter(
      (el) => (el.textContent || "").trim() === "思考中…" || (el.textContent || "").includes("思考中")
    );
    stale.forEach((el) => el.remove());
    saveChatSession();
  } catch (e) { /* 忽略 */ }
}

function actionWrap() {
  const wrap = document.createElement("div");
  wrap.className = "actions";
  return wrap;
}

function speakButton(getUrls) {
  const btn = document.createElement("button");
  btn.className = "speak-btn";
  btn.innerHTML = ICONS.speaker + "<span>重播</span>";
  btn.title = "重新播放语音";
  btn.onclick = () => { speechPromise = playQueue(getUrls()); };
  return btn;
}

/* 合成进度占位：合成期间用「细横线进度条」占据重播按钮的位置，合成完成后被按钮取代。
   进度取法（回答通常只有一句，按「句数」算不出进度）：
     · 只要有流式合成（stream_id）就先占位显示进度条；
     · 第一句音频到达前 —— 不确定动画（表示正在合成）；
     · 音频开始播放后 —— (已合成句数 - 1 + 当前句播放比例) / 总句数，单句时即播放比例 0→100%；
     · 拿不到总句数（例如旧版后台）或播放时长时保持不确定动画：不会卡住不动，也不会不显示。 */
function speakSlot(getUrls, total, hasStream) {
  const box = document.createElement("span");
  box.className = "speak-slot";
  const expect = Number(total) || 0;
  let bar = null, fill = null, btn = null;

  function showBar() {
    if (bar || btn) return;
    bar = document.createElement("span");
    bar.className = "speak-progress indeterminate";
    bar.title = "正在合成语音…";
    fill = document.createElement("i");
    bar.appendChild(fill);
    box.appendChild(bar);
  }

  function setProgress(evt) {
    if (!bar || !expect) return;
    const total_ = Number(evt && evt.total) || expect;
    const have = Math.max(0, (Number(evt && evt.have) || 0) - 1);
    const ratio = Math.min(1, Math.max(0, Number(evt && evt.ratio) || 0));
    const pct = Math.max(0, Math.min(100, (have + ratio) / total_ * 100));
    bar.classList.remove("indeterminate");
    fill.style.width = pct + "%";
  }

  function finish() {
    if (btn) return;
    if (bar) { bar.remove(); bar = null; fill = null; }
    btn = speakButton(getUrls);
    box.appendChild(btn);
  }

  // 有流式合成就先占位显示进度条（拿不到总句数也显示，只是保持不确定动画）
  if (hasStream || expect > 0) showBar();
  else finish();
  return { node: box, progress: setProgress, finish: finish };
}

function onlineBadge() {
  const b = document.createElement("span");
  b.className = "online-tag";
  b.textContent = "联网";
  return b;
}

/* ==================== 音频播放（流式 + 队列） ==================== */
let currentAudio = null;
let currentResolve = null;
let speechPromise = Promise.resolve();
let speechEpoch = 0;

/* 音量统一在「设置 · 通用设置」里调整：主页初始读一次，之后设置页保存时实时回传（立即生效） */
let ttsVolume = 1.0;
let musicVolume = 0.7;

async function loadVolumes() {
  try {
    const s = await api("/api/settings");
    const st = (s && s.settings) || {};
    if (typeof st.tts_volume === "number") ttsVolume = st.tts_volume;
    if (typeof st.music_volume === "number") musicVolume = st.music_volume;
    musicAudio.volume = musicVolume;
  } catch (e) { /* 忽略 */ }
}

// 播放单个音频；结束时（或被打断时）resolve。onRatio(0~1) 汇报当前音频的播放比例（进度条用）
function playOne(url, onRatio) {
  return new Promise((resolve) => {
    stopCurrentAudio();       // 打断上一段，并让其 resolve
    currentResolve = resolve;
    const audio = new Audio(url);
    currentAudio = audio;
    audio.volume = ttsVolume;
    const done = () => {
      if (onRatio) onRatio(1);
      if (currentAudio === audio) currentAudio = null;
      if (currentResolve === resolve) currentResolve = null;
      resolve();
    };
    if (onRatio) {
      audio.ontimeupdate = () => {
        const dur = Number(audio.duration);
        if (dur > 0) onRatio(Math.min(1, audio.currentTime / dur));
      };
    }
    audio.onended = done;
    audio.onerror = done;
    audio.play().catch(done);
  });
}

// 顺序播放固定列表（重播）；每次重播会打断正在进行的流式播放
async function playQueue(urls) {
  const epoch = ++speechEpoch;
  beginSpeak("正在播报语音…");
  try {
    for (const url of (urls || [])) {
      if (epoch !== speechEpoch) break;
      await playOne(url);
    }
  } finally {
    endSpeak();
  }
}

// 流式播放：长轮询服务端，逐句拿到即播放；边播放边预取下一句，消除衔接等待
// onProgress({have, total, ratio})：已合成句数 / 总句数 / 当前句播放比例（进度条用）
async function playStream(streamId, onUrl, onProgress) {
  if (!streamId) return;
  const epoch = ++speechEpoch;
  const fetchNext = () => api(`/api/tts/next?id=${encodeURIComponent(streamId)}`).catch(() => ({ done: true }));
  let pending = fetchNext();
  let have = 0;
  let total = 0;
  beginSpeak("正在合成语音…");        // 还没出声：先提示「合成中」，第一句开始播再改成「播报中」
  try {
    while (true) {
      const r = await pending;
      if (r.done) break;
      if (epoch !== speechEpoch) break;   // 已被新的播放 / 手动停止打断
      if (r.total) total = r.total;
      if (r.audio) {
        have += 1;
        updateSpeak("正在播报语音…");
        if (onUrl) onUrl(r.audio);
        if (onProgress) onProgress({ have, total, ratio: 0 });
        pending = fetchNext();            // 预取下一句
        const idx = have;
        await playOne(r.audio, (ratio) => { if (onProgress) onProgress({ have: idx, total, ratio }); });
      } else {
        if (onProgress && r.total) onProgress({ have: r.produced || have, total: r.total, ratio: 0 });
        pending = fetchNext();
      }
    }
  } finally {
    endSpeak();
  }
}

function stopCurrentAudio() {
  if (currentAudio) { try { currentAudio.pause(); } catch (e) {} currentAudio = null; }
  if (currentResolve) { const r = currentResolve; currentResolve = null; r(); }
}

// 停止当前语音并终止整个流式播放（含后续尚未播放的句子）
function stopSpeech() {
  stopCurrentAudio();
  speechEpoch++;
  speakBusyCount = 0;              // 手动停止：胶囊里的合成 / 播报状态立刻收掉
  clearCapsuleState("speak");
}

/* 多人对话·流式音频队列（边生成边播放，可被新播放打断） */
let multiQueue = [];
let multiPlaying = false;
const MULTI_SPEAK_GAP_MS = 500;   // 上一个角色说完后，等待 0.5s 再播放下一位角色的发言

function multiStop() { multiQueue = []; }

/* 等待多人对话语音队列播完（含角色间 0.5s 间隔）。
   生成期间队列会持续变长，所以按「队列空 + 不在播放」判定，并加上限保护。 */
async function multiDrain(maxMs = 180000) {
  const deadline = Date.now() + maxMs;
  while ((multiPlaying || multiQueue.length) && Date.now() < deadline) await sleep(150);
}

async function multiPump() {
  if (multiPlaying) return;
  multiPlaying = true;
  const epoch = speechEpoch;
  let prevSpeaker = null;
  let speaking = false;
  try {
    while (multiQueue.length) {
      if (epoch !== speechEpoch) { multiQueue = []; break; }
      const item = multiQueue.shift();
      const url = typeof item === "string" ? item : item.url;
      const speaker = typeof item === "string" ? null : item.speaker;
      const onRatio = typeof item === "string" ? null : item.onRatio;
      if (prevSpeaker && speaker && speaker !== prevSpeaker) await sleep(MULTI_SPEAK_GAP_MS);
      if (epoch !== speechEpoch) { multiQueue = []; break; }
      if (speaker) prevSpeaker = speaker;
      if (!speaking) { beginSpeak("多人对话播报中…"); speaking = true; }
      await playOne(url, onRatio);
    }
  } finally {
    if (speaking) endSpeak();
    multiPlaying = false;
  }
}

/* 推入一段待播语音；onRatio 用于把「这一句的播放进度」回报给对应气泡的进度条 */
function multiPush(url, speaker, onRatio) {
  multiQueue.push(speaker ? { url, speaker, onRatio } : url);
  multiPump();
}

/* ==================== 音乐播放 ==================== */
const musicAudio = $("music-audio");

function showMusicBar(title) {
  $("music-bar").classList.remove("hidden");
  $("music-title").textContent = title || "音乐";
  renderMusicProgress();
}

let suspendLiveForMusic = false;
let activePlaylistPlugin = null;   // 当前活动的播放列表插件名

/* ---------- 音乐进度条：显示播放位置，支持点击 / 拖动跳转（样式与语音进度条保持一致） ---------- */
let musicSeeking = false;      // 正在拖动：期间不让 timeupdate 覆盖进度显示
let musicDragRatio = 0;        // 拖动中的目标比例
let musicStopping = false;     // 正在停止 / 换曲：用于区分「用户主动暂停」与「程序暂停」
let musicLoading = false;      // 选好歌→下载 / 等元数据期间：进度条走「等待」动画

/* 进入 / 退出「等待中」：等待动画只在这段准备期出现，
   用户主动暂停或点停止会立刻退出（暂停时不该继续转圈）。 */
function setMusicLoading(on) {
  musicLoading = !!on;
  renderMusicProgress();
}

function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/* 当前播放比例；时长还没拿到（直播流 / 尚未加载完成）时返回 0 */
function musicDuration() {
  const dur = Number(musicAudio.duration);
  return (isFinite(dur) && dur > 0) ? dur : 0;
}

/* 进度条是否显示「等待」动画：
   · 正在下载 / 还没拿到时长 → 显示（和语音合成进度条同一套扫描动画）；
   · 暂停状态 → 一定不显示（暂停时进度条要停在原处，不能转圈）。 */
function musicWaiting() {
  if (musicLoading) return true;
  return !musicDuration() && !!musicAudio.src && !musicAudio.paused;
}

function renderMusicProgress() {
  const dur = musicDuration();
  const ratio = musicSeeking ? musicDragRatio : (dur ? Math.min(1, Math.max(0, musicAudio.currentTime / dur)) : 0);
  const fill = $("music-progress-fill");
  if (fill) fill.style.width = (ratio * 100).toFixed(2) + "%";
  const knob = $("music-progress-knob");
  if (knob) knob.style.left = (ratio * 100).toFixed(2) + "%";
  const time = $("music-time");
  if (time) {
    time.textContent = musicLoading
      ? "准备中…"
      : fmtClock(musicSeeking ? ratio * dur : musicAudio.currentTime) + " / " + fmtClock(dur);
  }
  const bar = $("music-progress");
  if (bar) {
    bar.classList.toggle("indeterminate", musicWaiting());
    bar.classList.toggle("dragging", musicSeeking);
  }
}

function resetMusicProgress() {
  musicSeeking = false;
  musicDragRatio = 0;
  const fill = $("music-progress-fill");
  if (fill) fill.style.width = "0%";
  const knob = $("music-progress-knob");
  if (knob) knob.style.left = "0%";
  const bar = $("music-progress");
  if (bar) bar.classList.remove("indeterminate", "dragging");
  const time = $("music-time");
  if (time) time.textContent = "0:00 / 0:00";
}

/* 拖动 / 点击跳转：按下即预览目标位置，松手才真正跳（拖动期间不打断播放） */
function bindMusicProgress() {
  const bar = $("music-progress");
  if (!bar) return;
  const ratioAt = (e) => {
    const rect = bar.getBoundingClientRect();
    if (!rect.width) return 0;
    const x = (e.clientX != null ? e.clientX : 0) - rect.left;
    return Math.min(1, Math.max(0, x / rect.width));
  };
  const canSeek = () => musicDuration() > 0 && !musicStopping;
  bar.addEventListener("pointerdown", (e) => {
    if (!canSeek()) return;
    musicSeeking = true;
    musicDragRatio = ratioAt(e);
    try { bar.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    // 兜底：万一指针捕获不可用（老浏览器 / 指针被系统抢走），在窗口上也收一次松手
    try { window.addEventListener("pointerup", commit, { once: true }); } catch (err) { /* 忽略 */ }
    renderMusicProgress();
    e.preventDefault();
  });
  bar.addEventListener("pointermove", (e) => {
    if (!musicSeeking) return;
    musicDragRatio = ratioAt(e);
    renderMusicProgress();
    e.preventDefault();
  });
  const commit = () => {
    if (!musicSeeking) return;
    musicSeeking = false;
    const dur = musicDuration();
    try { musicAudio.currentTime = musicDragRatio * dur; } catch (e) { /* 忽略 */ }
    renderMusicProgress();
  };
  bar.addEventListener("pointerup", commit);
  bar.addEventListener("pointercancel", commit);
  // 键盘（无障碍）：左右方向键前后跳 5 秒
  bar.addEventListener("keydown", (e) => {
    const dur = musicDuration();
    if (!dur) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const step = e.key === "ArrowRight" ? 5 : -5;
      try { musicAudio.currentTime = Math.min(dur, Math.max(0, musicAudio.currentTime + step)); } catch (err) { /* 忽略 */ }
      renderMusicProgress();
      e.preventDefault();
    }
  });
}

musicAudio.addEventListener("timeupdate", () => { if (!musicSeeking) renderMusicProgress(); });
musicAudio.addEventListener("loadedmetadata", () => { musicLoading = false; renderMusicProgress(); });
musicAudio.addEventListener("durationchange", renderMusicProgress);
musicAudio.addEventListener("seeked", renderMusicProgress);
musicAudio.addEventListener("waiting", renderMusicProgress);
musicAudio.addEventListener("canplay", renderMusicProgress);
musicAudio.addEventListener("play", renderMusicProgress);
musicAudio.addEventListener("error", () => { musicLoading = false; renderMusicProgress(); });
musicAudio.addEventListener("emptied", () => { musicLoading = false; resetMusicProgress(); });

// 直接播放一个已就绪的音乐 URL（供播放列表插件使用）：文件已下载好，只等元数据
function playMusicUrl(url, title) {
  showMusicBar(title || "音乐");
  setCapsuleState("music", "音乐准备中…", "busy");
  resetMusicProgress();
  setMusicLoading(true);         // 拿到时长 / 开始播放就自动退出等待动画
  musicStopping = false;
  musicAudio.src = url;
  musicAudio.volume = musicVolume;
  musicAudio.play().catch(() => {});
}

/* 选好歌 → 立刻显示播放条并进入「等待」状态：下载（可能几十秒）期间用户就能看到
   正在准备哪一首、并且能点停止；下载完成后再切成真实进度条。 */
async function playMusic(url, title, keyword) {
  const label = title || keyword || "音乐";
  showMusicBar(label);
  setCapsuleState("music", "音乐准备中…", "busy");
  resetMusicProgress();
  setMusicLoading(true);

  // 实时模式：整个音乐过程（下载 → 播报 → 播放 → 播报结束）期间禁止语音识别
  if (currentMode === "live") {
    suspendLiveForMusic = true;
    setLiveIndicator();
  }

  let resp;
  try {
    resp = await api("/api/music/play", { method: "POST", body: { url, title, keyword } });
  } catch (e) {
    resp = { ok: false, error: String(e) };
  }

  if (!resp.ok) {
    // 下载失败：收起播放条，回到「什么都没在放」的状态（失败提示仍走聊天记录，避免漏看）
    setMusicLoading(false);
    resetMusicProgress();
    clearCapsuleState("music");
    $("music-bar").classList.add("hidden");
    if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
    addMessage("system", "音乐下载失败：" + (resp.error || ""));
    return;
  }

  showMusicBar(title || keyword || "音乐");
  setMusicLoading(true);          // 仍在准备：等这首歌的元数据到位
  // 在 WebUI 中同时显示“即将播放”提示文本（语音仍会播放）
  if (resp.intro_text) addMessage("assistant", resp.intro_text);
  if (resp.intro_audio && resp.intro_audio.length) {
    await playQueue(resp.intro_audio);
  }
  resetMusicProgress();          // 新的一首：进度条从 0 开始
  musicStopping = false;
  musicAudio.src = resp.music_url;
  musicAudio.volume = musicVolume;
  musicAudio.play().catch(() => {});
}

// 音乐开始播放：保持禁止语音识别，并打断可能仍在进行的录音
musicAudio.addEventListener("playing", () => {
  stopActiveRecording();
  musicLoading = false;          // 已经出声：等待动画换成真实进度
  const name = $("music-title") ? $("music-title").textContent : "";
  setCapsuleState("music", name ? "正在播放：" + name : "正在播放音乐…", "music");
  if (currentMode === "live") { suspendLiveForMusic = true; setLiveIndicator(); }
  renderMusicProgress();
});
// 暂停（用户主动或程序暂停）：胶囊里改成「音乐已暂停」，不再显示"正在播放"
musicAudio.addEventListener("pause", () => {
  if (musicStopping || musicAudio.ended || !musicAudio.src) return;
  setCapsuleState("music", "音乐已暂停", "paused");
});

musicAudio.addEventListener("ended", async () => {
  // 播放列表：向插件请求下一首
  if (activePlaylistPlugin) {
    let r;
    try {
      r = await api("/api/plugins/action", { method: "POST", body: { name: activePlaylistPlugin, action: "next" } });
    } catch (e) { r = { ok: false }; }

    if (r && r.ok && r.music && r.music.url) {
      if (r.reply) addMessage("assistant", r.reply);
      if (r.speak && r.stream_id) await playStream(r.stream_id);
      playMusicUrl(r.music.url, r.music.title);
      return;
    }
    // 播放列表结束
    activePlaylistPlugin = null;
    musicStopping = true;
    musicAudio.src = "";
    resetMusicProgress();
    clearCapsuleState("music");
    $("music-bar").classList.add("hidden");
    if (r && r.reply) addMessage("assistant", r.reply);
    if (r && r.speak && r.stream_id) await playStream(r.stream_id);
    if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
    return;
  }

  // 单曲结束：**先立刻收起播放条**（不等结束语的生成 / 合成），结束语在后台播完。
  // 实时模式仍保持「暂停聆听」直到结束语播完，避免麦克风把结束语当成用户说话。
  const title = $("music-title").textContent;
  musicStopping = true;
  musicAudio.src = "";
  resetMusicProgress();
  clearCapsuleState("music");
  $("music-bar").classList.add("hidden");
  try {
    const resp = await api("/api/music/ended", { method: "POST", body: { title } });
    // 在 WebUI 中同时显示“播放完毕”提示文本（语音仍会播放）
    if (resp.ok && resp.text) addMessage("assistant", resp.text);
    if (resp.ok && resp.audio && resp.audio.length) {
      await playQueue(resp.audio);
    }
  } catch (e) { /* 忽略 */ }
  if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
});

/* ==================== 处理消息（核心） ==================== */
/* chatBusy：是否还有一轮对话没走完（生成 + 语音播报）。
   多人对话生成/播报期间不再接收语音输入，避免用户在生成过程中「插话」。
   用计数而不是布尔：极端情况下（例如生成期间手打文字再发一条）两轮会短暂重叠。 */
let chatBusyCount = 0;
function chatBusy() { return chatBusyCount > 0; }

async function handleMessage(text, mode, opts) {
  chatBusyCount += 1;
  try {
    return await runChatFlow(text, mode, opts);
  } finally {
    chatBusyCount -= 1;
    // 一轮结束后，如果还在实时对话，把胶囊恢复成「聆听中」
    if (chatBusyCount === 0 && typeof liveActive !== "undefined" && liveActive && !livePaused) {
      setCapsuleState("live", "正在聆听…（静音自动停止，或再次点击麦克风按钮）", "listening");
    }
  }
}

async function runChatFlow(text, mode, opts) {
  text = (text || "").trim();
  if (!text) return null;
  const voice = !!(opts && opts.voice);   // 是否来自语音识别（语音噪音静默丢弃，手打文字仍给提示）
  $("input").value = "";
  let userMsg = null;

  // 「先判断、再输出」：语音输入先让后端判断是不是真实对话，判定通过后才把这句话显示出来，
  // 避免噪声被显示后又被撤掉（一闪而过）。识别 / 生成这类持续性状态放常驻胶囊里，不占聊天记录。
  // （语音的转写在录音结束时就完成了，这里统一显示「正在生成回复…」）
  setCapsuleState("chat", "正在生成回复…", "busy");
  if (!voice) {
    conversationStarted = true;
    userMsg = addMessage("user", text);
  }

  let result;
  try {
    result = await api("/api/chat", { method: "POST", body: { message: text, mode: mode || "qa" } });
  } catch (e) {
    clearCapsuleState("chat");
    addMessage("system", "请求失败：" + e);
    return null;
  }
  clearCapsuleState("chat");
  if (voice) {
    const silentSkip = result && result.action === "skip" && result.skip_silent;
    if (silentSkip) return result;              // 非对话输入：完全不显示（不闪、不留痕）
    conversationStarted = true;
    userMsg = addMessage("user", text);         // 判定为真实对话：这时才显示
  }

  if (!result.ok) {
    addMessage("system", result.skip_reason || result.error || "处理失败");
    return result;
  }

  if (result.action === "skip") {
    // 语音输入在上面已经「先判断再输出」：静默跳过时什么都没显示，这里直接结束。
    // 手打文字是用户主动发出去的，仍然照常给「已跳过」提示（否则用户不知道为什么不回答）。
    if (result.skip_silent && voice) return result;
    addMessage("system", result.skip_reason ? "已跳过：" + result.skip_reason : "已跳过。");
    return result;
  }

  if (result.action === "music_control") {
    const collected = [];
    const wrap = actionWrap();
    const slot = speakSlot(() => collected, result.tts_total, !!result.stream_id);
    wrap.appendChild(slot.node);
    addMessage("assistant", result.reply, wrap);
    speechPromise = playStream(result.stream_id, (u) => collected.push(u),
                               (evt) => slot.progress(evt));
    speechPromise.then(() => slot.finish(), () => slot.finish());
    if (result.music_control === "pause") {
      musicAudio.pause();
      setMusicLoading(false);        // 暂停不是「等待」：进度条停在原处，不转圈
      // 用户主动暂停音乐：实时模式下恢复聆听（暂停期间不会再和音乐抢麦）
      if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
    }
    if (result.music_control === "resume") {
      musicStopping = false;
      musicLoading = false;
      musicAudio.play().catch(() => {});
      renderMusicProgress();
    }
    if (result.music_control === "stop") {
      musicStopping = true;
      musicAudio.pause(); musicAudio.src = ""; resetMusicProgress();
      clearCapsuleState("music");
      $("music-bar").classList.add("hidden");
      activePlaylistPlugin = null;
      suspendLiveForMusic = false; setLiveIndicator();
      await api("/api/music/stop", { method: "POST" });
    }
    return result;
  }

  if (result.action === "music_search") {
    addMessage("assistant", result.reply);
    renderVideoList(result.music_videos, result.music_keyword);
    return result;
  }

  // 插件播放列表：播报 + 播放音乐
  if (result.music && result.music.url) {
    activePlaylistPlugin = result.music_plugin || null;
    const collected = [];
    const wrap = actionWrap();
    const slot = speakSlot(() => collected, result.tts_total, !!result.stream_id);
    wrap.appendChild(slot.node);
    addMessage("assistant", result.reply, wrap);
    if (result.speak && result.stream_id) {
      await playStream(result.stream_id, (u) => collected.push(u), (evt) => slot.progress(evt));
    }
    slot.finish();
    playMusicUrl(result.music.url, result.music.title);
    return result;
  }

  // 多人对话·剧本模式：流式追加 + 边生成边播放。
  // 必须等「生成 + 全部语音播完」再结束本轮，否则实时模式会在生成过程中又开始聆听（用户插话）。
  if (result.multi_stream_id) {
    speechPromise = (async () => {
      await streamMultiDialogue(result.multi_stream_id, result.reply);
      await multiDrain();
    })();
    await speechPromise;
    return result;
  }

  // 多人对话：每个角色的回复单独一个聊天框显示，各自声线逐条播放（角色间留 0.5s 间隔）。
  // 每个气泡也带「细横线进度条」：音频到达前显示不确定动画，播放时按该句播放比例推进，
  // 播完自动换成「重播」按钮（与单人对话一致）。
  if (result.multi_audio && result.multi_audio.length) {
    for (const a of result.multi_audio) {
      const urls = (Array.isArray(a.audio) ? a.audio : [a.audio]).filter(Boolean);
      const slot = speakSlot(() => urls, urls.length, urls.length > 0);
      const wrap = actionWrap();
      wrap.appendChild(slot.node);
      addMessage("assistant", `${a.speaker}：${a.text}`, wrap);
      if (!urls.length) { slot.finish(); continue; }
      urls.forEach((u, idx) => {
        multiPush(u, a.speaker, (ratio) => {
          slot.progress({ have: idx + 1, total: urls.length, ratio });
          if (idx === urls.length - 1 && ratio >= 1) slot.finish();
        });
      });
    }
    speechPromise = multiDrain();   // 等所有角色的语音播完，本轮才算结束
    await speechPromise;
    return result;
  }

  // 正常对话（流式播报）：合成期间用细横线进度条占据「重播」按钮的位置，
  // 合成完成后进度条被按钮取代（拿不到总句数时直接显示按钮）
  const collected = [];
  const wrap = actionWrap();
  if (result.need_online) wrap.appendChild(onlineBadge());
  const slot = speakSlot(() => collected, result.tts_total, !!result.stream_id);
  wrap.appendChild(slot.node);
  addMessage("assistant", result.reply, wrap);
  if (result.stream_id) {
    speechPromise = playStream(result.stream_id, (u) => collected.push(u),
                               (evt) => slot.progress(evt));
    speechPromise.then(() => slot.finish(), () => slot.finish());
  } else {
    slot.finish();
  }
  return result;
}

async function sendMessage(text, opts) {
  await handleMessage(text, currentMode === "live" ? "live" : "qa", opts);
}

/* 多人对话·流式（剧本/自然对话）：每个角色的发言单独一个聊天框，各自带进度条 / 重播按钮 */
async function streamMultiDialogue(sid, initialText) {
  multiStop();
  const bubbles = {};          // seq -> 该句的气泡元素
  const segUrls = {};          // seq -> 该句累计音频 URL
  const segSlots = {};         // seq -> 该气泡的进度条占位（生成中显示不确定动画）
  const playedAudio = new Set();
  let placeholder = addMessage("assistant", initialText || "多人对话生成中…");
  const stopWait = () => {     // 结束「生成中」进度条（任何结束路径都调用，避免一直显示等待）
    try { if (waitWrap && waitWrap.parentNode) waitWrap.remove(); } catch (e) { /* 忽略 */ }
    try { if (waitSlot) waitSlot.finish(); } catch (e) { /* 忽略 */ }
  };
  const waitWrap = actionWrap();
  const waitSlot = speakSlot(() => [], 0, true);
  waitWrap.appendChild(waitSlot.node);
  if (placeholder) placeholder.appendChild(waitWrap);
  setCapsuleState("multi", "多人对话生成中…", "busy");
  // 上限保护：生成阶段最长等 5 分钟，避免轮询异常时进度条 / 胶囊一直停在「生成中」
  const deadline = Date.now() + 5 * 60 * 1000;
  try {
    while (true) {
      if (Date.now() > deadline) {
        if (placeholder) {
          const bodyEl = placeholder.querySelector(".body");
          if (bodyEl) bodyEl.textContent = "多人对话生成超时（已停止等待，可稍后重试）";
        }
        break;
      }
      const r = await api(`/api/multi_chat/poll?id=${encodeURIComponent(sid)}`).catch(() => ({ done: true }));
      for (const seg of (r.segments || [])) {
        const seq = seg.seq;
        // 文本：每个角色单独一个聊天框（第一个发言替换占位气泡，后续各开新气泡）
        if (!bubbles[seq]) {
          const line = `${seg.speaker}：${seg.text}`;
          if (placeholder) {
            const bodyEl = placeholder.querySelector(".body");
            if (bodyEl) bodyEl.textContent = line;
            stopWait();                    // 已有内容：移除「生成中」进度条
            bubbles[seq] = placeholder;
            placeholder = null;
          } else {
            bubbles[seq] = addMessage("assistant", line);
          }
        }
        // 音频：到达即播放（不同角色之间自动留 0.5s 间隔），气泡上带进度条 → 播完换成重播按钮
        const urls = (seg.audio || []).filter((u) => !playedAudio.has(u));
        if (urls.length) {
          urls.forEach((u) => playedAudio.add(u));
          segUrls[seq] = (segUrls[seq] || []).concat(urls);
          const bubble = bubbles[seq];
          if (bubble && !segSlots[seq]) {
            const slot = speakSlot(() => segUrls[seq] || [], segUrls[seq].length, true);
            const wrap = actionWrap();
            wrap.appendChild(slot.node);
            bubble.appendChild(wrap);
            segSlots[seq] = slot;
          }
          const slot = segSlots[seq];
          const total = segUrls[seq].length;
          urls.forEach((u, i) => {
            multiPush(u, seg.speaker, (ratio) => {
              if (!slot) return;
              slot.progress({ have: total - urls.length + i + 1, total, ratio });
              if (i === urls.length - 1 && ratio >= 1) slot.finish();
            });
          });
        }
      }
      if (r.done) {
        stopWait();                        // 生成结束：确保移除「生成中」进度条
        // 一句都没生成出来（如生成失败）时，把错误显示在占位气泡里
        if (placeholder && r.error) {
          const bodyEl = placeholder.querySelector(".body");
          if (bodyEl) bodyEl.textContent = r.error || "生成失败";
        }
        if (r.error === "多人对话已停止。") multiStop();   // 插件被停用：停止已入队的语音
        break;
      }
      await sleep(350);
    }
  } finally {
    stopWait();
    Object.keys(segSlots).forEach((k) => { try { segSlots[k].finish(); } catch (e) { /* 忽略 */ } });
    clearCapsuleState("multi");     // 生成结束：胶囊回落到播报 / 聆听 / 就绪
    saveChatSession();
  }
}

function renderVideoList(videos, keyword) {
  const chat = $("chat");
  const div = document.createElement("div");
  div.className = "msg assistant";
  const list = document.createElement("div");
  list.className = "video-list";
  if (!videos || !videos.length) {
    list.innerHTML = "<div>没有找到相关歌曲，换个关键词试试。</div>";
  } else {
    videos.forEach((v) => {
      const item = document.createElement("div");
      item.className = "video-item";
      const t = document.createElement("span");
      t.className = "v-title";
      t.textContent = v.title;
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = "播放";
      b.onclick = () => {
        // 点过之后立刻给反馈：按钮变「下载中…」，播放条同时出现并走等待动画
        if (b.disabled) return;
        b.disabled = true;
        b.classList.add("loading");
        b.textContent = "下载中…";
        Promise.resolve(playMusic(v.url, v.title, keyword)).finally(() => {
          b.disabled = false;
          b.classList.remove("loading");
          b.textContent = "播放";
        });
      };
      item.appendChild(t); item.appendChild(b);
      list.appendChild(item);
    });
  }
  div.appendChild(list);
  chat.appendChild(div);
  $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
}

/* ==================== 录音 ==================== */
let activeRecorder = null;
let sharedCtx = null;

function getSharedCtx() {
  if (!sharedCtx) sharedCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (sharedCtx.state === "suspended") sharedCtx.resume().catch(() => {});
  return sharedCtx;
}

// 强制停止正在进行的录音（例如音乐开始播放时）
function stopActiveRecording() {
  if (activeRecorder && activeRecorder.state === "recording") {
    try { activeRecorder.stop(); } catch (e) {}
  }
}

async function recordOnce() {
  // 录音前先停掉正在播放的语音，避免与麦克风抢占音频设备造成卡顿
  stopCurrentAudio();
  stopActiveRecording();

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    addMessage("system", "无法访问麦克风：" + e);
    return "";
  }

  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
  const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  activeRecorder = mr;
  const chunks = [];
  let ctx = null;
  let source = null;
  let analyser = null;
  let timer = null;

  const cleanup = () => {
    if (activeRecorder === mr) activeRecorder = null;
    $("btn-mic").classList.remove("recording");
    if (timer) clearInterval(timer);
    if (source) { try { source.disconnect(); } catch (e) {} }
    stream.getTracks().forEach((t) => t.stop());
  };

  return new Promise((resolve) => {
    mr.ondataavailable = (e) => chunks.push(e.data);
    mr.onstop = async () => {
      cleanup();
      // 录音结束：不再显示「聆听」（问答模式只录一次，之前这里会把状态一直挂在胶囊上）
      clearCapsuleState("live");
      setCapsuleState("chat", "识别中…", "busy");      // 正在转写
      const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
      let text = "";
      try {
        const wav = await encodeWav(blob);
        text = await transcribe(wav);
      } catch (e) {
        text = "";
      }
      clearCapsuleState("chat");                       // 转写阶段结束：交给调用方（生成回复 / 未识别提示）
      resolve(text);
    };

    // 静音检测：静音 1.2s 或最长 10s 自动停止（复用 AudioContext，避免反复开关导致卡顿）
    ctx = getSharedCtx();
    source = ctx.createMediaStreamSource(stream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    let silent = 0;
    const start = Date.now();
    timer = setInterval(() => {
      if (mr.state !== "recording") return;
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / data.length);
      if (rms < 0.01) silent++; else silent = 0;
      if (silent > 12 || Date.now() - start > 10000) {
        clearInterval(timer); timer = null;
        mr.stop();
      }
    }, 100);

    mr.start();
    $("btn-mic").classList.add("recording");
    setCapsuleState("live", "正在聆听…（静音自动停止，或再次点击麦克风按钮）", "listening");
  });
}

async function onMicClick() {
  // 上一轮还没结束（正在生成 / 仍在播报，多人对话尤其明显）时不接受新的语音输入
  if (chatBusy()) { setCapsuleNotice("正在生成回复，请稍候…", "busy", 1800); return; }
  if (currentMode === "live") {
    if (liveActive) { stopLive(); }
    else { startLive(); }
    return;
  }
  // 问答模式：单次录音（再次点击则停止）
  if (activeRecorder && activeRecorder.state === "recording") { activeRecorder.stop(); return; }
  const text = await recordOnce();
  // voice: true —— 语音识别结果，若被判为「疑似非对话输入」则静默丢弃（不显示识别内容与跳过提示）
  if (text) sendMessage(text, { voice: true });
  else setCapsuleNotice("未识别到语音。", "warn");
}

/* ==================== 实时模式（连续聆听） ==================== */
let liveActive = false;
let livePaused = false;

function setLiveIndicator() {
  const mic = $("btn-mic");
  if (liveActive && !livePaused && !suspendLiveForMusic) mic.classList.add("recording");
  else mic.classList.remove("recording");
}

async function startLive() {
  liveActive = true;
  livePaused = false;
  setLiveIndicator();
  setCapsuleState("live", "实时对话开启：连续聆听中（说“退出”结束，“暂停”暂停）", "listening");
  while (liveActive) {
    if (livePaused || suspendLiveForMusic) { await sleep(300); continue; }
    // 上一轮（生成 / 多人对话播报）还没结束：不开始新的录音，避免用户在生成过程中插话
    if (chatBusy()) { await sleep(150); continue; }

    const text = await recordOnce();
    if (!liveActive) break;          // 录音期间被用户停止
    if (!text) { if (liveActive) setCapsuleNotice("未识别到语音。", "warn"); continue; }

    const result = await handleMessage(text, "live", { voice: true });
    if (!result) continue;

    if (result.action === "exit") {
      setCapsuleState("live", result.reply || "已退出实时对话。", "paused");
      liveActive = false;
      break;
    }
    if (result.action === "live_pause") {
      livePaused = true;
      setLiveIndicator();
      setCapsuleState("live", "对话已暂停（点麦克风按钮继续，或说“继续”）", "paused");
      continue;
    }
    if (result.action === "live_resume") {
      livePaused = false;
      setLiveIndicator();
      setCapsuleState("live", "已继续：正在聆听…", "listening");
      continue;
    }
    // 等待语音播报完成后，留一小段缓冲让音频设备彻底释放，再聆听下一句
    await speechPromise;
    if (liveActive && !livePaused) setCapsuleState("live", "正在聆听…（静音自动停止，或再次点击麦克风按钮）", "listening");
    await sleep(300);
  }
  liveActive = false;
  livePaused = false;
  setLiveIndicator();
  clearCapsuleState("live");
  setCapsuleNotice("实时对话已停止", "paused");
}

function stopLive() {
  liveActive = false;
  setLiveIndicator();
}

/* ==================== WAV 编码 / 转写 ==================== */
async function encodeWav(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  // 复用共享 AudioContext，避免每次录音都新建/销毁导致音频设备抖动
  const ctx = getSharedCtx();
  const buffer = await ctx.decodeAudioData(arrayBuffer);
  const channels = buffer.numberOfChannels;
  const length = buffer.length;
  const merged = new Float32Array(length);
  for (let c = 0; c < channels; c++) {
    const ch = buffer.getChannelData(c);
    for (let i = 0; i < length; i++) merged[i] += ch[i] / channels;
  }
  const sampleRate = buffer.sampleRate;
  const pcm = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    let s = Math.max(-1, Math.min(1, merged[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return buildWav(pcm, sampleRate);
}

function buildWav(pcm, sampleRate) {
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
  return new Blob([buffer], { type: "audio/wav" });
}

async function transcribe(wavBlob) {
  const resp = await fetch("/api/transcribe", {
    method: "POST",
    headers: { "Content-Type": "audio/wav", ...CLIENT_HEADER },
    body: wavBlob,
  });
  const data = await resp.json();
  return data.text || "";
}

/* ==================== 事件绑定 ==================== */
$("btn-send").onclick = () => sendMessage($("input").value);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage($("input").value); }
});
$("btn-mic").onclick = onMicClick;

$("mode-qa").onclick = () => setMode("qa");
$("mode-live").onclick = () => setMode("live");

$("btn-stop-speech").onclick = () => { stopSpeech(); addMessage("system", "已停止语音播放。"); };
$("btn-clear").onclick = async () => {
  stopLive();
  await api("/api/history/clear", { method: "POST" });
  conversationStarted = false;
  $("chat").innerHTML = '<div class="welcome">已清空对话与上下文。</div>';
  saveChatSession();
};

/* 设置相关交互（角色 / 语音 / 音量 / 插件 / 对话记录）已统一迁移到设置页 settings.html，
   但设置页是盖在主页上的浮层（见文件前面的「设置浮层」），所以播放不会被打断 */
$("btn-music-toggle").onclick = () => {
  if (musicAudio.paused) {
    musicStopping = false;
    musicAudio.play().catch(() => {});
  } else {
    musicAudio.pause();
    setMusicLoading(false);      // 主动暂停：立刻退出「等待」动画
    // 主动暂停：实时模式下把麦克风还回来（音乐不会再出声，可以正常对话）
    if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
  }
  renderMusicProgress();
};
$("btn-music-stop").onclick = async () => {
  musicStopping = true;
  setMusicLoading(false);
  musicAudio.pause();
  musicAudio.src = "";
  resetMusicProgress();
  clearCapsuleState("music");
  $("music-bar").classList.add("hidden");
  activePlaylistPlugin = null;
  suspendLiveForMusic = false; setLiveIndicator();
  await api("/api/music/stop", { method: "POST" });
};

/* ==================== 图标注入 ==================== */
// 音乐条按钮随播放状态切换「播放 / 暂停」图标（播放中显示暂停，暂停或结束时显示播放）
function syncMusicToggleIcon() {
  setIcon("btn-music-toggle", musicAudio && musicAudio.paused === false ? "pause" : "play");
}
musicAudio.addEventListener("playing", syncMusicToggleIcon);
musicAudio.addEventListener("pause", syncMusicToggleIcon);
musicAudio.addEventListener("ended", syncMusicToggleIcon);

/* 脚本加载即同步注入图标，避免按钮出现空白（不动任何 id / class / 事件绑定） */
(function initIcons() {
  setIcon("btn-mic", "mic");
  setIcon("btn-stop-speech", "speaker-off");
  setIcon("btn-clear", "trash");
  setIcon("btn-music-stop", "stop");
  setIcon(document.querySelector(".music-icon"), "music");
  syncMusicToggleIcon();
  bindMusicProgress();          // 音乐进度条：拖动 / 点击跳转
})();

/* ==================== 初始化 ==================== */
(async function init() {
  restoreChatSession();
  renderCapsule();               // 状态胶囊常驻：先把「就绪」显示出来
  await loadVolumes();
  await refreshStatus();
  setInterval(refreshStatus, 5000);
  refreshTheme();
})();
