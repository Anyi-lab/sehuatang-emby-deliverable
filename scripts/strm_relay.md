# strm_relay.py —— 115 STRM 本地中转（部署与排障）

> 本文件是 `strm-relay.service` / `strm-relay-portproxy.service` 里 `Documentation=` 指向的那份说明。

## ⛔ 当前状态：已停用归档（2026-09-26）

**这个中继现在不跑，也不开机自启。** 它解决的那个根因（Emby 取远端字节不带 UA → 115 CDN 403/500）
已由 Emby 插件 **`Emby.StrmUaFix`** 在服务器进程内补 UA 根治，于是全库 strm 回到 11500 直连，
中继变成绕远路、纯冗余 —— 实测全库 8877 个 strm 里 **11501 残留 = 0**，仓库/面板/流水线无任何运行时消费者。

| 项 | 现状 |
|---|---|
| `strm-relay.service` | `disabled` / `inactive` |
| `strm-relay-portproxy.service` | `disabled` / `inactive` |
| WSL `:11501` | 未监听 |
| Windows portproxy `0.0.0.0:11501` | **规则已删**（其余 6 条规则未动） |
| 代码 | 全部**原位保留**（`strm_relay.py` / `strm_relay.md` / 两个 unit 文件） |

**一键开关**（幂等，推荐用它，别手敲 systemctl）：

```bash
bash scripts/strm_relay_toggle.sh status                 # 看现状（服务 / 端口 / portproxy / 面板 host）
bash scripts/strm_relay_toggle.sh on                     # 只把服务拉起来（strm 仍指 11500）
bash scripts/strm_relay_toggle.sh on --switch-strm       # 完整回退：拉起 + 全库 strm 切 11501 + 面板切 host
bash scripts/strm_relay_toggle.sh off                    # 再次停用（去自启 + 删 portproxy 规则）
bash scripts/strm_relay_toggle.sh off --fix-strm         # 同上，并把全库 strm 的 11501 改回 11500
```

**什么时候才该切回中继**：Emby 大版本升级把插件打挂（日志出现「补丁未挂载」、播放回 500）
且在插件修好前需要临时恢复播放。切回约 1 分钟（全库 portfix ~50 秒 + 重启面板），**不是热备**。

## 它解决什么问题

strm 内容是 `http://192.168.2.238:11500/d/<pickcode>/<文件名>`（115-Desktop 视频代理服务器）。
115-Desktop 的 `/d/` 是**下载路由**，恒 302 到 `https://cdnfhnfile.115cdn.net/...`，而该直链的签名
**绑定请求方的 User-Agent**。Emby 在「探测 / 取流」两跳里 UA 不一致（ffprobe vs 播放器 vs 服务器代理），
于是 403（Emby 侧表现为 `stream?static=true → 500 Forbidden`，客户端表现为"无兼容视频流"或黑屏）。

中继把这一跳收进本地：**固定 UA 取链 + 固定 UA 取流**，UA 永远一致，403 从根上消失。
顺带解决实测到的另外两类上游故障：Emby 自带 ffprobe 走 CDN 偶发
`TLS Unable to decrypt message (0x80090330)`、以及 CDN 传输中途截断。

## 工作原理

```
客户端 / Emby ──► http://192.168.2.238:11501/d/<pickcode>/<名>
                     │
                     ├─(1) 用固定 UA 向 11500 要直链：GET :11500/d/<pickcode>/<名> → 302 Location
                     ├─(2) 跟随该 Location 到 115 CDN 取字节（保留客户端 Range）
                     └─(3) 边取边流给客户端
                     出错自动重试：重新取链（刷新 t=/k=）、按"已送达字节数"Range 续传
```

- 客户端 UA 随便（Lavf / Emby / 播放器 / 空 UA 都行），中继不转发它。
- 支持 `GET` / `HEAD`，支持 `Range`，支持拖动（中途 seek 会重新取链）。
- 路径前缀 `/d/`、`/play/`、`/strm/` 都接受（与上游路由同名），`/health` 返回 `{"status":"ok"}`。
- **它是反代（搬字节），不是跳转器**：它不会给客户端发 302。所以全库播放会经过 WSL 一跳。

## 端口与部署

| 项 | 值 |
|---|---|
| 监听 | `0.0.0.0:11501`（WSL 内） |
| Windows 侧入口 | portproxy `0.0.0.0:11501 → <WSL IP>:11501` |
| systemd | `strm-relay.service`（`Restart=always`；**2026-09-26 起 disabled/inactive**） |
| 端口刷新 | `strm-relay-portproxy.service`（WSL IP 变化后重跑，幂等） |
| 日志 | `/var/log/strm_relay.log`（journal 只有启停行） |
| 上游 | 默认 `http://192.168.2.238:11500`，`--upstream` 可覆盖 |
| 固定 UA | 默认 `Lavf/60.16.100`，`--ua` 可覆盖 |

手工跑（排障用）：

```bash
python3 scripts/strm_relay.py --host 0.0.0.0 --port 11501 --upstream http://192.168.2.238:11500
```

## 验收 / 排障

```bash
systemctl status strm-relay --no-pager
tail -n 30 /var/log/strm_relay.log
ss -lntp | grep 11501
tail -n 20 /var/log/strm_relay_portproxy.log 2>/dev/null
```

Windows 侧（从 WSL 调 curl.exe）：

```bash
/mnt/c/Windows/System32/curl.exe -s -o /dev/null -w '%{http_code}\n' \
  -H 'Range: bytes=0-1023' \
  'http://192.168.2.238:11501/d/<pickcode>/<文件名>'
# 期望 206；返回 302/403/000 分别对应：上游变了 / UA 绑定变了 / 中继或 portproxy 没起
```

Emby 侧端到端（`scripts/` 同级，用 python 直连 Emby API）：

```bash
python3 - <<'PY'
# 见 HANDOVER 文档里的 verify_emby 片段；期望 206 + video/mp4 + ftyp isom
PY
```

## 已知限制

- **单点**（仅在中继态成立）：一旦 strm 全部指 11501，WSL 或本服务挂了全库都不播。
  当前是 11500 直连，单点回到 115-Desktop 那一侧。
- **多一跳**：字节两次过 WSL（客户端→Emby→中继→CDN）。
- **不省带宽**：要省只能改 `--mode redirect`（UA 回显后发 302），目前**未实现**，且要求客户端
  取链/取流同一 UA、能直连 CDN —— 会挑客户端，不建议默认开。
- 与 115-Desktop 的 **WebDAV 默认端口 11501 冲突**：WebDAV 关闭状态下无碍，别开。

## 变更记录

- 2026-09-24 初始版本：固定 UA 取链 + 取流 + 断点续传 + 重取链；
  同日全库 strm（8596 个）端口 `11500 → 11501`，见 `docs/05-*.md`。
- 2026-09-25 全库 strm 端口反转回 `11500`（Emby 插件 `Emby.StrmUaFix` 补 UA 根治 403），
  本中继保留为回退路径，见 `docs/05-*.md` 第九节。
- 2026-09-26 **停用归档**：`disable --now` 两个 unit + 删 Windows portproxy 规则；
  新增一键开关 `scripts/strm_relay_toggle.sh`。回退用 `... toggle.sh on --switch-strm`。
