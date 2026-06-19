# 微博群聊消息查看器 — 前端页面设计

> 为 `weibo_im.db` 做一个本地只读查看前端：按日期跳转浏览群聊消息，
> 支持发送者筛选与关键词模糊搜索（搜索结果可跳转到上下文翻看）。
>
> 零新依赖，仅用 Python 标准库 + 原生 HTML/JS/CSS。

---

## 1. 背景与数据约束

### 1.1 项目现状

- 纯 Python CLI 爬虫（`crawl.py` + `weibo_im/` 包），依赖仅 requests/urllib3/playwright。
- 数据全在本地 SQLite `weibo_im.db`，无任何前端代码。
- 现有 `weibo_im/db.py` 为爬虫设计（thread-local 连接、`get_latest_mid` 等），无按时间游标分页 / 按天聚合能力，**不复用**。

### 1.2 实测数据特征（决定设计的关键事实）

| 事实 | 影响 |
|------|------|
| `messages` 共 847,715 条，3 个群，但消息全部集中在「茧房建筑师协会」(gid=4761715839862414) | API 仍带 `gid` 支持多群，但默认选中消息最多的群 |
| `id`（自增主键）与 `created_at` **不单调一致**：回填时新旧混杂入库，id=1 比 id=2 更新 | **不能用 id 做时间游标**，只能用 `created_at` + `id` tiebreaker |
| `created_at` 是 UTC 毫秒戳；同毫秒最多 14 条重复 | 游标需 `(created_at, id)` 复合，严格 `<`/`>` 分页保证不漏不重 |
| 共 720 天有消息，单日平均 ~1177 条，单日最多 3295 条 | 左栏 720 天需按月折叠；单日 3000 条一次渲染偏重，**当天内仍分段加载** |
| `media_files` 5.7 万条全 `pending`，`messages.media_local_path` 全空，仅有需带 cookie 的 `media_orig_url` | 媒体不加载，只占位 + 原始链接 |
| `created_at` UTC ms，业务统一 CST(+08:00) | 日期聚合用 `date(datetime(created_at/1000,'unixepoch','+8 hours'))` |

### 1.3 时区约定

沿用项目既有口径：所有时间按 CST 处理，`datetime(..., tz=CST)` 显式锚定，不依赖系统时区。

---

## 2. 架构与文件布局

### 2.1 新增文件（不改现有代码）

```
weibogroup/
├── server.py              # 新增：HTTP 服务 + JSON API（标准库 http.server + sqlite3）
├── web/                   # 新增：静态前端资源
│   ├── index.html         # 单页结构
│   ├── app.js             # 全部交互逻辑（原生 JS，无框架）
│   └── style.css          # 样式
├── tests/                 # 新增：后端 API 单测
│   └── test_server.py
└── weibo_im/              # 不动
```

### 2.2 server.py 职责

`http.server.ThreadingHTTPServer` + 自定义 `RequestHandler`。

**静态资源：**
- `/` → `web/index.html`
- `/web/<file>` → `web/` 下文件（带 MIME 类型）

**JSON API（全部只读，`sqlite3` 以 `mode=ro` 只读打开 `weibo_im.db`，绝不误写）：**

| 端点 | 作用 |
|------|------|
| `GET /api/groups` | 群列表（gid/name），按消息数倒序 |
| `GET /api/dates?gid=` | 每天消息数 `[{date, count}]`，按日期倒序 |
| `GET /api/messages?gid=&before_ts=&before_id=&after_ts=&after_id=&limit=500` | 游标分页（见 §3）|
| `GET /api/messages/by_date?gid=&date=&sender_id=&limit=500` | 选定日期初始锚点（该日最新 limit 条）|
| `GET /api/messages/around?gid=&mid=&limit=500` | 以某消息为锚取前后（搜索跳转用）|
| `GET /api/search?gid=&q=&days=&limit=200` | `LIKE` 模糊搜索（≤3 个月范围）|
| `GET /api/senders?gid=` | 该群发送者列表（供筛选下拉，按发言数倒序）|

### 2.3 启动方式

`python server.py` 默认 `127.0.0.1:8765`，可 `--port`/`--host`/`--db` 覆盖。启动后打印访问地址。与 `crawl.py` 并列，职责清晰，符合项目"单一 CLI 入口"风格。

---

## 3. 游标分页 SQL

### 3.1 排序约定

右栏"最新在底"，DOM 顺序 = 时间升序，固定排序：

```sql
ORDER BY created_at ASC, id ASC
```

### 3.2 通用查询骨架（带可选筛选）

```sql
-- 参数: gid, [sender_id], [before 游标 或 after 游标], limit
SELECT id, mid, msg_type, msg_type_name, media_type,
       sender_id, sender_name, text, fid, media_orig_url,
       url_objects, pic_infos, template, template_data, recall_by,
       created_at, group_name
FROM messages
WHERE gid = :gid
  AND (:sender_id IS NULL OR sender_id = :sender_id)
  {AND 游标条件}
ORDER BY created_at ASC, id ASC
LIMIT :limit
```

### 3.3 双向游标

**向上加载更早**（before 游标 = 当前视图最旧消息 `(ts, id)`）：

```sql
AND (created_at < :before_ts
     OR (created_at = :before_ts AND id < :before_id))
```

查到后倒序拼接到视图顶部；新 before 游标 = 本页 `created_at/id` 最小者。

**向下加载更新**（after 游标 = 当前视图最新消息 `(ts, id)`）：

```sql
AND (created_at > :after_ts
     OR (created_at = :after_ts AND id > :after_id))
```

正序追加到视图底部；新 after 游标 = 本页 `created_at/id` 最大者。

### 3.4 初始锚点查询（选中日期）

```sql
-- 取该日(CST) 最新 limit 条作为初始视图
WHERE gid=:gid
  AND date(datetime(created_at/1000,'unixepoch','+8 hours'))=:date
  AND (:sender_id IS NULL OR sender_id=:sender_id)
ORDER BY created_at DESC, id DESC
LIMIT 500
-- 服务端反转为 ASC 后返回，保证 DOM 升序
```

渲染后 before 游标 = 视图最旧消息、after 游标 = 视图最新消息，双向可用。

### 3.5 跳转查询（搜索命中消息）

```sql
-- 以命中消息为锚，取它及之前 limit 条（更早方向）
WHERE gid=:gid
  AND (created_at < :hit_ts OR (created_at=:hit_ts AND id<=:hit_id))
ORDER BY created_at DESC, id DESC
LIMIT 500
-- 反转为 ASC 渲染，命中消息用 mid 定位高亮
```

after 游标 = 视图最新消息（**不是命中消息**），向下滚覆盖视图外更新，符合直觉；before 游标 = 本页最旧，可向上翻更早。

### 3.6 边界与正确性

- 同毫秒用 `id` tiebreaker 严格分页，**不漏不重**（严格 `<`/`>`）。
- `ORDER BY created_at, id` 命中 `idx_msg_ctime` 索引，id 是主键，足够快。
- `sender_id` 过滤无索引，在结果集上过滤，配合 limit 可接受。
- 每次响应：`messages[]` + `oldest`/`newest` 游标对象 + `has_more_older`/`has_more_newer` 布尔。

---

## 4. 搜索浮层

### 4.1 触发与时间范围

顶栏搜索框输关键词回车 → 打开浮层 + 发起请求。

搜索必须在 ≤3 个月范围。采用方案 A：**默认搜最近 3 个月，可手动缩短**。浮层顶部"搜索范围"选择：`最近1周 / 最近1个月 / 最近3个月`，默认最近3个月。下界 = 该群最新消息 `created_at - N*86400000`。

### 4.2 SQL 要点

```sql
WHERE gid=:gid
  AND created_at >= :min_ts          -- 3个月下界
  AND text LIKE :q ESCAPE '\'        -- '%关键词%'，转义 % _ \
ORDER BY created_at DESC, id DESC
LIMIT :limit                         -- 默认 200
```

- **`LIKE '%关键词%'`，不用 FTS5**（避免分词漏数据，用户明确要求）。
- `q` 需转义 LIKE 通配符 `%` `_` `\`，配 `ESCAPE '\'`，否则关键词含这些字符误匹配。
- 默认 `limit=200`，超出提示"结果较多，请缩小范围或关键词"。
- 只搜 `text` 字段（系统消息 text 也能搜到）。
- 跨群：API 带 `gid`，多群各自搜。

### 4.3 浮层 UI

```
┌─────────────────────────────────────────────┐
│ 搜索: [关键词____]  范围:[最近3个月▼]  [×]   │
├─────────────────────────────────────────────┤
│ 搜索中... / 共 47 条结果                     │
├─────────────────────────────────────────────┤
│ 肥圆真君_WWW  06-17 14:30                    │
│ ...关键词前后各~30字片段...      [跳转→]     │
├─────────────────────────────────────────────┤
│ 拱bot  06-17 14:15                           │
│ ...另一个命中片段...             [跳转→]     │
├─────────────────────────────────────────────┤
│ ...（可滚动）                                │
└─────────────────────────────────────────────┘
```

- 每条：发送者 + 时间 + 文本片段（关键词前后各 ~30 字，`<mark>` 高亮关键词）+ 跳转按钮。
- loading 态：`LIKE '%...%'` 全表扫 3 个月（约 10 万条），可能数百 ms 到 1-2 秒，显示 spinner。

### 4.4 跳转行为

点某条结果：

1. 关闭浮层。
2. 调 `/api/messages/around?gid=&mid=&limit=500`（§3.5）。
3. 右栏替换视图：渲染 500 条（命中消息在其中），自动 `scrollIntoView({block:'center'})` 居中定位命中消息，高亮闪烁 2 秒。
4. 游标重建：before = 本页最旧，after = 本页最新。
5. **左栏同步**：`selectedDate` 更新为命中消息的 CST 日期，高亮该日；若该日在折叠月份，自动展开该月并滚动到可见。

---

## 5. 消息渲染细节

### 5.1 气泡结构（普通消息）

```html
<div class="msg" data-mid="5310915936522819" data-date="2026-06-17">
  <div class="msg-meta">
    <span class="sender">Seaces</span>
    <span class="time">14:34</span>
  </div>
  <div class="msg-body"><!-- 按 media_type 渲染 --></div>
</div>
```

- 时间 `HH:MM`（CST）。跨天插入日期分隔条 `──── 06-17 ────`（基于 `data-date` 变化判断）。
- 发送者名加粗；`sender_name` 为空显示 `sender_id` 兜底。

### 5.2 按 media_type / msg_type 渲染

| 类型 | 渲染 |
|------|------|
| media_type=0 文本 | `text` 原文（保留换行、转义 HTML）|
| media_type=1 图片 | `🖼 [图片]` 占位 + `media_orig_url` 可点链接 |
| media_type=10/13 视频 | `🎬 [视频]` 占位 + 链接（media_type=13 且 text 含"红包" → `🧧 [红包]`）|
| media_type=5 文件 | `📎 [文件]` + 链接 |
| media_type=14 链接卡片 | `text`（通常 t.cn 短链）+ `[链接]` 标签；`url_objects` JSON 有标题则优先显示标题 |
| media_type=15 小程序 | `text`（如 `[动画表情]`）+ `[小程序]` 标签；`pic_infos` 有缩略图 URL 则显示 `[小程序缩略图]` 文字链接（不直接加载图）|
| media_type=4/9/11/16 等未知 | `text` + `[未知媒体类型:N]` 标签兜底 |
| 系统消息（见下） | **居中灰色小字**，无气泡边框、无发送者行 |

### 5.3 系统消息判定

`msg_type != 321 AND msg_type != 100`（非普通消息和微博分享）即按系统消息居中渲染。稳妥兜底，避免漏判新类型码。

| msg_type | 渲染 |
|----------|------|
| 322 新人入群 | 居中灰色，`text`（如"X 加入了群"）|
| 323/324 退群/被踢 | 居中灰色，`text` |
| 325/327 改名/转让 | 居中灰色，`text` |
| 331 撤回 | 居中灰色，`"{recall_by} 撤回了一条消息"`（text 已是此格式则直接用）|
| 337 管理员变更 | 居中灰色，`text` |
| 344（实测撤回类）| 居中灰色，`text`（如"X 撤回了一条消息"）|
| 499 通知 / 其他 | 居中灰色，`text` |

### 5.4 文本安全与链接化

- 所有数据库文本进 DOM 前先 HTML 转义（`& < > " '`），再叠加 URL 链接化，防 XSS。
- `text` 中 `http://` / `https://` URL 正则转为可点链接。

### 5.5 高亮

搜索跳转后命中消息加 `class="msg-highlight"`，CSS 动画闪烁 2 秒后移除，`scrollIntoView({block:'center'})` 居中。

---

## 6. 页面布局与交互

### 6.1 三栏布局

```
┌─────────────────────────────────────────────────────────────┐
│  顶栏：群选择下拉  |  搜索框  |  发送者筛选下拉  |  状态      │
├──────────────┬──────────────────────────────────────────────┤
│  左栏：日期  │  右栏：聊天视图（最新在底，向上是更早）        │
│  列表        │                                              │
│  按月折叠    │  ↑ 向上滚 → 加载更早（500 条/页）             │
│  带每天条数  │  ↓ 向下滚 → 加载更新                          │
│  最新在上    │  选中日期：该日最新 500 条为初始视图           │
│  可滚动      │                                              │
│  ~240px 宽   │  当前可见范围指示：06-17 14:30 → 06-17 15:20  │
└──────────────┴──────────────────────────────────────────────┘
```

### 6.2 左栏 — 日期列表

- 每项：`日期 + 当天消息数`，如 `06-17 (873)`。
- 720 天按月折叠：默认展开最近一个月，其余折叠为 `2026-05 (12340)` 摘要行（点开才展开该月每日）。月份倒序。
- 顶部固定日期跳转输入框（`<input type="date">`），输日期直接定位。
- 选中日期高亮，点击后右栏跳到该日最新 500 条。

### 6.3 左栏高亮规则（贯穿所有右栏视图变化）

左栏高亮 = "当前视图的基准锚点日期"。**仅显式选日期或搜索跳转时更新**；纯上下滚动加载不主动改高亮（跨日边界会闪烁打断浏览），改由右栏顶部"可见范围"指示当前位置。

### 6.4 右栏 — 聊天视图

- 消息气泡，最新在底，向上是更早（聊天软件惯例）。
- 分页：初始 500 条。向上滚到顶 → 加载更早（before 游标）；向下滚到底 → 加载更新（after 游标）。无更多时显示"没有更早/更新的消息了"。
- 加载方向默认向上（选中日期后初始是"该日最新 500 条"，看历史概率更高）。

### 6.5 顶栏 — 筛选与搜索

- 群选择：默认选中消息最多的群。切换群重置左右栏。
- 发送者筛选：下拉列出该群发送者（按发言数倒序）。选中后**重新查询数据库**（不是本地过滤），右栏按当前 `selectedDate` 重新 `by_date`（带 `sender_id`），走 §3 游标分页。清空恢复全部。
- 搜索框：输关键词回车 → 弹出搜索浮层（§4）。

---

## 7. 前端状态机、错误处理与测试

### 7.1 核心状态

```js
state = {
  gid: 4761715839862414,       // 当前群
  groups: [],                  // 顶栏下拉
  dates: [],                   // 左栏 [{month:'2026-06', days:[{date,count}...]}]
  selectedDate: '2026-06-17',  // 左栏高亮 + 右栏基准锚点
  selectedSender: null,        // 发送者筛选，null=全部
  messages: [],                // 当前渲染消息（升序，最新在底）
  before: null,                // {ts, id} 视图最旧游标
  after: null,                 // {ts, id} 视图最新游标
  hasMoreOlder: true,
  hasMoreNewer: true,
  loadingOlder: false,
  loadingNewer: false,
}
```

### 7.2 操作 → 状态转换

| 操作 | 触发 | 状态变化 |
|------|------|---------|
| 切群 | 顶栏下拉变 | 重置全部 state，重拉 dates + 右栏（该群最新 500 条）|
| 选日期 | 左栏点击 / 日期输入框 | `selectedDate` 更新，右栏调 `by_date` 重置 messages + 游标 |
| 发送者筛选 | 下拉变 | `selectedSender` 更新，右栏按当前 `selectedDate` 重新 `by_date`（带 sender）|
| 向上滚到顶 | IntersectionObserver 触顶 | `loadingOlder=true` → before 查询 → 拼到 messages 头部 → 更新 before 游标 |
| 向下滚到底 | IntersectionObserver 触底 | 同上，after 方向 |
| 搜索 | 回车 | 打开浮层，不影响右栏 state |
| 搜索跳转 | 点结果 | 关浮层，右栏调 `around` 重置 messages + 游标，`selectedDate` 更新为命中日，高亮命中 mid |

### 7.3 加载竞态防护

每次请求带自增 `reqId`，响应回来时若 `reqId` 不匹配当前 state 则丢弃。防止"快速切日期导致旧响应覆盖新视图"。

### 7.4 错误处理

- API 5xx/网络错：右栏底部/浮层显示"加载失败，点击重试"按钮，不破坏现有视图。
- 搜索结果为空：浮层显示"未找到匹配消息"。
- 空数据库/无该群消息：右栏显示空态提示。

### 7.5 性能

- 500 条/页 DOM 可控。视图消息数无硬上限（持续滚会累积），但单次增量 500，常规浏览不超几千条。
- **不做虚拟滚动**（复杂度高、收益小），用 `content-visibility: auto` 让屏外消息轻量渲染。
- 左栏 720 天按月折叠，只展开月份渲染每日项，折叠月份仅摘要行，DOM 数量可控。

### 7.6 测试策略

- **后端 `server.py`**：Python 标准库 `unittest`，对每个 API 写查询正确性测试。造临时小 SQLite，插已知消息，验证：
  - 游标分页边界、同毫秒 tiebreaker（不漏不重）
  - LIKE 通配符转义
  - 日期聚合 CST 正确性
  - 发送者筛选
  - `python -m unittest discover tests`
- **前端**：无框架无构建，不做自动化单测。靠手动走查清单验证（选日期 / 上下翻 / 筛选 / 搜索跳转 / 跨群）。

---

## 8. 范围与非目标

**本次实现范围：**
- 只读查看：日期跳转、双向分页浏览、发送者筛选、关键词模糊搜索 + 跳转上下文。
- 媒体仅占位 + 原始链接。
- 单机本地运行，`127.0.0.1`。

**明确不做（YAGNI）：**
- 不做媒体下载 / 代理加载 / 缩略图显示。
- 不做消息编辑、删除、导出。
- 不做实时推送（数据是爬取快照）。
- 不做多用户/鉴权。
- 不引入前端框架、构建工具或新 Python 依赖。
- 不做虚拟滚动。
- 不用 FTS5（LIKE 即可，避免漏数据）。
