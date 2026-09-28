/* 小笼洛包 图形启动器前端：
   1) 读取模式清单（Lite / 标准）并渲染成两张卡片；
   2) 点击卡片 → POST /api/launch/start 按该模式拉起服务；
   3) 轮询 /api/launch/state 显示每个步骤的状态，服务拉起后进入正式界面。
   这一步不涉及危险操作，因此不需要确认令牌，只需要客户端标识（服务端统一要求）。 */
"use strict";

const $ = (id) => document.getElementById(id);
const CLIENT_ID = "xllb-launcher";
const CLIENT_HEADER = { "X-XLLB-Client": CLIENT_ID };
const POLL_MS = 700;

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  opts.headers = { ...CLIENT_HEADER, ...(options.headers || {}) };
  if (opts.body && typeof opts.body === "object") {
    opts.headers = { "Content-Type": "application/json", ...opts.headers };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
}

function el(tag, cls, text) {  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

let MODES = [];
let polling = null;
let entered = false;

/* ==================== 模式卡片 ==================== */
function renderModes(modes) {
  MODES = modes || [];
  const box = $("launch-modes");
  box.innerHTML = "";
  if (!MODES.length) {
    box.appendChild(el("div", "launch-loading", "没有可用的启动模式。"));
    return;
  }
  MODES.forEach((m) => {
    const card = el("button", "launch-card");
    card.type = "button";
    card.dataset.mode = m.id;

    const head = el("div", "launch-card-head");
    head.appendChild(el("span", "launch-card-label", m.label || m.id));
    if (m.tag) head.appendChild(el("span", "launch-tag", m.tag));
    card.appendChild(head);

    card.appendChild(el("p", "launch-card-desc", m.desc || ""));
    if (m.need) card.appendChild(el("p", "launch-card-need", m.need));

    const foot = el("div", "launch-card-foot");
    foot.appendChild(el("span", "launch-card-go", "点此启动 →"));
    card.appendChild(foot);

    card.onclick = () => choose(m.id);
    box.appendChild(card);
  });
}

/* ==================== 选择模式 → 拉起服务 ==================== */
async function choose(mode) {
  if (polling) {                               // 启动中：不重复提交，给出提示
    const note = $("launch-note");
    note.className = "launch-note";
    note.textContent = "正在启动中，请稍候…";
    return;
  }
  const sub = $("launch-sub");
  sub.textContent = "正在启动服务…";
  document.querySelectorAll(".launch-card").forEach((c) => c.classList.add("disabled"));
  let r = null;
  try {
    r = await api("/api/launch/start", { method: "POST", body: { mode } });
  } catch (e) {
    r = null;
  }
  if (!r || !r.ok) {
    document.querySelectorAll(".launch-card").forEach((c) => c.classList.remove("disabled"));
    sub.textContent = "请选择启动模式";
    const note = $("launch-note");
    $("launch-progress").classList.remove("hidden");
    note.className = "launch-note bad";
    note.textContent = "启动失败：" + ((r && r.error) || "无法连接本地服务，请关闭本窗口后重新启动程序");
    return;
  }
  $("launch-progress").classList.remove("hidden");
  paint(r.state);
  startPolling();
}

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    let r = null;
    try {
      r = await api("/api/launch/state");
    } catch (e) {
      return;
    }
    if (r && r.state) paint(r.state);
  }, POLL_MS);
}

function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}

/* ==================== 进度渲染 ==================== */
const STATUS_MARK = { pending: "○", running: "◐", done: "✓", failed: "✕" };

function paint(state) {
  state = state || {};
  const stepsBox = $("launch-steps");
  stepsBox.innerHTML = "";
  (state.steps || []).forEach((s) => {
    const row = el("div", "launch-step " + (s.status || "pending"));
    row.appendChild(el("span", "launch-step-mark", STATUS_MARK[s.status] || "○"));
    const body = el("div", "launch-step-body");
    body.appendChild(el("div", "launch-step-label", s.label || s.id));
    const detail = s.detail || s.desc || "";
    if (detail) body.appendChild(el("div", "launch-step-desc", detail));
    row.appendChild(body);
    stepsBox.appendChild(row);
  });

  const note = $("launch-note");
  const actions = $("launch-actions");
  actions.innerHTML = "";
  const tts = state.tts || {};

  if (state.error) {
    note.className = "launch-note bad";
    note.textContent = state.error + "（可先进入界面，在「设置」里重试或查看日志）";
  } else if (state.done) {
    if (tts.ready) {
      note.className = "launch-note ok";
      note.textContent = `服务已就绪（语音合成端口 ${tts.port || "—"}）`;
    } else {
      note.className = "launch-note";
      note.textContent = "服务已拉起，语音合成正在后台加载模型（可先进入界面，稍后会自动可用）";
    }
  } else {
    note.className = "launch-note";
    note.textContent = "正在启动，请稍候…";
  }

  if (state.done && !entered) {
    entered = true;
    stopPolling();
    $("launch-sub").textContent = "启动完成";
    const go = el("button", "btn primary launch-go", "进入对话界面");
    go.type = "button";
    go.onclick = enterApp;
    actions.appendChild(go);
    // 稍等片刻自动进入（页面就在同一个窗口里，直接切换到正式界面）
    setTimeout(enterApp, state.error ? 2500 : 900);
  }
  if (!state.done) {
    $("launch-sub").textContent = "正在启动服务…";
  }
}

function enterApp() {
  if (window.__entered) return;
  window.__entered = true;
  // 启动页 → 正式界面属于页面内跳转：告诉服务端不要当成「关闭程序」
  if (window.XLLB_NAV) window.XLLB_NAV.allow();
  window.location.href = "/";
}

/* ==================== 启动 ==================== */
async function init() {
  let r = null;
  try {
    r = await api("/api/launch/state");
  } catch (e) {
    $("launch-modes").innerHTML = "";
    $("launch-modes").appendChild(el("div", "launch-loading",
      "无法连接本地服务，请关闭本窗口后重新启动程序（或运行「诊断.bat」检查环境）。"));
    return;
  }
  renderModes(r.modes || []);
  const st = r.state || {};
  // 每次打开启动页都要能重新选模式（不沿用上一次的选择）：
  // 只有「正在启动中」才直接展示进度，已经启动过也照常显示两个选项。
  if (st.starting) {
    $("launch-progress").classList.remove("hidden");
    document.querySelectorAll(".launch-card").forEach((c) => c.classList.add("disabled"));
    paint(st);
    startPolling();
  } else if (st.done) {
    $("launch-sub").textContent = "已有服务在运行，可重新选择启动模式";
  }
}

init();
