#!/bin/bash
# 蹲全志 FEL（1f3a:efe8）—— 它只活几秒，人工反应不过来，所以自动抓。
# 只做只读动作：version（BROM 版本 + SoC 型号）、sid（芯片 ID）。绝不 write / exec。
# 用法：bash fel_catch.sh        （Ctrl-C 退出）
set -u
while true; do
  if lsusb -d 1f3a:efe8 >/dev/null 2>&1; then
    echo "[$(date +%T)] ★★★ FEL 出现了 —— 立刻握手"
    sunxi-fel version 2>&1 | sed 's/^/      /'
    sunxi-fel sid     2>&1 | sed 's/^/      /'
    echo "[$(date +%T)] ★★★ 握手完成（FEL 一般还挂着，先别松手）"
    sleep 15          # 别疯狂重试刷屏
  fi
  sleep 0.2
done
