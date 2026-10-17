# WeiboGroupCrawler — 微博群聊消息本地爬虫

把微博群聊消息抓取到本地 SQLite 数据库的命令行工具。基于原始的 WeiboChatToWeChat
方案改造而成，**去除了对 Hermes / 微信的依赖**，可在本机直接运行，数据全部落在本地。
同源还有 [WeiboBlogCrawler](https://github.com/findxyz/jfjzsxh-weiboblog)（博主博文爬虫），
技术栈一致、互不依赖，各自独立数据库。

## 核心功能

- 定时增量爬取群聊消息（手动运行或交给系统计划任务）
- Playwright 扫码登录微博，自动提取 cookie（支持有头弹窗与无头截图两种方式）
- SQLite + FTS5 全文搜索
- 图片 / 视频 / 文件下载，按 Content-Disposition 自动识别扩展名
- 外部链接文件（PDF / ZIP / DOC 等）识别与下载
- 群列表同步，可配置跳过指定群
- 历史消息回填（可指定日期和单个群）
- 盲测最早可爬取边界（最久两年，两年内即入群时间）
- 按成员导出发言（JSON / CSV），便于 AI 分析
- 本地 Web 消息查看器，零外部依赖

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

依赖包：`requests`、`urllib3`、`playwright`（见 `pyproject.toml`）。

---

## 2. 项目结构

```
weibogroup/
├── crawl.py              # CLI 入口（唯一可执行脚本）
├── pyproject.toml        # 项目配置 + 依赖
├── README.md             # 本文档
├── weibo_im.db           # SQLite 数据库（运行时生成）
├── qrcode.png            # 扫码登录二维码截图（--renew-cookie 生成，可删）
├── media/                # 下载的媒体（运行时生成）
│   ├── images/           # 群聊图片
│   ├── videos/           # 群聊视频
│   └── files/            # 链接文件 PDF/ZIP/DOC 等附件
└── weibo_im/             # 核心包
    ├── types.py          # 消息/媒体类型码定义
    ├── parser.py         # API 原始消息解析
    ├── db.py             # SQLite + FTS5 读写
    ├── crawler.py        # HTTP 客户端 + 爬取逻辑
    ├── media.py          # 图片/视频/附件下载
    └── links.py          # 外部链接文件识别与下载
```

所有可变状态（数据库、媒体、二维码）都生成在项目根目录下，方便整体备份/迁移。
运行时产物（`weibo_im.db`、`media/`、`qrcode.png`、`__pycache__/`）不应纳入版本管理。

---

## 3. 首次部署

```bash
# 1. 安装依赖
uv sync

# 2. 安装浏览器（仅扫码登录需要）
uv run playwright install chromium

# 3. 验证环境
uv run crawl.py --check-playwright

# 4. 扫码登录（默认有头弹窗；无桌面环境加 --headless）
uv run crawl.py --renew-cookie
# 弹出浏览器 → 用微博 APP 扫码 → 程序自动提取 cookie 入库

# 5. 首次爬取（每个群默认回填到今天零点 CST）
uv run crawl.py
```

扫码失败的退路：浏览器打开 `https://api.weibo.com/chat` 手动登录，从 DevTools
复制 `.weibo.com` 域下所有 cookie 键值，拼成 `k1=v1; k2=v2` 形式，运行：

```
uv run crawl.py --set-cookie "SUB=xxx; SUBP=yyy; ..."
```

---

## 4. 命令清单

`crawl.py` 的全部子功能。除注明外，均作用于默认数据库 `weibo_im.db`；
任何命令都可加 `--db <路径>` 指定数据库。

### 4.1 登录与 cookie

| 命令 | 作用 |
|------|------|
| `uv run crawl.py --renew-cookie` | 扫码登录，自动存 cookie（默认有头弹窗） |
| `uv run crawl.py --renew-cookie --headless` | 无头模式：二维码截图存 `qrcode.png` 并用系统默认程序打开，适合无桌面 Linux |
| `uv run crawl.py --check-playwright` | 检查 Playwright + Chromium 是否就绪 |
| `uv run crawl.py --set-cookie 'SUB=xxx; SUBP=yyy'` | 手动写入 cookie，不依赖 Playwright |

### 4.2 爬取

| 命令 | 作用 |
|------|------|
| `uv run crawl.py` | 爬取所有群的新消息。首次对没有记录的群默认回填到今天零点（CST） |
| `uv run crawl.py --since 2026-01-01` | 回填到指定日期（CST） |
| `uv run crawl.py --since 2026-01-01 --gid GID` | 只回填指定群 |
| `uv run crawl.py --since 0` | `0` 表示无下限（全部可拉历史） |
| `uv run crawl.py --group-only` | 只刷新群列表，不爬消息 |
| `uv run crawl.py --probe-boundary [--gid GID]` | 盲测群最早可爬取边界（入群时间），仅打印不入库 |
| `uv run crawl.py --verbose` | 详细日志（DEBUG 级） |

> 回填无数量/页数上限，大量历史可能耗时较长，建议后台运行。

### 4.3 媒体与链接文件

| 命令 | 作用 |
|------|------|
| `uv run crawl.py --download` | 下载所有 `pending` 状态的媒体文件直到队列清空 |
| `uv run crawl.py --download-fid FID` | 下载指定 fid 的单个媒体文件 |

> 爬取时**不自动下载媒体**——只把 fid/url 入库（status=`pending`），需显式 `--download`。

### 4.4 群与跳过管理

| 命令 | 作用 |
|------|------|
| `uv run crawl.py --list-groups` | 列出数据库中所有群（gid / 成员数 / 群名） |
| `uv run crawl.py --list-skip` | 列出不爬取的群 |
| `uv run crawl.py --add-skip-gid GID` | 加入不爬取列表 |
| `uv run crawl.py --remove-skip-gid GID` | 从不爬取列表移除 |

### 4.5 查询

| 命令 | 作用 |
|------|------|
| `uv run crawl.py --stats` | 打印数据库统计（消息数 / 群数 / 媒体数） |
| `uv run crawl.py --search 关键词 [--search-limit 100]` | FTS5 全文搜索消息，按时间倒序（默认返回 50 条） |

### 4.6 导出群成员发言（用于 AI 分析）

把指定群内某个成员本人的发言导出为 JSON / CSV，便于直接喂给大模型或程序解析。

| 命令 | 作用 |
|------|------|
| `uv run crawl.py --list-members --gid GID` | 列出该群发过言的成员（sender_id / 昵称 / 发言数 / 最近发言时间） |
| `uv run crawl.py --export --gid GID --sender-name 昵称` | 按昵称导出 |
| `uv run crawl.py --export --gid GID --sender-id 12345` | 按发送者 ID 导出（比昵称稳定，不受改名影响） |
| `... --since 2026-01-01 --until 2026-07-14` | 可选：限定时间范围（CST 口径，`--until` 为开区间） |
| `... --output <路径>` | 可选：指定输出路径（默认 `export_<gid>_<sender>.json`） |
| `... --compact` | 可选：输出紧凑 JSON，体积约减至 1/3~1/2（仅对 JSON 生效） |
| `... --format csv` | 可选：输出 CSV（表头 `sender_name,text`，UTF-8-SIG 编码，Excel 可直接打开） |

**导出说明：**

- 仅导出该成员**本人**发言，自动排除系统消息（入群/撤回/通知等）。
- 每条仅含 `sender_name` 与 `text` 两项；JSON 按时间升序，无 `meta` 包裹。
- 同一昵称在不同群可能对应不同人，故 `--export` 必须配合 `--gid`；不确定昵称时先用 `--list-members --gid GID` 查。
- `--sender-name` 与 `--sender-id` 至少给一个，可同时给（取交集）；0 条结果也会写出合法的空文件并在控制台提示。

---

## 5. 数据存储

所有数据均在项目目录下，不依赖任何外部服务。

| 类型 | 位置 | 说明 |
|------|------|------|
| 数据库 | `weibo_im.db` | SQLite + FTS5。消息、群、cookie、配置、媒体/链接文件清单 |
| 图片 | `media/images/` | 群聊图片 |
| 视频 | `media/videos/` | 群聊视频 |
| 附件/链接文件 | `media/files/` | 群附件 + PDF/ZIP/DOC 等链接文件 |
| 二维码截图 | `qrcode.png` | `--renew-cookie` 生成，可随时删 |

### 时区约定

- 消息时间戳存储为 UTC 毫秒，工具内部统一按 **CST(+08:00)** 处理：
  `--since` 日期按 CST 解析，`--search` 输出按 CST 格式化。
- 不论系统时区如何，时间口径一致。

---

## 6. 与原版（WeiboChatToWeChat）的差异

| 方面 | 原版（Hermes） | 本版本 |
|------|---------------|--------|
| 二维码下发 | 发到微信 | 截图存本地 / 有头弹窗 |
| 登录成功通知 | 微信消息 | 控制台日志 |
| Cookie 失效提醒 | 微信推送 | 控制台 ERROR + 退出提示 |
| 定时爬取 | Hermes cron | 系统计划任务（任务计划 / cron / launchd） |
| 运行平台 | Ubuntu ARM64 | Windows / macOS / Linux 通用 |
| 新增功能 | — | `--stats` / `--list-groups` / `--search` / 成员发言导出 / Web 查看器 |

---

## 7. 注意事项与已知限制

1. **cookie 会过期**。过期后消息接口会返回失败，届时重新跑 `--renew-cookie` 或 `--set-cookie`。
2. **FTS5 中文分词**。对中文是「逐字」索引——短词（1~2 字）能搜，整句匹配需用 `"短语"` 加引号；复杂检索建议直接用 `sqlite3` 写 SQL。
3. **回填耗时**。回填数月历史会持续翻页，建议后台运行。
4. **媒体不自动下载**。爬取只入库 fid/url，要下载需显式 `--download`。
5. **盲测边界有误差**。`--probe-boundary` 的结果在边界附近有抖动，足够定位入群时间，但不要当作精确值。
6. **WAL 模式**。运行后会多出 `weibo_im.db-wal` / `weibo_im.db-shm`，属正常现象，备份时一并复制。

---

## 8. 定时爬取（可选）

本版本不带调度器，用系统原生计划任务即可。

**Windows 任务计划（每 10 分钟）：**

```powershell
schtasks /create /tn "WeiboCrawl" /tr "cmd /c cd /d C:\path\to\weibogroup && uv run crawl.py >> crawl.log 2>&1" /sc minute /mo 10
```

**Linux/macOS cron：**

```cron
*/10 * * * * cd /path/to/weibogroup && uv run crawl.py >> crawl.log 2>&1
```

---

## 9. 快速自检

部署完成后，按顺序跑这几条命令确认一切就绪：

```bash
uv run crawl.py --check-playwright          # ① 浏览器环境
uv run crawl.py --renew-cookie              # ② 扫码登录
uv run crawl.py                             # ③ 首次爬取（当天消息）
uv run crawl.py --stats                     # ④ 看统计
uv run crawl.py --list-groups               # ⑤ 看群列表
uv run crawl.py --search 微博               # ⑥ 试搜
uv run crawl.py --download                  # ⑦ 下载媒体
```

任何一步报错，对照第 7 节排查。

---

## 10. 消息查看器（Web 前端）

本地只读 Web 查看器，浏览已抓取的群聊消息。零外部依赖，仅用 Python 标准库
+ 原生 HTML/JS/CSS。

### 启动

```bash
uv run server.py
```

默认访问 http://127.0.0.1:8765 。可选参数：`--db`（数据库路径）、`--host`、`--port`。

### 功能

- 左栏按月折叠的日期列表（带每天消息数），点某天查看当天消息
- 右栏聊天视图，上下滚动加载更早 / 更新消息
- 高级搜索：发送者（精确匹配）+ 关键词（模糊）组合查询，结果可跳转到主窗上下文
- 搜索时间范围默认最近 3 个月，可缩为 1 周 / 1 个月
- 媒体显示占位与原始链接（需带 cookie 才能访问，不在页面内加载）
- 跨天日期分隔条；系统消息（入群 / 撤回等）居中灰色显示

### 测试

```bash
uv run python -m unittest discover tests
```
