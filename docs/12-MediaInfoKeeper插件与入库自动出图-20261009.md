# 12 · MediaInfoKeeper 插件与入库自动出图（2026-10-09）

> 目标：让「杂项」库新入库的条目（MDC 刮不到的内容）自动拿到封面。
> 结论：**已跑通，且已定为最终方案**——提取信息覆盖全部 8 个文件库，自动出图只给「杂项」。

## 1. 装了什么东西

| 项 | 值 |
|---|---|
| 插件 | MediaInfoKeeper **v1.7.5.5** |
| 文件 | `F:\Program Files (x86)\embyserver-win-x64-4.9.1.80\programdata\plugins\MediaInfoKeeper.dll`（原 `MediaInfoKeeper.win-x64.dll` 改名） |
| sha256 | `9955e7ae…58e` |
| 自有配置 | `…\programdata\plugins\configurations\MediaInfoKeeper.json` |
| Emby | 4.10.1.0（portable，programdata 在安装目录内） |
| 加载证据 | 启动日志 `Loading MediaInfoKeeper, Version=1.7.5.5` + `补丁加载摘要：启用=34，禁用=15，等待=0，失败=0` |

## 2. 最终方案（当前线上值）

```json
"ItemAddedTaskEditor": {
  "ItemAddedMediaInfoLibraries":   "5616,8170,13731,22552,24412,16878,3833,13730",
  "ItemAddedRefreshMetadataLibraries": "24412",
  "ItemAddedIntroScanLibraries":   ""
}
```

| 开关 | 库 | 干什么 |
|---|---|---|
| 入库提取媒体信息 | 全部 8 个文件库（丝袜 5616 / 里番 8170 / 欧美 13731 / 无码 22552 / 杂项 24412 / 自存 16878 / AV 3833 / FC2 13730） | 新条目入库时跑一次 ffprobe，把流信息/时长写进 JSON，Emby 立刻有码率分辨率时长 |
| 入库刷新元数据 | 仅杂项 24412 | 提取完成后刷新元数据并触发 **Emby 原生抽帧** → 生成 `poster.jpg` |
| 入库扫描片头 | 无 | 不启用 |

> 逗号分隔、无空格；合集库 5483（`CollectionType=boxsets`）不算文件库，不列入。

## 3. 为什么「出图」不放给全部库

实测（探针 r2，见第 4 节）：刷新范围会**覆盖已有图片**——目录里原本的 MDC 风格海报（1000×583）被抽帧图（1920×1080）顶掉。
其他 7 个库的海报是 MDC-ng 刮削来的成品图，一旦放进去就会被视频截图覆盖。**故只留杂项**（杂项本身就是刮不到的东西，抽帧图正是我们想要的兜底）。

## 4. 验证证据

- **提取全库**（探针 `欧美/zz-probe-scopeall`，条目 24621）：`22:47:07 新入库事件` → `入库媒体信息 提取开始` → `提取成功` → `覆盖写入Json`；条目 `MediaStreams=[(Video,h264,1920,1080),(Audio,aac)]`、`RunTimeTicks=15360500000`。
- **自动出图**（探针 `杂项/zz-probe-r1`，条目 24627）：无图目录，单条刷新后 `ProviderManager: Saving image to …\zz-probe-r1\poster.jpg`，新图 1920×1080 / 69,625 B。
- **限定生效**（探针 `欧美/zz-probe-r3`）：只有提取、日志无任何 `刷新元数据` 行、目录无 poster ⇒ 欧美海报不受影响。
- **入库自动链路**（r1/r2 均为自然入库触发）：`新增媒体文件` → `新入库事件` → `提取成功` → `刷新元数据` → Image Capture（`ffmpeg -ss … -f mp4 -threads 1 -i "http://192.168.2.238:11500/d/…"`）→ `Saving image`。

## 5. 已知问题（未处理）

- **并发抽帧会失败**：三条探针同时入库时，r1 连续 4 次 `Error ImageExtraction: ffmpeg image extraction failed … (output file not found)`；随后单独刷新一次即成功（`SingleImageExtractor` 退出码 1 但仍写出图，属正常）。
  ⇒ 批量入库时可能有条目漏图。可选缓解：串行化刷新、失败后补图重试、或入库后对无图条目做一次单条刷新。
- 插件会主动重写 `MediaInfoKeeper.json`（观察到两次），**改配置必须"改文件 + 重启 + 重启后回读"**；JSON 改了不重启不生效。
- 插件无标准配置 API（`/emby/Plugins/{id}/Configuration` 500/404），只能文件改。

## 6. 回滚 / 维护

- 配置备份：`backups/media-info-keeper/`
  - `MediaInfoKeeper.json.current`（当前线上值）
  - `MediaInfoKeeper.json.before_scope`（改动前：三项全空）
  - `MediaInfoKeeper.json.before_scope_all`（第一次改：提取=杂项）
  - `MediaInfoKeeper.json.before_refresh_scope`（第二次改：提取=全库，刷新=空）
- 改配置流程：备份 → 改 JSON → `POST /emby/System/Restart` → 等 20~30s → 回读配置确认 → 用探针目录验证。
- 改范围用的库 id 来源：`GET /emby/Library/VirtualFolders`。
- 彻底卸载：删 `programdata/plugins/MediaInfoKeeper.dll`（+ configurations 下的同名 JSON）后重启 Emby。

## 6.5 待办（2026-10-09 收工，明天继续）

- [ ] **决定是否加"漏图兜底"**：入库后检查条目有没有主图，没有就用本地 ffmpeg 抽帧补 `poster.jpg` + 刷新认图（串行，不抢流）。今天 Diana 那条就是这么修好的，一次成功。
- [ ] 观察**真实批量入库时**的出图成功率（今天样本只有 Diana 一个文件，成功 3 / 失败 9，不能代表全库）。
- [ ] 今天的单点修复记录：`杂项/FapHouse.24.09.30`（条目 24419）。原 `poster.jpg` 实为 **17 帧动画 GIF**（1280×720，4.2 MB）改了后缀；已换成 02:33 的视频帧（1920×1080，98 KB），原图备份在 `backups/media-info-keeper/replaced-posters/`。
- [ ] 抽帧参数对照实验结论：用同一个 `ffmpeg.exe` + Emby 原参数（`-ss 00:02:33.605 -f mp4 -threads 1 -i <url> -an -sn -threads 0 -vf thumbnail=24 -f image2 -b:v 100M out.jpg`）手工执行**成功**（69,625 B）⇒ 失败是取流偶发，不是参数问题。

## 7. 相关端点

- `POST /emby/System/Restart`
- `GET  /emby/Library/VirtualFolders`
- `GET  /emby/Items?Recursive=true&SearchTerm=…&Fields=Path,ImageTags,MediaStreams,RunTimeTicks`
- `DELETE /emby/Items/{id}`（204 成功）
- `POST /emby/Items/{id}/Refresh?MetadataRefreshMode=Default&ImageRefreshMode=Default&ReplaceAllImages=false&AllowFfProcess=true`
- 插件自有路由：`POST /MediaInfoKeeper/Items/ExtractMediaInfo {"Ids":[…]}`、`GET /MediaInfoKeeper/Items/DebugMediaInfo`
- 日志（直接读文件，比 API 可靠）：`…\programdata\logs\embyserver.txt`
## 8. 2026-10-10 实测：新入库「杂项」条目跑通全流程 + 抽帧失败根因落定

**测试对象**：thread `3820501`（【自购】【115ED2K】Myfans しんのすけ … 2.65G/1V），任务 `category=杂项`、`step=nfo`（MDC 未刮削）。

- 这类条目**不在** `已刮削/杂项` 里，而是停在 `待看/sehuatang_misc/thread_3820501/`（只放 strm）。真正进 Emby 库要靠帖子页 📤（`POST /api/metadata`）触发的 `_fallback_emby_movie_dir`：在 `已刮削/杂项/<条目名>/` 建目录并**硬链接** strm。
- 本次手工复刻了这一步（= 📤 的建目录+硬链接，不写 nfo/图）→ 硬链接 inode `844424931446542` → `POST /Library/Refresh`。

**插件侧（✅ 全绿）**

```
18:19:13 MediaInfoKeeper: 新增媒体文件，www.98T.la@【目隠しのみ】Hカップ爆乳痴女と3P中出し密会.strm
18:19:17 MediaInfoKeeper: 新入库事件 / 入库媒体信息 提取开始
18:19:19 MediaInfoKeeper: 入库媒体信息 提取成功  →  刷新元数据（触发 Emby 刷新）
```

提取结果落进 Emby 自动写的 nfo（`<streamdetails>`：h264 / 1920×1080 / 60fps / 6.1 Mbps）。

**出图侧（首轮 ❌，手动重试 1 次 ✅）**

- 18:19:22 `SingleImageExtractor` 抽帧 `-ss 00:06:05.900`（`-vf thumbnail=24`）→ `exit 1` → 兜底 `-vframes 1` → `exit 0` 但 *output file not found* → `Error in Image Capture dynamic image provider`，条目 `24638` 无图。
- 手工再发一次 `POST /Items/24638/Refresh?MetadataRefreshMode=Default&ImageRefreshMode=FullRefresh&ReplaceAllImages=false&AllowFfProcess=true` → **成功**：`poster.jpg` 83,843 B（1920×1080），`ImageTags.Primary` 更新，经 Emby 取图接口目视确认是画面帧。

**根因（这次抓到了，不是"偶发"）**

- strm → `http://192.168.2.238:11500/d/<pickcode>/<name>.mp4`。**11500 的监听进程是 `115-Desktop.exe`**（不是我们的 relay；`strm-relay.service`(11501) 处于 disabled）。它回 `302` → `https://cdnfhnfile.115cdn.net/...?<token>` → 再读 CDN。
- **CDN 那一段 TLS 读取会随机吐出损坏数据**，ffmpeg 报：
  - Emby 的 ffmpeg：`[tls] Unable to decrypt message (error 0x80090330)` / `reached eof, corrupted CTTS atom` / `error reading header`
  - WSL / Windows ffmpeg 同 URL 同时间点：`[tls] Decryption has failed.` → `error reading header` → `End of file`
- 成功率实测：同 URL 同时间点各 3 次 → **成功 2/6 ≈ 33%**；成功那几次输出**逐字节相同**（128,675 B）⇒ 解码没问题，是取流被损坏。
- 机制：损坏若落在 **moov/header 读取阶段** → 一行没写就退出 → Emby 判 `output file not found`（失败）；若落在**首帧已落盘之后** → JPEG 已生成再报错退出 → Emby 只检查文件存在 → 判成功（故日志里 `exit code 1` 紧跟 `Saving image`）。
- `curl -r 285000000-285065535`（走 7890 代理）连打 4 次，`Content-Range` 与内容完全一致 ⇒ 不是直链/偏移算错，是取流链路偶发损坏。

**结论**

- 插件三层（提取 / 触发刷新 / 交给 Emby 抽帧）都在工作；**唯一不稳定的是末端取流**，约 1/3 成功率。
- ⇒「漏图兜底」由"可选"变"必要"：入库后查一次主图，没有就**带重试**补（3 次重试 ≈ 96%），补完用 `AllowFfProcess=false` 刷新认图。

**本次落的文件**

- `已刮削/杂项/【目隠しのみ】Hカップ爆乳痴女と3P中出し密会/`：`*.strm`（硬链接）+ `*.nfo`（Emby 自动写）+ `poster.jpg` 83,843 B；Emby 条目 id `24638`。
- 复核用的工具脚本：`backups/tools/emby_api.py`；库配置备份：`backups/emby-library-options/VirtualFolders-20261010.json`。

---

## 9. 2026-10-11 根因落定：管线刷新漏带 `AllowFfProcess` —— 这是"入库很多条，只有一条出图"的真正原因

### 9.1 现象

上午 09:38–09:40 连续入库 6 条（`24650`–`24658`，杂项库），**只有 1 条（ManyVids.26.06.18 / `24656`）自己出了海报**，其余 4 条一直空白；后来 rkprime / 李娜娜 / 罗桑桑三条是"撞上别的刷新"才补上的。

### 9.2 取证（Emby 日志 `programdata/logs/embyserver.txt`）

- 逐条对齐请求与抽帧记录后发现：整批里 **只有 09:39:58 一条 `extract-image ProcessRun`**（对应 24656）。其余 4 条从头到尾**没有任何 ffmpeg 抽帧记录**，日志里也没有 `Error in Image Capture` —— 不是抽帧失败，是**抽帧根本没被发起**。
- 把该条目收到的刷新请求按调用方/参数分类，只有两种：
  - **管线侧（我们的 `import_api.py`）**：`/Items/{id}/Refresh?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh&ReplaceAllImages=true&ReplaceAllMetadata=true` —— **没有 `AllowFfProcess`**；
  - **本地侧（MediaInfoKeeper / Emby 界面）**：`...&AllowFfProcess=true&X-Emby-Token=...` —— 有。

### 9.3 对照实验（决定性）

对同一条目 `24658` 先后发两次刷新，只差一个参数：

| 请求 | 结果 |
|---|---|
| `MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh&ReplaceAllImages=true&ReplaceAllMetadata=true`（管线同款，**无** `AllowFfProcess`） | 只有 `RefreshItem Start`，**全程无 ffmpeg、无 `Saving image`** ← 复现"漏图" |
| 同上但 + `AllowFfProcess=true` | `09:48:52` 拉起 `extract-image`，`09:48:54` `Saving image to …\poster.jpg` ✅ |

⇒ **`AllowFfProcess=true` 是"允许 Emby 为本次刷新拉起 ffmpeg 抽帧"的总闸**。不带它，Emby 会**静默**跳过 Image Capture（不报错、不写盘）。

### 9.4 修复（`src/server/import_api.py`，2026-10-11）

1. `_refresh_emby_path()`：**缺主图的条目**在刷新请求里追加 `AllowFfProcess=true`（已有主图的不加，避免把刚上传的官方海报覆盖成截帧）；刷新成功后把缺图条目交给后台兜底。
2. 新增 `_emby_item_primary(item_id)`：查条目有没有主图。**注意 Emby 4.10 没有 `GET /Items/{id}`（会 404）**，单条目必须点名查 `/Items?Ids=<id>&Fields=ImageTags`。
3. 新增 `_refresh_emby_item_image()`：只补缺图的一次刷新（`ImageRefreshMode=Default` + `ReplaceAllImages=false` + `AllowFfProcess=true`）。
4. 新增 `_missing_poster_worker()` / `_start_missing_poster_fix()`：后台线程，对缺图条目**复查 + 最多重试 3 次**（间隔 8s / 12s / 12s），串行执行避免并发拉爆 115 客户端；不阻塞 `/api/metadata` 响应。
5. `_emby_items_by_path_prefix()`：查询加 `Fields=Path,ImageTags`，供上面的缺图判断复用。

### 9.5 验证

- **A/B 实验**：见 9.3。
- **生产代码路径**：临时造一条全新条目（从未有图 → Emby 无缓存）`已刮削/杂项/zz_probe_missingposter/`，扫库进库为条目 `24662`，直接调生产函数 `_refresh_emby_path(PROBE)`：
  - 日志：`[metadata] 定向刷新条目 24662 (zz_probe_missingposter) [缺主图→放行截帧]` → `[metadata] 缺图兜底: 启动 1 条` → `[metadata] 缺图兜底: 24662 已有主图, 无需补`；
  - 结果：**5 秒内 `Primary=True`，`poster.jpg` 写盘** ✅。
- **回归 `/api/metadata`**：把 `24648`（糖心罗桑桑）的主图删掉（`DELETE /Items/24648/Images/Primary` + 移走 poster.jpg）后 POST `/api/metadata`（thread `3825759`），返回 `{"ok": true, "items_refreshed": 1}`，**5 秒内主图恢复** ✅（该次命中的是 Emby 的 Image Capture 缓存，故无 ffmpeg）。
- 清理：探针目录、Emby 条目 `24662`、MediaInfoKeeper 侧车 `www.98t.la@zz_probe_missingposter-mediainfo.json`、被移走的旧海报全部删除；杂项库回到 12 条、缺图 0。

### 9.6 备注

- Emby 的 Image Capture 抽帧结果**按媒体文件缓存**（`programdata/cache/images`）：同一个 115 文件被复制成两个条目时，第二条可直接命中缓存（表现为 `poster.jpg` 与另一条**逐字节相同**）。真正的新文件才会走 ffmpeg。
- 备份：`src/server/.bak-20261011-095500-缺图兜底/import_api.py`。
- 服务日志在 `/var/log/import_api.log`（systemd `StandardOutput/StandardError=append:...`），不在 journald 里。
