#!/bin/sh
# play601.sh —— 设备侧的"嗓子"出口：经 D-Bus 让 ihwplayer 播/停一条 URL。
#
# ★ 它存在的理由是【这台设备的环境事实】，不是设计偏好：
#   ① spk_net.sh 的环境里【没有 DBUS_SESSION_BUS_ADDRESS】—— 实测 /proc/<pid>/environ
#      只有 HOME 和 PATH。而 dbus-send 必须知道去哪条总线，否则命令在写进 socket 之前
#      就被丢掉（表现是"发出去了、喇叭没响"，一句报错都没有）。
#   ② 那条会话总线的 socket 路径【每次开机都不一样】：/tmp/dbus-XXXXXXXXXX（随机后缀），
#      由 init 拉起的 `dbus-daemon --session`（实测开机 PID 273）创建。所以不能写死，
#      必须现找 —— 判据是【它得是个 socket】（test -S），不是"文件存在"。
#   ③ 如果让 macmini 直接把整条 dbus-send 拼进命令里，JSON 那段引号要穿过两层 shell
#      （macmini → cmd 文件 → 设备的 sh -c），错一个字符就是 ① 那个症状。
#   ⇒ 把"怎么说话"收在设备本地，macmini 侧只发一条短命令。
#
# ★ 它【不碰音量】。主人立的规矩：一律用音箱当前的音量，谁也不许在播放路径上改它。
#   JSON 里的 gain:0 是设备自己发同类命令时用的值（见总控日志 0x602 / 0x701），
#   不是我们挑的。真正的音量杠杆是 DAC volume（numid 19），那是另一条线的事。
#
# 用法：
#   play601.sh play <url> [playerId]     0x601 播（playerId 默认 1 = 总控的 curPlayer）
#   play601.sh stop [playerId]           0x603 停
#
# ★★★ dbus-send 的两个参数都是【实测定的】，一个都不能按直觉改（2026-09-22）：
#
#   ① 【必须有 --print-reply】。直觉上"反正是单向命令，不要回执更快"—— 错。
#      去掉它 dbus-send 会【不等 flush 就断开连接】，消息烂在发送队列里，
#      播放器一个字都收不到，而 dbus-send 自己 rc=0、一声不响（"看起来成功、
#      其实没送到"，本项目最怕的那种）。实测判据用 ihwplayer 日志里的 cmdId：
#          不带 --print-reply                    ✗ 没送到
#          --print-reply --reply-timeout=2500    ✓ 送到，但干等 2.546 秒
#          --print-reply --reply-timeout=200     ✓ 送到，0.239 秒
#          --print-reply --reply-timeout=50      ✓ 送到，0.087 秒
#      ⇒ 带上它，消息才会被真正 flush 出去。
#
#   ② 【超时必须很小】。播放器是【只收不回】的 —— 它收到 method_call 就干活，
#      从不发 reply。于是 dbus-send 会一直干等到超时，再吐一句
#          Error org.freedesktop.DBus.Error.NoReply
#      默认/2500ms 就是【每次说话凭空多等 2.5 秒】，而"停"是要在打断那一刻用的：
#      那 2.5 秒就是喇叭还响着的 2.5 秒。200ms 留了 4 倍余量（实测 50ms 也够，
#      但总线忙起来时 send+flush 本身要花时间，别抠）。
#
#   ★ 注意这 200ms 只是 dbus-send【自己退出前】的等待 —— 命令早就送到播放器了
#     （flush 是微秒级的）。所以对"打断"来说真正的延迟是 0，不是 200ms。
#
# ★ 判成败【不要看这里的输出】：命令是单向的，dbus-send 本来就不回话。
#   真正的判据在 macmini 那侧 —— 设备有没有来 GET 那个文件（8899 访问日志），
#   以及设备 ihwplayer 日志里的 cmdProcess(179):cmdId(0x601)。
#
# ★ playerId 必须等于总控（netease_control_center）的 curPlayer，否则它会在日志里
#   记一条 "Player(N) not the curPlayer(1) ... but in playing" 并把播放掐掉。
#   实测现在 curPlayer=1。换设备/换固件时这条要重新确认。

PLAYER=netease.ihw.player
OBJ=/netease/ihw/player
IFACE=netease.ihw.SmartAudio.API
DEST_BITS=16          # msgDests = 1 << 播放器模块号（player=4 ⇒ 16）

CMD=$1
case "$CMD" in
play)
	URL=$2
	PID=${3:-1}
	if [ -z "$URL" ]; then
		echo "用法: play601.sh play <url> [playerId] | play601.sh stop [playerId]"
		exit 2
	fi
	JSON='{"playerId":'"$PID"',"src":"'"$URL"'","srcUuid":"'"$URL"'","gain":0,"backGroundUrl":""}'
	CODE=1537        # 0x601 CMD_PLAY_PLAY
	NAME=0x601
	;;
stop)
	PID=${2:-1}
	JSON='{"playerId":'"$PID"'}'
	CODE=1539        # 0x603 CMD_PLAY_STOP
	NAME=0x603
	;;
*)
	echo "用法: play601.sh play <url> [playerId] | play601.sh stop [playerId]"
	exit 2
	;;
esac

BUS=$(ls /tmp/dbus-* 2>/dev/null | head -1)
if [ -z "$BUS" ] || [ ! -S "$BUS" ]; then
	echo "✗ 找不到会话总线（/tmp/dbus-* 里没有 socket）—— dbus-daemon --session 没在跑？"
	exit 1
fi
DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS"
export DBUS_SESSION_BUS_ADDRESS

# ★ msgSize 是【字节数】，必须和 JSON 逐字节一致，否则 ihwplayer 解不出来。
#   用 printf 而不是 ${#JSON}：busybox 的长度展开按字符算，命令里有中文就会错。
LEN=$(printf '%s' "$JSON" | wc -c)

# ★★★ 两个参数的理由见文件头 —— 单独去掉任何一个都是坑：
#   去 --print-reply ⇒ 消息不发出去；把 200 改回 2500 ⇒ 每次说话白等 2.5 秒。
dbus-send --session --print-reply --reply-timeout=200 \
	--dest="$PLAYER" "$OBJ" "$IFACE" \
	uint32:0 uint32:"$DEST_BITS" uint32:"$CODE" uint32:"$LEN" \
	string:"$JSON" >/dev/null 2>&1
RC=$?

echo "cmd=$NAME pid=$PID len=$LEN rc=$RC bus=$BUS"
exit 0
