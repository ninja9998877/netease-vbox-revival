#!/bin/sh
# deploy_mictap.sh —— 把本机编好的 mictap.so 部署到音箱（唯一部署入口）
#
# ★★★ 唯一部署目标 = 设备 rootfs 的 /lib/mictap.so。绝不是 UDISK。
#
#   为什么（2026-09-21 血泪，详见记忆 netease-vbox-boot-chain 坑②）：
#     netease_voice 起于【开机 5.60 秒】，而 UDISK 要【8.2 秒】才挂上。
#     那一刻 UDISK 上的 .so 还不存在 ⇒ musl 的 ldso 【静默忽略】LD_PRELOAD
#     （不报错、进程照常跑）⇒ 麦克风在跑但钩子根本不在，一个包都不发。
#     症状极难查：/proc/<pid>/environ 里 LD_PRELOAD 写得明明白白。
#     ⇒ 判据只有一个：grep mictap /proc/<pid>/maps（0 条 = 没加载，正常 3 条）。
#   rootfs（/lib）开机 0 秒就在，所以放那里。
#
# 用法:
#   ./deploy_mictap.sh                发布 device/mictap.so → 设备 /lib/mictap.so，核对 md5
#   ./deploy_mictap.sh --build        先交叉编译，再走上面那步
#   ./deploy_mictap.sh --restart      部署完重启 netease_voice_service（会先验前置条件）
#   ./deploy_mictap.sh --backup       顺带刷新 UDISK 的【只读备份】（不做则一个字节都不碰它）
#   ./deploy_mictap.sh --dry-run      只看现状与将要做什么，什么都不改
#
# ★ UDISK 上那份（/mnt/UDISK/spk/backup/）是【只读备份】，本脚本默认永不写它。
set -u

D=$(cd "$(dirname "$0")" && pwd)
SRC="$D/mictap.so"
TARGET=/lib/mictap.so
TARGET_V1=/lib/mictap.so.v1
BACKUP_DIR=/mnt/UDISK/spk/backup
V1_LOCAL="$D/mictap.so.v1-fallback"

DO_BUILD=0; DO_RESTART=0; DO_BACKUP=0; DRY=0
for a in "$@"; do
	case "$a" in
		--build)   DO_BUILD=1 ;;
		--restart) DO_RESTART=1 ;;
		--backup)  DO_BACKUP=1 ;;
		--dry-run) DRY=1 ;;
		*) echo "未知参数: $a"; exit 2 ;;
	esac
done

# 找 adb
ADB=$(command -v adb 2>/dev/null || true)
[ -z "$ADB" ] && [ -x "$HOME/platform-tools/adb" ] && ADB="$HOME/platform-tools/adb"
[ -z "$ADB" ] && { echo "✗ 找不到 adb"; exit 1; }

say() { printf '%s\n' "$*"; }
rule(){ say "------------------------------------------------------------"; }

# 在设备上按 comm 找进程（★ 绝不用 cmdline 做名字匹配：脚本自己的 cmdline 里
# 就含那些字面量，会匹配到自己的 adb shell —— 我为此拉过一次假警报）
dev_pid_of() {
	$ADB shell "for p in /proc/[0-9]*; do
		[ \"\$(cat \$p/comm 2>/dev/null)\" = '$1' ] && { basename \$p; break; }
	done" 2>/dev/null | tr -d '\r'
}
dev_md5() {
	$ADB shell "[ -f '$1' ] && md5sum '$1' | cut -d' ' -f1" 2>/dev/null | tr -d '\r'
}
# dbus-daemon --fork 的个数：先按 comm 卡住候选，再看 cmdline ⇒ 不会自匹配
dev_dbus_fork_count() {
	$ADB shell 'n=0
		for p in /proc/[0-9]*; do
			[ "$(cat $p/comm 2>/dev/null)" = "dbus-daemon" ] || continue
			case "$(cat $p/cmdline 2>/dev/null | tr "\0" " ")" in *--fork*) n=$((n+1));; esac
		done
		echo $n' 2>/dev/null | tr -d '\r'
}

if [ "$DO_BUILD" = 1 ]; then
	rule
	say "① 交叉编译"
	arm-linux-gnueabihf-gcc -O2 -fPIC -shared -nostdlib -lgcc -Wall -Wextra \
		-o "$SRC" "$D/mictap.c" || { say "✗ 编译失败"; exit 1; }
	say "   ✓ 产出 $(wc -c < "$SRC") 字节  md5=$(md5sum "$SRC" | cut -d' ' -f1)"
	# ★ 必须核实没有把 __aeabi_* 拉进来（ARMv7 软浮点除法辅助符号会污染符号表）
	nm=$(arm-linux-gnueabihf-nm -u "$SRC" 2>/dev/null | awk '{print $NF}' | grep '^__aeabi' || true)
	[ -n "$nm" ] && { say "   ✗ 符号表里混进了 __aeabi_*："; say "$nm"; exit 1; }
	say "   ✓ 未定义符号里没有 __aeabi_*"
fi

[ -f "$SRC" ] || { say "✗ 本机没有 $SRC（先跑 --build？）"; exit 1; }
SRC_MD5=$(md5sum "$SRC" | cut -d' ' -f1)

rule
say "② 部署前现状（先看清楚再动手）"
$ADB get-state >/dev/null 2>&1 || { say "✗ 设备不可达（adb get-state 失败）"; exit 1; }
CUR_MD5=$(dev_md5 "$TARGET")
V=$(dev_pid_of netease_voice)
say "   本机 $SRC"
say "        $(wc -c < "$SRC") 字节  md5=$SRC_MD5"
say "   设备 $TARGET"
say "        $( [ -n "$CUR_MD5" ] && echo "$CUR_MD5" )  $([ "$CUR_MD5" = "$SRC_MD5" ] && echo '（与本机一致，本次是空操作）' || echo '（与本机不同，本次会变）')"
if [ -n "$V" ]; then
	say "   麦克风钩子现状: pid=$V  maps 里 mictap $( $ADB shell "grep -c mictap /proc/$V/maps" 2>/dev/null | tr -d '\r' ) 条（3=正常，0=没加载）"
else
	say "   麦克风钩子现状: 找不到 netease_voice 进程（音箱在跑吗？）"
fi

if [ "$DRY" = 1 ]; then
	rule; say "（--dry-run，什么都没改）"; exit 0
fi
if [ "$CUR_MD5" = "$SRC_MD5" ]; then
	rule; say "✓ 已经是这份了，无需推送。"
	# ★ 别在这里无条件 exit —— 单用 --backup（或 --restart）时后面还有事要做
	if [ "$DO_RESTART" != 1 ] && [ "$DO_BACKUP" != 1 ]; then
		say "（要重启服务加 --restart；要刷新 UDISK 备份加 --backup）"; exit 0
	fi
else
	rule
	say "③ 推送到 $TARGET"
	$ADB push "$SRC" "$TARGET" >/dev/null 2>&1 || { say "✗ push 失败"; exit 1; }
	$ADB shell "chmod 755 $TARGET" >/dev/null 2>&1
	NEW_MD5=$(dev_md5 "$TARGET")
	[ "$NEW_MD5" = "$SRC_MD5" ] || { say "✗ 推送后 md5 不一致（设备 $NEW_MD5）—— 停手，别重启"; exit 1; }
	say "   ✓ 设备侧 md5=$NEW_MD5 与推送前 md5 一致（落盘完整）"
	say "   ★ 提示：本机那份是【唯一源】。UDISK 的只读备份要刷新请加 --backup。"
fi

if [ "$DO_BACKUP" = 1 ]; then
	rule
	say "④ 刷新 UDISK 只读备份（显式要求才做）"
	$ADB shell "mkdir -p $BACKUP_DIR
		cp $TARGET $BACKUP_DIR/mictap.so
		chmod 444 $BACKUP_DIR/mictap.so" >/dev/null 2>&1
	if [ -f "$V1_LOCAL" ]; then
		$ADB push "$V1_LOCAL" "$BACKUP_DIR/mictap.so.v1" >/dev/null 2>&1
		$ADB shell "chmod 444 $BACKUP_DIR/mictap.so.v1" >/dev/null 2>&1
	fi
	$ADB shell "ls -l $BACKUP_DIR" 2>/dev/null | sed 's/^/   /'
fi

if [ "$DO_RESTART" = 1 ]; then
	rule
	say "⑤ 重启 netease_voice_service"
	# ★★ 前置条件：init_dbus() 不满足时会 reboot -f 【硬重启音箱】。必须先验。
	DBUS_ENV=$( $ADB shell "[ -x /tmp/dbus_env.sh ] && echo ok" 2>/dev/null | tr -d '\r' )
	FORK=$(dev_dbus_fork_count)
	say "   /tmp/dbus_env.sh 可执行 : ${DBUS_ENV:-否}"
	say "   dbus-daemon --fork 个数 : ${FORK:-?}（必须正好 1）"
	if [ "$DBUS_ENV" != "ok" ] || [ "$FORK" != "1" ]; then
		say "   ✗★★ 前置条件不满足 —— 此刻 restart 会走 reboot -f 【硬重启音箱】。已中止。"
		exit 1
	fi
	say "   ✓ 前置条件满足，重启中…"
	$ADB shell "/etc/init.d/netease_voice_service restart" >/dev/null 2>&1
	sleep 6

	rule
	say "⑥ 重启后复验"
	V=$(dev_pid_of netease_voice)
	if [ -n "$V" ]; then
		N=$( $ADB shell "grep -c mictap /proc/$V/maps" 2>/dev/null | tr -d '\r' )
		say "   netease_voice pid=$V  maps 里 mictap $N 条  $([ "$N" = "3" ] && echo '✓ 钩子已加载' || echo '✗★ 没加载！看 /tmp/mictap.log')"
	else
		say "   ✗ 找不到 netease_voice（restart 没起来？）"
	fi
	# ★ 夜间禁声现在是【扳输出开关】(Headphone/Phoneout Switch)，不再压音量 ——
	#   所以"restart 后 ALSA 音量回默认"这件事不再影响夜间静音了（音量跟闸门正交）。
	#   开关本身被 codec 复位倒是真的，但 nightmute 开头的"同段内重启"分支会重新落闸门，
	#   不用人工补。这里只做一次核对，发现夜里开关还开着才值得看一眼。
	#   ⚠️ nightmute.saved 现在存的是【开关状态】(如 "on on")，不再是音量值。
	HP=$( $ADB shell "amixer -c 0 cget name='Headphone Switch' | grep '  : values=' | cut -d= -f2" 2>/dev/null | tr -d '\r' )
	PO=$( $ADB shell "amixer -c 0 cget name='Phoneout Switch' | grep '  : values=' | cut -d= -f2" 2>/dev/null | tr -d '\r' )
	VOL=$( $ADB shell "amixer -c 0 cget numid=19 | grep '  : values=' | cut -d= -f2" 2>/dev/null | tr -d '\r' )
	H=$( $ADB shell date +%H 2>/dev/null | tr -d '\r' ); H=${H#0}
	say "   输出开关 HP=${HP:-?} PO=${PO:-?}   DAC volume=${VOL:-?}   现在 ${H:-?} 点"
	if [ -n "$H" ] && { [ "$H" -ge 21 ] || [ "$H" -lt 7 ]; } && { [ "$HP" != "off" ] || [ "$PO" != "off" ]; }; then
		say "   ⚠️ 现在是夜间时段，开关却是开的 —— nightmute 应该已经扳掉了，查 adb shell cat /mnt/UDISK/spk/nightmute.log"
	fi
fi

rule
say "完成。"
