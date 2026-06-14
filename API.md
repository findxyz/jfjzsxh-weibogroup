# 微博 WebIM 接口规范文档

> 本文档是**与实现无关的接口契约**：每个接口都给出 URL、方法、入参、出参结构、前置条件、错误处理、频率限制。可作为 Python 之外（Java / Go / Rust / Node）的对接蓝本。
>
> 所有内容来自 `D:\weibogroup\weibo_im\crawler.py`、`parser.py`、`media.py`、`crawl.py` 的实际实现，并已逐一对照源码行号。

---

## 目录

- [0. 全局约定](#0-全局约定)
- [1. 接口清单（一览）](#1-接口清单一览)
- [2. 接口详细规范](#2-接口详细规范)
  - [2.1 扫码登录](#21-扫码登录-web-页面非-json-接口)
  - [2.2 获取群聊列表](#22-获取群聊列表)
  - [2.3 获取群聊消息（翻页）](#23-获取群聊消息翻页)
  - [2.4 下载媒体文件](#24-下载媒体文件)
- [3. 公共数据结构](#3-公共数据结构)
- [4. 错误与频率处理规范](#4-错误与频率处理规范)
- [5. 类型码对照表](#5-类型码对照表)
- [6. Cookie 字段说明](#6-cookie-字段说明)
- [7. 跨语言对接清单](#7-跨语言对接清单)

---

## 0. 全局约定

### 0.1 域名

| 用途 | 域名 | 说明 |
|------|------|------|
| API 服务 | `api.weibo.com` | 群列表、消息、登录页 |
| 媒体下载 | `upload.api.weibo.com` | 图片/视频/文件二进制 |

### 0.2 固定参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `source` | `209678993` | Web 客户端标识，几乎所有接口都要带 |
| `t` | 当前毫秒时间戳 | 防缓存，每次请求生成 |

### 0.3 公共请求头（所有 API 接口）

```
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
            (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Encoding: gzip, deflate, br
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
Origin: https://api.weibo.com
Referer: https://api.weibo.com/webim/
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
Cookie: <见 §0.5>
```

### 0.4 公共请求头（媒体下载）

```
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
            (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
Origin: https://web.im.weibo.com       # ⚠️ 与 API 不同
Referer: https://web.im.weibo.com/      # ⚠️ 与 API 不同
Cookie: <同 API>
```

### 0.5 Cookie 要求

所有受保护接口都需要登录 Cookie。**最少**需要 `SUB` 和 `SUBP` 两个键：

| Cookie 键 | 必需 | 说明 |
|-----------|------|------|
| `SUB` | ✅ | 主会话票据，失效后所有接口都拒绝 |
| `SUBP` | ✅ | 辅助票据 |
| `SSOLoginState` | 推荐 | 登录时间戳，某些风控判断会参考 |
| `_T_WM` / `ALF` / `SRF` / `SCF` 等 | 自动 | 扫码登录后会自动写入，无需关心 |

> **判断 Cookie 是否有效**：调用 §2.2 群列表接口，返回 200 且 `contacts` 数组非空即有效。

### 0.6 HTTPS / SSL

```python
verify=False   # ⚠️ 微博证书链在某些客户端下校验会失败，沿用原实现
urllib3.disable_warnings()
```

### 0.7 编码与时间

- 请求/响应均为 UTF-8。
- 所有时间戳字段（`t` 参数、响应里的 `time`）都是 **毫秒级**，如果值 < 1_000_000_000_000（13 位）则需 ×1000。
- 业务时区统一 CST(+08:00)。

---

## 1. 接口清单（一览）

| # | 名称 | 方法 | URL | 鉴权 | 用途 |
|---|------|------|-----|------|------|
| 1 | 扫码登录页 | GET | `https://api.weibo.com/chat` | 无 | 取得 Cookie |
| 2 | 群聊列表 | GET | `https://api.weibo.com/webim/2/direct_messages/contacts.json` | Cookie | 列出所有群 |
| 3 | 群消息查询 | GET | `https://api.weibo.com/webim/groupchat/query_messages.json` | Cookie | 翻页拉消息 |
| 4 | 媒体下载 | GET | `https://upload.api.weibo.com/2/mss/msget` | Cookie | 下载图片/视频/文件 |

> **接口顺序**：先用 ① 拿 Cookie → 用 ② 拉群列表 → 用 ③ 翻页拉每个群的消息 → 用 ④ 下载消息引用的媒体。

---

## 2. 接口详细规范

### 2.1 扫码登录 (Web 页面，非 JSON 接口)

**用途**：取得一个微博登录态 Cookie，供后续三个 JSON 接口使用。

| 项 | 值 |
|----|-----|
| URL | `https://api.weibo.com/chat` |
| 方法 | `GET` |
| 鉴权 | 无 |
| 返回 | HTML 单页应用（SPA） |

**流程（必须用无头浏览器模拟，不是简单 HTTP）：**

1. 用 Playwright / Selenium / Puppeteer 打开 URL。
2. 页面会自动跳到二维码登录界面（hash 路由，URL 中出现二维码区）。
3. 等待用户用微博 APP 扫码 + 手机端确认。
4. 登录成功后页面 hash 路由跳转：URL 从登录态变成 `https://api.weibo.com/chat#/chat`（即 `location.href` 含 `#/chat`）。
5. 此时浏览器 Cookie 容器已含 `.weibo.com` 域下的 `SUB`、`SUBP` 等。
6. 提取所有 `domain` 以 `.weibo.com` 结尾的 Cookie，拼成 `k1=v1; k2=v2` 字符串存库。

**判定登录成功的判据（源码 `crawl.py:140-141`）：**

```python
current_href = page.evaluate("window.location.href")
# 已登录: "https://api.weibo.com/chat#/chat"
# 未登录: "https://api.weibo.com/chat" 或带其他 hash
```

> ⚠️ **必须用 `page.evaluate("window.location.href")` 而不是 `page.url`**：该页面是 hash 路由 SPA，`page.url` 不含 hash，检测不到 `#/chat`。

**前置条件**

- 安装无头浏览器（Playwright + Chromium）。
- 用户已安装微博 APP 且账号可登录。
- 不需要预先有任何 Cookie。

**注意事项**

| 问题 | 处理 |
|------|------|
| 反爬检测 | `--disable-blink-features=AutomationControlled` 启动参数，否则部分环境下二维码不显示 |
| 无桌面环境 | 用 headless 模式 + 截图二维码到本地文件，用图片查看器打开 |
| User-Agent | 必须伪装为桌面 Chrome（实现里用 Windows Chrome），移动 UA 会被重定向到 H5 页面拿不到 Web Cookie |
| 二维码有效期 | 约 60-120 秒，实现里轮询 120 秒超时 |
| Cookie 有效期 | `SUB` 大约几天到几周（微博策略不固定），过期需重扫 |

**输出：** 一个 cookie 字符串（不存文件，直接进数据库 config 表）。

---

### 2.2 获取群聊列表

| 项 | 值 |
|----|-----|
| URL | `https://api.weibo.com/webim/2/direct_messages/contacts.json` |
| 方法 | `GET` |
| 鉴权 | Cookie（需含 `SUB`、`SUBP`） |
| 返回 | JSON |

**Query 参数**

| 参数 | 类型 | 必需 | 默认/示例 | 说明 |
|------|------|------|-----------|------|
| `special_source` | string | 是 | `3` | 固定 |
| `add_virtual_user` | string | 是 | `3,4` | 固定 |
| `is_include_group` | string | 是 | `0` | 固定 |
| `need_back` | string | 是 | `0,0` | 固定 |
| `is_include_folder` | string | 是 | `1` | 固定 |
| `count` | string | 是 | `50` | 每页数量（实测上限较高，通常一次返回全部群） |
| `source` | string | 是 | `209678993` | §0.2 |
| `t` | string | 是 | `1700000000000` | 当前毫秒时间戳 |

**成功响应（200）**

```json
{
  "contacts": [
    {
      "user": {
        "id": 4761715839862414,
        "type": 2,
        "name": "群名示例",
        "member_count": 42,
        "max_member_count": 200,
        "avatar_large": "https://...",
        "round_avatar_large": "https://...",
        "creator": 1234567890,
        "description": "群简介",
        "group_type": 0,
        "super_group_type": 0,
        "group_status": 0,
        "validateType": 0
      }
    }
  ]
}
```

**字段语义**（`user.type == 2` 才是群，其他是单聊）

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | int | **gid**，群唯一标识，后续接口的 `id` 参数 |
| `type` | int | 2 = 群聊，其他忽略 |
| `name` | string | 群名 |
| `member_count` | int | 当前成员数 |
| `max_member_count` | int | 群上限 |
| `avatar_large` | url | 群头像（方图） |
| `round_avatar_large` | url | 群头像（圆图） |
| `creator` | int | 群主 uid |
| `description` | string | 群简介 |
| `group_type` | int | 群类型码 |
| `super_group_type` | int | 超级群类型码 |
| `group_status` | int | 群状态 |
| `validateType` | int | 入群验证类型 |

**前置条件**

- 已通过 §2.1 获得有效 Cookie。

**注意事项**

| 场景 | 表现/处理 |
|------|----------|
| Cookie 失效 | 返回 200 但 `contacts` 为空数组，或返回 302 重定向到登录页 |
| 频率过高 | 未实测到限流，但建议两次调用间隔 ≥ 1 秒 |
| 成员数刷新 | `member_count` 是实时的，每次调用群列表都会刷新 |

**分页**：实测 `count=50` 通常一次返回所有群。如果群数量极大需要翻页（本实现未实现翻页，群多时需扩展 `page` 参数，但微博接口未公开分页机制）。

---

### 2.3 获取群聊消息（翻页）

> ⭐ **核心接口**。爬虫 90% 的时间花在这里。

| 项 | 值 |
|----|-----|
| URL | `https://api.weibo.com/webim/groupchat/query_messages.json` |
| 方法 | `GET` |
| 鉴权 | Cookie |
| 返回 | JSON |

**Query 参数**

| 参数 | 类型 | 必需 | 默认/示例 | 说明 |
|------|------|------|-----------|------|
| `id` | string | 是 | `4761715839862414` | 群 gid（来自 §2.2） |
| `count` | string | 是 | `50` | 每页消息数，**推荐 50**（实测过大可能被截断） |
| `max_mid` | string | 否 | — | **翻页游标**：返回比这个 mid **更早**的消息（不含自身）。**省略 = 取最新** |
| `convert_emoji` | string | 是 | `1` | 把微博私有 emoji 编码转成 Unicode |
| `query_sender` | string | 是 | `1` | 带上发送者用户对象 |
| `source` | string | 是 | `209678993` | §0.2 |
| `t` | string | 是 | `1700000000000` | 当前毫秒时间戳 |

**关键：返回顺序 ⚠️**

**返回的消息按「从旧到新」(oldest first) 排列**：

```
msgs[0]   = 这一页里最旧的消息  →  翻页游标（传给 max_mid 取更早）
msgs[-1]  = 这一页里最新的消息  →  停止条件判定
```

这与直觉相反（直觉以为新→旧），**翻译成其他语言时务必遵守这个顺序假设**。

**翻页算法（增量模式）**

```
1. 读取本地已知 max_mid（上次拉到的最新 mid）
2. cursor = "" (从最新开始)
3. loop:
     msgs = fetch_messages(gid, max_mid=cursor or None)
     if not msgs: break
     # 整页都在已知范围内？
     if msgs[-1].id <= local_max_mid: break
     # 入库 msgs 中 id > local_max_mid 的部分
     for m in msgs if m.id > local_max_mid: save(m)
     if len(msgs) < 50: break   # 没下一页
     cursor = msgs[0].id        # 翻到更早
     sleep(0.3s)
```

**翻页算法（回填模式，向更早翻）**

```
1. cursor = start_mid 或 ""
2. loop:
     msgs = fetch_messages(gid, max_mid=cursor or None)
     if not msgs: break
     for m in msgs (从新到旧遍历):
       if m.time < since_time: 停止
       else: save(m)
     cursor = msgs[0].id
     if len(msgs) < 50: break
     sleep(1.0s)   # 回填节奏更慢
```

**成功响应（200）**

```json
{
  "result": true,
  "messages": [
    {
      "id": "5302496155143676",
      "type": 321,
      "content": "消息文本",
      "media_type": 0,
      "time": 1718000000,
      "from_user": {
        "id": 1234567890,
        "screen_name": "发送者昵称"
      },
      "fids": ["5302496155143676_file"],
      "url_objects": [],
      "pic_infos": [],
      "template": "",
      "data": {}
    }
  ]
}
```

**字段语义**

| 字段 | 类型 | 含义 | 备注 |
|------|------|------|------|
| `id` / `idstr` | string/int | **mid**，消息唯一标识 | 单调递增，可用于排序/比较 |
| `type` | int | 消息类型码 | 见 §5.1 |
| `content` | string | 文本内容 | 系统/媒体消息可能为空 |
| `media_type` | int | 媒体类型码 | 见 §5.2 |
| `time` | int | 发送时间 | **秒级**（< 1e12 时 ×1000） |
| `from_user` | object | 发送者 | `id` + `screen_name` |
| `fids` | string[] | 文件 ID 列表 | `fids[0]` 用于媒体下载 §2.4 |
| `url_objects` | object[] | 卡片分享 | 详见 §3.3 |
| `pic_infos` | object/array | 小程序图片 | 详见 §3.4 |
| `template` | string | 系统消息模板文本 | 见 §3.5 |
| `data` | object | 模板变量 / 撤回 / 点赞等 | 视 type 不同 |

**返回 `result=false` 时**：表示接口拒绝（cookie 过期 / 被限流 / gid 错），实现里返回空数组（`crawler.py:191-193`）。

**前置条件**

- Cookie 有效。
- `id` 是已加入的群（否则 `result=false`）。

**注意事项**

| 场景 | 表现/处理 |
|------|----------|
| Cookie 失效 | 200 但 `result: false`，或 302 跳登录 |
| 限流 | 短时间高频会 429，详见 §4 |
| 频率建议 | 每页间 **≥ 0.3s**（增量）/ **≥ 1.0s**（回填大量历史） |
| 两年限制 | 微博服务端只保留 ~2 年消息，更早返回空页 |
| `max_mid` 越界 | 不报错，返回空数组 |

---

### 2.4 下载媒体文件

| 项 | 值 |
|----|-----|
| URL | `https://upload.api.weibo.com/2/mss/msget` |
| 方法 | `GET` |
| 鉴权 | Cookie（同 API 域名） |
| 返回 | 二进制流（图片/视频/文件） |

**URL 构造**（来自 `parser.py:31-35`）

```
基础: https://upload.api.weibo.com/2/mss/msget?fid={fid}&source=209678993
图片(媒体类型=1)附加: &imageType=origin
其他媒体类型: 不附加
```

**Query 参数**

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `fid` | string | 是 | 文件 ID（来自消息的 `fids[0]`） |
| `source` | string | 是 | `209678993` |
| `imageType` | string | 否 | 仅图片传 `origin` 取原图，否则取缩略图 |

**响应头（关键）**

| 头 | 用途 |
|----|------|
| `Content-Type` | 识别真实类型（PDF/ZIP/JPG...），代码据此修正扩展名 |
| `Content-Disposition` | 含原始文件名（`filename="报告.pdf"`），优先用这个命名 |

**成功响应（200）**：二进制流，按 chunk 写入文件。

**错误处理**

| 状态 | 含义 | 处理 |
|------|------|------|
| 200 | 成功 | 写文件 |
| 4xx | fid 错误 / 权限不足 | 标记 `failed`，不重试 |
| 5xx | 服务端错 | 可重试 |
| 网络异常 | 连接断 | 可重试 3 次 |

**前置条件**

- Cookie 有效。
- `fid` 来自先前抓取的消息。

**注意事项**

| 问题 | 处理 |
|------|------|
| 文件类型识别 | 下载后用 Content-Type / Content-Disposition 修正扩展名（实现见 `media.py:_patch_ext_from_response`） |
| 大文件 | 视频可达数百 MB，用 `stream=True` 分块下载 |
| 视频省流 | 项目支持 `--no-video`，把视频标 skipped 不下载 |
| 已下载 | 本地文件存在且 >0 字节则跳过（幂等） |
| 红包 (media_type=13 且内容含"红包") | 不下载，本地写一个占位文件 |
| 频率 | 实现里每下载一个文件 sleep 0.3s |

---

## 3. 公共数据结构

### 3.1 消息统一格式（解析后）

`parser.parse_message(raw)` 输出的标准字典，跨接口共用：

```python
{
  "mid":             str,      # 消息 ID（字符串，可数值比较）
  "gid":             int,      # 群 ID
  "msg_type":        int,      # 消息类型码 §5.1
  "msg_type_name":   str,      # 类型中文名
  "media_type":      int,      # 媒体类型码 §5.2
  "sender_id":       int,
  "sender_name":     str,
  "text":            str,      # 文本
  "fid":             str,      # fids[0]
  "media_orig_url":  str,      # 由 fid 构造的下载 URL
  "media_local_path":str,      # 下载后回填
  "url_objects":     str,      # JSON string
  "pic_infos":       str,      # JSON string
  "template":        str,
  "template_data":   str,      # JSON string
  "recall_mids":     str,      # JSON: 被撤回 mid 列表
  "recall_by":       str,
  "attitude_data":   str,      # JSON
  "faith_status":    int,
  "faith_icon":      str,
  "group_name":      str,
  "annotations":     str,      # JSON
  "created_at":      int,      # ms 时间戳
  "raw_json":        str,      # 原始 JSON 备份
}
```

### 3.2 url_objects（卡片分享，如网页/微博分享）

```json
[
  {
    "info": {
      "url_long":   "https://...",
      "url_short":  "https://t.cn/xxx",
      "title":      "分享标题",
      "display_name": "...",
      "pic":        "https://封面图"
    }
  }
]
```

字段提取优先级：`info.url_long` > `url_short`；过滤 `weibo.com` / `sinaimg` 域名后用于外部文件扫描。

### 3.3 pic_infos（小程序卡片图片）

两种形态都兼容：

```jsonc
// 形态 A：数组
[{"pic_big": "...", "pic_mid": "..."}]
// 形态 B：单对象
{"pic_big": "...", "pic_mid": "..."}
```

### 3.4 template / template_data（系统消息）

- `template`：模板文本，如 `"{{userA.DATA}} 邀请 {{userB.DATA}} 加入群聊"`
- `template_data`：变量字典

```json
{
  "userA": {"value": "张三", "scheme": "...", "color": ""},
  "userB": {"value": "李四", "scheme": "...", "color": ""}
}
```

### 3.5 撤回消息 (msg_type=331)

`info.ids` 是被撤回的 mid 列表：

```python
{
  "recall_mids": "[\"5302496155143676\", \"5302496155143677\"]",
  "recall_by":   "撤回者昵称"
}
```

### 3.6 态度/点赞 (msg_type=9999)

```python
{
  "attitude_data": "{\"mid\":\"...\",\"attitudes\":[...],\"users\":{...}}"
}
```

---

## 4. 错误与频率处理规范

### 4.1 HTTP 状态码处理矩阵

| 状态码 | 含义 | 重试？ | 退避 | 实现 |
|--------|------|--------|------|------|
| 200 | 成功 | — | — | 正常处理 |
| 429 | 限流 | ✅ | 4^n × (1 + rand[0,0.5]) 秒 | `crawler.py:78-84` |
| 5xx | 服务端错 | ✅ | 2^n × (1 + rand[0,0.5]) 秒 | `crawler.py:69-77` |
| 4xx (非429) | 客户端错 | ❌ | 立即抛出 | `crawler.py:97-99` |
| ConnectionError / Timeout | 网络错 | ✅ | 2^n × (1 + rand[0,0.5]) 秒 | `crawler.py:85-93` |

最大重试次数：**3**。

### 4.2 业务级错误

| 错误 | 检测 | 处理 |
|------|------|------|
| Cookie 过期 | §2.2 返回空 `contacts` / §2.3 返回 `result:false` | 提示重新扫码登录 |
| 限流 | 429 状态 | 退避重试 |
| gid 无权限 | §2.3 `result:false` | 跳过该群 |

### 4.3 频率限制（实测经验值）

| 接口 | 建议间隔 | 来源 |
|------|---------|------|
| §2.2 群列表 | ≥ 1s | 无文档，保守值 |
| §2.3 增量翻页 | ≥ 0.3s（带 ±20% 抖动） | `crawler.py:_jitter_sleep` |
| §2.3 回填翻页 | ≥ 1.0s（带 ±20% 抖动） | `crawler.py:_backfill_group` |
| §2.3 跨群切换 | ≥ 1.5s（带 ±15% 抖动） | `crawler.crawl_all` |
| §2.4 媒体下载 | ≥ 0.3s | `crawler.download_all_media` |
| §2.4 链接文件扫描 | ≥ 1s | `links.scan_and_download_messages` |

> **核心原则**：抖动 (`random.uniform(-0.2, 0.2)`) 让请求节奏不规则，规避固定间隔的简单频控。

### 4.4 实现伪代码（任何语言通用）

```
function request_with_retry(method, url, params, max_retries=3):
    for attempt in 0..max_retries:
        try:
            resp = http.request(method, url, params)
            if resp.status >= 500: continue with backoff(attempt, base=2)
            if resp.status == 429: continue with backoff(attempt, base=4)
            if 400 <= resp.status < 500 and resp.status != 429:
                throw   # 客户端错，不重试
            return resp
        except NetworkError:
            if attempt < max_retries: continue with backoff(attempt, base=2)
            throw
    throw "max retries exhausted"

function backoff(attempt, base):
    sleep(base^attempt * (1 + random(0, 0.5)))
```

---

## 5. 类型码对照表

### 5.1 消息类型码 `type`（`types.py:MSG_TYPES`）

| 码 | slug | 含义 |
|----|------|------|
| 100 | weibo_share | 微博分享 |
| 320 | invite | 邀请入群 |
| **321** | **normal** | **普通消息（最常见）** |
| 322 | join | 新人入群 |
| 323 | leave | 退群 |
| 324 | kick | 被踢出群 |
| 325 | rename | 群名修改 |
| 327 | transfer | 群主转让 |
| **331** | **recall** | **消息撤回** |
| 332 | sync | 协议同步（心跳，跳过） |
| 333 | silent_change | 免打扰变更 |
| 335 | group_update | 群信息更新 |
| 337 | admin_change | 管理员变更 |
| 421 | join_request | 入群申请 |
| 429 | removed | 被移出群 |
| 499 | notice | 群通知 |
| 9999 | attitude | 态度更新（跳过） |

**跳过类型** `SKIP_TYPES = {332, 9999}`：心跳/态度更新无实际内容，不入库。

### 5.2 媒体类型码 `media_type`（`types.py:MEDIA_TYPES`）

| 码 | slug | 含义 | 备注 |
|----|------|------|------|
| **0** | **text** | **纯文本** | 最常见 |
| **1** | **image** | **图片** | 下载时加 `&imageType=origin` |
| 4 | unknown_4 | 未知 | — |
| 5 | file | 文件附件 | PDF/DOC/ZIP 等 |
| 9 | unknown_9 | 未知 | — |
| **10** | **video** | **视频** | 体积大 |
| 11 | unknown_11 | 未知 | — |
| 13 | video_or_rp | 视频/红包 | 实现：内容含"红包消息"则当红包 |
| 14 | link | 链接/卡片分享 | 通常配合 `url_objects` |
| 15 | miniprogram | 小程序卡片 | 通常配合 `pic_infos` |

**自定义码 16 = 红包**（`media_type=13` 且内容含"收到红包消息"时改写为 16）。

**视频集合** `VIDEO_MEDIA_TYPES = {10, 13}` —— `--no-video` 时跳过下载这些。

---

## 6. Cookie 字段说明

| 键 | 必需 | 失效表现 | 续期方式 |
|----|------|---------|---------|
| `SUB` | ✅ | 接口返回空 / 302 | §2.1 扫码登录 |
| `SUBP` | ✅ | 同上 | 同上 |
| `SSOLoginState` | 推荐 | 风控更严 | 自动写入 |
| `ALF` | 自动 | — | 自动 |
| `SCF` / `SRF` | 自动 | — | 自动 |
| `_T_WM` | 自动 | — | 自动 |

**Cookie 失效的可靠检测**：调用 §2.2，返回 200 且 `contacts` 数组为空 → 失效。

---

## 7. 跨语言对接清单

### 7.1 最小可用流程

```
1. (Playwright/Selenium) 登录 → 取 Cookie 串
2. (HTTP GET) 群列表 → 存 gid 集合
3. (HTTP GET 翻页) 每个 gid 拉消息 → 入库（mid 唯一去重）
4. (HTTP GET 流式) 消息里的 fid → 下载到本地
```

### 7.2 Java + MySQL 移植要点

| 项 | Python 实现 | Java 等价 |
|----|-------------|-----------|
| HTTP 客户端 | `requests` | `OkHttp` / `HttpClient` |
| 浏览器自动化 | `Playwright Python` | `Playwright Java` / `Selenium` |
| JSON 解析 | 内置 `json` | `Jackson` / `Gson` |
| 数据库 | SQLite + FTS5 | MySQL + FULLTEXT 索引（或 Elasticsearch） |
| 时间戳 | int ms | `Instant` / `long` |
| 字符串比较 mid | 字典序 | `String.compareTo` |
| 跳过类型 | `SKIP_TYPES = {332, 9999}` | `Set.of(332, 9999)` |
| 重试退避 | `time.sleep(2^n * (1+rand))` | `Thread.sleep` / `ScheduledExecutor` |
| Cookie 存储 | SQLite config 表 | MySQL `kv_config` 表 |

### 7.3 MySQL 表结构映射（最小集）

```sql
CREATE TABLE groups (
    gid           BIGINT PRIMARY KEY,
    name          VARCHAR(255) NOT NULL DEFAULT '',
    member_count  INT DEFAULT 0,
    owner_id      BIGINT DEFAULT 0,
    min_mid       VARCHAR(32) DEFAULT '',
    max_mid       VARCHAR(32) DEFAULT '',
    updated_at    BIGINT DEFAULT 0
);

CREATE TABLE messages (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    mid           VARCHAR(32) NOT NULL,
    gid           BIGINT NOT NULL,
    msg_type      INT DEFAULT 321,
    media_type    INT DEFAULT 0,
    sender_id     BIGINT DEFAULT 0,
    sender_name   VARCHAR(255) DEFAULT '',
    text          TEXT,
    fid           VARCHAR(64) DEFAULT '',
    created_at    BIGINT NOT NULL,
    raw_json      LONGTEXT,
    UNIQUE KEY uk_mid (mid),
    KEY idx_gid_created (gid, created_at),
    FULLTEXT KEY ft_text (text, sender_name)   /* 需要 ngram 解析器支持中文 */
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE media_files (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    fid           VARCHAR(64) NOT NULL,
    gid           BIGINT DEFAULT 0,
    mid           VARCHAR(32) DEFAULT '',
    media_type    INT DEFAULT 0,
    orig_url      TEXT,
    local_path    TEXT,
    status        VARCHAR(16) DEFAULT 'pending',
    UNIQUE KEY uk_fid (fid),
    KEY idx_status (status)
);

CREATE TABLE config (
    `key`         VARCHAR(64) PRIMARY KEY,
    `value`       TEXT NOT NULL,
    updated_at    BIGINT DEFAULT 0
);
```

**中文全文搜索**：MySQL 5.7+ 需要 `WITH PARSER ngram` 才能对中文做全文搜索，否则默认按空格分词无效：

```sql
FULLTEXT KEY ft_text (text, sender_name) WITH PARSER ngram
```

### 7.4 类型映射表（Python → Java/MySQL）

| Python | Java | MySQL |
|--------|------|-------|
| `str` (mid) | `String` | `VARCHAR(32)` |
| `int` (gid/sender_id) | `long` / `Long` | `BIGINT` |
| `int` (msg_type/media_type) | `int` / `Integer` | `INT` |
| `str` (JSON 字段) | `String`（建议反序列化为 POJO） | `TEXT` / `LONGTEXT` |
| `int` ms 时间戳 | `long` | `BIGINT` |
| `bytes` (媒体) | `byte[]` / `InputStream` | 文件系统（不入库） |

### 7.5 字段语义迁移要点

1. **mid 必须按字符串存储**：虽然看起来是数字，但长度可达 18 位，超过 32 位 int 范围，必须用字符串或 BIGINT。
2. **mid 字典序比较 = 时间顺序**：字符串比较等价于时间顺序，所以「停止条件 `msgs[-1].id <= max_mid`」可以用字符串比较实现。
3. **time 是秒级**：响应里 `time` 字段是 10 位秒时间戳，必须 ×1000 转毫秒存储。
4. **raw_json 永远要存**：解析逻辑会演进，存原始 JSON 可重新解析。
5. **去重靠 UNIQUE(mid)**：无论 MySQL 还是 SQLite，`mid` 必须有唯一约束，用 `INSERT IGNORE` / `INSERT ... ON DUPLICATE KEY UPDATE` 实现。

---

## 附录：实现文件对照表

| 章节 | 源码位置 |
|------|---------|
| §0.3 公共请求头 | `crawler.py:HEADERS` (39-54行) |
| §0.4 媒体请求头 | `media.py:HEADERS` (26-35行) |
| §2.1 登录流程 | `crawl.py:_renew_cookie` (104-205行) |
| §2.2 群列表 | `crawler.py:fetch_contacts` (120-159行) |
| §2.3 消息翻页 | `crawler.py:fetch_messages` (162-194行) + `crawl_group`(233-310) / `_backfill_group`(312-384) |
| §2.4 媒体下载 | `parser.py:resolve_media_orig_url` (31-35行) + `media.py:download_file` (121-186行) |
| §3.1 统一格式 | `parser.py:parse_message` (110-232行) |
| §4 错误处理 | `crawler.py:_request_with_retry` (61-106行) |
| §5 类型码 | `types.py:MSG_TYPES` / `MEDIA_TYPES` / `VIDEO_MEDIA_TYPES` |
