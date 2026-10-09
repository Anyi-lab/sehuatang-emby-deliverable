# strm 生成面板（115-Desktop STRM 能力的本地化版）

入口：<http://127.0.0.1:5091> · 启动（systemd 托管）：

```bash
systemctl status  strm-panel.service     # 状态 / 最近日志
systemctl restart strm-panel.service     # 重启
systemctl stop    strm-panel.service     # 停止（不会自动拉回）
journalctl -u strm-panel.service -n 50   # 进程级日志（应用日志在 /var/log/strm_panel.log）
bash scripts/start_strm_panel.sh         # 等价入口：转发给 systemctl restart，顺带健康检查
```

前台调试想跑别的端口时，直接手工起（不影响 unit）：

```bash
cd /root/clacky_workspace/sehuatang-emby-deliverable/scripts
python3 strm_panel.py --port 5092            # 默认 --host 0.0.0.0
```

`/etc/systemd/system/strm-panel.service`（2026-09-24 建立，替换原裸 `setsid nohup` 进程）：

| 项 | 值 |
|---|---|
| ExecStart | `/usr/bin/python3 .../scripts/strm_panel.py --port 5091` |
| WorkingDirectory | `.../sehuatang-emby-deliverable/scripts` |
| StandardOutput/Error | `append:/var/log/strm_panel.log`（9080 面板日志 tab 照旧可读） |
| Restart / RestartSec | `always` / `5`（kill -9 后约 6s 自动拉起，已演练：PID 变更、5091 恢复 200） |
| After | `network-online.target mnt-g.mount` + ExecStartPre 等 `/mnt/g/srtm` 最多 60s |
| Install | `WantedBy=multi-user.target`（已 `enable`，开机自启） |

已在**本地服务管理面板**（<http://127.0.0.1:9080>，`/root/mdc-ng/panel.py`）登记为 `strm-panel`：
进程状态 / 端口 5091 探测 / ▶启动 ⏹停止 🔄重启 / 打开 strm 面板 ↗ / 运行日志 tab（读 `/var/log/strm_panel.log`）。
该条目带 `systemd_unit`，▶启动 ⏹停止 🔄重启 三个按钮都走 `systemctl start/stop/restart strm-panel.service`——
**不能**再 pkill/nohup（`Restart=always` 会把手动 stop 拉回来，或起第二份进程抢 5091）。

面板只写本地 `.strm` 指针，**不碰 115 上的任何文件**；写完交给 MDCng watcher 刮削 → Emby。

## 四个 tab

| tab | 干什么 | 对应 115-Desktop |
|---|---|---|
| ① 生成 strm | 浏览 115 目录、勾选、预览、生成 | 「STRM生成」对话框 |
| ② 定时任务 | 到点自动增量生成 | 「STRM 定时任务」 |
| ③ 地址修正 | 扫本地 strm 里的 host → 批量换 host（端口不变） | 「STRM 地址修正」 |
| ④ 清单 & 清理 | 看生成清单；清理 115 上已消失的源对应的 strm | 「增量更新 / 清理已删除」 |

### ① 生成 strm

- **来源**：`流水线`（待看/sehuatang_&lt;分类&gt;，与补刮脚本同规则）或 `自存`（待看/自存，与 sehuatang 分开）。
- **自存落点布局**：`auto` = 按 115 目录名分层（每个勾选项一个文件夹）；`flat` = 全部放进一个指定目录。
- **下钻**：只处理本层 / 递归全部 / 递归 1 层。
- **视频扩展名**：对应「视频文件后缀」，留空 = 默认集（见代码 `DEFAULT_EXTS`）。
- **文件名追加提取码**：`原名_<pickcode>.strm`，同名不同片不互相覆盖。
- **strm 里的地址**：`局域网`（当前 `192.168.2.238:11500`）/ `本机 127.0.0.1:11500` / 自定义。
- **元数据后缀**：填了就从 115 拉同名 nfo/jpg/srt 到本地；**默认关**，因为 MDCng 自己会写 nfo/海报，
  开了会互相覆盖。
- 运行中可 **⏸ 暂停 / ▶ 继续 / ■ 停止**；完成后写「清单已记录 N 条」。

### ② 定时任务

间隔支持 分钟 / 小时 / 天；「立即跑」手动触发一次；任务参数快照包含源目录、分类、落点、体积/扩展名/追加提取码/地址/元数据。
后台线程每 20 秒扫一次到期任务，日志进「最近任务」，与手动生成共用同一套日志。

### ③ 地址修正

先「扫描」列出本地 strm 里用到的所有 `host:port`（含计数与样例文件），再填新 host（**只填 host，端口保持不变**），
「试算」确认后「开始修正」。写入用临时文件 + `os.replace`，原子替换。

### ④ 清单 & 清理

`清单` 记录每个 strm 的 pickcode ↔ 本地落点 ↔ 115 源目录，是「增量更新」（已存在的 strm 自动跳过）与「清理已删除」的依据。
`清理已删除`：重新列清单里涉及的 115 源目录，源文件已不在 115 上的 strm → 删本地文件（**先试算**，只删面板自己记过的 strm）。

## 界面（2026-09-24 视觉/交互优化）

- **顶栏状态条**：115 host、定时任务数、清单条数、当前任务（空闲 / 运行中 x/y / 已暂停 / 上次出错），
  每 6 秒轮询 `/api/status`。该接口**刻意不做本地文件 stat**——`/mnt/g` 是 9p 网络盘，
  每条记录 3~4ms，清单长大后轮询会拖慢面板；「本地已丢失」只在 ④ 页按需计算。
- **深/浅色主题**：右上角按钮或按 `D` 切换，默认跟随系统，选择记在 `localStorage['strmPanel.theme']`。
- **快捷键**：`Backspace` / `Alt+←` 上一级目录、`/` 聚焦过滤框、`Esc` 清空勾选、`D` 切主题。
- **表单记忆**：来源/分类/落点/深度/体积/扩展名/地址/只看未生成/排序/目录优先/清理目录等存 `localStorage['strmPanel.form']`；
  「恢复默认」= 清掉记忆并重载。
- **列表**：斑马纹 + hover 高亮 + 悬浮「复制路径」；「只看未生成」、单页最多 400 条。
  - **排序**（2026-10-06 起）：`按名称` / `按时间（新→旧）` / `按体积（大→小）`，与「**目录优先**」开关（默认开）**自由组合**；
    关掉目录优先即为纯混排（旧版「按名称」的行为）。
  - **时间列**：取自 115 条目的 `upt`（秒级 unix 时间，目录=最近被改动时间，文件=入库/上传时间），
    显示 `MM-DD HH:MM`，鼠标悬停看完整时间；**不读本地 `/mnt/g` 的 stat**（9p 盘逐文件 stat 是性能坑）。
    115 未返回时间的条目留空，排序时落到最后。旧版存的 `sortBy='dir'` 会被忽略并回落成「按名称 + 目录优先」。
- **生成过程**：顶部细进度条 + 日志自动滚底 + 暂停/继续/停止；结束 toast 并自动刷新目录与状态条。
- **危险操作**（真跑生成、执行清理）统一走确认弹窗；试算仍是单按钮直达。
- 纯 CSS + 原生 JS，无第三方依赖、无 CDN，离线局域网可用。
- 元素 ID 与函数名保持与旧版一致，前端整块重写、后端语义未变。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 面板页面 |
| GET | `/api/status` | 顶栏状态条用（host、定时数、清单条数、最近任务） |
| GET | `/api/list` | 列 115 目录（`path/category/source/layout/nocache`） |
| GET | `/api/schedules` `/api/tasks` `/api/manifest` `/api/authorities` | 定时表 / 任务 / 清单 / 地址扫描 |
| POST | `/api/plan` `/api/run` `/api/task_ctl` `/api/schedule` `/api/fix` `/api/cleanup` | 预览 / 生成 / 控制 / 定时增删 / 改地址 / 清理 |

## 数据文件

| 文件 | 内容 |
|---|---|
| `data/strm_manifest.json` | pickcode → {strm 落点, 115 源, srcdir, 大小, 时间} |
| `data/strm_schedules.json` | 定时任务（含参数快照、上次/下次运行、结果） |
| `data/self_sources.json` | 自存落点 → 115 来源对照（面包屑徽章用） |

## 没有移植的部分

- **保存到远程设备 / 下载方式（aria2、BitComet、IDM）**：那是把文件推给下载器，我们只写 strm 指针交给 MDCng/Emby，没有意义。
- 115-Desktop 的 GUI 细节（对话框暂停/后台按钮等）在本面板换成日志卡按钮与定时线程。

## 代码位置

- `scripts/strm_panel.py` — 面板（HTTP + 前端 + 生成/清理逻辑）
- `scripts/strm-panel.service` — systemd unit 的版本内副本（机器上生效的是 `/etc/systemd/system/strm-panel.service`，两边应保持一致）
- `scripts/strm_tools.py` — 从 115-Desktop 内部搬来的三块：`scan_authorities/fix_authority`、`Manifest`、`Scheduler`
- `scripts/backfill_scrape.py` — `collect_videos()` 支持自定义扩展名（原补刮脚本复用）

## 回滚

```bash
cd /root/clacky_workspace/sehuatang-emby-deliverable/scripts
cp strm_panel.py.bak-20260924-2130 strm_panel.py     # 界面优化前（保留旧外观、含 /api/status 之前的状态）
cp strm_panel.py.bak-20260924-205528 strm_panel.py    # 上一轮改造前（没有四个 tab 的版本）
systemctl restart strm-panel.service                  # 或 bash start_strm_panel.sh / 9080 面板上的 🔄 重启
```

在 9080 面板上的登记要一并回滚：`cp /root/mdc-ng/panel.py.bak-20260924-2140 /root/mdc-ng/panel.py && systemctl restart mdc-panel`。

**回退 systemd 托管**（改回裸进程）：

```bash
systemctl disable --now strm-panel.service
mv /etc/systemd/system/strm-panel.service /root/mdc-ng/backups/strm-panel.service.bak 2>/dev/null || true
systemctl daemon-reload
# 老版启动脚本（nohup 版）在 2026-09-24 前可从 git/备份取回；应急直接：
cd /root/clacky_workspace/sehuatang-emby-deliverable/scripts
setsid nohup python3 strm_panel.py --port 5091 >> /var/log/strm_panel.log 2>&1 &
```

同时要把 9080 面板 `strm-panel` 条目里的 `"systemd_unit"` 删掉（否则按钮会去 systemctl 一个不存在的 unit）。

`strm_tools.py` 为新增文件，回滚时留着无副作用（旧版 `strm_panel.py` 不引用它）。
`data/strm_manifest.json`、`data/strm_schedules.json` 可随时删除，删掉只影响增量判定与定时任务列表。
