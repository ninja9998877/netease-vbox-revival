#!/bin/bash
# capture_spk.sh —— 音箱【长时间】全量取证（等一个人出现，或者等到截止时刻）
#
# 跟 v1 的区别（v1 只支持"录 N 秒"，而且 devlog 那一路是废的）：
#   ① 支持 `--until HH:MM`（绝对截止时刻）。我们要的是"等到人出现"，不是一个固定时长。
#   ② ★★ 麦克风改成【分段录】（默认 10 分钟一段）。v1 是单文件录 2 小时，
#      一旦进程崩了/磁盘满了，整个文件连 WAV 头都补不回来，两小时白录。
#      分段之后，崩一段只丢那 10 分钟，而且每段都是完整可读的 wav。
#   ③ ★★ devlog 改成【每 5 分钟 dump 一次 logread】。这台 busybox 没有
#      `logread -f`（实测 unrecognized option），而环形缓冲只有 806 行 ≈ 55 分钟 ——
#      录 2 小时的话开头那一个小时会被冲掉。只有定期 dump 才留得住。
#   ④ 磁盘保护：剩余不足 2GB 就提前收工（别把根分区写满，会连累本机别的服务）。
#
# 四路，时间轴靠 epoch 对齐：
#   ① mic_000.wav, mic_001.wav …  设备【原始麦克风】——★最值钱的一路：音箱自己的
#        喇叭也会被它听见，所以"叮"、网易的回答、人说的话，全在同一条音轨里。
#   ② dbus.txt    设备 DBus 全量（API / GOAPI / Notify）——唤醒与状态机的真身
#   ③ devlog.txt  设备系统日志（每 5 分钟 dump 一次；分析时要按 dump 段去重）
#   ④ *.delta     三个常驻服务的日志增量（起止字节数之差，不重不漏）
#
# 用法:
#   ./capture_spk.sh --until 12:00     ← 录到中午 12 点
#   ./capture_spk.sh --secs 600        ← 录 10 分钟
#   ./capture_spk.sh 600               ← 同上（兼容 v1 的写法）
set -u

CHUNK=${SPK_CHUNK:-600}             # 每段麦克风多少秒
DUMP_EVERY=${SPK_DUMP_EVERY:-300}   # devlog 多久 dump 一次

# ★ 你的部署环境可能不同：export SPK_HOST=192.168.1.100（跑"大脑"那台机在你局域网里的地址）
SRV=${SPK_HOST:-192.168.1.100}
# ★ 仓库根 = 本脚本所在目录（src/）的上一级
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# ★ 数据目录（capture/ 和 log/ 都落这儿）：export SPK_DATA_DIR=/your/data
DATA=${SPK_DATA_DIR:-$ROOT}
BASE=$DATA/capture

DUR=""; DEADLINE=""
while [ $# -gt 0 ]; do
	case "$1" in
		--until)
			[ $# -lt 2 ] && { echo "✗ --until 要跟一个时刻，如 12:00"; exit 2; }
			DEADLINE=$(date -d "today $2" +%s 2>/dev/null) || { echo "✗ 认不出这个时刻: $2"; exit 2; }
			shift 2 ;;
		--secs) DUR=$2; shift 2 ;;
		''|*[!0-9]*) echo "未知参数: $1（用法见脚本头）"; exit 2 ;;
		*) DUR=$1; shift ;;
	esac
done

NOW=$(date +%s)
[ -z "$DUR" ] && [ -z "$DEADLINE" ] && DUR=600
if [ -n "$DEADLINE" ]; then
	[ "$DEADLINE" -le "$NOW" ] && { echo "✗ $(date -d @$DEADLINE +%H:%M) 已经过去了（现在是 $(date +%H:%M)）"; exit 2; }
	TOT=$((DEADLINE - NOW))
else
	TOT=$DUR
	DEADLINE=$((NOW + DUR))
fi

OUT="$BASE/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
echo "$OUT" > /tmp/spk_capture_dir

ADB=$(command -v adb 2>/dev/null || true)
[ -z "$ADB" ] && [ -x "$HOME/platform-tools/adb" ] && ADB="$HOME/platform-tools/adb"
[ -z "$ADB" ] && { echo "✗ 找不到 adb"; exit 1; }

say() { printf '%s\n' "$*"; }

# ---- 前置：本机 IP 必须是 $SRV，否则 mictap 的流根本到不了这儿 ----
IP=$(ip -4 addr show 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1)
say "① 本机 LAN IP = ${IP:-无}"
if ! ip -4 addr show 2>/dev/null | awk '{print $2}' | cut -d/ -f1 | grep -qxF "$SRV"; then
	say "   ⚠️ 本机没有 $SRV 这个地址 —— mictap 默认送 $SRV，流可能收不到。继续录，但记下这一条。"
fi
$ADB get-state >/dev/null 2>&1 || { say "✗ 设备不可达"; exit 1; }
say "   设备可达"

# ---- 日志基线（结束后算增量）----
for f in voice macmini spk_srv; do
	L="$DATA/log/$f.log"
	if [ -f "$L" ]; then wc -c < "$L" > "$OUT/$f.off"; else echo 0 > "$OUT/$f.off"; fi
done
date +%s > "$OUT/start.epoch"

# ---- 预计体积（先说清楚，免得回头怪磁盘）----
say "② 输出目录 $OUT"
say "   窗口 $((TOT / 60)) 分钟（到 $(date -d @$DEADLINE +%H:%M:%S)）"
say "   麦克风按 63 KB/s 估，最多约 $((TOT * 63 / 1024)) MB；当前可用 $(( $(df -Pk "$DATA" | awk 'NR==2{print $4}') / 1024 )) MB"
say "③ 开录 —— ★ 从现在起，谁去跟音箱说话都会被完整录下来，不用做任何准备"

# ---- ② DBus 全量：一条长连接录到底（纯文本，2 小时也就几 MB）----
timeout $((TOT + 120)) $ADB shell '. /tmp/dbus_env.sh 2>/dev/null; exec dbus-monitor' \
	> "$OUT/dbus.txt" 2>&1 &
P_DBUS=$!

# ---- ③ 设备系统日志：定期 dump（环形缓冲只有 ~55 分钟，必须定期捞）----
(
	while [ "$(date +%s)" -lt "$DEADLINE" ]; do
		printf '\n===== dump %s =====\n' "$(date '+%F %T')" >> "$OUT/devlog.txt"
		$ADB shell 'logread' >> "$OUT/devlog.txt" 2>&1
		sleep "$DUMP_EVERY"
	done
) &
P_LOG=$!

# ---- ① 麦克风：分段循环 ----
i=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
	LEFT=$((DEADLINE - $(date +%s)))
	SEG=$CHUNK; [ "$SEG" -gt "$LEFT" ] && SEG=$LEFT
	F=$(printf 'mic_%03d.wav' "$i")
	# ★ --secs 是从「收到第一个包」才起算的 ⇒ 一个包都没有就永不退出，所以套 timeout 兜底
	timeout $((SEG + 45)) python3 "$ROOT/src/mictap_sink.py" \
		--wav "$OUT/$F" --secs "$SEG" >> "$OUT/mic.log" 2>&1
	i=$((i + 1))
	AVAIL=$(df -Pk "$DATA" 2>/dev/null | awk 'NR==2{print $4}')
	if [ -n "$AVAIL" ] && [ "$AVAIL" -lt 2000000 ]; then
		say "   ⚠ 磁盘只剩 $((AVAIL / 1024))MB，提前收工（不把根分区写满）"
		break
	fi
done

date +%s > "$OUT/end.epoch"
kill $P_DBUS $P_LOG 2>/dev/null
wait 2>/dev/null

# ---- 结束：算日志增量，这次活动窗口里到底写了什么 ----
say "④ 结束，汇总（共 $i 段麦克风）"
for f in voice macmini spk_srv; do
	L="$DATA/log/$f.log"
	O=$(cat "$OUT/$f.off" 2>/dev/null || echo 0)
	N=$(wc -c < "$L" 2>/dev/null || echo 0)
	tail -c +$((O + 1)) "$L" > "$OUT/$f.delta" 2>/dev/null
	say "   $f.log  $O → $N  增量 $((N - O)) 字节"
done
say "   mic      $(du -sh "$OUT" 2>/dev/null | cut -f1) （$(ls "$OUT"/mic_*.wav 2>/dev/null | wc -l) 段）"
say "   dbus.txt $(wc -l < "$OUT/dbus.txt" 2>/dev/null) 行"
say "   devlog   $(wc -l < "$OUT/devlog.txt" 2>/dev/null) 行（含重复 dump，分析时去重）"
say "完成：$OUT"
say "下一步：python3 $ROOT/src/analyze_capture.py $OUT"
