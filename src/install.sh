#!/bin/bash
# 音箱大脑 —— 一键安装（在盒子本机上跑，用 root）
#
#   curl 不适用，直接把整个 spkbrain 目录拷到盒子上，然后：
#     sudo ./install.sh                    # 会让你粘贴 LLM 密钥
#     sudo SPK_KEY=sk-xxx ./install.sh     # 或者直接给
#
# 装完就开机自启。使用侧只需要：插网线、插电、（音箱没配过网时）长按音箱配网键。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
AP_SSID=${SPK_AP_SSID:-SpeakerBrain}
AP_PWD=${SPK_AP_PWD:-spk12345678}
AP_IFACE=${SPK_AP_IFACE:-wlan0}
SELF_IP=${SPK_SELF_IP:-192.168.50.1}

[ "$(id -u)" = 0 ] || { echo "✗ 要用 root 跑：sudo ./install.sh"; exit 1; }

echo "═══ 1/6 装依赖 ═══"
# ★ dnsmasq-base（只有二进制，没有系统服务）—— 绝不能装 dnsmasq，
#   那会起一个系统级的 DNS 服务，和 NetworkManager 自带的那个打架。
if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg openssl iw network-manager dnsmasq-base bluez
elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip ffmpeg openssl iw NetworkManager dnsmasq bluez
fi
# bleak = 蓝牙 central（配网用）；edge-tts = 合成语音
python3 -m pip install --break-system-packages -q bleak edge-tts 2>/dev/null \
    || python3 -m pip install -q bleak edge-tts

echo "═══ 2/6 生成自签证书 ═══"
# 音箱【不校验】证书（实测），自签就够。vbox-server 那个服务会校验，所以别指望它。
mkdir -p /etc/spkbrain
if [ ! -f /etc/spkbrain/spoof.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout /etc/spkbrain/spoof.key -out /etc/spkbrain/spoof.pem \
        -subj "/CN=vbox-asr.3.163.com" \
        -addext "subjectAltName=DNS:vbox-asr.3.163.com,DNS:vbox-tts.3.163.com" 2>/dev/null
    echo "  已生成 /etc/spkbrain/spoof.pem"
else
    echo "  已存在，不动它"
fi

echo "═══ 3/6 存 LLM 密钥 ═══"
KEY="${SPK_KEY:-}"
if [ -z "$KEY" ] && [ ! -s /etc/spkbrain/deepseek.key ]; then
    read -rsp "把云端 LLM 的密钥粘进来（回车确认，不回显）: " KEY; echo
fi
if [ -n "$KEY" ]; then
    printf '%s\n' "$KEY" > /etc/spkbrain/deepseek.key
fi
[ -s /etc/spkbrain/deepseek.key ] || { echo "✗ 没有密钥，装不下去"; exit 1; }
chmod 600 /etc/spkbrain/deepseek.key

echo "═══ 4/6 装程序 ═══"
install -d /opt/spkbrain
install -m 755 "$HERE/spkbrain.py" /opt/spkbrain/spkbrain.py

echo "═══ 5/6 开热点 + 劫持 DNS ═══"
# 盒子用【网口】上网，用【2.4G 无线】开热点给音箱 —— 音箱只支持 2.4G。
# 音箱一连上这个热点，它的网关和 DNS 就都是盒子了，不碰上游路由器。
nmcli con delete spkbrain-ap >/dev/null 2>&1 || true
nmcli con add type wifi ifname "$AP_IFACE" con-name spkbrain-ap ssid "$AP_SSID"
nmcli con modify spkbrain-ap 802-11-wireless.mode ap
nmcli con modify spkbrain-ap 802-11-wireless.band bg
nmcli con modify spkbrain-ap 802-11-wireless.channel 6
nmcli con modify spkbrain-ap ipv4.method shared ipv4.addresses "$SELF_IP/24"
nmcli con modify spkbrain-ap ipv6.method disabled
nmcli con modify spkbrain-ap wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PWD"
nmcli con modify spkbrain-ap connection.autoconnect yes

# NetworkManager 的 shared 模式会自己起一个 dnsmasq 干 DHCP + DNS。
# 往这个目录塞一行，那个 dnsmasq 就会把这两个域名直接答成盒子自己 —— 不用我们另起 DNS 服务。
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/spkbrain.conf <<EOF
# 音箱的耳朵和嘴。答成盒子自己，剩下的转发给上游。
address=/vbox-asr.3.163.com/$SELF_IP
address=/vbox-tts.3.163.com/$SELF_IP
EOF

echo "═══ 6/6 装开机自启 ═══"
cat > /etc/systemd/system/spkbrain.service <<EOF
[Unit]
Description=音箱大脑（接管网易三音云音箱，接自己的云端 LLM）
After=network-online.target NetworkManager-wait-online.service bluetooth.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/spkbrain/spkbrain.py
Environment=SPK_AP_IFACE=$AP_IFACE
Environment=SPK_SELF_IP=$SELF_IP
Environment=SPK_AP_SSID=$AP_SSID
Environment=SPK_AP_PWD=$AP_PWD
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now spkbrain.service
systemctl restart spkbrain-ap 2>/dev/null || nmcli con up spkbrain-ap || true

echo
echo "════════════════════════════════════════════"
echo " 装好了。"
echo
echo " 盒子这边：网口插到你的局域网（或路由器 LAN 口），就完事了。"
echo " 音箱这边：把音箱通电。如果它还没配过网 ——"
echo "           长按音箱上的配网键，等指示灯闪，盒子的蓝牙会自己找上去。"
echo
echo " 看日志： journalctl -u spkbrain -f"
echo " 想重来： systemctl restart spkbrain spkbrain-ap"
echo "════════════════════════════════════════════"
