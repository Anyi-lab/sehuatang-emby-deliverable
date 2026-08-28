# sehuatang-emby-deliverable — 本地部署恢复存档
> 存档时间: 2026-08-27 (暂停,用户手动安装油猴脚本后继续)
> 环境: WSL2, 网关 172.25.224.1 (Windows 宿主), G:\ = /mnt/g

## 当前状态 (暂停时)
- **import_api 已停止**(暂停时 pid 16317,端口 5081 已释放)
- 其余服务保持运行: panel.py :9080、MDCng(mdc 容器) :9208、115-Desktop socat :11522
- 油猴脚本已改好待用户手动安装(桌面副本 D:\Desktop\sehuatang_import.user.js)
- MDCng watch 目录已配好并生效:`/media/待看/sehuatang → /media/已刮削/AV`(硬链接模式)

## 一键恢复
```bash
cd /root/clacky_workspace/sehuatang-emby-deliverable/src/server && nohup python3 import_api.py > /var/log/import_api.log 2>&1 &
sleep 4 && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5081/   # 期望 200
```

## 关键配置与适配清单
| 项 | 值 |
|---|---|
| 项目根 | /root/clacky_workspace/sehuatang-emby-deliverable |
| 服务源码 | src/server/(import_api.py 已打补丁) |
| 115 适配层 | src/server/mcp115.py(新建, McpClient 单例 + Lock), fs115.py/cd2grpc.py(重写) |
| EMBY_URL | http://172.25.224.1:8096 (Jellyfin on Windows) |
| EMBY_TOKEN | **待用户提供 Jellyfin API key 后填入 import_api.py** |
| LOCAL_STRM_ROOT | /mnt/g/srtm/待看/sehuatang (剧集: sehuatang_tv) |
| MDC_TARGET_ROOT | /mnt/g/srtm/已刮削/AV |
| STRM_HOST | http://192.168.2.238:11500 (115-Desktop 302 直链,播放依赖其运行) |
| DB | src/server/import_log.db (表 import_log / import_log_history 已建) |
| MDCng 配置 | /root/mdc-ng/docker-data/config.json (备份 .bak-20260827) |
| mcp_lib | /root/clacky_workspace/mcp_lib_115.py (McpClient.call 通用) |

## 已完成
1. ✅ zip 解压 + 源码通读
2. ✅ 115-Desktop strm 实测: strm 内是 302 直链 (http://192.168.2.238:11500/d/<pickcode>/<任意名>.mp4 → 302 → 115 CDN)
3. ✅ 适配: 用 115 MCP (免认证) 替代 CD2 (拿不到 pickcode); import_api.py 补丁全部生效 (SmartStrm→本地 MCP 生成 strm, MDCng 负责刮削, Jellyfin 刷新)
4. ✅ MDCng watch 目录配置 + mdc 重启, 监控器已启动 (待看/sehuatang → 已刮削/AV)
5. ✅ import_api 启动验证: UI 200 (12229B), /api/import/status 正常, DB 表已建
6. ✅ 油猴脚本适配: API_BASE=http://127.0.0.1:5081, @connect 加 127.0.0.1/localhost

## 待办 (恢复后继续)
- [ ] 用户手动安装油猴脚本 (file:///D:/Desktop/sehuatang_import.user.js 或粘贴进 Tampermonkey)
- [ ] Jellyfin 启动 (Windows 8096 当前未运行) + 用户提供 API key → 填 EMBY_TOKEN
- [ ] 端到端测试: 用户提供 1 个真实磁力/ed2k → 跑完整流程 (推送115→等下载→strm→MDC刮削→Jellyfin刷新)
- [x] ~~import_api 挂到 panel.py(:9080) 一键启停~~ (已完成 2026-08-28, 卡片"色花堂入库 import-api", 启停/重启/日志/打开界面实测通过)
- [ ] 可选: 验证 115 目录 /sehuatang (当前不存在, 首次入库 mkdir 自动创建)

## 注意事项
- strm 内容为局域网 IP 192.168.2.238:11500, 播放依赖 115-Desktop 保持运行
- MDCng 对 sehuatang watch 目录是"硬链接"整理模式 (非移动); 若异常可改 link_mode
- import_api 启动日志有一条无害 WARNING (import_log 迁移 thread_url 失败: no such table, DB 随后已建表)
- 临时脚本: /tmp/probe_mcp_formats.py, probe_search.py, probe_listfile.py, test_import.py, test_mcp_backend.py, setup_mdc_watch.py, fix_prewarm.py
