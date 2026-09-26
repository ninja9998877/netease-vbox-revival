#!/bin/sh
# waketap.sh —— 把设备【自己唤醒引擎】认出来的唤醒事件，转给 macmini 的 spk_ear
#
# ★★★ 为什么要有它（2026-09-21 晚实测定的案，不是猜的）：
#   我们的 KWS 吃的是 mictap 在 `snd_pcm_readi` 上抓的【AEC/波束成形之前】的原始单麦；
#   设备自己的 duilite 有 4 麦远场波束成形 + AEC。同一段时间里：
#
#       设备引擎  醒了 8 次，8 次全认出（confidence 0.545/0.542/0.579，它的门限 0.52）
#       我们的 KWS 醒了 2 次
#
#   主人三次喊"嘀嗒嘀嗒"（20:54:38 / 20:55:51 / 20:55:55）**设备每次都认出来了**，
#   我们一次都没认出来 —— 他那边看到的就是"根本没回应"。
#
#   ★ 这是【硬件差距】，调参补不回来。两条路都已经实测否掉：
#       · 电平：真机录音放大 100 倍仍不命中（模型做 CMVN 归一化，增益被吃掉）
#       · 音素：扫了 14 种声调/拼法，只有 keywords.txt 里原有那两条命中
#   ⇒ 直接抄它的答案。它认出来了，就等于我们被唤醒了。
#
# ★ 判据是【日志里那一行】，不是现象：
#     /tmp/netease_voice_<pid>.log 里的
#       [D] [main.c] - Duilite_wakeup_cb(195):Wake up(hao):{"wakeupWord":"di da di da",…,"confidence":0.545190}
#   这是 netease_voice 收到 duilite 唤醒回调时自己写的，带置信度，而且是【过了它自己门限】
#   才会写的。比"看设备有没有响"可靠得多 —— 我们早把它的提示音全顶哑了，
#   从现象上什么都看不出来。
#
# ★ 用 TCP 不是我们偷懒：设备上那个 nc 是 busybox 精简版（`nc [IPADDR PORT]`），**只支持 TCP**。
#   macmini 的 ufw 对 192.168.1.0/24 全放行，不用改防火墙。
#
# ★ 日志文件名带 pid ⇒ voice 服务一重启就换名字，而 `tail -f` 会一直跟着那个
#   【已经被删掉的旧文件】（busybox 的 tail 没有 -F，不能靠它自己重开）。
#   所以本脚本把当前跟的文件写进 $CUR，由 spkcheck.sh 每分钟核对，名字变了就重启本脚本。
#   —— 这就是"第二道闸"的用法，跟 muxguard/nightmute 一个道理：守自己没做好的事。
#
# ★ 起在 run.sh（开机）里，不进 crontab —— 它是常驻进程，由 spkcheck 保活。

D=/mnt/UDISK/spk
R=/tmp/spk
# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"
MAC=$SPK_SRV          # 跑"大脑"的那台机器（默认 192.168.1.100，以你的实际部署为准）
PORT=9997              # spk_ear 的"设备唤醒口"
LOG=$D/waketap.log
CUR=$R/waketap.cur     # 我正在跟哪个日志文件（spkcheck 靠它对账）
LOCK=$R/waketap.lock

mkdir -p "$R"

# ★★★ 单实例锁必须【崩溃安全】—— 照抄 muxguard.sh，别自己造。
#   旧写法 `mkdir "$LOCK" || exit 0` 【从来不 rmdir】，一次 kill -9（OOM/手滑/procd 强杀）
#   就把锁永久留在那儿 ⇒ 这个守护再也起不来，而且一声不响。
#   2026-09-21 就是栽在这上面，音箱哑了几个小时才查出来。
#   现在：mkdir 保原子性，锁里放 pid，再用 /proc/<pid>/cmdline 的【最后一个字段】
#   核实那个 pid 是不是真的是本脚本 ⇒ 陈旧锁自动抢过来。
#   ★ 旧注释这里写的是"宁可重复跑，也绝不再永久瘫痪"—— 那是【错的取舍】：
#     重复跑的代价不是零，是"一次唤醒上报两遍"（见下），所以两个方向都要堵。
#
# ★★ 2026-09-21 晚顺手补的【第二个洞】—— 注意：这是【读代码读出来的】，不是观测到的。
#   （同一晚我一度以为"双实例"是实测的，那是误判：见下面 newest() 前面那段长注释。
#     但那不代表这个洞不存在，它的逻辑是实打实的。）
#   上面那套只防"陈旧锁"，防不住两个实例【几乎同时】启动：
#       A: mkdir 成功 ─────────────────→ echo $$ > pid
#       B:      mkdir 失败 → 读 pid（此刻【还没落盘】）→ 读到空串
#              → 旧判据 `[ -n "$_op" ]` 不成立 → rm -rf 把 A 刚拿到的锁删掉
#              → 下一轮 mkdir 成功 ⇒ **A 和 B 同时活着**
#   ⇒ 判据改掉："读到空"必须理解成【刚有人拿到锁、pid 还没写】，让一步重试，**绝不 rm**。
#     只有【确定】pid 读到了、且那个进程确实不是本脚本时，才敢把它当陈旧锁清掉。
#   （★ 锁里的 pid 一定会紧接着落盘，这个空窗只有微秒级 —— sleep 1 之后重读必然读得到。）
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

log() {
	# 日志在 NAND 上，别写爆：攒到 64KB 翻一页。
	[ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

# ★★★ 一条【别再加回来】的教训（2026-09-21 晚，我自己在这上面栽了半小时）：
#   那晚我看到 waketap.log 里每次唤醒都是【两行】，又看到 /proc 里有两个
#   `/bin/sh /mnt/UDISK/spk/waketap.sh`，就断定"起了两个实例、上报了两遍"，
#   于是加了一段"启动时扫 /proc 把重复实例杀掉"的自愈代码。
#   **两条证据都是假的，那个自愈代码本身还更危险：**
#     · 两行日志 ≠ 两个实例：设备引擎【一次唤醒本来就写两行】
#         Duilite_wakeup_cb(195):Wake up(hao):{…}     ← 回调本体
#         Duilite_wakeup_cb(211):json ok, wakeupword:… ← 紧接着的解析成功
#       而判据写的是 `*Duilite_wakeup_cb*` ⇒ 两行都命中（真 bug 见下面 tail 那段）。
#     · /proc 里那个"第二个实例"是【本脚本自己的常驻子壳】：
#       `tail -f | while` 里那个 while 是 fork 出来的子壳，**不 exec ⇒ cmdline 与父进程
#       一模一样**，连 starttime 都只差几秒。数 /proc 根本区分不了"子壳"和"第二个实例"。
#   ⇒ 照那个判断写出来的"杀掉重复实例"，会把【自己的子壳】杀掉（cmdline 相同、pid 不同），
#     等于把自己的读循环掐死；而 $$ 排除不掉它（子壳在锁判定之后才诞生）。
#   ⇒ 结论：**不许按 cmdline 杀"同名进程"** —— 在能区分"子壳"之前，那段代码一律不要。
#     真正该修的是判据（见 tail 那段），不是加杀进程的逻辑。

newest() { ls -t /tmp/netease_voice_*.log 2>/dev/null | head -1; }

# 等 voice 服务把日志建出来（开机早期可能还没有）
f=''
i=0
while [ $i -lt 120 ]; do
	f=$(newest)
	[ -n "$f" ] && break
	sleep 2
	i=$((i+2))
done
if [ -z "$f" ]; then
	log "⚠ 等不到 netease_voice 的日志，退出（我们自己的 KWS 照常，只是没抄到答案）"
	exit 1
fi

echo "$f" > "$CUR"
log "★ 开始抄设备引擎的答案：跟 $f，命中就告诉 macmini:$PORT"
echo "$(date '+%m-%d %H:%M:%S') 跟 $f" >> "$LOG"

# ★ `tail -n0` 从【当前末尾】开始 —— 绝不把开机以来的历史唤醒重放一遍。
#   重放的话 spk_ear 会凭空醒好几次，每次都要问一句"你是在跟我说话吗"。
tail -n0 -f "$f" 2>/dev/null | while IFS= read -r line; do
	# ★★★ 判据必须【一次唤醒只命中一行】—— 这一条曾经写错过，代价是"一次唤醒上报两遍"：
	#   设备引擎一次唤醒会连写两行，都带 Duilite_wakeup_cb：
	#       Duilite_wakeup_cb(195):Wake up(hao):{"wakeupWord":"di da di da",…,"confidence":0.545}
	#       Duilite_wakeup_cb(211):json ok, wakeupword:di da di da
	#   原来只写 `*Duilite_wakeup_cb*` ⇒ 两行都命中 ⇒ 发两次 TCP ⇒ spk_ear 一次唤醒处理两遍。
	#   ★ 加 `*Wake up*` 就唯一了：第二行是 "json ok, wakeupword:"，没有 "Wake up" 这个词。
	#   （不用行号 195 当判据 —— 那是源码行号，固件一升就可能变；用文本。）
	case "$line" in
		*Duilite_wakeup_cb*"Wake up"*)
			# ★★ `</dev/null` 是必须的：不加的话后台的 nc 会去读【这个 while 的 stdin】，
			#    把日志行吃掉 ⇒ 后面的唤醒全丢。这是 shell 里最阴的一类 bug。
			#    整个发送放后台 ⇒ macmini 万一不在线，nc 卡住也【不会堵住读循环】。
			( printf 'WAKE\n' | nc "$MAC" "$PORT" ) </dev/null >/dev/null 2>&1 &
			log "→ 设备引擎认出了唤醒，已告诉 spk_ear"
			;;
	esac
done

log "⚠ 跟的那行日志断了（$f）⇒ 退出，等 spkcheck 拉起来"
exit 1
