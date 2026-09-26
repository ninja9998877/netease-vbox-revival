#!/usr/bin/env python3
"""probe_mic.py —— 不打断录制的前提下，看音箱麦克风【此刻】听到了什么。

★ 为什么不能直接用 wave 模块读正在写的那个 wav：
  mictap_sink 是边收边写的，WAV 头里的长度字段要到 close 才回填。
  录制中途用 wave.open() 读会拿到 0 帧（或旧值），看着像"没在录"。
  所以这里【绕开头部】—— 直接按 44 字节偏移切 raw PCM。格式是已知的：
  16 kHz / 2ch / S16_LE（mictap 的抽取输出）。头部 44 字节是标准 RIFF 头。

★ 它还顺手解决另一件事：**把"文件里的第 t 秒"换算成"墙上几点几分"**。
  拿文件 mtime 当"最后写入时刻"，往前倒推即可（误差 = 内核 page cache 的
  落盘延迟，通常 < 1 秒，对我们判断"哪个声音对哪个动作"完全够）。
  否则录了 2 小时之后，没人知道第 4213 秒到底是几点。

用法:
  python3 probe_mic.py                # 最新那段的最后 20 秒，每 0.5 秒一格
  python3 probe_mic.py --secs 60      # 看最后 60 秒
  python3 probe_mic.py --freq         # 每格再算主频（找我们自己放的那个音用）
  python3 probe_mic.py --file X.wav   # 指定文件
"""
import glob
import os
import sys
import time

import numpy as np

SR = 16000
HDR = 44                      # 标准 RIFF/WAVE 头长度（mictap_sink 写的就是标准头）


def newest_mic():
    d = open('/tmp/spk_capture_dir').read().strip()
    fs = sorted(glob.glob(os.path.join(d, 'mic_*.wav')))
    if not fs:
        raise SystemExit('✗ %s 下没有 mic_*.wav' % d)
    return fs[-1], d


def read_raw_tail(path, secs):
    """绕开 WAV 头，直接读文件尾部的 raw PCM。★ 文件正在被写也读得到。

    返回 (x 单声道 float32, 这一段的起始墙钟时刻, 文件总时长)
    """
    size = os.path.getsize(path)
    nbytes = int(secs * SR * 2 * 2)                 # 2ch × 2byte
    off = max(HDR, size - nbytes)
    # 对齐到 4 字节（一帧 = 2ch × 2byte），否则左右声道会串
    off -= (off - HDR) % 4
    with open(path, 'rb') as f:
        f.seek(off)
        raw = f.read()
    raw = raw[:len(raw) - len(raw) % 4]
    x = np.frombuffer(raw, dtype='<i2').astype(np.float32)
    x = x.reshape(-1, 2).mean(axis=1) / 32768.0     # 2ch → 单声道
    total = max(0.0, (size - HDR) / 4.0 / SR)       # 文件总共录了多久
    t_end = os.path.getmtime(path)
    t_start = t_end - len(x) / SR
    return x, t_start, total


def peak_freq(x, sr=SR):
    if len(x) < 128:
        return 0.0
    w = x * np.hanning(len(x))
    S = np.abs(np.fft.rfft(w)) ** 2
    fr = np.fft.rfftfreq(len(w), 1.0 / sr)
    m = (fr > 60) & (fr < 5000)
    if not m.any() or S[m].sum() <= 0:
        return 0.0
    return float(fr[m][int(np.argmax(S[m]))])


def main():
    a = sys.argv[1:]
    secs = 20.0
    show_freq = '--freq' in a
    path = None
    if '--file' in a:
        path = a[a.index('--file') + 1]
    if '--secs' in a:
        secs = float(a[a.index('--secs') + 1])
    if path is None:
        path, d = newest_mic()
        print('目录 %s' % d)

    x, t0, total = read_raw_tail(path, secs)
    print('文件 %s' % os.path.basename(path))
    print('  总共已录 %.1f 秒（%.1f 分钟） 本机现在 %s' % (total, total / 60, time.strftime('%H:%M:%S')))
    if not len(x):
        print('  ✗ 一个采样都没有 —— 要么还没收到流，要么偏移算错了')
        return 1

    rms_all = float(np.sqrt((x ** 2).mean()))
    print('  尾部 %.1f 秒：峰值 %.4f  RMS %.5f  %s'
          % (len(x) / SR, float(np.abs(x).max()), rms_all,
             '（接近量化底噪 3e-5 ⇒ 屋里是静的）' if rms_all < 0.0005 else '（★ 有动静）'))

    win = 0.5
    n = int(win * SR)
    print('\n  每 %.1f 秒一格（相对时刻 / 墙钟 / RMS / 主频）：' % win)
    for i in range(0, max(1, len(x) - n), n):
        seg = x[i:i + n]
        r = float(np.sqrt((seg ** 2).mean()))
        t = i / SR
        wall = time.strftime('%H:%M:%S', time.localtime(t0 + t))
        bar = '#' * min(40, int(r * 4000))
        f = ('  %5.0fHz' % peak_freq(seg)) if show_freq else ''
        flag = ' ★' if r > 0.002 else ''
        print('   %6.1fs  %s  %.5f %-40s%s%s' % (t, wall, r, bar, f, flag))
    return 0


if __name__ == '__main__':
    sys.exit(main())
