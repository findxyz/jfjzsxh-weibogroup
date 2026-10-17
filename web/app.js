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
  memberName: "",       // 成员过滤：空 = 全部
};

const LIMIT = 100;

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const elGroup = $("group-select");
const elMember = $("member-select");
const elSearchBtn = $("search-btn");
const elSyncBtn = $("sync-btn");
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

// 返回成员过滤的 query string 片段（含前导 &），无过滤时返回 ""
function memberFilterQ() {
  return state.memberName ? `&sender_name=${encodeURIComponent(state.memberName)}` : "";
}

async function loadMembers(gid) {
  try {
    const members = await api(`/api/members?gid=${gid}`);
    elMember.innerHTML = '<option value="">全部成员</option>'
      + members.map(m => `<option value="${escapeHtml(m.sender_name)}">${escapeHtml(m.sender_name)} (${m.msg_count})</option>`).join("");
    // 保持当前选中（若仍在该群）
    if (state.memberName) elMember.value = state.memberName;
  } catch (e) {
    elMember.innerHTML = '<option value="">全部成员</option>';
  }
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
  const link = url
    ? ` <span class="tag"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">[链接]</a></span>`
    : "";
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
  let params = `gid=${gid}&date=${encodeURIComponent(date)}&limit=${LIMIT}${memberFilterQ()}`;
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
  // 滚到底后，若有更新内容则立即显式加载下一页，便于用户直接下滑查看。
  // 不依赖底部 IntersectionObserver 异步触发——renderMessages 会移除并重新
  // 挂载底部哨兵，观察者回调时机不稳定。loadNewer 内部有 loadingNewer/
  // hasMoreNewer 同步守卫，与观察者触发的调用互斥，不会重复请求。
  if (state.hasMoreNewer) loadNewer();
}

// ---------- 选择操作 ----------
async function selectGroup(gid) {
  state.gid = gid;
  elStatus.textContent = "加载中…";
  await loadDates(gid);
  loadMembers(gid);  // 异步加载，不阻塞
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
  let sender = elSearchSender.value.trim();
  const keyword = elSearchKeyword.value.trim();
  // 若成员过滤器已选中但搜索面板发送者为空，自动带入
  if (!sender && state.memberName) sender = state.memberName;
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
  let params = `gid=${state.gid}&before_ts=${state.before.ts}&limit=${LIMIT}${memberFilterQ()}`;
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
  let params = `gid=${state.gid}&after_ts=${state.after.ts}&limit=${LIMIT}${memberFilterQ()}`;
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

elMember.onchange = () => {
  state.memberName = elMember.value;
  if (state.selectedDate) loadByDate(state.gid, state.selectedDate);
};

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

// ---------- 同步 ----------
let syncPollTimer = null;
// 同步 / 续期 进行中标志：二者互斥，避免并发触发长任务
let syncInProgress = false;
let renewInProgress = false;

async function runSync() {
  if (syncInProgress || renewInProgress) return;
  syncInProgress = true;
  elSyncBtn.disabled = true;
  elSyncBtn.textContent = "同步中...";
  try {
    const resp = await fetch("/api/sync", { method: "POST" });
    // 202 = 已启动；409 = 已有同步在跑，直接转入轮询
    if (resp.status !== 202 && resp.status !== 409) {
      throw new Error("同步启动失败");
    }
  } catch (e) {
    syncInProgress = false;
    elSyncBtn.disabled = false;
    elSyncBtn.textContent = "🔄 同步";
    elStatus.textContent = "同步启动失败";
    return;
  }
  syncPollTimer = setInterval(pollSync, 2000);
  pollSync();
}

elSyncBtn.addEventListener("click", () => runSync());

async function pollSync() {
  try {
    const data = await (await fetch("/api/sync/status")).json();
    if (!data.running) {
      clearInterval(syncPollTimer);
      syncPollTimer = null;
      syncInProgress = false;
      elSyncBtn.disabled = false;
      elSyncBtn.textContent = "🔄 同步";
      if (data.exit_code === 0) {
        // 同步成功：确保续期弹窗已关闭
        if (!elRenewModal.hidden) closeRenewModal();
        await silentRefresh();  // 静默局部更新，不再 location.reload()
      } else {
        // 同步失败：解析日志，渲染横幅 + 引导
        showSyncError(data.output || "", data.exit_code);
      }
    }
  } catch (e) {
    // 网络错误，继续轮询
  }
}

// ---------- 同步失败提示 + Cookie 一键续期弹窗 ----------
// Cookie 过期时直接弹出简洁模态，只提供「一键续期」；其余失败类型忽略（保持状态栏文字）。
function classifySyncError(output) {
  const text = (output || "").toLowerCase();
  if (text.includes("cookie 已过期") || text.includes("cookieexpirederror") || text.includes("cookie 未设置")) {
    return "cookie";
  }
  return "other";
}

// 续期弹窗 DOM
const elRenewModal = $("renew-modal");
const elRenewModalClose = $("renew-modal-close");
const elRenewIntro = $("renew-intro");
const elRenewScan = $("renew-scan");
const elRenewStart = $("renew-start-btn");
const elRenewStop = $("renew-stop-btn");
const elRenewStatus = $("renew-status");
const elRenewQrImg = $("renew-qr-img");

function showRenewModal() {
  elRenewModal.hidden = false;
  // 回到初始态
  elRenewIntro.hidden = false;
  elRenewScan.hidden = true;
  elRenewQrImg.removeAttribute("src");
}

function closeRenewModal() {
  elRenewModal.hidden = true;
  // 若续期仍在跑，先停止，避免残留进程
  if (renewInProgress) stopRenew();
}

elRenewModalClose.addEventListener("click", () => closeRenewModal());
// 点背景不关闭（避免误关丢失扫码进度）；仅右上角 × 可关

function showSyncError(output, exitCode) {
  // 仅 cookie 类弹出续期模态；其他失败保持原有状态栏小字提示
  if (classifySyncError(output) === "cookie") {
    showRenewModal();
  }
}

// ---------- Cookie 一键续期（长任务 + 轮询） ----------
let renewPollTimer = null;

async function startRenew() {
  if (renewInProgress) return;
  if (syncInProgress) {
    alert("同步任务正在进行，请等待同步完成后再续期。");
    return;
  }
  renewInProgress = true;
  elSyncBtn.disabled = true;  // 续期期间禁用同步
  // 切到扫码态：放大二维码 + 状态
  elRenewIntro.hidden = true;
  elRenewScan.hidden = false;
  elRenewStop.hidden = false;
  setRenewStatus("正在生成登录二维码…", "info");
  try {
    const resp = await fetch("/api/renew", { method: "POST" });
    if (resp.status === 409) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || "续期已在运行");
    }
    if (!resp.ok) throw new Error("续期启动失败");
  } catch (e) {
    renewInProgress = false;
    elSyncBtn.disabled = false;
    elRenewStop.hidden = true;
    elRenewIntro.hidden = false;
    elRenewScan.hidden = true;
    setRenewStatus("续期启动失败：" + e.message, "err");
    return;
  }
  renewPollTimer = setInterval(pollRenew, 2000);
  pollRenew();
}

async function pollRenew() {
  try {
    const data = await (await fetch("/api/renew/status")).json();
    // 二维码：仅在 url 存在时更新（后端已带 mtime 防缓存）
    if (data.qrcode_url) {
      elRenewQrImg.src = data.qrcode_url;
      elRenewQrImg.alt = "请使用微博 APP 扫码";
    }
    const stage = data.stage;
    if (stage === "waiting_scan") {
      setRenewStatus("请用微博 APP 扫码登录（扫一扫 → 确认登录）。", "info");
    } else if (stage === "scanning" || stage === "done") {
      // 扫码成功后立刻收起二维码，显示「正在处理」
      elRenewQrImg.removeAttribute("src");
      elRenewScan.querySelector(".rm-qr-wrap").style.display = "none";
      if (stage === "scanning") {
        setRenewStatus("已扫码，正在保存登录状态…", "info");
      }
    }
    if (stage === "done") {
      clearInterval(renewPollTimer);
      renewPollTimer = null;
      renewInProgress = false;
      elSyncBtn.disabled = false;
      setRenewStatus("✅ 续期成功！", "ok");
      // 静默刷新群列表，然后关闭整个弹窗
      await silentRefresh().catch(() => {});
      setTimeout(() => closeRenewModal(), 800);
    } else if (stage === "failed" || (data.running === false && data.exit_code !== 0)) {
      clearInterval(renewPollTimer);
      renewPollTimer = null;
      renewInProgress = false;
      elSyncBtn.disabled = false;
      elRenewStop.hidden = true;
      elRenewScan.querySelector(".rm-qr-wrap").style.display = "";
      const log = data.output || "";
      if (log.includes("playwright") || log.includes("未安装") || log.includes("先解决以上问题")) {
        setRenewStatus("续期失败：Playwright 未安装。请在服务器执行 uv run playwright install chromium 后重试。", "err");
      } else if (log.includes("超时") || log.includes("timeout")) {
        setRenewStatus("续期失败：120 秒内未扫码。请重试并尽快扫码。", "err");
      } else {
        setRenewStatus("续期失败（exit " + data.exit_code + "）。可关闭后重新点击一键续期。", "err");
      }
      // 失败：回到初始态，允许重试
      elRenewIntro.hidden = false;
      elRenewScan.hidden = true;
    }
  } catch (e) {
    // 网络错误，继续轮询
  }
}

async function stopRenew() {
  try {
    await fetch("/api/renew/stop", { method: "POST" });
  } catch (e) {}
  if (renewPollTimer) { clearInterval(renewPollTimer); renewPollTimer = null; }
  renewInProgress = false;
  elSyncBtn.disabled = false;
  elRenewStop.hidden = true;
  elRenewScan.querySelector(".rm-qr-wrap").style.display = "";
  setRenewStatus("已取消续期。", "info");
  // 回到初始态
  elRenewIntro.hidden = false;
  elRenewScan.hidden = true;
}

function setRenewStatus(msg, cls) {
  elRenewStatus.textContent = msg;
  elRenewStatus.className = "rm-renew-status" + (cls ? " " + cls : "");
}

elRenewStart.addEventListener("click", () => startRenew());
elRenewStop.addEventListener("click", () => stopRenew());

// 静默刷新：同步成功后局部更新当前视图，不打断阅读。
// 刷新群下拉计数 + 日期列表计数（保留展开态）+ 追加新消息到底部（不滚动）。
async function silentRefresh() {
  // 搜索面板打开时只更新群计数，不打断搜索
  const searchOpen = !elOverlay.hidden;

  // 1) 群下拉：更新计数，保留当前选中
  try {
    const groups = await api("/api/groups");
    const cur = elGroup.value;
    state.groups = groups;
    elGroup.innerHTML = groups.map(g =>
      `<option value="${g.gid}">${escapeHtml(g.name)} (${g.msg_count})</option>`).join("");
    if (cur) elGroup.value = cur;
  } catch (e) { /* 忽略，继续 */ }

  if (!state.gid) return;

  // 2) 日期列表：局部更新月计数（保留展开态），展开的月份重取每日计数
  try {
    const monthsData = await api(`/api/dates?gid=${state.gid}`);
    const byMonth = new Map(monthsData.map(m => [m.month, m.count]));
    // 更新已存在月份的计数，收集需重取每日的展开月份
    const toReloadDays = [];
    for (const mg of state.dates) {
      if (byMonth.has(mg.month)) {
        mg.count = byMonth.get(mg.month);
        byMonth.delete(mg.month);
        if (mg.open && mg.days) toReloadDays.push(mg);
      }
    }
    // 新增月份（同步后可能出现新月份），插入到对应位置（倒序）
    for (const [month, count] of byMonth) {
      const mg = { month, count, days: null, open: false };
      let idx = state.dates.findIndex(d => d.month < month);
      if (idx === -1) state.dates.push(mg);
      else state.dates.splice(idx, 0, mg);
    }
    // 重取已展开月份的每日列表（计数可能已变）
    await Promise.all(toReloadDays.map(async mg => {
      try {
        mg.days = await api(`/api/dates?gid=${state.gid}&month=${encodeURIComponent(mg.month)}`);
      } catch (e) { /* 保留旧数据 */ }
    }));
    renderDateList();
  } catch (e) { /* 忽略 */ }

  // 3) 消息：搜索打开时不追加；否则若处于最新位置则静默拉取新消息
  if (searchOpen || !state.selectedDate) return;
  // hasMoreNewer 为 true 说明用户在看较早的数据，前面还有未翻页的新消息，
  // 同步新增的消息在更后面，不打断当前阅读，仅更新了日期列表计数
  if (state.hasMoreNewer) return;
  if (state.loadingNewer || !state.after) return;
  const myReq = state.reqId;
  try {
    const data = await api(`/api/messages?gid=${state.gid}&after_ts=${state.after.ts}&limit=${LIMIT}${memberFilterQ()}`);
    if (myReq !== state.reqId) return;  // 用户已切走，丢弃
    if (!data.messages.length) {
      state.hasMoreNewer = false;
      return;
    }
    // 追加到底部，保持当前滚动位置（用户可自然下滑查看新内容）
    const prevScrollTop = elMsgList.scrollTop;
    state.messages = state.messages.concat(data.messages);
    state.after = data.newest;
    state.hasMoreNewer = data.has_more_newer;
    renderMessages(null);
    elMsgList.scrollTop = prevScrollTop;
  } catch (e) { /* 静默失败 */ }
}

// ---------- 自动同步（每 60 秒，默认不开启）----------
const autoRefreshCheck = $("auto-refresh-check");
const autoRefreshCountdown = $("auto-refresh-countdown");
let autoRefreshTimer = null;
let autoRefreshSeconds = 0;  // 距下次同步剩余秒数
const AUTO_REFRESH_INTERVAL = 60;

function updateCountdownDisplay() {
  if (autoRefreshCheck.checked) {
    // 实心圆顺时针排空：透明区从顶部 0° 顺时针扫过吃掉绿色
    // 60s 全绿，时间流逝透明弧顺时针增长至 0s 全空
    const elapsed = AUTO_REFRESH_INTERVAL - autoRefreshSeconds;
    const boundary = 360 * (elapsed / AUTO_REFRESH_INTERVAL);
    autoRefreshCountdown.style.background =
      `conic-gradient(from 0deg, transparent 0deg ${boundary}deg, #4caf50 ${boundary}deg 360deg)`;
    autoRefreshCountdown.title = `距下次同步 ${autoRefreshSeconds}s`;
    autoRefreshCountdown.hidden = false;
  } else {
    autoRefreshCountdown.hidden = true;
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer) return;
  autoRefreshSeconds = AUTO_REFRESH_INTERVAL;
  updateCountdownDisplay();
  autoRefreshTimer = setInterval(autoRefreshTick, 1000);
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
  updateCountdownDisplay();
}

async function autoRefreshTick() {
  autoRefreshSeconds--;
  if (autoRefreshSeconds <= 0) {
    // 倒计时归零：触发同步，重置计时
    autoRefreshSeconds = AUTO_REFRESH_INTERVAL;
    updateCountdownDisplay();
    // 已有同步在跑则跳过，避免并发
    try {
      const st = await (await fetch("/api/sync/status")).json();
      if (st.running) return;
    } catch (e) {
      return;  // 状态查询失败，跳过本次
    }
    runSync();
  } else {
    updateCountdownDisplay();
  }
}

autoRefreshCheck.addEventListener("change", () => {
  if (autoRefreshCheck.checked) {
    // 开启：立即同步一次，然后启动定时器
    runSync();
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});

init();
// 默认不开启自动同步，由用户勾选触发
