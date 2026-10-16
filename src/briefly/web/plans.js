// 周报页的“周报计划”（接口里叫集合）：本周准备（候选记录审核 + 生成周报）、追踪的文件（文件树、版本历史）、设置。
// 依赖 app.js 里的 $、api、state、escapeHtml、showNotice、fmtTime、fileIcon、kindName、showWeeklyView、openWeekly、loadWeeklyList。
"use strict";

const plans = { list: [], id: null, sub: "week", prep: null, selected: new Set(), editing: null, collapsed: new Set() };
const EV_STATUS = { PENDING: "待确认", CONFIRMED: "已确认", REJECTED: "已排除" };
const EV_CATEGORY = { done: "已完成", in_progress: "进行中", plan: "计划", issue: "问题风险", other: "其他" };
const ATTR_TEXT = { ATTRIBUTED: "有作者记录", USER_CONFIRMED: "你已确认", INFERRED: "推断", UNKNOWN: "无法判断", EXCLUDED: "已排除" };
const FLAG_TEXT = {
  first_seen: "本周新发现的文件", possibly_moved: "可能移动/改名", reverted: "中间有过增删", gap: "检查中断过",
  parse_failed: "解析失败", format_only: "只改了格式", pdf_text: "PDF 可能有提取误差", truncated: "变化过多已截断",
  others: "有他人的修订", mixed: "你和他人都有修订", no_owner: "未设置你在文档里的名字", deleted: "文件被删除",
  extract_failed: "部分变化未能概括", done_downgraded: "没写“完成”，不按已完成算",
  increment: "审核之后的新进展",
};
const TODO_STATUS_TEXT = { todo: "未开始", doing: "进行中", done: "已完成" };

// 记录关联的待办：所属（父 › 子）、现在的状态、本次报告的状态（只影响本次）、同步到待办（需要确认）
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
      <span class="muted">现在${TODO_STATUS_TEXT[t.status] || t.status}</span>
      ${editable ? `<select data-todo-status="${t.todo_id}" title="本次周报里这项工作的状态；只影响这次周报，不改待办">${options}</select>` : ""}${sync}</span>`;
  }).join("")}</div>`;
}

function evidenceEvents(e) {
  const link = (e.todos || []).find((t) => t.relation === "activity");
  const events = link?.snapshot?.events || [];
  return events.length ? `<ul class="ev-events">${events.map((x) => `<li>${fmtTime(x.at)} ${escapeHtml(x.text)}</li>`).join("")}</ul>` : "";
}
const FILE_STATUS = { present: "正常", missing: "不在了", unreachable: "无法访问", untracked: "已不追踪" };

function planMsg(html, kind = "") {
  showNotice($("#plan-msg"), html, kind);
}

function currentPlan() {
  return plans.list.find((p) => String(p.id) === String(plans.id));
}

function scanSummary(scan) {
  if (!scan) return "";
  const s = scan.stats || {};
  const extra = [...(s.unstable || []).map((x) => `跳过（可能正在写入）：${x}`), ...(s.errors || [])];
  return `检查${scan.status === "done" ? "完成" : "失败"}：${s.files || 0} 个文件，修改 ${s.modified || 0}，新出现 ${s.created || 0}，删除 ${s.deleted || 0}，可能移动 ${s.possibly_moved || 0}，无法访问 ${s.unreachable || 0}`
    + (s.gap ? `<br>注意：${escapeHtml(s.gap.since)} 之后漏了 ${s.gap.missed_days.length} 次每日检查，这期间的变化合并为一次` : "")
    + extra.map((x) => `<br>${escapeHtml(x)}`).join("");
}

function thisWeek() {
  const d = new Date();
  const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  return [iso(monday), iso(sunday)];
}

// ---------- 左栏：周报计划列表
async function loadPlans() {
  try { plans.list = await api("/tracking/collections"); } catch (_) { plans.list = []; }
  renderPlanList();
  renderTrackedSummary();
  if (state.materials) renderMaterialRows(); // 材料的“追踪”一栏要知道有哪些计划
  if (plans.id && currentPlan()) {  // 计划的文件数、最近检查时间变了：本周准备页跟着刷新
    renderWeekLabel();
    if (plans.sub === "week") renderPrep();
  }
  return plans.list;
}

function renderPlanList() {
  $("#plan-list").innerHTML = plans.list.length
    ? plans.list.map((p) => {
      const pending = p.this_week?.pending || 0;
      const when = p.schedule?.enabled ? p.schedule.text : "未设定时";
      return `<li data-plan="${p.id}" class="${String(p.id) === String(plans.id) ? "active" : ""}">
        <span class="row gap"><span class="grow">${escapeHtml(p.name)}</span>${pending ? `<span class="count-badge" title="本周待确认">${pending}</span>` : ""}</span>
        <small>${escapeHtml(when)} · ${p.files} 个文件</small></li>`;
    }).join("")
    : `<li class="muted" style="cursor:default">还没有周报计划</li>`;
}

$("#plan-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-plan]");
  if (li) openPlan(li.dataset.plan);
});

$("#new-plan").addEventListener("click", async () => {
  const answer = await uiPrompt({
    title: "新建周报计划",
    message: "一个计划对应一条周报线，比如一个项目或一门课。之后可以添加要追踪的文件、设定每周几自动准备。",
    fields: [
      { name: "name", label: "名字", placeholder: "如 项目A 周报", required: true },
      { name: "audience", label: "读者（选填）", placeholder: "如 直属上级、导师" },
      { name: "author", label: "署名（选填）", placeholder: "写在周报上的名字" },
    ],
    ok: "创建",
  });
  if (!answer) return;
  try {
    const c = await api("/tracking/collections", { method: "POST", json: { name: answer.name, owner_names: [] } });
    if (answer.audience || answer.author) {
      await api(`/tracking/collections/${c.id}/settings`, { method: "PUT", json: { audience: answer.audience, author: answer.author } });
    }
    await loadPlans();
    await openPlan(c.id, "files");
    planMsg("周报计划已创建。下一步：添加要追踪的文件或文件夹；也可以先到“设置”里设定每周几自动准备。");
  } catch (err) { uiAlert(err.message, "出错了"); }
});

// 从提醒条跳到某个计划的本周准备
async function gotoPlan(id) {
  document.querySelector('.tabs button[data-tab="weekly"]').click();
  await loadPlans();
  openPlan(id, "week");
}

// ---------- 打开计划
async function openPlan(id, sub = "week") {
  if (typeof stopPolling === "function") stopPolling();
  state.weeklyId = null;
  showWeeklyView("plan");
  plans.id = id;
  plans.selected.clear();
  renderPlanList();
  document.querySelectorAll("#weekly-list li").forEach((li) => li.classList.remove("active"));
  $("#plan-msg").classList.add("hidden");
  const p = currentPlan();
  if (!p) return;
  $("#plan-title").textContent = p.name;
  $("#plan-audience").value = p.settings?.audience || "";
  $("#plan-author").value = p.settings?.author || "";
  $("#set-audience").value = p.settings?.audience || "";
  $("#set-author").value = p.settings?.author || "";
  $("#set-owners").value = (p.owner_names || []).join("、");
  const [start, end] = thisWeek();
  $("#prep-start").value = start;
  $("#prep-end").value = end;
  showSub(sub);
  renderPlanMaterials();
  await Promise.all([loadPrep(), loadFiles(), loadSchedule()]);
}

function showSub(sub) {
  plans.sub = sub;
  document.querySelectorAll("#plan-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  ["week", "files", "settings"].forEach((k) => $(`#plan-${k}`).classList.toggle("hidden", k !== sub));
}

$("#plan-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-sub]");
  if (b) showSub(b.dataset.sub);
});

// ---------- 本周准备
function renderWeekLabel() {
  const p = currentPlan();
  const start = $("#prep-start").value, end = $("#prep-end").value;
  const [ws, we] = thisWeek();
  const label = start === ws && end === we ? "本周" : "周期";
  $("#week-label").textContent = `${label} ${shortDate(start)}–${shortDate(end)}`
    + (p?.last_scan ? ` · 最近检查：${fmtTime(p.last_scan)}` : " · 还没有检查过");
}

async function loadPrep() {
  renderWeekLabel();
  const params = new URLSearchParams({ start: $("#prep-start").value, end: $("#prep-end").value });
  plans.prep = await api(`/tracking/collections/${plans.id}/prep?${params}`);
  renderPrep();
}
["#prep-start", "#prep-end"].forEach((s) => $(s).addEventListener("change", () => plans.id && loadPrep()));

$("#scan-btn").addEventListener("click", async () => {
  planMsg("正在检查更新…");
  try {
    planMsg(scanSummary(await api(`/tracking/collections/${plans.id}/scan`, { method: "POST" })));
    await loadPlans();
    renderWeekLabel();
    loadFiles();
  } catch (err) { planMsg(escapeHtml(err.message), "error"); }
});

$("#prepare-btn").addEventListener("click", async () => {
  const btn = $("#prepare-btn");
  btn.disabled = true;
  planMsg("正在检查更新并整理本周工作…（文件多时需要一两分钟）");
  try {
    const r = await api(`/tracking/collections/${plans.id}/prepare`, { method: "POST", json: { start: $("#prep-start").value, end: $("#prep-end").value } });
    plans.prep = r;
    planMsg(`新整理出 ${r.build.created} 条，未变化 ${r.build.unchanged} 条。` + (r.scan ? "<br>" + scanSummary(r.scan) : "")
      + r.notes.map((n) => `<br>注意：${escapeHtml(n)}`).join(""));
    renderPrep();
    await loadPlans();
    renderWeekLabel();
    loadFiles();
  } catch (err) { planMsg(escapeHtml(err.message), "error"); } finally { btn.disabled = false; }
});

function evidenceHtml(e) {
  const files = [...new Set(e.source_refs.map((r) => (r.path || "").replace(/\\/g, "/").split("/").pop()).filter(Boolean))].join("、");
  const versions = e.source_refs.filter((r) => r.to_version).map((r) => `v${r.from_version || "-"}→v${r.to_version}`).join("，");
  const flags = e.flags.filter((f) => FLAG_TEXT[f]).map((f) => `<span class="badge warn">${FLAG_TEXT[f]}</span>`).join(" ");
  const cls = e.review_status === "CONFIRMED" ? "confirmed" : e.review_status === "REJECTED" ? "rejected" : "";
  return `<div class="ev ${cls}" data-ev="${e.id}">
    <div class="ev-head"><input type="checkbox" data-pick="${e.id}" ${plans.selected.has(e.id) ? "checked" : ""}>
      <b>${escapeHtml(e.title)}${e.user_edited && e.origin !== "manual" ? ' <span class="badge">你改过</span>' : ""}${e.has_update ? ' <span class="badge warn">有新变化，见新的待确认项</span>' : ""}</b>
      <span class="badge">${EV_CATEGORY[e.category] || e.category}</span></div>
    <div>${escapeHtml(e.description)}</div>${evidenceEvents(e)}
    <div class="ev-meta">归属：${e.origin === "todo" ? (e.attribution === "USER_CONFIRMED" ? "你已确认" : "你的待办") : (ATTR_TEXT[e.attribution] || e.attribution)}${e.attribution_basis.length && !["manual", "todo"].includes(e.origin) ? "（" + escapeHtml(e.attribution_basis[0]) + "）" : ""}
      · 依据：${files ? escapeHtml(files) + " " + versions : e.origin === "todo" ? "你的待办记录" : "你补充的"} ${flags}</div>${evidenceTodos(e)}
    <div class="ev-actions">
      ${e.review_status !== "CONFIRMED" ? `<button class="ghost" data-act="confirm">确认是我的</button>` : ""}
      ${e.review_status !== "REJECTED" ? `<button class="ghost" data-act="exclude">排除</button>` : ""}
      ${e.review_status !== "PENDING" && e.origin !== "manual" ? `<button class="ghost" data-act="reset">撤销</button>` : ""}
      <button class="ghost" data-act="edit">修改</button>
      ${e.source_refs.length ? `<button class="ghost" data-act="sources">看依据</button><button class="ghost" data-act="split">拆分</button><button class="ghost" data-act="regen">重新生成描述</button>` : ""}
    </div></div>`;
}

function renderPrep() {
  const box = $("#prep-box");
  const p = plans.prep;
  const plan = currentPlan();
  if (plan && !plan.sources && !p) {
    box.innerHTML = `<div class="notice">这个计划还没有追踪的文件。可以到“追踪的文件”里添加；只用待办也行：点“准备本周工作”，整理待办这周的进展交给你确认。
      只想用材料写周报，可以用左侧的“临时周报”。</div>`;
  } else if (!p) {
    box.innerHTML = `<p class="muted">还没有整理这个周期的工作。点“准备本周工作”：检查更新，对比这个周期的文件变化、整理待办的进展，交给你确认（文件变化会调用模型）。</p>`;
  } else {
    const groups = ["PENDING", "CONFIRMED", "REJECTED"].map((s) => [s, p.items.filter((e) => e.review_status === s)]);
    box.innerHTML = `
      <div class="prep-actions">
        <b>本周工作</b>
        <span class="muted">待确认 ${p.counts.PENDING} · 已确认 ${p.counts.CONFIRMED} · 已排除 ${p.counts.REJECTED}</span>
        ${p.weekly_report_id ? `<a href="#" id="open-weekly">查看已生成的周报</a>` : ""}
      </div>
      ${p.notes.map((n) => `<div class="notice warn">${escapeHtml(n)}</div>`).join("")}
      ${p.items.length ? `<div class="prep-actions">
        <button class="ghost" id="bulk-confirm">确认所选</button><button class="ghost" id="bulk-exclude">排除所选</button>
        <button class="ghost" id="bulk-merge">合并所选</button></div>` : `<p class="muted">这个周期没有发现文件变化，待办也没有进展。</p>`}
      ${groups.filter(([, items]) => items.length).map(([s, items]) => `<div class="ev-group"><h5>${EV_STATUS[s]}（${items.length}）</h5>${items.map(evidenceHtml).join("")}</div>`).join("")}
      <button class="ghost" id="add-evidence">＋ 补充文件里没体现的工作</button>`;
  }
  renderPlanMaterials();
  updateMakeReport();
}

function updateMakeReport() {
  const p = plans.prep;
  const btn = $("#make-report");
  let hint = "";
  if (!p) hint = "先准备本周工作";
  else if (p.counts.PENDING) hint = `还有 ${p.counts.PENDING} 条待确认`;
  btn.disabled = Boolean(hint);
  btn.textContent = p?.weekly_report_id ? "重新生成周报" : "生成周报";
  $("#make-report-hint").textContent = hint;
}

async function reviewAction(action, ids, note = "") {
  if (!ids.length) return uiAlert("先勾选要操作的记录。");
  try {
    plans.prep = await api(`/tracking/preps/${plans.prep.id}/review`, { method: "POST", json: { action, ids, note } });
    ids.forEach((i) => plans.selected.delete(i));
    renderPrep();
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
}

$("#prep-box").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-todo-status]");
  if (sel) {
    const id = Number(sel.closest("[data-ev]").dataset.ev);
    try {
      plans.prep = await api(`/tracking/preps/${plans.prep.id}/evidence/${id}/todos/${sel.dataset.todoStatus}`, { method: "PUT", json: { status: sel.value || null } });
      renderPrep();
      loadPlans();
    } catch (err) { uiAlert(err.message, "出错了"); }
    return;
  }
  const cb = e.target.closest("[data-pick]");
  if (cb) cb.checked ? plans.selected.add(Number(cb.dataset.pick)) : plans.selected.delete(Number(cb.dataset.pick));
});

$("#prep-box").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.id === "bulk-confirm") return reviewAction("confirm", [...plans.selected]);
  if (t.id === "bulk-exclude") return reviewAction("exclude", [...plans.selected]);
  if (t.id === "bulk-merge") {
    if (plans.selected.size < 2) return uiAlert("合并至少要勾选两条记录。");
    return openEvidenceDialog("merge", null);
  }
  if (t.id === "add-evidence") {
    if (!plans.prep) return uiAlert("先点“准备本周工作”，再补充文件里没体现的工作。");
    return openEvidenceDialog("add", null);
  }
  if (t.dataset.sync) {
    const id = Number(t.closest("[data-ev]").dataset.ev);
    const url = `/tracking/preps/${plans.prep.id}/evidence/${id}/todos/${t.dataset.sync}/sync`;
    try {
      await api(url, { method: "POST", json: { confirm: false } });
    } catch (err) {
      if (!err.needsConfirm) return uiAlert(err.message, "出错了");
      if (!(await uiConfirm(err.message, { title: "同步到待办", ok: "同步" }))) return;
      try {
        const r = await api(url, { method: "POST", json: { confirm: true } });
        plans.prep = r;
        renderPrep();
        if (r.notices?.length) planMsg(r.notices.map(escapeHtml).join("<br>"), "warn");
      } catch (err2) { uiAlert(err2.message, "出错了"); }
    }
    return;
  }
  if (t.id === "open-weekly") {
    e.preventDefault();
    await loadWeeklyList();
    return openWeekly(plans.prep.weekly_report_id);
  }
  const act = t.dataset.act;
  const card = t.closest("[data-ev]");
  if (!act || !card) return;
  const id = Number(card.dataset.ev);
  if (["confirm", "exclude", "reset"].includes(act)) return reviewAction(act, [id]);
  if (act === "edit" || act === "split") return openEvidenceDialog(act, plans.prep.items.find((x) => x.id === id));
  if (act === "sources") return showEvidenceSources(id);
  if (act === "regen") {
    const ev = plans.prep.items.find((x) => x.id === id);
    const force = ev.user_edited && ev.origin !== "manual";
    if (force && !(await uiConfirm("这条描述你修改过，重新生成会覆盖你的修改。", { title: "重新生成描述", ok: "覆盖并重新生成", danger: true }))) return;
    t.disabled = true;
    try {
      await api(`/tracking/preps/${plans.prep.id}/evidence/${id}/regenerate?force=${force}`, { method: "POST" });
      await loadPrep();
    } catch (err) { uiAlert(err.message, "出错了"); t.disabled = false; }
  }
});

function renderPlanMaterials() {
  const box = $("#plan-materials");
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => Number(i.value)));
  const suggested = new Set(plans.prep?.todo_material_ids || []);
  const label = (m) => `<label>${fileIcon(m.file_type || "paste")}<input type="checkbox" value="${m.id}" ${checked.has(m.id) ? "checked" : ""}> ${escapeHtml(m.title)} <span class="muted">（${m.char_count} 字）</span>${suggested.has(m.id) ? ' <span class="badge" title="本周有进展的待办关联了这份材料">本周待办的材料</span>' : ""}</label>`;
  const hint = suggested.size ? `<div class="row gap"><span class="muted small">本周有进展的待办关联了 ${suggested.size} 份不随追踪更新的材料。</span><button type="button" class="ghost small" id="pick-todo-materials">勾选它们</button></div>` : "";
  box.innerHTML = state.materials.length ? hint + state.materials.map(label).join("") : `<p class="muted">材料库是空的。</p>`;
}

// “勾选它们”：把本周有进展的待办关联的材料加进周报素材
$("#plan-materials").addEventListener("click", (e) => {
  if (e.target.id !== "pick-todo-materials") return;
  const ids = new Set(plans.prep?.todo_material_ids || []);
  document.querySelectorAll("#plan-materials input").forEach((i) => { if (ids.has(Number(i.value))) i.checked = true; });
});

// 生成周报：清单还没确认时先确认（有待确认的记录时按钮不可点），再用已确认的记录 + 勾选的材料 + 简述生成
$("#make-report").addEventListener("click", async () => {
  const btn = $("#make-report");
  const p = plans.prep;
  if (!p) return;
  btn.disabled = true;
  btn.textContent = "生成中…";
  planMsg("正在生成周报…（会调用模型，可能需要一两分钟）");
  try {
    if (p.status === "draft") plans.prep = await api(`/tracking/preps/${p.id}/confirm`, { method: "POST" });
    const body = {
      brief: $("#plan-brief").value.trim(),
      audience: $("#plan-audience").value.trim() || null,
      name: $("#plan-author").value.trim() || null,
      material_ids: [...document.querySelectorAll("#plan-materials input:checked")].map((i) => Number(i.value)),
    };
    const r = await api(`/tracking/preps/${p.id}/report`, { method: "POST", json: body });
    if (r.status !== "done") planMsg(`周报生成失败：${escapeHtml(r.error)}`, "error");
    await Promise.all([loadWeeklyList(), loadPlans()]);
    openWeekly(r.weekly_report_id);
  } catch (err) {
    planMsg(escapeHtml(err.message), "error");
    await loadPrep();
  }
});

async function showEvidenceSources(id) {
  const views = await api(`/tracking/evidence/${id}/sources`);
  $("#source-title").textContent = "依据";
  $("#source-body").innerHTML = views.map((v) => `
    <p><b>${escapeHtml(v.path)}</b><br><span class="muted">版本 ${escapeHtml(v.versions)} · 记录于 ${escapeHtml(v.captured)}</span></p>
    ${v.notes.map((n) => `<div class="muted">说明：${escapeHtml(n)}</div>`).join("")}
    ${v.locations.length ? `<div class="muted">位置：${escapeHtml(v.locations.slice(0, 8).join("；"))}</div>` : ""}
    ${v.quotes.slice(0, 8).map((q) => `<div>${escapeHtml(q.point)}</div><div class="quote">${escapeHtml(q.quote)}</div>`).join("")}
    ${v.hunks.length ? `<b>变化</b><div class="content-box">${escapeHtml(v.hunks.join("\n"))}</div>` : ""}`).join("<hr>") || "<p class='muted'>没有文件依据（你补充的记录）。</p>";
  $("#source-dialog").showModal();
}

function openEvidenceDialog(mode, ev) {
  plans.editing = { mode, id: ev ? ev.id : null };
  const titles = { edit: "修改记录（修改后还需要确认）", add: "补充文件里没体现的工作（直接算确认）", merge: "合并所选记录", split: "拆分记录" };
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
  const { mode, id } = plans.editing;
  const pid = plans.prep.id;
  const body = { title: $("#ev-title").value.trim() || null, description: $("#ev-desc").value.trim() || null, category: $("#ev-category").value };
  try {
    if (mode === "edit") await api(`/tracking/preps/${pid}/evidence/${id}`, { method: "PUT", json: body });
    if (mode === "add") await api(`/tracking/preps/${pid}/evidence`, { method: "POST", json: body });
    if (mode === "merge") {
      await api(`/tracking/preps/${pid}/merge`, { method: "POST", json: { ids: [...plans.selected], title: body.title, description: body.description } });
      plans.selected.clear();
    }
    if (mode === "split") {
      const parts = $("#ev-split").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [title, ...rest] = l.split(/[｜|]/);
        return { title: title.trim(), description: rest.join("｜").trim() };
      });
      await api(`/tracking/preps/${pid}/evidence/${id}/split`, { method: "POST", json: { parts } });
    }
    $("#evidence-dialog").close();
    await loadPrep();
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

// ---------- 追踪的文件：按追踪源分组的文件树，文件夹可折叠，子项缩进
const norm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "");
const lower = (p) => norm(p).toLowerCase();

function fileKind(f) {
  return f.file_type === "feishu" ? "feishu" : f.file_type;
}

function fileRow(f, depth, name) {
  const status = f.status !== "present" ? `<span class="badge warn">${FILE_STATUS[f.status] || f.status}</span>` : "";
  return `<div class="tree-row" style="--depth:${depth}">
    <span class="twisty"></span>${fileIcon(fileKind(f))}
    <span class="tree-name" title="${escapeHtml(f.path)}">${escapeHtml(name)}</span>
    ${status}${f.last_error && f.status !== "present" ? `<span class="muted small" title="${escapeHtml(f.last_error)}">${escapeHtml(f.last_error.slice(0, 40))}</span>` : ""}
    <span class="tree-meta">${f.versions} 个版本${f.last_change ? ` · 最近变化 ${fmtTime(f.last_change)}` : ""}</span>
    <button class="ghost small" data-versions="${f.id}">版本历史</button></div>`;
}

// 文件夹下的文件按相对路径建成树：{dirs: {名称: 子树}, files: [[名称, 文件]]}
// 本地文件夹用文件路径减去文件夹路径；飞书文件夹用成员记录里的相对路径（子文件夹/标题）
function buildTree(root, files, relOf = (f) => norm(f.path).slice(norm(root).length + 1)) {
  const tree = { dirs: {}, files: [] };
  for (const f of files) {
    const parts = relOf(f).split("/");
    let node = tree;
    for (const dir of parts.slice(0, -1)) node = node.dirs[dir] ??= { dirs: {}, files: [] };
    node.files.push([parts[parts.length - 1], f]);
  }
  return tree;
}

function countFiles(node) {
  return node.files.length + Object.values(node.dirs).reduce((n, d) => n + countFiles(d), 0);
}

function renderTree(node, depth, key) {
  const dirs = Object.keys(node.dirs).sort((a, b) => a.localeCompare(b, "zh"));
  const files = node.files.sort((a, b) => a[0].localeCompare(b[0], "zh"));
  return dirs.map((name) => {
    const k = `${key}/${name}`;
    const open = !plans.collapsed.has(k);
    return `<div class="tree-row dir" style="--depth:${depth}" data-toggle="${escapeHtml(k)}">
        <span class="twisty">${open ? "▾" : "▸"}</span>${fileIcon("folder")}
        <span class="tree-name">${escapeHtml(name)}</span><span class="tree-meta">${countFiles(node.dirs[name])} 个文件</span></div>`
      + (open ? renderTree(node.dirs[name], depth + 1, k) : "");
  }).join("") + files.map(([name, f]) => fileRow(f, depth, name)).join("");
}

async function loadFiles() {
  const [sources, files] = await Promise.all([
    api(`/tracking/collections/${plans.id}/sources`),
    api(`/tracking/collections/${plans.id}/files`),
  ]);
  const used = new Set();
  const blocks = sources.map((s) => {
    const inFeishuFolder = (f) => (f.folders || []).find((m) => m.source_id === s.id);
    const own = s.kind === "folder" ? files.filter((f) => lower(f.path).startsWith(lower(s.path) + "/"))
      : s.kind === "feishu_folder" ? files.filter(inFeishuFolder)
        : files.filter((f) => lower(f.path) === lower(s.path) || lower(f.path).startsWith(lower(s.path) + "/"));
    own.forEach((f) => used.add(f.id));
    const paused = s.status === "paused" ? `<span class="badge">已暂停</span>` : "";
    const error = s.last_error ? `<span class="badge warn" title="${escapeHtml(s.last_error)}">${escapeHtml(s.last_error.slice(0, 50))}</span>` : "";
    const actions = `${s.status === "active" ? `<button class="ghost small" data-src="${s.id}" data-act="pause">暂停</button>`
      : `<button class="ghost small" data-src="${s.id}" data-act="resume">恢复</button>`}
      <button class="ghost small danger" data-src="${s.id}" data-act="remove">移除</button>`;
    if (s.kind === "folder" || s.kind === "feishu_folder") {
      const key = `s${s.id}`;
      const open = !plans.collapsed.has(key);
      const feishu = s.kind === "feishu_folder";
      const label = feishu ? `${s.folder?.name || "飞书文件夹"}（飞书）` : `${s.path}${s.recursive ? "" : "（仅第一层）"}`;
      const meta = feishu
        ? `${own.length} 篇文档${s.folder?.skipped ? ` · ${s.folder.skipped} 个非文档未追踪` : ""}`
        : `${own.length} 个文件`;
      const tree = feishu ? buildTree("", own, (f) => inFeishuFolder(f).rel_path) : buildTree(s.path, own);
      return `<div class="tree-source">
        <div class="tree-row dir root" style="--depth:0" data-toggle="${key}">
          <span class="twisty">${open ? "▾" : "▸"}</span>${fileIcon("folder")}
          <span class="tree-name" title="${escapeHtml(feishu ? "新加进这个飞书文件夹的文档会自动追踪" : s.path)}">${escapeHtml(label)}</span>
          ${paused}${error}<span class="tree-meta">${meta}</span>${actions}</div>
        ${open ? (own.length ? renderTree(tree, 1, key) : `<div class="tree-row muted" style="--depth:1">（没有可追踪的文件）</div>`) : ""}</div>`;
    }
    const f = own[0];
    const title = s.kind === "feishu" ? (f ? norm(f.path).split("/").pop() : "飞书文档") : norm(s.path).split("/").pop();
    const kind = s.kind === "feishu" ? "feishu" : (f ? fileKind(f) : norm(s.path).split(".").pop());
    return `<div class="tree-source"><div class="tree-row root" style="--depth:0">
      <span class="twisty"></span>${fileIcon(kind)}
      <span class="tree-name" title="${escapeHtml(f ? f.path : s.path)}">${escapeHtml(title)}</span>${paused}${error}
      ${f ? `<span class="tree-meta">${f.versions} 个版本${f.last_change ? ` · 最近变化 ${fmtTime(f.last_change)}` : ""}</span><button class="ghost small" data-versions="${f.id}">版本历史</button>` : ""}
      ${actions}</div></div>`;
  });
  const others = files.filter((f) => !used.has(f.id));
  if (others.length) {
    blocks.push(`<div class="tree-source"><div class="tree-row root muted" style="--depth:0"><span class="twisty"></span>已不在追踪范围的文件（历史版本保留）</div>
      ${others.map((f) => fileRow(f, 1, norm(f.path).split("/").pop())).join("")}</div>`);
  }
  $("#source-tree").innerHTML = blocks.length ? blocks.join("") : `<p class="muted">还没有追踪的文件。点下面的“添加要追踪的文件”。</p>`;
  $("#add-source-box").open = !sources.length;
}

$("#source-tree").addEventListener("click", async (e) => {
  const toggle = e.target.closest("[data-toggle]");
  const versions = e.target.closest("[data-versions]");
  const src = e.target.closest("[data-src]");
  if (versions) return showVersions(Number(versions.dataset.versions), versions.closest(".tree-row").querySelector(".tree-name").textContent);
  if (src) {
    if (src.dataset.act === "remove" && !(await uiConfirm("不会删除文件，记录的历史版本保留。", { title: "不再追踪这一项", ok: "移除", danger: true }))) return;
    try {
      const r = await api(`/tracking/sources/${src.dataset.src}/${src.dataset.act}`, { method: "POST" });
      if (r.scan) planMsg(scanSummary(r.scan));
      await loadPlans();
      loadFiles();
    } catch (err) { planMsg(escapeHtml(err.message), "error"); }
    return;
  }
  if (toggle) {
    const k = toggle.dataset.toggle;
    plans.collapsed.has(k) ? plans.collapsed.delete(k) : plans.collapsed.add(k);
    loadFiles();
  }
});

$("#source-add").addEventListener("click", async () => {
  const paths = $("#source-paths").value.split("\n").map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean);
  if (!paths.length) return;
  planMsg("正在添加并记录初始版本…");
  try {
    const r = await api(`/tracking/collections/${plans.id}/sources`, { method: "POST", json: { paths, recursive: $("#source-recursive").checked } });
    const lines = [...r.added.map((s) => `已添加：${escapeHtml(s.path)}`), ...r.messages.map(escapeHtml)];
    planMsg(lines.join("<br>"));
    $("#source-paths").value = "";
    await loadPlans();
    loadFiles();
  } catch (err) { planMsg(escapeHtml(err.message), "error"); }
});

// 版本历史：每个版本与上一个版本的差异
async function showVersions(fileId, name) {
  const versions = await api(`/tracking/files/${fileId}/versions`);
  $("#source-title").textContent = `版本历史：${name}`;
  $("#source-body").innerHTML = `<div class="versions">${versions.map((v) => `
    <div class="version-row"><b>v${v.version}</b> <span class="muted">${escapeHtml(v.kind)} · ${fmtTime(v.captured_at)}${v.gap_before ? " · 之前漏了检查，变化合并为一次" : ""}${v.parse_error ? " · 解析失败" : ""}</span>
      ${v.version > 1 ? `<button class="ghost small" data-diff="${v.version}">看变化</button>` : ""}</div>`).join("")}</div>
    <div id="diff-box"></div>`;
  $("#source-body").onclick = async (e) => {
    const b = e.target.closest("[data-diff]");
    if (!b) return;
    const d = await api(`/tracking/files/${fileId}/diff?version=${b.dataset.diff}`);
    $("#diff-box").innerHTML = `<b>v${d.from_version} → v${d.to_version}</b>`
      + d.notes.map((n) => `<div class="muted">${escapeHtml(n)}</div>`).join("")
      + (d.hunks.length ? `<div class="content-box">${escapeHtml(d.hunks.join("\n"))}</div>` : `<p class="muted">正文没有变化。</p>`)
      + (d.truncated ? `<div class="muted">另有 ${d.truncated} 处变化未显示</div>` : "");
  };
  $("#source-dialog").showModal();
}

// ---------- 设置
async function loadSchedule() {
  const s = await api(`/tracking/collections/${plans.id}/schedule`);
  $("#schedule-weekday").innerHTML = s.weekdays.map((w, i) => `<option value="${i}">每${w}</option>`).join("");
  $("#schedule-weekday").value = s.schedule ? s.schedule.weekday : 4;
  if (s.schedule) $("#schedule-time").value = s.schedule.time;
  $("#schedule-text").textContent = s.schedule ? `当前：${s.schedule.text}` : "未设置";
  const status = { done: "完成", failed: "失败", running: "执行中", interrupted: "中断" };
  $("#schedule-runs").innerHTML = s.runs.map((r) => `<div class="muted">${fmtTime(r.occurrence)} 第 ${r.attempt} 次 [${status[r.status] || r.status}]${r.late_minutes ? `，晚 ${r.late_minutes} 分钟补做` : ""}${r.error ? "：" + escapeHtml(r.error.slice(0, 80)) : ""}</div>`).join("");
}

$("#schedule-save").addEventListener("click", async () => {
  try {
    await api(`/tracking/collections/${plans.id}/schedule`, { method: "PUT", json: { weekday: Number($("#schedule-weekday").value), time: $("#schedule-time").value } });
    loadSchedule();
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#schedule-off").addEventListener("click", async () => {
  try {
    await api(`/tracking/collections/${plans.id}/schedule`, { method: "DELETE" });
    loadSchedule();
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#settings-save").addEventListener("click", async () => {
  try {
    const r = await api(`/tracking/collections/${plans.id}/settings`, { method: "PUT", json: { audience: $("#set-audience").value, author: $("#set-author").value } });
    $("#plan-audience").value = r.audience;
    $("#plan-author").value = r.author;
    planMsg("已保存：生成周报时自动填写。");
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#owners-save").addEventListener("click", async () => {
  try {
    const owners = $("#set-owners").value.split(/[,，、;；\s]+/).filter(Boolean);
    await api(`/tracking/collections/${plans.id}/owners`, { method: "PUT", json: { name: currentPlan().name, owner_names: owners } });
    planMsg("已保存。之后整理本周工作时，会用这些名字判断改动是不是你做的。");
    loadPlans();
  } catch (err) { uiAlert(err.message, "出错了"); }
});

$("#plan-delete").addEventListener("click", async () => {
  if (!(await uiConfirm("记录的版本和候选记录会删除；原文件和已生成的周报不受影响。", { title: `删除周报计划“${currentPlan()?.name || ""}”`, ok: "删除", danger: true }))) return;
  await api(`/tracking/collections/${plans.id}`, { method: "DELETE" });
  plans.id = null;
  await loadPlans();
  showWeeklyView("empty");
});

// ---------- 材料页：正在追踪的文件（飞书文档的追踪入口在 app.js：材料行的“开启追踪”和云空间弹窗的“追踪…”）
async function renderTrackedSummary() {
  const total = plans.list.reduce((n, p) => n + p.sources, 0);
  $("#tracked-box").classList.toggle("hidden", !total);
  $("#tracked-summary").textContent = `正在追踪的文件（${total}）`;
  if (!$("#tracked-box").open) return;
  const rows = [];
  for (const p of plans.list.filter((x) => x.sources)) {
    const [sources, files] = await Promise.all([api(`/tracking/collections/${p.id}/sources`), api(`/tracking/collections/${p.id}/files`)]);
    for (const s of sources) {
      const f = files.find((x) => lower(x.path).startsWith(lower(s.path)));
      const name = s.kind === "feishu" ? (f ? norm(f.path).split("/").pop() : "飞书文档")
        : s.kind === "feishu_folder" ? `${s.folder?.name || "飞书文件夹"}（飞书）` : s.path;
      const kind = ["folder", "feishu_folder"].includes(s.kind) ? "folder" : s.kind === "feishu" ? "feishu" : norm(s.path).split(".").pop();
      rows.push(`<div class="tree-row" style="--depth:0">${fileIcon(kind)}<span class="tree-name" title="${escapeHtml(s.path)}">${escapeHtml(name)}</span>
        <span class="tree-meta">${escapeHtml(p.name)}</span><a href="#" data-goto-plan="${p.id}">打开</a></div>`);
    }
  }
  $("#tracked-list").innerHTML = rows.join("");
}

$("#tracked-box").addEventListener("toggle", () => renderTrackedSummary());
$("#tracked-list").addEventListener("click", (e) => {
  const a = e.target.closest("[data-goto-plan]");
  if (!a) return;
  e.preventDefault();
  gotoPlan(a.dataset.gotoPlan);
});

loadPlans();
