#!/bin/bash
# =============================================================================
# strm_relay_toggle.sh —— 115 STRM 本地中继的「停用 / 恢复」开关（幂等）
#
# 背景：全库 strm 已统一指向 11500（115-Desktop 直发），Emby 侧由插件
#       Emby.StrmUaFix 补 UA，403 根因已消。2026-09-26 起中继（strm-relay :11501）
#       停用归档，只作 Emby 大版本升级把插件打挂时的回退路。
#       说明：scripts/strm_relay.md ／ docs/05-strm播放修复记录-20260924.md 第十节
#
# 用法：
#   bash scripts/strm_relay_toggle.sh off               # 停用 + 去自启 + 删 portproxy 规则
#   bash scripts/strm_relay_toggle.sh off --fix-strm    # 同上，并把全库 strm 的 11501 改回 11500
#   bash scripts/strm_relay_toggle.sh on                # 只把服务拉起来（strm 仍指 11500）
#   bash scripts/strm_relay_toggle.sh on --switch-strm  # 拉起 + 全库 strm 切回 11501 + 面板切 host
#   bash scripts/strm_relay_toggle.sh status            # 看现状
# =============================================================================
set -u

REPO="/root/clacky_workspace/sehuatang-emby-deliverable"
NETSH="/mnt/c/Windows/System32/netsh.exe"
PORT=11501
UPSTREAM=11500
STRM_ROOT="/mnt/g/srtm"
UNIT_DIR="/etc/systemd/system"
SERVICES=("strm-relay.service" "strm-relay-portproxy.service")
CONSUMERS=("strm-panel" "sehuatang-import")

# 中继态：消费者（面板/入库流水线）也要写 11501，靠 drop-in 注入环境变量
relay_dropins() { echo "/etc/systemd/system/strm-panel.service.d" "/etc/systemd/system/sehuatang-import.service.d"; }

wsl_ip() { ip -4 addr show eth0 | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}'; }
pp_show() { "$NETSH" interface portproxy show all 2>/dev/null | tr -d '\r'; }

do_status() {
  echo "== systemd =="
  for s in "${SERVICES[@]}"; do
    printf '%-30s enabled=%-9s active=%s\n' "$s" \
      "$(systemctl is-enabled "$s" 2>&1)" "$(systemctl is-active "$s" 2>&1)"
  done
  echo "== WSL 监听 =="
  ss -ltnp 2>/dev/null | grep -E ":$PORT\b" || echo "  ($PORT 未监听)"
  echo "== Windows portproxy :$PORT =="
  if pp_show | grep -qE " $PORT[[:space:]]"; then
    pp_show | grep -E '地址|端口|---| '$PORT' '
  else
    echo "  (无 $PORT 规则)"
  fi
  echo "== 消费者 drop-in =="
  for d in $(relay_dropins); do
    [ -f "$d/strm-host.conf" ] && echo "  $d/strm-host.conf -> $(grep -h STRM_HOST "$d/strm-host.conf")" || echo "  $d/strm-host.conf (无)"
  done
  echo "== 服务健康 =="
  systemctl is-active strm-panel sehuatang-import 2>&1 | paste -sd' ' - | sed 's/^/  strm-panel sehuatang-import: /'
}

portproxy_delete() {
  "$NETSH" interface portproxy delete v4tov4 listenport=$PORT listenaddress=0.0.0.0 >/dev/null 2>&1
  if pp_show | grep -qE " $PORT[[:space:]]"; then
    echo "  [!!] portproxy 规则删除失败（需要管理员权限？）"
    return 1
  fi
  echo "  [ok] portproxy 0.0.0.0:$PORT 已删除"
}

do_off() {
  local fix_strm="${1:-}"
  echo "== 1) 停服务 + 去自启 =="
  systemctl disable --now "${SERVICES[@]}" 2>&1 | tr -d '\r' | sed 's/^/  /'

  echo "== 2) 删 Windows portproxy 规则 =="
  portproxy_delete

  echo "== 3) 清消费者 drop-in（服务回到默认 11500）=="
  for d in $(relay_dropins); do
    if [ -f "$d/strm-host.conf" ]; then
      rm -f "$d/strm-host.conf"; rmdir "$d" 2>/dev/null
      echo "  [ok] 移除 $d/strm-host.conf"
    else
      echo "  [--] $d/strm-host.conf 不存在"
    fi
  done
  systemctl daemon-reload
  systemctl restart "${CONSUMERS[@]}" 2>&1 | tr -d '\r' | sed 's/^/  /'

  if [ "$fix_strm" = "--fix-strm" ]; then
    echo "== 4) 全库 strm :$PORT -> :$UPSTREAM =="
    ( cd "$REPO" && python3 scripts/strm_portfix.py --root "$STRM_ROOT" \
        --from-port $PORT --to-port $UPSTREAM --apply )
  else
    echo "== 4) 全库 strm 端口未动（要一起改回 ${UPSTREAM} 就加 --fix-strm）=="
  fi
  echo "== 完成 =="
  do_status
}

do_on() {
  local switch_strm="${1:-}"
  echo "== 1) 装 unit + 起服务 =="
  for s in "${SERVICES[@]}"; do
    if [ -f "$REPO/scripts/$s" ]; then
      cp -f "$REPO/scripts/$s" "$UNIT_DIR/$s"; echo "  [ok] 安装 $UNIT_DIR/$s"
    else
      echo "  [!] 缺 $REPO/scripts/$s，沿用 $UNIT_DIR/$s"
    fi
  done
  systemctl daemon-reload
  systemctl enable --now "${SERVICES[@]}" 2>&1 | tr -d '\r' | sed 's/^/  /'
  sleep 2

  if [ "$switch_strm" = "--switch-strm" ]; then
    echo "== 2) 全库 strm :$UPSTREAM -> :$PORT =="
    ( cd "$REPO" && python3 scripts/strm_portfix.py --root "$STRM_ROOT" \
        --from-port $UPSTREAM --to-port $PORT --apply )

    echo "== 3) 消费者切 host（drop-in 注入 STRM_HOST）=="
    for name in "${CONSUMERS[@]}"; do
      d="/etc/systemd/system/$name.service.d"; mkdir -p "$d"
      printf '[Service]\nEnvironment=STRM_HOST=http://192.168.2.238:%s\n' "$PORT" > "$d/strm-host.conf"
      echo "  [ok] $d/strm-host.conf"
    done
    systemctl daemon-reload
    systemctl restart "${CONSUMERS[@]}" 2>&1 | tr -d '\r' | sed 's/^/  /'
    sleep 2
    echo "  host 现状: $(curl -s --max-time 5 http://127.0.0.1:5091/api/status | python3 -c 'import sys,json;print(json.load(sys.stdin).get("host"))' 2>/dev/null || echo '取不到')"
  else
    echo "== 2) 服务已起，但 strm 仍指 ${UPSTREAM}（要真切回中继就加 --switch-strm）=="
    echo "== 3) portproxy 刷新 =="
    systemctl restart strm-relay-portproxy 2>&1 | tr -d '\r' | sed 's/^/  /'
  fi
  echo "== 完成 =="
  do_status
}

case "${1:-status}" in
  off)    shift; do_off "${1:-}" ;;
  on)     shift; do_on "${1:-}" ;;
  status) do_status ;;
  *) echo "用法: $0 {off [--fix-strm] | on [--switch-strm] | status}"; exit 2 ;;
esac
