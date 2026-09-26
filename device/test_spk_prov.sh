#!/bin/bash
# test_spk_prov.sh —— 离线验证 spk_prov.sh 的状态机。
#
# ★ 全程不碰真音箱、不断网、不出声、不改任何生产文件。
#
# 【怎么做到"测的是真代码路径"】不用另写一份逻辑，靠两件事：
#   ① 把 spk_prov.sh 里那些"本来就不该写死"的东西改成可配置：
#      SPK_PROV_PROC（进程表根）、SPK_PROV_DIR、各处等待秒数……
#   ② PATH 前置一个桩目录，把 hostapd / wpa_supplicant / wpa_cli / iwconfig /
#      ifconfig / ip / curl / udhcpc 换掉。
#
# 【★ kill 不桩】—— 本验证台最关键的一个设计决定。
#   桩掉的进程是【真的活着的进程】（一个 sh 守护），它的 pid 被登记进假进程表。
#   spk_prov.sh 走 findpid → kill <pid> 时，杀的是这个真守护 ——
#   于是 `kill` 保持原样（不桩、不改），隔离性靠"假进程表里只有我们自己的桩"保证。
#   守护收到 SIGTERM 时自己把自己的假进程表条目删掉 ⇒ 下一次 findpid 看不到它。
#   ★ 要是一开始改用桩 kill，就得连 kill -0 / kill -HUP 一起桩，
#     那测到的就变成"我对 kill 的想象"，不是 kill 本身。
#
# 【★ 桩的名字必须从 $0 取，不能用环境变量传】——
#   环境变量是【全脚本共享】的：脚本要依次起 hostapd 和 wpa_supplicant，
#   名字一旦靠 export 传，后起的那个就会继承前一个的名字，
#   于是 findpid hostapd 永远找不到它。这是自查时改掉的一处。
#
# 【★ 喂参数必须等脚本自己说"我准备好了"】——
#   脚本进热点时会 `rm -f request` 清掉上一轮的残留，
#   所以"睡前 1 秒写 request"这种写法会被它吃掉，测出来是假失败。
#   这里改成轮询它的日志（`等 ... request 出现`），看到第 N 轮准备好了才写。
set -u

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

SPK=${SPK:-$(dirname "$0")/spk_prov.sh}
ROOT=$(mktemp -d /tmp/spkprovest.XXXXXX)
PROC=$ROOT/proc          # 假进程表
STUB=$ROOT/stub          # PATH 前置的桩
D=$ROOT/prov             # SPK_PROV_DIR
W=$ROOT/world            # 桩世界的状态（关联/IP/够不够到 macmini）
S=$ROOT/st               # 桩自己的状态（wpa network 表等）
DNS=$ROOT/dnsmasq.d      # 假的 /tmp/dnsmasq.d
REALCONF=$ROOT/wpa_supplicant.conf    # 假的"设备上的配置文件"
HUP=$ROOT/dnsmasq.hup
mkdir -p "$PROC" "$STUB" "$D" "$W" "$S" "$DNS"
export STUB_PROC=$PROC STUB_WORLD=$W STUB_ST=$S

PASS=0; FAIL=0
ok()    { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()   { FAIL=$((FAIL+1)); printf '  \033[31m✗ %s\033[0m\n' "$1"; }
chk()   { if [ "$2" = "$3" ]; then ok "$1（$2）"; else bad "$1：要 [$3] 得到 [$2]"; fi; }
head_() { printf '\n\033[1m══ %s ══\033[0m\n' "$1"; }
dhas()  { [ -n "$(find "$PROC" -maxdepth 2 -name comm -exec grep -l "^$1\$" {} \; 2>/dev/null)" ]; }

# ---------------------------------------------------------------------------
# 桩守护：自建假进程条目 → 等被杀 → 死前自己把条目删掉。
# ★ “-B”的语义 = 前台调用立刻返回、守护在后台活，靠 nohup 重新 exec 自己实现。
# ★ cmdline 也造一份 —— 真脚本要照抄它重启（start_wpa / start_dhcpc）。
mk_daemon() {
	cat > "$STUB/$1" <<'DAEMON'
#!/bin/sh
if [ "${1:-}" = "--d" ]; then
	P="$STUB_PROC/$$"
	mkdir -p "$P" 2>/dev/null
	printf '%s' "$(basename "$0")" > "$P/comm"
	printf '%s\0-B\0-i\0wlan0\0' "$0" > "$P/cmdline"
	[ -n "${STUB_PIDF:-}" ] && printf '%s' "$$" > "$STUB_PIDF"
	[ -n "${STUB_ONUP:-}" ] && sh -c "$STUB_ONUP"
	trap '[ -n "${STUB_ONTEM:-}" ] && sh -c "$STUB_ONTEM"; rm -rf "$P"; exit 0' TERM INT
	trap '[ -n "${STUB_ONHUP:-}" ] && sh -c "$STUB_ONHUP"' HUP
	while :; do sleep 1; done
fi
pidf=""; prev=""
for a in "$@"; do
	[ "$prev" = "-P" ] && pidf="$a"
	prev="$a"
done
# ★ 不用 nohup —— nohup 的职责就是把 SIGHUP 设成 SIG_IGN，
#   而"启动时就被忽略"的信号在 shell 里【再也 trap 不到】⇒ dnsmasq 桩永远收不到 HUP。
#   真实的 hostapd -B / dnsmasq 守护化都不忽略 SIGHUP（dnsmasq 靠它重读配置），
#   所以 nohup 在这里本来就是建模错了。
STUB_PIDF="$pidf" "$0" --d </dev/null >/dev/null 2>&1 &
exit 0
DAEMON
	chmod +x "$STUB/$1"
}

# 把"任意命令"变成守护（udhcpc 用；它要保留原始 argv）
cat > "$STUB/.daemonize" <<'DAEMON'
#!/bin/sh
nm=$1; shift
P="$STUB_PROC/$$"
mkdir -p "$P" 2>/dev/null
printf '%s' "$nm" > "$P/comm"
{ printf '%s\0' "$nm"; for a in "$@"; do printf '%s\0' "$a"; done; } > "$P/cmdline"
[ -n "${STUB_ONUP:-}" ] && sh -c "$STUB_ONUP"
trap '[ -n "${STUB_ONTEM:-}" ] && sh -c "$STUB_ONTEM"; rm -rf "$P"; exit 0' TERM INT
trap '[ -n "${STUB_ONHUP:-}" ] && sh -c "$STUB_ONHUP"' HUP
while :; do sleep 1; done
DAEMON
chmod +x "$STUB/.daemonize"

mk_daemon hostapd
mk_daemon wpa_supplicant
mk_daemon dnsmasq

cat > "$STUB/udhcpc" <<'EOS'
#!/bin/sh
# 带 -q 的是一次性（脚本自己调的，拿到租约就退）；不带 -q 的是原厂那个常驻
case " $* " in
*" -q "*) exit 0 ;;
esac
exec "$(dirname "$0")/.daemonize" udhcpc "$@"
EOS
chmod +x "$STUB/udhcpc"

# 桩 iwconfig：输出要骗过真脚本里那两个 grep（未关联时 Access Point 段为空）
cat > "$STUB/iwconfig" <<'EOS'
#!/bin/sh
printf 'wlan0     IEEE 802.11bgn  ESSID:"%s"\n' "$(cat "$STUB_WORLD/essid" 2>/dev/null)"
printf '          Mode:%s  Access Point: %s\n' \
	"$(cat "$STUB_WORLD/mode" 2>/dev/null || echo Managed)" \
	"$(cat "$STUB_WORLD/assoc" 2>/dev/null || echo Not-Associated)"
EOS
chmod +x "$STUB/iwconfig"

cat > "$STUB/ifconfig" <<'EOS'
#!/bin/sh
[ "${1:-}" = "wlan0" ] || exit 0
[ -n "${2:-}" ] && printf '%s' "$2" > "$STUB_WORLD/ip"
exit 0
EOS
chmod +x "$STUB/ifconfig"

cat > "$STUB/ip" <<'EOS'
#!/bin/sh
case "$*" in
*addr*show*)
	printf '2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n'
	ip=$(cat "$STUB_WORLD/ip" 2>/dev/null)
	[ -n "$ip" ] && printf '    inet %s/24 brd 192.168.5.255 scope global wlan0\n' "$ip"
	;;
esac
exit 0
EOS
chmod +x "$STUB/ip"

cat > "$STUB/curl" <<'EOS'
#!/bin/sh
[ "$(cat "$STUB_WORLD/link" 2>/dev/null)" = 1 ] && exit 0
exit 7
EOS
chmod +x "$STUB/curl"

# 桩 wpa_cli：只实现状态机真正要打交道的那几个子命令。
# ★ 值必须打在【最后一行】—— 真脚本的 w() 取的是 tail -1。
cat > "$STUB/wpa_cli" <<'EOS'
#!/bin/sh
while [ $# -gt 0 ]; do
	case "$1" in
	-p|-i) shift 2 ;;
	*) break ;;
	esac
done
cmd=${1:-}; [ $# -gt 0 ] && shift
case "$cmd" in
add_network)
	n=$(cat "$STUB_ST/wpa.next" 2>/dev/null || echo 0)
	printf '%s' "$((n+1))" > "$STUB_ST/wpa.next"
	printf '\n%s\n' "$n"
	;;
set_network)
	id=$1; field=$2; shift 2
	v=$(printf '%s' "$*" | sed 's/^"//; s/"$//')
	printf '%s' "$v" > "$STUB_ST/wpa.$id.$field"
	printf '\nOK\n'
	;;
select_network)
	id=$1
	ssid=$(cat "$STUB_ST/wpa.$id.ssid" 2>/dev/null)
	if [ -n "$ssid" ] && [ "$ssid" = "$(cat "$STUB_WORLD/good_ssid" 2>/dev/null)" ]; then
		cp "$STUB_WORLD/good_assoc" "$STUB_WORLD/assoc"
		cp "$STUB_WORLD/good_essid" "$STUB_WORLD/essid"
		cp "$STUB_WORLD/good_ip"    "$STUB_WORLD/ip"
	else
		: > "$STUB_WORLD/assoc"; : > "$STUB_WORLD/essid"; : > "$STUB_WORLD/ip"
	fi
	printf '\nOK\n'
	;;
remove_network)
	rm -f "$STUB_ST/wpa.$1.ssid" "$STUB_ST/wpa.$1.psk" "$STUB_ST/wpa.$1.priority"
	printf '\nOK\n'
	;;
save_config)
	printf 'saved-at-%s\n' "$(date +%s)" >> "$STUB_ST/saved.conf"
	printf '\nOK\n'
	;;
*)
	printf '\nOK\n'
	;;
esac
EOS
chmod +x "$STUB/wpa_cli"

# ---------------------------------------------------------------------------
# 假配置文件（内容不重要，只用来验"逐字节没被动过"）
cat > "$REALCONF" <<'EOC'
ctrl_interface=/mnt/UDISK/wifi/sockets
update_config=1

network={
	ssid="MyHomeWiFi"
	psk="secret"
}
EOC
CONF_MD5_BEFORE=$(md5sum "$REALCONF" | cut -d' ' -f1)

set_world() {   # good_ssid good_essid good_assoc good_ip link
	printf '%s' "$1" > "$W/good_ssid"
	printf '%s' "$2" > "$W/good_essid"
	printf '%s' "$3" > "$W/good_assoc"
	printf '%s' "$4" > "$W/good_ip"
	printf '%s' "$2" > "$W/essid"
	printf '%s' "$3" > "$W/assoc"
	printf '%s' "$4" > "$W/ip"
	printf 'Managed' > "$W/mode"
	printf '%s' "$5" > "$W/link"
}

# ★ 每场都必须把上一场的桩守护收干净 ——
#   否则 findpid 会命中残留的那个，断言全变成假信号。
kill_all() {
	for p in "$PROC"/[0-9]*; do
		[ -d "$p" ] && kill "$(basename "$p")" 2>/dev/null
	done
	sleep 0.5
	rm -rf "${PROC:?}"/* 2>/dev/null
	return 0
}

seed_base() {   # "开机就在"的两样：原厂那个抢 DHCP 的客户端 + dnsmasq
	"$STUB/udhcpc" -i wlan0 -h SING-F559 -S -T 1 &
	STUB_ONHUP="printf 'HUP\n' >> $HUP" "$STUB/dnsmasq" -C /var/etc/dnsmasq.conf -k &
	sleep 1
}

seed_wpa() {    # 设备上本来就跑着一个 wpa_supplicant
	# ★ 它的 ONUP = "起来就自己连回原来的网"，这正是设备上的真实行为
	#   （2026-09-23 实测：重启后 8 秒自己关联上，连 reconnect 都不用）
	STUB_ONUP="printf '%s' \"\$(cat $W/good_essid)\" > $W/essid; \
printf '%s' \"\$(cat $W/good_assoc)\" > $W/assoc; \
printf '%s' \"\$(cat $W/good_ip)\" > $W/ip; printf 'Managed' > $W/mode" \
	"$STUB/wpa_supplicant" -B -iwlan0 -Dnl80211 -c"$REALCONF" -O"$ROOT/sockets" &
	sleep 1
}

fresh() {
	kill_all
	rm -rf "$D"; mkdir -p "$D"
	rm -f "$S"/wpa.* "$S/saved.conf" "$S/wpa.next" "$DNS"/*.conf "$HUP"
	seed_base
	seed_wpa
}

PROV_ENV() {
	PATH="$STUB:$PATH" \
	SPK_PROV_PROC=$PROC SPK_PROV_DIR=$D SPK_PROV_DNSMASQ_DIR=$DNS \
	SPK_PROV_WPA_CONF=$REALCONF SPK_PROV_HOSTAPD_CONF=$ROOT/hostapd.conf \
	SPK_PROV_MACMINI=$SPK_SRV:$SPK_PORT_MP3 \
	SPK_PROV_TIMEOUT=${TMO:-4} SPK_PROV_RETRY=${RTY:-3} SPK_PROV_TICK=1 \
	SPK_PROV_AP_WAIT=1 SPK_PROV_ASSOC_WAIT=4 SPK_PROV_LINK_WAIT=4 \
	SPK_PROV_BACK_WAIT=3 \
	sh "$SPK" "$@"
}

last_line() { tail -1 "$ROOT/last.out"; }
grep_out()  { grep -q -- "$1" "$ROOT/last.out"; }

# ★ 起后台喂参数的进程之前，必须先把上一场的 last.out 清掉 ——
#   feed() 靠轮询 last.out 判断"脚本进到第几轮了"，读到残留就会算错轮次、
#   在脚本还没清空 request 时就写进去，然后被 `rm -f request` 吃掉，
#   表现成"第一轮的参数凭空消失"（这一版实测踩到的假失败）。
reset_out() { : > "$ROOT/last.out"; }

# ★ 等脚本进入第 N 轮的"等人配"，再把参数放进去（见文件头那段说明）
#   ★★ 标记必须取本轮【只出现一次】的那句。'request 出现' 一轮里会出现两次
#   （enter_ap 的"没有配网页服务…等 …request 出现"也含它）⇒ 计数一轮就跳到 2，
#   两个 feed 同时写入、后者盖掉前者。取"没人配就爬回原网"才是一轮一次。
feed() {   # feed <轮次> <ssid> <psk>
	i=0
	while [ "$(grep -c '没人配就爬回原网' "$ROOT/last.out" 2>/dev/null)" -lt "$1" ]; do
		sleep 0.3; i=$((i + 1))
		[ "$i" -gt 300 ] && { printf '  (feed 超时：脚本没进到第 %s 轮)\n' "$1"; return 1; }
	done
	sleep 0.4
	printf '%s\n%s\n' "$2" "$3" > "$D/request"
}

printf '\033[1m spk_prov.sh 离线验证 \033[0m 临时根 %s\n' "$ROOT"

# ═══════════════════════════════════════════════════════════════════════════
head_ "① 正常成功：进热点 → request → 试连 → 够到 macmini → PROV-OK"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 1
fresh; reset_out
( feed 1 "MyHomeWiFi" "secret" ) &
PROV_ENV run > "$ROOT/last.out" 2>&1
chk "最后一行" "$(last_line)" 'PROV-OK 配网成功：ssid="MyHomeWiFi"  IP=192.168.1.50  已够到 macmini'
[ -f "$D/just_paired" ] && ok "just_paired 已写（P6 靠它播欢迎语）" || bad "没写 just_paired"
[ -f "$S/saved.conf" ]  && ok "成功后调了 save_config（持久化）"      || bad "没有 save_config"
[ -f "$DNS/captive.conf" ] && bad "退出时 captive.conf 没撤干净"     || ok "captive.conf 已撤"
[ -f "$D/last_error" ] && bad "成功路径不该留 last_error"            || ok "没有 last_error"
grep_out '口令长度' && ok "口令只报长度、不进日志" || bad "没走到口令那句"
dhas hostapd && bad "hostapd 还在跑" || ok "hostapd 已收"

# ═══════════════════════════════════════════════════════════════════════════
head_ "② SSID 填错 ⇒ 回热点让他重填；第二轮填对就成"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 1
fresh; reset_out; rm -f "$ROOT/err1.txt"
# ★ last_error 是给配网页看的那句话，只落文件、不进日志 ⇒
#   必须趁"第 1 轮失败、脚本回到热点等人"那一刻把它抓下来（成功时会被删掉）
(
	feed 1 "不存在的网" "x"
	i=0
	while [ ! -s "$ROOT/err1.txt" ]; do
		[ -s "$D/last_error" ] && cp "$D/last_error" "$ROOT/err1.txt"
		sleep 0.3; i=$((i + 1)); [ "$i" -gt 100 ] && break
	done
	feed 2 "MyHomeWiFi" "secret"
) &
TMO=12 RTY=3 PROV_ENV run > "$ROOT/last.out" 2>&1
chk "第二轮填对后成功" "$(last_line)" 'PROV-OK 配网成功：ssid="MyHomeWiFi"  IP=192.168.1.50  已够到 macmini'
grep -q '密码不对' "$ROOT/err1.txt" 2>/dev/null \
	&& ok "第一轮给配网页留了「再试一次」那句话" || bad "第一轮没留 last_error"
grep_out '第 2/3 轮' && ok "真的回到热点重来（有第 2 轮）"  || bad "没有第二轮"
[ -f "$D/last_error" ] && bad "成功后 last_error 该被清掉" || ok "成功后 last_error 已清"

# ═══════════════════════════════════════════════════════════════════════════
head_ "③ 超时没人配 ⇒ 自动爬回原网（PROV-TIMEOUT）"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 1
fresh
TMO=3 PROV_ENV run > "$ROOT/last.out" 2>&1
chk "最后一行开头" "$(last_line | cut -d' ' -f1)" "PROV-TIMEOUT"
grep_out '已爬回原网' && ok "报了爬回原网" || bad "没报爬回"
chk "爬回后 ESSID 复原" "$(cat $W/essid)" "MyHomeWiFi"
[ -f "$DNS/captive.conf" ] && bad "超时退出后 captive.conf 还在" || ok "captive.conf 已撤"
dhas hostapd && bad "hostapd 还活着" || ok "hostapd 已收"
dhas wpa_supplicant && ok "wpa_supplicant 已放回去" || bad "wpa_supplicant 没回来"
dhas udhcpc && ok "原厂那个抢 DHCP 的客户端已放回去" || bad "udhcpc 没放回去"
[ -s "$HUP" ] && ok "dnsmasq 收到过 SIGHUP" || bad "dnsmasq 没收到 HUP"
chk "SIGHUP 次数（进 1 + 出 1）" "$(wc -l < "$HUP")" "2"

# ═══════════════════════════════════════════════════════════════════════════
head_ "④ 关联成功但够不到 macmini ⇒ 判失败（★ 主人要的语义）"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 0   # link=0
fresh; reset_out
( feed 1 "MyHomeWiFi" "secret"; feed 2 "MyHomeWiFi" "secret" ) &
TMO=10 RTY=2 PROV_ENV run > "$ROOT/last.out" 2>&1
grep_out '够不到 macmini' && ok "识别出「连上了但够不到 macmini」" || bad "没识别出来"
chk "最后一行开头" "$(last_line | cut -d' ' -f1)" "PROV-FAIL"
[ -f "$S/saved.conf" ] && bad "够不到 macmini 却还是 save_config 了（下次开机会白绕）" \
	|| ok "没有 save_config（连不上的网不留在配置里）"
chk "试满 2 轮" "$(grep -c '第 .*轮' "$ROOT/last.out")" "2"

# ═══════════════════════════════════════════════════════════════════════════
head_ "⑤ 保命：跑完全程，配置文件逐字节没被动过"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 1
fresh
TMO=3 PROV_ENV run > "$ROOT/last.out" 2>&1
chk "假配置文件的 md5" "$(md5sum "$REALCONF" | cut -d' ' -f1)" "$CONF_MD5_BEFORE"
ls "$D"/backup/wpa_supplicant.conf.* >/dev/null 2>&1 \
	&& ok "只读快照落在 backup/（最后保底）" || bad "没有快照"

# ═══════════════════════════════════════════════════════════════════════════
head_ "⑥ 单实例：已有实例在跑时不许并发"
fresh
mkdir -p "$D/.lock" "$PROC/999999"
printf '%s' "999999" > "$D/.lock/pid"
printf 'sh' > "$PROC/999999/comm"
printf 'sh /mnt/UDISK/spk/spk_prov.sh' > "$PROC/999999/cmdline"
PROV_ENV run > "$ROOT/last.out" 2>&1
chk "最后一行" "$(last_line)" "PROV-SKIP 已有实例在跑"
rm -rf "$PROC/999999"

# ═══════════════════════════════════════════════════════════════════════════
head_ "⑦ 陈旧锁（锁主已死）⇒ 抢过来继续干，不许卡死"
set_world "MyHomeWiFi" "MyHomeWiFi" "AA:BB:CC:DD:EE:FF" "192.168.1.50" 1
fresh
mkdir -p "$D/.lock"; printf '%s' "888888" > "$D/.lock/pid"   # 假进程表里没有 888888
printf '上一轮配网留下的旧错误\n' > "$D/last_error"          # 陈旧的报错，开跑就该被清
TMO=2 PROV_ENV run > "$ROOT/last.out" 2>&1
grep_out '捡到一个陈旧的锁' && ok "识别出陈旧锁并抢过来" || bad "没识别出陈旧锁"
chk "最后一行开头" "$(last_line | cut -d' ' -f1)" "PROV-TIMEOUT"
[ -f "$D/last_error" ] && bad "陈旧 last_error 没清（用户一进配网页就看见旧报错）" \
	|| ok "开跑时清掉了陈旧的 last_error"

# ═══════════════════════════════════════════════════════════════════════════
head_ "⑧ stat：只看不动"
fresh
PROV_ENV stat > "$ROOT/last.out" 2>&1
{ grep_out 'wpa_supplicant :' && grep_out 'link_ok'; } && ok "stat 能报状态" || bad "stat 输出不对"
[ -f "$DNS/captive.conf" ] && bad "stat 动了东西" || ok "stat 什么也没动"
dhas hostapd && bad "stat 起了 hostapd" || ok "stat 没起 hostapd"

# ═══════════════════════════════════════════════════════════════════════════
printf '\n\033[1m══ 收尾 ══\033[0m\n'
printf '真音箱：全程 adb 一次都没调，没连过设备\n'
printf '生产文件：wpa_supplicant.conf 用的是 %s（假的，md5 %s）\n' "$REALCONF" "$CONF_MD5_BEFORE"
printf '临时根：%s（可删）\n' "$ROOT"
printf '\n\033[1m通过 %d   失败 %d\033[0m\n' "$PASS" "$FAIL"

kill_all
[ "$FAIL" = 0 ] || exit 1
