// 待办页：两级待办（父待办 + 子待办）、状态、材料（上传或从材料库选择，只存一份）、进展与活动、相关周报。
// 依赖 app.js 里的 $、api、state、escapeHtml、showNotice、fmtTime、fileIcon、ui*、loadMaterials、showMaterialDetail、pickMaterials，
// 以及 weekly.js 里的 gotoWeek。
"use strict";

const todos = { list: [], id: null, filter: "open", detail: null, detailOpen: false, detailView: 0, editMode: false, editSaving: false, editDrafts: new Map(), expanded: new Set(), inlineBusy: false, inlineDrafts: new Map(), noteDrafts: new Map(), noteOpen: new Set(), timeline: [], timelineLimit: 5, inlineNotices: new Map() };
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
    return false;
  }
  renderTodoList();
  if (todos.detailOpen && todos.id && todos.list.some((t) => t.id === todos.id)) openTodo(todos.id, false);
  return true;
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

function statusIcon(status) {
  const shapes = {
    todo: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.7"/>',
    doing: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 5a7 7 0 0 1 0 14z" fill="currentColor"/>',
    done: '<circle cx="12" cy="12" r="9" fill="currentColor"/><path d="m7.5 12 3 3 6-6" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    remove: '<circle cx="12" cy="12" r="9" fill="currentColor"/><path d="m9 9 6 6m0-6-6 6" stroke="white" stroke-width="1.8" stroke-linecap="round"/>'
  };
  return `<svg class="status-symbol ${status}" viewBox="0 0 24 24" aria-hidden="true">${shapes[status]}</svg>`;
}
function todoStatusMark(t) {
  const status = t.archived_at ? "已归档" : TODO_STATUS[t.status];
  return `<button type="button" class="ticket-status" data-status-menu="${t.id}" title="更改状态：${status}" aria-label="更改${escapeHtml(t.title)}的状态：${status}" aria-haspopup="menu" aria-expanded="false" ${t.archived_at ? "disabled" : ""}>${statusIcon(t.status)}</button>`;
}
function childProgress(t) {
  if (!t.children_total) return "";
  return `<span class="ticket-progress"><progress max="${t.children_total}" value="${t.children_done}" aria-label="子待办完成数量"></progress><span>已完成 ${t.children_done} / ${t.children_total} 个子待办</span></span>`;
}

function defaultChildDueDate(now = new Date()) {
  const day = new Date(now);
  if (day.getHours() * 60 + day.getMinutes() >= 15 * 60 + 30) day.setDate(day.getDate() + 1);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

function inlineChildren(t, children) {
  const draft = todos.inlineDrafts.get(t.id) || {title:"", due:defaultChildDueDate()};
  const locked = Boolean(t.archived_at);
  const notice = todos.inlineNotices.get(t.id);
  return `<fieldset class="inline-child-editor" data-inline-editor="${t.id}" data-locked="${locked ? "1" : "0"}" ${locked || todos.inlineBusy ? "disabled" : ""}><legend class="sr-only">${escapeHtml(t.title)}的子待办操作</legend>
    ${children.length ? `<ul class="inline-children">${children.map((c) => `<li class="${c.status}"><div class="inline-child-row"><button class="inline-child-open" type="button" data-inline-detail="${c.id}" aria-label="查看子待办详情：${escapeHtml(c.title)}"><span class="inline-child-title">${escapeHtml(c.title)}</span></button>${todoStatusMark(c)}</div></li>`).join("")}</ul>` : '<p class="muted small">暂无子待办。</p>'}
    ${!t.parent_id && !locked ? `<form class="inline-add-child" data-inline-add="${t.id}"><label class="sr-only" for="inline-child-title-${t.id}">子待办标题</label><div class="inline-child-input"><input id="inline-child-title-${t.id}" name="title" required maxlength="200" placeholder="添加一条子待办…" value="${escapeHtml(draft.title)}"><button type="submit" class="inline-child-submit" title="添加子待办（回车提交）" aria-label="添加子待办"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M7 12h10" fill="none" stroke-width="1.8" stroke-linecap="round"/></svg></button></div><input name="due" type="date" aria-label="新子待办截止日期（可选）" title="截止日期（可选）" value="${escapeHtml(draft.due)}"></form>` : ""}
    </fieldset><div class="notice inline-child-notice ${notice ? notice.kind : "hidden"}" data-inline-notice="${t.id}" role="status">${notice ? escapeHtml(notice.text) : ""}</div>`;
}

function noteToggleContent(open) {
  return `<span class="note-action-icon">${uiIcon(open ? "close" : "pen")}</span><span class="note-action-copy"><b>${open ? "收起输入" : "追加进展"}</b><small>${open ? "填写后保存到活动记录" : "记录本次推进，保留工作轨迹"}</small></span>${uiIcon("arrow")}`;
}
function descriptionToggleContent(open) {
  return `${open ? "收起说明" : "展开完整说明"}${uiIcon("arrow")}`;
}

function todoItem(t) {
  const parent = t.parent_id ? todos.list.find((x) => x.id === t.parent_id) : null;
  const points = String(t.description || "").split(/\n+/).map((x) => x.trim()).filter(Boolean);
  const children = todos.list.filter((x) => x.parent_id === t.id);
  const description = String(t.description || "");
  const canCollapse = description.length > 100 || points.length > 3;
  const descriptionOpen = disclosureState.get(`todo:${t.id}:description`) === true;
  const noteOpen = todos.noteOpen.has(t.id);
  const summary = t.latest_progress ? `最近进展 · ${fmtTime(t.latest_progress.at)}：${t.latest_progress.text}` : points[0] || "暂无任务说明";
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
  const overdue = t.due_date && t.due_date < today && t.status !== "done" && !t.archived_at;
  return `<li data-todo="${t.id}" class="${t.status} ${t.archived_at ? "archived" : ""} ${t.id === todos.id ? "active" : ""}">
    <details class="todo-ticket" data-ticket="${t.id}" ${todos.expanded.has(t.id) ? "open" : ""}>
      <summary class="todo-ticket-row">
        <span class="ticket-chevron" aria-hidden="true">›</span>
        <span class="ticket-copy"><b class="todo-name">${escapeHtml(t.title)}</b><span class="ticket-subtitle" title="${escapeHtml(summary)}">${escapeHtml(summary)}</span></span>
        <span class="ticket-meta">${t.due_date ? `<span class="ticket-due ${overdue ? "overdue" : ""}">${overdue ? "已逾期 · " : ""}${shortDay(t.due_date)}</span>` : ""}${childProgress(t)}</span>
        ${todoStatusMark(t)}
      </summary>
      <div class="ticket-expanded">
        <div class="ticket-overview-head"><span class="eyebrow">${parent ? `所属主任务 · ${escapeHtml(parent.title)}` : "任务内容"}</span><button type="button" class="ghost small ticket-detail-button" data-inline-detail="${t.id}">查看详情 ${uiIcon("arrow")}</button></div>
        <div class="ticket-overview-grid">
          <section class="ticket-main-content"><div class="ticket-section-heading"><span class="ticket-section-icon">${uiIcon("report")}</span><h4>原始说明</h4></div><div class="ticket-description-surface">
            <div id="ticket-description-${t.id}" class="ticket-description ${canCollapse && !descriptionOpen ? "is-collapsed" : ""}">${description.trim() ? escapeHtml(description) : '<span class="muted">暂无说明，可在详情中补充。</span>'}</div>
            ${canCollapse ? `<button type="button" class="ghost small description-toggle" data-description-toggle="${t.id}" aria-controls="ticket-description-${t.id}" aria-expanded="${descriptionOpen}">${descriptionToggleContent(descriptionOpen)}</button>` : ""}
            </div><div class="ticket-source-meta">${uiIcon("folder")}<span>关联材料 <b>${t.materials || 0}</b> 份</span><span class="muted">· 来源与历史可在详情查看</span></div>
            ${!t.archived_at ? `<button type="button" class="ghost small note-toggle" data-note-toggle="${t.id}" aria-controls="inline-note-form-${t.id}" aria-expanded="${noteOpen}">${noteToggleContent(noteOpen)}</button>
            <form id="inline-note-form-${t.id}" class="inline-note-form stack ${noteOpen ? "" : "hidden"}" data-inline-note="${t.id}"><div class="note-form-heading"><b>本次进展</b><span>不改变任务状态</span></div><label class="sr-only" for="inline-note-${t.id}">追加进展</label><textarea id="inline-note-${t.id}" name="note" maxlength="500" rows="3" required placeholder="补充本次进展，不修改任务状态" ${!noteOpen || todos.inlineBusy ? "disabled" : ""}>${escapeHtml(todos.noteDrafts.get(t.id) || "")}</textarea><div class="row gap"><button type="submit" class="primary small" ${todos.inlineBusy ? "disabled" : ""}>保存进展</button><button type="button" class="ghost small" data-note-cancel="${t.id}" ${todos.inlineBusy ? "disabled" : ""}>取消</button></div></form>` : ""}
          </section>
          <section class="ticket-children-content"><div class="ticket-section-heading"><span class="ticket-section-icon">${uiIcon("check")}</span><h4>子待办</h4><span class="child-summary">${t.children_done || 0} / ${t.children_total || 0} 项已完成</span></div>
          ${inlineChildren(t, children)}</section>
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
  if (typeof statusMenu !== "undefined" && statusMenu.matches(":popover-open")) statusMenu.hidePopover();
  $("#todo-list").innerHTML = html || `<li class="cards-empty muted">${empty}</li>`;
  $("#todo-empty-panel").classList.toggle("hidden", Boolean(top.length));
  $("#todo-count").textContent = top.length ? `${top.length} 项工作 · 点击任务行展开要点和子待办` : "把工作放在眼前，一件一件向前推进。";
}

$("#todo-list").addEventListener("click", (e) => {
  const description = e.target.closest("[data-description-toggle]");
  if (description) {
    const id = description.dataset.descriptionToggle;
    const open = description.getAttribute("aria-expanded") !== "true";
    disclosureState.set(`todo:${id}:description`, open);
    document.getElementById(`ticket-description-${id}`).classList.toggle("is-collapsed", !open);
    description.setAttribute("aria-expanded", String(open));
    description.innerHTML = descriptionToggleContent(open);
    return;
  }
  const note = e.target.closest("[data-note-toggle], [data-note-cancel]");
  if (note) {
    if (todos.inlineBusy) return;
    const id = Number(note.dataset.noteToggle || note.dataset.noteCancel);
    const form = document.getElementById(`inline-note-form-${id}`);
    const trigger = document.querySelector(`[data-note-toggle="${id}"]`);
    const open = !note.hasAttribute("data-note-cancel") && !todos.noteOpen.has(id);
    if (note.hasAttribute("data-note-cancel")) { todos.noteDrafts.delete(id); form.elements.note.value = ""; }
    open ? todos.noteOpen.add(id) : todos.noteOpen.delete(id);
    form.classList.toggle("hidden", !open);
    form.elements.note.disabled = !open;
    trigger.setAttribute("aria-expanded", String(open));
    trigger.innerHTML = noteToggleContent(open);
    if (open) form.elements.note.focus(); else trigger.focus();
    return;
  }
  const button = e.target.closest("[data-inline-detail]");
  if (button) openTodo(Number(button.dataset.inlineDetail));
  const trigger = e.target.closest("[data-status-menu]");
  if (trigger) { e.preventDefault(); e.stopPropagation(); openStatusMenu(trigger); }

});

function inlineNotice(parent, text, kind = "") {
  todos.inlineNotices.set(parent, {text, kind});
  const box = document.querySelector(`[data-inline-notice="${parent}"]`);
  if (box) showNotice(box, escapeHtml(text), kind);
}

async function saveInlineChild(parent, action, success) {
  if (todos.inlineBusy) return;
  todos.inlineBusy = true;
  document.querySelectorAll("[data-inline-editor]").forEach((el) => el.disabled = true);
  inlineNotice(parent, "正在保存…");
  try {
    const result = await action();
    if (result === null) { inlineNotice(parent, "操作已取消。"); return; }
    const refreshed = await loadTodos();
    inlineNotice(parent, refreshed ? success : "已保存，但列表刷新失败。请点击列表上方的重试。", refreshed ? "" : "warn");
    return refreshed;
  } catch (err) {
    showNotice($("#todo-load-msg"), escapeHtml(err.message), "error");
    if (todos.detailOpen) todoMsg(escapeHtml(err.message), "error");
    todos.inlineNotices.set(parent, {text:err.message, kind:"error"});
    renderTodoList(); // 失败时恢复服务端原状态；新增草稿单独保存。
  } finally {
    todos.inlineBusy = false;
    document.querySelectorAll("[data-inline-editor]").forEach((el) => el.disabled = el.dataset.locked === "1");
  }
}

function rememberInlineDraft(e) {
  const form = e.target.closest("[data-inline-add]");
  if (form) todos.inlineDrafts.set(Number(form.dataset.inlineAdd), {title:form.elements.title.value, due:form.elements.due.value});
}
$("#todo-list").addEventListener("input", rememberInlineDraft);
$("#todo-list").addEventListener("change", rememberInlineDraft);
$("#todo-list").addEventListener("focusin", (e) => {
  const form = e.target.closest("[data-inline-add]");
  // 首次开始填写时重新取本机日期，避免页面打开很久后仍沿用旧默认值。
  if (form && !todos.inlineDrafts.has(Number(form.dataset.inlineAdd))) form.elements.due.value = defaultChildDueDate();
});
$("#todo-list").addEventListener("submit", (e) => {
  const form = e.target.closest("[data-inline-add]");
  if (!form) return;
  e.preventDefault();
  rememberInlineDraft({target:form});
  const parent = Number(form.dataset.inlineAdd), draft = todos.inlineDrafts.get(parent);
  if (!draft?.title.trim()) return inlineNotice(parent, "请填写子待办标题。", "error");
  saveInlineChild(parent, async () => {
    await api("/todos", {method:"POST", json:{parent_id:parent, title:draft.title.trim(), status:"todo", due_date:draft.due || null}});
    todos.inlineDrafts.delete(parent);
  }, "子待办已添加。");
});

// 单个浮层菜单，避免列表重绘或卡片 overflow 裁切菜单。
const statusMenu = document.createElement("div");
statusMenu.id = "todo-status-menu";
statusMenu.className = "todo-status-menu";
statusMenu.setAttribute("popover", "auto");
statusMenu.setAttribute("role", "menu");
statusMenu.setAttribute("aria-label", "更改待办状态");
document.body.append(statusMenu);
let statusTarget = null, statusTrigger = null;
function openStatusMenu(trigger) {
  if (todos.inlineBusy) return;
  const task = todos.list.find((t) => t.id === Number(trigger.dataset.statusMenu));
  if (!task || task.archived_at) return;
  if (statusMenu.matches(":popover-open")) statusMenu.hidePopover();
  // 模态框之外的 DOM 会变成 inert；菜单挂在触发者所在弹窗内才可操作。
  (trigger.closest("dialog") || document.body).append(statusMenu);
  statusTarget = task.id; statusTrigger = trigger;
  statusMenu.classList.remove("popconfirm");
  statusMenu.setAttribute("role", "menu");
  statusMenu.setAttribute("aria-label", "更改待办状态");
  statusMenu.innerHTML = `<div class="status-menu-label">更改状态</div>` + Object.entries(TODO_STATUS).map(([key,label]) => `<button type="button" role="menuitemradio" aria-checked="${task.status === key}" data-status-choice="${key}">${statusIcon(key)}<span>${label}</span><span class="status-check">${task.status === key ? "✓" : ""}</span></button>`).join("") + `<button type="button" role="menuitem" class="status-remove" data-status-choice="remove">${statusIcon("remove")}<span>移除</span></button>`;
  statusMenu.showPopover();
  positionStatusMenu();
  trigger.setAttribute("aria-expanded", "true");
  statusMenu.querySelector('[aria-checked="true"]').focus();
}
statusMenu.addEventListener("toggle", (e) => {
  if (e.newState === "closed" && statusTrigger) statusTrigger.setAttribute("aria-expanded", "false");
});
statusMenu.addEventListener("keydown", (e) => {
  const buttons = [...statusMenu.querySelectorAll("button")];
  if (["ArrowDown","ArrowUp","Home","End"].includes(e.key)) {
    e.preventDefault();
    const i = buttons.indexOf(document.activeElement);
    buttons[e.key === "Home" ? 0 : e.key === "End" ? buttons.length-1 : (i + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
  }
});
statusMenu.addEventListener("click", async (e) => {
  const choice = e.target.closest("[data-status-choice]")?.dataset.statusChoice;
  const task = todos.list.find((t) => t.id === statusTarget);
  if (!choice || !task || todos.inlineBusy) return;
  const parent = task.parent_id || task.id;
  if (choice === "remove") {
    statusMenu.classList.add("popconfirm");
    statusMenu.setAttribute("role", "dialog");
    statusMenu.setAttribute("aria-label", "确认移除待办");
    statusMenu.innerHTML = `<b>移除这项待办？</b><p>“${escapeHtml(task.title)}”${task.children_total ? "及所有子待办将被删除" : "将被删除"}，无法恢复。</p><small>原始文件、材料和历史周报保留；无其他引用的关联文件将停止追踪。</small><div class="row gap end"><button type="button" data-remove-cancel>取消</button><button type="button" class="danger-solid" data-status-choice="delete">确认移除</button></div>`;
    positionStatusMenu();
    statusMenu.querySelector("[data-remove-cancel]").focus();
    return;
  }
  statusMenu.hidePopover();
  statusTrigger?.focus();
  if (choice === "delete") {
    await saveInlineChild(parent, async () => {
      await api(`/todos/${task.id}/permanent?confirm=true`, {method:"DELETE"});
      if (todos.id === task.id || todos.detail?.parent_id === task.id) $("#todo-panel").close();
    }, "待办已彻底删除。");
  } else if (choice !== task.status) {
    const updated = await saveInlineChild(parent, () => withConfirm((confirm) => api(`/todos/${task.id}/status`, {method:"POST", json:{status:choice, confirm}}), "完成父待办"), "待办状态已更新；其他待办状态保持不变。");
    if (todos.detailOpen && updated) todoMsg(`状态已更新：${TODO_STATUS[choice]}。`);
  }
});
statusMenu.addEventListener("click", (e) => {
  if (e.target.closest("[data-remove-cancel]")) {
    statusMenu.hidePopover();
    statusTrigger?.focus();
  }
});

function positionStatusMenu() {
  if (!statusTrigger?.isConnected) return;
  const rect = statusTrigger.getBoundingClientRect();
  const width = Math.min(statusMenu.classList.contains("popconfirm") ? 300 : 210, innerWidth - 16);
  statusMenu.style.width = `${width}px`;
  const height = statusMenu.getBoundingClientRect().height;
  const top = rect.bottom + 6 + height <= innerHeight - 8 ? rect.bottom + 6 : rect.top - height - 6;
  statusMenu.style.left = `${Math.max(8, Math.min(rect.right - width, innerWidth - width - 8))}px`;
  statusMenu.style.top = `${Math.max(8, Math.min(top, innerHeight - height - 8))}px`;
}
window.addEventListener("resize", () => { if (statusMenu.matches(":popover-open")) positionStatusMenu(); });
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
  if (todos.id !== id || scroll) todos.editMode = false;
  if (todos.id !== id) todos.timelineLimit = 5;
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

$("#todo-close").addEventListener("click", () => { if (!todos.editSaving) $("#todo-panel").close(); });
$("#todo-panel").addEventListener("cancel", (e) => { if (todos.editSaving) e.preventDefault(); });
$("#todo-panel").addEventListener("requestclose", (e) => { if (todos.editSaving) e.preventDefault(); });
$("#todo-back").addEventListener("click", () => {
  if (todos.detail?.parent_id && !todos.editSaving) openTodo(todos.detail.parent_id);
});
$("#todo-panel").addEventListener("close", () => { if (statusMenu.matches(":popover-open")) statusMenu.hidePopover(); todos.detailOpen = false; todos.editMode = false; todos.detailView++; });

function renderTodo(t, timeline, reports) {
  const archived = Boolean(t.archived_at);
  $("#todo-title").textContent = t.title;
  $("#todo-status").dataset.statusMenu = t.id;
  $("#todo-status").dataset.state = t.status;
  $("#todo-status").innerHTML = `${statusIcon(t.status)}<span>${archived ? "已归档" : TODO_STATUS[t.status]}</span>`;
  $("#todo-status").setAttribute("aria-label", `更改${t.title}的状态：${archived ? "已归档" : TODO_STATUS[t.status]}`);
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
    ["截止日期", t.due_date ? `<input type="date" class="readonly-date" aria-label="截止日期（只读）" value="${escapeHtml(t.due_date)}" disabled>` : `<span class="muted">未设置</span>`],
    ["时间", `创建 ${fmtTime(t.created_at)}${t.completed_at ? ` · 完成 ${fmtTime(t.completed_at)}` : ""}${archived ? ` · 归档 ${fmtTime(t.archived_at)}` : ""}`],
  ];
  $("#todo-info").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("#todo-back").classList.toggle("hidden", !t.parent_id);
  $("#todo-back").title = t.parent ? `返回：${t.parent.title}` : "返回父待办";
  renderTodoEditor(t);

  // 子待办：子待办自己不能再有子待办
  $("#todo-children-box").classList.toggle("hidden", Boolean(t.parent_id));
  $("#todo-progress").textContent = t.children_total ? `${t.children_done}/${t.children_total} 个子待办已完成` : "";
  $("#todo-children").innerHTML = t.children.length
    ? t.children.map((k) => `<div class="child-card"><button type="button" class="child-detail-open grow" data-open-todo="${k.id}">${escapeHtml(k.title)}</button>
        <span class="muted small">${TODO_STATUS[k.status]}</span>${k.due_date ? `<span class="muted small">截止 ${shortDay(k.due_date)}</span>` : ""}${todoStatusMark(k)}</div>`).join("")
    : `<p class="muted small">没有子待办。大的工作可以拆成几个子待办，各自有状态和截止日期。</p>`;

  for (const [selector, part] of [["#todo-material-section","materials"],["#todo-history-section","history"],["#todo-report-section","reports"]]) {
    const el = $(selector), key = `todo:${t.id}:${part}`;
    el.dataset.disclosure = key;
    el.open = Boolean(disclosureOpen(key));
  }
  const materialIssues = t.material_list.filter((m) => !m.current_id || m.tracking?.last_error || ["missing", "unreachable"].includes(m.tracking?.status)).length;
  $("#todo-material-count").textContent = `${t.material_list.length} 份${materialIssues ? ` · ${materialIssues} 项异常，请展开核对` : ""}`;
  $("#todo-material-count").classList.toggle("danger", materialIssues > 0);
  $("#todo-report-count").textContent = `${reports.length} 份`;
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
  todos.timeline = items;
  $("#todo-history-count").textContent = `共 ${items.length} 条 · 当前显示最近 ${Math.min(todos.timelineLimit, items.length)} 条`;
  const rows = [...items].reverse().slice(0, todos.timelineLimit).map((x) => {
    if (x.kind === "file_change") {
      const name = String(x.path).replace(/\\/g, "/").split("/").pop();
      const versions = x.to_version ? ` v${x.from_version || "-"}→v${x.to_version}` : "";
      const shared = x.relation === "shared" ? "（父待办的共享材料）" : "";
      return `<div class="tl file"><time>${fmtTime(x.at)}</time><span>文件《${escapeHtml(name)}》${CHANGE_TEXT[x.change_type] || x.change_type}${versions}${shared}</span></div>`;
    }
    const text = (EVENT_TEXT[x.event_type] || (() => x.event_type))(x);
    return `<div class="tl"><time>${fmtTime(x.at)}</time><span>${escapeHtml(text)}</span></div>`;
  });
  $("#todo-timeline").innerHTML = (rows.join("") || `<p class="muted small">还没有活动。</p>`) + (items.length > todos.timelineLimit ? `<button type="button" class="ghost small" data-more-activities>查看全部 ${items.length} 条活动</button>` : "");
}

$("#todo-panel").addEventListener("click", async (e) => {
  if (e.target.closest("[data-more-activities]")) { todos.timelineLimit = todos.timeline.length; renderTimeline(todos.timeline); return; }
  const status = e.target.closest("[data-status-menu]");
  if (status) return openStatusMenu(status);
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

$("#todo-upload").closest("label").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#todo-upload").click(); }
});

function renderTodoEditor(t) {
  const editing = todos.editMode && !t.archived_at;
  $("#todo-panel").classList.toggle("is-editing", editing);
  $("#todo-info").classList.toggle("hidden", editing);
  $("#todo-edit-form").classList.toggle("hidden", !editing);
  $("#todo-edit").classList.toggle("hidden", editing);
  $("#todo-edit-cancel").classList.toggle("hidden", !editing);
  $("#todo-edit-save").classList.toggle("hidden", !editing);
  $("#todo-edit-save").disabled = todos.editSaving;
  $("#todo-edit-save").innerHTML = `${uiIcon("tick")}${todos.editSaving ? "保存中…" : "保存"}`;
  ["#todo-edit-cancel", "#todo-close", "#todo-back"].forEach((sel) => $(sel).disabled = todos.editSaving);
  ["#todo-status", "#todo-promote", "#todo-archive", "#todo-restore"].forEach((sel) => $(sel).disabled = editing || Boolean(t.archived_at && sel === "#todo-status"));
  $("#todo-edit-form").setAttribute("aria-busy", String(todos.editSaving));
  for (const field of $("#todo-edit-form").elements) field.disabled = todos.editSaving;
  document.querySelectorAll("#todo-detail-body > .todo-section").forEach((el) => el.inert = editing);
  if (editing) {
    const draft = todos.editDrafts.get(t.id) || {title:t.title, description:t.description, due:t.due_date || ""};
    $("#todo-edit-title").value = draft.title;
    $("#todo-edit-description").value = draft.description;
    $("#todo-edit-due").value = draft.due;
  }
}
$("#todo-edit").addEventListener("click", () => {
  if (!todos.detail || todos.detail.archived_at) return;
  const restored = todos.editDrafts.has(todos.id);
  todos.editMode = true;
  renderTodoEditor(todos.detail);
  if (restored) todoMsg("已恢复这项任务尚未保存的修改。保存后才会更新任务。");
  else $("#todo-msg").classList.add("hidden");
  $("#todo-edit-title").focus();
});
$("#todo-edit-form").addEventListener("input", () => {
  todos.editDrafts.set(todos.id, {title:$("#todo-edit-title").value, description:$("#todo-edit-description").value, due:$("#todo-edit-due").value});
});
$("#todo-edit-cancel").addEventListener("click", () => {
  if (todos.editSaving) return;
  todos.editDrafts.delete(todos.id);
  todos.editMode = false;
  renderTodoEditor(todos.detail);
  $("#todo-msg").classList.add("hidden");
});
$("#todo-edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (todos.editSaving || !todos.editMode) return;
  const id = todos.id, form = $("#todo-edit-form");
  if (!form.reportValidity()) return;
  const draft = {title:$("#todo-edit-title").value.trim(), description:$("#todo-edit-description").value, due:$("#todo-edit-due").value};
  if (!draft.title) { todoMsg("请填写任务标题。", "error"); $("#todo-edit-title").focus(); return; }
  todos.editDrafts.set(id, draft);
  todos.editSaving = true;
  renderTodoEditor(todos.detail);
  try {
    const updated = await api(`/todos/${id}`, {method:"PUT", json:{title:draft.title, description:draft.description, due_date:draft.due || null}});
    todos.editDrafts.delete(id);
    todos.editMode = false;
    todos.detail = updated;
    $("#todo-title").textContent = updated.title;
    await loadTodos();
    todoMsg("任务详情已保存。现在为查看模式。");
  } catch (err) { todoMsg(`保存失败：${escapeHtml(err.message)}。修改仍保留，可重试。`, "error"); }
  finally {
    todos.editSaving = false;
    if (todos.detailOpen && todos.id === id) renderTodoEditor(todos.detail);
  }
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

$("#todo-list").addEventListener("input", (e) => {
  const form = e.target.closest("[data-inline-note]");
  if (form) todos.noteDrafts.set(Number(form.dataset.inlineNote), form.elements.note.value);
});
$("#todo-list").addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-inline-note]");
  if (!form) return;
  e.preventDefault();
  const id = Number(form.dataset.inlineNote), text = form.elements.note.value.trim();
  if (!text || todos.inlineBusy || !todos.noteOpen.has(id)) return;
  for (const control of form.elements) control.disabled = true;
  await saveInlineChild(id, async () => {
    await api(`/todos/${id}/notes`, {method:"POST", json:{text}});
    todos.noteDrafts.delete(id);
    todos.noteOpen.delete(id);
  }, "进展已记录，任务状态未修改。");
  const current = document.getElementById(`inline-note-form-${id}`);
  if (current) {
    for (const control of current.elements) control.disabled = false;
    current.elements.note.disabled = !todos.noteOpen.has(id);
  }
});
