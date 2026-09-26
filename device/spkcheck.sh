#!/bin/sh
# spkcheck.sh —— 音箱健康自检（一次性，由 crond 每分钟拉起）
#
# ★ 存在的理由：muxguard/nightmute/netguard 都是【常驻进程】，崩了就没人管，
#   而本机每次开机混音器默认就是全哑档 [1] ⇒ 守卫一死 = 永久哑、且不报错。
#   cron 是设备开机流程按点叫的，跟 run.sh 和任何常驻进程都无关 ⇒ 崩了下一分钟照样来。
#
# 只读为主；只有在发现不对时才动手（改混音器 / 重启守护）。不出声。
# 日志只在【有问题时】写（NAND 友好）。
L=/mnt/UDISK/spk/spkcheck.log
D=/mnt/UDISK/spk
SSD=/sbin/start-stop-daemon

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$L"; }

# 日志别长蘑菇：超 100KB 只留最后 200 行
if [ -f "$L" ] && [ "$(wc -c < "$L" 2>/dev/null || echo 0)" -gt 102400 ]; then
  tail -200 "$L" > "$L.t" 2>/dev/null && mv "$L.t" "$L"
fi

# ★★ 判活必须【精确】：取 /proc/<pid>/cmdline 的【最后一个字段】，要求它正好等于脚本绝对路径。
#    绝不能用"cmdline 里包含 xxx.sh"—— 任何恰好提到这个名字的进程都会被当成"活着"：
#    grep、tail 日志、尤其【外层 adb shell 会把整条命令串塞进自己的 cmdline】。
#    后果是"该重启的没重启，而且一句报错都没有"，正是这个项目最怕的那种坏法（已实测踩中）。
#    ★ 定义在这儿（不在第 3 节）是因为第 2 节就要用它判 nightmute 活没活。
alive() {
  for f in /proc/[0-9]*/cmdline; do
    last=$( { tr '\0' '\n' < "$f"; } 2>/dev/null | tail -1 )
    [ "$last" = "$1" ] && return 0
  done
  return 1
}

# ---- 0) 把设备【自己的声音】全部顶成静音 ----
# ★ 实现在 $D/voicemute.sh —— 因为 run.sh【开机时也要调一次】（S95 跑在语音服务 S109 之前，
#   这样开机那几声在有机会响之前就被盖住了）。共用一份，别在这儿抄第二份。
#   本行是【每分钟的自愈】：mount 不持久，重启即失效。
[ -f "$D/voicemute.sh" ] && sh "$D/voicemute.sh"

# ---- 1) 混音器：必须是 0（DACL HPL Switch）；1 = 空模拟混音器 = 全哑 ----
v=$(amixer -c 0 cget name='HP_L Mux' 2>/dev/null | sed -n 's/^ *: values=//p')
if [ -z "$v" ]; then
  log "⚠ amixer 读不到 HP_L Mux（声卡没起来？）"
elif [ "$v" != "0" ]; then
  amixer -c 0 cset name='HP_L Mux' 'DACL HPL Switch' >/dev/null 2>&1
  now=$(amixer -c 0 cget name='HP_L Mux' 2>/dev/null | sed -n 's/^ *: values=//p')
  log "★ HP_L Mux 曾是 [$v]（全哑档），已扳回（现在 [$now]）"
fi

# ---- 2) 嗓音开关：HP / PO 必须是 on ----
# ★★★ 但【夜里绝对不许碰它们】—— 这两个开关现在【就是夜间闸门本身】。
#
#   nightmute.sh 2026-09-21 重写之后，夜间禁声靠的正是把 HP/PO 扳到 off
#   （旧的 headphone volume 是纸闸门：实测挪 43dB，喇叭那头只动 0.7dB）。
#   本节原来无条件要求"必须是 on" ⇒ 跟 nightmute 正面打架：
#   nightmute 每 20 秒守一次、本节每 60 秒扳回来
#   ⇒ ★ **夜里每分钟最多有 20 秒闸门是开着的**（实测 21:07:00 日志留了痕：
#     `★ Headphone Switch 曾是 [off]，已打开`）。
#   "夜里绝对不出声"是主人的硬要求，这个开口不能留。
#
#   判据读 nightmute 自己写的 STATE，**不在这儿重算一遍时间** ——
#   时段只能有一个真话来源；算两遍，迟早算出两个答案。
#
#   ★ 但 STATE 只在【nightmute 活着】时才可信：它要是死了，STATE 可能永远停在 "night"
#   ⇒ 白天也永远哑 —— 正是本项目最怕的那种静默瘫痪。所以先核实它在不在跑。
#   （它真死了的话，第 3 节这一分钟就会把它拉起来，闸门随之恢复。）
night_now=0
if alive "$D/nightmute.sh" && [ "$(cat "$D/nightmute.state" 2>/dev/null)" = "night" ]; then
  night_now=1
fi
if [ "$night_now" = 1 ]; then
  : # 夜间：闸门归 nightmute 管，本节一个字都不动
else
  for sw in 'Headphone Switch' 'Phoneout Switch'; do
    s=$(amixer -c 0 cget name="$sw" 2>/dev/null | sed -n 's/^ *: values=//p')
    if [ -n "$s" ] && [ "$s" != "on" ]; then
      amixer -c 0 cset name="$sw" on >/dev/null 2>&1
      log "★ $sw 曾是 [$s]，已打开"
    fi
  done
fi

# ---- 3) 守护保活 ----
# ★ alive() 定义在文件开头（第 1/2 节都要用），别在这儿再写一份。

if ! alive "$D/run.sh"; then
  log "★★ run.sh 不在跑 ⇒ 重启它"
  $SSD -S -b -x "$D/run.sh" >/dev/null 2>&1
  sleep 3
fi

# ★ ackrotate.sh 2026-09-21 晚加进来：它原来【不在这张名单里】，死了没人管。
#   而它死了从现象上完全看不出来（应答只是不再随机，仍然是能响的那一句）
#   —— 正是本项目最怕的静默瘫痪。它的锁同一晚也改成了崩溃安全（见 ackrotate.sh），
#   否则加进来会变成"每分钟重启、每次都因陈旧锁立刻退出"的自锁。
# ★ spk_net.sh 2026-09-22 上午加进来 —— 跟 ackrotate 同一个病，但更隐蔽：
#   它是**设备侧那条无线命令通道**（每 2 秒来问 macmini:8898「有活儿吗」），
#   可它**从来没被部署到设备上**：macmini 的 device/ 里有源文件、8899/8898 也都在监听，
#   唯独设备这边连脚本都没有 ⇒ 后果和"死了没人管"一模一样。
#   09-21 17:32 之后我们**再也读不到设备状态**，夜里音箱全哑时只能靠隔空推理
#   （那次真损失见 netease-vbox-night-quiet）。
#   ★ 它比名单里别的守护更该活着：**这是我们【不插 USB】就能看设备一眼的唯一通道**，
#     而拿 adb 每次都要给音箱断电重上电。它死了 = 我们瞎了，且从现象上完全看不出来。
# ★ dacguard.sh 2026-09-22 加进来 —— 跟 muxguard 是【同一个病的两个位置】：
#   muxguard 保"左声道接在 DAC 上"（HP_L Mux 被改坏 ⇒ 全哑），
#   dacguard 保"这条路的增益底不是 0"（`DAC volume` 掉到 0 = -119.25dB ⇒ 全哑）。
#   两个都是"崩了就永久哑、而且一声不响"，所以两个都得进保活名单。
for g in muxguard.sh dacguard.sh nightmute.sh netguard.sh muxtrace.sh ackrotate.sh spk_net.sh; do
  if ! alive "$D/$g"; then
    log "★★ $g 不在跑 ⇒ 重启它"
    $SSD -S -b -x "$D/$g" >/dev/null 2>&1
  fi
done

# ---- 4) 抄答案那条线：waketap.sh（设备引擎认出的唤醒 → 告诉 macmini:9997）----
# ★ 存在理由：我们的 KWS 吃的是波束成形【之前】的原始单麦，实测真唤醒 8 次只认出 2 次，
#   而设备自己的 duilite 8 次全认出。硬件差距补不回来 ⇒ 抄它的答案（详见 waketap.sh）。
#
# ★ 它比别的守护【多一个死法】，而且是静悄悄的那种：
#   `tail -f` 跟的是 netease_voice_<pid>.log，voice 服务一重启文件名就变了，
#   而 tail 会一直跟着那个【已经被删掉的旧文件】—— 进程活着、不报错、
#   **从此再也抄不到任何唤醒**。所以除了判活，还要核对它跟的是不是最新的那个。
if ! alive "$D/waketap.sh"; then
  log "★★ waketap.sh 不在跑 ⇒ 重启它（抄不到设备引擎的唤醒了）"
  $SSD -S -b -x "$D/waketap.sh" >/dev/null 2>&1
else
  cur=$(cat /tmp/spk/waketap.cur 2>/dev/null)
  now=$(ls -t /tmp/netease_voice_*.log 2>/dev/null | head -1)
  # ★★ `[ -n "$cur" ]` 这一道不能省：waketap 启动时要先等 voice 的日志出现
  #    （开机早期可能要等一两分钟），那段时间它【活着的、但还没写 $CUR】。
  #    不加这一道的话，spkcheck 会判它"跟错文件"⇒ 每分钟杀一次 ⇒
  #    它永远走不到写 $CUR 那一步 ⇒ **永远起不来**，而且日志上每分钟一行，
  #    看起来像"一直在重试"，其实是自锁。（写完就想到的，别删。）
  if [ -n "$cur" ] && [ -n "$now" ] && [ "$cur" != "$now" ]; then
    log "★★ waketap 跟的是 [$cur]，最新日志已是 [$now] ⇒ 重启它（它自己不会重开）"
    # 先按 pid 收掉旧的，再起新的。它自带【崩溃安全】锁，陈旧锁会被新实例抢过来。
    $SSD -K -x "$D/waketap.sh" >/dev/null 2>&1
    sleep 1
    $SSD -S -b -x "$D/waketap.sh" >/dev/null 2>&1
  fi
fi

# ---- 5) 唤醒应答：设备是不是还在读【我们放应答的那个文件】 ----
# ★ 存在理由（2026-09-21 晚，白丢了一个多小时）：
#   白天的方案是改 skins.db 让 v303 指到我们自造的 spk_ack.mp3。18:31 那次设备重启时，
#   res 表里 ID=12 整行被抹掉（[1..11,13..30]），v303 悬空 ⇒ 查表落回 ROM 默认音
#   /rom/usr/share/resource/voice/S003.mp3 ⇒ 而那一声被我们自己的静音件顶着
#   ⇒ **醒了，一声不响**。整件事在日志上【一点痕迹都没有】—— 这就是本节要堵的洞。
#   现在应答放在设备实际读的那个 ROM 路径上（见 ackrotate.sh 开头），不碰库。
#   但万一设备哪天又改去读别的文件，本节负责【把那件事喊出来】。
# ★ 只在【变了】的时候写一行（拿 /tmp 里的上一次值对账）—— NAND 友好。
ROM_S003=/rom/usr/share/resource/voice/S003.mp3
cc=$(ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1)
if [ -n "$cc" ]; then
  v=$(grep -h 'Get skin key: v303' "$cc" 2>/dev/null | tail -1 | sed 's/.*val: //; s/ *coast.*//')
  if [ -n "$v" ] && [ "$v" != "$ROM_S003" ]; then
    if [ "$v" != "$(cat /tmp/spk/spkcheck.v303 2>/dev/null)" ]; then
      log "★★★ 唤醒应答改去读 [$v] 了（不再是 $ROM_S003）⇒ 很可能又哑了，见 ackrotate.sh 开头"
      echo "$v" > /tmp/spk/spkcheck.v303
    fi
  else
    rm -f /tmp/spk/spkcheck.v303
  fi
fi
