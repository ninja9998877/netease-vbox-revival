#!/bin/sh
# spkbrain —— 设备侧启动包装（顺带把隧道拉起来）
#
# ★ 隧道是**可选的加挂**，不是前提：设备是**主动来取**的（它去连大脑，不是被连），
#   大脑和音箱在同一个局域网里时，下面跟 tailscaled 有关的整段都可以删掉。
#   只有当大脑在别处、中间必须过一条隧道时才需要它。
#
# 为什么需要包装（两条都是实测出来的，不是猜的；都只在用隧道时成立）：
#  1. kernel 3.4.39 在自动选路时会忽略策略路由：table 52 和 5270 规则都在，
#     内核就是不认，于是发往 100.x 的包被扔给默认网关。必须等 tailscale0
#     出现后，往【主表】补一条，自动选路才认得。
#  2. 持久的东西（二进制、node key）放 UDISK，运行时的东西（socket、日志）
#     放 /tmp —— NAND 只写必要的。state 丢了要重新点网址认证，socket 丢了无所谓。
D=/mnt/UDISK/spk
R=/tmp/spk
mkdir -p "$R"
LOG=$R/tsd.log

# 关掉 Wi-Fi 省电（实测：开着 avg 64ms/丢5%，关掉 avg 31ms/丢0%）。
# 省电模式下包要等 beacon 周期才发，对语音对话是致命的。重启会恢复，所以每次开机都关。
iwconfig wlan0 power off 2>/dev/null

# 日志轮转：攒到 256KB 就翻一页，内存盘也要省
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 262144 ]; then
	mv "$LOG" "$LOG.1"
fi

# ★★★ 把设备【自己的声音】顶成静音 —— 必须在这里，不能只靠 cron。
#
#   本脚本由 S95spkbrain 拉起，而语音服务是 S109netease_voice_service ——
#   ★ 95 < 109 ⇒ 我们【跑在语音服务之前】，开机那几声"叮/网络异常/欢迎使用"
#   在它们有机会响之前就已经被盖住了。
#   只靠 spkcheck.sh 的话，开机到 cron 第一次滴答之间有一段几十秒的空窗，
#   那段时间设备是原声 —— 半夜跳闸重启，这几声就是会把人吵醒的那几声。
#   spkcheck.sh 里仍然每分钟再调一次（mount 不持久，重启即失效）。
[ -f "$D/voicemute.sh" ] && sh "$D/voicemute.sh"

# ★★★ 左声道扳回 DAC —— 这是"整台音箱一个字都不出声"的真凶，别删。
#
#   实测（2026-09-20）：`HP_L Mux` 被指到了 'Left Analog Mixer HPL Switch'，
#   而那条模拟混音路上的 LINEINL / MIC1 / MIC2 / PHONEN 【全部是 off】——
#   等于左声道接进一个什么都没有的岔路口。设备是【单声道喇叭】、认的就是左路，
#   于是整台机器彻底哑掉：状态机好、功放静音会乖乖解开、麦克风好、DLNA 推流也接，
#   就是没声。查这个要一层层往下剥，极难定位。
#   扳回 'DACL HPL Switch'（DAC 直连耳机输出）声音立刻就有。
#
#   ★ amixer 的改动【只在内存里，重启就丢】。这台音箱会自己重启（网络自愈/OTA），
#     所以必须每次开机重扳，而且要盯着 —— 谁把它改回去的还没查出来，但功放音量
#     实测就是设备自己在改的，说明它确实会动 ALSA 控件。发现被改回去就立刻扳回。
#
#   本体在 muxguard.sh —— 拆出去是为了：① 能单独手动起（不必等重启）
#   ② 它自带单实例锁，procd respawn 重跑本文件时不会把守护攒成一堆。
if [ -x "$D/muxguard.sh" ]; then
	"$D/muxguard.sh" &
else
	echo "$(date) 警告：$D/muxguard.sh 不在，音箱可能是哑的" >> "$LOG"
fi

# ★★★ DAC volume 守护 —— "播放器明明在播、喇叭一点声没有"的【第二个】真凶，别删。
#
#   实测（2026-09-22）：`DAC volume`(numid=19) 是【所有路共同的增益底】，位置在
#   headphone volume 之前。刻度 min=0 max=255、dBscale-min=-119.25dB
#   ⇒ 159≈0dB（满刻度），**掉到 0 就是 -119.25dB = 全哑**。
#   那一轮的对照：ADC volume=160、headphone volume=59 全好，**只有这一级是 0** ——
#   状态机好、0x601 推得出去、设备也真来取流，就是没声。
#
#   ★ 它跟 muxguard 是【同一个病的两个位置】，都要常驻：
#     muxguard 保"左声道接在 DAC 上"（路走错了），本脚本保"这条路的增益底不是 0"（路上没信号）。
#     两个都是"崩了就永久哑、而且一声不响"，两个都是 amixer 只在内存里、重启就丢。
#
#   ★★★ 它【故意】没有 night 判断：夜间禁声靠的是 HP/PO 输出开关（nightmute 管），
#     不是 DAC volume。夜里归零而没人修 ⇒ 天亮放行之后音箱还是哑的，且现象上
#     "夜里静音"和"彻底哑了"一模一样（09-21 夜就是被这么瞒过去的）。
#     细节写在 dacguard.sh 开头，别改成"夜里不修"。
#
#   本体在 dacguard.sh —— 拆出去跟 muxguard 同理：① 能单独手动起 ② 自带崩溃安全锁，
#   procd respawn 重跑本文件不会把守护攒成一堆。
if [ -x "$D/dacguard.sh" ]; then
	"$D/dacguard.sh" &
else
	echo "$(date) 警告：$D/dacguard.sh 不在，DAC volume 归零就没人扳回来了（全哑）" >> "$LOG"
fi

# ★★ 夜间禁声的硬闸门 —— 21:00–07:00 把 codec 耳机输出档压到 0（-63dB 真静音）。
#
#   主人要求（2026-09-20）：夜里音箱【绝对不出声】。macmini 那边 say() 里的软闸门
#   只能挡住我们推的流；设备自己出声的（唤醒"叮"、云端 TTS、闹钟、手机投屏）
#   挡不住，必须在设备本地按嗓子。细节和"为什么不能用功放 mute"都写在 nightmute.sh 里。
#
#   ★ 它跟 muxguard.sh 是【两个方向】的守护，别搞混：
#     muxguard 保"白天能出声"（HP_L Mux 被改坏就扳回 DAC）；
#     nightmute 管"夜里不出声"（压 headphone volume，不碰 HP_L Mux）。
#   两个都带单实例锁，procd respawn 重跑本文件不会攒一堆。
if [ -x "$D/nightmute.sh" ]; then
	"$D/nightmute.sh" &
else
	echo "$(date) 警告：$D/nightmute.sh 不在，夜里可能被吵到" >> "$LOG"
fi

# ★★★ 网络守护 —— 保证 wlan0 永远有 IP（2026-09-21 加，跟 muxguard/nightmute
#   一个道理：守设备自己没做好的事）。
#
#   这台音箱【重启后不会自己去要 IP】：wpa_supplicant 没有 -a 关联后动作脚本，
#   /etc/hotplug.d/iface/ 里也没有拉 DHCP 的钩子。于是关联成功、Access Point 有值，
#   可 wlan0 上一个 inet 都没有 —— 于是 mictap 发不出去、spkclient 连不上、
#   tailscaled 拿不到 tailscale0 的地址，整条链是死的。
#   完整实测证据和判据设计写在 netguard.sh 开头。


# 唤醒应答随机轮换（2026-09-21 加）—— 设备每次唤醒都重读 spk_ack.mp3，所以只换文件
# 就能换应答，不碰 skins.db。判据是 atime：装上去时钉 1970，被读过 atime 就变了。
# 池子在 /mnt/UDISK/resources/voice/spk_ack_pool，部署/还原见 deploy_ackrotate.sh。
if [ -x "$D/ackrotate.sh" ]; then
	"$D/ackrotate.sh" &
else
	echo "$(date) 警告：$D/ackrotate.sh 不在，唤醒应答不会随机" >> "$LOG"
fi
if [ -x "$D/netguard.sh" ]; then
	"$D/netguard.sh" &
else
	echo "$(date) 警告：$D/netguard.sh 不在，重启后音箱可能拿不到 IP（整条链会死）" >> "$LOG"
fi

# ★★★ 抄设备引擎的唤醒答案 —— 它认出来了就当我们自己被唤醒。
#
#   为什么非有不可：我们的 KWS 吃的是 mictap 在 snd_pcm_readi 上抓的【波束成形之前】
#   的原始单麦；设备自己的 duilite 有 4 麦远场波束成形 + AEC。2026-09-21 晚实测，
#   同一段时间里设备引擎 8 次全认出、我们的 KWS 只认出 2 次 ——
#   主人喊了三次"嘀嗒嘀嗒"，设备每次都认出来了，我们一次都没认出来。
#   ★ 这是硬件差距，电平/音素两条调参路都已实测否掉。详见 waketap.sh 开头。
#
#   ★ 它跟别的守护不同：spkcheck 除了判活，还要核对它跟的日志文件换没换
#     （voice 服务重启会换 pid 文件名，tail -f 会一直跟着那个已删除的旧文件，
#      进程活着但再也抄不到东西 —— 静悄悄的那种坏）。
if [ -x "$D/waketap.sh" ]; then
	"$D/waketap.sh" &
else
	echo "$(date) 警告：$D/waketap.sh 不在，只能靠我们自己的 KWS（真唤醒会漏掉大半）" >> "$LOG"
fi

# ★★★ muxtrace —— 0.15 秒一轮盯 HP_L Mux，只读不出声，揪"是谁把左声道改回全哑档"。
#
#   ★ 2026-09-21 晚补进 run.sh：它原来【不在本脚本里】，只挂在 spkcheck 的保活名单上。
#     后果实测到了（就是这次冷启动演练）：开机后它【要等 spkcheck 下一分钟才发现它不在】，
#     中间那段空窗谁改了 mux 就没证据 —— 而 mux 恰恰是"整台机器一个字都不出声"那个
#     查了好几小时的故障。空窗正好落在【开机后最乱的那几分钟】，也就是最需要证据的时候。
#     日志铁证：21:31:10 run.sh 起完其它守护 → 21:33:07 spkcheck 才 "muxtrace.sh 不在跑 ⇒ 重启它"。
#   它自带崩溃安全锁，重复起不会攒成一堆（spkcheck 那一侧照旧保活，两条路都留着）。
if [ -x "$D/muxtrace.sh" ]; then
	"$D/muxtrace.sh" &
else
	echo "$(date) 警告：$D/muxtrace.sh 不在，HP_L Mux 被改坏就查不出是谁干的" >> "$LOG"
fi

# ★★★ spk_net.sh —— 【不插 USB 就能看设备一眼的唯一通道】（2026-09-22 冷启动演练后补进来）。
#
#   ★ 它原来【不在本脚本里】，只挂在 spkcheck 的保活名单上 —— 跟上面 muxtrace 当年
#     一模一样，而那次已经把代价实测出来了。这次冷启动演练又实测了一遍它的后果：
#       16:06:28 断电重启 → 16:06:37 run.sh 起完 7 个守护（它在名单外，没起）
#       → **16:08:11 spkcheck 才发现它不在**（pid start=10311 ⇒ 空窗 103.1 秒）
#     那 103 秒里，设备侧那条无线命令通道是断的：音箱收不到 macmini 的任何命令，
#     而这正是"开机最乱、最需要看设备一眼"的窗口。
#   ★ 第一轮 spkcheck 为什么那么晚：run.sh 在 16:06:37 写了 crontab（内容不一致）⇒
#     crond 要等下一个分钟边界重载、再下一个边界才执行 ⇒ 比理论最早（16:07:00）晚 60 秒；
#     再叠加 voicemute 逐个核 90 个文件耗 11 秒，才落到 16:08:11。
#   ★ 代价比 muxtrace 那次更重：它是**不看设备就不知道设备死活的那条路**
#     （拿 adb = 给音箱断电重上电）。09-21 那次"17:32 之后再也读不到设备"就是它的同族。
#
#   ★ 前提：它 2026-09-22 才补上崩溃安全单实例锁（原来没有，见 spk_net.sh 开头）——
#     没有那把锁就挂进这里，会变成两个实例同时轮询、同一条命令执行两遍。
#   ★ spkcheck 那一侧照旧保活，两条路都留着（跟 muxtrace/waketap 同款）。
#   ★ 早起 15 秒（wlan0 还没 IP）无害 —— 它本来就是轮询循环，连不上就重试。
if [ -x "$D/spk_net.sh" ]; then
	"$D/spk_net.sh" &
else
	echo "$(date) 警告：$D/spk_net.sh 不在，开机后 100 秒内我们看不见设备" >> "$LOG"
fi

# ★★★ 第二道闸的自我修复：crontab 丢了就写回来（2026-09-21 加）。
#
#   为什么需要第二道闸：muxguard / nightmute / netguard 全是【常驻进程】，崩了就没
#   人管；而本机【每次开机 mux 默认就是全哑档】⇒ 守卫一死 = 永久哑、且一声不响。
#   2026-09-21 真出了这事（muxguard 的单实例锁被 kill -9 留下，守护再也起不来），
#   音箱哑了几个小时才查出来。于是加了 cron 当第二道闸：/etc/crontabs/root
#   每分钟拉起 spkcheck.sh 体检一次。cron 由设备自己的 S50cron 开机拉起，
#   完全不经本脚本、不经任何常驻进程 ⇒ 崩了下一分钟照样来。
#
#   ★ 它唯一的死法：/etc/init.d/cron 第一句是
#         [ -z "$(ls /etc/crontabs/)" ] && return 1
#     ——【目录空就拒绝启动】。而 /etc/crontabs/ 落在 overlay，OTA 重刷/某些自愈
#     可能把它清掉；那之后整套第二道闸就静默消失，而且从现象上完全看不出来
#     （音箱不哑、也没报错，只是"再崩就没人兜了"）。所以每次开机核对一遍。
CRONTAB=/etc/crontabs/root
if [ -f "$D/crontab.root" ]; then
	if ! cmp -s "$D/crontab.root" "$CRONTAB" 2>/dev/null; then
		mkdir -p /etc/crontabs
		cp "$D/crontab.root" "$CRONTAB"
		echo "$(date) crontab 与 $D/crontab.root 不一致，已写回（第二道闸自保）" >> "$LOG"
	fi
else
	echo "$(date) 警告：$D/crontab.root 不在，健康自检的第二道闸没有保障" >> "$LOG"
fi
# 确认 crond 真在跑。判进程名只认 /proc/<pid>/comm（cmdline 里可能只是"提到"它）。
# S50cron 正常已经起过了，这里是兜底；不在就自己拉起来。
crond_up() {
	for p in /proc/[0-9]*/comm; do
		c=$(cat "$p" 2>/dev/null)
		[ "$c" = "crond" ] && return 0
	done
	return 1
}
if ! crond_up; then
	/etc/init.d/cron start >/dev/null 2>&1
	echo "$(date) crond 不在跑，已拉起（第二道闸自保）" >> "$LOG"
fi

# ★ 先把可能还活着的旧 tailscaled 收干净 —— 这段是踩出来的，别删。
#   /etc/init.d/spkbrain restart 时 procd 会另起一个新的 tailscaled，而旧的那个
#   未必已经退出。新的抢不到 $R/ts.sock 就会退出（日志里是
#   "safesocket.Listen: /tmp/spk/ts.sock: address already in use"），
#   【但它在退出前会把 tailscale0 重建一遍，把地址弄丢】。
#   后果极其隐蔽：隧道其实还通（macmini 那边看是 direct、lastRecv 一直在跳），
#   可 tailscale0 上一个地址都没有，于是所有【出方向】的包源地址退化成 wlan0 的
#   192.168.1.50（这台音箱的局域网地址，以你的实际部署为准），tailscaled 一看源地址不是自己的就把包全丢了 ——
#   表现是连接永远卡在 SYN_SENT，而抓包在对端【一个包都看不到】。
#   （2026-09-20 就是这么丢的，查了一小时。清掉之后客户端才连得上。）
for p in /proc/[0-9]*; do
	# ★ 用 `cat|tr` 而不是 `tr < file`：POSIX sh 里 `tr ... < $p/cmdline 2>/dev/null`
	#   的重定向先于 2>/dev/null 生效，文件打不开时是 shell 自己报错到 stderr
	#   ⇒ 开机日志被 "can't open /proc/NNN/cmdline: no such file" 刷屏
	#   （进程在枚举途中退出的正常竞态，无害，但会淹掉真信号 ——
	#   2026-09-21 正是靠 logread 才定位到 UDISK 竞态的，噪音必须清掉）。
	c=$(cat $p/cmdline 2>/dev/null | tr '\0' ' ')
	case "$c" in
		$D/tailscaled*)
			echo "$(date) 启动前先收掉旧 tailscaled pid=$(basename $p)" >> "$LOG"
			kill "$(basename $p)" 2>/dev/null ;;
	esac
done
# 等它把 socket 放开，再强制清掉残留（否则新实例照样起不来）
i=0
while [ -e "$R/ts.sock" ] && [ $i -lt 10 ]; do
	sleep 1
	i=$((i+1))
done
rm -f "$R/ts.sock"

# ★★★ 起 tailscaled 之前必须等 wlan0 拿到 IP（2026-09-21 加）。
#   原来没这一步，而设备重启后又不会自己跑 DHCP（见上面 netguard.sh 那段），
#   于是 tailscaled 恰好起在"没网"的时刻：连不上控制面 ⇒ 拿不到 tailscale0 的
#   地址 ⇒ 下面那个 60 秒等待必然超时、补主表路由也白做。
#   netguard.sh 已在后台去要 IP 了，这里只是等它到手（正常 30~60 秒）。
n=0
while [ $n -lt 120 ]; do
	ip -4 addr show wlan0 2>/dev/null | grep -q "inet " && break
	sleep 2
	n=$((n+2))
done
if [ $n -ge 120 ]; then
	echo "$(date) 警告：120 秒内 wlan0 没拿到 IP，照常起 tailscaled（它自己会重试控制面）" >> "$LOG"
elif [ $n -gt 0 ]; then
	echo "$(date) 等了 ${n}s 等到 wlan0 的 IP（$(ip -4 addr show wlan0 | grep -o 'inet [0-9.]*')）" >> "$LOG"
fi

# socks5-server 是给我们的客户端用的备用出口：走它连 mac mini 完全不碰内核路由，
# 万一哪天路由又被人删了，客户端照样能通。只绑 127.0.0.1。
"$D/tailscaled" \
	--tun=tailscale0 \
	--state="$D/ts.state" \
	--socket="$R/ts.sock" \
	--port=41641 \
	--socks5-server=127.0.0.1:1055 \
	--no-logs-no-support \
	--verbose=1 >> "$LOG" 2>&1 &
TSD=$!

# 等 tailscale0 【拿到地址】—— 只等"接口出现"是不够的：接口先出现、地址后到，
# 而上面那个坑的后果恰好就是"接口在、地址不在"。判据必须是有没有 inet。
i=0
while [ $i -lt 60 ]; do
	ip -4 addr show tailscale0 2>/dev/null | grep -q "inet " && break
	sleep 1
	i=$((i+1))
done

if [ $i -lt 60 ]; then
	# 先删再补：这条路由可能是上一轮"地址还没有的时候"加的，那时内核算出来的
	# 源地址是错的；删掉重加才会按现在的地址重算。
	ip route del 100.64.0.0/10 dev tailscale0 2>/dev/null
	ip route add 100.64.0.0/10 dev tailscale0 2>/dev/null
	echo "$(date) tailscale0 就绪（$(ip -4 addr show tailscale0 | grep -o 'inet [0-9.]*')），主表路由已重补" >> "$LOG"
else
	echo "$(date) 警告：60 秒内 tailscale0 没拿到地址（tun 在但无 IP）。隧道出方向会坏，客户端会自动退回局域网直连，家里不影响。" >> "$LOG"
fi

wait $TSD
