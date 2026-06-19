# 图片/视频内嵌显示与放大查看 — 设计文档

- 日期：2026-06-19
- 状态：已确认，待实现
- 关联分支：基于 master（消息查看器已合并）

## 1. 背景与目标

微博群聊消息查看器（本地只读：`server.py` stdlib HTTP 服务 + `web/` 前端 + `weibo_im.db` SQLite）当前对图片/视频消息仅渲染文本占位符 `🖼 [图片]` / `🎬 [视频]`，无法查看实际内容。

**目标：** 聊天中的图片和视频直接内嵌显示，支持点击放大查看与播放。

## 2. 现状分析

### 2.1 媒体数据现状

- `media_files` 表共 57312 条记录，但 **`local_path` 全部为空、`status` 全为 `pending`** —— 媒体文件实际均未下载到本地。
- `media_type` 分布：1=图片(52943)、10=视频(3425)、5=文件(774)、4=语音(170)。
- 原始 URL 指向 `https://upload.api.weibo.com/2/mss/msget?fid=...&source=...`，**下载需要微博 Cookie 鉴权**。
- `config` 表中存有 `weibo_cookie`，可被服务端读取用于下载。
- `messages` 表通过 `fid` 字段关联 `media_files.fid`，并有 `media_type`、`media_orig_url`、`media_local_path`、`pic_infos` 等字段。
- 爬虫 `weibo_im/media.py` 的 `download_file(fid, url, media_type)` 已实现单文件下载逻辑（带 cookie、跳过已存在、修正扩展名、返回 status），可复用。

### 2.2 现有渲染逻辑

`web/app.js` 的 `renderMessageBody(m)` 按 `media_type` 渲染：

```js
if (mt === 1) return `🖼 [图片]${link}`;
if (mt === 5) return `📎 [文件]${link}`;
if (mt === 10) return `🎬 [视频]${link}`;
if (mt === 13) { /* 红包或视频 */ }
```

其中 `link` 是指向 `media_orig_url` 的 `[链接]` 跳转。

### 2.3 目录结构

- `media/images/`、`media/videos/`、`media/files/` 目录已存在但为空（下载落盘点）。
- `.gitignore` 已配置忽略 media 内容、保留目录结构。

## 3. 关键决策（用户确认）

1. **媒体来源**：按需下载 —— 用户点击图片/视频时，后端按需下载该单个文件到 `media/` 并缓存。
2. **列表展示**：占位框 —— 列表内不预加载实际图片，显示固定占位框，点击后才下载并放大。
3. **视频播放**：下载后内联播放 —— 点击视频 → 后端下载完整 mp4 → `<video controls autoplay>` 内联播放。
4. **鉴权**：服务端用 DB 里的 `weibo_cookie` 下载，前端无需鉴权。

## 4. 选定方案：按需下载接口 + 静态文件服务（方案 A）

### 4.1 整体架构与数据流

**新增组件：**

1. **`/api/media/<fid>` 接口**（server.py）：按需下载并返回媒体文件，带 cookie 鉴权、文件缓存、并发保护。
2. **`/media/` 静态服务**（server.py）：直接服务已下载的本地文件（`<img>`/`<video>` 的 src 终点）。
3. **前端 lightbox**（app.js + index.html + style.css）：点击占位框 → 弹出大图/视频播放器。

**数据流（点击一张图片为例）：**

```
用户点击占位框
  → 前端：打开 lightbox，显示 loading 动画
  → 前端：GET /api/media/<fid>
  → 后端：查 media_files.local_path
     ├─ 已存在且文件在 → 直接返回文件（Content-Type: image/jpeg）
     └─ 未下载 → 用 DB cookie 调 download_file() → 回写 DB → 返回文件
  → 前端：<img> 加载完成 → lightbox 显示大图
  → 失败 → lightbox 显示"加载失败，点击重试"
```

**视频**：同上，但返回 `Content-Type: video/mp4`，前端用 `<video controls autoplay>`；视频较大时首次等待更久，loading 动画持续到可播放。

**关键约束：**

- `media_type` 决定文件子目录和 Content-Type：1→`images/`+image/\*，10/13→`videos/`+video/mp4，5→`files/`（文件暂不在本期内嵌，保持占位）。
- 并发保护：同一 fid 多次请求时，用进程内锁 + 文件存在检查，避免重复下载。
- `weibo_im.media.download_file()` 已处理：跳过已存在文件、修正扩展名、失败返回 `status:failed`。

### 4.2 后端 `/api/media/<fid>` 接口

**路由：** `GET /api/media/<fid>`

**路径变量：** `fid`（媒体文件 ID，字符串，如 `5309760582717320`）

**处理流程：**

```
1. 解析 fid，查 media_files 表：
   SELECT media_type, orig_url, local_path, status, mid FROM media_files WHERE fid=?
   - 查不到 → 404 {"error":"media not found"}

2. 判断 local_path 是否已有缓存：
   - local_path 非空 且 os.path.isfile(local_path) 且 size>0
     → 命中缓存，跳到步骤 4

3. 未命中缓存，按需下载：
   - 获取 DB 中的 weibo_cookie（首次从 config 表读，进程内缓存）
   - 调用 weibo_im.media.download_file(fid, orig_url, media_type)
     （该函数内部：已存在则跳过、下载、修正扩展名、返回 {status,local_path,file_size,md5}）
   - 下载成功 → 回写 media_files（status=done, local_path, file_size, md5）
                  和 messages.media_local_path（用 media_files.mid）
   - 下载失败 → 回写 media_files(status=failed) → 404 {"error":"download failed"}
   - 并发保护：进程内 dict {fid: Lock}，同一 fid 串行化；不同 fid 并行

4. 返回文件：
   - 根据 local_path 扩展名推断 Content-Type（.jpg→image/jpeg, .png→image/png,
     .gif→image/gif, .webp→image/webp, .mp4→video/mp4）
   - 读取文件内容，设 Content-Length，200 返回字节流
   - 设 Cache-Control: max-age=31536000（已下载文件不变，可长期缓存）
```

**`/media/` 静态服务：**

- `GET /media/images/<file>` / `/media/videos/<file>` / `/media/files/<file>`
- 安全检查：路径必须在 `media/` 目录下（防 `../` 穿越，用 `os.path.realpath` 校验）。
- 直接读文件返回，Content-Type 同上推断。
- 这个端点给前端 `<img src="/media/images/xxx.jpg">` 用，**仅服务已下载文件**；未下载的由 `/api/media/<fid>` 触发下载。

**前端 URL 策略：**

- 统一用 `/api/media/<fid>`：前端只需知道 fid，由后端负责"下载或取缓存"。前端逻辑最简单，不必先查 local_path 是否存在。
- 已下载的 `/api/media/<fid>` 会快速命中缓存返回（查一次 DB + 读文件），性能可接受。

**鉴权与安全：**

- 本地查看器，无用户鉴权（与现有 API 一致）。
- cookie 仅服务端使用，不暴露给前端。
- `media_type=5`（文件/pdf 等）本期不内嵌，接口仍可下载但前端不主动调用。

### 4.3 前端：列表占位框 + Lightbox

#### 4.3.1 列表占位框渲染

改造 `renderMessageBody(m)`，对 `media_type=1`（图片）和 `media_type=10/13`（视频，13 中红包除外）：

**当前：**

```js
if (mt === 1) return `🖼 [图片]${link}`;
if (mt === 10) return `🎬 [视频]${link}`;
```

**改为：** 渲染一个可点击的占位框元素，携带 `data-fid` 和 `data-mtype`：

```
图片：  <div class="media-ph" data-fid="..." data-mtype="1">
          <span class="media-icon">🖼</span><span>图片</span>
        </div>
视频：  <div class="media-ph" data-fid="..." data-mtype="10">
          <span class="media-icon">🎬</span><span>视频</span>
        </div>
```

- 占位框样式：固定宽度（如 200px）、高度自适应/居中，带边框、圆角、浅灰背景、hover 高亮，光标 pointer。
- 点击事件：事件委托在 `#message-list` 上，根据 `data-mtype` 分流到 `openImage(fid)` 或 `openVideo(fid)`。
- `media_type=5`（文件）保持 `📎 [文件]` 文本占位不变（本期不内嵌）。
- `media_type=13` 中含"红包"的走现有红包占位逻辑，不触发 lightbox；不含红包的按视频处理。
- 其他 media_type 不变。

#### 4.3.2 Lightbox（图片放大 / 视频播放）

**新增 DOM（index.html，一个全局 lightbox 容器）：**

```html
<div id="lightbox" class="lightbox hidden">
  <div class="lightbox-backdrop"></div>
  <div class="lightbox-content">
    <button class="lightbox-close" title="关闭">×</button>
    <div class="lightbox-stage"></div>   <!-- img 或 video 注入处 -->
    <div class="lightbox-status"></div>  <!-- loading / 失败提示 -->
  </div>
</div>
```

**打开图片 `openImage(fid)`：**

1. 显示 lightbox，stage 内放 loading 动画（"加载中…"）。
2. 创建 `new Image()`，src 设为 `/api/media/<fid>?t=Date.now()`（防缓存读到失败响应）。
3. `onload` → 清 stage，插入该 `<img>`（CSS 限 max-width/max-height 90vw/90vh）。
4. `onerror` → stage 显示"加载失败"，并提供"重试"按钮（重试即重新 set src）。

**打开视频 `openVideo(fid)`：**

1. 显示 lightbox，stage 内放 loading 动画。
2. 创建 `<video controls autoplay>`，src 设为 `/api/media/<fid>?t=...`。
3. `oncanplay` → 隐藏 loading（视频已可播放）。
4. `onerror` → 显示"加载失败"+ 重试。

**关闭交互：**

- 点击 `×` 按钮 / 点击 backdrop / 按 ESC → 关闭。
- 关闭时：若是视频则 `pause()` 并清空 src（释放资源），清空 stage。

**状态管理：**

- lightbox 是模态的，同一时间只显示一个媒体。
- 关闭时取消正在加载的图片（`img.src=""`）避免回调乱序。

### 4.4 错误处理

| 场景 | 后端行为 | 前端表现 |
|------|---------|---------|
| `fid` 不在 media_files 表 | 404 `{"error":"media not found"}` | lightbox "加载失败"+重试 |
| 下载失败（HTTP 非200/超时/网络异常） | 回写 `status=failed`，返回 404 | lightbox "加载失败"+重试 |
| cookie 过期导致下载失败 | 同上（download_file 返回 failed） | 同上；用户需在 config 更新 cookie 后重试 |
| 文件已下载但磁盘上丢失 | 重新触发下载（步骤2 isfile 检查失败 → 走步骤3） | 透明重下，用户无感 |
| 并发请求同一 fid | 进程内锁串行化，第二个请求等首个完成后命中缓存 | 无感 |
| fid 合法但 media_type 非图片/视频（如5文件） | 接口正常返回文件 | 前端不主动调用（占位框不绑定点击） |

### 4.5 边界情况

- **扩展名修正**：`download_file` 会按 Content-Disposition/Content-Type 改名（如 `.bin`→`.pdf`）。前端只依赖 `/api/media/<fid>`，不关心最终扩展名，改名不影响。
- **图片尺寸未知**：media_files 的 width/height 大多为 0（未下载时无尺寸）。占位框用固定尺寸，不依赖数据库尺寸。lightbox 大图用 CSS `max-width:90vw; max-height:90vh` 自适应。
- **超大视频**：首次下载需等待，loading 持续。`download_file` 是完整下载（非流式），大视频等待较久——本期接受（用户已确认"下载后内联播放"）。
- **同毫秒/红包消息**：media_type=16（红包）走现有占位逻辑，不触发 lightbox。
- **滚动加载新消息**：新消息的占位框通过现有事件委托自动获得点击能力，无需额外绑定。

## 5. 测试策略

### 5.1 后端单元测试（tests/test_server.py 扩展）

构造测试夹具：在 media_files 表插入 fake 记录（fid、media_type、orig_url 指向本地测试文件），mock `download_file`。

- `test_media_not_found`：不存在的 fid → 404
- `test_media_cached_returns_file`：local_path 指向已存在的测试文件 → 200 + 正确 Content-Type，不调用 download_file
- `test_media_download_on_demand`：local_path 空、mock download_file 成功 → 200，DB 已回写 local_path
- `test_media_download_fails`：mock download_file 返回 failed → 404，DB status=failed
- `test_media_static_serves_file`：`/media/images/<file>` 返回文件；`../` 路径穿越 → 403/404
- `test_media_concurrent_same_fid`：（可选）验证锁机制，第二个请求命中缓存

### 5.2 前端

手动验证（无前端测试框架）。验证清单：

- 列表占位框正确显示（图片/视频图标 + 文字）
- 点击图片 → lightbox 放大显示
- 点击视频 → lightbox 内联播放（控件可用）
- ESC / 点击 backdrop / 点击 × → 关闭
- 加载失败 → 显示失败提示 + 重试可用
- 已下载的媒体再次点击 → 秒开（命中缓存）

## 6. 不在本期范围（YAGNI）

- 文件类（pdf/zip 等）内嵌预览
- 批量下载管理界面
- 图片画廊（上一张/下一张切换）
- 视频流式边下边播
- 下载进度条（前端只显示 loading/完成/失败三态）

## 7. 涉及文件

| 文件 | 改动 |
|------|------|
| `server.py` | 新增 `/api/media/<fid>` 路由、`/media/` 静态服务、cookie 读取、并发锁、Content-Type 推断 |
| `web/app.js` | 改造 `renderMessageBody` 占位框、新增 lightbox 逻辑（openImage/openVideo/close）、事件委托 |
| `web/index.html` | 新增 lightbox DOM 容器 |
| `web/style.css` | 占位框样式、lightbox 样式（backdrop/content/stage/status/close/loading） |
| `tests/test_server.py` | 新增 MediaApiTest 测试类 |
