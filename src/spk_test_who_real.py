#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验【真声纹 → 状态机 → 跨轮次】这条缝 —— 零设备、零网络、零声音。

★★★ 为什么要有它（补的是唯一的真空）
    认人这条链上已经有三层测试，各自都全过，但它们【接不上】：

      · `spk_speaker.py --selftest`   验状态机 —— 喂的是**合成分数**（手写的元组）
      · `spk_test_who_turn.py`        验接线   —— 把 `observe` **整个打桩**掉
      · `spk_ear.py --wav --who`      验感知   —— 但**每次 `Ear.__new__` 造新耳朵**，只有单句

    ⇒ "**一串真实音频，逐轮喂进同一个真 Identity**"这一条，从没人跑过。
      而 `netease-vbox-speaker-id` 记的那 8 个坑，**全都住在这条缝上**
      （stranger 段被静默丢光 / 生人轮交出上一个人记忆 / 单票渲染出 None …）——
      前两层各自都测不到它们，因为它们只在**两者接起来**的时候才出现。

★ 语料是真的，不是合成的
    `wakeword/clips/` 是 2026-09-21 从**设备麦克风**采的远场录音：RMS 只有
    0.003~0.010，ASR 要放大 20~40 倍才认得出字。这正是生产条件，比 sherpa 那些
    近讲干净的测试 wav 难得多（`spk_speaker.py` 文件头第 ④ 条自己就担心过这件事）。
    实测里面是**两个真人**，按采集时刻天然分成两组。

★★ 为什么【不用 TTS 造多人】—— 试过了，不行，记在这免得下一个人再试
    edge-tts 四个嗓音两两相似度 0.265~0.590，其中「晓晓 vs 小艺」= **0.590**，
    **高于 `THR_NAME` 0.55**；换成 5 秒长句仍是 0.590 ⇒ 不是"短音频"的锅，是模型的
    真实性质（同语言的 TTS 共享太多声学底子）。
    ⇒ 拿 TTS 当"多个人"会造出一个**恒假阳**的测试：两个"人"本来就该被判成同一个。
      真实的第二个人只能靠**真录音**——这份语料里正好有。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                          # noqa: E402
import spk_speaker as spk                                   # noqa: E402
import spk_voiceprint as vp                                 # noqa: E402

CLIPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wakeword', 'clips')

# ── 语料 ground truth（2026-09-24 实测标定；改语料必须重标，别照抄）──
#   甲/乙是**两个真人**，不是两个 TTS 嗓音。分组依据：组内相似度 0.93~0.98（甲）、
#   对甲 0.365~0.473 而彼此 0.67~0.69（乙）—— 两簇分得很干净。
JIA = ['w00_100354', 'w02_100410', 'w03_100511', 'w04_100637']
YI = ['w01_100402', 'w09_105707']
SKIP = ['w05_101136', 'w06_101144', 'w08_101500']   # 有声，但 observe 判"量不出可信语音"⇒ 不留向量
STRG = ['w07_101158']                               # 跟两个人都够不着
#   ★ 只拿这两条注册，其余全部当"没见过的"来喂 —— 注册样本自己考自己（1.000）不算识别。
ENROLL = {'甲': 'w00_100354', '乙': 'w09_105707'}

fails = []


def chk(name, cond, extra=''):
    print(('  ✓ ' if cond else '  ✗ ') + name + (('   ' + extra) if extra else ''))
    if not cond:
        fails.append(name)


def load(tag):
    """文件名前缀 → pcm。★ 前缀是 9 字符（`w00_10035`），够区分这 10 条。"""
    hits = [f for f in os.listdir(CLIPS) if f.startswith(tag)]
    if not hits:
        raise SystemExit('语料缺了：%s（看 %s）' % (tag, CLIPS))
    return vp.pcm_of(os.path.join(CLIPS, hits[0]))


def observe(pcm, book):
    """→ (state, who, score, margin, vec)。★ 必须走 `unpack`：`observe` 对 skip 那类
    返回的是 **2 元组** `('skip', 原因)`，直接按 5 元组拆会 ValueError ——
    `spk_test_who_turn.py` 里那句 `spk.unpack(r)[4]` 就是为这个。"""
    return spk.unpack(spk.observe(pcm, book, off=''))


def fresh(book_names=('甲', '乙')):
    """造一本册子（用注册样本）+ 一只全新的 Identity。"""
    book = {n: vp.embed(load(ENROLL[n])) for n in book_names}
    return spk.Identity(book), book


print('语料：%s' % CLIPS)
print('  甲 %s' % JIA)
print('  乙 %s' % YI)
if not vp.ready():
    raise SystemExit('✗ 声纹模型没就绪：%s' % (vp.why_not() or '未知'))
print()

# ══════════════════════════════════════════════════════════════
print('① 同一个人跨轮次必须稳定认得出（真音频，同一个 Identity 连喂）')
ident, book = fresh()
seq = []
for tag in ['w02_100410', 'w03_100511', 'w04_100637']:
    st, who, sc, mg, _ = observe(load(tag), book)
    got = ident.step((st, who, sc, mg, None))
    seq.append((tag, got, ident.who()))
    chk('%s ⇒ known 甲' % tag, got == 'known' and ident.who() == '甲',
        'state=%r who=%r' % (got, ident.who()))
chk('★ 三轮之间 cur 一次都没漂', len({w for _, _, w in seq}) == 1,
    str([w for _, _, w in seq]))

print()
print('② 第二个人（另一个真人）也认得出 —— 这条是"换人"能力的地基')
for tag in YI:
    st, who, sc, mg, _ = observe(load(tag), book)
    ident.step((st, who, sc, mg, None))
    chk('%s ⇒ 乙' % tag, ident.who() == '乙', 'who=%r' % ident.who())

print()
print('③ ★★ 假阳护栏：没有任何一条被判成【不是他的那个人】')
#   这是本模块文件头标为"代价最重"的方向 —— 把生人认成已注册的人
#   ⇒ 别人的记忆被喂进去，而且**不可见、永久污染**。
ident, book = fresh()
wrong = []
for tag in JIA + YI:
    st, who, sc, mg, _ = observe(load(tag), book)
    ident.step((st, who, sc, mg, None))
    truth = '甲' if tag in JIA else '乙'
    if ident.who() not in (None, truth):
        wrong.append((tag, ident.who(), truth))
chk('零跨认（10 条全喂一遍）', not wrong, str(wrong))

print()
print('④ skip 的轮次【不许留下向量】—— 留了就会绑到下一个人身上')
n_skip = 0
for tag in SKIP:
    st, who, sc, mg, vec = observe(load(tag), book)
    if st == 'skip':
        n_skip += 1
        chk('%s ⇒ skip 且 vec 是 None' % tag, vec is None, 'vec=%r' % (vec,))
if n_skip == 0:
    chk('语料里应该有 skip 样本（不然这条没测到）', False,
        '★ 语料变了？SKIP 那三条现在都量得出向量了')

print()
print('⑤ ★ 冷启动"两票制"：弱证据第一票不许认下（真音频里天然有这种样本）')
#   `w01` 对乙 0.691 → **低于 `THR_LOCK` 0.75** ⇒ 走弱证据路：
#   第一票只记账不认人，第二票才 `_set`。
ident, book = fresh()
weak = 'w01_100402'
st, who, sc, mg, _ = observe(load(weak), book)
chk('这条确实是弱证据（< THR_LOCK=%.2f）' % spk.THR_LOCK, sc is not None and sc < spk.THR_LOCK,
    'score=%r' % sc)
v1 = ident.step((st, who, sc, mg, None))
chk('★ 第一票：不许认下（cur 仍是 None）', ident.who() is None, 'who=%r' % ident.who())
chk('★ 第一票的状态必须是 unknown —— 【绝不能】是 known',
    #   踩过的坑：这里要是照 obs 把名字放出去，提示词就会渲染出"现在说话的人是 None"。
    v1 == 'unknown', 'state=%r' % v1)
v2 = ident.step((st, who, sc, mg, None))
chk('第二票：认下 乙', v2 == 'known' and ident.who() == '乙',
    'state=%r who=%r' % (v2, ident.who()))

print()
print('⑥ ★★ 两个人轮流说话 —— 每一轮都要跟得上（Debounce 那套会死在这儿）')
#   `Identity` 文件头专门写了：主人和某人轮流是 A/B/A/B，**永远凑不出连续两轮同名**
#   ⇒ 任何"连续 N 轮才换人"的方案都会卡死在第一个说话的人身上。
ident, book = fresh()
alt = ['w02_100410', 'w01_100402', 'w03_100511', 'w09_105707']
want = ['甲', '乙', '甲', '乙']
got = []
for tag in alt:
    st, who, sc, mg, _ = observe(load(tag), book)
    ident.step((st, who, sc, mg, None))
    got.append(ident.who())
chk('甲↔乙 交替四轮，每轮都跟得上', got == want, '拿到 %s 期望 %s' % (got, want))

print()
print('=' * 56)
if fails:
    print('✗ 失败 %d 项：%s' % (len(fails), fails))
    sys.exit(1)
print('✓ 真声纹→状态机 跨轮次全过 —— 零设备、零网络、零声音')
