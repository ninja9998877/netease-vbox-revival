#!/usr/bin/env python3
"""看音箱真实麦克风的频谱 —— 设计滤波器之前先量，别按教科书猜。

用法：
    python3 analyze_mic.py /tmp/mictap_live.wav [起始秒] [时长秒]
"""
import sys
import wave

import numpy as np


def welch(x, fs, seg=16384, ov=0.5):
    """自己实现 Welch PSD（不引 scipy）。返回 (freqs, psd)，psd 单位 x^2/Hz。"""
    step = int(seg * (1 - ov))
    win = np.hanning(seg)
    cg = (win ** 2).mean()
    nseg = max(1, (len(x) - seg) // step + 1)
    acc = None
    for i in range(nseg):
        s = x[i * step:i * step + seg]
        if len(s) < seg:
            break
        X = np.fft.rfft(s * win)
        # ★ 别忘了 /seg。漏了它，整条谱会虚高 10*log10(seg) ≈ 42.1 dB ——
        #   2026-09-20 就漏过，害我差点把 -29 dBFS 的次声当成"比整段 RMS 还高 37 dB"。
        #   自查办法：任何单个频带的 dBFS 不该高于整段 RMS。
        P = (np.abs(X) ** 2) / (fs * seg * cg)
        if len(P) > 2:
            P[1:-1] *= 2.0            # 单边谱
        acc = P if acc is None else acc + P
    return np.fft.rfftfreq(seg, 1.0 / fs), acc / nseg


def band_db(f, P, lo, hi):
    """频带内的总功率，折算成 dBFS（x 已归一化到 ±1）。"""
    m = (f >= lo) & (f < hi)
    if not m.any():
        return -999.0
    df = f[1] - f[0]
    return 10 * np.log10(max(P[m].sum() * df, 1e-30))


def tone_db(f, P, hz, tol=3.0):
    """某个频点附近 ±tol Hz 内最响的那根线，折算成 dBFS（单根谱线，不含带宽）。"""
    m = (f >= hz - tol) & (f <= hz + tol)
    if not m.any():
        return -999.0
    df = f[1] - f[0]
    return 10 * np.log10(max(P[m].max() * df, 1e-30))


def peaks(f, P, n=14, floor_db=-120):
    df = f[1] - f[0]
    db = 10 * np.log10(np.maximum(P * df, 1e-30))
    idx = []
    for i in range(2, len(db) - 2):
        if db[i] > floor_db and db[i] >= db[i - 1] and db[i] > db[i + 1] \
                and db[i] >= db[i - 2] and db[i] > db[i + 2]:
            idx.append(i)
    idx.sort(key=lambda i: -db[i])
    return [(f[i], db[i]) for i in idx[:n]]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/mictap_live.wav'
    skip = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0

    w = wave.open(path, 'rb')
    nch, sw, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    print('文件 %s' % path)
    print('  %d 声道 / %d Hz / 每样本 %d 字节 / %d 帧 = %.1f 秒' % (
        nch, fs, sw, n, n / float(fs)))

    start = int(skip * fs)
    cnt = min(int(dur * fs), n - start)
    if cnt < fs:
        print('  文件太短，从头读')
        start, cnt = 0, n
    w.setpos(start)
    raw = w.readframes(cnt)
    w.close()
    print('  分析窗口：第 %.1f 秒起，长 %.1f 秒' % (start / float(fs), cnt / float(fs)))
    print()

    if sw == 4:
        xi = np.frombuffer(raw, dtype='<i4').reshape(-1, nch)
        x = (xi >> 8).astype(np.float64) / 8388608.0     # 24 位有效
    elif sw == 2:
        xi = np.frombuffer(raw, dtype='<i2').reshape(-1, nch)
        x = xi.astype(np.float64) / 32768.0
    else:
        print('  不认识的样本宽度 %d' % sw)
        return 1

    BANDS = [('10-45  次声/直流附近', 10, 45),
             ('45-55  市电基波', 45, 55),
             ('55-95', 55, 95),
             ('95-105 市电二次', 95, 105),
             ('105-145', 105, 145),
             ('145-155 市电三次', 145, 155),
             ('195-205 市电四次', 195, 205),
             ('250-300', 250, 300),
             ('300-3400 人声主体', 300, 3400),
             ('3400-7000 齿音/清晰度', 3400, 7000),
             ('7000-8000', 7000, 8000),
             ('8000-16000', 8000, 16000),
             ('16000-24000', 16000, 24000),
             ('24000-48000', 24000, 48000)]

    for c in range(nch):
        xc = x[:, c]
        rms = 10 * np.log10(max((xc ** 2).mean(), 1e-30))
        print('=== 声道 %d ===  RMS %.1f dBFS' % (c, rms))
        f, P = welch(xc, fs)
        for name, lo, hi in BANDS:
            print('   %-22s %8.1f dBFS' % (name, band_db(f, P, lo, hi)))
        print('   -- 单根谱线 --')
        for hz in (50, 100, 150, 200, 250, 300):
            print('   %-22s %8.1f dBFS' % ('%.0f Hz' % hz, tone_db(f, P, hz)))
        print('   -- 最响的 %d 根线 --' % 12)
        for hz, db in peaks(f, P, 12):
            print('   %9.1f Hz  %8.1f dBFS' % (hz, db))
        print()

    if nch >= 2:
        a, b = x[:, 0], x[:, 1]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na > 0 and nb > 0:
            cc = float(np.dot(a, b) / (na * nb))
            print('两声道相关系数 %.4f  （1.0=完全相同，0=完全无关）' % cc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
