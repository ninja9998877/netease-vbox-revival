#!/bin/sh
# 夜间禁声 —— 21:00–07:00 把 codec 的【输出级通断开关】扳到 off。
#
# 用户要求（2026-09-20）：夜里音箱【绝对不出声】。macmini 那边 say() 里的软闸门
# 只挡得住"我们推的流"，挡不住设备自己出声的 —— 唤醒"叮"、云端 TTS、闹钟、
# 手机蓝牙/AirPlay 投屏，全都是它自己播的。所以必须在【设备本地】按嗓子。
#
# ★ 为什么改用 Headphone/Phoneout Switch（2026-09-21 重写；旧版压 headphone volume）
#   旧版压 `headphone volume` 是【纸闸门】：实测挪 43dB，喇叭那头只动 0.7dB
#   （见记忆 netease-vbox-voice「音量」一节）—— 那四条控件全在链路末端，是装饰。
#   现在这两条是 BOOLEAN 通断（ADAU1761 输出级使能），实测（窄带法，1kHz 纯音）：
#       DAC=100 开关ON   窄带 2.333e-04   ← 基准
#       DAC=100 开关OFF  窄带 2.250e-06   ← -40.3 dB，掉回房间本底
#   而且【播完全程开关仍是 off，设备没把它拧回去】—— 当年功放 mute(/bin/adau1761)
#   就是栽在这里（设备一开始播放就自己解掉）。
#
# ★ 为什么它比压 DAC volume 更适合当闸门
#   ① 开关是【断路】，不依赖 dB 标定、不赌"0 是不是真静音"；
#   ② 它和音量【正交】：夜里扳 off、早上扳回 on，主人白天手动调的音量分毫不动，
#      不会出现"早上把她的调节覆盖成 DAY_VOL"这种事故；
#   ③ 同样在 DAC 之后 ⇒ 设备自己播的东西也走这条路。
#
# ★ 禁用过的两条路（别改回去）
#   - HP_L Mux 扳回空路 → 那是"全哑"故障态（见 muxguard.sh），白天还得多修一次。
#   - 功放 mute（/bin/adau1761 2 1）→ 实测设备一开始播放就自己解掉，"按不住"。
LOG=/mnt/UDISK/spk/nightmute.log
LOCK=/tmp/spk/nightmute.lock         # 锁必须放内存盘：重启要自动清掉，否则再也起不来
# ★ STATE/SAVED 放 UDISK 不放 /tmp：这台音箱会自己重启（网络自愈/OTA），
#   放内存盘一重启就没了。
STATE=/mnt/UDISK/spk/nightmute.state # 上次所处的时段（night|day）
SAVED=/mnt/UDISK/spk/nightmute.saved # 进夜间前记下的开关状态（如 "on on"）

SW_HP='Headphone Switch'
SW_PO='Phoneout Switch'
NIGHT_FROM=${NIGHT_FROM:-21}
NIGHT_TO=${NIGHT_TO:-7}

# ★★★ 临时放行 —— 主人夜里要试听（"临时把夜间闸打开 我测一下"）时用。
#
#   文件内容 = 一个【unix 秒】的截止时刻，到点【自己失效】，不需要任何人记得关。
#   为什么要自失效：这道闸门的失败方向是【不对称】的 ——
#     忘了开 ⇒ 最多是主人这次没听到，再喊我一声就行（轻）；
#     忘了关 ⇒ 整夜闸门开着，半夜设备自己出声把人吵醒（重，而且正是主人立这条规矩的原因）。
#   ⇒ 放行必须是"限时的"，而不是"开关式的"。谁都不用记得把它关回去。
#
#   ★ 放 /tmp（tmpfs）不放 UDISK，跟 STATE 的选择【故意相反】：
#     STATE 怕丢（重启要把闸门重新落实），放行怕【留】——
#     设备一重启，放行标记自动消失、闸门自动恢复。失败方向还是"闭嘴"这一侧。
#
#   ★ 解析必须【fail-closed】：文件不存在 / 空 / 内容不是纯数字 ⇒ 一律不放行。
#     宁可这次放行没生效（主人多喊我一声），也绝不因为一个写坏的文件把整夜闸门开着。
OVERRIDE=/tmp/spk/nightmute.off

# ★★★ 第二处放行：能扛住【一次重启】的那份，落在 UDISK 上。
#
#   为什么非加不可（2026-09-21 晚，主人当场要的场景）：
#     主人说"今晚要试，开个到 23:30 的窗口"，【紧接着又说要拔电搬地方】。
#     而 /tmp 那份一拔电就没了 —— 于是"开窗口"和"搬完再试"这两件事直接互斥：
#     他到新位置插上电，音箱是哑的，而且从现象上完全看不出是为什么。
#
#   ★ 它【没有】削弱原来那条保护：窗口本身带【截止时刻】，到点一样自己失效。
#     /tmp 那份"重启即落闸"的语义一个字节没动；这只是给"限时"这件事多一个落脚点。
#     真正的风险（放行变成永久）在这条路上依然不存在。
#   ★ 两份判据完全一样，只是换个地方存 —— 复用一个 _ov()，不写第二套解析。
#   ★ 用完请删：`rm /mnt/UDISK/spk/nightmute.off`（不删也会在截止时刻自己失效）。
OVERRIDE2=/mnt/UDISK/spk/nightmute.off

# 判据：存在 + 内容是【纯十进制整数】+ 还没到点。任一条不满足 ⇒ 不放行。
_ov() {
	[ -f "$1" ] || return 1
	d=$(cat "$1" 2>/dev/null)
	case "$d" in
		''|*[!0-9]*) return 1 ;;   # 空、乱码、多个数、带单位 —— 全部不算放行
	esac
	[ "$(date +%s)" -lt "$d" ]
}

override_on() {
	_ov "$OVERRIDE" && return 0
	_ov "$OVERRIDE2"
}

mkdir -p /tmp/spk
# ★★★ 单实例锁必须【崩溃安全】—— 旧写法 `mkdir || exit 0` 从不 rmdir，一次 kill -9
#   就把锁永久留下、守护再也起不来且不报错（2026-09-21 muxguard 就是这么哑了几小时）。
#   详见 muxguard.sh 里的长注释。锁里存 pid，核实它真是本脚本，陈旧就抢过来。
for _t in 1 2 3; do
	mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
	_op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -n "$_op" ] && [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
		exit 0
	fi
	rm -rf "$LOCK"
done

log() {
	# ★ 别写 wc -c < "$LOG"：日志还没建时【输入重定向失败是 shell 报的错】，
	#   2>/dev/null 在 $( ) 外面拦不住，会往 stderr 冒一行 "can't open"。
	#   把文件当参数传，错误才归 wc 管。
	sz=$(wc -c "$LOG" 2>/dev/null | awk '{print $1}')
	[ "${sz:-0}" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

# 等 codec 注册出来（开机早期 card0 可能还没有）
i=0
while [ $i -lt 60 ]; do
	amixer -c 0 cget name="$SW_HP" >/dev/null 2>&1 && break
	sleep 2
	i=$((i+1))
done

# ★ 取值必须认【`  : values=` 那一行】，不能图省事 tail -1 ——
#   INTEGER 控件的最后一行是 dBscale（`  | dBscale-min=-63.00dB,...`），
#   拿 tail -1 会取回一整串垃圾。BOOLEAN 也有这行规矩，一并守着。
#   只认 on/off，别的（空、乱码）一律回空，由调用方决定怎么办。
sw_get() {
	v=$(amixer -c 0 cget name="$1" 2>/dev/null | sed -n 's/^ *: values=//p')
	case "$v" in
		on|off) echo "$v" ;;
		*) echo '' ;;
	esac
}
sw_set() { amixer -c 0 cset name="$1" "$2" >/dev/null 2>&1; }
sw_show() { v=$(sw_get "$1"); [ -z "$v" ] && v='?'; echo "$v"; }

# ★★★ 静音时段【由 macmini 说了算】—— 它写这份文件，我们只读。
#
#   为什么不在这台设备上自己存时段：设置这件事发生在 macmini（大模型跑在那边），
#   如果两边各存一份，就一定会有"主人改了、设备没跟上"的不一致，而且没人查得出来。
#   ⇒ 单向：macmini 是唯一权威，这份文件是只读副本。
#
#   内容两种形态（跟 macmini 的 ~/.spk_quiet 逐字一致）：
#     "22 8"   起 22 点、止 8 点（不跨零点的时段也支持）
#     "off"    主人说"以后都不用静音了"
#
#   ★ 失败方向跟 _ov() 一样是【不对称】的，所以解析一律 fail-closed：
#     读不到 / 写坏了 ⇒ 回默认 21:00–07:00（照常静音），
#     【绝不】倒向"不静音"—— 那正是主人立这条规矩要防的事。
QUIET=/mnt/UDISK/spk/quiet.conf

# ★ 每轮都重读（20 秒读一个小文件，成本可忽略）⇒ 改时段【不用重启任何东西】。
#   每次都从默认值重设，不保留上次读到的值 —— 否则文件被删掉后会停在旧值上。
_read_quiet() {
	NIGHT_OFF=0; NIGHT_FROM=21; NIGHT_TO=7
	[ -f "$QUIET" ] || return 0
	_q=$(cat "$QUIET" 2>/dev/null)

	# 单个词且恰好是 off ⇒ 永久关闭。
	# ★ 必须【恰好】：`off 7` 这种畸形的要往"照常静音"那边倒，
	#   绝不能因为开头是 off 就当成关闭（那是危险方向）。
	case "$_q" in
		off) NIGHT_OFF=1; return 0 ;;
	esac

	# 按空白切成词。★ 不用 `set --`：那会吃掉本脚本的位置参数，是个埋着的雷。
	_n=0
	for _w in $_q; do
		_n=$((_n+1))
		[ "$_n" = 1 ] && _a=$_w
		[ "$_n" = 2 ] && _b=$_w
	done
	[ "$_n" -eq 2 ] || return 0                 # 词的个数不对 ⇒ 默认

	case "$_a$_b" in ''|*[!0-9]*) return 0 ;; esac   # 非纯数字 ⇒ 默认（抄 _ov 的判据）

	# ★ 去前导零：busybox 的 test/算术会把 "08" 当八进制，`[ "08" -le 23 ]` 有可能炸。
	_a=$(echo "$_a" | sed 's/^0*//'); [ -z "$_a" ] && _a=0
	_b=$(echo "$_b" | sed 's/^0*//'); [ -z "$_b" ] && _b=0

	[ "$_a" -le 23 ] && [ "$_b" -le 23 ] || return 0
	[ "$_a" -ne "$_b" ] || return 0             # ★ 起止同一个点 = 零长度时段，
	                                            #   绝不能被当成"永不静音"
	NIGHT_FROM=$_a; NIGHT_TO=$_b
}

# 现在是哪个时段
# ★ 临时放行期间一律当【白天】：这样初始块和主循环那两条"跨时段"的路都自动走对——
#   放行一开始 ⇒ m=day ⇒ go_day 把开关扳回 on（且把 STATE 写成 day，spkcheck 第 2 节
#   跟着一起放行，不会来打架）；放行一失效 ⇒ m=night ⇒ go_night 原样落闸。
#   闸门自己不需要为放行写第二套逻辑，这是选在 mode_now 上挂钩的原因。
mode_now() {
	override_on && { echo day; return; }

	# ★★ 时钟没同步时 fail-closed（开机早期 date 给的是 01-01 08:00）——
	#   拿 08 去比 21/7 会判成 day ⇒ 【夜里开机不静音】，正是那个危险方向。
	#   判据取年份：真实年份远大于 2020，而没同步时是 1970/2000。
	#   代价说清楚：开机后到时间同步完成（几秒到几十秒）这段时间音箱会哑着，
	#   白天开机也多哑这几十秒 —— 拿"短暂过哑"换"夜里漏哑"，方向是安全的那个。
	#   ★ 放在 override_on 之后：主人明确开了放行阀就是更强的意图，那时照常放行。
	[ "$(date +%Y)" -lt 2020 ] && { echo night; return; }

	_read_quiet
	[ "$NIGHT_OFF" = 1 ] && { echo day; return; }

	h=$(date +%H)
	h=${h#0}                                  # busybox 的 %H 给 "09"，去前导零再比
	[ -z "$h" ] && h=0
	# ★★ 必须分两种写法。原来只有下面那一种（`-ge A || -lt B`），它隐含
	#   "A > B"（时段跨零点）。写死 21/7 的年代永远跨零点，所以这个 bug 从没露头；
	#   一旦允许任意时段就活了：设成 1 点到 6 点，0 点会被算成夜间。
	if [ "$NIGHT_FROM" -lt "$NIGHT_TO" ]; then
		[ "$h" -ge "$NIGHT_FROM" ] && [ "$h" -lt "$NIGHT_TO" ] && { echo night; return; }
	else
		[ "$h" -ge "$NIGHT_FROM" ] || [ "$h" -lt "$NIGHT_TO" ] && { echo night; return; }
	fi
	echo day
}

# 切到夜间：先把当前开关状态记下来（正常是 "on on"），再双双扳 off
go_night() {
	a=$(sw_get "$SW_HP"); b=$(sw_get "$SW_PO")
	[ -n "$a" ] && [ -n "$b" ] && [ "$a$b" != "offoff" ] && echo "$a $b" > "$SAVED"
	sw_set "$SW_HP" off
	sw_set "$SW_PO" off
}

# 切回白天：把记下的状态还回去（读不到存档就默认都开 —— 有声是常态）
go_day() {
	set -- $(cat "$SAVED" 2>/dev/null)
	w1=${1:-on}; w2=${2:-on}
	sw_set "$SW_HP" "$w1"
	sw_set "$SW_PO" "$w2"
}

# 夜间守护：开关【不是】主人会手动调的东西（音量才是），所以夜里发现它被谁打开了
# 就立刻扳回去 —— "保证"两个字靠这一步，不然半夜一次播放初始化就漏声了。
#
# ★ 唯一例外：闹钟响铃中。响铃时由闹钟服务负责把开关打开放轻音乐，我们要让路，
#   否则守护会在 20 秒内把闹钟掐死。"响铃豁免"用这个标记文件约定 ——
#   闹钟服务在整段响铃期间持有它（macmini 侧 /tmp/spk 与设备侧 /tmp/spk 是两回事，
#   这份是【设备本机】的，由闹钟服务 adb 创建/删除；见 spk_alarm.py）。
RING_FLAG=/tmp/spk/ringing
guard_night() {
	[ -e "$RING_FLAG" ] && return 0
	[ "$(sw_get "$SW_HP")" != off ] && sw_set "$SW_HP" off
	[ "$(sw_get "$SW_PO")" != off ] && sw_set "$SW_PO" off
}

last=""
[ -f "$STATE" ] && last=$(cat "$STATE")
m=$(mode_now)

if [ "$m" = "$last" ]; then
	# 同段内重启（比如被 OTA/自愈重启）：夜间要重新落实闸门，白天什么都不做
	if [ "$m" = "night" ]; then
		go_night
		log "启动：当前是 night 时段，重新落实闸门（HP=$(sw_show "$SW_HP") PO=$(sw_show "$SW_PO")）"
	else
		log "启动：当前是 day 时段，不动（HP=$(sw_show "$SW_HP") PO=$(sw_show "$SW_PO")）"
	fi
else
	if [ "$m" = "night" ]; then
		before="$(sw_show "$SW_HP") $(sw_show "$SW_PO")"
		go_night
		log "★ 进入夜间禁声（$NIGHT_FROM:00–$NIGHT_TO:00）：HP/PO $before → off off，原状态存为 $(cat "$SAVED" 2>/dev/null)"
	else
		go_day
		log "★ 退出夜间禁声：HP/PO → $(sw_show "$SW_HP") $(sw_show "$SW_PO")"
	fi
	echo "$m" > "$STATE"
fi

# 常驻：每 20 秒看一次。跨过时段边界才动音量相关的事；夜间期间只守护闸门。
while true; do
	sleep 20
	m=$(mode_now)
	if [ "$m" != "$(cat "$STATE" 2>/dev/null)" ]; then
		if [ "$m" = "night" ]; then
			before="$(sw_show "$SW_HP") $(sw_show "$SW_PO")"
			go_night
			log "★ 进入夜间禁声：HP/PO $before → off off，原状态存为 $(cat "$SAVED" 2>/dev/null)"
		else
			go_day
			log "★ 退出夜间禁声：HP/PO → $(sw_show "$SW_HP") $(sw_show "$SW_PO")"
		fi
		echo "$m" > "$STATE"
	elif [ "$m" = "night" ]; then
		guard_night
	fi
done
