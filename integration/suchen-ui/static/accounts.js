(function () {
  const state = { page: 1, pageSize: 20, total: 0, query: "", loading: false, request: null, resetUser: null, trigger: null, accessChange: null };
  const $ = (id) => document.getElementById(id);
  const formatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

  function dateText(value) { if (!value) return "从未登录"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "--" : formatter.format(date); }
  function setStatus(copy, error) { $("accounts_status").textContent = copy; $("accounts_status").className = `accounts-status${error ? " error" : ""}`; }
  function cell(text, className) { const td = document.createElement("td"); td.textContent = text; if (className) td.className = className; return td; }

  function render(data) {
    const rows = $("accounts_rows"); rows.replaceChildren(); state.total = Number(data.total || 0); state.page = Number(data.page || 1);
    (data.items || []).forEach((user) => {
      const row = document.createElement("tr");
      row.append(cell(user.email), cell(user.display_name || "未填写"));
      const role = document.createElement("td"); const roleBadge = document.createElement("span"); roleBadge.className = `account-role ${user.role === "admin" ? "admin" : "member"}`; roleBadge.textContent = user.role === "admin" ? "负责人" : "成员"; role.append(roleBadge); row.append(role);
      const enabled = document.createElement("td"); const stateBadge = document.createElement("span"); stateBadge.className = `account-state ${user.enabled ? "enabled" : "disabled"}`; stateBadge.textContent = user.enabled ? "正常" : "已停用"; enabled.append(stateBadge); row.append(enabled);
      row.append(cell(dateText(user.created_at)), cell(dateText(user.last_login_at)), cell(user.password_state, "password-safe"));
      const action = document.createElement("td"); action.className = "account-actions";
      const password = actionButton("重置密码", "", () => openDialog(user, password));
      const roleButton = actionButton(user.role === "admin" ? "取消负责人" : "授权负责人", user.role === "admin" ? "danger" : "success", () => openAccess(user, { role: user.role === "admin" ? "member" : "admin" }, roleButton));
      const enabledButton = actionButton(user.enabled ? "停用账号" : "启用账号", user.enabled ? "danger" : "success", () => openAccess(user, { enabled: !user.enabled }, enabledButton));
      action.append(password, roleButton, enabledButton); row.append(action); rows.append(row);
    });
    const count = (data.items || []).length; const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0; const end = start ? start + count - 1 : 0;
    $("accounts_empty").hidden = count !== 0; $("accounts_total").textContent = `数据库共 ${state.total} 个注册账号`; $("accounts_range").textContent = `${start}–${end} / ${state.total}`;
    $("accounts_prev").disabled = state.page <= 1; $("accounts_next").disabled = end >= state.total; setStatus(`已读取第 ${state.page} 页 · 密码字段未返回浏览器`, false);
  }

  function actionButton(label, tone, handler) { const button = document.createElement("button"); button.type = "button"; button.className = `account-action ${tone}`.trim(); button.textContent = label; button.addEventListener("click", handler); return button; }

  async function load() {
    if (state.loading) state.request?.abort(); state.request = new AbortController(); state.loading = true; setStatus("正在读取账号数据库…", false); $("accounts_refresh").disabled = true;
    try { const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) }); if (state.query) params.set("q", state.query); const response = await fetch(`/api/admin/users?${params}`, { cache: "no-store", signal: state.request.signal }); if (response.status === 401) return window.location.assign("/login?next=/accounts"); if (response.status === 403) throw new Error("当前账号没有管理员权限"); const data = await response.json(); if (!response.ok) throw new Error(data.detail || "账号数据库读取失败"); render(data); }
    catch (error) { if (error.name !== "AbortError") setStatus(error.message || "账号数据库读取失败，请重试", true); }
    finally { state.loading = false; $("accounts_refresh").disabled = false; }
  }

  function openDialog(user, trigger) { state.resetUser = user; state.trigger = trigger; $("password_dialog_title").textContent = `重置 ${user.email} 的密码`; $("new_password").value = ""; $("password_error").hidden = true; $("password_dialog").hidden = false; document.querySelector(".accounts-shell").inert = true; requestAnimationFrame(() => $("new_password").focus()); }
  function closeDialog() { $("password_dialog").hidden = true; document.querySelector(".accounts-shell").inert = false; state.trigger?.focus(); state.resetUser = null; state.trigger = null; }
  function showPasswordError(copy) { $("password_error").textContent = copy; $("password_error").hidden = false; }

  function openAccess(user, patch, trigger) { const isRole = Object.hasOwn(patch, "role"); const action = isRole ? (patch.role === "admin" ? "授权为负责人" : "取消负责人权限") : (patch.enabled ? "重新启用账号" : "停用账号"); state.accessChange = { user, patch, trigger, action }; $("access_dialog_title").textContent = action; $("access_dialog_copy").textContent = `${action}：${user.email}。${patch.enabled === false ? "停用后该用户将无法继续登录。" : "新权限将在下次请求时生效。"}`; $("access_error").hidden = true; $("access_dialog").hidden = false; document.querySelector(".accounts-shell").inert = true; requestAnimationFrame(() => $("access_cancel").focus()); }
  function closeAccess() { const trigger = state.accessChange?.trigger; $("access_dialog").hidden = true; document.querySelector(".accounts-shell").inert = false; state.accessChange = null; trigger?.focus(); }

  $("password_form").addEventListener("submit", async (event) => { event.preventDefault(); const value = $("new_password").value; if (value.length < 8) { showPasswordError("新密码至少需要 8 位"); $("new_password").focus(); return; } const button = $("password_submit"); const email = state.resetUser.email; button.disabled = true; button.setAttribute("aria-busy", "true"); try { const response = await fetch(`/api/admin/users/${encodeURIComponent(state.resetUser.id)}/reset-password`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_password: value }) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || "密码重置失败"); closeDialog(); setStatus(`已重置 ${email} 的密码`, false); }
    catch (error) { showPasswordError(error.message || "密码重置失败，请重试"); } finally { button.disabled = false; button.removeAttribute("aria-busy"); } });
  $("access_submit").addEventListener("click", async () => { if (!state.accessChange) return; const { user, patch, action } = state.accessChange; const button = $("access_submit"); button.disabled = true; button.setAttribute("aria-busy", "true"); try { const response = await fetch(`/api/admin/users/${encodeURIComponent(user.id)}/access`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || "权限变更失败"); closeAccess(); setStatus(`${user.email}：${action}成功`, false); await load(); } catch (error) { $("access_error").textContent = error.message || "权限变更失败，请重试"; $("access_error").hidden = false; } finally { button.disabled = false; button.removeAttribute("aria-busy"); } });
  document.querySelectorAll("[data-close-dialog]").forEach((node) => node.addEventListener("click", closeDialog)); document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("password_dialog").hidden) closeDialog(); });
  document.querySelectorAll("[data-close-access]").forEach((node) => node.addEventListener("click", closeAccess)); document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("access_dialog").hidden) closeAccess(); });
  $("password_toggle").addEventListener("click", () => { const input = $("new_password"); const show = input.type === "password"; input.type = show ? "text" : "password"; $("password_toggle").textContent = show ? "隐藏" : "显示"; $("password_toggle").setAttribute("aria-label", show ? "隐藏新密码" : "显示新密码"); });
  let timer; let composing = false; $("accounts_search").addEventListener("compositionstart", () => { composing = true; }); $("accounts_search").addEventListener("compositionend", () => { composing = false; scheduleSearch(); }); $("accounts_search").addEventListener("input", scheduleSearch);
  function scheduleSearch() { $("accounts_search_clear").hidden = !$("accounts_search").value; if (composing) return; clearTimeout(timer); timer = setTimeout(() => { state.query = $("accounts_search").value.trim(); state.page = 1; load(); }, 300); }
  $("accounts_search_clear").addEventListener("click", () => { clearTimeout(timer); $("accounts_search").value = ""; $("accounts_search_clear").hidden = true; state.query = ""; state.page = 1; load(); $("accounts_search").focus(); });
  $("accounts_refresh").addEventListener("click", load); $("accounts_prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; load(); } }); $("accounts_next").addEventListener("click", () => { if (state.page * state.pageSize < state.total) { state.page += 1; load(); } });
  async function loadOperations() {
    try {
      const response = await fetch('/api/admin/operations', { cache: 'no-store' });
      if (!response.ok) throw new Error('无法读取运维数据，请确认负责人权限后刷新');
      const data = await response.json();
      const rows = $('operations_rows'); rows.replaceChildren();
      data.records.forEach((item) => { const row = document.createElement('tr'); row.append(cell(item.name), cell(String(item.count))); rows.append(row); });
      const names = { asr_model: '语音模型', pose_model: '动作模型', ffmpeg: '视频渲染', ffprobe: '视频检测', database: '数据库' };
      $('operations_status').textContent = `${data.database} · ${Object.entries(data.components).map(([key, ready]) => `${names[key] || key}：${ready ? '已就绪' : '缺失'}`).join(' · ')}`;
    } catch (error) { $('operations_status').textContent = error.message; }
  }
  $('accounts_refresh').addEventListener('click', loadOperations);
  function refreshSharedData() {
    if (document.hidden || state.loading || state.resetUser || state.accessChange || document.querySelector('.accounts-main')?.contains(document.activeElement)) return;
    loadOperations();
    load();
  }
  setInterval(refreshSharedData, 15000);
  document.addEventListener('visibilitychange', refreshSharedData);
  window.addEventListener('online', refreshSharedData);
  loadOperations();
  load();
}());
