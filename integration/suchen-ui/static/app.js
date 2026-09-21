const $ = (id) => document.getElementById(id);

const app = {
  canManageKnowledge: document.body.dataset.canManageKnowledge === "true",
  folder: "",
  media: [],
  selected: null,
  taskCurrent: null,
  timelines: {},
  results: [],
  filter: "all",
  view: "source",
  running: false,
  timelineScale: 28,
  phase: "idle",
  phaseMessage: "全自动任务在后台静默运行",
  transcript: { ready: false, sentences: [], dirty: false, loading: false },
  transcriptToken: 0,
  activeDock: "transcript",
  taskKind: "batch",
  actionTemplate: null,
  actionTemplateDirty: false,
  screen: "workspace",
  reviews: {},
  reviewDirty: false,
  reviewSaving: false,
  agent: { jobs: [], outputs: {}, traces: {}, active: false },
  memory: {
    loading: false,
    error: null,
    summary: null,
    recentRuns: [],
    recommendations: {},
    updatedAt: null,
  },
  knowledge: {
    items: [], loading: false, loaded: false, error: null, system: null, contract: null,
    page: 1, pages: 1, pageSize: 10, total: 0, allTotal: 0, active: 0,
    editingId: null, updatedAt: null,
  },
};
window.app = app;

let toastTimer = null;
let dialogResolver = null;
let dialogTrigger = null;
let dialogConfirmAction = null;
let memoryRequestController = null;
let knowledgeRequestController = null;
let knowledgeSearchTimer = null;
let knowledgeHealthTimer = null;
let healthRetryTimer = null;
let pollTimer = null;
let pollInFlight = false;
let pollFailures = 0;
let reconnectInFlight = false;
let logStream = null;

function toast(message, kind = "info") {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 2800);
}

function closeAppDialog(confirmed = false) {
  const dialog = $("app_dialog");
  if (dialog.hidden) return;
  const value = $("app_dialog_input").value;
  dialog.hidden = true;
  $("app_dialog_input_field").hidden = true;
  $("app_dialog_cancel").hidden = false;
  $("app_dialog_confirm").className = "button danger";
  $("app_dialog_confirm").disabled = false;
  $("app_dialog_cancel").disabled = false;
  $("app_dialog").removeEventListener("keydown", trapDialogFocus);
  const shell = document.querySelector(".app-shell");
  if (shell) shell.inert = false;
  const resolve = dialogResolver;
  dialogResolver = null;
  dialogConfirmAction = null;
  if (dialogTrigger && document.contains(dialogTrigger)) dialogTrigger.focus();
  dialogTrigger = null;
  if (resolve) resolve({ confirmed, value });
}

async function submitAppDialog() {
  if (!dialogConfirmAction) {
    closeAppDialog(true);
    return;
  }
  const confirm = $("app_dialog_confirm");
  const cancel = $("app_dialog_cancel");
  const original = confirm.textContent;
  confirm.disabled = true;
  cancel.disabled = true;
  confirm.textContent = "正在处理…";
  try {
    await dialogConfirmAction($("app_dialog_input").value);
    closeAppDialog(true);
  } catch (error) {
    confirm.disabled = false;
    cancel.disabled = false;
    confirm.textContent = original;
    toast(error.message || "操作失败，请重试", "error");
  }
}

function trapDialogFocus(event) {
  if (event.key === "Escape") {
    event.preventDefault();
    closeAppDialog(false);
    return;
  }
  if (event.key !== "Tab") return;
  const controls = [...$("app_dialog").querySelectorAll("button:not(:disabled), input:not([hidden]):not(:disabled)")]
    .filter((item) => item.offsetParent !== null);
  if (!controls.length) return;
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function openAppDialog({
  title = "请确认",
  description = "",
  confirmLabel = "确认",
  intent = "danger",
  inputLabel = "",
  inputValue = "",
  cancelLabel = "取消",
  hideCancel = false,
  confirmAction = null,
} = {}) {
  if (dialogResolver) closeAppDialog(false);
  dialogTrigger = document.activeElement;
  $("app_dialog_title").textContent = title;
  $("app_dialog_description").textContent = description;
  const confirm = $("app_dialog_confirm");
  confirm.textContent = confirmLabel;
  confirm.className = `button ${intent}`;
  $("app_dialog_cancel").textContent = cancelLabel;
  $("app_dialog_cancel").hidden = hideCancel;
  const inputField = $("app_dialog_input_field");
  inputField.hidden = !inputLabel;
  $("app_dialog_input_label").textContent = inputLabel || "路径";
  $("app_dialog_input").value = inputValue;
  dialogConfirmAction = typeof confirmAction === "function" ? confirmAction : null;
  const shell = document.querySelector(".app-shell");
  if (shell) shell.inert = true;
  $("app_dialog").hidden = false;
  $("app_dialog").addEventListener("keydown", trapDialogFocus);
  requestAnimationFrame(() => (inputLabel ? $("app_dialog_input") : $("app_dialog_cancel")).focus());
  return new Promise((resolve) => { dialogResolver = resolve; });
}

function formatTime(seconds) {
  if (!Number.isFinite(Number(seconds))) return "--:--";
  const value = Math.max(0, Number(seconds));
  const minutes = Math.floor(value / 60);
  const secs = Math.floor(value % 60);
  const tenths = Math.floor((value % 1) * 10);
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${tenths}`;
}

function fileStem(name) {
  return String(name || "").replace(/\.[^.]+$/, "");
}

function resultFor(name) {
  return app.results.find((item) => item.name === name) || null;
}

function itemStatus(name) {
  const result = resultFor(name);
  if (app.running && name === app.taskCurrent) return "processing";
  if (app.running && result && result.status === "失败") {
    return name === app.taskCurrent ? "processing" : "queued";
  }
  if (result && result.status === "失败") return "failed";
  const timeline = app.timelines[name];
  const qa = timeline && timeline.qa;
  if (qa && Number(qa["驳回项"] || 0) > 0) return "failed";
  if (qa && Number(qa["告警项"] || 0) > 0) return "review";
  if (timeline) return "done";
  return "pending";
}

function statusCopy(status) {
  return { pending: "待处理", queued: "等待自动重剪", processing: "正在重新剪辑", done: "AI 成片已生成", review: "有质检告警", failed: "处理失败，可重新剪辑" }[status] || "待处理";
}

function currentTimeline() {
  return app.selected ? app.timelines[app.selected] || null : null;
}

function appendLog(line) {
  const box = $("log");
  box.textContent += `${line}\n`;
  box.scrollTop = box.scrollHeight;
}

function setLogOpen(open) {
  $("log_drawer").classList.toggle("open", open);
  $("log_drawer").setAttribute("aria-hidden", open ? "false" : "true");
}

function setSegment(containerId, key, value) {
  $(containerId).querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset[key] === value);
  });
}

async function pickMediaFolder() {
  const answer = await openAppDialog({
    title: "导入本地素材目录",
    description: "输入包含待剪辑视频的完整文件夹路径。",
    confirmLabel: "导入目录",
    intent: "primary",
    inputLabel: "素材目录",
    inputValue: $("folder").value || "",
  });
  if (answer.confirmed && answer.value.trim()) loadMedia(answer.value.trim());
}

function clearAssetSearch() {
  $("asset_search").value = "";
  $("asset_search_clear").hidden = true;
  $("asset_search").focus();
  renderAssets();
}

function setAssetFilter(filter) {
  app.filter = filter;
  setSegment("asset_filters", "filter", app.filter);
  renderAssets();
}

function setPreviewView(view) {
  const button = $("view_switch").querySelector(`[data-view="${view}"]`);
  if (!button || button.disabled) return;
  app.view = view;
  renderSelected();
}

function setTimelineZoom(delta) {
  app.timelineScale = Math.min(72, Math.max(16, app.timelineScale + Number(delta || 0)));
  renderTimeline();
}

function resetActionTemplate() {
  applyActionRequirements(ACTION_DEFAULTS);
  $("action_template_preset").value = "standard";
  toast("已恢复标准动作模板，请保存或应用");
}

function clearRunLog() {
  $("log").textContent = "";
}

const ACTION_DEFAULTS = {
  modes: {
    opening_full_front: "optional",
    origin_size_intro: "off",
    move_to_center_product_detail: "off",
    back_full_body_show: "optional",
    return_origin_front_finish: "off",
  },
  opening_max_sec: 1.5,
  approach_preroll_sec: 0.75,
  back_min_duration_sec: 0.6,
  return_stable_sec: 1.0,
};

const PRODUCT_PROFILES = {
  auto: {
    min: 3, max: 120, target: 25, estimateMin: 25, estimateMax: 120,
    rule: "AI 自动 · 3–120 秒",
    hint: "AI 将读取中文口播与画面，逐条选择对应时长标准。",
  },
  single: {
    min: 3, max: 30, target: 25, estimateMin: 25, estimateMax: 25,
    rule: "单品及小件 · 30 秒以内",
    hint: "重点保留面料与尺码，预计目标可在 3–30 秒内调整。",
  },
  set: {
    min: 3, max: 60, target: 50, estimateMin: 50, estimateMax: 50,
    rule: "套装及两件套 · 60 秒以内",
    hint: "完整覆盖上下装、面料与尺码，预计目标可在 3–60 秒内调整。",
  },
  bulky: {
    min: 3, max: 120, target: 120, estimateMin: 120, estimateMax: 120,
    rule: "大货外套 · 约 120 秒",
    hint: "皮草、羽绒、派克、双面呢优先保留面料、工艺、尺码与后背。",
  },
};

function productProfile() {
  return PRODUCT_PROFILES[$("product_type").value] || PRODUCT_PROFILES.auto;
}

function savedProductTargets() {
  try {
    return JSON.parse(localStorage.getItem("suchen.productTargets") || "{}") || {};
  } catch (_) {
    return {};
  }
}

function applyProductType(productType, persist = true) {
  const type = PRODUCT_PROFILES[productType] ? productType : "auto";
  const profile = PRODUCT_PROFILES[type];
  const targets = savedProductTargets();
  const target = Number(targets[type]);
  $("product_type").value = type;
  $("min_sec").value = String(profile.min);
  $("max_sec").value = String(profile.max);
  $("duration_profile_range").textContent = profile.rule;
  $("product_profile_hint").textContent = profile.hint;
  $("target_sec").min = String(profile.min);
  $("target_sec").max = String(profile.max);
  $("target_sec").value = String(
    Number.isFinite(target) && target >= profile.min && target <= profile.max
      ? target : profile.target,
  );
  $("target_sec").disabled = type === "auto";
  $("target_duration_label").textContent = type === "auto"
    ? "AI 识别后确定单条目标"
    : "预计每条成片目标";
  if (persist) localStorage.setItem("suchen.productType", type);
  updateBatchEstimate();
}

function rememberProductTarget() {
  const type = $("product_type").value;
  if (type === "auto") return;
  const profile = productProfile();
  const value = Number($("target_sec").value);
  if (!Number.isFinite(value) || value < profile.min || value > profile.max) return;
  const targets = savedProductTargets();
  targets[type] = value;
  localStorage.setItem("suchen.productTargets", JSON.stringify(targets));
}

function actionRequirementsFromUI() {
  const modes = {};
  document.querySelectorAll("[data-action-role]").forEach((select) => {
    modes[select.dataset.actionRole] = select.value;
  });
  return {
    modes,
    opening_max_sec: Number($("opening_max_sec").value),
    approach_preroll_sec: Number($("approach_preroll_sec").value),
    back_min_duration_sec: Number($("back_min_duration_sec").value),
    return_stable_sec: Number($("return_stable_sec").value),
  };
}

function applyActionRequirements(requirements = ACTION_DEFAULTS, persist = true) {
  const modes = requirements.modes || ACTION_DEFAULTS.modes;
  document.querySelectorAll("[data-action-role]").forEach((select) => {
    select.value = modes[select.dataset.actionRole] || ACTION_DEFAULTS.modes[select.dataset.actionRole];
    select.closest(".action-rule-row").classList.toggle("disabled", select.value === "off");
  });
  ["opening_max_sec", "approach_preroll_sec", "back_min_duration_sec", "return_stable_sec"].forEach((key) => {
    const value = Number(requirements[key]);
    $(key).value = Number.isFinite(value) ? String(value) : String(ACTION_DEFAULTS[key]);
  });
  if (persist) {
    localStorage.setItem("suchen.actionRequirements", JSON.stringify(actionRequirementsFromUI()));
    app.actionTemplateDirty = true;
    updateActionTemplateState("未保存修改");
  }
}

function restoreActionRequirements() {
  try {
    const saved = JSON.parse(localStorage.getItem("suchen.actionRequirements") || "null");
    applyActionRequirements(saved || ACTION_DEFAULTS, false);
  } catch (_) {
    applyActionRequirements(ACTION_DEFAULTS, false);
  }
}

function updateActionTemplateState(label) {
  const state = $("action_template_state");
  const note = $("action_template_note");
  if (state) state.textContent = label || (app.actionTemplateDirty ? "未保存修改" : "本机已保存");
  if (note && !app.actionTemplateDirty) note.textContent = "当前批次将使用已保存模板；安全质量门始终锁定。";
  if (note && app.actionTemplateDirty) note.textContent = "模板有未保存修改。保存后可应用到当前批次。";
}

function actionTemplatePayload() {
  return {
    name: $("action_template_name")?.value.trim() || "我的动作模板",
    ...actionRequirementsFromUI(),
  };
}

async function loadActionTemplate() {
  try {
    const data = await readApiJSON("/api/action-template");
    if (data.error) throw new Error(data.error || "动作模板读取失败");
    const template = data.template || ACTION_DEFAULTS;
    app.actionTemplate = template;
    if ($("action_template_name") && template.name) $("action_template_name").value = template.name;
    applyActionRequirements(template, false);
    app.actionTemplateDirty = false;
    updateActionTemplateState("本机已保存");
  } catch (error) {
    // A first-run/offline page still works with the local browser defaults.
    app.actionTemplate = actionRequirementsFromUI();
    updateActionTemplateState("浏览器本地模板");
  }
}

async function saveActionTemplate(quiet = false) {
  const payload = actionTemplatePayload();
  const response = await fetch("/api/action-template", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "动作模板保存失败");
  app.actionTemplate = data.template || payload;
  app.actionTemplateDirty = false;
  localStorage.setItem("suchen.actionRequirements", JSON.stringify(actionRequirementsFromUI()));
  updateActionTemplateState("已保存到本机");
  if (!quiet) toast("动作模板已保存，可应用到当前批次");
  return data.template;
}

function applyActionTemplateToBatch() {
  const settings = actionRequirementsFromUI();
  localStorage.setItem("suchen.actionRequirements", JSON.stringify(settings));
  app.actionTemplateDirty = false;
  updateActionTemplateState("已应用到当前批次");
  toast("本批次 AI 剪辑将使用当前动作模板");
}

const ACTION_PRESETS = {
  standard: ACTION_DEFAULTS,
  steady: {
    modes: { ...ACTION_DEFAULTS.modes, move_to_center_product_detail: "optional" },
    opening_max_sec: 1.5, approach_preroll_sec: 0.75,
    back_min_duration_sec: 0.6, return_stable_sec: 1.2,
  },
  detail: {
    modes: { ...ACTION_DEFAULTS.modes, back_full_body_show: "optional" },
    opening_max_sec: 1.5, approach_preroll_sec: 0.9,
    back_min_duration_sec: 0.8, return_stable_sec: 1.0,
  },
};

function chooseActionPreset(name) {
  if (!ACTION_PRESETS[name]) return;
  applyActionRequirements(ACTION_PRESETS[name]);
  app.actionTemplateDirty = true;
  updateActionTemplateState("预设已载入，未保存");
}

function editorSettings(asForm = false) {
  const actions = actionRequirementsFromUI();
  const settings = {
    folder: $("folder").value.trim() || app.folder,
    output: $("output").value.trim(),
    template: "main",
    product_type: $("product_type").value,
    min_sec: $("min_sec").value,
    max_sec: $("max_sec").value,
    target_sec: $("target_sec").value,
    model: $("model").value || "small",
    quality: $("quality").checked ? "1" : "0",
    pending_only: $("pending_only").checked ? "1" : "0",
    cam: "0",
    opening_max_sec: String(actions.opening_max_sec),
    approach_preroll_sec: String(actions.approach_preroll_sec),
    back_min_duration_sec: String(actions.back_min_duration_sec),
    return_stable_sec: String(actions.return_stable_sec),
  };
  Object.entries(actions.modes).forEach(([role, mode]) => { settings[`action_${role}`] = mode; });
  if (!asForm) return settings;
  const form = new FormData();
  Object.entries(settings).forEach(([key, value]) => form.append(key, value));
  return form;
}

async function loadMedia(folder) {
  const value = String(folder || "").trim();
  if (!value) return toast("请填写素材目录", "error");
  const response = await fetch(`/api/media?folder=${encodeURIComponent(value)}`);
  const data = await response.json();
  if (data.error) return toast(data.error, "error");
  app.folder = value;
  app.media = data.media || [];
  $("folder").value = value;
  if (!app.media.includes(app.selected)) app.selected = app.media[0] || null;
  renderAssets();
  if (app.selected) await selectMedia(app.selected, false);
  else renderAll();
  toast(`已导入 ${app.media.length} 条素材`);
}

async function restoreManagedUploads() {
  const response = await fetch("/api/media");
  const data = await response.json();
  if (!response.ok || data.error) return;
  app.folder = data.folder || "";
  app.media = data.media || [];
  if (!app.media.includes(app.selected)) app.selected = app.media[0] || null;
  renderAssets();
  if (app.selected) await selectMedia(app.selected, false);
}

async function selectOutput(value, quiet = false) {
  const output = String(value || "").trim();
  if (!output) return;
  const response = await fetch("/api/output", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ output }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    if (!quiet) toast(data.error || "输出目录不可用", "error");
    return;
  }
  await pollOnce();
  if (app.selected) await loadTranscript(app.selected, true);
  if (!quiet) toast(`输出目录已就绪，载入 ${data.loaded || 0} 条结果`);
}

function isVideoFile(file) {
  const name = String(file?.name || "").toLowerCase();
  return [".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".wmv"].some((ext) => name.endsWith(ext));
}

async function uploadFileCollection(files, triggerId, statusPrefix = "正在上传") {
  const candidates = Array.from(files || []).filter(isVideoFile);
  const skippedFormat = Math.max(0, Array.from(files || []).length - candidates.length);
  if (!candidates.length) return toast("文件夹中没有可接收的视频文件", "error");
  const button = $(triggerId);
  button.disabled = true;
  const batchSize = 30;
  let accepted = 0;
  let duplicateCount = 0;
  let renamedCount = 0;
  let skipped = skippedFormat;
  let lastFolder = "";
  try {
    for (let offset = 0; offset < candidates.length; offset += batchSize) {
      const batch = candidates.slice(offset, offset + batchSize);
      const form = new FormData();
      batch.forEach((file) => form.append("files", file, file.webkitRelativePath || file.name));
      $("upload_status").textContent = `${statusPrefix} ${Math.min(offset + batch.length, candidates.length)} / ${candidates.length} 个视频…`;
      const response = await fetch("/api/upload", { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok && !data.saved?.length) throw new Error(data.error || "上传失败");
      accepted += Number(data.accepted || 0);
      duplicateCount += (data.duplicates || []).length;
      renamedCount += (data.renamed || []).length;
      skipped += (data.skipped || []).length;
      lastFolder = data.folder || lastFolder;
      app.folder = lastFolder;
      app.media = data.saved || app.media;
      $("folder").value = lastFolder;
      renderAssets();
    }
    if (lastFolder) {
      await selectOutput($("output").value.trim(), true).catch(() => {});
      if (app.media[0] && !app.selected) await selectMedia(app.media[0], false);
    }
    $("upload_status").textContent = `已导入 ${accepted} 个视频${renamedCount ? ` · 重名已重命名 ${renamedCount}` : ""}${duplicateCount ? ` · 重复已跳过 ${duplicateCount}` : ""}${skipped ? ` · 跳过 ${skipped}` : ""}`;
    toast(`批量导入完成：新增 ${accepted} 个视频`);
  } catch (error) {
    $("upload_status").textContent = `上传中断：已新增 ${accepted} 个`;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function uploadVideos() {
  const input = $("upload_files");
  await uploadFileCollection(input.files, "btn_upload", "正在上传");
  input.value = "";
}

async function uploadFolderVideos() {
  const input = $("upload_folder");
  const first = input.files && input.files[0];
  if (first && first.webkitRelativePath) {
    const rootName = first.webkitRelativePath.split("/")[0];
    $("upload_status").textContent = `已选择“${rootName}”，正在筛选未剪辑视频…`;
  }
  await uploadFileCollection(input.files, "btn_upload_folder", "正在导入文件夹");
  input.value = "";
}

async function deleteFailedAsset(name) {
  const runningCurrent = app.running && name === app.taskCurrent;
  const answer = await openAppDialog({
    title: runningCurrent ? "删除正在运行的视频？" : "删除失败视频？",
    description: runningCurrent
      ? `“${name}”会从本批次跳过，源文件和对应成片将被删除。`
      : `“${name}”的未剪辑源文件将被删除，此操作无法撤销。`,
    confirmLabel: "删除视频",
  });
  if (!answer.confirmed) return;
  try {
    const response = await fetch(`/api/media?name=${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "删除失败");
    app.media = app.media.filter((item) => item !== name);
    app.results = app.results.filter((item) => item.name !== name);
    delete app.timelines[name];
    if (app.selected === name) {
      app.selected = app.media[0] || null;
      app.transcript = { ready: false, sentences: [], dirty: false, loading: false };
    }
    renderAll();
    toast(runningCurrent ? `已删除运行中视频：${name}` : `已删除失败视频：${name}`);
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderAssets() {
  const query = $("asset_search").value.trim().toLowerCase();
  const rows = app.media.filter((name) => {
    const status = itemStatus(name);
    const filterMatch = app.filter === "all"
      || (app.filter === "pending" && ["pending", "queued", "processing"].includes(status))
      || (app.filter === "done" && status === "done")
      || (app.filter === "review" && status === "review")
      || (app.filter === "failed" && status === "failed");
    return filterMatch && name.toLowerCase().includes(query);
  });
  const box = $("media_list");
  box.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = app.media.length ? "当前筛选没有素材" : "尚未导入素材";
    box.appendChild(empty);
  }
  rows.forEach((name) => {
    const status = itemStatus(name);
    const item = document.createElement("div");
    item.className = "asset-item";
    const row = document.createElement("button");
    row.type = "button";
    row.className = `asset-row${name === app.selected ? " active" : ""}`;
    row.title = name;
    const thumb = document.createElement("span");
    thumb.className = "asset-thumb";
    thumb.textContent = "VIDEO";
    const copy = document.createElement("span");
    copy.className = "asset-copy";
    const title = document.createElement("strong");
    title.textContent = name;
    const meta = document.createElement("small");
    const timeline = app.timelines[name];
    const result = resultFor(name);
    const attempts = result && Number(result.attempts || 0) > 1 ? ` · 尝试 ${result.attempts} 次` : "";
    const detail = status === "failed" && result && result.error ? ` · ${result.error}` : "";
    meta.textContent = timeline
      ? `${statusCopy(status)} · ${Number(timeline.total || 0).toFixed(1)}s${attempts}${detail}`
      : `${statusCopy(status)}${attempts}${detail}`;
    if (result && result.error) row.title = `${name}\n${result.error}`;
    copy.append(title, meta);
    const dot = document.createElement("span");
    dot.className = `asset-status ${status}`;
    row.append(thumb, copy, dot);
    if (["failed", "processing"].includes(status)) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "asset-delete-failed";
      remove.textContent = "×";
      remove.title = status === "processing" ? "删除当前运行视频" : "删除这条失败视频";
      const activate = (event) => {
        event.preventDefault();
        event.stopPropagation();
        deleteFailedAsset(name);
      };
      remove.addEventListener("click", activate);
      item.appendChild(remove);
    }
    if (status === "done" || status === "review") {
      const removeFinished = document.createElement("button");
      removeFinished.type = "button";
      removeFinished.className = "asset-delete-finished";
      removeFinished.textContent = "×";
      removeFinished.title = "删除已完成成片，素材回到待处理并换方案重新剪辑";
      const activateFinished = (event) => {
        event.preventDefault();
        event.stopPropagation();
        deleteFinishedAsset(name);
      };
      removeFinished.addEventListener("click", activateFinished);
      item.appendChild(removeFinished);
    }
    row.addEventListener("click", () => selectMedia(name));
    item.prepend(row);
    box.appendChild(item);
  });
  $("media_count").textContent = app.media.length;
  const done = app.media.filter((name) => itemStatus(name) === "done").length;
  $("queue_ready").textContent = Math.max(0, app.media.length - done);
  $("queue_done").textContent = done;
  $("btn_clear_media").disabled = app.running || app.deletingAll || app.media.length === 0;
  const failed = app.media.filter((name) => itemStatus(name) === "failed").length;
  if (!app.running) {
    $("btn_ai").textContent = failed ? "↻ Agent 重试失败视频" : "▶ 批量 Agent 剪辑";
    $("btn_ai").title = failed ? `由 Agent 重新处理 ${failed} 条失败视频` : "批量 Agent 剪辑";
    $("btn_ai_side").textContent = failed ? `↻ Agent 重试 ${failed} 条失败视频` : "▶ 批量 Agent 剪辑";
  }
  updateBatchEstimate();
}

async function deleteFinishedAsset(name) {
  const answer = await openAppDialog({
    title: "作废旧成片并立即重剪？",
    description: `“${name}”的旧成片会先在数据库中永久作废，再清理报告和缓存。原始素材保留，系统随后立即用新的剪辑代次和方案重新生成。`,
    confirmLabel: "作废并重剪",
    intent: "warning",
  });
  if (!answer.confirmed) return;
  try {
    const response = await fetch("/api/reedit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, reupload: false, remove_from_queue: false }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "删除成片失败");
    // 素材保留在队列里，只是回到未剪辑状态，等待下一次重新剪辑。
    app.results = app.results.filter((item) => item.name !== name);
    delete app.timelines[name];
    if (app.selected === name) {
      app.transcript = { ready: false, sentences: [], dirty: false, loading: false };
    }
    renderAll();
    const cleanupNote = (data.cleanup_warnings || []).length
      ? "（旧文件暂被占用，但成功状态已作废）"
      : "";
    toast((data.message || `已作废旧成片：${name}`) + cleanupNote);
    await selectMedia(name, false);
    await startCurrent();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function clearAllMedia() {
  if (app.deletingAll) return;
  if (app.running) return toast("请先停止剪辑，再一键删除", "error");
  if (!app.media.length) return;
  const answer = await openAppDialog({
    title: `一键删除全部 ${app.media.length} 条视频？`,
    description:
      "删除当前列表的原视频、对应成片、报告和缓存，并使旧剪辑方案失效。文件删除无法撤销。删除后不会自动剪辑；请重新导入素材，再手动点击右上方剪辑。",
    confirmLabel: "一键删除",
    intent: "warning",
  });
  if (!answer.confirmed) return;
  if (app.running || app.deletingAll) return;
  app.deletingAll = true;
  const names = [...app.media];
  const button = $("btn_clear_media");
  button.disabled = true;
  try {
    // Release browser file handles before Windows attempts to unlink media.
    document.querySelectorAll("video").forEach((video) => {
      video.pause();
      video.removeAttribute("src");
      video.querySelectorAll("source").forEach((source) => source.removeAttribute("src"));
      video.load();
    });
    app.view = "source";
    app.transcript = { ready: false, sentences: [], dirty: false, loading: false };
    const failures = [];
    let warnings = 0;
    for (const name of names) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        $("upload_status").textContent = `正在删除：${name}`;
        const deletion = await fetch(`/api/media?name=${encodeURIComponent(name)}`, { method: "DELETE", signal: controller.signal });
        const result = await deletion.json();
        if (!deletion.ok || result.error) throw new Error(result.error || "删除失败");
        warnings += (result.cleanup_warnings || []).length;
        app.media = app.media.filter((item) => item !== name);
        app.results = app.results.filter((item) => item.name !== name);
        delete app.timelines[name];
      } catch (error) {
        failures.push(`${name}：${error.name === "AbortError" ? "请求超时，请刷新核对删除结果" : error.message}`);
        if (error.name === "AbortError") break;
      } finally {
        clearTimeout(timeout);
      }
    }
    app.selected = app.media[0] || null;
    const deletedCount = names.filter((name) => !app.media.includes(name)).length;
    const summary = `已删除 ${deletedCount} 条原视频及对应旧剪辑结果；未启动剪辑。`;
    $("upload_status").textContent = summary
      + (failures.length ? ` 删除失败：${failures.join("；")}` : "")
      + (warnings ? ` ${warnings} 条成片或缓存清理有告警，请检查文件占用后重试。` : "");
    toast(summary, failures.length || warnings ? "error" : undefined);
  } catch (error) {
    $("upload_status").textContent = `删除失败：${error.message}`;
    toast(error.message, "error");
  } finally {
    app.deletingAll = false;
    renderAll();
  }
}

function formatDuration(seconds) {
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

function formatEstimatedFinish(startDate, endDate) {
  const sameDay = startDate.toDateString() === endDate.toDateString();
  const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  if (sameDay) {
    return `今天 ${timeFormatter.format(startDate)}–${timeFormatter.format(endDate)}`;
  }
  const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `${dateTimeFormatter.format(startDate)} 至 ${dateTimeFormatter.format(endDate)}`;
}

function updateBatchEstimate() {
  const count = app.media.filter((name) => itemStatus(name) !== "done").length;
  const type = $("product_type").value;
  const profile = productProfile();
  const targetValue = Number($("target_sec").value || profile.target);
  const target = Math.min(profile.max, Math.max(profile.min, targetValue));
  const output = $("estimated_output_duration");
  const processing = $("estimated_processing_time");
  const finish = $("estimated_finish_time");
  const topOutput = $("estimated_output_duration_top");
  const topProcessing = $("estimated_processing_time_top");
  if (!count) {
    const outputCopy = app.media.length ? "当前没有待处理素材" : "导入素材后计算";
    const processingCopy = app.media.length ? "--" : "导入素材后计算";
    output.textContent = outputCopy;
    processing.textContent = processingCopy;
    topOutput.textContent = outputCopy;
    topProcessing.textContent = processingCopy;
    finish.textContent = app.media.length ? "--" : "导入素材后计算";
    return;
  }
  const outputCopy = type === "auto"
    ? `${count} 条 · 约 ${formatDuration(count * profile.estimateMin)}–${formatDuration(count * profile.estimateMax)}`
    : `${count} 条 · 约 ${formatDuration(count * target)}`;
  const processingCopy = `${count} 条 · 约 ${formatDuration(count * 60)}–${formatDuration(count * 120)}`;
  output.textContent = outputCopy;
  processing.textContent = processingCopy;
  topOutput.textContent = outputCopy;
  topProcessing.textContent = processingCopy;
  const now = Date.now();
  finish.textContent = formatEstimatedFinish(
    new Date(now + count * 60 * 1000),
    new Date(now + count * 120 * 1000),
  );
}

async function selectMedia(name, resetView = true) {
  if ((app.transcript.dirty || app.reviewDirty) && name !== app.selected) {
    const dirtyItems = [app.transcript.dirty ? "口播文字" : "", app.reviewDirty ? "人工复核" : ""].filter(Boolean).join("和");
    const answer = await openAppDialog({
      title: "放弃未保存修改？",
      description: `当前${dirtyItems}尚未保存，切换素材会丢失这些修改。`,
      confirmLabel: "放弃并切换",
    });
    if (!answer.confirmed) return;
  }
  app.reviewDirty = false;
  app.selected = name;
  if (resetView) app.view = app.timelines[name] ? "output" : "source";
  app.transcript = { ready: false, sentences: [], dirty: false, loading: true };
  renderAll();
  await loadTranscript(name, true);
}

function renderSelected() {
  const name = app.selected;
  const timeline = currentTimeline();
  const agentOutput = name ? app.agent.outputs[name] : null;
  $("selected_name").textContent = name || "未选择素材";
  $("selected_meta").textContent = timeline
    ? `AI 成片 ${Number(timeline.total || 0).toFixed(1)}s · ${timeline.video.length} 个连续区间`
    : (name ? "原片 · 等待口播与动作分析" : "从左侧选择视频");
  const outputButton = $("view_switch").querySelector('[data-view="output"]');
  outputButton.disabled = (!timeline || !timeline.file) && !agentOutput;
  if (app.view === "output" && outputButton.disabled) app.view = "source";
  setSegment("view_switch", "view", app.view);
  $("btn_download").disabled = (!timeline || !timeline.file) && !agentOutput;
  $("btn_export").disabled = !timeline || !timeline.file;
  $("btn_ai_current").disabled = !name || app.running;
  const video = $("preview_video");
  const empty = $("preview_empty");
  const tag = $("preview_tag");
  if (!name) {
    video.pause();
    video.removeAttribute("src");
    video.hidden = true;
    empty.hidden = false;
    tag.hidden = true;
    return;
  }
  const nextSrc = app.view === "output" && agentOutput
    ? agentOutput
    : app.view === "output" && timeline && timeline.file
    ? `/file/${encodeURIComponent(timeline.file)}`
    : `/source/${encodeURIComponent(name)}`;
  const current = video.getAttribute("src") || "";
  if (current !== nextSrc) {
    video.src = nextSrc;
    video.load();
  }
  video.hidden = false;
  empty.hidden = true;
  tag.hidden = false;
  tag.textContent = app.view === "output" ? "AI 成片" : "原片";
  $("task_current").textContent = fileStem(name);
}

function setActiveDock(name) {
  app.activeDock = name === "timeline" ? "timeline" : "transcript";
  document.querySelectorAll("[data-dock]").forEach((button) => button.classList.toggle("active", button.dataset.dock === app.activeDock));
  $("dock_transcript").classList.toggle("active", app.activeDock === "transcript");
  $("dock_timeline").classList.toggle("active", app.activeDock === "timeline");
  if (app.activeDock === "timeline") renderTimeline();
}

function setTranscriptDirty(dirty) {
  app.transcript.dirty = dirty;
  $("btn_save_text").disabled = !app.selected || !app.transcript.ready || !dirty || app.running;
  const edited = app.transcript.sentences.filter((item) => item.text !== item.original_text).length;
  const deleted = app.transcript.sentences.filter((item) => !item.keep).length;
  if (!app.transcript.ready) return;
  $("transcript_summary").textContent = `${app.transcript.sentences.length} 句 · 改写 ${edited} · 删除 ${deleted}${dirty ? " · 未保存" : " · 已保存"}`;
}

function renderTranscript() {
  const list = $("transcript_list");
  list.replaceChildren();
  const selected = Boolean(app.selected);
  $("btn_transcribe").disabled = !selected || app.running;
  $("btn_restore_text").disabled = !selected || !app.transcript.ready || app.running;
  $("btn_save_text").disabled = !selected || !app.transcript.ready || !app.transcript.dirty || app.running;
  if (!selected) {
    list.innerHTML = '<div class="transcript-empty"><strong>尚未选择素材</strong><span>选择视频后可识别并编辑逐句口播。</span></div>';
    $("transcript_summary").textContent = "选择素材后读取口播";
    return;
  }
  if (app.transcript.loading) {
    list.innerHTML = '<div class="transcript-empty"><strong>正在读取口播文字</strong><span>已有缓存会立即载入，不会重复识别。</span></div>';
    $("transcript_summary").textContent = "正在读取文字…";
    return;
  }
  if (!app.transcript.ready) {
    list.innerHTML = '<div class="transcript-empty"><strong>尚未识别口播</strong><span>点击右上角“识别口播”，识别后可逐句修改与删除。</span></div>';
    $("transcript_summary").textContent = "尚未识别 · 点击识别口播";
    return;
  }
  app.transcript.sentences.forEach((sentence, index) => {
    const row = document.createElement("div");
    row.className = `transcript-row${sentence.keep ? "" : " deleted"}${sentence.dirty ? " dirty" : ""}`;
    const keepLabel = document.createElement("label");
    keepLabel.className = "transcript-keep";
    const keep = document.createElement("input");
    keep.type = "checkbox";
    keep.checked = sentence.keep;
    keep.title = "取消后不作为语义锚点；为保护完整动作，AI 不会强行在动作中段切断原声";
    keep.addEventListener("change", () => {
      sentence.keep = keep.checked;
      sentence.dirty = true;
      row.classList.toggle("deleted", !sentence.keep);
      row.classList.add("dirty");
      setTranscriptDirty(true);
    });
    keepLabel.appendChild(keep);
    const time = document.createElement("button");
    time.type = "button";
    time.className = "transcript-time";
    time.textContent = `${formatTime(sentence.start)}–${formatTime(sentence.end)}`;
    time.title = "点击跳转到原片对应位置";
    time.addEventListener("click", () => seekSource(sentence.start));
    const text = document.createElement("textarea");
    text.className = "transcript-text";
    text.rows = 1;
    text.value = sentence.text;
    text.setAttribute("aria-label", `第 ${index + 1} 句口播`);
    text.addEventListener("input", () => {
      sentence.text = text.value;
      sentence.dirty = true;
      row.classList.add("dirty");
      setTranscriptDirty(true);
    });
    row.append(keepLabel, time, text);
    list.appendChild(row);
  });
  setTranscriptDirty(app.transcript.dirty);
}

async function loadTranscript(name = app.selected, quiet = false) {
  if (!name) return;
  const token = ++app.transcriptToken;
  app.transcript.loading = true;
  renderTranscript();
  try {
    const params = new URLSearchParams({
      name,
      folder: $("folder").value.trim() || app.folder,
      output: $("output").value.trim(),
    });
    const response = await fetch(`/api/transcript?${params}`);
    const data = await response.json();
    if (token !== app.transcriptToken || name !== app.selected) return;
    if (!response.ok || data.error) throw new Error(data.error || "口播读取失败");
    app.transcript = {
      ready: Boolean(data.ready),
      sentences: (data.sentences || []).map((item) => ({ ...item, dirty: false })),
      dirty: false,
      loading: false,
    };
    renderTranscript();
    if (data.ready && !quiet) toast(`已载入 ${data.sentences.length} 句口播`);
  } catch (error) {
    if (token !== app.transcriptToken) return;
    app.transcript = { ready: false, sentences: [], dirty: false, loading: false };
    renderTranscript();
    toast(error.message, "error");
  }
}

function restoreTranscriptText() {
  if (!app.transcript.ready) return;
  app.transcript.sentences.forEach((sentence) => {
    sentence.text = sentence.original_text;
    sentence.keep = true;
    sentence.dirty = true;
  });
  app.transcript.dirty = true;
  renderTranscript();
  toast("已恢复原始识别文字，点击保存后生效");
}

async function saveTranscript(quiet = false) {
  if (!app.selected || !app.transcript.ready) throw new Error("当前素材没有可保存的口播文字");
  const response = await fetch("/api/transcript/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: app.selected,
      folder: $("folder").value.trim() || app.folder,
      output: $("output").value.trim(),
      sentences: app.transcript.sentences.map((item) => ({ text: item.text, keep: item.keep })),
    }),
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "保存失败");
  app.transcript = {
    ready: true,
    sentences: (data.sentences || []).map((item) => ({ ...item, dirty: false })),
    dirty: false,
    loading: false,
  };
  renderTranscript();
  if (!quiet) toast(`文字已保存：改写 ${data.edited_count || 0} 句，删除 ${data.deleted_count || 0} 句`);
  return data;
}

function seekSource(seconds) {
  app.view = "source";
  renderSelected();
  const video = $("preview_video");
  const seek = () => {
    video.currentTime = Math.max(0, Number(seconds) || 0);
    video.play().catch(() => {});
  };
  if (video.readyState >= 1) seek();
  else video.addEventListener("loadedmetadata", seek, { once: true });
}

function mappedTimeline(timeline) {
  let cursor = 0;
  const clips = (timeline.video || []).map((clip, index) => {
    const duration = Math.max(0, Number(clip.end || 0) - Number(clip.start || 0));
    const item = { ...clip, index, outStart: cursor, outEnd: cursor + duration, duration };
    cursor += duration;
    return item;
  });
  const semantics = [];
  (timeline.subtitle || []).forEach((sentence) => {
    clips.forEach((clip) => {
      const start = Math.max(Number(sentence.start || 0), Number(clip.start || 0));
      const end = Math.min(Number(sentence.end || 0), Number(clip.end || 0));
      if (end > start) semantics.push({ text: sentence.text || "口播", outStart: clip.outStart + start - Number(clip.start || 0), outEnd: clip.outStart + end - Number(clip.start || 0) });
    });
  });
  return { clips, semantics, total: Number(timeline.total || cursor || 0) };
}

function setLaneWidth(width) {
  ["timeline_ruler", "lane_video", "lane_semantic", "lane_audio"].forEach((id) => { $(id).style.width = `${width}px`; });
}

function addEmptyLane(lane, text) {
  const empty = document.createElement("div");
  empty.className = "timeline-empty";
  empty.textContent = text;
  lane.appendChild(empty);
}

function renderTimeline() {
  const timeline = currentTimeline();
  const ruler = $("timeline_ruler");
  const laneVideo = $("lane_video");
  const laneSemantic = $("lane_semantic");
  const laneAudio = $("lane_audio");
  [ruler, laneVideo, laneSemantic, laneAudio].forEach((lane) => lane.replaceChildren());
  if (!timeline) {
    setLaneWidth(680);
    addEmptyLane(laneVideo, "等待 AI 生成保留区间");
    addEmptyLane(laneSemantic, "等待口播语义规划");
    addEmptyLane(laneAudio, "主播原声保持 1.0x");
    $("tl_total").textContent = "尚未生成剪辑计划";
    $("tl_clip_count").textContent = "0 个保留区间";
    return;
  }
  const mapped = mappedTimeline(timeline);
  const available = Math.max(680, $("timeline_scroll").clientWidth - 72);
  const width = Math.max(available, Math.ceil(mapped.total * app.timelineScale));
  setLaneWidth(width);
  $("tl_total").textContent = `AI 成片 ${mapped.total.toFixed(1)}s`;
  $("tl_clip_count").textContent = `${mapped.clips.length} 个连续保留区间`;
  const tickStep = mapped.total > 45 ? 10 : 5;
  for (let time = 0; time <= Math.ceil(mapped.total); time += tickStep) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.style.left = `${time / Math.max(mapped.total, 1) * width}px`;
    const label = document.createElement("span");
    label.textContent = `${time}s`;
    tick.appendChild(label);
    ruler.appendChild(tick);
  }
  mapped.clips.forEach((clip) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `timeline-clip stage-${clip.index % 6}`;
    button.style.left = `${clip.outStart / mapped.total * width}px`;
    button.style.width = `${Math.max(clip.duration / mapped.total * width - 2, 3)}px`;
    button.textContent = clip.label || `片段 ${clip.index + 1}`;
    button.title = `${button.textContent} · ${clip.duration.toFixed(1)}s`;
    button.addEventListener("click", () => {
      app.view = "output";
      renderSelected();
      $("preview_video").currentTime = clip.outStart;
      $("preview_video").play().catch(() => {});
    });
    laneVideo.appendChild(button);
  });
  mapped.semantics.forEach((item) => {
    const block = document.createElement("div");
    block.className = "timeline-clip semantic-clip";
    block.style.left = `${item.outStart / mapped.total * width}px`;
    block.style.width = `${Math.max((item.outEnd - item.outStart) / mapped.total * width - 1, 3)}px`;
    block.textContent = item.text;
    laneSemantic.appendChild(block);
  });
  if (!mapped.semantics.length) addEmptyLane(laneSemantic, "未获得可映射口播");
  const audio = document.createElement("div");
  audio.className = "audio-bar";
  audio.style.width = `${width}px`;
  const wave = document.createElement("span");
  wave.className = "wave";
  for (let index = 0; index < Math.max(28, Math.floor(width / 12)); index += 1) {
    const bar = document.createElement("i");
    bar.style.height = `${7 + ((index * 13) % 15)}px`;
    wave.appendChild(bar);
  }
  audio.appendChild(wave);
  laneAudio.appendChild(audio);
  const playhead = document.createElement("span");
  playhead.className = "play-progress";
  playhead.id = "play_progress";
  laneVideo.appendChild(playhead);
}

function renderStageCoverage() {
  const timeline = currentTimeline();
  const steps = $("stage_flow").querySelectorAll(".flow-step");
  steps.forEach((step) => { step.classList.remove("covered", "disabled"); });
  if (!timeline) return;
  const coverage = timeline.agent && timeline.agent.stage_coverage;
  const modes = (timeline.action_requirements && timeline.action_requirements.modes) || {};
  const roles = ["opening_full_front", "origin_size_intro", "move_to_center_product_detail", "back_full_body_show", "return_origin_front_finish"];
  if (coverage) {
    steps.forEach((step, index) => {
      if (modes[roles[index]] === "off") step.classList.add("disabled");
      if (coverage[roles[index]] && coverage[roles[index]].covered) step.classList.add("covered");
    });
  }
}

function renderAgentDecision() {
  const timeline = currentTimeline();
  const decision = timeline && timeline.agent;
  const box = $("agent_decision");
  const stages = $("agent_stages");
  const cutPanel = $("cut_risk_panel");
  stages.replaceChildren();
  cutPanel.replaceChildren();
  if (!decision) {
    box.className = "agent-decision";
    box.innerHTML = '<div class="decision-head"><strong>等待剪辑计划</strong><span>--</span></div><small>生成后显示动作覆盖与剪点风险</small>';
    cutPanel.innerHTML = '<div class="cut-risk-empty">生成后检查硬剪点是否造成肢体闪现</div>';
    return;
  }
  const status = decision.status || "review";
  const confidence = Math.round(Number(decision.confidence || 0) * 100);
  const alignment = timeline.character_alignment_summary || {};
  const alignmentCopy = Number(alignment["已导出"] || 0)
    ? ` · 逐字动作时间轴 ${alignment["已导出"]} 字`
    : "";
  box.className = `agent-decision ${status}`;
  box.innerHTML = `<div class="decision-head"><strong>${decision.status_label || status}</strong><span>${confidence}%</span></div><small>${(decision.reasons || []).join("；") || "规划证据已写入剪辑报告"}${alignmentCopy}</small>`;
  const coverage = decision.stage_coverage || {};
  const roles = ["opening_full_front", "origin_size_intro", "move_to_center_product_detail", "back_full_body_show", "return_origin_front_finish"];
  roles.forEach((role) => {
    const item = coverage[role] || { label: role, covered: false, confidence: 0 };
    const mode = ((timeline.action_requirements || {}).modes || {})[role] || "required";
    const row = document.createElement("div");
    row.className = `agent-stage${item.covered ? " covered" : ""}${mode === "off" ? " disabled" : ""}`;
    const dot = document.createElement("i");
    const label = document.createElement("span");
    label.textContent = item.label || role;
    label.title = item.evidence || "";
    const score = document.createElement("b");
    score.textContent = mode === "off" ? "关闭" : (item.covered ? `${Math.round(Number(item.confidence || 0) * 100)}%` : "缺失");
    row.append(dot, label, score);
    stages.appendChild(row);
  });
  const qaCuts = (timeline.qa && timeline.qa["剪点检查"]) || [];
  const checks = qaCuts.length ? qaCuts : (decision.cut_assessments || []);
  if (!checks.length) {
    cutPanel.innerHTML = '<div class="cut-risk-empty">没有硬剪点或动作连续保留</div>';
    return;
  }
  checks.forEach((item, index) => {
    const poseRisk = item.risk || "low";
    const pixelRisk = item.pixel_risk || "unknown";
    const severity = pixelRisk === "block" ? "block" : (pixelRisk === "warn" ? "warn" : "low");
    const row = document.createElement("div");
    row.className = `cut-risk-row ${severity}`;
    row.innerHTML = `<span class="cut-risk-index">${item.index || index + 1}</span><span class="cut-risk-copy"><strong>${severity === "block" ? "阻断发布" : (severity === "warn" ? "需要复核" : "剪点连续")}</strong><small>${item.both_low_motion ? "双侧低动作" : "检查动作边界"} · ${item.pixel_diff == null ? "像素待检" : `像素差 ${Number(item.pixel_diff).toFixed(3)}`}</small></span><b class="cut-risk-score">姿态 ${Number(item.jump_score || 0).toFixed(2)}</b>`;
    cutPanel.appendChild(row);
  });
}

function renderQA() {
  const timeline = currentTimeline();
  const qa = timeline && timeline.qa;
  const badge = $("preview_qa");
  const summary = $("qa_summary");
  const issues = $("qa_issues");
  issues.replaceChildren();
  if (!timeline || !qa) {
    badge.className = "qa-badge neutral";
    badge.textContent = "未质检";
    summary.innerHTML = '<span class="qa-icon neutral">?</span><span><strong>等待剪辑结果</strong><small>完成后显示质量门结论</small></span>';
    return;
  }
  const warn = Number(qa["告警项"] || 0);
  const block = Number(qa["驳回项"] || 0);
  const notes = Number(qa["提示项"] || 0);
  let kind = "pass"; let title = "质量门通过"; let detail = "画幅、原声、时长与动作连续"; let icon = "✓";
  if (block) { kind = "block"; title = "质量门驳回"; detail = `${block} 项阻断问题`; icon = "×"; }
  else if (warn) { kind = "warn"; title = "建议复核"; detail = `${warn} 项质量告警`; icon = "!"; }
  else if (notes) { detail = "画幅、原声、时长与动作连续已通过"; }
  badge.className = `qa-badge ${kind}`;
  badge.textContent = title;
  summary.innerHTML = `<span class="qa-icon ${kind}">${icon}</span><span><strong>${title}</strong><small>${detail}</small></span>`;
  (qa["问题"] || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = `qa-issue${item["级别"] === "block" ? " block" : ""}`;
    row.textContent = `${item["标题"] || item["类型"]}：${item["说明"] || ""}`;
    issues.appendChild(row);
  });
}

const REVIEW_COPY = {
  approved: { label: "已认可", help: "这条成片已进入安全学习池，会提高同类剪辑规则的权重。", kind: "approved" },
  needs_review: { label: "待调整", help: "这条成片已被隔离，不会影响后续自动规划。", kind: "needs-review" },
  rejected: { label: "不采纳", help: "这条成片已被隔离，不会作为成功样本学习。", kind: "rejected" },
};

function renderReviewPanel() {
  const name = app.selected;
  const timeline = currentTimeline();
  const review = name ? app.reviews[name] || null : null;
  const form = $("review_form");
  const controls = [...form.querySelectorAll("select, textarea, button")];
  const available = Boolean(name && timeline);
  controls.forEach((control) => { control.disabled = !available || app.reviewSaving; });

  if (form.dataset.source !== (name || "")) {
    form.dataset.source = name || "";
    $("review_score").value = review && review.score ? String(review.score) : "";
    $("review_note").value = review ? review.note || "" : "";
    app.reviewDirty = false;
    $("review_error").hidden = true;
  }

  const state = $("review_state");
  if (!name) {
    state.className = "review-state neutral";
    state.textContent = "未选择";
    $("review_help").textContent = "选择已生成成片的素材后，可告诉 AI 这次结果是否符合标准。";
  } else if (!timeline) {
    state.className = "review-state neutral";
    state.textContent = "等待成片";
    $("review_help").textContent = "该素材生成成片后即可进行人工复核。";
  } else if (app.reviewSaving) {
    state.className = "review-state saving";
    state.textContent = "保存中";
    $("review_help").textContent = "正在写入后端剪辑记忆数据库…";
  } else if (app.reviewDirty) {
    state.className = "review-state dirty";
    state.textContent = "未保存";
    $("review_help").textContent = "请选择认可、待调整或不采纳，保存本次人工判断。";
  } else if (review && REVIEW_COPY[review.status]) {
    const copy = REVIEW_COPY[review.status];
    state.className = `review-state ${copy.kind}`;
    state.textContent = copy.label;
    $("review_help").textContent = copy.help;
  } else {
    state.className = "review-state neutral";
    state.textContent = "未复核";
    $("review_help").textContent = "质量门结论仍会保留；人工判断将决定这条成片是否可用于学习。";
  }
}

async function submitReview(status) {
  if (!app.selected || !currentTimeline() || app.reviewSaving) return;
  const name = app.selected;
  app.reviewSaving = true;
  $("review_error").hidden = true;
  renderReviewPanel();
  try {
    const scoreValue = $("review_score").value;
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        status,
        score: scoreValue ? Number(scoreValue) : null,
        note: $("review_note").value.trim(),
      }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "人工复核保存失败");
    app.reviews[name] = data.review;
    app.reviewDirty = false;
    toast(status === "approved" ? "已认可，成片进入安全学习池" : "复核已保存，成片不会进入学习池", "success");
    if (app.memory.summary || app.screen === "memory") loadMemory({ quiet: true });
  } catch (error) {
    const box = $("review_error");
    box.textContent = error.message;
    box.hidden = false;
    toast(error.message, "error");
  } finally {
    app.reviewSaving = false;
    renderReviewPanel();
  }
}

function memoryMetric(id, value) {
  $(id).textContent = Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "--";
}

function formatMemoryDate(value, withTime = true) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ");
  return new Intl.DateTimeFormat("zh-CN", withTime
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function runLearningState(run) {
  if (Number(run.invalidated) === 1) return { label: "旧版本已作废", kind: "held" };
  if (run.review_status === "approved") return { label: "人工认可学习", kind: "learned" };
  if (run.review_status === "needs_review" || run.review_status === "rejected") return { label: "人工隔离", kind: "held" };
  if (Number(run.ok) === 1 && Number(run.quality_pass) === 1 && Number(run.is_fallback) === 0 && run.decision_status === "pass") {
    return { label: "质量门自动学习", kind: "learned" };
  }
  return { label: Number(run.is_fallback) === 1 ? "兜底隔离" : "未进入学习", kind: "held" };
}

function renderMemoryProfiles(recommendations) {
  const container = $("memory_profiles");
  container.replaceChildren();
  const types = [
    ["single", "单品 / 小件", "≤ 30 秒"],
    ["set", "上下套装 / 两件套", "≤ 60 秒"],
    ["bulky", "皮草 / 羽绒 / 双面呢", "约 120 秒"],
  ];
  types.forEach(([key, label, baseline]) => {
    const memory = recommendations[key] || {};
    const article = document.createElement("article");
    article.className = "profile-strip";
    const heading = document.createElement("div");
    heading.className = "profile-name";
    const title = document.createElement("strong");
    title.textContent = label;
    const small = document.createElement("small");
    small.textContent = `基础标准 ${baseline}`;
    heading.append(title, small);
    const metrics = document.createElement("div");
    metrics.className = "profile-metrics";
    [
      ["学习样本", Number(memory.sample_count || 0)],
      ["人工认可", Number(memory.approved_count || 0)],
      ["目标时长", memory.target_duration == null ? "沿用基础标准" : `${Number(memory.target_duration).toFixed(1)} 秒`],
      ["常用片段", memory.preferred_clip_count == null ? "--" : `${memory.preferred_clip_count} 段`],
      ["偏好模板", memory.preferred_template || "尚未形成"],
    ].forEach(([metricLabel, metricValue]) => {
      const span = document.createElement("span");
      const caption = document.createElement("small");
      caption.textContent = metricLabel;
      const value = document.createElement("strong");
      value.textContent = String(metricValue);
      span.append(caption, value);
      metrics.appendChild(span);
    });
    article.append(heading, metrics);
    container.appendChild(article);
  });
}

function renderMemoryHistory(runs) {
  const wrap = $("memory_table_wrap");
  wrap.replaceChildren();
  $("memory_history_count").textContent = `${runs.length} 条记录`;
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "memory-empty";
    empty.textContent = "数据库已连接，尚无剪辑记录。完成一次剪辑或同步历史报告后会显示在这里。";
    wrap.appendChild(empty);
    return;
  }
  const table = document.createElement("table");
  table.className = "memory-table";
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = "最近剪辑记忆记录";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["时间", "素材", "类型", "成片", "AI 决策", "学习状态", "人工复核", "操作"].forEach((label) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  const productCopy = { single: "单品", set: "套装", bulky: "大货" };
  const reviewCopy = { approved: "已认可", needs_review: "待调整", rejected: "不采纳" };
  runs.forEach((run) => {
    const row = document.createElement("tr");
    const learning = runLearningState(run);
    const values = [
      formatMemoryDate(run.created_at),
      run.source_name || "--",
      productCopy[run.product_type] || run.product_type || "--",
      Number(run.actual_duration) > 0 ? `${Number(run.actual_duration).toFixed(1)}s` : "--",
      run.decision_status || "--",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) cell.className = "memory-source-cell";
      row.appendChild(cell);
    });
    const learningCell = document.createElement("td");
    const learningBadge = document.createElement("span");
    learningBadge.className = `memory-badge ${learning.kind}`;
    learningBadge.textContent = learning.label;
    learningCell.appendChild(learningBadge);
    row.appendChild(learningCell);
    const reviewCell = document.createElement("td");
    reviewCell.textContent = reviewCopy[run.review_status] || "未复核";
    row.appendChild(reviewCell);
    const actionCell = document.createElement("td");
    const explain = document.createElement("button");
    explain.type = "button";
    explain.className = "table-action";
    explain.textContent = "查看剪辑理由";
    explain.addEventListener("click", () => openMemoryRunDetail(run.id));
    actionCell.appendChild(explain);
    if (app.media.includes(run.source_name)) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "table-action";
      open.textContent = "在工作台打开";
      open.addEventListener("click", async () => {
        setScreen("workspace");
        await selectMedia(run.source_name);
      });
      actionCell.appendChild(open);
    }
    row.appendChild(actionCell);
    body.appendChild(row);
  });
  table.append(caption, head, body);
  wrap.appendChild(table);
}

async function openMemoryRunDetail(runId) {
  try {
    const response = await fetch(`/api/memory/run/${encodeURIComponent(runId)}`);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "无法读取剪辑理由");
    const detail = data.detail || {};
    const run = detail.run || {};
    const decision = detail.decision || {};
    const covered = (detail.content_evidence || [])
      .filter((item) => Number(item.covered) === 1)
      .map((item) => item.label || item.role)
      .filter(Boolean);
    const ranges = (detail.segments || []).map((item) =>
      `${item.position}. ${Number(item.source_start).toFixed(1)}–${Number(item.source_end).toFixed(1)} 秒 · ${item.stage || "保留片段"}`,
    );
    const latestEvent = (detail.events || [])[0];
    const lines = [
      `素材：${run.source_name || "--"}`,
      `剪辑代次：第 ${Number(run.edit_generation || 0)} 轮 · ${run.edit_strategy || "原始方案"}`,
      `规划结论：${decision.status || run.decision_status || "--"} · 模板 ${run.template || "--"} · ${Number(run.actual_duration || 0).toFixed(1)} 秒`,
      `内容依据：${covered.length ? covered.join("、") : "未记录命中证据"}`,
      `保留区间：\n${ranges.length ? ranges.join("\n") : "未记录片段区间"}`,
      `学习状态：${detail.invalidated ? "旧版本已作废，不进入学习" : runLearningState(run).label}`,
      latestEvent ? `最近操作：${latestEvent.reason || latestEvent.event_type}` : "最近操作：无主动重剪记录",
    ];
    await openAppDialog({
      title: "这条视频为什么这样剪",
      description: lines.join("\n\n"),
      confirmLabel: "关闭",
      intent: "primary",
      hideCancel: true,
    });
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderMemory() {
  const memory = app.memory;
  const status = $("memory_status");
  if (memory.loading && !memory.summary) {
    status.className = "memory-status loading";
    status.querySelector("strong").textContent = "正在连接剪辑记忆";
    status.querySelector("small").textContent = "等待后端数据库响应";
    return;
  }
  if (memory.error) {
    status.className = "memory-status error";
    status.querySelector("strong").textContent = "剪辑记忆暂时不可用";
    status.querySelector("small").textContent = memory.error;
    return;
  }
  const summary = memory.summary;
  if (!summary) return;
  status.className = `memory-status${memory.loading ? " loading" : " ready"}`;
  status.querySelector("strong").textContent = memory.loading ? "正在刷新，现有数据仍可查看" : "后端数据库已连接";
  status.querySelector("small").textContent = `SQLite v${summary.schema_version} · ${summary.database}`;
  const reviews = summary.reviews || {};
  const restricted = Number(reviews.needs_review || 0) + Number(reviews.rejected || 0);
  memoryMetric("memory_runs", summary.runs);
  memoryMetric("memory_eligible", summary.learning_eligible_runs);
  memoryMetric("memory_held", Math.max(0, Number(summary.runs || 0) - Number(summary.learning_eligible_runs || 0)));
  memoryMetric("memory_fallback", summary.fallback_runs);
  memoryMetric("memory_quality", summary.quality_pass_runs);
  memoryMetric("memory_approved", reviews.approved || 0);
  memoryMetric("memory_restricted", restricted);
  memoryMetric("memory_reedits", summary.reedit_events);
  $("memory_updated_at").textContent = memory.updatedAt ? `更新于 ${formatMemoryDate(memory.updatedAt)}` : "尚未读取";
  renderMemoryProfiles(memory.recommendations || {});
  renderMemoryHistory(memory.recentRuns || []);
}

async function loadMemory({ sync = false, quiet = false } = {}) {
  if (memoryRequestController) memoryRequestController.abort();
  const controller = new AbortController();
  memoryRequestController = controller;
  app.memory.loading = true;
  app.memory.error = null;
  renderMemory();
  $("btn_memory_refresh").disabled = true;
  $("btn_memory_sync").disabled = true;
  try {
    const response = await fetch("/api/memory?limit=30", { method: sync ? "POST" : "GET", signal: controller.signal });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "无法读取剪辑记忆");
    app.memory.summary = data.summary || {};
    app.memory.recentRuns = data.recent_runs || [];
    app.memory.recommendations = data.recommendations || {};
    app.memory.updatedAt = new Date().toISOString();
    if (data.backfill && !quiet) {
      toast(`历史报告同步完成：新增 ${data.backfill.imported || 0}，跳过 ${data.backfill.skipped || 0}`, "success");
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      app.memory.error = error.message;
      if (!quiet) toast(error.message, "error");
    }
  } finally {
    if (memoryRequestController === controller) {
      memoryRequestController = null;
      app.memory.loading = false;
      $("btn_memory_refresh").disabled = false;
      $("btn_memory_sync").disabled = false;
      renderMemory();
    }
  }
}

const KNOWLEDGE_API = ["127.0.0.1", "localhost"].includes(window.location.hostname) && window.location.port === "5000"
  ? "http://127.0.0.1:8000/api/knowledge"
  : `${window.location.origin}/api/knowledge`;
const KNOWLEDGE_CATEGORY_COPY = {
  editing_sop: "剪辑 SOP",
  body_action: "模特动作",
  spoken_content: "口播内容",
  quality_gate: "质量门",
  product_rule: "商品类型",
};

async function knowledgeRequest(path = "", options = {}) {
  const timeout = AbortSignal.timeout(15000);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  const response = await fetch(`${KNOWLEDGE_API}${path}`, { ...options, signal });
  if (response.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?next=${next}`);
    throw new Error("登录已失效，正在返回登录页");
  }
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "知识库请求失败");
  return data;
}

function setButtonBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    if (button.getAttribute("aria-busy") !== "true") button.dataset.idleLabel = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.idleLabel || button.textContent;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function setKnowledgeCheck(dotId, stateId, online, onlineCopy, offlineCopy, pendingCopy = "检测中") {
  $(dotId).classList.toggle("offline", online === false);
  $(dotId).classList.toggle("pending", online === null || online === undefined);
  $(stateId).textContent = online === null || online === undefined ? pendingCopy : online ? onlineCopy : offlineCopy;
}

function renderKnowledgeSystem(system = {}) {
  setKnowledgeCheck("knowledge_engine_dot", "knowledge_engine_state", !!system.engine_online, "SOCHEN 在线", "SOCHEN 未连接");
  setKnowledgeCheck("knowledge_asr_dot", "knowledge_asr_state", system.speech_recognition, "ASR 模型已安装", "ASR 未就绪", "ASR 检测中");
  setKnowledgeCheck("knowledge_pose_dot", "knowledge_pose_state", system.body_recognition, "姿态模型已安装", "人体识别未就绪", "人体识别检测中");
  setKnowledgeCheck("knowledge_fusion_dot", "knowledge_fusion_state", !!system.text_action_fusion, "文字 × 动作已联动", "等待引擎与规则");
  const databaseName = system.database === "postgres" ? "PostgreSQL 已连接" : "SQLite 已连接";
  setKnowledgeCheck("knowledge_db_dot", "knowledge_db_state", !!system.database_online, databaseName, "数据库未连接");
  const maximum = Number(system.maximum_output_seconds || 0);
  setKnowledgeCheck("knowledge_duration_dot", "knowledge_duration_state", maximum > 0, maximum > 0 ? `${maximum} 秒硬上限` : "--", "未读取到上限");
}

function setKnowledgeConnectivity(online, message = "") {
  const banner = $("connection_banner");
  banner.hidden = online;
  if (!online) {
    $("connection_banner_title").textContent = "知识库后端未连接";
    $("connection_banner_copy").textContent = message || "当前内容暂时保留；重新连接成功后才能保存或应用规则。";
  }
}

function formatKnowledgeDate(value) {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function renderKnowledge({ preservePosition = false } = {}) {
  const list = $("knowledge_list");
  const query = $("knowledge_search").value.trim();
  $("knowledge_search_clear").hidden = !query;
  $("knowledge_result_count").textContent = app.knowledge.loaded
    ? `匹配 ${app.knowledge.total} 条 · 全库 ${app.knowledge.allTotal} 条`
    : "等待数据";
  $("knowledge_page_state").textContent = `第 ${app.knowledge.page} / ${app.knowledge.pages} 页`;
  $("knowledge_prev").disabled = app.knowledge.loading || app.knowledge.page <= 1;
  $("knowledge_next").disabled = app.knowledge.loading || app.knowledge.page >= app.knowledge.pages;
  list.setAttribute("aria-busy", String(app.knowledge.loading));
  if (app.knowledge.loading && !app.knowledge.loaded) {
    delete list.dataset.renderSignature;
    list.innerHTML = '<div class="memory-loading"><span></span>正在读取知识库…</div>';
    return;
  }
  const items = app.knowledge.items;
  const signature = JSON.stringify([items, items.length ? null : app.knowledge.error, app.knowledge.loaded]);
  if (signature === list.dataset.renderSignature) {
    if (!preservePosition) list.scrollTop = 0;
    return;
  }
  const scrollTop = preservePosition ? list.scrollTop : 0;
  const focused = list.contains(document.activeElement) ? document.activeElement : null;
  const focusedEntry = focused?.closest(".knowledge-entry")?.dataset.entryId;
  const focusedAction = focused?.dataset.action;
  list.dataset.renderSignature = signature;
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "knowledge-empty";
    empty.textContent = app.knowledge.error ? `读取失败：${app.knowledge.error}` : app.knowledge.loaded ? "没有匹配的知识条目" : "知识库尚未载入";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const article = document.createElement("article");
    article.className = `knowledge-entry${item.enabled ? "" : " is-disabled"}`;
    article.dataset.entryId = item.id;
    const kind = document.createElement("div");
    kind.className = "knowledge-entry-kind";
    const badge = document.createElement("span");
    badge.textContent = KNOWLEDGE_CATEGORY_COPY[item.category] || item.category;
    const priority = document.createElement("small");
    priority.textContent = `P${item.priority} · ${item.rule_key ? "参与剪辑" : "参考知识"}`;
    kind.append(badge, priority);
    const copy = document.createElement("div");
    copy.className = "knowledge-entry-copy";
    const title = document.createElement("h3");
    title.textContent = item.title;
    const content = document.createElement("p");
    content.textContent = item.content;
    const footer = document.createElement("footer");
    footer.textContent = `${item.source === "company-sop" ? "公司核心标准" : "手动添加"} · ${item.enabled ? "已启用" : "已停用"} · 更新 ${formatKnowledgeDate(item.updated_at)}`;
    copy.append(title, content, footer);
    const actions = document.createElement("div");
    actions.className = "knowledge-entry-actions";
    if (app.canManageKnowledge) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.dataset.action = "toggle";
      toggle.textContent = item.enabled ? "停用" : "启用";
      toggle.addEventListener("click", () => setKnowledgeEnabled(item.id, !item.enabled, toggle));
      const edit = document.createElement("button");
      edit.type = "button";
      edit.dataset.action = "edit";
      edit.textContent = "编辑";
      edit.addEventListener("click", () => editKnowledge(item));
      actions.append(toggle, edit);
    }
    if (app.canManageKnowledge && !item.protected) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.action = "delete";
      remove.className = "delete";
      remove.textContent = "删除";
      remove.addEventListener("click", () => deleteKnowledge(item));
      actions.appendChild(remove);
    }
    article.append(kind, copy, actions);
    list.appendChild(article);
  });
  if (preservePosition && focusedEntry && focusedAction) {
    const article = Array.from(list.children).find((node) => node.dataset.entryId === focusedEntry);
    const action = article?.querySelector(`[data-action="${focusedAction}"]`);
    (action || list).focus({ preventScroll: true });
  }
  list.scrollTop = scrollTop;
}

async function loadKnowledge({ quiet = false, resetPage = false } = {}) {
  if (resetPage) app.knowledge.page = 1;
  if (knowledgeRequestController) knowledgeRequestController.abort();
  const controller = new AbortController();
  knowledgeRequestController = controller;
  app.knowledge.loading = !quiet;
  app.knowledge.error = null;
  setButtonBusy($("btn_knowledge_refresh"), true, "正在刷新…");
  renderKnowledge({ preservePosition: true });
  try {
    const params = new URLSearchParams({
      q: $("knowledge_search").value.trim(),
      category: $("knowledge_filter").value,
      enabled: $("knowledge_enabled_filter").value,
      page: String(app.knowledge.page),
      page_size: String(app.knowledge.pageSize),
    });
    const data = await knowledgeRequest(`?${params}`, { signal: controller.signal, cache: "no-store" });
    if (knowledgeRequestController !== controller || controller.signal.aborted) return;
    app.knowledge.items = data.items || [];
    app.knowledge.system = data.system || {};
    app.knowledge.contract = data.contract || {};
    app.knowledge.total = Number(data.total || 0);
    app.knowledge.allTotal = Number(data.all_total || 0);
    app.knowledge.active = Number(data.active || 0);
    app.knowledge.page = Number(data.page || 1);
    app.knowledge.pages = Number(data.pages || 1);
    app.knowledge.updatedAt = data.updated_at || null;
    app.knowledge.loaded = true;
    renderKnowledgeSystem(data.system || {});
    $("knowledge_contract_count").textContent = `${data.active || 0} 条规则启用`;
    $("knowledge_updated").textContent = `已同步 ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date())} · 自动更新`;
    $("knowledge_updated").title = `规则最近修改：${formatKnowledgeDate(data.updated_at)}`;
    setKnowledgeConnectivity(true);
  } catch (error) {
    if (error.name === "AbortError" || knowledgeRequestController !== controller) return;
    app.knowledge.error = error.message;
    renderKnowledgeSystem({});
    setKnowledgeConnectivity(false, error.message);
    if (!quiet) toast(error.message, "error");
  } finally {
    if (knowledgeRequestController === controller) {
      knowledgeRequestController = null;
      app.knowledge.loading = false;
      setButtonBusy($("btn_knowledge_refresh"), false, "");
      renderKnowledge({ preservePosition: quiet });
    }
  }
}

function resetKnowledgeForm({ focus = false } = {}) {
  app.knowledge.editingId = null;
  $("knowledge_editor_eyebrow").textContent = "NEW KNOWLEDGE";
  $("knowledge_editor_title").textContent = "添加公司标准";
  $("btn_knowledge_save").textContent = "保存到知识库";
  $("btn_knowledge_cancel_edit").hidden = true;
  $("knowledge_form_error").hidden = true;
  $("knowledge_form").reset();
  $("knowledge_priority").value = "50";
  if (focus) $("knowledge_entry_title").focus();
}

function editKnowledge(item) {
  app.knowledge.editingId = item.id;
  $("knowledge_category").value = item.category;
  $("knowledge_entry_title").value = item.title;
  $("knowledge_entry_content").value = item.content;
  $("knowledge_priority").value = String(item.priority);
  $("knowledge_editor_eyebrow").textContent = "EDIT KNOWLEDGE";
  $("knowledge_editor_title").textContent = "编辑公司标准";
  $("btn_knowledge_save").textContent = "保存修改";
  $("btn_knowledge_cancel_edit").hidden = false;
  $("knowledge_form_error").hidden = true;
  $("knowledge_entry_title").focus({ preventScroll: true });
  $("knowledge_form").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function createKnowledge(event) {
  event.preventDefault();
  if (!app.canManageKnowledge) return toast("只有管理员可以修改知识库", "warning");
  const errorBox = $("knowledge_form_error");
  const title = $("knowledge_entry_title").value.trim();
  const content = $("knowledge_entry_content").value.trim();
  if (title.length < 2 || content.length < 2) {
    errorBox.textContent = "请填写知识标题和完整规则内容。";
    errorBox.hidden = false;
    (title.length < 2 ? $("knowledge_entry_title") : $("knowledge_entry_content")).focus();
    return;
  }
  errorBox.hidden = true;
  const button = $("btn_knowledge_save");
  const editingId = app.knowledge.editingId;
  setButtonBusy(button, true, editingId ? "正在更新…" : "正在保存…");
  try {
    await knowledgeRequest(editingId ? `/${encodeURIComponent(editingId)}` : "", {
      method: editingId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        category: $("knowledge_category").value,
        title,
        content,
        priority: Number($("knowledge_priority").value || 50),
      }),
    });
    resetKnowledgeForm();
    await loadKnowledge({ resetPage: true });
    toast(editingId ? "知识规则已更新" : "已保存到 SOCHEN 知识库", "success");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    setButtonBusy(button, false, "");
    button.textContent = app.knowledge.editingId ? "保存修改" : "保存到知识库";
  }
}

async function setKnowledgeEnabled(id, enabled, button) {
  setButtonBusy(button, true, enabled ? "启用中…" : "停用中…");
  try {
    await knowledgeRequest(`/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    await loadKnowledge({ quiet: true });
    toast(enabled ? "规则已启用" : "规则已停用");
  } catch (error) { toast(error.message, "error"); }
  finally { setButtonBusy(button, false, ""); }
}

async function deleteKnowledge(item) {
  await openAppDialog({
    title: "删除知识条目",
    description: `确定永久删除“${item.title}”吗？该条规则将从数据库移除，且不能撤销。`,
    confirmLabel: "删除知识",
    confirmAction: async () => {
      await knowledgeRequest(`/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      if (app.knowledge.items.length === 1 && app.knowledge.page > 1) app.knowledge.page -= 1;
      await loadKnowledge({ quiet: true });
      toast("知识条目已永久删除", "success");
    },
  });
}

async function applyKnowledge() {
  if (!app.canManageKnowledge) return toast("只有管理员可以应用公司规则", "warning");
  const button = $("btn_knowledge_apply");
  setButtonBusy(button, true, "正在应用…");
  try {
    const data = await knowledgeRequest("/apply", { method: "POST" });
    await loadActionTemplate();
    await loadKnowledge({ quiet: true });
    toast(data.message || "知识库规则已应用", "success");
  } catch (error) { toast(error.message, "error"); }
  finally { setButtonBusy(button, false, ""); }
}

function screenFromLocation() {
  const view = new URLSearchParams(window.location.search).get("view");
  return view === "memory" || view === "knowledge" ? view : "workspace";
}

function setScreen(screen, { updateHistory = true } = {}) {
  const next = screen === "memory" || screen === "knowledge" ? screen : "workspace";
  app.screen = next;
  $("memory_page").hidden = next !== "memory";
  $("knowledge_page").hidden = next !== "knowledge";
  document.querySelector(".app-body").hidden = next !== "workspace";
  document.body.classList.toggle("memory-route", next === "memory");
  document.body.classList.toggle("knowledge-route", next === "knowledge");
  ["workspace", "memory", "knowledge"].forEach((name) => {
    const link = $(`nav_${name}`);
    const active = name === next;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.title = next === "memory" ? "AI 智能剪辑 | 剪辑记忆" : next === "knowledge" ? "AI 智能剪辑 | 知识库" : "AI 智能剪辑";
  if (updateHistory) {
    const url = new URL(window.location.href);
    if (next !== "workspace") url.searchParams.set("view", next);
    else url.searchParams.delete("view");
    window.history.pushState({ screen: next }, "", `${url.pathname}${url.search}${url.hash}`);
  }
  if (next === "memory" && !app.memory.summary && !app.memory.loading) loadMemory();
  if (next === "knowledge" && !app.knowledge.loading) loadKnowledge({ quiet: app.knowledge.loaded });
}

function renderAll() {
  renderAssets();
  renderSelected();
  renderTranscript();
  renderTimeline();
  renderStageCoverage();
  renderAgentDecision();
  renderQA();
  renderReviewPanel();
}

function setRunning(running) {
  app.running = running;
  ["btn_ai", "btn_ai_side"].forEach((id) => { $(id).disabled = running; });
  $("btn_stop").disabled = !running;
  $("btn_ai_current").disabled = running || !app.selected;
  $("btn_ai_current").title = running ? "当前批量任务正在自动重剪失败视频，请等待队列完成" : "重新剪辑当前视频";
  $("btn_transcribe").disabled = running || !app.selected;
  $("btn_save_text").disabled = running || !app.transcript.ready || !app.transcript.dirty;
  $("btn_clear_media").disabled = running || app.deletingAll || app.media.length === 0;
  const state = $("run_state");
  state.className = `run-state ${running ? "running" : "idle"}`;
  state.innerHTML = `<i></i> ${running ? "运行中" : "空闲"}`;
  $("status").textContent = running ? "AI 正在处理" : "等待任务";
  if (running) {
    $("btn_ai").textContent = "Agent 运行中";
    $("btn_ai_side").textContent = "Agent 正在分析与执行";
  }
}

const PHASE_ORDER = ["analyze", "plan", "render", "qa"];
const AGENT_STATES = ["RECEIVED", "ANALYZING", "PLANNING", "VALIDATING", "RENDERING", "EVALUATING"];

function renderAgentRuntime(state = "RECEIVED", terminal = "") {
  const current = AGENT_STATES.indexOf(state);
  $("agent_runtime_trace").querySelectorAll("span").forEach((node, index) => {
    node.classList.toggle("active", !terminal && index === current);
    node.classList.toggle("done", terminal === "PASSED" || index < current);
    node.classList.toggle("failed", terminal === "NEEDS_REVIEW" && index === Math.max(0, current));
  });
}

function renderAgentTrace(job, trace) {
  const events = trace.events || [];
  const run = trace.run || {};
  const state = run.current_state || events.at(-1)?.state || (job.status === "queued" ? "RECEIVED" : "ANALYZING");
  renderAgentRuntime(state, ["PASSED", "NEEDS_REVIEW"].includes(state) ? state : "");
  const decision = $("agent_decision");
  const plans = trace.plans || [];
  const qualities = trace.quality || [];
  const latestPlan = plans.at(-1)?.plan || {};
  const latestQuality = qualities.at(-1)?.quality || {};
  const stageCount = Array.isArray(latestPlan.stages) ? latestPlan.stages.length : 0;
  decision.replaceChildren();
  const head = document.createElement("div");
  head.className = "decision-head";
  const title = document.createElement("strong");
  title.textContent = state === "PASSED" ? "Agent 质量门通过" : state === "NEEDS_REVIEW" ? "Agent 已交人工复核" : `Agent 正在 ${state}`;
  const badge = document.createElement("span");
  badge.textContent = `计划 v${plans.length || 0}`;
  head.append(title, badge);
  const note = document.createElement("small");
  const warnings = latestPlan.warnings || job.result?.agent?.warnings || [];
  note.textContent = warnings.length
    ? warnings.join("；")
    : `${stageCount || "尚未生成"} 个证据阶段 · ${qualities.length} 轮质检 · 最多自动修正 2 轮`;
  decision.append(head, note);
  decision.className = `agent-decision ${state === "PASSED" ? "success" : state === "NEEDS_REVIEW" ? "warning" : ""}`;
  const issues = latestQuality.issues || [];
  $("qa_summary").querySelector("strong").textContent = state === "PASSED" ? "全部硬质量门通过" : state === "NEEDS_REVIEW" ? "需要负责人复核" : "Agent 正在执行质量闭环";
  $("qa_summary").querySelector("small").textContent = issues.length ? issues.map((item) => item.message || item.code).join("；") : `已记录 ${events.length} 个状态事件`;
}

async function pollAgentJobs() {
  if (!app.agent.active || document.hidden) return;
  try {
    const jobs = await Promise.all(app.agent.jobs.map((item) => readApiJSON(`/api/jobs/${item.id}`)));
    app.agent.jobs = jobs;
    const completed = jobs.filter((item) => ["approved", "needs_review", "failed"].includes(item.status)).length;
    const current = jobs.find((item) => ["queued", "running"].includes(item.status)) || jobs.at(-1);
    if (current) {
      $("task_current").textContent = fileStem(current.source_name);
      const phase = { queued: "analyze", analyzing: "analyze", planning: "plan", rendering: "render", quality: "qa", completed: "qa", failed: "qa" }[current.stage] || "analyze";
      setTaskPhase(phase, `真实 Agent · ${current.stage || current.status}`);
      const trace = await readApiJSON(`/api/jobs/${current.id}/agent-trace`);
      app.agent.traces[current.id] = trace;
      renderAgentTrace(current, trace);
    }
    jobs.forEach((item) => { if (item.media_url) app.agent.outputs[item.source_name] = item.media_url; });
    setProgress(completed, jobs.length);
    renderSelected();
    if (completed < jobs.length) {
      window.setTimeout(pollAgentJobs, 3000);
      return;
    }
    app.agent.active = false;
    setRunning(false);
    const passed = jobs.filter((item) => item.status === "approved").length;
    const review = jobs.filter((item) => item.status === "needs_review").length;
    const failed = jobs.length - passed - review;
    setTaskPhase(review || failed ? "partial" : "complete", `Agent 完成：通过 ${passed} · 待复核 ${review} · 失败 ${failed}`);
    $("results_line").textContent = `Agent 已完成 ${jobs.length} 条：质量门通过 ${passed}，人工复核 ${review}，失败 ${failed}`;
    if (app.selected && app.agent.outputs[app.selected]) app.view = "output";
    renderSelected();
    toast(review || failed ? "Agent 已停止在需要人工处理的环节" : "Agent 剪辑与质检已完成", review || failed ? "error" : "info");
  } catch (error) {
    app.agent.active = false;
    setRunning(false);
    setTaskPhase("partial", error.message);
    toast(`Agent 状态读取失败：${error.message}`, "error");
  }
}

async function restoreAgentSession() {
  const listing = await readApiJSON("/api/jobs?page=1&page_size=50");
  const jobs = listing.items || [];
  jobs.forEach((item) => { if (item.media_url) app.agent.outputs[item.source_name] = item.media_url; });
  const active = [];
  for (const item of jobs.filter((job) => ["queued", "running"].includes(job.status))) {
    const trace = await readApiJSON(`/api/jobs/${item.id}/agent-trace`);
    if (trace.run) active.push(item);
  }
  if (!active.length) {
    renderSelected();
    return;
  }
  app.agent.jobs = active;
  app.agent.active = true;
  setRunning(true);
  $("btn_stop").disabled = true;
  $("btn_stop").title = "Agent 会在当前有限步骤结束后自行完成或进入人工复核";
  pollAgentJobs();
}

async function createAgentJobs(names) {
  if (!names.length) throw new Error("当前没有需要启动的素材");
  const type = $("product_type").value;
  const target = type === "auto" ? null : Number($("target_sec").value);
  const sources = names.map((name) => ({ name, origin: "legacy" }));
  const response = await fetch(names.length === 1 ? "/api/agent/jobs" : "/api/agent/jobs/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(names.length === 1
      ? { source_name: names[0], source_origin: "legacy", product_type: type, target_duration: target }
      : { sources, product_type: type, target_duration: target }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || "Agent 任务创建失败");
  const jobs = names.length === 1 ? [data] : (data.items || []);
  if (!jobs.length) throw new Error((data.errors || []).map((item) => `${item.name}：${item.reason}`).join("；") || "没有可启动的素材");
  app.agent.jobs = jobs;
  app.agent.active = true;
  return { jobs, errors: data.errors || [] };
}
function setTaskPhase(phase, message = "") {
  app.phase = phase || "idle";
  app.phaseMessage = message || app.phaseMessage;
  const copy = { idle: "待机", analyze: "动作与口播识别", plan: "AI 剪辑规划", render: "1080P 高清渲染", qa: "无闪现质检", complete: "任务完成", partial: "有未完成", stopping: "安全停止中" };
  $("task_phase").textContent = copy[app.phase] || app.phase;
  $("task_message").textContent = app.phaseMessage;
  $("task_result").textContent = copy[app.phase] || "--";
  const live = $("task_live");
  live.className = `task-live${app.phase === "complete" ? " done" : (app.running ? " running" : "")}`;
  const current = PHASE_ORDER.indexOf(app.phase);
  $("phase_pipeline").querySelectorAll("span").forEach((item, index) => {
    item.classList.toggle("active", index === current);
    item.classList.toggle("done", app.phase === "complete" || (current >= 0 && index < current));
  });
  $("phase_pipeline").querySelectorAll("i").forEach((item, index) => item.classList.toggle("done", app.phase === "complete" || (current >= 0 && index < current)));
}

function setProgress(done, total) {
  const percent = total > 0 ? Math.round(done / total * 100) : 0;
  $("progress_bar").style.width = `${percent}%`;
  $("pct").textContent = `${percent}%`;
  $("task_counter").textContent = `${done} / ${total}`;
  if (app.running) $("status").textContent = `处理中 ${Math.min(done + 1, total)} / ${total}`;
  updateBatchEstimate();
}

async function refreshTimelines() {
  const data = await readApiJSON("/api/timelines");
  app.timelines = data.timelines || {};
  app.reviews = data.reviews || {};
  renderAssets();
  renderSelected();
  renderTimeline();
  renderStageCoverage();
  renderAgentDecision();
  renderQA();
  renderReviewPanel();
}

async function pollOnce() {
  const status = await readApiJSON("/api/status");
  app.results = status.results || [];
  app.taskCurrent = status.current || null;
  if (status.current) $("task_current").textContent = fileStem(status.current);
  setTaskPhase(status.phase || (status.running ? "analyze" : app.phase), status.phase_message || app.phaseMessage);
  setProgress(status.done || 0, status.total || 0);
  await refreshTimelines();
  return status;
}

async function poll() {
  if (pollInFlight) return;
  clearTimeout(pollTimer);
  pollInFlight = true;
  let status;
  try {
    status = await pollOnce();
    pollFailures = 0;
  } catch (error) {
    setKnowledgeConnectivity(false, error.message);
    $("connection_banner_title").textContent = "剪辑服务连接中断";
    pollFailures += 1;
    if (pollFailures <= 6) pollTimer = setTimeout(poll, Math.min(15000, 1000 * 2 ** pollFailures));
    return;
  } finally { pollInFlight = false; }
  if (!status.running && app.running) {
    setRunning(false);
    const state = $("run_state");
    if (app.selected) {
      await loadTranscript(app.selected, true);
      if (app.timelines[app.selected]) app.view = "output";
      renderAll();
    }
    const ok = app.results.filter((item) => item.status === "成功").length;
    const failed = Math.max(0, app.results.length - ok);
    state.className = `run-state ${failed ? "error" : "done"}`;
    state.innerHTML = failed ? "<i></i> 有未完成" : "<i></i> 已完成";
    setTaskPhase(failed ? "partial" : "complete", status.phase_message || (failed ? "部分视频未通过质量门" : "任务已完成"));
    if (app.taskKind === "transcript") {
      $("results_line").textContent = "口播文字识别完成，可逐句修改后重新剪辑";
      toast("口播文字已就绪");
    } else {
      $("results_line").textContent = failed
        ? `仍有 ${failed} 条未通过严格质量门：成功 ${ok} / 共 ${app.results.length}，可重新智能剪辑`
        : `全部视频制作完成：成功 ${ok} / 共 ${app.results.length}`;
      toast(failed
        ? "部分视频未通过质量门，请查看失败原因"
        : (app.taskKind === "current" ? "当前视频 AI 剪辑已完成" : "批量 AI 剪辑已完成"), failed ? "error" : "info");
    }
    return;
  }
  if (app.running) pollTimer = setTimeout(poll, 1200);
}

async function readApiJSON(path) {
  let response;
  try {
    response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(10000) });
  } catch (_) {
    throw new Error("连接中断或请求超时，请确认后台电脑和网络在线后重新连接");
  }
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    throw new Error("登录已失效，请重新登录");
  }
  if (!response.ok) throw new Error(`服务暂不可用（${response.status}），可重新连接`);
  return response.json();
}

async function reconnectWorkspace() {
  if (reconnectInFlight) return;
  reconnectInFlight = true;
  const button = $("connection_retry");
  setButtonBusy(button, true, "连接中…");
  try {
    const status = await pollOnce();
    setRunning(Boolean(status.running));
    if (!app.actionTemplateDirty) await loadActionTemplate();
    await loadHealth();
    if (app.screen === "knowledge") await loadKnowledge({ quiet: true });
    else setKnowledgeConnectivity(true);
    pollFailures = 0;
    connectLogs();
    if (app.running) poll();
  } catch (error) {
    setKnowledgeConnectivity(false, error.message);
    $("connection_banner_title").textContent = "服务尚未恢复";
  } finally {
    reconnectInFlight = false;
    setButtonBusy(button, false, "");
  }
}

async function startBatch() {
  if (app.deletingAll) return toast("正在删除视频，请等待完成", "error");
  if (!app.folder || !app.media.length) return toast("请先导入素材目录", "error");
  app.taskKind = "batch";
  $("log").textContent = "";
  setRunning(true);
  setTaskPhase("analyze", "正在创建批量 Agent 任务并读取真实证据");
  setProgress(0, app.media.length);
  try {
    const result = await createAgentJobs(app.media.filter((name) => itemStatus(name) !== "done"));
    $("btn_stop").disabled = true;
    $("btn_stop").title = "Agent 会在当前有限步骤结束后自行完成或进入人工复核";
    toast(`已启动 ${result.jobs.length} 个真实 Agent 任务${result.errors.length ? `，跳过 ${result.errors.length} 条` : ""}`);
    pollAgentJobs();
  } catch (error) {
    setRunning(false);
    toast(`启动失败：${error.message}`, "error");
  }
}

async function startCurrent() {
  if (app.deletingAll) return toast("正在删除视频，请等待完成", "error");
  if (!app.selected) return toast("请先选择一条素材", "error");
  try {
    if (app.transcript.dirty) await saveTranscript(true);
  } catch (error) {
    return toast(error.message, "error");
  }
  app.taskKind = "current";
  const body = { name: app.selected, ...editorSettings(false) };
  setRunning(true);
  setTaskPhase("analyze", "正在由 Agent 分析口播、人物动作与画面证据");
  setProgress(0, 1);
  try {
    await createAgentJobs([body.name]);
    $("btn_stop").disabled = true;
    $("btn_stop").title = "Agent 会在当前有限步骤结束后自行完成或进入人工复核";
    toast("当前视频 Agent 已启动");
    pollAgentJobs();
  } catch (error) {
    setRunning(false);
    toast(error.message || "当前视频 Agent 启动失败", "error");
  }
}

async function transcribeCurrent() {
  if (!app.selected) return;
  app.taskKind = "transcript";
  const body = { name: app.selected, ...editorSettings(false) };
  setRunning(true);
  setTaskPhase("analyze", "正在识别当前素材口播，完成后可逐句修改");
  setProgress(0, 1);
  const response = await fetch("/api/transcript/transcribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    setRunning(false);
    return toast(data.error || "口播识别启动失败", "error");
  }
  toast("口播识别已启动");
  poll();
}

async function exportJianying() {
  if (!app.selected || !currentTimeline()) return;
  const button = $("btn_export");
  button.disabled = true;
  const response = await fetch("/api/export_jianying", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: app.selected }),
  });
  const data = await response.json();
  button.disabled = false;
  if (data.status === "SUCCESS") {
    $("results_line").textContent = `剪映草稿已导出：${data.draft_name}`;
    toast("剪映草稿导出完成");
  } else toast(data.msg || "导出失败", "error");
}

function clearKnowledgeSearch() {
  $("knowledge_search").value = "";
  clearTimeout(knowledgeSearchTimer);
  $("knowledge_search_clear").hidden = true;
  loadKnowledge({ resetPage: true });
  $("knowledge_search").focus();
}

function changeKnowledgePage(delta) {
  const target = Math.min(app.knowledge.pages, Math.max(1, app.knowledge.page + Number(delta || 0)));
  if (target === app.knowledge.page) return;
  app.knowledge.page = target;
  loadKnowledge();
}

function bindEvents() {
  const returnAuthForm = $("return_auth_form");
  if (returnAuthForm) returnAuthForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const unsaved = app.reviewDirty || app.actionTemplateDirty || app.knowledge.editingId
      || $("knowledge_entry_title").value.trim() || $("knowledge_entry_content").value.trim();
    if (unsaved) {
      const proceed = await openAppDialog({
        title: "返回登录 / 注册", intent: "warning", confirmLabel: "放弃修改并返回",
        cancelLabel: "继续编辑", description: "尚有未保存的修改，返回账号界面后将丢失。已保存的知识库、视频和后台剪辑任务不受影响。",
      });
      if (!proceed.confirmed) return;
    }
    setButtonBusy(returnAuthForm.querySelector("button"), true, "正在返回…");
    HTMLFormElement.prototype.submit.call(returnAuthForm);
  });
  $("knowledge_form").addEventListener("submit", createKnowledge);
  $("btn_knowledge_apply").addEventListener("click", applyKnowledge);
  $("knowledge_filter").addEventListener("change", () => loadKnowledge({ resetPage: true }));
  $("knowledge_enabled_filter").addEventListener("change", () => loadKnowledge({ resetPage: true }));
  let knowledgeSearchComposing = false;
  const scheduleKnowledgeSearch = () => {
    $("knowledge_search_clear").hidden = !$("knowledge_search").value;
    if (knowledgeSearchComposing) return;
    clearTimeout(knowledgeSearchTimer);
    knowledgeSearchTimer = setTimeout(() => loadKnowledge({ resetPage: true }), 300);
  };
  $("knowledge_search").addEventListener("compositionstart", () => { knowledgeSearchComposing = true; });
  $("knowledge_search").addEventListener("compositionend", () => { knowledgeSearchComposing = false; scheduleKnowledgeSearch(); });
  $("knowledge_search").addEventListener("input", scheduleKnowledgeSearch);
  const folderInput = $("folder");
  if (folderInput && folderInput.type !== "hidden") {
    folderInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadMedia(folderInput.value);
    });
  }
  $("output").addEventListener("change", () => selectOutput($("output").value));
  $("product_type").addEventListener("change", (event) => applyProductType(event.target.value));
  $("target_sec").addEventListener("input", updateBatchEstimate);
  $("target_sec").addEventListener("change", rememberProductTarget);
  $("upload_files").addEventListener("change", uploadVideos);
  $("upload_folder").addEventListener("change", uploadFolderVideos);
  $("btn_clear_media").addEventListener("click", clearAllMedia);
  $("asset_search").addEventListener("input", () => {
    $("asset_search_clear").hidden = !$("asset_search").value;
    renderAssets();
  });
  document.querySelectorAll("[data-action-role]").forEach((select) => select.addEventListener("change", () => {
    select.closest(".action-rule-row").classList.toggle("disabled", select.value === "off");
    localStorage.setItem("suchen.actionRequirements", JSON.stringify(actionRequirementsFromUI()));
    app.actionTemplateDirty = true;
    updateActionTemplateState("未保存修改");
  }));
  ["opening_max_sec", "approach_preroll_sec", "back_min_duration_sec", "return_stable_sec"].forEach((id) => $(id).addEventListener("change", () => {
    localStorage.setItem("suchen.actionRequirements", JSON.stringify(actionRequirementsFromUI()));
    app.actionTemplateDirty = true;
    updateActionTemplateState("未保存修改");
  }));
  $("action_template_preset").addEventListener("change", (event) => chooseActionPreset(event.target.value));
  $("btn_ai_current").addEventListener("click", startCurrent);
  $("btn_transcribe").addEventListener("click", transcribeCurrent);
  $("btn_restore_text").addEventListener("click", restoreTranscriptText);
  $("btn_save_text").addEventListener("click", () => saveTranscript().catch((error) => toast(error.message, "error")));
  $("btn_stop").addEventListener("click", async () => { await fetch("/api/stop", { method: "POST" }); setTaskPhase("stopping", "当前素材完成后安全停止"); toast("已提交停止请求"); });
  $("btn_export").addEventListener("click", exportJianying);
  $("btn_download").addEventListener("click", () => {
    const agentOutput = app.selected && app.agent.outputs[app.selected];
    if (agentOutput) {
      const link = document.createElement("a");
      link.href = agentOutput;
      link.download = `${fileStem(app.selected)}_Agent成片.mp4`;
      link.click();
      return;
    }
    const timeline = currentTimeline();
    if (!timeline || !timeline.file) return;
    const link = document.createElement("a");
    link.href = `/file/${encodeURIComponent(timeline.file)}`;
    link.download = timeline.file;
    link.click();
  });
  $("preview_video").addEventListener("timeupdate", () => {
    const video = $("preview_video");
    $("source_time").textContent = `${formatTime(video.currentTime)} / ${formatTime(video.duration)}`;
    const playhead = $("play_progress");
    if (playhead && currentTimeline() && app.view === "output" && video.duration) playhead.style.left = `${video.currentTime / video.duration * 100}%`;
  });
  $("preview_video").addEventListener("loadedmetadata", () => {
    const video = $("preview_video");
    $("source_time").textContent = `${formatTime(video.currentTime)} / ${formatTime(video.duration)}`;
  });
  $("review_score").addEventListener("change", () => { app.reviewDirty = true; renderReviewPanel(); });
  $("review_note").addEventListener("input", () => { app.reviewDirty = true; renderReviewPanel(); });
  $("review_form").addEventListener("submit", (event) => {
    event.preventDefault();
    const status = event.submitter && event.submitter.dataset.reviewStatus;
    if (status) submitReview(status);
  });
  document.querySelectorAll("[data-screen]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    setScreen(link.dataset.screen);
  }));
  document.querySelector(".brand").addEventListener("click", (event) => {
    event.preventDefault();
    setScreen("workspace");
  });
  window.addEventListener("popstate", () => setScreen(screenFromLocation(), { updateHistory: false }));
  document.querySelectorAll("[data-dialog-cancel]").forEach((item) => item.addEventListener("click", () => closeAppDialog(false)));
  $("app_dialog_input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitAppDialog();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) reconnectWorkspace();
  });
  window.addEventListener("online", reconnectWorkspace);
  window.addEventListener("beforeunload", (event) => {
    if (!app.transcript.dirty && !app.reviewDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function connectLogs() {
  if (logStream && logStream.readyState !== EventSource.CLOSED) return;
  const stream = new EventSource("/stream");
  logStream = stream;
  stream.onmessage = (event) => appendLog(event.data);
  stream.onerror = () => {};
}

async function loadHealth() {
  try {
    const healthApi = ["127.0.0.1", "localhost"].includes(window.location.hostname) && window.location.port === "5000"
      ? "http://127.0.0.1:8000/api/health"
      : `${window.location.origin}/api/health`;
    const data = await (await fetch(healthApi, { cache: "no-store", signal: AbortSignal.timeout(10000) })).json();
    const health = $("agent_health");
    health.className = `agent-health ${data.ready && !data.component_check_pending ? "ready" : "degraded"}`;
    const checks = data.checks || {};
    const cloud = String(data.mode || "").includes("云端");
    health.querySelector("strong").textContent = data.component_check_pending
      ? "原引擎在线，组件检测中"
      : data.ready
      ? (cloud ? "云端模型已就绪" : "本地模型已就绪")
      : "部分能力降级";
    health.querySelector("small").textContent = `${data.mode} · ${Object.values(checks).filter(Boolean).length}/${Object.keys(checks).length} 组件 · ${data.output}`;
    $("agent_version").textContent = `${String(data.agent_version || "v2").toUpperCase()} · ${cloud ? "云端" : "本地"}`;
    const worker = $("worker_health");
    worker.className = `worker-state${data.ready && !data.component_check_pending ? "" : " degraded"}`;
    worker.innerHTML = `<i></i> ${data.component_check_pending ? "AGENT CHECKING" : data.ready ? "AGENT READY" : "AGENT DEGRADED"}`;
    clearTimeout(healthRetryTimer);
    if (data.component_check_pending) healthRetryTimer = setTimeout(loadHealth, 5000);
  } catch (_) {
    $("agent_health").className = "agent-health degraded";
    $("agent_health").querySelector("strong").textContent = "无法读取模型状态";
    clearTimeout(healthRetryTimer);
    healthRetryTimer = setTimeout(loadHealth, 5000);
  }
}

async function initialize() {
  restoreActionRequirements();
  applyProductType(localStorage.getItem("suchen.productType") || "auto", false);
  bindEvents();
  setScreen(screenFromLocation(), { updateHistory: false });
  await loadActionTemplate();
  connectLogs();
  setActiveDock("transcript");
  renderAll();
  await loadHealth();
  try {
    const status = await readApiJSON("/api/status");
    app.results = status.results || [];
    app.taskCurrent = status.current || null;
    setRunning(Boolean(status.running));
    setTaskPhase(status.phase || (status.running ? "analyze" : "idle"), status.phase_message || app.phaseMessage);
    setProgress(status.done || 0, status.total || 0);
    await refreshTimelines();
    if (status.running) poll();
  } catch (_) {
    setKnowledgeConnectivity(false, "后台可能正在恢复，请点击重新连接。当前文字不会清空。");
    $("connection_banner_title").textContent = "剪辑服务连接中断";
  }
  if ($("folder").value.trim()) await loadMedia($("folder").value.trim()).catch(() => {});
  else await restoreManagedUploads().catch(() => {});
  await selectOutput($("output").value.trim(), true).catch(() => {});
  await restoreAgentSession().catch(() => {});
  clearInterval(knowledgeHealthTimer);
  knowledgeHealthTimer = setInterval(() => {
    if (!document.hidden && app.screen === "knowledge" && !app.knowledge.loading) loadKnowledge({ quiet: true });
  }, 15000);
}

initialize();
