/* 知识库：每库草稿与请求归属独立；所有业务写入仍以服务端回执为准。 */
"use strict";
(() => {
  const drafts = new Map();
  let current = "", revision = 0, timer = null;
  const names = {queued:"等待解析", parsing:"解析中", preview:"准备建立索引", indexing:"建立索引中", ready:"已完成", failed:"处理失败", rejected:"未接收"};
  function draft(id = current) {
    if (!drafts.has(id)) drafts.set(id, {files:[], message:"", uploading:false, target:null});
    return drafts.get(id);
  }
  function active(id, rev = revision) { return current === id && revision === rev && $("#tab-knowledge").classList.contains("active"); }
  function message(text, id = current) {
    if (id) draft(id).message = text;
    if (!id || current === id) $("#kb-message").textContent = text;
  }
  let bases = [];
  let createFiles = [], skippedFiles = [], creation = null;
  const supportedFile = /\.(txt|md|doc|docx|pdf|xlsx|xls|csv|pptx|ppt|png|jpg|jpeg|bmp|tif|tiff|webp)$/i;
  const uploadQueues = new Map();
  let inspectionSequence = 0, inspectionSignature = "", folderSupport = null, fileTreeSignature = "";
  function renderBaseCards() {
    $("#kb-base-cards").innerHTML = bases.map(base => `<button type="button" class="kb-base-card" data-kb-base="${escapeHtml(base.id)}" title="${escapeHtml(base.name)}" aria-pressed="${base.id === current}"><span class="start-icon">${uiIcon("folder")}</span><span><strong>${escapeHtml(base.name)}</strong><small>${base.external_allowed ? "已允许外部模型问答" : "外发未开启"}</small></span><span class="kb-card-check" aria-hidden="true">${base.id === current ? "✓" : "→"}</span></button>`).join("");
    $("#kb-current-name").textContent = bases.find(base => base.id === current)?.name || "";
  }
  function renderLibraryState() {
    renderBaseCards();
    $("#kb-library-bar").classList.toggle("hidden", !bases.length);
    $("#kb-empty").classList.toggle("hidden", Boolean(bases.length || creation));
    const box = $("#kb-create-progress");
    box.classList.toggle("hidden", !creation);
    $("#kb-open-create").disabled = Boolean(creation && !creation.error);
    if (!creation) return box.replaceChildren();
    box.innerHTML = `<strong>${creation.error ? "创建未完成" : "正在创建知识库"} · ${escapeHtml(creation.name)}</strong><p>${escapeHtml(creation.error || "创建完成后开始上传所选资料。请保持此网页打开。")}</p>${creation.error ? '<div class="row gap"><button class="ghost small" id="kb-create-refresh">刷新列表核对</button><button class="ghost small" id="kb-create-edit">返回填写</button></div>' : ''}`;
  }
  function renderSelection() {
    $("#kb-create-selection").innerHTML = `${createFiles.length ? `<div class="kb-selection-head"><strong>已选 ${createFiles.length} 份资料</strong><button type="button" class="ghost small" data-kb-clear-files>清空</button></div><ul class="kb-selected-files">${createFiles.map((file, i) => `<li><span>${escapeHtml(file.webkitRelativePath || file.name)}</span><small class="muted">${(file.size / 1024).toFixed(1)} KB</small><button type="button" class="ghost small" data-kb-remove-file="${i}" aria-label="移除 ${escapeHtml(file.name)}">×</button></li>`).join("")}</ul>` : '<p class="muted small">尚未选择资料，请至少添加一个文件。</p>'}
      ${skippedFiles.length ? `<details class="kb-skipped"><summary>有 ${skippedFiles.length} 份文件格式不支持，不会上传</summary><ul>${skippedFiles.map(name => `<li>${escapeHtml(name)}</li>`).join("")}</ul></details>` : ""}`;
  }
  function addCreateFiles(files) {
    const keys = new Set(createFiles.map(file => `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`));
    for (const file of files) {
      if (!supportedFile.test(file.name)) { skippedFiles.push(file.webkitRelativePath || file.name); continue; }
      const key = `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
      if (!keys.has(key)) { createFiles.push(file); keys.add(key); }
    }
    $("#kb-create-error").textContent = "";
    renderSelection();
  }
  function openCreate() {
    if (creation && !creation.error) return;
    $("#kb-create-error").textContent = "";
    renderSelection(); $("#kb-create-dialog").showModal(); $("#kb-name").focus();
  }
  $("#kb-open-create").addEventListener("click", openCreate);
  $("#kb-empty-create").addEventListener("click", openCreate);
  $("#kb-create-close").addEventListener("click", () => $("#kb-create-dialog").close());
  $("#kb-create-cancel").addEventListener("click", () => $("#kb-create-dialog").close());
  $("#kb-create-files").addEventListener("change", event => { addCreateFiles([...event.target.files]); event.target.value = ""; });
  $("#kb-create-folder").addEventListener("change", event => { addCreateFiles([...event.target.files]); event.target.value = ""; });
  $("#kb-create-selection").addEventListener("click", event => {
    const remove = event.target.closest("[data-kb-remove-file]");
    if (remove) createFiles.splice(Number(remove.dataset.kbRemoveFile), 1);
    if (event.target.closest("[data-kb-clear-files]")) { createFiles = []; skippedFiles = []; }
    renderSelection();
  });
  $("#kb-create-progress").addEventListener("click", event => {
    if (event.target.closest("#kb-create-edit")) openCreate();
    if (event.target.closest("#kb-create-refresh")) window.loadKnowledge();
  });
  // 网络传输进度来自浏览器，不用解析耗时推算百分比。分批仍沿用原接口限制及幂等标识。
  async function uploadBatch(kbId, batch, changed) {
    {
      folderSupport ||= api("/openapi.json").then(schema => Boolean(schema.paths["/knowledge-bases/{kb_id}/documents/{doc_id}/versions/{version_id}/outline"]));
      if (!await folderSupport) throw new Error("当前服务尚未启用自动入库，文件尚未发送。请更新服务后重试此批。");
    }
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(), form = new FormData();
      batch.files.forEach(file => form.append("files", file));
      form.append("relative_paths", JSON.stringify(batch.files.map(file => file.webkitRelativePath || "")));
      form.append("request_key", batch.key); form.append("tokens", batch.tokens);
      xhr.open("POST", `/knowledge-bases/${encodeURIComponent(kbId)}/imports`);
      xhr.upload.onprogress = event => { batch.percent = event.lengthComputable ? Math.round(event.loaded / event.total * 100) : null; changed(); };
      xhr.onload = () => {
        let result;
        try { result = JSON.parse(xhr.responseText); } catch (_) { reject(new Error("服务响应无法读取，请核对任务列表后重试此批。")); return; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(result);
        else reject(new Error(typeof result.detail === "string" ? result.detail : "上传未完成，请核对任务列表后重试此批。"));
      };
      xhr.onerror = () => reject(new Error("连接中断，接收结果待核对。请检查任务列表后重试此批；重试沿用原上传编号。"));
      xhr.send(form);
    });
  }
  function renderUploads(id = current) {
    if (id !== current) return;
    const batches = uploadQueues.get(id) || [];
    const pending = batches.some(batch => ["queued", "uploading"].includes(batch.state));
    $("#kb-transfers").innerHTML = (pending ? '<p class="muted small">正在上传资料，请保持此网页打开；可以切换页面继续其他工作。接收完成后会自动解析并建立索引，无需审批。</p>' : '') + batches.filter(batch => batch.state !== "received" || batch.rejected?.length).map(batch => `<article class="kb-transfer" data-kb-transfer="${batch.key}"><strong>${escapeHtml(batch.title)}</strong><span>${batch.state === "queued" ? "等待上传" : batch.state === "uploading" ? batch.percent === 100 ? "文件已传输，等待服务接收结果" : `正在上传${batch.percent == null ? "" : ` · ${batch.percent}%`}` : batch.state === "error" ? "上传未完成" : "部分文件未接收"}</span>
      ${batch.state === "uploading" ? `<progress max="100" ${batch.percent == null ? "" : `value="${batch.percent}"`} aria-label="本批文件传输进度"></progress>` : ""}
      ${batch.error ? `<p class="danger small">${escapeHtml(batch.error)}</p><button class="ghost small" data-kb-transfer-retry="${batch.key}">重试此批</button>` : ""}
      ${(batch.rejected || []).map(file => `<p class="danger small">${escapeHtml(file.filename)}：${escapeHtml(file.error)}</p>`).join("")}</article>`).join("");
  }
  async function runUploads(id) {
    const batches = uploadQueues.get(id) || [];
    if (batches.some(batch => batch.state === "uploading")) return;
    for (const batch of batches) {
      if (batch.state !== "queued") continue;
      batch.state = "uploading"; batch.error = ""; batch.percent = null; renderUploads(id);
      try {
        const result = await uploadBatch(id, batch, () => renderUploads(id));
        batch.state = "received"; batch.rejected = result.files.filter(file => file.status === "rejected");
        batch.files = []; // 服务器已接收的文件不再保留浏览器内存副本。
      } catch (error) { batch.state = "error"; batch.error = error.message; }
      renderUploads(id);
      if (active(id)) await refresh();
    }
    if (batches.some(batch => batch.state === "queued")) runUploads(id);
  }
  function enqueueUploads(id, files, tokens) {
    const batches = []; let group = [], size = 0;
    const flush = () => { if (group.length) batches.push({key:crypto.randomUUID(), tokens, files:group, state:"queued", title:group.length === 1 ? group[0].name : `${group[0].name} 等 ${group.length} 份文件`}); group = []; size = 0; };
    for (const file of files) {
      if (group.length === 16 || size + file.size > 50 * 1024 * 1024) flush();
      group.push(file); size += file.size;
    }
    flush();
    const queue = uploadQueues.get(id) || []; queue.push(...batches); uploadQueues.set(id, queue);
    renderUploads(id); runUploads(id);
  }
  $("#kb-transfers").addEventListener("click", event => {
    const button = event.target.closest("[data-kb-transfer-retry]");
    const batch = button && (uploadQueues.get(current) || []).find(item => item.key === button.dataset.kbTransferRetry);
    if (!batch || batch.state !== "error") return;
    batch.state = "queued"; runUploads(current);
  });
  // 浏览器的离开提示无法自定义，只在资料确实在传输时拦截；失败待重试的批次关掉网页后重新上传即可。
  window.addEventListener("beforeunload", event => {
    if (creation && !creation.error || [...uploadQueues.values()].some(batches => batches.some(batch => ["queued", "uploading"].includes(batch.state)))) { event.preventDefault(); event.returnValue = ""; }
  });
  // 外发授权是本库最重要的设置：标题旁常驻状态，点开设置弹窗即可切换。
  function syncEgress() {
    const allowed = Boolean(bases.find(kb => kb.id === current)?.external_allowed);
    $("#kb-external").checked = allowed;
    const chip = $("#kb-egress-chip");
    chip.dataset.allowed = String(allowed);
    chip.querySelector("span").textContent = allowed ? "已允许外部模型问答" : "外部模型问答未开启";
  }
  function renderFileTree() {
    const d = draft(), query = $("#kb-file-search").value.trim().toLowerCase();
    const root = {dirs:new Map(), files:[]};
    for (const doc of d.documents || []) {
      const path = doc.relative_path || doc.title;
      if (query && !path.toLowerCase().includes(query)) continue;
      const parts = path.split("/"); let node = root;
      for (const part of parts.slice(0, -1)) {
        if (!node.dirs.has(part)) node.dirs.set(part, {dirs:new Map(), files:[]});
        node = node.dirs.get(part);
      }
      node.files.push({doc, name:parts.at(-1)});
    }
    function branch(node, path = "") {
      return [...node.dirs].sort(([a],[b]) => a.localeCompare(b, "zh")).map(([name, child]) => {
        const key = `kb-folder:${current}:${path}/${name}`;
        return `<details class="kb-folder" data-disclosure="${escapeHtml(key)}" ${disclosureOpen(key, true)}><summary>${fileIcon("folder")}<span>${escapeHtml(name)}</span></summary><div>${branch(child, path + "/" + name)}</div></details>`;
      }).join("") + node.files.sort((a,b) => a.name.localeCompare(b.name,"zh")).map(({doc, name}) => `<div class="kb-tree-row" data-kb-doc="${escapeHtml(doc.id)}">
        <button type="button" class="kb-tree-file" data-kb-document="${escapeHtml(doc.id)}" aria-pressed="${d.fileKey === `doc:${doc.id}`}" title="${escapeHtml(doc.relative_path || doc.title)}">${fileIcon(doc.title.split(".").at(-1))}<span><strong>${escapeHtml(name)}</strong><small>修订 ${doc.revision} · ${doc.index_status === "ready" ? "已有有效索引" : "尚无有效索引"}</small></span>${doc.workspace_update ? '<span class="kb-update-dot" title="工作区有更新">•</span>' : ""}</button></div>`).join("");
    }
    const html = branch(root) || `<p class="muted small kb-tree-empty">${query ? "没有匹配的文件" : "当前库暂无文档，请添加资料，系统将自动处理入库。"}</p>`;
    const signature = JSON.stringify([current,d.documents,query,d.fileKey]);
    if (fileTreeSignature !== signature) { $("#kb-documents").innerHTML = html; fileTreeSignature = signature; }
    const jobs = d.jobs || [], count = (d.documents || []).length;
    $("#kb-library-summary").textContent = `${count} 份文档 · ${jobs.filter(job => ["queued","parsing","indexing"].includes(job.status)).length} 份处理中 · ${jobs.filter(job => job.status === "failed").length} 份处理失败`;
  }
  function inspectSelection() {
    const d = draft();
    if (d.fileKey?.startsWith("job:")) {
      const job = d.jobs?.find(job => `job:${job.id}` === d.fileKey);
      const doc = job?.status === "ready" && d.documents?.find(doc => doc.source_key === job.source_key);
      if (doc) d.fileKey = `doc:${doc.id}`;
    }
    const exists = d.documents?.some(doc => `doc:${doc.id}` === d.fileKey) || d.jobs?.some(job => `job:${job.id}` === d.fileKey);
    if (!exists) d.fileKey = d.documents?.length ? `doc:${d.documents[0].id}` : d.jobs?.length ? `job:${d.jobs[0].id}` : null;
    renderFileTree();
    if (d.fileKey) inspectFile(d.fileKey);
    else { inspectionSequence++; inspectionSignature = ""; $("#kb-inspector").innerHTML = '<div class="kb-inspector-empty"><span class="empty-icon">' + uiIcon("report") + '</span><h3>从左侧选择一份文件</h3><p class="muted">添加资料后，可在这里查看章节与文件结构。</p><button class="primary" data-kb-add-empty>添加资料</button></div>'; }
  }
  function renderOutline(rows) {
    if (!rows.length) return '<p class="muted kb-tree-empty">暂未生成文件结构。</p>';
    const root = new Map();
    for (const row of rows) {
      const parts = row.section ? row.section.split(" > ").map(part => part.trim()).filter(Boolean) : ["未识别章节标题"];
      if (!parts.length) parts.push("未识别章节标题");
      let children = root, node;
      for (const title of parts) {
        if (!children.has(title)) children.set(title, {title, children:new Map(), own:0, preview:""});
        node = children.get(title); children = node.children;
      }
      // 摘录取本节自身的第一个片段；片段数只记在最末级章节，父级不再重复累计。
      node.own++;
      if (!node.preview && row.preview) node.preview = row.preview;
    }
    function branch(nodes, depth) {
      return `<ul>${[...nodes.values()].map(node => `<li><div class="kb-outline-row" data-depth="${depth}"><div class="kb-outline-text"><span class="kb-outline-title" title="${escapeHtml(node.title)}">${escapeHtml(node.title)}</span>${node.preview ? `<span class="kb-outline-preview">${escapeHtml(node.preview)}</span>` : ""}</div>${!node.children.size && node.own > 1 ? `<small class="kb-outline-count" title="本节 ${node.own} 个片段">${node.own}</small>` : ""}</div>${node.children.size ? branch(node.children, depth + 1) : ""}</li>`).join("")}</ul>`;
    }
    return branch(root, 0);
  }
  async function inspectFile(key, versionId = null, force = false) {
    const id = current, rev = revision, d = draft(id);
    const doc = d.documents?.find(doc => key === `doc:${doc.id}`);
    const job = d.jobs?.find(job => key === `job:${job.id}`);
    if (!doc && !job) return;
    if (d.fileKey !== key) { d.fileKey = key; d.fileVersion = null; }
    else if (!force) { versionId = d.fileVersion || null; }
    d.fileVersion = versionId;
    const signature = JSON.stringify([id,key,doc?.revision,doc?.workspace_update,job?.status,job?.chunk_count,versionId]);
    if (!force && inspectionSignature === signature) return;
    inspectionSignature = signature;
    const seq = ++inspectionSequence, pane = $("#kb-inspector");
    pane.setAttribute("aria-busy", "true"); pane.innerHTML = '<p class="muted">正在读取文件结构…</p>'; renderFileTree();
    try {
      let version, versions = [], rows = [], total = 0, outlineStatus = "";
      if (doc) {
        const detail = await api(`/knowledge-bases/${id}/documents/${doc.id}/versions`);
        versions = detail.versions;
        version = versions.find(item => item.id === (versionId || detail.active_version_id)) || versions.at(-1);
        if (version) {
          const outline = await api(`/knowledge-bases/${id}/documents/${doc.id}/versions/${version.id}/outline`);
          rows = outline.items; total = outline.total; outlineStatus = outline.status;
        }
      } else if (job.chunk_count) {
        const outline = await api(`/knowledge-bases/${id}/imports/${job.id}/outline`); rows = outline.items; total = outline.total; outlineStatus = outline.status;
      }
      if (seq !== inspectionSequence || !active(id, rev)) return;
      pane.removeAttribute("aria-busy");
      const title = doc?.title || job.filename;
      const config = version?.chunk_config || job?.chunk_config;
      const state = job ? names[job.status] : version?.id === doc.active_version_id ? "当前可检索版本" : "未发布或历史版本";
      pane.innerHTML = `<header class="kb-inspector-head"><div><p class="eyebrow">文件结构</p><h3>${escapeHtml(title)}</h3><p class="muted small">${escapeHtml(doc?.relative_path || job?.relative_path || title)}</p></div><span class="badge">${escapeHtml(state)}</span></header>
        <div class="kb-file-stats"><span><b>${total}</b> 个片段</span><span>片段目标 <b>${version?.chunk_config?.parameters?.tokens || job?.tokens || config?.tokens || "—"}</b></span>${version ? `<label>版本 <select id="kb-inspect-version" aria-label="查看文件版本">${[...versions].reverse().map(item => `<option value="${escapeHtml(item.id)}" ${item.id === version.id ? "selected" : ""}>第 ${item.number} 版${item.id === doc.active_version_id ? "（当前可检索）" : "（未发布或历史）"}</option>`).join("")}</select></label>` : ""}</div>
        <div class="row gap wrap kb-file-actions">${doc ? `<button class="ghost small" data-kb-update="${doc.id}">更新</button>${doc.index_status === "ready" ? `<button class="ghost small" data-kb-copy="${doc.id}">复制到工作区</button>` : ""}<button class="ghost small danger" data-kb-remove="${doc.id}">移出知识库</button>` : ""}</div>
        ${doc?.workspace_update ? `<p class="notice warn small" data-kb-workspace-update="${doc.id}">工作区有更新（${escapeHtml(fmtTime(doc.workspace_update.updated_at))}，《${escapeHtml(doc.workspace_update.title)}》），知识库仍为第 ${doc.workspace_update.version} 版。需要时请用“更新”导入新版本后将自动建立索引；工作区改动不会自动同步。</p>` : ""}
        ${job && job.status !== "ready" ? `<p class="notice small">${escapeHtml(job.error || "系统正在自动解析并建立索引，完成后即可用于知识问答。")}</p>` : ""}
        ${rows.some(row => Object.keys(row.constraints || {}).length) ? '<p class="kb-quality-note">部分标题或识别信息存在不确定性，问答会标明相关限制。</p>' : ""}
        ${["source_changed","source_deleted","source_missing","document_removed"].includes(outlineStatus) ? '<p class="notice warn">原文已变化或不可用，请更新文件后查看结构。</p>' : `<div class="kb-outline">${renderOutline(rows)}</div>`}`;
    } catch (error) {
      if (seq !== inspectionSequence || !active(id, rev)) return;
      pane.removeAttribute("aria-busy"); inspectionSignature = "";
      pane.innerHTML = `<p class="danger">读取结构失败：${escapeHtml(error.message)}</p><button class="ghost" data-kb-inspect-retry>重试</button>`;
    }
  }
  $("#kb-base-cards").addEventListener("click", event => { const button = event.target.closest("[data-kb-base]"); if (button) { $("#kb-select").value = button.dataset.kbBase; choose(button.dataset.kbBase); } });
  $("#kb-file-search").addEventListener("input", renderFileTree);
  $("#kb-inspector").addEventListener("click", event => {
    if (event.target.closest("[data-kb-add-empty]")) $("#kb-open-upload").click();
    if (event.target.closest("[data-kb-inspect-retry]")) inspectFile(draft().fileKey, draft().fileVersion, true);
  });
  $("#kb-inspector").addEventListener("change", event => { if (event.target.id === "kb-inspect-version") inspectFile(draft().fileKey, event.target.value, true); });
  $("#kb-open-upload").addEventListener("click", () => { draft().target = null; restore(); $("#kb-upload-error").textContent = ""; $("#kb-upload-dialog").showModal(); });
  $("#kb-upload-close").addEventListener("click", () => $("#kb-upload-dialog").close());
  function openSettings() { syncEgress(); $("#kb-settings-status").textContent = ""; $("#kb-settings-dialog").showModal(); }
  $("#kb-open-settings").addEventListener("click", openSettings);
  $("#kb-egress-chip").addEventListener("click", openSettings);
  $("#kb-settings-close").addEventListener("click", () => $("#kb-settings-dialog").close());
  $("#kb-pick-files").addEventListener("click", () => $("#kb-files").click());
  $("#kb-pick-folder").addEventListener("click", () => $("#kb-add-folder").click());
  function renderUploadSelection() {
    const d = draft(), files = d.files;
    $("#kb-pick-folder").disabled = Boolean(d.target);
    $("#kb-add-folder").disabled = Boolean(d.target);
    $("#kb-upload-selection").innerHTML = files.length ? `<div class="kb-selection-head"><strong>已选 ${files.length} 份资料</strong><button type="button" class="ghost small" data-upload-clear>清空</button></div><ul class="kb-selected-files">${files.map((file, i) => `<li><span>${escapeHtml(file.webkitRelativePath || file.name)}</span><small class="muted">${(file.size / 1024).toFixed(1)} KB</small><button type="button" class="ghost small" data-upload-remove="${i}" aria-label="移除 ${escapeHtml(file.name)}">×</button></li>`).join("")}</ul>` : '<p class="muted small">尚未选择资料。可选择多个文件，或直接选择整个文件夹。</p>';
    if (d.skippedFiles?.length) $("#kb-upload-selection").insertAdjacentHTML("beforeend", `<details class="kb-skipped"><summary>${d.skippedFiles.length} 份文件格式不支持，不会上传</summary><ul>${d.skippedFiles.map(name => `<li>${escapeHtml(name)}</li>`).join("")}</ul></details>`);
  }
  $("#kb-upload-selection").addEventListener("click", event => {
    const remove = event.target.closest("[data-upload-remove]");
    if (remove) draft().files.splice(Number(remove.dataset.uploadRemove), 1);
    if (event.target.closest("[data-upload-clear]")) { draft().files = []; draft().skippedFiles = []; }
    restore();
  });
  function selectUploadFiles(files) {
    const d = draft();
    d.files = files.filter(file => supportedFile.test(file.name));
    d.skippedFiles = files.filter(file => !supportedFile.test(file.name)).map(file => file.webkitRelativePath || file.name);
    $("#kb-upload-error").textContent = "";
    restore();
  }
  $("#kb-to-chat").addEventListener("click", async () => { const id = current; await activateTab("chat"); await loadChatBases(); $("#chat-kb").value = id; $("#chat-kb").dispatchEvent(new Event("change")); $("#input").focus(); });
  function restore() {
    const d = draft();
    syncEgress();
    $("#kb-upload").disabled = d.uploading;
    $("#kb-update-target").textContent = d.target ? `正在更新《${d.target.title}》，仅选择一份文件；旧有效版本保留到新索引完成。` : "新文件将作为独立来源纳入当前库。";
    $("#kb-cancel-update").classList.toggle("hidden", !d.target);
    const files = new DataTransfer();
    d.files.forEach(file => files.items.add(file));
    $("#kb-files").files = files.files;
    renderUploadSelection();
    message(d.message);
  }
  function renderDocuments(documents) {
    draft().documents = documents;
    renderFileTree();
  }
  function renderJobs(jobs) {
    draft().jobs = jobs;
    $("#kb-import-count").textContent = `（${jobs.length}）`;
    const pending = jobs.some(job => job.status !== "ready");
    $("#kb-import-history").open = pending;
    $("#kb-import-history").classList.toggle("hidden", !jobs.length);
    $("#kb-jobs").innerHTML = jobs.map(job => `<div class="knowledge-row" data-kb-job="${escapeHtml(job.id)}"><button class="kb-job-file" data-kb-select-job="${escapeHtml(job.id)}">${fileIcon(job.filename.split(".").at(-1))}${escapeHtml(job.relative_path || job.filename)}</button>
      <span>${escapeHtml(names[job.status] || job.status)}</span>${job.chunk_count ? `<span class="muted small">${job.chunk_count} 个原文片段</span>` : ""}

      ${job.status === "failed" ? `<button class="ghost small" data-kb-retry="${escapeHtml(job.id)}">重试此文件</button><button class="ghost small" data-kb-reparse="${escapeHtml(job.id)}">重新解析</button>` : ""}
      ${job.error ? `<p class="danger small">${escapeHtml(job.error)}</p>` : ""}</div>`).join("");
  }
  async function refresh() {
    clearTimeout(timer);
    const id = current, rev = revision;
    if (!id || !active(id, rev)) return;
    try {
      const [documents, jobs] = await Promise.all([api(`/knowledge-bases/${id}/documents`), api(`/knowledge-bases/${id}/imports`)]);
      if (!active(id, rev)) return;
      renderDocuments(documents); renderJobs(jobs); inspectSelection();
      if (jobs.some(job => ["queued", "parsing", "indexing"].includes(job.status))) timer = setTimeout(refresh, 1200);
    } catch (error) { if (active(id, rev)) { message(error.message); timer = setTimeout(refresh, 3000); } }
  }
  async function choose(id) {
    revision++; clearTimeout(timer);
    $("#kb-source").close(); $("#kb-settings-dialog").close();
    current = id;
    inspectionSequence++; inspectionSignature = ""; fileTreeSignature = "";
    $("#kb-file-search").value = "";
    $("#kb-inspector").innerHTML = "";
    $("#kb-upload-dialog").close();
    renderLibraryState(); renderUploads();
    $("#kb-content").classList.toggle("hidden", !id);
    $("#kb-documents").replaceChildren(); $("#kb-jobs").replaceChildren();
    if (id) { restore(); await refresh(); }
    else message("");
  }
  window.pauseKnowledge = () => { revision++; clearTimeout(timer); $("#kb-source").close(); $("#kb-settings-dialog").close(); $("#kb-create-dialog").close(); $("#kb-upload-dialog").close(); inspectionSequence++; inspectionSignature = ""; };
  window.loadKnowledge = async () => {
    const rev = ++revision;
    try {
      bases = await api("/knowledge-bases");
      if (revision !== rev) return;
      $("#kb-select").innerHTML = '<option value="">选择知识库</option>' + bases.map(kb => `<option value="${escapeHtml(kb.id)}">${escapeHtml(kb.name)}</option>`).join("");
      const id = bases.some(kb => kb.id === current) ? current : bases[0]?.id || "";
      $("#kb-select").value = id;
      await choose(id);
    } catch (error) { if (revision === rev) message(error.message); }
  };
  $("#kb-select").addEventListener("change", () => choose($("#kb-select").value));
  // 一次性授权：保存到服务端，对本库之后的问答和助理均生效，可随时关闭。
  $("#kb-external").addEventListener("change", async () => {
    const id = current, allowed = $("#kb-external").checked;
    if (!id) return;
    $("#kb-external").disabled = true;
    try {
      await api(`/knowledge-bases/${id}/egress`, {method:"PUT", json:{allowed}});
      const kb = bases.find(item => item.id === id); if (kb) kb.external_allowed = allowed;
      renderBaseCards(); if (current === id) syncEgress();
      const note = allowed ? "已允许本知识库外发，之后提问不再逐次确认；可随时关闭。" : "已关闭本知识库外发，之后只在本地核验依据。";
      message(note, id); if (current === id) $("#kb-settings-status").textContent = note;
      if (typeof loadChatBases === "function") loadChatBases().catch(() => {});
    } catch (error) { message(error.message, id); if (current === id) { $("#kb-settings-status").textContent = error.message; syncEgress(); } }
    finally { $("#kb-external").disabled = false; }
  });
  $("#kb-create-form").addEventListener("submit", async event => {
    event.preventDefault();
    const name = $("#kb-name").value.trim();
    if (!name || !createFiles.length) { $("#kb-create-error").textContent = "请填写名称，并至少选择一份支持的资料。"; return; }
    if (creation && !creation.error) return;
    const files = [...createFiles], tokens = Number($("#kb-create-tokens").value);
    if (!Number.isInteger(tokens) || tokens < 32 || tokens > 512) { $("#kb-create-error").textContent = "切片目标须为 32～512 之间的整数。"; $("#kb-create-tokens").focus(); return; }
    if (files.some(file => file.size > 50 * 1024 * 1024)) { $("#kb-create-error").textContent = "有文件超过单批 50MB 的接收上限，请移除该文件后再提交。"; return; }
    const rev = revision;
    creation = {name, error:""};
    $("#kb-create-dialog").close(); renderLibraryState();
    $("#kb-create").disabled = true;
    try {
      const kb = await api("/knowledge-bases", {method:"POST", json:{name}});
      if (!bases.some(item => item.id === kb.id)) bases.push(kb);
      creation = null;
      if (![...$("#kb-select").options].some(option => option.value === kb.id)) {
        $("#kb-select").insertAdjacentHTML("beforeend", `<option value="${escapeHtml(kb.id)}">${escapeHtml(kb.name)}</option>`);
      }
      createFiles = []; skippedFiles = []; $("#kb-name").value = "";
      $("#kb-create-tokens").value = "512";
      if (rev === revision) { $("#kb-select").value = kb.id; await choose(kb.id); }
      renderLibraryState();
      enqueueUploads(kb.id, files, tokens);
    }
    catch (error) { if (creation) { creation.error = `${error.message}。所选资料仍保留在本页，请先刷新知识库列表核对创建结果，再返回填写。`; renderLibraryState(); } else message(error.message); }
    finally { $("#kb-create").disabled = false; }
  });
  $("#kb-files").addEventListener("change", () => selectUploadFiles([...$("#kb-files").files]));
  $("#kb-add-folder").addEventListener("change", event => {
    if (draft().target) { $("#kb-upload-error").textContent = "更新已有文档时，请选择对应的一份文件。"; event.target.value = ""; return; }
    selectUploadFiles([...event.target.files]);
    event.target.value = "";
  });
  $("#kb-cancel-update").addEventListener("click", () => { draft().target = null; restore(); });
  $("#kb-upload").addEventListener("click", async () => {
    const id = current, d = draft(id), files = [...d.files], target = d.target;
    if (!files.length || target && files.length !== 1) { $("#kb-upload-error").textContent = "请选择文件；更新同一来源时只选择一份。"; return; }
    if (files.some(file => file.size > 50 * 1024 * 1024)) { $("#kb-upload-error").textContent = "有文件超过单批50MB上限，请移除后再提交。"; return; }
    $("#kb-upload-dialog").close();
    if (!target) { d.files = []; d.skippedFiles = []; restore(); enqueueUploads(id, files, Number($("#kb-tokens").value)); return; }
    const form = new FormData();
    files.forEach(file => form.append("files", file));
    form.append("request_key", crypto.randomUUID()); form.append("tokens", $("#kb-tokens").value);
    if (target) { form.append("source_key", target.source_key); form.append("expected_revision", target.revision); }
    d.uploading = true; $("#kb-upload").disabled = true; message("正在接收文件，解析结果将逐份显示。", id);
    try {
      const result = await api(`/knowledge-bases/${id}/imports`, {method:"POST", body:form});
      d.files = []; d.target = null;
      message(result.files.some(job => job.status === "rejected") ? result.files.filter(job => job.status === "rejected").map(job => `${job.filename}：${job.error}`).join("；") : "文件已接收，正在自动处理，请查看逐文件状态。", id);
      if (current === id) { restore(); await refresh(); }
    } catch (error) { message(error.message, id); }
    finally { d.uploading = false; if (current === id) $("#kb-upload").disabled = false; }
  });
  $("#kb-content").addEventListener("click", async event => {
    const file = event.target.closest("[data-kb-document]");
    if (file) { inspectFile(`doc:${file.dataset.kbDocument}`); return; }
    const update = event.target.closest("[data-kb-update]"), remove = event.target.closest("[data-kb-remove]"), copy = event.target.closest("[data-kb-copy]");
    if (update) { draft().target = draft().documents.find(doc => doc.id === update.dataset.kbUpdate); restore(); $("#kb-upload-dialog").showModal(); return; }
    if (copy) {
      const id = current;
      copy.disabled = true;
      try {
        const r = await api(`/knowledge-bases/${id}/documents/${copy.dataset.kbCopy}/copy-to-workspace`, {method:"POST"});
        message(`已把《${r.title}》复制到工作区（材料库），并记为与本文档关联；工作区副本随本库的外发授权。`, id);
        if (typeof refreshLibrary === "function") refreshLibrary();
      } catch (error) { message(error.message, id); }
      finally { copy.disabled = false; }
      return;
    }
    if (!remove) return;
    const id = current, doc = draft().documents.find(item => item.id === remove.dataset.kbRemove);
    if (!await uiConfirm(`从当前知识库移出《${doc.title}》？原材料及待办、周报引用会保留。`)) return;
    try { await api(`/knowledge-bases/${id}/documents/${doc.id}?expected_revision=${doc.revision}`, {method:"DELETE"}); if (current === id) await refresh(); }
    catch (error) { message(error.message, id); }
  });
  $("#kb-jobs").addEventListener("click", async event => {
    const button = event.target.closest("button"); if (!button) return;
    if (button.dataset.kbSelectJob) { inspectFile(`job:${button.dataset.kbSelectJob}`); return; }
    const id = current, rev = revision;
    try {
      const job = button.dataset.kbRetry || button.dataset.kbReparse;
      await api(`/knowledge-bases/${id}/imports/${job}/retry`, {method:"POST", json:{reparse:Boolean(button.dataset.kbReparse)}});
      if (active(id, rev)) await refresh();
    } catch (error) { message(error.message, id); }
  });
  $("#kb-source-close").addEventListener("click", () => $("#kb-source").close());
})();
