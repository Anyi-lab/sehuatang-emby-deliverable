# 色花堂 → 115 → Emby 媒体自动化入库系统

> 一个把论坛资源（磁力/ed2k）**一键入库到 115 网盘**，并自动完成 **strm 映射、刮削、Emby 入库、302 直链播放**的全链路解决方案。
>
> 本交付包为**交流材料**，已去除全部敏感信息（服务器 IP、账号、密码、Token、密钥等），占位符 `<...>` 需替换为实际环境值。

---

## 一、项目是什么

色花堂（sehuatang.net）是中文成人内容论坛，帖子内通常包含磁力/ed2k 下载链接。人工下载、整理、刮削、入库 Emby 是非常繁琐的工作。

本项目实现：

```
浏览器油猴脚本（在帖子页点按钮）
    → 识别磁力/ed2k 链接 + 用户选择类型（影片 / 剧集）
    → 调用服务器入库 API（import-api :5081）
    → 推送到 115 网盘离线下载（CD2 gRPC）
    → 等待文件落地
    → SmartStrm 生成 strm 文件（本地文本指针，不占 115 API 配额）
    → MDCng 自动刮削 / 油猴手动补充元数据（nfo + 海报）
    → Emby 扫库入库（电影库 / 剧集库）
    → 客户端经 302 代理直连 115 CDN 播放（不经过媒体服务器中转）
```

**核心设计思想**：

1. **strm 映射规避风控**：115 网盘对高频 API 扫描有风控（错误码 770004），strm 只是几 KB 文本文件，Emby 扫库不会反复调 115 接口
2. **302 直链播放**：SmartStrm 把播放请求 302 到 115 CDN，视频流不经过媒体服务器，带宽/性能开销极小
3. **浏览器端刮削**：刮削所需的标题/简介/图片全部从帖子页面由油猴脚本抓取，服务器不需要反爬浏览器

---

## 二、交付包结构

```
sehuatang-emby-deliverable/
├── README.md                        # 本文件（总览）
├── docs/
│   ├── 01-项目概述.md               # 项目背景、目标、成果、演进历程
│   ├── 02-系统架构.md               # 架构图、组件清单、数据流、端口
│   ├── 03-使用方法.md               # 油猴脚本安装/使用、API 说明、任务监控
│   ├── 04-部署指南.md               # 服务器端组件部署、配置项说明
│   └── media-server-adapter.md      # 媒体服务器通用适配方案（Emby/Jellyfin，含扩展指引）
├── src/
│   ├── userscript/
│   │   └── sehuatang_import.user.js # 油猴脚本（浏览器端，脱敏）
│   └── server/
│       ├── import_api.py            # 入库核心 API 服务（5081，脱敏；含 /api/import/lookup 库存反查）
       ├── mcp115.py                # 115 网盘 MCP 客户端（离线下载/列目录/搜索反查，脱敏）
│       ├── scrape_sehuatang.py      # 网页刮削器（nfo/图片生成，脱敏）
│       ├── fs115.py                 # 115 文件系统封装（CD2 后端，脱敏）
│       ├── cd2grpc.py               # CD2 gRPC 客户端（离线下载/列目录，脱敏）
│       ├── global_login_115.py      # 115 扫码登录（可选，脱敏）
│       └── monitor_ratelimit2.py    # 115 限频监控 + 自动恢复（脱敏）
└── configs/
    ├── smartstrm-config.example.yaml # SmartStrm 配置模板（脱敏）
    ├── import-api.service           # systemd 服务单元示例
    ├── strm-nginx.conf              # 播放入口反代配置（脱敏）
    └── docker-compose.example.yml   # 容器编排示例
```

---

## 三、快速开始

### 3.1 服务器端（一次性部署）

1. 部署容器：SmartStrm、strm-nginx、Emby、CD2、MDCng（参考 `configs/docker-compose.example.yml`）
2. 配置 SmartStrm：替换 `configs/smartstrm-config.example.yaml` 中占位符 → 扫码授权 115
3. 部署 import-api：`src/server/import_api.py` → `/opt/115push/import_api.py`，注册 `configs/import-api.service`
4. 放行端口：5081（入库 API）、8097（播放）、8096（Emby 管理）、8024（SmartStrm 管理）

### 3.2 浏览器端

1. 安装油猴（Tampermonkey）插件
2. 导入 `src/userscript/sehuatang_import.user.js`
3. 修改脚本头部 `API_BASE` 为你的服务器地址：`http://<SERVER_IP>:5081`
4. 打开色花堂任意帖子页 → 左上角出现导航面板 → 点链接类型按钮入库

### 3.3 使用流程

详见 `docs/03-使用方法.md`。

---

## 四、核心成果

- ✅ 论坛 → 115 → Emby 全链路自动化（影片 + 剧集双流程）
- ✅ 302 直链播放（客户端直连 115 CDN，秒开满速）
- ✅ 浏览器端元数据补充（手动选图、海报标记、多图轮播）
- ✅ 115 限频感知与自动恢复（770004 风控闭环）
- ✅ Emby 删除联动（删条目同时清理 115 目录，保持一致性）
- ✅ 磁链库存反查（帖子页导航面板直接标 ✅在库 / ❓不在库 / ⚠️未校验，避免重复入库）

---

## 五、环境要求

| 组件 | 说明 |
|---|---|
| 服务器 | Linux + Docker（内存建议 ≥4GB；Emby 转码建议限制并发 =1） |
| 115 网盘 | VIP 账号（离线下载功能），需扫码授权 SmartStrm / CD2 |
| 浏览器 | Chrome/Edge + Tampermonkey 油猴插件 |
| Emby 客户端 | 网页版 / 网易爆米花 / VidHub / SenPlayer（支持 302 的均可） |

---

## 六、媒体服务器接入（Emby / Jellyfin，可多台）

> 完整方案（差异矩阵 / 探测机制 / 扩展第三家 / 失败降级 / 验证矩阵）见 **`docs/media-server-adapter.md`**。

扫库触发已做成 **Emby / Jellyfin 通用**，且支持一次广播到多台服务器。差异全部在适配层消化，
调用点看到的始终是同一个 `media_refresh()`。

**原理**：对得上的不是 URL 前缀，而是**接口名**（`Library/Refresh`、`Items`、`Items/{id}/PlaybackInfo` 三边同名同义）。
每台服务器第一次被访问时自动探测两件事，并记住结果：

| 探测项 | 怎么探 | 记住什么 |
|---|---|---|
| 家族 | `System/Info/Public` 的 `ProductName`（Emby 4.10 无此字段 → 判 Emby） | `flavor` |
| 前缀 | 先试 `/emby/System/Info/Public`，404/405 再试无前缀版 | `prefix` |
| 扫库路由 | 上次成功的路由 → `/emby/Library/Refresh` → `/Library/Refresh` | `route` |

于是所有路径只写接口名（`/Library/Refresh`、`/Items`、`/Users`），由 `_media_path()` 按该台的
`prefix` 拼完整路径 —— 对接**已删除 `/emby` 旧前缀的新版 Jellyfin** 时不会漏改。
另外 `PlaybackInfo` 对 Emby 带 `UserId`、Jellyfin 不带，也按家族自动决定。

### 6.1 配置

实际生效值写在 `/etc/default/sehuatang-import`（systemd `EnvironmentFile`，权限 600）：

```ini
# 单台（向后兼容旧变量名 EMBY_URL / EMBY_TOKEN）
MEDIA_SERVER_URL=http://172.25.224.1:8096
MEDIA_SERVER_TOKEN=08ba...2b54

# 多台：url|token 用逗号分隔，扫库会广播到每一台
# MEDIA_SERVERS=http://172.25.224.1:8096|emby_key,http://172.25.224.1:8097|jellyfin_key
```

改完 `systemctl restart sehuatang-import`。API 密钥建议在各自后台「高级 → API 密钥」生成
（Emby 也可 `POST /Auth/Keys?App=sehuatang-import`）—— 注意**不要**用浏览器登录令牌，登录一次就会被轮换掉。

### 6.2 自检与回归测试

```bash
python3 src/server/import_api.py --media-check        # 逐台打印 家族/前缀/扫库返回码/UserId/路由
python3 tests/media_server_stub_test.py              # 离线回归：3 个 stub 服务器，25 项断言
python3 tests/inventory_lookup_stub_test.py          # 离线回归：库存反查，stub 掉 115 搜索，51 项断言
```

回归测试覆盖的三种后端形态：

```
① Emby 形态       强制 /emby 前缀 + PlaybackInfo 强制 UserId
② 老 Jellyfin     两种前缀都认
③ 新版 Jellyfin   任何 /emby 一律 404（最苛刻场景）
```

回退链：某台完全不可达时**不阻塞**入库 —— 广播里任一台返回 204 即算成功，全失败才记 warning。

库存反查（帖子页导航面板的 ✅/❓/⚠️ 徽章）想手工验证一条链接，直接打接口即可：

```bash
curl -s -X POST http://127.0.0.1:5081/api/import/lookup \
  -H 'Content-Type: application/json' \
  -d '{"links":["magnet:?xt=urn:btih:xxxx&dn=SNOS-400.mp4"]}' | python3 -m json.tool
```

`state` 只会是 `in`（在库）/ `out`（搜不到）/ `unknown`（没查成，含 `reason`）。
`unknown` **不等于**“不在库”，前端也绝不这么显示。详见 `docs/03-使用方法.md`。

---
*本材料由 QwenPaw 整理，用于项目交流展示。*
