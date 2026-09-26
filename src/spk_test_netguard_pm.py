# -*- coding: utf-8 -*-
"""netguard.sh 里 pm_off() 的离线自测 —— 不碰设备、不出声、不联网。

【为什么值得单列一套】pm_off 是"修那个从没生效过的省电开关"的补丁：
`run.sh:17` 本来就有 `iwconfig wlan0 power off`，但 run.sh 跑在所有网络组件之前
⇒ 那一刻 wlan0 还不存在、报错被自己的 `2>/dev/null` 吃掉 ⇒ **静默失败**，
开机后驱动按默认值把省电打开（真因见记忆 netease-vbox-wifi-client 第九节 /
netease-vbox-boot-chain 坑④）。省电开着时包要等 beacon 周期才发
（实测 avg 64.3 → 30.9ms），对语音对话是致命的。

它跑在音箱上、每 20 秒一轮，**坏了不会有任何报错** —— 只会让省电悄悄开着。
所以把判据钉在这儿，而不是靠人记得去 iwconfig 看一眼。

★ 测的是【真文件里那一段】—— 用 awk 从 device/netguard.sh 抽出 pm_now/pm_off
  再喂假命令（PATH 前置）。改了 netguard.sh 忘了同步这里不会失真；
  反过来，函数被删/改名 ⇒ 抽不到 ⇒ 直接红。
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NETGUARD = os.path.join(HERE, 'device', 'netguard.sh')

# 假命令。iwconfig 把 PM 状态存成文件；有 refuse 文件就"死活关不掉"。
# ip 的 link show 成功与否由 ifdown 文件决定（模拟 wlan0 还没起来的开机早期）。
FAKE_IWCONFIG = '''#!/bin/sh
S="$T/pm"
if [ "$2" = power ] && [ "$3" = off ]; then
	[ -f "$T/refuse" ] && { echo on > "$S"; exit 0; }
	echo off > "$S"; exit 0
fi
echo 'wlan0     IEEE 802.11bgn  ESSID:"MyHomeWiFi"'
echo "          Power Management:$(cat "$S" 2>/dev/null || echo on)"
'''

FAKE_IP = '''#!/bin/sh
[ -f "$T/ifdown" ] && exit 1
exit 0
'''

# 场景脚本：把从真文件抽出的函数 source 进来，跑 5 组断言，逐条打 ✓/✗。
HARNESS = r'''#!/bin/sh
T=$1
# ★ 必须 export —— 假命令是【子进程】，不 export 的话它们眼里的 $T 是空的，
#   于是"状态文件"写到了 /pm（失败）⇒ 假 iwconfig 永远报 on
#   ⇒ 表面上像"pm_off 坏了"，其实是我这个自测台坏了（这个坑真踩过一次）。
export T
export PATH="$T/bin:$PATH"
IF=wlan0
LOG="$T/log"
log() { echo "$1" >> "$LOG"; }
. "$T/fn.sh"

bad=0
ck() {  # ck <说明> <实得> <期望>
	if [ "$2" = "$3" ]; then echo "  ok  $1"
	else echo "  ✗   $1  期望[$3] 实得[$2]"; bad=$((bad+1)); fi
}

# ★★ 空转闸门：确认 PATH 里生效的真是我们的假 iwconfig。
#   没有这道闸，假命令一旦没被用上（比如 PATH 写错），下面每一组都会"自洽地"
#   跑出结果、全套绿 —— 而它什么都没测。
echo on > "$T/pm"
if ! iwconfig wlan0 2>/dev/null | grep -q 'MyHomeWiFi'; then
	echo "  ✗   假 iwconfig 没生效（PATH 或 chmod 出了问题）⇒ 本套自测无效"
	exit 99
fi
if ! ip link show wlan0 >/dev/null 2>&1 && [ ! -f "$T/ifdown" ]; then
	echo "  ✗   假 ip 没生效（wlan0 竟被判成不存在）⇒ 本套自测无效"
	exit 99
fi

# ① 开机后省电是 on ⇒ 应该关掉，并记一行
rm -f "$T/refuse" "$T/ifdown"; echo on > "$T/pm"; : > "$LOG"
pm_seen=""; pm_fail=0; pm_off
ck "PM on → 关掉"           "$(cat "$T/pm")" "off"
ck "记了一行'已关掉'"        "$(grep -c '原来是 on，已关掉' "$LOG")" "1"

# ② 再跑一轮（已经 off）⇒ 不该再记
pm_off
ck "已 off 不重复记"         "$(wc -l < "$LOG")" "1"

# ③ 有人又把它打开 ⇒ 应该扳回并再记一行
echo on > "$T/pm"; pm_off
ck "漂回 on → 扳回"          "$(cat "$T/pm")" "off"
ck "又记了一行"              "$(wc -l < "$LOG")" "2"

# ④ wlan0 还没起来 ⇒ 直接跳过，不记日志也不记失败（run.sh 栽的就是这个坑）
rm -f "$T/pm"; : > "$LOG"; pm_fail=0; touch "$T/ifdown"
pm_off
ck "wlan0 不在 → 不记日志"    "$(wc -l < "$LOG")" "0"
ck "wlan0 不在 → 不记失败"    "$pm_fail" "0"
rm -f "$T/ifdown"

# ⑤ 驱动死活关不掉 ⇒ 只在第 1、16 次记账（NAND 是有限的）
: > "$LOG"; pm_fail=0; pm_seen=""; echo on > "$T/pm"; touch "$T/refuse"
i=1; while [ $i -le 16 ]; do pm_off; i=$((i+1)); done
ck "关不掉 → 只记 2 行"       "$(wc -l < "$LOG")" "2"
ck "失败计数=16"             "$pm_fail" "16"
rm -f "$T/refuse"

echo "失败 $bad 项"
exit $bad
'''


def main():
    if not os.path.isfile(NETGUARD):
        print('★ 找不到 %s' % NETGUARD)
        return 1

    t = tempfile.mkdtemp(prefix='netguard-pm-')
    try:
        os.makedirs(os.path.join(t, 'bin'))
        for name, body in (('iwconfig', FAKE_IWCONFIG), ('ip', FAKE_IP)):
            p = os.path.join(t, 'bin', name)
            with open(p, 'w') as f:
                f.write(body)
            os.chmod(p, 0o755)

        # ★ 从真文件里抽出 pm_now/pm_off —— 抽不到就说明 netguard 被改坏了，直接红
        src = open(NETGUARD).read()
        a = src.find('pm_now() {')
        b = src.find('log "启动', a)
        if a < 0 or b < 0:
            print('★ device/netguard.sh 里抽不到 pm_now/pm_off（被删了、改名了，或')
            print('  下面的锚点行变了）—— 本套自测就是在守这一段，请先核对文件。')
            return 1
        with open(os.path.join(t, 'fn.sh'), 'w') as f:
            f.write(src[a:b])

        hp = os.path.join(t, 'harness.sh')
        with open(hp, 'w') as f:
            f.write(HARNESS)
        os.chmod(hp, 0o755)

        r = subprocess.run(['/bin/sh', hp, t], capture_output=True, text=True, timeout=60)
        sys.stdout.write(r.stdout)
        if r.stderr.strip():
            sys.stdout.write('（stderr）%s\n' % r.stderr.strip())
        if r.returncode != 0:
            print('★ pm_off 有 %d 项不符' % r.returncode)
            return 1
        print('netguard 省电逻辑：全部通过（假命令，未碰设备）')
        return 0
    finally:
        shutil.rmtree(t, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
