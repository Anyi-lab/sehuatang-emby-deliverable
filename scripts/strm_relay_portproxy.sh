#!/bin/bash
# 把 Windows 侧 0.0.0.0:11501 转发到当前 WSL 的 11501（WSL IP 每次重启会变，故开机重设）
# 这样 strm 里就能写一个稳定地址 http://192.168.2.238:11501/d/<pickcode>/<文件名>
set -u
NETSH=/mnt/c/Windows/System32/netsh.exe
PORT=11501
IP=$(ip -4 addr show eth0 | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
if [ -z "${IP:-}" ]; then
  echo "拿不到 WSL IP，跳过 portproxy 设置"
  exit 1
fi
"$NETSH" interface portproxy delete v4tov4 listenport=$PORT listenaddress=0.0.0.0 >/dev/null 2>&1
"$NETSH" interface portproxy add v4tov4 listenport=$PORT listenaddress=0.0.0.0 connectport=$PORT connectaddress="$IP" >/dev/null 2>&1
if "$NETSH" interface portproxy show all 2>/dev/null | tr -d '\r' | grep -q " $PORT "; then
  echo "portproxy 已设置: 0.0.0.0:$PORT -> $IP:$PORT"
else
  echo "portproxy 设置失败（可能需要管理员权限）"
  exit 1
fi
