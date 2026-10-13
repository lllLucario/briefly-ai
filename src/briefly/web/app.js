// Briefly AI 网页：对话、材料、周报。原生 JS，直接调用同源的 FastAPI 接口。
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  threadId: null, // 当前会话；null 表示新会话，发送第一句后由后端分配
  picked: new Set(), // 对话中选用的材料编号
  materials: [],
  skills: [],
  weeklyId: null,
  weeklyTimer: null,
  weeklyReport: null,
  activeMaterial: null,
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
    try {
      const body = await resp.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* 非 JSON 错误 */ }
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Markdown 渲染；CDN 不可用时退回纯文本
function renderMarkdown(text) {
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(text, { breaks: true }));
  }
  return `<div style="white-space:pre-wrap">${escapeHtml(text)}</div>`;
}

function fmtTime(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "materials") loadMaterials();
    if (btn.dataset.tab === "weekly") { loadMaterials(); loadWeeklyList(); }
  });
});

// ---------- 材料 ----------
async function loadMaterials() {
  state.materials = await api("/materials");
  renderMaterialRows();
  renderWeeklyMaterialChecklist();
  // 已删除的材料从对话选择中去掉
  for (const id of [...state.picked]) if (!state.materials.some((m) => m.id === id)) state.picked.delete(id);
  renderPicked();
}

function renderMaterialRows() {
  const rows = $("#material-rows");
  if (!state.materials.length) {
    rows.innerHTML = `<tr><td colspan="8" class="muted">还没有材料。可以上传文件或粘贴文字。</td></tr>`;
    return;
  }
  rows.innerHTML = state.materials.map((m) => `
    <tr class="clickable ${state.activeMaterial === m.id ? "active" : ""}" data-id="${m.id}">
      <td>${m.id}</td>
      <td>${escapeHtml(m.title)} ${m.warnings.length ? `<span class="warn-dot" title="${escapeHtml(m.warnings.join("\n"))}">⚠</span>` : ""}</td>
      <td>${m.file_type || "粘贴"}</td>
      <td>${m.char_count}</td>
      <td>${m.chunk_count} / ${m.section_count}</td>
      <td>${m.structure || "-"}</td>
      <td>${fmtTime(m.created_at)}</td>
      <td><button class="ghost danger" data-delete="${m.id}">删除</button></td>
    </tr>`).join("");
}

$("#material-rows").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-delete]");
  if (del) {
    e.stopPropagation();
    const id = Number(del.dataset.delete);
    if (!confirm(`删除材料 ${materialTitle(id)}？`)) return;
    try {
      await api(`/materials/${id}`, { method: "DELETE" });
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
    const outline = m.sections.length
      ? m.sections.map((s) => `<div style="padding-left:${(s.level - 1) * 14}px">${escapeHtml(s.title)}</div>`).join("")
      : `<div class="muted">没有识别出章节</div>`;
    const report = m.parse_report || {};
    const warnings = m.warnings.length
      ? `<div class="notice warn"><b>解析提示</b><ul>${m.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>` : "";
    box.innerHTML = `
      <div class="row gap wrap" style="justify-content:space-between">
        <h3 style="margin:0">${escapeHtml(m.title)}</h3>
        <div class="row gap">
          <button class="ghost" id="use-in-chat">用于对话</button>
          <button class="ghost" id="close-detail">收起</button>
        </div>
      </div>
      <p class="muted">${m.char_count} 字 · ${m.chunk_count} 块 · ${m.section_count} 个章节 · 结构来源：${m.structure || "-"}</p>
      ${warnings}
      <div class="detail-grid">
        <div><b>大纲</b><div class="outline">${outline}</div></div>
        <div><b>正文</b><div class="content-box">${escapeHtml(m.content)}</div></div>
      </div>
      <details class="meta"><summary>解析报告</summary><pre class="content-box">${escapeHtml(JSON.stringify(report, null, 2))}</pre></details>`;
    $("#close-detail").onclick = () => { state.activeMaterial = null; box.classList.add("hidden"); renderMaterialRows(); };
    $("#use-in-chat").onclick = () => {
      state.picked.add(id);
      renderPicked();
      document.querySelector('.tabs button[data-tab="chat"]').click();
    };
  } catch (err) {
    box.innerHTML = `<div class="notice error">${escapeHtml(err.message)}</div>`;
  }
}

async function uploadFiles(files) {
  const msg = $("#material-msg");
  const results = [];
  for (const file of files) {
    showNotice(msg, `正在解析 ${escapeHtml(file.name)}…（PDF、Word 可能需要几秒）`);
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await api("/materials/upload", { method: "POST", body: form });
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
  const msg = $("#material-msg");
  try {
    const r = await api("/materials", { method: "POST", json: { title: $("#paste-title").value.trim(), content: $("#paste-content").value } });
    showNotice(msg, `${r.created ? "已保存" : "内容相同的材料已存在"}：${escapeHtml(r.material.title)}（${r.material.char_count} 字）`);
    $("#paste-title").value = "";
    $("#paste-content").value = "";
    await loadMaterials();
  } catch (err) { showNotice(msg, escapeHtml(err.message), "error"); }
});

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

async function openSession(threadId) {
  state.threadId = threadId;
  setSessionButtons();
  const detail = await api(`/chat/sessions/${threadId}`);
  $("#session-title").textContent = detail.title;
  $("#messages").innerHTML = "";
  detail.messages.forEach(appendMessage);
  scrollMessages();
  loadSessions();
}

function newSession() {
  state.threadId = null;
  setSessionButtons();
  $("#session-title").textContent = "新会话";
  $("#messages").innerHTML = `<div class="empty">可以直接粘贴文字，或在下方选择材料后提问。<br>能力：文档总结、会议纪要拆解、周报生成、待办梳理、文本优化。</div>`;
  loadSessions();
  $("#input").focus();
}
$("#new-session").addEventListener("click", newSession);

function appendMessage(m) {
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${m.role}${m.pending ? " pending" : ""}`;
  const meta = [];
  if (m.role === "user") {
    if (m.material_ids?.length) meta.push(`材料：${m.material_ids.map(materialTitle).map(escapeHtml).join("、")}`);
    if (m.skill) meta.push(`能力：${escapeHtml(state.skills.find((s) => s.name === m.skill)?.description || m.skill)}`);
  } else if (m.tools?.length) {
    meta.push(`调用了工具：${escapeHtml([...new Set(m.tools)].join("、"))}`);
  }
  const body = m.role === "assistant" && !m.pending
    ? `<div class="bubble markdown">${renderMarkdown(m.text)}</div>`
    : `<div class="bubble">${escapeHtml(m.text)}</div>`;
  el.innerHTML = body + (meta.length ? `<div class="msg-meta">${meta.join(" · ")}</div>` : "");
  box.appendChild(el);
  return el;
}

function scrollMessages() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (!text) return;
  const material_ids = [...state.picked];
  const skill = $("#skill").value || null;
  appendMessage({ role: "user", text, material_ids, skill });
  const pending = appendMessage({ role: "assistant", text: "思考中…（读取材料或长文档时可能需要一两分钟）", pending: true });
  scrollMessages();
  $("#input").value = "";
  $("#send").disabled = true;
  try {
    const r = await api("/chat", { method: "POST", json: { text, thread_id: state.threadId, material_ids, skill } });
    const isNew = !state.threadId;
    state.threadId = r.thread_id;
    setSessionButtons();
    // 重新取历史，拿到本轮调用的工具名
    const detail = await api(`/chat/sessions/${r.thread_id}`);
    pending.remove();
    appendMessage(detail.messages[detail.messages.length - 1]);
    if (isNew) $("#session-title").textContent = detail.title;
    state.picked.clear();
    renderPicked();
    loadSessions();
  } catch (err) {
    pending.remove();
    const el = appendMessage({ role: "assistant", text: `出错了：${err.message}`, pending: true });
    el.querySelector(".bubble").classList.add("danger");
    $("#input").value = text; // 失败时把输入放回去
  } finally {
    $("#send").disabled = false;
    scrollMessages();
  }
});

$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#composer").requestSubmit();
});

$("#delete-session").addEventListener("click", async () => {
  if (!state.threadId || !confirm("删除这个会话？对话记录将一并删除。")) return;
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
        ${m.id}. ${escapeHtml(m.title)} <span class="muted">（${m.char_count} 字）</span></label>`).join("")
    : `<p class="muted">材料库是空的，请先到“材料”页上传或粘贴。</p>`;
  $("#picker").showModal();
});
$("#picker-done").addEventListener("click", () => {
  state.picked = new Set([...document.querySelectorAll("#picker-list input:checked")].map((i) => Number(i.value)));
  renderPicked();
  $("#picker").close();
});

// ---------- 周报 ----------
const STATUS_TEXT = { pending: "排队中", running: "生成中", done: "已完成", failed: "失败" };

function renderWeeklyMaterialChecklist() {
  const box = $("#weekly-materials");
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => Number(i.value)));
  box.innerHTML = state.materials.length
    ? state.materials.map((m) => `<label><input type="checkbox" value="${m.id}" ${checked.has(m.id) ? "checked" : ""}>
        ${m.id}. ${escapeHtml(m.title)} <span class="muted">（${m.char_count} 字）</span></label>`).join("")
    : `<p class="muted">材料库是空的。也可以只写简述生成周报。</p>`;
}

async function loadWeeklyList() {
  const reports = await api("/weekly-reports");
  $("#weekly-list").innerHTML = reports.length
    ? reports.map((r) => `<li data-id="${r.id}" class="${r.id === state.weeklyId ? "active" : ""}">
        ${r.period_start} ~ ${r.period_end}
        <small><span class="status-pill ${r.status}">${STATUS_TEXT[r.status] || r.status}</span> ${fmtTime(r.created_at)}</small></li>`).join("")
    : `<li class="muted" style="cursor:default">还没有周报</li>`;
  return reports;
}

$("#weekly-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) openWeekly(Number(li.dataset.id));
});

$("#new-weekly").addEventListener("click", () => {
  stopPolling();
  state.weeklyId = null;
  $("#weekly-form-panel").classList.remove("hidden");
  $("#weekly-detail-panel").classList.add("hidden");
  loadWeeklyList();
});

$("#weekly-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    material_ids: [...document.querySelectorAll("#weekly-materials input:checked")].map((i) => Number(i.value)),
    brief: $("#brief").value.trim(),
    period_start: $("#period-start").value || null,
    period_end: $("#period-end").value || null,
    audience: $("#audience").value.trim() || null,
    name: $("#name").value.trim() || null,
  };
  try {
    const r = await api("/weekly-reports", { method: "POST", json: body });
    openWeekly(r.id);
  } catch (err) {
    alert(err.message);
  }
});

function stopPolling() {
  if (state.weeklyTimer) clearTimeout(state.weeklyTimer);
  state.weeklyTimer = null;
}

async function openWeekly(id) {
  stopPolling();
  state.weeklyId = id;
  $("#weekly-form-panel").classList.add("hidden");
  $("#weekly-detail-panel").classList.remove("hidden");
  exitEdit();
  await refreshWeekly();
}

// 出处标记〔1-3〕〔简述〕渲染成可点的小标签（html 为已转义或已净化的 HTML）
function tagify(html) {
  return html.replace(/〔([^〕]*)〕/g, (_, tag) => `<span class="tag" data-tag="${tag}">${tag}</span>`);
}

function renderReport(content) {
  return tagify(renderMarkdown(content));
}

function stripTags(content) {
  return content.replace(/〔[^〕]*〕/g, "").replace(/[ \t]+\n/g, "\n").trim();
}

async function refreshWeekly() {
  const id = state.weeklyId;
  let r;
  try {
    r = await api(`/weekly-reports/${id}`);
  } catch (err) {
    showNotice($("#weekly-status"), escapeHtml(err.message), "error");
    return;
  }
  if (id !== state.weeklyId) return; // 已切到别的周报
  state.weeklyReport = r;
  $("#weekly-title").textContent = `周报 ${r.period_start} ~ ${r.period_end}`;
  const done = r.status === "done";
  ["#edit-weekly", "#print-weekly", "#show-tags"].forEach((s) => ($(s).disabled = !done));
  $("#export-md").classList.toggle("hidden", !done);
  $("#export-md").href = `/weekly-reports/${id}/export.md`;

  const status = $("#weekly-status");
  if (r.status === "pending" || r.status === "running") {
    showNotice(status, `${STATUS_TEXT[r.status]}…逐块提取并核对素材，材料多时需要几分钟。页面会自动刷新。`);
    state.weeklyTimer = setTimeout(refreshWeekly, 3000);
  } else if (r.status === "failed") {
    showNotice(status, `生成失败：${escapeHtml(r.error)}`, "error");
  } else {
    status.classList.add("hidden");
  }

  const g = r.generation || {};
  const reminders = $("#weekly-reminders");
  if (g.reminders?.length) {
    showNotice(reminders, `<b>给你的提醒</b>（不会进入周报和导出）<ul>${g.reminders.map((x) => `<li>${tagify(escapeHtml(x))}</li>`).join("")}</ul>`, "warn");
  } else reminders.classList.add("hidden");
  const warnings = $("#weekly-warnings");
  if (g.warnings?.length) {
    showNotice(warnings, `<b>系统提示</b><ul>${g.warnings.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`);
  } else warnings.classList.add("hidden");

  $("#weekly-preview").innerHTML = done ? renderReport(r.content) : "";
  $("#weekly-preview").classList.toggle("hide-tags", !$("#show-tags").checked);
  renderWeeklyMeta(r);
  loadWeeklyList();
}

function renderWeeklyMeta(r) {
  const g = r.generation || {};
  const evidence = r.options?.evidence_ids || [];
  const rows = [
    ["素材", r.material_ids.length ? r.material_ids.map(materialTitle).join("、") : evidence.length ? "无" : "无（只用简述）"],
    ...(evidence.length ? [["工作记录", `${evidence.length} 条已确认（${evidence.map((i) => `E${i}`).join("、")}，来自文件追踪）`]] : []),
    ["简述", r.brief || "无"],
    ["读者 / 姓名", `${r.options?.audience || "直属上级"} / ${r.options?.name || "-"}`],
  ];
  if (g.chunks_total !== undefined) {
    rows.push(
      ["处理块数", `${g.chunks_processed} / ${g.chunks_total}（缓存 ${g.chunks_cached || 0}）`],
      ["工作项", `${r.items.length} 条通过核对，丢弃 ${g.dropped?.length || 0} 条，失败 ${g.failed?.length || 0} 块`],
      ["模型调用", `${g.calls} 次，输入 ${g.input_tokens} / 输出 ${g.output_tokens} tokens，耗时 ${g.seconds} 秒`],
    );
  }
  if (g.uncovered?.length) rows.push(["未写入的要点", g.uncovered.map((i) => i.point).join("；")]);
  $("#weekly-meta-body").innerHTML = `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${escapeHtml(v)}</td></tr>`).join("")}</table>`;
}

// 点击出处标签（正文或提醒里）：显示对应工作项的原文
$("#weekly-detail-panel").addEventListener("click", (e) => {
  const tag = e.target.closest(".tag");
  if (!tag || !state.weeklyReport) return;
  const r = state.weeklyReport;
  const sources = tag.dataset.tag.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);
  let html = "";
  for (const src of sources) {
    if (src === "简述") {
      html += `<p><b>来自你写的简述</b></p><div class="quote">${escapeHtml(r.brief || "（空）")}</div>`;
      continue;
    }
    const items = r.items.filter((i) => i.source === src);
    if (/^E\d+$/.test(src)) {
      html += `<p><b>已确认的工作记录 ${escapeHtml(src)}</b>（来自文件追踪）</p>`;
    } else {
      const [mid, chunk] = src.split("-");
      html += `<p><b>${escapeHtml(materialTitle(Number(mid)))} 第 ${escapeHtml(chunk || "?")} 块</b></p>`;
    }
    html += items.length
      ? items.map((i) => `<div>${escapeHtml(i.point)}</div><div class="quote">${escapeHtml(i.quote)}</div>`).join("")
      : `<p class="muted">没有找到这一出处的工作项。</p>`;
  }
  $("#source-title").textContent = `出处 ${tag.dataset.tag}`;
  $("#source-body").innerHTML = html;
  $("#source-dialog").showModal();
});

$("#show-tags").addEventListener("change", (e) => $("#weekly-preview").classList.toggle("hide-tags", !e.target.checked));

function exitEdit() {
  $("#weekly-editor").classList.add("hidden");
  $("#weekly-preview").classList.remove("hidden");
}
$("#edit-weekly").addEventListener("click", () => {
  $("#weekly-text").value = state.weeklyReport.content;
  $("#weekly-editor").classList.remove("hidden");
  $("#weekly-preview").classList.add("hidden");
});
$("#cancel-edit").addEventListener("click", exitEdit);
$("#save-weekly").addEventListener("click", async () => {
  try {
    await api(`/weekly-reports/${state.weeklyId}`, { method: "PUT", json: { content: $("#weekly-text").value } });
    exitEdit();
    await refreshWeekly();
  } catch (err) { alert(err.message); }
});

$("#delete-weekly").addEventListener("click", async () => {
  if (!confirm("删除这份周报？")) return;
  stopPolling();
  await api(`/weekly-reports/${state.weeklyId}`, { method: "DELETE" });
  $("#new-weekly").click();
});

// 打印为 PDF：只打印去掉出处标记的正文（提醒不进入导出）
$("#print-weekly").addEventListener("click", () => {
  $("#print-area").innerHTML = renderMarkdown(stripTags(state.weeklyReport.content));
  window.print();
});

// ---------- 提醒（文件追踪：周报已准备好等）----------
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
      <button class="ghost" data-read="${n.id}">知道了</button>
    </div>`).join("");
}

$("#inbox").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-read]");
  if (!b) return;
  await api("/notifications/read", { method: "POST", json: { ids: [Number(b.dataset.read)] } });
  loadInbox();
});

// ---------- 启动 ----------
(async function init() {
  try {
    await Promise.all([loadSkills(), loadMaterials(), loadSessions(), loadInbox()]);
    setInterval(loadInbox, 60000);
  } catch (err) {
    showNotice($("#context-box"), `连接后端失败：${escapeHtml(err.message)}`, "error");
  }
})();
