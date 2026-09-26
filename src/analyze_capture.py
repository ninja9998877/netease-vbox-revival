#!/usr/bin/env python3
"""analyze_capture.py —— 啃 capture_spk.sh 录下来的那一坨。

要回答三个问题，按重要性排：

  ① 【哪一声是"叮"】麦克风能听见音箱自己的喇叭 ⇒ 录到的"叮"和本机 69 个候选
     mp3 是同一段音频（只是过了房间和麦）。所以不用耳朵：比【时长】和
     【主频】就能对上号。候选之间的主频是分开的（S010=1392 / S009=2087 /
     S003=1319 / S006=530 …），这个判据足够硬。

  ② 【它到底有没有被打断/多轮】DBus 里的 GOAPI 有一条约 40~70 秒一次的
     基线（cmd=2580 value=14）。这条线一破，就是有人跟它说话了。
     再看 voice_engine 上有没有新的 cmd。

  ③ 【人说的话 vs 设备的反应】mic.wav 里能量高的片段 = 有人在说话的时间点，
     和 DBus 事件对齐，就看得出"它是在人说完多久之后才应答的"。

用法: python3 analyze_capture.py [目录]      （默认取 /tmp/spk_capture_dir）
"""
import collections
import glob
import json
import os
import re
import struct
import subprocess
import sys
import wave

import numpy as np

from _cfg import DATA_DIR

FFMPEG = 'ffmpeg'
DINGDIR = str(DATA_DIR / 'ding' / 'voice')
SR = 16000                      # mictap v2 抽取后的采样率
MIC_CACHE = '/tmp/_cap_mic.npy'


# ------------------------------------------------------------------ 音频
def load_wav(path):
    """读 mictap 存的 wav（16k/2ch/S16）→ 单声道 float32。

    ★ 2ch 要合成单声道：音箱的喇叭在房间里是【一个点声源】，两个麦的相位差
      对我们比主频没有帮助，反而会互相抵消（尤其低频）。平均最稳。
    """
    with wave.open(path, 'rb') as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit('  麦克风 wav 不是 16bit（sw=%d），先看看 mictap 的格式' % sw)
    x = np.frombuffer(raw, dtype='<i2').astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SR:                # 万一以后改了抽取比，重采样一次，别静默错
        idx = (np.arange(int(len(x) * SR / sr)) * sr / SR).astype(int)
        x = x[np.clip(idx, 0, len(x) - 1)]
    return x / 32768.0


def load_audio(path, sr=SR):
    """任意音频 → 单声道 float32 @sr（走 ffmpeg，mp3/wav 通吃）。"""
    p = subprocess.run([FFMPEG, '-v', 'error', '-i', path, '-ac', '1',
                        '-ar', str(sr), '-f', 's16le', '-'], capture_output=True)
    return np.frombuffer(p.stdout, dtype='<i2').astype(np.float32) / 32768.0


def peaks_of(x, sr=SR, topn=3):
    """主频（Hz）列表，按能量降序。★ 这是"这是不是同一个声音"的主判据。"""
    if len(x) < 256:
        return []
    w = x * np.hanning(len(x))
    S = np.abs(np.fft.rfft(w)) ** 2
    if S.sum() <= 0:
        return []
    freqs = np.fft.rfftfreq(len(w), 1.0 / sr)
    # 只取 80Hz~4kHz（提示音都在这个带里；低于 80 是空调/电流，高于 4k 是齿音）
    m = (freqs > 80) & (freqs < 4000)
    S, freqs = S[m], freqs[m]
    out, S = [], S.copy()
    for _ in range(topn):
        if S.max() <= 0:
            break
        i = int(np.argmax(S))
        out.append(float(freqs[i]))
        S[max(0, i - 3):i + 4] = 0      # 掐掉这个峰附近的，免得同一个峰报三遍
    return out


def sig(path):
    """一个候选提示音的指纹：(时长秒, 主频列表, 集中度)。"""
    x = load_audio(path)
    if len(x) < 128:
        return None
    w = x * np.hanning(len(x))
    S = np.abs(np.fft.rfft(w)) ** 2
    conc = float(np.sort(S)[-5:].sum() / (S.sum() or 1))
    return len(x) / SR, peaks_of(x), conc


def bursts(x, sr=SR, win=0.02, hop=0.01, rel=0.18, gap=0.25, minlen=0.05, maxlen=2.5):
    """找出"值得一看"的声音片段：能量超过背景一定倍数的连续区间。

    ★ 阈值取【相对】而非绝对：音箱音量是可变的（现在 50，夜里可能压到 0），
      绝对阈值会在音量小的时候一个事件都找不出来。
    """
    n = max(1, int(win * sr))
    fr = [x[i:i + n] for i in range(0, max(1, len(x) - n), int(hop * sr))]
    if not fr:
        return []
    rms = np.array([float(np.sqrt((f ** 2).mean() + 1e-12)) for f in fr])
    floor = float(np.percentile(rms, 20))           # 20 分位当底噪，躲开偶发噪声
    thr = max(floor * 3.0, float(np.percentile(rms, 99)) * rel)
    on = rms > thr
    out, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and (on[j] or (j + int(gap / hop) < len(on) and on[j:j + int(gap / hop)].any())):
                j += 1
            t0, t1 = i * hop, min(len(x) / sr, (j + 1) * hop)
            if minlen <= t1 - t0 <= maxlen:
                out.append((t0, t1))
            i = j
        i += 1
    return out


# ------------------------------------------------------------------ 主流程
def main():
    d = sys.argv[1] if len(sys.argv) > 1 else open('/tmp/spk_capture_dir').read().strip()
    print('目录: %s\n' % d)

    # ---------------- ① 麦克风 ----------------
    mp = os.path.join(d, 'mic.wav')
    if not os.path.exists(mp) or os.path.getsize(mp) < 1000:
        print('① ✗ 没有麦克风录音（%s）' % mp)
        x = None
    else:
        x = load_wav(mp)
        dur = len(x) / SR
        print('① 麦克风 %.1f 秒（%.1f 分钟）  峰值 %.3f  RMS %.4f'
              % (dur, dur / 60, float(np.abs(x).max()), float(np.sqrt((x ** 2).mean()))))
        ev = bursts(x)
        print('   检测到 %d 段"有动静"的区间' % len(ev))
        for t0, t1 in ev[:40]:
            seg = x[int(t0 * SR):int(t1 * SR)]
            pk = peaks_of(seg)
            print('     %6.2fs→%6.2fs (%.2fs)  主频 %s'
                  % (t0, t1, t1 - t0, ', '.join('%.0f' % p for p in pk[:2])))

    # ---------------- ② 候选提示音指纹 ----------------
    print('\n② 候选提示音指纹（本机 69 个）')
    cands = []
    for f in sorted(glob.glob(os.path.join(DINGDIR, '*.mp3'))):
        s = sig(f)
        if s:
            cands.append((os.path.basename(f), s[0], s[1], s[2]))
    print('   共 %d 个' % len(cands))

    # ---------------- ③ 把麦克风里的事件跟候选对上 ----------------
    if x is not None and cands:
        print('\n③ 对号（时长±30%% 且主频±8%% 才算）')
        ev = bursts(x)
        hits = collections.Counter()
        for t0, t1 in ev:
            seg = x[int(t0 * SR):int(t1 * SR)]
            dseg, pk = t1 - t0, peaks_of(seg)
            if not pk:
                continue
            for name, cd, cpk, cc in cands:
                if not (0.7 * cd <= dseg <= 1.35 * cd + 0.06):
                    continue
                if not cpk:
                    continue
                if abs(cpk[0] - pk[0]) <= 0.08 * cpk[0]:
                    hits[name] += 1
                    print('   %6.2fs (%.2fs/%.0fHz)  ↔  %-18s (%.2fs/%.0fHz)'
                          % (t0, dseg, pk[0], name, cd, cpk[0]))
        if hits:
            print('\n   ★ 命中统计:', ', '.join('%s×%d' % kv for kv in hits.most_common()))
        else:
            print('   （没有对上的 —— 要么这次没人唤醒它，要么提示音不在候选里）')

    # ---------------- ④ DBus：基线破没破 ----------------
    bp = os.path.join(d, 'dbus.txt')
    if os.path.exists(bp):
        raw = open(bp, encoding='utf-8', errors='replace').read()
        blocks = re.split(r'\n(?=(?:signal|method call|method return|error) time=)', raw)
        calls, notifies = collections.Counter(), collections.Counter()
        for b in blocks:
            m = re.search(r'path=(\S+?);.*?member=(\w+)', b)
            if not m:
                continue
            nums = [int(v) for v in re.findall(r'^\s+uint32 (\d+)\s*$', b, re.M)]
            if m.group(2) in ('API', 'GOAPI') and len(nums) >= 4:
                # GOAPI 前两个是 时间戳/序号，会一直变 ⇒ 只统计后两个（cmd/value）
                key = (m.group(1), tuple(nums[-2:]))
                calls[key] += 1
            elif m.group(2) == 'Notify' and len(nums) >= 3:
                notifies[tuple(nums[:3])] += 1
        print('\n④ DBus（%d 行，%d 条消息）' % (raw.count('\n'), len(blocks)))
        print('   API/GOAPI 的 (path, cmd, value) 分布：')
        for (p, t), c in calls.most_common(15):
            print('     %-30s %-18s %d 次' % (p, str(t), c))
        print('   Notify 的 (module, target, code) 分布：')
        for t, c in notifies.most_common(10):
            print('     %-34s %d 次' % (str(t), c))

    # ---------------- ⑤ 三个常驻服务的日志增量 ----------------
    for f in ('voice', 'macmini', 'spk_srv'):
        p = os.path.join(d, '%s.delta' % f)
        if not os.path.exists(p):
            continue
        txt = open(p, encoding='utf-8', errors='replace').read().strip()
        lines = [l for l in txt.split('\n') if l.strip()]
        print('\n⑤ %s.log 增量 %d 行' % (f, len(lines)))
        # 只挑"非例行"的（例行=心跳，没法看）
        interesting = [l for l in lines if '例行' not in l and '心跳 #' not in l]
        print('   其中非例行 %d 行：' % len(interesting))
        for l in interesting[:25]:
            print('     %s' % l)


if __name__ == '__main__':
    main()
