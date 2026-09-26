#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen 音色探测：拿到 key 后跑这个，选出另一个壳该用哪只嗓子。

★ 为什么必须实测而不是照抄文档：阿里文档写着「每个模型仅支持特定的一组音色，
  不能混用」，而**能听 instructions 的 Instruct 系列到底认哪些音色，文档没列全**
  （Ethan/Dylan 那几个在文档里被归到"实时版音色列表"里）。所以别猜 ——
  逐个打一遍：谁真能用、谁的韵律最活，数据说话。

★ 全程不出声：只写 /tmp/qwen_voices/，不推流、不碰另一个壳、不碰音箱、不占声卡。

用法：
    DASHSCOPE_API_KEY=sk-xxx python3 qwen_voice_probe.py
    （或把 key 放进 .qwen_key 后直接跑）
"""
import importlib.util
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location(
    'pp', os.path.join(HERE, 'prosody_probe.py'))
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)          # main 有 __main__ 保护，不会出声

import spk_tts_qwen as q             # noqa: E402

OUT = '/tmp/qwen_voices'
os.makedirs(OUT, exist_ok=True)

# 这一句就是"老朋友"人设的开场白 —— 拿它当选型语料，因为最终就是它要说的。
SAMPLE = '哎，好久没联系了，你猜我是谁？晚上出来吃个饭吧。'

# (voice, 中文名, 备注)  —— 常见中文 + 方言；认不认由服务端说了算
VOICES = [
    ('Cherry',   '芊悦', '女·阳光亲切（官方默认）'),
    ('Serena',   '苏瑶', '女·温柔'),
    ('Chelsie',  '千雪', '女·二次元'),
    ('Ethan',    '晨煦', '★男·北方口音·阳光活力'),
    ('Dylan',    '晓东', '★男·北京话'),
    ('Momo',     '茉兔', '女'),
    ('Bella',    '萌宝', '女·儿童'),
    ('Vivian',   '十三', '女'),
    ('Moon',     '月白', '女'),
    ('Neil',     '阿闻', '男·新闻'),
    ('Peter',    '李彼得', '★男·天津话'),
    ('Marcus',   '秦川', '★男·陕西话'),
    ('Li',       '老李', '★男·南京话'),
    ('Sunny',    '晴儿', '女·四川话'),
    ('Eric',     '程川', '男·四川话'),
    ('Rocky',    '阿强', '男·粤语'),
    ('Kiki',     '阿清', '女·粤语'),
    ('Jada',     '阿珍', '女·上海话'),
    ('Roy',      '阿杰', '男·闽南话'),
]

q.set_logger(lambda *a: None)        # 静默：失败原因自己看返回就行


def main():
    if not q.available():
        print('✘ 没有 key：既没设 DASHSCOPE_API_KEY，也没有 %s' % q.KEYFILE)
        print('  先在百炼控制台建一个 API-KEY，然后：')
        print('     printf %%s "sk-xxxx" > %s && chmod 600 %s' % (q.KEYFILE, q.KEYFILE))
        return 1

    print('端点 %s' % q.URL)
    print('模型 %s（Instruct 系列才认语气指令）' % q.MODEL_INSTRUCT)
    print('语料 %r' % SAMPLE)
    print('=' * 104)

    rows = []
    for v, cn, note in VOICES:
        wav = os.path.join(OUT, '%s.wav' % v)
        if not q.synth(SAMPLE, wav, voice=v):
            rows.append((v, cn, note, False, 0.0, 0.0, 0.0))
            print('  %-9s %-4s ✘ 这个模型不支持它' % (v, cn))
            continue
        try:
            x = pp.read_wav(wav)
            f0 = pp.f0_track(x)
            dur = len(x) / pp.SR
        except Exception as e:                       # noqa: BLE001
            print('  %-9s %-4s ✓ 合成成功但分析失败 %s' % (v, cn, e))
            continue
        if len(f0) < 5:
            rows.append((v, cn, note, True, dur, 0.0, 0.0))
            print('  %-9s %-4s ✓ %5.2fs  有声帧太少，测不出韵律' % (v, cn, dur))
            continue
        st = 12 * np.log2(f0 / np.median(f0))
        sig, rng = float(st.std()), float(np.percentile(st, 90) - np.percentile(st, 10))
        rows.append((v, cn, note, True, dur, sig, rng))
        print('  %-9s %-4s ✓ %5.2fs  半音σ %5.2f  活动范围 %5.2f   F0中位 %6.1fHz  %s'
              % (v, cn, dur, sig, rng, np.median(f0), note))

    print('=' * 104)
    ok = [r for r in rows if r[3]]
    print('能用 %d / %d 个音色' % (len(ok), len(rows)))
    if ok:
        print('\n按【韵律起伏】排序（σ 越大 = 语调越不平 = 越不像念稿）：')
        for r in sorted(ok, key=lambda x: -x[5])[:8]:
            print('  %-9s %-4s σ %5.2f  范围 %5.2f  %s' % (r[0], r[1], r[5], r[6], r[2]))
    print('\n样本在 %s（★ 全程零声音，自己挑来听）' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
