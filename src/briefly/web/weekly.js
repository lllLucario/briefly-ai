// 周报页：一周一份周报。到点自动整理，或右下角“整理本周工作”——两者都是 检查更新 → 整理审核清单 → 你审核 → 生成。
// 左栏：本周 + 周报历史 + 周报设置。右侧：这一周的审核清单、本周材料、本周说明、周报正文。
// 依赖 app.js 里的 $、api、state、escapeHtml、renderMarkdown、showNotice、fmtTime、fileIcon、materialTitle、ui*、pickMaterials、loadMaterials。
"use strict";

const weekly = { list: [], current: null, day: null, settings: null, selected: new Set(), expanded: new Set(), editing: null, timer: null };
const WEEK_STATUS = { none: "还没生成", pending: "排队中", running: "生成中", done: "已生成", failed: "生成失败" };
const EV_STATUS = { PENDING: "待确认", CONFIRMED: "已确认", REJECTED: "已排除" };
const EV_CATEGORY = { done: "已完成", in_progress: "进行中", plan: "计划", issue: "问题风险", other: "其他" };
const ATTR_TEXT = { ATTRIBUTED: "有作者记录", USER_CONFIRMED: "你已确认", INFERRED: "推断", UNKNOWN: "无法判断", EXCLUDED: "已排除" };
const FLAG_TEXT = {
  first_seen: "本周新发现的文件", possibly_moved: "可能移动/改名", reverted: "中间有过增删", gap: "检查中断过",
  parse_failed: "解析失败", format_only: "只改了格式", pdf_text: "PDF 可能有提取误差", truncated: "变化过多已截断",
  others: "有他人的修订", mixed: "你和他人都有修订", no_owner: "未设置你在文档里的名字", deleted: "文件被删除",
  extract_failed: "部分变化未能概括", done_downgraded: "没写“完成”，不按已完成算", increment: "审核之后的新进展",
};
const TODO_STATUS_TEXT = { todo: "未开始", doing: "进行中", done: "已完成" };

function weekMsg(html, kind = "") {
  showNotice($("#week-msg"), html, kind);
}

function shortDate(iso) {
  const [, m, d] = String(iso).split("-");
  return `${m}.${d}`;
}

function isThisWeek(r) {
  const today = new Date();
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  return iso(today) >= String(r.period_start) && iso(today) <= String(r.period_end);
}

// ---------- 左栏
async function loadWeekly(open = true) {
  const [list, current, settings] = await Promise.all([api("/weekly"), api("/weekly/week"), api("/weekly-settings")]);
  weekly.list = list;
  weekly.settings = settings;
  renderWeekSide(current);
  if (open) await openWeek(weekly.current?.id ? { id: weekly.current.id } : {});
}

function renderWeekSide(current) {
  const active = weekly.current;
  const isActive = (r) => active && String(active.period_start) === String(r.period_start);
  $("#week-current").innerHTML = `<li data-week="current" class="${!active || isActive(current) ? "active" : ""}">
    <span class="row gap"><b class="grow">本周</b>${current.pending ? `<span class="count-badge" title="待确认">${current.pending}</span>` : ""}</span>
    <small>${shortDate(current.period_start)}–${shortDate(current.period_end)} · ${WEEK_STATUS[current.status]}</small></li>`;
  const past = weekly.list.filter((r) => !isThisWeek(r));
  $("#weekly-list").innerHTML = past.length
    ? past.map((r) => `<li data-id="${r.id}" class="${isActive(r) ? "active" : ""}">
        ${shortDate(r.period_start)}–${shortDate(r.period_end)}
        <small><span class="status-pill ${r.status}">${WEEK_STATUS[r.status] || r.status}</span>${r.pending ? ` · ${r.pending} 条待确认` : ""}</small></li>`).join("")
    : `<li class="muted" style="cursor:default">还没有以前的周报</li>`;
}

$("#week-current").addEventListener("click", () => openWeek({}));
$("#weekly-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) openWeek({ id: Number(li.dataset.id) });
});

// 从提醒条、待办页跳到某一周
async function gotoWeek(reportId) {
  await activateTab("weekly", false);
  await Promise.all([loadMaterials(), loadWeekly(false)]);
  await openWeek(reportId ? { id: reportId } : {});
}

// ---------- 打开一周
function stopPolling() {
  if (weekly.timer) clearTimeout(weekly.timer);
  weekly.timer = null;
}

async function openWeek({ id = null, day = null } = {}) {
  stopPolling();
  weekly.selected.clear();
  exitEdit();
  $("#week-msg").classList.add("hidden");
  const r = id ? await api(`/weekly/${id}`) : await api(`/weekly/week${day ? `?day=${day}` : ""}`);
  weekly.current = r;
  renderWeek();
  renderWeekSide(id ? await api("/weekly/week") : r);
}

async function refreshWeek() {
  const r = weekly.current;
  if (!r?.id) return;
  weekly.current = await api(`/weekly/${r.id}`);
  renderWeek();
}

// 本周材料、本周说明之前要有这一周的记录
async function ensureWeek() {
  if (!weekly.current.id) weekly.current = await api("/weekly/week", { method: "POST", json: { day: weekly.current.period_start } });
  return weekly.current.id;
}

function renderWeek() {
  const r = weekly.current;
  const thisWeek = isThisWeek(r);
  $("#week-title").textContent = `周报 ${shortDate(r.period_start)}–${shortDate(r.period_end)}${thisWeek ? "（本周）" : ""}`;
  const stale = r.status === "done" && r.checklist === "draft";
  if (r.status === "done") $("#week-msg").classList.add("hidden");
  $("#week-status").className = `status-pill ${r.status}`;
  $("#week-status").textContent = stale ? "已生成 · 清单有改动" : WEEK_STATUS[r.status] || r.status;
  const s = weekly.settings?.schedule;
  $("#week-auto").textContent = thisWeek && s?.enabled && s.next ? `下次自动整理：${fmtTime(s.next)}；核对后生成正文` : thisWeek && s && !s.enabled ? "未开启每周自动整理" : "";
  $("#week-delete").classList.toggle("hidden", !r.id);
  $("#week-notes").innerHTML = (r.prep_notes || []).map((n) => `<div class="notice warn">${escapeHtml(n)}</div>`).join("");
  renderChecklist();
  renderWeekMaterials();
  if (document.activeElement !== $("#week-note")) $("#week-note").value = r.note || "";
  renderReportBox();
  $("#week-stage").textContent = r.status === "done" && r.checklist === "confirmed"
    ? "③ 查看与交付：核对正文后，可修改、导出或发送。原始输入与核对清单可展开查看。"
    : r.prepared_at ? `② 核对纳入内容：${r.pending ? `还有 ${r.pending} 条待核对` : "清单已处理，可确认并生成周报"}`
    : "① 收集本周信息：加入材料和说明，再整理本周工作；此步不会直接生成正文。";
  if (weekly.inputsFor !== r.id || weekly.inputsStatus !== r.checklist + r.status) {
    $("#week-inputs").open = !(r.status === "done" && r.checklist === "confirmed");
    weekly.inputsFor = r.id; weekly.inputsStatus = r.checklist + r.status;
  }
  renderFoot();
  if (r.status === "pending" || r.status === "running") weekly.timer = setTimeout(async () => { await refreshWeek(); loadWeekly(false); }, 3000);
}

// 右下角按钮：自动和手动只差“是不是到点自动整理”。没整理过 → 整理本周工作（先整理清单）；有待确认 → 先审核；审核完 → 确认并生成
function footState() {
  const r = weekly.current;
  if (r.status === "pending" || r.status === "running") return { label: "生成中…", disabled: true, hint: "逐条提取并核对，材料多时需要几分钟" };
  if (!r.prepared_at) {  // 手动和自动一样：先检查更新、整理审核清单，审核之后才生成
    return { label: "整理本周工作", action: "prepare", hint: "检查更新并整理清单，核对后再生成正文" };
  }
  if (r.pending) return { label: "确认并生成周报", disabled: true, hint: `还有 ${r.pending} 条待核对，请先纳入或排除` };
  if (r.status === "done" && r.checklist === "confirmed") return { label: "重新生成", action: "generate", hint: r.generated_at ? `已生成 · ${fmtTime(r.generated_at)}` : "" };
  return { label: r.status === "done" ? "重新生成" : "确认并生成周报", action: "generate", hint: r.status === "done" ? "清单有改动，可以重新生成" : "用已确认的记录、本周材料和本周说明生成" };
}

function renderFoot() {
  const st = footState();
  $("#week-go").textContent = st.label;
  $("#week-go").disabled = Boolean(st.disabled);
  $("#week-go").dataset.action = st.action || "";
  $("#week-hint").textContent = st.hint || "";
}

async function prepareWeek() {
  const r = weekly.current;
  weekMsg("正在检查更新并整理审核清单…（文件变化多时需要一两分钟）");
  const res = await api("/weekly/prepare", { method: "POST", json: { day: r.period_start } });
  weekly.current = res;
  const b = res.build;
  weekMsg(`新整理出 ${b.created} 条（其中待办 ${b.from_todos} 条），未变化 ${b.unchanged} 条。`
    + (res.scan ? "<br>" + scanSummary(res.scan) : "")
    + res.notes.map((n) => `<br>注意：${escapeHtml(n)}`).join("")
    + (res.pending ? "<br>审核完清单后，再点右下角生成。" : ""));
  renderWeek();
  loadWeekly(false);
}

$("#week-go").addEventListener("click", async () => {
  const btn = $("#week-go");
  const action = btn.dataset.action;
  btn.disabled = true;
  try {
    if (action === "prepare") return await prepareWeek();
    if (action !== "generate") return;
    const r = weekly.current;
    if (r.content && !(await uiConfirm("当前的正文（包括你改过的内容）会存为历史版本，可以在“历史版本”里恢复。", { title: "重新生成周报", ok: "重新生成" }))) return;
    weekly.current = await api(`/weekly/${await ensureWeek()}/generate`, { method: "POST", json: {} });
    renderWeek();
    loadWeekly(false);
  } catch (err) {
    weekMsg(escapeHtml(err.message), "error");
  } finally { renderFoot(); }
});

$("#week-prepare").addEventListener("click", async () => {
  const btn = $("#week-prepare");
  btn.disabled = true;
  try { await prepareWeek(); } catch (err) { weekMsg(escapeHtml(err.message), "error"); } finally { btn.disabled = false; }
});

$("#week-delete").addEventListener("click", async () => {
  if (!(await uiConfirm("删除这一周的审核清单、正文和历史版本；待办、材料、追踪历史，以及已写入飞书、已发出的邮件都不受影响。", { title: "删除这一周的周报", ok: "删除", danger: true }))) return;
  await api(`/weekly/${weekly.current.id}`, { method: "DELETE" });
  weekly.current = null;
  loadWeekly();
});

// ---------- 审核清单
// 记录关联的待办：所属（父 › 子）、现在的状态、本次周报的状态（只影响这一份周报）、同步到待办（需要确认）
function evidenceTodos(e) {
  if (!e.todos?.length) return "";
  const editable = ["PENDING", "CONFIRMED", "REJECTED"].includes(e.review_status);
  return `<div class="ev-todos">${e.todos.map((t) => {
    const path = t.parent_title ? `${escapeHtml(t.parent_title)} › ${escapeHtml(t.title)}` : escapeHtml(t.title);
    const via = t.relation === "material"
      ? (t.snapshot?.shared_with_children ? "关联文件的修改（父待办的共享材料，不算子待办的进展）" : "关联文件的修改（不代表任务完成）")
      : "待办本周的记录";
    const options = [["", "按记录判断"], ...Object.entries(TODO_STATUS_TEXT)].map(([v, label]) => `<option value="${v}" ${(t.report_status || "") === v ? "selected" : ""}>${v ? `本次：${label}` : label}</option>`).join("");
    const differs = t.report_status && t.report_status !== t.status;
    const sync = t.synced_at ? `<span class="badge ok">已同步到待办</span>`
      : differs && editable ? `<button class="ghost" data-sync="${t.todo_id}" title="把本次的状态写回待办（会先确认）">同步到待办</button>` : "";
    return `<span class="ev-todo" title="${escapeHtml(via)}">待办：<b>${path}</b>${t.archived ? "（已归档）" : ""}
      <span class="muted">待办当前：${TODO_STATUS_TEXT[t.status] || t.status}</span>
      ${editable ? `<label>本周报表述：<select data-todo-status="${t.todo_id}" title="只修改本周报中的状态表述；同步到待办需另行确认">${options}</select></label>` : ""}${sync}</span>`;
  }).join("")}</div>`;
}

function evidenceEvents(e) {
  const link = (e.todos || []).find((t) => t.relation === "activity");
  const events = link?.snapshot?.events || [];
  return events.length ? `<ul class="ev-events">${events.map((x) => `<li>${fmtTime(x.at)} ${escapeHtml(x.text)}</li>`).join("")}</ul>` : "";
}

function evidenceHtml(e) {
  const files = [...new Set(e.source_refs.map((r) => (r.path || "").replace(/\\/g, "/").split("/").pop()).filter(Boolean))].join("、");
  const versions = e.source_refs.filter((r) => r.to_version).map((r) => `v${r.from_version || "-"}→v${r.to_version}`).join("，");
  const flags = e.flags.filter((f) => FLAG_TEXT[f]).map((f) => `<span class="badge warn">${FLAG_TEXT[f]}</span>`).join(" ");
  const cls = e.review_status === "CONFIRMED" ? "confirmed" : e.review_status === "REJECTED" ? "rejected" : "";
  const attr = e.origin === "todo" ? (e.attribution === "USER_CONFIRMED" ? "你已确认" : "你的待办") : (ATTR_TEXT[e.attribution] || e.attribution);
  const basis = e.attribution_basis.length && !["manual", "todo"].includes(e.origin) ? "（" + escapeHtml(e.attribution_basis[0]) + "）" : "";
  const source = files ? escapeHtml(files) + " " + versions : e.origin === "todo" ? "你的待办记录" : "你补充的";
  return `<div class="ev ${cls}" data-ev="${e.id}">
    <div class="ev-head"><input type="checkbox" data-pick="${e.id}" ${weekly.selected.has(e.id) ? "checked" : ""}>
      <b>${escapeHtml(e.title)}${e.user_edited && !["manual", "todo"].includes(e.origin) ? ' <span class="badge">你改过</span>' : ""}${e.has_update ? ' <span class="badge warn">有新变化，见新的待确认项</span>' : ""}</b>
      </div>
    <div>${escapeHtml(e.description)}</div>
    <div class="ev-meta">来源：${source} ${flags}</div>
    <details class="meta" data-detail="${e.id}" ${weekly.expanded.has(e.id) ? "open" : ""}><summary>查看来源与调整周报中的状态表述</summary>
    <p class="muted small">周报分类：${EV_CATEGORY[e.category] || e.category}。这里的表述只影响本周报；点击同步并确认后才修改待办当前状态。</p>
    ${evidenceEvents(e)}归属：${attr}${basis}${evidenceTodos(e)}</details>
    <div class="ev-actions">
      ${e.review_status !== "CONFIRMED" ? `<button class="ghost" data-act="confirm">${["todo", "manual"].includes(e.origin) || ["ATTRIBUTED", "USER_CONFIRMED"].includes(e.attribution) ? "纳入本周" : "确认是我的并纳入"}</button>` : ""}
      ${e.review_status !== "REJECTED" ? `<button class="ghost" data-act="exclude">排除</button>` : ""}
      ${e.review_status !== "PENDING" && e.origin !== "manual" ? `<button class="ghost" data-act="reset">撤销</button>` : ""}
      <button class="ghost" data-act="edit">修改</button>
      ${e.source_refs.length ? `<button class="ghost" data-act="sources">看依据</button><button class="ghost" data-act="split">拆分</button><button class="ghost" data-act="regen">重新生成描述</button>` : ""}
    </div></div>`;
}

function renderChecklist() {
  const r = weekly.current;
  const items = r.checklist_items;
  $("#week-counts").textContent = items.length ? `待确认 ${r.counts.PENDING} · 已确认 ${r.counts.CONFIRMED} · 已排除 ${r.counts.REJECTED}` : "";
  $("#week-prepare").classList.toggle("hidden", !r.prepared_at);
  const box = $("#week-checklist");
  if (!r.prepared_at && !items.length) {
    box.innerHTML = `<p class="muted">还没有整理。点右下角“整理本周工作”：检查追踪的文件有没有更新，把这一周的文件变化和待办进展整理成清单交给你确认（文件变化会调用模型）。
      ${isThisWeek(r) && weekly.settings?.schedule?.enabled ? "到了设定的时间也会自动整理。" : ""}</p>`;
    return;
  }
  const groups = ["PENDING", "CONFIRMED", "REJECTED"].map((s) => [s, items.filter((e) => e.review_status === s)]);
  box.innerHTML = `${items.length ? `<div class="prep-actions">
      <button class="ghost" id="bulk-confirm">确认所选</button><button class="ghost" id="bulk-exclude">排除所选</button>
      <button class="ghost" id="bulk-merge">合并所选</button></div>` : `<p class="muted">这一周没有发现文件变化，待办也没有进展。</p>`}
    ${groups.filter(([, xs]) => xs.length).map(([s, xs]) => `<div class="ev-group"><h5>${EV_STATUS[s]}（${xs.length}）</h5>${xs.map(evidenceHtml).join("")}</div>`).join("")}
    <button class="ghost" id="add-evidence">＋ 补充工作记录（逐条核对）</button>`;
}

async function reviewAction(action, ids, note = "") {
  if (!ids.length) return uiAlert("先勾选要操作的记录。");
  try {
    weekly.current = await api(`/weekly/${weekly.current.id}/review`, { method: "POST", json: { action, ids, note } });
    ids.forEach((i) => weekly.selected.delete(i));
    renderWeek();
    loadWeekly(false);
  } catch (err) { uiAlert(err.message, "出错了"); }
}

$("#week-checklist").addEventListener("toggle", (e) => {
  const detail = e.target;
  if (!detail.isConnected || !detail.matches("details[data-detail]")) return;
  const id = Number(detail.dataset.detail);
  if (detail.open) weekly.expanded.add(id); else weekly.expanded.delete(id);
}, true);

$("#week-checklist").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-todo-status]");
  if (sel) {
    const id = Number(sel.closest("[data-ev]").dataset.ev);
    try {
      weekly.current = await api(`/weekly/${weekly.current.id}/items/${id}/todos/${sel.dataset.todoStatus}`, { method: "PUT", json: { status: sel.value || null } });
      renderWeek();
    } catch (err) { uiAlert(err.message, "出错了"); }
    return;
  }
  const cb = e.target.closest("[data-pick]");
  if (cb) cb.checked ? weekly.selected.add(Number(cb.dataset.pick)) : weekly.selected.delete(Number(cb.dataset.pick));
});

$("#week-checklist").addEventListener("click", async (e) => {
  const t = e.target;
  const rid = weekly.current.id;
  if (t.id === "bulk-confirm") return reviewAction("confirm", [...weekly.selected]);
  if (t.id === "bulk-exclude") return reviewAction("exclude", [...weekly.selected]);
  if (t.id === "bulk-merge") {
    if (weekly.selected.size < 2) return uiAlert("合并至少要勾选两条记录。");
    return openEvidenceDialog("merge", null);
  }
  if (t.id === "add-evidence") {
    await ensureWeek();
    return openEvidenceDialog("add", null);
  }
  if (t.dataset.sync) {
    const id = Number(t.closest("[data-ev]").dataset.ev);
    const url = `/weekly/${rid}/items/${id}/todos/${t.dataset.sync}/sync`;
    try {
      await api(url, { method: "POST", json: { confirm: false } });
    } catch (err) {
      if (!err.needsConfirm) return uiAlert(err.message, "出错了");
      if (!(await uiConfirm(err.message, { title: "同步到待办", ok: "同步" }))) return;
      try {
        weekly.current = await api(url, { method: "POST", json: { confirm: true } });
        renderWeek();
        if (weekly.current.notices?.length) weekMsg(weekly.current.notices.map(escapeHtml).join("<br>"), "warn");
      } catch (err2) { uiAlert(err2.message, "出错了"); }
    }
    return;
  }
  const act = t.dataset.act;
  const card = t.closest("[data-ev]");
  if (!act || !card) return;
  const id = Number(card.dataset.ev);
  if (["confirm", "exclude", "reset"].includes(act)) return reviewAction(act, [id]);
  if (act === "edit" || act === "split") return openEvidenceDialog(act, weekly.current.checklist_items.find((x) => x.id === id));
  if (act === "sources") return showEvidenceSources(id);
  if (act === "regen") {
    const ev = weekly.current.checklist_items.find((x) => x.id === id);
    const force = ev.user_edited && ev.origin !== "manual";
    if (force && !(await uiConfirm("这条描述你修改过，重新生成会覆盖你的修改。", { title: "重新生成描述", ok: "覆盖并重新生成", danger: true }))) return;
    t.disabled = true;
    try {
      weekly.current = await api(`/weekly/${rid}/items/${id}/regenerate?force=${force}`, { method: "POST" });
      renderWeek();
    } catch (err) { uiAlert(err.message, "出错了"); t.disabled = false; }
  }
});

async function showEvidenceSources(id) {
  const views = await api(`/weekly/items/${id}/sources`);
  $("#source-title").textContent = "依据";
  $("#source-body").onclick = null;
  $("#source-body").innerHTML = views.map((v) => `
    <p><b>${escapeHtml(v.path)}</b><br><span class="muted">版本 ${escapeHtml(v.versions)} · 记录于 ${escapeHtml(v.captured)}</span></p>
    ${v.notes.map((n) => `<div class="muted">说明：${escapeHtml(n)}</div>`).join("")}
    ${v.locations.length ? `<div class="muted">位置：${escapeHtml(v.locations.slice(0, 8).join("；"))}</div>` : ""}
    ${v.quotes.slice(0, 8).map((q) => `<div>${escapeHtml(q.point)}</div><div class="quote">${escapeHtml(q.quote)}</div>`).join("")}
    ${v.hunks.length ? `<b>变化</b><div class="content-box">${escapeHtml(v.hunks.join("\n"))}</div>` : ""}`).join("<hr>") || "<p class='muted'>没有文件依据（来自你的待办或你补充的）。</p>";
  $("#source-dialog").showModal();
}

function openEvidenceDialog(mode, ev) {
  weekly.editing = { mode, id: ev ? ev.id : null };
  const titles = { edit: "修改记录（修改后还需要确认）", add: "补充文件和待办体现不了的工作（直接算确认）", merge: "合并所选记录", split: "拆分记录" };
  $("#evidence-dialog-title").textContent = titles[mode];
  const split = mode === "split";
  ["#ev-title", "#ev-desc", "#ev-category"].forEach((s) => $(s).classList.toggle("hidden", split));
  ["#ev-split", "#ev-split-hint"].forEach((s) => $(s).classList.toggle("hidden", !split));
  $("#ev-category").classList.toggle("hidden", split || mode === "merge");
  $("#ev-title").value = ev && mode === "edit" ? ev.title : "";
  $("#ev-desc").value = ev && mode === "edit" ? ev.description : "";
  $("#ev-category").value = ev && mode === "edit" ? ev.category : "other";
  $("#ev-title").placeholder = mode === "merge" ? "合并后的标题（不填用第一条的）" : "标题";
  $("#ev-desc").placeholder = mode === "merge" ? "合并后的描述（不填把各条描述拼起来）" : "描述";
  $("#ev-split").value = split && ev ? `${ev.title}｜${ev.description}\n` : "";
  $("#evidence-dialog").showModal();
}

$("#evidence-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { mode, id } = weekly.editing;
  const rid = weekly.current.id;
  const body = { title: $("#ev-title").value.trim() || null, description: $("#ev-desc").value.trim() || null, category: $("#ev-category").value };
  try {
    let r;
    if (mode === "edit") r = await api(`/weekly/${rid}/items/${id}`, { method: "PUT", json: body });
    if (mode === "add") r = await api(`/weekly/${rid}/items`, { method: "POST", json: body });
    if (mode === "merge") {
      r = await api(`/weekly/${rid}/merge`, { method: "POST", json: { ids: [...weekly.selected], title: body.title, description: body.description } });
      weekly.selected.clear();
    }
    if (mode === "split") {
      const parts = $("#ev-split").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [title, ...rest] = l.split(/[｜|]/);
        return { title: title.trim(), description: rest.join("｜").trim() };
      });
      r = await api(`/weekly/${rid}/items/${id}/split`, { method: "POST", json: { parts } });
    }
    $("#evidence-dialog").close();
    weekly.current = r;
    renderWeek();
    loadWeekly(false);
  } catch (err) { uiAlert(err.message, "出错了"); }
});

// ---------- 本周材料、本周说明
function renderWeekMaterials() {
  const r = weekly.current;
  const rows = r.material_ids.map((id) => {
    const m = state.materials.find((x) => x.id === id);
    return `<div class="todo-row">${fileIcon(m?.file_type || "paste")}<span class="grow">${escapeHtml(m ? m.title : `材料 ${id}`)}</span>
      <button class="ghost small" data-unpick="${id}">移出</button></div>`;
  });
  const removed = (r.removed_materials || []).map((x) => `<div class="todo-row muted">《${escapeHtml(x.title)}》已从材料库删除</div>`);
  const suggested = (r.todo_material_ids || []).filter((id) => !r.material_ids.includes(id));
  const hint = suggested.length
    ? `<div class="row gap"><span class="muted small">本周有进展的待办关联了 ${suggested.length} 份不随追踪更新的材料。</span><button class="ghost small" id="add-todo-materials">加入它们</button></div>` : "";
  const body = [...rows, ...removed].join("");
  const empty = `<p class="muted small">尚未加入整份材料。会议纪要等来源可在这里选择；文件变化和待办进展通过“整理本周工作”进入上方核对清单。</p>`;
  $("#week-materials").innerHTML = (body || (hint ? "" : empty)) + hint;
}

async function setWeekMaterials(ids) {
  const rid = await ensureWeek();
  weekly.current = await api(`/weekly/${rid}/materials`, { method: "PUT", json: { material_ids: ids } });
  renderWeek();
}

$("#week-pick").addEventListener("click", async () => {
  const r = weekly.current;
  const ids = await pickMaterials({ title: "加入本周材料", ok: "加入", exclude: r.material_ids, suggested: r.todo_material_ids || [] });
  if (ids?.length) {
    try { await setWeekMaterials([...r.material_ids, ...ids]); } catch (err) { weekMsg(escapeHtml(err.message), "error"); }
  }
});

$("#week-materials").addEventListener("click", async (e) => {
  const r = weekly.current;
  const un = e.target.closest("[data-unpick]");
  try {
    if (un) await setWeekMaterials(r.material_ids.filter((id) => id !== Number(un.dataset.unpick)));
    if (e.target.id === "add-todo-materials") await setWeekMaterials([...new Set([...r.material_ids, ...r.todo_material_ids])]);
  } catch (err) { weekMsg(escapeHtml(err.message), "error"); }
});

$("#week-note").addEventListener("change", async () => {
  const note = $("#week-note").value.trim();
  if (note === (weekly.current.note || "")) return;
  try {
    const rid = await ensureWeek();
    weekly.current = await api(`/weekly/${rid}/note`, { method: "PUT", json: { note } });
    $("#week-note-state").textContent = "已保存";
    setTimeout(() => ($("#week-note-state").textContent = ""), 2000);
    renderWeek();
  } catch (err) { weekMsg(escapeHtml(err.message), "error"); }
});

// ---------- 周报正文
// 出处标记〔E3〕〔1-3〕〔简述〕渲染成可点的小标签（html 为已转义或已净化的 HTML）
function tagify(html) {
  return html.replace(/〔([^〕]*)〕/g, (_, tag) => `<span class="tag" data-tag="${tag}">${tag}</span>`);
}

function stripTags(content) {
  return content.replace(/〔[^〕]*〕/g, "").replace(/[ \t]+\n/g, "\n").trim();
}

function renderReportBox() {
  const r = weekly.current;
  const show = Boolean(r.content) || ["pending", "running", "failed"].includes(r.status);
  $("#week-report-box").classList.toggle("hidden", !show);
  if (!show) return;
  const done = r.status === "done" || Boolean(r.content);
  ["#edit-weekly", "#print-weekly", "#show-tags", "#feishu-weekly", "#mail-weekly"].forEach((s) => ($(s).disabled = !r.content));
  $("#export-md").classList.toggle("hidden", !r.content);
  $("#export-md").href = `/weekly/${r.id}/export.md`;
  const status = $("#weekly-status");
  if (r.status === "pending" || r.status === "running") {
    showNotice(status, `${WEEK_STATUS[r.status]}…逐条提取并核对，材料多时需要几分钟。页面会自动刷新。`);
  } else if (r.status === "failed") {
    showNotice(status, `生成失败：${escapeHtml(r.error)}`, "error");
  } else if (r.checklist === "draft" && r.content) {
    showNotice(status, "生成之后审核清单、本周材料或本周说明有改动，正文还是之前生成的。需要的话点右下角“重新生成”。", "warn");
  } else status.classList.add("hidden");
  const g = r.generation || {};
  if (g.reminders?.length) {
    showNotice($("#weekly-reminders"), `<b>给你的提醒</b>（不会进入周报和导出）<ul>${g.reminders.map((x) => `<li>${tagify(escapeHtml(x))}</li>`).join("")}</ul>`, "warn");
  } else $("#weekly-reminders").classList.add("hidden");
  if (g.warnings?.length) {
    showNotice($("#weekly-warnings"), `<b>系统提示</b><ul>${g.warnings.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`);
  } else $("#weekly-warnings").classList.add("hidden");
  $("#weekly-preview").innerHTML = done ? tagify(renderMarkdown(r.content)) : "";
  $("#weekly-preview").classList.toggle("hide-tags", !$("#show-tags").checked);
  renderWeeklyTodos(r);
  renderWeeklyMeta(r);
  if (r.content && state.mail?.configured) loadMailSends(r.id); else $("#weekly-mail").classList.add("hidden");
  if (r.content && state.feishu?.configured) loadFeishuExports(r.id); else $("#weekly-feishu").classList.add("hidden");
}

// 周报关联的待办：生成时保存的快照（当时的标题、状态、层级），之后改待办不影响这里
function renderWeeklyTodos(r) {
  const refs = r.inputs?.todo_refs || [];
  const box = $("#weekly-todos");
  box.classList.toggle("hidden", !refs.length);
  if (!refs.length) return;
  const groups = {};
  for (const x of refs) (groups[x.todo.parent_title || x.todo.title] ??= []).push(x);
  box.innerHTML = `<details class="meta"><summary>关联待办与生成时状态（历史记录）</summary>${Object.entries(groups).map(([name, xs]) => `<div><b>${escapeHtml(name)}</b>：${xs.map((x) => {
    const status = x.report_status ? `本周报表述：${TODO_STATUS_TEXT[x.report_status]}` : `生成时待办：${TODO_STATUS_TEXT[x.todo.status] || ""}`;
    const who = x.todo.parent_title ? `${escapeHtml(x.todo.title)} · ` : "";
    return `<span class="badge">${who}${escapeHtml(x.evidence?.title || "")}（E${x.evidence_id} · ${status}${x.relation === "material" ? " · 关联文件" : ""}${x.synced ? " · 已同步" : ""}）</span>`;
  }).join(" ")}</div>`).join("")}</details>`;
}

function renderWeeklyMeta(r) {
  const g = r.generation || {};
  const inputs = r.inputs || {};
  const evidence = inputs.evidence_ids || [];
  const removed = r.removed_materials || [];
  const rows = [
    ["审核清单", evidence.length ? `${evidence.length} 条已确认（${evidence.map((i) => `E${i}`).join("、")}）` : "无"],
    ["本周材料", r.material_ids.length ? r.material_ids.map(materialTitle).join("、") : "无"],
    ...(removed.length ? [["已删除的材料", removed.map((x) => `《${x.title}》（生成后从材料库删除，正文不受影响）`).join("、")]] : []),
    ["本周说明", r.note || "无"],
    ["读者 / 署名", `${inputs.audience || "直属上级"} / ${inputs.name || "-"}`],
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
$("#week-report-box").addEventListener("click", (e) => {
  const tag = e.target.closest(".tag");
  if (!tag || !weekly.current) return;
  const r = weekly.current;
  const sources = tag.dataset.tag.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);
  let html = "";
  for (const src of sources) {
    if (src === "简述") {
      html += `<p><b>来自本周说明</b></p><div class="quote">${escapeHtml(r.note || "（空）")}</div>`;
      continue;
    }
    const items = r.items.filter((i) => i.source === src);
    if (/^E\d+$/.test(src)) {
      html += `<p><b>审核清单里的记录 ${escapeHtml(src)}</b></p>`;
    } else {
      const [mid, chunk] = src.split("-");
      html += `<p><b>${escapeHtml(materialTitle(Number(mid)))} 第 ${escapeHtml(chunk || "?")} 块</b></p>`;
    }
    html += items.length
      ? items.map((i) => `<div>${escapeHtml(i.point)}</div><div class="quote">${escapeHtml(i.quote)}</div>`).join("")
      : `<p class="muted">没有找到这一出处的工作项。</p>`;
  }
  $("#source-title").textContent = `出处 ${tag.dataset.tag}`;
  $("#source-body").onclick = null;
  $("#source-body").innerHTML = html;
  $("#source-dialog").showModal();
});

$("#show-tags").addEventListener("change", (e) => $("#weekly-preview").classList.toggle("hide-tags", !e.target.checked));

function exitEdit() {
  $("#weekly-editor").classList.add("hidden");
  $("#weekly-preview").classList.remove("hidden");
}
$("#edit-weekly").addEventListener("click", () => {
  $("#weekly-text").value = weekly.current.content;
  $("#weekly-editor").classList.remove("hidden");
  $("#weekly-preview").classList.add("hidden");
});
$("#cancel-edit").addEventListener("click", exitEdit);
$("#save-weekly").addEventListener("click", async () => {
  try {
    weekly.current = await api(`/weekly/${weekly.current.id}/content`, { method: "PUT", json: { content: $("#weekly-text").value } });
    exitEdit();
    renderWeek();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

// 历史版本：重新生成、手动修改前的正文
$("#versions-weekly").addEventListener("click", async () => {
  const versions = await api(`/weekly/${weekly.current.id}/versions`);
  const reason = { regenerate: "重新生成前", edit: "修改前", restore: "恢复前" };
  $("#source-title").textContent = "历史版本";
  $("#source-body").innerHTML = versions.length
    ? versions.map((v) => `<details class="meta"><summary>${fmtTime(v.created_at)} · ${reason[v.reason] || v.reason}
        <button class="ghost small" data-restore="${v.id}">恢复这个版本</button></summary>
        <div class="content-box">${escapeHtml(v.content)}</div></details>`).join("")
    : `<p class="muted">还没有历史版本。重新生成或修改正文时，之前的内容会存在这里。</p>`;
  $("#source-body").onclick = async (e) => {
    const b = e.target.closest("[data-restore]");
    if (!b) return;
    e.preventDefault();
    weekly.current = await api(`/weekly/${weekly.current.id}/versions/${b.dataset.restore}/restore`, { method: "POST" });
    $("#source-dialog").close();
    renderWeek();
  };
  $("#source-dialog").showModal();
});

// 打印为 PDF：只打印去掉出处标记的正文（提醒不进入导出）
$("#print-weekly").addEventListener("click", () => {
  $("#print-area").innerHTML = renderMarkdown(stripTags(weekly.current.content));
  window.print();
});

// ---------- 写入飞书、发送邮件（只由用户手动操作）
function feishuLinks(exports) {
  return exports.map((x) => `<a href="${escapeHtml(x.url)}" target="_blank" rel="noopener">${escapeHtml(x.title)}</a>（${fmtTime(x.created_at)}）`
    + (x.share_error ? `<br><span class="danger">${escapeHtml(x.share_error)}</span>` : "")).join("<br>");
}

async function loadFeishuExports(id) {
  try {
    const exports = await api(`/weekly/${id}/feishu`);
    if (id !== weekly.current?.id) return;
    if (exports.length) showNotice($("#weekly-feishu"), `<b>已写入飞书</b><br>${feishuLinks(exports.slice(0, 3))}`);
    else $("#weekly-feishu").classList.add("hidden");
  } catch (_) { /* 只是展示历史，失败不影响周报 */ }
}

$("#feishu-weekly").addEventListener("click", async () => {
  const box = $("#weekly-feishu");
  const btn = $("#feishu-weekly");
  btn.disabled = true;
  showNotice(box, "正在写入飞书文档…");
  try {
    const x = await api(`/weekly/${weekly.current.id}/feishu`, { method: "POST" });
    showNotice(box, `<b>已写入飞书</b>（出处标记不写入）<br>${feishuLinks([x])}`, x.share_error ? "warn" : "");
  } catch (err) {
    showNotice(box, escapeHtml(err.message), "error");
  } finally { btn.disabled = false; }
});

function mailLines(sends) {
  return sends.map((x) => `${fmtTime(x.created_at)} ${x.status === "sent" ? "已发送" : "<span class=\"danger\">发送失败</span>"} → ${escapeHtml(x.to)}`
    + (x.cc ? `（抄送 ${escapeHtml(x.cc)}）` : "") + (x.error ? `<br><span class="danger">${escapeHtml(x.error)}</span>` : "")).join("<br>");
}

async function loadMailSends(id) {
  try {
    const sends = await api(`/weekly/${id}/email`);
    if (id !== weekly.current?.id) return;
    if (sends.length) showNotice($("#weekly-mail"), `<b>邮件</b><br>${mailLines(sends.slice(0, 3))}`);
    else $("#weekly-mail").classList.add("hidden");
  } catch (_) { /* 只是展示历史 */ }
}

$("#mail-weekly").addEventListener("click", async () => {
  const answer = await uiPrompt({
    title: "发送周报邮件",
    message: `从 <b>${escapeHtml(state.mail.sender)}</b> 发出，正文去掉出处标记，附件是 Markdown 文件。<br><span class="danger">发出后无法撤回。</span>`,
    fields: [{ name: "to", label: "收件人", value: state.mail?.default_to || "", placeholder: "多个用逗号分隔", required: true }],
    ok: "发送",
  });
  if (!answer) return;
  const btn = $("#mail-weekly");
  btn.disabled = true;
  showNotice($("#weekly-mail"), "正在发送邮件…");
  try {
    const x = await api(`/weekly/${weekly.current.id}/email`, { method: "POST", json: { to: answer.to } });
    showNotice($("#weekly-mail"), `<b>邮件</b><br>${mailLines([x])}`, x.status === "sent" ? "" : "error");
  } catch (err) {
    showNotice($("#weekly-mail"), escapeHtml(err.message), "error");
  } finally { btn.disabled = false; }
});

// ---------- 周报设置
$("#open-settings").addEventListener("click", async () => {
  const s = await api("/weekly-settings");
  weekly.settings = s;
  $("#set-weekday").innerHTML = s.weekdays.map((w, i) => `<option value="${i}">每${w}</option>`).join("");
  $("#set-weekday").value = s.schedule.weekday;
  $("#set-time").value = s.schedule.time;
  $("#set-schedule-on").checked = s.schedule.enabled;
  $("#set-next").textContent = s.schedule.enabled && s.schedule.next ? `下次：${fmtTime(s.schedule.next)}` : "";
  const status = { done: "完成", failed: "失败", running: "执行中", interrupted: "中断" };
  $("#set-runs").innerHTML = s.runs.map((r) => `<div class="muted">${fmtTime(r.occurrence)} 第 ${r.attempt} 次 [${status[r.status] || r.status}]${r.late_minutes ? `，晚 ${r.late_minutes} 分钟补做` : ""}${r.error ? "：" + escapeHtml(r.error.slice(0, 80)) : ""}</div>`).join("");
  $("#set-audience").value = s.audience;
  $("#set-author").value = s.author;
  $("#set-owners").value = s.owner_names.join("、");
  $("#settings-dialog").showModal();
});

$("#settings-save").addEventListener("click", async () => {
  try {
    await api("/weekly-settings", {
      method: "PUT",
      json: { audience: $("#set-audience").value, author: $("#set-author").value, owner_names: $("#set-owners").value.split(/[,，、;；\s]+/).filter(Boolean) },
    });
    if ($("#set-schedule-on").checked) {
      await api("/weekly-settings/schedule", { method: "PUT", json: { weekday: Number($("#set-weekday").value), time: $("#set-time").value } });
    } else if (weekly.settings?.schedule?.enabled) {
      await api("/weekly-settings/schedule", { method: "DELETE" });
    }
    $("#settings-dialog").close();
    await loadWeekly(false);
    renderWeek();
  } catch (err) { uiAlert(err.message, "出错了"); }
});
