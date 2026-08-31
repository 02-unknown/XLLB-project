/* 小笼洛包 · 插件管理页前端逻辑（独立页面，与主页面 app.js 分离） */
"use strict";

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", ...options };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Blob)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  return resp.json();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 轻量提示（复用 .msg.system 气泡样式） */
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

/* ==================== 确认弹窗（二级确认：风险警告 + 二次确认） ==================== */
function showConfirmModal(title, text, onConfirm) {
  let overlay = $("confirm-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "confirm-overlay";
    overlay.className = "modal-overlay hidden";
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

/* 动作长输出：渲染到插件下方的输出区（而不是塞进小提示气泡） */
function renderActionOutput(pluginName, text) {
  const host = document.getElementById(`plugin-state-${pluginName}`) || document.querySelector(`#plugin-list .plugin-item`);
  let box = document.getElementById(`action-output-${pluginName}`);
  if (!box) {
    box = document.createElement("div");
    box.id = `action-output-${pluginName}`;
    box.className = "action-output";
    if (host && host.parentElement) host.parentElement.appendChild(box);
    else document.getElementById("plugin-list").appendChild(box);
  }
  box.textContent = text || "";
}

/* ==================== Ollama 模型缓存 ==================== */
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

/* ==================== 背景主题 ==================== */
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

/* ==================== 插件列表 ==================== */
async function loadPlugins() {
  const r = await api("/api/plugins");
  // 先用当前缓存的模型列表渲染，模型列表异步拉取，避免阻塞页面（Ollama 忙时卡顿）
  renderPlugins(r.plugins || [], ollamaModelsCache || []);
  const bg = (r.plugins || []).find((p) => p.name === "背景设置");
  applyBackgroundTheme(bg);
  fetchOllamaModels().then((models) => {
    if (models && models.length) updateModelDatalists(models);
  });
}

// 模型列表到达后就地补充下拉选项，不重建整个表单（避免丢失用户正在编辑的内容）
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

async function refreshPluginState(name) {
  if (!name) return;
  const el = document.getElementById(`plugin-state-${name}`);
  if (!el) return;
  try {
    const r = await api(`/api/plugins/state?name=${encodeURIComponent(name)}`);
    renderPluginState(el, r.state || {});
  } catch (e) { /* 忽略 */ }
}

function renderPluginState(el, state) {
  const queue = state && state.queue;
  if (!queue || !queue.length) {
    el.innerHTML = '<div class="p-desc">播放列表为空</div>';
    return;
  }
  const items = queue.map((q) => {
    const mark = q.status === "playing" ? "▶" : `${q.index}.`;
    return `<div class="q-item${q.status === "playing" ? " playing" : ""}">${mark} ${escapeHtml(q.title)}</div>`;
  }).join("");
  el.innerHTML = `<div class="q-list">${items}</div>`;
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
    fill();   // 渲染时也补一次（如旧配置只填了一边）
  });
}

function renderPlugins(plugins, ollamaModels) {
  const list = $("plugin-list");
  list.innerHTML = "";
  if (!plugins.length) {
    list.innerHTML = '<div class="p-desc">暂无插件（把 .py 文件放入 plugins/ 目录后点“重新加载插件”）。</div>';
    return;
  }
  plugins.forEach((p) => {
    const div = document.createElement("div");
    div.className = "plugin-item" + (p.enabled ? "" : " disabled");
    const head = document.createElement("div");
    head.className = "p-head";
    const nameEl = document.createElement("span");
    nameEl.className = "p-name";
    nameEl.textContent = `${p.name} `;
    const ver = document.createElement("span");
    ver.className = "p-version";
    ver.textContent = `v${p.version}`;
    nameEl.appendChild(ver);

    const badge = document.createElement("span");
    badge.className = "p-badge " + (p.official ? "official" : "third");
    badge.textContent = p.official ? "官方" : "第三方";
    nameEl.appendChild(badge);

    if (p.hot_swap) {
      const hs = document.createElement("span");
      hs.className = "p-badge hot-swap";
      hs.textContent = "热切换";
      hs.title = "可在对话进行中随时启用 / 停用";
      nameEl.appendChild(hs);
    }

    const toggle = document.createElement("button");
    toggle.className = "btn p-toggle";
    toggle.textContent = p.enabled ? "停用" : "启用";
    toggle.onclick = async () => {
      const r = await api(`/api/plugins/${p.enabled ? "disable" : "enable"}`, { method: "POST", body: { name: p.name } });
      renderPlugins(r.plugins || [], ollamaModelsCache || []);
      const bg = (r.plugins || []).find((x) => x.name === "背景设置");
      applyBackgroundTheme(bg);
    };
    head.appendChild(nameEl); head.appendChild(toggle);
    div.appendChild(head);

    const desc = document.createElement("div");
    desc.className = "p-desc";
    desc.textContent = p.description || "";
    div.appendChild(desc);

    if (p.commands && p.commands.length) {
      const cmds = document.createElement("div");
      cmds.className = "p-cmds";
      cmds.textContent = "命令：" + p.commands.map((c) => c.name || c).join("  ");
      div.appendChild(cmds);
    }

    // 插件动作按钮（一键触发；音乐/语音播放请在主页执行）
    if (p.actions && p.actions.length) {
      const actRow = document.createElement("div");
      actRow.className = "btn-row";
      p.actions.forEach((a) => {
        const btn = document.createElement("button");
        btn.className = "btn";
        btn.textContent = a.label || a.name;
        btn.title = a.desc || "";
        btn.onclick = async () => {
          const r = await api("/api/plugins/action", { method: "POST", body: { name: p.name, action: a.name } });
          if (r && r.page) { location.href = r.page; return; }   // 同标签页跳转（与插件管理页一致）
          if (r && r.confirm) {
            // 二级确认：先弹风险警告，选择「是」后再调用 confirm 指定的执行动作
            showConfirmModal(a.label || "请确认", r.reply || "确定继续吗？", async () => {
              const r2 = await api("/api/plugins/action", { method: "POST", body: { name: p.name, action: r.confirm } });
              if (r2 && r2.reply) renderActionOutput(p.name, r2.reply);
              else toast((r2 && r2.error) || "已执行。");
            });
            return;
          }
          if (r && r.reply) {
            if (r.reply.length > 100 || r.reply.includes("\n")) renderActionOutput(p.name, r.reply);
            else toast(r.reply);
          }
          else if (r && r.music) toast(`已在主页触发播放：「${r.music.title || ""}」`);
          else if (r && r.music_control === "stop") toast("已触发停止播放");
          else if (r && r.stream_id) toast("已触发语音播放");
        };
        actRow.appendChild(btn);
      });
      div.appendChild(actRow);
    }

    if (p.has_state) {
      const stateBox = document.createElement("div");
      stateBox.className = "plugin-state";
      stateBox.id = `plugin-state-${p.name}`;
      div.appendChild(stateBox);
      refreshPluginState(p.name);
    }

    // 插件设置表单
    if (p.settings_schema && p.settings_schema.length) {
      const form = document.createElement("div");
      form.className = "plugin-settings";
      p.settings_schema.forEach((field) => appendSchemaField(form, field, p, ollamaModels));

      const saveBtn = document.createElement("button");
      saveBtn.className = "btn";
      saveBtn.textContent = "保存设置";
      saveBtn.onclick = async () => {
        const patch = {};
        form.querySelectorAll("input,select").forEach((el) => {
          let v;
          if (el.type === "checkbox") v = el.checked;
          else { v = el.value; if (el.type === "number") v = Number(v); }
          patch[el.dataset.key] = v;
        });
        const r = await api("/api/plugins/settings", { method: "POST", body: { name: p.name, settings: patch } });
        if (!r.ok) {
          toast(`保存失败：${r.error || "未知错误"}`);
          return;
        }
        toast(`已保存「${p.name}」设置。`);
        loadPlugins();
      };
      form.appendChild(saveBtn);
      div.appendChild(form);
      wireApiAutofill(form);
    }
    list.appendChild(div);
  });
}

/* 渲染一个设置字段（支持「高级选项」分组 / 分组内动作按钮 / 字段说明） */
function appendSchemaField(container, field, p, ollamaModels) {
  if (field.type === "section") {
    // 折叠分组：默认收起，普通用户不被打扰
    const details = document.createElement("details");
    details.className = "p-section";
    const summary = document.createElement("summary");
    summary.textContent = field.label || "高级选项";
    details.appendChild(summary);
    if (field.desc) {
      const sdesc = document.createElement("div");
      sdesc.className = "p-hint";
      sdesc.textContent = field.desc;
      details.appendChild(sdesc);
    }
    const body = document.createElement("div");
    body.className = "p-sec-body";
    (field.fields || []).forEach((sub) => appendSchemaField(body, sub, p, ollamaModels));
    // 分组内动作按钮（如 查看记忆 / 记忆初始化，收进高级选项避免误触）
    if (field.actions && field.actions.length) {
      const actRow = document.createElement("div");
      actRow.className = "btn-row";
      field.actions.forEach((a) => {
        const btn = document.createElement("button");
        btn.className = "btn";
        btn.textContent = a.label || a.name;
        btn.title = a.desc || "";
        btn.onclick = async () => {
          const r = await api("/api/plugins/action", { method: "POST", body: { name: p.name, action: a.name } });
          if (r && r.page) { location.href = r.page; return; }
          if (r && r.confirm) {
            showConfirmModal(a.label || "请确认", r.reply || "确定继续吗？", async () => {
              const r2 = await api("/api/plugins/action", { method: "POST", body: { name: p.name, action: r.confirm } });
              if (r2 && r2.reply) renderActionOutput(p.name, r2.reply);
              else toast((r2 && r2.error) || "已执行。");
            });
            return;
          }
          if (r && r.reply) {
            if (r.reply.length > 100 || r.reply.includes("\n")) renderActionOutput(p.name, r.reply);
            else toast(r.reply);
          } else toast((r && r.error) || "已执行。");
        };
        actRow.appendChild(btn);
      });
      body.appendChild(actRow);
    }
    details.appendChild(body);
    container.appendChild(details);
    return;
  }

  const row = document.createElement("div");
  row.className = "row";
  const label = document.createElement("label");
  label.textContent = field.label || field.key;
  label.title = field.desc || "";
  row.appendChild(label);

  let input;
  const val = (p.settings && p.settings[field.key]) !== undefined ? p.settings[field.key] : "";
  if (field.type === "select") {
    input = document.createElement("select");
    (field.options || []).forEach((opt) => {
      const o = document.createElement("option");
      const v = (typeof opt === "object" && opt !== null) ? opt.value : opt;
      const t = (typeof opt === "object" && opt !== null) ? (opt.label || opt.value) : opt;
      o.value = v; o.textContent = t; o.selected = (v === val);
      input.appendChild(o);
    });
    if (field.key.endsWith("_backend")) {
      input.onchange = () => {
        const modelKey = field.key === "chat_backend" ? "chat_model" : "judge_model";
        const modelInput = container.querySelector(`input[data-key="${modelKey}"]`);
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
            container.appendChild(dl);
          }
          dl.innerHTML = "";
          const cur = modelInput.value;
          [...new Set([cur, ...(ollamaModels || [])])].filter(Boolean).forEach((m) => {
            const o = document.createElement("option");
            o.value = m;
            dl.appendChild(o);
          });
        }
      };
    }
  } else if (field.type === "checkbox") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!val;
  } else if (field.type === "datalist") {
    input = document.createElement("input");
    input.type = "text";
    input.value = val;
    input.setAttribute("list", `dl-${p.name}-${field.key}`);
    const dl = document.createElement("datalist");
    dl.id = `dl-${p.name}-${field.key}`;
    let opts = field.options || [];
    if (field.options_source === "ollama_models") {
      const extra = (ollamaModels || []).filter((m) => !opts.includes(m));
      opts = opts.concat(extra);
    }
    opts.forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt;
      dl.appendChild(o);
    });
    row.appendChild(dl);
    if (field.placeholder) input.placeholder = field.placeholder;
  } else {
    input = document.createElement("input");
    input.type = field.type === "password" ? "password" : (field.type === "number" ? "number" : "text");
    input.value = val;
    if (field.placeholder) input.placeholder = field.placeholder;
  }
  input.dataset.key = field.key;
  row.appendChild(input);
  container.appendChild(row);
  if (field.desc) {
    const hint = document.createElement("div");
    hint.className = "p-hint";
    hint.textContent = field.desc;
    container.appendChild(hint);
  }
}

/* ==================== 初始化 ==================== */
$("btn-plugins-reload").onclick = async () => {
  const r = await api("/api/plugins/reload", { method: "POST" });
  toast(r.ok ? `已重新加载插件（${r.plugins.length} 个）` : "插件加载失败");
  loadPlugins();
};

loadPlugins();
