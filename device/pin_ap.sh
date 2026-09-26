#!/bin/sh
# 把音箱强制钉到 ext1 那个强 AP 上。
#
# 背景：同一个信道上有两个 AP，本质是两个不同的 BSSID：
#   AA:BB:CC:DD:EE:02  -81dBm  [WPA2-PSK-CCMP][WPS]            MyAP_5G_ext2   ← 现在连的
#   AA:BB:CC:DD:EE:01  -46dBm  [WPA-PSK-CCMP][WPA2-PSK-CCMP]   MyAP_5G_ext    ← 想连的，强 35dB
# （上面是示例值，改成你自己那两个 AP 的 BSSID / SSID，以你的实际部署为准。）
# SSID 名不同 ⇒ 客户端永远不漫游。select_network 只会沿用缓存的 BSS，所以必须显式
# 指定 bssid 才能真的换过去。
#
# ★ 风险：两个 AP 安全配置不同(ext2 只有 WPA2，ext1 是 WPA/WPA2 混合)，不排除口令也不同。
#   所以本脚本等 25 秒，连不上就把 ssid/bssid 全部还原回 ext2。备份在
#   wpa_supplicant.conf.before-ext1。adb 是 USB 直连，Wi-Fi 全断也进得来。
S=/mnt/UDISK/wifi/sockets
C="/usr/sbin/wpa_cli -p $S -i wlan0"
TARGET=MyAP_5G_ext
TARGET_BSSID=AA:BB:CC:DD:EE:01
OLD=MyAP_5G_ext2
CONF=/mnt/UDISK/wifi/wpa_supplicant.conf

show() { $C status 2>/dev/null | grep -E '^ssid|^bssid|^wpa_state|^ip_address|^freq'; }
phy()  { iwconfig wlan0 2>/dev/null | grep -iE 'bit rate|quality|signal'; }

echo "===== 前 ====="; show; phy

echo
echo "===== 钉到 $TARGET ($TARGET_BSSID) ====="
$C set_network 0 ssid "\"$TARGET\""
$C set_network 0 bssid $TARGET_BSSID
$C select_network 0

i=0
while [ $i -lt 25 ]; do
	sleep 1
	st=$($C status 2>/dev/null | grep '^wpa_state' | cut -d= -f2)
	[ "$st" = "COMPLETED" ] && break
	i=$((i+1))
done

bs=$($C status 2>/dev/null | grep '^bssid' | cut -d= -f2)
echo "等了 ${i}s，wpa_state=$st bssid=$bs"

# 必须真的落在目标 BSSID 上才算成功 —— ssid 字符串对不算数
if [ "$st" = "COMPLETED" ] && [ "$bs" = "$TARGET_BSSID" ]; then
	echo
	echo "★★★ 成功：真的落在 $TARGET_BSSID 上了"
	show; phy
	if ! ip addr show wlan0 2>/dev/null | grep -q 'inet '; then
		echo "!! 没有 IPv4，跑一次 udhcpc"
		udhcpc -i wlan0 -q -n -t 5 2>&1 | tail -3
		sleep 3
		ip addr show wlan0 2>/dev/null | grep 'inet '
	fi
	echo "=== 最终 ==="; show; phy
else
	echo
	echo "!! 没落到目标上（ssid=$($C status 2>/dev/null | grep '^ssid' | cut -d= -f2) bssid=$bs）"
	echo "!! 回滚到 $OLD"
	$C set_network 0 ssid "\"$OLD\""
	# ★★★ 这里【绝不能】用 set_network 0 bssid AA:BB:CC:DD:EE:01 来"解钉"。
	#   在 wpa_supplicant 里全零 BSSID 不是"不限制"，而是"只连这个 MAC 的 AP"，
	#   而全零匹配不上任何真实 AP ⇒ 【永久拒绝关联、静默不联网】，重启也不会自愈
	#   （配置是持久的）。2026-09-21 实测：音箱因此断网一整夜，查了半天才发现
	#   是配置里那一行。wpa_cli 也没有"清除 bssid"的命令（hwaddr_aton 拒空串），
	#   所以要解钉只能把这行从配置文件里删掉，再让它重读。
	sed -i "/^[[:space:]]*bssid=/d" "$CONF"
	$C reconfigure
	$C select_network 0
	j=0
	while [ $j -lt 20 ]; do
		sleep 1
		[ "$($C status 2>/dev/null | grep '^wpa_state' | cut -d= -f2)" = "COMPLETED" ] && break
		j=$((j+1))
	done
	echo "--- 回滚后 ---"; show; phy
	echo "(若仍不通，恢复备份：cp /mnt/UDISK/wifi/wpa_supplicant.conf.before-ext1 /mnt/UDISK/wifi/wpa_supplicant.conf)"
fi
