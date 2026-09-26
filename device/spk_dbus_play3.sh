#!/bin/sh
# ★ 正确的播放指令（2026-09-21 从 dbus-monitor 全量抓取对照得到）
#
# 签名: API(uint32 flag, uint32 svcId, uint32 cmd, uint32 jsonLen, string json)
#
# body[1] 是【目标服务的编号】，跟 JSON 里的 playerId 是两回事：
#     voice_engine = 8      player = 16      bt = 256
# 第一版照着 voice_engine 那条的 playerId:8 抄成了 2，指令上了总线但 player 不理会
# （没有 0x0800 起播上报、也不来取文件）。
# 生效的那条长这样: sender=:1.8 -> netease.ihw.player  0, 16, 1537, 120, "{...}"
#
# 用法: sh spk_dbus_play3.sh [src]

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

. /tmp/dbus_env.sh

SRC=${1:-http://$SPK_SRV:$SPK_PORT_MP3/dbus_cmd_test.mp3}
JSON='{"playerId":2,"src":"'"$SRC"'","srcUuid":"'"$SRC"'","gain":0,"backGroundUrl":""}'
LEN=${#JSON}

echo "flag=0 svcId=16 cmd=0x0601 jsonLen=$LEN"
echo "src=$SRC"
dbus-send --session \
  --dest=netease.ihw.player \
  /netease/ihw/player \
  netease.ihw.SmartAudio.API \
  uint32:0 uint32:16 uint32:1537 uint32:"$LEN" string:"$JSON"
echo "rc=$?"
