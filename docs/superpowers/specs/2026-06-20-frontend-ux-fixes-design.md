# 前端 UX 三处修复设计

日期：2026-06-20
状态：已确认，待编写实现计划

## 背景

消息查看器前端（`web/app.js` / `web/index.html` / `web/style.css`）存在三处影响体验的不一致与小缺陷，本次一并修复。均为纯前端改动，不涉及后端 `server.py` 与数据库。

## 问题 1：点日期后下拉加载不稳定

### 现象

点击左栏日期加载某天消息后，有时会自动触发一次下拉加载（`/api/messages?...&after_ts=...`），有时不会。期望是**每次**点日期都触发，这样用户加载完当天最新一屏后可直接下滑查看跨天的新消息。

### 根因

`loadByDate()` 在 `renderMessages(null)` 后执行 `elMsgList.scrollTop = elMsgList.scrollHeight` 滚到底部，随后依赖底部 `IntersectionObserver` 的异步回调去触发 `loadNewer()`。但 `renderMessages` 会先把底部哨兵 `elSentinelBottom` 从 DOM 移除再重新挂载，新挂载的哨兵是否被判为"已相交"取决于浏览器在该帧的时序判断，导致行为不确定：有时回调在滚动落定前触发（哨兵仍在视口内 → 触发），有时在落定后触发（哨兵已被推出视口 → 不触发）。

`query_by_date` 后端会正确返回 `has_more_newer`（当存在晚于当日最新一屏的消息时为 true），因此点日期后做一次下拉加载是合理且必要的。

### 方案

在 `loadByDate` 滚到底之后，**显式调用** `loadNewer()`，不再依赖观察者异步触发：

```js
renderMessages(null);
elMsgList.scrollTop = elMsgList.scrollHeight;
elStatus.textContent = "";
// 滚到底后，若有更新内容则立即显式加载下一页，便于用户直接下滑查看
if (state.hasMoreNewer) loadNewer();
```

`loadNewer()` 内部已有 `state.loadingNewer` 与 `state.hasMoreNewer` 守卫，`after_ts` 游标由 `by_date` 返回的 `newest.ts` 设定，可安全调用。`loadNewer` 渲染后保持 `scrollTop` 不变（不跳到底），新内容追加在底部，用户下滑即可见——与期望一致。

`setupSentinels` 的底部观察者**保留不动**，继续负责用户手动下滑到接近底部时的预加载。两条路径通过 `state.loadingNewer` 守卫互斥，不会重复请求。

### 防重复与级联加载说明

**不会重复加载同一批数据**，由两层机制保证：

1. **同步守卫互斥**：`loadNewer()` 入口检查 `state.loadingNewer` 并立即置 `true`，二者之间无 `await`。无论点日期后的显式调用与观察者异步回调谁先谁后执行，只有一个调用能进入函数体，其余在首行 return。时序如下：
   - `loadByDate` 滚到底 → 底部哨兵进入视口，观察者把回调排入异步任务队列（尚未执行）。
   - `loadByDate` 紧接着显式调用 `loadNewer()` → 守卫通过，置 `loadingNewer = true`，`await` 请求让出执行权。
   - 观察者回调此时执行 → 调 `loadNewer()` → 撞 `loadingNewer === true` → return，不发请求。
   - 显式加载完成，`finally` 置 `loadingNewer = false`。
2. **游标前进**：每次 `loadNewer()` 成功后 `state.after = data.newest`。即使两次调用都进入函数体（实际不会），所用 `after_ts` 不同，取的是不同页，不会拿到重复数据。

**级联预加载（非重复，保留现状）**：显式那次加载完成后，若新追加内容很少、底部哨兵仍在观察者 rootMargin 触发区内，观察者会再触发一次加载——但加载的是下一页（游标已前进），受 `hasMoreNewer` 兜底。这与现有手动下滑时的"提前预加载一屏"语义一致，能让点日期后底部尽快填满一屏缓冲，符合"直接下滑看新内容"的初衷，故保留，不做额外抑制。

### 影响范围

仅 `web/app.js` 的 `loadByDate` 函数末尾增加 2 行。

## 问题 2：媒体消息 `[链接]` 字号不一致

### 现象

媒体类消息中的 `[链接]` 文字，有的显示为小字（12px），有的显示为大字（16px），视觉不统一。

### 根因

`renderMessageBody` 中 `link` 变量定义为：

```js
const link = url ? ` <a href="${escapeHtml(url)}" target="_blank" rel="noopener">[链接]</a>` : "";
```

此 `link` 用于图片（mt 1）、文件（mt 5）、视频（mt 10）、红包（mt 13）等分支的 fallback 文本，渲染为裸 `<a>[链接]</a>`，继承 `.msg-body` 的字号——`.msg-body` 无显式 `font-size`，继承 body 默认 16px。

而 mt 14 / mt 15 / 默认分支用的是 `<span class="tag">[链接]</span>`，`.tag` 显式 `font-size: 12px`。

同一个 `[链接]` 标签两种字号。

### 方案

把 `link` 统一用 `.tag` 包裹，使所有 `[链接]` 走 12px：

```js
const link = url
  ? ` <span class="tag"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">[链接]</a></span>`
  : "";
```

各分支末尾的 `${link}` 渲染出来均为 12px 的 `[链接]`，与 mt 14/15 分支一致。

为防止 `<a>` 默认样式覆盖 `.tag` 字号，在 `web/style.css` 补一条防护：

```css
.tag a { font-size: inherit; color: inherit; }
```

（`.msg-body a` 已有 `color: #1a73e8`，`.tag` 也有 `color: #1a73e8`，颜色本就一致；`font-size: inherit` 确保链接不回退到默认字号。）

### 影响范围

- `web/app.js` 的 `renderMessageBody`：`link` 变量一行改写。
- `web/style.css`：新增 `.tag a` 一条规则。

## 问题 3：搜索输入框清除按钮不一致

### 现象

高级搜索浮层中，关键词输入框有浏览器原生的 ✕ 清除按钮，发送者名称输入框没有。

### 根因

`web/index.html` 中 `search-keyword` 为 `type="search"`（浏览器原生提供 ✕），`search-sender` 为 `type="text"`（无 ✕）。

### 方案

把 `search-sender` 改为 `type="search"`：

```html
<input id="search-sender" type="search" placeholder="发送者名称（精确匹配，可选）" autocomplete="off">
```

两个框都将获得浏览器原生 ✕，行为一致。

`type="search"` 在部分浏览器会给输入框本体加原生装饰（内边距、圆角）。现有 `.search-fields input` 已统一覆盖 padding/border/border-radius，但为彻底消除原生外观差异，补一条：

```css
.search-fields input { -webkit-appearance: none; appearance: none; }
```

`appearance: none` 只移除输入框本体的原生装饰，不移除原生 ✕ 清除按钮（✕ 由 `::-webkit-search-cancel-button` 控制，不受 `appearance` 影响）。

### 影响范围

- `web/index.html`：`search-sender` 的 `type` 属性一处改动。
- `web/style.css`：`.search-fields input` 规则内补 `appearance` 属性。

## 测试策略

本项目前端无自动化测试框架，验证以手动浏览器验证为主：

1. **问题 1**：选择一个有跨天后续消息的群与日期，点击该日期，确认网络面板立即出现一次 `after_ts` 请求，且消息列表底部追加新内容、滚动位置不跳动；再选一个当日已是最新（无后续消息）的日期，确认不发起多余请求（`has_more_newer=false` 时不调用）。
2. **问题 2**：在含不同 media_type 消息的群中，确认所有 `[链接]` 文字字号一致（12px），与 `[小程序]` 等标签视觉统一。
3. **问题 3**：打开高级搜索，在两个输入框分别输入文字，确认都出现原生 ✕ 且点击可清空；两框外观（圆角、内边距）一致。

现有后端测试（`tests/`）不受影响，回归确认 `pytest` 仍全绿。

## 非目标

- 不调整 `IntersectionObserver` 的 rootMargin 或触发逻辑（仅问题 1 改为显式调用，观察者保持原样）。
- 不重构 `renderMessageBody` 的分支结构，仅统一 `link` 变量写法。
- 不引入自定义清除按钮组件，沿用浏览器原生 ✕。
