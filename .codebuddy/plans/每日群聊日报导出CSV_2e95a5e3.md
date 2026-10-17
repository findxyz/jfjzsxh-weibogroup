---
name: 每日群聊日报导出CSV
overview: 在现有 --export 之上新增「日报模式」，导出指定群+成员+日期的当日群聊全部消息（含目标成员发言和其他成员发言作为上下文）为 CSV，列含 created_at(CST)、sender_name、text、is_target(1/0)，文件命名为 daily_<gid>_<sender>_<YYYY-MM-DD>.csv，供后续 AI 生成日报使用。
todos:
  - id: add-export-functions
    content: 在 weibo_im/export.py 新增 get_daily_context_messages、_resolve_target_sender_id、export_daily_report 三个函数，实现当日群聊上下文 CSV 导出
    status: completed
  - id: add-cli-daily
    content: 在 crawl.py 新增 --daily 和 --date 参数及 _do_daily_export 分支，串联 CLI 到导出函数
    status: completed
    dependencies:
      - add-export-functions
---

## 产品概述

在现有微博群聊爬虫项目基础上，新增"每日群聊日报数据导出"功能。用户指定群聊、日期和成员，系统从已抓取的 SQLite 数据库中导出该群当日全部用户发言（含目标成员和其他成员），以 CSV 文件格式输出，供后续交给 AI 生成成员当日群聊总结日报。

## 核心功能

- 指定群聊(gid)、日期(date)、成员(sender_name 或 sender_id) 导出当日群聊数据
- 导出当日群内全部用户发言消息（msg_type 100/321），作为目标成员发言的上下文
- CSV 输出四列：created_at(CST时间字符串)、sender_name(发言者昵称)、text(消息正文)、is_target(1=目标成员发言, 0=其他成员发言)
- 文件命名：daily_<gid>_<sender>_<YYYY-MM-DD>.csv
- 通过 CLI 命令调用：`python crawl.py --daily --gid <gid> --date <YYYY-MM-DD> --sender-name <昵称>` 或 `--sender-id <ID>`

## Tech Stack

- 语言：Python 3（项目已用，net48 无关，纯 Python 脚本项目）
- 数据库：SQLite（已有 weibo_im.db，通过 weibo_im/db.py 访问）
- CLI 框架：argparse（已有 crawl.py 入口）
- CSV 输出：Python 标准库 csv，UTF-8-SIG 编码（与现有 export CSV 一致）
- 依赖管理：uv（项目已有 pyproject.toml + uv.lock）

## Implementation Approach

### 策略

在现有 `weibo_im/export.py` 模块中新增日报导出函数，在 `crawl.py` 中新增 `--daily` CLI 参数分支。完全复用现有的 DB 连接管理（set_db_path/init_db/get_conn）、群列表查询（get_group_list）、时间格式化（ms_to_cst）和日期解析（_parse_since）。

### 关键技术决策

1. **当日时间范围计算**：用 CST 时区算当日零点，不能简单用 `ts % 86400000`（那是 UTC 边界）。算法：将日期字符串解析为 CST 零点毫秒时间戳，end = start + 86400000，查询 `created_at >= start AND created_at < end`。
2. **消息类型筛选**：仅保留 msg_type IN (100, 321) 的用户发言，过滤系统消息(322/344)，与现有 `get_member_messages` 默认一致。
3. **is_target 判定**：优先用 sender_id 匹配（比 sender_name 稳定）。若用户传 --sender-name 未传 --sender-id，则先查该成员最近一条发言的 sender_id 作为目标 ID，再用此 ID 标记 is_target。
4. **CSV 编码**：utf-8-sig（BOM），Excel 直接打开中文不乱码，与现有 export CSV 一致。

### Performance

- 查询走 `idx_msg_gid(gid)` + `idx_msg_ctime(created_at)` 复合索引路径，单群单日消息量通常 < 1000 条，毫秒级返回，无性能瓶颈。
- 一次 SQL 查询拿到当日全部消息，Python 端逐行标记 is_target，O(n) 遍历，无 N+1 问题。

### Avoiding Technical Debt

- 不新建模块，在现有 export.py 中追加函数，保持项目结构一致。
- 复用现有 `_parse_since`、`ms_to_cst`、`get_group_list`，不重复造轮子。
- CLI 参数复用现有 `--gid`、`--sender-name`、`--sender-id`、`--output`，仅新增 `--daily` 和 `--date`。

## Implementation Notes

- 时区常量 CST 已在 export.py 定义（`timezone(timedelta(hours=8))`），直接复用。
- `_parse_since` 已支持 "2026-08-14" 格式解析为 CST 零点毫秒时间戳，但返回的是 since_ms（闭区间下界），日报需要当日范围 [start, start+86400000)，因此 --date 参数独立解析更清晰：直接用 datetime 构造 CST 零点。
- 若目标成员当日无发言但仍导出当日群聊消息，is_target 全为 0，CSV 仍合法。控制台应提示"目标成员当日无发言"。
- 文件名中的 sender slug 需做安全过滤（仅保留字母数字和 `._-`），与现有 `export_member_messages` 逻辑一致。
- 日期参数格式校验：支持 "2026-08-14" 和 "2026/08/14"，不支持时间戳（日报只按天）。

## Architecture Design

在现有三层架构（CLI入口层 → 导出层 → 持久层）中，仅扩展导出层：

```
crawl.py (--daily)
  └─ _do_daily_export(db_path, gid, date_str, sender_name, sender_id, output, log)
       └─ export.export_daily_report(db_path, gid, date_str, sender_name, sender_id, output_path)
            ├─ set_db_path / init_db           ← 复用 db.py
            ├─ _resolve_target_sender_id()      ← 新增：按 name 查 id
            ├─ get_daily_context_messages()     ← 新增：查当日全部用户发言
            ├─ get_group_list()                 ← 复用 db.py 取群名
            └─ csv.DictWriter                   ← 写 CSV (utf-8-sig)
```

## Directory Structure Summary

修改 2 个现有文件，不新增文件：

```
project-root/
├── crawl.py              # [MODIFY] 新增 --daily 和 --date 参数，新增 _do_daily_export 函数
├── weibo_im/
│   └── export.py          # [MODIFY] 新增 get_daily_context_messages / _resolve_target_sender_id / export_daily_report 函数
└── weibo_im.db            # [READ-ONLY] 只读查询，不修改
```

### 文件修改详情

**crawl.py** [MODIFY]

- argparse 新增 `--daily`（store_true）和 `--date`（字符串，格式 YYYY-MM-DD）
- 新增 `_do_daily_export(db_path, gid, date_str, sender_name, sender_id, output, log)` 函数：校验参数 → 调用 `export.export_daily_report` → 打印结果摘要
- main() 中在 `--export` 分支后新增 `--daily` 分支

**weibo_im/export.py** [MODIFY]

- 新增 `_resolve_target_sender_id(gid, sender_name, sender_id) -> int | None`：若 sender_id 已提供直接返回；否则查该群内该 sender_name 最近一条消息的 sender_id
- 新增 `get_daily_context_messages(gid, date_str, target_sender_id) -> list[dict]`：解析 date_str 为 CST 零点毫秒 → 查 messages 表 gid + created_at 范围 + msg_type IN (100,321) → ORDER BY created_at ASC → 返回 dict 列表
- 新增 `export_daily_report(db_path, gid, date_str, sender_name, sender_id, output_path) -> dict`：完整编排（init_db → resolve target → query → 查群名 → 写 CSV → 返回结果 dict）