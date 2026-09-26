#!/bin/bash
# ==============================================================
#  灯环状态集中测试  (ring_probe.sh)
# --------------------------------------------------------------
#  目的：一次跑完，摸清「哪条 buscmd → 灯环什么效果」的映射。
#        主人只需站在音箱旁边看灯，对照下面打印的时间表。
#
#  用法：  bash ring_probe.sh          # 真跑
#          bash ring_probe.sh --dry    # 只打印时间表
#
#  安全保证：
#    · 只发【实测被 CC 认过】的命令；
#    · 不出声 / 不改音量 / 不碰网络/蓝牙/OTA/闹钟/云信；
#    · 每条之间停 5 秒，方便肉眼分辨是哪一条起的效果。
# ==============================================================
set -u

DRY=0
[ "${1:-}" = "--dry" ] && DRY=1

# ---------- 找到会话总线地址（每次开机随机）----------
BUS=$(adb shell 'sed -n "s/.*DBUS_SESSION_BUS_ADDRESS=\"\([^\"]*\)\".*/\1/p" /tmp/dbus_env.sh 2>/dev/null' | tr -d '\r')
if [ -z "$BUS" ]; then
  BUS="unix:path=$(adb shell 'ls -d /tmp/dbus-* 2>/dev/null | head -1' | tr -d '\r')"
fi
echo "会话总线 : $BUS"

CCLOG=$(adb shell 'ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1' | tr -d '\r')
echo "CC 日志  : $CCLOG"
echo

send() {  # $1=buscmd  $2=modId
  adb shell "export DBUS_SESSION_BUS_ADDRESS=$BUS; dbus-send --session --print-reply --reply-timeout=400 --dest=netease.ihw.controller /netease/ihw/controller netease.ihw.SmartAudio.API uint32:$2 uint32:1 uint32:$1 uint32:2 string:\"{}\" 2>&1 | head -1"
}

T=0
step() {  # $1=buscmd $2=modId $3=名称 $4=期望灯效 $5=备注
  echo "──────────────────────────────────────────────────────"
  echo "▶ T+${T}s    [$1]  $3"
  echo "    期望灯环 ： $4"
  [ -n "${5:-}" ] && echo "    注意     ： $5"
  if [ "$DRY" = "1" ]; then return; fi
  send "$1" "$2"
  sleep 5
  T=$((T+5))
}

echo "════════════ 灯环集中测试 · 时间表 ════════════"
echo
step 2564 3 "RECOGING        倾听中"        "白灯常亮（＝免唤醒监听态）"  ""
step 2563 3 "SESSION_BEGIN   会话开始"     "可能是过渡效果"              ""
step 2562 3 "WAKEUP          唤醒"          "★ 很可能就是「转圈流动」"     "会打开免唤醒窗，测完等它自己过期"
step 2560 3 "RECOG_SUCCESS   识别成功"     "★ 应该是绿色"                ""
step 2561 3 "RECOG_ERROR     识别失败"     "红色 / 错误效果"             ""
step 2567 3 "INITARGS_REQ    只读查询"     "对照：应无变化"              ""
step 2569 3 "PLAYERSTATUS    只读查询"     "对照：应无变化"              ""
step 2048 0 "未知命令 2048"                "？被认但效果未知"            ""
step 2564 3 "RECOGING        收尾"         "回到白灯常态"                ""

if [ "$DRY" = "1" ]; then
  echo
  echo "（--dry：未发送任何命令）"
  exit 0
fi

echo
echo "════════════ 测完，CC 日志尾部 30 行 ════════════"
adb shell "tail -n 30 $CCLOG" 2>&1 | grep -v -e 'Tina is Based' -e 'BusyBox v1' -e '^ *|' -e '^root@'
echo
echo "（核对：每条命令都该有一行 'modId msgmsk buscmd msglen: … <值> 2 {}'；"
echo "  若某条后面紧跟 '[W] Unknow bus command'，说明它其实不被认。）"
