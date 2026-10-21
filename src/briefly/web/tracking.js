// 材料页的“追踪的文件”：按追踪源分组的文件树（文件夹可折叠、子项缩进）、添加路径、暂停 / 恢复 / 移除、版本历史与差异、检查更新。
// 依赖 app.js 里的 $、api、escapeHtml、showNotice、fmtTime、fileIcon、ui*。
"use strict";

const tracking = { collapsed: new Set(), summary: null };
const FILE_STATUS = { present: "正常", missing: "不在了", unreachable: "无法访问", untracked: "已不追踪" };

function trackingMsg(html, kind = "") {
  showNotice($("#tracking-msg"), html, kind);
}

function scanSummary(scan) {
  if (!scan) return "";
  const s = scan.stats || {};
  const extra = [...(s.unstable || []).map((x) => `跳过（可能正在写入）：${x}`), ...(s.errors || [])];
  return `检查${scan.status === "done" ? "完成" : "失败"}：${s.files || 0} 个文件，修改 ${s.modified || 0}，新出现 ${s.created || 0}，删除 ${s.deleted || 0}，可能移动 ${s.possibly_moved || 0}，无法访问 ${s.unreachable || 0}`
    + (s.gap ? `<br>注意：${escapeHtml(s.gap.since)} 之后漏了 ${s.gap.missed_days.length} 次每日检查，这期间的变化合并为一次` : "")
    + extra.map((x) => `<br>${escapeHtml(x)}`).join("");
}

const norm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "");
const lower = (p) => norm(p).toLowerCase();

function fileKind(f) {
  return f.file_type === "feishu" ? "feishu" : f.file_type;
}

async function loadTrackingSummary() {
  try { tracking.summary = await api("/tracking/summary"); } catch (_) { tracking.summary = null; }
  const s = tracking.summary;
  $("#tracked-count").textContent = s?.sources ? `（${s.files}）` : "";
  $("#tracked-label").textContent = s?.sources
    ? `${s.sources} 个追踪源，${s.files} 个文件` + (s.last_scan ? ` · 最近检查：${fmtTime(s.last_scan)}` : "")
    : "还没有追踪的文件";
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
    const open = !tracking.collapsed.has(k);
    return `<div class="tree-row dir" style="--depth:${depth}" data-toggle="${escapeHtml(k)}">
        <span class="twisty">${open ? "▾" : "▸"}</span>${fileIcon("folder")}
        <span class="tree-name">${escapeHtml(name)}</span><span class="tree-meta">${countFiles(node.dirs[name])} 个文件</span></div>`
      + (open ? renderTree(node.dirs[name], depth + 1, k) : "");
  }).join("") + files.map(([name, f]) => fileRow(f, depth, name)).join("");
}

async function loadTrackedFiles() {
  const [sources, files] = await Promise.all([api("/tracking/sources"), api("/tracking/files"), loadTrackingSummary()]);
  const used = new Set();
  sources.sort((a,b) => Number(Boolean(b.last_error)) - Number(Boolean(a.last_error)) || Number(b.status === "paused") - Number(a.status === "paused"));
  const anomalies = files.filter((f) => f.status !== "present" && f.status !== "untracked").length;
  if (anomalies || sources.some((s) => s.status === "paused" || s.last_error)) $("#tracked-label").textContent += ` · ${anomalies} 个文件异常，${sources.filter((s) => s.status === "paused").length} 个来源暂停`;
  const blocks = sources.map((s) => {
    const inFeishuFolder = (f) => (f.folders || []).find((m) => m.source_id === s.id);
    const own = s.kind === "folder" ? files.filter((f) => lower(f.path).startsWith(lower(s.path) + "/"))
      : s.kind === "feishu_folder" ? files.filter(inFeishuFolder)
        : files.filter((f) => lower(f.path) === lower(s.path) || lower(f.path).startsWith(lower(s.path) + "/"));
    own.forEach((f) => used.add(f.id));
    const paused = s.status === "paused" ? `<span class="badge">已暂停</span>` : "";
    const error = s.last_error ? `<span class="badge warn" title="${escapeHtml(s.last_error)}">${escapeHtml(s.last_error.slice(0, 50))}</span>` : "";
    const actions = `${s.status === "active" ? `<button class="ghost small" data-src="${s.id}" data-act="pause">${uiIcon("pause")}暂停</button>`
      : `<button class="ghost small" data-src="${s.id}" data-act="resume">${uiIcon("play")}恢复</button>`}
      <button class="ghost small danger" data-src="${s.id}" data-act="remove">${uiIcon("trash")}移除</button>`;
    if (s.kind === "folder" || s.kind === "feishu_folder") {
      const key = `s${s.id}`;
      const open = tracking.collapsed.has(key);
      const feishu = s.kind === "feishu_folder";
      const label = feishu ? `${s.folder?.name || "飞书文件夹"}（飞书）` : `${s.path}${s.recursive ? "" : "（仅第一层）"}`;
      const meta = feishu ? `${own.length} 篇文档${s.folder?.skipped ? ` · ${s.folder.skipped} 个非文档未追踪` : ""}` : `${own.length} 个文件`;
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
  $("#source-tree").innerHTML = blocks.length ? blocks.join("") : `<p class="muted" style="padding:0 10px">还没有追踪的文件。点下面的“添加要追踪的文件”。</p>`;
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
      if (r.scan) trackingMsg(scanSummary(r.scan));
      await loadTrackedFiles();
      loadMaterials();
    } catch (err) { trackingMsg(escapeHtml(err.message), "error"); }
    return;
  }
  if (toggle) {
    const k = toggle.dataset.toggle;
    tracking.collapsed.has(k) ? tracking.collapsed.delete(k) : tracking.collapsed.add(k);
    loadTrackedFiles();
  }
});

$("#source-add").addEventListener("click", async () => {
  const paths = $("#source-paths").value.split("\n").map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean);
  if (!paths.length) return;
  trackingMsg("正在添加并记录初始版本…");
  try {
    const r = await api("/tracking/sources", { method: "POST", json: { paths, recursive: $("#source-recursive").checked } });
    trackingMsg([...r.added.map((s) => `已添加：${escapeHtml(s.path)}`), ...r.messages.map(escapeHtml)].join("<br>"));
    $("#source-paths").value = "";
    await loadTrackedFiles();
    loadMaterials();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); }
});

$("#scan-btn").addEventListener("click", async () => {
  const btn = $("#scan-btn");
  btn.disabled = true;
  trackingMsg("正在检查更新…");
  try {
    trackingMsg(scanSummary(await api("/tracking/scan", { method: "POST" })));
    await loadTrackedFiles();
    loadMaterials();
  } catch (err) { trackingMsg(escapeHtml(err.message), "error"); } finally { btn.disabled = false; }
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

// 材料页的两个子栏目
$("#mat-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mt]");
  if (!b) return;
  document.querySelectorAll("#mat-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $("#mat-library").classList.toggle("hidden", b.dataset.mt !== "library");
  $("#mat-tracked").classList.toggle("hidden", b.dataset.mt !== "tracked");
  if (b.dataset.mt === "tracked") loadTrackedFiles();
});

loadTrackingSummary();
