#!/bin/sh
# netguard —— 保证 wlan0 永远拿得到 IP。每 20 秒一轮，只在状态变化时写一行日志。
#
# 【为什么需要它】2026-09-21 重启实测：这台音箱【重启后不会自己去要 IP】。
#   wpa_supplicant 由 S110netease_wifi_service 拉起，它没有 -a 关联后动作脚本；
#   /etc/hotplug.d/iface/ 里只有 00-netstate 和 25-dnsmasq 两个 OpenWrt 钩子，
#   没有拉 DHCP 的（而且这设备不是 netifd 管网络，那两个钩子本就不为它服务）。
#   于是：关联是成功的（iwconfig 的 Access Point 有值、72 Mb/s），
#   可 wlan0 上一个 inet 都没有。整条链跟着死：
#     · mictap 的 sendto 全失败 —— 麦克风音频发不出去（它设计成每包试发、失败即丢，
#       所以表现是"静默无流"，不是报错，特别难查）
#     · spkclient 连不上 macmini:9999
#     · tailscaled 起在没网的时刻 ⇒ 连不上控制面 ⇒ 拿不到 tailscale0 的地址
#       ⇒ run.sh 那个 60 秒等待必然超时、补主表路由那步也白做
#   这是【每次重启必然发生】，不是偶发。手动 udhcpc 一次是秒级成功的
#   ⇒ 与路由器、与信号、与口令都无关，纯粹是"没人去要"。
#
# 【判据为什么用 ping，而不是只看 ip addr】有 IP 但网关不通同样是故障态：
#   关联假死、AP 换了、租约被别人顶掉，都会留下一个"看着正常"的假 IP。
#
# 【为什么要先看有没有关联】没关联时跑 udhcpc 纯属白跑（白等 6 秒）。
#   而且"没关联"和"没 IP"是两种完全不同的故障，分开记日志，下次排错才快。
#
# 【为什么 udhcpc 用 -q -n】-q 拿到租约就退、-n 失败就退 ⇒ 不留常驻进程，
#   不会跟设备自己（或网易那边）的 DHCP 客户端抢。而且 -n 失败时【不会动
#   已有的 IP】—— 万一是"网关暂时不通"而 IP 是好的，也不会把好的弄丢。
#
# 【为什么顺带管 Wi-Fi 省电】省电开着时包要等 beacon 周期才发 —— 实测关掉它
#   avg 64.3 → 30.9ms、max 181 → 92ms，对语音对话是致命的。
#   `run.sh` 第 17 行本来【就写着】要关，但它是 S95spkbrain 拉起的、而 wlan0 由
#   S110netease_wifi_service 才拉起来（95 < 109）⇒ 跑那行时 wlan0 还不存在，
#   报错被它自己的 2>/dev/null 吃掉 ⇒ **那行从来没成功过一次**，开机后驱动
#   按默认值把省电打开。★ 修法不是在 run.sh 里挪位置（那样要动开机主脚本，
#   风险大），而是放进这里每 20 秒确认一次 —— 跟管 IP 是同一个理由：
#   【守设备自己没做好的事】。见 pm_off()。
LOG=/mnt/UDISK/spk/netguard.log
# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"
LOCK=/tmp/spk/netguard.lock
IF=wlan0
EVERY=20

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
	# 日志在 NAND 上，别写爆：攒到 64KB 翻一页。正常一天都不长一行。
	[ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

# 当前关联的 AP。★ 只认 iwconfig 的 Access Point —— wpa_cli status 的 ssid/bssid
# 是配置值/缓存不是实际值，这个坑踩过（见 netease-vbox-wifi-client）。
# 没关联时 iwconfig 打的是 "Access Point: Not-Associated"，N 不是 hex 字符
# ⇒ 下面这个 grep 匹配到空 ⇒ ap 为空串，正好当"没关联"用。
ap_now() {
	iwconfig $IF 2>/dev/null | grep -o 'Access Point: [0-9A-Fa-f:]*' | cut -d' ' -f3
}

ip_now() {
	ip -4 addr show $IF 2>/dev/null | grep -o 'inet [0-9.]*' | head -1 | cut -d' ' -f2
}

gw_now() {
	g=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')
	[ -n "$g" ] && echo "$g" || echo "$SPK_GW"
}

healthy() {
	[ -n "$(ip_now)" ] || return 1
	ping -c 1 -W 2 "$(gw_now)" >/dev/null 2>&1
}

# 当前省电档位：iwconfig 打的是 "Power Management:on" / ":off"。
# 读不到就返回空串 ⇒ 不等于 off ⇒ 下一轮接着试（不缓存、不猜，只信内核这一路）。
pm_now() {
	iwconfig $IF 2>/dev/null | grep -o 'Power Management:[a-z]*' | cut -d: -f2
}

# ★★★ 关 Wi-Fi 省电（为什么必须在这里，见文件头那段）—— 幂等：已经是 off 就只是确认一下。
#   ★ 判据【现取现比】：改完再读一次，读回来不是 off 就按失败记账
#     （跟上面 udhcpc 的失败记账同款，别把 NAND 写爆）。
#   ★ wlan0 还没出现时直接跳过 —— 开机早期别去凑那份失败账
#     （这正是 run.sh 栽的那个坑，这里不能再栽一次）。
pm_off() {
	ip link show $IF >/dev/null 2>&1 || return 0
	if [ "$(pm_now)" = off ]; then
		[ "$pm_seen" != off ] && log "Wi-Fi 省电 off（已确认）"
		pm_seen=off
		return 0
	fi
	was=$(pm_now)
	iwconfig $IF power off 2>/dev/null
	if [ "$(pm_now)" = off ]; then
		log "Wi-Fi 省电原来是 ${was:-读不到}，已关掉"
		pm_seen=off
	else
		pm_fail=$((pm_fail + 1))
		# 每 15 次（≈5 分钟）才记一行
		[ $((pm_fail % 15)) -eq 1 ] && log "★ Wi-Fi 省电关不掉（iwconfig 仍报 ${was:-读不到}），第 ${pm_fail} 次"
	fi
	return 0
}

log "启动（每 ${EVERY}s 一轮）"
last=""
fail=0
pm_seen=""
pm_fail=0

while true; do
	pm_off
	if healthy; then
		if [ "$last" != ok ]; then
			log "网络正常：$(ip_now)  AP=$(ap_now)  gw=$(gw_now)"
			last=ok
		fi
		fail=0
	else
		ap=$(ap_now)
		if [ -z "$ap" ]; then
			# 还没关联上任何 AP。这时候跑 udhcpc 是白跑，等它关联。
			if [ "$last" != noap ]; then
				log "wlan0 没关联到 AP，等它（此时不跑 udhcpc，跑了也白跑）"
				last=noap
			fi
		else
			fail=$((fail + 1))
			if [ "$last" != wantip ]; then
				cur=$(ip_now); cur=${cur:-无}
				log "AP=$ap 已关联但网关不通（当前 IP=$cur），去要一个"
				last=wantip
			fi
			udhcpc -i $IF -q -n -t 6 >/dev/null 2>&1
			if healthy; then
				log "拿到 IP：$(ip_now)（第 ${fail} 次尝试）"
				last=ok
				fail=0
			elif [ $((fail % 15)) -eq 1 ]; then
				# 每 15 次（≈5 分钟）才记一行失败，别把 NAND 写爆
				log "第 ${fail} 次要 IP 仍失败（AP=$ap，网关 $(gw_now) ping 不通）"
			fi
		fi
	fi
	sleep $EVERY
done
