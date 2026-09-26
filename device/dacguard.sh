#!/bin/sh
# 盯住 DAC volume —— "播放器明明在播、喇叭一点声没有"的【第二个】真凶。
#
# 实测（2026-09-22）：
#   `DAC volume`(numid=19) 是【所有路共同的增益底】，位置在 headphone volume 之前。
#   刻度 min=0 max=255、dBscale-min=-119.25dB、step=0.75dB ⇒ **159≈0dB（满刻度）**。
#   它掉到 0 = -119.25dB = **全哑**：状态机好、0x601 推得出去、设备也真来取流了、
#   功放静音乖乖解开 —— 就是一点声没有。
#   同一时刻的对照：ADC volume=160、headphone volume=59 全是好的，**只有这一级是 0**。
#   ⇒ 极难定位：每一个"看起来该有声音"的环节都是对的。
#
# ★★★ 别跟另外两个控件搞混（名字像，命运完全不同，这条踩过）：
#     `DAC volume`(numid=19)   ← 本脚本盯的这个，真杠杆，dBscale 准，0 就是全哑
#     `digital volume`(numid=21) / `DAC mixer gain`(numid=20)
#       —— 这两个是当年误判成真凶的假线索，值是 0 也【正常】，别去动它们。
#
# 为什么要常驻：
#   1. amixer 的改动【只在内存里，重启就丢】—— 这台音箱会自己重启（网络自愈/OTA）。
#   2. ★★ 跟 HP_L Mux 是同一个"崩了就永久哑"的模式：归零之后【没有任何一层会去开它】。
#      2026-09-21 夜那次"三层防线一致认为就该哑着"的闭锁，就是这类故障的标本。
#   3. 归零多半发生在"音频链路初始化做了一半"的时候 —— 14:18:46 那次网易自己的 TTS
#      撞车（0x700 stop + 0x701 play），同一个事件【同时】造成 DAC volume 归零
#      **和** ihwplayer 卡死。⇒ 那会儿主人已经在叫音箱了。
#      所以本守护比 muxguard 快：**5 秒一轮**，不是 30 秒
#      （开销可以忽略：一次本地 ioctl；muxtrace 是 0.15 秒一轮，比这重得多）。
#
# ★★★ 夜间静音【不会】被本守护破坏，而且本守护【必须】在夜里照常工作：
#   夜间禁声的硬闸门是 HP/PO 输出开关（-40.3dB，nightmute.sh 管），**不是 DAC volume**。
#   DAC volume 保持 150 而开关关着 = 仍然是真静音，两者互不干扰。
#   ⇒ 反过来说：**要是夜里归零了而我们不修，天亮 nightmute 放行之后音箱【还是哑的】**，
#     而且从现象上完全看不出来（"夜里静音"和"彻底哑了"长得一模一样，
#     09-21 夜就是这么被瞒过去的）。
#   ⇒ 所以这里【故意不加】任何 night 判断。★ 别给后来的人改成"夜里不修"。
#
#   ★ 三个守护的分工，别搞混：
#     muxguard    保"左声道接在 DAC 上"      —— 路走错了 ⇒ 全哑
#     本脚本      保"这条路的增益底不是 0"   —— 路上没信号 ⇒ 全哑
#     nightmute   管"夜里不出声"（压 HP/PO 开关，不碰 DAC volume）
LOG=/mnt/UDISK/spk/dacguard.log
LOCK=/tmp/spk/dacguard.lock
TARGET=150		# -6.75dB，历史基准。窄带法实测过这一级是真杠杆
			# （150→130 = -14.0dB、150→110 = -27.3dB，与 dBscale 预测吻合）

mkdir -p /tmp/spk
# ★★★ 单实例锁 —— 必须【崩溃安全】，照抄 muxguard.sh。
#   血案：旧写法 `mkdir "$LOCK" || exit 0` 不在任何地方 rmdir，一次 kill -9
#   （OOM、手滑、procd 强杀）就把锁永久留下 ⇒ 守护【再也起不来】，而且一声不响。
#   现在：mkdir 保原子性，锁里放 pid，用 /proc/<pid>/cmdline 的【最后一个字段】
#   核实那个 pid 真的是本脚本 ⇒ 陈旧锁自动抢过来。宁可重复跑，也绝不再"永久瘫痪"。
for _t in 1 2 3; do
	mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; break; }
	_op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -n "$_op" ] && [ "$( { tr '\0' '\n' < "/proc/$_op/cmdline"; } 2>/dev/null | tail -1 )" = "$0" ]; then
		exit 0			# 真有一个同伙在跑
	fi
	rm -rf "$LOCK"			# 陈旧锁：上一个我被 kill -9 了，没来得及清
done

log() {
	# 日志在 NAND 上，别写爆：攒到 64KB 翻一页。正常情况下这文件一天都不长一行。
	[ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && mv "$LOG" "$LOG.1"
	echo "$(date '+%m-%d %H:%M:%S') $1" >> "$LOG"
}

# ★★★ 只能认 `  : values=` 那一行 —— 【绝不能 tail -1】。
#   cget 出来是四行：控件头 / 类型行 / `  : values=150,150` / `  | dBscale-min=...`。
#   最后一行是 dBscale（-119.25dB），拿它当值 ⇒ 永远判成"错的" ⇒ 每轮无脑 cset。
#   （设备上实测过：`tail -1` 抓到的是 `  | dBscale-min=-119.25dB,step=0.75dB,mute=0`。
#     同一个坑在 nightmute.sh 那边叫"amixer cget 必须认 `  : values=` 行"，
#     当时的教训是 headphone volume 上踩的 —— 两个文件同一条铁律，别在任一处图省事。）
dac_now() {
	amixer -c 0 cget name='DAC volume' 2>/dev/null | sed -n 's/^ *: values=//p'
}

# 等 codec 注册出来（开机早期 card0 可能还没有）。
i=0
while [ $i -lt 60 ]; do
	amixer -c 0 cget name='DAC volume' >/dev/null 2>&1 && break
	sleep 2
	i=$((i+1))
done

# 扳回目标值。
#
# ★ 判据是【只认 TARGET,TARGET 这一个正确值】，其余一律扳回：
#   谁可能设成别的值？我们只手动设过 150；网易的 enable_soundcard() 设的是
#   headphone volume（59），没见它碰这一级。所以任何偏离都是坏状态。
#   ★ 日志里把"全哑"和"偏离"分开标 —— 全哑那类才是查过好几小时的故障，
#     而"偏离"（比如被设成 130）是另一种坏法，也要能一眼认出来。
#
# ★★ 日志报的是【结果】不是【意图】—— 先 cset 再读回复核，读回对了才说"已扳回"。
#   "说成功了"必须真的是成功了，否则这个守护自己就成了假消息来源（这个项目栽过）。
#
# ★★ 「扳不动」必须【节流】：真扳不动只有两种可能 —— ① 有东西在跟我们抢
#   （那正是要找的真凶）② 我们的 cset 在设备上根本不生效。
#   两种都会【每 5 秒失败一次】，不节流就是一分钟 12 行、把 NAND 刷爆。
#   ⇒ 每 12 次（≈1 分钟）记一笔，带次数 ⇒ 既看得出"持续了多久"，也不刷屏。
#   ★ 但【绝不停手】：重试照旧每 5 秒一次，因为抢的那一方可能只是暂时在动。
nofix=0
fix() {					# $1 = 读到的原值，原样写进日志
	amixer -c 0 cset name='DAC volume' "$TARGET,$TARGET" >/dev/null 2>&1
	# ★ 复核：cset 是同步的（ioctl 返回即生效），所以当场读回就是准的。
	v2=$(dac_now)
	if [ "$v2" = "$TARGET,$TARGET" ]; then
		if [ "$1" = "0,0" ] || [ "$1" = "0" ]; then
			log "★★ 全哑（DAC volume=[$1] ⇒ -119.25dB）⇒ 已扳回 $TARGET"
		else
			log "★ 偏离（DAC volume=[$1]）⇒ 已扳回 $TARGET"
		fi
		nofix=0
		return 0
	fi
	nofix=$((nofix+1))
	# ★ 第 1 次立刻记（新情况要马上知道），之后每分钟一笔
	[ $((nofix % 12)) -eq 1 ] && \
		log "★★ 扳不动：读到 [$1]，cset 后仍 [$v2]（第 $nofix 次；有东西在跟我们抢？）"
	return 1
}

v=$(dac_now)
if [ -z "$v" ]; then
	log "启动：读不到 DAC volume（codec 还没出来？）"
elif [ "$v" = "$TARGET,$TARGET" ]; then
	log "启动：DAC volume 已是 $TARGET，正常"
else
	fix "$v"
fi

# ★★ 循环里【读不到 ≠ 坏】—— 空值是"我还看不见"，不是"它坏了"。
#   amixer 偶发失败、codec 正在重配时会读出空串；这时候瞎写比什么都不做更危险
#   （可能跟正在初始化的那条链打架）。所以空值只记一笔（节流到 5 分钟一次）就跳过。
blank=0
while true; do
	sleep 5
	v=$(dac_now)
	if [ "$v" = "$TARGET,$TARGET" ]; then
		blank=0
		continue
	fi
	if [ -z "$v" ]; then
		blank=$((blank+1))
		[ $((blank % 60)) -eq 1 ] && log "读不到 DAC volume（连续 $blank 轮）"
		continue
	fi
	blank=0
	fix "$v"
done
