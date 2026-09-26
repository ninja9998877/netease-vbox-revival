#!/bin/sh
# fix_mux_source.sh —— 从【源头】修掉"每次开机必哑"。
#
# 【真凶】2026-09-21 定案：不是驱动默认值，是网易自己的开机脚本写死的。
#   /etc/init.d/{voice_service,netease_player_service,netease_services} 三个文件里
#   各有一份一模一样的 enable_soundcard()，其中都有一行：
#         amixer cset iface=MIXER,name='HP_L Mux' 1
#   三个服务都是 S 开头（S109netease_voice / S110player / S110voice / S120netease_services），
#   开机各跑一遍，最后一个跑完 mux 就是 1 ⇒ 左声道接进一条空路 ⇒ 整机哑。
#   它们跑在 S95spkbrain（我们）【之后】，所以我们的守卫永远被覆盖。
#   这也解释了 09-20 那次"运行时翻转"：音频服务被 procd 重启一次，函数就又跑一遍。
#
# 【为什么断定这一行是网易的 bug，而不是设计】
#   ① 函数第一句注释是 echo "set r16 audio pass through" —— 意图明写是"直通"；
#      而 'Left Analog Mixer HPL Switch' 恰恰【不是直通】。自相矛盾。
#   ② 同一段里右边选的是 HP_R Mux 0 = 'DACR HPR Switch'（直连 DAC，正确）。
#      左右不对称 —— 同一个意图不可能一边直连一边绕模拟路。
#   ③ 查遍整张控件表，含 "Analog" 的控件【一个都没有】：
#      'Left Analog Mixer HPL Switch' 所指的那条路上，没有任何"输入源"控件可开
#      ⇒ 在这台设备上它是一条信号无来源的死路，选了必然哑。
#   ⇒ 改它是【修 bug】，不是改原厂设计。
#
# 【枚举表】HP_L Mux: 0='DACL HPL Switch'(直连DAC左,正确)  1='Left Analog Mixer HPL Switch'(绕死路)
#           HP_R Mux: 0='DACR HPR Switch'(直连DAC右,正确)  1='Right Analog Mixer HPR Switch'
#
# 【落点】rc.d 下全是符号链接（K110voice_service -> ../init.d/voice_service），
#   所以只改 init.d 即可。改动 copy-up 到 /overlay（rootfs_data, ext4, rw,sync）⇒ 重启不丢。
#
# 【还原】/mnt/UDISK/spk/orig_init/*.orig 是改前原件的副本（建时已 md5 校验一致）。
#   跑 restore_orig_init.sh 一键还原。
#
# ★ 本脚本【不出声】、【不重启】、【不动任何其他行】：只替换三个文件里各一处字符串，
#   然后做语法自检 + 逐项回报。改完对【当前这一刻】无影响，只在下次这些服务启动时生效。

set -u
B=/mnt/UDISK/spk/orig_init
TS=$(date '+%Y%m%d-%H%M%S')
FILES="voice_service netease_player_service netease_services"

echo "==================== 改前 ===================="
for f in $FILES; do
	printf '%-26s : ' "$f"
	grep -n "name='HP_L Mux'" "/etc/init.d/$f" || echo "（没找到这一行？！）"
done

# 再存一份带时间戳的改前快照（orig 是出厂原件，snap 是本次改前的样子，双保险）
mkdir -p "$B/snap-$TS"
for f in $FILES; do
	cp -a "/etc/init.d/$f" "$B/snap-$TS/$f"
done
echo "改前快照 -> $B/snap-$TS/"

echo
echo "==================== 动手 ===================="
for f in $FILES; do
	file="/etc/init.d/$f"
	n=$(grep -c "name='HP_L Mux' 1" "$file")
	if [ "$n" != "1" ]; then
		# ★ 安全闸：只改【恰好出现一次】的文件。0 次（改过了/没有）、多次（结构意外）
		#   一律不动 —— 宁可这次没改到，也绝不改坏原厂脚本。
		echo "  !! $f 里 \"name='HP_L Mux' 1\" 出现 $n 次（期望 1 次）—— 跳过不动"
		continue
	fi
	sed -i "s/name='HP_L Mux' 1/name='HP_L Mux' 0/" "$file"
	echo "  ✓ $f 已改（1 -> 0）"
done

echo
echo "==================== 改后 ===================="
for f in $FILES; do
	printf '%-26s : ' "$f"
	grep -n "name='HP_L Mux'" "/etc/init.d/$f"
done

echo
echo "=== 语法自检 sh -n ==="
for f in $FILES; do
	if sh -n "/etc/init.d/$f" 2>/dev/null; then
		echo "  ✓ $f 语法 OK"
	else
		echo "  ✗✗ $f 语法坏了！立刻跑 restore_orig_init.sh 还原！"
	fi
done

echo
echo "=== 确认 HP_R Mux 那一行【没有】被动过（应仍是 0）==="
for f in $FILES; do
	printf '%-26s : ' "$f"
	grep -n "name='HP_R Mux'" "/etc/init.d/$f"
done

echo
echo "=== 权限与大小（应仍是 -rwxr-xr-x，大小不变）==="
ls -l /etc/init.d/voice_service /etc/init.d/netease_player_service /etc/init.d/netease_services

echo
echo "=== 确认已落到 overlay（持久层，不是内存）==="
ls -l /overlay/etc/init.d/voice_service /overlay/etc/init.d/netease_player_service /overlay/etc/init.d/netease_services 2>&1

echo
echo "★ 改动只在【下次这些服务启动时】生效；当前这一刻的 mux 值是："
amixer -c 0 cget name='HP_L Mux' 2>/dev/null | sed -n 's/^ *: values=*/  现在 = /p'
echo "  要还原：sh /mnt/UDISK/spk/restore_orig_init.sh"
