# WeiboGroupCrawler — 微博群聊消息本地爬虫

把微博群聊消息抓取到本地 SQLite 数据库的工具。基于原始的 WeiboChatToWeChat
方案改造而成，**去除了对 Hermes / 微信的依赖**，可在本机直接命令行运行，
数据全部落在本地。

- 定时增量爬取群聊消息（手动运行或交给系统计划任务）
- Playwright 扫码登录微博 → 自动提取 cookie（截图保存到本地，扫码弹窗/图片任选）
- SQLite + FTS5 全文搜索
- 图片 / 视频 / 文件下载，按 Content-Disposition 自动识别扩展名
- 外部链接文件（PDF / ZIP / DOC 等）识别与下载
- 群列表同步，可配置跳过指定群
- 历史消息回填（可指定日期和单个群）
- 盲测最早可爬取边界（最久两年，两年内即入群时间）

> 原版面向 Ubuntu(ARM64) + Hermes，本版本面向 **Windows / macOS / Linux 桌面**，
> 全部交互通过控制台和本地文件完成，不调用任何外部 Agent。

---

## 1. 环境要求

| 项 | 要求 |
|----|------|
| 操作系统 | Windows 10+ / macOS / Linux（有桌面或无头均可） |
| Python | ≥ 3.11 |
| 包管理 | 推荐 `uv`；也可用标准 `pip` |
| 浏览器 | Playwright + Chromium（**仅** `--renew-cookie` 扫码需要） |
| 账号 | 一个已加入至少一个微博群聊的微博账号 |

依赖包（见 `pyproject.toml`）：

```
requests
urllib3
playwright
```

---

## 2. 项目结构规范

```
weibogroup/
├── crawl.py              # CLI 入口（唯一可执行脚本）
├── pyproject.toml        # 项目配置 + 依赖
├── README.md             # 本文档
├── weibo_im.db           # SQLite 数据库（运行时生成）
├── qrcode.png            # 扫码登录二维码截图（--renew-cookie 生成，可删）
├── media/                # 下载的媒体（运行时生成）
│   ├── images/           # 群聊图片（media_type=1）
│   ├── videos/           # 群聊视频（media_type=10/13）
│   └── files/            # 链接文件 PDF/ZIP/DOC + media_type=5 附件
└── weibo_im/             # 核心包
    ├── __init__.py
    ├── types.py          # 消息/媒体类型码定义
    ├── parser.py         # API 原始消息 → 统一字典
    ├── db.py             # SQLite + FTS5 读写
    ├── crawler.py        # HTTP 客户端 + 爬取逻辑
    ├── media.py          # 图片/视频/附件下载
    └── links.py          # 外部链接文件识别与下载
```

**结构约定：**

- `crawl.py` 是唯一入口，**不放置业务逻辑**，只做参数解析、初始化、调用 `weibo_im.*`。
- `weibo_im/` 包对外暴露 `Crawler` 类与若干 `db.*` 函数，其余为内部实现。
- 所有可变状态（数据库、媒体、二维码）都生成在**项目根目录**下，方便整体备份/迁移。
- 运行时产物（`weibo_im.db`、`media/`、`qrcode.png`、`__pycache__/`）不应纳入版本管理。

---

## 3. 首次部署步骤

```bash
# 1. 安装依赖（任选其一）
uv sync                           # 推荐
# 或：pip install requests urllib3 playwright

# 2. 安装浏览器（仅扫码登录需要）
uv run playwright install chromium
# 或：python -m playwright install chromium

# 3. 验证环境
python crawl.py --check-playwright
# 期望：✅ playwright Python 包可导入  /  ✅ Chromium 启动正常

# 4. 扫码登录（默认有头弹窗；无桌面环境加 --headless）
python crawl.py --renew-cookie
# 弹出浏览器 → 用微博 APP 扫码 → 程序自动提取 cookie 入库

# 5. 首次爬取（每个群默认回填到今天零点 CST）
python crawl.py
```

扫码失败的两种退路：

- 简单修复无效 → 浏览器打开 `https://api.weibo.com/chat` 手动登录，
  从 DevTools → Application → Cookies 复制 `.weibo.com` 域下所有键值，
  拼成 `k1=v1; k2=v2` 形式，运行：
  ```
  python crawl.py --set-cookie "SUB=xxx; SUBP=yyy; ..."
  ```

---

## 4. 命令清单（CLI 接口规范）

`crawl.py` 的全部子功能。除注明外，均作用于默认数据库 `weibo_im.db`。

### 4.1 登录与 cookie

| 命令 | 作用 |
|------|------|
| `python crawl.py --renew-cookie` | Playwright 打开扫码页，扫码后自动存 cookie。默认**有头弹窗**。 |
| `python crawl.py --renew-cookie --headless` | 无头模式：仅把二维码截图存到 `qrcode.png` 并尝试用系统默认程序打开。适合无桌面 Linux。 |
| `python crawl.py --check-playwright` | 检查 Playwright + Chromium 是否就绪，返回 exit code 0/1。 |
| `python crawl.py --set-cookie 'SUB=xxx; SUBP=yyy'` | 手动写入 cookie，不依赖 Playwright。 |
| `python crawl.py --db D:\path\to.db ...` | 任何命令都可加 `--db` 指定数据库路径。 |

### 4.2 爬取

| 命令 | 作用 |
|------|------|
| `python crawl.py` | 爬取所有群的新消息。**首次**对没有记录的群默认回填到今天零点（CST）。 |
| `python crawl.py --since 2026-01-01` | 回填到指定日期（CST）。已有记录的群会从最旧 mid 往前补到该日期。 |
| `python crawl.py --since 2026-01-01 --gid 4761715839862414` | 只回填指定群。 |
| `python crawl.py --since 0` | `0` 表示无下限（全部可拉历史）。 |
| `python crawl.py --group-only` | 只刷新群列表，不爬消息。 |
| `python crawl.py --probe-boundary` | 盲测每个群最早可爬取边界（入群时间），仅打印不入库。 |
| `python crawl.py --probe-boundary --gid GID` | 只测指定群。 |
| `python crawl.py --verbose` | 详细日志（DEBUG 级）。 |

**爬取行为要点：**

- 消息翻页采用线段模型，每次拉 50 条，用 `max_mid` 控制方向。
- API 返回顺序是**从旧到新**：`msgs[0]` 最旧（翻页游标），`msgs[-1]` 最新（停止判定）。
- 回填停止条件：API 返回空页 或 消息时间早于 `since_time`；**无数量/页数上限**。
- 回填大量历史可能耗时较长，建议在后台运行。

### 4.3 媒体与链接文件

| 命令 | 作用 |
|------|------|
| `python crawl.py --download` | 下载所有 `pending` 状态的媒体文件直到队列清空。 |
| `python crawl.py --download-fid 5302496155143676` | 下载指定 fid 的单个媒体文件。 |

> 爬取时**不自动下载媒体**——只把 fid/url 写入 `media_files` 表（status=`pending`）。
> 外部链接文件（PDF/ZIP 等）由 `scan_links` 扫描近期消息识别，仅在
> `crawl_all(download_media=True)` 时触发；当前 CLI 路径默认不调用，可按需在代码里启用。

### 4.4 群与跳过管理

| 命令 | 作用 |
|------|------|
| `python crawl.py --list-groups` | 列出数据库中所有群（gid / 成员数 / 群名）。 |
| `python crawl.py --list-skip` | 列出不爬取的群。 |
| `python crawl.py --add-skip-gid GID` | 加入不爬取列表。 |
| `python crawl.py --remove-skip-gid GID` | 从不爬取列表移除。 |

### 4.5 查询

| 命令 | 作用 |
|------|------|
| `python crawl.py --stats` | 打印数据库统计（消息数 / 群数 / 媒体数）。 |
| `python crawl.py --search 关键词` | FTS5 全文搜索消息（按时间倒序）。 |
| `python crawl.py --search 关键词 --search-limit 100` | 限制返回条数（默认 50）。 |

---

## 5. 数据存储规范

所有数据均在项目目录下，不依赖任何外部服务。

| 类型 | 位置 | 说明 |
|------|------|------|
| 数据库 | `weibo_im.db` | SQLite + FTS5。消息、群、cookie、配置、媒体/链接文件清单 |
| 图片 | `media/images/` | media_type=1 |
| 视频 | `media/videos/` | media_type=10/13 |
| 附件/链接文件 | `media/files/` | media_type=5 附件 + PDF/ZIP/DOC 等链接文件 |
| 二维码截图 | `qrcode.png` | `--renew-cookie` 生成，可随时删 |

### 5.1 数据库表结构

| 表 | 用途 |
|----|------|
| `config` | key-value：`weibo_cookie`、`skip_gids` 等 |
| `groups` | 群信息 + `min_mid`/`max_mid`（已存消息的 mid 范围，线段模型用） |
| `messages` | 消息主表，`mid` 唯一，含 `created_at`(ms)、`text`、`fid`、结构化 JSON 等 |
| `messages_fts` | FTS5 虚表，索引 `text`/`sender_name`/`group_name`，由触发器自动同步 |
| `media_files` | 媒体文件清单，`fid` 唯一，`status` ∈ pending/done/failed |
| `link_files` | 链接文件清单，`url_hash` 唯一（懒创建） |

### 5.2 时区约定 ⚠️

- 消息 `created_at` 字段是 **UTC 毫秒时间戳**。
- 代码内部统一用 **CST(+08:00)** 处理：`--since` 的日期字符串按 CST 解析，
  `--search` 输出按 CST 格式化，首次回填的「今天零点」也是 CST 零点。
- 这意味着**不论系统时区如何，时间口径都一致**（已用 `datetime(..., tz=CST)` 显式锚定，
  不依赖系统时区设置）。

---

## 6. HTTP / API 接口规范

项目直接调用微博 WebIM 的 REST API，不经过任何中间服务。

| 端点 | 方法 | 用途 |
|------|------|------|
| `https://api.weibo.com/webim/2/direct_messages/contacts.json` | GET | 群列表 |
| `https://api.weibo.com/webim/groupchat/query_messages.json` | GET | 群消息（`max_mid` 翻页） |
| `https://api.weibo.com/chat` | GET | 扫码登录页（Playwright 打开） |
| `https://upload.api.weibo.com/2/mss/msget?fid=...&source=...` | GET | 媒体文件下载 |

**请求规范：**

- 所有请求携带 `Cookie` 头（来自数据库 `config.weibo_cookie`）。
- 固定 `source=209678993`，`User-Agent` 伪装为桌面 Chrome。
- `verify=False`（微博证书链问题，沿用原实现）。
- 重试策略：5xx 指数退避、429 限流加倍等待、4xx(非429) 立即抛出。
- 请求间带抖动 `_jitter_sleep`，避免触发风控。

**关键函数（`weibo_im/crawler.py`）：**

```python
fetch_contacts(session) -> list[dict]
fetch_messages(session, gid, count=50, max_mid=None) -> list[dict]
make_session(cookie) -> requests.Session
class Crawler(db_path, cookie=""):
    .sync_groups() / .crawl_all(...) / .crawl_group(...)
    ._backfill_group(...) / .probe_boundary(...)
    .download_all_media() / .download_fid(fid) / .scan_links(limit)
    .stats()
```

---

## 7. 与原版（WeiboChatToWeChat）的差异

| 方面 | 原版（Hermes） | 本版本 |
|------|---------------|--------|
| 二维码下发 | `hermes send --to weixin` 发到微信 | 截图存本地 + 系统默认程序打开 / 有头弹窗 |
| 登录成功通知 | 微信消息 | 控制台日志 |
| Cookie 失效提醒 | 微信推送 | 控制台 ERROR + 退出提示 |
| 定时爬取 | `hermes cron` + wrapper 脚本 | 交给用户用系统计划任务（Windows 任务计划 / cron / launchd） |
| `--set-cron-id` / cron 恢复 | 有 | **已移除**（无对应概念） |
| 运行平台 | Ubuntu ARM64 | Windows / macOS / Linux 通用 |
| 新增查询命令 | 无 | `--stats` / `--list-groups` / `--search` |

**已删除的命令/参数：** `--set-cron-id`、所有 `hermes` 子进程调用。

---

## 8. 注意事项与已知限制

1. **cookie 会过期**。微博 cookie 有效期有限，过期后 `query_messages` 会返回
   `result=false` 或 4xx。届时重新跑 `--renew-cookie` 或 `--set-cookie`。
2. **FTS5 中文分词**。SQLite FTS5 默认 `unicode61` 分词器按非字母数字切词，
   对中文是「逐字」索引——短词（1~2 字）能搜，整句匹配需要用 `"短语"` 加引号
   或拆词。复杂检索建议直接 `sqlite3 weibo_im.db` 写 SQL。
3. **回填耗时**。回填数月历史会持续翻页，无页数上限。建议后台运行。
4. **媒体不自动下载**。爬取只入库 fid/url，要下载需显式 `--download`。
5. **盲测边界会漂移**。`probe_boundary` 用 mid↔时间线性回归（每 ms ≈ 4194），
   微博 2 年限制会让结果在边界附近有抖动，精度足够定位入群时间，不要当作精确值。
6. **媒体文件名修正**。下载时先按 media_type 起临时名，再依据响应头
   `Content-Disposition` / `Content-Type` 改名（如 `xxx.bin` → `报告.pdf`）。
7. **Windows 路径**。`--db` 支持反斜杠路径；`qrcode.png`、`media/` 都生成在
   项目根目录。`os.startfile` 用于打开图片，仅 Windows 有该方法。
8. **WAL 模式**。数据库开启 `journal_mode=WAL`，运行后会多出
   `weibo_im.db-wal` / `weibo_im.db-shm`，属正常现象，备份时一并复制。
9. **SSL 警告**。`urllib3.disable_warnings()` 已关闭证书告警，控制台干净。

---

## 9. 定时爬取（可选）

本版本不带调度器，用系统原生计划任务即可。

**Windows 任务计划（每 10 分钟）：**

```powershell
# schtasks 创建（路径按实际调整）
schtasks /create /tn "WeiboCrawl" /tr "cmd /c cd /d C:\Users\fixyz\Desktop\weibogroup && python crawl.py >> crawl.log 2>&1" /sc minute /mo 10
```

**Linux/macOS cron：**

```cron
*/10 * * * * cd /path/to/weibogroup && python crawl.py >> crawl.log 2>&1
```

---

## 10. 快速自检

部署完成后，按顺序跑这几条命令确认一切就绪：

```bash
python crawl.py --check-playwright          # ① 浏览器环境
python crawl.py --renew-cookie              # ② 扫码登录
python crawl.py                             # ③ 首次爬取（当天消息）
python crawl.py --stats                     # ④ 看统计
python crawl.py --list-groups               # ⑤ 看群列表
python crawl.py --search 微博               # ⑥ 试搜
python crawl.py --download                  # ⑦ 下载媒体
```

任何一步报错，对照第 8 节排查。

---

## 11. 消息查看器（web 前端）

本地只读 web 查看器，浏览已抓取的群聊消息。零外部依赖，仅用 Python 标准库
+ 原生 HTML/JS/CSS。

### 启动

```bash
python server.py
```

默认访问 http://127.0.0.1:8765 。可选参数：`--db`（数据库路径，默认同目录
`weibo_im.db`）、`--host`、`--port`。

### 功能

- 左栏按月折叠的日期列表（带每天消息数），点某天查看当天消息，最新在底
- 右栏聊天视图，向上 / 向下滚动加载更早 / 更新消息（每页 100 条，`(created_at, id)` 游标分页），加载时有 loading 提示
- 顶栏「高级搜索」按钮打开模态窗：可填发送者名称（精确匹配，可选）和 / 或关键词（模糊 `LIKE`，可选），两者 AND；结果先在窗内列出，点击跳转到主窗以命中消息为锚的上下文（命中及之前 100 条），可继续上下翻看
- 搜索时间范围默认最近 3 个月，可缩为 1 周 / 1 个月
- 媒体仅显示占位与原始链接（需带 cookie 才能访问，不在页面内加载）
- 跨天日期分隔条；系统消息（入群 / 撤回等）居中灰色显示

### 设计与实现文档

- 设计规格：`docs/superpowers/specs/2026-06-19-weibo-im-message-viewer-design.md`
- 实现计划：`docs/superpowers/plans/2026-06-19-weibo-im-message-viewer.md`

### 测试

```bash
python -m unittest discover tests
```
