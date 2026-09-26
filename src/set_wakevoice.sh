#!/bin/bash
# set_wakevoice.sh —— 把音箱的【唤醒应答】换成任意一句话
#
# ★ 原理（2026-09-21 实测确认，见 memory: netease-vbox-wake-voice）：
#   唤醒时 control_center 会查一次 skins.db：
#       Get skin key: v303  val: <res.localPath>   coast time: 2.4ms
#   然后把 val 交给 Tts 调度器播（playerId=13/15/16…）。
#   所以只要改 skins.db 里 v303 指向的那条 res 记录，下一次唤醒就是新声音 ——
#   不用重启任何服务、不用碰固件、原始 mp3 一个字节都不用动。
#
#   skin.v303 → resID=12 → res.ID=12 的 localPath 就是唤醒音。
#   （确认过 resID=12 只被 v303 一个 keyName 引用，改它不会波及别的音效。）
#
# ★ 两条实测得来的硬约束：
#   1. 写回必须 `cat 新文件 > 原文件`（保 inode）。设备可能持有句柄，
#      换 inode 它读的还是旧的。
#   2. 新音频用 44100Hz/2ch/mp3 最稳（对齐原唤醒音的格式），目标响度
#      I=-15 LUFS / TP=-1.0（用 loudnorm 两遍法；单遍 loudnorm 会给音频
#      加 padding，实测把 0.86s 的短句全撑成 1.907s）。
#
# 用法:
#   ./set_wakevoice.sh ding/ack/en.mp3    # 装上（自动备份、自动校验、可秒级还原）
#   ./set_wakevoice.sh --restore          # 还原成网易原版
#   ./set_wakevoice.sh --status           # 现在装的是哪一句
set -u

ADB=$(command -v adb 2>/dev/null || echo "$HOME/platform-tools/adb")
DEST=/mnt/UDISK/resources/voice/spk_ack.mp3      # ★ 自取的名字，不在网易清单里 ⇒ 云端不会覆盖
BAK=/mnt/UDISK/skins.db.spkvoice.bak             # 首次改动前的原始库，永久留着
ORIG_PATH='/mnt/UDISK/resources/voice/20180627164700196S0033DB.mp3'
ORIG_MD5='7f37c535bed5fe7827b21d314eeb4d73'
WORK=/tmp/wakevoice; mkdir -p "$WORK"
say() { printf '%s\n' "$*"; }

pull_db() { $ADB pull /mnt/UDISK/skins.db "$WORK/skins.db" >/dev/null 2>&1; }

case "${1:-}" in
--status)
	pull_db || { say "✗ 拉不到 skins.db"; exit 1; }
	python3 - "$WORK/skins.db" "$ORIG_PATH" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
lp, md5 = c.execute("select localPath, md5Chksum from res where ID=12").fetchone()
print('   v303 现在指向: %s' % lp)
print('   md5: %s' % md5)
print('   %s' % ('← 网易原版（叮）' if lp == sys.argv[2] else '★ 已替换'))
PY
	;;

--restore)
	if ! $ADB shell "[ -f $BAK ]" 2>/dev/null; then
		say "✗ 设备上没有原始备份 $BAK —— 没改过就不需要还原"; exit 1
	fi
	$ADB shell "cat $BAK > /mnt/UDISK/skins.db"
	NOW=$($ADB shell "md5sum /mnt/UDISK/skins.db" | awk '{print $1}')
	say "✓ 已还原，skins.db md5=$NOW"
	say "  再确认一次："; "$0" --status
	;;

--help|"")
	sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
	exit 0
	;;

*)
	SRC="$1"
	[ -f "$SRC" ] || { say "✗ 找不到文件: $SRC"; exit 1; }

	# ---- 0. 首次改动前，把原始库钉死在设备上（永久保留）----
	if ! $ADB shell "[ -f $BAK ]" 2>/dev/null; then
		$ADB shell "cp -a /mnt/UDISK/skins.db $BAK"
		say "① 首次改动 —— 已在设备上留下原始备份 $BAK"
	else
		say "① 原始备份已存在（$BAK）"
	fi

	# ---- 1. 检查音频格式（不合格就当场转，免得上去播不出来）----
	FMT=$(ffprobe -v error -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 "$SRC" 2>/dev/null | head -1)
	say "② 源音频 $SRC   [$FMT]"
	READY="$WORK/ready.mp3"
	if [ "$FMT" != "mp3,44100,2" ]; then
		say "   格式不是 mp3/44100/2ch，转一份"
		ffmpeg -y -v error -i "$SRC" -ar 44100 -ac 2 -b:a 256k "$READY" || { say "✗ 转码失败"; exit 1; }
	else
		cp "$SRC" "$READY"
	fi
	MD5=$(md5sum "$READY" | awk '{print $1}')
	DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$READY")
	say "   ${DUR}s  md5=$MD5"

	# ---- 2. 推上去 ----
	$ADB push "$READY" "$DEST" >/dev/null 2>&1 || { say "✗ push 失败"; exit 1; }
	UP=$($ADB shell "md5sum $DEST" | awk '{print $1}')
	if [ "$UP" != "$MD5" ]; then say "✗ 设备上 md5 不符（$UP ≠ $MD5）"; exit 1; fi
	say "③ 已推到 $DEST （md5 校验通过）"

	# ---- 3. 改库 ----
	pull_db || { say "✗ 拉不到 skins.db"; exit 1; }
	python3 - "$WORK/skins.db" "$DEST" "$MD5" <<'PY' || { say "✗ SQL 失败"; exit 1; }
import sqlite3, sys
db, path, md5 = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db)
keys = [r[0] for r in c.execute("select keyName from skin where resID=12")]
if keys != ['v303']:
    print('   ✗ resID=12 被 %s 引用，不止 v303，中止以免误伤别的音效' % keys); sys.exit(1)
old = c.execute("select localPath from res where ID=12").fetchone()[0]
c.execute("update res set localPath=?, md5Chksum=? where ID=12", (path, md5))
c.commit(); c.close()
print('   res.ID=12  %s\n            → %s' % (old, path))
PY

	# ★ push 到设备上的 /tmp，再用 cat 覆盖原库 —— 保 inode（设备可能持有句柄）
	$ADB push "$WORK/skins.db" /tmp/wakevoice_new.db >/dev/null 2>&1
	$ADB shell 'cat /tmp/wakevoice_new.db > /mnt/UDISK/skins.db; rm -f /tmp/wakevoice_new.db' 2>/dev/null
	NEW=$($ADB shell "md5sum /mnt/UDISK/skins.db" | awk '{print $1}')
	LOCAL=$(md5sum "$WORK/skins.db" | awk '{print $1}')
	[ "$NEW" = "$LOCAL" ] || { say "✗ 写回校验失败，马上还原"; "$0" --restore; exit 1; }
	say "④ 写回成功（cat 保 inode，md5=$NEW）"
	say "   备份还在: $BAK   ← 一条命令还原:  $0 --restore"
	say ""
	say "⑤ 现在去唤醒它试试（说「嘀嗒嘀嗒」）。"
	say "   ★ 提醒：唤醒音被设备的回声消除吃掉了，从麦克风【听不到】。"
	say "     要确认是否生效，看日志里这行的播放时长："
	say "     adb shell 'grep -a \"Get skin key\\|Tts schedule\" /tmp/netease_control_center_726.log | tail -4'"
	;;
esac
