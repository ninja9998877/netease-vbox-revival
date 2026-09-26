#!/bin/sh
# 盯住 HP_L Mux —— "整台音箱一个字都不出声"的真凶就是它被指到了空模拟混音器。
#
# 实测（2026-09-20）：
#   HP_L Mux = 'Left Analog Mixer HPL Switch' 时，那条模拟混音路上的
#   LINEINL / MIC1 / MIC2 / PHONEN 【全部是 off】—— 等于左声道接进一个什么都没有的
#   岔路口。设备是【单声道喇叭】、认的就是左路，于是整台机器彻底哑掉：
#   状态机好、功放静音会乖乖解开、麦克风好、DLNA 推流也接，就是没声。
#   一层层往下剥才找到，极难定位。扳回 'DACL HPL Switch'（DAC 直连耳机输出）
#   声音立刻就有。
#
# 为什么要常驻：
#   1. amixer 的改动【只在内存里，重启就丢】—— 这台音箱会自己重启，所以每次开机都要重扳。
#   2. 谁把它改回去的【还没查出来】，但功放音量实测就是设备自己在改的，
#      说明它确实会动 ALSA 控件。所以不能只开机扳一次，得盯着。
#   3. 只读检查 30 秒一次，值不对才动手，开销可以忽略。
LOG=/mnt/UDISK/spk/muxguard.log
LOCK=/tmp/spk/muxguard.lock

mkdir -p /tmp/spk
# ★★★ 单实例锁 —— 必须【崩溃安全】。2026-09-21 血案就是这一行：
#   旧写法 `mkdir "$LOCK" || exit 0` 有个致命缺陷 —— 脚本里【没有任何地方 rmdir 这个锁】，
#   于是一次 kill -9（OOM、手滑、procd 强杀）就把锁永久留在那儿 ⇒ 这个守护【再也起不来】，
#   而且一声不响。音箱因此哑了几个小时才被查出来（muxguard 死 ⇒ 没人扳 HP_L Mux ⇒ 全哑）。
#   现在：mkdir 保原子性，锁里放 pid，并用 /proc/<pid>/cmdline 的【最后一个字段】
#   核实那个 pid 是否真的是本脚本 ⇒ 陈旧锁自动抢过来。宁可重复跑，也绝不再"永久瘫痪"。
for _t in 1 2 3; do
	mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
	_op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -n "$_op" ] && [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
		exit 0                      # 真有一个同伙在跑
	fi
	rm -rf "$LOCK"                  # 陈旧锁：上一个我被 kill -9 了，没来得及清
done

log() {
	# 日志在 NAND 上，别写爆：攒到 64KB 翻一页。正常情况下这文件一天都不长一行。
	[ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

# 等 codec 注册出来（开机早期 card0 可能还没有）。
i=0
while [ $i -lt 60 ]; do
	amixer -c 0 cget name='HP_L Mux' >/dev/null 2>&1 && break
	sleep 2
	i=$((i+1))
done

# ★ 只能取【最后一行】的 values —— cget 的第二行是控件计数 `values=1`，
#   拿它当值就会永远判成"错的"，于是每 30 秒无脑 cset 一次。
mux_now() {
	amixer -c 0 cget name='HP_L Mux' 2>/dev/null | tail -1 | sed 's/.*values=//'
}

v=$(mux_now)
if [ "$v" = "0" ]; then
	log "启动：HP_L Mux 已是 DACL HPL Switch，正常"
else
	amixer -c 0 cset name='HP_L Mux' 'DACL HPL Switch' >/dev/null 2>&1
	log "★ 启动：HP_L Mux 曾是 [$v]（空模拟混音器 ⇒ 全哑），已扳回 DACL HPL Switch"
fi

while true; do
	sleep 30
	v=$(mux_now)
	if [ "$v" != "0" ]; then
		amixer -c 0 cset name='HP_L Mux' 'DACL HPL Switch' >/dev/null 2>&1
		log "★ 被改成 [$v] 了，已扳回 DACL HPL Switch"
	fi
done
