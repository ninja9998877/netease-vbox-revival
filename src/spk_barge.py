#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_barge.py —— 边播边听：主人在我说话时插话，我要停下来让路。

主人 2026-09-21 定的形态，两句话：
    「音箱在说话时 有用户说话 音箱要停止 并说 嗯 你说之类的 话 给人类让步 然后倾听」
    「这个打断其实是要双线进行 一边播 一遍还会听」

★ 现状（为什么这是一块新东西，而不是改个参数）
  现在说话时耳朵是【闭着】的：`dlna.say()` 阻塞最长 25 秒，`Ear._flush()` 把播放期间
  攒下的麦克风音频**全丢掉**。所以那 25 秒里我们完全聋 —— 主人喊破嗓子也不知道。
  这个模块就是把那 25 秒变成"听得见"。

★★ 判据是【声纹】，不是音量 —— 理由见 spk_voiceprint 文件头（音量那把尺子会自己变形）。
  这里负责的是声纹判据缺的那一半：**时间对齐**。
  "此刻我自己是不是在大声说"必须知道我们播到哪儿了，而"推流→开口"的延迟
  （发现 + SOAP + 设备缓冲）实测 1~3 秒、事先不知道 ⇒ 用**我们自己那条 mp3 的包络
  去和麦克风包络做互相关**把它量出来。

  ★ 这个互相关顺手还是个证据：相关峰够高 ⇒ 麦克风里听到的确实是我们自己。
    这正是声纹模板能成立的前提 —— DLNA 推流的**不被设备 AEC 吃掉**（实测），
    所以麦克风听得见我们自己。要是哪天峰没了，说明这条路的前提变了。

★ 不打桩、不碰 UDP：构造函数只吃一个"吐 16k float32 音频"的队列，
  所以它能拿文件离线跑自测（`--selftest`），不需要音箱出声、不需要主人在场。

★ 铁律：观察者【绝不能】把主循环弄挂。线程里的一切异常都吞掉并记一行日志 ——
  它是"更好"，说话是"这次要命"。
"""
import difflib
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                                   # noqa: E402
import spk_voiceprint as vp                           # noqa: E402

SR = 16000
SLOT = 0.05               # 包络格宽，跟 dlna.Ear 的格子对齐
ENV_FRAC = float(os.environ.get('SPK_BARGE_ENV_FRAC', '0.20'))
# ↑ "我们自己在出声"的包络门限 = 这条音频自己包络的 90 分位 × 这个比例。
#   ★ 必须【相对】而不能写绝对值：软件增益会变（现役 -28dB），
#     写死一个数会在改增益的那天静默失效 —— 要么永远判"我没在说"，要么永远判"我在说"。
ENV_MIN_FRAC = 0.60       # 一窗里我们自己的包络得有这么大比例在响，才算"这窗主要是我"
CORR_MIN = float(os.environ.get('SPK_BARGE_CORR', '0.60'))
# ↑ 互相关峰低于这个值就认为"听到的不是我自己" ⇒ 那一轮不喂声纹基准（保守）。
#   ★ 别为了"让它锁上"往下调：锁错延迟比锁不上糟得多 —— 锁错会让判据静默失效，
#     锁不上只是退回冷启动门限（保守，少打断几次而已，而且日志里写着）。
MAX_LAG = 2.5             # 延迟最多量到这么久
MIN_SLOTS = 20            # 互相关至少要有这么长的重叠（20 格 = 1 秒），否则不算
# ★★★ EST_MIN 必须【由 MAX_LAG 推出来】，不能各写各的：
#   要搜到 lag，手里至少得有 MIN_SLOTS + lag 格重叠。攒得不够就搜不满整个范围，
#   而"搜不满"的时候，范围内的最好值可能落在正中间而不是边上 ——
#   我一开始想用"峰卡在边界上就不认"来挡，实测**挡不住**（真延迟 2.0s 被错锁成 1.30s，
#   相关只有 0.46）。所以干脆等到能覆盖满再量，从根上消掉这种局面。
EST_MIN = MAX_LAG + MIN_SLOTS * SLOT


def log(fmt, *a):
    print('%s  [打断] %s' % (time.strftime('%H:%M:%S'), fmt % a if a else fmt), flush=True)


def _rms(x):
    return float(np.sqrt((np.asarray(x, dtype=np.float32) ** 2).mean())) if len(x) else 0.0


def env_of_pcm(pcm, slot=SLOT):
    """一段音频 → 每 50ms 一格的 RMS。跟 spk_voiceprint.slots 同一件事，
    这里单独写一份是为了让 spk_barge 不依赖任何"当前在播哪条"的全局状态。"""
    n = int(len(pcm) / (SR * slot))
    return [_rms(pcm[int(k * SR * slot):int((k + 1) * SR * slot)]) for k in range(n)]


class Barge:
    """边播边听。用法：

        b = Barge(mic_queue, '/tmp/spk_ai.mp3', on_fire=lambda: (ctl.stop(), abort.set()))
        b.start();  dlna.say(mp3);  b.stop()
        if b.fired:  ...用 b.preroll 接着听主人说...

    线程只做一件事：把队列里的音频攒成窗，判"这窗里是不是只剩我自己"。
    一旦连续若干窗判成"不是我" ⇒ 调 `on_fire()`，并把**主人开口那一段**留下来。
    """

    def __init__(self, q, mp3_path, on_fire=None, win=None, hop=None):
        self.q = q
        self.mp3 = mp3_path
        self.on_fire = on_fire
        self.win, self.hop = win or vp.WIN, hop or vp.HOP
        self.fired = False
        self.preroll = None            # ★ 主人开口那段（见 preroll 属性）
        self.reason = ''
        self.t0 = None
        self.delay = None              # 推流→开口的延迟（秒），量出来才有
        self.corr = None
        self.n_win = 0
        self.n_other = 0
        self._stop = False
        self._thr = None
        # ★ 不能写 `vp.pcm_of(...) or np.zeros(...)` —— 数组的真值会抛 ValueError
        _pcm = vp.pcm_of(mp3_path)
        self._env = env_of_pcm(_pcm) if _pcm is not None else []
        self._env_thr = (ENV_FRAC * float(np.percentile(self._env, 90))) if self._env else 0.0
        self._ref = vp.reference(mp3_path)
        self._tracker = vp.Tracker()
        self._deb = vp.Debounce()

    # ------------------------------------------------------------ 对齐
    def _estimate_delay(self, mic_env):
        """拿【我们自己的包络】去对麦克风包络做互相关，量出"推流→开口"差了多少。

        ★ 必须量而不能估：这个延迟是 SSDP 发现 + SOAP + 设备缓冲的总和，
          实测 1~3 秒之间飘。估错 1 秒，"此刻我在大声说"就变成了"此刻我安静"，
          于是主人的话被当成基准喂进去 —— 判据当场就废了，而且**不会有任何报错**。

        ★★ 返回"这次算不算数"。**手里音频越少，能搜到的延迟上限就越低**
          （要搜到 lag，至少得有 MIN_SLOTS + lag 格的重叠）：
          攒到 2.5 秒时最多只能搜到 1.5 秒。
          所以**峰要是卡在可搜范围的最边上，就不能认** —— 真延迟很可能更长，
          认了就会锁死在一个偏小的值上。这时返回 False，等音频再多攒点重算。
          （自测时真延迟恰好 1.5s，正卡在边界上 —— 侥幸量对了，这是运气不是设计。）
        """
        e = np.asarray(self._env, dtype=np.float32)
        m = np.asarray(mic_env, dtype=np.float32)
        self.delay = self.corr = None
        if len(e) < MIN_SLOTS or len(m) < MIN_SLOTS:
            return False
        lim = min(int(MAX_LAG / SLOT), len(m) - MIN_SLOTS)
        if lim < 1:
            return False
        best, best_lag = -1.0, None
        for lag in range(0, lim + 1):
            n = min(len(e), len(m) - lag)
            if n < MIN_SLOTS:
                break
            a = e[:n] - e[:n].mean()
            b = m[lag:lag + n] - m[lag:lag + n].mean()
            d = float(np.linalg.norm(a) * np.linalg.norm(b))
            if d <= 1e-12:
                continue
            c = float(a @ b) / d
            if c > best:
                best, best_lag = c, lag
        if best_lag is None:
            return False
        self.corr = best
        # ★ 只有"峰够强 且 不卡在边界"才算数。两条缺一，delay 一律留 None ⇒
        #   _mine_loud 返回 False ⇒ 不喂基准，走冷启动门限（保守，不乱打断）。
        if best_lag >= lim:                        # 卡在最边上 ⇒ 真延迟可能更长，不认
            self.delay = None
            return False
        if best < CORR_MIN:                        # 听到的不像我自己 ⇒ 这个延迟没有意义
            self.delay = None
            return False
        self.delay = best_lag * SLOT
        log('量到延迟 %.2fs（相关 %.2f）—— 麦克风里听到的确实是我自己', self.delay, best)
        return True

    def _warmup(self, buf, upto):
        """定下延迟之后，把之前那些"盲判"的窗重判一遍，只为喂基准。

        ★ 为什么只喂基准、不碰去抖：那些窗是【过去】。主人那句话早说完了，
          现在才"发现"再报一次打断，只会让他莫名其妙 —— 去抖必须只由实时音频驱动。
        ★ 也不改 n_win/n_other：那两个数是给日志看的实时统计，
          把补喂的算进去会让"判了几窗"对不上时间线。
        """
        win, hop = int(SR * self.win), int(SR * self.hop)
        n = 0
        i = 0
        while i + win <= upto and i + win <= len(buf):
            loud = self._mine_loud(i)
            if loud:
                v, s = vp.judge(buf[i:i + win], self._ref)
                self._tracker.feed(v, s, True)
                n += 1
            i += hop
        if n:
            log('回头补喂 %d 个窗（延迟定下来之前判的那些）⇒ %s',
                n, self._tracker.stats())

    def _mine_loud(self, i0):
        """这一窗里，我自己是不是主要在大声说。`i0` = 窗起点（样本）。"""
        if self.delay is None or not self._env:
            return False
        t = i0 / float(SR) - self.delay
        k0 = int(t / SLOT)
        k1 = k0 + int(self.win / SLOT)
        if k1 <= 0:
            return False
        seg = self._env[max(0, k0):max(0, k1)]
        if not seg:
            return False
        loud = sum(1 for v in seg if v >= self._env_thr)
        return loud >= ENV_MIN_FRAC * len(seg)

    # ------------------------------------------------------------ 主循环
    def start(self):
        self.t0 = time.time()
        threading.Thread(target=self._loop, daemon=True, name='barge').start()
        return self

    def stop(self):
        self._stop = True

    def _loop(self):
        try:
            self._run()
        except Exception as e:                          # noqa: BLE001
            import traceback
            log('✗ 观察者挂了（不影响这次播放）：%s: %s', type(e).__name__, e)
            traceback.print_exc()

    def _run(self):
        win = int(SR * self.win)
        hop = int(SR * self.hop)
        buf = np.zeros(0, np.float32)
        mic_env = []
        nxt = 0
        delay_done = False
        cut_from = None            # 本轮 other 连续段的起点（样本）

        while not self._stop and not self.fired:
            try:
                a = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if a is None or not len(a):
                continue
            buf = np.concatenate((buf, np.asarray(a, np.float32)))

            # 包络：够一格就补一格
            while len(buf) >= (len(mic_env) + 1) * int(SR * SLOT):
                k = len(mic_env)
                mic_env.append(_rms(buf[k * int(SR * SLOT):(k + 1) * int(SR * SLOT)]))

            # ★ 一直重算到"算数"为止（峰卡在边界上会返回 False，等音频多了再算）。
            #   这期间 delay 是 None ⇒ _mine_loud 一律 False ⇒ 不喂基准，走冷启动门限。
            #   攒到 MAX_LAG+2 还量不出来就认了：这一轮不喂基准（★ 说白了就是
            #   "我没听见自己" —— 那多半是喇叭哑了或麦克风没在推，见自听那套判据）。
            if not delay_done and len(mic_env) * SLOT >= EST_MIN:
                if self._estimate_delay(mic_env):
                    delay_done = True
                    # ★★★ 回头补喂基准：延迟要到 3.5 秒才定得下来，在那之前判过的窗
                    #   全都因为"不知道播到哪儿了"而没喂基准 —— 于是短句（3 秒那种）
                    #   永远来不及自适应，一直吃冷启动门限。
                    #   可音频明明都还在 buf 里 ⇒ 定下延迟后把前面那些窗重判一遍，
                    #   **只补喂基准，不碰去抖**（过去的事不该触发打断，主人早说完了）。
                    self._warmup(buf, nxt)
                elif len(mic_env) * SLOT >= MAX_LAG + 2.0:
                    log('⚠ 攒了 %.1fs 仍没量出可信延迟（相关 %s）—— 麦克风里不像有我自己。'
                        '这一轮不喂声纹基准，按冷启动门限保守判',
                        len(mic_env) * SLOT,
                        ('%.2f' % self.corr) if self.corr is not None else '—')
                    delay_done = True

            while len(buf) - nxt >= win:
                seg = buf[nxt:nxt + win]
                thr = self._tracker.thr()
                verdict, score = vp.judge(seg, self._ref, thr=thr)
                self.n_win += 1
                loud = self._mine_loud(nxt)
                self._tracker.feed(verdict, score, loud)
                self._thr = thr
                if verdict == 'other':
                    self.n_other += 1
                    if cut_from is None:
                        cut_from = nxt
                else:
                    cut_from = None
                if self._deb.feed(verdict):
                    # ★★★ 就是这里 —— 主人真的在插话。
                    self.fired = True
                    self.preroll = buf[cut_from:] if cut_from is not None else buf[-win:]
                    self.reason = ('连续 %d 窗判"不是我"（最近一窗相似度 %.3f / 门限 %.3f ／ %s）'
                                   % (self._deb.need, score, thr, self._tracker.stats()))
                    log('✋ %s', self.reason)
                    try:
                        if self.on_fire:
                            self.on_fire()
                    except Exception as e:              # noqa: BLE001
                        log('✗ on_fire 出错：%s: %s', type(e).__name__, e)
                    return
                nxt += hop

    # ------------------------------------------------------------ 给调用方的交代
    def stats(self):
        return ('判了 %d 窗（其中 %d 窗判"不是我"）/ 延迟 %s / 相关 %s / %s'
                % (self.n_win, self.n_other,
                   ('%.2fs' % self.delay) if self.delay is not None else '没量到',
                   ('%.2f' % self.corr) if self.corr is not None else '—',
                   self._tracker.stats()))

    @property
    def preroll_secs(self):
        return (len(self.preroll) / float(SR)) if self.preroll is not None else 0.0


# ---------------------------------------------------------------- 离线自测
def selftest(level_db=0.0):
    """★ 全程文件、**一点声音都不放**（主人明令：屋里有人时，测试不许吵到人）。

    做法：把"我们自己的 mp3"和"一段真人录音"按给定电平混成一条【假的麦克风流】，
    用队列喂给 Barge，看它会不会在真人开口之后、按要求连续 N 窗之内报出来。
    这验的是状态机和对齐，不是真房间的阈值 —— 真房间的阈值等音箱有声音了再标。
    """
    import glob
    import spk_ai_dlna as dlna
    if not vp.ready():
        print('✗ 声纹用不了：%s' % vp.why_not())
        return 1
    mine = dlna.tts('好的，我记下了。明天早上七点半叫你起床，提前十分钟。还有别的要交代的吗？')
    me = vp.pcm_of(mine)
    hs = sorted(glob.glob(os.path.join(HERE, 'asr', '*', 'test_wavs', 'zh_*.wav')))
    hp = vp.pcm_of(hs[0])
    print('自测素材：我 %.1fs（%s） / 真人 %.1fs（%s）'
          % (len(me) / SR, os.path.basename(mine), len(hp) / SR, os.path.basename(hs[0])))

    # 造一条假麦克风流：前 1.5 秒只有我 → 真人从第 1.5 秒插入，混到结尾
    # ★ 两个可调的量，就是为了能故意造出"真延迟比一开始能搜到的上限还长"这种局面，
    #   验证它会不会忍住不锁一个错值（这个坑自测真抓到过）。
    DELAY = float(os.environ.get('SPK_BARGE_T_DELAY', '1.5'))   # 推流→开口的延迟
    AT = float(os.environ.get('SPK_BARGE_T_AT', '3.0'))         # 真人从第几秒开始插话
    total = max(int(SR * DELAY) + len(me), int(SR * AT) + len(hp)) + SR

    def put(dst, at_sec, src, gain):
        """往 dst 的 at_sec 处叠加 src（★ 必须按剩余长度裁，不能被顶出尾巴）。"""
        i = int(SR * at_sec)
        n = min(len(src), len(dst) - i)
        if n > 0:
            dst[i:i + n] += gain * src[:n] / (_rms(src) + 1e-9)

    mic = np.zeros(total, np.float32)
    put(mic, DELAY, me, 1.0)                                   # 我们自己
    put(mic, AT, hp, 10 ** (level_db / 20.0))                  # 真人插话
    mic *= 0.05                 # 别削顶

    # ★★★ 必须【按真实时间流失喂】，不能一口气塞进队列。
    # 踩过的坑：一开始把所有包一次 put 进去，Barge 线程就一口气跑完了 ——
    # 于是"延迟锁定"和"实时判窗"的先后被打乱，"回头补喂基准"几乎没跑到（只剩 1 帧），
    # 而这个假象看起来像"算法不work"。**测试喂数据的方式必须和真实链路一样**，
    # 否则验的就不是同一个东西。真实 mictap 就是 100ms 一包。
    q = queue.Queue(maxsize=600)
    STEP = int(SR * 0.1)

    def feed():
        for i in range(0, len(mic), STEP):
            q.put(mic[i:i + STEP])
            time.sleep(0.1)                    # 实时
    threading.Thread(target=feed, daemon=True, name='feed').start()

    b = Barge(q, mine).start()      # 自测不接 on_fire：只看它认不认得出
    t0 = time.time()
    while not b.fired and time.time() - t0 < 40:
        time.sleep(0.05)
    b.stop()
    print('\n真人 %.1fs 处插话（电平 %+.0f dB），推流→开口延迟被人为设成 %.1fs' % (AT, level_db, DELAY))
    print('  观察者：%s' % b.stats())
    if b.fired:
        print('  ★ 报出来了：%s' % b.reason)
        print('  预卷 %.2fs ⇒ 拿来接着听主人说（他开口那段不会丢）' % b.preroll_secs)
        hit = b.delay is not None and abs(b.delay - DELAY) <= 0.15
        print('  延迟量得%s（真值 %.1fs，量到 %s）'
              % ('准 ✓' if hit else '不准 ✗', DELAY,
                 ('%.2fs' % b.delay) if b.delay is not None else '无'))
    else:
        print('  ✗ 没报出来 —— 这条电平下声纹推不动（见 spk_voiceprint 的已知边界）')
    return 0 if b.fired else 1


# ------------------------------------------------------------------ 预卷是自己还是主人
# ★★★ 2026-09-21 定案：被打断之后【什么都不说，直接停 + 倾听】。
#
#   曾经想做的事：让大模型写一句"嗯，你说"再播出来，把发言权交还得更自然。
#   代码写过、离线验过，然后被实测数据否掉了 —— 三个结果：
#     ① 大模型写这句要 5~11 秒（pro），"明天天气到底怎么样"那次更是【29.8 秒然后返回空】
#        （它在试着回答一个还没说完的问题）。而"主人停顿"这个窗口只有一两秒。
#     ② 四种不同场景（抢话/制止/插话/没听清），它四次都在说同一件事的四种说法 ——
#        "请说"、"您请讲"、"您说"、"请你说"。这句话根本不是"内容"，是【协议帧】：
#        它要的是【瞬时且永远正确】，不是【贴切】。生成式模型在这个位置上没有增量。
#     ③ 于是整条路的性价比是负的：为了几个字，引入 5~30 秒的延迟、
#        一个 1 秒封顶的闸门、一句"不可打断"的特权、还有"准备好了但主人还在说"的矛盾。
#   ⇒ 结论：打断的正确响应是【沉默】。住口本身就是让路，不需要配台词。
#     ★ 而且沉默顺手把上面全部难题一起消掉了 —— 没有话，就没有"什么时候说"、
#       "能不能被打断"、"说多长"这些问题。
#     ⚠️ 别再往这条热路径里塞大模型。要重开这个案子，先回答：它比沉默好在哪。
#
#   真正剩下要做的只有一件机械事：主人接着说的那半句不能丢。就是下面这个闸门 +
#   spk_ear 里的"预卷接上、别清队列"。
_PUNCT = '，。！？、；：""\'\'（）《》 \t\n,.!?;:"\'()<>[]-'


def _bare(s):
    return ''.join(c for c in (s or '') if c not in _PUNCT)


def is_self_echo(heard, our_text, min_frac=0.6):
    """预卷转出来的话，是不是【我们自己刚说的】？

    ★ 为什么必须有这道闸门：那 1.55 秒预卷是"我们正大声说 + 主人插话"的【混音】
      （相关 0.86 就是证据 —— 麦克风里确实有我们自己）。直接拼进下一轮录音，
      ASR 很可能转出我们自己刚说的那句话，然后大模型一本正经地回应它自己，
      看着像自言自语。
    ★ 为什么用"包含"而不是整句相似度：我们听到的往往只是自己那句话的【尾巴一段】
      （预卷只有 1.5 秒），整句比相似度会把它冲淡。所以问的是
      "他说的这些字，有多少能在我们自己说的话里找到"。
    ★ 判成"自己"时的动作是【丢掉预卷】—— 宁可丢掉主人那句话的开头
      （他可能要重说一遍），也绝不能让他听见音箱自问自答。

    ★★★ `min_frac`（2026-09-23 加，**默认值就是原来的 0.6，所以音箱那边一个字节没变**）：
      0.6 这把尺子对【另一条线】太松了 —— 那边 ASR 出来的**每一句**都要过这道闸门，
      而主人正常回话时**很自然地会引用/重复我们刚说的词**，那正是对话的常态。
      实测三例全是误伤、零真阳性：`'你'`（单字原样命中）、`'你定啊'`（2/3=67%，
      而他正是在回应我们说的"吃什么**你定**"）、`'没什么'`（同 67%）。
      真回声**整句都是我们的话**（frac≈1.0），而主人引用的只有两三个字 ⇒
      **把尺子提到 0.8 两边就分开了**。调用方按自己的场景传，别改默认值。
    """
    h, o = _bare(heard), _bare(our_text)
    if not h or not o:
        return False, '没得比（空）'
    if h in o:
        return True, '原样在我们自己那句话里'
    m = difflib.SequenceMatcher(None, h, o).find_longest_match(0, len(h), 0, len(o))
    frac = m.size / float(len(h))
    if frac >= min_frac:
        return True, '有 %.0f%%（%d/%d 字）能在我们自己那句话里找到' % (
            frac * 100, m.size, len(h))
    return False, '只有 %.0f%% 像我们自己' % (frac * 100)


# ------------------------------------------------------------------ 让路：一个固定片段
# ★★ 主人 2026-09-21 拍板：「就先做一个 嗯 你说」——做成本地固定音频。
#   这三条合起来正好补上上面那段定案缺的那一角：
#     固定 ⇒ 内容不会跑偏，不需要"5 字封顶"的闸门
#     本地 ⇒ 热路径上没有 edge-tts 那趟网络往返
#     预先做好 ⇒ 热路径上没有大模型那 5~30 秒
#   于是从"决定说话"到"开口"只剩推流那一截（实测 1.5 秒）。
YIELD_TEXT = '嗯，你说'
CLIP = os.path.join(HERE, 'audio', 'yield.mp3')
CLIP_LABEL = CLIP + '.label'

# 等主人把话交回来：连续这么久的安静才算数
QUIET_HOLD = 0.6
QUIET_MAX = 12.0
MIC_GAP = 0.5             # 多久没收到帧就认为麦克风/tap 断了（真机 100ms 一帧）


def clip_secs(path=None):
    """片段有多长。★ 用总时长就够 —— 这里是拿来算"躲开自己声音"的窗口，宁长勿短。"""
    try:
        import spk_filler
        return spk_filler.dur_of(path or CLIP)
    except Exception:                                   # noqa: BLE001
        return 1.5                                          # 兜底：按最长的那句算


def _trim_tail(path, keep_tail=0.15):
    """掐掉尾巴上的静音。

    ★ 这是本项目的音频规矩（先掐尾静音，再 loudnorm）。实测这个片段总长 1.90s，
      而真正在说话只到 1.20s —— 后面 0.67 秒是纯静音，白占着
      "我们还在说话、耳朵还闭着"的那段时间。掐掉它，主人就能早 0.67 秒被听见。
    ★ 只掐尾巴，不动中间那一顿（"嗯，‹停›你说"里那个停顿是自然的，留着）。
    """
    try:
        import spk_filler
        sp = spk_filler.speech_span(path)
        if not sp:
            return path
        want = sp[1] + keep_tail
        if want >= spk_filler.dur_of(path) - 0.05:
            return path
        tmp = path + '.trim.mp3'
        subprocess.run(['ffmpeg', '-y', '-i', path, '-t', '%.3f' % want,
                        '-c:a', 'libmp3lame', '-b:a', '128k', tmp],
                       check=True, capture_output=True)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            log('掐尾静音：%.2fs → %.2fs', spk_filler.dur_of(tmp), want)
    except Exception as e:                              # noqa: BLE001
        log('（掐尾静音没成，用原样，不影响）：%s: %s', type(e).__name__, e)
    return path


def yield_clip(force=False):
    """把那个固定片段准备好，返回它的路径。平时就是一个躺在盘上的文件。

    ★ 为什么不直接提交一个 mp3 了事：响度由 spk_voice.chain() 决定，而增益是会变的
      （`.spk_gain`）。提交一个把当年增益烧死在里面的 mp3，改增益那天它就
      【静默】不匹配了 —— 回答和让路话一个响一个轻，而且没有任何报错。
      所以：内容固定、按当前嗓子现渲染一次，并把嗓子标签写进 sidecar，
      标签变了就重建。检查是一次字符串比较，不碰网络。
    """
    import spk_voice as _v
    want = _v.label()
    try:
        have = open(CLIP_LABEL).read().strip()
    except Exception:                                   # noqa: BLE001
        have = ''
    if not force and have == want and os.path.exists(CLIP):
        return CLIP
    import spk_ai_dlna as dlna
    os.makedirs(os.path.dirname(CLIP), exist_ok=True)
    t0 = time.time()
    dlna.tts(YIELD_TEXT, out=CLIP)
    _trim_tail(CLIP)
    with open(CLIP_LABEL, 'w') as f:
        f.write(want)
    log('固定片段做好了：%r → %s（%.2fs，嗓子 %s）',
        YIELD_TEXT, os.path.basename(CLIP), clip_secs(), want)
    return CLIP


def wait_quiet_and_collect(mic_q, hold=QUIET_HOLD, timeout=QUIET_MAX):
    """等主人把话交回来，并且【把他这段时间说的话收好】。返回 (等到了吗, 帧列表, 统计)。

    ★★ 为什么"等"和"收"必须是同一个动作：这道门要读麦克风才知道主人停没停，
      而读就是从队列里拿 —— 拿了不还回去，主人接着说的这半句就没了
      （而前半句已经被 Barge 当预卷拿走了，两半都丢就等于他白说）。所以
      调用方必须把这里收下的帧拼进下一轮录音。

    ★★ 判据是【两件事同时成立】：
        ① 最近还在【收到帧】—— 证明麦克风/tap 活着；
        ② 最近的帧都【不响】—— 证明主人没在说话。
      为什么"队列空"不能算安静：真机上 mictap 是【不间断】推帧的（每 100ms 一帧，
      主人不说话时推的是房间底噪）。所以"没有帧到达"不等于"主人停下来了"，
      它等于【麦克风或 tap 死了】。把沉默当许可，就是对着一个听不见的人说话。
      ⇒ 这两条任一不成立，我们就一直不出声。宁可这次没让路，也不能压住主人。
    """
    frames, rms, peak = [], [], 0.0
    t0 = time.time()
    last_loud = last_frame = None
    while time.time() - t0 < timeout:
        try:
            a = mic_q.get(timeout=0.2)
        except queue.Empty:
            a = None
        if a is not None:
            a = np.asarray(a, dtype=np.float32)
            frames.append(a)
            v = _rms(a)
            rms.append(v)
            peak = max(peak, v)
            # ★ 相对 + 绝对双门限：只用绝对门限，房间里底噪一变就失灵；
            #   只用相对门限，"一直很安静"时任何一点风声都算说话。
            thr = max(0.004, 0.18 * peak)
            now = time.time()
            last_frame = now
            if last_loud is None or v >= thr:
                last_loud = now
        now = time.time()
        alive = last_frame is not None and (now - last_frame) <= MIC_GAP
        silent = last_loud is not None and (now - last_loud) >= hold
        if alive and silent:
            st = {'secs': now - t0, 'peak': peak, 'n': len(frames)}
            log('★ 主人停下来了（等了 %.2fs，峰值 %.4f，收了 %d 帧）',
                st['secs'], peak, len(frames))
            return True, frames, st
    st = {'secs': time.time() - t0, 'peak': peak, 'n': len(frames)}
    if not (last_frame and time.time() - last_frame <= MIC_GAP):
        log('✗ 一帧都没来 —— 麦克风/tap 死了，绝不出声')
    else:
        log('★ 等了 %.1f 秒主人一直在说 ⇒ 这次不出声（让路已经完成了）', st['secs'])
    return False, frames, st


def main():
    if '--selftest' in sys.argv:
        i = sys.argv.index('--selftest')
        lv = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 0.0
        return selftest(lv)
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main())
