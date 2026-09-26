#!/bin/sh
# deploy_scripts.sh —— 把 device/ 下的脚本推到音箱（唯一部署入口，管"哪份是活的"）
#
# ★ 为什么需要它（2026-09-21 的教训）：
#   这些脚本【在 UDISK 上跑、本机是源】。改了本机忘了推，就会出现"我改的是 A、
#   跑的还是 B"——最难查的一类故障（跟 mictap 当初两份漂移同一个病）。
#   跟 mictap 不同的是它们【不必挪到 rootfs】：由 run.sh 拉起，而 run.sh 会等 UDISK，
#   没有开机时序问题。所以这里只需要解决"唯一入口 + 认内容不认文件名"。
#
# ★★★ 最关键的一条（不然会静默弄哑音箱）：
#   adb push 落盘是 666 —— 【没有可执行位】。而 run.sh 里每个守护都是
#     if [ -x "$D/xxx.sh" ]; then "$D/xxx.sh" & else 记一行"警告：xxx 不在" fi
#   所以推完不补 chmod，三个守护会【一个都不起来】，而且几乎没有报错：
#     音箱变哑（muxguard 没了）+ 夜里可能被吵（nightmute 没了）+ 重启拿不到 IP（netguard 没了）。
#   ⇒ 本脚本推完一律 chmod 755。（spkbrain.init 里对 spkclient 也是同样的 [ -x ] 判断。）
#
# 用法:
#   ./deploy_scripts.sh              对比，只推有差异的
#   ./deploy_scripts.sh --dry-run    只对比，一个字节都不推
#   ./deploy_scripts.sh --all        强制全推（哪怕 md5 一致）
#   ./deploy_scripts.sh --restart-guards   推完重启三个守护（不碰 run.sh/tailscaled）
set -u

D=$(cd "$(dirname "$0")" && pwd)
ADB=$(command -v adb 2>/dev/null || true)
[ -z "$ADB" ] && [ -x "$HOME/platform-tools/adb" ] && ADB="$HOME/platform-tools/adb"
[ -z "$ADB" ] && { echo "✗ 找不到 adb"; exit 1; }

DRY=0; ALL=0; RG=0
for a in "$@"; do
	case "$a" in
		--dry-run) DRY=1 ;;
		--all) ALL=1 ;;
		--restart-guards) RG=1 ;;
		*) echo "未知参数: $a"; exit 2 ;;
	esac
done

# 本地文件|设备目标|重启方式|说明
#   重启方式: guard:<名字> = 可单独重启的守护 / spkbrain = 要重启 spkbrain / service = 要重启服务 / none = 手动工具
ENTRIES='
spk_conf.sh|/mnt/UDISK/spk/spk_conf.sh|none|★ 集中配置（地址/端口）—— **别的脚本都 `.` 它，它不在设备上，守护会当场静默死掉**
run.sh|/mnt/UDISK/spk/run.sh|spkbrain|tailscaled 启动包装 + 拉起三个守护
netguard.sh|/mnt/UDISK/spk/netguard.sh|guard:netguard|网络守护（重启后要 IP 就靠它）
muxguard.sh|/mnt/UDISK/spk/muxguard.sh|guard:muxguard|左声道守护（哑不哑就靠它）
dacguard.sh|/mnt/UDISK/spk/dacguard.sh|guard:dacguard|DAC volume 守护（归零=全哑，同一个病的第二个位置）
nightmute.sh|/mnt/UDISK/spk/nightmute.sh|guard:nightmute|夜间禁声守护
spkbrain.init|/etc/init.d/spkbrain|spkbrain|开机入口（等 UDISK，再起 run.sh/spkclient）
netease_voice_service.deployed|/etc/init.d/netease_voice_service|service|含 LD_PRELOAD=/lib/mictap.so:/lib/fespatap.so（两个都要），别推旧版
pin_ap.sh|/mnt/UDISK/spk/pin_ap.sh|none|手动工具（钉 AP）
switch_ap.sh|/mnt/UDISK/spk/switch_ap.sh|none|手动工具（带回滚的换 AP）
play601.sh|/mnt/UDISK/spk/play601.sh|none|设备侧嗓子出口（0x601 播 / 0x603 停，不依赖 adb）
face3005.sh|/mnt/UDISK/spk/face3005.sh|none|设备侧点阵出口（buscmd 3005，给 ext/face.py 用）★ 第一次真发必须主人在场
spk_net.sh|/mnt/UDISK/spk/spk_net.sh|guard:spk_net|无线耳朵（设备向外拉命令；顺带补同步静音时段）
spkcheck.sh|/mnt/UDISK/spk/spkcheck.sh|none|每分钟 cron 体检 + 守护保活名单（★ 改了它不在清单里就永远推不上去）
crontab.root|/mnt/UDISK/spk/crontab.root|none|第二道闸的 crontab【源】★ run.sh 开机就是拿它还原 /etc/crontabs/root 的 —— 只推活文件不推它，自保就是空转
crontab.root|/etc/crontabs/root|none|第二道闸的 crontab【活文件】cron 真正读的就是它（在 /overlay）；推完 crond 下一分钟自己重读，不用重启
'

say() { printf '%s\n' "$*"; }
rule() { say "------------------------------------------------------------"; }

[ "$DRY" = 0 ] && { $ADB get-state >/dev/null 2>&1 || { say "✗ 设备不可达"; exit 1; }; }

# ★ 目标列表必须挤在【一行】里，用空格分隔（路径里没有空格）。
#   实测这台设备的 shell：换行只要落在 `for ... in` 的【词表中间】就报
#     syntax error: unexpected word (expecting "do")
#   换行只在【语句边界】（如 do 之后、done 之前）才安全。踩过一次，别改回去。
TARGETS=$(printf '%s\n' "$ENTRIES" | awk -F'|' 'NF && $2!="" {printf "%s ", $2}')
DEVOUT=$($ADB shell "for f in $TARGETS; do if [ -f \"\$f\" ]; then echo \"\$f|\$(md5sum \$f | cut -d' ' -f1)\"; else echo \"\$f|MISSING\"; fi; done" 2>/dev/null | tr -d '\r')

rule
say "① 对比（认内容不认文件名）"
# ★ 排版：printf 的 %-Ns 按【字节】补位，而一个汉字 3 字节 ⇒ 表头用 CJK 就对不齐。
#   所以固定宽度的那几列一律 ASCII，CJK 只放最后一列。
printf '   %s %-31s %-42s %s\n' " " "本地文件" "设备目标" "状态"
CHANGED=""
while IFS='|' read -r local target how desc; do
	[ -z "$local" ] && continue
	src="$D/$local"
	if [ ! -f "$src" ]; then
		printf '   %s %-31s %-42s %s\n' "✗" "$local" "$target" "本机没有这个文件"
		continue
	fi
	lm=$(md5sum "$src" | cut -d' ' -f1)
	dm=$(printf '%s\n' "$DEVOUT" | awk -F'|' -v t="$target" '$1==t{print $2}')
	mark="✓"; st="一致"
	if [ "$dm" = "MISSING" ]; then
		mark="⚠"; st="设备上没有"
	elif [ "$dm" != "$lm" ]; then
		mark="⚠"; st="有差异（本机 ${lm%"${lm#??????}"} / 设备 ${dm%"${dm#??????}"}）"
	fi
	printf '   %s %-31s %-42s %s\n' "$mark" "$local" "$target" "$st"
	# ★ 分隔符这里必须是【真换行】，不能写成 `\n` —— 双引号里的 `\n` 在 sh 里
	#   是字面反斜杠 + n，会把几个文件拼成一整行、后面几个被 desc 吃掉。
	#   （现在是对的，写在这里只是别让人"顺手"改成 \n。）
	if [ "$dm" != "$lm" ]; then
		CHANGED="$CHANGED$local|$target|$how|$desc
"
	fi
done <<EOF
$ENTRIES
EOF

if [ "$ALL" = 1 ]; then
	CHANGED=$(printf '%s\n' "$ENTRIES" | awk -F'|' 'NF && $1!="" {print $0}')
	say "   （--all：强制全推）"
fi

if [ -z "$CHANGED" ]; then
	rule; say "✓ 全部一致，无需推送。"; [ "$RG" = 1 ] || exit 0
fi

if [ "$DRY" = 1 ]; then
	rule; say "（--dry-run，什么都没推）"; exit 0
fi

if [ -n "$CHANGED" ]; then
	rule
	say "② 推送 + chmod 755（★ 不 chmod 会丢掉可执行位，守护会全都不起来）"
	printf '%s\n' "$CHANGED" | while IFS='|' read -r local target how desc; do
		[ -z "$local" ] && continue
		# ★★★ 每个 adb 调用都必须带 `< /dev/null`，【一个都不能漏】——
		#   adb 会读 stdin，而这里的 stdin 正是上面那个 `printf | while read` 的管道
		#   ⇒ 它把管道里剩下的待推文件【全吃光】⇒ read 立刻 EOF ⇒ 循环只跑第一轮
		#   ⇒ **只推第一个文件，后面的静默丢掉、一个错都不报**。
		#   实测症状（2026-09-22 加 dacguard 时撞上）：一次改了三个文件，
		#   ② 只列出一条，③ 却把三条都列出来 —— 因为 ③ 的循环里没有 adb 调用，
		#   两个循环口径不一致，一眼看着像"推成功了"。
		#   ⇒ 以后往这个循环里加任何会读 stdin 的命令，都要带上 `< /dev/null`。
		$ADB push "$D/$local" "$target" >/dev/null 2>&1 < /dev/null || { say "   ✗ $local 推送失败"; continue; }
		$ADB shell "chmod 755 $target" >/dev/null 2>&1 < /dev/null
		nm=$($ADB shell "md5sum $target | cut -d' ' -f1" 2>/dev/null < /dev/null | tr -d '\r')
		om=$(md5sum "$D/$local" | cut -d' ' -f1)
		if [ "$nm" = "$om" ]; then
			say "   ✓ $local → $target   md5 一致，权限 $( $ADB shell "ls -l $target | cut -c1-10" 2>/dev/null < /dev/null | tr -d '\r')"
		else
			say "   ✗ $local → $target   md5 不一致（设备 $nm）"
		fi
	done
fi

# ---- ③ 生效方式 ----
rule
say "③ 怎么生效"
RESTART_GUARDS=0
printf '%s\n' "${CHANGED:-}" | while IFS='|' read -r local target how desc; do
	[ -z "$how" ] && continue
	case "$how" in
		guard:*)   say "   · $local   → 需重启守护 ${how#guard:}（可加 --restart-guards 自动做）" ;;
		spkbrain)  say "   · $local   → 要 /etc/init.d/spkbrain restart（★ 会顺带重启 tailscaled，隧道瞬断）" ;;
		service)   say "   · $local   → 要 /etc/init.d/netease_voice_service restart" ;;
		none)      say "   · $local   → 手动工具，下次调用即是新版" ;;
	esac
done
say ""
say "   ★ 重启 netease_voice_service 前【必须先验两个前置条件】，否则它会走 reboot -f 硬重启音箱："
say "       /tmp/dbus_env.sh 可执行  且  dbus-daemon --fork 正好 1 个"
say "       （现成做法见 ./deploy_mictap.sh --restart，它把这两条验好了）"

if [ "$RG" = 1 ]; then
	rule
	say "④ 重启三个守护（不碰 run.sh / tailscaled）"
	# ★ 按 cmdline 【精确相等】找进程：脚本自己的 cmdline 里含那些字面量，
	#   用 grep/子串匹配会匹配到自己的 adb shell（我为此拉过假警报）。
	dev_ps() {
		$ADB shell 'for p in /proc/[0-9]*; do
			echo "$(basename $p)|$(cat $p/cmdline 2>/dev/null | tr "\0" " ")"
		done' 2>/dev/null | tr -d '\r'
	}
	dev_pids_of() {   # $1 = 设备上的脚本路径 → 空格分隔的 pid 串
		dev_ps | awk -F'|' -v want="/bin/sh $1 " '$2==want{print $1}' | tr '\n' ' '
	}
	for g in netguard muxguard dacguard nightmute; do
		path="/mnt/UDISK/spk/$g.sh"
		old=$(dev_pids_of "$path")
		# ★★★ kill 必须套 $ADB shell ★★★
		#   裸 `kill $pid` 杀的是【本机 mac mini 上】同号的进程，设备上旧的照跑；
		#   而下面又把锁删了 ⇒ 新实例的 mkdir 成功 ⇒ 【每个守护跑两份】，
		#   输出还长得像成功（新 pid 列里混着旧 pid）。我 2026-09-21 就这么把
		#   三个守护全搞成双份的。写完这一步必须用「恰好 1 份」来验，别信 pid 变了。
		[ -n "$old" ] && $ADB shell "kill $old" >/dev/null 2>&1
		[ -n "$old" ] && sleep 1
		# 先确认旧的真死了，再往下走 —— 没死就绝不能起第二个
		still=$(dev_pids_of "$path")
		if [ -n "$still" ]; then
			say "   ✗ $g.sh 杀不掉（剩 pid=[$still]）—— 停手，不再起第二个"
			continue
		fi
		# ★ 锁必须一起清：守护开头是 mkdir "$LOCK" || exit 0，残留锁会让新进程立刻自杀
		$ADB shell "rm -rf /tmp/spk/$g.lock" >/dev/null 2>&1
		# ★ 不能用 -x /bin/sh -- 路径（identifier 撞 /bin/sh 会报 already running），直接给脚本
		$ADB shell "start-stop-daemon -S -b -x $path" >/dev/null 2>&1
		sleep 1
		new=$(dev_pids_of "$path")
		nn=$(printf '%s' "$new" | wc -w)
		if [ "$nn" = 1 ]; then
			say "   ✓ $g.sh  旧 pid=[${old:-无}] → 新 pid=[$new]（恰好 1 份）"
		else
			say "   ✗ $g.sh  **跑了 $nn 份** pid=[${new:-无}] —— 手动清: adb shell \"kill $new\""
		fi
	done
fi

rule
say "完成。"
