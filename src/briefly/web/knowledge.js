/* 知识库：每库草稿与请求归属独立；所有业务写入仍以服务端回执为准。 */
"use strict";
(() => {
  const drafts = new Map();
  let current = "", revision = 0, timer = null, preview = null;
  const names = {queued:"等待解析", parsing:"解析中", preview:"等待核对切片", indexing:"建立索引中", ready:"已完成", failed:"处理失败", rejected:"未接收"};
  const statuses = {answered:"草稿回答", partial_answer:"部分可答", insufficient_evidence:"依据不足", needs_review:"需要复核", external_authorization_required:"本库未允许外发", located:"文档定位", blocked:"已拦截", incomplete_scope:"部分资料未就绪", not_ready:"资料未就绪", empty_scope:"范围为空", empty_library:"库内暂无可用资料", workflow_required:"请使用完整材料流程", clarification_required:"请补充问题", no_candidates:"没有召回依据", low_relevance:"没有直接相关的内容"};
  function draft(id = current) {
    if (!drafts.has(id)) drafts.set(id, {question:"", privateText:"", all:true, selected:new Set(), files:[], seq:0, result:null, message:"", uploading:false, asking:false, target:null});
    return drafts.get(id);
  }
  function active(id, rev = revision) { return current === id && revision === rev && $("#tab-knowledge").classList.contains("active"); }
  function message(text, id = current) {
    if (id) draft(id).message = text;
    if (!id || current === id) $("#kb-message").textContent = text;
  }
  function invalidate() { draft().seq++; }
  let bases = [];
  let createFiles = [], skippedFiles = [], creation = null;
  const supportedFile = /\.(txt|md|doc|docx|pdf|xlsx|xls|csv|pptx|ppt|png|jpg|jpeg|bmp|tif|tiff|webp)$/i;
  const uploadQueues = new Map();
  let inspectionSequence = 0, inspectionSignature = "", folderSupport = null, fileTreeSignature = "";
  function renderBaseCards() {
    $("#kb-base-cards").innerHTML = bases.map(base => `<button type="button" class="kb-base-card" data-kb-base="${escapeHtml(base.id)}" aria-pressed="${base.id === current}"><span class="start-icon">${uiIcon("folder")}</span><span><strong>${escapeHtml(base.name)}</strong><small>${base.external_allowed ? "已允许外部模型问答" : "外发未开启"}</small></span><span class="kb-card-check" aria-hidden="true">${base.id === current ? "✓" : "→"}</span></button>`).join("");
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
    if (batch.files.some(file => file.webkitRelativePath)) {
      folderSupport ||= api("/openapi.json").then(schema => Boolean(schema.paths["/knowledge-bases/{kb_id}/documents/{doc_id}/versions/{version_id}/chunks"]));
      if (!await folderSupport) throw new Error("当前服务尚未更新文件夹目录功能，文件尚未发送。请更新服务后重试此批。");
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
    $("#kb-transfers").innerHTML = (pending ? '<p class="muted small">正在上传资料，请保持此网页打开；可以切换页面继续其他工作。解析完成后仍需逐份核对，再确认入库。</p>' : '') + batches.filter(batch => batch.state !== "received" || batch.rejected?.length).map(batch => `<article class="kb-transfer" data-kb-transfer="${batch.key}"><strong>${escapeHtml(batch.title)}</strong><span>${batch.state === "queued" ? "等待上传" : batch.state === "uploading" ? batch.percent === 100 ? "文件已传输，等待服务接收结果" : `正在上传${batch.percent == null ? "" : ` · ${batch.percent}%`}` : batch.state === "error" ? "上传未完成" : "部分文件未接收"}</span>
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
  window.addEventListener("beforeunload", event => {
    if (creation && !creation.error || [...uploadQueues.values()].some(batches => batches.some(batch => ["queued", "uploading", "error"].includes(batch.state)))) { event.preventDefault(); event.returnValue = ""; }
  });
  function syncEgress() { $("#kb-external").checked = Boolean(bases.find(kb => kb.id === current)?.external_allowed); }
  function selectedScope(d) { return d.all ? null : [...d.selected]; }
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
        ${!d.all ? `<input type="checkbox" aria-label="单次问答纳入 ${escapeHtml(doc.title)}" data-kb-scope="${escapeHtml(doc.id)}" ${d.selected.has(doc.id) ? "checked" : ""}>` : `<input class="hidden" type="checkbox" data-kb-scope="${escapeHtml(doc.id)}" checked disabled>`}
        <button type="button" class="kb-tree-file" data-kb-document="${escapeHtml(doc.id)}" aria-pressed="${d.fileKey === `doc:${doc.id}`}" title="${escapeHtml(doc.relative_path || doc.title)}">${fileIcon(doc.title.split(".").at(-1))}<span><strong>${escapeHtml(name)}</strong><small>修订 ${doc.revision} · ${doc.index_status === "ready" ? "已有有效索引" : "尚无有效索引"}</small></span>${doc.workspace_update ? '<span class="kb-update-dot" title="工作区有更新">•</span>' : ""}</button></div>`).join("");
    }
    const html = branch(root) || `<p class="muted small kb-tree-empty">${query ? "没有匹配的文件" : "当前库暂无文档，请先上传并核对切片。"}</p>`;
    const signature = JSON.stringify([current,d.documents,query,d.all,[...d.selected],d.fileKey]);
    if (fileTreeSignature !== signature) { $("#kb-documents").innerHTML = html; fileTreeSignature = signature; }
    const jobs = d.jobs || [], count = (d.documents || []).length;
    $("#kb-library-summary").textContent = `${count} 份文档 · ${jobs.filter(job => ["queued","parsing","indexing"].includes(job.status)).length} 份处理中 · ${jobs.filter(job => job.status === "preview").length} 份待核对`;
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
    else { inspectionSequence++; inspectionSignature = ""; $("#kb-inspector").innerHTML = '<div class="kb-inspector-empty"><span class="empty-icon">' + uiIcon("report") + '</span><h3>从左侧选择一份文件</h3><p class="muted">添加资料后，可在这里查看标题与原文片段。</p><button class="primary" data-kb-add-empty>添加资料</button></div>'; }
  }
  async function inspectFile(key, offset = 0, versionId = null, force = false) {
    const id = current, rev = revision, d = draft(id);
    const doc = d.documents?.find(doc => key === `doc:${doc.id}`);
    const job = d.jobs?.find(job => key === `job:${job.id}`);
    if (!doc && !job) return;
    if (d.fileKey !== key) { d.fileKey = key; d.fileOffset = 0; d.fileVersion = null; }
    else if (!force) { offset = d.fileOffset || 0; versionId = d.fileVersion || null; }
    d.fileOffset = offset; d.fileVersion = versionId;
    const signature = JSON.stringify([id,key,doc?.revision,doc?.workspace_update,job?.status,job?.preview_digest,offset,versionId]);
    if (!force && inspectionSignature === signature) return;
    inspectionSignature = signature;
    const seq = ++inspectionSequence, pane = $("#kb-inspector");
    pane.setAttribute("aria-busy", "true"); pane.innerHTML = '<p class="muted">正在读取文件片段…</p>'; renderFileTree();
    try {
      let version, versions = [], rows = [], total = 0;
      if (doc) {
        const detail = await api(`/knowledge-bases/${id}/documents/${doc.id}/versions`);
        versions = detail.versions;
        version = versions.find(item => item.id === (versionId || detail.active_version_id)) || versions.at(-1);
        if (version) {
          const prefix = `/knowledge-bases/${id}/documents/${doc.id}/versions/${version.id}/chunks`;
          let page;
          try { page = await api(`${prefix}?offset=${offset}&limit=10`); }
          catch (error) {
            if (error.status !== 404) throw error;
            // 已运行的旧服务尚未重启时，沿用原逐片接口完成预览。
            const count = Math.max(0, Math.min(10, version.chunk_count - offset));
            page = {total:version.chunk_count, items:await Promise.all(Array.from({length:count}, (_, i) => api(`${prefix}/${offset+i+1}`)))};
          }
          rows = page.items; total = page.total;
        }
      } else if (job.preview_digest) {
        rows = await api(`/knowledge-bases/${id}/imports/${job.id}/chunks?offset=${offset}&limit=10`); total = job.chunk_count || 0;
      }
      if (seq !== inspectionSequence || !active(id, rev)) return;
      pane.removeAttribute("aria-busy");
      const title = doc?.title || job.filename;
      const config = version?.chunk_config || job?.chunk_config;
      const state = job ? names[job.status] : version?.id === doc.active_version_id ? "当前可检索版本" : "未发布或历史版本";
      pane.innerHTML = `<header class="kb-inspector-head"><div><p class="eyebrow">原文片段预览</p><h3>${escapeHtml(title)}</h3><p class="muted small">${escapeHtml(doc?.relative_path || job?.relative_path || title)}</p></div><span class="badge">${escapeHtml(state)}</span></header>
        <div class="kb-file-stats"><span><b>${total}</b> 个片段</span><span>片段目标 <b>${version?.chunk_config?.parameters?.tokens || job?.tokens || config?.tokens || "—"}</b></span>${version ? `<label>版本 <select id="kb-inspect-version" aria-label="查看文件版本">${[...versions].reverse().map(item => `<option value="${escapeHtml(item.id)}" ${item.id === version.id ? "selected" : ""}>第 ${item.number} 版${item.id === doc.active_version_id ? "（当前可检索）" : "（未发布或历史）"}</option>`).join("")}</select></label>` : ""}</div>
        <div class="row gap wrap kb-file-actions">${doc ? `<button class="ghost small" data-kb-update="${doc.id}">更新</button>${doc.index_status === "ready" ? `<button class="ghost small" data-kb-copy="${doc.id}">复制到工作区</button>` : ""}<button class="ghost small danger" data-kb-remove="${doc.id}">移出知识库</button>` : job.preview_digest ? `<button class="primary small" data-inspect-review="${job.id}">核对并确认入库</button>` : ""}</div>
        ${doc?.workspace_update ? `<p class="notice warn small" data-kb-workspace-update="${doc.id}">工作区有更新（${escapeHtml(fmtTime(doc.workspace_update.updated_at))}，《${escapeHtml(doc.workspace_update.title)}》），知识库仍为第 ${doc.workspace_update.version} 版。需要时请用“更新”导入新版本并核对切片，不会自动发布。</p>` : ""}
        ${job && job.status !== "ready" ? `<p class="notice small">${job.status === "preview" ? "原文片段已生成，请核对后确认入库；当前还不能用于知识问答。" : escapeHtml(job.error || "文件正在处理，完成后会显示原文片段。")}</p>` : ""}
        <div class="kb-chunk-list">${rows.map((row, i) => `<article class="kb-chunk-card"><div class="kb-chunk-heading"><span>片段 ${row.chunk ?? row.index ?? offset + i + 1}</span><small class="muted">原文位置 ${row.start}–${row.end}</small></div><h4>${escapeHtml(row.section || "未识别章节标题")}</h4>${Object.keys(row.constraints || {}).length ? '<p class="kb-quality-note">此片段存在需复核的结构或识别信息，使用时请对照原件。</p>' : ""}<pre>${escapeHtml(row.content ?? "原文已变化或不可用，请核对原件；这里不会用新内容替代旧片段。")}</pre></article>`).join("") || '<p class="muted kb-tree-empty">当前暂无可显示的片段。</p>'}</div>
        ${total ? `<div class="kb-inspect-pager"><button class="ghost small" data-inspect-page="${Math.max(0,offset-10)}" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${offset+1}–${Math.min(offset+10,total)} / ${total} 个片段</span><button class="ghost small" data-inspect-page="${offset+10}" ${offset+10 >= total ? "disabled" : ""}>下一页</button></div>` : ""}`;
    } catch (error) {
      if (seq !== inspectionSequence || !active(id, rev)) return;
      pane.removeAttribute("aria-busy"); inspectionSignature = "";
      pane.innerHTML = `<p class="danger">读取片段失败：${escapeHtml(error.message)}</p><button class="ghost" data-kb-inspect-retry>重试</button>`;
    }
  }
  $("#kb-base-cards").addEventListener("click", event => { const button = event.target.closest("[data-kb-base]"); if (button) { $("#kb-select").value = button.dataset.kbBase; choose(button.dataset.kbBase); } });
  $("#kb-file-search").addEventListener("input", renderFileTree);
  $("#kb-inspector").addEventListener("click", event => {
    if (event.target.closest("[data-kb-add-empty]")) $("#kb-open-upload").click();
    const review = event.target.closest("[data-inspect-review]");
    if (review) document.querySelector(`[data-kb-preview="${review.dataset.inspectReview}"]`)?.click();
    const page = event.target.closest("[data-inspect-page]");
    if (page) inspectFile(draft().fileKey, Number(page.dataset.inspectPage), draft().fileVersion, true);
    if (event.target.closest("[data-kb-inspect-retry]")) inspectFile(draft().fileKey, draft().fileOffset, draft().fileVersion, true);
  });
  $("#kb-inspector").addEventListener("change", event => { if (event.target.id === "kb-inspect-version") inspectFile(draft().fileKey, 0, event.target.value, true); });
  $("#kb-open-upload").addEventListener("click", () => { draft().target = null; restore(); $("#kb-upload-error").textContent = ""; $("#kb-upload-dialog").showModal(); });
  $("#kb-upload-close").addEventListener("click", () => $("#kb-upload-dialog").close());
  $("#kb-to-chat").addEventListener("click", async () => { const id = current; await activateTab("chat"); await loadChatBases(); $("#chat-kb").value = id; $("#chat-kb").dispatchEvent(new Event("change")); $("#input").focus(); });
  function restore() {
    const d = draft();
    $("#kb-question").value = d.question;
    $("#kb-private").value = d.privateText;
    $("#kb-all").checked = d.all;
    syncEgress();
    $("#kb-partial").checked = false;
    $("#kb-upload").disabled = d.uploading;
    $("#kb-ask").disabled = d.asking;
    $("#kb-update-target").textContent = d.target ? `正在更新《${d.target.title}》，仅选择一份文件；旧有效版本保留到新索引完成。` : "新文件将作为独立来源纳入当前库。";
    $("#kb-cancel-update").classList.toggle("hidden", !d.target);
    const files = new DataTransfer();
    d.files.forEach(file => files.items.add(file));
    $("#kb-files").files = files.files;
    message(d.message);
    renderResult(d.result);
  }
  function renderDocuments(documents) {
    const d = draft();
    const valid = new Set(documents.map(doc => doc.id));
    d.selected = new Set([...d.selected].filter(id => valid.has(id)));
    if (d.result?.evidence?.some(item => !documents.some(doc => doc.id === item.document_id && doc.active_version_id === item.version_id))) {
      d.result.stale = true; renderResult(d.result);
    }
    d.documents = documents;
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
      ${job.preview_digest ? `<button class="ghost small" data-kb-preview="${escapeHtml(job.id)}">查看切片</button>` : ""}
      ${job.status === "failed" ? `<button class="ghost small" data-kb-retry="${escapeHtml(job.id)}">重试此文件</button><button class="ghost small" data-kb-reparse="${escapeHtml(job.id)}">重新解析预览</button>` : ""}
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
    $("#kb-preview").close(); $("#kb-source").close(); preview = null;
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
  window.pauseKnowledge = () => { revision++; clearTimeout(timer); $("#kb-preview").close(); $("#kb-source").close(); $("#kb-create-dialog").close(); $("#kb-upload-dialog").close(); inspectionSequence++; inspectionSignature = ""; };
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
      renderBaseCards();
      message(allowed ? "已允许本知识库外发，之后提问不再逐次确认；可随时关闭。" : "已关闭本知识库外发，之后只在本地核验依据。", id);
      if (typeof loadChatBases === "function") loadChatBases().catch(() => {});
    } catch (error) { message(error.message, id); syncEgress(); }
    finally { $("#kb-external").disabled = false; }
  });
  $("#kb-create-form").addEventListener("submit", async event => {
    event.preventDefault();
    const name = $("#kb-name").value.trim();
    if (!name || !createFiles.length) { $("#kb-create-error").textContent = "请填写名称，并至少选择一份支持的资料。"; return; }
    if (creation && !creation.error) return;
    const files = [...createFiles], tokens = Number($("#kb-create-tokens").value);
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
  $("#kb-question").addEventListener("input", () => { draft().question = $("#kb-question").value; invalidate(); });
  $("#kb-private").addEventListener("input", () => { draft().privateText = $("#kb-private").value; invalidate(); });
  $("#kb-files").addEventListener("change", () => { draft().files = [...$("#kb-files").files]; });
  $("#kb-add-folder").addEventListener("change", event => {
    if (draft().target) { $("#kb-upload-error").textContent = "更新已有文档时，请选择对应的一份文件。"; event.target.value = ""; return; }
    const files = [...event.target.files]; draft().files = files.filter(file => supportedFile.test(file.name));
    restore(); $("#kb-upload-error").textContent = files.length !== draft().files.length ? "已排除不支持格式的文件，请核对文件清单后上传。" : "";
    event.target.value = "";
  });
  $("#kb-all").addEventListener("change", () => { const d = draft(); d.all = $("#kb-all").checked; if (!d.all) d.selected = new Set((d.documents || []).map(doc => doc.id)); invalidate(); renderDocuments(d.documents || []); });
  $("#kb-cancel-update").addEventListener("click", () => { draft().target = null; restore(); });
  $("#kb-upload").addEventListener("click", async () => {
    const id = current, d = draft(id), files = [...d.files], target = d.target;
    if (!files.length || target && files.length !== 1) { $("#kb-upload-error").textContent = "请选择文件；更新同一来源时只选择一份。"; return; }
    if (files.some(file => file.size > 50 * 1024 * 1024)) { $("#kb-upload-error").textContent = "有文件超过单批50MB上限，请移除后再提交。"; return; }
    $("#kb-upload-dialog").close();
    if (!target) { d.files = []; restore(); enqueueUploads(id, files, Number($("#kb-tokens").value)); return; }
    const form = new FormData();
    files.forEach(file => form.append("files", file));
    form.append("request_key", crypto.randomUUID()); form.append("tokens", $("#kb-tokens").value);
    if (target) { form.append("source_key", target.source_key); form.append("expected_revision", target.revision); }
    d.uploading = true; $("#kb-upload").disabled = true; message("正在接收文件，解析结果将逐份显示。", id);
    try {
      const result = await api(`/knowledge-bases/${id}/imports`, {method:"POST", body:form});
      d.files = []; d.target = null;
      message(result.files.some(job => job.status === "rejected") ? result.files.filter(job => job.status === "rejected").map(job => `${job.filename}：${job.error}`).join("；") : "文件已接收，请核对逐文件状态与切片。", id);
      if (current === id) { restore(); await refresh(); }
    } catch (error) { message(error.message, id); }
    finally { d.uploading = false; if (current === id) $("#kb-upload").disabled = false; }
  });
  $("#kb-documents").addEventListener("change", event => { const id = event.target.dataset.kbScope; if (id) { event.target.checked ? draft().selected.add(id) : draft().selected.delete(id); invalidate(); } });
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
      if (button.dataset.kbPreview) {
        const job = draft().jobs.find(item => item.id === button.dataset.kbPreview);
        preview = {kb:id, rev, job, offset:0, seq:0, loaded:false}; $("#kb-preview-checked").checked = false; $("#kb-confirm").disabled = true;
        $("#kb-preview-error").textContent = ""; $("#kb-preview").showModal(); await previewPage();
      } else {
        const job = button.dataset.kbRetry || button.dataset.kbReparse;
        await api(`/knowledge-bases/${id}/imports/${job}/retry`, {method:"POST", json:{reparse:Boolean(button.dataset.kbReparse)}});
        if (active(id, rev)) await refresh();
      }
    } catch (error) { message(error.message, id); if (preview?.kb === id && $("#kb-preview").open) $("#kb-preview-error").textContent = error.message; }
  });
  async function previewPage() {
    const p = preview; if (!p) return;
    const seq = ++p.seq, offset = p.offset;
    p.loaded = false; $("#kb-preview-checked").checked = false; $("#kb-preview-checked").disabled = true; $("#kb-confirm").disabled = true;
    $("#kb-preview-body").textContent = "正在读取原文切片…";
    const rows = await api(`/knowledge-bases/${p.kb}/imports/${p.job.id}/chunks?offset=${offset}&limit=10`);
    if (preview !== p || p.seq !== seq || !active(p.kb, p.rev)) return;
    p.loaded = true; $("#kb-preview-checked").disabled = false;
    const overlap = p.job.chunk_config?.parameters?.overlap ?? 0;
    $("#kb-preview-info").textContent = `${p.job.filename} · 目标 ${p.job.tokens} token，正文重叠约 ${overlap} token；表格/图形不重叠 · 第${offset+1}～${offset+rows.length}段，共${p.job.chunk_count}段。仅当前页展示原文，请按需翻页核对。`;
    $("#kb-preview-body").innerHTML = rows.map(row => `<article><h4>${escapeHtml(row.section || "章节未确定")}</h4><pre>${escapeHtml(row.content)}</pre>${Object.keys(row.constraints).length ? '<p class="danger small">该片段有未确认的结构关系，问答时将保留复核限制。</p>' : ""}</article>`).join("");
    $("#kb-preview-prev").disabled = p.offset === 0; $("#kb-preview-next").disabled = p.offset+10 >= p.job.chunk_count;
    $("#kb-confirm").disabled = p.job.status !== "preview" || !$("#kb-preview-checked").checked;
  }
  $("#kb-preview-prev").addEventListener("click", () => { preview.offset = Math.max(0, preview.offset-10); previewPage().catch(error => $("#kb-preview-error").textContent = error.message); });
  $("#kb-preview-next").addEventListener("click", () => { preview.offset += 10; previewPage().catch(error => $("#kb-preview-error").textContent = error.message); });
  $("#kb-preview-close").addEventListener("click", () => $("#kb-preview").close());
  $("#kb-preview-checked").addEventListener("change", () => { $("#kb-confirm").disabled = !preview?.loaded || preview.job.status !== "preview" || !$("#kb-preview-checked").checked; });
  $("#kb-confirm").addEventListener("click", async () => {
    const p = preview; if (!p) return; $("#kb-confirm").disabled = true;
    try { await api(`/knowledge-bases/${p.kb}/imports/${p.job.id}/confirm`, {method:"POST", json:{digest:p.job.preview_digest}}); $("#kb-preview").close(); if (active(p.kb, p.rev)) await refresh(); }
    catch (error) { $("#kb-preview-error").textContent = error.message; $("#kb-confirm").disabled = !$("#kb-preview-checked").checked; }
  });
  function renderResult(result) {
    if (!result) { $("#kb-result").replaceChildren(); return; }
    $("#kb-result").innerHTML = `<h4>${escapeHtml(statuses[result.status] || result.status)}</h4><div class="markdown">${renderMaterialMarkdown(result.answer || "")}</div>
      ${result.stale ? '<p class="danger small">引用所对应的文档已更新或移出，请重新检索；旧引用仍可核对原版本。</p>' : ""}
      ${(result.warnings || []).map(note => `<p class="danger small">${escapeHtml(note)}</p>`).join("")}
      ${result.model_called && result.egress ? `<p class="muted small">本次已将问题与 ${egressParts(result.egress)}经规则处理后发送给外部生成服务。</p>` : ""}
      ${(result.evidence || []).length ? '<h4>核对原文与版本</h4>' : ""}
      ${(result.evidence || []).map((item, i) => item.origin === "workspace"
        ? `<button class="ghost small" data-ws-material="${item.material_id}">依据${i+1} · 工作区 · ${escapeHtml(item.source.title)}</button>`
        : `<button class="ghost small" data-kb-reference="${escapeHtml(item.reference)}">依据${i+1} · ${item.origin ? "知识库 · " : ""}${escapeHtml(item.source.title)}</button>`).join("")}`;
  }
  $("#kb-ask").addEventListener("click", async () => {
    const id = current, d = draft(id), seq = ++d.seq;
    if (!d.question.trim()) return message("请输入问题。", id);
    const json = {question:d.question, document_ids:selectedScope(d),
      allow_partial:$("#kb-partial").checked, private_values:d.privateText.split("\n").map(line => line.trim()).filter(Boolean)};
    d.asking = true; $("#kb-ask").disabled = true; message("正在核验当前范围的依据。", id);
    try { const result = await api(`/knowledge-bases/${id}/answer`, {method:"POST", json}); if (seq === d.seq) { d.result = result; if (current === id) renderResult(result); } }
    catch (error) { message(error.message, id); }
    finally { d.asking = false; if (current === id) { $("#kb-ask").disabled = false; message(d.message === "正在核验当前范围的依据。" ? "核验已完成，请核对回答与原文。" : d.message, id); } }
  });
  $("#kb-result").addEventListener("click", async event => {
    const material = event.target.closest("[data-ws-material]");
    if (material) { showMaterialDetail(Number(material.dataset.wsMaterial)); return; }  // 工作区依据：打开材料
    const button = event.target.closest("[data-kb-reference]"); if (!button) return;
    const id = current, rev = revision;
    try {
      const item = await api(`/knowledge-bases/${id}/references/${encodeURIComponent(button.dataset.kbReference)}`);
      if (!active(id, rev)) return;
      $("#kb-source-title").textContent = item.source.title;
      $("#kb-source-info").textContent = `技术版本 ${item.number} · 业务版本 ${item.business_version || "未提供"} · ${item.section || "章节未确定"} · ${item.status === "current" ? "当前有效版本" : item.status === "historical" ? "历史版本" : "来源已变化或不可用"}`;
      $("#kb-source-text").textContent = item.content ?? "来源已删除或变化，旧引用不会指向新内容。";
      $("#kb-source").showModal();
    } catch (error) { message(error.message, id); }
  });
  $("#kb-source-close").addEventListener("click", () => $("#kb-source").close());
})();
