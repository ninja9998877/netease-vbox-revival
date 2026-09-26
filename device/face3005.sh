#!/bin/sh
# face3005.sh —— 设备侧"点阵"出口：经 D-Bus 发 buscmd 3005，让总控驱动正面那块 9×9 点阵。
#
# 用法：
#   face3005.sh '<confirmParam>' [data毫秒]     例：face3005.sh '快捷命令WakeupbyMain' 1000
#
# ★ 它和 play601.sh 是同一个模子（0x601 管嗓子，这条管屏）。为什么也要放设备本地：
#   ① spk_net.sh 的环境里【没有 DBUS_SESSION_BUS_ADDRESS】（/proc/<pid>/environ 只有 HOME/PATH），
#      而 dbus-send 不知道去哪条总线时，命令在写进 socket 之前就被丢掉 —— 一声不响。
#   ② 会话总线的 socket 路径每次开机都变（/tmp/dbus-XXXXXXXXXX），必须现找。
#   ③ 让 macmini 直接拼整条 dbus-send ⇒ 中文 JSON 的引号要穿过两层 shell（macmini → cmd 文件
#      → 设备的 sh -c），错一个字符就是 ① 那个症状。
#   ⇒ 把"怎么发"收在设备本地，macmini 侧只发一条短命令。
#
# ★★★ 两个 dbus-send 参数是【实测定的】，别按直觉改（理由逐字见 play601.sh 文件头）：
#   ① 必须有 --print-reply  —— 去掉它 dbus-send 不等 flush 就断开，消息烂在队列里，
#      而它自己 rc=0、一声不响（"看起来成功、其实没送到"）。
#   ② 超时必须小（这里 200ms）—— 总控收到就干活、从不回 reply，dbus-send 必然干等到超时
#      再吐 NoReply。那 200ms 只是它自己退出前的等待，命令早就到了。
#
# ★★★ 风险（主人立的规矩，见记忆 [[netease-vbox-led-matrix]] §十 铁律 4）：
#   **不知道哪个 confirmParam 会顺带触发音乐播放 / 出声。**
#   ⇒ 这条脚本【第一次真发必须主人在场】，一次只发一个值、盯着音箱。
#   ⇒ 所以它【绝不】批量发、绝不循环发；一次调用只发一条。
#
# ★ 它【不碰】音量（numid 19 / DAC volume）、不碰夜间静音、不碰串口、不碰网络。
#   走的是设备自己的 DBus 方法，不是往 /dev/ttyS2 盲写
#   （同颗 MCU 还管音量 ADC 与触摸键，盲写是找死）。
#
# ★ 判成败【不要只看这里的输出】：总控的日志才是判据（`modId msgmsk buscmd msglen: 3 1 3005 <n>`，
#   紧跟 `[W] Unknow bus command` 说明没被认）。这里回的那行只是给 macmini 看的收条。

PARAM=$1
DATA=${2:-1000}

if [ -z "$PARAM" ]; then
	echo "✗ 用法: face3005.sh '<confirmParam>' [data毫秒]"
	exit 2
fi

BUS=$(ls /tmp/dbus-* 2>/dev/null | head -1)
if [ -z "$BUS" ] || [ ! -S "$BUS" ]; then
	echo "✗ 找不到会话总线（/tmp/dbus-* 里没有 socket）—— dbus-daemon --session 没在跑？"
	exit 1
fi
DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS"
export DBUS_SESSION_BUS_ADDRESS

JSON='{"confirmParam":"'"$PARAM"'","data":'"$DATA"'}'

# ★ msgSize 是【字节数】，必须和 JSON 逐字节一致，否则总控解不出来。
#   用 printf|wc -c 而不是 ${#JSON}：busybox 的长度展开按【字符】算，串里有中文就会少算。
LEN=$(printf '%s' "$JSON" | wc -c)

# modId=3 / msgDests=1 / buscmd=3005 —— 三个值都是实测定的（见 matrix_probe.sh 与
# 记忆 [[netease-vbox-led-ring-cmdmap]] 二/五之三节：外部唯一入口 = controller 的 goapi）。
dbus-send --session --print-reply --reply-timeout=200 \
	--dest=netease.ihw.controller /netease/ihw/controller netease.ihw.SmartAudio.API \
	uint32:3 uint32:1 uint32:3005 uint32:"$LEN" \
	string:"$JSON" >/dev/null 2>&1
RC=$?

echo "cmd=0x3005 param=$PARAM data=$DATA len=$LEN rc=$RC bus=$BUS"
exit 0
