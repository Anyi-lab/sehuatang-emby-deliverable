# 媒体服务器适配方案（Emby / Jellyfin 通用 · 可多台）

> 目标读者：接手这套代码的人。本文说明「怎么做到一套代码同时触发 Emby 和 Jellyfin」，
> 以及**为什么这么设计、边界在哪、怎么扩展第三家**。代码见 `src/server/import_api.py` 的
> 「媒体服务器适配」段（约 65–310 行 + 1364–1440 行）。

---

## 一、先定义什么算「通用」

一句话判定标准：

> **新增一台服务器时，只需要给一个 URL 和一把 key，不用改任何一行代码，也不用管它是 Emby 还是 Jellyfin。**

对应到代码上，三条可检验的硬指标：

1. `media_refresh()` 对两类服务器都返回 204（扫库真被排进队列）；
2. 条目查询 / 播放信息 / 用户列表这三条**读路径**，在两类服务器上都可用；
3. 任一台挂掉不影响其它台，也不影响入库主流程。

---

## 二、差异矩阵（实测 + 已知）

| 维度 | Emby（本机 4.9 / 4.10 均在测） | Jellyfin（3.5.2 fork，10.x） | 适配层怎么消化 |
|---|---|---|---|
| 路由前缀 | `/emby/<控制器>/<方法>`，**必须带** | 早期版本两种都认；新版可能已删 `/emby` 旧前缀 | 前缀**探测 + 学习**（见第五部分），所有路径只写接口名 |
| 扫库 | `POST /emby/Library/Refresh` → 204 | `POST /Library/Refresh` → 204 | 路由候选表 + 404/405 换路 |
| 家族识别 | 4.10 的 `System/Info/Public` **不返回** `ProductName` | 返回 `ProductName: "Jellyfin Server"` | 字段缺失判 Emby，含 jellyfin 判 Jellyfin |
| 认证头 | `X-Emby-Token` / `X-Emby-Authorization` | 认上面两个 + `X-MediaBrowser-Token` | **三个头一次写全** + URL 带 `api_key`，两边都通 |
| API 密钥 | 后台「高级 → API 密钥」或 `POST /Auth/Keys?App=<名>`；**存进 `Tokens_2` 表** | 后台「控制台 → API 密钥」 | 密钥**按台存**，不是全局一个；统一走环境变量注入 |
| PlaybackInfo | 部分版本**强制要求** `UserId` | 不需要（给了也不报错） | 按家族决定要不要带 `UserId` |
| 条目路径 | 服务器跑在 Windows 上时返回 `G:\srtm\...` | 同左（取决于宿主） | 路径**双向归一化**（见第七部分） |
| 令牌稳定性 | 浏览器登录令牌**每次登录被轮换** | 同上 | 只用专用 API 密钥，不用登录令牌 |

> 关键判断：**对得上的不是 URL 前缀，而是接口名**。
> `Library/Refresh`、`Items`、`Items/{id}/PlaybackInfo`、`Users` 在两家是**同名同义**的。
> 前缀只是可探测的路由糖，所以不赌前缀 —— 探到哪个用哪个。

---

## 三、设计原则（三条，先记这个再看细节）

1. **按接口名写代码，不按前缀写代码**。调用点写 `/Library/Refresh`，拼前缀是适配层的事。
2. **一次探测、终身缓存**（TTL 内不再打网）。家族 / 前缀 / 扫库路由 / UserId 全部按台缓存。
3. **失败不阻塞入库**。扫库失败只记 warning；多台里任一台成功即算成功；单台完全不可达也不回滚入库。

---

## 四、分层架构

```
 入库流程 / 校验流程 / 剧集迁移 / 元数据刷新        ← 调用方，只认接口名
        │
        │  media_refresh()                 ← 唯一契约：广播扫库（语义与改造前单机版一致）
        │  media_playback_url()            ← 播放信息 URL
        │  _emby_items_by_path_prefix()    ← 条目查询（带路径归一化）
        │  media_server_status()           ← 自检 / 监控页
        ▼
 ┌──────────────── 适配层 ────────────────┐
 │ 每台服务器一份状态: {url, token, name,  │
 │   flavor, prefix, route, user_id, at}  │
 │                                        │
 │ 探测与学习: 家族 · 前缀 · 扫库路由      │
 │ 路径组装:   _media_path() 拼前缀        │
 │ 认证:       _media_headers() 三头写全   │
 │ 多台:       逐台广播 + 结果留痕          │
 └────────────────────────────────────────┘
        ▼
   Emby 4.x         Jellyfin 10.x       （未来：第三家）
```

**对外（调用方可见）只有 4 个函数**，其余带下划线的都是内部件：

| 函数 | 语义 | 调用点 |
|---|---|---|
| `media_refresh(timeout)` | 向所有配置的服务器**广播**扫库，任一台 204 即返回 204；全失败抛最后一个异常 | `_trigger_emby_scan()`（3 处）+ 元数据刷新（1 处） |
| `media_playback_url(item_id, srv)` | 拼 PlaybackInfo URL，按家族决定是否带 UserId | prewarm（当前本地模式跳过，接口保留） |
| `_emby_items_by_path_prefix(prefix, srv)` | 按路径前缀查条目，WSL/Windows 两种写法都能匹配 | prewarm / 校验 |
| `media_server_status(probe)` | 每台的家族/前缀/路由/UserId/扫库码，自检与监控页用 | `--media-check` |

> 签名与语义**对齐改造前的单机版**，所以 4 个集成点零改动 —— 这也是「通用化不改业务」的验收依据。

---

## 五、探测与学习机制（核心）

每台服务器第一次被访问时，一次性探清它是什么、认什么路径，然后记住：

```
第 1 步 · 家族 + 前缀（一次探完）
   GET /emby/System/Info/Public
     ├─ 200 → prefix='/emby'，看 ProductName 判家族（缺字段=Emby）
     └─ 404/405 → GET /System/Info/Public
            ├─ 200 → prefix=''，同上判家族
            └─ 失败 → 沿用上次结论（缓存 600 秒）
       结果写进 s['flavor'] / s['prefix'] / s['at']

第 2 步 · 扫库路由（用前缀推导 + 双路兜底）
   候选顺序: 上次成功的 route  →  prefix+/Library/Refresh  →  /emby/Library/Refresh  →  /Library/Refresh
   命中规则: 404/405 → 换下一条继续试
             其它码(401/500) → 视为「路由对、别的问题」，**立即返回该码，不再换路**
                              （这样 key 挂了一眼能看出来，而不是被一轮轮 404 掩盖）
   命中后写进 s['route']，下次直接走第一条，不再试错

第 3 步 · UserId（首次需要时取一次，之后缓存）
   GET <prefix>/Users → 取第一个用户的 Id
```

**缓存策略**：`MEDIA_FLAVOR_TTL = 600` 秒。探测失败不覆盖旧结论（网络抖动不会把已学到的路由清掉）。

**为什么缓存「前缀」而不是每轮重探**：入库是高频路径（每个任务至少一次扫库），每轮多打两次
探测请求纯属浪费；服务器换版本是低频事件，TTL 到期或进程重启自然重学。

---

## 六、认证：三头写全 + 按台存密钥

```
Headers:
  X-Emby-Token: <token>               # Emby 认
  X-MediaBrowser-Token: <token>       # Jellyfin 认
  X-Emby-Authorization: MediaBrowser Token="<token>", Client="sehuatang-import", ...
URL:
  ...?api_key=<token>                 # 两家都支持
```

密钥**按服务器存**：`MEDIA_SERVERS="url1|key1,url2|key2"`，所以两台各用各的 key，互不干扰。

**踩过的坑（务必别再用登录令牌）**：Emby 把 API 密钥和浏览器登录令牌**都存进同一个 `Tokens_2` 表**。
用浏览器登录令牌当 key，用户下次登录就被轮换掉 → 401。正确做法是生成**专用密钥**：

```bash
# 用一把现有有效 key 换一把专用 key（AppName 会写进 Tokens_2，不绑设备登录）
curl -X POST "http://<host>:8096/Auth/Keys?App=sehuatang-import&api_key=<现有有效key>"
curl "http://<host>:8096/Auth/Keys?api_key=<新key>"     # 查看/确认
```

Emby 的数据目录在安装目录里（托盘版：`<安装目录>\programdata\data\authentication.db`），
不在 `%APPDATA%\Emby-Server` —— 排查 401 时用 `GET /Auth/Keys` 看清单比翻数据库快。

---

## 七、路径与标识归一化

服务器和入库程序往往不在同一台机器上（本机是：Emby 跑 Windows，程序跑 WSL），
同一个文件两边的写法不同 —— **不归一化一条都匹配不上**（实测：修前 0 条，修后 255 条）：

| 程序侧 | 服务器侧 |
|---|---|
| `/mnt/g/srtm/已刮削/AV/...` | `G:\srtm\已刮削\AV\...` |
| `/mnt/g/...` | `G:/...` |

`_media_path_variants()` 生成所有等价写法，比较前统一 `\` → `/`，按 `等值 或 前缀+/` 命中。

---

## 八、多台广播语义

```python
media_refresh(timeout=30)
  for 每台: _media_refresh_one(台, timeout)     # 逐台独立，互不影响
  任一台 == 204 → 返回 204（入库视为成功）
  全失败        → 抛最后一个异常（日志里带每台的码）
  结果留痕      → MEDIA_LAST_REFRESH = [(名字, 码/ERR), ...]
```

- **成功判定**：任一台 204 即成功 —— 「至少一台能看到新文件」就够了，不要求全绿。
- **失败隔离**：单台超时/401 不会让别的台不触发。
- **留痕**：每台的结果存进 `MEDIA_LAST_REFRESH`，`media_server_status(probe=True)` 可直接读，供自检与监控卡片。

---

## 九、配置与运维

实际生效值写在 `/etc/default/sehuatang-import`（chmod 600，systemd `EnvironmentFile` 注入）：

```ini
# 单台（向后兼容旧变量名 EMBY_URL / EMBY_TOKEN）
MEDIA_SERVER_URL=http://172.25.224.1:8096
MEDIA_SERVER_TOKEN=<专用密钥>

# 多台：url|token 逗号或分号分隔（分隔符兼容中英文逗号/换行）
# MEDIA_SERVERS=http://172.25.224.1:8096|emby_key,http://172.25.224.1:8097|jellyfin_key
```

先写的当「主服务器」（条目查询 / PlaybackInfo 走它；扫库是全部广播）。

改动生效：

```bash
sudo systemctl restart sehuatang-import
python3 src/server/import_api.py --media-check     # 逐台打印 家族/前缀/扫库码/UserId/路由
```

> 程序启动时会读一次这个环境文件补进 `os.environ`（真实环境变量优先），
> 所以**命令行自检和服务进程看到的是同一套配置** —— 不会出现「命令行通过、服务里 401」。

---

## 十、怎么扩展第三家（以 Plex 为例）

| 步骤 | 要做的事 | 是否要改适配层 |
|---|---|---|
| 1 | 配置 `MEDIA_SERVERS` 里加一条 `url\|token` | 否 |
| 2 | 用 `--media-check` 看探测结果 | 否 |
| 3 | 若它认「同一批接口名」→ 直接可用 | **不用改代码** |
| 4 | 若不是（Plex 是 `/library/sections/{id}/refresh` 这类完全不同的接口）→ 在探测里加一条家族分支，并给 `_media_refresh_one` 加一条 Plex 专用路由分支 | 是，改动限于适配层内 |
| 5 | 补一个 stub 后端进 `tests/media_server_stub_test.py` | 是（测试） |

**可复用的部分**：多台广播、按台缓存、三头认证、密钥管理、状态自检、失败隔离 —— 都不用重写。
真正要新写的只有「路由长什么样」和「鉴权头怎么带」这两件事。

---

## 十一、验证方案

### 离线回归（可随时复跑，不依赖任何真机）

```bash
python3 tests/media_server_stub_test.py     # 3 个本地 stub 后端，25 项断言
```

| stub | 形态 | 覆盖点 |
|---|---|---|
| ① Emby | 强制 `/emby` 前缀；PlaybackInfo **强制** UserId | 前缀必加、UserId 必带 |
| ② 老 Jellyfin | `/emby` 与无前缀**都认** | 探测顺序取带前缀那条 |
| ③ 新版 Jellyfin | **任何 `/emby` 一律 404** | 探测阶段试一次即弃用，之后全程无前缀 |

断言清单（节选）：家族与前缀各自独立学习、`_media_path()` 正确地加/去前缀、
三台广播全 204、第二次一发命中（不再试错路由）、UserId 只进 Emby 的 URL、
Items 前缀正确 + 两种路径写法都能匹配、`status` 汇总、单机回退兼容。

### 真机自检

```bash
python3 src/server/import_api.py --media-check
#  - primary  家族=emby  前缀=/emby  扫库=204  UserId=-  路由=/emby/Library/Refresh
#    条目     : /mnt/g/srtm/已刮削/AV 前缀命中 255 条
```

**交叉验证**：Emby 自己的日志里能看到我们的请求，用来确认「204 是真的排了任务」而不是被吞掉：

```
POST http://172.25.224.1:8096/emby/Library/Refresh ... UserAgent: Python-urllib/3.10
Response 204 to 172.25.224.1
TaskManager: Queueing task RefreshMediaLibraryTask        ← 关键这一行
```

---

## 十二、失败模式与降级

| 现象 | 含义 | 行为 |
|---|---|---|
| 404 / 405 | 这台不认这条路由 | 换下一条候选（自愈，学到为止） |
| 401 | 路由对，key 无效/被轮换 | 立即返回 401，日志/自检可见，**不再瞎试路由** |
| 500 | 服务器内部错 | 返回 500，同上 |
| 超时 / 连接失败 | 这台挂了 | 该台记 `ERR:...`，其它台照常；单台时记 warning，不阻塞入库 |
| 前缀探测失败 | 网络抖动 | 沿用上次结论（有缓存）；无缓存则按无前缀处理 |

---

## 十三、已知边界

1. **Jellyfin 未经真机验证** —— 本机只有 Emby（Windows 4.9）。Jellyfin 侧是 3 形态 stub 验证的，
   接入真机时跑一次 `--media-check` 即可确认（预期：前缀 `(无)` 或 `/emby`，扫库 204）。
2. **预热在本地模式下是关的** —— `_prewarm_items()` 直接标记 done 跳过（本地 strm 无需 PlaybackInfo 预热），
   所以 `_emby_items_by_path_prefix()` / `media_playback_url()` 目前只经该路径可达；
   一旦开启预热，前缀与路径归一化都已就位。
3. **监控页卡片未挂** —— `media_server_status(probe=True)` 已可直接返回结构化结果，
   挂到任务监控页是纯前端工作（尚未做）。
4. **`MEDIA_LAST_REFRESH` 只在进程内** —— 重启即清空，不做持久化（自检足够了）。

---

## 十四、结论

通用的实现方式不是「写两套分支」，而是**把差异收敛成 4 个可探测、可缓存的变量**
（家族 / 前缀 / 路由 / UserId），让调用方只看接口名。新增一台的成本 = 一行配置。
