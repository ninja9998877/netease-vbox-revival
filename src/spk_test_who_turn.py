#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验【turn() 里那一段接线】—— 全程无声、零设备、零网络（record/speak/brain 全打桩）。

判据：
  ① `ctx` 真的送到了 `brain()`，而且状态/名字对得上
  ② ★★★ 预卷非空 ⇒ 观测收到 off='预卷混音'（两个人接在一起的音频不许判）
  ③ ★★★ 早退口（音频太短 / ASR 空）⇒ 一个向量都不许活到下一轮
  ④ ★★ 生人那一轮 `ctx.who` 必须是 None（粘性的 cur 不许把上一个人的记忆交出去）
  ⑤ 观测出错（模型炸了）⇒ 照样把这一轮说完，只是不认人
  ⑥ WHO 关着 ⇒ `_observe_who` 一次 embed 都不算
"""
import os
import shutil
import sys

TMP = '/tmp/spk_who_turn'
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
os.environ['SPK_MEM_DIR'] = TMP                    # ★ 必须在 import 之前
# ★★ 2026-09-22 新增：**必须关掉思考音**。
#   以前 `_filler_go()` 依赖 `_warm_filler()` 拼好的那条轨，而这个测试从不预热 ⇒
#   它一直安静地返回 None ⇒ 没声。阶梯改版后那道依赖没了：`turn()` 一调就真起阶梯、
#   真 `dlna.push` ⇒ **这个离线测试会往音箱里出声**。
#   它不是本次要验的东西，关掉即可。
os.environ['SPK_FILLER'] = '0'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                                          # noqa: E402
import spk_ear as E                                         # noqa: E402
import spk_skills as S                                      # noqa: E402
import spk_speaker as spk                                   # noqa: E402

SR = E.SR
REAL_OBSERVE = spk.observe          # ★ 在任何打桩之前存住真函数（见 ②）
fails = []


def chk(name, cond, extra=''):
    print(('  ✓ ' if cond else '  ✗ ') + name + (('   ' + extra) if extra else ''))
    if not cond:
        fails.append(name)


class FakeIdent:
    """真的 Identity 太贵（要载模型）—— 这里只喂状态机的输出，验的是【接线】。"""
    def __init__(self, book=None, cur=None):
        self.book = book or {'某人': 'VEC'}
        self._cur = cur

    def step(self, obs):
        return obs[0] if obs[0] != 'unsure' else 'known_soft'

    def who(self):
        return self._cur


def mkear(ident, record_ret, asr_ret='你好'):
    """造一只只带 turn() 需要的零件的耳朵。"""
    e = E.Ear.__new__(E.Ear)
    e.pending = None
    e.speaking = False
    e.asr = None
    e.ident = ident
    e.record = lambda pre=None: (record_ret[0], record_ret[1])
    e._flush = lambda: 0
    e.mic = type('M', (), {'q': None})()
    # ★★ 2026-09-24 从 /tmp 救回项目后补的（代码漂移）：
    #   `turn()` 在 `:2332` 加了 `lv = self._live`（ASR 流式化，见 `spk-asr-streaming`），
    #   而 `__new__` 造的裸耳朵**不跑 `__init__`** ⇒ 没有这个属性 ⇒ AttributeError。
    #   `None` 就是生产里的合法默认（`:1782`）＝"这条流没开" ⇒ 自动退回整段老路。
    e._live = None
    # ★★ 2026-09-24 同上（第二批漂移）：回灌闸门那套是**后加的**，`turn()` 现在会调
    #   `self._is_self_echo(text)`，它要读 `_last_said`（见 `spk-echo-gate-blind-paths`）。
    #   三个值取生产 `__init__` 的原样默认（`:1803-1809`）＝"我从没说过话"。
    e._last_said = ''
    e._said_at = 0.0
    e._speech_at = 0.0
    return e


audio = np.zeros(SR * 2, 'float32')
E.asr_text = lambda asr, a: asr_ret_holder[0]
asr_ret_holder = ['你好']
grabbed = []


def stub_brain(text, dry=False, session=None, ctx=None):
    grabbed.append(ctx)
    return '好', None


E.brain = stub_brain
# ★ 2026-09-22：`speak()` 多了 `pre=`（后台预合成的那一份）。桩跟着签名走 ——
#   这里打桩是为了**不出声**，签名对不上就等于把整条路堵死了（会 TypeError）。
E.speak = lambda text, mic_q=None, pre=None: None

print('① ctx 真送到 brain，状态/名字对得上')
obs_holder = [('known', '某人', 0.88, 0.30, 'VEC')]
spk.observe = lambda pcm, book, off='': obs_holder[0]
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
e.turn(None)
chk('brain 收到 ctx 了', len(grabbed) == 1 and grabbed[0] is not None)
chk('who=某人 / state=known / score=0.88',
    grabbed[0].who == '某人' and grabbed[0].state == 'known' and abs(grabbed[0].score - 0.88) < 1e-9,
    'who=%r state=%r score=%r' % (grabbed[0].who, grabbed[0].state, grabbed[0].score))
chk('★ 只有 answering 那一轮才带 claim_voice', grabbed[0].claim_voice is None)
chk('ident 也递下去了（第3步注册要用）', grabbed[0].ident is e.ident)

print()
print('② ★★★ 预卷非空 ⇒ 观测必须收到 off="预卷混音"（不许判）')
seen_off = []
spk.observe = lambda pcm, book, off='': (seen_off.append(off) or obs_holder[0])
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
e.pending = None
e.turn(None)
chk('没预卷 ⇒ off 是空串', seen_off[-1] == '', repr(seen_off[-1]))
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
e.pending = np.zeros(SR, 'float32')          # ★ 上一次被打断留下的那半句
e.turn(None)
chk('★ 有预卷 ⇒ off="预卷混音"（audio 是两个人接在一起的）', seen_off[-1] == '预卷混音',
    repr(seen_off[-1]))
chk('★ 有预卷 ⇒ 这一轮绝不带 claim_voice（混音算出来的向量不许绑名字）',
    grabbed[-1].claim_voice is None)
# ★ 打桩的 observe 绕过了 off 判断 —— 所以"off ⇒ skip"这一条必须【直接验真函数】。
#   真函数在开头就存住了（`REAL_OBSERVE`）；**绝不能靠 reload 去还原** ——
#   reload 会把后面几节的打桩一起冲掉，于是 ④ 那一节偷偷跑了真模型（这就是它刚才失败的原因）。
r = REAL_OBSERVE(np.zeros(SR * 2, 'float32'), {'某人': 'VEC'}, off='预卷混音')
chk('★★ 真 observe(off="预卷混音") ⇒ 立刻 skip，【一条 embed 都不算】',
    r == ('skip', '预卷混音'), repr(r))
chk('★ 而且 skip 里没有向量可绑（unpack 给出 None）', spk.unpack(r)[4] is None)
chk('★ 预卷被消费掉了（pending 清空，不会拼第二遍）', e.pending is None)

print()
print('③ ★★★ 早退口：一个向量都不许活到下一轮')
e = mkear(FakeIdent(cur='某人'), (None, '没等到人说话'))
e.pending = np.zeros(SR, 'float32')
n0 = len(grabbed)
chk('音频太短 ⇒ 早退，brain 没被调', e.turn(None) is None and len(grabbed) == n0)
chk('★ pending 仍然被清（不然那半句会拼进下下轮）', e.pending is None)
asr_ret_holder[0] = ''
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
got = e.turn(None)
chk('ASR 转出空串 ⇒ 早退，brain 没被调', got is None and len(grabbed) == n0)
asr_ret_holder[0] = '你好'

print()
print('④ ★★ 生人那一轮 ctx.who 必须是 None（粘性 cur 不许把上一个人的记忆交出去）')
obs_holder[0] = ('stranger', 0.31, 0.12, 'VEC')
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))     # cur 还粘着"某人"
e.turn(None)
chk('state=stranger', grabbed[-1].state == 'stranger')
chk('★★ who=None（虽然 Identity 还粘着"某人"）', grabbed[-1].who is None,
    'who=%r' % grabbed[-1].who)
chk('★ 也不带 claim_voice（还没开报名窗口）', grabbed[-1].claim_voice is None)
obs_holder[0] = ('unsure', '某人', 0.50, 0.05, 'VEC')
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
e.turn(None)
chk('★ unsure ⇒ 沿用"某人"（这次没听准，不是换人了）', grabbed[-1].who == '某人')

print()
print('⑤ 观测炸了 ⇒ 这一轮照样说完，只是不认人')
def boom(pcm, book, off=''):
    raise RuntimeError('模型文件没了')


spk.observe = boom
e = mkear(FakeIdent(cur='某人'), (audio, 'VAD 切出来了'))
n0 = len(grabbed)
e.turn(None)
chk('brain 还是被调了（没被拖挂）', len(grabbed) == n0 + 1)
chk('★ ctx 是干净的 unknown（退化成"不认人"）',
    grabbed[-1].state == 'unknown' and grabbed[-1].who is None)
spk.observe = lambda pcm, book, off='': obs_holder[0]
obs_holder[0] = ('known', '某人', 0.88, 0.30, 'VEC')

print()
print('⑥ WHO 关着 ⇒ 一次 observe 都不算')
calls = []
spk.observe = lambda pcm, book, off='': (calls.append(1) or ('known', '某人', 0.9, 0.3, 'VEC'))
e = mkear(None, (audio, 'VAD 切出来了'))          # ident=None = 没开
e.turn(None)
chk('observe 一次都没调', not calls)
chk('ctx 是空白（state=unknown ⇒ 提示词一个字都不加）', grabbed[-1].state == 'unknown')

print()
print('=' * 56)
if fails:
    print('✗ 失败 %d 项：%s' % (len(fails), fails))
    sys.exit(1)
print('✓ turn() 接线全过 —— 全程无声、零设备、零网络')
