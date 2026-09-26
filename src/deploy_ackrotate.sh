#!/bin/bash
# deploy_ackrotate.sh —— 把「唤醒应答随机轮换」装到音箱上
#
# 用法：
#   ./deploy_ackrotate.sh <候选目录>   # 目录里的 *.mp3 全当候选池（会清掉旧池子）
#   ./deploy_ackrotate.sh --status     # 看状态
#   ./deploy_ackrotate.sh --off        # 暂停轮换（手动钉某句时用）
#   ./deploy_ackrotate.sh --on         # 恢复轮换
#   ./deploy_ackrotate.sh --test       # 手动催一次轮换（不用等唤醒）
#   ./deploy_ackrotate.sh --uninstall  # 卸守护（run.sh 还原，池子留着）
#
# ★ 安全边界：只写 /mnt/UDISK/spk/ 和 UDISK 上的池子目录，【绝不碰 skins.db】。
#   所以还原路径极短：卸掉守护 + set_wakevoice.sh --restore 就回到网易原版。

set -u
V=/mnt/UDISK/resources/voice
POOL="$V/spk_ack_pool"
D=/mnt/UDISK/spk
# ★ CUR 必须是【设备实际读到的那个文件】—— 不再是 $V/spk_ack.mp3。
#   原因见 ackrotate.sh 第 55-70 行：网易自己的资源服务会把 skins.db 同步回去，
#   我们改的那条被抹掉 ⇒ v303 悬空 ⇒ 落到 /rom/usr/share/resource/voice/S003.mp3，
#   而那个目录正是我们的静音目录盖住的，且 /rom 与 UDISK 是同一块 ext4
#   ⇒ 只有写这里才真正生效，而且一个字节都不碰库。
#   ⚠️ 2026-09-22 修：这里原来还写着旧路径，导致 `--status` 看的是一个【不存在】的文件、
#      `--test` 催不动轮换（守护压根不读那儿）—— 两个功能静默失效，看着还挺正常。
CUR="$D/silent/rom/S003.mp3"
RUNSH="$D/run.sh"
BAK="$RUNSH.bak-pre-ackrotate"
HERE=$(cd "$(dirname "$0")" && pwd)

say() { printf '%s\n' "$*"; }
sh_() { adb shell "$1" 2>&1 | tr -d '\r'; }

need_adb() {
	adb get-state >/dev/null 2>&1 || { say "✗ 连不上设备（adb）"; exit 1; }
}

# ---------------------------------------------------------------- 状态
do_status() {
	need_adb
	say "── 守护进程 ──"
	sh_ 'for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); [ "$c" = "ackrotate.sh" ] && echo "  pid ${p#/proc/}  $c"; done'
	say "── 候选池 ──"
	sh_ "ls -l $POOL/*.mp3 2>/dev/null | sed 's/.*root *[0-9]* //' | sed 's|.*/||'"
	# ★★★ 绝不读 $CUR！open 它就会推 atime，守护下一轮马上误判成"设备刚被唤醒过"⇒
	#   每查一次状态，音箱就换一句应答。实测教训（2026-09-21）：这里原本是 md5sum $CUR，
	#   就是这么把 atime 从 10:14 推到 10:57 的（而同期 voice.log 里没有 Get skin key ⇒ 是人读的不是设备读的）。
	#   想知道"当前装的是哪个"⇒ 问守护自己写的 $D/ackrotate.prev；要看文件本身只用 ls -lu（只 stat 不 open）。
	say "── 当前装的是 ──"
	sh_ "p=\$(cat $D/ackrotate.prev 2>/dev/null); [ -n \"\$p\" ] && echo \"  \$(basename \$p)\" || echo '  （无记录：守护没跑过，或池子里只有一个所以没换过）'"
	say "── 判据盯着的那个文件的 atime ──"
	sh_ "ls -lu $CUR 2>/dev/null | sed 's/.*root *[0-9]* //' | sed 's|^|  |'"
	sh_ "ls -lu $CUR 2>/dev/null | grep -q 1970 && echo '  ⇒ 还带 1970 ＝ 没被读过（正常待机，守护不动）' || echo '  ⇒ atime 已被推走 ＝ 设备读过 / 或有人读过它'"
	say "── 急停开关 ──"
	sh_ "[ -f /tmp/spk/ackrotate.off ] && echo '  已暂停' || echo '  运行中'"
	say "── 最近日志 ──"
	sh_ "tail -8 $D/ackrotate.log 2>/dev/null || echo '  (还没有日志)'"
}

# ---------------------------------------------------------------- 部署
do_deploy() {
	local src="$1"
	[ -d "$src" ] || { say "✗ 不是目录: $src"; exit 1; }
	local n; n=$(ls "$src"/*.mp3 2>/dev/null | wc -l)
	[ "$n" -gt 0 ] || { say "✗ $src 里没有 *.mp3"; exit 1; }
	say "候选 $n 个"
	need_adb

	say "① 建池子目录并推候选"
	sh_ "mkdir -p $POOL"
	sh_ "rm -f $POOL/*.mp3"
	bad=0
	for f in "$src"/*.mp3; do
		if adb push "$f" "$POOL/$(basename "$f")" </dev/null >/dev/null 2>&1; then
			printf '   ✓ %s\n' "$(basename "$f")"
		else
			printf '   ✗ %s\n' "$(basename "$f")"
			bad=$((bad + 1))
		fi
	done
	# ★ 池子在这一步之前刚被清空过：少推上去几个 = 池子不完整，
	#   而守护在池子为空时【直接 exit 1】（ackrotate.sh 第 153 行）⇒ 会被 spkcheck
	#   反复拉起、反复退出。不把这个数报出来，失败就伪装成了"推完了"。
	if [ "$bad" -gt 0 ]; then
		say "   ⚠ $bad 个没推上去 —— 池子不完整（别不管：空池子会让守护直接退出）"
	fi
	# push 落盘是 666 没有可执行位，这里只读不执行，无所谓；但权限统一一下更整齐
	sh_ "chmod 644 $POOL/*.mp3 2>/dev/null"

	say "② 推守护"
	# ★ 推之前先记下设备上那份的 md5：④ 步靠它判断"脚本到底变没变"，
	#   没变就别去重启守护（重启正是 ④ 步以前起成两份的根源）。
	old_md5=$(sh_ "md5sum $D/ackrotate.sh 2>/dev/null | cut -d' ' -f1")
	new_md5=$(md5sum "$HERE/ackrotate.sh" | cut -d' ' -f1)
	adb push "$HERE/ackrotate.sh" "$D/ackrotate.sh" </dev/null >/dev/null 2>&1 \
		&& say "   ✓ $D/ackrotate.sh" || { say "   ✗ 推失败"; exit 1; }
	# ★ 记忆里的坑：adb push 落盘 666 无可执行位 ⇒ [ -x ] 判false ⇒ 守护静默不起
	sh_ "chmod 755 $D/ackrotate.sh"
	sh_ "[ -x $D/ackrotate.sh ] && echo '   可执行位 ✓' || echo '   ✗ 可执行位没设上'"

	say "③ 挂进开机链条（改 run.sh 前先备份）"
	if sh_ "grep -q ackrotate $RUNSH && echo yes" | grep -q yes; then
		say "   run.sh 里已有，跳过"
	else
		sh_ "[ -f $BAK ] || cp $RUNSH $BAK"
		# 在 netguard 那段之后插入（那里 UDISK 必定已挂载）
		sh_ "cat > /tmp/_ack.ins <<'EOF'

# 唤醒应答随机轮换（2026-09-21 加）—— 设备每次唤醒都重读 spk_ack.mp3，所以只换文件
# 就能换应答，不碰 skins.db。判据是 atime：装上去时钉 1970，被读过 atime 就变了。
# 池子在 /mnt/UDISK/resources/voice/spk_ack_pool，部署/还原见 deploy_ackrotate.sh。
if [ -x \"\$D/ackrotate.sh\" ]; then
	\"\$D/ackrotate.sh\" &
else
	echo \"\$(date) 警告：\$D/ackrotate.sh 不在，唤醒应答不会随机\" >> \"\$LOG\"
fi
EOF
awk '/netguard\.sh/&&!done{print;print \"\";while((getline line < \"/tmp/_ack.ins\")>0)print line;done=1;next}1' $RUNSH > /tmp/_run.new && mv /tmp/_run.new $RUNSH"
		sh_ "chmod 755 $RUNSH; rm -f /tmp/_ack.ins"
		say "   已插入（备份在 $BAK）"
	fi

	say "④ 守护（有就跑着，没有才起，脚本变了才重启）"
	# ★★ 这一步以前是 `rm -rf $LOCK; start-stop-daemon -S ...` —— 【会起成两份】：
	#   守护本来就在跑，我们却把它的锁拆了 ⇒ 新实例 mkdir 成功、拿锁跑起来，
	#   而老实例还活着（它不知道自己锁没了）⇒ 两份每 3 秒各 rotate 一次，
	#   应答音被换得飞快，还互相踩 atime。
	#   ★ 而且池子更新【根本不需要】重启守护 —— 它的主循环每 3 秒重读 $POOL 和 $CUR
	#     （ackrotate.sh 第 162-168 行），下次轮换自己就用上新的了。
	local pid
	pid=$(sh_ "cat /tmp/spk/ackrotate.lock/pid 2>/dev/null")
	if [ -n "$pid" ] && sh_ "[ \"\$(cat /proc/$pid/comm 2>/dev/null)\" = ackrotate.sh ] && echo y" | grep -q y; then
		if [ "$old_md5" = "$new_md5" ]; then
			say "   ○ 已在跑（pid $pid）且脚本没变 ⇒ 不动它"
		else
			say "   ↻ 已在跑（pid $pid）但脚本变了 ⇒ 重启"
			sh_ "kill $pid"
			# ★ 必须等它【真死】再动锁 —— 不等就 rm 锁 = 又把老实例的锁拆了，照样两份。
			for _i in 1 2 3 4 5 6 7 8 9 10; do
				sh_ "[ -d /proc/$pid ] || echo gone" | grep -q gone && break
				sleep 1
			done
			sh_ "rm -rf /tmp/spk/ackrotate.lock"
			sh_ "start-stop-daemon -S -b -x $D/ackrotate.sh"
			sleep 3
		fi
	else
		say "   ▶ 不在跑 ⇒ 起一个"
		sh_ "rm -rf /tmp/spk/ackrotate.lock"     # 只有确认没人跑，才敢动这个锁
		sh_ "start-stop-daemon -S -b -x $D/ackrotate.sh"
		sleep 3
	fi

	local cnt; cnt=$(sh_ 'for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); [ "$c" = "ackrotate.sh" ] && echo x; done' | grep -c x)
	if [ "$cnt" = "1" ]; then
		say "   ✓ 恰好 1 份在跑"
	elif [ "$cnt" = "0" ]; then
		say "   ✗ 没起来 —— 手动跑一次看报错：adb shell sh $D/ackrotate.sh"
	else
		say "   ⚠ 跑了 $cnt 份（不该发生，检查锁）"
	fi

	say ""
	do_status
}

# ---------------------------------------------------------------- 维护动作
do_ctl() {
	need_adb
	case "$1" in
	--off)    sh_ "touch /tmp/spk/ackrotate.off"; say "✓ 已暂停（想手动钉句请用 set_wakevoice.sh）" ;;
	--on)     sh_ "rm -f /tmp/spk/ackrotate.off"; say "✓ 已恢复轮换" ;;
	--test)   sh_ "rm -f /tmp/spk/ackrotate.off; ls -lu $CUR | sed 's/.*root *[0-9]* //'"
	          say "   ↑ 催一次：把 atime 改成非 1970，守护 3 秒内会换"
	          sh_ "touch $CUR"; sleep 5
	          sh_ "tail -3 $D/ackrotate.log 2>/dev/null" ;;
	--uninstall)
	          sh_ "for p in /proc/[0-9]*; do c=\$(cat \$p/comm 2>/dev/null); [ \"\$c\" = ackrotate.sh ] && kill \${p#/proc/}; done"
	          sh_ "rm -rf /tmp/spk/ackrotate.lock"
	          if sh_ "grep -q ackrotate $RUNSH && echo yes" | grep -q yes; then
	          	sh_ "[ -f $BAK ] && cp $BAK $RUNSH && chmod 755 $RUNSH && echo '   run.sh 已还原'"
	          fi
	          say "✓ 守护已卸。唤醒应答保持当前那句；要回网易原版再跑："
	          say "    $HERE/set_wakevoice.sh --restore" ;;
	esac
}

case "${1:---status}" in
	--status)    do_status ;;
	--off|--on|--test|--uninstall) do_ctl "$1" ;;
	-h|--help)   sed -n '2,20p' "$0" ;;
	*)           do_deploy "$1" ;;
esac
