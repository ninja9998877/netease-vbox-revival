#!/bin/sh
# voicemute.sh —— 把设备【自己的声音】全部顶成静音（幂等，可以反复跑）
#
# ★ 目标（2026-09-21 主人拍板）：把这台音箱从网易那套里拿回来，只让我们的 spk-ear 说话。
#   固件一个字节不动；`umount` 秒级还原。
#
# ★★ 被两处调用，这正是它能"及时"生效的原因：
#   ① run.sh      —— 开机 S95，【早于】netease_voice_service(S109)
#                    ⇒ 开机那几声，在它们有机会响之前就已经被盖住了
#   ② spkcheck.sh —— 每分钟自愈（mount 不持久，重启即失效）
#
# ★ 为什么必须做：设备【自己会误唤醒】（2026-09-21 晚 20:16/20:29/20:30 自醒了三次），
#   每次都要嚷一句"网络异常" —— 半夜这就是会把人吵醒的那一声。
#   （`nightmute.sh` 压的 headphone volume 在链路末端，实测挡不住它，是纸闸门。）
#
# ★ 覆盖范围怎么定的：拿 ihwplayer 自己的日志（/tmp/ihwplayer_721.log 里它开过哪些 mp3）数的，
#   不是猜的：
#     /rom/usr/share/resource/voice/   51 次   固件提示音，69 条，只读 squashfs
#     /mnt/UDISK/resources/voice/      13 次   ★网易下载缓存的 TTS —— 能说任意句子，最危险
#     //192.168.1.100:8899/spk_ai.mp3   12 次   ← 这条【是我们自己说话的路】，千万别碰
#
# ★ 两处分别处理：
#   /rom  —— 【目录级】一条 mount 盖住整个目录。以后固件里再多几条也自动哑，
#            不用回来改名单。
#   UDISK —— 【逐条】盖。因为同一个目录里躺着我们自己的 spk_ack.mp3 / spk_ack_pool，
#            目录级盖会把我们自己的应答音也一起弄哑。
#
# ★ 静音件是【按原时长逐条生成】的（ffmpeg anullsrc），不是拿一条盖所有 ——
#   播放流程若"等播完"再走下一步，时长不一致会把节奏带歪。

D=/mnt/UDISK/spk
L=$D/spkcheck.log
log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$L"; }

VROM="$D/silent/rom"      # 69 条固件提示音的静音替身（目录，整体盖）
VUD="$D/silent/udisk"     # 21 条网易缓存 TTS 的静音替身（逐条盖）
ROMVOICE=/rom/usr/share/resource/voice
UDVOICE=/mnt/UDISK/resources/voice

# ---- ① 固件提示音：目录级 ----
if [ -d "$VROM" ]; then
	if ! grep -q " $ROMVOICE " /proc/mounts; then
		mount --bind "$VROM" "$ROMVOICE" 2>/dev/null \
			&& log "★ 顶掉固件提示音【整个目录】（69 条全静音）"
	fi
else
	log "⚠ 静音目录缺失 $VROM ⇒ 固件提示音还是原声"
fi

# 兜底：目录级挂失败（/rom 路径变了？）时，至少把那三条最常响的单独盖上。
# 判据用 mount 是否真在里面，不用上一步的返回值 —— "命令成功"和"确实挂上了"不是一回事。
if ! grep -q " $ROMVOICE " /proc/mounts; then
	for n in S003 b-h-51 b-h-52; do
		src="$D/silent/$n.mp3"; dst="$ROMVOICE/$n.mp3"
		[ -f "$src" ] || { log "⚠ 静音件缺失 $src ⇒ $n 还是原声"; continue; }
		grep -q " $dst " /proc/mounts && continue
		mount --bind "$src" "$dst" 2>/dev/null && log "⚠ 目录级静音没挂上，退而顶掉 $n"
	done
fi

# ---- ② 网易缓存的 TTS：逐条 ----
# 同目录里 spk_ack.mp3 / spk_ack_pool 是【我们自己的】，绝不能碰。
for f in "$VUD"/2018*.mp3; do
	[ -f "$f" ] || continue
	n=$(basename "$f"); dst="$UDVOICE/$n"
	[ -f "$dst" ] || continue
	grep -q " $dst " /proc/mounts && continue
	mount --bind "$f" "$dst" 2>/dev/null && log "★ 顶掉缓存 TTS $n"
done
