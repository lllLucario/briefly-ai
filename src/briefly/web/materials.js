// 材料库浏览：标题搜索、筛选、排序和分页都由服务端完成（/materials/library）。
// 只管材料库页面；助理、待办、周报的选择仍用 state.materials 里的完整列表，不受这里的条件影响。
"use strict";

const LIB_PAGE = 50;
const LIB_BATCH_MAX = 100; // “查看本次添加”一次最多列出的材料数
const LIB_FILTERS = { q: "", source: "all", tracking: "all", relation: "all", todoId: null, todoLabel: "", includeArchived: false, sort: "newest" };
const libraryState = {
  ...LIB_FILTERS,
  offset: 0,
  ids: null, // “查看本次添加”的材料编号；为空表示普通浏览
  saved: null, // 进入“本次添加”前的条件，返回时恢复
  batch: null, // 最近一次在材料页添加的结果 {ids, created, reused, failed}
  highlight: new Set(),
  seq: 0, // 请求序号：晚到的旧响应不覆盖新结果
  items: [],
  total: 0,
  loaded: false,
  loading: false,
  error: "",
  todoOptions: [],
};

const SOURCE_NAMES = { file: "本机文件", paste: "粘贴文字", feishu: "飞书" };
const FEISHU_KINDS = { feishu: "文档", bitable: "多维表格", minutes: "妙记" };

function sourceLabel(m) {
  const category = m.source_category || (m.feishu ? "feishu" : m.source === "paste" ? "paste" : "file");
  if (category === "paste") return "粘贴文字";
  if (category === "feishu") return `飞书 · ${FEISHU_KINDS[m.file_type] || "文档"}`;
  return `本机文件${m.file_type ? ` · ${kindName(m.file_type)}` : ""}`;
}

function updateLabel(m) {
  if (m.source === "tracked") return "追踪最新版本";
  return !m.tracking_state || m.tracking_state === "fixed" ? "固定副本" : "固定副本 · 已开启追踪";
}

function trackingHint(m) {
  if (m.tracking_state === "paused") return `<span class="badge warn" title="${escapeHtml(m.tracking_note)}">${uiIcon("pause")}已暂停</span>`;
  if (m.tracking_state === "error") return `<span class="badge bad" title="${escapeHtml(m.tracking_note)}">${uiIcon("alert")}追踪异常</span>`;
  return "";
}

function todoName(r) {
  return `${r.parent_title ? `${r.parent_title} › ` : ""}${r.title}`;
}

function relatedTodoButton(r) {
  const notes = [r.relation === "shared" ? "来自父待办" : "", r.archived ? "已归档" : ""].filter(Boolean);
  const tip = r.relation === "shared" ? "父待办关联的材料，子待办共享查看" : "直接关联在这个待办上";
  return `<button type="button" class="todo-link" data-lib-todo="${r.id}" title="${tip}；打开待办详情">${escapeHtml(todoName(r))}${notes.map((n) => `<small>${n}</small>`).join("")}</button>`;
}

function relatedCell(m) {
  const list = m.related_todos || [];
  if (!list.length) return `<span class="muted">未关联</span>`;
  const more = list.slice(2);
  return `<div class="todo-links">${list.slice(0, 2).map(relatedTodoButton).join("")}${more.length
    ? `<details class="more-todos" data-disclosure="lib:${m.id}:todos" ${disclosureOpen(`lib:${m.id}:todos`)}><summary>另 ${more.length} 项</summary>${more.map(relatedTodoButton).join("")}</details>` : ""}</div>`;
}

function updateCell(m) {
  const actions = trackCell(m); // 开启 / 停止追踪等现有操作
  const button = /data-track=|data-untrack=/.test(actions) ? actions : "";
  const why = m.source === "tracked" ? "随追踪自动更新为最新内容；以前的版本在“追踪的文件”里查看"
    : m.tracking_state && m.tracking_state !== "fixed" ? "这份是添加时内容的副本，不会改变；原文档已在追踪，新版本另存为材料"
      : "添加时内容的副本，原文件后续修改不会自动更新";
  return `<div class="update-mode"><span title="${why}">${updateLabel(m)}</span>${trackingHint(m)}${button}</div>`;
}

function libraryRow(m) {
  const fresh = libraryState.highlight.has(m.id);
  const warn = m.warnings.length ? `<span class="warn-dot" title="${escapeHtml(m.warnings.join("\n"))}" role="img" aria-label="解析警告">${uiIcon("alert")}</span>` : "";
  return `<tr class="clickable ${state.activeMaterial === m.id ? "active" : ""} ${fresh ? "is-new" : ""}" data-id="${m.id}">
      <td class="lib-main" data-label="材料"><div class="lib-title">${fileIcon(m.file_type || "paste")}<button type="button" class="lib-open" data-open-material="${m.id}">${escapeHtml(m.title)}</button>${warn}${fresh ? `<span class="badge ok">新添加</span>` : ""}</div>
        ${m.preview ? `<div class="lib-preview">${escapeHtml(m.preview)}</div>` : ""}</td>
      <td data-label="来源">${escapeHtml(sourceLabel(m))}</td>
      <td class="track-cell" data-label="更新方式">${updateCell(m)}</td>
      <td data-label="关联待办">${relatedCell(m)}</td>
      <td data-label="添加时间" class="nowrap">${fmtTime(m.created_at)}</td>
      <td class="lib-actions">${m.source === "tracked" ? "" : `<button class="ghost danger small" data-delete="${m.id}" aria-label="删除《${escapeHtml(m.title)}》">删除</button>`}</td>
    </tr>`;
}

function libraryFiltered() {
  const s = libraryState;
  return Boolean(s.q || s.source !== "all" || s.tracking !== "all" || s.relation !== "all" || s.includeArchived);
}

function activeFilterCount() {
  const s = libraryState;
  return [s.source !== "all", s.tracking !== "all", s.relation !== "all", s.includeArchived].filter(Boolean).length;
}

function renderLibrary() {
  const s = libraryState;
  const rows = $("#material-rows");
  $("#mat-library").setAttribute("aria-busy", String(s.loading));
  const batch = Boolean(s.ids);
  $("#lib-batch").classList.toggle("hidden", !batch);
  if (batch) $("#lib-batch-text").textContent = `正在查看本次添加的 ${s.ids.length} 份材料${s.batch?.truncated ? `（本次共 ${s.batch.truncated} 份，只列出最近 ${LIB_BATCH_MAX} 份）` : ""}`;
  $("#lib-toolbar").querySelectorAll("input, select, button").forEach((el) => { el.disabled = batch; });
  const count = activeFilterCount();
  $("#lib-filter-count").textContent = count ? String(count) : "";
  $("#lib-filter-toggle").setAttribute("aria-label", count ? `筛选（${count} 个条件）` : "筛选");
  $("#lib-q-clear").classList.toggle("hidden", !s.q);
  $("#lib-reset").classList.toggle("hidden", batch || !libraryFiltered());
  $("#lib-todo-chip").classList.toggle("hidden", s.relation !== "todo");
  $("#lib-todo-chip-text").textContent = s.todoLabel;
  $("#lib-error").classList.toggle("hidden", !s.error || !s.loaded);
  if (s.error && s.loaded) $("#lib-error-text").textContent = `刷新失败：${s.error}。下面是上次的结果。`;

  if (!s.loaded) {
    rows.innerHTML = s.error
      ? `<tr><td colspan="6"><div class="notice error">材料加载失败：${escapeHtml(s.error)} <button type="button" class="ghost small" data-lib-retry>重试</button></div></td></tr>`
      : `<tr><td colspan="6" class="muted">正在加载材料…</td></tr>`;
    $("#lib-count").textContent = "";
    $("#lib-pager").classList.add("hidden");
    return;
  }
  if (!s.items.length) {
    rows.innerHTML = libraryFiltered() || batch
      ? `<tr><td colspan="6" class="lib-empty"><b>没有匹配的材料</b><span class="muted">换个关键词，或</span><button type="button" class="ghost small" data-lib-reset>${batch ? "返回之前的筛选" : "清空搜索和筛选"}</button></td></tr>`
      : `<tr><td colspan="6" class="lib-empty"><b>还没有材料</b><span class="muted">可以上传文件、粘贴文字，或从飞书导入。</span><button type="button" class="primary small" data-lib-add>${uiIcon("plus")}添加材料</button></td></tr>`;
  } else {
    rows.innerHTML = s.items.map(libraryRow).join("");
  }
  const end = s.offset + s.items.length;
  $("#lib-count").textContent = s.total
    ? `${libraryFiltered() || batch ? "找到" : "共"} ${s.total} 份材料${s.total > LIB_PAGE ? `，第 ${s.offset + 1}–${end} 份` : ""}`
    : libraryFiltered() ? "没有匹配的材料" : "";
  $("#lib-pager").classList.toggle("hidden", s.total <= LIB_PAGE);
  $("#lib-prev").disabled = s.offset <= 0;
  $("#lib-next").disabled = end >= s.total;
  $("#lib-page").textContent = `${Math.floor(s.offset / LIB_PAGE) + 1} / ${Math.max(1, Math.ceil(s.total / LIB_PAGE))}`;
}

function libraryParams() {
  const s = libraryState;
  const p = new URLSearchParams({ offset: String(s.offset), limit: String(LIB_PAGE) });
  if (s.ids) {
    s.ids.forEach((id) => p.append("material_ids", String(id)));
    return p;
  }
  if (s.q) p.set("q", s.q);
  for (const k of ["source", "tracking", "relation", "sort"]) if (s[k] !== LIB_FILTERS[k]) p.set(k, s[k]);
  if (s.relation === "todo") p.set("todo_id", String(s.todoId));
  if (s.includeArchived) p.set("include_archived", "true");
  return p;
}

async function loadLibrary() {
  const s = libraryState;
  const seq = ++s.seq;
  s.loading = true;
  $("#mat-library").setAttribute("aria-busy", "true");
  try {
    const r = await api(`/materials/library?${libraryParams()}`);
    if (seq !== s.seq) return;
    if (!r.items.length && r.total && s.offset >= r.total) { // 最后一页的材料删完了：回到最后一个有效页
      s.offset = Math.floor((r.total - 1) / LIB_PAGE) * LIB_PAGE;
      return loadLibrary();
    }
    Object.assign(s, { items: r.items, total: r.total, loaded: true, error: "" });
  } catch (err) {
    if (seq !== s.seq) return;
    s.error = err.message;
  } finally {
    if (seq === s.seq) {
      s.loading = false;
      renderLibrary();
    }
  }
}

// 材料变化后由 loadMaterials 调用：材料库页面打开时按当前条件刷新，不重置条件
function refreshLibrary() {
  if ($("#tab-materials").classList.contains("active")) return loadLibrary();
}

function setLibrary(changes) {
  Object.assign(libraryState, changes, { offset: 0 });
  libraryState.highlight.clear();
  loadLibrary();
}

// ---------- 工具栏
let libSearchTimer = null;
$("#lib-q").addEventListener("input", (e) => {
  clearTimeout(libSearchTimer);
  $("#lib-q-clear").classList.toggle("hidden", !e.target.value);
  libSearchTimer = setTimeout(() => setLibrary({ q: e.target.value.trim() }), 250);
});
$("#lib-q").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  clearTimeout(libSearchTimer);
  setLibrary({ q: e.target.value.trim() });
});
$("#lib-q-clear").addEventListener("click", () => {
  clearTimeout(libSearchTimer);
  $("#lib-q").value = "";
  setLibrary({ q: "" });
  $("#lib-q").focus();
});
$("#lib-source").addEventListener("change", (e) => setLibrary({ source: e.target.value }));
$("#lib-tracking").addEventListener("change", (e) => setLibrary({ tracking: e.target.value }));
$("#lib-sort").addEventListener("change", (e) => setLibrary({ sort: e.target.value }));
$("#lib-archived").addEventListener("change", (e) => setLibrary({ includeArchived: e.target.checked }));
$("#lib-relation").addEventListener("change", async (e) => {
  if (e.target.value !== "todo") return setLibrary({ relation: e.target.value, todoId: null, todoLabel: "" });
  const picked = await chooseLibraryTodo();
  if (!picked) { e.target.value = libraryState.relation; return; }
  setLibrary({ relation: "todo", todoId: picked.id, todoLabel: picked.label });
});
$("#lib-todo-chip").addEventListener("click", async () => {
  const picked = await chooseLibraryTodo();
  if (picked) setLibrary({ relation: "todo", todoId: picked.id, todoLabel: picked.label });
});
$("#lib-filter-toggle").addEventListener("click", () => {
  const open = !$("#lib-filters").classList.contains("open");
  $("#lib-filters").classList.toggle("open", open);
  $("#lib-filter-toggle").setAttribute("aria-expanded", String(open));
});

function resetLibraryControls() {
  $("#lib-q").value = libraryState.q;
  $("#lib-source").value = libraryState.source;
  $("#lib-tracking").value = libraryState.tracking;
  $("#lib-relation").value = libraryState.relation;
  $("#lib-sort").value = libraryState.sort;
  $("#lib-archived").checked = libraryState.includeArchived;
}

function resetLibrary() {
  if (libraryState.ids) return leaveBatchView();
  clearTimeout(libSearchTimer);
  Object.assign(libraryState, LIB_FILTERS);
  resetLibraryControls();
  setLibrary({});
}
$("#lib-reset").addEventListener("click", resetLibrary);
$("#lib-prev").addEventListener("click", () => { libraryState.offset = Math.max(0, libraryState.offset - LIB_PAGE); loadLibrary(); });
$("#lib-next").addEventListener("click", () => { libraryState.offset += LIB_PAGE; loadLibrary(); });
$("#lib-retry").addEventListener("click", () => loadLibrary());

// ---------- 按具体待办筛选：在弹窗里搜索选择，父子显示为“父 › 子”
let libTodoResolve = null;
async function chooseLibraryTodo() {
  const dialog = $("#lib-todo-dialog");
  $("#lib-todo-q").value = "";
  $("#lib-todo-list").innerHTML = `<p class="muted">正在加载待办…</p>`;
  dialog.showModal();
  const result = new Promise((resolve) => { libTodoResolve = resolve; });
  try {
    const list = await api("/todos?archived=true");
    const byId = new Map(list.map((t) => [t.id, t]));
    libraryState.todoOptions = list.map((t) => ({
      id: t.id, archived: Boolean(t.archived_at),
      label: `${t.parent_id && byId.get(t.parent_id) ? `${byId.get(t.parent_id).title} › ` : ""}${t.title}`,
      order: t.parent_id ? [t.parent_id, 1, t.id] : [t.id, 0, 0],
    })).sort((a, b) => a.order[0] - b.order[0] || a.order[1] - b.order[1] || a.order[2] - b.order[2]);
    renderLibraryTodos();
  } catch (err) {
    $("#lib-todo-list").innerHTML = `<div class="notice error">待办加载失败：${escapeHtml(err.message)}</div>`;
  }
  return result;
}

function renderLibraryTodos() {
  const q = $("#lib-todo-q").value.trim().toLowerCase();
  const archived = $("#lib-todo-archived").checked;
  const rows = libraryState.todoOptions.filter((t) => (archived || !t.archived) && (!q || t.label.toLowerCase().includes(q)));
  $("#lib-todo-list").innerHTML = rows.length
    ? rows.map((t) => `<button type="button" class="lib-todo-option" data-pick-todo="${t.id}">${escapeHtml(t.label)}${t.archived ? " <small class=\"muted\">已归档</small>" : ""}</button>`).join("")
    : `<p class="muted">${libraryState.todoOptions.length ? "没有匹配的待办" : "还没有待办"}</p>`;
}
$("#lib-todo-q").addEventListener("input", renderLibraryTodos);
$("#lib-todo-archived").addEventListener("change", renderLibraryTodos);
$("#lib-todo-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pick-todo]");
  if (!b) return;
  const t = libraryState.todoOptions.find((x) => x.id === Number(b.dataset.pickTodo));
  const resolve = libTodoResolve;
  libTodoResolve = null;
  $("#lib-todo-dialog").close();
  resolve?.({ id: t.id, label: t.label });
});
$("#lib-todo-dialog").addEventListener("close", () => {
  if ($("#lib-todo-dialog").open) return;
  if (libTodoResolve) libTodoResolve(null);
  libTodoResolve = null;
});

// 从材料库打开待办：关闭详情后回到材料库（条件和页码不变）；在详情里转去其他页面时不再跳回
async function openTodoFromLibrary(id) {
  await activateTab("todos");
  todos.returnTab = "materials";
  return openTodo(id);
}

// ---------- 列表里的操作：打开待办 / 材料详情；子操作不触发打开材料
$("#material-rows").addEventListener("click", async (e) => {
  if (e.target.closest("summary")) { e.stopPropagation(); return; }
  const todo = e.target.closest("[data-lib-todo]");
  if (todo) {
    e.stopPropagation();
    return openTodoFromLibrary(Number(todo.dataset.libTodo));
  }
  if (e.target.closest("[data-lib-reset]")) { e.stopPropagation(); return resetLibrary(); }
  if (e.target.closest("[data-lib-retry]")) { e.stopPropagation(); return loadLibrary(); }
  if (e.target.closest("[data-lib-add]")) { e.stopPropagation(); return $("#open-add").click(); }
}, true);

// ---------- 添加结果：汇总成功 / 复用 / 失败，定位并高亮；可切到“本次添加”临时视图
function libraryBeginAdd() {
  return { ids: [], created: 0, reused: 0, failed: 0, onPage: $("#tab-materials").classList.contains("active") };
}

function libraryAdded(batch, result) {
  if (!result) { batch.failed += 1; return; }
  if (!batch.ids.includes(result.material.id)) batch.ids.push(result.material.id);
  result.created ? (batch.created += 1) : (batch.reused += 1);
}

async function libraryFinishAdd(batch) {
  if (!batch.ids.length) return;
  libraryState.batch = { ...batch, ids: batch.ids.slice(-LIB_BATCH_MAX), truncated: batch.ids.length > LIB_BATCH_MAX ? batch.ids.length : 0 };
  if (!batch.onPage) return; // 从助理等入口添加：留在原页面，可用“在材料库查看”再看
  libraryState.highlight = new Set(batch.ids);
  await loadLibrary();
  const visible = libraryState.items.filter((m) => libraryState.highlight.has(m.id)).length;
  const parts = [`新增 ${batch.created} 份`, batch.reused ? `已有相同内容 ${batch.reused} 份（直接复用）` : "", batch.failed ? `失败 ${batch.failed} 份` : ""].filter(Boolean);
  const where = libraryState.error ? "已保存，列表刷新失败，请稍后重试刷新，不需要重新添加。"
    : visible < batch.ids.length ? "部分材料不在当前筛选或页码中。" : "";
  showNotice($("#material-msg"), `本次添加：${parts.join("，")}。${where} <button type="button" class="ghost small" data-lib-batch>查看本次添加</button>`, batch.failed || where ? "warn" : "");
  $("#material-rows .is-new")?.scrollIntoView({ block: "nearest" });
}

async function showAddedInLibrary() {
  if (!$("#tab-materials").classList.contains("active")) await activateTab("materials", false);
  document.querySelector('#mat-tabs [data-mt="library"]').click();
  enterBatchView();
}

function enterBatchView() {
  const batch = libraryState.batch;
  if (!batch) return;
  if (!libraryState.ids) libraryState.saved = Object.fromEntries(Object.keys(LIB_FILTERS).concat("offset").map((k) => [k, libraryState[k]]));
  Object.assign(libraryState, LIB_FILTERS, { ids: batch.ids, offset: 0 });
  loadLibrary();
}

function leaveBatchView() {
  Object.assign(libraryState, libraryState.saved || LIB_FILTERS, { ids: null });
  libraryState.saved = null;
  resetLibraryControls();
  loadLibrary();
}
$("#material-msg").addEventListener("click", (e) => { if (e.target.closest("[data-lib-batch]")) enterBatchView(); });
$("#lib-batch-leave").addEventListener("click", leaveBatchView);
