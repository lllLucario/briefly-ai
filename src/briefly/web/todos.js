// 待办页：两级待办（父待办 + 子待办）、状态、材料（上传或从材料库选择，只存一份）、进展与活动、相关周报。
// 依赖 app.js 里的 $、api、state、escapeHtml、showNotice、fmtTime、fileIcon、ui*、loadMaterials、showMaterialDetail、pickMaterials，
// 以及 weekly.js 里的 gotoWeek。
"use strict";

const todos = { list: [], id: null, filter: "open", detail: null, detailOpen: false, detailView: 0, expanded: new Set() };
const TODO_STATUS = { todo: "未开始", doing: "进行中", done: "已完成" };
const TRACK_STATUS = {
  present: ["追踪中", "ok"],
  unreachable: ["无法访问", "bad"], // 权限被撤销、网络问题等：不等于文件被删除
  missing: ["文件已不在", "bad"],
  untracked: ["已停止追踪", ""],
};

$("#todo-week").addEventListener("click", () => gotoWeek());

function todoMsg(html, kind = "") {
  showNotice($("#todo-msg"), html, kind);
}

function shortDay(iso) {
  if (!iso) return "";
  const [, m, d] = iso.slice(0, 10).split("-");
  return `${Number(m)}月${Number(d)}日`;
}

// ---------- 横向任务列表，原位展开概览，明确按钮打开详情弹窗
async function loadTodos() {
  const archived = todos.filter === "archived";
  try {
    todos.list = await api(`/todos?archived=${archived}`);
    $("#todo-load-msg").classList.add("hidden");
  } catch (err) {
    showNotice($("#todo-load-msg"), `加载待办失败：${escapeHtml(err.message)} <button class="ghost small" data-retry-todos>重试</button>`, "error");
    return;
  }
  renderTodoList();
  if (todos.detailOpen && todos.id && todos.list.some((t) => t.id === todos.id)) openTodo(todos.id, false);
}
$("#todo-load-msg").addEventListener("click", (e) => {
  if (e.target.closest("[data-retry-todos]")) loadTodos();
});

function visibleTodos() {
  const rows = todos.list.filter((t) => (todos.filter === "archived" ? t.archived_at : !t.archived_at));
  if (todos.filter !== "open") return rows;
  // 未完成：隐藏已完成的待办，但已完成的父待办下还有未完成的子待办时仍显示父待办
  const openChild = new Set(rows.filter((t) => t.parent_id && t.status !== "done").map((t) => t.parent_id));
  return rows.filter((t) => t.status !== "done" || openChild.has(t.id));
}

function todoStatusMark(t) {
  const status = t.archived_at ? "已归档" : TODO_STATUS[t.status];
  return `<span class="ticket-status" title="${status}" aria-label="${status}"><span class="card-status-icon" aria-hidden="true">${t.status === "done" ? "✓" : ""}</span></span>`;
}

function todoItem(t) {
  const parent = t.parent_id ? todos.list.find((x) => x.id === t.parent_id) : null;
  const points = String(t.description || "").split(/\n+/).map((x) => x.trim()).filter(Boolean);
  const children = todos.list.filter((x) => x.parent_id === t.id);
  const summary = points[0] || "暂无任务说明";
  return `<li data-todo="${t.id}" class="${t.status} ${t.archived_at ? "archived" : ""} ${t.id === todos.id ? "active" : ""}">
    <details class="todo-ticket" data-ticket="${t.id}" ${todos.expanded.has(t.id) ? "open" : ""}>
      <summary class="todo-ticket-row">
        <span class="ticket-chevron" aria-hidden="true">›</span>
        <span class="ticket-copy"><b class="todo-name">${escapeHtml(t.title)}</b><span class="ticket-subtitle">${escapeHtml(summary)}</span></span>
        <span class="ticket-meta">${t.due_date ? `<span class="ticket-due">${shortDay(t.due_date)}</span>` : ""}<span class="ticket-child-count">${t.children_total || 0} 项子待办</span></span>
        ${todoStatusMark(t)}
      </summary>
      <div class="ticket-expanded">
        <div class="ticket-overview-head"><span class="eyebrow">${parent ? `所属主任务 · ${escapeHtml(parent.title)}` : "主任务概览"}</span><button type="button" class="ghost small" data-inline-detail="${t.id}">展开详情 ↗</button></div>
        <div class="ticket-overview-grid">
          <section><h4>任务要点 <small>原始说明</small></h4>${points.length ? `<ul class="ticket-points">${points.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : '<p class="muted small">暂无说明，可在详情中补充。</p>'}</section>
          <section><div class="section-head"><h4>子待办 <small>${t.children_done || 0} / ${t.children_total || 0} 项已完成</small></h4></div>
          ${children.length ? `<ul class="inline-children">${children.map((c) => `<li class="${c.status}"><button type="button" data-inline-detail="${c.id}" aria-label="查看子待办详情：${escapeHtml(c.title)}"><span class="inline-child-title">${escapeHtml(c.title)}</span><span class="inline-child-hint">详情 ↗</span>${todoStatusMark(c)}</button></li>`).join("")}</ul>` : '<p class="muted small">暂无子待办，可在详情中添加。</p>'}</section>
        </div>
      </div>
    </details></li>`;
}

function renderTodoList() {
  const rows = visibleTodos();
  const ids = new Set(rows.map((t) => t.id));
  const top = rows.filter((t) => !t.parent_id || !ids.has(t.parent_id));
  const html = top.map(todoItem).join("");
  const empty = { open: "没有未完成的待办", all: "还没有待办", archived: "没有归档的待办" }[todos.filter];
  $("#todo-list").innerHTML = html || `<li class="cards-empty muted">${empty}</li>`;
  $("#todo-empty-panel").classList.toggle("hidden", Boolean(top.length));
  $("#todo-count").textContent = top.length ? `${top.length} 项工作 · 点击任务行展开要点和子待办` : "把工作放在眼前，一件一件向前推进。";
}

$("#todo-list").addEventListener("click", (e) => {
  const button = e.target.closest("[data-inline-detail]");
  if (button) openTodo(Number(button.dataset.inlineDetail));
});
$("#todo-list").addEventListener("toggle", (e) => {
  const detail = e.target;
  if (!detail.isConnected || !detail.matches("details[data-ticket]")) return;
  const id = Number(detail.dataset.ticket);
  if (detail.open) todos.expanded.add(id);
  else todos.expanded.delete(id);
}, true);
$("#todo-filter").addEventListener("change", (e) => {
  todos.filter = e.target.value;
  loadTodos();
});

$("#new-todo").addEventListener("click", async () => {
  const r = await uiPrompt({
    title: "新建待办",
    fields: [
      { name: "title", label: "标题", required: true, placeholder: "如 支付改造上线" },
      { name: "description", label: "说明（可选）", type: "textarea", placeholder: "工作背景和目标" },
      { name: "due", label: "截止日期（可选）", type: "date" },
    ],
    ok: "创建",
  });
  if (!r) return;
  try {
    const t = await api("/todos", { method: "POST", json: { title: r.title, description: r.description, due_date: r.due || null } });
    if (todos.filter === "archived") { todos.filter = "open"; $("#todo-filter").value = "open"; }
    todos.id = t.id;
    await loadTodos();
    await openTodo(t.id);
  } catch (err) { uiAlert(err.message, "出错了"); }
});

// ---------- 详情
async function openTodo(id, scroll = true) {
  const view = ++todos.detailView;
  todos.id = id;
  todos.detailOpen = true;
  renderTodoList();
  if (!$("#todo-panel").open) $("#todo-panel").showModal();
  $("#todo-title").textContent = "正在加载…";
  $("#todo-panel").setAttribute("aria-busy", "true");
  $("#todo-detail-body").inert = true;
  $("#todo-detail-body").classList.add("hidden");
  showNotice($("#todo-loading"), "正在读取任务详情…");
  if (scroll) $("#todo-msg").classList.add("hidden");
  try {
    const [t, timeline, reports] = await Promise.all([api(`/todos/${id}`), api(`/todos/${id}/timeline`), api(`/todos/${id}/reports`)]);
    if (todos.id !== id || !todos.detailOpen || todos.detailView !== view) return;
    todos.detail = t;
    renderTodo(t, timeline, reports);
    $("#todo-detail-body").inert = false;
    $("#todo-detail-body").classList.remove("hidden");
    $("#todo-loading").classList.add("hidden");
  } catch (err) { if (todos.detailView === view && todos.detailOpen) showNotice($("#todo-loading"), `加载任务失败：${escapeHtml(err.message)}。请关闭后重新打开。`, "error"); }
  finally { if (todos.detailView === view) $("#todo-panel").removeAttribute("aria-busy"); }
}

$("#todo-close").addEventListener("click", () => $("#todo-panel").close());
$("#todo-panel").addEventListener("close", () => { todos.detailOpen = false; todos.detailView++; });

function renderTodo(t, timeline, reports) {
  const archived = Boolean(t.archived_at);
  $("#todo-title").textContent = t.title;
  $("#todo-status").value = t.status;
  $("#todo-status").disabled = archived;
  $("#todo-edit").disabled = archived;
  $("#todo-promote").classList.toggle("hidden", !t.parent_id || archived);
  $("#todo-archive").classList.toggle("hidden", archived);
  $("#todo-restore").classList.toggle("hidden", !archived);
  ["#todo-add-child", "#todo-pick", "#todo-note"].forEach((s) => ($(s).disabled = archived));
  $("#todo-upload").disabled = archived;

  const rows = [
    ["说明", t.description ? escapeHtml(t.description) : `<span class="muted">无</span>`],
    ...(t.parent ? [["父待办", `<a data-open-todo="${t.parent.id}">${escapeHtml(t.parent.title)}</a>（${TODO_STATUS[t.parent.status]}）`]] : []),
    ["截止日期", t.due_date ? shortDay(t.due_date) + `（${t.due_date}）` : `<span class="muted">未设置</span>`],
    ["时间", `创建 ${fmtTime(t.created_at)}${t.completed_at ? ` · 完成 ${fmtTime(t.completed_at)}` : ""}${archived ? ` · 归档 ${fmtTime(t.archived_at)}` : ""}`],
  ];
  $("#todo-info").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  // 子待办：子待办自己不能再有子待办
  $("#todo-children-box").classList.toggle("hidden", Boolean(t.parent_id));
  $("#todo-progress").textContent = t.children_total ? `${t.children_done}/${t.children_total} 个子待办已完成` : "";
  $("#todo-children").innerHTML = t.children.length
    ? t.children.map((k) => `<button type="button" class="child-card" data-open-todo="${k.id}"><span class="todo-dot ${k.status}"></span><span class="grow">${escapeHtml(k.title)}</span>
        <span class="badge">${TODO_STATUS[k.status]}</span>${k.due_date ? `<span class="muted">截止 ${shortDay(k.due_date)}</span>` : ""}<span aria-hidden="true">→</span></button>`).join("")
    : `<p class="muted small">没有子待办。大的工作可以拆成几个子待办，各自有状态和截止日期。</p>`;

  renderTodoMaterials(t);
  renderTimeline(timeline);
  $("#todo-reports").innerHTML = reports.length
    ? reports.map((r) => `<div class="todo-row"><a class="grow" data-open-weekly="${r.id}">周报 ${r.period_start} ~ ${r.period_end}</a>
        ${r.refs.map((x) => `<span class="badge" title="生成周报时的记录">${escapeHtml(x.evidence?.title || x.group)}${x.relation === "material" ? "（关联文件）" : ""}</span>`).join("")}</div>`).join("")
    : `<p class="muted small">还没有周报用到这个待办。</p>`;
}

function trackBadges(m) {
  if (!m.tracking) {
    const why = m.track_key ? "这篇飞书文档还没有开启追踪，可以在“材料”页开启" : "上传、粘贴的是一份副本，不追踪原文件";
    return `<span class="badge" title="${escapeHtml(why)}">${m.track_key ? "未追踪" : "副本"}</span>`;
  }
  const [text, cls] = TRACK_STATUS[m.tracking.status] || [m.tracking.status, ""];
  return `<span class="badge ${cls}" title="${escapeHtml(m.tracking.last_error || "变化会进入周报的审核清单")}">${text}</span>`;
}

function materialRow(m, removable) {
  const open = m.current_id ? `<a class="grow" data-open-material="${m.current_id}">${escapeHtml(m.title)}</a>` : `<span class="grow muted">${escapeHtml(m.title)}</span>`;
  const latest = m.current_id && m.current_id !== m.material_id ? `<span class="badge" title="随追踪更新，打开的是最新版本">最新版本</span>` : "";
  return `<div class="todo-row">${fileIcon(m.file_type || "paste")}${open}${latest}${trackBadges(m)}
    ${removable ? `<button class="ghost small" data-unlink="${m.material_id}" title="只解除关联，材料仍在材料库">解除关联</button>` : ""}</div>`;
}

function renderTodoMaterials(t) {
  const direct = t.material_list.filter((m) => m.relation === "direct");
  const shared = t.material_list.filter((m) => m.relation === "shared");
  let html = direct.length ? direct.map((m) => materialRow(m, !t.archived_at)).join("") : `<p class="muted small">还没有关联材料。</p>`;
  if (shared.length) {
    html += `<div class="mat-group">来自父待办“${escapeHtml(t.parent?.title || "")}”的共享材料（背景资料；它们的修改不算这个待办的进展）</div>`
      + shared.map((m) => materialRow(m, false)).join("");
  }
  $("#todo-materials").innerHTML = html;
}

const EVENT_TEXT = {
  created: () => "新建",
  updated: (a) => `修改了${Object.keys(a.new_value || {}).map((k) => ({ title: "标题", description: "说明", due_date: "截止日期", parent_id: "父待办" }[k] || k)).join("、")}`,
  status: (a) => `状态：${TODO_STATUS[a.old_value?.status] || ""} → ${TODO_STATUS[a.new_value?.status] || ""}`,
  completed: () => "标记为已完成",
  reopened: (a) => `重新打开（${TODO_STATUS[a.new_value?.status] || ""}）`,
  note: (a) => `进展：${a.note}`,
  child_added: (a) => `添加子待办“${a.new_value?.title || ""}”`,
  child_removed: (a) => `子待办“${a.new_value?.title || ""}”转为独立待办`,
  promoted: (a) => `从“${a.old_value?.parent_title || ""}”下转为独立待办`,
  material_linked: (a) => `关联材料《${a.new_value?.title || a.material_id}》`,
  material_unlinked: (a) => `解除关联《${a.old_value?.title || a.material_id}》`,
  material_deleted: (a) => `材料《${a.old_value?.title || a.material_id}》已从材料库删除`,
  archived: (a) => `归档${a.note ? `（${a.note}）` : ""}`,
  restored: () => "恢复",
  status_synced: (a) => `周报审核中确认状态为${TODO_STATUS[a.new_value?.status] || ""}`,
};
const CHANGE_TEXT = { MODIFIED: "修改", CREATED: "新出现", DELETED: "被删除", POSSIBLY_MOVED: "可能移动/改名" };

function renderTimeline(items) {
  const rows = [...items].reverse().map((x) => {
    if (x.kind === "file_change") {
      const name = String(x.path).replace(/\\/g, "/").split("/").pop();
      const versions = x.to_version ? ` v${x.from_version || "-"}→v${x.to_version}` : "";
      const shared = x.relation === "shared" ? "（父待办的共享材料）" : "";
      return `<div class="tl file"><time>${fmtTime(x.at)}</time><span>文件《${escapeHtml(name)}》${CHANGE_TEXT[x.change_type] || x.change_type}${versions}${shared}</span></div>`;
    }
    const text = (EVENT_TEXT[x.event_type] || (() => x.event_type))(x);
    return `<div class="tl"><time>${fmtTime(x.at)}</time><span>${escapeHtml(text)}</span></div>`;
  });
  $("#todo-timeline").innerHTML = rows.join("") || `<p class="muted small">还没有活动。</p>`;
}

$("#todo-panel").addEventListener("click", async (e) => {
  const openT = e.target.closest("[data-open-todo]");
  if (openT) return openTodo(Number(openT.dataset.openTodo));
  const openM = e.target.closest("[data-open-material]");
  if (openM) {
    document.querySelector('.tabs button[data-tab="materials"]').click();
    return showMaterialDetail(Number(openM.dataset.openMaterial));
  }
  const openW = e.target.closest("[data-open-weekly]");
  if (openW) {
    return gotoWeek(Number(openW.dataset.openWeekly));
  }
  const unlink = e.target.closest("[data-unlink]");
  if (unlink) {
    if (!(await uiConfirm("只解除和这个待办的关联，材料、文件和追踪历史都保留。", { title: "解除关联", ok: "解除关联" }))) return;
    try {
      await api(`/todos/${todos.id}/materials/${unlink.dataset.unlink}`, { method: "DELETE" });
      openTodo(todos.id, false);
      loadTodos();
    } catch (err) { todoMsg(escapeHtml(err.message), "error"); }
  }
});

// ---------- 修改
async function withConfirm(call, title) {
  try {
    return await call(false);
  } catch (err) {
    if (!err.needsConfirm) throw err;
    if (!(await uiConfirm(err.message, { title, ok: "确定" }))) return null;
    return call(true);
  }
}

$("#todo-status").addEventListener("change", async (e) => {
  const status = e.target.value;
  try {
    const t = await withConfirm((confirm) => api(`/todos/${todos.id}/status`, { method: "POST", json: { status, confirm } }), "完成父待办");
    if (!t) { e.target.value = todos.detail.status; return; }
    await loadTodos();
    todoMsg(t.notices.length ? t.notices.map(escapeHtml).join("<br>") : `状态已改为${TODO_STATUS[status]}。`, t.notices.length ? "warn" : "");
  } catch (err) {
    e.target.value = todos.detail.status;
    todoMsg(escapeHtml(err.message), "error");
  }
});

$("#todo-edit").addEventListener("click", async () => {
  const t = todos.detail;
  const fields = [
    { name: "title", label: "标题", required: true, value: t.title },
    { name: "description", label: "说明", type: "textarea", value: t.description },
    { name: "due", label: "截止日期", type: "date", value: t.due_date || "" },
  ];
  const r = await uiPrompt({ title: "编辑待办", fields, ok: "保存" });
  if (!r) return;
  const body = { title: r.title, description: r.description, due_date: r.due || null };
  try {
    await api(`/todos/${t.id}`, { method: "PUT", json: body });
    await loadTodos();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#todo-add-child").addEventListener("click", async () => {
  const r = await uiPrompt({
    title: `给“${todos.detail.title}”添加子待办`,
    fields: [
      { name: "title", label: "标题", required: true },
      { name: "description", label: "说明（可选）", type: "textarea" },
      { name: "due", label: "截止日期（可选，可以和父待办不同）", type: "date" },
    ],
    ok: "添加",
  });
  if (!r) return;
  try {
    await api("/todos", { method: "POST", json: { title: r.title, description: r.description, due_date: r.due || null, parent_id: todos.id } });
    await loadTodos();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#todo-promote").addEventListener("click", async () => {
  const t = todos.detail;
  const ok = await uiConfirm(`把“${t.title}”从“${t.parent?.title}”下移出，变成独立待办。\n编号、状态、直接关联的材料、活动和历史周报都保留；父待办的共享材料不会复制过来，需要的话可以再关联。`, { title: "转为独立待办", ok: "转为独立待办" });
  if (!ok) return;
  try {
    await api(`/todos/${t.id}/promote`, { method: "POST" });
    await loadTodos();
    todoMsg("已转为独立待办，现在可以在它下面添加子待办。");
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#todo-archive").addEventListener("click", async () => {
  const t = todos.detail;
  let children = null;
  if (t.children.length) {
    const r = await uiPrompt({
      title: "归档待办",
      message: `“${escapeHtml(t.title)}”下还有 ${t.children.length} 个子待办，怎么处理它们？`,
      fields: [{ name: "children", label: "子待办", type: "select", options: [{ value: "promote", label: "转为独立待办，保留" }, { value: "archive", label: "一起归档" }] }],
      ok: "归档",
      danger: true,
    });
    if (!r) return;
    children = r.children;
  } else if (!(await uiConfirm(`归档“${t.title}”？关联的材料、活动和周报记录都保留，以后可以在“已归档”里恢复。`, { title: "归档待办", ok: "归档", danger: true }))) return;
  try {
    await api(`/todos/${t.id}${children ? `?children=${children}` : ""}`, { method: "DELETE" });
    await loadTodos();
    renderTodoList();
    openTodo(t.id, false);
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#todo-restore").addEventListener("click", async () => {
  try {
    await api(`/todos/${todos.id}/restore`, { method: "POST" });
    todos.filter = "open";
    $("#todo-filter").value = "open";
    await loadTodos();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#todo-note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#todo-note").value.trim();
  if (!text) return;
  const button = e.target.querySelector("button"); button.disabled = true;
  try {
    const status = $("#todo-note-status").value || null;
    const saved = await withConfirm((confirm) => api(`/todos/${todos.id}/notes`, { method: "POST", json: { text, status, confirm } }), "记录进展并更新状态");
    if (!saved) return;
    $("#todo-note").value = "";
    $("#todo-note-status").value = "";
    await loadTodos();
    openTodo(todos.id, false);
    todoMsg(saved.notices?.length ? saved.notices.map(escapeHtml).join("<br>") : "进展已记录。", saved.notices?.length ? "warn" : "");
  } catch (err) { todoMsg(escapeHtml(err.message), "error"); }
  finally { button.disabled = false; }
});

// ---------- 材料：上传（存进材料库并关联）、从材料库选择（只建关联）
$("#todo-upload").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  const lines = [];
  for (const f of files) {
    todoMsg(`正在上传并解析《${escapeHtml(f.name)}》…`);
    const form = new FormData();
    form.append("file", f);
    try {
      const r = await api(`/todos/${todos.id}/materials/upload`, { method: "POST", body: form });
      lines.push(`《${escapeHtml(f.name)}》${r.new_material ? "已存进材料库并关联" : "材料库里已有相同内容，直接关联了已有的那份"}`);
    } catch (err) { lines.push(`《${escapeHtml(f.name)}》失败：${escapeHtml(err.message)}`); }
  }
  todoMsg(lines.join("<br>"));
  openTodo(todos.id, false);
  loadMaterials();
  loadTodos();
});

$("#todo-pick").addEventListener("click", async () => {
  const linked = todos.detail.material_list.filter((m) => m.relation === "direct").flatMap((m) => [m.material_id, m.current_id]);
  const ids = await pickMaterials({ title: "从材料库选择", ok: "关联", exclude: linked });
  if (!ids?.length) return;
  try {
    for (const id of ids) await api(`/todos/${todos.id}/materials`, { method: "POST", json: { material_id: id } });
    todoMsg(`已关联 ${ids.length} 份材料（没有复制，材料库里仍是同一份）。`);
  } catch (err) { todoMsg(escapeHtml(err.message), "error"); }
  openTodo(todos.id, false);
  loadTodos();
});
