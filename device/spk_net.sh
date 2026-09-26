#!/bin/sh
# spk_net.sh —— 设备侧的"无线耳朵"：每 2 秒问一次 macmini「有活儿吗」。
#
# 这是【取代 USB 上 adb】的那条路。方向是反的：设备主动往外连，
# 所以设备上不用开任何入站端口、不用碰 iptables、也不用指望 adbd 的 TCP 模式
# （那条 2026-09-21 实测是死的，而且会把 USB 一起弄断 —— 见 spk_netd.py 开头）。
#
# 它顺带也是 nightmute 的"响铃豁免"通道：
#   nightmute 想知道"主人在放闹钟吗"，就去 GET macmini:8899/spk_ringing
#   （8899 是 macmini 上那个 DLNA 静态服务器，根就是 /tmp）——
#   macmini 只要在本地 touch 一个文件就行，全程不碰设备。
#
# ★ 启动一律 start-stop-daemon -S -b -m -p（这台设备没有 nohup/setsid，
#   直接 `sh x.sh &` 在 ssh/adb 断开时会被带走）。
# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"
LOG=/mnt/UDISK/spk/net.log
TOKF=/mnt/UDISK/spk/net.token
SRV=${SPK_NET_SRV:-$SPK_SRV:$SPK_PORT_NETD}
PID=/tmp/spk/net.pid
GAP=${SPK_NET_GAP:-2}

mkdir -p /tmp/spk

# ★★★ 单实例锁（2026-09-22 加）—— 【挂进 run.sh 之前必须先有它】。
#
#   为什么非有不可：别的守护（muxguard/nightmute/ackrotate/waketap/muxtrace…）都自带
#   崩溃安全锁，所以 run.sh 【重复起它们是安全的】。本脚本原来只有下面那个"自写 PID"，
#   没有"已在跑就退出"⇒ 一旦也挂到 run.sh 上，`spkbrain restart` / procd respawn 每次
#   都会再起一个；而 spkcheck 的 alive() 判据只看"有没有这个脚本在跑"，
#   **两个实例它照样认为正常** ⇒ 两个实例同时轮询 8898，**同一条命令被执行两遍**。
#   那比"迟到 103 秒"严重得多，所以这一条是加进 run.sh 的前提、不是附赠。
#
#   锁法照抄 waketap.sh（同一套判据），特别是 2026-09-21 读出来的那个洞：
#   两个实例【几乎同时】启动时，晚的那个会读到"锁在、但 pid 还没落盘"的空串 ——
#   那时【绝不许 rm 锁】（rm 等于把前一个的锁删掉，两个一起活），要让一步重试。
#   只有【确定】读到了 pid、且那个进程确实不是本脚本，才敢当陈旧锁清掉。
LOCK=/tmp/spk/spk_net.lock
for _t in 1 2 3 4 5 6 7 8 9 10; do
	mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
	_op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -z "$_op" ]; then
		sleep 1
		continue
	fi
	if [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
		exit 0                      # 真有一个同伙在跑
	fi
	rm -rf "$LOCK"                  # 陈旧锁：那个 pid 确实不是本脚本（已 kill -9 / 已退出）
done

# ★ 自己的 PID 自己写（2026-09-22 加）。上面那个 PID 变量原来【声明了却没人用】，
#   于是重启它只能去 /proc 里按 cmdline 找 —— 而"按 cmdline 杀进程"是本项目
#   明令禁止的（认错进程的代价是弄哑音箱）。自己写下来，重启就变成
#   `kill $(cat /tmp/spk/net.pid)` 一条命令，跟别的守护一个规矩。
echo $$ > "$PID"

log() {
	# ★ 别写 wc -c < "$LOG"：日志还没建时【输入重定向失败是 shell 报的错】，
	#   2>/dev/null 在 $( ) 外面拦不住。把文件当参数传，错误才归 wc 管。
	sz=$(wc -c "$LOG" 2>/dev/null | awk '{print $1}')
	[ "${sz:-0}" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

TOK=""
[ -f "$TOKF" ] && TOK=$(cat "$TOKF" 2>/dev/null)
log "起来：服务器 $SRV  口令$( [ -n "$TOK" ] && echo 有 || echo 没有 )"

U="http://$SRV"
fails=0

# ★★ 补同步静音时段（2026-09-22 加）。
#
#   时段这件事是【单向】的：macmini 那份 ~/.spk_quiet 是唯一权威，设备这份只是副本。
#   平时靠 macmini 的 set_quiet 工具【推】过来（走上面那条 cmd 通道），
#   可推的那一刻设备完全可能不在线（主人半夜改了、音箱正断着网）——
#   那时 macmini 会如实跟主人说"它上线后会自己拉取补上"，这个函数就是那句话的兑现物。
#
#   走 8899 那个静态服务器（根就是 /tmp），跟 nightmute 取 spk_ringing 是同一个套路：
#   macmini 只要往 /tmp/spk_quiet 写个文件就行，不用给 spk_netd 另开 HTTP 路由。
IP=${SRV%%:*}                          # 192.168.1.100:8898 → 192.168.1.100
STATIC="http://$IP:8899"
QUIET=/mnt/UDISK/spk/quiet.conf
PULL_EVERY=${SPK_NET_PULL_EVERY:-150}  # 每 150 轮（2 秒 × 150 = 5 分钟）兜底一次

pull_quiet() {
	# ★★ 全部声明 local（2026-09-22，跟上面那个 404 同一轮抓到的第二个缺陷）：
	#   shell 函数的变量【默认是全局的】—— 我加形态闸门时用了 `_a`/`_b`/`_n`/`_v`，
	#   结果把【调用方的同名变量】改掉了：离线测试里恰好也用 `_b` 存 mtime，
	#   于是"期望 8、得到 1790046802"这种看不懂的失败就冒出来了。
	#   线上现在没暴露，只因主循环凑巧没用到这些名字 —— 这种"凑巧"不是防线。
	#   （nightmute.sh 里"不用 set --，它会吃掉本脚本的位置参数"是同一类病。）
	local Q _n _w _v _a _b
	# ★★ `-f` 不能省（2026-09-22 首次上线实机抓到的）：这个文件还不存在时，
	#   python http.server 回的是 **404 加一整页 HTML**，而 `curl -s` 不加 -f 时
	#   【退出码是 0、stdout 就是那页 HTML】⇒ 被当成配置写进了设备（真机上就是这么写的）。
	#   危害被 nightmute 的 fail-closed 兜住了（_read_quiet 数出词数不对 ⇒ 回默认 21/7，
	#   仍然是"照常静音"），但每次开机都会白写一次盘、把日志刷屏。
	#   ★ 我的离线测试没测出它：curl 桩在没内容时【回空】，从没模拟过"回 404 HTML"。
	Q=$(curl -s -f -m 5 "$STATIC/spk_quiet" 2>/dev/null)
	# ★ 拉不到 ⇒ 什么都不动。macmini 没起来 / 还没写过这个文件 / 只有 8898 通 ——
	#   保持现有配置就是"照常静音"，是安全方向。清空虽然也会落到默认 21/7（同样安全），
	#   但没必要凭空制造一次状态跳变。
	[ -n "$Q" ] || return 0
	# ★ 第二道：形态闸门。`-f` 只挡 HTTP 错误码，"200 但内容是垃圾"（中间有代理、
	#   或者谁改坏了那个文件）它挡不住。只认 "off" 或恰好两个 0~23 的整数 ——
	#   跟 nightmute 的 _read_quiet 同一套判据，fail-closed 的方向也一样：认不出就不动。
	case "$Q" in
	off) ;;
	*)
		_n=0
		for _w in $Q; do
			_n=$((_n+1))
			[ "$_n" -gt 2 ] && break
			# 去前导零再判：busybox 的 test 会把 "08" 当八进制。
			# ★ 只拿这个副本去比较，写盘的仍是原内容（否则跟 macmini 那份
			#   永远不相等 ⇒ 每轮都写一次盘，正好是要避免的那件事）。
			_v=$(echo "$_w" | sed 's/^0*//'); [ -z "$_v" ] && _v=0
			case "$_v" in *[!0-9]*) return 0 ;; esac
			[ "$_v" -le 23 ] || return 0
			[ "$_n" = 1 ] && _a=$_v
			[ "$_n" = 2 ] && _b=$_v
		done
		[ "$_n" -eq 2 ] || return 0
		[ "$_a" -ne "$_b" ] || return 0     # 起止同一个点 = 零长度时段，不许
		;;
	esac
	# ★ 内容一样就别写盘：这条路开机后每 5 分钟跑一次，无脑写会把 flash 磨坏，
	#   而且每次写都会让 nightmute 那边重新读一遍（无害但没必要）。
	[ "$Q" = "$(cat "$QUIET" 2>/dev/null)" ] && return 0
	echo "$Q" > "$QUIET"
	log "← 补同步静音时段：$(echo $Q)"
}

# 每次【确认连上了】都调一次（两条路：有活儿 / ping 通）。
#   back=1 表示"上一轮还在失败"—— 那正是"推的时候我没在线"的那一刻，立刻补拉。
#   初值也设 1 ⇒ 开机第一次连上就拉，这样设备重启后总跟权威对一遍。
ticks=0
back=1
onlink() {
	ticks=$((ticks+1))
	if [ "$back" = 1 ] || [ $((ticks % PULL_EVERY)) -eq 0 ]; then
		pull_quiet
	fi
	back=0
}

while true; do
	C=$(curl -s -m 5 "$U/cmd?t=$TOK" 2>/dev/null)
	if [ -n "$C" ]; then
		fails=0
		onlink
		log "← $C"
		# ★ 超时给足：下载这类命令可能要跑一会儿。但绝不能【不设超时】——
		#   没有 -m 的话一条卡住的命令就让整条通道哑了，而外面看不出来。
		O=$(sh -c "$C" 2>&1)
		curl -s -m 20 -X POST --data-binary "$O" "$U/ret?t=$TOK" >/dev/null 2>&1
		continue        # ★ 刚干完活，立刻回去听下一条（不睡）
	fi
	# —— 空手回来：要么没活儿，要么连不上，得分清楚（不然网络断了会看起来一切正常）——
	if curl -s -m 5 -o /dev/null -f "$U/ping?t=$TOK" 2>/dev/null; then
		fails=0
		onlink
		continue        # ★★ 不睡 —— 这就是 2026-09-22 那次提速的全部秘密。
		                #    macmini 那侧已经把 /cmd 挂了 HOLD 秒才回（长轮询），
		                #    那次挂起本身就是节流。再睡 GAP 只会凭空多出一段
		                #    【没人听】的窗口：命令正好落在那段里就得等下一轮。
		                #    实测这一条把"停"的往返从 4.9 秒压到 0.4 秒以内。
		                #    （设备没有小数 sleep，最小 1 秒 —— 所以"少睡一次"
		                #      比"睡短一点"是唯一可行的方向。）
	fi
	fails=$((fails+1))
	back=1
	[ $((fails % 30)) -eq 1 ] && log "⚠ 连不上 $SRV（第 $fails 次）"
	# ★ 只有【真连不上】才节流：这条路上没有长轮询在替我们踩刹车，
	#   不睡的话 1 秒能打十几个空转请求，把设备和 macmini 一起拖住。
	sleep "$GAP"
done
