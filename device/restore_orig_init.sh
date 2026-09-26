#!/bin/sh
# restore_orig_init.sh —— 把三个原厂 init 脚本还原成出厂原版（HP_L Mux 1）。
#
# 原件在 /mnt/UDISK/spk/orig_init/*.orig（建时已 md5 校验与出厂件一致）。
# 用 fix_mux_source.sh 改过之后，跑这个就能一键撤回【网易原样】。
#
# ★ 还原后【每次开机又会哑】（网易脚本把左声道接进死路）。muxguard / spkcheck
#   仍会在运行中把 mux 扳回 0，但开机头几十秒到一分钟是哑的。

set -u
B=/mnt/UDISK/spk/orig_init
FILES="voice_service netease_player_service netease_services"

echo "=== 还原（用出厂原件覆盖）==="
for f in $FILES; do
	if [ ! -f "$B/$f.orig" ]; then
		echo "  !! 缺 $B/$f.orig —— 跳过"
		continue
	fi
	cp -a "$B/$f.orig" "/etc/init.d/$f" && echo "  ✓ $f 已还原"
done

echo
echo "=== 还原后（应看到 name='HP_L Mux' 1，即网易原样）==="
for f in $FILES; do
	printf '%-26s : ' "$f"
	grep -n "name='HP_L Mux'" "/etc/init.d/$f"
done

echo
echo "=== 语法自检 ==="
for f in $FILES; do
	if sh -n "/etc/init.d/$f" 2>/dev/null; then echo "  ✓ $f OK"; else echo "  ✗ $f 坏了！"; fi
done

echo
echo "★ 提醒：还原后开机必哑，靠 muxguard / spkcheck 兜底扳回。"
