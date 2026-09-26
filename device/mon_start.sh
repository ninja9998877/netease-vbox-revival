#!/bin/sh
# 把 DBus 全量抓进 /tmp/mon.txt。
# ★ 必须挂独立会话（start-stop-daemon -b）：adb shell 一退出就发 SIGHUP，
#   直接 `dbus-monitor &` 会被带走，文件只剩半行。
# ★ 输出到文件是块缓冲，所以要让它跑够久再去读，别用 timeout 掐。
# ★ 必须给 -p 独立 pidfile：不给的话它按可执行名(/bin/sh)匹配，
#   系统里本来就一堆 sh 在跑，于是报 "already running" 直接拒绝启动。
. /tmp/dbus_env.sh
rm -f /tmp/mon.txt /tmp/mon.pid
start-stop-daemon -S -b -p /tmp/mon.pid -m \
  -x /bin/sh -- -c 'exec /usr/bin/dbus-monitor --session > /tmp/mon.txt 2>&1'
sleep 1
echo "monitor pid(s):"
for p in /proc/[0-9]*; do
    [ "$(cat $p/comm 2>/dev/null)" = "dbus-monitor" ] && echo "  ${p#/proc/}"
done
ls -la /tmp/mon.txt
