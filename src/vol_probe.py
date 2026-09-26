#!/usr/bin/env python3
"""headphone volume 到底管不管用 —— 用自听峰值当仪表，逐档实测。

为什么非测不可（2026-09-21）：
  这台设备的音量连咬两次，而两条记忆互相打架 ——
    [[netease-vbox-night-quiet]] 说 `0 = -63dB 真静音、50 = -13dB`（即这档是真杠杆）
    [[netease-vbox-control-center]] 说「DLNA 推流完全绕过设备音量」（即这档对我们无效）
  两个不可能同时对。而"调成 20 还是最大声"这件事，只有实测能定案。

仪器就是【生产路径本身】：spk_ai_dlna 的 Ear（收 mictap 的 UDP 包，按 50ms 算 RMS）
＋ say()（SSDP → SetAVTransportURI → Play → 自听）。所以测到的是"家里人耳朵听到的那个量"，
不是寄存器里的数 —— 寄存器我们早就知道写进去了（读回来是 20）。

判据：
  固定软件增益，只动设备那一档，看麦克风收到的峰值变不变。
    变 → 这档是真杠杆（再看方向）
    不变 → 这档对 DLNA 无效（就是"影子"），音量的唯一杠杆是软件增益

★ 安全设计（刚把人吵醒过，不能再赌）：
  1. **只往小里试**。正常方向（记忆里 nightmute 那条证据）下越试越轻；
     万一方向是反的，一跳最多 +STEP dB。
  2. 每步都跟基线比，**一旦发现"变小反而更响"就立刻停手并还原** —— 那说明方向是反的，
     再往下试就是拿别人的睡眠赌。所以累积风险被掐死在单步内。
  3. 测试音频很短（~2s），最坏情况也就是一声短促。
"""
import math
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import spk_ai_dlna as D       # noqa: E402
import spk_voice as V         # noqa: E402
from _cfg import PORT_MP3     # noqa: E402

TEST = '/tmp/voltest.mp3'
TEST_TEXT = '音量测试，一二三四五。'
CTRL = os.environ.get('SPK_PROBE_CTRL', 'headphone volume')   # 换控件: SPK_PROBE_CTRL='lineout volume'
# ★★ 走【设备自己的 ioctl】而不是 ALSA：SPK_PROBE_IOCTL=1
# 2026-09-21 发现 /bin/adau1761 的 ADAU1761_CMD_VOL_* 是**另一个寄存器**：
#   ioctl 设 5 → ioctl 读 5，ALSA 仍读 20；ALSA 设 12 → ALSA 读 12，ioctl 仍读 5。
#   两者【不共享状态】(早先"都读 20"纯属巧合)。量程 **0~20**，而它正卡在 20=最大 —— 从没被碰过。
#   设备 control center 的模块表里有 volumControl(音量)/ShowVolumeLed ⇒ 大概率就是设备自己那档音量。
IOCTL = os.environ.get('SPK_PROBE_IOCTL') == '1'
if IOCTL:
    # 显示名（这个模式下读写都走 ioctl，命令行里用不到 ALSA 名字）
    CTRL = '/bin/adau1761 VOL（0~20，独立寄存器）'


# ------------------------------------------------------------------ 设备那一档
def mixer(v=None):
    """读/写设备音量。★ ALSA 取值必须认 `  : values=` 那一行（tail -1 会拿到 dBscale）。"""
    if IOCTL:
        # ★ 这个二进制【必须给两个参数】，少一个就 segfault（亲测）。
        #   读的噪声行("argv1 is cmd…"/"fd 3, cmd 1d…")之后才是 "return: N"。
        tail = "2>/dev/null | sed -n 's/^return: //p'"
        if v is None:
            cmd = '/bin/adau1761 1 0 ' + tail
        else:
            cmd = '/bin/adau1761 0 %d >/dev/null 2>&1; /bin/adau1761 1 0 %s' % (int(v), tail)
        p = subprocess.run(['adb', 'shell', cmd], capture_output=True, timeout=20)
        return p.stdout.decode().strip()
    if v is None:
        cmd = ("amixer -c 0 cget name='%s' 2>/dev/null | sed -n 's/^ *: values=//p'" % CTRL)
    else:
        cmd = ("amixer -c 0 cset name='%s' %d >/dev/null 2>&1; "
               "amixer -c 0 cget name='%s' | sed -n 's/^ *: values=//p'"
               % (CTRL, int(v), CTRL))
    p = subprocess.run(['adb', 'shell', cmd], capture_output=True, timeout=20)
    if p.returncode != 0:
        raise RuntimeError('adb 失败: %s' % p.stderr.decode('utf-8', 'replace')[:200])
    # ★ 多声道控件会回 "150,150" —— 只取第一个数。
    #   曾经这里是 `return p.stdout.decode().strip()` ⇒ `int('150,150')` 崩在【还原那一行】上，
    #   结果设备被留在最后一档（DAC volume=60，几乎没声）。**还原失败是最不能出的错。**
    raw = p.stdout.decode().strip()
    return raw.split(',')[0] if raw else raw


# ------------------------------------------------------------------ 仪表
MADE = []


class Ear(D.Ear):
    """同一个 Ear，只是把实例留个把手 —— say() 造的那个我要能从外面读到序列。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        MADE.append(self)


D.Ear = Ear          # say() 内部用 D.Ear() 创建，这里就被我们接管


def peak_of(series):
    return max([r for _, r in series] or [0.0])


def floor_peak(secs=4.0):
    """不出声时的本底峰值 —— 每个数都要跟它比，否则测的是房间不是音箱。"""
    MADE.clear()
    e = Ear()
    if not e.start():
        return None
    time.sleep(secs)
    return peak_of(e.stop())


def measure(v):
    """把设备那一档设成 v，推一次固定音频，回 (峰值, say是否成功, 实际档位)。"""
    got = mixer(v)
    time.sleep(0.3)
    MADE.clear()
    ok = D.say(TEST)
    pk = peak_of(MADE[-1].series()) if MADE else None
    return pk, ok, got


# ================================================================ 窄带模式（--nb）
#
# 为什么非换检波器不可（2026-09-21 16:30 的教训）：
#   宽带 RMS 分不清"音箱的声音"和"屋里的声音"。测试音走生产路径、带着 -28dB 软件增益，
#   本身只有 ~0.0107；屋里有人走动说话时本底能到 0.0094 —— 【信噪比只剩 1.1 倍】。
#   同一档连测四次得到 0.0049 / 0.0107 / 0.0275（差 15dB），全是废数。
#   ★★ 更坏的后果：房间一吵，【真杠杆会表现得像影子】—— 峰值被噪声顶住，怎么拧寄存器都不动。
#
# 为什么窄带能解决：
#   人声/脚步/椅子声是【宽带】的，能量摊在几千赫兹上，落进"正好 1kHz"这一个 bin 的极少；
#   而我们的纯音【100% 集中在那一个 bin】。窗口越长 bin 越窄、抑制越强。
#   ⇒ 不用把音量开大就能把信噪比拉起来（这就是"处理增益"）。
#   ★ 而且纯音比说话更不惹人烦，可以放长一点慢慢平均。
#
# 为什么用【相对】读数：设备那条 DLNA 路上可能有工厂 EQ，某个频点被压了也说不定。
#   但 EQ 是【固定】的 ⇒ 同一频点前后比，EQ 自动抵消，只留下"我们拧的那个寄存器"的贡献。

NB_SECS = 8.0                                      # 纯音时长
NB_WIN = 1.0                                       # 分析窗 1 秒 ⇒ bin 宽 1Hz
NB_HOP = 0.25
NB_FREQ = float(os.environ.get('SPK_NB_FREQ', '1000'))
# ★ 纯音电平：可以比我们说话【还轻】—— 窄带的处理增益足够把它从房间里捞出来。
#   按 -40dB 估：自听约 0.002，比房间本底(0.0094)还低 14dB，但在 1kHz 那一个 bin 上
#   它比宽带噪声高几十 dB ⇒ 照样测得出来，而屋里几乎听不见。电平写进文件名，改了会自动重造。
NB_LEVEL = float(os.environ.get('SPK_NB_LEVEL', '-40'))
NB_TONE = '/tmp/nbtone%d_%s.mp3' % (int(NB_FREQ), str(NB_LEVEL).replace('-', 'm'))


def tone(path, secs=NB_SECS):
    """造一条纯音 mp3。★ 逐位固定：文件只造一次，之后每次测的都是同一条，
    否则测到的是文件的抖动不是设备的行为。"""
    if os.path.exists(path):
        return path
    subprocess.run(['ffmpeg', '-y', '-f', 'lavfi',
                    '-i', 'sine=frequency=%d:sample_rate=48000:duration=%.2f' % (NB_FREQ, secs),
                    '-af', 'volume=%.1fdB' % NB_LEVEL,
                    '-c:a', 'libmp3lame', '-b:a', '128k', path],
                   capture_output=True, timeout=60)
    return path


def push(path):
    """只推流，不管判据 —— 窄带模式自己就是判据（1kHz 有没有能量）。
    照抄 say() 的那几步，但不做自听判断、不写留证 wav、不重试。"""
    base, udn = D.discover()
    if not base:
        raise RuntimeError('SSDP 没应答')
    D.soap(base, udn, 'RenderingControl', D.RC, 'SetVolume',
           '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>100</DesiredVolume>')
    uri = 'http://%s:%d/%s' % (D.OUR_IP, PORT_MP3, os.path.basename(path))
    didl = ('&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
            '&lt;item id="1" parentID="0" restricted="1"&gt;&lt;dc:title&gt;NB&lt;/dc:title&gt;'
            '&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
            '&lt;res protocolInfo="http-get:*:audio/mpeg:*"&gt;%s&lt;/res&gt;'
            '&lt;/item&gt;&lt;/DIDL-Lite&gt;' % uri)
    D.soap(base, udn, 'AVTransport', D.AV, 'SetAVTransportURI',
           '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>'
           '<CurrentURIMetaData>%s</CurrentURIMetaData>' % (uri, didl))
    D.soap(base, udn, 'AVTransport', D.AV, 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
    return base


def _nb_pcm(ear):
    """把 Ear 收到的原始包拼成 (float 数组[N,ch], 采样率)。"""
    import numpy as np
    if not ear._raw or not ear.fmt:
        return None, None
    rate, ch, fmt = ear.fmt
    w = D._mit.FMT_WIDTH.get(fmt, 2)
    buf = b''.join(ear._raw)
    n = len(buf) // w * w
    dt = {1: np.int8, 2: np.int16, 4: np.int32}.get(w)
    if dt is None or n == 0:
        return None, None
    a = np.frombuffer(buf[:n], dtype=dt).astype(np.float64)
    a /= float(1 << (8 * w - 1))                     # 按满量程归一，跟宽带那套对齐
    if ch > 1:
        a = a[:len(a) // ch * ch].reshape(-1, ch)
    else:
        a = a.reshape(-1, 1)
    return a, rate


def _goertzel_band(x, fs, f, half=5.0):
    """f 附近 ±half Hz 内最大的单频点幅度。
    ★ 不钉死一个 bin：设备可能 48k→16k 重采样，纯音落在哪个 bin 由它决定 ——
      钉死单个 bin 会在漂移时读出一个【假的衰减】。"""
    import numpy as np
    n = len(x)
    if n < 256:
        return 0.0
    sp = np.abs(np.fft.rfft(x * np.hanning(n)))
    df = fs / n
    lo = max(1, int((f - half) / df))
    hi = min(len(sp), int((f + half) / df) + 1)
    if hi <= lo:
        return 0.0
    return float(sp[lo:hi].max()) * 2.0 / n


def nb_analyze(ear):
    """回 (窄带幅度, 宽带RMS, 说明)。两个都算，为的是把"窄带稳、宽带飘"直接摆在眼前。"""
    import numpy as np
    a, fs = _nb_pcm(ear)
    if a is None:
        return None, None, '没收到包（mictap 没在推？）'
    win = int(NB_WIN * fs)
    hop = max(1, int(NB_HOP * fs))
    if len(a) < win:
        return None, None, '只收到 %.1fs，不足一个分析窗' % (len(a) / fs)
    nbs, bbs = [], []
    for i in range(0, len(a) - win + 1, hop):
        seg = a[i:i + win]
        # 两个声道各算一遍取大的 —— 这台音箱单声道认左路（见 HP_L Mux 那次事故），
        # 但输出具体落在哪个声道不该由探针假设
        nbs.append(max(_goertzel_band(seg[:, c] - seg[:, c].mean(), fs, NB_FREQ)
                       for c in range(seg.shape[1])))
        bbs.append(float(np.sqrt((seg ** 2).mean())))
    # ★ 取"最强的那些窗"的中位数，不取全局最大：最大会被一次瞬态污染，
    #   而纯音是【稳态】的，前 10% 的窗本来就都在满值上。
    k = max(1, len(nbs) // 10)
    nb = float(np.median(sorted(nbs)[-k:]))
    bb = float(np.sqrt((np.array(bbs) ** 2).mean()))
    return nb, bb, '窗 %d 个' % len(nbs)


def nb_measure(v):
    """把设备那一档设成 v，推一次纯音，回 (窄带幅度, 宽带RMS, 说明, 实际档位)。"""
    got = mixer(v)
    time.sleep(0.3)
    e = Ear()
    if not e.start():
        return None, None, '耳朵起不来: %s' % e.err, got
    try:
        push(NB_TONE)
    except Exception as ex:                      # noqa: BLE001 —— 推不出去要如实报，不能装作测了
        e.stop()
        return None, None, '推流失败: %s' % ex, got
    time.sleep(NB_SECS + 1.2)                    # 放完再多收一点，免得尾巴被切
    e.stop()
    nb, bb, why = nb_analyze(e)
    return nb, bb, why, got


# ══════════════════════════════════════════════════════════════════════════
# --ack：验「叮」—— 设备【自己】的唤醒应答音，能不能被 DAC volume 压住
# ══════════════════════════════════════════════════════════════════════════
# 这是夜间静音唯一还没验的一环：DLNA 推流被 DAC volume 管住【已经实测过了】，
# 但"设备自己的声音跟推流走同一条 pcm0p 流"至今只是【推断】，没测过。
#
# 为什么不能用窄带法：叮是 ~1.25 秒的瞬态，不是稳态纯音，单频点积分无从谈起。
# 为什么宽带就够：要分辨的是 150 vs 110（相差 27dB），而宽带重复性是 ±4dB
#   —— 27dB 的差距远在噪声之上，用不着精密方法。
#
# ★★ 为什么【第一档必须测 150】：150 是最大档。若连最大档都听不到叮，那结论
#   只能是"我们的耳朵听不见它"（比如被 AEC 吃掉了），【绝不能】读成"DAC volume
#   把它压住了"。先测最大档，这一步当场把两种解释分开。
#
# 时序（实测得来，别改）：重启后 mictap 要约 1 秒（100 次取数）才肯载入注入
# 文件，注入本身 1.000 秒，之后 inj_on 归零、上行流【自动切回真麦克风】
# —— 叮就落在后面那个窗口里，所以包络要一路录到 9 秒。
ACK_SECS = float(os.environ.get('SPK_ACK_SECS', '12'))

# ★★★ 硬闸门（2026-09-21 主人当场叫停后加的）：测试档位绝不许超过这个值。
#   教训：--ack 的控件走 CTRL（默认 headphone volume），我第一次传了 150，
#   等于把放大器档从 20 拉到 150 —— 那声应答音被放大 31 档，把主人吵到了。
#   "默认值不等于安全值"：150 对 DAC volume 是正常档，对 headphone volume
#   却是越界值。所以闸门不能按"哪个控件"来定，只能按【数字】定死。
ACK_MAX = int(os.environ.get('SPK_ACK_MAX', '100'))


DEV_INJ = '/tmp/mictap_inject.pcm'
DEV_HOLD = '/tmp/mictap_inject.hold'

# ★ 重启后等引擎就绪再放注入文件。实测（2026-09-21 16:44）：
#   26.971 fespa new success → 27.071 Vad new success → 27.130 Record start
#   → 28.153 fespa_set(words=...) —— 到这行引擎才真正开始判别。
ACK_WARMUP = float(os.environ.get('SPK_ACK_WARMUP', '7'))


def _dev(cmd, timeout=25):
    return subprocess.run(['adb', 'shell', cmd], capture_output=True, timeout=timeout)


def ack_trigger_pre():
    """把注入文件【挪走】再重启 —— 这样新进程读不到它，inj_try_load() 会一直重试。"""
    _dev('mv %s %s 2>/dev/null; true' % (DEV_INJ, DEV_HOLD))
    # ★★ 用 init.d restart（不是 killall）：脚本已确认 start_service() 里 procd 只
    #   open 了 voice 一个 instance ⇒ 只重启 voice 一个进程，不碰 player/控制中心。
    #   init_dbus() 只在 dbus 真死了才 `reboot -f`，而 dbus 一直活着 ⇒ 安全。
    #   而 killall 走的是 procd respawn，不重新执行 start_service()，行为不等价
    #   —— 2026-09-21 16:44 那次 1 秒注入没唤醒，怀疑与此有关。
    return _dev('/etc/init.d/netease_voice_service restart', timeout=40)


def ack_trigger_post():
    """引擎就绪了，把注入文件放回去 ⇒ 下一个 100 次取数（约 1.6 秒）内载入播放。"""
    return _dev('mv %s %s' % (DEV_HOLD, DEV_INJ))


def ack_analyze(ear):
    """回 {'env','pk','floor','at','secs'}：50ms 一格的能量包络。
    叮是瞬态 ⇒ 必须看它【在第几秒、多大】，一个平均值什么也说明不了。"""
    import numpy as np
    a, fs = _nb_pcm(ear)
    if a is None:
        return None
    step = max(1, int(0.05 * fs))
    n = len(a) // step
    if n < 6:
        return None
    env = np.sqrt((a[:n * step].reshape(n, step, -1) ** 2).mean(axis=(1, 2)))
    head = max(4, n // 12)                 # 头 1/12 当本底：那时注入还没开始
    return {'env': env, 'pk': float(env.max()), 'at': float(env.argmax()) * 0.05,
            'floor': float(np.median(env[:head])), 'secs': n * 0.05}


def ack_measure(v):
    """设成 v → 起耳朵 → 触发一次注入唤醒 → 录满 → 回包络分析。"""
    got = mixer(v)
    time.sleep(0.3)
    e = Ear()
    if not e.start():
        return None, '耳朵起不来（9998 被占？先把 spk-ear 停了）: %s' % e.err, got
    try:
        time.sleep(0.5)                    # 先存一点本底，供包络自比
        ack_trigger_pre()                  # 挪走文件 + 重启 voice 单进程
        time.sleep(ACK_WARMUP)             # 等引擎真正开始判别（fespa_set 之后）
        r = ack_trigger_post()             # 文件放回去
        if r.returncode != 0:
            return None, '文件没放回去: %s' % r.stderr.decode('utf-8', 'replace')[:120], got
        time.sleep(ACK_SECS)               # 载入 ~1.6s + 注入 1s + 叮 + 余量
    finally:
        e.stop()
    return ack_analyze(e), '', got


def main(argv):
    # ── --ack：注入唤醒词，量设备【自己】那声「叮」────────────────────────
    if argv and argv[0] == '--ack':
        orig = mixer()
        vals = [int(a) for a in argv[1:]] or [150, 110]
        print('① 验「叮」：把唤醒词直接喂进设备的 KWS，量它自己那声唤醒应答')
        print('   这是注入法，不是放给麦克风听 ⇒ 屋里没人也能跑')
        print('   设备当前 %s = %s' % (CTRL, orig))
        print('   ⚠ 跑之前 spk-ear 必须【先停】：它绑着 9998，会抢答、会污染窗口')
        rows = []
        for v in vals:
            if v > ACK_MAX:
                print('\n② %s → %s  ★★ 超过测试上限 %s，拒绝执行' % (CTRL, v, ACK_MAX))
                print('   主人定的规矩：测试时声音不许超过 %s 档。' % ACK_MAX)
                continue
            print('\n② %s → %s，触发中…（挪文件 → 重启 → 等引擎就绪 → 放回）' % (CTRL, v))
            r, why, got = ack_measure(v)
            if r is None:
                print('   → 测不到（%s）' % why)
                continue
            rows.append((v, r))
            ratio = ('%.1fx 本底' % (r['pk'] / r['floor'])) if r['floor'] > 0 else '本底为 0'
            print('   本底 %.5f   峰值 %.5f @ %.2fs   (%s)'
                  % (r['floor'], r['pk'], r['at'], ratio))
            print('   --- 包络 0 → %.1fs，每格 0.5s，满格 = 本段峰值 ---' % r['secs'])
            env = r['env']
            for i in range(0, len(env), 10):
                m = float(env[i:i + 10].max())
                bar = '#' * min(50, int(m / r['pk'] * 50)) if r['pk'] > 0 else ''
                print('   %5.2fs %.5f %s' % (i * 0.05, m, bar))
        print('\n③ 还原到原值 %s' % orig)
        print('   %s = %s' % (CTRL, mixer(orig)))
        if len(rows) == 2:
            (v0, r0), (v1, r1) = rows
            if r0['pk'] <= 1e-5:
                print('④ 结论：最大档（%s）都收不到东西 ⇒ 是"探针不灵"，')
                print('   不是"DAC volume 把它压住了" —— 两者必须分开，别混读。' % v0)
            else:
                dB = 20 * math.log10(max(r1['pk'], 1e-12) / r0['pk'])
                print('④ 结论：%s→%s 实测 %+.1f dB（预测 -27.3dB）' % (v0, v1, dB))
        elif len(rows) == 1:
            v0, r0 = rows[0]
            if r0['pk'] <= 1e-5:
                print('④ 结论：最大档（%s）收不到东西 ⇒ 探针不灵，先别下任何结论' % v0)
            else:
                print('④ 单档就跑到了叮 ⇒ 探针有效。把另一档也跑一遍才能定论。')
        return 0

    # ── --nb：窄带模式（纯音 + 单频点）。房间里有人也能测，不用加大音量。 ────────
    if argv and argv[0] == '--nb':
        orig = mixer()
        tone(NB_TONE)
        vals = [int(a) for a in argv[1:]] or [150, 130, 110]
        print('① 窄带探针：%.0fHz 纯音 %.1fs ⇒ 只在 %.0fHz±5Hz 这一个频点上量能量'
              % (NB_FREQ, NB_SECS, NB_FREQ))
        print('   测试音 %s（固定文件，逐位不变；直接推，不过 tts ⇒ 软件增益不影响它）' % NB_TONE)
        print('   设备当前 %s = %s' % (CTRL, orig))
        print('② 逐档实测，档位序列 %s' % vals)
        base = None
        rows = []
        for v in vals:
            nb, bb, why, got = nb_measure(v)
            if nb is None or nb <= 0:
                print('   %-4s → 测不到（%s），跳过' % (v, why))
                continue
            if base is None:
                base = nb
            dB = 20 * math.log10(nb / base) if base > 0 else 0.0
            rows.append((v, nb, bb, dB))
            print('   %-4s (读回 %s) → 窄带 %.3e  %+6.1f dB   |  宽带 %.5f   [%s]'
                  % (v, got, nb, dB, bb, why))
            if nb > base * 1.4:
                print('   ★★ 变小反而更响（%+.1f dB）⇒ 方向是反的，立刻停手还原' % dB)
                break
        print('③ 还原到原值 %s' % orig)
        print('   %s = %s' % (CTRL, mixer(orig)))
        if rows:
            print('④ 结论：档位 vs 窄带幅度（以 %s 为 0dB 基准）' % rows[0][0])
            for v, nb, bb, dB in rows:
                print('   %-4s  %+6.1f dB' % (v, dB))
            print('   ★ 看窄带那一列：房间吵的时候宽带那列会飘，窄带不该飘')
        return 0

    # ★ 先记住原值 —— 收尾必须还原到【原值】而不是写死某个数。扫不同控件时
    #   "还原成 20" 会把 lineout volume 这种量程到 31 的控件设成乱七八糟的值。
    orig = mixer()
    # ── --hold V：设成 V，推一次音频，【全程每 0.4s 采样寄存器】 ──────────────
    # 为什么单列一个模式：上面那个逐档扫描只能得出"没变"，但**没变**有两个完全不同的
    # 解释 —— ①这档真被旁路了 ②设备在播放开始时把它重写了、我们设的值压根没生效。
    # 记忆里有一条旁证支持 ②：功放 mute「设备一开始播放就自己解掉，按不住」。
    # 分辨办法就是把寄存器在播放过程中的轨迹录下来：值被改回去 = ②，值一直是我们设的 = ①。
    if argv and argv[0] == '--hold':
        want = int(argv[1]) if len(argv) > 1 else 0
        if not os.path.exists(TEST):
            D.tts(TEST_TEXT, TEST)
        print('设定 %s = %d   写回 %s' % (CTRL, want, mixer(want)))
        MADE.clear()
        stop = []

        def sample():
            while not stop:
                print('   [%5.1fs] %s = %s' % (time.time() - t0, CTRL, mixer()))
                time.sleep(0.4)

        t0 = time.time()
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        ok = D.say(TEST)
        # 播放后再多盯 3 秒 —— 有些实现是"播完才恢复"，那也一样是它说了算
        time.sleep(3.0)
        stop.append(1)
        th.join(timeout=2)
        print('峰值 %s   念了=%s' % (
            '%.5f' % peak_of(MADE[-1].series()) if MADE else '无', ok))
        print('最终 %s = %s' % (CTRL, mixer()))
        print('还原 %s → %s' % (orig, mixer(orig)))
        return 0

    if not os.path.exists(TEST):
        print('① 造一条固定的测试音频（走生产路径 tts + 当前软件增益）')
        D.tts(TEST_TEXT, TEST)
    print('   测试音频 %s' % TEST)

    g = V.gain_db()
    print('② 当前软件增益 %.1f dB（全程不动它）' % g)
    print('   设备当前 %s = %s' % (CTRL, mixer()))

    print('③ 本底（不出声）…')
    fl = floor_peak()
    print('   本底峰值 %s' % ('%.5f' % fl if fl else '测不到'))
    if not fl:
        print('✗ 仪表起不来，先查 mictap/端口，别往下测')
        return 1

    vals = [int(a) for a in argv] or [20, 10, 0]
    print('④ 逐档实测，档位序列 %s' % vals)
    base = None
    rows = []
    for v in vals:
        pk, ok, got = measure(v)
        if pk is None:
            print('   %-3s → 没收到麦克风包，跳过' % v)
            continue
        if base is None:
            base = pk
            # ★★ 信噪闸门：测试音走生产路径，而生产路径带着软件增益（现役 -28dB）⇒
            #   测试音本身很轻。一旦房间有人走动/说话，本底就能追平它 ——
            #   2026-09-21 16:30 实测吃过这个亏：本底 0.0094 vs 测试音 0.0107（只差 1.1dB），
            #   同一档连测四次得到 0.0049/0.0107/0.0275，全是废数。
            #   ⇒ 信噪比不够就【当场停手】，宁可不出结论，也别把房间噪声当成设备行为。
            if fl and pk / fl < 6.0:
                print('   ★★ 信噪比只有 %.1f 倍（测试音 %.5f / 本底 %.5f）——房间太吵，'
                      '这次测不出东西，别拿它下结论。' % (pk / fl, pk, fl))
                print('   ⇒ 等屋里安静了再跑，或临时把软件增益调高一点再测。')
                break
        dB = 20 * math.log10(pk / base) if (base and pk > 0) else 0.0
        rows.append((v, got, pk, ok, dB))
        print('   %-3s (读回 %s) → 峰值 %.5f  %+6.1f dB   %s'
              % (v, got, pk, dB, '念了' if ok else '★没念'))
        # ★ 一发现"往小里调反而更响"就停手 —— 方向是反的，再试就是赌
        if pk > base * 1.4:
            print('   ★★ 变小反而更响（%+.1f dB）⇒ 方向是反的，立刻停手还原' % dB)
            break

    print('⑤ 还原到原值 %s' % orig)
    print('   %s = %s' % (CTRL, mixer(orig)))
    if rows:
        print('⑥ 结论：档位 vs 峰值（以 %s 为 0 dB 基准）' % rows[0][0])
        for v, got, pk, ok, dB in rows:
            print('   %-3s  %+6.1f dB   峰值 %.5f' % (v, dB, pk))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
