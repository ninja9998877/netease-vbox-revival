#!/bin/sh
# muxtrace.sh —— 0.15 秒一轮盯着 HP_L Mux。一旦翻转，立刻记下时间戳 + 当时谁开着声卡。
# 只读、不出声。日志只在【变化时】写（NAND 友好），另加 10 分钟一次心跳。
# 目的：揪出"到底是谁把 HP_L Mux 压回 [1]"（是开机时的音频栈初始化？某次录音？某次播放？）
L=/mnt/UDISK/spk/muxtrace.log
PIDF=/tmp/muxtrace.pid

# 单实例锁：mkdir 保原子性（纯 pidfile 会有竞态，实测起过两份）+ pid 核实保崩溃安全。
# ★ 不能只看"那个 pid 还活着"（start-stop-daemon -m 会先写 pidfile，那样一上来就把
#   自己当成"已经在跑"而静静退出），也不能用"cmdline 里含 muxtrace"（外层 adb shell
#   会把整条命令串塞进自己 cmdline，误判成活着）。只认 cmdline 的【最后一个字段】。
mkdir -p /tmp/spk
LOCK=/tmp/spk/muxtrace.lock
for _t in 1 2 3; do
  mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
  _op=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "$_op" ] && [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
    exit 0
  fi
  rm -rf "$LOCK"
done

get() { amixer -c 0 cget name='HP_L Mux' 2>/dev/null | sed -n 's/^ *: values=//p'; }

prev=$(get)
echo "$(date '+%m-%d %H:%M:%S') 启动：HP_L Mux=$prev（0=正确DACL / 1=全哑）" >> "$L"

n=0
while true; do
  cur=$(get)
  if [ "$cur" != "$prev" ]; then
    {
      echo "=== $(date '+%m-%d %H:%M:%S')  HP_L Mux  $prev -> $cur ==="
      echo "-- 谁开着声卡 --"
      ls -l /proc/*/fd 2>/dev/null | grep -i 'snd/'
      echo "-- 这些 pid 是谁 --"
      for p in $(ls -l /proc/*/fd 2>/dev/null | grep -i 'snd/' | sed 's|/proc/\([0-9]*\)/fd.*|\1|' | sort -u); do
        printf '   pid=%s comm=%s cmd=%s\n' "$p" "$(cat /proc/$p/comm 2>/dev/null)" "$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)"
      done
      echo "-- 进程表 --"
      ps 2>/dev/null
      echo
    } >> "$L"
    prev=$cur
  fi
  n=$((n+1))
  if [ $((n % 4000)) -eq 0 ]; then
    echo "$(date '+%m-%d %H:%M:%S') 心跳 $n 轮（约 10 分钟），仍是 $prev" >> "$L"
  fi
  sleep 0.15
done
