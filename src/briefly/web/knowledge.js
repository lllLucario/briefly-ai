/* 知识库：每库草稿与请求归属独立；所有业务写入仍以服务端回执为准。 */
"use strict";
(() => {
  const drafts = new Map();
  let current = "", revision = 0, timer = null, preview = null;
  const names = {queued:"等待解析", parsing:"解析中", preview:"等待核对切片", indexing:"建立索引中", ready:"已完成", failed:"处理失败", rejected:"未接收"};
  const statuses = {answered:"草稿回答", partial_answer:"部分可答", insufficient_evidence:"依据不足", needs_review:"需要复核", external_authorization_required:"本库未允许外发", located:"文档定位", blocked:"已拦截", incomplete_scope:"部分资料未就绪", not_ready:"资料未就绪", empty_scope:"范围为空", empty_library:"库内暂无可用资料", workflow_required:"请使用完整材料流程", clarification_required:"请补充问题", no_candidates:"没有召回依据"};
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
  function syncEgress() { $("#kb-external").checked = Boolean(bases.find(kb => kb.id === current)?.external_allowed); }
  function selectedScope(d) { return d.all ? null : [...d.selected]; }
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
    $("#kb-documents").innerHTML = documents.length ? documents.map(doc => `<div class="knowledge-row" data-kb-doc="${escapeHtml(doc.id)}">
      <label><input type="checkbox" data-kb-scope="${escapeHtml(doc.id)}" ${d.all || d.selected.has(doc.id) ? "checked" : ""} ${d.all ? "disabled" : ""}> ${escapeHtml(doc.title)}</label>
      <span class="muted small">修订 ${doc.revision} · ${doc.index_status === "ready" ? "已有有效索引" : "尚无有效索引"}</span>
      <button class="ghost small" data-kb-update="${escapeHtml(doc.id)}">更新</button><button class="ghost small" data-kb-remove="${escapeHtml(doc.id)}">移出知识库</button></div>`).join("") : '<p class="muted">当前库暂无文档，请先上传并核对切片。</p>';
  }
  function renderJobs(jobs) {
    draft().jobs = jobs;
    $("#kb-jobs").innerHTML = jobs.map(job => `<div class="knowledge-row" data-kb-job="${escapeHtml(job.id)}"><strong>${escapeHtml(job.filename)}</strong>
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
      renderDocuments(documents); renderJobs(jobs);
      if (jobs.some(job => ["queued", "parsing", "indexing"].includes(job.status))) timer = setTimeout(refresh, 1200);
    } catch (error) { if (active(id, rev)) { message(error.message); timer = setTimeout(refresh, 3000); } }
  }
  async function choose(id) {
    revision++; clearTimeout(timer);
    $("#kb-preview").close(); $("#kb-source").close(); preview = null;
    current = id;
    $("#kb-content").classList.toggle("hidden", !id);
    $("#kb-documents").replaceChildren(); $("#kb-jobs").replaceChildren();
    if (id) { restore(); await refresh(); }
    else message("先创建或选择一个知识库。");
  }
  window.pauseKnowledge = () => { revision++; clearTimeout(timer); $("#kb-preview").close(); $("#kb-source").close(); };
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
      message(allowed ? "已允许本知识库外发，之后提问不再逐次确认；可随时关闭。" : "已关闭本知识库外发，之后只在本地核验依据。", id);
      if (typeof loadChatBases === "function") loadChatBases().catch(() => {});
    } catch (error) { message(error.message, id); syncEgress(); }
    finally { $("#kb-external").disabled = false; }
  });
  $("#kb-create").addEventListener("click", async () => {
    const name = $("#kb-name").value.trim();
    if (!name) return message("请输入知识库名称。");
    const rev = revision;
    $("#kb-create").disabled = true;
    try { const kb = await api("/knowledge-bases", {method:"POST", json:{name}}); if (rev === revision) { current = kb.id; $("#kb-name").value = ""; await window.loadKnowledge(); } }
    catch (error) { message(error.message); }
    finally { $("#kb-create").disabled = false; }
  });
  $("#kb-question").addEventListener("input", () => { draft().question = $("#kb-question").value; invalidate(); });
  $("#kb-private").addEventListener("input", () => { draft().privateText = $("#kb-private").value; invalidate(); });
  $("#kb-files").addEventListener("change", () => { draft().files = [...$("#kb-files").files]; });
  $("#kb-all").addEventListener("change", () => { const d = draft(); d.all = $("#kb-all").checked; if (!d.all) d.selected = new Set((d.documents || []).map(doc => doc.id)); invalidate(); renderDocuments(d.documents || []); });
  $("#kb-cancel-update").addEventListener("click", () => { draft().target = null; restore(); });
  $("#kb-upload").addEventListener("click", async () => {
    const id = current, d = draft(id), files = [...d.files], target = d.target;
    if (!files.length || files.length > 16 || target && files.length !== 1) return message("请选择1～16份文件；更新同一来源时只选择一份。");
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
  $("#kb-documents").addEventListener("click", async event => {
    const update = event.target.closest("[data-kb-update]"), remove = event.target.closest("[data-kb-remove]");
    if (update) { draft().target = draft().documents.find(doc => doc.id === update.dataset.kbUpdate); restore(); return; }
    if (!remove) return;
    const id = current, doc = draft().documents.find(item => item.id === remove.dataset.kbRemove);
    if (!await uiConfirm(`从当前知识库移出《${doc.title}》？原材料及待办、周报引用会保留。`)) return;
    try { await api(`/knowledge-bases/${id}/documents/${doc.id}?expected_revision=${doc.revision}`, {method:"DELETE"}); if (current === id) await refresh(); }
    catch (error) { message(error.message, id); }
  });
  $("#kb-jobs").addEventListener("click", async event => {
    const button = event.target.closest("button"); if (!button) return;
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
      ${result.model_called && result.egress ? `<p class="muted small">本次已将问题与 ${result.egress.evidence_sent} 个片段经规则处理后发送给外部生成服务。</p>` : ""}
      ${(result.evidence || []).length ? '<h4>核对原文与版本</h4>' : ""}
      ${(result.evidence || []).map((item, i) => `<button class="ghost small" data-kb-reference="${escapeHtml(item.reference)}">依据${i+1} · ${escapeHtml(item.source.title)}</button>`).join("")}`;
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
