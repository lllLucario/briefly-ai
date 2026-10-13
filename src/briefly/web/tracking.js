// “追踪”页：集合与追踪源、扫描、每周定时、本周候选清单的审核与生成周报。依赖 app.js 里的 $、api、escapeHtml、showNotice、fmtTime。
"use strict";

const tracking = { ref: null, prep: null, selected: new Set(), editing: null };
const EV_STATUS = { PENDING: "待确认", CONFIRMED: "已确认", REJECTED: "已排除" };
const EV_CATEGORY = { done: "已完成", in_progress: "进行中", plan: "计划", issue: "问题风险", other: "其他" };
const ATTR_TEXT = { ATTRIBUTED: "有作者记录", USER_CONFIRMED: "你已确认", INFERRED: "推断", UNKNOWN: "无法判断", EXCLUDED: "已排除" };
const FLAG_TEXT = {
  first_seen: "本周新发现的文件", possibly_moved: "可能移动/改名", reverted: "中间有过增删", gap: "扫描中断过",
  parse_failed: "解析失败", format_only: "只改了格式", pdf_text: "PDF 可能有提取误差", truncated: "变化过多已截断",
  others: "有他人的修订", mixed: "你和他人都有修订", no_owner: "未设置署名", deleted: "文件被删除",
  extract_failed: "部分变化未能概括", done_downgraded: "没写“完成”，不按已完成算",
};
const FILE_STATUS = { present: "正常", missing: "不在了", unreachable: "无法访问", untracked: "已不追踪" };
const SRC_STATUS = { active: "启用", paused: "已暂停", removed: "已移除" };

function trackingMsg(html, kind = "") {
  showNotice($("#tracking-msg"), html, kind);
}

function scanSummary(scan) {
  if (!scan) return "";
  const s = scan.stats || {};
  const extra = [...(s.unstable || []).map((x) => `跳过（可能正在写入）：${x}`), ...(s.errors || [])];
  return `扫描${scan.status === "done" ? "完成" : "失败"}：${s.files || 0} 个文件，新版本 ${s.new_snapshots || 0}，修改 ${s.modified || 0}，新出现 ${s.created || 0}，删除 ${s.deleted || 0}，可能移动 ${s.possibly_moved || 0}，无法访问 ${s.unreachable || 0}`
    + (s.gap ? `<br>注意：${escapeHtml(s.gap.since)} 之后漏了 ${s.gap.missed_days.length} 次每日扫描，这期间的变化合并为一次` : "")
    + extra.map((x) => `<br>${escapeHtml(x)}`).join("");
}

function thisWeek() {
  const d = new Date();
  const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
  const iso = (x) => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  return [iso(monday), iso(sunday)];
}

// ---------- 集合
async function loadTracking() {
  const collections = await api("/tracking/collections");
  $("#collection-list").innerHTML = collections.length
    ? collections.map((c) => `<li data-ref="${c.id}" class="${String(c.id) === String(tracking.ref) ? "active" : ""}">
        ${escapeHtml(c.name)}<small>${c.sources} 个追踪源 · ${c.files} 个文件${c.schedule ? " · " + escapeHtml(c.schedule.text) : ""}</small></li>`).join("")
    : `<li class="muted" style="cursor:default">还没有集合</li>`;
  if (tracking.ref && !collections.some((c) => String(c.id) === String(tracking.ref))) tracking.ref = null;
  if (!tracking.ref && collections.length) await openCollection(collections[0].id);
  else if (tracking.ref) await openCollection(tracking.ref, false);
  else {
    $("#tracking-body").classList.add("hidden");
    $("#tracking-empty").classList.remove("hidden");
  }
}

$("#collection-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-ref]");
  if (li) openCollection(li.dataset.ref);
});

$("#collection-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const owners = $("#collection-owners").value.split(/[,，、\s]+/).filter(Boolean);
    const c = await api("/tracking/collections", { method: "POST", json: { name: $("#collection-name").value, owner_names: owners } });
    $("#collection-name").value = "";
    $("#collection-owners").value = "";
    tracking.ref = c.id;
    await loadTracking();
    trackingMsg("集合已创建。下一步：在“追踪源与文件”里添加要追踪的文件或文件夹。");
  } catch (err) { alert(err.message); }
});

async function openCollection(ref, resetMsg = true) {
  tracking.ref = ref;
  tracking.selected.clear();
  document.querySelectorAll("#collection-list li[data-ref]").forEach((li) => li.classList.toggle("active", li.dataset.ref === String(ref)));
  $("#tracking-empty").classList.add("hidden");
  $("#tracking-body").classList.remove("hidden");
  if (resetMsg) $("#tracking-msg").classList.add("hidden");
  const collections = await api("/tracking/collections");
  const c = collections.find((x) => String(x.id) === String(ref));
  $("#collection-title").textContent = c ? `${c.name}${c.owner_names.length ? "（署名：" + c.owner_names.join("、") + "）" : "（未设置署名）"}` : "";
  const [start, end] = thisWeek();
  if (!$("#prep-start").value) { $("#prep-start").value = start; $("#prep-end").value = end; }
  await Promise.all([loadSources(), loadSchedule(), loadPrep()]);
}

$("#collection-delete").addEventListener("click", async () => {
  if (!confirm("删除这个集合及其全部版本和候选记录？原文件不受影响。")) return;
  await api(`/tracking/collections/${tracking.ref}`, { method: "DELETE" });
  tracking.ref = null;
  loadTracking();
});

$("#scan-btn").addEventListener("click", async () => {
  trackingMsg("正在扫描…");
  try {
    trackingMsg(scanSummary(await api(`/tracking/collections/${tracking.ref}/scan`, { method: "POST" })));
    loadSources();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); }
});

// ---------- 追踪源与文件
async function loadSources() {
  const [sources, files] = await Promise.all([
    api(`/tracking/collections/${tracking.ref}/sources`),
    api(`/tracking/collections/${tracking.ref}/files`),
  ]);
  $("#source-rows").innerHTML = sources.length
    ? sources.map((s) => `<div class="src-row">
        <span class="badge">${SRC_STATUS[s.status] || s.status}</span>
        <span class="path">${s.kind === "folder" ? "📁" : "📄"} ${escapeHtml(s.path)}${s.kind === "folder" && !s.recursive ? "（仅第一层）" : ""}
        ${s.last_error ? `<span class="badge warn">${escapeHtml(s.last_error)}</span>` : ""}</span>
        ${s.status === "active" ? `<button class="ghost" data-src="${s.id}" data-act="pause">暂停</button>` : `<button class="ghost" data-src="${s.id}" data-act="resume">恢复</button>`}
        <button class="ghost danger" data-src="${s.id}" data-act="remove">移除</button></div>`).join("")
    : `<p class="muted">还没有追踪源。</p>`;
  $("#file-rows").innerHTML = files.length
    ? `<b>被追踪的文件（${files.length}）</b>` + files.map((f) => `<div class="file-row">
        <span class="badge ${f.status === "present" ? "" : "warn"}">${FILE_STATUS[f.status] || f.status}</span>
        <span class="path">${escapeHtml(f.path)}</span><span class="muted">${f.versions} 个版本</span>
        ${f.moved_from_id ? `<span class="badge">可能由文件 ${f.moved_from_id} 移动而来</span>` : ""}</div>`).join("")
    : "";
}

$("#source-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-src]");
  if (!b) return;
  if (b.dataset.act === "remove" && !confirm("移除这个追踪源？不会删除文件，历史版本保留。")) return;
  try {
    const r = await api(`/tracking/sources/${b.dataset.src}/${b.dataset.act}`, { method: "POST" });
    if (r.scan) trackingMsg(scanSummary(r.scan));
    loadSources();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); }
});

$("#source-add").addEventListener("click", async () => {
  const paths = $("#source-paths").value.split("\n").map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean);
  if (!paths.length) return;
  trackingMsg("正在添加并采集基线…");
  try {
    const r = await api(`/tracking/collections/${tracking.ref}/sources`, { method: "POST", json: { paths, recursive: $("#source-recursive").checked } });
    const lines = [...r.added.map((s) => `已添加：${escapeHtml(s.path)}`), ...r.messages.map(escapeHtml)];
    trackingMsg(lines.join("<br>") + (r.scan ? "<br>" + scanSummary(r.scan) : ""));
    $("#source-paths").value = "";
    loadSources();
    loadTracking();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); }
});

// ---------- 每周定时
async function loadSchedule() {
  const s = await api(`/tracking/collections/${tracking.ref}/schedule`);
  $("#schedule-weekday").innerHTML = s.weekdays.map((w, i) => `<option value="${i}">每${w}</option>`).join("");
  if (s.schedule) {
    $("#schedule-weekday").value = s.schedule.weekday;
    $("#schedule-time").value = s.schedule.time;
  } else {
    $("#schedule-weekday").value = 4;
  }
  $("#schedule-text").textContent = s.schedule ? `当前：${s.schedule.text}` : "未设置";
  const status = { done: "完成", failed: "失败", running: "执行中", interrupted: "中断" };
  $("#schedule-runs").innerHTML = s.runs.map((r) => `<div class="muted">${fmtTime(r.occurrence)} 第 ${r.attempt} 次 [${status[r.status] || r.status}]${r.late_minutes ? `，晚 ${r.late_minutes} 分钟补做` : ""}${r.error ? "：" + escapeHtml(r.error.slice(0, 80)) : ""}</div>`).join("");
}

$("#schedule-save").addEventListener("click", async () => {
  try {
    await api(`/tracking/collections/${tracking.ref}/schedule`, { method: "PUT", json: { weekday: Number($("#schedule-weekday").value), time: $("#schedule-time").value } });
    loadSchedule();
    loadTracking();
  } catch (err) { alert(err.message); }
});
$("#schedule-off").addEventListener("click", async () => {
  try {
    await api(`/tracking/collections/${tracking.ref}/schedule`, { method: "DELETE" });
    loadSchedule();
  } catch (err) { alert(err.message); }
});

// ---------- 周报准备与审核
async function loadPrep() {
  const params = new URLSearchParams({ start: $("#prep-start").value, end: $("#prep-end").value });
  tracking.prep = await api(`/tracking/collections/${tracking.ref}/prep?${params}`);
  renderPrep();
}
["#prep-start", "#prep-end"].forEach((s) => $(s).addEventListener("change", () => tracking.ref && loadPrep()));

$("#prepare-btn").addEventListener("click", async () => {
  const btn = $("#prepare-btn");
  btn.disabled = true;
  trackingMsg("正在最终扫描并生成候选清单…（文件多时需要一两分钟）");
  try {
    const r = await api(`/tracking/collections/${tracking.ref}/prepare`, { method: "POST", json: { start: $("#prep-start").value, end: $("#prep-end").value } });
    tracking.prep = r;
    trackingMsg(`新候选 ${r.build.created} 条，未变化 ${r.build.unchanged} 条。` + (r.scan ? "<br>" + scanSummary(r.scan) : "") + r.notes.map((n) => `<br>注意：${escapeHtml(n)}`).join(""));
    renderPrep();
    loadSources();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); } finally { btn.disabled = false; }
});

function evidenceHtml(e) {
  const files = [...new Set(e.source_refs.map((r) => (r.path || "").replace(/\\/g, "/").split("/").pop()).filter(Boolean))].join("、");
  const versions = e.source_refs.filter((r) => r.to_version).map((r) => `v${r.from_version || "-"}→v${r.to_version}`).join("，");
  const flags = e.flags.filter((f) => FLAG_TEXT[f]).map((f) => `<span class="badge warn">${FLAG_TEXT[f]}</span>`).join(" ");
  const cls = e.review_status === "CONFIRMED" ? "confirmed" : e.review_status === "REJECTED" ? "rejected" : "";
  const canEdit = true;
  return `<div class="ev ${cls}" data-ev="${e.id}">
    <div class="ev-head"><input type="checkbox" data-pick="${e.id}" ${tracking.selected.has(e.id) ? "checked" : ""}>
      <b>${escapeHtml(e.title)}${e.user_edited && e.origin !== "manual" ? ' <span class="badge">你改过</span>' : ""}${e.has_update ? ' <span class="badge warn">有新变化，见新的待确认项</span>' : ""}</b>
      <span class="badge">${EV_CATEGORY[e.category] || e.category}</span></div>
    <div>${escapeHtml(e.description)}</div>
    <div class="ev-meta">归属：${ATTR_TEXT[e.attribution] || e.attribution}${e.attribution_basis.length && e.origin !== "manual" ? "（" + escapeHtml(e.attribution_basis[0]) + "）" : ""}
      · 来源：${files ? escapeHtml(files) + " " + versions : "你补充的"} ${flags}</div>
    <div class="ev-actions">
      ${e.review_status !== "CONFIRMED" ? `<button class="ghost" data-act="confirm">确认是我的</button>` : ""}
      ${e.review_status !== "REJECTED" ? `<button class="ghost" data-act="exclude">排除</button>` : ""}
      ${e.review_status !== "PENDING" && e.origin !== "manual" ? `<button class="ghost" data-act="reset">撤销</button>` : ""}
      ${canEdit ? `<button class="ghost" data-act="edit">修改</button>` : ""}
      ${e.source_refs.length ? `<button class="ghost" data-act="sources">看依据</button><button class="ghost" data-act="split">拆分</button><button class="ghost" data-act="regen">重新生成描述</button>` : ""}
    </div></div>`;
}

function renderPrep() {
  const box = $("#prep-box");
  const p = tracking.prep;
  if (!p) {
    box.innerHTML = `<p class="muted">这个周期还没有准备。点“准备候选清单”开始。</p>`;
    return;
  }
  const groups = ["PENDING", "CONFIRMED", "REJECTED"].map((s) => [s, p.items.filter((e) => e.review_status === s)]);
  const status = { draft: "草稿（还没确认）", confirmed: "清单已确认", reported: "已生成周报" };
  box.innerHTML = `
    <div class="prep-actions">
      <span class="status-pill ${p.status === "draft" ? "pending" : "done"}">${status[p.status] || p.status}</span>
      <span class="muted">待确认 ${p.counts.PENDING} · 已确认 ${p.counts.CONFIRMED} · 已排除 ${p.counts.REJECTED}</span>
      ${p.weekly_report_id ? `<a href="#" id="open-weekly">查看周报 ${p.weekly_report_id}</a>` : ""}
    </div>
    ${p.notes.map((n) => `<div class="notice warn">${escapeHtml(n)}</div>`).join("")}
    <div class="prep-actions">
      <button class="ghost" id="bulk-confirm">确认所选</button><button class="ghost" id="bulk-exclude">排除所选</button>
      <button class="ghost" id="bulk-merge">合并所选</button><button class="ghost" id="add-evidence">＋ 补充文件没体现的工作</button>
    </div>
    ${p.items.length ? "" : `<p class="muted">本周期没有发现文件变化。可以补充文件体现不了的工作后生成周报。</p>`}
    ${groups.filter(([, items]) => items.length).map(([s, items]) => `<div class="ev-group"><h5>${EV_STATUS[s]}（${items.length}）</h5>${items.map(evidenceHtml).join("")}</div>`).join("")}
    <div class="prep-actions">
      <button class="primary" id="confirm-checklist">确认清单</button>
      <input id="report-brief" placeholder="补充说明（可选）" style="flex:1;min-width:160px">
      <button class="primary" id="make-report" ${p.status === "draft" ? "disabled title='先确认清单'" : ""}>生成周报</button>
    </div>
    <small class="muted">只有你确认的记录会写进周报；还有待确认的记录时不能确认清单（可以随时离开，草稿会保留）。</small>`;
}

async function reviewAction(action, ids, note = "") {
  if (!ids.length) return alert("先勾选记录");
  try {
    tracking.prep = await api(`/tracking/preps/${tracking.prep.id}/review`, { method: "POST", json: { action, ids, note } });
    ids.forEach((i) => tracking.selected.delete(i));
    renderPrep();
  } catch (err) { alert(err.message); }
}

async function refreshPrep() {
  await loadPrep();
}

$("#prep-box").addEventListener("change", (e) => {
  const cb = e.target.closest("[data-pick]");
  if (cb) cb.checked ? tracking.selected.add(Number(cb.dataset.pick)) : tracking.selected.delete(Number(cb.dataset.pick));
});

$("#prep-box").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.id === "bulk-confirm") return reviewAction("confirm", [...tracking.selected]);
  if (t.id === "bulk-exclude") return reviewAction("exclude", [...tracking.selected]);
  if (t.id === "bulk-merge") {
    if (tracking.selected.size < 2) return alert("至少勾选两条记录");
    return openEvidenceDialog("merge", null);
  }
  if (t.id === "add-evidence") return openEvidenceDialog("add", null);
  if (t.id === "open-weekly") {
    e.preventDefault();
    return gotoWeekly(tracking.prep.weekly_report_id);
  }
  if (t.id === "confirm-checklist") {
    try {
      tracking.prep = await api(`/tracking/preps/${tracking.prep.id}/confirm`, { method: "POST" });
      renderPrep();
    } catch (err) { alert(err.message); }
    return;
  }
  if (t.id === "make-report") {
    t.disabled = true;
    t.textContent = "生成中…";
    try {
      const r = await api(`/tracking/preps/${tracking.prep.id}/report`, { method: "POST", json: { brief: $("#report-brief").value } });
      if (r.status !== "done") alert(`周报生成失败：${r.error}`);
      await refreshPrep();
      gotoWeekly(r.weekly_report_id);
    } catch (err) { alert(err.message); t.disabled = false; t.textContent = "生成周报"; }
    return;
  }
  const act = t.dataset.act;
  const card = t.closest("[data-ev]");
  if (!act || !card) return;
  const id = Number(card.dataset.ev);
  if (["confirm", "exclude", "reset"].includes(act)) return reviewAction(act, [id]);
  if (act === "edit" || act === "split") return openEvidenceDialog(act, tracking.prep.items.find((x) => x.id === id));
  if (act === "sources") return showEvidenceSources(id);
  if (act === "regen") {
    const ev = tracking.prep.items.find((x) => x.id === id);
    const force = ev.user_edited && ev.origin !== "manual";
    if (force && !confirm("这条描述你修改过，重新生成会覆盖。继续？")) return;
    t.disabled = true;
    try {
      await api(`/tracking/preps/${tracking.prep.id}/evidence/${id}/regenerate?force=${force}`, { method: "POST" });
      await refreshPrep();
    } catch (err) { alert(err.message); t.disabled = false; }
  }
});

function gotoWeekly(reportId) {
  document.querySelector('.tabs button[data-tab="weekly"]').click();
  setTimeout(() => openWeekly(reportId), 300);
}

async function showEvidenceSources(id) {
  const views = await api(`/tracking/evidence/${id}/sources`);
  $("#source-title").textContent = "依据";
  $("#source-body").innerHTML = views.map((v) => `
    <p><b>${escapeHtml(v.path)}</b><br><span class="muted">版本 ${escapeHtml(v.versions)} · 采集 ${escapeHtml(v.captured)}</span></p>
    ${v.notes.map((n) => `<div class="muted">说明：${escapeHtml(n)}</div>`).join("")}
    ${v.locations.length ? `<div class="muted">位置：${escapeHtml(v.locations.slice(0, 8).join("；"))}</div>` : ""}
    ${v.quotes.slice(0, 8).map((q) => `<div>${escapeHtml(q.point)}</div><div class="quote">${escapeHtml(q.quote)}</div>`).join("")}
    ${v.hunks.length ? `<b>变化</b><div class="content-box">${escapeHtml(v.hunks.join("\n"))}</div>` : ""}`).join("<hr>") || "<p class='muted'>没有文件依据（你补充的记录）。</p>";
  $("#source-dialog").showModal();
}

function openEvidenceDialog(mode, ev) {
  tracking.editing = { mode, id: ev ? ev.id : null };
  const titles = { edit: "修改记录（修改后还需要确认）", add: "补充文件没体现的工作（直接算确认）", merge: "合并所选记录", split: "拆分记录" };
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
  const { mode, id } = tracking.editing;
  const pid = tracking.prep.id;
  const body = { title: $("#ev-title").value.trim() || null, description: $("#ev-desc").value.trim() || null, category: $("#ev-category").value };
  try {
    if (mode === "edit") await api(`/tracking/preps/${pid}/evidence/${id}`, { method: "PUT", json: body });
    if (mode === "add") await api(`/tracking/preps/${pid}/evidence`, { method: "POST", json: body });
    if (mode === "merge") {
      await api(`/tracking/preps/${pid}/merge`, { method: "POST", json: { ids: [...tracking.selected], title: body.title, description: body.description } });
      tracking.selected.clear();
    }
    if (mode === "split") {
      const parts = $("#ev-split").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [title, ...rest] = l.split(/[｜|]/);
        return { title: title.trim(), description: rest.join("｜").trim() };
      });
      await api(`/tracking/preps/${pid}/evidence/${id}/split`, { method: "POST", json: { parts } });
    }
    $("#evidence-dialog").close();
    await refreshPrep();
  } catch (err) { alert(err.message); }
});

// 从提醒条跳到某个集合的审核
function gotoTracking(collectionId) {
  tracking.ref = collectionId;
  document.querySelector('.tabs button[data-tab="tracking"]').click();
}
