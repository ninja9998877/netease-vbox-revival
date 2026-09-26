#!/bin/sh
# 用 DBus 让音箱【自己的播放器】播一段音频 —— 这是"指令下发"的验证。
#
# 与 DLNA 那条路的区别：DLNA 是外部塞进来的（绕过设备音量），
# 这条是走 player 的 API，等同于音箱自己决定要播 —— 是"排队"不是"抢"。
#
# 签名（从 dbus-monitor 原始输出反推，2026-09-21）：
#   API(uint32 flag, uint32 playerId, uint32 cmd, uint32 jsonLen, string json)
# ★ jsonLen 是 json 的【字节数】，必须精确，否则服务端解析失败。

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

. /tmp/dbus_env.sh

SRC="http://$SPK_SRV:$SPK_PORT_MP3/dbus_cmd_test.mp3"
JSON='{"playerId":2,"src":"'"$SRC"'","srcUuid":"'"$SRC"'","gain":0,"backGroundUrl":""}'
LEN=${#JSON}

echo "DBUS=$DBUS_SESSION_BUS_ADDRESS"
echo "jsonLen=$LEN"
echo "json=$JSON"

dbus-send --session \
  --dest=netease.ihw.player \
  /netease/ihw/player \
  netease.ihw.SmartAudio.API \
  uint32:0 uint32:2 uint32:1537 uint32:"$LEN" string:"$JSON"
echo "dbus-send rc=$?"
