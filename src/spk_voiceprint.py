#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_voiceprint.py —— 声纹：分清"这句话是我自己在说"还是"主人在说"。

主人 2026-09-21 的原话：
    「每个人的声音都是有声闻（纹）的 你自己的声音还分不清吗」

★ 为什么这条比"音量判据"强（打断 barge-in 的立足点）
  音量判据要先知道"我们自己的回声有多大"，而那个数**随音量根本不是线性的**
  （实测：软件增益 +24dB，麦克风里只 +4.5dB）—— 一把会自己变形的尺子。
  声纹用的是**音色**：我们自己的嗓子是**固定且已知**的，而音色不吃音量。
  更关键的是它补上了音量判据唯一的死角：**主人盖着我们说话**。
  停顿那 0.4 秒里插话，音量判据也许能抓；但主人**在我们正出声时**说
  "你先听我说"，那段在音量上完全糊在一起，只有音色分得开。

★★ 参考模板【从正在播的那条 mp3 现算】，不做注册、不需要主人念样本。
  三个好处，一个都不能少：
    ① 换嗓子/改语速/改均衡，模板**自动跟着变**，不会偷偷失效；
    ② 不用主人配合；③ 不落任何生物特征文件。
  （主人自己的声音我们【不做注册】—— 那是敏感数据，且没必要：
    我们只需要判"这像不像我自己"，一个模板就够。）

★★★ 实测（2026-09-21，全在文件上跑，一点声音都没放；`--calib` 可复现）
  同一嗓子不同句子      sim = 0.889
  我们 vs 真人（7 段）  sim = -0.11 ~ +0.08      ← 干净帧上这道题是白送的
  纯自己随窗长          0.5s 0.651 / 1.0s 0.749 / 2.0s 0.774
  混入真人（窗长 × 真人相对我们的电平，中位数；括号内是"无人对照"）
      窗长      无人对照    +0dB    -6dB   -12dB   -18dB   -24dB
      0.5s      0.654     0.542   0.585   0.632   0.639   0.635
      1.0s      0.734     0.498   0.621   0.660   0.684   0.708
      2.0s      0.773     0.470   0.597   0.667   0.708   0.740
  ⇒ ① 0.5 秒太短（自己跟自己才 0.651，且一有人就掉得没规律），**默认 1.0 秒**。
     ② **真人得压到 −6dB 以内（也就是跟我们差不多响）才推得动分数** ——
        这是这条判据的真实灵敏度，不吹。所以门限取 0.55（卡在
        "同电平 0.498" 与 "无人 0.734" 之间），**不是**看着好看的 0.9。
     ③ ★ 这张表是在**干净文件**上算的；真房间里混响+底噪会把 self 基准往下推，
        所以门限交给 `Tracker` **现算**，不靠这里的常数（冷启动才用 0.55）。

★ 已知边界（老实写在这儿）
  这条判据回答的是"**这一窗里是不是只剩我自己的声音**"。所以
    ① 我们正大声念、主人小声在底下说 —— 声纹**会漏**（我们占主导）。
       **量化了：主人比我们低 6dB 以上就抓不到**（看上面那张表）。
       那一段该由"拿已知的 mp3 当参考做残差"去补（下一步）。
       ★ 但也别急着把它当缺陷：主人真要对着一台正大声说话的音箱插话，
       他自然会提高音量盖过来 —— 那正好落在"跟我们差不多响"这一档，抓得住。
    ② 它答不了"说话的人是谁"—— 我们不认主人，只认"不是我"。
  反过来，为了压住**误报**（把噪声/混响尾巴当成主人），`Debounce` 要
  **连续 N 窗**都判 other 才算数，而不是一窗就跳。

★ 铁律：这条链【绝不许拖慢或弄挂嗓子】。模型载入失败、依赖缺失 —— 一律返回
  "不知道"，让调用方按"没有声纹"的保守路径走，绝不抛异常到主循环。
"""
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MODEL = os.environ.get('SPK_VP_MODEL',
                       os.path.join(HERE, 'asr', 'speaker', 'campplus_zh.onnx'))

# ---- 判据常数（标定依据见文件头；动它们之前先重跑 --calib）----
WIN = float(os.environ.get('SPK_VP_WIN', '1.0'))     # 判一窗用多长的音频（秒）
HOP = float(os.environ.get('SPK_VP_HOP', '0.25'))    # 窗与窗之间挪多少（秒）
# ★ 门限只是【冷启动的兜底】。真正该用的是 Tracker 现算的那个 —— 见它上面的长注释。
THR_SELF = float(os.environ.get('SPK_VP_THR', '0.55'))   # ≥ 这个 ⇒ 还是我自己
MARGIN = float(os.environ.get('SPK_VP_MARGIN', '0.18'))  # 门限 = 本次实测 self 基准 − 这个
NEED = int(os.environ.get('SPK_VP_NEED', '3'))       # 连续几窗判 other 才算数（去抖）
RMS_MIN = float(os.environ.get('SPK_VP_RMS', '0.004'))   # 静音帧直接跳过

_lock = threading.Lock()
_ex = None
_err = ''
_dim = 0


def log(fmt, *a):
    import time
    print('%s  [声纹] %s' % (time.strftime('%H:%M:%S'), fmt % a if a else fmt),
          flush=True)


# ---------------------------------------------------------------- 嵌入
def _extractor():
    """惰性载入。★ 只用一把锁、只载一次 —— 打断判据是热路径，不能每帧都载模型。"""
    global _ex, _err, _dim
    if _ex is not None or _err:
        return _ex
    with _lock:
        if _ex is not None or _err:
            return _ex
        if not os.path.exists(MODEL):
            _err = '模型不在：%s' % MODEL
            log('✗ %s —— 声纹这一路关掉，打断按"没有声纹"走', _err)
            return None
        try:
            import sherpa_onnx as so
            cfg = so.SpeakerEmbeddingExtractorConfig(
                model=MODEL, num_threads=1, debug=False, provider='cpu')
            _ex = so.SpeakerEmbeddingExtractor(cfg)
            _dim = _ex.dim
            log('载入说话人模型（%d 维，%s）', _dim, os.path.basename(MODEL))
        except Exception as ex:                    # noqa: BLE001
            _err = '%s: %s' % (type(ex).__name__, ex)
            log('✗ 模型载不起来：%s —— 声纹这一路关掉', _err)
    return _ex


def ready():
    """声纹能不能用。调用方拿它决定走哪条路。"""
    return _extractor() is not None


def why_not():
    return _err


def embed(pcm):
    """16k 单声道 float32 → 192 维单位向量。失败返回 None（绝不抛）。"""
    ex = _extractor()
    if ex is None or pcm is None or len(pcm) < 1600:      # 少于 0.1 秒没意义
        return None
    try:
        import numpy as np
        s = ex.create_stream()
        s.accept_waveform(16000, np.ascontiguousarray(pcm, dtype=np.float32))
        s.input_finished()
        v = np.asarray(ex.compute(s), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else None
    except Exception as ex_:                       # noqa: BLE001
        log('✗ 算嵌入失败：%s: %s', type(ex_).__name__, ex_)
        return None


def sim(a, b):
    """两个单位向量的余弦。★ 嵌入已归一化，所以就是点积。"""
    if a is None or b is None:
        return 0.0
    import numpy as np
    return float(np.dot(a, b))


# ---------------------------------------------------------------- 读音频
def pcm_of(path, secs=None):
    """任何音频 → 16k 单声道 float32。用 ffmpeg，跟 tts() 那条链同源。"""
    cmd = ['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', '16000',
           '-f', 's16le', '-']
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        log('✗ 解码不了 %s：%s', path, ex)
        return None
    import numpy as np
    a = np.frombuffer(p.stdout[:len(p.stdout) // 2 * 2], '<i2').astype(np.float32) / 32768.0
    return a[:int(16000 * secs)] if secs else a


# ---------------------------------------------------------------- 参考模板
_ref_cache = {}


def reference(mp3_path):
    """我们自己的模板 —— 从**正在播的这条 mp3** 现算（见文件头 ★★）。
    按 (路径, 大小, mtime) 缓存：同一句话重播不必重算。"""
    try:
        st = os.stat(mp3_path)
        key = (mp3_path, st.st_size, int(st.st_mtime))
    except OSError:
        return None
    if key in _ref_cache:
        return _ref_cache[key]
    pcm = pcm_of(mp3_path)
    ref = embed(pcm) if pcm is not None else None
    if ref is not None:
        if len(_ref_cache) > 32:                   # 别让它无限长
            _ref_cache.clear()
        _ref_cache[key] = ref
        log('建了自我模板：%s（%d 维）', os.path.basename(mp3_path), _dim)
    return ref


# ---------------------------------------------------------------- 判一帧
def slots(pcm, slot=0.05):
    """每 50ms 一格的 RMS —— 跟 dlna.Ear 的格子对齐，方便互相对照。"""
    import numpy as np
    n = int(len(pcm) / (16000 * slot))
    return [float(np.sqrt((pcm[int(k * 16000 * slot):int((k + 1) * 16000 * slot)] ** 2).mean()))
            for k in range(n)]


def judge(pcm, ref, thr=None):
    """一段音频 → ('self' | 'other' | 'quiet', 分数)。

    ★ 三步走，缺一不可：
      ① 太安静 ⇒ 'quiet'（没人说话，也没我在说）—— 先把静音挡掉，
         不然静音帧的嵌入是纯噪声，会随机地判成 other。
      ② 算嵌入、比模板。
      ③ 低于门限才算 other。`thr` 给 None 时用冷启动常数 THR_SELF；
         **热路径应当传 Tracker.thr() 那个现算的**（理由见 Tracker）。
    """
    import numpy as np
    if ref is None or pcm is None or len(pcm) < 1600:
        return 'quiet', 0.0
    rms = float(np.sqrt((np.asarray(pcm, dtype=np.float32) ** 2).mean()))
    if rms < RMS_MIN:
        return 'quiet', 0.0
    v = embed(pcm)
    if v is None:
        return 'quiet', 0.0
    s = sim(v, ref)
    return ('self' if s >= (THR_SELF if thr is None else thr) else 'other'), s


class Tracker:
    """★★★ 门限【不写死】，跟着本次播放的实测走。这是这套判据能不能用在真房间的关键。

    原理（免费，因为我们的包络是已知的）：
      播放期间，"我自己的包络正大声"的那些帧，**按定义就是 self** —— 主人就算在底下
      说话，那一段也是我们占主导。把这些帧的分数收起来取中位数，就得到
      **这台房间、这个音量、这只麦克风下的 self 基准**。

    为什么非这样不可（实测）：
      干净文件里 self 基准是 0.734（1 秒窗），但那是在**没有混响、没有底噪**的文件上算的。
      真房间里麦克风收到的是"我们的声音 + 房间混响 + 底噪"，嵌入会被推离模板，
      这个基准**只会比 0.734 低**，低多少事先不知道。
      写死一个常数 ⇒ 要么太松（真房间全都判 self，永远不打断），
      要么太紧（一有混响就判 other，一惊一乍）。**跟随实测就不存在这个问题。**

    `MARGIN` 是"要掉多少才算别人"：按实测的混音表（1 秒窗），
    真人跟我们同电平掉 0.236、小 6dB 掉 0.113 ⇒ **0.18 恰好卡在中间**：
    同电平稳抓，"小 6dB"看运气，"小 12dB 以上"抓不到（那是已知边界，见文件头）。
    """

    def __init__(self, margin=None, window=40):
        self.margin = MARGIN if margin is None else margin
        self.window = window               # 用最近多少个 self 帧当基准
        self.selfs = []

    def feed(self, verdict, score, mine_loud):
        """`mine_loud` = 按**已知包络**（拿正在播的那条 mp3 算的），此刻我们自己是否正大声。

        ★★★ 这个参数**必须来自已知包络，绝不能写成 `verdict == 'self'`**。
        那样就成了拿判据的结论去定判据的门限 —— 门限会自我证明：
        判成 other 的帧不许进基准 ⇒ 基准永远只由 self 帧构成 ⇒ 门限永远够不着。
        **机器会安静得像坏了一样，而且一句报错都没有。**
        包络是我们自己算的、和判据无关，用它才是一个独立的尺子。"""
        if mine_loud and verdict != 'quiet' and score > 0:
            self.selfs.append(score)
            if len(self.selfs) > self.window:
                self.selfs.pop(0)

    def thr(self):
        """现算门限。self 帧还不够时回落冷启动常数（宁可保守，别乱打断）。"""
        if len(self.selfs) < 5:
            return THR_SELF
        import numpy as np
        return float(np.median(self.selfs)) - self.margin

    def stats(self):
        """★ 必须说清门限是【现算的还是冷启动的】—— 这两种情况的含义完全不同：
        现算的说明"这个房间的基准我摸到了"；冷启动的说明"我还没摸到，用的保守值"。
        日志里混为一谈的话，事后看根本分不出判据是在正常工作还是在降级。"""
        if len(self.selfs) < 5:
            import numpy as np
            return ('基准还没攒够（%d/5 帧%s）⇒ 用冷启动门限 %.3f'
                    % (len(self.selfs),
                       ('，中位 %.3f' % float(np.median(self.selfs))) if self.selfs else '',
                       THR_SELF))
        import numpy as np
        return ('基准 %.3f（%d 帧）⇒ 现算门限 %.3f'
                % (float(np.median(self.selfs)), len(self.selfs), self.thr()))


def latency(win=None, hop=None, need=None):
    """从"主人开口"到"我们认出来"要多久（秒）。

    ★ 这个数必须摆在明面上，因为它决定打断"像不像人"：
        (need-1) × hop + win  =  (3-1)×0.25 + 1.0 = **1.5 秒**
    1.5 秒对"你先听我说"是偏慢的（人能感到那一顿）。要压到 1 秒以内只有两条路：
      · 窗缩到 0.75s + need 2 ⇒ 1.0s —— 但窗越短，self 基准本身越抖（实测 0.5s 才 0.65）；
      · ★ 更好的那条：**我们自己的停顿期用短窗**。停顿那 0.4 秒里我们自己几乎不出声，
        那一窗只有主人，短窗又快又准 —— 而"我什么时候停顿"我们**本来就知道**（包络）。
    第二条是下一步，等真房间的数据到了再定阈值。
    """
    win = WIN if win is None else win
    hop = HOP if hop is None else hop
    need = NEED if need is None else need
    return (need - 1) * hop + win


def watch(pcm, ref, win=None, hop=None):
    """整段音频 → [(起点秒, 判定, 分数)]。给离线复盘和自测用。"""
    win = win or WIN
    hop = hop or HOP
    out = []
    step = int(16000 * hop)
    n = int(16000 * win)
    for i in range(0, max(0, len(pcm) - n + 1), step):
        v, s = judge(pcm[i:i + n], ref)
        out.append((i / 16000.0, v, s))
    return out


class Debounce:
    """★ 去抖：连续 NEED 窗都判 other 才放行。

    为什么必须有：单窗会因噪声、混响尾巴、模型抖动偶尔跳到 other。
    打断是要**停下正在说的话**去让路 —— 一次误触发就是白白打断自己，
    主人会觉得"这音箱一惊一乍"。宁可慢 0.5 秒（NEED 窗 × HOP），
    也别一惊一乍。
    """

    def __init__(self, need=None):
        self.need = need or NEED
        self.n = 0

    def feed(self, verdict):
        if verdict == 'other':
            self.n += 1
        else:
            self.n = 0
        return self.n >= self.need

    def reset(self):
        self.n = 0


# ---------------------------------------------------------------- 自测 / 标定
def calib():
    """重跑一遍标定 —— 换了嗓子/换了模型之后，用它确认判据还立得住。

    ★ 全程在文件上跑，**一点声音都不放**（主人明令：屋里有人时，测试不许吵到人）。
    """
    import glob
    import numpy as np
    if not ready():
        print('✗ 声纹用不了：%s' % why_not())
        return 1
    import spk_ai_dlna as dlna       # ★ 只借它的 tts()（同一条嗓子、同一条增益链）
    mine = [dlna.tts(t, out='/tmp/vp_c%d.mp3' % i) for i, t in enumerate(
        ['好的，我记下了。明天早上七点半叫你起床，提前十分钟。',
         '今天天气不错，要不要出去走走？我在家等你回来。'])]
    emb = [embed(pcm_of(p)) for p in mine]
    print('\n① 同一嗓子、不同句子（应当很高）:  sim = %.3f' % sim(emb[0], emb[1]))

    humans = []
    for p in sorted(glob.glob(os.path.join(HERE, 'asr', '*', 'test_wavs', 'zh_*.wav'))):
        v = pcm_of(p)
        if v is not None and len(v) >= 16000 * 2.0:
            humans.append((os.path.basename(p), v))
    print('② 我们 vs 真人（%d 段，应当很低）:' % len(humans))
    for name, v in humans:
        print('     %-10s sim = %+.3f' % (name, sim(emb[0], embed(v))))

    # ★★★ ③ 和 ④ 【必须同一段、同一归一化】。
    # 踩过的坑：③ 取 me[1.0s:] 当基准、④ 却取 me[:0s] 做混音 —— 两段不同的音频，
    # 于是"真人比我们小 24dB、几乎什么都没加"那格也掉到 0.397，看起来像判据很灵，
    # 其实是拿错了段。**混音实验里唯一的变量必须只有那个真人。**
    # 所以下面 ④ 多印一列「无人对照」：它和 ③ 同窗长时必须几乎相等，
    # 不等就说明这段音频/这个归一化本身有问题，那张表一个字都不能信。
    BASE = 1.0                       # 从第 1 秒起取，避开开头的起音气口
    me = pcm_of(mine[1])
    print('③ 纯自己的相似度随窗长（窗太短会自己都不像自己）:')
    for w in (0.5, 1.0, 2.0):
        n = int(16000 * w)
        seg = me[int(16000 * BASE):int(16000 * BASE) + n]
        print('     窗 %.1fs  sim = %.3f' % (w, sim(embed(seg), emb[0])))

    def _rms(x):
        return float(np.sqrt((np.asarray(x, dtype=np.float32) ** 2).mean()))

    print('④ 混入真人（比值 = 真人相对我们的电平）—— 门限该定在哪:')
    print('     %-6s%11s' % ('窗长', '无人对照')
          + ''.join('%9s' % ('%+d dB' % r) for r in (0, -6, -12, -18, -24)))
    for w in (0.5, 1.0, 2.0):
        n = int(16000 * w)
        raw = me[int(16000 * BASE):int(16000 * BASE) + n]
        a = raw / (_rms(raw) + 1e-9)               # 归一化后当"我们自己"那一路
        row = [sim(embed(a), emb[0])]              # ← 对照：同段同归一化，只是不加人
        for r in (0, -6, -12, -18, -24):
            ss = []
            for _, h in humans:
                m = min(len(h), n)
                b = h[:m] / (_rms(h[:m]) + 1e-9)
                g = 10 ** (r / 20.0)
                ss.append(sim(embed((a[:m] + g * b) / (1 + g)), emb[0]))
            row.append(float(np.median(ss)))
        print('     %-6s' % ('%.1fs' % w), ''.join('%10.3f' % v for v in row))
    print('\n冷启动门限 %.2f / 窗 %.1fs / 步长 %.2fs / 连续 %d 窗 ⇒ 反应延迟 %.2fs'
          % (THR_SELF, WIN, HOP, NEED, latency()))
    print('★ 判据立得住的两个条件，两条都得满足：')
    print('   ① 第 ① 行（自己跟自己）要【明显高于】门限；② 第 ② 行（真人）要【明显低于】门限。')
    print('   ③ 第 ④ 行的「无人对照」列必须≈第 ③ 行同窗长 —— 不等就说明这张表被别的因素污染了。')
    return 0


def main():
    if '--calib' in sys.argv or not sys.argv[1:]:
        return calib()
    args = sys.argv[1:]
    if '--sim' in args:
        i = args.index('--sim')
        a, b = args[i + 1], args[i + 2]
        print('%.3f' % sim(embed(pcm_of(a)), embed(pcm_of(b))))
        return 0
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main())
