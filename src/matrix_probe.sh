#!/bin/bash
# ==============================================================
#  点阵屏(9x9)集中测试  (matrix_probe.sh)
# --------------------------------------------------------------
#  目的：验证【buscmd 3005 → MscSuccessCommand → IconLedDesc.Show】
#        这条路能不能点亮阵，以及不同 payload 各是什么效果。
#
#  用法：  bash matrix_probe.sh          # 只打印时间表，不发命令（默认）
#          bash matrix_probe.sh --go     # ★真发（必须先确认主人就在音箱旁）
#
#  ★★★ 安全警告（务必读）
#   1. 3005 = MSC「快捷命令」通道，载荷里带【命令名】。
#      已知内建载荷有 "下一首" / "快捷命令WakeupbyMain" ——
#      ⇒ 某些 payload 很可能【顺带触发音乐播放 / 出声】。
#      因此本脚本默认 dry；--go 只能在主人明确同意、且人在现场时用。
#   2. 本脚本【从不】改音量、不碰网络/蓝牙/OTA/闹钟/云信。
#   3. 每条之间停 6 秒，方便肉眼分辨是哪一条起的效果。
# ==============================================================
set -u

GO=0
[ "${1:-}" = "--go" ] && GO=1

BUS=$(adb shell 'sed -n "s/.*DBUS_SESSION_BUS_ADDRESS=\"\([^\"]*\)\".*/\1/p" /tmp/dbus_env.sh 2>/dev/null' | tr -d '\r')
if [ -z "$BUS" ]; then
  BUS="unix:path=$(adb shell 'ls -d /tmp/dbus-* 2>/dev/null | head -1' | tr -d '\r')"
fi
CCLOG=$(adb shell 'ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1' | tr -d '\r')
echo "会话总线 : $BUS"
echo "CC 日志  : $CCLOG"
echo

send3005() {  # $1 = json 字符串
  local J="$1"
  local N
  N=$(printf '%s' "$J" | wc -c)
  adb shell "export DBUS_SESSION_BUS_ADDRESS=$BUS; dbus-send --session --print-reply --reply-timeout=400 --dest=netease.ihw.controller /netease/ihw/controller netease.ihw.SmartAudio.API uint32:3 uint32:1 uint32:3005 uint32:$N string:'$J' 2>&1 | head -1"
}

T=0
step() {  # $1=json $2=名称 $3=期望 $4=风险
  echo "──────────────────────────────────────────────────────"
  echo "▶ T+${T}s   $2"
  echo "    payload ： $1   (${#1} 字节)"
  echo "    期望     ： $3"
  [ -n "${4:-}" ] && echo "    ⚠ 风险   ： $4"
  if [ "$GO" != "1" ]; then return; fi
  send3005 "$1"
  sleep 6
  T=$((T+6))
}

echo "════════════ 点阵集中测试 · 时间表 ════════════"
echo
step '{}' '空 payload（对照）' '大概率无反应；若有反应说明入参被忽略' ''
step '{"confirmParam":"","data":1000}' 'confirmParam 空串' '★ 最可能只走 LED/点阵分支' '若出声立即 Ctrl-C'
step '{"clientParam":"","data":1000}' 'clientParam 空串' '同上（另一字段）' '若出声立即 Ctrl-C'
step '{"confirmParam":"","recType":0,"playerStatus":""}' '带 recType/playerStatus 的完整形' '看是否走语音会话分支' '若出声立即 Ctrl-C'

cat <<'EOF'

════════════ 以下两条是【已知内建载荷】，默认不放进自动流程 ════════════
  它们是反汇编里实证的真实值，最可能"有反应"，但也最可能【出声】：
      {"confirmParam":"下一首","data":1000}
      {"confirmParam":"快捷命令WakeupbyMain","data":1000}
  ⇒ 想测请手动执行（人在音箱旁、随时能断电）：
      bash matrix_probe.sh --go        # 先跑上面 4 条
      # 再单独手动发：
      #   send3005 '{"confirmParam":"下一首","data":1000}'
EOF

if [ "$GO" != "1" ]; then
  echo
  echo "（默认 dry：未发送任何命令。真发加 --go，且务必人在现场。）"
  exit 0
fi

echo
echo "════════════ 测完，CC 日志尾部 30 行 ════════════"
adb shell "tail -n 30 $CCLOG" 2>&1 | grep -v -e 'Tina is Based' -e 'BusyBox v1' -e '^ *|' -e '^root@'
echo
echo "（核对：每条该有一行 'modId msgmsk buscmd msglen: … 3005 <n> <json>'；"
echo "  若紧跟 '[W] Unknow bus command' 说明没被认；"
echo "  若点阵亮了，记下是哪条 payload。）"
