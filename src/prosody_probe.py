#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静默韵律探针：只写 /tmp 文件，**不出声、不推流、不碰音箱/电话线/声卡**。

量的是"像不像人"里可以客观化的那一半：
  · F0 半音标准差  —— 语调起伏（越大越不平）
  · F0 半音 p10~p90 —— 语调的活动范围
  · 停顿时长方差     —— 节奏变化
  · 时长             —— 顺带看代价

★ 为什么用【半音】而不是 Hz：跨性别比较 Hz 没意义（男声本来就低）。
  半音是相对该样本自己的 F0 中位算的，衡量的是"这个人自己变了多少"。
"""
import asyncio
import os
import subprocess
import sys
import wave

import numpy as np

OUT = '/tmp/prosody'
os.makedirs(OUT, exist_ok=True)

import edge_tts  # noqa: E402

SR = 16000


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


async def one(text, voice, rate='+0%', pitch='+0Hz', out=None):
    c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await c.save(out)
    return out


def to_wav(mp3, wav):
    r = sh(['ffmpeg', '-y', '-loglevel', 'error', '-i', mp3,
            '-ar', str(SR), '-ac', '1', wav])
    if r.returncode:
        raise RuntimeError(r.stderr[:400])


def read_wav(p):
    with wave.open(p) as w:
        assert w.getnchannels() == 1
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float64) / 32768.0


def f0_track(x, sr=SR, fmin=60.0, fmax=400.0, win=0.040, hop=0.010):
    """自相关基频跟踪。返回有声帧的 F0（Hz）数组。"""
    n, h = int(win * sr), int(hop * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    out = []
    for s in range(0, len(x) - n, h):
        fr = x[s:s + n]
        if np.sqrt((fr ** 2).mean()) < 0.01:        # 静音/太轻
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, 'full')[n - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] < 0.30:                            # 不像周期信号
            continue
        # 抛物线插值，细化峰值
        if 0 < k < len(ac) - 1:
            a_, b_, c_ = ac[k - 1], ac[k], ac[k + 1]
            d = a_ - 2 * b_ + c_
            k = k + (0.5 * (a_ - c_) / d if d else 0)
        out.append(sr / k)
    return np.array(out)


def pause_stats(x, sr=SR, thresh=0.012, minpause=0.08):
    """句内停顿的时长序列（秒）。"""
    fr = int(0.010 * sr)
    e = np.array([np.sqrt((x[i:i + fr] ** 2).mean())
                  for i in range(0, len(x) - fr, fr)])
    sil = e < thresh
    runs, cur = [], 0
    for s in sil:
        if s:
            cur += 1
        elif cur:
            if cur * 0.010 >= minpause:
                runs.append(cur * 0.010)
            cur = 0
    if cur * 0.010 >= minpause:
        runs.append(cur * 0.010)
    # 掐掉首尾静音造成的"停顿"
    return runs[1:-1] if len(runs) > 2 else []


def report(tag, wav):
    x = read_wav(wav)
    f0 = f0_track(x)
    dur = len(x) / SR
    if len(f0) < 5:
        print('%-22s ✗ 有声帧太少(%d)' % (tag, len(f0)))
        return
    st = 12 * np.log2(f0 / np.median(f0))
    ps = pause_stats(x)
    print('%-22s 时长%5.2fs  F0中位%6.1fHz  半音σ %5.2f  p10~p90 %5.2f'
          '  停顿 n=%d %s'
          % (tag, dur, np.median(f0), st.std(),
             np.percentile(st, 90) - np.percentile(st, 10),
             len(ps), ('σ%.2fs' % np.std(ps)) if len(ps) > 1 else ''))


# ── 文本：老朋友会说的话，三句，正好能拆 ──────────────────────────────────
SENT = ['哎，好久没联系了，', '你最近怎么样啊？', '今晚出来吃个饭吧。']
FULL = ''.join(SENT)

# (标签, voice, [(rate,pitch) × 3])  同一个 tuple 就是整段一个参数
CASES = [
    ('A 现役 晓晓',        'zh-CN-XiaoxiaoNeural',        [('-8%', '-10Hz')] * 3),
    ('B 晓晓·逐句变韵律',   'zh-CN-XiaoxiaoNeural',        [('+12%', '+3Hz'), ('-18%', '-12Hz'), ('-4%', '+6Hz')]),
    ('C 男声 云健 Passion', 'zh-CN-YunjianNeural',         [('-8%', '-10Hz')] * 3),
    ('D 东北话 小北',       'zh-CN-liaoning-XiaobeiNeural', [('-8%', '-10Hz')] * 3),
    ('E 男声 云希 Sunshine', 'zh-CN-YunxiNeural',           [('-8%', '-10Hz')] * 3),
    ('F 东北话·逐句变韵律',  'zh-CN-liaoning-XiaobeiNeural', [('+12%', '+3Hz'), ('-18%', '-12Hz'), ('-4%', '+6Hz')]),
]


async def main():
    print('=' * 100)
    for tag, voice, pros in CASES:
        parts = []
        for i, (s, (r, p)) in enumerate(zip(SENT, pros)):
            m = os.path.join(OUT, '%s_%d.mp3' % (tag[0], i))
            w = os.path.join(OUT, '%s_%d.wav' % (tag[0], i))
            await one(s, voice, r, p, m)
            to_wav(m, w)
            parts.append(w)
        # 拼起来当成一句完整回答来量
        lst = os.path.join(OUT, '%s.list' % tag[0])
        with open(lst, 'w') as f:
            for p in parts:
                f.write("file '%s'\n" % p)
        cat = os.path.join(OUT, '%s_cat.wav' % tag[0])
        r = sh(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat',
                '-safe', '0', '-i', lst, '-c', 'copy', cat])
        if r.returncode:
            cat = parts[0]
        report(tag, cat)
    print('=' * 100)
    print('全部文件在 %s —— ★ 全程零声音' % OUT)


if __name__ == '__main__':
    asyncio.run(main())
