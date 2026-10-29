// 显式预览与确认：从最新回答保存待办，或把原始信息带入正式周报。
"use strict";

const actionDrafts = new Map();
let addForAssistant = false;

function useMaterial(id) {
  document.querySelector('.tabs button[data-tab="chat"]').click();
  state.picked.add(id);
  renderPicked();
  $("#input").focus();
}

function resultActions(el, message) {
  if (!message.message_id && !message.system_reply) return;
  document.querySelectorAll(".result-action").forEach((b) => { b.disabled = true; });
  const kinds = new Set(message.result_kinds || []);
  const saved = message.saved_todo_ids?.length || 0;
  const actionable = !message.system_reply && kinds.size > 0;
  const todoMain = actionable && (kinds.has("todo") || kinds.has("minutes"));
  const weekMain = actionable && kinds.has("weekly");
  const todoButton = `<button class="${todoMain ? "primary" : "ghost"} small result-action" data-todos>预览并保存待办</button>`;
  const weekButton = `<button class="${weekMain ? "primary" : "ghost"} small result-action" data-week>带入周报页</button>`;
  const box = document.createElement("div");
  box.className = "row gap wrap msg-actions";
  const secondary = [!todoMain ? todoButton : "", !weekMain ? weekButton : ""].filter(Boolean).join("");
  const status = saved ? `已保存 ${saved} 条待办。` : todoMain ? "行动项尚未保存到待办。" : "";
  box.innerHTML = `<button class="${!todoMain && !weekMain ? "primary" : "ghost"} small" data-copy>复制结果</button>
    ${todoMain ? todoButton : ""}${weekMain ? weekButton : ""}
    ${actionable && secondary ? `<details class="result-more"><summary>更多操作</summary><div class="row gap wrap">${secondary}</div></details>` : ""}
    <span class="muted small" data-save-state>${status}${weekMain ? "对话周报是草稿，正式周报需核对后生成。" : !status ? "当前回复保存在会话中。" : ""}</span>`;
  el.appendChild(box);
  box.querySelector("[data-copy]").onclick = async () => {
    try { await navigator.clipboard.writeText(message.text); box.querySelector("[data-copy]").textContent = "已复制"; }
    catch (_) { await uiAlert("浏览器未允许剪贴板，请选中结果后复制。"); }
  };
  const thread = state.threadId;
  const todo = box.querySelector("[data-todos]");
  if (todo) todo.onclick = () => previewTodos(thread, message.message_id, box);
  if (saved && todo) {
    todo.textContent = "这些候选已保存";
    todo.disabled = true;
  }
  const week = box.querySelector("[data-week]");
  if (week) week.onclick = () => transferToWeek({ thread_id: thread, message_id: message.message_id });
  if (message.warnings?.length || message.system_reply || !actionable) {
    const warning = document.createElement("div");
    warning.className = message.warnings?.length ? "notice warn result-warning" : "result-help";
    warning.innerHTML = `${(message.warnings || []).map((w)=>`<p>${escapeHtml(w)}</p>`).join("")}
      ${message.warnings?.length ? "<p>此回答仍需核对，建议修正后再使用。</p>" : message.system_reply ? "<p>本次未生成可用结果，可调整内容或范围后重试。</p>" : ""}
      <button class="ghost small result-action" data-retry>修改要求后重试</button>
      <button class="ghost small result-action" data-supplement>补充信息</button>`;
    el.appendChild(warning);
    // 只把原请求填回输入区，不自动发起模型调用或覆盖用户正在编辑的要求。
    const original = state.lastRequest ? {...state.lastRequest, material_ids:[...state.lastRequest.material_ids]} : null;
    warning.querySelector("[data-retry]").disabled = !original;
    warning.querySelector("[data-retry]").onclick = async () => {
      if (!original) return;
      if ($("#input").value.trim() && $("#input").value !== original.text && !(await uiConfirm("替换输入框中尚未发送的内容，填回这次原始请求？"))) return;
      $("#input").value = original.text;
      state.picked = new Set(original.material_ids); renderPicked();
      $("#input").focus();
    };
    warning.querySelector("[data-supplement]").onclick = () => $("#input").focus();
  }
}

function actionDialog(title) {
  const dialog = document.createElement("dialog");
  dialog.className = "wide";
  dialog.innerHTML = `<div class="dialog-head"><h3>${escapeHtml(title)}</h3><button class="ghost icon-close" data-close-action aria-label="关闭预览">${uiIcon("close")}</button></div><div data-body></div><div class="notice hidden" data-error></div>`;
  dialog.querySelector("[data-close-action]").onclick = () => dialog.close();
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  return dialog;
}

async function previewTodos(thread, message, resultBox) {
  const key = `${thread}:${message}`;
  const dialog = actionDialog("预览并保存待办");
  const body = dialog.querySelector("[data-body]");
  const error = dialog.querySelector("[data-error]");
  body.innerHTML = '<p>正在根据原始信息核对候选，尚未保存任何待办…</p>';
  try {
    let draft = actionDrafts.get(key);
    if (!draft) {
      draft = await api(`/chat/sessions/${thread}/todo-preview`, {method:"POST", json:{message_id:message}});
      actionDrafts.set(key, draft);
    }
    if (!dialog.open) return;
    if (draft.saved) { body.innerHTML = `<p>这次候选已保存 ${draft.saved.count} 条。可在待办页查看；修改内容需重新提问后预览。</p>`; return; }
    const parents = (await api("/todos")).filter((t) => !t.parent_id && !t.archived_at);
    if (!dialog.open) return;
    body.innerHTML = `<p>这是根据原始信息重新核对的候选，尚未保存。请确认任务含义、负责人和期限后勾选；相对期限保留原话，他人负责的事项不会自动变成你的任务。</p>
      ${draft.warnings.map((w) => `<p class="notice warn">${escapeHtml(w)}</p>`).join("")}
      ${draft.items.length ? "" : "<p>未找到有原文依据的明确行动项。可以补充任务要求后重新提问。</p>"}
      ${draft.items.map((x,i) => `<section class="week-section" data-item="${i}">
        <label class="toggle"><input type="checkbox" data-selected>保存这条${x.possible_duplicate ? "（已有同名待办，请确认是否重复）" : ""}</label>
        <label>任务标题<input data-title class="block" maxlength="200" value="${escapeHtml(x.title)}"></label>
        <p>原文负责人：${escapeHtml(x.owner)} · 原文期限：${escapeHtml(x.deadline)}</p>
        ${(x.review_warnings || []).map((w) => `<p class="notice warn">${escapeHtml(w)}</p>`).join("")}
        <p class="quote">${escapeHtml(x.quote)}</p>
        <label>说明与来源<textarea data-description class="block" rows="3">${escapeHtml(x.description)}</textarea></label>
        <div class="row gap wrap"><label>截止日期（相对日期请确认后填写）<input type="date" data-due value="${escapeHtml(x.due_date || "")}"></label>
        <label>保存到<select data-parent><option value="">独立待办</option>${parents.map((t)=>`<option value="${t.id}">作为“${escapeHtml(t.title)}”的子待办</option>`).join("")}</select></label></div>
      </section>`).join("")}
      ${draft.items.length ? '<button class="primary" data-save>确认保存所选待办</button>' : ""}`;
    const button = body.querySelector("[data-save]");
    if (!button) return;
    button.onclick = async () => {
      const items = [...body.querySelectorAll("[data-item]")].filter((row)=>row.querySelector("[data-selected]").checked).map((row)=>({
        index:Number(row.dataset.item), title:row.querySelector("[data-title]").value,
        description:row.querySelector("[data-description]").value, due_date:row.querySelector("[data-due]").value || null,
        parent_id:Number(row.querySelector("[data-parent]").value) || null,
      }));
      if (!items.length) return showNotice(error, "请先勾选要保存的任务。", "error");
      // 一次提交失败后保留同一份请求；用户修改表单后再提交由后端防重复判断。
      button.disabled = true;
      try {
        const receipt = await api(`/chat/sessions/${thread}/todo-import/${draft.draft_id}`, {method:"POST", json:{items}});
        error.classList.add("hidden");
        draft.saved = receipt;
        if (resultBox.isConnected) {
          resultBox.querySelector("[data-save-state]").textContent = `已保存 ${receipt.count} 条待办，原始来源已保留。`;
          resultBox.querySelector("[data-todos]").disabled = true;
          resultBox.querySelector("[data-todos]").textContent = "这些候选已保存";
        }
        body.innerHTML = `<p>已保存 ${receipt.count} 条待办，原始来源已保留。</p><button class="primary" data-view>查看待办</button>`;
        body.querySelector("[data-view]").onclick = async () => {
          dialog.close();
          todos.filter = "all"; $("#todo-filter").value = "all";
          await activateTab("todos");
          await openTodo(receipt.todo_ids[0]);
        };
      } catch (err) {
        if (err.status === 409) actionDrafts.delete(key);
        showNotice(error, escapeHtml(err.message), "error"); button.disabled = false;
      }
    };
  } catch (err) {
    actionDrafts.delete(key);
    body.innerHTML = "<p>预览未完成，未保存任何待办。关闭后可以重试。</p>";
    showNotice(error, escapeHtml(err.message), "error");
  }
}

async function transferToWeek(origin) {
  const dialog = actionDialog("把原始信息带入正式周报");
  const body = dialog.querySelector("[data-body]");
  const error = dialog.querySelector("[data-error]");
  const now = new Date();
  const day = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
  body.innerHTML = `<p>助理回复仅作草稿参考，不会自动变成事实来源。加入输入后仍需在周报页核对并生成。</p>
    <label>目标周（选择该周任一天）<input type="date" data-day value="${day}"></label>
    <button class="ghost" data-load>预览这一周</button><div data-preview></div>`;
  let preview = null, request = null;
  body.querySelector("[data-load]").onclick = async () => {
    const button = body.querySelector("[data-load]"); button.disabled = true;
    try {
      const selected = {...origin, day:body.querySelector("[data-day]").value};
      // 会话来源先返回可选择的材料，只有用户勾选的才进入最终提交。
      preview = await api("/weekly/input-preview", {method:"POST", json:selected});
      const sourceMaterials = preview.available_materials || preview.materials;
      request = {...selected, revision:preview.revision, target_revision:preview.target_revision, request_id:crypto.randomUUID()};
      const zone = body.querySelector("[data-preview]");
      zone.innerHTML = `<h4>${escapeHtml(preview.period_start)} 至 ${escapeHtml(preview.period_end)}</h4>
        <p>已有说明（仅展示，不覆盖）：${escapeHtml(preview.existing_note || "无")}</p>
        <p class="muted small">勾选的材料会整份参与正式周报，不沿用聊天里的删减范围。只需部分内容时，可取消勾选材料，核对并补充下面的工作说明。</p>
        ${sourceMaterials.map((m)=>`<label class="toggle"><input type="checkbox" data-mid="${m.id}" ${origin.material_ids?.includes(m.id)?"checked":""}>${escapeHtml(m.title)}${preview.existing_material_ids.includes(m.id)?"（本周已加入）":""}</label>`).join("")}
        <details><summary>选择要追加的用户原始输入</summary>${preview.source_inputs.map((s,i)=>`<p>${escapeHtml(s.title)} <button class="ghost small" data-use-input="${i}">追加这段原文</button></p><pre class="content-box">${escapeHtml(s.content)}</pre>`).join("")}</details>
        <label>要追加的工作说明（可编辑；空白则只加入材料）<textarea data-note class="block" rows="5"></textarea></label>
        <button class="primary" data-commit>确认加入输入并前往周报页</button>`;
      zone.querySelectorAll("[data-use-input]").forEach((b) => b.onclick = () => {
        const note = zone.querySelector("[data-note]");
        note.value = [note.value, preview.source_inputs[Number(b.dataset.useInput)].content].filter(Boolean).join("\n\n");
      });
      zone.querySelector("[data-commit]").onclick = async () => {
        const commit = zone.querySelector("[data-commit]"); commit.disabled = true;
        try {
          if (body.querySelector("[data-day]").value !== request.day) throw new Error("目标周已改变，请重新预览。");
          const material_ids = [...zone.querySelectorAll("[data-mid]:checked")].map((x)=>Number(x.dataset.mid));
          // 首次预览已绑定会话内全部候选材料的快照；勾选范围单独送交确认。
          const sent = {...request, material_ids, note:zone.querySelector("[data-note]").value};
          const result = await api("/weekly/input-transfer", {method:"POST", json:sent});
          dialog.close();
          await gotoWeek(result.report_id);
        } catch (err) { showNotice(error, escapeHtml(err.message), "error"); commit.disabled = false; }
      };
    } catch (err) { showNotice(error, escapeHtml(err.message), "error"); }
    finally { button.disabled = false; }
  };
  body.querySelector("[data-load]").click();
}

// 欢迎页卡片只预填一句话，不锁定能力：意图由服务端漏斗判断，用户之后可继续转待办等其他任务。
function chooseQuickAction(e) {
  const button = e.target.closest("[data-start]");
  if (!button || !button.closest(".welcome")) return;
  if (!$("#input").value.trim()) $("#input").value = {summarize:"请总结所选材料。", minutes:"请整理成会议纪要。", todo:"请提取明确的待办事项。", polish:"请优化这段文字，保持事实不变。", weekly:"请根据所选材料起草周报。"}[button.dataset.start];
  $("#input").focus();
}
$("#messages").addEventListener("click", chooseQuickAction);
$("#start-todo").onclick = () => $("#new-todo").click();
$("#start-assistant").onclick = () => activateTab("chat");
$("#chat-add").onclick = () => { $("#open-add").click(); addForAssistant = true; };
$("#add-current").onclick = () => showAddPane("upload");
$("#add-follow").onclick = () => {
  $("#add-dialog").close();
  document.querySelector('.tabs button[data-tab="materials"]').click();
  document.querySelector('#mat-tabs [data-mt="tracked"]').click();
  $("#add-source-box").open = true; $("#source-paths").focus();
};

function importedForAssistant(id) {
  if (addForAssistant) { state.picked.add(id); renderPicked(); }
  const next = $("#import-next"); next.classList.remove("hidden");
  next.innerHTML = `<span class="muted small">材料已保存，下一步：</span><button type="button" class="ghost small" data-import-chat>用于助理</button><button type="button" class="ghost small" data-import-todo>关联到待办</button><button type="button" class="ghost small" data-import-week>加入本周材料</button><button type="button" class="text-button small" data-import-library>在材料库查看</button>`;
  next.querySelector("[data-import-library]").onclick = () => { $("#add-dialog").close(); showAddedInLibrary(); };
  next.querySelector("[data-import-chat]").onclick = () => { $("#add-dialog").close(); useMaterial(id); };
  next.querySelector("[data-import-week]").onclick = () => { $("#add-dialog").close(); transferToWeek({material_ids:[id]}); };
  next.querySelector("[data-import-todo]").onclick = async () => {
    $("#add-dialog").close(); activateTab("todos");
    await loadTodos();
    const options = todos.list.filter((t) => !t.archived_at).map((t) => ({value:String(t.id), label:t.title}));
    if (!options.length) return uiAlert("请先创建待办，再在任务详情中从材料库选择这份材料。");
    const choice = await uiPrompt({title:"关联到待办", fields:[{name:"todo", label:"选择待办", type:"select", options}], ok:"关联"});
    if (!choice) return;
    try { await api(`/todos/${choice.todo}/materials`, {method:"POST", json:{material_id:id}}); await loadTodos(); await openTodo(Number(choice.todo)); $("#todo-material-section").open = true; }
    catch (err) { uiAlert(err.message,"关联失败"); }
  };
}
