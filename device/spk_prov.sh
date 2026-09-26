#!/bin/sh
# spk_prov.sh —— 音箱配网：进热点 → 等人填 → 试连 → 连上 macmini 才算成功。
#
# 【形态】★ 一次性执行，不是常驻守护。被触发者用 start-stop-daemon -S -b 起，跑完就退：
#     · 长按顶部键（P5 的 spk_provkey.sh 监听 event2 的 KEY_HOME）
#     · 或 macmini 经 8898 下一句 `sh /mnt/UDISK/spk/spk_prov.sh`
#   常驻的那个是【触发者】，不是本脚本 —— 本脚本只在"要配网"时活几分钟。
#
# 【为什么整件事必须在设备侧跑】★ 单射频：开热点就得把 wlan0 从家里网摘下来，
#   那一刻 macmini 就够不到这台音箱了（Tailscale 也断）。
#   ⇒ 进去之前能下命令，出来后能收结果，中间这段【只有设备自己】。
#   这不是设计选择，是硬件形状决定的（BCM43436B0 单频 + bcmdhd）。
#
# 【为什么敢不备份 WiFi 配置】★★★ 因为本脚本【一个字节都不写 wpa_supplicant.conf】。
#   试连走 wpa_cli 的运行时配置（add_network/set_network/select_network），
#   /mnt/UDISK/wifi/wpa_supplicant.conf 里 update_config=1 ⇒ 只有我们明确
#   save_config 时才写回。密码错了就 remove_network 抹掉，原配置毫发无损。
#   退一万步，出来时只要【重启 wpa_supplicant】，它读原配置自己就连回原网了
#   （2026-09-23 实测：8 秒内自己关联上，连 wpa_cli reconnect 都不用）。
#   ⇒ 还是拷一份只读快照到 backup/，但那是"最后保底"，不是机制本身。
#
# 【接口解耦：配网参数从哪来】本脚本只认一个文件：$D/request，两行（SSID / 密码）。
#   谁写的不管 —— P3 的配网页写它、人手工 echo 写它、将来别的东西也能写。
#   ★ 这样"状态机"和"网页"是两件事，可以分开验（一次只动一个变量）。
#
# 用法：
#   sh /mnt/UDISK/spk/spk_prov.sh                正常跑一遍
#   sh /mnt/UDISK/spk/spk_prov.sh stat           只看现在什么状态，什么也不动
#
# 输出【每条路径的最后一行固定是 PROV-OK / PROV-FAIL / PROV-TIMEOUT / PROV-SKIP】——
#   走 8898 的 ret 通道时，空回复会被当成"没回话"（spk_netd 踩过：白等 8 秒），
#   所以本脚本一条出口都不留空。★ 同 spk_ota.sh 的规矩。
#
# 回退：本脚本不被任何东西自动调用（P5 的触发者还没装）。删掉它对全机零影响。

set -u

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

# ---------------------------------------------------------------------------
# 可配置项。★ 这些【不是测试专用钩子】—— 都是"本来就不该写死"的东西
#   （状态目录、进程表根、各处等待时长）。测试靠改它们 + PATH 前置桩目录，
#   所以测的是真代码路径，不是另写一份。
D=${SPK_PROV_DIR:-/mnt/UDISK/spk/prov}        # 状态目录（备份/request/日志）
PROC=${SPK_PROV_PROC:-/proc}                  # 进程表根
LOG=$D/prov.log
IF=wlan0
WPACONF=${SPK_PROV_WPA_CONF:-/mnt/UDISK/wifi/wpa_supplicant.conf}
HAPDCONF=${SPK_PROV_HOSTAPD_CONF:-/mnt/UDISK/wifi/hostapd.conf}
DNSMASQ_DIR=${SPK_PROV_DNSMASQ_DIR:-/tmp/dnsmasq.d}
CAPTIVE=$DNSMASQ_DIR/captive.conf
MACMINI=${SPK_PROV_MACMINI:-$SPK_SRV:$SPK_PORT_MP3}   # ★ "连上 macmini"的判据落点
AP_IP=${SPK_PROV_AP_IP:-192.168.5.1}          # 与原厂 /etc/dnsmasq.conf 的网段严丝合缝

TIMEOUT=${SPK_PROV_TIMEOUT:-300}              # 进热点后没人来配 ⇒ 超时爬回原网
RETRY=${SPK_PROV_RETRY:-3}                    # 最多来回几轮（密码错/够不到 macmini）
TICK=${SPK_PROV_TICK:-1}                      # 内层轮询间隔
AP_WAIT=${SPK_PROV_AP_WAIT:-8}                # 等 hostapd 起来
ASSOC_WAIT=${SPK_PROV_ASSOC_WAIT:-25}         # 等关联上目标网
LINK_WAIT=${SPK_PROV_LINK_WAIT:-30}           # 等够到 macmini
BACK_WAIT=${SPK_PROV_BACK_WAIT:-20}           # 等"爬回原网"关联上

HTTPD=${SPK_PROV_HTTPD:-}                     # ★ P3 才有的东西。空=不起（配网页还没做）
HTTPD_PORT=${SPK_PROV_HTTPD_PORT:-80}
HTTPD_ROOT=${SPK_PROV_HTTPD_ROOT:-/mnt/UDISK/spk/www}
LONG_PRESS_NOTE=1                             # 只是提醒读的人：触发在 P5，不在本脚本

# ★ TICK=0 会让所有等待循环空转（超时永远不到）⇒ 归一成至少 1 秒
[ "$TICK" -ge 1 ] 2>/dev/null || TICK=1

mkdir -p "$D" 2>/dev/null
LOCK=$D/.lock

# ---------------------------------------------------------------------------
log() {
	# 同时写盘和 stdout：走 8898 时人要看见，事后排错要有账。
	# ★★ 口令【永远】不许进日志 —— 本脚本拿得到 $psk，但只让它活在 wpa_cli 的参数里。
	printf '%s %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
	printf '%s\n' "$*"
}

# 每条出口的最后一行。★ 空回复会被 8898 的 ret 通道当成"没回话"。
finish() { log "$1"; exit "${2:-0}"; }

now() { date +%s; }

# ---------------------------------------------------------------------------
# 进程。★ 一律 findpid + kill <pid>，【绝不 pkill -f】（spk_net.sh 里踩过：
#   `pkill -f` 会把自己也匹配上，等于自杀）。★ 判名字读 comm ——
#   cmdline 的第一段是解释器（对 /bin/sh 跑的脚本来说 comm 是 "sh"），
#   所以查脚本进程要另用 cmdline，查二进制进程用 comm。
findpid() {
	# findpid <comm>  —— 按 /proc/<pid>/comm 精确匹配二进制名
	for d in "$PROC"/[0-9]*; do
		[ -r "$d/comm" ] || continue
		[ "$(cat "$d/comm" 2>/dev/null)" = "$1" ] && { basename "$d"; return 0; }
	done
	return 1
}

findpid_script() {
	# findpid_script <脚本路径>  —— 脚本进程的 comm 是解释器，只能看 cmdline
	for d in "$PROC"/[0-9]*; do
		[ -r "$d/cmdline" ] || continue
		case "$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null )" in
		*"$1"*) basename "$d"; return 0 ;;
		esac
	done
	return 1
}

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# 抢 DHCP 的客户端 —— ★★ 进热点前必须按下去，否则配网页会失效。
#
# 【为什么】原厂有个【开机就在】的常驻客户端：
#     /sbin/udhcpc -i wlan0 -h SING-F559 -S -T 1
#   （没有 -n / -q ⇒ 它不会退，一直在发 DISCOVER，重试间隔 1 秒。）
#   热点起来、wlan0 配上 192.168.5.1 之后，设备【自己的】dnsmasq 会应答它
#   ⇒ wlan0 的地址被换成 192.168.5.x ⇒ 网关地址没了 ⇒ 手机连上热点也打不开配网页。
#   2026-09-23 实测确认它一直在跑（pid 695，ppid=1）。
#   netguard.sh 那个 udhcpc 反而无害（它只在"已关联但网关不通"时才跑，
#   热点模式下 ap_now 为空 ⇒ 它不跑），所以只需要按原厂这一个。
DHC_PIDS=""
find_dhcpc() {
	for d in "$PROC"/[0-9]*; do
		[ -r "$d/cmdline" ] || continue
		case "$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null )" in
		*udhcpc*"$IF"*) basename "$d" ;;
		esac
	done
}

stop_dhcpc() {
	DHC_PIDS=$(find_dhcpc | tr '\n' ' ')
	[ -z "$DHC_PIDS" ] && { log "没有抢 DHCP 的客户端"; return 0; }
	for p in $DHC_PIDS; do
		# ★ 存下它原来的 argv —— 出来时要按原样放回去，不凭记忆拼
		tr '\0' '\n' < "$PROC/$p/cmdline" > "$D/dhcpc.$p.argv" 2>/dev/null
		kill "$p" 2>/dev/null
	done
	log "按下去了 $(echo $DHC_PIDS | wc -w) 个抢 DHCP 的客户端（出来会放回去）"
	sleep 1
}

start_dhcpc() {
	# 出来时逐个放回（按存下来的 argv）
	for f in "$D"/dhcpc.*.argv; do
		[ -f "$f" ] || continue
		set --
		while IFS= read -r a; do
			[ -n "$a" ] && set -- "$@" "$a"
		done < "$f"
		log "把 DHCP 客户端放回去：$*"
		"$@" >/dev/null 2>&1 &
		rm -f "$f"
	done
	return 0
}

# ---------------------------------------------------------------------------
# 网络状态判据。★★ 一律用 iwconfig 而不是 wpa_cli status —— 后者报的可能是
#   配置值/缓存，不是接口的实际状态（这条坑踩过，见 netease-vbox-wifi-client）。
ap_now() {
	iwconfig $IF 2>/dev/null | grep -o 'Access Point: [0-9A-Fa-f:]*' | cut -d' ' -f3
}

essid_now() {
	iwconfig $IF 2>/dev/null | grep -o 'ESSID:"[^"]*"' | cut -d'"' -f2
}

ip_now() {
	ip -4 addr show $IF 2>/dev/null | grep -o 'inet [0-9.]*' | head -1 | cut -d' ' -f2
}

link_ok() {
	# ★ 判据是"能不能跟 macmini 说上话"，不是"WiFi 关联上了"—— 这正是主人要的语义。
	#   curl 对 HTTP 404/403 也返回退出码 0 ⇒ 只要 TCP 通了、请求发出去收回了回应就算通。
	#   （8899 根路径回 404 是正常的；8898 要 token 回 403。）
	#   -o /dev/null 不落盘：设备上 curl 拉 0 字节文件会挂死的旧账，这里绕开。
	curl -s -o /dev/null -m 5 "http://$MACMINI/" 2>/dev/null
}

# ---------------------------------------------------------------------------
# 等人配：只认这一个文件。两行，第一行 SSID，第二行密码。
# ★ 用 mv 原子地"取走"—— 避免同一个 request 被处理两遍。
take_request() {
	[ -f "$D/request" ] || return 1
	mv "$D/request" "$D/request.taken" 2>/dev/null || return 1
	SSID=$(sed -n '1p' "$D/request.taken" 2>/dev/null)
	PSK=$(sed -n '2p' "$D/request.taken" 2>/dev/null)
	rm -f "$D/request.taken"
	[ -n "$SSID" ] || return 1
	return 0
}

# ---------------------------------------------------------------------------
# wpa_cli 通道。★ socket 路径【从配置文件里读】ctrl_interface，不写死
#   （原厂那份写的是 /mnt/UDISK/wifi/sockets）。
#   ★★ 必须带 -p：不带的话 wpa_cli 会去默认的 /var/run/wpa_supplicant 找，
#      报 "Failed to connect to non-global ctrl_ifname" —— 那个报错是虚惊，
#      2026-09-23 实测带 -p 就完全正常。
WPA_SOCK=""
wpa_sock() {
	[ -n "$WPA_SOCK" ] && return 0
	WPA_SOCK=$(sed -n 's/^[[:space:]]*ctrl_interface=//p' "$WPACONF" 2>/dev/null | head -1)
	[ -n "$WPA_SOCK" ] || WPA_SOCK=/mnt/UDISK/wifi/sockets
}

w() {
	# w <命令...>  —— 回显 wpa_cli 的返回值（最后一行）
	wpa_sock
	wpa_cli -p "$WPA_SOCK" -i $IF "$@" 2>/dev/null | tail -1
}

# ---------------------------------------------------------------------------
# 进 / 出 热点
start_wpa() {
	# ★ 照抄【进程原来的】argv 重启，不凭记忆拼命令行 ——
	#   参数一变（少个 -O 或 -I）行为就不同，而这种差异极难查。
	if [ -s "$D/wpa.argv" ]; then
		set --
		while IFS= read -r a; do
			[ -n "$a" ] && set -- "$@" "$a"
		done < "$D/wpa.argv"
		log "重启 wpa_supplicant：$*"
		"$@"
	else
		log "★ 没存到原 argv，用已知配方（这种情况不该发生）"
		wpa_supplicant -B -i$IF -Dnl80211 -c"$WPACONF" \
			-I/mnt/UDISK/wifi/wpa_supplicant_overlay.conf \
			-O/mnt/UDISK/wifi/sockets
	fi
}

enter_ap() {
	log "--- 进热点 ---"
	# 1. 记住 wpa_supplicant 是怎么起的（出来时要照抄）
	wp=$(findpid wpa_supplicant || true)
	if [ -z "$wp" ]; then
		log "★ 没找到 wpa_supplicant —— 接口状态未知，仍继续（hostapd 会告诉我们行不行）"
	else
		tr '\0' '\n' < "$PROC/$wp/cmdline" > "$D/wpa.argv"
		log "记住 wpa_supplicant(pid $wp) 的 argv"
		# 2. 停它。★ 单射频：它和 hostapd 抢同一个接口，必须让它先走。
		kill "$wp" 2>/dev/null
		sleep 3
	fi

	# 3. 把抢 DHCP 的客户端按下去（★ 否则我们的 192.168.5.1 会被自己的 dnsmasq 顶掉）
	stop_dhcpc

	# 4. 清掉上一轮的 request（否则一进热点就立刻读到旧参数）
	rm -f "$D/request" "$D/request.taken"

	# 4. 起热点。★ hostapd 用原厂那份 conf（开放热点 三音云音箱-F559，P1b 实测能起）。
	#    守卫：已经在跑就不重复起。
	if [ -n "$(findpid hostapd || true)" ]; then
		log "★ hostapd 已经在跑，先收掉"
		kill "$(findpid hostapd)" 2>/dev/null
		sleep 2
	fi
	hostapd -B -P "$D/hostapd.pid" "$HAPDCONF" > "$D/hostapd.out" 2>&1
	sleep "$AP_WAIT"

	hp=$(findpid hostapd || true)
	if [ -z "$hp" ]; then
		log "★ hostapd 没起来。它的输出："
		sed 's/^/    /' "$D/hostapd.out" 2>/dev/null
		return 1
	fi
	log "hostapd 起来了 pid=$hp  模式=$(iwconfig $IF 2>/dev/null | grep -o 'Mode:[A-Za-z]*')"

	# 5. 给热点接口配上网关地址 —— 原厂 /etc/dnsmasq.conf 的 dhcp-range 就在
	#    192.168.5.x，配上它 dnsmasq 才认这个网段、才会发租约。★ 不改网易任何文件。
	ifconfig $IF "$AP_IP" up 2>/dev/null

	# 6. captive portal：把所有域名解析到配网页，并声明 RFC 8910 的 API 地址。
	#    ★ 只写 /tmp/dnsmasq.d/（tmpfs，重启自净），绝不碰 /etc/dnsmasq.conf。
	#    ★★ 绝不用 kill $(pidof dnsmasq) —— 那会把全屋 DNS 搞崩（本机老账）。
	#       改完用 SIGHUP 让它重读。
	mkdir -p "$DNSMASQ_DIR" 2>/dev/null
	{
		printf 'address=/#/%s\n' "$AP_IP"
		printf 'address=/captive.apple.com/%s\n' "$AP_IP"
		printf 'address=/connectivitycheck.gstatic.com/%s\n' "$AP_IP"
		printf 'address=/www.msftconnecttest.com/%s\n' "$AP_IP"
		printf 'dhcp-option=114,"http://%s/"\n' "$AP_IP"
	} > "$CAPTIVE"
	sighup_dnsmasq

	# 7. 配网页服务（P3 才有）。空 ⇒ 跳过，人可以用别的方式写 request。
	if [ -n "$HTTPD" ] && [ -x "$HTTPD" ]; then
		"$HTTPD" -h "$HTTPD_ROOT" -p "$HTTPD_PORT" > "$D/httpd.out" 2>&1 &
		log "配网页服务起了（$HTTPD_PORT，docroot $HTTPD_ROOT）"
	else
		log "★ 没有配网页服务（P3 还没做）—— 等 $D/request 出现"
	fi
	return 0
}

sighup_dnsmasq() {
	dp=$(findpid dnsmasq || true)
	if [ -n "$dp" ]; then
		kill -HUP "$dp" 2>/dev/null && log "dnsmasq(pid $dp) 已 SIGHUP 重读"
	else
		log "★ dnsmasq 没在跑，captive 配置写了也没用"
	fi
}

stop_captive() {
	[ -f "$CAPTIVE" ] && { rm -f "$CAPTIVE"; sighup_dnsmasq; }
	return 0
}

leave_ap() {
	# 停热点 + 撤 captive。★ 顺序要紧：先撤 DNS 劫持，再停 hostapd ——
	# 反过来的话中间那一小段里 DNS 还指着已经不存在的配网页。
	stop_captive
	hp=$(findpid hostapd || true)
	[ -n "$hp" ] && { kill "$hp" 2>/dev/null; sleep 3; }
	return 0
}

# ---------------------------------------------------------------------------
# 试连目标网（★ 全程不碰配置文件）
try_connect() {
	# 用 wpa_cli 加一个临时 network，只启用它 ⇒ 不会被原有那几个网的信号强度抢走。
	# ★★ 这一步很关键：配置里本来就有两个 network（主 AP / 中继 AP），
	#    要是只 add 不 select，设备可能继续连旧网，用户会以为"配网没生效"。
	NETID=$(w add_network)
	case "$NETID" in
	''|FAIL*|*[!0-9]*) log "★ add_network 失败（回了 '$NETID'）"; return 1 ;;
	esac
	w set_network "$NETID" ssid "\"$SSID\"" >/dev/null
	w set_network "$NETID" psk "\"$PSK\"" >/dev/null
	w set_network "$NETID" priority 100 >/dev/null   # 持久化后它也优先
	w select_network "$NETID" >/dev/null
	log "已下发试连：ssid=\"$SSID\"（network id=$NETID，口令不进日志）"
	return 0
}

wait_assoc() {
	# ★ 判据用 iwconfig 的 Access Point：有值 = 关联上了。
	#   WPA2 密码错会关联失败 ⇒ 这个判据同时覆盖"密码对不对"。
	i=0
	while [ "$i" -lt "$ASSOC_WAIT" ]; do
		[ -n "$(ap_now)" ] && { log "关联上了：AP=$(ap_now)"; return 0; }
		sleep "$TICK"
		i=$((i + 1))
	done
	log "★ 等 $ASSOC_WAIT 秒没关联上（多半是密码错、SSID 不对、或信号太弱）"
	return 1
}

wait_link() {
	# ★★ 主人要的语义就在这儿：不是"WiFi 连上了"，是"能跟 macmini 说上话"。
	i=0
	while [ "$i" -lt "$LINK_WAIT" ]; do
		if link_ok; then
			log "够到 macmini 了（$MACMINI，IP=$(ip_now)）"
			return 0
		fi
		sleep "$TICK"
		i=$((i + 1))
	done
	log "★ 关联上了但 $LINK_WAIT 秒够不到 macmini —— 配的是别的网，或者这张网跟家里隔离"
	return 1
}

drop_network() {
	[ -n "${NETID:-}" ] && { w remove_network "$NETID" >/dev/null; NETID=""; }
	return 0
}

keep_network() {
	# 试连成功 ⇒ 持久化（配置文件里 update_config=1 才会真写）。
	# ★ 失败就【不写】—— 一个连不上的网留在配置里，下次开机会多绕一圈。
	r=$(w save_config)
	log "save_config ⇒ $r"
	return 0
}

# ---------------------------------------------------------------------------
# 恢复：把音箱弄回"进热点之前"的样子
restore() {
	log "--- 恢复：爬回原来的网 ---"
	leave_ap
	[ -n "${NETID:-}" ] && drop_network
	start_wpa
	start_dhcpc
	# ★ 实测：wpa_supplicant 起来后自己就会关联（8 秒足够），
	#   连 wpa_cli reconnect 都不需要。这里只是等它。
	i=0
	while [ "$i" -lt "$BACK_WAIT" ]; do
		[ -n "$(ap_now)" ] && break
		sleep "$TICK"
		i=$((i + 1))
	done
	[ -n "$(ap_now)" ] || log "★ 还没连回原网（可能原网不在范围）"
	udhcpc -i $IF -q -n -t 8 >/dev/null 2>&1
	log "现在：ESSID=\"$(essid_now)\"  IP=$(ip_now)  AP=$(ap_now)"
	return 0
}

# ---------------------------------------------------------------------------
# 收工清理（★ 崩溃安全：锁里存 pid，核实它真是本脚本，陈旧就抢过来）
#
# ★★★ 这条是从 muxguard 的教训来的：旧写法 `mkdir || exit` 从不 rmdir，
#     一次 kill -9 就把锁永久留下，守护再也起不来【且不报错】。
#     本脚本是一次性的，更要保证异常退出也能清锁 —— 用 trap。
RELEASED=0
release_lock() {
	[ "$RELEASED" = 1 ] && return 0
	RELEASED=1
	rm -rf "$LOCK" 2>/dev/null
}
trap 'release_lock' EXIT INT TERM HUP

# ---------------------------------------------------------------------------
cmd_stat() {
	printf 'wpa_supplicant : %s\n' "$(findpid wpa_supplicant || echo 无)"
	printf 'hostapd        : %s\n' "$(findpid hostapd || echo 无)"
	printf '本脚本(别的实例): %s\n' "$(findpid_script spk_prov.sh || echo 无)"
	printf 'wlan0          : ESSID="%s"  AP=%s  IP=%s  %s\n' \
		"$(essid_now)" "$(ap_now)" "$(ip_now)" \
		"$(iwconfig $IF 2>/dev/null | grep -o 'Mode:[A-Za-z]*')"
	printf 'captive        : %s\n' "$([ -f "$CAPTIVE" ] && echo "在（$CAPTIVE）" || echo 不在)"
	printf 'request        : %s\n' "$([ -f "$D/request" ] && echo 在 || echo 不在)"
	printf 'just_paired    : %s\n' "$([ -f "$D/just_paired" ] && echo 在 || echo 不在)"
	printf 'link_ok        : %s\n' "$(link_ok && echo 通 || echo 不通)"
	printf '超时设定       : %s 秒   最多来回 %s 轮\n' "$TIMEOUT" "$RETRY"
}

# ---------------------------------------------------------------------------
cmd_main() {
	# 单实例锁（崩溃安全，见上面 trap 那段）
	for _t in 1 2 3; do
		mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
		# 锁被占了：看锁主还活着吗。★ 判据是"那个 pid 还在跑本脚本"——
		# 脚本进程的 comm 是解释器（sh），所以只能读 cmdline 找 spk_prov。
		_op=$(cat "$LOCK/pid" 2>/dev/null)
		if [ -n "$_op" ] && [ -r "$PROC/$_op/cmdline" ] \
			&& { tr '\0' ' ' < "$PROC/$_op/cmdline"; } 2>/dev/null | grep -q spk_prov; then
			log "★ 已经有一个在跑（pid $_op），这次不做"
			finish "PROV-SKIP 已有实例在跑"
		fi
		# 锁主不在了（崩过 / 被 kill -9）⇒ 抢过来。★ muxguard 的教训：锁必须崩溃安全
		log "★ 捡到一个陈旧的锁（pid ${_op:-空} 不在了）"
		rm -rf "$LOCK"
	done

	# 只读快照（最后保底，不是机制本身 —— 本脚本不写配置文件）
	mkdir -p "$D/backup" 2>/dev/null
	[ -f "$WPACONF" ] && cp "$WPACONF" "$D/backup/wpa_supplicant.conf.$(date '+%Y%m%d-%H%M%S')" 2>/dev/null
	# 备份只留最近 3 份（UDISK 只剩十几 MB）
	ls -1t "$D/backup/wpa_supplicant.conf."* 2>/dev/null | tail -n +4 | while read -r f; do rm -f "$f"; done

	log "=== 开始配网 ==="
	log "进之前：ESSID=\"$(essid_now)\"  IP=$(ip_now)  AP=$(ap_now)"
	# ★ last_error 也一起清 —— 它是给配网页看的那句话，
	#   不清的话上一轮配网留下的「密码不对」会漏到这一次，
	#   用户刚进配网页就看见一句陈旧的报错（写验证台时发现的）。
	rm -f "$D/just_paired" "$D/last_error"

	attempt=0
	while [ "$attempt" -lt "$RETRY" ]; do
		attempt=$((attempt + 1))
		log "--- 第 $attempt/$RETRY 轮 ---"

		enter_ap || { restore; finish "PROV-FAIL hostapd 起不来（热点开不出去）" 1; }

		# ---- 等人来配（超时就从这儿出去）----
		log "等 $D/request 出现（最多 $TIMEOUT 秒没人配就爬回原网）"
		t=0
		while [ "$t" -lt "$TIMEOUT" ]; do
			take_request && break
			sleep "$TICK"
			t=$((t + TICK))
		done
		if [ -z "${SSID:-}" ]; then
			restore
			finish "PROV-TIMEOUT $TIMEOUT 秒没人配，已爬回原网（ESSID=\"$(essid_now)\"）" 2
		fi
		log "收到配网请求：ssid=\"$SSID\"（口令长度 ${#PSK}，不打印内容）"

		# ---- 试连 ----
		leave_ap
		# ★★★ 致命的一步：wpa_supplicant 在 enter_ap 里被停掉了，试连前【必须起回来】——
		#   否则 wpa_cli 连不上 ctrl socket，add_network 必然失败（这一版自查时抓到的）。
		#   起来后给它 2 秒把 socket 建好（那个 "Failed to connect to non-global
		#   ctrl_ifname" 报错就是这个 socket 还没就绪）。
		start_wpa
		sleep 2
		try_connect
		rc=$?
		if [ $rc -ne 0 ]; then
			printf 'add_network 失败，请重试\n' > "$D/last_error"
			SSID=""
			continue
		fi

		wait_assoc || {
			printf '连不上「%s」：密码不对，或者这个网不在范围。再试一次。\n' "$SSID" > "$D/last_error"
			drop_network
			SSID=""
			continue
		}
		udhcpc -i $IF -q -n -t 8 >/dev/null 2>&1

		if wait_link; then
			keep_network
			rm -f "$D/last_error"
			touch "$D/just_paired"      # ★ P6 的 onlink() 认这个标记播欢迎语
			finish "PROV-OK 配网成功：ssid=\"$SSID\"  IP=$(ip_now)  已够到 macmini"
		fi

		printf '连上「%s」了，但够不到 macmini —— 是不是配错网了？再试一次。\n' "$SSID" > "$D/last_error"
		drop_network
		SSID=""
	done

	restore
	finish "PROV-FAIL 试了 $RETRY 轮都没成，已爬回原网（ESSID=\"$(essid_now)\"）" 1
}

# ---------------------------------------------------------------------------
case "${1:-run}" in
stat) cmd_stat ;;
run)  cmd_main ;;
*)
	printf 'spk_prov —— 音箱配网（进热点→等人填→试连→连上 macmini 才算成功）\n'
	printf '用法：\n'
	printf '  sh %s          跑一遍\n' "$0"
	printf '  sh %s stat     只看状态，什么也不动\n' "$0"
	exit 2
	;;
esac
