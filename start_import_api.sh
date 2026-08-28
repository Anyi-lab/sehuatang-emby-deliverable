#!/bin/bash
# 色花堂入库 import-api 启动脚本
# 用法: bash /root/clacky_workspace/sehuatang-emby-deliverable/start_import_api.sh
set -e
cd /root/clacky_workspace/sehuatang-emby-deliverable/src/server

echo "[1/2] 停止旧进程..."
pkill -f "import_api.py" 2>/dev/null || true
sleep 1

echo "[2/2] 启动 import-api (端口 5081)..."
nohup python3 /root/clacky_workspace/sehuatang-emby-deliverable/src/server/import_api.py > /var/log/import_api.log 2>&1 &
sleep 3

# 健康检查
if curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:5081/ | grep -q 200; then
  echo "✅ 色花堂入库 import-api 已启动: http://127.0.0.1:5081/"
else
  echo "❌ 启动失败，查看日志: tail -20 /var/log/import_api.log"
fi
