# 增量同步工作流详解

## 一、为什么不用项目内置 `--diff`

项目 `crawl.py --diff --old 旧.csv --new 新.csv` 能对比两个导出包输出补充包，但本场景禁用，原因：

### 根因：导出函数丢弃了 mid
`weibo_im/export.py` 的 `build_export_json` 默认只保留 `sender_name` 与 `text`，**丢弃了消息唯一 ID `mid` 和时间戳 `created_at`**。于是 `compute_delta` 只能用 `(sender_name, text, 出现序号)` 当去重键——靠"文本是否相同"认消息。

### 三个硬伤
1. **重复文本失灵**：陈同辉有 3149 条一模一样的"我发布了新微博，快来围观吧！"和 3150 条纯 http 链接。diff 只能数"旧包 2000 条、新包 3149 条→补 1149 条相同文字"，吐出来的是 1149 条无身份无时间的重复行，知识库无法分辨。
2. **噪声过滤不一致算错账**：旧导出 chen_0715.csv 含噪声（"快来围观吧"、纯URL），而当前导出会自动过滤噪声。拿含噪旧包和去噪新包做文本 diff，计数对不上、occurrence 匹配错乱。
3. **丢失时间线**：delta 只有 `sender_name,text`，进知识库后无时间维度，对宏观策略时效性分析是损失。

## 二、mid 水位线方案（已实现）

### 原理
SQLite 里每条消息有唯一且单调递增的 `mid`（TEXT，18 位，字典序比较=时间序）。正确增量机制不是"导两份再比"，而是：
> 记水位线 = 已成功上传到知识库的最大 mid。每次只导 `mid > 水位线` 的新消息，上传成功后把水位线推进到新的 max_mid。

| 维度 | `--diff` | mid 水位线 |
|---|---|---|
| 认消息靠什么 | 文本内容 | 唯一 ID |
| 重复文本 | 失灵 | 不受影响 |
| 噪声过滤变化 | 算错 | 不受影响 |
| 重复上传 | 漏/重 | 零重叠零遗漏 |
| 失败重跑 | 难恢复 | 水位线不动，原样重导 |
| 需保留旧导出包 | 是 | 否 |

### 代码改动（export.py / crawl.py）
- `get_member_messages(since_mid)`：加 `mid>?` 过滤。
- `build_export_json(with_meta, slim)`：三种形态（默认 sender_name,text；with_meta 带 mid+created_at；slim 仅 created_at,text）。
- `export_member_messages(since_mid, with_meta, slim, watermark_file)`：透传，CSV 按形态选列，返回 `max_mid`。
- `_read_watermark` / `write_watermark`：读写 `_chen/.watermark.json`。
- crawl.py：`--since-mid` / `--with-meta` / `--slim` / `--watermark-file`。

## 三、上传格式：为什么用 `--slim`

mid 在文件里对 AI 分析无价值且浪费 token（4416 行 × ~19 字符 ≈ 8.4 万字符纯浪费）。关键认识：**水位线 max_mid 由导出命令返回值提供，与文件列无关**——命令会打印「本批最大 mid: X」，写入 .watermark.json 即可，文件里不必有 mid 列。

| 格式 | 列 | 用途 | 体积（4416 行） |
|---|---|---|---|
| `--with-meta` | mid,created_at,sender_name,text | 本地核验 | 696K |
| `--slim`（上传用） | created_at,text | 上传知识库 | 523K（-25%） |
| 默认 | sender_name,text | 备用 | — |

slim 砍掉 mid 和冗余 sender_name（单成员导出 sender 全相同），保留 created_at（宏观策略时效性必需）。

## 四、COS 上传细节

`create_media`（ima MCP）返回 STS 临时凭证 + cos_key。用 `cos-python-sdk-v5`（已 `uv add` 为项目依赖）PUT 上传：

- bucket：返回的 `bucket_name`（如 `ima-share-kb-1258344701`）
- region：返回的 `region`（如 `ap-shanghai`）
- Key：返回的 `cos_key`（不要加前导斜杠）
- 鉴权：`CosConfig(Region, SecretId, SecretKey, Token=...)`，Token 必传（STS）
- Body：文件字节，ContentType：与 create_media 的 content_type 一致（csv→text/csv）

凭证有效期约 12 小时，过期需重新 create_media。上传成功返回 ETag。脚本 `scripts/cos_upload.py` 封装此步，读 creds JSON 上传。

## 五、Bootstrap 基线（已完成，仅供参考）

首次运行前需确定基线水位线。本案例基线 = chen_0715.csv（7/15 导出，已导入 ima）的内容边界。因 chen_0715.csv 不含 mid，用日期反推：`max_mid = MAX(mid) WHERE created_at < '2026-07-15 00:00:00 CST'` = `5320695427109315`（7/14 21:35）。校验：该时刻前 DB 含噪 17219 条 ≈ chen_0715.csv 17173 行，对得上。

首批增量（mid > 5320695427109315）= 4416 条（7/15 07:12 ~ 8/20 20:23，过滤 1174 噪声），已于 2026-08-21 上传，水位线推进到 `5334085878683728`。

## 六、排错

- **CookieExpiredError / sync_groups 返回空**：cookie 过期，`uv run python crawl.py --renew-cookie` 扫码续期。
- **create_media 报错**：检查 file_size 与实际字节一致（`stat -f %z`）；content_type 必须按扩展名精确填（csv→text/csv），禁止 application/octet-stream 兜底。
- **COS 上传 403**：STS 凭证过期，重新 create_media 拿新凭证。
- **add_knowledge 后知识库看不到**：ima 解析异步，media_state 从 PARSING→SUCCESS 需要时间；用 get_knowledge_list 复查。
- **导出 0 条**：无新消息，正常结束，勿推进水位线。
- **水位线推进了但上传失败**：理论上不应发生（铁律是 add_knowledge 成功后才推进）。若误推进，把 .watermark.json 的 max_mid 改回上一值即可重跑该批。

## 七、可复现性
- cos-python-sdk-v5 已 `uv add` 声明为项目依赖（pyproject.toml / uv.lock），`uv sync` 即装。
- 代码改动在 export.py / crawl.py，已随项目版本管理。
- 水位线与产物在 `_chen/`，不随版本管理（.workbuddy 外的工作产物）。
