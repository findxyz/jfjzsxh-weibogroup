# 项目长期记忆 — jfjzsxh-weibogroup（微博群聊爬虫）

## 项目性质
微博群聊消息本地爬虫（SQLite + FTS5），CLI 入口 `crawl.py`，核心包 `weibo_im/`。
支持爬取/回填/媒体下载/全文搜索/成员发言导出/每日日报/增量对比。

## 关键约定
- **运行用 `uv run python crawl.py ...`**（项目 `.venv` 已就绪，含 requests/playwright）。
  托管 Python（`/Users/john/.workbuddy/binaries/python/...`）缺 requests，跑 `crawl.py` 会失败；但纯查询/导出可绕过 crawl.py 直接 import weibo_im.export（仅依赖 stdlib+sqlite3）。
- **时区**：业务统一 CST(+08:00)，消息时间戳存 UTC 毫秒。
- **mid**：消息唯一 ID，存 TEXT（18 位，超 int 范围），字典序比较=时间顺序，单调递增。

## 陈同辉→ima 增量导出工作流（2026-08-21 建立）
- 目标：把「陈同辉-宏观策略」(sender_id=1401164841) 在群 4778124434997704 的发言增量导入 ima「陈同辉」知识库(ID 7482714307449732)。
- **用 mid 水位线，不要用 `--diff`**：`--diff` 靠文本匹配去重，重复文本/噪声过滤变化会失灵；水位线靠 mid 唯一 ID，零重叠零遗漏。
- 水位线文件 `_chen/.watermark.json`，基线=5320695427109315（chen_0715.csv 7/15 导出前 DB 顶点）。
- 导出命令见当日日志；上传成功后才 `write_watermark` 推进，失败不推进。
- 代码改动：`export.py` 加 `since_mid/with_meta/watermark_file/slim`；`crawl.py` 加 `--since-mid/--with-meta/--slim/--watermark-file`。
- **skill 已建**：项目级 `chen-tonghui-ima-sync`（.workbuddy/skills/），含 cos_upload.py 脚本。下次说"更新陈同辉知识库"即触发。
