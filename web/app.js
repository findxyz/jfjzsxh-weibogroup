"use strict";

// ---------- 全局状态 ----------
const state = {
  gid: null,
  groups: [],
  dates: [],            // [{month:'YYYY-MM', days:[{date,count}], open:bool}]
  selectedDate: null,
  messages: [],         // 升序，最新在底
  before: null,         // {ts,id}
  after: null,          // {ts,id}
  hasMoreOlder: true,
  hasMoreNewer: true,
  loadingOlder: false,
  loadingNewer: false,
  reqId: 0,
};

const LIMIT = 100;

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const elGroup = $("group-select");
const elSearchBtn = $("search-btn");
const elStatus = $("status");
const elDateList = $("date-list");
const elDatePicker = $("date-picker");
const elMsgList = $("message-list");
const elRange = $("range-indicator");
const elEmpty = $("empty-hint");
const elSentinelTop = $("sentinel-top");
const elSentinelBottom = $("sentinel-bottom");
const elLightbox = $("lightbox");
const elLbStage = document.querySelector(".lightbox-stage");
const elLbStatus = document.querySelector(".lightbox-status");
const elLbClose = document.querySelector(".lightbox-close");
const elLbBackdrop = document.querySelector(".lightbox-backdrop");

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

function dateStrToCstStartMs(dateStr) {
  // 与后端 _cst_day_bounds 一致：CST 当日 00:00 对应的 UTC 毫秒。
  // dateStr 为 'YYYY-MM-DD'，Date.UTC 得到该日 UTC 00:00 的 ms，
  // 再减 8h 即为 CST 00:00 的 UTC ms。非法返回 null。
  if (!dateStr) return null;
  const [y, m, d] = dateStr.split("-").map(Number);
  if (!y || !m || !d) return null;
  return Date.UTC(y, m - 1, d) - 8 * 3600 * 1000;
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
  if (mt === 1) {
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="1"><span class="media-icon">🖼</span><span>图片</span></div>`
      : `🖼 [图片]${link}`;
  }
  if (mt === 5) return `📎 [文件]${link}`;
  if (mt === 10) {
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="10"><span class="media-icon">🎬</span><span>视频</span></div>`
      : `🎬 [视频]${link}`;
  }
  if (mt === 13) {
    if ((m.text || "").includes("红包")) return `🧧 [红包]${link}`;
    return m.fid
      ? `<div class="media-ph" data-fid="${escapeHtml(m.fid)}" data-mtype="10"><span class="media-icon">🎬</span><span>视频</span></div>`
      : `🎬 [视频]${link}`;
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
    head.textContent = `${mg.month} (${mg.count})`;
    head.onclick = async () => {
      mg.open = !mg.open;
      if (mg.open && !mg.days) await loadMonthDays(mg.month);
      group.classList.toggle("open");
    };
    const days = document.createElement("div");
    days.className = "month-days";
    if (mg.days) {
      for (const d of mg.days) {
        const item = document.createElement("div");
        item.className = "date-item" + (d.date === state.selectedDate ? " active" : "");
        item.dataset.date = d.date;
        const mmdd = d.date.slice(5);
        item.innerHTML = `<span>${mmdd}</span><span class="count">${d.count}</span>`;
        item.onclick = () => selectDate(d.date);
        days.appendChild(item);
      }
    }
    group.append(head, days);
    elDateList.appendChild(group);
  }
}

function highlightDate(date) {
  // 展开对应月份（若每日未加载则懒加载后补渲染）并选中
  const month = date.slice(0, 7);
  const mg = state.dates.find(d => d.month === month);
  state.selectedDate = date;
  if (mg) {
    mg.open = true;
    if (!mg.days) {
      loadMonthDays(month).then(() => {
        renderDateList();
        const item = elDateList.querySelector(`.date-item[data-date="${date}"]`);
        if (item) item.scrollIntoView({ block: "nearest" });
      });
    }
  }
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

async function loadDates(gid) {
  const data = await api(`/api/dates?gid=${gid}`);
  // 按月聚合，倒序；days 初始为 null（点击展开才查）
  state.dates = data.map(m => ({ month: m.month, count: m.count, days: null, open: false }));
  renderDateList();
}

async function loadMonthDays(month) {
  const mg = state.dates.find(d => d.month === month);
  if (!mg || mg.days) return; // 已加载过则不重复查
  const data = await api(`/api/dates?gid=${state.gid}&month=${encodeURIComponent(month)}`);
  mg.days = data;
  renderDateList();
}

async function loadByDate(gid, date) {
  const myReq = ++state.reqId;
  elStatus.textContent = "加载中…";
  let params = `gid=${gid}&date=${encodeURIComponent(date)}&limit=${LIMIT}`;
  const data = await api(`/api/messages/by_date?${params}`);
  if (myReq !== state.reqId) return; // 已被新请求覆盖
  state.messages = data.messages;
  state.before = data.oldest;
  state.after = data.newest;
  state.hasMoreOlder = data.has_more_older;
  state.hasMoreNewer = data.has_more_newer;
  renderMessages(null);
  // 滚到底（最新在底）
  elMsgList.scrollTop = elMsgList.scrollHeight;
  elStatus.textContent = "";
}

// ---------- 选择操作 ----------
async function selectGroup(gid) {
  state.gid = gid;
  elStatus.textContent = "加载中…";
  await loadDates(gid);
  // 默认展开最近月 + 加载每日 + 选最新日期
  if (state.dates.length) {
    const latest = state.dates[0];
    latest.open = true;
    await loadMonthDays(latest.month);
    if (latest.days && latest.days.length) {
      await selectDate(latest.days[0].date);
    }
  }
}

async function selectDate(date) {
  highlightDate(date);
  elDatePicker.value = date;
  await loadByDate(state.gid, date);
}

// ---------- 搜索 ----------
const elOverlay = $("search-overlay");
const elSearchStart = $("search-start");
const elSearchEnd = $("search-end");
const elSearchSender = $("search-sender");
const elSearchKeyword = $("search-keyword");
const elSearchSubmit = $("search-submit");
const elSearchStatus = $("search-status");
const elSearchResults = $("search-results");
const elSearchClose = $("search-close");

function openSearch() {
  // 默认最近 30 天：起 = 今天往前30天，止 = 今天（CST 日期）
  const todayCst = fmtDate(Date.now());
  const todayMs = dateStrToCstStartMs(todayCst);
  if (!elSearchStart.value) {
    elSearchStart.value = fmtDate(todayMs - 30 * 86400000);
  }
  if (!elSearchEnd.value) elSearchEnd.value = todayCst;
  elOverlay.hidden = false;
  elSearchKeyword.focus();
}

function closeSearch() {
  elOverlay.hidden = true;
}

function snippetToHtml(snippet) {
  // \x00..\x01 包裹关键词 → <mark>
  const esc = escapeHtml(snippet);
  return esc.replace(/\x00/g, "<mark>").replace(/\x01/g, "</mark>");
}

async function doSearch() {
  if (!state.gid) { elSearchStatus.textContent = "请先选择群"; return; }
  const sender = elSearchSender.value.trim();
  const keyword = elSearchKeyword.value.trim();
  if (!sender && !keyword) {
    elSearchStatus.textContent = "请至少填写发送者名称或关键词一项";
    return;
  }
  // 起止日期 → CST 当日零点毫秒；end 取次日零点使区间含当天（[start, end)）
  const startTs = dateStrToCstStartMs(elSearchStart.value);
  let endTs = dateStrToCstStartMs(elSearchEnd.value);
  if (endTs != null) endTs += 86400000; // 含结束当天
  elSearchStatus.textContent = "搜索中…";
  elSearchResults.innerHTML = "";
  try {
    const params = new URLSearchParams({ gid: state.gid, limit: 1000 });
    if (startTs != null) params.set("start_ts", startTs);
    if (endTs != null) params.set("end_ts", endTs);
    if (keyword) params.set("q", keyword);
    if (sender) params.set("sender_name", sender);
    const data = await api(`/api/search?${params}`);
    const results = data.results || [];
    if (!results.length) {
      elSearchStatus.textContent = "未找到匹配消息";
      return;
    }
    elSearchStatus.textContent = `共 ${results.length} 条结果` + (results.length >= 1000 ? "（已达上限，请缩小范围）" : "");
    elSearchResults.innerHTML = "";
    for (const r of results) {
      const div = document.createElement("div");
      div.className = "search-result";
      div.innerHTML = `<div class="sr-meta"><span class="sender">${escapeHtml(r.sender_name)}</span><span>${fmtDate(r.created_at)} ${fmtTime(r.created_at)}</span></div><div class="sr-snippet">${snippetToHtml(r.snippet)}</div>`;
      div.onclick = () => jumpToMessage(r.mid);
      elSearchResults.appendChild(div);
    }
  } catch (e) {
    elSearchStatus.textContent = "搜索失败：" + e.message;
  }
}

async function jumpToMessage(mid) {
  closeSearch();
  const myReq = ++state.reqId;
  // around 单侧取 floor(LIMIT/2) 条，命中消息位于列表中间，便于看上下文
  const half = Math.floor(LIMIT / 2);
  const data = await api(`/api/messages/around?gid=${state.gid}&mid=${encodeURIComponent(mid)}&limit=${half}`);
  if (myReq !== state.reqId) return;
  state.messages = data.messages;
  state.before = data.oldest;
  state.after = data.newest;
  state.hasMoreOlder = data.has_more_older;
  state.hasMoreNewer = data.has_more_newer;
  renderMessages(mid);
  // 滚到命中消息并居中（前后各有内容，可真正居中）
  const target = elMsgList.querySelector(`[data-mid="${CSS.escape(mid)}"]`);
  if (target) target.scrollIntoView({ block: "center" });
  // 左栏同步到命中消息所在日
  if (state.messages.length) {
    const hit = state.messages.find(m => m.mid === mid) || state.messages[state.messages.length - 1];
    highlightDate(cstDate(hit.created_at));
  }
  elStatus.textContent = "";
}

// ---------- 双向滚动加载 ----------
async function loadOlder() {
  if (!state.before || state.loadingOlder || !state.hasMoreOlder) return;
  state.loadingOlder = true;
  showLoadingMarker("top");
  const myReq = state.reqId;
  let params = `gid=${state.gid}&before_ts=${state.before.ts}&limit=${LIMIT}`;
  try {
    const data = await api(`/api/messages?${params}`);
    if (myReq !== state.reqId) return;
    // 锚点定位法保持滚动位置：渲染前记录视口顶部附近的消息元素相对容器的偏移，
    // 渲染后定位到同一元素，使视口内容不跳动。比高度差补偿更稳健，不受
    // loading 标记等任何高度变化干扰。用 getBoundingClientRect 不依赖 offsetParent。
    const anchor = firstVisibleMsg();
    const oldRect = anchor
      ? anchor.el.getBoundingClientRect().top - elMsgList.getBoundingClientRect().top
      : 0;
    state.messages = data.messages.concat(state.messages);
    state.before = data.oldest;
    state.hasMoreOlder = data.has_more_older;
    renderMessages(null);
    if (anchor) {
      const same = elMsgList.querySelector(`[data-mid="${CSS.escape(anchor.mid)}"]`);
      if (same) {
        const newRect = same.getBoundingClientRect().top - elMsgList.getBoundingClientRect().top;
        elMsgList.scrollTop += newRect - oldRect;
      }
    }
  } catch (e) {
    elStatus.textContent = "加载更早失败：" + e.message;
  } finally {
    state.loadingOlder = false;
    showLoadingMarker(null);
  }
}

// 找到当前视口顶部第一个可见的消息元素（含系统消息），返回 {el, mid}。
// 用于上滑加载前锚定阅读位置，渲染后恢复。用 getBoundingClientRect 判定可见性。
function firstVisibleMsg() {
  const containerTop = elMsgList.getBoundingClientRect().top;
  let best = null;
  for (const el of elMsgList.querySelectorAll("[data-mid]")) {
    // 元素底部超过容器顶部即视为进入视口；记录首个及它之前最后一个
    if (el.getBoundingClientRect().bottom > containerTop) {
      best = el;
      break;
    }
    best = el;
  }
  return best ? { el: best, mid: best.dataset.mid } : null;
}

async function loadNewer() {
  if (!state.after || state.loadingNewer || !state.hasMoreNewer) return;
  state.loadingNewer = true;
  showLoadingMarker("bottom");
  const myReq = state.reqId;
  let params = `gid=${state.gid}&after_ts=${state.after.ts}&limit=${LIMIT}`;
  try {
    const data = await api(`/api/messages?${params}`);
    if (myReq !== state.reqId) return;
    // 记录当前滚动位置；新消息追加到底部，渲染后恢复同样的 scrollTop，
    // 使当前阅读位置不动，用户可自然向下滚看新内容（不跳到底）。
    const prevScrollTop = elMsgList.scrollTop;
    state.messages = state.messages.concat(data.messages);
    state.after = data.newest;
    state.hasMoreNewer = data.has_more_newer;
    renderMessages(null);
    elMsgList.scrollTop = prevScrollTop;
  } catch (e) {
    elStatus.textContent = "加载更新失败：" + e.message;
  } finally {
    state.loadingNewer = false;
    showLoadingMarker(null);
  }
}

let loadingMarkerTop = null, loadingMarkerBottom = null;
function showLoadingMarker(pos) {
  if (pos === "top") {
    if (!loadingMarkerTop) {
      loadingMarkerTop = document.createElement("div");
      loadingMarkerTop.className = "loading-more";
      loadingMarkerTop.textContent = "加载更早…";
    }
    if (loadingMarkerTop.parentElement !== elMsgList) elMsgList.insertBefore(loadingMarkerTop, elSentinelTop.nextSibling);
  } else if (pos === "bottom") {
    if (!loadingMarkerBottom) {
      loadingMarkerBottom = document.createElement("div");
      loadingMarkerBottom.className = "loading-more";
      loadingMarkerBottom.textContent = "加载更新…";
    }
    if (loadingMarkerBottom.parentElement !== elMsgList) elMsgList.insertBefore(loadingMarkerBottom, elSentinelBottom);
  } else {
    if (loadingMarkerTop && loadingMarkerTop.parentElement) loadingMarkerTop.remove();
    if (loadingMarkerBottom && loadingMarkerBottom.parentElement) loadingMarkerBottom.remove();
  }
}

// ---------- 图片/视频放大查看 ----------
let lbCurrent = null; // 'img' | 'video' | null

function openLightbox(loading) {
  elLbStage.innerHTML = "";
  elLbStatus.textContent = loading ? "加载中…" : "";
  elLbStatus.className = "lightbox-status";
  elLightbox.classList.remove("hidden");
}

function closeLightbox() {
  const v = elLbStage.querySelector("video");
  if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  elLbStage.innerHTML = "";
  elLbStatus.textContent = "";
  elLbStatus.className = "lightbox-status";
  elLightbox.classList.add("hidden");
  lbCurrent = null;
}

function openImage(fid) {
  lbCurrent = "img";
  openLightbox(true);
  const img = new Image();
  img.onload = () => {
    if (lbCurrent !== "img") return;
    elLbStage.innerHTML = "";
    elLbStage.appendChild(img);
    elLbStatus.textContent = "";
  };
  img.onerror = () => {
    if (lbCurrent !== "img") return;
    elLbStage.innerHTML = "";
    elLbStatus.innerHTML = "加载失败<button class='retry-btn' type='button'>重试</button>";
    elLbStatus.querySelector(".retry-btn").onclick = () => openImage(fid);
  };
  img.src = `/api/media/${encodeURIComponent(fid)}?t=${Date.now()}`;
}

function openVideo(fid) {
  lbCurrent = "video";
  openLightbox(true);
  const v = document.createElement("video");
  v.controls = true;
  v.autoplay = true;
  v.oncanplay = () => {
    if (lbCurrent !== "video") return;
    elLbStatus.textContent = "";
  };
  v.onerror = () => {
    if (lbCurrent !== "video") return;
    elLbStage.innerHTML = "";
    elLbStatus.innerHTML = "加载失败<button class='retry-btn' type='button'>重试</button>";
    elLbStatus.querySelector(".retry-btn").onclick = () => openVideo(fid);
  };
  v.src = `/api/media/${encodeURIComponent(fid)}?t=${Date.now()}`;
  elLbStage.innerHTML = "";
  elLbStage.appendChild(v);
}

function setupSentinels() {
  // rootMargin 提前预加载：用 100%（相对滚动容器可见高度）而非固定像素，
  // 这样大屏/小屏都能"提前约一屏"触发，避免固定 800px 在大屏上显得太晚。
  // IntersectionObserver 的 rootMargin 支持百分比，会随容器尺寸自动伸缩。
  const obsTop = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting) loadOlder();
  }, { root: elMsgList, rootMargin: "100% 0px 50px 0px" });
  obsTop.observe(elSentinelTop);
  const obsBottom = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting) loadNewer();
  }, { root: elMsgList, rootMargin: "50px 0px 100% 0px" });
  obsBottom.observe(elSentinelBottom);
}

// ---------- 初始化 ----------
async function init() {
  setupSentinels();
  await loadGroups();
  if (state.groups.length) {
    await selectGroup(state.groups[0].gid);
  } else {
    elStatus.textContent = "数据库中没有群";
  }
}

// 事件绑定
// 媒体占位框点击（事件委托）
elMsgList.addEventListener("click", (e) => {
  const ph = e.target.closest(".media-ph");
  if (!ph) return;
  const fid = ph.dataset.fid;
  const mtype = ph.dataset.mtype;
  if (mtype === "1") openImage(fid);
  else if (mtype === "10") openVideo(fid);
});

// lightbox 关闭
elLbClose.onclick = closeLightbox;
elLbBackdrop.addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !elLightbox.classList.contains("hidden")) closeLightbox();
});

elGroup.onchange = () => selectGroup(parseInt(elGroup.value, 10));

elDatePicker.onchange = () => {
  if (elDatePicker.value) selectDate(elDatePicker.value);
};

// 搜索事件绑定
elSearchBtn.onclick = openSearch;
elSearchSubmit.onclick = doSearch;
elSearchKeyword.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); doSearch(); }
});
elSearchSender.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); doSearch(); }
});
elSearchClose.onclick = closeSearch;
elOverlay.addEventListener("click", (e) => { if (e.target === elOverlay) closeSearch(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !elOverlay.hidden) closeSearch();
});

init();
