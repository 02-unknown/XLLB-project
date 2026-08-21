/* 小笼洛包 Web 前端逻辑 */
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ==================== 状态栏 ==================== */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const modelLabel = `${s.llm_backend} · ${s.llm_model || "未配置"}`;
    setBadge("status-ollama", s.llm_ready, "模型 · " + modelLabel);
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
    addMessage("system", "对话已开始，请先清空对话（🗑️）再切换模式。");
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

/* ==================== 设置面板 ==================== */
function openSettings() { $("settings-pane").classList.remove("hidden"); loadSettings(); loadCharacters(); loadVoicePresets(); loadHistory(); loadPlugins(); }
function closeSettings() { $("settings-pane").classList.add("hidden"); }

async function loadSettings() {
  const s = await api("/api/settings");
  if (!s || !s.settings) return;
  const st = s.settings;
  $("current-character").textContent = st.character_name;
  $("current-voice").textContent = st.current_voice_name;
  $("influence-min").value = st.influence_min;
  $("influence-max").value = st.influence_max;
  $("tts-volume").value = st.tts_volume;
  $("music-volume").value = st.music_volume;
  $("tts-volume-val").textContent = st.tts_volume.toFixed(2);
  $("music-volume-val").textContent = st.music_volume.toFixed(2);
  $("internet-enabled").checked = st.internet_enabled;
  $("debug-mode").checked = st.debug_mode;
  $("usage-info").textContent = `${st.tavily.used} / ${st.tavily.limit}（剩 ${st.tavily.remaining}）`;
}

async function loadCharacters() {
  const r = await api("/api/characters");
  const sel = $("character-presets");
  sel.innerHTML = "";
  (r.presets || []).forEach((p) => {
    const o = document.createElement("option");
    o.value = p; o.textContent = p;
    sel.appendChild(o);
  });
}

async function loadVoicePresets() {
  const r = await api("/api/voice_presets");
  const sel = $("voice-presets");
  sel.innerHTML = "";
  (r.presets || []).forEach((p) => {
    const o = document.createElement("option");
    o.value = p.name; o.textContent = `${p.name} — ${p.description || ""}`;
    sel.appendChild(o);
  });
}

async function loadHistory() {
  const r = await api("/api/history");
  const list = $("history-list");
  list.innerHTML = "";
  (r.records || []).forEach((rec) => {
    const div = document.createElement("div");
    div.className = "history-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = rec.saved;
    cb.dataset.index = rec.index;
    const body = document.createElement("div");
    body.className = "h-body";
    body.innerHTML = `<b>#${rec.index}</b> 用户：${escapeHtml(rec.user)}<br>助手：${escapeHtml(rec.assistant)}`;
    div.appendChild(cb); div.appendChild(body);
    list.appendChild(div);
  });
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ==================== 插件管理 ==================== */
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

async function loadPlugins() {
  const r = await api("/api/plugins");
  const models = await fetchOllamaModels();
  renderPlugins(r.plugins || [], models);
}

// 刷新插件的动态状态面板（如播放列表队列）
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
      renderPlugins(r.plugins || [], ollamaModelsCache || []);   // 直接使用返回结果
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

    // 插件动作按钮（一键触发）
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
          if (r && r.reply) addMessage("assistant", r.reply);
          if (r && r.speak && r.stream_id) await playStream(r.stream_id);
          if (r && r.music && r.music.url) {
            activePlaylistPlugin = r.music_plugin || null;
            playMusicUrl(r.music.url, r.music.title);
          } else if (r && r.music_control === "stop") {
            musicAudio.pause(); musicAudio.src = ""; $("music-bar").classList.add("hidden");
            activePlaylistPlugin = null;
          }
          // 动作可能修改了设置（如自动调优），重新加载插件以刷新设置表单与状态
          loadPlugins();
        };
        actRow.appendChild(btn);
      });
      div.appendChild(actRow);
    }

    // 插件动态状态（如播放列表队列）
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
      p.settings_schema.forEach((field) => {
        const row = document.createElement("div");
        row.className = "row";
        const label = document.createElement("label");
        label.textContent = field.label || field.key;
        row.appendChild(label);

        let input;
        const val = (p.settings && p.settings[field.key]) !== undefined ? p.settings[field.key] : "";
        if (field.type === "select") {
          input = document.createElement("select");
          (field.options || []).forEach((opt) => {
            const o = document.createElement("option");
            // 兼容 {value, label} 与纯字符串两种选项
            const v = (typeof opt === "object" && opt !== null) ? opt.value : opt;
            const t = (typeof opt === "object" && opt !== null) ? (opt.label || opt.value) : opt;
            o.value = v; o.textContent = t; o.selected = (v === val);
            input.appendChild(o);
          });
          // 后端切换时就地更新对应模型字段（不重渲染整表单，避免重置已选后端）
          if (field.key.endsWith("_backend")) {
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
          // options_source=ollama_models 时用缓存的可安装模型列表填充
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
        form.appendChild(row);
      });

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
          addMessage("system", `保存失败：${r.error || "未知错误"}`);
          return;
        }
        addMessage("system", `已保存「${p.name}」设置。`);
        loadPlugins();
      };
      form.appendChild(saveBtn);
      div.appendChild(form);
    }
    list.appendChild(div);
  });
}

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
  return div;
}

function actionWrap() {
  const wrap = document.createElement("div");
  wrap.className = "actions";
  return wrap;
}

function speakButton(getUrls) {
  const btn = document.createElement("button");
  btn.className = "speak-btn";
  btn.textContent = "🔊 重播";
  btn.title = "重新播放语音";
  btn.onclick = () => { speechPromise = playQueue(getUrls()); };
  return btn;
}

function onlineBadge() {
  const b = document.createElement("span");
  b.className = "online-tag";
  b.textContent = "🌐 联网";
  return b;
}

/* ==================== 音频播放（流式 + 队列） ==================== */
let currentAudio = null;
let currentResolve = null;
let speechPromise = Promise.resolve();
let speechEpoch = 0;

// 播放单个音频；结束时（或被打断时）resolve
function playOne(url) {
  return new Promise((resolve) => {
    stopCurrentAudio();       // 打断上一段，并让其 resolve
    currentResolve = resolve;
    const audio = new Audio(url);
    currentAudio = audio;
    audio.volume = $("tts-volume") ? parseFloat($("tts-volume").value) : 1.0;
    const done = () => {
      if (currentAudio === audio) currentAudio = null;
      if (currentResolve === resolve) currentResolve = null;
      resolve();
    };
    audio.onended = done;
    audio.onerror = done;
    audio.play().catch(done);
  });
}

// 顺序播放固定列表（重播）；每次重播会打断正在进行的流式播放
async function playQueue(urls) {
  const epoch = ++speechEpoch;
  for (const url of (urls || [])) {
    if (epoch !== speechEpoch) break;
    await playOne(url);
  }
}

// 流式播放：长轮询服务端，逐句拿到即播放；边播放边预取下一句，消除衔接等待
async function playStream(streamId, onUrl) {
  if (!streamId) return;
  const epoch = ++speechEpoch;
  const fetchNext = () => api(`/api/tts/next?id=${encodeURIComponent(streamId)}`).catch(() => ({ done: true }));
  let pending = fetchNext();
  while (true) {
    const r = await pending;
    if (r.done) break;
    if (epoch !== speechEpoch) break;   // 已被新的播放 / 手动停止打断
    if (r.audio) {
      if (onUrl) onUrl(r.audio);
      pending = fetchNext();            // 预取下一句
      await playOne(r.audio);
    } else {
      pending = fetchNext();
    }
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
}

/* ==================== 音乐播放 ==================== */
const musicAudio = $("music-audio");

function showMusicBar(title) {
  $("music-bar").classList.remove("hidden");
  $("music-title").textContent = title || "音乐";
}

let suspendLiveForMusic = false;
let activePlaylistPlugin = null;   // 当前活动的播放列表插件名

// 直接播放一个已就绪的音乐 URL（供播放列表插件使用）
function playMusicUrl(url, title) {
  showMusicBar(title || "音乐");
  musicAudio.src = url;
  musicAudio.volume = $("music-volume") ? parseFloat($("music-volume").value) : 0.7;
  musicAudio.play();
}

async function playMusic(url, title, keyword) {
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
    if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
    addMessage("system", "音乐下载失败：" + (resp.error || ""));
    return;
  }

  showMusicBar(title || keyword);
  // 在 WebUI 中同时显示“即将播放”提示文本（语音仍会播放）
  if (resp.intro_text) addMessage("assistant", resp.intro_text);
  if (resp.intro_audio && resp.intro_audio.length) {
    await playQueue(resp.intro_audio);
  }
  musicAudio.src = resp.music_url;
  musicAudio.volume = $("music-volume") ? parseFloat($("music-volume").value) : 0.7;
  musicAudio.play();
}

// 音乐开始播放：保持禁止语音识别，并打断可能仍在进行的录音
musicAudio.addEventListener("playing", () => {
  stopActiveRecording();
  if (currentMode === "live") { suspendLiveForMusic = true; setLiveIndicator(); }
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
      refreshPluginState(activePlaylistPlugin);
      return;
    }
    // 播放列表结束
    activePlaylistPlugin = null;
    musicAudio.src = "";
    $("music-bar").classList.add("hidden");
    if (r && r.reply) addMessage("assistant", r.reply);
    if (r && r.speak && r.stream_id) await playStream(r.stream_id);
    if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
    return;
  }

  // 原有单曲逻辑
  const title = $("music-title").textContent;
  try {
    const resp = await api("/api/music/ended", { method: "POST", body: { title } });
    // 在 WebUI 中同时显示“播放完毕”提示文本（语音仍会播放）
    if (resp.ok && resp.text) addMessage("assistant", resp.text);
    if (resp.ok && resp.audio && resp.audio.length) {
      await playQueue(resp.audio);
    }
  } catch (e) { /* 忽略 */ }
  musicAudio.src = "";
  $("music-bar").classList.add("hidden");
  if (currentMode === "live") { suspendLiveForMusic = false; setLiveIndicator(); }
});

/* ==================== 处理消息（核心） ==================== */
async function handleMessage(text, mode) {
  text = (text || "").trim();
  if (!text) return null;
  $("input").value = "";
  conversationStarted = true;
  addMessage("user", text);
  const thinking = addMessage("system", "思考中…");

  let result;
  try {
    result = await api("/api/chat", { method: "POST", body: { message: text, mode: mode || "qa" } });
  } catch (e) {
    thinking.remove();
    addMessage("system", "请求失败：" + e);
    return null;
  }
  thinking.remove();

  if (!result.ok) {
    addMessage("system", result.skip_reason || result.error || "处理失败");
    return result;
  }

  if (result.action === "skip") {
    addMessage("system", "⏭️ " + (result.skip_reason || "跳过"));
    return result;
  }

  if (result.action === "music_control") {
    const collected = [];
    const wrap = actionWrap();
    wrap.appendChild(speakButton(() => collected));
    addMessage("assistant", result.reply, wrap);
    speechPromise = playStream(result.stream_id, (u) => collected.push(u));
    if (result.music_control === "pause") musicAudio.pause();
    if (result.music_control === "resume") musicAudio.play().catch(() => {});
    if (result.music_control === "stop") {
      musicAudio.pause(); musicAudio.src = ""; $("music-bar").classList.add("hidden");
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
    wrap.appendChild(speakButton(() => collected));
    addMessage("assistant", result.reply, wrap);
    if (result.speak && result.stream_id) {
      await playStream(result.stream_id, (u) => collected.push(u));
    }
    playMusicUrl(result.music.url, result.music.title);
    refreshPluginState(activePlaylistPlugin);
    return result;
  }

  // 正常对话（流式播报）
  const collected = [];
  const wrap = actionWrap();
  if (result.need_online) wrap.appendChild(onlineBadge());
  wrap.appendChild(speakButton(() => collected));
  addMessage("assistant", result.reply, wrap);
  speechPromise = playStream(result.stream_id, (u) => collected.push(u));
  return result;
}

async function sendMessage(text) {
  await handleMessage(text, currentMode === "live" ? "live" : "qa");
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
      b.onclick = () => playMusic(v.url, v.title, keyword);
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
      const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
      try {
        const wav = await encodeWav(blob);
        resolve(await transcribe(wav));
      } catch (e) {
        resolve("");
      }
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
    addMessage("system", "🎤 正在聆听…（静音自动停止，或再次点击 🎤）");
  });
}

async function onMicClick() {
  if (currentMode === "live") {
    if (liveActive) { stopLive(); }
    else { startLive(); }
    return;
  }
  // 问答模式：单次录音（再次点击则停止）
  if (activeRecorder && activeRecorder.state === "recording") { activeRecorder.stop(); return; }
  const text = await recordOnce();
  if (text) sendMessage(text);
  else addMessage("system", "未识别到语音。");
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
  addMessage("system", "🎙️ 实时对话开启，连续聆听中…（说“退出”结束，“暂停”暂停）");
  while (liveActive) {
    if (livePaused || suspendLiveForMusic) { await sleep(300); continue; }

    const text = await recordOnce();
    if (!liveActive) break;          // 录音期间被用户停止
    if (!text) { if (liveActive) addMessage("system", "未识别到语音。"); continue; }

    const result = await handleMessage(text, "live");
    if (!result) continue;

    if (result.action === "exit") {
      addMessage("system", result.reply || "已退出实时对话。");
      liveActive = false;
      break;
    }
    if (result.action === "live_pause") {
      livePaused = true;
      setLiveIndicator();
      addMessage("system", "⏸️ 对话已暂停（点 🎤 继续，或说“继续”）");
      continue;
    }
    if (result.action === "live_resume") {
      livePaused = false;
      setLiveIndicator();
      continue;
    }
    // 等待语音播报完成后，留一小段缓冲让音频设备彻底释放，再聆听下一句
    await speechPromise;
    await sleep(300);
  }
  liveActive = false;
  livePaused = false;
  setLiveIndicator();
  addMessage("system", "实时对话已停止。");
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
    headers: { "Content-Type": "audio/wav" },
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
};

$("btn-settings").onclick = openSettings;
$("btn-settings-close").onclick = closeSettings;

$("btn-char-default").onclick = async () => { await api("/api/character", { method: "POST", body: { mode: "default" } }); loadSettings(); addMessage("system", "已切换为默认角色。"); };
$("btn-char-preset").onclick = async () => { await api("/api/character", { method: "POST", body: { mode: "preset", preset_name: $("character-presets").value } }); loadSettings(); };
$("btn-char-custom").onclick = async () => {
  await api("/api/character", { method: "POST", body: { mode: "custom", raw_input: $("char-name").value, custom_req: $("char-req").value, use_search: $("char-search").checked, save: true } });
  loadSettings(); loadCharacters();
};

$("btn-voice-default").onclick = async () => { await api("/api/voice_preset", { method: "POST", body: { mode: "default" } }); loadSettings(); };
$("btn-voice-apply").onclick = async () => { await api("/api/voice_preset", { method: "POST", body: { mode: "preset", name: $("voice-presets").value } }); loadSettings(); };
$("btn-voice-create").onclick = async () => {
  const r = await api("/api/voice_preset", { method: "POST", body: { mode: "create", name: $("voice-name").value } });
  addMessage("system", r.ok ? r.message : (r.error || "创建失败"));
  loadVoicePresets();
};

$("btn-influence").onclick = async () => {
  await api("/api/settings", { method: "POST", body: { influence_min: +$("influence-min").value, influence_max: +$("influence-max").value } });
  loadSettings();
};

$("tts-volume").oninput = () => $("tts-volume-val").textContent = parseFloat($("tts-volume").value).toFixed(2);
$("music-volume").oninput = () => { $("music-volume-val").textContent = parseFloat($("music-volume").value).toFixed(2); musicAudio.volume = parseFloat($("music-volume").value); };
$("tts-volume").onchange = async () => { await api("/api/settings", { method: "POST", body: { tts_volume: parseFloat($("tts-volume").value) } }); };
$("music-volume").onchange = async () => { await api("/api/settings", { method: "POST", body: { music_volume: parseFloat($("music-volume").value) } }); };

$("internet-enabled").onchange = async () => { await api("/api/settings", { method: "POST", body: { internet_enabled: $("internet-enabled").checked } }); };
$("debug-mode").onchange = async () => { await api("/api/settings", { method: "POST", body: { debug_mode: $("debug-mode").checked } }); };

$("btn-usage-reset").onclick = async () => {
  await api("/api/usage", { method: "POST", body: { used: +$("usage-used").value } });
  loadSettings();
};

$("btn-history-refresh").onclick = loadHistory;
$("btn-history-save").onclick = async () => {
  const indexes = [...document.querySelectorAll("#history-list input:checked")].map((c) => c.dataset.index);
  await api("/api/history/mark", { method: "POST", body: { indexes } });
  const r = await api("/api/save", { method: "POST", body: { mode: "marked" } });
  addMessage("system", r.ok ? `已保存 ${r.result.count} 条记录。` : "保存失败");
  loadHistory();
};
$("btn-history-latest").onclick = async () => {
  const r = await api("/api/save", { method: "POST", body: { mode: "latest" } });
  addMessage("system", r.ok ? "已保存最新记录。" : "保存失败");
};
$("btn-history-clear").onclick = async () => { await api("/api/history/clear", { method: "POST" }); loadHistory(); };

$("btn-plugins-reload").onclick = async () => {
  const r = await api("/api/plugins/reload", { method: "POST" });
  addMessage("system", r.ok ? `已重新加载插件（${r.plugins.length} 个）` : "插件加载失败");
  loadPlugins();
};

$("btn-music-toggle").onclick = () => { musicAudio.paused ? musicAudio.play() : musicAudio.pause(); };
$("btn-music-stop").onclick = async () => {
  musicAudio.pause();
  musicAudio.src = "";
  $("music-bar").classList.add("hidden");
  activePlaylistPlugin = null;
  suspendLiveForMusic = false; setLiveIndicator();
  await api("/api/music/stop", { method: "POST" });
};

/* ==================== 初始化 ==================== */
(async function init() {
  await refreshStatus();
  setInterval(refreshStatus, 5000);
})();
