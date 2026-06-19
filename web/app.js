"use strict";

// ---------- 全局状态 ----------
const state = {
  gid: null,
  groups: [],
  dates: [],            // [{month:'YYYY-MM', days:[{date,count}], open:bool}]
  selectedDate: null,
  selectedSender: null, // sender_id 或 null
  senders: [],
  messages: [],         // 升序，最新在底
  before: null,         // {ts,id}
  after: null,          // {ts,id}
  hasMoreOlder: true,
  hasMoreNewer: true,
  loadingOlder: false,
  loadingNewer: false,
  reqId: 0,
};

const LIMIT = 500;

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const elGroup = $("group-select");
const elSender = $("sender-select");
const elSearch = $("search-input");
const elStatus = $("status");
const elDateList = $("date-list");
const elDatePicker = $("date-picker");
const elMsgList = $("message-list");
const elRange = $("range-indicator");
const elEmpty = $("empty-hint");
const elSentinelTop = $("sentinel-top");
const elSentinelBottom = $("sentinel-bottom");

// ---------- API ----------
async function api(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ---------- 工具 ----------
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function linkify(escaped) {
  // 在已转义的文本里把 URL 转链接（&amp; 已是实体，不误伤）
  return escaped.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function fmtTime(ms) {
  // 与后端 CST(+8) 一致：按 UTC+8 取时分，避免依赖运行机器时区
  const d = new Date(ms + 8 * 3600 * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function fmtDate(ms) {
  // 同样锚定 +8，取 YYYY-MM-DD
  const d = new Date(ms + 8 * 3600 * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())}`;
}

function cstDate(ms) {
  // 与后端一致：UTC ms + 8h 取 YYYY-MM-DD
  return fmtDate(ms);
}

// ---------- 消息渲染 ----------
function renderMessageBody(m) {
  const mt = m.media_type;
  const text = escapeHtml(m.text || "");
  const url = m.media_orig_url || "";
  const link = url ? ` <a href="${escapeHtml(url)}" target="_blank" rel="noopener">[链接]</a>` : "";
  if (m.msg_type !== 321 && m.msg_type !== 100) {
    // 系统消息（居中由外层处理），body 即文本
    return linkify(text);
  }
  if (mt === 0) return linkify(text);
  if (mt === 1) return `🖼 [图片]${link}`;
  if (mt === 5) return `📎 [文件]${link}`;
  if (mt === 10) return `🎬 [视频]${link}`;
  if (mt === 13) {
    if ((m.text || "").includes("红包")) return `🧧 [红包]${link}`;
    return `🎬 [视频]${link}`;
  }
  if (mt === 14) return `${linkify(text)} <span class="tag">[链接]</span>`;
  if (mt === 15) return `${linkify(text)} <span class="tag">[小程序]</span>`;
  return `${linkify(text)} <span class="tag">[未知媒体:${mt}]</span>`;
}

function isSystem(m) {
  return m.msg_type !== 321 && m.msg_type !== 100;
}

function messageEl(m, anchorMid) {
  if (isSystem(m)) {
    const div = document.createElement("div");
    div.className = "msg-system";
    div.dataset.mid = m.mid;
    div.dataset.date = cstDate(m.created_at);
    div.innerHTML = linkify(escapeHtml(m.text || ""));
    return div;
  }
  const div = document.createElement("div");
  div.className = "msg";
  div.dataset.mid = m.mid;
  div.dataset.date = cstDate(m.created_at);
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.innerHTML = `<span class="sender">${escapeHtml(m.sender_name || String(m.sender_id))}</span><span class="time">${fmtTime(m.created_at)}</span>`;
  const body = document.createElement("div");
  body.className = "msg-body";
  body.innerHTML = renderMessageBody(m);
  div.append(meta, body);
  if (anchorMid && m.mid === anchorMid) div.classList.add("msg-highlight");
  return div;
}

function dateSepEl(date) {
  const div = document.createElement("div");
  div.className = "date-sep";
  div.innerHTML = `<span>──── ${date} ────</span>`;
  return div;
}

function renderMessages(anchorMid) {
  // 清空旧消息但保留哨兵（哨兵在 #message-list 内）
  if (elSentinelTop.parentElement === elMsgList) elMsgList.removeChild(elSentinelTop);
  if (elSentinelBottom.parentElement === elMsgList) elMsgList.removeChild(elSentinelBottom);
  elMsgList.innerHTML = "";
  elMsgList.appendChild(elSentinelTop);
  let lastDate = null;
  for (const m of state.messages) {
    const d = cstDate(m.created_at);
    if (d !== lastDate) {
      elMsgList.appendChild(dateSepEl(d));
      lastDate = d;
    }
    elMsgList.appendChild(messageEl(m, anchorMid));
  }
  elMsgList.appendChild(elSentinelBottom);
  updateRangeIndicator();
  updateEmptyHint();
}

function updateRangeIndicator() {
  if (!state.messages.length) { elRange.textContent = ""; return; }
  const first = state.messages[0];
  const last = state.messages[state.messages.length - 1];
  elRange.textContent = `${fmtDate(first.created_at)} ${fmtTime(first.created_at)} → ${fmtDate(last.created_at)} ${fmtTime(last.created_at)}`;
}

function updateEmptyHint() {
  elEmpty.hidden = state.messages.length > 0;
  if (state.messages.length === 0) {
    elEmpty.textContent = state.gid ? "该范围内没有消息" : "请选择一个群";
  }
}

// ---------- 左栏日期列表 ----------
function renderDateList() {
  elDateList.innerHTML = "";
  for (const mg of state.dates) {
    const group = document.createElement("div");
    group.className = "month-group" + (mg.open ? " open" : "");
    const head = document.createElement("div");
    head.className = "month-header";
    const total = mg.days.reduce((s, d) => s + d.count, 0);
    head.textContent = `${mg.month} (${total})`;
    head.onclick = () => { mg.open = !mg.open; group.classList.toggle("open"); };
    const days = document.createElement("div");
    days.className = "month-days";
    for (const d of mg.days) {
      const item = document.createElement("div");
      item.className = "date-item" + (d.date === state.selectedDate ? " active" : "");
      item.dataset.date = d.date;
      const mmdd = d.date.slice(5);
      item.innerHTML = `<span>${mmdd}</span><span class="count">${d.count}</span>`;
      item.onclick = () => selectDate(d.date);
      days.appendChild(item);
    }
    group.append(head, days);
    elDateList.appendChild(group);
  }
}

function highlightDate(date) {
  // 展开对应月份并高亮
  for (const mg of state.dates) {
    if (mg.month === date.slice(0, 7)) mg.open = true;
  }
  state.selectedDate = date;
  renderDateList();
  const item = elDateList.querySelector(`.date-item[data-date="${date}"]`);
  if (item) item.scrollIntoView({ block: "nearest" });
}

// ---------- 数据加载 ----------
async function loadGroups() {
  state.groups = await api("/api/groups");
  elGroup.innerHTML = state.groups.map(g =>
    `<option value="${g.gid}">${escapeHtml(g.name)} (${g.msg_count})</option>`).join("");
}

async function loadSenders(gid) {
  state.senders = await api(`/api/senders?gid=${gid}`);
  elSender.innerHTML = `<option value="">全部发送者</option>` +
    state.senders.map(s => `<option value="${s.sender_id}">${escapeHtml(s.sender_name)} (${s.count})</option>`).join("");
}

async function loadDates(gid) {
  const data = await api(`/api/dates?gid=${gid}`);
  // 按月分组，倒序
  const byMonth = {};
  for (const d of data) {
    const m = d.date.slice(0, 7);
    if (!byMonth[m]) byMonth[m] = [];
    byMonth[m].push(d);
  }
  state.dates = Object.keys(byMonth).sort((a, b) => b.localeCompare(a)).map(m => ({
    month: m, days: byMonth[m], open: false,
  }));
  if (state.dates.length) state.dates[0].open = true; // 默认展开最近月
  renderDateList();
}

async function loadByDate(gid, date, senderId) {
  const myReq = ++state.reqId;
  elStatus.textContent = "加载中…";
  let params = `gid=${gid}&date=${encodeURIComponent(date)}&limit=${LIMIT}`;
  if (senderId) params += `&sender_id=${senderId}`;
  const data = await api(`/api/messages/by_date?${params}`);
  if (myReq !== state.reqId) return; // 已被新请求覆盖
  state.messages = data.messages;
  state.before = data.oldest;
  state.after = data.newest;
  state.hasMoreOlder = data.has_more_older;
  state.hasMoreNewer = data.has_more_newer;
  renderMessages(null);
  elStatus.textContent = `共 ${state.messages.length} 条`;
  // 滚到底（最新在底）
  elMsgList.scrollTop = elMsgList.scrollHeight;
}

// ---------- 选择操作 ----------
async function selectGroup(gid) {
  state.gid = gid;
  state.selectedSender = null;
  elSender.value = "";
  elStatus.textContent = "加载中…";
  await Promise.all([loadDates(gid), loadSenders(gid)]);
  // 默认选最新日期
  if (state.dates.length && state.dates[0].days.length) {
    await selectDate(state.dates[0].days[0].date);
  }
}

async function selectDate(date) {
  highlightDate(date);
  elDatePicker.value = date;
  await loadByDate(state.gid, date, state.selectedSender);
}

// ---------- 初始化 ----------
async function init() {
  await loadGroups();
  if (state.groups.length) {
    await selectGroup(state.groups[0].gid);
  } else {
    elStatus.textContent = "数据库中没有群";
  }
}

// 事件绑定
elGroup.onchange = () => selectGroup(parseInt(elGroup.value, 10));
elSender.onchange = () => {
  const v = elSender.value;
  state.selectedSender = v ? parseInt(v, 10) : null;
  if (state.selectedDate) loadByDate(state.gid, state.selectedDate, state.selectedSender);
};
elDatePicker.onchange = () => {
  if (elDatePicker.value) selectDate(elDatePicker.value);
};

init();
