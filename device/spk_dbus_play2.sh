#!/bin/sh
# 带 flag 参数的播放指令，用来定位 body[0] 该填什么。
# 用法: sh spk_dbus_play2.sh <flag> [src]
#
# 实测(2026-09-21)：flag=0 + playerId=2 + cmd=0x0601 → 指令确实上了总线，
# 但音箱不动（没有 0x0800 起播上报、不来取文件）。DLNA 那条生效的 0x0601
# 用的也是 playerId:2，所以差异只可能在 flag 或发送者身份上。

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

. /tmp/dbus_env.sh

FLAG=${1:-0}
SRC=${2:-http://$SPK_SRV:$SPK_PORT_MP3/dbus_cmd_test.mp3}
JSON='{"playerId":2,"src":"'"$SRC"'","srcUuid":"'"$SRC"'","gain":0,"backGroundUrl":""}'
LEN=${#JSON}

echo "flag=$FLAG playerId=2 cmd=0x0601 jsonLen=$LEN"
dbus-send --session \
  --dest=netease.ihw.player \
  /netease/ihw/player \
  netease.ihw.SmartAudio.API \
  uint32:"$FLAG" uint32:2 uint32:1537 uint32:"$LEN" string:"$JSON"
echo "rc=$?"
