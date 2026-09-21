/* suchen 界面动态层
   在不改动 app.js 业务逻辑的前提下，为界面加上：
   1. 按钮的加载 / 成功反馈（知识库刷新、应用规则、保存条目）
   2. 知识库导航的实时规则数角标与更新脉冲
   3. 页面切换过渡与骨架屏
   4. 登录 / 注册由 login.html 自己处理，这里只负责主界面的动效
*/

(function () {
  "use strict";

  var BUSY_CLASS = "is-busy";
  var DONE_CLASS = "is-done";

  function $(id) {
    return document.getElementById(id);
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.classList.add(BUSY_CLASS);
      button.classList.remove(DONE_CLASS);
    } else {
      button.classList.remove(BUSY_CLASS);
      button.classList.add(DONE_CLASS);
      window.setTimeout(function () {
        button.classList.remove(DONE_CLASS);
      }, 1200);
    }
  }

  // ---------------------------------------------------------------------
  // 1. Fetch 拦截：把网络请求映射成按钮状态
  // ---------------------------------------------------------------------

  var KNOWLEDGE_BUTTONS = {
    "btn_knowledge_refresh": "refresh",
    "btn_knowledge_apply": "apply",
    "btn_knowledge_save": "save",
  };

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input && input.url) return input.url;
    return "";
  }

  function methodOf(input, init) {
    if (init && init.method) return String(init.method).toUpperCase();
    if (input && input.method) return String(input.method).toUpperCase();
    return "GET";
  }

  var originalFetch = window.fetch;

  window.fetch = function (input, init) {
    var url = urlOf(input);
    var method = methodOf(input, init);
    var isKnowledge = url.indexOf("/api/knowledge") !== -1;

    if (!isKnowledge) {
      return originalFetch.apply(this, arguments);
    }

    var target = null;
    if (url.indexOf("/api/knowledge/apply") !== -1) {
      target = $("btn_knowledge_apply");
    } else if (method === "POST") {
      target = $("btn_knowledge_save");
    } else if (method === "GET") {
      target = $("btn_knowledge_refresh");
    }
    setBusy(target, true);

    function finish() {
      setBusy(target, false);
      refreshBadge();
    }

    return originalFetch.apply(this, arguments).then(
      function (response) {
        finish();
        return response;
      },
      function (error) {
        finish();
        throw error;
      },
    );
  };

  // ---------------------------------------------------------------------
  // 2. 知识库导航角标：实时规则数 + 更新脉冲
  // ---------------------------------------------------------------------

  var lastSignature = "";

  function refreshBadge() {
    if (!originalFetch) return Promise.resolve();
    return originalFetch("/api/knowledge?page_size=1", { credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        var nav = $("nav_knowledge");
        if (!nav) return;
        var badge = nav.querySelector(".nav-badge");
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "nav-badge";
          nav.appendChild(badge);
        }
        badge.textContent = String(data.active || 0);
        badge.title = "生效中的剪辑规则：" + (data.active || 0) + " 条";

        var signature = String(data.active || 0) + "|" + String(data.updated_at || "");
        if (lastSignature && signature !== lastSignature) {
          var pulse = document.createElement("em");
          pulse.className = "nav-pulse";
          nav.appendChild(pulse);
          window.setTimeout(function () {
            if (pulse.parentNode) pulse.parentNode.removeChild(pulse);
          }, 5000);
          var updated = $("knowledge_updated");
          if (updated) {
            updated.classList.add("is-fresh");
            window.setTimeout(function () {
              updated.classList.remove("is-fresh");
            }, 2400);
          }
        }
        lastSignature = signature;
      })
      .catch(function () {
        // The console already surfaces connectivity problems.
      });
  }

  // ---------------------------------------------------------------------
  // 3. 页面切换过渡
  // ---------------------------------------------------------------------

  function playSwitch() {
    var body = document.querySelector(".app-body");
    if (!body) return;
    body.style.animation = "none";
    void body.offsetWidth;
    body.style.animation = "motionRise 320ms cubic-bezier(0.22,1,0.36,1) both";
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("[data-screen]") : null;
    if (!link) return;
    window.setTimeout(playSwitch, 0);
    if (link.dataset.screen === "knowledge") {
      window.setTimeout(refreshBadge, 260);
    }
  });

  // ---------------------------------------------------------------------
  // 4. 状态点高亮与骨架屏
  // ---------------------------------------------------------------------

  function highlightDots() {
    var dots = document.querySelectorAll(".knowledge-system i, .worker-state i");
    Array.prototype.forEach.call(dots, function (dot) {
      if (dot.classList.contains("is-online")) dot.classList.add("is-on");
    });
  }

  function skeleton(target) {
    if (!target) return;
    target.innerHTML =
      '<div class="motion-skeleton"><span></span><span></span><span></span></div>';
  }

  // ---------------------------------------------------------------------
  // 5. 账号区：未登录时给出登录入口，登录后显示邮箱与退出
  // ---------------------------------------------------------------------

  function renderAccount(user) {
    var actions = document.querySelector(".top-actions");
    if (!actions) return;
    if (actions.querySelector(".account-chip")) return;
    var existing = $("motion_account");
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    var wrap = document.createElement("span");
    wrap.id = "motion_account";
    wrap.className = "motion-account";

    if (user) {
      var name = document.createElement("strong");
      name.textContent = user.display_name || user.email;
      name.title = user.email;
      var out = document.createElement("button");
      out.type = "button";
      out.className = "button secondary compact-button";
      out.textContent = "退出";
      out.addEventListener("click", function () {
        originalFetch("/api/auth/logout", {
          method: "POST",
          credentials: "same-origin",
        }).then(function () {
          window.location.reload();
        });
      });
      wrap.appendChild(name);
      wrap.appendChild(out);
    } else {
      var enter = document.createElement("button");
      enter.type = "button";
      enter.className = "button primary compact-button";
      enter.textContent = "登录 / 注册";
      enter.addEventListener("click", function () {
        window.location.href = "/login";
      });
      wrap.appendChild(enter);
    }

    actions.insertBefore(wrap, actions.firstChild);
  }

  function loadAccount() {
    if (!originalFetch) return;
    originalFetch("/api/auth/me", { credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) renderAccount(data.user);
      })
      .catch(function () {
        // Offline: keep the toolbar unchanged.
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    highlightDots();
    loadAccount();
    refreshBadge();
    window.setInterval(refreshBadge, 60000);
    var list = $("knowledge_list");
    if (list && !list.children.length) skeleton(list);
  });

  window.suchenMotion = { refreshBadge: refreshBadge, playSwitch: playSwitch };
})();
