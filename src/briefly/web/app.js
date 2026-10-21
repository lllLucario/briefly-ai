// Briefly AI 网页：对话、待办、材料、周报。原生 JS，直接调用同源的 FastAPI 接口。
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  threadId: null, // 当前会话；null 表示新会话，发送第一句后由后端分配
  picked: new Set(), // 对话中选用的材料编号
  materials: [],
  skills: [],
  activeMaterial: null,
  feishu: null, // 飞书配置状态（/feishu/status）
  mail: null, // 邮件配置状态（/mail/status）
  lastRequest: null,
  viewRevision: 0,
  drafts: new Map(), // 未发送输入按会话保存，不带到其他会话
};

// ---------- 通用 ----------
async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    delete opts.json;
  }
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    let body = null;
    try {
      body = await resp.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* 非 JSON 错误 */ }
    const err = new Error(detail);
    err.status = resp.status;
    err.needsConfirm = resp.status === 409 && Boolean(body?.confirm); // 需要用户确认后带 confirm / force 重试
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 名称仅由界面代码指定，复用本地 sprite；装饰图标不重复朗读按钮文字。
function uiIcon(name) {
  return `<svg class="icon action-icon" aria-hidden="true"><use href="#i-${escapeHtml(name)}"/></svg>`;
}

// 仅在当前页面记住对象各自的展开状态，不跨周/任务复用。
const disclosureState = new Map();
function disclosureOpen(key, initial = false) {
  return (disclosureState.has(key) ? disclosureState.get(key) : initial) ? "open" : "";
}
document.addEventListener("toggle", (e) => {
  if (e.target.isConnected && e.target.matches?.("details[data-disclosure]")) {
    disclosureState.set(e.target.dataset.disclosure, e.target.open);
  }
}, true);

// Markdown 渲染；CDN 不可用时退回纯文本
function renderMarkdown(text) {
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(text, { breaks: true }));
  }
  return `<div style="white-space:pre-wrap">${escapeHtml(text)}</div>`;
}

// 文件类型图标（内联 SVG）：本地文件按扩展名显示带颜色的类型标签，文件夹、飞书文档、多维表格、妙记各有图形
const FILE_LABELS = {
  pdf: ["PDF", "#e5484d"], docx: ["DOC", "#2f6fed"], doc: ["DOC", "#2f6fed"], xlsx: ["XLS", "#1f9d55"], xls: ["XLS", "#1f9d55"],
  csv: ["CSV", "#1f9d55"], md: ["MD", "#6b7280"], txt: ["TXT", "#6b7280"],
};
const KIND_NAMES = {
  folder: "文件夹", pdf: "PDF", docx: "Word", doc: "Word", xlsx: "Excel", xls: "Excel", csv: "CSV", md: "Markdown", txt: "文本",
  feishu: "飞书文档", bitable: "多维表格", minutes: "妙记", wiki: "知识库", sheet: "电子表格", paste: "粘贴",
};

function fileIcon(kind) {
  const k = String(kind || "").toLowerCase();
  const svg = (body) => `<svg class="ficon" viewBox="0 0 20 20" aria-hidden="true">${body}</svg>`;
  const page = (stroke, fill = "#fff") =>
    `<path d="M5.5 2h6.5l4 4v11a1 1 0 0 1-1 1h-9.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z" fill="${fill}" stroke="${stroke}" stroke-width="1.2"/>`
    + `<path d="M12 2v4h4" fill="none" stroke="${stroke}" stroke-width="1.2"/>`;
  if (k === "folder") {
    return svg('<path d="M2 5.2A1.2 1.2 0 0 1 3.2 4h4.1l1.7 1.8h7.8A1.2 1.2 0 0 1 18 7v8.8a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 15.8z" fill="#f2b33d"/>'
      + '<path d="M2 8.2h16v7.6a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 15.8z" fill="#ffcd5e"/>');
  }
  if (k === "feishu" || k === "docx-feishu") {
    return svg(page("#3370ff", "#eaf0ff") + '<path d="M7 9.5h6M7 12h6M7 14.5h4" stroke="#3370ff" stroke-width="1.2" stroke-linecap="round"/>');
  }
  if (k === "bitable" || k === "sheet") {
    const c = k === "bitable" ? "#7a55e6" : "#1f9d55";
    return svg(`<rect x="3" y="3.5" width="14" height="13" rx="1.5" fill="#fff" stroke="${c}" stroke-width="1.2"/>`
      + `<path d="M3 7.8h14M3 12.2h14M8 3.5v13" stroke="${c}" stroke-width="1.1"/>`);
  }
  if (k === "minutes") {
    return svg('<rect x="7.5" y="2.5" width="5" height="9" rx="2.5" fill="#f0e9ff" stroke="#8b5cf6" stroke-width="1.2"/>'
      + '<path d="M5 9.5a5 5 0 0 0 10 0M10 14.5V17M7.5 17h5" fill="none" stroke="#8b5cf6" stroke-width="1.2" stroke-linecap="round"/>');
  }
  if (k === "wiki") {
    return svg('<path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H16v12H5.5A1.5 1.5 0 0 0 4 16.5z" fill="#eaf0ff" stroke="#3370ff" stroke-width="1.2"/>'
      + '<path d="M4 16.5A1.5 1.5 0 0 1 5.5 15H16v2H5.5A1.5 1.5 0 0 1 4 16.5z" fill="#3370ff"/>');
  }
  const [label, color] = FILE_LABELS[k] || ["", "#9aa1ab"];
  return svg(page(color) + (label
    ? `<rect x="2" y="10.5" width="13" height="6" rx="1" fill="${color}"/>`
      + `<text x="8.5" y="15.1" text-anchor="middle" font-size="4.6" font-weight="700" fill="#fff" font-family="Arial, sans-serif">${label}</text>`
    : '<path d="M7 9.5h6M7 12h6M7 14.5h4" stroke="#9aa1ab" stroke-width="1.2" stroke-linecap="round"/>'));
}

function kindName(kind) {
  return KIND_NAMES[String(kind || "").toLowerCase()] || kind || "";
}

function fmtTime(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ---------- 通用弹窗（代替浏览器自带的 alert / confirm / prompt，风格与页面一致）
// 同一时间只有一个；回车确认、Esc 或“取消”关闭。返回 Promise。
let uiResolve = null;

function uiOpen({ title = "提示", message = "", fields = [], ok = "确定", cancel = "取消", danger = false }) {
  if (uiResolve) uiResolve(null); // 上一个还开着：按取消处理
  $("#ui-title").textContent = title;
  $("#ui-message").innerHTML = message;
  $("#ui-message").classList.toggle("hidden", !message);
  $("#ui-fields").innerHTML = fields.map((f, i) => `<label class="stack ui-field">
      <span>${escapeHtml(f.label || "")}${f.required ? ' <span class="danger">*</span>' : ""}</span>
      ${f.type === "select"
        ? `<select data-ui="${i}">${(f.options || []).map((o) => `<option value="${escapeHtml(String(o.value))}" ${String(o.value) === String(f.value ?? "") ? "selected" : ""}>${escapeHtml(o.label)}</option>`).join("")}</select>`
        : f.type === "textarea"
        ? `<textarea data-ui="${i}" rows="${f.rows || 3}" placeholder="${escapeHtml(f.placeholder || "")}">${escapeHtml(f.value || "")}</textarea>`
        : `<input data-ui="${i}" type="${f.type || "text"}" value="${escapeHtml(f.value || "")}" placeholder="${escapeHtml(f.placeholder || "")}">`}
      ${f.hint ? `<small class="muted">${escapeHtml(f.hint)}</small>` : ""}</label>`).join("");
  $("#ui-error").classList.add("hidden");
  $("#ui-ok").textContent = ok;
  $("#ui-ok").className = danger ? "danger-solid" : "primary";
  $("#ui-cancel").textContent = cancel;
  $("#ui-cancel").classList.toggle("hidden", cancel === null);
  const dialog = $("#ui-dialog");
  dialog.showModal();
  (dialog.querySelector("[data-ui]") || $("#ui-ok")).focus();
  return new Promise((resolve) => {
    uiResolve = (value) => {
      uiResolve = null;
      if (dialog.open) dialog.close();
      resolve(value);
    };
    uiResolve.fields = fields;
  });
}

$("#ui-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!uiResolve) return;
  const fields = uiResolve.fields;
  const values = {};
  for (const [i, f] of fields.entries()) {
    const value = document.querySelector(`#ui-fields [data-ui="${i}"]`).value.trim();
    if (f.required && !value) {
      $("#ui-error").textContent = `请填写${f.label || "内容"}`;
      $("#ui-error").classList.remove("hidden");
      return document.querySelector(`#ui-fields [data-ui="${i}"]`).focus();
    }
    values[f.name || i] = value;
  }
  uiResolve(fields.length ? values : true);
});
// 取消：输入框返回 null，确认 / 提示返回 false
function uiCancelValue() { return uiResolve.fields.length ? null : false; }
$("#ui-cancel").addEventListener("click", () => uiResolve && uiResolve(uiCancelValue()));
$("#ui-dialog").addEventListener("cancel", (e) => { e.preventDefault(); if (uiResolve) uiResolve(uiCancelValue()); }); // Esc

// 输入：返回 {字段名: 值}，取消返回 null
function uiPrompt(options) { return uiOpen(options); }
// 确认：返回 true / false
function uiConfirm(message, { title = "请确认", ok = "确定", danger = false } = {}) {
  return uiOpen({ title, message: escapeHtml(message).replace(/\n/g, "<br>"), ok, danger });
}
// 提示：只有一个“知道了”
function uiAlert(message, title = "提示") {
  return uiOpen({ title, message: escapeHtml(message).replace(/\n/g, "<br>"), ok: "知道了", cancel: null });
}

function showNotice(el, html, kind = "") {
  el.className = `notice ${kind}`;
  el.innerHTML = html;
}

function materialTitle(id) {
  const m = state.materials.find((x) => x.id === id);
  return m ? `${id}《${m.title}》` : `材料 ${id}`;
}

// ---------- 标签页 ----------
async function activateTab(name, load = true) {
    if (name !== "todos" && $("#todo-panel").open) $("#todo-panel").close();
    const btn = document.querySelector(`.tabs button[data-tab="${name}"]`);
    document.querySelectorAll(".tabs button").forEach((b) => {
      b.classList.toggle("active", b === btn);
      if (b === btn) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${btn.dataset.tab}`));
    const headings = {
      chat: ["办公助理", "从一段文字、一份文档，开始今天的工作。"],
      todos: ["待办与进展", "明确任务，关联材料，留下每一步进展。"],
      materials: ["你的材料库", "把工作素材放在一起，随时取用、持续关注。"],
      weekly: ["本周，值得记录", "收集、核对、生成。每一份周报，都有据可依。"],
    };
    $("#page-title").textContent = headings[name][0];
    $("#page-description").textContent = headings[name][1];
    if (!load) return;
    if (name === "materials") await Promise.all([loadMaterials(), loadTrackingSummary()]);
    if (name === "todos") await Promise.all([loadMaterials(), loadTodos()]);
    if (name === "weekly") await Promise.all([loadMaterials(), loadWeekly()]);
}
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

// ---------- 材料 ----------
async function loadMaterials() {
  const [materials, tracking] = await Promise.all([
    api("/materials"),
    api("/tracking/feishu-docs").catch(() => ({})),
  ]);
  state.materials = materials;
  state.feishuTracking = tracking;
  renderMaterialRows();
  // 已删除的材料从对话选择中去掉
  for (const id of [...state.picked]) if (!state.materials.some((m) => m.id === id)) state.picked.delete(id);
  renderPicked();
}

const FILE_TYPES = { feishu: "飞书文档", bitable: "多维表格", minutes: "妙记" };

function renderMaterialRows() {
  const rows = $("#material-rows");
  if (!state.materials.length) {
    rows.innerHTML = `<tr><td colspan="5" class="muted">还没有材料。可以上传文件、粘贴文字，或从飞书导入。</td></tr>`;
    return;
  }
  rows.innerHTML = state.materials.map((m) => `
    <tr class="clickable ${state.activeMaterial === m.id ? "active" : ""}" data-id="${m.id}">
      <td>${escapeHtml(m.title)} ${m.warnings.length ? `<span class="warn-dot" title="${escapeHtml(m.warnings.join("\n"))}" role="img" aria-label="解析警告">${uiIcon("alert")}</span>` : ""}${m.source === "tracked" ? ` <span class="badge" title="随追踪自动更新为最新内容；以前的版本在“追踪的文件”里查看">最新版本</span>` : ""}</td>
      <td class="nowrap">${fileIcon(m.file_type || "paste")} ${escapeHtml(FILE_TYPES[m.file_type] || kindName(m.file_type || "paste"))}</td>
      <td class="track-cell">${trackCell(m)}</td>
      <td>${fmtTime(m.created_at)}</td>
      <td>${m.source === "tracked" ? "" : `<button class="ghost danger" data-delete="${m.id}">删除</button>`}</td>
    </tr>`).join("");
}

// ---------- 材料的追踪状态 ----------
// 材料是导入时内容的副本；飞书文档可以另外开启追踪：每个工作日检查原文档的修改，变化进入周报的审核清单
const UNTRACKABLE = {
  bitable: "多维表格暂不支持追踪，导入的是当时的内容",
  minutes: "妙记的文字稿不会再变，不需要追踪",
};

function trackCell(m) {
  if (m.tracked) {  // 追踪中文件的最新版本（本机文件或飞书文档）
    const token = m.tracked.key.startsWith("feishu://docx/") ? m.tracked.key.split("/").pop() : null;
    const entry = token ? state.feishuTracking?.[token] : null;
    if (entry && !entry.folder) {
      return `<span class="chip">追踪中${entry.status === "paused" ? "（已暂停）" : ""}<button type="button" data-untrack="${entry.source_id}" title="停止追踪（历史版本保留）">✕</button></span>`;
    }
    const why = entry?.folder ? `随飞书文件夹“${entry.folder}”追踪；要停止，请在“追踪的文件”里移除整个文件夹` : "在“追踪的文件”里管理";
    return `<span class="chip" title="${escapeHtml(why)}">追踪中${entry?.folder ? "（随文件夹）" : ""}</span>`;
  }
  if (m.feishu?.kind !== "docx") {
    const why = UNTRACKABLE[m.feishu?.kind]
      || "上传或粘贴的是一份副本，没法回去检查原文件的修改。要追踪本机文件，请在“追踪的文件”里添加文件路径";
    return `<span class="muted" title="${escapeHtml(why)}">不可追踪</span>`;
  }
  const entry = state.feishuTracking?.[m.feishu.token];
  if (entry) return `<span class="chip" title="${entry.folder ? `随飞书文件夹“${escapeHtml(entry.folder)}”追踪` : "这篇文档的最新版本在材料库里单独一行"}">追踪中</span>`;
  return `<button type="button" class="ghost small" data-track="${m.id}" title="每个工作日检查这篇飞书文档的修改，变化进入周报的审核清单">开启追踪</button>`;
}

async function trackMaterial(id) {
  const m = state.materials.find((x) => x.id === id);
  const msg = $("#material-msg");
  showNotice(msg, `正在开启追踪并记录《${escapeHtml(m.title)}》的初始版本…`);
  try {
    const r = await api(`/materials/${id}/track`, { method: "POST" });
    showNotice(msg, r.added.length ? `已开始追踪《${escapeHtml(m.title)}》：每个工作日检查修改，变化进入周报的审核清单。材料库里会多一行它的最新版本，原来这份是导入时的内容。`
      : r.messages.map(escapeHtml).join("<br>"), r.added.length ? "" : "warn");
  } catch (err) { showNotice(msg, escapeHtml(err.message), "error"); }
  await loadMaterials();
}

async function untrack(sourceId) {
  if (!(await uiConfirm("不再追踪这篇文档？已记录的历史版本会保留，材料也不受影响。", { title: "停止追踪", ok: "停止追踪", danger: true }))) return;
  try {
    await api(`/tracking/sources/${sourceId}/remove`, { method: "POST" });
  } catch (err) { showNotice($("#material-msg"), escapeHtml(err.message), "error"); }
  await loadMaterials();
}

$("#material-rows").addEventListener("click", async (e) => {
  const track = e.target.closest("[data-track]");
  if (track) {
    e.stopPropagation();
    return trackMaterial(Number(track.dataset.track));
  }
  const stop = e.target.closest("[data-untrack]");
  if (stop) {
    e.stopPropagation();
    return untrack(Number(stop.dataset.untrack));
  }
  const del = e.target.closest("[data-delete]");
  if (del) {
    e.stopPropagation();
    const id = Number(del.dataset.delete);
    if (!(await uiConfirm(`删除材料 ${materialTitle(id)}？`, { title: "删除材料", ok: "删除", danger: true }))) return;
    try {
      try {
        await api(`/materials/${id}`, { method: "DELETE" });
      } catch (err) {
        // 材料关联在待办上：说明影响，再确认一次
        if (!err.needsConfirm) throw err;
        if (!(await uiConfirm(err.message, { title: "删除材料", ok: "仍然删除", danger: true }))) return;
        await api(`/materials/${id}?force=true`, { method: "DELETE" });
      }
      if (state.activeMaterial === id) { state.activeMaterial = null; $("#material-detail").classList.add("hidden"); }
      await loadMaterials();
    } catch (err) { showNotice($("#material-msg"), escapeHtml(err.message), "error"); }
    return;
  }
  const row = e.target.closest("tr[data-id]");
  if (row) showMaterialDetail(Number(row.dataset.id));
});

async function showMaterialDetail(id) {
  state.activeMaterial = id;
  renderMaterialRows();
  const box = $("#material-detail");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted">加载中…</p>`;
  try {
    const m = await api(`/materials/${id}`);
    if (state.activeMaterial !== id) return;
    const outline = m.sections.length
      ? m.sections.map((s) => `<div style="padding-left:${(s.level - 1) * 14}px">${escapeHtml(s.title)}</div>`).join("")
      : `<div class="muted">没有识别出章节</div>`;
    const report = m.parse_report || {};
    const warnings = m.warnings.length
      ? `<div class="notice warn"><b>解析提示</b><ul>${m.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>` : "";
    box.innerHTML = `
      <div class="row gap wrap" style="justify-content:space-between">
        <h3 style="margin:0">${escapeHtml(m.title)}</h3>
        <div class="row gap wrap">
          <button class="ghost" id="use-in-chat">用于对话</button>
          <button class="ghost" data-material-skill="summarize">总结</button>
          <button class="ghost" data-material-skill="minutes">整理纪要</button>
          <button class="ghost" data-material-skill="todo">提取待办</button>
          <button class="ghost" id="material-to-week">加入本周材料</button>
          <button class="ghost" id="close-detail">收起</button>
        </div>
      </div>
      <details class="meta"><summary>技术信息</summary>编号 ${m.id} · ${m.char_count} 字 · ${m.chunk_count} 块 · ${m.section_count} 个章节 · 结构来源：${escapeHtml(m.structure || "-")}</details>
      ${warnings}
      <p class="muted small">${m.source === "tracked" ? "追踪来源 · 展示最新版本" : "固定副本 · 原文件后续修改不会自动更新"}${m.warnings.length ? ` · ${m.warnings.length} 项解析提示，请先核对` : " · 未报告解析警告（不代表所有内容均已完整识别）"}</p>
      <div><b>正文预览</b><div class="content-box material-preview">${escapeHtml(m.content)}</div></div>
      <details class="meta" data-disclosure="material:${m.id}:outline" ${disclosureOpen(`material:${m.id}:outline`)}><summary>文档大纲（${m.sections.length} 个章节）</summary><div class="outline">${outline}</div></details>
      <details class="meta"><summary>解析报告</summary><pre class="content-box">${escapeHtml(JSON.stringify(report, null, 2))}</pre></details>`;
    $("#close-detail").onclick = () => { state.activeMaterial = null; box.classList.add("hidden"); renderMaterialRows(); };
    $("#use-in-chat").onclick = () => {
      state.picked.add(id);
      renderPicked();
      document.querySelector('.tabs button[data-tab="chat"]').click();
    };
    box.querySelectorAll("[data-material-skill]").forEach((b) => b.onclick = () => {
      useMaterial(id, b.dataset.materialSkill);
      $("#input").value = {summarize:"请总结所选材料。", minutes:"请整理所选材料中的会议纪要。", todo:"请提取所选材料中的明确待办。"}[b.dataset.materialSkill];
    });
    $("#material-to-week").onclick = () => transferToWeek({material_ids:[id]});
  } catch (err) {
    box.innerHTML = `<div class="notice error">${escapeHtml(err.message)}</div>`;
  }
}

// ---------- 添加材料（弹窗：上传文件 / 粘贴文字 / 飞书链接）
function showAddPane(name) {
  const cloud = name === "cloud" || name === "link";
  document.querySelectorAll("#add-tabs button").forEach((b) => {
    const active = b.dataset.add === (cloud ? "cloud" : name);
    b.classList.toggle("active", active); b.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("#cloud-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.add === name));
  $(".intake-local").classList.toggle("hidden", cloud);
  $("#cloud-intake").classList.toggle("hidden", !cloud);
  $("#drive-panel").classList.toggle("hidden", name !== "cloud");
  $("#add-upload").classList.toggle("hidden", name !== "upload");
  $("#paste-form").classList.toggle("hidden", name !== "paste");
  $("#feishu-form").classList.toggle("hidden", name !== "link");
  $("#local-title").textContent = name === "paste" ? "粘贴要处理的文字" : "导入本机文件";
  if (cloud) loadFeishu().then(() => {
    if (!$("#add-dialog").open || !["cloud", "link"].includes(state.intakePane)) return;
    if (name === "cloud" && state.feishu?.user && !state.driveTree[""]) loadDriveFolder("");
    else renderDrive();
  });
  state.intakePane = name;
}

$("#open-add").addEventListener("click", async () => {
  addForAssistant = false;
  $("#add-msg").classList.add("hidden");
  showAddPane("upload");
  $("#add-dialog").showModal();
  $("#import-next").classList.add("hidden");
});

$("#add-dialog").addEventListener("click", (e) => {
  const b = e.target.closest("[data-add]");
  if (b) showAddPane(b.dataset.add);
});

// 弹窗右上角的 ✕
document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

// 仅从遮罩开始并结束的点击才取消，拖选文字到弹窗外不会误关闭。
let backdropStart = null;
function outsideDialog(event, dialog) {
  const r = dialog.getBoundingClientRect();
  return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom;
}
document.addEventListener("pointerdown", (e) => {
  backdropStart = e.target.matches?.("dialog[open]") && outsideDialog(e, e.target) ? e.target : null;
}, true);
document.addEventListener("click", (e) => {
  const dialog = backdropStart;
  backdropStart = null;
  if (!dialog || e.target !== dialog || !outsideDialog(e, dialog)) return;
  e.preventDefault();
  e.stopPropagation();
  if (dialog.id === "ui-dialog" && uiResolve) uiResolve(uiCancelValue());
  else if (dialog.dispatchEvent(new CustomEvent("requestclose", {cancelable:true}))) dialog.close();
}, true);

async function uploadFiles(files) {
  const msg = $("#add-msg");
  const results = [];
  for (const [i, file] of files.entries()) {
    showNotice(msg, `正在解析 ${escapeHtml(file.name)}（${i + 1}/${files.length}）…PDF、Word 可能需要几秒`);
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await api("/materials/upload", { method: "POST", body: form });
      importedForAssistant(r.material.id);
      results.push(`${r.created ? "已导入" : "已存在"}：${escapeHtml(r.material.title)}（${r.material.char_count} 字）`);
    } catch (err) {
      results.push(`<span class="danger">${escapeHtml(file.name)}：${escapeHtml(err.message)}</span>`);
    }
  }
  showNotice(msg, results.join("<br>"));
  $("#file").value = "";
  await loadMaterials();
}

$("#file").addEventListener("change", (e) => uploadFiles([...e.target.files]));
const drop = $("#drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

$("#paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#add-msg");
  try {
    const r = await api("/materials", { method: "POST", json: { title: $("#paste-title").value.trim(), content: $("#paste-content").value } });
    importedForAssistant(r.material.id);
    showNotice(msg, `${r.created ? "已保存" : "内容相同的材料已存在"}：${escapeHtml(r.material.title)}（${r.material.char_count} 字）`);
    $("#paste-title").value = "";
    $("#paste-content").value = "";
    await loadMaterials();
  } catch (err) { showNotice(msg, escapeHtml(err.message), "error"); }
});

function importedText(r) {
  const warn = r.material.warnings.length ? `（${r.material.warnings.map(escapeHtml).join("；")}）` : "";
  return `${r.created ? "已导入" : "内容没有变化，已存在"}：${escapeHtml(r.material.title)}（${r.material.char_count} 字）${warn}`;
}

$("#feishu-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#add-msg");
  const url = $("#feishu-url").value.trim();
  if (!url) return;
  showNotice(msg, "正在读取飞书内容…");
  try {
    const imported = await api("/materials/feishu", { method: "POST", json: { url } });
    importedForAssistant(imported.material.id);
    showNotice(msg, importedText(imported));
    $("#feishu-url").value = "";
    await loadMaterials();
  } catch (err) { showNotice(msg, escapeHtml(err.message), "error"); }
});

// ---------- 邮件（配置了 SMTP 才显示“发送邮件”；发送只由用户手动操作）
async function loadMail() {
  try { state.mail = await api("/mail/status"); } catch (_) { state.mail = null; }
  document.querySelectorAll(".mail-only").forEach((el) => el.classList.toggle("hidden", !state.mail?.configured));
}

// ---------- 飞书（配置了应用凭证才显示相关入口）
async function loadFeishu() {
  try { state.feishu = await api("/feishu/status"); } catch (_) { state.feishu = null; }
  document.querySelectorAll(".feishu-only").forEach((el) => el.classList.toggle("hidden", !state.feishu?.configured));
  renderFeishuAccount();
}

// 飞书账号（用户授权）：连接后可以浏览、搜索自己的云文档并导入，不用逐篇添加应用
function renderFeishuAccount() {
  const box = $("#feishu-account");
  const user = state.feishu?.user;
  $("#feishu-browser").classList.toggle("hidden", !user);
  if (!state.feishu?.configured) {
    box.innerHTML = `<div class="cloud-empty"><span class="empty-icon">${uiIcon("cloud")}</span><b>${state.feishu ? "飞书云空间尚未配置" : "暂时无法连接飞书"}</b><p>${state.feishu ? "配置飞书后，可以在这里选择云文档。" : "请稍后重新打开此窗口重试。"}现在可以继续上传本地文件或粘贴文字。</p></div>`;
    return;
  }
  if (user) {
    const missing = user.missing_scopes || [];
    box.innerHTML = `<div class="row gap wrap"><span class="muted">已连接：<b>${escapeHtml(user.name || user.open_id)}</b></span>
      <button class="ghost small danger" id="feishu-logout">断开</button></div>`
      + (missing.length ? `<div class="notice warn">权限有更新，请重新连接飞书以授权：${missing.map(escapeHtml).join("、")}
        <a class="button" href="/feishu/oauth/login">重新连接</a></div>` : "");
  } else {
    box.innerHTML = `<div class="row gap wrap"><a class="button primary" href="/feishu/oauth/login">连接飞书</a>
      <small class="muted">用你的飞书账号授权后，可以浏览和搜索自己的云文档，勾选后一次导入</small></div>
      <small class="muted">首次使用需在开发者后台“安全设置 → 重定向 URL”登记：<code>${escapeHtml(state.feishu?.redirect_uri || "")}</code></small>`;
    state.driveTree = {};
    state.driveSelected.clear();
  }
}

$("#feishu-account").addEventListener("click", async (e) => {
  if (e.target.id !== "feishu-logout") return;
  if (!(await uiConfirm("本机保存的授权会被删除，之后只能读取添加了应用的文档。", { title: "断开飞书账号", ok: "断开", danger: true }))) return;
  await api("/feishu/oauth/logout", { method: "POST" });
  await loadFeishu();
});

// ---------- 飞书云空间（弹窗）：文件夹在原地展开成树；文档和文件夹都能勾选（跨文件夹保留）
// 文档：导入当前内容，或追踪变化；文件夹：整个追踪（里面新加的文档自动纳入），不能直接导入
state.driveSelected = new Map(); // 选择键（文档用链接，文件夹用 folder:<token>）→ {name, kind, url}
state.driveTree = {}; // 文件夹 token（根目录是 ""）→ {items, open, loading, error}
state.driveSearch = "";
state.driveIndex = {}; // 选择键 → 条目（当前显示的行）
state.driveInherited = new Set(); // 当前显示的行里，随上层文件夹一起选中的选择键

const folderUrl = (token) => `https://feishu.cn/drive/folder/${token}`;
const pickKey = (x) => (x.kind === "folder" ? `folder:${x.token}` : x.url);
const pickable = (x) => x.kind === "folder" || x.importable;

async function loadDriveFolder(token) {
  const node = (state.driveTree[token] ??= { items: [], open: true });
  node.open = true;
  node.loading = true;
  node.error = "";
  renderDrive();
  try {
    node.items = await api(`/feishu/drive/files?folder=${encodeURIComponent(token)}`);
  } catch (err) {
    node.error = err.message;
    if (err.message.includes("连接飞书")) loadFeishu(); // 授权失效：回到“连接飞书”
  }
  node.loading = false;
  renderDrive();
}

function toggleDriveFolder(token) {
  const node = state.driveTree[token];
  if (!node) return loadDriveFolder(token);
  node.open = !node.open;
  renderDrive();
}

function driveTime(x) {
  return x.modified ? fmtTime(new Date(x.modified * 1000).toISOString()) : escapeHtml(x.owner || "");
}

// inherited：上层文件夹已选中，这一行随文件夹一起追踪，显示为选中且不能单独取消
function driveRow(x, depth, inherited = false) {
  const key = pickKey(x);
  state.driveIndex[key] = x;
  if (inherited && pickable(x)) state.driveInherited.add(key);
  const picked = inherited || state.driveSelected.has(key);
  const isFolder = x.kind === "folder";
  const node = isFolder ? state.driveTree[x.token] : null;
  const open = node?.open;
  const cls = [isFolder ? "folder" : "", pickable(x) ? "selectable" : "disabled", picked ? "selected" : "", inherited ? "inherited" : ""].join(" ");
  const kindText = isFolder ? "" : x.importable ? x.kind_name : `${x.kind_name}（不支持）`;
  return `<li class="${cls}" data-key="${escapeHtml(key)}" style="--depth:${depth}">
    <span class="twisty">${isFolder ? (open ? "▾" : "▸") : ""}</span>
    ${pickable(x) ? `<input type="checkbox" data-pick ${picked ? "checked" : ""} ${inherited ? `disabled title="已随上层文件夹选中"` : ""}>` : `<span class="pick-gap"></span>`}
    ${fileIcon(x.kind === "docx" ? "feishu" : x.kind)}
    <span class="name" title="${escapeHtml(x.name)}">${escapeHtml(x.name)}</span>
    <span class="kind">${escapeHtml(kindText)}</span>
    <small class="muted">${driveTime(x)}</small>
  </li>`;
}

function driveRows(items, depth, inherited = false) {
  return items.map((x) => {
    let html = driveRow(x, depth, inherited);
    const node = x.kind === "folder" ? state.driveTree[x.token] : null;
    if (node?.open) {
      if (node.loading) html += `<li class="muted" style="--depth:${depth + 1}"><span class="twisty"></span>加载中…</li>`;
      else if (node.error) html += `<li class="danger" style="--depth:${depth + 1}"><span class="twisty"></span>${escapeHtml(node.error)}</li>`;
      else if (!node.items.length) html += `<li class="muted" style="--depth:${depth + 1}"><span class="twisty"></span>（空文件夹）</li>`;
      else html += driveRows(node.items, depth + 1, inherited || state.driveSelected.has(pickKey(x)));
    }
    return html;
  }).join("");
}

function renderDrive() {
  state.driveIndex = {};
  state.driveInherited = new Set();
  const root = state.driveSearch ? state.driveTree.search : state.driveTree[""];
  $("#drive-path").innerHTML = state.driveSearch ? `搜索“${escapeHtml(state.driveSearch)}”` : "我的空间";
  $("#drive-back").classList.toggle("hidden", !state.driveSearch);
  const list = $("#drive-list");
  if (!root || root.loading) list.innerHTML = `<li class="muted">加载中…</li>`;
  else if (root.error) list.innerHTML = `<li class="danger">${escapeHtml(root.error)} <button type="button" class="ghost small" data-drive-retry>重试</button></li>`;
  else list.innerHTML = root.items.length ? driveRows(root.items, 0) : `<li class="muted">${state.driveSearch ? "没有搜到结果" : "云空间是空的"}</li>`;
  renderDriveSelection();
}

function renderDriveSelection() {
  const visible = Object.entries(state.driveIndex)
    .filter(([key, x]) => pickable(x) && !state.driveInherited.has(key)).map(([, x]) => x);
  const pickedHere = visible.filter((x) => state.driveSelected.has(pickKey(x))).length;
  const all = $("#drive-all");
  all.disabled = !visible.length;
  all.checked = Boolean(visible.length) && pickedHere === visible.length;
  all.indeterminate = pickedHere > 0 && pickedHere < visible.length;
  const folders = visible.filter((x) => x.kind === "folder").length;
  $("#drive-count").textContent = visible.length ? `当前显示 ${folders ? folders + " 个文件夹、" : ""}${visible.length - folders} 个文档` : "";
  const picked = [...state.driveSelected.values()];
  const n = picked.length;
  const pickedFolders = picked.filter((x) => x.kind === "folder").length;
  $("#drive-selected").textContent = n
    ? `已选 ${n} 项${pickedFolders ? `（含 ${pickedFolders} 个文件夹）` : ""}${n > pickedHere ? `，其中 ${n - pickedHere} 项不在当前列表` : ""}`
    : "未选择";
  $("#drive-clear").classList.toggle("hidden", !n);
  $("#drive-import").disabled = !n;
  const docs = n - pickedFolders;
  const parts = [docs ? `${docs} 篇` : "", pickedFolders ? `${pickedFolders} 个文件夹` : ""].filter(Boolean).join(" + ");
  $("#drive-import").textContent = n ? `导入为材料（${parts}）` : "导入为材料";
  $("#drive-import").title = pickedFolders ? "文件夹会导入其中（含子文件夹）支持的文档，导入前会确认篇数" : "";
  $("#drive-track").disabled = !n;
  $("#drive-track").textContent = pickedFolders ? `追踪（含 ${pickedFolders} 个文件夹）` : "追踪";
}

function setPick(x, on) {
  const key = pickKey(x);
  if (on) state.driveSelected.set(key, { name: x.name, kind: x.kind, url: x.kind === "folder" ? (x.url || folderUrl(x.token)) : x.url });
  else state.driveSelected.delete(key);
}

// 选中的文件夹已覆盖其中所有内容：去掉已加载的子项里单独选中的，避免重复
function dropCovered() {
  const drop = (token) => (state.driveTree[token]?.items || []).forEach((x) => {
    state.driveSelected.delete(pickKey(x));
    if (x.kind === "folder") drop(x.token);
  });
  [...state.driveSelected.keys()].filter((k) => k.startsWith("folder:")).forEach((k) => drop(k.slice(7)));
}

$("#drive-list").addEventListener("click", (e) => {
  if (e.target.closest("[data-drive-retry]")) {
    if (state.driveSearch) $("#drive-search").requestSubmit();
    else loadDriveFolder("");
    return;
  }
  const li = e.target.closest("li[data-key]");
  if (!li) return;
  const x = state.driveIndex[li.dataset.key];
  const box = e.target.closest("[data-pick]");
  const inherited = state.driveInherited.has(li.dataset.key);
  if (box && x.kind === "folder") {
    setPick(x, box.checked); // 文件夹的勾选框：只选中不展开；子项的显示随之变化，整体重画
    dropCovered();
    return renderDrive();
  } else if (box) {
    setPick(x, box.checked);
  } else if (x.kind === "folder") {
    return toggleDriveFolder(x.token); // 点文件夹这一行：原地展开 / 收起
  } else if (x.importable && !inherited) {
    setPick(x, !state.driveSelected.has(pickKey(x))); // 点文档这一行：勾选
  } else {
    return;
  }
  li.classList.toggle("selected", state.driveSelected.has(pickKey(x)));
  li.querySelector("[data-pick]").checked = state.driveSelected.has(pickKey(x));
  renderDriveSelection();
});

$("#drive-all").addEventListener("change", (e) => {
  Object.entries(state.driveIndex)
    .filter(([key, x]) => pickable(x) && !state.driveInherited.has(key))
    .forEach(([, x]) => setPick(x, e.target.checked));
  dropCovered();
  renderDrive();
});

$("#drive-clear").addEventListener("click", () => {
  state.driveSelected.clear();
  renderDrive();
});

$("#drive-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#drive-q").value.trim();
  if (!q) return $("#drive-back").click();
  state.driveSearch = q;
  state.driveTree.search = { items: [], loading: true };
  renderDrive();
  try {
    state.driveTree.search.items = await api(`/feishu/drive/search?q=${encodeURIComponent(q)}`);
  } catch (err) { state.driveTree.search.error = err.message; }
  state.driveTree.search.loading = false;
  renderDrive();
});

// 搜索结果里“返回”：回到文件夹树（展开状态保留）
$("#drive-back").addEventListener("click", () => {
  state.driveSearch = "";
  $("#drive-q").value = "";
  if (!state.driveTree[""]) return loadDriveFolder("");
  renderDrive();
});

// 文件夹里（含子文件夹）支持导入的文档：用已加载的内容，没加载过的向飞书列出；最多 10 层
const IMPORT_FOLDER_LIMIT = 50;
async function folderDocs(token, depth = 0, out = [], stats = { skipped: 0 }) {
  if (depth > 10) return { docs: out, ...stats };
  const node = state.driveTree[token];
  const items = node?.items?.length && !node.error ? node.items
    : await api(`/feishu/drive/files?folder=${encodeURIComponent(token)}`);
  for (const x of items) {
    if (x.kind === "folder") await folderDocs(x.token, depth + 1, out, stats);
    else if (x.importable) out.push(x);
    else stats.skipped += 1;
  }
  return { docs: out, ...stats };
}

// 批量导入：文档直接导入；文件夹先列出其中的文档、确认篇数后导入。逐个读取（同时读太多会触发飞书限频），完成后取消勾选成功的项
$("#drive-import").addEventListener("click", async () => {
  const msg = $("#drive-msg");
  const picked = [...state.driveSelected.entries()];
  const folderKeys = picked.filter(([, x]) => x.kind === "folder").map(([key]) => key);
  const jobs = new Map(picked.filter(([, x]) => x.kind !== "folder").map(([key, x]) => [x.url, { key, name: x.name, url: x.url }]));
  $("#drive-import").disabled = true;
  let skipped = 0;
  if (folderKeys.length) {
    showNotice(msg, "正在列出所选文件夹里的文档…");
    try {
      let found = 0;
      for (const key of folderKeys) {
        const r = await folderDocs(key.slice("folder:".length));
        skipped += r.skipped;
        found += r.docs.length;
        r.docs.forEach((x) => jobs.has(x.url) || jobs.set(x.url, { name: x.name, url: x.url }));
      }
      if (!jobs.size) {
        renderDriveSelection();
        return showNotice(msg, `所选文件夹里没有能导入的文档${skipped ? `（${skipped} 个条目类型不支持）` : ""}。`, "warn");
      }
      if (jobs.size > IMPORT_FOLDER_LIMIT) {
        renderDriveSelection();
        return showNotice(msg, `一共 ${jobs.size} 篇文档，超过一次导入的上限 ${IMPORT_FOLDER_LIMIT} 篇。请展开文件夹只勾选需要的文档；需要持续关注整个文件夹，可以用“追踪”。`, "warn");
      }
      const ok = await uiConfirm(
        `所选的 ${folderKeys.length} 个文件夹里共有 ${found} 篇可导入的文档${skipped ? `（另有 ${skipped} 个条目类型不支持，会跳过）` : ""}${jobs.size > found ? `，加上单独勾选的，一共 ${jobs.size} 篇` : ""}，每篇成为一份材料。\n\n导入的是现在的内容，之后文档有改动不会自动更新；需要跟踪变化，可以导入后在材料库里开启追踪，或用“追踪”。`,
        { title: "导入文件夹里的文档", ok: `导入 ${jobs.size} 篇` },
      );
      if (!ok) {
        msg.classList.add("hidden");
        return renderDriveSelection();
      }
    } catch (err) {
      renderDriveSelection();
      return showNotice(msg, `列出文件夹失败：${escapeHtml(err.message)}`, "error");
    }
  }
  const list = [...jobs.values()];
  const results = [];
  let failed = 0;
  for (const [i, job] of list.entries()) {
    showNotice(msg, `正在导入 ${i + 1}/${list.length}：${escapeHtml(job.name)}…`);
    try {
      const imported = await api("/materials/feishu", { method: "POST", json: { url: job.url } });
      importedForAssistant(imported.material.id);
      results.push(importedText(imported));
      if (job.key) state.driveSelected.delete(job.key);
    } catch (err) {
      failed += 1;
      results.push(`<span class="danger">${escapeHtml(job.name)}：${escapeHtml(err.message)}</span>`);
    }
  }
  if (!failed) folderKeys.forEach((key) => state.driveSelected.delete(key));
  if (skipped) results.push(`文件夹里另有 ${skipped} 个条目类型不支持（如电子表格、文件），没有导入。`);
  showNotice(msg, results.join("<br>"), failed ? "warn" : "");
  await loadMaterials();
  renderDrive();
});

// 批量追踪：文档、知识库页面、文件夹（整个文件夹）可以追踪；多维表格、妙记只能导入

$("#drive-track").addEventListener("click", async () => {
  const chosen = [...state.driveSelected.entries()].filter(([, x]) => ["docx", "wiki", "folder"].includes(x.kind));
  const skipped = state.driveSelected.size - chosen.length;
  const msg = $("#drive-msg");
  if (!chosen.length) {
    return showNotice(msg, "所选项里没有能追踪的：只有飞书文档、知识库页面和文件夹能追踪，多维表格、妙记请用“导入为材料”。", "warn");
  }
  const folders = chosen.filter(([, x]) => x.kind === "folder").length;
  const ok = await uiConfirm(`追踪所选的 ${chosen.length - folders ? `${chosen.length - folders} 篇文档` : ""}${chosen.length - folders && folders ? "和" : ""}${folders ? `${folders} 个文件夹` : ""}：之后每个工作日检查修改，变化进入周报的审核清单。`
    + (folders ? "\n文件夹会整个追踪：以后新放进去的文档也会自动纳入。" : ""), { title: "追踪所选", ok: "开始追踪" });
  if (!ok) return;
  showNotice(msg, `正在开始追踪并记录初始版本…${folders ? "（文件夹里的文档较多时需要一会儿）" : ""}`);
  try {
    const names = Object.fromEntries(chosen.map(([, x]) => [x.url, x.name]));
    const r = await api("/tracking/sources", { method: "POST", json: { paths: chosen.map(([, x]) => x.url), names } });
    const addedFolders = r.added.filter((s) => s.kind === "feishu_folder");
    const docsInFolders = addedFolders.reduce((n, s) => n + (s.folder?.docs || 0), 0);
    const lines = [`已开始追踪：${r.added.length - addedFolders.length} 篇文档`
      + (addedFolders.length ? `、${addedFolders.length} 个文件夹（其中 ${docsInFolders} 篇文档，之后新加进文件夹的文档也会自动追踪）` : "")
      + "。每个工作日自动检查更新。", ...r.messages.map(escapeHtml)];
    if (skipped) lines.push(`另有 ${skipped} 项（多维表格、妙记等）不能追踪，没有加入。`);
    showNotice(msg, lines.join("<br>"), r.messages.length || skipped ? "warn" : "");
    chosen.forEach(([key]) => state.driveSelected.delete(key));
    renderDrive();
    await Promise.all([loadMaterials(), loadTrackingSummary()]);
  } catch (err) { showNotice(msg, escapeHtml(err.message), "error"); }
});

// 授权回调后回到本页：显示结果，连接成功直接打开云空间
function handleFeishuRedirect() {
  const params = new URLSearchParams(location.search);
  if (!params.has("feishu") && !params.has("feishu_error")) return;
  document.querySelector('.tabs button[data-tab="materials"]').click();
  history.replaceState(null, "", "/");
  if (params.has("feishu_error")) return showNotice($("#material-msg"), `连接飞书失败：${escapeHtml(params.get("feishu_error"))}`, "error");
  showNotice($("#material-msg"), `已连接飞书账号：${escapeHtml(params.get("name") || "")}`);
  $("#open-add").click();
}

// ---------- 对话 ----------
async function loadSkills() {
  state.skills = await api("/skills");
  $("#skill").innerHTML = `<option value="">自动判断能力</option>` +
    state.skills.map((s) => `<option value="${s.name}">${escapeHtml(s.description)}</option>`).join("");
}

async function loadSessions() {
  const sessions = await api("/chat/sessions");
  $("#session-list").innerHTML = sessions.length
    ? sessions.map((s) => `<li data-id="${s.thread_id}" class="${s.thread_id === state.threadId ? "active" : ""}" title="${escapeHtml(s.title)}">
        ${escapeHtml(s.title)}<small>${fmtTime(s.updated_at)}</small></li>`).join("")
    : `<li class="muted" style="cursor:default">还没有会话</li>`;
}

$("#session-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) openSession(li.dataset.id);
});

function setSessionButtons() {
  $("#show-context").disabled = !state.threadId;
  $("#delete-session").disabled = !state.threadId;
  $("#context-box").classList.add("hidden");
}

function rememberComposer() {
  state.drafts.set(state.threadId || "new", {text:$("#input").value, skill:$("#skill").value, material_ids:[...state.picked]});
}

async function openSession(threadId) {
  rememberComposer();
  const view = ++state.viewRevision;
  const draft = state.drafts.get(threadId);
  state.picked = new Set(draft?.material_ids || []); renderPicked();
  $("#skill").value = draft?.skill || "";
  $("#input").value = draft?.text || "";
  state.lastRequest = null;
  state.threadId = threadId;
  setSessionButtons();
  const detail = await api(`/chat/sessions/${threadId}`);
  if (state.viewRevision !== view) return;
  $("#session-title").textContent = detail.title;
  $("#messages").innerHTML = "";
  detail.messages.forEach(appendMessage);
  scrollMessages();
  loadSessions();
}

function newSession() {
  rememberComposer();
  state.viewRevision++;
  state.threadId = null;
  setSessionButtons();
  $("#session-title").textContent = "新会话";
  state.picked.clear(); renderPicked(); $("#skill").value = "";
  state.lastRequest = null;
  $("#input").value = "";
  showWelcome();
  loadSessions();
  $("#input").focus();
}
$("#new-session").addEventListener("click", newSession);

function showWelcome() {
  $("#messages").replaceChildren($("#chat-welcome").content.cloneNode(true));
}

function appendMessage(m) {
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${m.role}${m.pending ? " pending" : ""}`;
  const meta = [];
  if (m.role === "user") {
    state.lastRequest = {text:m.text, material_ids:m.material_ids || [], skill:m.skill || null};
    if (m.material_ids?.length) meta.push(`材料：${m.material_ids.map(materialTitle).map(escapeHtml).join("、")}`);
    if (m.skill) meta.push(`能力：${escapeHtml(state.skills.find((s) => s.name === m.skill)?.description || m.skill)}`);
  } else if (m.tools?.length) {
    meta.push(`调用了工具：${escapeHtml([...new Set(m.tools)].join("、"))}`);
  }
  const body = m.role === "assistant" && !m.pending
    ? `<div class="bubble markdown">${renderMarkdown(m.text)}</div>`
    : `<div class="bubble">${escapeHtml(m.text)}</div>`;
  el.innerHTML = `<div class="message-author">${m.role === "user" ? "你" : 'Briefly <span>办公助理</span>'}</div>` + body + (meta.length ? `<div class="msg-meta">${meta.join(" · ")}</div>` : "");
  box.appendChild(el);
  if (m.role === "assistant" && !m.pending) {
    el.querySelectorAll(".bubble table").forEach((table) => { const wrap = document.createElement("div"); wrap.className = "answer-table-scroll"; wrap.tabIndex = 0; wrap.setAttribute("role","region"); wrap.setAttribute("aria-label","回答表格，可横向滚动"); table.before(wrap); wrap.append(table); });
    const metaBox = el.querySelector(".msg-meta");
    if (metaBox && m.tools?.length) { const detail = document.createElement("details"); detail.className = "message-process"; const summary = document.createElement("summary"); summary.textContent = "查看处理过程"; metaBox.before(detail); detail.append(summary,metaBox); }
    resultActions(el, m);
    addAnswerNavigation(el, m.text);
  }
  return el;
}

function scrollMessages() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("#send").disabled) return;
  const text = $("#input").value.trim();
  if (!text) return;
  const material_ids = [...state.picked];
  const skill = $("#skill").value || null;
  const requestThread = state.threadId;
  const view = state.viewRevision;
  appendMessage({ role: "user", text, material_ids, skill });
  const pending = appendMessage({ role: "assistant", text: "思考中…（读取材料或长文档时可能需要一两分钟）", pending: true });
  scrollMessages();
  $("#input").value = "";
  $("#send").disabled = true;
  const previousActions = [...document.querySelectorAll(".result-action")].map((button) => ({button, disabled:button.disabled}));
  previousActions.forEach(({button}) => button.disabled = true);
  try {
    const r = await api("/chat", { method: "POST", json: { text, thread_id: requestThread, material_ids, skill } });
    // 等待期间用户切换了会话时，不把旧任务的结果和能力设置带到新会话。
    if (state.viewRevision !== view) { loadSessions(); return; }
    const isNew = !requestThread;
    state.threadId = r.thread_id;
    setSessionButtons();
    // 重新取历史，拿到本轮调用的工具名
    const detail = await api(`/chat/sessions/${r.thread_id}`);
    if (state.viewRevision !== view) return;
    pending.remove();
    // 预检拒绝可能没有新增模型历史；仍显示这次实际返回的说明，不能误用上一条回答。
    const answer = [...detail.messages].reverse().find((m) => m.role === "assistant" && m.text === r.reply);
    appendMessage(answer || {role:"assistant", text:r.reply, system_reply:true});
    if (isNew) $("#session-title").textContent = detail.title;
    state.picked.clear();
    $("#skill").value = "";
    state.drafts.delete(requestThread || "new");
    renderPicked();
    loadSessions();
  } catch (err) {
    pending.remove();
    if (state.viewRevision !== view) return;
    const el = appendMessage({ role: "assistant", text: `出错了：${err.message}`, pending: true });
    el.querySelector(".bubble").classList.add("danger");
    $("#input").value = text; // 失败时把输入放回去
    previousActions.forEach(({button, disabled}) => { if (button.isConnected) button.disabled = disabled; });
  } finally {
    $("#send").disabled = false;
    scrollMessages();
  }
});

$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#composer").requestSubmit();
});

$("#delete-session").addEventListener("click", async () => {
  if (!state.threadId || !(await uiConfirm("对话记录将一并删除。", { title: "删除这个会话", ok: "删除", danger: true }))) return;
  await api(`/chat/sessions/${state.threadId}`, { method: "DELETE" });
  newSession();
});

$("#show-context").addEventListener("click", async () => {
  const box = $("#context-box");
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  const c = await api(`/chat/sessions/${state.threadId}/context`);
  if (!c.usage) {
    showNotice(box, "本会话还没有调用记录。");
    return;
  }
  const u = c.usage;
  const actual = u.input_tokens ? `，接口实际 ${u.input_tokens}` : "";
  const limit = u.context_limit ? `（上限 ${u.context_limit}）` : "";
  showNotice(box, `
    上次调用：估算 ${u.estimated_tokens} tokens${actual}${limit}<br>
    系统 ${u.system_tokens} / 工具定义 ${u.tool_schema_tokens} / 记忆 ${u.memory_tokens} /
    历史 ${u.history_tokens}（${u.history_turns} 轮） / 当前轮 ${u.current_tokens}
    （其中材料 ${u.material_tokens}、工具结果 ${u.tool_result_tokens}）
    ${u.omitted_turns ? `<br>有 ${u.omitted_turns} 轮历史未放进上下文` : ""}
    ${c.memory_version ? `<br>会话记忆 v${c.memory_version}：已压缩 ${c.compacted_turns} 轮` : ""}`);
});

// 选择材料
function renderPicked() {
  $("#picked").innerHTML = [...state.picked].map((id) =>
    `<span class="chip">${escapeHtml(materialTitle(id))}<button type="button" data-unpick="${id}" title="移除">×</button></span>`).join("");
}
$("#picked").addEventListener("click", (e) => {
  const b = e.target.closest("[data-unpick]");
  if (b) { state.picked.delete(Number(b.dataset.unpick)); renderPicked(); }
});

$("#pick-materials").addEventListener("click", async () => {
  await loadMaterials();
  $("#picker-list").innerHTML = state.materials.length
    ? state.materials.map((m) => `<label><input type="checkbox" value="${m.id}" ${state.picked.has(m.id) ? "checked" : ""}>
        ${m.id}. ${escapeHtml(m.title)} <span class="muted">（${m.char_count} 字${m.tracked ? "，追踪中 · 最新版本" : ""}）</span></label>`).join("")
    : `<p class="muted">材料库是空的，请先到“材料”页上传或粘贴。</p>`;
  $("#picker").showModal();
});
$("#picker-done").addEventListener("click", () => {
  state.picked = new Set([...document.querySelectorAll("#picker-list input:checked")].map((i) => Number(i.value)));
  renderPicked();
  $("#picker").close();
});

// ---------- 从材料库选择（待办、本周材料共用）----------
// 返回选中的材料编号；取消返回 null。exclude 显示为已选且不能取消，suggested 标“建议”。
let pickResolve = null;
const pickState = { picking: new Set(), exclude: new Set(), suggested: new Set() };

function renderMatPicker() {
  const q = $("#mat-picker-q").value.trim().toLowerCase();
  const rows = state.materials.filter((m) => !q || m.title.toLowerCase().includes(q));
  $("#mat-picker-list").innerHTML = rows.length
    ? rows.map((m) => {
      const has = pickState.exclude.has(m.id);
      const tip = has ? "（已加入）" : `（${m.char_count} 字${m.tracked ? "，追踪中的最新版本" : m.feishu ? "，飞书" : ""}）`;
      return `<label>${fileIcon(m.file_type || "paste")}<input type="checkbox" value="${m.id}" ${has || pickState.picking.has(m.id) ? "checked" : ""} ${has ? "disabled" : ""}>
        ${escapeHtml(m.title)} <span class="muted">${tip}</span>${pickState.suggested.has(m.id) && !has ? ' <span class="badge">本周待办的材料</span>' : ""}</label>`;
    }).join("")
    : `<p class="muted">${state.materials.length ? "没有匹配的材料" : "材料库是空的"}</p>`;
  $("#mat-picker-count").textContent = pickState.picking.size ? `已选 ${pickState.picking.size} 份` : "";
}

async function pickMaterials({ title = "从材料库选择", ok = "确定", exclude = [], suggested = [] } = {}) {
  await loadMaterials();
  pickState.picking = new Set();
  pickState.exclude = new Set(exclude);
  pickState.suggested = new Set(suggested);
  $("#mat-picker-title").textContent = title;
  $("#mat-picker-done").textContent = ok;
  $("#mat-picker-q").value = "";
  renderMatPicker();
  $("#mat-picker").showModal();
  return new Promise((resolve) => { pickResolve = resolve; });
}

$("#mat-picker-q").addEventListener("input", renderMatPicker);
$("#mat-picker-list").addEventListener("change", (e) => {
  const cb = e.target.closest("input[type=checkbox]");
  if (!cb) return;
  cb.checked ? pickState.picking.add(Number(cb.value)) : pickState.picking.delete(Number(cb.value));
  $("#mat-picker-count").textContent = pickState.picking.size ? `已选 ${pickState.picking.size} 份` : "";
});
$("#mat-picker-done").addEventListener("click", () => {
  $("#mat-picker").close();
  if (pickResolve) pickResolve([...pickState.picking]);
  pickResolve = null;
});
$("#mat-picker").addEventListener("close", () => {
  if (pickResolve) pickResolve(null);
  pickResolve = null;
});

// ---------- 提醒（审核清单已整理好、自动整理失败等）----------
async function loadInbox() {
  let items = [];
  try {
    items = await api("/notifications");
  } catch (_) {
    return; // 提醒加载失败不影响页面
  }
  const box = $("#inbox");
  box.classList.toggle("hidden", !items.length);
  box.innerHTML = items.map((n) => `
    <div class="inbox-item">
      <div><b>🔔 ${escapeHtml(n.title)}</b>${escapeHtml(n.body)}</div>
      ${n.report_id ? `<button class="primary" data-go="${n.report_id}" data-read-id="${n.id}">去审核</button>` : ""}
      <button class="ghost" data-read="${n.id}">知道了</button>
    </div>`).join("");
}

$("#inbox").addEventListener("click", async (e) => {
  const go = e.target.closest("[data-go]");
  if (go) {
    await api("/notifications/read", { method: "POST", json: { ids: [Number(go.dataset.readId)] } });
    loadInbox();
    gotoWeek(Number(go.dataset.go));
    return;
  }
  const b = e.target.closest("[data-read]");
  if (!b) return;
  await api("/notifications/read", { method: "POST", json: { ids: [Number(b.dataset.read)] } });
  loadInbox();
});

// ---------- 启动 ----------
(async function init() {
  showWelcome();
  try {
    await Promise.all([loadSkills(), loadMaterials(), loadSessions(), loadInbox(), loadFeishu(), loadMail()]);
    await activateTab(document.querySelector(".tabs button.active").dataset.tab);
    handleFeishuRedirect();
    setInterval(loadInbox, 60000);
  } catch (err) {
    showNotice($("#app-status"), `连接服务失败：${escapeHtml(err.message)}。请刷新页面重试。`, "error");
  }
})();

function addAnswerNavigation(el, text) {
  const bubble = el.querySelector(".bubble");
  // 不改变输出文本，不将标题识别用作业务保存依据。
  if (text.length < 400) return;
  let headings = [...bubble.querySelectorAll("h1,h2,h3")];
  if (headings.length < 2) {
    const pattern = /(?:^|\n)(【[^】\n]{1,32}】)/g;
    const matches = [...text.matchAll(pattern)];
    if (matches.length < 2) return;
    // 遍历文本节点只标记独占一行的中文栏目；不注入用户HTML。
    const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT);
    const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement.closest("code,pre,table")) continue;
      const parts = node.textContent.split(/(^【[^】\n]{1,32}】(?=\n|$))/gm);
      if (parts.length < 2) continue;
      const fragment = document.createDocumentFragment();
      for (const part of parts) {
        if (/^【[^】\n]{1,32}】$/.test(part)) {
          const span = document.createElement("span"); span.className = "answer-heading"; span.textContent = part; fragment.append(span); headings.push(span);
        } else fragment.append(document.createTextNode(part));
      }
      node.replaceWith(fragment);
    }
  }
  if (headings.length < 2) return;
  const nav = document.createElement("nav"); nav.className = "answer-nav"; nav.setAttribute("aria-label", "回答栏目导航");
  for (const heading of headings) {
    const button = document.createElement("button"); button.className = "ghost small"; button.type = "button"; button.textContent = heading.textContent;
    button.onclick = () => { heading.scrollIntoView({block:"start"}); heading.tabIndex = -1; heading.focus({preventScroll:true}); };
    nav.append(button);
  }
  bubble.before(nav);
}
