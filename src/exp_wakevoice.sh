#!/bin/bash
# exp_wakevoice.sh —— ★ 决定性实验：唤醒应答能不能换成"说话"
#
# 假设：control_center 每次唤醒都查一次 skins.db（日志 `Get skin key: v303
#       coast time: 2.079875ms` = 2ms 的真实查询），所以改一行 SQL 就能换掉
#       唤醒时播的那个音，不用重启任何服务、不用动任何 mp3 文件。
#
# 做法（全程原子，跑完必还原）：
#   ① 双备份（设备上留 .bak，本机留 .orig）
#   ② 把 res.ID=12（= v303 指向的那条）的 localPath 改成 /rom 里一句现成人声
#      "Hi 早上好，我在了"，md5Chksum 同步改成那个文件的真 md5
#      —— 用 /rom 里已存在的文件，是为了把"机制对不对"和"文件能不能读"分开：
#         万一没反应，就能确定问题在机制，而不是我们放的新文件。
#   ③ ★ push 用 `cat > 原文件` 而不是覆盖 —— 保 inode。
#      control_center 可能在启动时就打开了 skins.db 并一直持有句柄，
#      换 inode 的话它读的还是旧数据（这条坑在台账 /etc/p60-hosts 上踩过）。
#   ④ 播一次唤醒词「嘀嗒嘀嗒」，等 10 秒
#   ⑤ ★ 无论成败，立刻从设备上的 .bak 还原，并校验 md5 回到原值
#
# 用法: ./exp_wakevoice.sh            （探针=Ashiyong002「Hi 早上好，我在了」）
#       ./exp_wakevoice.sh /path/x.mp3 （用自己的文件当探针）
set -u

ADB=$(command -v adb 2>/dev/null || echo "$HOME/platform-tools/adb")
# ★ 仓库根 = 本脚本所在目录（src/）的上一级
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TS=$(date +%Y%m%d-%H%M%S)
PROBE_SRC="${1:-/rom/usr/share/resource/voice/Ashiyong002.mp3}"
ORIG_MD5='7f37c535bed5fe7827b21d314eeb4d73'   # v303 原始文件的 md5（= skins.db 里登记的）
WORK=/tmp/wv_exp
mkdir -p "$WORK"
say() { printf '%s\n' "$*"; }

say "① 备份"
$ADB shell "cp -a /mnt/UDISK/skins.db /mnt/UDISK/skins.db.bak-$TS" || { say "✗ 设备备份失败，中止"; exit 1; }
$ADB pull /mnt/UDISK/skins.db "$WORK/skins.orig.db" >/dev/null 2>&1 || { say "✗ 拉取失败，中止"; exit 1; }
BEFORE=$($ADB shell "md5sum /mnt/UDISK/skins.db" | awk '{print $1}')
say "   设备备份 /mnt/UDISK/skins.db.bak-$TS   当前 md5=$BEFORE"

say "② 取探针文件 + 算 md5"
$ADB pull "$PROBE_SRC" "$WORK/probe.mp3" >/dev/null 2>&1 || { say "✗ 探针文件拉不到: $PROBE_SRC"; exit 1; }
PMD5=$(md5sum "$WORK/probe.mp3" | awk '{print $1}')
say "   探针 $PROBE_SRC"
say "   md5  $PMD5"

say "③ 改 SQL（只动 res.ID=12 这一行）"
python3 - "$WORK" "$PROBE_SRC" "$PMD5" <<'PY'
import sqlite3, shutil, sys, os
work, path, md5 = sys.argv[1], sys.argv[2], sys.argv[3]
src = os.path.join(work, 'skins.orig.db')
dst = os.path.join(work, 'skins.new.db')
shutil.copy(src, dst)
c = sqlite3.connect(dst)
# 先确认没有别的 keyName 也指向 resID=12（有的话这个实验会波及它们）
n = c.execute("select count(*) from skin where resID=12").fetchone()[0]
keys = [r[0] for r in c.execute("select keyName from skin where resID=12")]
print('   resID=12 被这些 keyName 引用: %s  (%d 个)' % (keys, n))
if n != 1:
    print('   ✗ 不止一个引用，为免误伤，中止')
    sys.exit(9)
c.execute("update res set localPath=?, md5Chksum=? where ID=12", (path, md5))
c.commit()
row = c.execute("select localPath, md5Chksum from res where ID=12").fetchone()
print('   改后 res.ID=12 → %s' % row[0])
c.close()
PY
[ $? -ne 0 ] && { say "✗ SQL 阶段失败，中止（设备未改动）"; exit 1; }

say "④ 写回设备（★ cat 保 inode，不覆盖）"
$ADB push "$WORK/skins.new.db" /tmp/skins.new.db >/dev/null 2>&1 || { say "✗ push 失败"; exit 1; }
$ADB shell 'cat /tmp/skins.new.db > /mnt/UDISK/skins.db; rm -f /tmp/skins.new.db'
AFTER=$($ADB shell "md5sum /mnt/UDISK/skins.db" | awk '{print $1}')
say "   写入后设备 md5=$AFTER （应等于本机 new.db 的 md5）"
md5sum "$WORK/skins.new.db" | awk '{print "   本机 new.db  md5="$1}'
if [ "$AFTER" != "$(md5sum "$WORK/skins.new.db" | awk '{print $1}')" ]; then
	say "   ✗ 写入后 md5 不符 —— 可能没写进去，立即还原"
	$ADB shell "cat /mnt/UDISK/skins.db.bak-$TS > /mnt/UDISK/skins.db"
	exit 1
fi
say "   ✓ 写入成功，inode 保持不变（cat 重定向）"
$ADB shell 'ls -i /mnt/UDISK/skins.db /mnt/UDISK/skins.db.bak-'"$TS" 2>/dev/null

say "⑤ 唤醒一次（播「嘀嗒嘀嗒」+ 10 秒静音）"
EDGE=${EDGE_TTS:-$HOME/.local/bin/edge-tts}
if [ ! -f "$WORK/wake_seq.wav" ]; then
	"$EDGE" --voice zh-CN-liaoning-XiaobeiNeural --text '嘀嗒嘀嗒' --write-media "$WORK/w.mp3" >/dev/null 2>&1
	ffmpeg -y -v error -i "$WORK/w.mp3" -af 'apad=pad_dur=10' -ar 48000 -ac 1 "$WORK/wake_seq.wav"
fi
T0=$(date +%s.%N)
say "   播放开始 $(date '+%H:%M:%S.%3N')"
aplay -q "$WORK/wake_seq.wav" 2>/dev/null
say "   播放结束 $(date '+%H:%M:%S.%3N')，等 4 秒让它反应"
sleep 4

say "⑥ ★ 还原（无论成败）"
$ADB shell "cat /mnt/UDISK/skins.db.bak-$TS > /mnt/UDISK/skins.db"
BACK=$($ADB shell "md5sum /mnt/UDISK/skins.db" | awk '{print $1}')
if [ "$BACK" = "$BEFORE" ]; then
	say "   ✓ 已还原，md5 回到 $BEFORE"
else
	say "   ✗✗ 还原后 md5=$BACK ≠ $BEFORE —— 需要用 .bak 再还原一次！"
	$ADB shell "cp -a /mnt/UDISK/skins.db.bak-$TS /mnt/UDISK/skins.db; md5sum /mnt/UDISK/skins.db"
fi

echo "$T0" > "$WORK/exp_t0.txt"
say "⑦ 完成。唤醒时刻 epoch=$T0 ($(date -d @${T0%.*} '+%H:%M:%S'))"
say "   下一步分析麦克风： python3 $ROOT/src/analyze_capture.py"
