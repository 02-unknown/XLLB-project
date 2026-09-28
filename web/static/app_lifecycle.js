/* 小笼洛包 · 窗口生命周期上报（所有页面共用，放在各页面 <head> 之后尽早加载）
 *
 * 作用：让后台知道「界面窗口还开着吗」。
 *   · 心跳：每隔几秒 POST /api/app/beat；
 *   · 关闭：窗口真正关闭时（pagehide 且不是 bfcache 往返）POST /api/app/window-closed；
 *   · 页面之间跳转会先调用 XLLB_NAV.allow()，此时只发心跳、不发「关闭」信标。
 * 服务端据此在窗口关闭后完整退出：卸载模型权重、停止 GPT-SoVITS / Ollama、清理缓存
 * （见 web/app_lifecycle.py；任何 HTTP 请求也算存活，所以旧页面关掉后同样会被兜底清理）。
 */
(function () {
  "use strict";
  var CLIENT_ID = "xllb-life";
  var timer = null;
  var navAllowed = false;

  function beat(path) {
    try {
      fetch(path || "/api/app/beat", {
        method: "POST",
        keepalive: true,
        headers: { "X-XLLB-Client": CLIENT_ID },
      }).catch(function () {});
    } catch (e) { /* 忽略：心跳失败不影响界面 */ }
  }

  function restartTimer() {
    if (timer) clearInterval(timer);
    // 后台标签页里浏览器会把定时器降频，这里同步放宽间隔，避免无意义请求
    timer = setInterval(function () { beat(); }, document.hidden ? 10000 : 4000);
  }

  /** 页面内跳转前调用：这一次卸载不算「关闭程序」 */
  window.XLLB_NAV = {
    allow: function () {
      navAllowed = true;
      setTimeout(function () { navAllowed = false; }, 3000);
    },
  };

  beat();
  restartTimer();
  document.addEventListener("visibilitychange", function () { beat(); restartTimer(); });
  window.addEventListener("pageshow", function () { beat(); });          // bfcache 恢复
  window.addEventListener("pagehide", function (e) {
    if (e.persisted) return;
    if (navAllowed) { beat(); return; }        // 页面内跳转：不是关闭
    beat("/api/app/window-closed");            // 真正关闭
  });
})();
