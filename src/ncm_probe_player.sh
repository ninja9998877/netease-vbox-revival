#!/bin/bash
# ★ 冒充 ncm-cli 的"播放器"：把 argv 原样记下来，然后立刻退出。
#   用途：把 ncm-cli 真正要喂给播放器的**音频 URL** 捞出来（不依赖逆向混淆代码）。
#   ★★ 它什么也不播 —— 本机的喇叭永远保持哑（主人铁律）。
# ★ 仓库根 = 本脚本所在目录（src/）的上一级；数据目录（log/）默认落这儿
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${SPK_DATA_DIR:-$ROOT}
{
  printf '===== %s =====\n' "$(date '+%F %T')"
  for a in "$@"; do printf '%s\n' "$a"; done
} >> "$DATA/log/ncm_probe.log" 2>&1
exit 0
