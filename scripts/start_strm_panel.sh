#!/bin/bash
# 115 → strm 生成面板 启动脚本
# 用法: bash /root/clacky_workspace/sehuatang-emby-deliverable/scripts/start_strm_panel.sh
#
# 2026-09-24 起本面板已托管给 systemd: /etc/systemd/system/strm-panel.service
#   systemctl {start|stop|restart|status} strm-panel.service
# 本脚本保留为「等价入口」: 直接转发给 systemctl, 避免有人手工 nohup 起第二份
# 进程抢 5091 端口 (systemd 那份会一直活着, 两份互抢必然出乱子)。
# 9080「本地服务管理面板」的 strm-panel 条目也已带 systemd_unit, 走 systemctl。
set -u
UNIT=strm-panel.service
DIR=/root/clacky_workspace/sehuatang-emby-deliverable/scripts
PORT=5091
LOG=/var/log/strm_panel.log

if systemctl list-unit-files "$UNIT" 2>/dev/null | grep -q "^$UNIT"; then
  echo "[1/2] systemctl restart $UNIT ..."
  systemctl restart "$UNIT" || { echo "❌ systemctl 失败"; exit 1; }
  sleep 3
  echo "[2/2] 健康检查 http://127.0.0.1:$PORT/ ..."
  if curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" | grep -q 200; then
    echo "✅ strm 面板已启动 (systemd 托管): http://127.0.0.1:$PORT/"
    exit 0
  fi
  echo "❌ 启动失败, 查: systemctl status $UNIT / tail -20 $LOG"
  exit 1
fi

# ---- 兜底: unit 不存在时退回裸进程方式 (仅应急用) ----
echo "[warn] 未找到 $UNIT, 退回裸进程方式 (应急)"
pkill -f "strm_panel.py --port $PORT" 2>/dev/null || true
for i in $(seq 1 10); do
  ss -ltn 2>/dev/null | grep -q ":$PORT " || break
  sleep 1
done
cd "$DIR" || exit 1
setsid nohup python3 strm_panel.py --port "$PORT" >> "$LOG" 2>&1 &
sleep 3
if curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" | grep -q 200; then
  echo "✅ strm 面板已启动 (裸进程): http://127.0.0.1:$PORT/"
else
  echo "❌ 启动失败, 查看日志: tail -20 $LOG"
  exit 1
fi
