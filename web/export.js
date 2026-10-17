"use strict";

// ---------- 全局状态 ----------
const state = {
  gid: null,
  groups: [],
  members: [],          // 当前群成员列表
  lastExportToken: null, // 最近一次导出的下载 token
  activeTab: "member",   // 'member' | 'daily' | 'diff'
  lastResultTab: null,   // 上次渲染结果区的 Tab，用于切换时清理
  diff: {               // 增量对比状态
    oldRows: null,      // 解析后的 [{sender_name, text}]
    newRows: null,
    deltaRows: null,    // 新增消息数组
  },
};

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const elGroup = $("group-select");
const elStatus = $("status");
const elTabMemberBtn = document.querySelector('.tab[data-tab="member"]');
const elTabDailyBtn = document.querySelector('.tab[data-tab="daily"]');
const elTabDiffBtn = document.querySelector('.tab[data-tab="diff"]');
const elTabMember = $("tab-member");
const elTabDaily = $("tab-daily");
const elTabDiff = $("tab-diff");

// 成员导出 Tab
const elMemberSelectM = $("member-select-member");
const elSinceM = $("since-member");
const elUntilM = $("until-member");
const elFmtRadios = document.querySelectorAll('input[name="fmt"]');
const elCompactCheck = $("compact-check");
const elCompactWrap = $("compact-wrap");
const elExportMemberBtn = $("export-member-btn");

// 日报导出 Tab
const elMemberSelectD = $("member-select-daily");
const elDateDaily = $("date-daily");
const elExportDailyBtn = $("export-daily-btn");

// 增量对比 Tab
const elDiffOldFile = $("diff-old-file");
const elDiffNewFile = $("diff-new-file");
const elDiffOldName = $("diff-old-name");
const elDiffNewName = $("diff-new-name");
const elDiffOldMeta = $("diff-old-meta");
const elDiffNewMeta = $("diff-new-meta");
const elDiffBtn = $("diff-btn");
const elDiffFmtRadios = document.querySelectorAll('input[name="diff-fmt"]');

// 结果区
const elResultArea = $("result-area");
const elResultMeta = $("result-meta");
const elDownloadBtn = $("download-btn");
const elPreviewTable = $("preview-table");
const elPreviewCount = $("preview-count");

// ---------- 工具 ----------
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && data.error) msg = data.error;
    } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return resp.json();
}

function setStatus(msg, isError) {
  elStatus.textContent = msg || "";
  elStatus.style.color = isError ? "#dc3545" : "#888";
}

function clearResult() {
  elResultArea.hidden = true;
  elResultMeta.innerHTML = "";
  elPreviewTable.innerHTML = "";
  elPreviewCount.textContent = "0";
  state.lastExportToken = null;
  elDownloadBtn.textContent = "下载文件";
}

function showError(msg) {
  elResultArea.hidden = false;
  elResultMeta.innerHTML = `<div class="result-error">❌ ${escapeHtml(msg)}</div>`;
  elDownloadBtn.disabled = true;
  elPreviewTable.innerHTML = "";
  elPreviewCount.textContent = "0";
  state.lastExportToken = null;
}

function metaItem(label, value, highlight) {
  const cls = highlight ? "meta-value meta-highlight" : "meta-value";
  return `<span class="meta-item"><span class="meta-label">${escapeHtml(label)}：</span><span class="${cls}">${escapeHtml(value)}</span></span>`;
}

// ---------- Tab 切换 ----------
function switchTab(tab) {
  state.activeTab = tab;
  elTabMemberBtn.classList.toggle("active", tab === "member");
  elTabDailyBtn.classList.toggle("active", tab === "daily");
  elTabDiffBtn.classList.toggle("active", tab === "diff");
  elTabMember.classList.toggle("active", tab === "member");
  elTabDaily.classList.toggle("active", tab === "daily");
  elTabDiff.classList.toggle("active", tab === "diff");
  // 切换 Tab 时清理结果区（diff 也复用同一结果区）
  if (tab !== state.lastResultTab) {
    clearResult();
    state.lastResultTab = tab;
  }
}
elTabMemberBtn.onclick = () => switchTab("member");
elTabDailyBtn.onclick = () => switchTab("daily");
elTabDiffBtn.onclick = () => switchTab("diff");

// 格式联动：CSV 时禁用紧凑模式
function updateCompactState() {
  const fmt = document.querySelector('input[name="fmt"]:checked').value;
  const isCsv = fmt === "csv";
  elCompactCheck.disabled = isCsv;
  elCompactWrap.style.opacity = isCsv ? "0.5" : "1";
  if (isCsv) elCompactCheck.checked = false;
}
elFmtRadios.forEach(r => r.addEventListener("change", updateCompactState));

// ---------- 数据加载 ----------
async function loadGroups() {
  try {
    state.groups = await api("/api/groups");
    elGroup.innerHTML = state.groups.map(g =>
      `<option value="${g.gid}">${escapeHtml(g.name)} (${g.msg_count})</option>`).join("");
  } catch (e) {
    setStatus("加载群列表失败：" + e.message, true);
  }
}

async function loadMembers(gid) {
  try {
    state.members = await api(`/api/members?gid=${gid}`);
    const opts = state.members.map(m =>
      `<option value="${escapeHtml(m.sender_name)}">${escapeHtml(m.sender_name)} (${m.msg_count})</option>`).join("");
    // 成员导出：必选，无"全部"项
    elMemberSelectM.innerHTML = opts || '<option value="">（无成员）</option>';
    // 日报导出：可选，留空 = 仅上下文
    elMemberSelectD.innerHTML = '<option value="">（不指定，仅上下文）</option>' + opts;
  } catch (e) {
    elMemberSelectM.innerHTML = '<option value="">（加载失败）</option>';
    elMemberSelectD.innerHTML = '<option value="">（加载失败）</option>';
  }
}

async function selectGroup(gid) {
  state.gid = gid;
  clearResult();
  setStatus("加载成员…");
  await loadMembers(gid);
  setStatus("");
}

// ---------- 成员发言导出 ----------
async function doExportMember() {
  if (!state.gid) {
    setStatus("请先选择群", true);
    return;
  }
  const senderName = elMemberSelectM.value;
  if (!senderName) {
    setStatus("请选择成员", true);
    return;
  }
  const fmt = document.querySelector('input[name="fmt"]:checked').value;
  const compact = elCompactCheck.checked;
  const since = elSinceM.value;
  const until = elUntilM.value;

  const body = { gid: state.gid, sender_name: senderName, fmt, compact };
  if (since) body.since = since;
  if (until) body.until = until;

  elExportMemberBtn.disabled = true;
  elExportMemberBtn.textContent = "导出中…";
  setStatus("导出中…");
  try {
    const data = await api("/api/export/member", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderMemberResult(data);
    setStatus("");
  } catch (e) {
    showError(e.message);
    setStatus("导出失败", true);
  } finally {
    elExportMemberBtn.disabled = false;
    elExportMemberBtn.textContent = "导出";
  }
}

function renderMemberResult(data) {
  elResultArea.hidden = false;
  state.lastExportToken = data.download_token;
  elDownloadBtn.disabled = false;

  const parts = [
    metaItem("群", data.group_name || "(未知)"),
    metaItem("条数", String(data.count)),
    metaItem("起始", data.start_cst || "—"),
    metaItem("结束", data.end_cst || "—"),
    metaItem("文件", data.path),
  ];
  elResultMeta.innerHTML = parts.join("");

  // 预览表格
  elPreviewCount.textContent = String(data.preview_total);
  const preview = data.preview || [];
  if (!preview.length) {
    elPreviewTable.innerHTML = '<tr><td class="result-empty">无数据</td></tr>';
    return;
  }
  // JSON 导出每条只有 sender_name / text
  elPreviewTable.innerHTML =
    `<thead><tr><th class="col-sender_name">发言者</th><th class="col-text">正文</th></tr></thead>` +
    `<tbody>${preview.map(m =>
      `<tr><td class="col-sender_name">${escapeHtml(m.sender_name || "")}</td>` +
      `<td class="col-text">${escapeHtml(m.text || "")}</td></tr>`).join("")}</tbody>`;
}

// ---------- 日报导出 ----------
async function doExportDaily() {
  if (!state.gid) {
    setStatus("请先选择群", true);
    return;
  }
  const dateStr = elDateDaily.value;
  if (!dateStr) {
    setStatus("请选择日期", true);
    return;
  }
  const senderName = elMemberSelectD.value || null;

  const body = { gid: state.gid, date_str: dateStr };
  if (senderName) body.sender_name = senderName;

  elExportDailyBtn.disabled = true;
  elExportDailyBtn.textContent = "导出中…";
  setStatus("导出中…");
  try {
    const data = await api("/api/export/daily", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderDailyResult(data);
    setStatus("");
  } catch (e) {
    showError(e.message);
    setStatus("导出失败", true);
  } finally {
    elExportDailyBtn.disabled = false;
    elExportDailyBtn.textContent = "导出日报";
  }
}

function renderDailyResult(data) {
  elResultArea.hidden = false;
  state.lastExportToken = data.download_token;
  elDownloadBtn.disabled = false;

  const targetName = data.target_sender_name || "(未指定)";
  const parts = [
    metaItem("群", data.group_name || "(未知)"),
    metaItem("日期", data.date),
    metaItem("目标成员", targetName, data.target_count > 0),
    metaItem("当日总数", String(data.count)),
    metaItem("目标发言", String(data.target_count)),
    metaItem("上下文", String(data.context_count)),
    metaItem("文件", data.path),
  ];
  elResultMeta.innerHTML = parts.join("");

  elPreviewCount.textContent = String(data.preview_total);
  const preview = data.preview || [];
  if (!preview.length) {
    elPreviewTable.innerHTML = '<tr><td class="result-empty">当日无消息</td></tr>';
    return;
  }
  // CSV 预览每行有 created_at / sender_name / text / is_target
  elPreviewTable.innerHTML =
    `<thead><tr>` +
    `<th class="col-created_at">时间</th>` +
    `<th class="col-sender_name">发言者</th>` +
    `<th class="col-text">正文</th>` +
    `<th class="col-is-target">目标</th>` +
    `</tr></thead>` +
    `<tbody>${preview.map(r => {
      const isTarget = String(r.is_target) === "1";
      return `<tr class="${isTarget ? "is-target" : ""}">` +
        `<td class="col-created_at">${escapeHtml(r.created_at || "")}</td>` +
        `<td class="col-sender_name">${escapeHtml(r.sender_name || "")}</td>` +
        `<td class="col-text">${escapeHtml(r.text || "")}</td>` +
        `<td class="col-is-target">${isTarget ? "★" : ""}</td>` +
        `</tr>`;
    }).join("")}</tbody>`;
}

// ---------- 下载 ----------
elDownloadBtn.onclick = () => {
  if (!state.lastExportToken) return;
  // 用 a 标签触发下载（带 filename 头），避免导航影响
  const a = document.createElement("a");
  a.href = `/api/export/download?token=${encodeURIComponent(state.lastExportToken)}`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};

// ---------- 增量对比 ----------

/**
 * 检测文件格式：JSON 数组 或 CSV。
 * 优先按扩展名判断，扩展名不明时按内容首字符判断。
 */
function detectFormat(filename, text) {
  const lower = (filename || "").toLowerCase();
  if (lower.endsWith(".json")) return "json";
  if (lower.endsWith(".csv")) return "csv";
  // 兜底：内容首非空白字符
  const trimmed = text.replace(/^\s+/, "");
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) return "json";
  return "csv";
}

/**
 * 轻量 CSV 解析：支持 BOM 前缀、引号包裹、字段内逗号/换行/引号转义。
 * 仅取前两个字段 sender_name, text，多余字段忽略。
 * 返回 [{sender_name, text}, ...]
 */
function parseCsv(text) {
  // 去 BOM
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  const rows = [];
  let i = 0, n = text.length;
  let field = "", row = [], inQuotes = false;
  while (i < n) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i++; continue;
      }
      // 引号内的 \r\n 归一化为 \n，避免 Windows 换行残留
      if (c === '\r' && text[i + 1] === '\n') { field += '\n'; i += 2; continue; }
      field += c; i++; continue;
    }
    if (c === '"') { inQuotes = true; i++; continue; }
    if (c === ',') { row.push(field); field = ""; i++; continue; }
    if (c === '\r') { i++; continue; }
    if (c === '\n') {
      row.push(field); field = "";
      if (row.length && !(row.length === 1 && row[0] === "")) rows.push(row);
      row = []; i++; continue;
    }
    field += c; i++;
  }
  // 末行（无换行结尾）
  if (field !== "" || row.length) {
    row.push(field);
    if (row.length && !(row.length === 1 && row[0] === "")) rows.push(row);
  }
  // 跳过表头（若含 sender_name）
  let startIdx = 0;
  if (rows.length && rows[0].length >= 2 &&
      String(rows[0][0]).toLowerCase().includes("sender_name")) {
    startIdx = 1;
  }
  const result = [];
  for (let r = startIdx; r < rows.length; r++) {
    const row = rows[r];
    if (row.length < 2) continue;
    result.push({
      sender_name: String(row[0] || ""),
      text: String(row[1] || ""),
    });
  }
  return result;
}

/**
 * 解析文件文本为消息数组 [{sender_name, text}]。
 */
function parseFileText(filename, text) {
  const fmt = detectFormat(filename, text);
  if (fmt === "json") {
    const data = JSON.parse(text);
    if (!Array.isArray(data)) {
      throw new Error("JSON 文件不是数组格式");
    }
    return data.map(m => ({
      sender_name: String(m.sender_name || ""),
      text: String(m.text || ""),
    }));
  }
  return parseCsv(text);
}

/**
 * 读取 File 对象为文本。
 */
function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsText(file, "utf-8");
  });
}

/**
 * 取前 N 条 sender_name 供核对（用户要求只看前 10 条）。
 */
function topSenders(rows, n = 10) {
  return rows.slice(0, n).map(r => r.sender_name || "(空)");
}

/**
 * 组合键差集：新包减旧包，返回新包有但旧包没有的消息。
 * key = sender_name \u0001 text \u0001 seq，seq 为同 (sender_name,text) 第几次出现。
 */
function computeDelta(oldRows, newRows) {
  // 旧包：baseKey -> count
  const oldCounts = new Map();
  for (const r of oldRows) {
    const base = `${r.sender_name}\u0001${r.text}`;
    oldCounts.set(base, (oldCounts.get(base) || 0) + 1);
  }
  // 新包：按顺序消费旧包配额，超出则为新增
  const cursor = new Map();
  const delta = [];
  for (const r of newRows) {
    const base = `${r.sender_name}\u0001${r.text}`;
    const used = cursor.get(base) || 0;
    const limit = oldCounts.get(base) || 0;
    if (used < limit) {
      cursor.set(base, used + 1); // 视为重复
    } else {
      delta.push(r); // 新增
    }
  }
  return delta;
}

/**
 * 处理文件选择：读取并解析，更新文件槽显示。
 */
async function handleDiffFileChange(which) {
  const input = which === "old" ? elDiffOldFile : elDiffNewFile;
  const nameEl = which === "old" ? elDiffOldName : elDiffNewName;
  const metaEl = which === "old" ? elDiffOldMeta : elDiffNewMeta;
  const file = input.files && input.files[0];
  if (!file) {
    nameEl.textContent = "未选择";
    nameEl.classList.remove("loaded");
    metaEl.textContent = "";
    if (which === "old") state.diff.oldRows = null;
    else state.diff.newRows = null;
    return;
  }
  nameEl.textContent = file.name;
  nameEl.classList.remove("loaded");
  metaEl.textContent = "解析中…";
  metaEl.classList.remove("error");
  try {
    const text = await readFileAsText(file);
    const rows = parseFileText(file.name, text);
    if (which === "old") state.diff.oldRows = rows;
    else state.diff.newRows = rows;
    nameEl.classList.add("loaded");
    metaEl.textContent = `${rows.length} 条`;
  } catch (e) {
    if (which === "old") state.diff.oldRows = null;
    else state.diff.newRows = null;
    metaEl.textContent = "解析失败：" + e.message;
    metaEl.classList.add("error");
  }
}

/**
 * 执行增量对比。
 */
async function doDiff() {
  const oldRows = state.diff.oldRows;
  const newRows = state.diff.newRows;
  if (!oldRows || !oldRows.length) {
    setStatus("请先选择旧数据包", true);
    return;
  }
  if (!newRows || !newRows.length) {
    setStatus("请先选择新数据包", true);
    return;
  }

  elDiffBtn.disabled = true;
  elDiffBtn.textContent = "对比中…";
  setStatus("对比中…");
  try {
    const delta = computeDelta(oldRows, newRows);
    state.diff.deltaRows = delta;
    renderDiffResult(oldRows, newRows, delta);
    setStatus("");
  } catch (e) {
    showError(e.message);
    setStatus("对比失败", true);
  } finally {
    elDiffBtn.disabled = false;
    elDiffBtn.textContent = "开始对比";
  }
}

function renderDiffResult(oldRows, newRows, delta) {
  elResultArea.hidden = false;
  state.lastExportToken = null; // diff 走前端 Blob，不用后端 token
  elDownloadBtn.disabled = false;

  const oldTop = topSenders(oldRows);
  const newTop = topSenders(newRows);
  // 前 10 条 sender_name 是否一致
  const sameSender = oldTop.length === newTop.length &&
    oldTop.every((s, i) => s === newTop[i]);
  const senderNote = sameSender
    ? "一致"
    : "不一致（请确认两包为同一群同一成员）";

  const parts = [
    metaItem("旧包条数", String(oldRows.length)),
    metaItem("新包条数", String(newRows.length)),
    metaItem("新增条数", String(delta.length), delta.length > 0),
    metaItem("重复条数", String(newRows.length - delta.length)),
    metaItem("发言者核对", senderNote, !sameSender),
  ];
  elResultMeta.innerHTML = parts.join("");

  // 下载按钮文案
  elDownloadBtn.textContent = "下载补充包";

  // 预览表格（前 50 条新增）
  const PREVIEW_LIMIT = 50;
  elPreviewCount.textContent = String(Math.min(delta.length, PREVIEW_LIMIT));
  if (!delta.length) {
    elPreviewTable.innerHTML = '<tr><td class="result-empty">无新增消息</td></tr>';
    return;
  }
  elPreviewTable.innerHTML =
    `<thead><tr><th class="col-sender_name">发言者</th><th class="col-text">正文</th></tr></thead>` +
    `<tbody>${delta.slice(0, PREVIEW_LIMIT).map(m =>
      `<tr><td class="col-sender_name">${escapeHtml(m.sender_name || "")}</td>` +
      `<td class="col-text">${escapeHtml(m.text || "")}</td></tr>`).join("")}</tbody>`;
}

/**
 * CSV 序列化：表头 sender_name,text，字段含逗号/引号/换行时用引号包裹并转义。
 * 加 BOM 前缀保证 Excel 中文不乱码（与后端 utf-8-sig 一致）。
 */
function toCsv(rows) {
  const esc = (v) => {
    const s = String(v == null ? "" : v);
    if (/[",\r\n]/.test(s)) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  };
  let out = "\uFEFFsender_name,text\r\n";
  for (const r of rows) {
    out += esc(r.sender_name) + "," + esc(r.text) + "\r\n";
  }
  return out;
}

/**
 * 下载补充包：根据当前选择的格式序列化为 Blob 并触发下载。
 */
function downloadDelta() {
  const delta = state.diff.deltaRows;
  if (!delta || !delta.length) {
    setStatus("没有新增消息可下载", true);
    return;
  }
  const fmt = document.querySelector('input[name="diff-fmt"]:checked').value;
  let blob, ext;
  if (fmt === "csv") {
    const csv = toCsv(delta);
    blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    ext = "csv";
  } else {
    const json = JSON.stringify(delta, null, 2);
    blob = new Blob([json], { type: "application/json;charset=utf-8;" });
    ext = "json";
  }
  const ts = new Date();
  const pad = (x) => String(x).padStart(2, "0");
  const stamp = `${ts.getFullYear()}${pad(ts.getMonth()+1)}${pad(ts.getDate())}_${pad(ts.getHours())}${pad(ts.getMinutes())}`;
  const filename = `delta_${stamp}.${ext}`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// diff 下载走前端 Blob，重写下载按钮行为
const _origDownloadHandler = elDownloadBtn.onclick;
elDownloadBtn.onclick = (ev) => {
  if (state.activeTab === "diff") {
    downloadDelta();
    return;
  }
  if (_origDownloadHandler) _origDownloadHandler.call(elDownloadBtn, ev);
};

// ---------- 事件绑定 ----------
elGroup.onchange = () => selectGroup(parseInt(elGroup.value, 10));
elExportMemberBtn.onclick = doExportMember;
elExportDailyBtn.onclick = doExportDaily;
elDiffBtn.onclick = doDiff;
elDiffOldFile.onchange = () => handleDiffFileChange("old");
elDiffNewFile.onchange = () => handleDiffFileChange("new");

// ---------- 初始化 ----------
async function init() {
  updateCompactState();
  await loadGroups();
  if (state.groups.length) {
    await selectGroup(state.groups[0].gid);
  } else {
    setStatus("数据库中没有群");
  }
}

init();
