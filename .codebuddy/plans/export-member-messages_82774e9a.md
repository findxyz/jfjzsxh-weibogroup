---
name: export-member-messages
overview: 为已爬取的微博群聊数据新增「按群成员导出发言」功能：在 crawl.py 增加 --export 命令，按 --gid + 成员标识（昵称/ID）从 SQLite 查询该成员发言，输出为适合 AI 分析的 JSON 文件（含 meta 元信息与 messages 数组）。同时增加 --list-members 便于定位成员标识。
todos:
  - id: add-db-queries
    content: 在 db.py 新增 get_member_messages 与 get_members 只读查询
    status: completed
  - id: add-export-cli
    content: 在 crawl.py 新增 --export/--list-members 参数与 _do_export 编排函数
    status: completed
    dependencies:
      - add-db-queries
  - id: update-readme
    content: 更新 README.md 命令清单，补充导出与成员列示用法
    status: completed
    dependencies:
      - add-export-cli
  - id: add-tests
    content: 新增 tests/test_db_export.py 覆盖过滤与空结果用例
    status: completed
    dependencies:
      - add-db-queries
---

## 用户需求

将已爬取并存储在本地 SQLite 数据库中的群聊数据，按指定群、指定群成员，把该成员本人的发言导出为文件，用于后续 AI 分析。

## 产品概述

这是一个增量功能：在已有微博群聊爬虫（CLI 入口 `crawl.py` + 持久层 `weibo_im/db.py`）之上，新增「按群成员导出发言」能力。用户在命令行指定群（`--gid`）、成员（昵称或 ID），程序从 `messages` 表中筛选该成员在该群内本人发出的发言，输出为结构化的 JSON 文件，可直接喂给大模型或供程序解析。

## 核心功能

- 指定单个群（`--gid`）导出某一成员的发言
- 按成员识别：支持昵称（`--sender-name`）或稳定的发送者 ID（`--sender-id`，二选一或同时提供）
- 仅导出该成员本人发言（消息类型限定为普通消息 321 与微博分享 100，与现有 `server.py` 成员判定惯例一致），不含他人上下文
- 可选时间范围过滤：复用 `--since`，新增 `--until`（日期或毫秒时间戳，统一 CST 口径）
- 输出 JSON：含元信息（群、成员、条数、时间范围、过滤条件）+ 按时间升序排列的发言数组，每条含时间（UTC 毫秒与 CST 字符串两种）、发送者、消息类型、文本内容、媒体信息
- 默认输出到项目根目录 `export_<gid>_<sender>.json`，可用 `--output` 指定路径；控制台同步打印导出条数与时间范围摘要
- 同步新增 `--list-members --gid GID` 便于在 CLI 中查询准确昵称/ID

## 技术栈

- 语言：Python 3.11+（沿用现有项目，不引入新依赖）
- 存储：SQLite（已有 `weibo_im.db`），导出用标准库 `json` / `csv` 不涉及（本次仅 JSON）
- 架构：延续现有分层约定——`crawl.py` 仅做参数解析与流程编排，`weibo_im/db.py` 承载只读查询逻辑

## 实现方案

### 总体策略

在持久层 `db.py` 新增只读查询函数 `get_member_messages`（仿 `get_stats`/`get_group_list` 的 `get_conn()` + `sqlite3.Row` 写法），在 CLI 入口 `crawl.py` 新增 `--export` 分支（仿 `_do_search` 的「`set_db_path`+`init_db()` 后加分叉」模式），将查询结果组装为带元信息的 JSON 并落盘。

### 关键技术决策

1. **成员发言判定沿用 `server.py` 惯例**：`msg_type IN (100, 321)`（321=普通消息、100=微博分享），保证与消息查看器中「真实用户发言」语义一致，避免把系统消息（入群/撤回/通知等）混入导出。
2. **识别方式双支持**：优先 `sender_id`（稳定、不随改名变化），也可按 `sender_name`（用户可感知）。`get_member_messages(gid, sender_id=None, sender_name=None, since_ms=None, until_ms=None, msg_types=(100,321))` 中两者按传入情况拼接 WHERE 条件。
3. **时间口径统一 CST**：输入 `--since`/`--until` 复用现有 `_parse_since`（已支持日期/时间戳）；`created_at` 在库中为 UTC 毫秒，JSON 中同时输出 `created_at`（毫秒）与 `created_at_cst`（CST 字符串，格式 `%Y-%m-%d %H:%M:%S`），便于人读与 AI 解析。
4. **升序输出**：`ORDER BY created_at ASC`，使发言天然按时间线排列，契合对话/行为序列分析。
5. **纯增量、零副作用**：仅做 SELECT 查询与文件写出，不改动任何表结构、爬取逻辑或现有命令；导出文件生成在项目根目录（与 `media/`、`qrcode.png` 同级的运行时产物约定一致）。

### 性能与可靠性

- 查询命中现有 `(gid, created_at)` / `idx_msg_gid` 索引，单群单成员范围扫描无全表排序；消息量极大时仍可流式逐行拼装，内存可控（先收集 rows 再 json.dumps，群成员发言体量远低于全群，无性能风险）。
- 边界处理：未找到群、成员或 0 条消息时，仍输出合法 JSON（空 `messages` 数组 + 元信息），并打印提示，不抛异常中断。
- 参数校验：`--export` 必须带 `--gid`；成员标识（`--sender-name` / `--sender-id`）至少给一个，否则报错退出并打印用法。

## 实现注意事项

- 复用 `_parse_since` 处理 `--until`，不新增解析逻辑，避免口径分歧。
- JSON 字段精简且稳定：每条消息输出 `created_at`、`created_at_cst`、`sender_id`、`sender_name`、`msg_type`、`msg_type_name`、`text`、`media_type`、`media_orig_url`；图片/链接类消息 `text` 可能为空，但 `msg_type_name` 与 `media_orig_url` 已能标识内容类型，满足 AI 分析所需的最小信息集。不内嵌 `raw_json`（避免体积膨胀），保持导出文件「干净、可直接喂模型」。
- 控制台日志沿用项目 `logging` 约定（`%(asctime)s [%(levelname)s] %(name)s`），打印导出文件路径、条数、CST 起止时间。
- 不改 `server.py`、爬取链路及任何 `INSERT` 逻辑，确保向后兼容。

## 架构设计

无新增架构/模块，完全贴合现有分层：

```
用户 → crawl.py (参数解析/编排) → weibo_im.db.get_member_messages (只读 SELECT)
                               → 组装 JSON → 落盘 export_*.json
```

修改范围小、隔离清晰，后续若要支持「多群/CSV/带上下文」可在此分支平滑扩展。

## 目录结构

```
weibo_im/
└── db.py          # [MODIFY] 在读取区（约 429 行之后）新增 get_member_messages / get_members
                   #   get_member_messages: 按 gid + (sender_id|sender_name) + msg_type IN (100,321)
                   #     + 可选 since_ms/until_ms 过滤，ORDER BY created_at ASC，返回 list[dict]
                   #   get_members: 照搬 server.py query_members_list 的 SQL（sender_id/sender_name/
                   #     msg_count/last_msg_at），供 --list-members 使用

crawl.py           # [MODIFY] 在 main() 的参数定义中新增 --export / --sender-name / --sender-id /
                   #   --output(-o) / --until；在命令分叉区新增 --list-members 与 --export 分支；
                   #   新增 _do_export(db_path, gid, sender_name, sender_id, since, until, output)
                   #     与 _print_members / do_list_members 辅助函数，组装 JSON 并写文件

README.md          # [MODIFY] 第 4 节「命令清单」增补 --list-members 与 --export 用法示例，
                   #   说明参数、默认输出路径与 JSON 结构

tests/
└── test_db_export.py  # [NEW] 基于内存 SQLite 的 unittest：构造群与多类型消息，验证
                   #   get_member_messages 的 gid/成员/时间范围/msg_type 过滤正确性与空结果行为
```

## 关键代码结构

```python
# weibo_im/db.py —— 新增只读查询
def get_member_messages(
    gid: int,
    sender_id: int | None = None,
    sender_name: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    msg_types: tuple[int, ...] = (100, 321),
) -> list[dict]:
    """返回指定群内某成员本人发言（默认普通消息+微博分享），按时间升序。"""

def get_members(gid: int) -> list[dict]:
    """返回该群内发过言的成员列表（sender_id/sender_name/msg_count/last_msg_at）。"""
```