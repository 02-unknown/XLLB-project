/* 记忆管理浮层窗口（由「上下文记忆库」插件的「查看 / 管理记忆」动作打开）
 *
 * 不再单独打开页面：本文件注册到 window.XLLB_MODALS.memory_manager，
 * 由设置页在收到插件动作的 {"modal": "memory_manager"} 时弹出大号浮层。
 * 记忆能力由服务端门禁（memory_engine.service）决定，这里只如实展示：
 *   · 插件未启用 / 只读 / 完整权限 三种状态分别显示；
 *   · 只读或插件停用时禁用「保存 / 编辑 / 删除」，并说明原因；
 *   · 服务端 409（门禁拒绝、令牌失效）把 error 显示在错误区，不静默忽略。
 *
 * 整个文件包在 IIFE 中，避免与 settings.js 的同名顶层声明冲突（两者同页加载）。
 */
(function () {
  "use strict";

const $ = (id) => document.getElementById(id);

/* 客户端标识：页面加载时生成一次并缓存到 sessionStorage（服务端做防跨站校验 + 绑定确认令牌） */
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

/* 当前记忆能力（来自 GET /api/memory/status）：can_manage 为假时禁用增删改 */
let gate = { plugin_enabled: false, mode: "readonly", level: "只读（仅本次会话）", can_manage: false, reason: "记忆状态未知" };

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  opts.headers = { "X-XLLB-Client": CLIENT_ID, ...(opts.headers || {}) };
  if (opts.body && typeof opts.body === "object") {
    opts.headers = { "Content-Type": "application/json", ...opts.headers };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
}

/* 危险操作（删除记忆）：先领一次性确认令牌，再带令牌发真正的请求 */
async function apiWithConfirm(path, body, op, target) {
  const prep = await api("/api/confirm/prepare", { method: "POST", body: { op, target: target || "" } });
  if (!prep || !prep.ok || !prep.token) {
    return prep || { ok: false, error: "无法获取确认令牌（服务未就绪）" };
  }
  return api(path, { method: "POST", body: { ...(body || {}), token: prep.token } });
}

/* ==================== 记忆能力状态条 ==================== */
function ensureGateBox() {
  let box = $("mv-gate");
  if (!box) {
    box = document.createElement("div");
    box.id = "mv-gate";
    box.className = "mv-gate";
    // 浮层窗口里挂在窗口内容顶部；极少数情况下（未打开浮层）退回到 body
    const host = $("memory-modal-body") || document.body;
    host.insertBefore(box, host.firstChild);
  }
  return box;
}

function renderGate() {
  const box = ensureGateBox();
  const enabled = !!gate.plugin_enabled;
  const mode = gate.mode === "readwrite" ? "readwrite" : "readonly";
  let text, cls;
  if (!enabled) {
    cls = "mv-gate-off";
    text = `记忆插件未启用：${gate.reason || "请在「设置 → 插件管理」中启用「上下文记忆库」"}`
      + "（当前不能查看或修改长期记忆）";
  } else if (mode === "readonly") {
    cls = "mv-gate-ro";
    text = `当前模式：${gate.level || "只读（仅本次会话）"} · 可以查看，但不能新增 / 修改 / 删除长期记忆`
      + `（原因：${gate.reason || "当前为只读模式"}）`;
  } else {
    cls = "mv-gate-rw";
    text = `当前模式：${gate.level || "完整权限"} · 可查看、可新增 / 修改 / 删除长期记忆`;
  }
  box.className = "mv-gate " + cls;
  box.textContent = text;
  const manage = !!gate.can_manage;
  // 编辑 / 删除 / 保存：能力不足时禁用，避免用户以为设置已生效
  document.querySelectorAll('[data-op="edit"],[data-op="del"]').forEach((b) => {
    b.disabled = !manage;
    b.title = manage ? "" : (gate.reason || "当前模式不允许修改长期记忆");
  });
  const submit = $("f-submit");
  if (submit) {
    submit.disabled = !manage;
    submit.title = manage ? "" : (gate.reason || "当前模式不允许修改长期记忆");
  }
  return manage;
}

async function loadGate() {
  try {
    const r = await api("/api/memory/status");
    if (r && r.ok && r.gate) gate = r.gate;
  } catch (e) {
    gate = { plugin_enabled: false, mode: "readonly", level: "只读（仅本次会话）", can_manage: false, reason: "无法读取记忆状态（服务未就绪）" };
  }
  renderGate();
}

/* 服务端门禁拒绝（409）：用返回的当前模式刷新能力状态，并把原因显示在页面错误区 */
function applyGateFromError(r) {
  if (!r || r.plugin_enabled === undefined) return;
  gate = {
    plugin_enabled: !!r.plugin_enabled,
    mode: r.mode || "readonly",
    level: r.level || "只读（仅本次会话）",
    can_manage: !!r.can_manage,
    can_read: !!r.can_read,
    can_write: !!r.can_write,
    reason: r.reason || r.error || "",
  };
  renderGate();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 确认弹窗（是 / 否） */
function confirmYesNo(text, onYes) {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">请确认</div>
      <div class="modal-text"></div>
      <div class="modal-actions">
        <button class="btn" data-act="no">否，取消</button>
        <button class="btn danger" data-act="yes">是，继续</button>
      </div>
    </div>`;
  ov.querySelector(".modal-text").textContent = text;
  document.body.appendChild(ov);
  ov.querySelector('[data-act="no"]').onclick = () => ov.remove();
  ov.querySelector('[data-act="yes"]').onclick = () => { ov.remove(); onYes && onYes(); };
}

let _editId = null;
/* 分页状态：管理页一次只渲染一页（默认 200 条），避免记录多时整页渲染导致滚动卡顿。
   服务端按 ts DESC 返回，配合每行「显示更多」按页累加。 */
const PAGE_SIZE = 200;
let _limit = PAGE_SIZE;

function render(append = false) {
  const q = $("mv-q").value.trim();
  const date = $("mv-date").value.trim();
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (date) params.set("date", date);
  params.set("limit", String(_limit));
  api(`/api/memory/view?${params.toString()}`).then((r) => {
    if (!r.ok) {
      // 门禁拒绝（409）也显示在错误区，并刷新能力状态
      applyGateFromError(r);
      showErr(r.error || "加载失败");
      $("box-active").textContent = r.error || "加载失败";
      return;
    }
    showErr("");
    const data = r;
    if (data.topics && data.topics.length) $("topic-hint").textContent = data.topics.join("/");
    renderL0(data.l0 || []);
    renderL1(data.l1 || {});
    renderFrags($("box-active"), data.active || [], "活跃区", data.totals, data.has_more, append);
    renderFrags($("box-archive"), data.archive || [], "归档区", data.totals, data.has_more, append);
    renderCold(data.cold || []);
  }).catch(() => { $("box-active").textContent = "加载失败（服务未就绪？）"; });
}

/* 每页列表底部的统计行：已显示 X / 共 M 条 + 「显示更多」 */
function fragFooter(label, shown, total, hasMore, box, boxId) {
  const bar = document.createElement("div");
  bar.className = "mv-more";
  const info = document.createElement("span");
  info.className = "mv-more-info";
  info.textContent = total > shown
    ? `${label}：已显示 ${shown} / 共 ${total} 条（按时间倒序，越靠后越早；也可用关键词 / 日期精确查找）`
    : `${label}：共 ${shown} 条`;
  bar.appendChild(info);
  if (hasMore) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = "显示更多";
    btn.onclick = () => {
      _limit = Math.min(_limit + PAGE_SIZE, 5000);
      render(true);
    };
    bar.appendChild(btn);
  }
  box.appendChild(bar);
}

function renderL0(entries) {
  const box = $("box-l0");
  if (!entries.length) { box.innerHTML = '<div class="mv-empty">（空）当前没有临时会话上下文。</div>'; return; }
  box.innerHTML = entries.slice(0, 10).map((e) => {
    const who = e.role === "user" ? "用户" : "助手";
    return `<div class="mv-item"><div class="meta">${fmtTs(e.ts)} · ${who}</div>${esc(e.content)}</div>`;
  }).join("");
  if (entries.length > 10) box.innerHTML += `<div class="mv-empty">… 共 ${entries.length} 条</div>`;
}

function renderL1(l1) {
  const s = l1.stats || {};
  $("box-l1").innerHTML = `<div class="mv-item"><div class="meta">缓存项 ${l1.size} 项</div>`
    + `命中 ${s.hit || 0} 次 / 未中 ${s.miss || 0} 次 / 淘汰 ${s.evict || 0} 次（30 分钟后自动失效）</div>`;
}

/* 记忆条目卡片（统一风格）：meta 行 + 摘要 + 可点开的详情 */
function fragCard(f) {
  const card = document.createElement("div");
  card.className = "mv-item";
  card.innerHTML = `
    <div class="meta">
      <span>${esc(f.id)}</span><span>${fmtTs(f.ts)}</span>
      <span>${f.year}/${f.quarter}</span><span>主题：${esc(f.topic)}</span>
      <span>参与者：${esc((f.participants || []).join("、"))}</span>
    </div>
    <div class="body">${esc(f.summary)}</div>
    <div class="detail" hidden>
      <div class="d-title">检索词（searchable_text）</div>
      <div class="d-text">${esc(f.searchable || "")}</div>
    </div>
    <div class="ops">
      <button class="btn" data-op="expand">展开详情</button>
      <button class="btn" data-op="edit">编辑</button>
      <button class="btn danger" data-op="del">删除</button>
    </div>`;
  card.querySelector('[data-op="expand"]').onclick = () => {
    const d = card.querySelector(".detail");
    const btn = card.querySelector('[data-op="expand"]');
    d.hidden = !d.hidden;
    btn.textContent = d.hidden ? "展开详情" : "收起详情";
  };
  return card;
}

function renderFrags(box, frags, label = "", totals = null, hasMore = null, append = false) {
  const boxId = box.id;
  if (!append) box.innerHTML = "";
  if (!frags.length && !append) {
    box.innerHTML = '<div class="mv-empty">（空）没有符合条件的记忆。</div>';
    return;
  }
  // 追加模式：先移除上一页的统计行，渲染完再补新的
  const oldFooter = box.querySelector(".mv-more");
  if (oldFooter) oldFooter.remove();
  frags.forEach((f) => {
    const card = fragCard(f);
    card.querySelector('[data-op="edit"]').onclick = () => {
      if (!gate.can_manage) { showErr(gate.reason || "当前模式不允许修改长期记忆"); return; }
      startEdit(f);
    };
    card.querySelector('[data-op="del"]').onclick = () => {
      if (!gate.can_manage) { showErr(gate.reason || "当前模式不允许删除长期记忆"); return; }
      confirmYesNo(`确定删除这条记忆吗？\n（${f.id}）${f.summary}`, async () => {
        // 删除是危险操作：先领一次性令牌（op=memory.delete，target=记忆 id）再执行
        const r = await apiWithConfirm("/api/memory/view/delete", { id: f.id }, "memory.delete", f.id);
        if (!r || !r.ok) {
          applyGateFromError(r);
          showErr((r && r.error) || "删除失败");
          return;
        }
        showErr("");
        resetForm();
        render();
      });
    };
    box.appendChild(card);
  });
  const total = (totals && totals[boxId === "box-active" ? "active" : "archive"]) || frags.length;
  const more = !!(hasMore && hasMore[boxId === "box-active" ? "active" : "archive"]);
  fragFooter(label, frags.length, total, more, box, boxId);
  renderGate();
}

/* L3 冷存储分区：点开展示该分区内的原文条目 */
function renderCold(cold) {
  const box = $("box-cold");
  if (!cold.length) { box.innerHTML = '<div class="mv-empty">（空）没有原文冷存储分区。</div>'; return; }
  box.innerHTML = "";
  cold.forEach((c) => {
    const card = document.createElement("div");
    card.className = "mv-item";
    card.innerHTML = `
      <div class="meta"><span>分区</span><span>${esc(c.partition)}</span></div>
      <div class="detail" hidden><div class="d-text">加载中…</div></div>
      <div class="ops"><button class="btn" data-op="expand">展开分区内容</button></div>`;
    const d = card.querySelector(".detail");
    const btn = card.querySelector('[data-op="expand"]');
    let loaded = false;
    btn.onclick = () => {
      if (d.hidden) {
        d.hidden = false;
        btn.textContent = "收起分区内容";
        if (!loaded) {
          api(`/api/memory/view/cold?partition=${encodeURIComponent(c.partition)}`).then((r) => {
            loaded = true;
            if (!r.ok || !r.entries) { d.innerHTML = '<div class="d-text">' + esc((r && r.error) || "读取失败") + "</div>"; return; }
            d.innerHTML = r.entries.length
              ? r.entries.map((e) => `<div class="d-item"><b>${esc(e.id)}</b>：${esc(e.full_text)}</div>`).join("")
              : '<div class="d-text">（空）</div>';
          });
        }
      } else {
        d.hidden = true;
        btn.textContent = "展开分区内容";
      }
    };
    box.appendChild(card);
  });
}

/* 编辑 / 新增 */
function startEdit(f) {
  _editId = f.id;
  $("form-title").textContent = `编辑记忆（${f.id}）`;
  $("f-date").value = String(f.ts ? new Date(f.ts * 1000).toISOString().slice(0, 10) : "");
  $("f-participants").value = (f.participants || []).join("、");
  $("f-topic").value = f.topic && f.topic !== "综合" ? f.topic : "";
  $("f-content").value = f.summary || "";
  $("f-cancel").style.display = "";
  $("f-submit").textContent = "保存修改";
  document.getElementById("sec-form").scrollIntoView({ behavior: "smooth" });
}

function resetForm() {
  _editId = null;
  $("form-title").textContent = "新增记忆（标准格式）";
  $("f-date").value = ""; $("f-participants").value = ""; $("f-topic").value = ""; $("f-content").value = "";
  $("f-cancel").style.display = "none";
  $("f-submit").textContent = "保存";
  showErr("");
}

function showErr(msg) { $("f-err").textContent = msg || ""; }

/* 保存（新增 / 编辑）：元素在浮层打开后才存在，因此由 openMemoryManager 绑定 */
async function submitForm() {
  const body = {
    date: $("f-date").value.trim(),
    participants: $("f-participants").value.trim(),
    topic: $("f-topic").value.trim(),
    content: $("f-content").value.trim(),
  };
  // 客户端基础校验（服务端还会严格校验一遍）
  if (!/^\d{4}(-\d{1,2}(-\d{1,2})?)?$/.test(body.date)) { showErr("时间格式不正确：请使用 2026 或 2026-08 或 2026-08-27。"); return; }
  if (!body.participants) { showErr("参与者不能为空（中文顿号分隔）。"); return; }
  if (body.content.length < 2 || body.content.length > 1000) { showErr("内容长度需在 2~1000 字之间。"); return; }
  if (!gate.can_manage) { showErr(gate.reason || "当前模式不允许新增 / 修改长期记忆"); return; }
  const url = _editId ? "/api/memory/view/update" : "/api/memory/view/add";
  if (_editId) body.id = _editId;
  const r = await api(url, { method: "POST", body });
  if (!r.ok) { applyGateFromError(r); showErr(r.error || "保存失败"); return; }
  resetForm();
  render();
}

/* 新查询：分页从头开始（避免沿用上一次「显示更多」的条数） */
function search() {
  _limit = PAGE_SIZE;
  render();
}

/* ==================== 浮层窗口：记忆管理 ==================== */
const MODAL_HTML = `
  <div class="modal-box memory-modal">
    <div class="modal-head">
      <div>
        <div class="modal-title">记忆管理（上下文记忆库）</div>
        <div class="modal-sub">按层级查看 / 关键词与日期查询 / 手动新增、编辑、删除；数据只降级不删除</div>
      </div>
      <button class="btn" data-act="close">关闭</button>
    </div>
    <div class="memory-body" id="memory-modal-body">
      <div class="mv-search">
        <input class="input" id="mv-q" placeholder="关键词（如：奶茶 / 爬山）">
        <input class="input" id="mv-date" placeholder="日期（2026 / 2026-08 / 2026-08-27）">
        <button class="btn" id="mv-search-btn">查询</button>
        <button class="btn" id="mv-reset-btn">显示全部</button>
      </div>

      <div class="mv-nav">
        <a href="#sec-form">新增记忆</a>
        <a href="#sec-l0">L0 会话缓存</a>
        <a href="#sec-l1">L1 检索缓存</a>
        <a href="#sec-active">L2 活跃区</a>
        <a href="#sec-archive">L2 归档区</a>
        <a href="#sec-cold">L3 冷存储</a>
      </div>

      <section class="mv-sec" id="sec-form">
        <h2 id="form-title">新增记忆（标准格式）</h2>
        <div class="mv-spec">
          标准格式（不接受其它格式，保存时会校验）：<br>
          · 时间：<b>2026</b> 或 <b>2026-08</b> 或 <b>2026-08-27</b><br>
          · 参与者：用中文顿号分隔，如 <b>洛天依、洛天依（朋友）</b><br>
          · 主题：可选，从 <b id="topic-hint">美食/旅行/娱乐/爱好/学习/社交/回忆/健康/日程/日常/综合</b> 中选择一个；留空自动归类<br>
          · 内容：一句话即可，2~1000 字，如「今天下午三点约好一起去公园散步」
        </div>
        <div class="mv-form">
          <div class="row"><label>时间 *</label><input class="input" id="f-date" placeholder="如 2026-08-27"></div>
          <div class="row"><label>参与者 *</label><input class="input" id="f-participants" placeholder="如 洛天依、洛天依（朋友）"></div>
          <div class="row"><label>主题</label><input class="input" id="f-topic" placeholder="留空自动归类（美食/旅行/娱乐/爱好/学习/社交/回忆/健康/日程/日常）"></div>
          <div class="row"><label>内容 *</label><textarea class="textarea" id="f-content" placeholder="一句话描述这次记忆…"></textarea></div>
          <div class="row"><label></label>
            <button class="btn primary" id="f-submit">保存</button>
            <button class="btn" id="f-cancel" style="display:none">取消编辑</button>
            <span class="mv-err" id="f-err"></span>
          </div>
        </div>
      </section>

      <section class="mv-sec" id="sec-l0"><h2>L0 会话缓存 <span class="cnt">（最近对话，进程内临时）</span></h2><div id="box-l0"></div></section>
      <section class="mv-sec" id="sec-l1"><h2>L1 检索热缓存 <span class="cnt">（最近查询结果，30 分钟）</span></h2><div id="box-l1"></div></section>
      <section class="mv-sec" id="sec-active"><h2>L2 活跃区 <span class="cnt">（近期记忆，可编辑 / 删除）</span></h2><div id="box-active"></div></section>
      <section class="mv-sec" id="sec-archive"><h2>L2 归档区 <span class="cnt">（长期记忆，可编辑 / 删除）</span></h2><div id="box-archive"></div></section>
      <section class="mv-sec" id="sec-cold"><h2>L3 冷存储 <span class="cnt">（原始全文分区）</span></h2><div id="box-cold"></div></section>
    </div>
    <div class="modal-actions"><button class="btn" data-act="close">关闭</button></div>
  </div>`;

function openMemoryManager() {
  if ($("memory-modal-overlay")) return;          // 已打开
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "memory-modal-overlay";
  overlay.innerHTML = MODAL_HTML;
  document.body.appendChild(overlay);
  overlay.querySelectorAll('[data-act="close"]').forEach((b) => {
    b.onclick = () => overlay.remove();
  });
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  // 事件绑定（元素此时才存在）
  $("mv-search-btn").onclick = search;
  $("mv-reset-btn").onclick = () => { $("mv-q").value = ""; $("mv-date").value = ""; search(); };
  $("mv-q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("mv-date").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("f-cancel").onclick = resetForm;
  $("f-submit").onclick = submitForm;
  _limit = PAGE_SIZE;
  // 先取记忆能力状态（插件未启用时也要能显示原因），再加载记忆内容
  loadGate().then(search);
}

window.XLLB_MODALS = window.XLLB_MODALS || {};
window.XLLB_MODALS.memory_manager = openMemoryManager;

})();
