#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ab_asr.py —— 流式识别器 vs 整段批量：**纯离线、零声音、不碰服务**的 A/B。

为什么要有它（2026-09-23）：
    `spk_ear.asr_text()` 把整段音频一次性喂给 `sherpa_onnx.OnlineRecognizer`
    （那是个**流式**模型）⇒ 整段解码全压在"说完之后"。实测 190 轮：中位 0.40s、
    p90 1.0s、最长 **4.0s**。修法是把音频**一边来一边喂**（见 `spk_ear.LiveAsr`）。
    但这个修法动了识别器的**输入电平**（`_loud` 的整句增益因果上拿不到），
    所以上线前必须回答两件事，缺一不可：

      ① **文字有没有退化**（不一致的段数 / CER）—— 有一条明显变差就不上线
      ② **省了多少**（cut 耗时的中位 / p90 / max，以及"退回老路"触发几次）

它只调 `make_asr` / `read_wav16k` / `seg_audio` / `asr_text` / `LiveAsr`：
    **不碰麦克风、不碰 DLNA、不调 brain()（因此不需要 DS_KEY）、一个字都不出声。**

★ 保真点（写下来免得以后改坏）：
    · 真实 `record()` 是【每轮 vad.reset() 一次、VAD 一 pop 就 return】⇒ 这里也逐轮模拟
      （pop 后 reset 再接着喂），而不是把整个文件一口气喂到底。逐轮才有"这一轮之前
      攒了多少静音"这件事，而它正是门控省 CPU 的那一块。
    · 批式那一路喂的是 **`seg_audio(seg, concat(buf))`** —— 与 `turn()` 里一模一样，
      不是自己另切一份。
    · 流式那一路喂的是**同一批 `buf` 对象**，且只从 `is_speech_detected()` 的**上升沿**
      开始（含往前补 `GBASE` 秒）—— 这正是 `LiveAsr` 在线上要做的事。

用法：
    python3 ab_asr.py                    # 默认语料
    python3 ab_asr.py --files a.wav b.wav
    python3 ab_asr.py --json /tmp/ab.json   # 落盘，供改动前后 diff
"""
import argparse
import glob
import json
import math
import os
import sys
import time

import numpy as np

from _cfg import DATA_DIR

HERE = os.path.dirname(os.path.abspath(__file__))

import spk_ear as E

PKT = 1600                      # ≈100ms，与 Mic 一包同量级
TAIL_PAD = 1.2                  # 尾巴补静音：VAD 不 pop 就什么都不出（踩过这个坑）


def corpus(files=None):
    if files:
        return files
    out = []
    out += sorted(glob.glob(os.path.join(DATA_DIR, 'capture',
                                         '20260921-100025', 'mic_00*.wav')))
    phone = os.environ.get('SPK_PHONE_REC_DIR')
    if phone:                       # 另一个壳那套的录音（不在本仓库里，配了才收）
        out += sorted(glob.glob(os.path.join(phone, '*-in.wav')))
    out += sorted(glob.glob(os.path.join(HERE, 'asr', 'kwtest', '*.wav')))
    for x in ('/tmp/spk_ear_last.wav', '/tmp/spk_e2e_q.wav'):
        try:
            open(x, 'rb').close()
            out.append(x)
        except OSError:
            pass
    return out


def turns(path, vad):
    """把一个 wav 切成"一轮一轮"，每轮 = (包列表, 上升沿下标, 批式切片)。

    ★ 与 `record()` 同构：reset → 攒 buf + 喂 VAD → pop 就收工 → 下一轮再 reset。

    ★★★ 切片必须【在 pop 的同一个迭代里】算掉，不能把 `seg` 攒着过后再处理：
      `seg.samples` 是块活不过 pop 之后的内存，攒下来读它就是那个
      "非确定性地返回未初始化内存"（`seg_audio` 的注释记着这件事）——
      实测攒着会直接 `MemoryError: std::bad_alloc`。生产代码里
      `record()` 是当场调的，这也是它 8 次里只坏 1 次的原因。
    """
    a = E.read_wav16k(path)
    if TAIL_PAD > 0:
        a = np.concatenate([a, np.zeros(int(E.SR * TAIL_PAD), np.float32)])
    res, i = [], 0
    while i + PKT <= len(a):
        vad.reset()
        buf, prev, onset, seg = [], False, None, None
        while i + PKT <= len(a):
            pk = a[i:i + PKT]
            i += PKT
            buf.append(pk)
            vad.accept_waveform(pk)
            d = vad.is_speech_detected()
            if d and not prev:
                onset = len(buf) - 1
            prev = d
            if not vad.empty():
                s = vad.front
                vad.pop()
                try:
                    seg = E.seg_audio(s, np.concatenate(buf))     # ★ 当场算，别攒
                except Exception as e:                            # noqa: BLE001
                    print('   ⚠ seg_audio 这次坏了（%s: %s）—— 跳过这一轮'
                          % (type(e).__name__, e))
                    seg = None
                break
        if seg is None:
            break
        if seg is not None and len(seg) >= E.SR * 0.2:
            res.append((buf, onset, seg))
        # 收工后把剩下的静音吃掉一点，免得下一轮从上一轮的尾巴里起步
        i += int(E.SR * 0.2)
    return res


def live_real(rec, buf, onset, tail=None):
    """★★★ 用【线上那个】`spk_ear.LiveAsr` 跑一遍 —— 它才是要上线的东西。

    返回 (文字, cut 耗时, feed 总耗时)。

    ★ `cut 耗时`才是【与批式 bsec 可比】的那个数：批式把整段解码全压在"主人停下之后"，
      流式只有 cut 那一截在"停下之后"。feed 那部分藏在"主人还在说"里 ——
      所以 `省 = bsec − cut`。feed 耗时单独报出来，用来确认它【装得下】
      （feed 总耗时必须 < 那段音频的时长，否则就是把活推到了别处，不是省了）。

    ★★★ 必须【照线上那样逐包调 feed】—— 包括 onset 之后那些 False 的包：
      线上 `record()` 是每收一包就 `self._live.feed(a, detected)`，detected 掉回
      False 也照样调（锁存）。只喂上升沿那一包就等于【没验锁存】。

    ★ `tail`：临时改 `SPK_ASR_TAIL`（横扫用）。`LiveAsr.cut()` 是现读模块全局的，
      所以这里改了、跑完必须改回去。
    """
    if onset is None:
        return '', 0.0, 0.0
    old = E.SPK_ASR_TAIL
    if tail is not None:
        E.SPK_ASR_TAIL = tail
    try:
        L = E.LiveAsr(rec)
        t_feed0 = time.time()
        for i, pk in enumerate(buf):
            L.feed(pk, i == onset)
        feed_s = time.time() - t_feed0
        t0 = time.time()
        text = L.cut()
        return text, time.time() - t0, feed_s
    finally:
        E.SPK_ASR_TAIL = old


def live_run(rec, buf, onset, gbase=None, hold=None):
    """`LiveAsr` 的【独立参照实现】—— 用来交叉校验线上那个类（`--ref`）。

    ★ 它与 `spk_ear.LiveAsr` 是分开写的两份代码，逐条语义对齐（含 `GBASE`/`HOLD`
      都从 `spk_ear` 现取，不许各写一份常量 —— 第一版就是因为这里各写一份 0.10，
      才让"两边不一致"看起来像实现问题，其实是设计问题）。
      两份都跑、结果一致 ⇒ 说明线上那个类没有偏离据以定案的那个设计。
    """
    if onset is None:
        return '', 0.0
    gbase = E.SPK_ASR_GBASE if gbase is None else gbase
    hold = E.SPK_ASR_HOLD if hold is None else hold
    s = rec.create_stream()
    feed_n = [0]
    t0 = time.time()

    def gain(x):
        r = float(np.sqrt((np.asarray(x, np.float32) ** 2).mean()))
        if r < 1e-6:
            return 1.0
        return min(E.ASR_RMS / r, E.ASR_GAIN_MAX)

    def give(x, k):
        x = np.asarray(x, np.float32)
        s.accept_waveform(E.SR, np.clip(x * k, -1.0, 1.0) if k > 1.0 else x)
        feed_n[0] += len(x)
        while rec.is_ready(s):
            rec.decode_stream(s)

    # 上升沿那一包 + 往前补 gbase 秒（VAD 的判定点滞后于开口，见 spk_ear 规矩②）
    pre = np.concatenate(buf[:onset + 1]) if onset + 1 <= len(buf) else np.concatenate(buf)
    n_back = min(len(pre), int(E.SR * gbase))
    seed = pre[len(pre) - n_back:]
    post = []                                   # 开口沿之后的音频（估增益只用它）
    post.append(seed)
    tail = list(buf[onset + 1:])
    fed = 0
    # 回撤 hold：落后的那点先攒着，够长了再喂（第一次喂出去时窗口就已经有 0.5 秒）
    pend = []
    for pk in tail:
        pend.append(pk)
        post.append(pk)
        while sum(len(p) for p in pend) > int(E.SR * hold):
            p = pend.pop(0)
            if fed == 0:
                give(seed, gain(np.concatenate(post)))
                fed = 1
            give(p, gain(np.concatenate(post)))
    for p in pend:
        if fed == 0:
            give(seed, gain(np.concatenate(post)))
            fed = 1
        give(p, gain(np.concatenate(post)))
    if fed == 0:
        give(seed, gain(np.concatenate(post)))
    s.accept_waveform(E.SR, np.zeros(int(E.SR * E.SPK_ASR_TAIL), np.float32))
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    r = rec.get_result(s)
    return (r.text if hasattr(r, 'text') else str(r or '')).strip(), time.time() - t0


def tail_lost(batch, live):
    """★ 判据【只看一件事】：流式是不是把尾巴吞了。

    "吞尾巴"的精确定义 = 批式的文字是【流式文字 + 后面还有字】：
    比如 批='现在几点了' / 流='现在几点' ⇒ 吞了 1 个字。
    这条比 CER 尖 —— CER 会把"多认出一个字"和"少认出一个字"糊在一起，
    而这里要判的只有"少"。
    """
    if not live or not batch:
        return 0
    if batch.startswith(live) and len(batch) > len(live):
        return len(batch) - len(live)
    return 0


def tail_sweep(rec, vad, files, tails):
    """★ 尾垫横扫：`cut()` 末尾补多少秒假静音才不吞最后一个字（2026-09-23）。

    只调 `LiveAsr` 与 `asr_text`，不出声、不碰服务。判据 = `tail_lost` 的段数与总丢字数。
    """
    turns_all = []
    for p in files:
        for buf, onset, audio in turns(p, vad):
            if audio is None or len(audio) < E.SR * 0.2 or onset is None:
                continue
            turns_all.append((p.split('/')[-1], buf, onset, E.asr_text(rec, audio)))
    print('\n===== 尾垫横扫：n=%d 段 =====' % len(turns_all))
    print('  %-8s %-10s %-10s %-10s %-8s' % ('尾垫', '完全一致', '吞尾巴段数', '共丢字数', '转空'))
    for t in tails:
        same = lost_n = lost_c = empty = 0
        diff = []
        for name, buf, onset, bt in turns_all:
            lt = live_real(rec, buf, onset, tail=t)[0]
            if lt == bt:
                same += 1
            if not lt:
                empty += 1
            n = tail_lost(bt, lt)
            if n:
                lost_n += 1
                lost_c += n
                if len(diff) < 6:
                    diff.append((name, bt, lt))
        print('  %-8.2f %-10s %-10d %-10d %-8d'
              % (t, '%d/%d' % (same, len(turns_all)), lost_n, lost_c, empty))
        for name, bt, lt in diff:
            print('        %-28s 批=%r 流=%r' % (name, bt, lt))
    return 0


def cer(ref, hyp):
    """字级编辑距离 / 参考长度。空参考记 0（两边都空）或 1（只有一边空）。"""
    if not ref and not hyp:
        return 0.0
    if not ref:
        return 1.0
    d = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        nd = [i] + [0] * len(hyp)
        for j, b in enumerate(hyp, 1):
            nd[j] = min(d[j] + 1, nd[j - 1] + 1, d[j - 1] + (a != b))
        d = nd
    return d[-1] / float(len(ref))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', nargs='*')
    ap.add_argument('--json')
    ap.add_argument('--live', action='store_true',
                    help='（默认就跑流式；这个参数只为兼容旧命令行）')
    ap.add_argument('--ref', action='store_true',
                    help='同时跑独立参照实现，交叉校验线上那个 LiveAsr')
    ap.add_argument('--only-batch', action='store_true', help='只跑基线')
    ap.add_argument('--tail-sweep', action='store_true',
                    help='★ 只横扫 cut() 末尾的假静音（0/0.2/0.35/0.5），判据=有没有吞最后一个字')
    ap.add_argument('--tails', nargs='*', type=float,
                    default=[0.0, 0.1, 0.2, 0.35, 0.5], help='--tail-sweep 要扫的值')
    args = ap.parse_args()

    rec = E.make_asr()
    vad = E.make_vad()
    if args.tail_sweep:
        return tail_sweep(rec, vad, corpus(args.files), args.tails)
    rows = []
    for p in corpus(args.files):
        segs = turns(p, vad)
        if not segs:
            print('%-34s 0 轮（VAD 一段都没出）' % p.split('/')[-1])
            continue
        for buf, onset, audio in segs:
            if audio is None or len(audio) < E.SR * 0.2:
                continue
            t0 = time.time()
            bt = E.asr_text(rec, audio)
            bsec = time.time() - t0
            lt, lsec, lfeed, rt = ('', 0.0, 0.0, '')
            if not args.only_batch:
                lt, lsec, lfeed = live_real(rec, buf, onset)
                if args.ref:
                    rt = live_run(rec, buf, onset)[0]
            rows.append(dict(file=p.split('/')[-1], dur=round(len(audio) / float(E.SR), 2),
                             onset=onset, nbuf=len(buf),
                             batch=bt, live=lt, ref=rt,
                             bsec=round(bsec, 3), lsec=round(lsec, 3),
                             lfeed=round(lfeed, 3)))
            print('  %5.2fs 批[%5.2fs] %-22s | 流[%5.2fs] %s%s'
                  % (len(audio) / float(E.SR), bsec, bt or '（空）', lsec, lt or '（空）',
                     ('  ≠参照 %r' % rt) if (args.ref and rt != lt) else ''))
        print('%-34s %d 轮' % (p.split('/')[-1], len(segs)))

    if not rows:
        print('语料为空'); return 1
    b = np.array([r['bsec'] for r in rows])
    print('\n===== 批式基线：n=%d =====' % len(rows))
    print(' 耗时 中位 %.3fs / p90 %.3fs / max %.3fs / 合计 %.1fs'
          % (np.median(b), np.percentile(b, 90), b.max(), b.sum()))
    print(' 转空 %d 轮（%.0f%%）'
          % (sum(1 for r in rows if not r['batch']), 100.0 * sum(1 for r in rows if not r['batch']) / len(rows)))
    if not args.only_batch:
        l = np.array([r['lsec'] for r in rows])
        f = np.array([r['lfeed'] for r in rows])
        d = np.array([r['dur'] for r in rows])
        same = [(r['batch'] == r['live']) for r in rows]
        cs = [cer(r['batch'], r['live']) for r in rows]
        deg = [r for r in rows if r['batch'] and not r['live']]
        print('\n===== 流式（线上 spk_ear.LiveAsr）：n=%d =====' % len(rows))
        print(' ★ cut 耗时（= 主人停下之后还欠的那一截）'
              ' 中位 %.3fs / p90 %.3fs / max %.3fs' % (np.median(l), np.percentile(l, 90), l.max()))
        print('   批式同一批              '
              ' 中位 %.3fs / p90 %.3fs / max %.3fs' % (np.median(b), np.percentile(b, 90), b.max()))
        print(' ★★ 省（批 − 流）          '
              ' 中位 %.3fs / p90 %.3fs / max %.3fs / 全部合计 %.1fs'
              % (np.median(b - l), np.percentile(b - l, 90), (b - l).max(), (b - l).sum()))
        print(' ★ feed 耗时（藏在"主人还在说"里）：中位 %.3fs / max %.3fs'
              '  实时率 %.2f×（>1 = 装得下）'
              % (np.median(f), f.max(), float((d / np.maximum(f, 1e-6)).mean())))
        print('   流式合计（feed+cut）中位 %.3fs —— 与批式中位 %.3fs 比 = 总 CPU %+.0f%%'
              % (np.median(f + l), np.median(b),
                 100.0 * (np.median(f + l) - np.median(b)) / max(np.median(b), 1e-6)))
        print(' 文字完全一致 %d/%d（%.0f%%）  平均 CER %.3f  最大 CER %.3f'
              % (sum(same), len(rows), 100.0 * sum(same) / len(rows),
                 float(np.mean(cs)), float(np.max(cs))))
        print(' ★★ 批式有字、流式转空（**最该看的反例**）：%d 轮' % len(deg))
        for r in deg[:10]:
            print('     %5.2fs 批=%r 流=%r' % (r['dur'], r['batch'], r['live']))
        bad = [r for r in rows if cer(r['batch'], r['live']) > 0.34]
        print(' CER>0.34 的段：%d 轮' % len(bad))
        for r in bad[:10]:
            print('     %5.2fs 批=%r 流=%r' % (r['dur'], r['batch'], r['live']))
        if args.ref:
            nd = [r for r in rows if r['ref'] != r['live']]
            print(' ★ 与独立参照实现不一致：%d 轮' % len(nd))
            for r in nd[:10]:
                print('     %5.2fs 线上=%r 参照=%r' % (r['dur'], r['live'], r['ref']))
        # ★ 段长 <0.6s 的那些单独看：滚动增益在极短话语上最可能估不准（存的已知风险）
        sh = [r for r in rows if r['dur'] < 0.6]
        if sh:
            print(' ★ 段长 <0.6s 的 %d 轮：文字一致 %d，转空 %d'
                  % (len(sh), sum(1 for r in sh if r['batch'] == r['live']),
                     sum(1 for r in sh if r['batch'] and not r['live'])))

    if args.json:
        with open(args.json, 'w') as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print('\n落盘 → %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
