---
name: chen-tonghui-ima-sync
description: 将微博群聊爬虫项目(jfjzsxh-weibogroup)中「陈同辉-宏观策略」成员的发言，用 mid 水位线增量同步到 ima「陈同辉」知识库——只补新消息、不重不漏、上传版省 token。当用户说"更新陈同辉知识库""陈同辉增量上传ima""同步陈同辉发言到ima""把陈同辉群里新发言传到知识库"或类似表达时使用本 skill。本 skill 紧耦合 /Users/john/jfjzsxh-weibogroup 项目（crawl.py、_chen/、weibo_im.db）。
agent_created: true
---

# 陈同辉群聊增量同步 ima

## 用途
把 jfjzsxh-weibogroup 爬虫 SQLite（weibo_im.db）里「陈同辉-宏观策略」成员的新发言，增量上传到 ima「陈同辉」知识库。核心机制是 **mid 水位线**：记录已成功上传的最大 mid，每次只导出/上传 mid 更大的新消息，上传成功后才推进水位线。**不要用项目内置 `--diff`**（靠文本匹配去重，重复文本/噪声过滤变化会失灵，详见 references/workflow.md）。

## 关键常量
- 项目根：`/Users/john/jfjzsxh-weibogroup`
- 群 gid：`4778124434997704`（陈同辉-宏观策略的客服群）
- 成员 sender_id：`1401164841`（陈同辉-宏观策略）
- ima 知识库 ID：`7482714307449732`（名称「陈同辉」，可写）
- 水位线文件：`_chen/.watermark.json`（字段 `max_mid`）
- 运行方式：`uv run python crawl.py ...`（项目 .venv 已就绪；托管 python 缺 requests 会失败）

## 工作流（端到端 5 步）

### 1. 爬最新消息进 SQLite
```bash
cd /Users/john/jfjzsxh-weibogroup && uv run python crawl.py
```
若 cookie 过期（报 CookieExpiredError 或 sync_groups 返回空），先 `uv run python crawl.py --renew-cookie` 扫码续期，再爬。

### 2. 增量导出（slim 格式，省 token）
```bash
uv run python crawl.py --export --gid 4778124434997704 --sender-id 1401164841 \
  --watermark-file _chen/.watermark.json --slim --format csv \
  -o _chen/delta_$(date +%Y%m%d)_slim.csv
```
- `--watermark-file` 自动读水位线作为 `--since-mid`（文件不存在=全量，首次运行）。
- `--slim` 输出 `created_at,text` 两列（保留宏观策略时效性，去 mid 和冗余 sender_name，比 `--with-meta` 小约 25%）。
- **命令会打印「本批最大 mid: <X>」——记下这个值，第 5 步推进水位线用。** mid 由命令返回值追踪，不依赖文件内的 mid 列，所以上传文件用 slim（无 mid）即可。
- 若输出 0 条，说明无新消息，结束。

### 3. 上传到 COS（create_media → cos_upload.py）
先用 ima MCP 工具 `create_media` 拿 COS 上传凭证：
- `knowledge_base_id`: `7482714307449732`
- `file_name`: 形如 `陈同辉-宏观策略_发言增量_<起>-<止>.csv`（用导出的时间范围）
- `file_size`: CSV 实际字节数（`stat -f %z <file>`）
- `content_type`: `text/csv`，`file_ext`: `csv`

`create_media` 返回 `media_id` 与 `cos_credential`（含 token/secret_id/secret_key/bucket_name/region/cos_key）。把整个返回 JSON 存到临时文件，调本 skill 的脚本上传：
```bash
uv run python .workbuddy/skills/chen-tonghui-ima-sync/scripts/cos_upload.py \
  --creds /tmp/ima_creds.json --file _chen/delta_<日期>_slim.csv --content-type text/csv
```
脚本成功会打印 `OK_RESULT` 与 ETag。详见 references/workflow.md 的 COS 上传细节。

### 4. 入库知识库（add_knowledge）
用 ima MCP 工具 `add_knowledge`：
- `knowledge_base_id`: `7482714307449732`
- `media_id`: 第 3 步 create_media 返回的 media_id
- `duplicate_name_strategy`: `DUPLICATE_NAME_STRATEGY_SAVE`（保留，绝不覆盖基线）

返回 media_id 即入库成功。可用 `get_knowledge_list` 验证文档在库（解析进度会从 PARSING → SUCCESS）。

### 5. 推进水位线（仅在上传成功后）
```bash
uv run python -c "from weibo_im.export import write_watermark; write_watermark('_chen/.watermark.json', '<第2步打印的max_mid>', note='<本批条数与日期范围>已上传ima')"
```
**铁律：只在 add_knowledge 成功后才推进水位线。** 上传失败绝不推进，下次重跑同一批。

## 关键规则
1. **水位线靠 mid 不靠文本**：mid 是消息唯一 ID（单调递增，字符串字典序=时间序），`--since-mid` 精确去重；`--diff` 靠 `(sender_name,text,序号)` 会漏会重，禁用。
2. **上传用 `--slim` 不用 `--with-meta`**：mid 是 18 位无意义数字，浪费 AI token；且 max_mid 由命令返回值单独追踪，无需进文件。`--with-meta` 仅本地核验用。
3. **推进水位线在 add_knowledge 成功之后**：失败不推进，保证零遗漏、可重跑。
4. **基线**：`_chen/.watermark.json` 现值 `5334085878683728`（首批 4416 条 7/15-8/20 已上传）。基线前内容（chen_0715.csv，库里即 chen.csv）已在 ima，勿重传。
5. **重名 SAVE 不覆盖**：ima 重复上传同名会保留为新文档，不会覆盖已导入数据。

## 参考资源
- `references/workflow.md`：`--diff` 为何不可靠、水位线原理、slim 格式决策、COS 上传细节、bootstrap 基线、cookie 失效与排错。
- `scripts/cos_upload.py`：COS 上传脚本（读 create_media 返回的凭证，用 cos-python-sdk-v5 PUT）。

## 触发词
更新陈同辉知识库 / 陈同辉增量上传ima / 同步陈同辉发言到ima / 把陈同辉群里新发言传到知识库 / 陈同辉 ima sync / weibogroup ima incremental
