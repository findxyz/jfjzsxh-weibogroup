---
name: export-web-gui
overview: 在现有 web 查看器中新增"导出"功能页签，把 weibo_im/export.py 的三个核心能力（成员列表、成员发言导出 JSON/CSV、每日群聊日报 CSV）做成可视化 GUI：选群→选成员→选时间范围/日期→预览→下载文件。后端在 server.py 新增 4 个只读 API（成员列表已有 /api/members 直接复用；新增预览 + 导出文件下载接口），前端在 web/ 下新增独立的 export 页面与脚本，复用现有 CSS 风格，零新增依赖。
design:
  architecture:
    framework: html
  styleKeywords:
    - Functional
    - Clean
    - Consistent
  fontSystem:
    fontFamily: system-ui
    heading:
      size: 18px
      weight: 600
    subheading:
      size: 14px
      weight: 500
    body:
      size: 13px
      weight: 400
  colorSystem:
    primary:
      - "#1a73e8"
      - "#1557b0"
    background:
      - "#f5f5f5"
      - "#fafafa"
      - "#ffffff"
    text:
      - "#333333"
      - "#555555"
      - "#888888"
    functional:
      - "#fff3a0"
      - "#4caf50"
      - "#dc3545"
todos:
  - id: export-api
    content: 在 server.py 新增 POST /api/export/member、POST /api/export/daily、GET /api/export/download 三个路由及 token 管理逻辑
    status: completed
  - id: export-frontend
    content: 创建 web/export.html、export.js、export.css 导出页面，并在 index.html 顶栏加入口链接
    status: completed
    dependencies:
      - export-api
  - id: export-tests
    content: 创建 tests/test_export_api.py 覆盖导出流程、token 下载与参数校验
    status: completed
    dependencies:
      - export-api
---

## 用户需求

将 `weibo_im/export.py` 导出模块的功能做成一个 Web GUI 界面，让用户在浏览器中完成所有导出操作，无需命令行。

## 产品概述

在现有消息查看器（`server.py` + `web/`）基础上新增「数据导出」页面，提供两个核心功能：成员发言导出（JSON/CSV）和每日群聊日报导出（CSV）。用户通过 Web 界面选择群、成员、日期范围等参数，预览导出数据，并下载结果文件。界面复用现有群列表/成员列表 API，风格与现有查看器一致。

## 核心功能

- 成员发言导出：选群→自动加载成员列表→选成员→可选时间范围→选格式(JSON/CSV)→可选紧凑模式→执行导出→预览数据→下载文件
- 每日群聊日报导出：选群→选成员→选日期→执行导出→预览当日消息(目标标记高亮)→下载 CSV
- 导出结果展示：条数、时间范围、群名等元信息，数据预览(前 N 条)
- 文件下载：直接通过浏览器下载生成的文件

## Tech Stack

- 后端：Python 标准库（`http.server`），复用现有 `server.py` 架构
- 前端：原生 HTML/CSS/JS，零框架依赖，与现有 `web/` 一致
- 数据层：复用 `weibo_im.export` 模块（`export_member_messages` / `export_daily_report` / `list_members` / `get_members`）

## Implementation Approach

**策略**：在现有 `server.py` 中新增导出相关 API 端点，前端新增独立页面 `web/export.html` + `web/export.js` + `web/export.css`，与消息查看器并存。

**后端新增 3 个 API**：

1. `POST /api/export/member` — 成员发言导出。接收 JSON body（gid, sender_name/sender_id, since, until, fmt, compact），调用 `export_member_messages`，返回结果元信息 + 预览数据 + 下载令牌
2. `POST /api/export/daily` — 日报导出。接收 JSON body（gid, date_str, sender_name/sender_id），调用 `export_daily_report`，返回结果 + 预览 + 下载令牌
3. `GET /api/export/download?token=xxx` — 文件下载。用内存字典暂存导出文件路径（token→path），设短 TTL，防目录穿越

**关键设计决策**：

- **export 模块会调 `set_db_path`+`init_db`**：这是 thread-local 连接，与 server 的只读 conn 互不干扰（SQLite WAL 支持并发读写）。但需注意 `_DB_PATH` 是全局变量——export 函数会覆写它指向正确库，由于 server 每次请求都传 `db_path`，不会出问题
- **文件路径安全**：export 函数返回绝对路径，需通过 token 映射而非直接暴露路径给前端，避免目录穿越攻击
- **时间范围**：前端传 `YYYY-MM-DD` 日期字符串，后端复用 `server._cst_day_bounds` 转为毫秒，保持与现有 `[start, end)` 约定一致
- **预览数据**：导出后读取文件内容前 50 条返回给前端展示，避免用户盲下载
- **单飞控制**：导出操作可并发（只读 DB + 写文件），不需要像 sync 那样限制单飞

## Implementation Notes

- **性能**：`export_member_messages` 对大群可能返回数万条消息。预览仅取前 50 条，文件写入用流式 `csv.DictWriter`，内存峰值可控。JSON 导出用 `json.dump` 一次性写入（已有实现），大数据量时可接受
- **时区一致性**：前端日期选择器输出 `YYYY-MM-DD`，后端用 `_cst_day_bounds` 转 ms，与现有 `/api/messages/by_date` 路径完全一致。`since` 闭区间、`until` 开区间的约定通过 `_cst_day_bounds(date)` 自然满足（end_ms 为次日零点）
- **DB 连接隔离**：`weibo_im.db._DB_PATH` 是全局变量，export 函数会覆写它。由于 server 的只读 conn 在初始化时已建立（指向正确的 `db_path`），不受影响。但 export 函数内部调 `set_db_path(db_path)` 传入的是 `Handler.db_path`（即 server 启动时绑定的库路径），保证一致
- **文件清理**：导出文件写入项目根目录（与 CLI 行为一致），下载 token 字典设 5 分钟 TTL，过期后 token 失效但文件保留（用户可手动清理）
- **错误处理**：`export_member_messages` 和 `export_daily_report` 已有完善的参数校验（返回空列表也写合法文件）。API 层需捕获 `ValueError`（如日期解析失败）返回 400
- **向后兼容**：不修改现有任何 API 和前端页面，纯新增

## Architecture Design

```
浏览器
  │
  ├── / (现有消息查看器 index.html)
  │
  └── /export.html (新导出页面)
        │
        ▼
  server.py Handler
        │
        ├── POST /api/export/member  ──→ weibo_im.export.export_member_messages
        ├── POST /api/export/daily   ──→ weibo_im.export.export_daily_report
        ├── GET  /api/export/download?token=  ──→ token_map[token] → 读取文件返回
        ├── GET  /api/groups          ──→ 现有 query_groups (复用)
        └── GET  /api/members?gid=    ──→ 现有 query_members_list (复用)
```

## Directory Structure

```
项目根/
├── server.py              # [MODIFY] 新增 3 个导出 API 路由 + token 管理逻辑
├── web/
│   ├── export.html       # [NEW] 导出页面 HTML，含两个导出面板(成员发言/日报)
│   ├── export.js         # [NEW] 导出页面 JS，处理表单交互、API 调用、预览渲染、下载
│   ├── export.css        # [NEW] 导出页面样式，复用现有 CSS 变量和配色
│   ├── index.html        # [MODIFY] 顶栏新增「数据导出」入口链接
│   ├── app.js            # [不修改]
│   └── style.css         # [不修改]
└── tests/
    └── test_export_api.py # [NEW] 导出 API 单元测试，覆盖正常导出/空结果/参数校验/token下载
```

### 文件详细说明

**server.py [MODIFY]**

- 新增模块级 `_export_tokens: dict[str, dict]`（token → {path, created_at}）和 TTL 常量（300s）
- 新增 `_generate_token()` 生成短随机 token
- 新增 `_register_export(path) -> token`：存入字典并返回 token
- 新增 `_resolve_export_token(token) -> path|None`：校验 TTL，过期返回 None
- `Handler.do_POST` 新增路由：`/api/export/member` 和 `/api/export/daily`，读取 JSON body，调用对应 export 函数，读取生成文件前 50 行预览，注册 token，返回 `{path, count, start_cst, end_cst, group_name, preview, download_token}`
- `Handler._route_api` 新增 `/api/export/download` 路由：校验 token，读取文件返回（Content-Disposition: attachment; filename=...）
- `Handler.do_GET` 新增 `/export.html` 静态文件路由（或复用 `_serve_static`）
- 复用现有 `_cst_day_bounds(date)` 将日期字符串转为 ms，传给 `export_member_messages` 的 `since_ms`/`until_ms`

**web/export.html [NEW]**

- 顶部导航：群选择下拉框（复用 `/api/groups`）、「返回消息查看器」链接
- Tab 切换：成员发言导出 / 每日群聊日报
- 成员发言导出 Tab：成员下拉框（选群后自动加载）、时间范围(起止日期)、格式选择(JSON/CSV)、紧凑模式复选框、导出按钮
- 日报导出 Tab：成员下拉框、日期选择器、导出按钮
- 结果区域：元信息卡片(条数/时间范围/群名)、数据预览表格(前 50 条)、下载按钮

**web/export.js [NEW]**

- 页面状态管理：当前 gid、成员列表、导出结果
- `loadGroups()`：调 `/api/groups` 填充群下拉
- `loadMembers(gid)`：调 `/api/members?gid=` 填充成员下拉
- `doExportMember()`：收集表单参数 POST `/api/export/member`，渲染预览
- `doExportDaily()`：收集参数 POST `/api/export/daily`，渲染预览（is_target=1 高亮）
- `downloadFile(token)`：`window.location = /api/export/download?token=xxx`
- 复用 `escapeHtml` 工具函数

**web/export.css [NEW]**

- 复用现有配色体系（#1a73e8 主色、#f5f5f5 背景、#ddd 边框）
- Tab 栏、表单卡片、预览表格、结果信息卡片样式

**web/index.html [MODIFY]**

- 顶栏新增 `<a href="/export.html">数据导出</a>` 链接按钮

**tests/test_export_api.py [NEW]**

- 基于 `_ServerTestBase`，测试：成员导出正常流程返回元信息和预览、日报导出含 is_target 标记、token 下载返回正确文件和 Content-Disposition、参数校验缺 gid 返回 400、空结果也返回合法文件

## 设计风格

采用与现有消息查看器一致的功能性设计风格，简洁实用。使用原生 HTML/CSS/JS，零外部依赖。页面布局为顶部导航栏 + 左右分栏 Tab 切换式导出面板 + 底部结果预览区。配色沿用现有体系（主色 #1a73e8，背景 #f5f5f5/#fafafa，边框 #ddd/#eee）。表单元素使用圆角输入框和蓝色主按钮，预览数据以表格形式展示，日报导出的目标成员行用浅黄色高亮区分。整体风格克制、信息密度适中，便于快速完成导出操作。