/* 记忆管理页面：按层级查看 / 关键词+日期查询 / 手动编辑、增删（标准格式校验） */
"use strict";

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  if (opts.body && typeof opts.body === "object") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
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

function render() {
  const q = $("mv-q").value.trim();
  const date = $("mv-date").value.trim();
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (date) params.set("date", date);
  api(`/api/memory/view?${params.toString()}`).then((r) => {
    if (!r.ok) { $("box-active").textContent = r.error || "加载失败"; return; }
    const data = r;
    if (data.topics && data.topics.length) $("topic-hint").textContent = data.topics.join("/");
    renderL0(data.l0 || []);
    renderL1(data.l1 || {});
    renderFrags($("box-active"), data.active || []);
    renderFrags($("box-archive"), data.archive || []);
    renderCold(data.cold || []);
  }).catch(() => { $("box-active").textContent = "加载失败（服务未就绪？）"; });
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

function renderFrags(box, frags) {
  if (!frags.length) { box.innerHTML = '<div class="mv-empty">（空）没有符合条件的记忆。</div>'; return; }
  box.innerHTML = "";
  frags.forEach((f) => {
    const card = fragCard(f);
    card.querySelector('[data-op="edit"]').onclick = () => startEdit(f);
    card.querySelector('[data-op="del"]').onclick = () => {
      confirmYesNo(`确定删除这条记忆吗？\n（${f.id}）${f.summary}`, async () => {
        const r = await api("/api/memory/view/delete", { method: "POST", body: { id: f.id } });
        showErr(r.ok ? "" : (r.error || "删除失败"));
        if (r.ok) { resetForm(); render(); }
      });
    };
    box.appendChild(card);
  });
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
  $("form-title").textContent = `✎ 编辑记忆（${f.id}）`;
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
  $("form-title").textContent = "＋ 新增记忆（标准格式）";
  $("f-date").value = ""; $("f-participants").value = ""; $("f-topic").value = ""; $("f-content").value = "";
  $("f-cancel").style.display = "none";
  $("f-submit").textContent = "保存";
  showErr("");
}

function showErr(msg) { $("f-err").textContent = msg || ""; }

$("f-cancel").onclick = resetForm;

$("f-submit").onclick = async () => {
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
  const url = _editId ? "/api/memory/view/update" : "/api/memory/view/add";
  if (_editId) body.id = _editId;
  const r = await api(url, { method: "POST", body });
  if (!r.ok) { showErr(r.error || "保存失败"); return; }
  resetForm();
  render();
};

$("mv-search-btn").onclick = render;
$("mv-reset-btn").onclick = () => { $("mv-q").value = ""; $("mv-date").value = ""; render(); };
$("mv-q").addEventListener("keydown", (e) => { if (e.key === "Enter") render(); });
$("mv-date").addEventListener("keydown", (e) => { if (e.key === "Enter") render(); });

render();
