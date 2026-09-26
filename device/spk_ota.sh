#!/bin/sh
# spk_ota.sh —— 我们自己的 OTA：把新版文件从 macmini 送到这台音箱上，安全地生效。
#
# 【为什么需要它】通道早就有了：macmini:8899 能下发文件、8898 能远程执行 shell。
#   缺的是"这一次写盘要【原子、可验、可退】"——
#   直接 wget 覆盖正在用的脚本，中途断网就是一个半个文件；而这几个是【开机脚本】，
#   写坏一个下次开机整条链就起不来（踩过：muxguard 没起来，音箱哑了一整夜）。
#   所以本脚本不是"造通道"，是给现成的通道补上安全落盘那一段。
#
# 【原子性从哪来】★ 临时文件必须落在【目标同一个分区】里，最后用 mv(rename) 顶上。
#   rename 只改目录项、不搬数据 ⇒ 要么全成要么全不成；断电最多是"还是旧版"。
#   ★★ 绝不能下到 /tmp 再 mv —— /tmp 是 tmpfs，跨分区 mv 是"复制+删除"，
#     不原子，等于把"半个文件"这个风险又原样请回来。
#
# 【正在跑的脚本被换掉会不会坏】不会。rename 不动已经打开的 inode ——
#   正在循环的 muxguard.sh 继续读它手里那份老代码（老 inode 没被释放），
#   直到下次重启才用新版。所以替换是安全的，不需要先停谁。
#   ★ 唯一的例外是【本脚本自己】：边读边执行的脚本被换掉，行为取决于 shell
#     实现，不想赌 ⇒ 明确拒绝（见 self_guard）。
#
# 用法：
#   sh /mnt/UDISK/spk/spk_ota.sh put <源名> <目标路径> [期望md5]
#   sh /mnt/UDISK/spk/spk_ota.sh rollback <目标路径>
#   sh /mnt/UDISK/spk/spk_ota.sh show <目标路径>
#
#   源名    macmini:8899 下的文件名（根目录是 /tmp，见 spk_ai_dlna.py 的 STAGE_FILE）
#   目标    设备上的绝对路径，必须在白名单目录内（防手滑写错一个字母）
#   期望md5 省略则跳过校验。★ 不推荐 —— 没有校验就没有"整包丢弃"这道闸门
#
# 输出【每条路径的最后一行固定是 OTA-OK / OTA-FAIL】—— 走 8898 的 ret 通道时，
#   空回复会被当成"没回话"（spk_netd.send 踩过：空结果被当超时，白等 8 秒），
#   所以本脚本一条出口都不留空。
#
# 回退：本脚本自身不被任何东西调用，删掉它对全机零影响。

set -u

# 配置（见同目录 spk_conf.sh）
_d=$(dirname "$0"); [ -f "$_d/spk_conf.sh" ] || _d=${SPK_DIR:-/mnt/UDISK/spk}
# ★ 必须先 [ -f ] 判一次再 `.` —— `.` 找不到文件时**非交互 shell 直接退出**（rc=2），
#   后面一行都不执行、也没有像样的报错，症状就是"守护一个都没起来、音箱变哑"。
[ -f "$_d/spk_conf.sh" ] || { echo "缺 $_d/spk_conf.sh —— 先把它推到设备上" >&2; exit 1; }
. "$_d/spk_conf.sh"

D=/mnt/UDISK/spk
BAK=$D/backup
LOG=$D/ota.log
SRV=${SPK_OTA_SRV:-http://$SPK_SRV:$SPK_PORT_MP3}
KEEP=3          # 每个目标最多留几份备份。★ UDISK 只剩十几 MB，不能无限堆
MAXLOG=65536    # 日志超过这个大小就轮转一次（跟 spk_net.sh 同一个理由）

# --------------------------------------------------------------------------
log() {
	# 同时写盘和 stdout：走 8898 时人要看见，事后排错要有账。
	printf '%s %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"
	printf '%s\n' "$*"
}

die() {
	log "OTA-FAIL: $*"
	exit 1
}

rotate() {
	# ★ 日志在 UDISK 上，不轮转会一直吃空间（这台机器只剩十几 MB）。
	[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$MAXLOG" ] && mv "$LOG" "$LOG.1"
	return 0
}

allowed() {
	# 只许替换这几个目录里的东西。
	# ★ 这不是防外人 —— 通道本身就能 `sh -c` 任意命令，脚本不比通道更严。
	#   它防的是【手滑】：目标路径写错一个字母就换掉系统文件，代价太大。
	case "$1" in
		/mnt/UDISK/spk/*|/lib/*|/usr/lib/*|/etc/init.d/*) return 0 ;;
		*) return 1 ;;
	esac
}

self_guard() {
	# 换别的脚本没事（rename 不动已打开的 inode，见文件头）；换【自己】不行。
	case "$(basename "$1")" in
		spk_ota.sh) die "不许替换 spk_ota.sh 自己（边读边执行，被换掉行为未定义）—— 要更新它请插一次线" ;;
	esac
}

md5_of() {
	# busybox 的 md5sum 回的是 "<hash>  <名字>"，取第一段。
	md5sum "$1" 2>/dev/null | cut -d' ' -f1
}

# --------------------------------------------------------------------------
cmd_put() {
	local src=$1 dst=$2 want=${3:-}
	local dir new bk old got

	case "$src" in
		'')   die "源名是空的" ;;
		*/*)  die "源名不许带路径（它只是 8899 根下的一个文件名）：$src" ;;
	esac
	allowed "$dst" || die "目标不在白名单目录里：$dst"
	self_guard "$dst"

	dir=$(dirname "$dst")
	[ -d "$dir" ] || die "目标目录不存在：$dir"
	# ★ 只做替换，不负责新建 —— "新建"该由人明确决定，不该是 OTA 的副作用。
	[ -f "$dst" ] || die "目标文件不存在：$dst（本脚本只做替换）"

	# ★★ 临时文件落在【目标同一个目录】里。这是原子性的全部秘密，别挪到 /tmp。
	new="$dir/.ota.$(basename "$dst").new"

	rotate
	old=$(md5_of "$dst")

	if ! wget -q -O "$new" "$SRV/$src"; then
		rm -f "$new"
		die "下载失败：$SRV/$src（macmini:8899 通吗？文件放进 /tmp 了吗？）"
	fi

	# ★ 空文件闸门：wget 失败有时会留下一个 0 字节的文件，它盖上去就是灾难。
	#   校验和这道闸门各挡一面 —— 没给 md5 时这道就是唯一的防线。
	[ -s "$new" ] || { rm -f "$new"; die "拿到的是空文件 —— 已丢弃，老文件没动"; }

	if [ -n "$want" ]; then
		want=$(printf '%s' "$want" | tr 'A-F' 'a-f')     # 大小写不敏感
		got=$(md5_of "$new")
		[ "$got" = "$want" ] || {
			rm -f "$new"
			die "md5 不符（要 $want，得 $got）—— 已丢弃，老文件没动"
		}
	fi

	mkdir -p "$BAK"
	bk="$BAK/$(basename "$dst").$(date '+%Y%m%d-%H%M%S')"
	cp "$dst" "$bk" || { rm -f "$new"; die "备份失败：$bk"; }

	# 修剪：同一个目标只留最近 KEEP 份（名字带时间戳 ⇒ ls -t 就是新→旧）。
	ls -1t "$BAK/$(basename "$dst")."* 2>/dev/null | tail -n +$((KEEP + 1)) |
		while read -r f; do rm -f "$f"; done

	# ★★ 保住可执行位（2026-09-23 真事故）。wget 生成的新文件默认 0644，
	#   替换 /etc/init.d/* 这类可执行文件会【丢掉 +x】：下次开机该服务根本起不来，
	#   而且当场 restart 回的是 rc=126 "Permission denied"，看起来像"改动没生效"，
	#   极容易误判成别的原因。设备上没有 stat，用 -x 判一下就够了 ——
	#   这台机器上需要 +x 的就那几种（init.d 脚本、play601.sh 之类）。
	if [ -x "$dst" ]; then chmod 755 "$new"; fi

	# ★★ 原子替换。失败也不留半个文件（rename 本来就全成或全不成）。
	mv "$new" "$dst" || { rm -f "$new"; die "替换失败（老文件没动，备份在 $bk）"; }
	sync

	log "OTA-OK $dst  ${old:-无} -> $(md5_of "$dst")  备份 $(basename "$bk")"
}

# --------------------------------------------------------------------------
cmd_rollback() {
	local dst=$1 dir tmp bk

	allowed "$dst" || die "目标不在白名单目录里：$dst"
	self_guard "$dst"
	[ -f "$dst" ] || die "目标文件不存在：$dst"

	bk=$(ls -1t "$BAK/$(basename "$dst")."* 2>/dev/null | head -1)
	[ -n "$bk" ] || die "没有 $dst 的备份，退不回去"

	dir=$(dirname "$dst")
	# ★ 备份在 UDISK，目标可能在 /lib（另一个分区）⇒ 同样要"先落到目标同分区再 mv"。
	tmp="$dir/.ota.$(basename "$dst").rb"
	cp "$bk" "$tmp" || die "取备份失败：$bk"
	# ★ 同一个坑（见 cmd_put 里那段）：cp 出来的也是默认 0644，回滚可执行文件会把 +x 退没。
	if [ -x "$dst" ]; then chmod 755 "$tmp"; fi
	mv "$tmp" "$dst" || { rm -f "$tmp"; die "回滚替换失败"; }
	sync

	# ★ 备份不删：回滚要幂等，而且退回去之后很可能还想再退一次。
	log "OTA-OK 已回滚 $dst <- $(basename "$bk")  现在 $(md5_of "$dst")"
}

# --------------------------------------------------------------------------
cmd_show() {
	local dst=$1 bk

	printf '目标 %s\n' "$dst"
	if [ -f "$dst" ]; then
		printf '  当前  md5 %s   %s 字节\n' "$(md5_of "$dst")" "$(wc -c < "$dst")"
	else
		printf '  当前  不存在\n'
	fi
	bk=$(ls -1t "$BAK/$(basename "$dst")."* 2>/dev/null)
	if [ -n "$bk" ]; then
		printf '  备份（新→旧）：\n'
		printf '%s\n' "$bk" | while read -r f; do
			printf '    %s  %s  %s 字节\n' "$(basename "$f")" "$(md5_of "$f")" "$(wc -c < "$f")"
		done
	else
		printf '  备份  没有\n'
	fi
	log "OTA-OK show $dst"
}

# --------------------------------------------------------------------------
case "${1:-}" in
	put)
		[ $# -ge 3 ] || die "用法：put <源名> <目标路径> [期望md5]"
		shift
		cmd_put "$@"
		;;
	rollback)
		[ $# -ge 2 ] || die "用法：rollback <目标路径>"
		shift
		cmd_rollback "$@"
		;;
	show)
		[ $# -ge 2 ] || die "用法：show <目标路径>"
		shift
		cmd_show "$@"
		;;
	*)
		printf 'spk_ota —— 我们自己的 OTA（拉新版 / 校验 / 原子替换 / 留旧版可回滚）\n'
		printf '用法：\n'
		printf '  put <源名> <目标路径> [期望md5]   从 %s 拉、校验、原子替换\n' "$SRV"
		printf '  rollback <目标路径>               退回最近一份备份\n'
		printf '  show <目标路径>                   看当前版本和所有备份\n'
		exit 2
		;;
esac
