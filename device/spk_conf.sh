#!/bin/sh
# spk_conf.sh —— 设备侧集中配置。
#
# ★ 用法：需要配置的脚本，开头 `. /path/to/spk_conf.sh` 一次即可。
#
# ★ 为什么要有这个文件：
#   以前每个脚本各自写死"大脑的地址"。换一台机器跑大脑，就得挨个脚本翻。
#   现在只有一处 —— 而且那处（$SPK_DIR/spk.conf）**不进仓库**。
#
# ★ 优先级：环境变量 > $SPK_DIR/spk.conf > 这里的默认值。
#   默认值只保证"脚本能跑起来"，**不保证连得上**。真值请在设备上生成 spk.conf：
#
#       cat > /mnt/UDISK/spk/spk.conf <<'EOF'
#       SPK_SRV=192.168.1.100
#       SPK_GW=192.168.1.1
#       EOF

SPK_DIR=${SPK_DIR:-/mnt/UDISK/spk}
SPK_TMP=${SPK_TMP:-/tmp/spk}

# 读设备上的本地配置（部署时生成，不进仓库）
[ -f "$SPK_DIR/spk.conf" ] && . "$SPK_DIR/spk.conf"

# ── 跑"大脑"那台机器的地址（只写地址，不带端口）──────────────
: "${SPK_SRV:=192.168.1.100}"

# ── 端口（要和本机 config/spk.env 里的对上）─────────────────
: "${SPK_PORT_NETD:=8898}"
: "${SPK_PORT_MP3:=8899}"
: "${SPK_PORT_TTS:=8896}"
: "${SPK_PORT_MICTAP:=9998}"

# ── 网关（设备所在网段的网关，黑洞指向之类要用）──────────────
: "${SPK_GW:=192.168.1.1}"

# ── 这台音箱自己的地址（配网脚本要拿它当"连上了"的判据）──────
# 留空 = 运行时现取，取不到就跳过相关自检（比写死一个错的强）
: "${SPK_DEV_IP:=}"

# ── 兼容老变量名（存量脚本用的是这两个，host:port 形态）──────
: "${SPK_NET_SRV:=$SPK_SRV:$SPK_PORT_NETD}"
: "${SPK_PROV_MACMINI:=$SPK_SRV:$SPK_PORT_MP3}"

export SPK_DIR SPK_TMP SPK_SRV SPK_GW SPK_DEV_IP
export SPK_PORT_NETD SPK_PORT_MP3 SPK_PORT_TTS SPK_PORT_MICTAP
export SPK_NET_SRV SPK_PROV_MACMINI
