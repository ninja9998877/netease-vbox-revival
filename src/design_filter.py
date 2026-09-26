#!/usr/bin/env python3
"""给 mictap 设计并【在真录音上验证】「抗混叠低通 + 抽取 + 隔直高通」这条链。

设计依据全部来自 /tmp/mictap_live.wav 量出来的真数据，不是教科书默认值：
  · 24-48 kHz 那段超声垃圾 -71 dBFS。不滤掉就抽取到 16k 的话，它会整个
    【混叠折叠】回 0-8 kHz，比人声还高 14 dB —— 所以抗混叠低通是【必须】的，
    不是优化项。那簇 32 kHz 的谱线正是 96k 采样时钟的 1/3 分频串扰。
  · 29.3 / 46.9 / 50 Hz 几根低频强线。抽取不动它们（本来就在奈奎斯特以下），
    但它们白占 14 dB 动态范围、把 VAD 的判决面压扁，用隔直高通清掉。

产出两个：
  1. mictap_filt.h —— Q20 定点的两级系数（FIR 低通 + 4 阶巴特沃斯高通）
  2. 在真录音上跑一遍【定点】实现，打印滤波前后各频段电平，证明它真管用

重新生成：python3 design_filter.py
"""
import wave

import numpy as np

FS_IN = 96000
FS_OUT = 16000
DECIM = FS_IN // FS_OUT          # 6

# ---- 低通：通带 6.5k / 阻带 8k（= 输出奈奎斯特），Kaiser 70 dB ----
FIR_FS = FS_IN
FIR_FC = 7250.0                  # -6 dB 点，取通带边和阻带边的中点
FIR_A = 70.0                     # 阻带衰减 dB
FIR_N = 277                      # 奇数 = I 型线性相位，群延迟是整数

# ---- 高通：4 阶巴特沃斯 @150 Hz（两级 biquad 串联）----
HP_FS = FS_OUT
HP_FC = 150.0
HP_QS = (0.541196100146197, 1.306562964876377)   # 4 阶巴特沃斯的两级 Q

Q = 20                           # 系数定点位数
ONE = 1 << Q


def design_fir():
    beta = 0.1102 * (FIR_A - 8.7)
    n = np.arange(FIR_N)
    w = np.kaiser(FIR_N, beta)
    h = (2.0 * FIR_FC / FIR_FS) * np.sinc(2.0 * FIR_FC / FIR_FS * (n - (FIR_N - 1) / 2.0)) * w
    h /= h.sum()                 # 直流增益归一到 1
    return h


def design_hp():
    K = np.tan(np.pi * HP_FC / HP_FS)
    out = []
    for q in HP_QS:
        norm = 1.0 / (1.0 + K / q + K * K)
        out.append((norm, -2.0 * norm, norm,
                    2.0 * norm * (K * K - 1.0),
                    norm * (1.0 - K / q + K * K)))
    return out


def quant(a):
    return np.round(np.asarray(a, dtype=np.float64) * ONE).astype(np.int64)


def resp_fir(h, f):
    w = 2.0 * np.pi * np.atleast_1d(np.asarray(f, dtype=np.float64)) / FIR_FS
    return np.abs(np.polyval(np.asarray(h)[::-1], np.exp(-1j * w)))


def resp_hp(stages, f):
    w = 2.0 * np.pi * np.atleast_1d(np.asarray(f, dtype=np.float64)) / HP_FS
    z = np.exp(-1j * w)
    H = np.ones_like(z)
    for b0, b1, b2, a1, a2 in stages:
        H *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return np.abs(H)


def db(x):
    return 20.0 * np.log10(np.maximum(np.asarray(x, dtype=np.float64), 1e-30))


# ------------------------------------------------------------------ 定点仿真
def run_fixed(x_int, h_q, stages_q, decim=DECIM):
    """完全照 mictap.c 里那套整数流程走：int64 累加、Q20 移位、四舍五入。

    x_int: int64 数组，24 位样本（±2^23），形状 (n, ch)
    """
    n, ch = x_int.shape
    fir = np.zeros((n // decim, ch), dtype=np.int64)
    hp = np.zeros_like(fir)
    for c in range(ch):
        xc = x_int[:, c]
        # --- FIR：每个输出点取最近 FIR_N 个样本做点积 ---
        acc = np.zeros(n // decim, dtype=np.int64)
        for k in range(len(h_q)):
            # y[m] 用 x[m*decim + decim-1 - k]，开头的负索引当 0（延迟线初始为 0）
            idx = np.arange(n // decim) * decim + (decim - 1) - k
            acc += h_q[k] * np.where(idx >= 0, xc[np.maximum(idx, 0)], 0)
        fir[:, c] = (acc + (1 << (Q - 1))) >> Q
        # --- 两级 biquad 串联，每级直接 I 型 ---
        u = fir[:, c]
        for b0, b1, b2, a1, a2 in stages_q:
            o = np.zeros_like(u)
            x1 = x2 = y1 = y2 = 0
            for i in range(len(u)):
                a = b0 * u[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                yi = (a + (1 << (Q - 1))) >> Q
                x2, x1 = x1, u[i]
                y2, y1 = y1, yi
                o[i] = yi
            u = o
        hp[:, c] = u
    return fir, hp


def band_db(x, fs, lo, hi):
    """x: float 数组（已归一化 ±1），算频带总功率 dBFS。"""
    seg = 16384
    if len(x) < seg:
        seg = 1 << int(np.log2(max(len(x), 2)))
    win = np.hanning(seg)
    cg = (win ** 2).mean()
    step = seg // 2
    nseg = max(1, (len(x) - seg) // step + 1)
    acc = None
    for i in range(nseg):
        s = x[i * step:i * step + seg]
        if len(s) < seg:
            break
        X = np.fft.rfft(s * win)
        P = (np.abs(X) ** 2) / (fs * seg * cg)     # ★ 别忘了 /seg，昨晚就漏了这个
        if len(P) > 2:
            P[1:-1] *= 2.0
        acc = P if acc is None else acc + P
    P = acc / nseg
    f = np.fft.rfftfreq(seg, 1.0 / fs)
    m = (f >= lo) & (f < hi)
    return 10.0 * np.log10(max(P[m].sum() * (f[1] - f[0]), 1e-30))


def main():
    h = design_fir()
    stages = design_hp()
    h_q = quant(h)
    stages_q = [tuple(quant(v) for v in s) for s in stages]

    print('==================== 设计结果 ====================')
    print('低通 FIR：N=%d 抽头（%.2f ms 群延迟 @96k），Kaiser β=%.3f，Q%d 定点'
          % (FIR_N, (FIR_N - 1) / 2.0 / FIR_FS * 1000, 0.1102 * (FIR_A - 8.7), Q))
    print('高通 IIR：4 阶巴特沃斯 @%.0f Hz（两级 Q=%.4f / %.4f），Q%d 定点'
          % (HP_FC, HP_QS[0], HP_QS[1], Q))
    print()
    print('  定点 vs 理想 的响应偏差：')
    for f in (50, 100, 150, 300, 1000, 3000, 6500, 7000, 7500, 8000, 12000, 32000):
        if f <= FS_IN // 2:
            ri, rq = resp_fir(h, f)[0], resp_fir(h_q / ONE, f)[0]
            di = 20 * np.log10(max(ri, 1e-30))
            dq = 20 * np.log10(max(rq, 1e-30))
            print('    低通 %6d Hz  理想 %8.2f dB  定点 %8.2f dB  差 %+.3f dB'
                  % (f, di, dq, dq - di))
    for f in (29.3, 46.9, 50, 100, 150, 300, 1000, 4000):
        ri = resp_hp(stages, f)[0]
        rq = resp_hp([[v / ONE for v in s] for s in stages_q], f)[0]
        print('    高通 %6.1f Hz  理想 %8.2f dB  定点 %8.2f dB  差 %+.3f dB'
              % (f, 20 * np.log10(max(ri, 1e-30)), 20 * np.log10(max(rq, 1e-30)),
                 20 * np.log10(max(rq, 1e-30)) - 20 * np.log10(max(ri, 1e-30))))
    print()

    # ---------------------------------------------------------- 真录音实测
    print('==================== 真录音实测 ====================')
    w = wave.open('/tmp/mictap_live.wav', 'rb')
    nch, sw, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    start, cnt = int(5 * fs), int(20 * fs)
    w.setpos(start)
    raw = w.readframes(min(cnt, n - start))
    w.close()
    x24 = np.frombuffer(raw, dtype='<i4').reshape(-1, nch).astype(np.int64) >> 8
    print('读入 %d 帧 × %d 声道（24 位域 ±2^23）' % x24.shape)
    print('  ch0 均值 %.1f（直流偏置）  ch1 均值 %.1f' % (x24[:, 0].mean(), x24[:, 1].mean()))
    print()

    fir, hp = run_fixed(x24, h_q, stages_q)
    print('定点链路跑完：FIR 输出 %d 帧，高通输出 %d 帧（16 kHz）' % (len(fir), len(hp)))
    print('  中间值范围：FIR ±%d，高通 ±%d（int64 里，离溢出十万八千里）'
          % (np.abs(fir).max(), np.abs(hp).max()))
    print()

    do = np.abs(hp).max() / float(2 ** 23)
    over = (np.abs(hp) > 2 ** 23).sum()
    print('  溢出检查：峰值 %.4f 满刻度，超 2^23 的样本 %d 个' % (do, over))
    print()

    src = x24[:, 0].astype(np.float64) / float(2 ** 23)
    firf = fir[:, 0].astype(np.float64) / float(2 ** 23)
    hpf = hp[:, 0].astype(np.float64) / float(2 ** 23)

    print('  ch0 各频段电平（dBFS）：')
    print('    %-22s %10s %10s %10s' % ('频段', '原始96k', '只抽取', '抽取+高通'))
    for name, lo, hi in (('10-25', 10, 25), ('25-35（29.3Hz）', 25, 35),
                         ('40-55（46.9/50）', 40, 55), ('55-100', 55, 100),
                         ('100-250', 100, 250), ('250-300', 250, 300),
                         ('300-3400 人声', 300, 3400), ('3400-7000', 3400, 7000),
                         ('7000-8000', 7000, 8000)):
        if hi > FS_OUT // 2:
            b2 = b3 = float('nan')
        else:
            b2 = band_db(firf, FS_OUT, lo, hi)
            b3 = band_db(hpf, FS_OUT, lo, hi)
        b1 = band_db(src, FS_IN, lo, hi)
        print('    %-22s %10.1f %10.1f %10.1f' % (name, b1, b2, b3))

    print()
    print('  折算回【原始 96k 域】，看抽取有没有把超声垃圾折进来：')
    print('    原始 8000-48000（超声垃圾总量）  %8.1f dBFS'
          % band_db(src, FS_IN, 8000, 48000))
    print('    抽取后 0-8000（折进来的量）      %8.1f dBFS' % band_db(firf, FS_OUT, 0, 8000))
    print('    抽取+高通后 0-8000               %8.1f dBFS' % band_db(hpf, FS_OUT, 0, 8000))
    print()
    print('  整段 RMS：原始 %.1f dBFS  →  抽取+高通 %.1f dBFS'
          % (20 * np.log10(max(np.sqrt((src ** 2).mean()), 1e-30)),
             20 * np.log10(max(np.sqrt((hpf ** 2).mean()), 1e-30))))

    # ---------------------------------------------------------- 出 C 头文件
    with open('device/mictap_filt.h', 'w') as f:
        f.write('/* 由 design_filter.py 生成 —— 不要手改，改完重跑脚本。\n'
                ' *\n'
                ' * 系数是从 /tmp/mictap_live.wav 的真录音里量出来设计的：\n'
                ' *   低通 FIR  %d 抽头，Kaiser %.0f dB，-6dB@%.0f Hz @%d Hz 采样\n'
                ' *   高通 IIR  4 阶巴特沃斯 @%.0f Hz @%d Hz 采样（两级 biquad）\n'
                ' * 全部 Q%d 定点（int32 存，int64 累加）—— 一个浮点都不用，\n'
                ' * 免得编译器偷偷引 libgcc 的 __aeabi_* 符号污染进程全局作用域。\n'
                ' */\n' % (FIR_N, FIR_A, FIR_FC, FIR_FS, HP_FC, HP_FS, Q))
        f.write('#define MT_FIRN   %d\n' % FIR_N)
        f.write('#define MT_DECIM  %d\n' % DECIM)
        f.write('#define MT_FS_OUT %d\n' % FS_OUT)
        f.write('#define MT_COEF_Q %d\n\n' % Q)
        f.write('static const int mt_fir_q[MT_FIRN] = {\n')
        for i in range(0, FIR_N, 8):
            f.write('    ' + ', '.join('%9d' % v for v in h_q[i:i + 8]) + ',\n')
        f.write('};\n\n')
        f.write('/* 每级 biquad：b0 b1 b2 a1 a2（都是 Q%d 定点）*/\n' % Q)
        f.write('static const int mt_hp_q[2][5] = {\n')
        for s in stages_q:
            f.write('    { ' + ', '.join('%9d' % v for v in s) + ' },\n')
        f.write('};\n')
    print()
    print('已写出 device/mictap_filt.h')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
