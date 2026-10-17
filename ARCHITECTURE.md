# 微博群聊爬虫架构设计文档

> 本文档描述 `D:\weibogroup` 项目的架构设计、模块职责、数据流、状态机和跨语言迁移要点。**与实现语言无关**——目的是让任何人读完之后能用 Java/Go/Rust 重新实现一遍。
>
> 接口层细节见配套文档 [`API.md`](./API.md)。本文档关注「怎么组织代码」「数据怎么流」「核心算法怎么设计」。

---

## 目录

- [1. 设计目标与原则](#1-设计目标与原则)
- [2. 总体架构](#2-总体架构)
- [3. 模块职责](#3-模块职责)
- [4. 核心数据流](#4-核心数据流)
- [5. 关键算法](#5-关键算法)
- [6. 状态机](#6-状态机)
- [7. 数据模型](#7-数据模型)
- [8. 并发与一致性](#8-并发与一致性)
- [9. 可观测性与运维](#9-可观测性与运维)
- [10. 跨语言迁移指南](#10-跨语言迁移指南)
- [11. 已知局限与演进方向](#11-已知局限与演进方向)

---

## 1. 设计目标与原则

### 1.1 目标

1. **数据完整性**：不漏消息（按 mid 去重，全量覆盖）。
2. **幂等性**：任何命令重复跑都不产生脏数据、不重复下载。
3. **断点续传**：进程挂了重启能从上次的位置继续。
4. **抗风控**：节奏抖动 + 重试退避，规避微博简单频控。
5. **零外部服务依赖**：不依赖任何消息队列 / 通知服务（Hermes 等），单机即可跑。
6. **可移植**：核心是 HTTP + JSON + DB，方便用其他语言重写。

### 1.2 设计原则

| 原则 | 体现 |
|------|------|
| **逻辑/入口分离** | `crawl.py` 只做参数解析；业务全在 `weibo_im/` 包 |
| **解析与 IO 分离** | `parser.py` 是纯函数，不碰网络/数据库 |
| **去重靠数据库约束** | `UNIQUE(mid)` + `INSERT IGNORE` 兜底 |
| **节奏抖动** | 所有 sleep 都带随机偏移，避免等差数列式请求 |
| **原始数据留存** | `raw_json` 字段永久保留，便于重新解析 |
| **Cookie 持久化在 DB** | 不靠文件、不靠环境变量，单一数据源 |

---

## 2. 总体架构

### 2.1 分层架构图

```
┌──────────────────────────────────────────────────────────┐
│  CLI 入口层  (crawl.py)                                  │
│  - 参数解析 / 日志初始化 / 流程编排                       │
│  - 调用 weibo_im.* 完成具体业务                          │
└─────────────────────────┬────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────┐
│  业务层  (weibo_im.crawler.Crawler)                      │
│  - sync_groups / crawl_all / crawl_group                 │
│  - _backfill_group（回填）/ probe_boundary（探边界）      │
│  - download_all_media / download_fid / scan_links        │
└────────┬────────────────────┬──────────────────┬─────────┘
         │ HTTP               │ DB               │ 文件
┌────────▼─────────┐  ┌───────▼────────┐  ┌─────▼──────────┐
│ API 客户端层      │  │  持久层 db.py   │  │ 媒体层 media.py │
│ - fetch_contacts │  │  - SQLite 连接  │  │ - download_file │
│ - fetch_messages │  │  - save_message │  │ - 类型/扩展名   │
│ - 重试退避        │  │  - save_groups  │  │ - 链接文件      │
│  crawler.py 上半  │  │  - FTS5 触发器  │  │  links.py       │
└────────┬─────────┘  └────────────────┘  └─────────────────┘
         │
┌────────▼─────────────────────────────────────────────────┐
│  解析层  (parser.py) — 纯函数，无副作用                    │
│  parse_message(raw) → {mid, gid, msg_type, ...}          │
│  类型码定义: types.py                                      │
└──────────────────────────────────────────────────────────┘
```

### 2.2 调用关系（运行时）

```
crawl.main()
  ├─ 若 --renew-cookie → _renew_cookie() (Playwright)
  │                          ↓
  │                   db.set_cookie()
  │
  ├─ 若 --stats / --list-groups / --search / --list-members / --export / --add-skip-gid ...
  │     → 直接调 db.* 静态查询，不构造 Crawler
  │
  └─ 否则 → Crawler(db_path)
              ├─ crawl_all(since_time, gid)
              │    ├─ sync_groups()       → fetch_contacts() → save_groups()
              │    └─ for g in groups:
              │         crawl_group(gid, since_time)
              │           ├─ get_latest_mid()  (判断首次/增量)
              │           ├─ 首次 → _backfill_group(since=今天零点)
              │           ├─ 有 since_time → _backfill_group(since, start_mid)
              │           └─ 增量 → 翻页直到 msgs[-1].id <= max_mid
              │
              ├─ 若 --download → download_all_media(skip_video)
              │                    └─ 循环 download_pending() 直到空
              │
              ├─ 若 --probe-boundary → probe_boundary(gid)
  │
  ├─ 若 --export --gid GID --sender-name/id ...
  │     → _do_export(db_path, gid, sender_name, sender_id, since, until, output, compact)
  │          ├─ _parse_since(since/until)                   ← 日期→毫秒（crawl.py）
  │          ├─ export.export_member_messages(db_path, ...) ← 委托导出模块
  │          │    ├─ export.get_member_messages(gid, ...)   ← 查询本人发言
  │          │    ├─ db.get_group_list()                    ← 取群名
  │          │    ├─ export.build_export_json(rows)         ← 组装 JSON 列表
  │          │    └─ json.dump() → export_<gid>_<name>.json ← 写文件（compact 时 minified）
  │          └─ 日志输出
  │
  └─ 若 --list-members --gid GID
        → _do_list_members(db_path, gid)
             └─ export.list_members(db_path, gid)
                  └─ export.get_members(gid)                ← 列出发言成员
```

---

## 3. 模块职责

### 3.1 模块清单

| 模块 | 行数 | 职责 | 对外接口 |
|------|------|------|---------|
| `crawl.py` | ~480 | CLI 入口、参数解析、流程编排 | `main()` / `_do_export` / `_do_list_members` |
| `weibo_im/crawler.py` | ~600 | API 客户端 + 爬取业务 | `Crawler` 类 |
| `weibo_im/parser.py` | ~250 | 原始消息 → 统一字典 | `parse_message` / `parse_messages` |
| `weibo_im/db.py` | ~490 | SQLite + FTS5 读写 | `init_db` / `save_*` / `get_*` / `get_stats` |
| `weibo_im/export.py` | ~200 | 成员发言导出（用于 AI 分析） | `get_members` / `get_member_messages` / `build_export_json` / `export_member_messages` / `list_members` |
| `weibo_im/media.py` | ~230 | 媒体文件下载 | `download_file` / `download_pending` |
| `weibo_im/links.py` | ~280 | 外部链接文件扫描下载 | `scan_and_download_messages` |
| `weibo_im/types.py` | ~75 | 类型码常量与工具 | `MSG_TYPES` / `MEDIA_TYPES` / `VIDEO_MEDIA_TYPES` |

### 3.2 各模块详解

#### `crawl.py` — 入口层

**只做三件事：**
1. 解析命令行参数。
2. 初始化日志。
3. 根据 `--xxx` 分支调对应业务函数。

**不做：** 任何业务逻辑、HTTP 请求、SQL。所有「干活的代码」都在 `weibo_im/`。

**特殊处理：**
- `--renew-cookie` 走 `_renew_cookie()`（含 Playwright），与其他分支互斥。
- `--stats` / `--list-groups` / `--list-skip` / `--add-skip-gid` / `--search` / `--list-members` / `--export` 只需初始化 DB，不构造 `Crawler`（不需要 cookie）。
- 其他爬取/下载命令先构造 `Crawler`（这步会校验 cookie 是否存在）。
- `--export` 命令接受 `--gid`（必选）+ `--sender-name` 或 `--sender-id`（至少一个）+ 可选 `--since` / `--until` / `--output` / `--compact` 参数，将指定成员在指定群的本人发言导出为 JSON 文件（用于 AI 分析）。`--compact` 输出紧凑 JSON（去缩进、去空格），减小文件体积。
- `--list-members` 命令搭配 `--gid` 列出群内发过言的成员及其发言数/最近发言时间，便于定位准确的成员标识（昵称或 sender_id）。

#### `weibo_im/crawler.py` — 业务层

**上半部分（模块级函数）：HTTP 客户端**

- `make_session(cookie)` — 构造带 Cookie 的 requests.Session
- `_request_with_retry(...)` — 带重试退避的请求封装
- `fetch_contacts(session)` — §2.2 群列表
- `fetch_messages(session, gid, count, max_mid)` — §2.3 消息
- `_jitter_sleep(base, jitter)` — 抖动 sleep

**下半部分：`Crawler` 类**

| 方法 | 职责 |
|------|------|
| `__init__(db_path, cookie)` | 初始化 DB + Cookie + Session |
| `sync_groups()` | 刷新群列表 |
| `crawl_all(since_time, gid, download_media)` | 爬所有群（顶层入口） |
| `crawl_group(gid, name, since_time)` | 爬单个群（**核心**） |
| `_backfill_group(gid, name, since_time, start_mid)` | 回填历史（向更早翻） |
| `probe_boundary(gid)` | 盲测最早可爬取边界 |
| `download_all_media(skip_video)` | 下载所有 pending 媒体 |
| `download_fid(fid)` | 下载单个 fid |
| `scan_links(limit)` | 扫描链接文件 |
| `stats()` | 数据库统计 |

#### `weibo_im/parser.py` — 解析层

**核心是 `parse_message(raw)` 纯函数**：输入 API 原始字典，输出统一格式字典（见 API.md §3.1）。

设计要点：
- **纯函数**：不读 DB、不发请求、无全局状态。
- **双源兼容**：支持 REST API（字段平铺）和 CometD 推送（字段包在 `info` 里）。
- **跳过逻辑**：`SKIP_TYPES = {332, 9999}` 返回 `None`。
- **辅助函数**：`resolve_fid` / `resolve_media_orig_url` / `extract_*` 都是私有，被 `parse_message` 调用。

> 跨语言迁移时这个文件**最重要**——是业务知识的载体。建议逐函数翻译。

#### `weibo_im/db.py` — 持久层

- **连接管理**：thread-local 连接（每个线程一个 Connection），全局 DB 路径变量。
- **自动建表**：`init_db()` 用 `CREATE TABLE IF NOT EXISTS` + FTS5 + 触发器。
- **幂等写入**：所有 `save_*` 用 `INSERT OR IGNORE`（依赖 UNIQUE 约束）。
- **索引**：`gid` / `msg_type` / `created_at` / `mid` / `fid` / `status` 上有索引。

#### `weibo_im/export.py` — 导出层（新增）

独立的成员发言导出模块，纯读操作，不修改任何表数据。

| 函数 | 职责 |
|------|------|
| `ms_to_cst(ms)` | 毫秒时间戳转 CST 格式化字符串 |
| `get_members(gid)` | 查询群内发过言的成员（sender_id/昵称/发言数/最近时间），按最近发言倒序；仅统计 msg_type 100/321 的真实用户发言 |
| `get_member_messages(gid, sender_id, sender_name, since_ms, until_ms, msg_types)` | 按群+成员+时间范围+消息类型筛选发言，按 created_at ASC 排序 |
| `build_export_json(rows)` | 将查询结果组装为发言列表，每条仅含 `sender_name` 与 `text` 两项 |
| `export_member_messages(db_path, gid, ..., compact=False, fmt="json")` | 完整导出流程：初始化 DB → 查询 → 组装数据 → 写文件，返回结果 dict；fmt="csv" 时输出 CSV，compact=True 时输出紧凑 JSON |
| `list_members(db_path, gid)` | 初始化 DB 后查询并打印群成员列表 |

设计要点：
- **依赖 db.py 只读接口**：通过 `set_db_path` / `init_db` / `get_conn` / `get_group_list` 访问数据库。
- **无需 Cookie / 网络**：纯本地 DB 查询 + JSON/CSV 序列化。
- **输出格式**：导出文件每条仅含 `sender_name`（发言者昵称）和 `text`（消息正文）两项。
  - `fmt="json"`（默认）：纯 JSON 数组，无 `meta` 包裹，可直接喂给大模型。
  - `fmt="csv"`：CSV 文件，表头为 `sender_name,text`，UTF-8-SIG 编码（Excel 可直接打开且中文不乱码）。
- **紧凑模式**：`export_member_messages` 的 `compact=True`（CLI 对应 `--compact`）使用 `separators=(",", ":")` 去掉缩进与多余空白，输出 minified JSON，文件体积通常减到默认的 1/3~1/2，适合直接喂大模型或网络传输；默认 `compact=False` 保留 2 空格缩进便于人工阅读（该参数仅对 JSON 生效，CSV 无影响）。
- **默认输出路径**：`<项目根>/export_<gid>_<sender>.json`（CSV 为 `.csv`），可通过 `--output` 自定义。
- 行数 ~200，是项目中行数最少、耦合最轻的模块。

#### `weibo_im/media.py` — 媒体下载

- `download_file(fid, url, media_type)` — 单文件下载（流式）
- `_patch_ext_from_response(...)` — 根据响应头修正扩展名
- `download_pending(limit, skip_video)` — 批量下载队列
- `_mark_videos_skipped()` — 把视频标 skipped

#### `weibo_im/links.py` — 链接文件

独立于媒体下载的第二类文件——消息正文里的外部 URL（非微博域名）指向的 PDF/ZIP 等。

- `extract_urls(text)` — 正则提取 URL
- `resolve_tcn(url)` — 解析 t.cn 短链
- `is_downloadable_file(url)` — 后缀 + HEAD 判断是否文件
- `scan_and_download_messages(limit)` — 扫描消息+下载

#### `weibo_im/types.py` — 常量

纯常量定义，无逻辑。跨语言迁移时直接照抄码表。

---

## 4. 核心数据流

### 4.1 爬取主流程

```
[微博 API]
    │
    │  fetch_messages(gid, count=50, max_mid=cursor)
    │  ← JSON: {result: true, messages: [...50条...]}
    ▼
[parser.parse_messages(raw_list)]
    │
    │  每条 raw → parse_message → 标准化 dict 或 None（跳过）
    ▼
[crawl_group 过滤层]
    │
    │  for pm in parsed:
    │    if pm.mid <= local_max_mid: continue  ← 内存去重
    ▼
[db.save_message(pm)]
    │
    │  INSERT OR IGNORE INTO messages ...      ← DB 去重（兜底）
    │  if pm.fid and pm.media_orig_url:
    │      save_media_file(...)                ← 同步入 media_files 队列
    ▼
[sqlite: messages + media_files 表]
```

### 4.2 三层去重（关键设计）

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 层：翻页层（省请求）                                    │
│   拉到一页后，如果 msgs[-1].id <= local_max_mid，整页丢弃    │
│   不入库、不发后续请求                                       │
├─────────────────────────────────────────────────────────────┤
│ 第 2 层：内存过滤（省入库）                                  │
│   遍历每条消息，pm.mid <= local_max_mid 直接 continue        │
├─────────────────────────────────────────────────────────────┤
│ 第 3 层：DB 约束（兜底保证）                                  │
│   messages.mid 有 UNIQUE 约束                                │
│   INSERT OR IGNORE 撞主键静默忽略                            │
│   ⇒ 任何情况下都不会有重复行                                  │
└─────────────────────────────────────────────────────────────┘
```

**翻译到其他语言时，这三层都要实现**。第 3 层是「正确性底线」，前两层是「性能优化」。

### 4.3 媒体下载流程

```
[media_files 表 status='pending']
    │
    │  get_pending_media(limit=10)
    ▼
[for f in pending]
    │
    │  download_file(fid, url, media_type)
    │    ├─ 已存在 → 直接返回 done（幂等）
    │    ├─ 红包 → 写占位文件
    │    └─ HTTP GET stream → 写 chunk → 修正扩展名
    ▼
[update_media_status()]
    │
    │  status: pending → done / failed / skipped
    │  回填 local_path / file_size / md5
    ▼
[同步 messages.media_local_path]
```

**视频跳过（`--no-video`）：**

```
download_pending(skip_video=True)
    │
    │  _mark_videos_skipped()
    │    UPDATE media_files SET status='skipped'
    │    WHERE status='pending' AND media_type IN (10, 13)
    ▼
[视频全部 skipped，不再进下载队列]
```

### 4.4 成员发言导出流程

为 AI 分析提供指定群成员的发言导出能力（`--export`），纯读操作，不修改任何表数据。

```
[用户指定 --gid + --sender-name|--sender-id + 可选 --since/--until]
    │
    │  _parse_since() 将日期/时间戳字符串统一为 UTC 毫秒
    ▼
[db.get_member_messages(gid, sender_id, sender_name, since_ms, until_ms)]
    │
    │  SELECT mid, msg_type, sender_id, sender_name, text, created_at, ...
    │  FROM messages
    │  WHERE gid=? AND msg_type IN (100,321)          ← 仅真实用户发言
    │    AND (sender_id=? 若传入)                      ← 按 ID 筛选（稳定）
    │    AND (sender_name=? 若传入)                    ← 按昵称筛选（可读）
    │    AND (created_at>=? 若指定 --since)           ← 时间下界
    │    AND (created_at<?  若指定 --until)           ← 时间上界（开区间）
    │  ORDER BY created_at ASC                        ← 按时间线供 AI 分析
    ▼
[_do_export 组装 JSON]
    │
    │  [
    │    {"sender_name": "...", "text": "..."},
    │    {"sender_name": "...", "text": "..."}
    │  ]
    ▼
[写入 JSON 文件]
    │
    │  默认路径: 项目根/export_<gid>_<sender>.json
    │  可通过 --output/-o 自定义
```

**设计要点**：

- **纯读操作**：不修改任何表数据，不依赖 Cookie，不构造 Crawler。
- **消息类型限定**：默认仅导出 msg_type 100（微博分享）和 321（普通消息），排除 322（系统消息）、332（撤回）等非用户发言。
- **导出为纯数组**：输出文件不含任何 `meta` 包裹，直接是发言列表，每条仅含 `sender_name`（发言者昵称）与 `text`（消息正文）两项，省略时间戳、消息类型、媒体等冗余字段，聚焦发言内容本身，便于直接用于 AI 分析。
- **紧凑模式（可选）**：加 `--compact` 时 `json.dump` 使用 `separators=(",", ":")` 且无缩进，输出单行 minified JSON，显著减小体积；默认带 2 空格缩进，便于人工查看。
- **空结果也是合法 JSON**：列表为空时仍输出 `[]`，控制台提示用 `--list-members` 核对标识。
- **`--list-members`** 作为定位辅助：先列出群内成员（sender_id / 昵称 / 发言数 / 最近时间），帮用户找到准确的成员标识，再用于 `--export`。

> 跨语言迁移时，导出模块只需实现对应的 DB 查询 + JSON 序列化，不涉及任何网络请求或状态变更，是最简单的模块之一。

---

## 5. 关键算法

### 5.1 翻页方向模型（线段模型）

把每个群已爬到的 mid 范围看作数轴上的一段 `[min_mid, max_mid]`。

**两种操作：**

```
增量（右扩）：
                          [min_mid ─────── max_mid]  ← 已有
                                              └──────► 拉新
   cursor = ""（从最新开始）→ 向左翻到 max_mid 为止

回填（左扩）：
                          [min_mid ─────── max_mid]  ← 已有
              ◄────── 拉更早
   cursor = min_mid（向更早）→ 翻到 since_time 或空页为止
```

**核心代码**（`crawler.py:crawl_group`）：

```python
last_mid = get_latest_mid(gid)
first_run = not last_mid

if first_run:
    since_time = since_time or midnight_today_ms()
    return self._backfill_group(gid, name, since_time=since_time)

if since_time is not None:
    min_mid, _ = get_group_mid_range(gid)
    return self._backfill_group(gid, name,
                                since_time=since_time,
                                start_mid=min_mid)

# 增量模式
_, max_mid = get_group_mid_range(gid)
cursor = ""
for page in range(100):
    msgs = fetch_messages(gid, max_mid=cursor or None)
    if not msgs: break
    if msgs[-1].id <= max_mid: break    # 整页已知
    for pm in parsed:
        if pm.mid <= max_mid: continue
        save_message(pm)
    if len(msgs) < 50: break
    cursor = msgs[0].id                 # 向更早翻
```

### 5.2 最早边界探测（盲测）

**问题**：微博服务端只保留 ~2 年消息，但具体边界会漂移。怎么知道一个群最早能拉到什么时候？

**算法**（`crawler.py:probe_boundary`）：

mid 和时间戳有近似线性关系：`mid ≈ SLOPE × ts_ms + INTERCEPT`，其中 `SLOPE = 4194.3044`。

```
Phase 1: 指数后退
  从已知最早时间往回探，间隔依次 1, 3, 7, 14, 30, 90, 180, 365, 730, 1095 天
  探到空 → 上界确定（这个时间点之前没消息）
  探到有 → 更新下界继续后退

Phase 2: 二分查找
  在 [下界, 上界] 区间二分，每次取中点
  直到窗口 < 1 小时

Phase 3: 翻页到空
  从下界开始往更早翻，直到 API 返回空
  最后一条就是真正的入群消息
```

**精度**：足够定位入群日期（±几小时），不要当作精确值。

> ⚠️ SLOPE 是经验值，微博可能调整，过期后探测会失败。建议跨语言迁移时把 SLOPE/INTERCEPT 做成可配置常量。

### 5.3 重试退避

见 API.md §4.1。核心：

```
5xx / 网络错：  backoff = 2^attempt × (1 + rand[0, 0.5])
429 限流：      backoff = 4^attempt × (1 + rand[0, 0.5])  ← 更激进
4xx 其他：      不重试
```

---

## 6. 状态机

### 6.1 媒体文件状态机

```
        ┌─────────┐
        │ pending │ ← 默认（save_media_file 入库）
        └────┬────┘
             │
    ┌────────┼──────────┬────────────┐
    │        │          │            │
    ▼        ▼          ▼            ▼
┌──────┐ ┌──────┐  ┌─────────┐  ┌────────┐
│ done │ │failed│  │ skipped │  │ (重试) │
└──┬───┘ └──┬───┘  └─────────┘  └────────┘
   │        │           ▲
   │        │           │ --no-video 把视频标 skipped
   │        │           │ （不会自动回到 pending）
   │        │
   │        └─ 手动 --download-fid FID 可重新尝试
   │
   └─ 已下载文件存在，重跑 download_file 直接返回 done（幂等）
```

**状态语义**：

| 状态 | 含义 | 是否重试 |
|------|------|---------|
| `pending` | 待下载 | ✅ `--download` 会取 |
| `done` | 已下载 | ❌（除非手动改回 pending） |
| `failed` | 下载失败 | ❌（除非手动改回） |
| `skipped` | 主动放弃（视频） | ❌（除非手动改回） |

### 6.2 群爬取状态机

```
                     ┌──────────────────┐
                     │  DB 无此群消息    │
                     │  (first_run=True)│
                     └────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ▼               ▼               ▼
      --since 指定      --since 0       不指定 --since
      回填到指定日期    全部历史        默认回填到今天零点
              │               │               │
              └───────────────┴───────────────┘
                              │
                              ▼
                   ┌────────────────────┐
                   │ DB 已有此群消息     │
                   │ (first_run=False)  │
                   └────────┬───────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
        --since 指定              不指定 --since
        左扩填补到 since          右扩增量拉新
```

### 6.3 Cookie 状态机

```
[未登录] ──scan QR──► [已登录] ──SUB 过期──► [失效]
   ▲                                          │
   │                                          │
   └────────── --renew-cookie ────────────────┘
```

失效检测：`sync_groups()` 返回空数组。

---

## 7. 数据模型

### 7.1 ER 图

```
┌─────────────┐         ┌──────────────────┐
│   groups    │ 1     N │    messages       │
│─────────────│◄────────│──────────────────│
│ gid (PK)    │         │ id (PK autoinc)   │
│ name        │         │ mid (UNIQUE)      │
│ member_count│         │ gid (FK→groups)   │
│ min_mid     │         │ msg_type          │
│ max_mid     │         │ media_type        │
└──────┬──────┘         │ sender_id         │
       │                │ text              │
       │                │ fid               │
       │                │ created_at        │
       │                │ raw_json          │
       │                └─────────┬──────────┘
       │                          │
       │                ┌──────────────────┐
       │           1  N │   media_files     │
       └────────────────│──────────────────│
                        │ fid (UNIQUE)     │
                        │ gid              │
                        │ mid              │
                        │ status           │
                        │ local_path       │
                        └──────────────────┘

┌──────────────────┐    ┌──────────────────┐
│   config (KV)    │    │   link_files     │
│──────────────────│    │──────────────────│
│ key (PK)         │    │ url_hash (UNIQUE)│
│ value            │    │ url              │
│  - weibo_cookie  │    │ local_path       │
│  - skip_gids     │    │ file_type        │
└──────────────────┘    └──────────────────┘
```

### 7.2 表字段语义

> 完整 DDL 见 `db.py:init_db()`，跨语言 DDL 见 API.md §7.3。

| 表 | 关键字段 | 说明 |
|----|---------|------|
| `groups` | `gid` PK, `min_mid`/`max_mid` | min/max_mid 是线段模型的两端 |
| `messages` | `mid` UNIQUE, `gid`, `created_at`(ms) | `raw_json` 永久保留原始数据 |
| `messages_fts` | FTS5 虚表 | 索引 `text/sender_name/group_name`，触发器自动同步 |
| `media_files` | `fid` UNIQUE, `status` | 状态机见 §6.1 |
| `link_files` | `url_hash` UNIQUE | URL MD5 前 16 位，避免长 URL |
| `config` | `key` PK | `weibo_cookie`、`skip_gids` 等 |

### 7.3 设计取舍

| 决策 | 选择 | 原因 |
|------|------|------|
| mid 类型 | TEXT (字符串) | 长度可达 18 位，超过 int 范围 |
| 时间戳 | INTEGER ms | 统一毫秒，避免歧义 |
| 原始 JSON | LONGTEXT / TEXT | 永久保留，便于重新解析 |
| 全文搜索 | SQLite FTS5 | 零依赖；MySQL 用 FULLTEXT+ngram |
| Cookie 存储 | DB config 表 | 单一数据源，不靠文件 |
| 时区 | 业务统一 CST | 不依赖系统时区 |

### 7.4 导出 JSON 结构（用于 AI 分析）

`--export` 输出的 JSON 文件是一个纯数组，每个元素是一条发言记录。

```json
[
  {
    "sender_name": "某成员",
    "text": "发言内容..."
  }
]
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `[].sender_name` | 发言者昵称 |
| `[].text` | 消息正文（纯文本，不含 HTML） |

> 导出为纯 JSON 数组文件，无 `meta` 包裹、无额外依赖，可直接喂给大模型或任意 JSON 解析器。空结果也输出合法 JSON（`[]`）。

---

## 8. 并发与一致性

### 8.1 单进程模型

当前实现**单进程单线程**。SQLite 用 WAL 模式，读写并发安全，但同一进程里没有真正的并行。

### 8.2 多进程/多线程注意事项 ⚠️

| 场景 | 风险 | 建议 |
|------|------|------|
| 同时开两个 `crawl.py` | 都走 `INSERT OR IGNORE`，不会脏数据，但可能各自少拉一些（互相抢 mid） | ❌ 不要并行 |
| 同时跑爬虫 + 下载 | 都操作同一 DB，SQLite WAL 下读读/读写并发 OK | ✅ 可以 |
| 分布式爬多个群 | 多机共用一个 MySQL 时，用 `SELECT ... FOR UPDATE` 锁定 gid | 跨语言迁移时考虑 |

### 8.3 事务边界

`save_message` 每条 commit 一次（`crawler.py` 调用方未批量）。这换来的是断点安全性：进程挂掉已写的不会丢。

跨语言迁移到 MySQL 时建议：
- 单条消息自动 commit 没问题。
- 批量回填时可用 `INSERT ... VALUES (...),(...),...` 批量 + `IGNORE` 提速。

---

## 9. 可观测性与运维

### 9.1 日志

```
%(asctime)s [%(levelname)s] %(name)s: %(message)s
%H:%M:%S
```

logger 命名约定：
- `crawl` — 入口层
- `weibo_im.crawler` — 爬取业务
- `weibo_im.media` — 媒体下载
- `weibo_im.links` — 链接文件
- `weibo_im.parser` — 解析（仅警告级）

### 9.2 关键日志事件

| 日志 | 含义 | 处理 |
|------|------|------|
| `query_messages 返回 result=false` | Cookie 失效/限流 | 检查 Cookie |
| `↻ 5xx 重试` | 服务端错 | 等待自动恢复 |
| `↻ 429 限流` | 触发风控 | 加大间隔 |
| `⇣ [群名] +N 条新消息` | 增量成功 | — |
| `⬇ [群名] 回填 N 条历史` | 回填成功 | — |

### 9.3 统计与导出命令

`--stats` 输出（基于 `get_stats()`）：

```
消息总数 / 有消息的群 / 群总数
媒体已下载 / 待下载 / 失败 / 跳过
```

`--list-members --gid GID` 输出：

```
群成员列表（sender_id / 昵称 / 发言数 / 最近发言时间）
用于定位 --export 所需的成员标识
```

`--export --gid GID --sender-name/id ...` 输出：

```
导出 N 条发言 → export_<gid>_<sender>.json
时间范围: YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS (CST)
# 加 --compact 则输出紧凑（minified）JSON，无缩进/空格
# 加 --format csv 则输出 export_<gid>_<sender>.csv（表头 sender_name,text）
```

### 9.4 定时任务

无内置调度器，靠系统原生：

| 平台 | 工具 | 推荐 |
|------|------|------|
| Windows | 任务计划程序 | 每 10 分钟跑一次 `uv run crawl.py` |
| Linux | cron | `*/10 * * * *` |
| macOS | launchd | 每 600 秒 |

**注意**：每次跑前最好先验证 Cookie（`--group-only` 失败就告警）。

---

## 10. 跨语言迁移指南

### 10.1 迁移优先级

```
必做（核心）
  1. types.py        ← 码表，直接照抄
  2. parser.py       ← 解析逻辑，逐函数翻译
  3. db.py 表结构    ← 见 API.md §7.3
  4. crawler.py HTTP ← fetch_contacts / fetch_messages
  5. 翻页算法        ← §5.1 线段模型

建议做（增强）
  6. 重试退避        ← §5.3
  7. 媒体下载        ← media.py
  8. 链接文件        ← links.py

可选
  9. probe_boundary  ← §5.2 边界探测
 10. 成员发言导出     ← §4.4 导出流程（仅 DB 查询 + JSON 序列化，无网络依赖）
 11. FTS5 全文搜索   ← 数据库相关
```

### 10.2 Java + MySQL 迁移示例架构

```
src/main/java/weiboim/
├── App.java                          ← 入口（对应 crawl.py）
│   - parseArgs()
│   - renewCookie()  ← Selenium/Playwright
│
├── api/                              ← 对应 crawler.py 上半
│   ├── WeiboClient.java              ← make_session + retry
│   ├── ContactsApi.java              ← fetch_contacts
│   └── MessagesApi.java              ← fetch_messages
│
├── core/                             ← 对应 crawler.py 下半
│   ├── Crawler.java                  ← 业务编排
│   ├── GroupCrawler.java             ← crawl_group + _backfill
│   └── MediaDownloader.java          ← download_all
│
├── parser/                           ← 对应 parser.py
│   ├── MessageParser.java
│   └── FieldExtractor.java
│
├── model/                            ← 对应 types.py + db schema
│   ├── Message.java
│   ├── Group.java
│   ├── MediaFile.java
│   └── MsgType.java                  ← enum
│
├── store/                            ← 对应 db.py
│   ├── MessageRepository.java
│   ├── GroupRepository.java
│   ├── MediaFileRepository.java
│   ├── ConfigRepository.java
│   ├── MemberExportRepository.java    ← get_member_messages / get_members
│   └── ExportService.java            ← _do_export 对应
│
└── util/
    ├── Backoff.java                  ← 重试退避
    ├── JitterSleep.java
    └── CookieUtils.java
```

### 10.3 关键迁移决策点

| 决策 | Python 选择 | Java/MySQL 推荐 |
|------|-------------|-----------------|
| mid 类型 | TEXT | `VARCHAR(32)` + Java `String` |
| 时间戳 | INTEGER ms | `BIGINT` + Java `long` |
| 全文搜索 | SQLite FTS5 | MySQL `FULLTEXT ... WITH PARSER ngram` 或 Elasticsearch |
| Cookie 存储 | DB config 表 | 同（KV 表） |
| 重试退避 | `time.sleep` | `Thread.sleep` 或 Resilience4j |
| 浏览器 | Playwright Python | Playwright Java 或 Selenium |
| HTTP | requests | OkHttp（带连接池） |
| JSON | 内置 json | Jackson |

### 10.4 必须原样保留的设计

无论用什么语言，以下设计**不能省**，否则会出 bug：

1. **三层去重**（§4.2）—— 翻页层 + 内存过滤 + DB UNIQUE 约束
2. **抖动 sleep**（§5.3）—— 固定间隔易被风控
3. **mid 字符串比较**—— 不能转数字（溢出）
4. **`raw_json` 永久保留**—— 解析逻辑会演进
5. **消息返回顺序假设**（API.md §2.3）—— msgs[0] 最旧
6. **time 字段秒→毫秒转换**（< 1e12 时 ×1000）
7. **视频跳过状态机**（§6.1）—— skipped 不会自动回 pending

---

## 11. 已知局限与演进方向

### 11.1 当前局限

| 局限 | 影响 | 临时规避 |
|------|------|---------|
| 单线程爬取 | 群多时慢 | 可按 gid 分片多进程 |
| `--since` 重填会重复请求 | 浪费 API | 增量分支已优化，回填未优化 |
| Cookie 续期需手动扫码 | 不能全自动 | 可加 OCR 自动扫码（不推荐） |
| 中文 FTS 分词粗糙 | 短词命中差 | 接 Elasticsearch /jieba 分词 |
| 无 CometD 实时推送 | 增量靠轮询 | parser 已兼容 CometD，可接 WebSocket |
| 群列表无翻页 | 群特别多时漏 | 群多时需扩展分页 |

### 11.2 演进路线

```
当前（单机 SQLite）
    │
    ├─► 分布式（MySQL + 多 worker）
    │     - gid 分片，每 worker 负责一组群
    │     - 用 Redis 分布式锁避免重复
    │
    ├─► 实时（接 CometD WebSocket）
    │     - parser 已兼容，只需加 WS 客户端
    │     - 消息到达即入库，省轮询
    │
    └─► 搜索增强（接 Elasticsearch）
          - FTS5 换 ES + jieba 分词
          - 支持复杂检索 + 高亮
```

---

## 附录：架构决策记录（ADR 摘要）

| # | 决策 | 选择 | 备选 | 理由 |
|---|------|------|------|------|
| 1 | 数据库 | SQLite + FTS5 | MySQL / PG | 单机零依赖，迁移容易 |
| 2 | 入口 | 单一 CLI 文件 | Web UI | 简单可靠，cron 友好 |
| 3 | Cookie 来源 | Playwright 扫码 | 抓包手动 | 自动化，体验好 |
| 4 | 去重 | DB UNIQUE 兜底 | 应用层 hash set | 永不重复，无需内存 |
| 5 | 时区 | 业务层固定 CST | 系统时区 | 跨机器一致 |
| 6 | 节奏 | 抖动 sleep | 固定间隔 | 规避简单频控 |
| 7 | 视频 | 默认跳过可选 | 默认下载 | 节省存储 |
| 8 | 解析 | 双源兼容 (REST + CometD) | 只支持 REST | 为接实时推送铺路 |

---

## 文档对照

| 想了解什么 | 看哪里 |
|----------|-------|
| 怎么用（用户视角） | `README.md` |
| 接口细节（URL/参数/响应） | `API.md` |
| 架构/数据流/迁移（开发者视角） | **本文档** |
| 类型码对照 | `API.md` §5 |
| 表结构 DDL | `API.md` §7.3 + `db.py:init_db` |
| 算法细节 | 本文档 §5 |
