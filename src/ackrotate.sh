#!/bin/sh
# ackrotate.sh —— 唤醒应答随机轮换：让音箱每次被叫醒，应的话都不一样
#
# 【为什么只要换文件，不用碰数据库】
#   唤醒应答由 netease_control_center 每次唤醒时【实时查一次】skins.db 决定：
#       Get skin key: v303  val: /mnt/UDISK/resources/voice/spk_ack.mp3
#   拿到 val 后交给 Tts 调度器播。实测（2026-09-21）它【每次唤醒都重新读文件、不缓存】——
#   只推一个新 mp3 覆盖过去、一个字节都不动 skins.db，播放时长立刻跟着变
#   （旧 1.149s 播 1.27s → 新 1.515s 播 1.696s）。
#   ⇒ 换应答只需换文件。改库那条路的失败模式是"把库改坏"，这条路的失败模式只是
#     "这次播了上一句"。风险差一个量级。
#
# 【怎么知道"这次被叫了"】
#   不去 tail 日志（日志路径带 pid，重启就变；格式也可能变）。改用文件系统的硬事实：
#   UDISK 是 ext4 + relatime，而【新换进去的文件 atime == mtime】正好满足 relatime
#   的更新条件 ⇒ 设备一读它，atime 就被推上去（实测：钉 1970 → cat → 变成当前时间）。
#   于是把装上去的文件的 atime 钉到 1970 当"还没被读过"的标记，主循环只认一件事：
#       ls -lu 里还能看到 1970  ⇒ 没人读过 ⇒ 什么都不做
#       看不到 1970            ⇒ 刚被叫过 ⇒ 换下一句
#
#   ★★ 这条只对 UDISK 成立！/tmp 是 tmpfs 且挂载带 noatime，在上面测会得出"判据失效"
#      的假结论（2026-09-21 我就在 /tmp 上这么误判过一次）。要测必须在 /mnt/UDISK 上测。
#
#   ★★★ 判据最大的污染源是【我们自己】：任何 open/读这个文件的操作都会推 atime，守护就会把
#       "有人查了一下"误判成"设备刚被唤醒"⇒ 音箱莫名其妙换一句。实测教训（2026-09-21）：
#       维护脚本 --status 里写了 md5sum $CUR，于是每查一次状态音箱就换一次应答 ——
#       现场留下的痕迹是 mtime 10:14 而 atime 10:57，可同一时段 voice.log 里一条
#       "Get skin key" 都没有（有唤醒就必然有）⇒ 证明那次读是人读的，不是设备读的。
#   ⇒ 铁律：除本脚本 rotate() 内部外，谁都不许读 $CUR。
#      要知道"现在装的是哪个"读 $PREV；要看文件状态只用 ls -lu（只 stat、不 open，安全）。
#
#   ★★★ 2026-09-21 晚补：**换成 `/rom/usr/share/resource/voice/S003.mp3` 去读也一样犯禁** ——
#      那条路径和 $CUR 是【同一个 inode】（目录级 bind），从哪边 open 都推同一个 atime。
#      当晚我拿 `md5sum /rom/.../S003.mp3` 当"静默验证"，日志立刻多出一次 `换 -> bc3`，
#      而那一分钟没有任何唤醒 ⇒ 音箱被我的检查动作无端换掉一句应答。
#      **要验证"应答路通没通"，看 `ackrotate.log` 的轮换时刻跟真实唤醒时刻对不对得上**
#      （21:17:07 / 21:17:16 对上 21:17:06 / 21:17:15 两次唤醒，就是这么验的），
#      不要去看文件内容。这条误用过一次，写在这儿免得再犯。
#
# 【随机怎么来】
#   busybox 没有 $RANDOM，也没有 od。但根本不需要：用户【什么时候叫】本身就是随机的，
#   date +%s 取模就是天然随机种子。再排除掉"上一次用的那个"，保证每次听起来都不同 ——
#   纯随机反而有概率连着两次抽中同一句，那听感上还是"死的"。
#
# 【急停 / 还原】
#   touch /tmp/spk/ackrotate.off   暂停轮换（想手动钉死某一句时用）
#   rm    /tmp/spk/ackrotate.off   恢复
#   注意：手动用 set_wakevoice.sh 装的句子会被本守护顶掉，想留住请先暂停。
#   ./set_wakevoice.sh --restore   还原成网易原版（本守护不碰 skins.db，还原只跟文件有关）

POOL=/mnt/UDISK/resources/voice/spk_ack_pool
# ★★★ 2026-09-21 晚【改目标路径，原因实测】：
#   原来写的是 /mnt/UDISK/resources/voice/spk_ack.mp3，靠 skins.db 里
#   `skin.v303 → resID=12 → res.ID=12.localPath` 指过去。
#   ★ 这条路【活不过一次设备重启】。证据链（2026-09-21）：
#       ackrotate.log  09-21 18:31:26 「启动：池内 5 个」  ← 守护重启 = 设备重启
#       skins.db mtime 09-21 18:31                        ← 同一秒被改写
#       现状：res 表 ID 列表 = [1..11, 13..30]            ← 【只少了 12 这一行】
#       10:11 的库备份里 res 12 还在（=不是我们删的）
#     ⇒ 开机时网易自己的资源服务把库同步回去了，我们改的那条被抹掉，
#       v303 悬空 ⇒ 查表落回 ROM 默认 /rom/usr/share/resource/voice/S003.mp3
#       （control_center 日志实锤：`Get skin key: v303 val: /rom/.../S003.mp3`）
#       ⇒ 而那一声正是我们顶哑的 ⇒ **醒了，一声不响**，而且从现象上完全看不出来。
#       （`spk_ack.mp3` 的 atime 至今钉在 1970 = 设备从此再没读过它，与推断一致。）
#
#   ⇒ 改成写【设备实际读到的那个文件】。`/rom/usr/share/resource/voice/` 就是我们
#     自己的静音目录盖上去的（voicemute.sh 的目录级 bind），而 /rom 和 UDISK 是
#     【同一块 ext4 分区】⇒ 换掉源文件立刻生效，且**一个字节都不碰 skins.db**，
#     重启、云端同步都动不到它。
#   ★ 因为挂的是【目录】，mv 换 inode 也照样可见（单文件 bind 才有 inode 钉死的问题）。
CUR=/mnt/UDISK/spk/silent/rom/S003.mp3
# ★ PREV 放 UDISK 不放 /tmp：① 重启后还知道上一句是哪个，免得开机头一次就听重复；
#   ② 维护脚本靠它报"当前装的是哪个"——这样它就不用去读 $CUR，也就不会污染 atime 判据（见下）。
PREV=/mnt/UDISK/spk/ackrotate.prev
LOG=/mnt/UDISK/spk/ackrotate.log
LOCK=/tmp/spk/ackrotate.lock
OFF=/tmp/spk/ackrotate.off

mkdir -p /tmp/spk

# ★★★ 单实例锁必须【崩溃安全】—— 照抄 muxguard.sh / waketap.sh，别自己造。
#   旧写法是 `mkdir "$LOCK" || exit 0`，【从来不 rmdir】：一次 kill -9（OOM / 手滑 /
#   procd 强杀）就把锁永久留在那儿 ⇒ 这个守护再也起不来，而且一声不响。
#   2026-09-21 就是栽在这个写法上（muxguard 那样哑了几个小时才查出来）。
#   现在：mkdir 保原子性，锁里放 pid，再用 /proc/<pid>/cmdline 的【最后一个字段】
#   核实那个 pid 是不是真的是本脚本 ⇒ 陈旧锁自动抢过来。
#   ★ 这条现在【更要紧】：spkcheck.sh 第 3 节已经把本脚本加进保活名单了，
#     要是锁还是旧写法，就会变成"每分钟重启一次、每次都因陈旧锁立刻退出"——
#     日志上看起来像一直在重试，其实是自锁，永远起不来。
for _t in 1 2 3; do
	mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
	_op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -n "$_op" ] && [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
		exit 0                      # 真有一个同伙在跑
	fi
	rm -rf "$LOCK"                  # 陈旧锁：上一个我被 kill -9 了，没来得及清
done

log() {
	# 日志在 NAND 上，攒到 64KB 翻一页。正常运行一天也就几十行。
	[ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

pool_count() {
	n=0
	for f in "$POOL"/*.mp3; do
		[ -f "$f" ] && n=$((n + 1))
	done
	echo "$n"
}

# 挑一个：拿当前秒数当种子，且必须不同于上一次用的
pick() {
	n=$(pool_count)
	[ "$n" -eq 0 ] && return 1
	last=$(cat "$PREV" 2>/dev/null)
	i=0
	while [ "$i" -lt 10 ]; do
		k=$(( ($(date +%s) + i) % n ))
		c=0
		sel=
		for f in "$POOL"/*.mp3; do
			[ -f "$f" ] || continue
			if [ "$c" -eq "$k" ]; then
				sel="$f"
				break
			fi
			c=$((c + 1))
		done
		if [ -n "$sel" ] && [ "$sel" != "$last" ]; then
			echo "$sel"
			return 0
		fi
		i=$((i + 1))
	done
	# 池里只有一个（或挑不出不同的）⇒ 保持原样，不乱换
	return 1
}

rotate() {
	new=$(pick) || return 1
	# mv 是原子的：设备下次 open 拿到的要么是完整旧文件、要么是完整新文件，不会读到半个
	cp "$new" "$CUR.new" 2>/dev/null || return 1
	mv "$CUR.new" "$CUR" || return 1
	# ★ 钉 1970 = "还没被读过"的标记（busybox 的 -t 会把 atime/mtime 一起钉，正好）
	touch -t 197001010000 "$CUR" 2>/dev/null
	echo "$new" > "$PREV"
	log "换 -> $(basename "$new")"
	return 0
}

if [ "$(pool_count)" -eq 0 ]; then
	log "候选池 $POOL 是空的，退出（唤醒应答保持原样）"
	exit 1
fi

# 开机先换一个，免得沿用上一轮那句
rotate
log "启动：池内 $(pool_count) 个，当前 $(basename "$(cat "$PREV" 2>/dev/null)")"

while true; do
	sleep 3
	[ -f "$OFF" ] && continue
	# atime 里还有 1970 ⇒ 没被读过 ⇒ 不动
	ls -lu "$CUR" 2>/dev/null | grep -q 1970 && continue
	rotate
done
