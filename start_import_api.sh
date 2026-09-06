#!/bin/bash
# 色花堂入库 import-api 启动脚本
# 用法: bash /root/clacky_workspace/sehuatang-emby-deliverable/start_import_api.sh
#
# 2026-09-04: 服务已托管给 systemd (sehuatang-import.service, 开机自启 + 崩溃重启)。
# 本脚本保留为兼容入口, 直接委托给 systemctl —— 不能再用 nohup 起裸进程,
# 否则会和 systemd 那份同时抢 5081 端口。
set -e
UNIT=sehuatang-import.service

if systemctl list-unit-files "$UNIT" >/dev/null 2>&1 && \
   [ -f /etc/systemd/system/$UNIT ]; then
  echo "[1/2] 通过 systemd 重启 $UNIT ..."
  systemctl restart "$UNIT"
  sleep 3
else
  # systemd 不可用时的回退路径 (例如 wsl.conf 未开 systemd)
  echo "[1/2] systemd 未托管, 回退到 nohup 方式; 停止旧进程..."
  pkill -f "import_api.py" 2>/dev/null || true
  sleep 1
  echo "[2/2] 启动 import-api (端口 5081)..."
  cd /root/clacky_workspace/sehuatang-emby-deliverable/src/server
  nohup python3 /root/clacky_workspace/sehuatang-emby-deliverable/src/server/import_api.py > /var/log/import_api.log 2>&1 &
  sleep 3
fi

# 健康检查
if curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:5081/ | grep -q 200; then
  echo "✅ 色花堂入库 import-api 已启动: http://127.0.0.1:5081/"
else
  echo "❌ 启动失败，查看日志: tail -20 /var/log/import_api.log"
  exit 1
fi
