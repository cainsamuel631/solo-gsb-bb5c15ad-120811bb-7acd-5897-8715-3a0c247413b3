/* 法规条款变更审阅 —— 原生 JavaScript 前端（无框架、无构建） */
"use strict";

const TYPE_LABELS = {
  unchanged: "无变更",
  added: "新增",
  deleted: "删除",
  split: "拆分",
  merged: "合并",
  rewritten: "改写",
};
const STATUS_LABELS = { pending: "待审阅", confirmed: "已确认" };
const DECISION_LABELS = { approve: "认可", reject: "驳回", modify: "需修改" };

const state = {
  review: null,        // GET /api/batches/:id 的完整数据
  filterType: "all",
  filterStatus: "all",
  modalAlignmentId: null,
};

// ------------------------------------------------------------------ 工具

function $(sel, root = document) {
  return root.querySelector(sel);
}
function $all(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function confClass(c) {
  if (c >= 0.8) return "conf-high";
  if (c >= 0.55) return "conf-mid";
  return "conf-low";
}

function joinIdx(arr) {
  return (arr || []).join("、");
}

// ------------------------------------------------------------------ 路由

function navigate(hash) {
  window.location.hash = hash;
}

function parseRoute() {
  const h = window.location.hash.replace(/^#/, "") || "/";
  const m = h.match(/^\/batch\/(\d+)/);
  if (m) return { name: "review", id: Number(m[1]) };
  if (h === "/upload") return { name: "upload" };
  return { name: "home" };
}

async function render() {
  const route = parseRoute();
  $all(".mainnav a").forEach((a) => a.classList.remove("active"));
  const navKey = route.name === "upload" ? "upload" : "home";
  const navEl = document.querySelector(`.mainnav a[data-nav="${navKey}"]`);
  if (navEl) navEl.classList.add("active");

  $("#modal-overlay").classList.add("hidden");

  try {
    if (route.name === "home") await renderHome();
    else if (route.name === "upload") renderUpload();
    else await renderReview(route.id);
  } catch (err) {
    $("#app").innerHTML =
      `<div class="card empty"><div class="big">⚠️</div>${escapeHtml(err.message)}</div>`;
  }
}

window.addEventListener("hashchange", render);

// ------------------------------------------------------------------ 首页：批次列表

async function renderHome() {
  const batches = await api("/api/batches");
  const rows = batches.map((b) => {
    const pct = b.alignment_count
      ? Math.round((b.confirmed_count / b.alignment_count) * 100)
      : 0;
    return `
    <tr class="clickable" data-action="open-batch" data-id="${b.id}">
      <td><b>${escapeHtml(b.name)}</b>
        <div class="muted" style="font-size:12px">#${b.id} · ${escapeHtml(b.created_at.replace("T", " ").replace("+00:00", " UTC"))}</div>
      </td>
      <td>${b.old_count} 段</td>
      <td>${b.new_count} 段</td>
      <td>${b.alignment_count} 条候选</td>
      <td>
        <div class="conf-bar" style="display:inline-block;vertical-align:middle">
          <i class="conf-high" style="width:${pct}%"></i>
        </div>
        <span class="muted" style="font-size:12px;margin-left:6px">
          ${b.confirmed_count}/${b.alignment_count}（${pct}%）
        </span>
      </td>
      <td class="text-right">
        <button class="btn btn-sm btn-danger" data-action="delete-batch"
          data-id="${b.id}" data-name="${escapeHtml(b.name)}">删除</button>
      </td>
    </tr>`;
  }).join("");

  $("#app").innerHTML = `
    <div class="page-head">
      <div>
        <h2>审阅批次</h2>
        <div class="page-sub">上传法规旧版/新版纯文本，生成条款对齐候选并逐条审阅</div>
      </div>
      <a class="btn btn-primary" href="#/upload">+ 新建对齐</a>
    </div>
    ${batches.length === 0
      ? `<div class="card empty">
           <div class="big">📂</div>
           <p>还没有任何批次。<a href="#/upload">上传两版文本</a>开始第一次审阅。</p>
         </div>`
      : `<div class="table-wrap">
           <table>
             <thead><tr>
               <th>批次</th><th>旧版</th><th>新版</th>
               <th>对齐候选</th><th>审阅进度</th><th></th>
             </tr></thead>
             <tbody>${rows}</tbody>
           </table>
         </div>`}
  `;
}

// ------------------------------------------------------------------ 上传页

function renderUpload() {
  $("#app").innerHTML = `
    <div class="page-head">
      <div><h2>新建对齐</h2>
        <div class="page-sub">支持 UTF-8 / GB18030 编码的 .txt 文件；按空行或行序切分段落</div>
      </div>
    </div>
    <form id="upload-form" class="card">
      <div class="form-field" style="margin-bottom:18px">
        <label for="batch-name">批次名称</label>
        <input type="text" id="batch-name" placeholder="例如：XX法 2024 → 2026 修订"
          style="max-width:420px" required>
      </div>
      <div class="form-grid">
        <div class="form-field">
          <label>旧版文本 <span class="hint">（修订前）</span></label>
          <label class="dropzone" id="dz-old">
            <input type="file" id="file-old" accept=".txt,text/plain">
            <div class="dz-title">点击选择或拖拽旧版 .txt 到此处</div>
            <div class="dz-file"></div>
          </label>
        </div>
        <div class="form-field">
          <label>新版文本 <span class="hint">（修订后）</span></label>
          <label class="dropzone" id="dz-new">
            <input type="file" id="file-new" accept=".txt,text/plain">
            <div class="dz-title">点击选择或拖拽新版 .txt 到此处</div>
            <div class="dz-file"></div>
          </label>
        </div>
      </div>
      <div style="margin-top:22px;display:flex;gap:10px">
        <button class="btn btn-primary" type="submit" id="submit-btn">
          生成对齐候选
        </button>
        <a class="btn btn-ghost" href="#/">取消</a>
      </div>
    </form>`;

  const files = { old: null, new: null };

  for (const side of ["old", "new"]) {
    const dz = $(`#dz-${side}`);
    const input = $(`#file-${side}`);
    dz.addEventListener("dragover", (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    });
    dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
      if (e.dataTransfer.files[0]) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      }
    });
    input.addEventListener("change", () => {
      const f = input.files[0];
      files[side] = f || null;
      dz.classList.toggle("has-file", !!f);
      $(".dz-file", dz).textContent = f
        ? `已选择：${f.name}（${(f.size / 1024).toFixed(1)} KB）`
        : "";
    });
  }

  $("#upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!files.old || !files.new) {
      toast("请先选择旧版和新版文本文件", "error");
      return;
    }
    const btn = $("#submit-btn");
    btn.disabled = true;
    btn.textContent = "正在比对并生成候选…";
    try {
      const fd = new FormData();
      fd.append("name", $("#batch-name").value);
      fd.append("old_file", files.old);
      fd.append("new_file", files.new);
      const result = await api("/api/batches", { method: "POST", body: fd });
      toast(`已生成 ${result.alignment_count} 条对齐候选`, "success");
      navigate(`/batch/${result.id}`);
    } catch (err) {
      toast(err.message, "error");
      btn.disabled = false;
      btn.textContent = "生成对齐候选";
    }
  });
}

// ------------------------------------------------------------------ 审阅页

async function renderReview(id) {
  state.review = await api(`/api/batches/${id}`);
  state.filterType = "all";
  state.filterStatus = "all";
  drawReview();
}

function filteredAlignments() {
  const { alignments } = state.review;
  return alignments.filter((a) =>
    (state.filterType === "all" || a.change_type === state.filterType) &&
    (state.filterStatus === "all" || a.status === state.filterStatus)
  );
}

function drawReview() {
  const data = state.review;
  const b = data.batch;
  const alignments = data.alignments;

  // 统计（全部候选）
  const byType = {};
  Object.keys(TYPE_LABELS).forEach((t) => (byType[t] = 0));
  let confirmed = 0;
  alignments.forEach((a) => {
    byType[a.change_type]++;
    if (a.status === "confirmed") confirmed++;
  });

  const typeChips = ["all", ...Object.keys(TYPE_LABELS)].map((t) => {
    const label = t === "all" ? "全部类型" : TYPE_LABELS[t];
    const count = t === "all" ? alignments.length : byType[t];
    return `<button class="chip ${state.filterType === t ? "active" : ""}"
      data-action="filter-type" data-type="${t}">${label} ${count}</button>`;
  }).join("");

  const shown = filteredAlignments();

  $("#app").innerHTML = `
    <div class="page-head">
      <div>
        <h2>${escapeHtml(b.name)}</h2>
        <div class="page-sub">
          #${b.id} ·
          旧版文件：${escapeHtml(b.old_filename || "—")}（${data.old_paragraphs.length} 段）
          ｜新版文件：${escapeHtml(b.new_filename || "—")}（${data.new_paragraphs.length} 段）
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <a class="btn btn-ghost" href="/api/batches/${b.id}/export.csv"
           target="_blank">导出 CSV</a>
        <a class="btn btn-ghost" href="/api/batches/${b.id}/export.json"
           target="_blank">导出 JSON</a>
        <a class="btn btn-ghost" href="#/">返回列表</a>
      </div>
    </div>

    <div class="stats">
      <span class="stat-pill">对齐候选 <b>${alignments.length}</b></span>
      <span class="stat-pill">已确认 <b>${confirmed}</b></span>
      <span class="stat-pill">待审阅 <b>${alignments.length - confirmed}</b></span>
      <span class="stat-pill" style="color:var(--added)">新增 <b>${byType.added}</b></span>
      <span class="stat-pill" style="color:var(--deleted)">删除 <b>${byType.deleted}</b></span>
      <span class="stat-pill" style="color:var(--split)">拆分 <b>${byType.split}</b></span>
      <span class="stat-pill" style="color:var(--merged)">合并 <b>${byType.merged}</b></span>
      <span class="stat-pill" style="color:var(--rewritten)">改写 <b>${byType.rewritten}</b></span>
      <span class="stat-pill" style="color:var(--unchanged)">无变更 <b>${byType.unchanged}</b></span>
    </div>

    <div class="toolbar">
      <div class="chip-group">${typeChips}</div>
      <div class="spacer"></div>
      <select id="filter-status" style="width:auto">
        <option value="all">全部状态</option>
        <option value="pending" ${state.filterStatus === "pending" ? "selected" : ""}>仅待审阅</option>
        <option value="confirmed" ${state.filterStatus === "confirmed" ? "selected" : ""}>仅已确认</option>
      </select>
    </div>

    <div id="alignment-list">
      ${shown.length === 0
        ? `<div class="card empty"><div class="big">🔍</div>当前筛选条件下没有对齐候选</div>`
        : shown.map(alignmentCardHtml).join("")}
    </div>`;

  $("#filter-status").addEventListener("change", (e) => {
    state.filterStatus = e.target.value;
    drawReview();
  });
}

function paraSideHtml(side, paras, emptyText) {
  const label = side === "old" ? "旧版" : "新版";
  const items = paras.length
    ? paras.map(
        (p) =>
          `<div class="para-item"><span class="seq">第 ${p.idx} 段</span>${escapeHtml(p.text)}</div>`
      ).join("")
    : `<div class="para-empty">${emptyText}</div>`;
  return `
    <div class="para-side ${side}">
      <div class="side-label ${side}">${label} · ${paras.length} 段映射</div>
      ${items}
    </div>`;
}

function alignmentCardHtml(a) {
  const c = a.confidence;
  const pct = Math.round(c * 100);
  const ev = JSON.stringify(a.rationale.evidence || {}, null, 2);
  const statusBadge =
    `<span class="badge badge-status ${a.status}">${STATUS_LABELS[a.status]}</span>`;
  const manualBadge = a.is_manual
    ? `<span class="badge badge-manual">手动调整</span>` : "";

  return `
  <div class="align-card t-${a.change_type}" data-id="${a.id}">
    <div class="align-head">
      <span class="align-id">#${a.id}</span>
      <span class="badge ${a.change_type}">${TYPE_LABELS[a.change_type]}</span>
      ${statusBadge}
      ${manualBadge}
      <span class="conf">
        置信度 ${pct}%
        <span class="conf-bar"><i class="${confClass(c)}" style="width:${pct}%"></i></span>
      </span>
      <span class="spacer"></span>
      <button class="btn btn-sm btn-ghost" data-action="edit-mapping"
        data-id="${a.id}">编辑映射</button>
    </div>
    <details class="rationale">
      <summary>判定依据：${escapeHtml(a.rationale.summary || "")}</summary>
      <div class="ev">${escapeHtml(ev)}</div>
    </details>
    <div class="align-body">
      ${paraSideHtml("old", a.old_paragraphs,
        `（无旧版段落 —— ${TYPE_LABELS.added}）`)}
      ${paraSideHtml("new", a.new_paragraphs,
        `（无新版段落 —— ${TYPE_LABELS.deleted}）`)}
    </div>
    <div class="align-foot">
      <label class="muted" style="font-size:12.5px">审阅决定</label>
      <select data-action="decision-select" data-id="${a.id}">
        <option value="">未选择</option>
        <option value="approve" ${a.decision === "approve" ? "selected" : ""}>认可</option>
        <option value="reject" ${a.decision === "reject" ? "selected" : ""}>驳回</option>
        <option value="modify" ${a.decision === "modify" ? "selected" : ""}>需修改</option>
      </select>
      <input type="text" placeholder="审阅备注（可选）"
        data-action="decision-note" data-id="${a.id}"
        value="${escapeHtml(a.decision_note || "")}">
      <button class="btn btn-sm btn-primary" data-action="save-decision"
        data-id="${a.id}">保存决定</button>
      <span class="spacer"></span>
      ${a.decision
        ? `<span class="muted" style="font-size:12px">
             ${DECISION_LABELS[a.decision]} · ${escapeHtml((a.decided_at || "").replace("T", " ").replace("+00:00", " UTC"))}
           </span>` : ""}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ 弹层：编辑映射

function openMappingModal(alignmentId) {
  const a = state.review.alignments.find((x) => x.id === alignmentId);
  if (!a) return;
  state.modalAlignmentId = alignmentId;

  const oldSel = new Set(a.old_idx);
  const newSel = new Set(a.new_idx);

  const pickerHtml = (paras, side, selected) =>
    paras.map((p) => `
      <label class="picker-item ${selected.has(p.idx) ? "selected" : ""}">
        <input type="checkbox" data-side="${side}" data-idx="${p.idx}"
          ${selected.has(p.idx) ? "checked" : ""}>
        <span>
          <span class="p-idx">第 ${p.idx} 段</span><br>
          <span class="p-text">${escapeHtml(p.text)}</span>
        </span>
      </label>`).join("");

  $("#picker-old").innerHTML =
    pickerHtml(state.review.old_paragraphs, "old", oldSel);
  $("#picker-new").innerHTML =
    pickerHtml(state.review.new_paragraphs, "new", newSel);

  // 判定当前映射形态，预选类型；手动类型一律显示“自动判定”以外的值
  $("#modal-type").value = "";
  $("#modal-hint").textContent =
    "多选段落可表达拆分（1 旧 → 多新）或合并（多旧 → 1 新）；" +
    "只选一侧表示新增/删除。保存后系统会重新计算变更类型、置信度与依据。";
  $("#modal-title").textContent = `调整条款映射 · 对齐 #${alignmentId}`;
  $("#modal-overlay").classList.remove("hidden");

  $all(".picker-item input").forEach((cb) =>
    cb.addEventListener("change", () =>
      cb.closest(".picker-item").classList.toggle("selected", cb.checked)
    )
  );
}

function closeMappingModal() {
  state.modalAlignmentId = null;
  $("#modal-overlay").classList.add("hidden");
}

async function saveMapping() {
  const id = state.modalAlignmentId;
  if (id == null) return;
  const old_idx = $all('#picker-old input:checked').map((c) => Number(c.dataset.idx));
  const new_idx = $all('#picker-new input:checked').map((c) => Number(c.dataset.idx));
  if (!old_idx.length && !new_idx.length) {
    toast("旧版与新版映射不能同时为空", "error");
    return;
  }
  const change_type = $("#modal-type").value || null;
  try {
    await api(`/api/alignments/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ old_idx, new_idx, change_type }),
    });
    closeMappingModal();
    await refreshReview();
    toast("映射已更新并重新判定", "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function refreshReview() {
  const id = state.review.batch.id;
  state.review = await api(`/api/batches/${id}`);
  drawReview();
}

// ------------------------------------------------------------------ 全局事件委托

document.addEventListener("click", async (e) => {
  const target = e.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;

  if (action === "open-batch") {
    navigate(`/batch/${target.dataset.id}`);
  } else if (action === "delete-batch") {
    e.stopPropagation();
    if (!confirm(`确定删除批次「${target.dataset.name}」及其全部审阅记录？`)) return;
    try {
      await api(`/api/batches/${target.dataset.id}`, { method: "DELETE" });
      toast("批次已删除", "success");
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  } else if (action === "filter-type") {
    state.filterType = target.dataset.type;
    drawReview();
  } else if (action === "edit-mapping") {
    openMappingModal(Number(target.dataset.id));
  } else if (action === "save-decision") {
    const id = target.dataset.id;
    const card = target.closest(".align-card");
    const decision = $('[data-action="decision-select"]', card).value;
    const note = $('[data-action="decision-note"]', card).value;
    try {
      await api(`/api/alignments/${id}`, {
        method: "PATCH",
        body: JSON.stringify({
          decision: decision || null,
          decision_note: note,
        }),
      });
      await refreshReview();
      toast("审阅决定已保存", "success");
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

$("#modal-close").addEventListener("click", closeMappingModal);
$("#modal-cancel").addEventListener("click", closeMappingModal);
$("#modal-save").addEventListener("click", saveMapping);
$("#modal-overlay").addEventListener("click", (e) => {
  if (e.target.id === "modal-overlay") closeMappingModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMappingModal();
});

// ------------------------------------------------------------------ 启动

render();
