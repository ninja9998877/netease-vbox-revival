#!/bin/sh
# 把音箱从中继器 ext2 切到 ext1。
#
# 为什么：实测 ext2 信号 -74dBm(36/70)，而 ext1 是 -46dBm(64/70) —— 强 28dB。
# 它一直连差的那个，是因为 ext1/ext2 是【两个不同的 SSID】，SSID 不同就等于两个
# 互不相干的网络，客户端永远不会自己漫游过去，BLE 配网时锁死在谁身上就是一辈子。
#
# 两个都是同一套中继器、都只开 WPA2-PSK ⇒ 只改 SSID 一个词，psk 不动。
# update_config=1 ⇒ wpa_cli 改完自动写回 wpa_supplicant.conf，持久生效。
# 万一 ext1 的口令其实不同，本脚本自己回滚。备份已存 wpa_supplicant.conf.before-ext1。
S=/mnt/UDISK/wifi/sockets
C="/usr/sbin/wpa_cli -p $S -i wlan0"
TARGET=MyAP_5G_ext
OLD=MyAP_5G_ext2

show() { $C status 2>/dev/null | grep -E '^ssid|^bssid|^wpa_state|^ip_address|^freq'; }

echo "===== 切换前 ====="
show
iwconfig wlan0 2>/dev/null | grep -iE 'bit rate|quality|signal'

echo
echo "===== 切到 $TARGET ====="
$C set_network 0 ssid "\"$TARGET\""
$C select_network 0

# 最多等 20 秒看它能不能关联上
i=0
while [ $i -lt 20 ]; do
	sleep 1
	[ "$($C status 2>/dev/null | grep '^wpa_state' | cut -d= -f2)" = "COMPLETED" ] && break
	i=$((i+1))
done
echo "等了 ${i}s"

echo
echo "===== 切换后 ====="
show
iwconfig wlan0 2>/dev/null | grep -iE 'bit rate|quality|signal'

NOW=$($C status 2>/dev/null | grep '^ssid' | cut -d= -f2)
if [ "$NOW" = "$TARGET" ]; then
	echo
	echo "★★ 成功：已连上 $TARGET"
	# 换了 AP，IPv4 可能要重拿
	if ! ip addr show wlan0 2>/dev/null | grep -q 'inet '; then
		echo "!! 没有 IPv4，跑一次 udhcpc"
		udhcpc -i wlan0 -q -n -t 5 2>&1 | tail -3
		sleep 2
		ip addr show wlan0 2>/dev/null | grep 'inet '
	fi
else
	echo
	echo "!! 没切过去（现在 ssid=$NOW），回滚到 $OLD"
	$C set_network 0 ssid "\"$OLD\""
	$C select_network 0
	sleep 10
	echo "--- 回滚后 ---"
	show
fi
