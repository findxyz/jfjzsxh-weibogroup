---
name: filter-daily-export-noise
overview: 在 weibo_im/export.py 的每日群聊日报导出（export_daily_report / get_daily_context_messages）中加入过滤逻辑，剔除三类噪声消息：sender_name 为「粉丝群」的系统提示、文本以「快来围观吧」或「我发布了新微博，快来围观吧！」开头的自动提示、以及纯 URL 网页分享发言。
todos:
  - id: add-noise-filter-func
    content: 在 export.py 新增 _is_noise_message 函数并集成到 export_daily_report 过滤逻辑
    status: completed
  - id: update-tests
    content: 更新 tests/test_export_api.py 增加噪声过滤测试用例
    status: completed
    dependencies:
      - add-noise-filter-func
---

## 需求概述

修改每日群聊日报导出功能（`weibo_im/export.py` 中的 `export_daily_report`），在导出 CSV 时自动剔除三类系统噪声消息：

## 核心功能

1. **剔除 sender_name 为「粉丝群」的条目**：这些是系统自动生成的提示消息（如"恭喜...获得标识"、"涉及资金问题请务必提高警惕，谨防诈骗"等），通过 sender_name 字段匹配过滤。
2. **剔除文本包含「快来围观吧」的条目**：微博分享时系统自动生成的提示消息，通常与纯 URL 消息成对出现。
3. **剔除纯网页 URL 的发言**：text 仅为一个 URL（以 `http://` 或 `https://` 开头），无其他文字内容。这类消息是微博分享时系统自动生成的纯链接，无实质分析价值。

过滤后的 count、target_count、context_count 统计数字应与实际导出条数一致。

## 技术栈

- Python 3（现有项目，无需引入新依赖）
- SQLite（通过 `weibo_im/db.py` 的 `get_conn` 查询）
- CSV 导出（`csv.DictWriter`，UTF-8-SIG 编码）

## 实现方案

### 策略

在 `export_daily_report` 函数中，查询返回结果后、组装 CSV 行之前，用 Python 层过滤替代 SQL 层过滤。原因：SQL 正则匹配不便、Python 层过滤逻辑清晰且易维护、便于后续扩展新的噪声规则。

### 核心设计

新增工具函数 `_is_noise_message(row: dict) -> bool`，封装三类过滤规则：

1. `sender_name == "粉丝群"` → 系统自动提示
2. `text.strip().startswith(("http://", "https://"))` 且去除 URL 后无其他文字 → 纯网页链接
3. `"快来围观吧" in text` → 微博分享自动提示

在 `export_daily_report` 中调用该函数过滤 rows，过滤后再计算 is_target 标记和统计数据。

### 关键接口

```python
def _is_noise_message(row: dict) -> bool:
    """判断消息是否为应剔除的系统噪声。

    规则：
    - sender_name == "粉丝群" → 系统自动提示
    - text 去首尾空白后以 http:// 或 https:// 开头 → 纯 URL 网页分享
    - text 包含 "快来围观吧" → 微博分享自动提示

    返回 True 表示该条消息应被剔除。
    """
```

## 实现注意事项

- **过滤位置**：在 `export_daily_report` 函数中、`rows = get_daily_context_messages(gid, date_str)` 之后、组装 `csv_rows` 循环之前，添加 `rows = [r for r in rows if not _is_noise_message(r)]`。
- **统计一致性**：过滤后 `len(csv_rows)` 自然反映过滤后条数，`target_count` 和 `context_count` 基于过滤后的数据计算，保证三者一致。
- **target_sname 反查影响**：过滤发生在 target_sname 反查逻辑之后（反查需要基于原始 rows），需确保过滤不影响反查。实际代码中反查在第 335-349 行，过滤应放在第 349 行之后、第 352 行 `csv_rows = []` 之前。
- **纯 URL 判定细节**：text 去除首尾空白后，以 `http://` 或 `https://` 开头即判定为纯 URL。不额外检查是否包含其他文字，因为微博分享的纯链接消息 text 就是 URL 本身（如 `http://t.cn/AXNj6YrQ`），不含其他文字。
- **不影响 SQL 查询**：`get_daily_context_messages` 函数本身不修改，保持 SQL 层仅做 msg_type 过滤。
- **日志**：过滤后在 log.info 中体现过滤条数，如 `已导出日报 %d 条消息（过滤 %d 条噪声）`。

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 探索 `export.py` 中 `export_daily_report` 的完整调用链，确认 server.py API 层是否直接消费返回的 count 等字段，确保过滤后统计数据一致性
- Expected outcome: 确认 server.py 的 `/api/export/daily` 端点直接透传 `export_daily_report` 返回值，无需额外修改 server.py