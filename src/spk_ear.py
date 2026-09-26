#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_ear.py —— 音箱的耳朵和脑子（常驻）

    音箱 netease_voice ──mictap.so──> UDP:9998 ──> 本进程【唯一主人】
                                                     ├─ 原样转发一份到 127.0.0.1:9999
                                                     │  （say() 的自听耳朵会退到那儿，见 spk_ai_dlna.Ear）
                                                     └─ ① KWS 一直筛唤醒词 → 命中
                                                        ② 跳过唤醒应答那声"叮"
                                                        ③ VAD 守着切句：说完了就收
                                                        ④ 本地 ASR → 文字
                                                        ⑤ spk_skills 带工具问模型：它自己决定
                                                           是【直接回答】还是【调工具动手】
                                                           （工具在本地白名单里执行，结果回传给它）
                                                        ⑥ spk_voice 合成 → say() 播回去

★ 全程不碰网易：这条链上没有一个字节经过 163。设备自己那条 vbox-asr 通道另外在 DNS 上黑洞掉。
★ 为什么 9998 是"抢"来的：mictap 只往 9998 发，而 UDP 上两个都带 SO_REUSEADDR 的 socket
  能同时绑同一端口、包却只进其中一个 —— 那种"耳朵起来了却收不到包"最难查。
  所以这里的规矩是：本进程独占 9998，转发一份给 9999；say() 撞到 EADDRINUSE 自动退号。

自测（不用人开口，也不用人帮忙）：
    .venv/bin/python spk_ear.py --wav asr/kwtest/嘀嗒嘀嗒-.wav        只验"听→懂"
    .venv/bin/python spk_ear.py --text "现在几点了"                    只验"懂→说"
    .venv/bin/python spk_ear.py --pipe '嘀嗒嘀嗒-.wav:现在几点了'      整条链，声音走 DLNA 从喇叭里出来
    .venv/bin/python spk_ear.py --wav xx.wav --who --dry              顺便验【认人】（不出声）

★ 认人（SPK_WHO=1）默认关着；`--who` 是它唯一的离线入口，不受那个开关限制。
"""
import argparse
import os
import queue
import socket
import sys
import threading
import time
import wave

import numpy as np
import sherpa_onnx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _cfg import PORT_MICTAP       # noqa: E402

import mictap_sink as mit          # 包格式只在那里定义一次
import spk_ai_dlna as dlna
import spk_agent as agent          # 只为 _key()（三级回退那一处定义，别抄第二份）
import spk_help as helpq           # 求助队列（读"装成了什么、报过没有"；纯 stdlib，无副作用）
import spk_skills as skills        # 脑子：提示词 + 工具白名单 + 执行回路
import spk_session as sess         # 一次"会话"的状态（连着聊）
import spk_memory as mem           # 收工固化：归档 + 挑重要的记
import spk_said                    # ★ 我们【刚说出去的话】的共用登记簿（回灌闸门的第二对照）
import spk_tts as tts              # 常驻连接的 TTS（快路）—— 只预热+日志，说话仍归 dlna.tts()

# ---------------------------------------------------------------- 配置
SR = 16000
PORT = int(os.environ.get('MICTAP_PORT', PORT_MICTAP))
RELAY = ('127.0.0.1', PORT + 1)

KWS_DIR = os.path.join(HERE, 'asr', 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20')
ASR_DIR = os.path.join(HERE, 'asr', 'sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30')
VAD_ONNX = os.path.join(HERE, 'asr', 'silero_vad.onnx')
KW_FILE = os.path.join(HERE, 'asr', 'kw', 'keywords.txt')

# 实测扫出来的档位：chunk-8 + 阈值 0.25 时三个样本全命中，chunk-16 漏一个
KWS_CHUNK, KWS_THR = 8, 0.25

# ★★★★★ 2026-09-21 夜【退役：这个盲丢窗口就是「我问几点了 他没回答」的真凶】
#   它原来的职责是"唤醒后先躲开设备那声叮"。可那声叮早被 voicemute.sh 顶哑了
#   （固件 S003 等 90 条都被盖掉）⇒ 这个窗口唯一还在丢的，就是
#   【主人唤醒词后面紧接着说的那句话】。
#
#   主人三次报同一个毛病，三次都是同一句「几点了」、都紧接唤醒词、中间没有停顿：
#     21:45  「我问几点了 他没回答」                        → ASR 空
#     22:24  主人 :49 开口 → 设备 :50 才认出来 → 盲丢 1.0s → 说完 0.64s → ASR 空
#     22:41  🔔 设备引擎认出 → 盲丢 1.0s → 说完 0.67s      → ASR 空
#
#   后果链（三样症状一个根）：
#     盲丢 1.0s ⇒ 「几点了」整段进垃圾桶 ⇒ `record()` 的 VAD 只切到 0.67s 残渣
#     ⇒ ASR 吐空串 ⇒ `turn()` 在 `not text` 处早退 ⇒ **大模型根本没被调用**
#     ⇒ 既没有回答，也不可能有思考音。
#
#   ★ 22:24 那次我只把 `_drain` 改成按电平扔，却只接到了 `wake_from_device` 的
#     **会话中途**分支上 —— **首次唤醒这条路（session_loop / cycle）一直是盲丢的**。
#     "改了一个调用方就以为改完了"，正是 `_drain` 那段注释警告过的错。
#   ⇒ 现在两处都改：`_flush()`（只清已攒着的尾音，无未来窗口）+ `_ack_until` 电平窗口。
#   ★ 常量留着只为兼容旧环境变量，**任何地方都不许再拿它做醒后丢弃**。
WAKE_SKIP = float(os.environ.get('SPK_WAKE_SKIP', '1.0'))   # ★ 已退役，勿再用于醒后丢弃
# ★★ 实测（2026-09-21 21:45:47，主人报"我问几点了 他没回答"那一次）：
#     主人说话   峰值 0.010~0.018  （-35dBFS，说了 2 秒多）
#     设备应答   峰值 0.28~0.55    （ -5dBFS，0.42 秒）
#   ⇒ **设备自己的声音比主人响 30 倍**，VAD 永远先抓到它：那次把应答录成
#     0.42 秒的"一句话"，ASR 转出个「见」，音箱就着这个字回了一句。
#   所以唤醒后那段闭嘴期 **会话中途也必须算** —— 原先只有 session_loop 开头躲一次，
#   会话中途再喊唤醒词那一下是完全敞着的。ACK_SKIP 就是补这个。
ACK_SKIP = float(os.environ.get('SPK_ACK_SKIP', '1.0'))     # 每次唤醒后躲应答的窗口时长
# ★★★ 2026-09-21 22:24【现场抓到：那一秒在吃主人的话，不是在躲应答】
#   证据（被动录音 + journald 对齐，每格 100ms）：
#     22:24:49 我们的 VAD 开始攒 —— 主人在说「嘀嗒嘀嗒」
#     22:24:50 设备 KWS 才认出来 ⇒ 躲避 1.0s 开始
#     22:24:51 说完，0.64 秒 ⇒ ASR（空）  ← 他紧接着说的「几点了」全落在那 1 秒里，被扔了
#   全程最大峰值只有 0.0076 —— **根本没有应答在响**（见 voicemute.sh：S003 被顶哑）。
#   ⇒ 那一秒躲掉的自始至终是主人自己的话。原判"设备应答比主人响 30 倍"没错，
#     但那是【应答还在响的年代】；现在应答哑着，时间窗口就只剩下害处。
#   ⇒ 改成【按电平扔】：只有真响的那几帧才当应答丢掉，安静的一律放行。
#   尺度（★ 队列里的音频已经乘过 MIC_GAIN）：应答 0.28~0.55 → ×6 削顶在 1.0；
#   主人说话 0.010~0.018 → ×6 后 0.06~0.11。0.40 离主人 3.6 倍、离应答 2.5 倍。
ACK_LVL = float(os.environ.get('SPK_ACK_LVL', '0.40'))      # 判定"这帧是设备那声应答"的电平
# ★ 主人说话到麦克风只有 -35dBFS，而 ASR/VAD 那个量级基本是在听一个听不清的人。
#   这一级是【纯本机】的软件增益：设备不给 adb 了，改不了它的采集增益，
#   而 mictap 上行的就是原始 PCM，我们在这头放大一样有效。倍数可调，宁可先小。
MIC_GAIN = float(os.environ.get('SPK_MIC_GAIN', '6'))
WAIT_SPEECH = 6.0        # 唤醒了却没人说话，等这么久就放弃
LISTEN_MAX = 15.0        # 一句话最长录这么久
CHUNK = 1600             # KWS 按 100ms 一喂（官方例子就是这么喂的）

# ★ 设备【自己那个唤醒引擎】认出来的事件，从这儿进来（见 WakeTap）。
#   它和我们自己的 KWS 是【并存】的两条路，不是替换。
WAKE_PORT = int(os.environ.get('SPK_WAKE_PORT', '9997'))

# ★ ANSWER_PROMPT 已退休（2026-09-21）：它的"说话规矩"那几条并进了
#   spk_skills.system_prompt()，而且那边还多讲了它住在哪、手上有什么、什么时候动手。
#   一句身份说明撑不起一只音箱 —— 提示词该在 spk_skills 里，不该散在这里。


def log(fmt, *a):
    print('%s  %s' % (time.strftime('%H:%M:%S'), fmt % a if a else fmt), flush=True)


# ---------------------------------------------------------------- 耳朵
class Mic:
    """收 mictap 的 UDP 包 → 16k 单声道 float32 → 队列；顺带原样转发一份给 9999。"""

    def __init__(self, port=PORT, relay=RELAY):
        self.port, self.relay = port, relay
        self.q = queue.Queue(maxsize=600)      # 600×100ms = 60 秒，够任何一次卡顿
        self.drops = 0
        self.fmt_seen = None
        self.last = 0.0
        self._sock = self._relay = self._thr = None
        self._stop = False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        try:
            s.bind(('0.0.0.0', self.port))
        except OSError as e:
            log('✗ UDP %d 绑不上（%s）—— 是不是已经有一个 spk_ear 在跑？', self.port, e)
            return False
        s.settimeout(0.5)
        self._sock = s
        self._relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        return True

    def stop(self):
        self._stop = True
        if self._thr:
            self._thr.join(timeout=1.0)

    def _loop(self):
        while not self._stop:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not mit.allowed(addr[0]):
                continue
            self.last = time.time()
            # ★ 转发在解析之前：自听要的是原包，不是被我们理解过一遍的版本
            try:
                self._relay.sendto(data, self.relay)
            except OSError:
                pass
            if len(data) <= mit.HDR.size:
                continue
            magic, _ver, _seq, _frames, rate, ch, fmt, _flags = mit.HDR.unpack_from(data, 0)
            if magic != mit.MAGIC:
                continue
            key = (rate, ch, fmt)
            if key != self.fmt_seen:      # 格式只报一次 —— 变了要看得见，不是每次都刷屏
                self.fmt_seen = key
                log('🎙 流格式 %d Hz / %d ch / %s', rate, ch, mit.FMT_NAME.get(fmt, fmt))
            a = self._to16k(data[mit.HDR.size:], rate, ch, fmt)
            if a is None or not len(a):
                continue
            try:
                self.q.put_nowait(a)
            except queue.Full:
                # ★ 丢最旧的，不是丢最新的：攒着只会越听越延迟，而"延迟 10 秒的回应"比"漏 100ms"糟得多
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(a)
                except queue.Full:
                    pass
                self.drops += 1

    @staticmethod
    def _to16k(pcm, rate, ch, fmt):
        w = mit.FMT_WIDTH.get(fmt, 2)
        if w == 2:
            a = np.frombuffer(pcm[:len(pcm) // 2 * 2], '<i2').astype(np.float32) / 32768.0
        elif w == 4:
            # 设备真麦是 S24_LE，但 mictap 上行时已经 >>16 过了（见 device/mictap.c 取样那段）
            v = np.frombuffer(pcm[:len(pcm) // 4 * 4], '<i4')
            a = (v >> 16).astype(np.float32) / 32768.0
        elif w == 1:
            a = (np.frombuffer(pcm, np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            return None
        # ★ 软件增益（见文件头 MIC_GAIN）。放在声道/重采样之后，一路只加一次。
        #   ★★ 自听（转发 127.0.0.1:9999 那一份）走的是【转发原包】那条路，
        #     根本不经过这里 ⇒ 不会把自听的门限算歪。
        if MIC_GAIN != 1.0:
            a = np.clip(a * MIC_GAIN, -1.0, 1.0)
        if ch > 1:
            # ★ 取左声道：实测真机 L 恒比 R 响 2~4 倍，左是主麦
            a = a[:len(a) // ch * ch].reshape(-1, ch)[:, 0]
        if rate != SR:
            m = int(len(a) * SR / rate)
            if m < 1:
                return None
            a = np.interp(np.linspace(0, len(a) - 1, m), np.arange(len(a)), a).astype(np.float32)
        return np.ascontiguousarray(a, np.float32)


# ---------------------------------------------------------------- 引擎
def make_kws(thr=KWS_THR, chunk=KWS_CHUNK):
    tag = 'chunk-%d' % chunk
    return sherpa_onnx.KeywordSpotter(
        tokens=os.path.join(KWS_DIR, 'tokens.txt'),
        encoder=os.path.join(KWS_DIR, 'encoder-epoch-13-avg-2-%s-left-64.int8.onnx' % tag),
        decoder=os.path.join(KWS_DIR, 'decoder-epoch-13-avg-2-%s-left-64.onnx' % tag),
        joiner=os.path.join(KWS_DIR, 'joiner-epoch-13-avg-2-%s-left-64.int8.onnx' % tag),
        num_threads=2, keywords_file=KW_FILE, sample_rate=SR, feature_dim=80,
        max_active_paths=4, keywords_score=1.0, keywords_threshold=thr,
        num_trailing_blanks=1, provider='cpu')


def make_asr():
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(ASR_DIR, 'tokens.txt'),
        encoder=os.path.join(ASR_DIR, 'encoder.int8.onnx'),
        decoder=os.path.join(ASR_DIR, 'decoder.onnx'),
        joiner=os.path.join(ASR_DIR, 'joiner.int8.onnx'),
        num_threads=2, sample_rate=SR, feature_dim=80,
        decoding_method='greedy_search', provider='cpu')


def make_vad():
    c = sherpa_onnx.VadModelConfig()
    c.silero_vad.model = VAD_ONNX
    c.silero_vad.threshold = 0.5
    c.silero_vad.min_silence_duration = 0.6    # 句中的自然停顿不该被切成两句
    c.silero_vad.min_speech_duration = 0.25
    c.silero_vad.max_speech_duration = LISTEN_MAX
    c.sample_rate = SR
    c.num_threads = 1
    c.provider = 'cpu'
    return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=30)


# ★★★★★ 2026-09-21 夜定案：ASR 前面必须抬电平 —— 这就是「我问几点了 他没回答」的最后一块拼图
#
#   现场（真机、主人那句「几点了」、`/tmp/spk_ear_last.wav` 原样留证）：
#       原样     rms 0.0157（峰值 1.0000）        → ''
#       ×3       rms 0.0418                       → ''
#       ×10      rms 0.1373                       → '几点了'   ★ 认出来了
#       ×30      rms 0.3814                       → '几点了'
#       ×100     rms 0.6484                       → '你这个'   （过载，反而糊）
#   ⇒ **音频里明明就是「几点了」，ASR 只是听不见它。** 门限在 rms ≈ 0.14 上下。
#   主人的话到麦克风是 -50dBFS 量级（真机 rms 0.0026），× MIC_GAIN 6 = 0.0157 —— **差 10 倍**。
#
#   ★★ 必须按 **RMS**，绝不能按峰值。这段音频头上带一个满量程尖峰（pk=1.0000，
#      麦克风路径的产物，不是话）。按峰值归一化 = 除以 1.0 = 把整段**往下压** ——
#      实测那条路正是「归一化到峰值 0.5 → ''」。**尖峰会把按峰值的做法变成反效果。**
#   ★ `ASR_GAIN_MAX` 是防"把一段纯静音放大成噪声再让 ASR 编出字来"。
#   ★ 只动喂给 ASR 的这份拷贝 —— 声纹那条链用的是另一份 `audio`，一个字都不改。
ASR_RMS = float(os.environ.get('SPK_ASR_RMS', '0.20'))          # 抬到这个 RMS 就停
ASR_GAIN_MAX = float(os.environ.get('SPK_ASR_GAIN_MAX', '40'))  # 放大倍数上限

# ★★★ 2026-09-21 深夜：VAD 交回来的片段，起点要【往前多够一截】。
#   现场（真机，同一句「几点了」，三连测）：
#       0.77s → 「几点了」      ✓
#       0.58s → 「一点」        ✗ 开头丢了
#       0.51s → （空）          ✗ 整句废掉
#   一句话本来就有 0.7~0.9 秒。⇒ **VAD 报的 `seg.start` 是它"判定成了语音"的那一帧，
#   不是人真正开口的那一帧** —— 它要攒够几帧才敢下判断，那几帧就是被切掉的开头。
#   silero 又是逐 512 样本（32ms）推进的，起判点天然带着几十到几百毫秒的滞后。
#   ★ 往前多够 0.30 秒：多出来的只会是静音/底噪（ASR 对前导静音无所谓），
#     而不够的话丢的是【声母】—— 那正是「几点了」变成「一点」的原因。
VAD_PAD_BACK = float(os.environ.get('SPK_VAD_PAD_BACK', '0.30'))
VAD_PAD_N = int(VAD_PAD_BACK * SR)


def _loud(a):
    """按 RMS 把一段话抬到 ASR 听得见的电平。返回 (音频, 实际倍数, rms, 非有限值个数)。"""
    if a is None or not len(a):
        return a, 1.0, 0.0, 0
    x0 = np.asarray(a, np.float32)
    # ★★★★★ 2026-09-21 深夜：这一行是整晚那个谜团的答案。
    #   真机日志第一次开口就说了实话 —— `🔊 ASR 前：rms 6918204.50000  ×1.0`。
    #   **不是 Inf/NaN，是少数几个天文数字的样本**（几个 1e7~1e37 的值就够把整段 rms
    #   拉到百万级，而其余样本完全正常）。上游 = sherpa VAD 的 `seg.samples` 悬空内存，
    #   已在 seg_audio() 里从源头掐掉。
    #   ★ 它凭什么能藏一整晚：`k = min(0.20/6918204, 40) = 3e-8` ⇒ `k <= 1.0` ⇒ 直接 return
    #     ⇒ **既不抬电平、也不满足 `k > 1.5`** ⇒ 那行日志不打。脏波形进 ASR ⇒ 吐空
    #     ⇒ `turn()` 早退 ⇒ **大模型压根没被调用**（没回答、也没思考音）。
    #   两件教训，都立成铁律：
    #     ① **判据里带 NaN 的比较永远为假** ⇒ 日志条件会被静默吃掉。
    #        所以下面那行日志已改成【无条件打印】，不许再有第二个隐身衣。
    #     ② 上游一旦可能给出格数据，**下游就必须留一道量级闸门**（见下面 r > 1 那条）。
    bad = int((~np.isfinite(x0)).sum())
    x = np.nan_to_num(x0, nan=0.0, posinf=0.0, neginf=0.0)
    r = float(np.sqrt((x ** 2).mean()))
    if r > 1.0:
        # ★ 真实音频在 Mic 那一级就被钳进了 [-1,1]，rms 绝不可能 > 1
        #   ⇒ 唯一来源是上游的脏数据。硬钳 + 大声报出来 —— 绝不静默放过，
        #   因为"静默"正是它藏了一整晚的原因。
        log('   ⚠⚠ 音频 rms %.3g > 1 —— 上游给了脏数据，已硬钳到 [-1,1]', r)
        x = np.clip(x, -1.0, 1.0)
        r = float(np.sqrt((x ** 2).mean()))
    if r < 1e-6:
        return x, 1.0, r, bad                # 纯静音：不放大（放大了只会喂噪声给 ASR）
    k = min(ASR_RMS / r, ASR_GAIN_MAX)
    if k <= 1.0:
        return x, 1.0, r, bad                # 本来就够响：一个采样都不碰
    return np.clip(x * k, -1.0, 1.0), k, r, bad


def seg_audio(seg, stream):
    """VAD 交回来的片段 → 音频。★ 绝不读 `seg.samples` 的【值】。

    ★★★★★ 2026-09-21 深夜定案 —— 这就是主人「我问几点了 他没回答、也没有思考音」的真凶。

    `sherpa_onnx 1.13.8` 的 `SpeechSegment.samples` 会【非确定性地】返回一块
    未初始化内存。实测同一个 wav 连跑 8 次：7 次 max=0.524（正常），1 次 **max=8.5e+37**；
    再连跑 3 次又变成 2 次 **max=3.4e+10**、1 次正常。同一块垃圾每次数值还不一样。
    垃圾那次 `corr(seg.samples, 真实音频)` = **-0.0000** —— 那些值根本不是音频。

    ⇒ 后果链（每一环都实测过）：
        偶尔拿到 1e7~1e37 的样本 ⇒ `rms` = **6918204**（真机日志原话）
        ⇒ `k = min(0.20/rms, 40) ≈ 3e-8` ⇒ `k <= 1.0` ⇒ **不抬电平，脏波形直接喂 ASR**
        ⇒ ASR 吐空 ⇒ `turn()` 早退 ⇒ **大模型压根没被调用**（所以既没回答也没思考音）。
      ★ 而被它连累的还有我自己的判断：写盘那句 `np.clip(audio,-1,1)*32767` 会把
        天文数字【削平成 ±1.0】—— `/tmp/spk_ear_last.wav` 上只看得到几个"满量程尖峰"，
        真相被那一行毁了。我因此一度误判成"电平太低"，还写了套抬电平的测试
        ——**测试读的是文件，而文件里病因已被毁尸灭迹，所以它永远测不出来。**

    ★ 可信的两样：`seg.start`（纯 int，每次都是同一个值）和 `len(seg.samples)`（长度稳）。
      拿它们去切【我们自己攒的流】，实测 **corr = 1.0000**，与真片段逐样本一致。
    """
    n = 0
    try:
        n = len(seg.samples)          # ★ 只取长度，不取值
    except Exception:
        pass
    st = int(getattr(seg, 'start', 0) or 0)
    if stream is not None and len(stream) and n > 0:
        # ★ 往前多够一截 —— `seg.start` 是 VAD【判定】的那一帧，不是人【开口】的那一帧。
        #   不补的话丢的是声母（真机实测：「几点了」→「一点」→ 空）。见 VAD_PAD_BACK。
        #   终点仍按【原】起点 + 长度算，别让钳到 0 的那一截把尾巴也拉长。
        a = stream[max(0, st - VAD_PAD_N):st + n]
        if len(a) >= n * 0.9:
            return np.ascontiguousarray(a, np.float32)
        if len(a) > 0:
            log('   ⚠ VAD 片段要 %d 个样本、我们只攒到 %d —— 就用这些（总比读悬空内存强）',
                n, len(a))
            return np.ascontiguousarray(a, np.float32)
    # 兜底：本地流没攒着（不该发生）—— 退回去读 seg.samples，但必须钳进 [-1,1]
    v = np.asarray(seg.samples, np.float32)
    log('   ⚠ 没有本地流可切，退回读 seg.samples（已硬钳到 [-1,1]）')
    return np.clip(np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)


# ★★★★★ 非人声闸门 —— 2026-09-22 深夜，主人原话：「**非人声就别回复了 咳嗽也回思考音**」。
#
#   现场（21:34:57）：屋里一声咳嗽 ⇒ VAD 判"说完" ⇒ **思考音已经推出去了**（早跑在 ASR 之前）
#   ⇒ ASR 硬给它转出一个字「爱」⇒ 脑子一本正经回一句"嗯？没太听清，你是想说什么呀？"。
#   主人的要求是两半：**咳嗽不许有思考音、非人声不许有回答**。
#
#   为什么不用音量：咳嗽那一轮实测 rms **0.0956** —— 比真说话（"几点了" 0.02~0.06）**还响**。
#   用**周期性**（归一化自相关的峰）：人说话（哪怕一个"嗯"）是**浊音** —— 声带振动
#   ⇒ 波形以基音周期重复 ⇒ 自相关在 70~350Hz 那一带有高蜂（实测 >.5）；
#   咳嗽 / 磕碰 / 放杯子 / 键盘是**宽带噪声** ⇒ 那一带没有峰（<.2）。
#   判据 = **出声帧里"带峰"的帧占多少**（0~1）。
#
#   ★★ 三道"宁可不压"的保险（这条判据只用来【少出声】：误杀的代价远大于漏判 ——
#     把他一句真话静默吞掉，比让他多听一声"唔……"糟得多）：
#     ① 算不出来 / 出不了结论一律回 **1.0**（= 像人声 ⇒ 不压），**绝不抛**。
#     ② 出声帧太少（< 4 帧，几乎全静音）⇒ 回 1.0 —— 那种情况交给"ASR 空就不回"兜。
#     ③ 门槛取得**低**（.25）：四分之一的帧是浊的就算人声。气声、耳语、小声说话
#        都是"浊音少但绝不是噪声"，绝不能被当成咳嗽。
#   ★ 打分**无条件打印**（见 turn()），拿真机的数校正门槛 —— 别拿估的当实测。
SPEECH_LIKE = float(os.environ.get('SPK_SPEECH_LIKE', '0.25'))
_PERIOD_MIN = float(os.environ.get('SPK_PERIOD_MIN', '0.35'))
# ★ "非人声 + 只转出这么几个字" ⇒ 当噪声，不回。咳嗽那轮的「爱」是 1 个字。
#   为啥还看字数：ASR 对噪声的幻觉**通常是单字/双字**（"爱""嗯""啊"），而真人的
#   "嗯/好/停/对"是**浊音**（过得了上面那道判据）—— 两条判据同时成立才丢，
#   所以短应答的确认流（`ask_user` / 点头）不会被误伤。
NONSPEECH_CHARS = int(os.environ.get('SPK_NONSPEECH_CHARS', '2'))


def speech_like(audio, frame=0.032, hop=0.016, f_lo=70.0, f_hi=350.0, rms_min=0.008):
    """这段音频【像不像人说话】：0~1 = 出声帧里带基音周期的帧占比。

    ★ 只做一件事、只有一个出口原则：**拿不准就当人声**（回 1.0 ⇒ 不压）。
      这条判据是拿来【少出声】的，所以它的错误方向必须是"漏判噪声"。
    ★ 成本：1 秒音频 60 帧 × 自相关(512) —— 实测几十毫秒，跑在"说完"那一刻，
      够便宜（早跑那条梯子因此晚 30ms 左右出门，换来咳嗽不出声）。
    """
    try:
        x = np.asarray(audio, np.float32)
        if x.ndim > 1:
            x = x.mean(axis=1)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        n, h = int(frame * SR), int(hop * SR)
        if n <= 0 or len(x) < n * 2:
            return 1.0
        lag_lo, lag_hi = max(1, int(SR / f_hi)), int(SR / f_lo)
        tot = voiced = 0
        for i in range(0, len(x) - n + 1, h):
            f = np.asarray(x[i:i + n], np.float32)
            if float(np.sqrt(float(np.dot(f, f)) / n)) < rms_min:
                continue                       # 静音帧：不算分母
            tot += 1
            f = f - f.mean()
            d = float(np.dot(f, f))
            if d <= 1e-12:
                continue
            c = np.correlate(f, f, 'full')[n - 1:] / d     # 归一化自相关，lag0=1.0
            if lag_hi < c.size and float(c[lag_lo:lag_hi + 1].max()) >= _PERIOD_MIN:
                voiced += 1
        if tot < 4:
            return 1.0                         # 几乎全静音：不判，交给空 ASR 兜
        return voiced / float(tot)
    except Exception as e:                              # noqa: BLE001
        log('   ⚠ 非人声判据算不出来（%s: %s）⇒ 当人声，不压', type(e).__name__, e)
        return 1.0


def asr_text(rec, a):
    """一段音频 → 文字。★ 尾补静音 + input_finished：流式模型不这么喂，最后一个字常被吞掉。

    ★ 门口先抬电平（见上面那段定案）—— 放在这里是因为它是 ASR 的【唯一咽喉】：
      主循环那一句、预卷那句、自测那句，三处一次性全管。
    """
    a, k, rms_in, bad = _loud(a)
    # ★ 无条件打印（原来只在 k>1.5 时打 ⇒ Inf/NaN 两条路都不打 ⇒ 病灶隐身一整晚）
    log('   🔊 ASR 前：rms %.5f  ×%.1f%s', rms_in, k,
        ('  ★★ 含 %d 个 Inf/NaN —— 已清零！' % bad) if bad else '')
    s = rec.create_stream()
    s.accept_waveform(SR, a)
    s.accept_waveform(SR, np.zeros(int(SR * 0.5), np.float32))
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    r = rec.get_result(s)
    # ★ 这个版本 get_result 返回的是 str（旧版是带 .text 的对象）—— 两种都认
    return (r.text if hasattr(r, 'text') else str(r or '')).strip()


# ★★★★★ 2026-09-23：ASR 流式化 —— 「音箱也压到地板 流式识别器用对」
#
#   病根：`asr_text()` 把【整段】音频一次性喂给 `sherpa_onnx.OnlineRecognizer`
#   —— 那是个【流式】模型。后果：整段解码全压在"主人说完之后"。
#   实测 14 天 190 轮：ASR 中位 0.40s / p90 1.0s / 最长 4.0s，
#   而 **p25 == 中位 == 0.40s** ⇒ 这一截主要是【固定开销】，不是与话长成正比。
#   （其中约 0.20s 就是在解码 `asr_text` 自己补的那 0.5 秒假静音，实测吞吐 2.44× 实时。）
#
#   ★ 同一个缺陷在另一个壳那边已经修过（那套的 `LiveAsr`，ASR 0.7~2.5s → 0.25s），
#     但那个修法【只活在那个壳里】，音箱一分钱好处没拿到。这个类就是在音箱这边补上。
#
#   修法：音频【一边来一边喂】，主人停下时只剩"补静音 + input_finished + 取结果"。
#   唯一的真障碍是 `asr_text` 门口的 `_loud()` —— 它是【整句一个标量增益】，因果上拿不到。
#   实测（37 段真机录音）这道障碍是纸的：
#       用「开口后 0.4 秒」估增益 → 中位偏差 +0.0dB，p10/p90 −2.9/+3.5，16% 超 3dB
#       用「开口后 0.8 秒」估增益 → 中位偏差 +0.0dB，p10/p90 −1.8/+1.0，**只有 3% 超 3dB**
#   而 `_loud` 自己的定案注释给了【容差】：rms 0.137（×10）与 0.381（×30）【都】转得出
#   「几点了」，0.648（×100）才糊 ⇒ 平台宽 **±8.9dB**。
#   ⇒ **±1.8dB 落在安全区里。这是量出来的，不是猜的。**
#   ★ 落到实现上，第一次喂出去时增益窗口就是 `SPK_ASR_GBASE`(0.63s) —— 其中前
#     0.30s 是前导静音、其余是人声（与 `_loud` 看到的开头同一段，见规矩②）。
#     实测线上窗口估出的增益比 `_loud` 高约 2dB，仍在平台上。
#
# ★★★★★ 三条硬规矩（写死在这里，别靠记性）
#
#   ① 喂进去的样本必须【逐样本等于 `np.concatenate(buf)`】—— 也就是 `seg_audio()`
#      切出来的那条流。`accept_waveform` 只是往流里追加样本，所以这一条是【结构上】
#      保证的：同一批 `buf` 对象、同一个顺序，切点不影响结果。
#      ⇒ 「转出来的字不变」不是靠祈祷。
#
#   ② ★ 开口之前【一个字节都不喂】。门就是 `record()` 里【已经存在】的那个信号
#      `self.vad.is_speech_detected()`，喂的时机 = 它那一包的【上升沿】。
#      ★★★ 但上升沿【比 `seg.start` 晚 0.33 秒】—— 这是 2026-09-23 实测出来的
#      （110 段真机录音：中位 +0.330s，p10 0.314 / p90 0.338，极稳）。
#      不是噪声，是结构性的：silero 要攒够 `min_speech_duration=0.25` 才敢报。
#      而 `seg_audio` 是从 `seg.start − VAD_PAD_BACK(0.30)` 切的
#      ⇒ 批式切片的起点【落后上升沿 `VAD_PAD_BACK + VAD_LAG` ≈ 0.63 秒】。
#      ⇒ **往前必须补足这 0.63 秒**，补少了就是每一句都丢开头。
#      ★ 这条我第一版写错过：当时只补 0.10 秒（把"上升沿比 seg.start 晚"记反了方向），
#        A/B 立刻抓出来 —— `现在几点了`→`几点了`、`今天天气怎么样`→`气怎么样`、
#        `至上午十点零六分`→`十点零六分`，全是【掐头】，而参照实现与线上类
#        0 处不一致 ⇒ 说明是【设计】错了，不是实现错了。
#        所以补的秒数现在是【推导出来的】（`VAD_PAD_BACK + VAD_LAG`），不是拍的。
#      ⇒ 补完之后，流式的起点与 `seg_audio()` 的起点【对齐】（差 0 秒）。
#      ★ 上升沿之后【锁存】：VAD 允许 0.6s 句内停顿（min_silence_duration），
#        那 0.6 秒绝不能断 —— 断了就是把主人的话拦腰切开。
#      ★ 这一条同时是【省 CPU 的关键】：不门控就要替唤醒后那段静音
#        （WAIT_SPEECH 6s / LISTEN_MAX 15s）白解码。
#
#   ③ `cut()` = 补喂余量 → 补 0.2s 静音 → `input_finished()` → 解码 → 取结果。
#      与 `asr_text` 同构，只少了那 0.5 秒【假】静音（真静音垫由 VAD 那 0.6 秒
#      尾静音提供 —— 它本来就在 `seg_audio` 切出来的那段里）。
#
#   ★ `cut()` 之前【没有独立线程】—— 喂和解码都发生在 `record()` 那个循环里。
#     这是故意的：另一个壳那边的 LiveAsr 带一个线程，就要管它的生死（忘了 close 会炸
#     `terminate called without an active exception`）。这里全程同步，没有那个面。
#
#   ★★★ 一处【必须】退回批量：`record(prepend=...)` 那条路。那时 `turn()` 拿到的
#     `audio` 是 `prepend + 这一句`（见 `record()` 的 done()），而流里只有"这一句"
#     ⇒ 用流式结果会把主人【被打断时说的那半句开头】丢掉，正是"你先听我说"只剩
#     "说"那个老 bug。见 `turn()` 里的 `pre is None or not len(pre)`。
SPK_ASR_LIVE = os.environ.get('SPK_ASR_LIVE', '1') not in ('0', 'no', 'false', '')
# ★★★ 上升沿比 `seg.start` 晚多少 —— 2026-09-23 实测 110 段：中位 0.330s
#   （p10 0.314 / p90 0.338）。它是结构性的（silero 要攒够 min_speech_duration），
#   所以敢当常量用。见上面规矩②那一整段。
VAD_LAG = float(os.environ.get('SPK_VAD_LAG', '0.330'))
# 往前补多少：批式切片往前够的那一截（VAD_PAD_BACK）+ 上升沿自己晚的那一截（VAD_LAG）。
# ★ 写成【推导】而不是写死 0.63 —— `VAD_PAD_BACK` 哪天调了，这里必须跟着走。
SPK_ASR_GBASE = float(os.environ.get('SPK_ASR_GBASE', '%.3f' % (VAD_PAD_BACK + VAD_LAG)))
GB_N = max(1, int(SR * SPK_ASR_GBASE))     # 上面那个秒数换算成样本（往上补的窗口）
# ★ 落后最新音频多少秒才喂（回撤）。**默认 0**：当初设它是为了"第一次喂出去时
#   估增益的窗口已经有 0.5 秒"，而那条需求现在由 `SPK_ASR_GBASE`(0.63s) 自己满足了
#   —— 再回撤只是白白把喂的时机推后，短句上会丢掉流式的好处。留着这个旋钮备用。
SPK_ASR_HOLD = float(os.environ.get('SPK_ASR_HOLD', '0.0'))
# ★★★ `cut()` 末尾补多少秒静音。**这不是可有可无的**——2026-09-23 实测：
#   把同一段音频按 100ms 流式喂进去（即便增益用整段的精确值、**且没有真尾静音**），
#   **最后一个字会被吞掉**：`你能听到我说话吗`→`你能听到我说话`、
#   `现在几点了`→`现在几点`、`今天天气不错我们聊两句`→`今天天气不错我们聊两`。
#   这正是 `asr_text()` docstring 里那句"尾补静音：流式模型不这么喂，最后一个字
#   常被吞掉"（老路补的是 0.5 秒）。所以这一条【不能靠推理省掉，只能量】。
#
# ★★★★★ 但它【不是】线上护住尾巴的那个东西 —— 2026-09-23 横扫（112 段真机音频）：
#       尾垫  0.00 / 0.10 / 0.20 / 0.35 / 0.50
#       吞尾      0     0     0     0     0     ← 全零
#       一致  99/112 99/112 98/112 98/112 98/112
#   连 **0.00 都一个尾巴没吞** ⇒ 护住尾巴的是 `make_vad()` 的
#   **`min_silence_duration = 0.6`**：VAD 是攒够 0.6 秒静音才 pop 的，
#   那 0.6 秒【真静音】在 `record()` 里已经喂进去了 —— 比任何假垫子都实在。
#
# ★ 那为什么还留 0.2 不归零？因为 **pop 还有另一条路**：VAD 的
#   `max_speech_duration = LISTEN_MAX`(15s)。主人一口气说满 15 秒被**截断**时，
#   尾部【没有】真静音，这个垫子就是唯一的东西；而且保险丝也兜不住它
#   （流式会吐出一个"少了尾字"的**非空**结果，`turn()` 的 `if not text` 不触发）。
#   这一条罕见，但代价只有 0.2/2.44 ≈ **0.08 秒**解码 —— 值得买。
SPK_ASR_TAIL = float(os.environ.get('SPK_ASR_TAIL', '0.2'))


def _chunks(a, n=CHUNK):
    """把一包音频切成 ≤CHUNK 的片。★ 切点不影响识别结果（见规矩①），
    切一刀只是为了给解码一个上界：设备一包可能远大于 100ms，
    整包喂进去就会来一次长的解码停顿，把 `_drain(timeout=0.3)` 那一路堵住。"""
    a = np.asarray(a, np.float32)
    if len(a) <= n:
        yield a
        return
    for i in range(0, len(a), n):
        yield a[i:i + n]


class LiveAsr:
    """把 `asr_text` 的"整段解码"摊进"主人还在说"里。定案与三条硬规矩见上面那段。

    用法（与 `record()` 的出口一一对应）：
        L = LiveAsr(self.asr)                      # 与 self.vad.reset() 同一处
        L.feed(a, self.vad.is_speech_detected())   # 每收到一包就调；喂不喂它自己判
        text = L.cut()                             # ★ 只有 why == '说完' 才调
        L.ok                                       # 这条流还能不能要（出过错就 False）
    """

    def __init__(self, rec):
        self.rec = rec
        self.why_not = ''          # 转不出字时把原因说清楚（turn() 打日志用）
        self.ok = True
        self.reset()

    def reset(self):
        """丢弃当前流、开一条新的。★ 丢弃【不产出】—— 与另一个壳那边同一条纪律。

        ★ `SPK_ASR_LIVE=0` 时直接把 `ok` 置 False ⇒ `feed()` 立刻返回、`cut()` 返回 ''
          ⇒ 一个字都不喂、一次都不解码，**等价于今天的行为**（一个开关一键回退）。
        """
        self._started = False          # 锁存：上升沿之后一路喂到 cut
        self._seed = None              # 上升沿往前补的那一截
        self._hist = []                # 开口【之前】攒着的那些包（只留够往回补的那一截）
        self._hist_n = 0               # 上面那些一共多少样本
        self._pend = []                # 回撤的余量（还没喂出去的片）
        self._bl = 0                   # 上面那些片一共多少样本
        self._fed = False              # seed 出去没有
        self._ss = 0.0                 # Σx²（估增益用）
        self._nn = 0
        self._ns = 0                   # 一共喂进去多少样本
        self._nc = 0                   # 一共喂了几次
        self._k = 1.0                  # 最后一次用的增益
        self._t_on = 0.0               # 上升沿那一刻
        self._t_feed = 0.0             # 第一次真喂出去的时刻
        self._bad = 0                  # 喂进去的音频里有几个 Inf/NaN（应该永远是 0）
        self._err = ''
        self.why_not = ''
        self.ok = True
        self._s = None
        if not SPK_ASR_LIVE:
            self.ok = False
            self.why_not = 'SPK_ASR_LIVE=0（关着）'
            return
        self._s = self.rec.create_stream()

    def feed(self, a, onset):
        """收一包。`onset` = `self.vad.is_speech_detected()` 的当前值。

        ★ `onset` 只有【上升沿那一次】有用：之后它掉回 False 也照样喂（锁存）。
          因为 VAD 的 False 只是"这一帧没判成语音"，句中的自然停顿本来就会让它掉下去。
        """
        if not self.ok or a is None or not len(a):
            return
        try:
            pk = a if isinstance(a, np.ndarray) and a.dtype == np.float32 \
                else np.asarray(a, np.float32)
            if not self._started:
                if not onset:
                    # ★ 规矩②：开口之前一个字节都不【喂】—— 但要留着，
                    #   因为上升沿比真开口晚 0.33 秒，得往回够。见规矩②那一整段。
                    self._hist.append(pk)
                    self._hist_n += len(pk)
                    while self._hist and self._hist_n - len(self._hist[0]) >= GB_N:
                        self._hist_n -= len(self._hist.pop(0))
                    return
                self._started = True
                self._t_on = time.time()
                # ★ 往回补 `VAD_PAD_BACK + VAD_LAG`（默认 0.63 秒）—— 与
                #   `seg_audio()` 的起点对齐。补的是【同一批包对象】，所以
                #   「逐样本等于 np.concatenate(buf)」这条保证没有被破坏。
                pre = np.concatenate(self._hist + [pk]) if self._hist else pk
                nb = min(len(pre), GB_N)
                self._seed = pre[len(pre) - nb:]
                self._hist = []            # 用完了就丢掉，别白占内存
                self._hist_n = 0
                self._acc(self._seed)
                return                     # ★ 这一包已经在 seed 里了，别重复喂
            for q in _chunks(pk):
                self._acc(q)
                self._pend.append(q)
                self._bl += len(q)
                self._drain()
        except Exception as e:             # noqa: BLE001
            self._die('%s: %s' % (type(e).__name__, e))

    def _acc(self, x):
        """把一截音频记进【增益窗口】（只累加 Σx² 和样本数，不存音频）。

        ★ 窗口的构成刻意与 `_loud()` 对齐：`_loud` 看到的是【整段切片】
          （= 0.30s 前导 + 人声 + VAD 尾静音），所以这里也必须从 `seed`
          开始累 —— `seed` 就等于批式切片最前面那一截（见规矩②）。
        ★ 窗口是一路【长大】的（每喂一截就多一截），所以最前面几截用的是
          "短窗口估出来的增益"，后面才收敛到整段的水平。短窗口里静音占比高
          ⇒ rms 偏低 ⇒ 增益偏高（实测比 `_loud` 高约 2dB，落在 ±8.9dB 平台里）。
          ★ 这正是 `GBASE` 必须补足 0.63 秒的第二个理由：补足了，第一次喂出去
            时窗口里就已经有人声，不至于拿纯前导静音去估增益。
        """
        v = x.astype(np.float64)
        self._ss += float((v * v).sum())
        self._nn += len(x)

    def _gain(self):
        """滚动增益 —— 公式与 `_loud()` 逐字一致（`k = min(ASR_RMS/rms, ASR_GAIN_MAX)`，
        `k <= 1` 就不放大），只是从"整句一次"变成"每喂一次重算"。

        ★ `_loud()` 本身【一个字不改】：它还要给 `turn()` 的兜底、预卷（`turn` 之上
          那条插话路）、自测（`--selftest`）三处用。
        """
        if self._nn <= 0:
            return 1.0
        r = float(np.sqrt(self._ss / self._nn))
        if r > 1.0:
            r = 1.0        # 真实音频在 Mic 那一级就钳进 [-1,1] ⇒ 只有脏数据会走到这
        if r < 1e-6:
            return 1.0     # 纯静音：不放大（放大了只会喂噪声给 ASR）
        return min(ASR_RMS / r, ASR_GAIN_MAX)

    def _drain(self, all_=False):
        """把积压喂出去。★ 非 all 时保留 `SPK_ASR_HOLD` 秒余量；默认 0 秒
        （= 收到就喂）—— 见 `SPK_ASR_HOLD` 那段：当初设回撤是为了给增益窗口攒长度，
        而那条需求现在由 `SPK_ASR_GBASE`(0.63s) 自己满足了。"""
        hold = 0 if all_ else int(SR * SPK_ASR_HOLD)
        while self._bl > hold:
            pk = self._pend.pop(0)
            self._bl -= len(pk)
            if not self._fed:
                self._give(self._seed)
                self._fed = True
            self._give(pk)

    def _give(self, x):
        """喂一截进去，并顺手把能解的都解掉。★ 增益在这里现算。"""
        x = np.asarray(x, np.float32)
        if self._bad == 0:
            self._bad = int((~np.isfinite(x)).sum())
        if self._bad:
            x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        k = self._gain()
        self._k = k
        if k > 1.0:
            x = np.clip(x * k, -1.0, 1.0)
        self._s.accept_waveform(SR, x)
        self._ns += len(x)
        self._nc += 1
        if not self._t_feed:
            self._t_feed = time.time()
        while self.rec.is_ready(self._s):
            self.rec.decode_stream(self._s)

    def _die(self, why):
        """这条路出了事 ⇒ 这一轮退回整段那条老路。★ 绝不静默：一定要打出来。"""
        self._err = why
        self.ok = False
        self.why_not = why
        log('   ⚠⚠ 流式识别出事（%s）⇒ 这一轮退回整段那条老路', why)

    def cut(self):
        """收工：补喂余量 → 补 `SPK_ASR_TAIL` 秒静音 → `input_finished()` → 解码 → 取字。

        ★ 与 `asr_text` 同构，只少了那 0.5 秒【假】静音。
        ★ 但尾巴那点静音【不能省到 0】：流式模型不喂尾部静音就会吞最后一个字
          （`SPK_ASR_TAIL` 那段注释里有实测）。默认 0.2 是量出来的，见 `ab_asr.py`。
        """
        if not self.ok:
            return ''
        if not self._started:
            self.why_not = '开口前就收工（VAD 没认出人声）'
            return ''
        try:
            self._drain(all_=True)
            if not self._fed:
                self._give(self._seed)     # 极短一句：连余量都没攒够
            if SPK_ASR_TAIL > 0:
                self._s.accept_waveform(SR, np.zeros(int(SR * SPK_ASR_TAIL), np.float32))
            self._s.input_finished()
            while self.rec.is_ready(self._s):
                self.rec.decode_stream(self._s)
            r = self.rec.get_result(self._s)
            text = (r.text if hasattr(r, 'text') else str(r or '')).strip()
        except Exception as e:             # noqa: BLE001
            self._die('%s: %s' % (type(e).__name__, e))
            return ''
        log('   🌀 流式：喂 %.2fs / 开了 %d 次 / 增益 ×%.1f / 第一次喂在开口后 %.2fs%s',
            self._ns / float(SR), self._nc, self._k,
            (self._t_feed - self._t_on) if self._t_feed else 0.0,
            ('  ⚠ 含 %d 个 Inf/NaN' % self._bad) if self._bad else '')
        if not text:
            self.why_not = '空结果'
        return text


# ---------------------------------------------------------------- 脑子
def brain(text, dry=False, session=None, ctx=None):
    """一句话 → (要念出来的回答, 会话控制)。控制 = None / 'ask' / 'end'。

    ★ `ctx` = 这一轮谁在说话（`spk_skills.TurnCtx`）。不传 = 老行为，一个字节都不差
      —— 离线自测那两处调用（`--wav` / `--selftest`）就不传。

    ★ 2026-09-21 换脑子：以前是【两段式】—— 先问 spk_agent"这是不是指令"，
      是就执行、不是就再拿 ANSWER_PROMPT 去问一遍 DeepSeek。
      那等于把一个人劈成两半：一半只会动手不会说话，一半只会说话没有手。
      现在一条路走到底：spk_skills.run() 带着【它住在一只音箱里】这个身份和
      一张工具白名单去问模型，模型自己决定这次是回答还是动手。
      时间、音量、在放什么、音乐库都由 spk_skills 现取现拼进提示词，
      所以"现在几点"答得对 —— 这里不再单独喂时间。

    ★ 2026-09-21 再接上 session：传了它，模型才看得见前几句。没传就是老样子
      （一次一句、历史为空）—— 那正是"七点半吧"被静默定成今晚 19:30 的成因，
      所以除了自测，主循环一律带 session。
    """
    said, ctl = skills.run(text, dry=dry, session=session, ctx=ctx)
    return said.strip(), ctl


# ★★ 认人（声纹，L3 人物册）—— 默认【关】，用 SPK_WHO=1 打开。
#
#   为什么默认关：判据层离线验过了（真空带 + 九档劣化扫描），但"真房间里认不认得准"
#   没验过 —— 那需要一次出声的对话。跟 SPK_BARGE 一样：代码先接好、离线验过，
#   开关留在手上，等那一轮验证再打开。
#   ★ 关着的时候这条链【一行都不跑】：不载 28MB 声纹模型、不算一次 embed、
#     提示词里一个字都不加 —— `_observe_who` 直接交回一个空的 TurnCtx，
#     `state` 是 `unknown`，而 `unknown` 态是一个字节都不加的。
WHO_ON = os.environ.get('SPK_WHO', '0') == '1'


def _warm_who():
    """后台预热声纹模型（约 0.8 秒）。

    ★★ 绝不能放进 `Ear.load()` 里同步做 —— 那样唤醒后第一轮会多愣 0.8 秒，
      而"喊一声→它应你"那一下正是最不能愣的地方。开一条守护线程，
      抢在主人第一次开口之前把模型热好。
    ★ 预热失败什么都不用做：`observe()` 自己会返回 `skip('没有声纹')`，
      整条链退化成"不认人"，一句话都不影响。
    """
    if not WHO_ON:
        return
    try:
        import spk_speaker as spk
        t0 = time.time()
        spk.warm()
        log('👤 声纹模型预热好了（%.2fs）', time.time() - t0)
    except Exception as e:                              # noqa: BLE001
        log('👤 声纹预热没成（照常不认人）：%s: %s', type(e).__name__, e)


# ------------------------------------------- 思考音：等大模型的那几秒别空着
#
# ★ 主人 2026-09-21 深夜要的：「把思考音接上」。
#   ★★★ 2026-09-22 下午把这几个数**重新实测了一遍** —— 当初那版账是
#      **pro 模型 + DLNA 出口**时候记的，后来两样都换了（模型换 flash、出口换 0x601）：
#
#        录音 → ASR 中位 0.4 秒 → 大模型 0.7~0.9 秒（要调工具的一轮约 1.5 秒）
#             → TTS 2.7~3.2 秒 → 0x601 推流出声 0.185 秒
#
#      ⇒ 他"说完"到听见回答：**旧 DLNA 路实测 13 秒**（ASR 1.0 + 思考音 1 + 大模型 5
#        + TTS 2.4 + SSDP 推流 6），**现在这条路约 5.5~7 秒**。
#      ⇒ **最大的一块已经从大模型变成 TTS**（2.7 秒 vs 0.8 秒，贵三倍多）。
#        再往下压就得动 TTS，动大模型已经没意义了。
#   ★ 但思考音照旧要接：三四秒的静默照样让他以为音箱没搭理他。
#     思考音就是那几秒里的"嗯……我想想" —— 让人知道"它在办"。
#
# ★★ 三条约束，每条都有具体理由，不是风格问题：
#   ① **必须走后台线程**。思考音的意义是【别让他干等】；同步推完再调大模型，
#      等于"为了报一句嗯……先卡六秒"，比不做还糟（当初走 DLNA 时 discover 实测抖到 6.34 秒）。
#   ② **推完就撒手**：不等它来取、不自听、不判 —— 那是后面 say() 那条正式路的事。
#      思考音判成败没有意义，它就是个氛围。
#   ③ **必须在 say() 之前收干净**。两条流推的是同一台设备，后 Play 的掐掉先 Play 的。
#      思考音线程要是拖到回答推出去之后才发 Play，**回答会被掐成半句**。
#      所以：`push(deadline=)` 带硬截止 + speak() 之前 join。见 spk_ai_dlna.push 的注释。
FILLER_ON = os.environ.get('SPK_FILLER', '1') == '1'
# ★ 阶梯最多出声多久：到点就【不再出声】，静默等答案（答案一到就让它说完当前那条）。
#   ★ "30 秒的轨"那个概念随 2026-09-22 的阶梯改动一起没了 —— 见 `_filler_go` 上面那段。
FILLER_DEADLINE = float(os.environ.get('SPK_FILLER_DEADLINE', '20.0'))
# ★★ join 的上界。★ 2026-09-22 主人改口后它的语义**变了**：不再是"等当前这条思考词说完"
#   （`ready` 一置位阶梯当场让路 ⇒ 正常路径下这个 join 几乎是瞬时的），
#   现在它是**纯兜底** —— 只在"阶梯卡在 `dlna.push()` 里出不来"时才用得上。
#   ⇒ 数值仍是 6.0：一条长句（5 秒档，实测最长 4.46 秒）连推带等也在这个数内，够宽。
#   ★ 等不到就 set(stop) 硬中止 —— `push()` 在发 Play 之前会再看一眼，设了就一个字都不发。
FILLER_JOIN = float(os.environ.get('SPK_FILLER_JOIN', '6.0'))

# ★★★★★ 思考音【提前到"说完那一刻"起跑】（2026-09-22 深夜，主人：「反应有点慢
#   思考音也不是1秒后出的」）。原来阶梯是在 **ASR 出字之后**才起跑（`turn()` 里
#   `_filler_go()` 的位置），于是第一声"唔……"要等：
#
#       他闭嘴 →「说完」→ ASR 0.3~1.3s → FIRST_DELAY 0.6s → 推 → 真出声 0.35s
#       ⇒ **他停嘴后 1.5~2.1 秒**才听见思考音（20:32 实测：说完 20:32:24，
#         思考音人耳 20:32:26）。
#
#   ★ 现在改成【VAD 一判"说完"就让阶梯起跑】（`turn()` 里 `record()` 之后、
#     ASR 之前），第一声变成：**说完 → EARLY_FIRST 0.25s → 推 → 真出声 0.35s
#     ⇒ 他闭嘴后约 0.6 秒**（净省 ASR 那 0.3~1.3 秒）。
#   ★★ 代价说清楚：**"这一轮到底算不算数"这时候还不知道**（要等 ASR 出字 +
#      回灌判据）。所以这条早跑的路必须配两道闸：
#        ① **回灌那几秒不起跑**（`_speech_at > _said_at + EARLY_ECHO_MARGIN`）——
#           回灌落在念完 +0.4~0.5 秒，主人追问落在 +1.5~3.0 秒，闸门取 1.0 卡中间。
#           ★★ 老版这里是"念完 **4 秒**内一律不起跑"，**已作废**：它把主人每次追问
#           （= 他最常说话的时刻）都拖慢 1 秒（快路 0.75s vs 慢路 1.8s）。
#           ★★ 中间还有一版给 0.2，**也太松**（回灌当场就进来了，见
#           `EARLY_ECHO_MARGIN` 那段的 21:24:51 实测）。数字的账全在那两段里。
#        ② 这一轮真被丢掉时（ASR 空 / 判成回灌）**当场把阶梯停掉**（`_filler_drop`）——
#           已经在飞的那一推拦不住（`push` 发 Play 前会看 `stop`），但绝不许它
#           接着往下推第二条。
#   ★ 想退回老行为：`SPK_FILLER_EARLY=0`（一行环境变量，不用改代码）；
#     只退回"老闸门"那种保守行为：`SPK_FILLER_ECHO_MARGIN` 给个大数（如 4.0）。
FILLER_EARLY = os.environ.get('SPK_FILLER_EARLY', '1') == '1'
# ★★★★★ 早跑闸门①的判据（2026-09-22 深夜第二次修正）—— 从「念完 4 秒内不起跑」
#   换成「**人声起点早于我们念完 ⇒ 当回灌，不起跑**」。
#
#   为什么换（旧闸门是**每次追问都白等 1 秒**的那道闸）：
#     旧判据 `time.time() - _said_at > ECHO_WINDOW(4.0)` 的意思是"我们刚念完的
#     4 秒内不许早跑"。而主人最常说话的时刻**恰恰就是听完回答马上追问**，
#     于是早跑几乎总被拦下，退回 ASR 之后的老路。实测两种路的账：
#         早跑命中（`推在 0.40s`）⇒ 真出声 = 说完 + **0.75 秒**
#         被闸掉（`推在 0.60s`，零点是 ASR 之后）⇒ 真出声 = 说完 + **1.8 秒**
#     （21:20:18 那一轮：说完 18.204 → ASR 19.060 → 思考音 20.429）
#     ⇒ 他感觉到的"还是思考音慢"就是后者，而后者才是**常态**。
#
#   为什么新判据是对的：回灌的定义就是"我们自己的声音飘回来"，
#   而它必然发生在我们**还在播**的时候（打断是关的，`SPK_BARGE=0`，
#   所以他不可能在我们播的中途真说话）。所以只要这段人声是
#   **我们念完之后**才开始的，就是主人说的 ⇒ 可以立刻起跑。
#
#   实测依据（换之前查的，别丢）：最近 1 小时 **25 轮说话、回灌 0 次** ——
#   `speak()` 之后那个 0.4 秒静置 + 丢掉播放期间攒下的旧音频，已经把回灌
#   解决掉了，那道 4 秒窗口是在一个实测不存在的故障上白加的保险。
#   ★★ `EARLY_ECHO_MARGIN = 1.0`：**这是当晚第三次调，前两次都错在数字上**。
#     第一版给 0.2（想的是"余量够不着他的追问"）——**太松，回灌当场就进来了**：
#       21:24:49 `念完了` → 同一秒 `👂 听见了，录…`（耳朵一开就听见）
#       → 21:24:51 ASR「这我记着呢」（**我们自己那句的尾巴**）→ 判成回灌丢掉，
#       可 21:24:52 那声"诶……"**已经飘在屋里了**（闸门②只能停后面几条）。
#     ⇒ 实测两个数：**回灌落在 `_said_at` + 0.4~0.5 秒**（`speak()` 之后那个
#       0.4 秒静置结束时，屋里余音还没散）；**他真追问落在 +1.5~3.0 秒**
#       （21:24:52→+3s、21:25:02→+1.5s 实测）。1.0 正好卡在中间：
#       回灌拦住 ✓、追问照旧早跑 ✓。
#     ★ 为什么用"人声起点"当锚而不是"此刻"：回灌那一轮 ASR 要跑 1~2 秒，
#       锚在起点，它一开始就能被认出来，不用等 ASR。
#   ★ 早跑真跑错了还有第二道闸兜底：丢了的那一轮 `_filler_drop` 当场停阶梯。
#
#   ★★★ 2026-09-22 深夜【第三次修正】：1.0 → **0.5**。上面那两个数（回灌 +0.4~0.5、
#     追问 +1.5~3.0）是拿**旧的** `_said_at` 量的，而旧的锚是"我们推完那一刻"——
#     比真响完早 0.4 秒（正常）到 **4 秒**（补发那一轮）。现在锚换成了真响完
#     （见 `dlna.audible_end()` 和 `speak()` 的 finally）⇒ 回灌落到锚点附近
#     （0~0.1 秒），主人真追问落到锚点后 1.1~2.6 秒。
#     **0.5 还是卡在那两个数中间**，所以这道闸不用再改：回灌拦住 ✓、追问照旧早跑 ✓。
#     麦克风全程开着（主人立的规矩），所以这里拦的是**真会被录进来的**那些回灌。
EARLY_ECHO_MARGIN = float(os.environ.get('SPK_FILLER_ECHO_MARGIN', '0.5'))
# ★ 早跑那条路的 FIRST_DELAY。**2026-09-22 深夜实测后从 0.4 收到 0.25。**
#
#   收的理由：老注释说"再叠上尾静默 0.45 秒才是他真闭嘴的时刻"——**那个 0.45 是估的**。
#   实测 5 轮（`说完` − `听见了` − 语长）：
#       21:19:40 -0.01s / 21:19:44 -0.01s / 21:19:59 -0.01s / 21:20:18 -0.03s / 21:20:39 +0.19s
#   ⇒ **尾静默几乎为 0**：`说完` 就是他说完的瞬间，VAD 那个
#     `min_silence_duration=0.6` 并不落在这条延迟账上。
#   所以 0.4 就是实打实 0.4 秒的死气，没有"自然停顿"垫着。0.25 留的余量足够
#   他换气/咳一声不被切（VAD 已经保证了"人声结束"才走到这里）。
#
#   当前的账：说完 → 0.25 → 推 → 真出声 **0.35 秒**（下面这段实测）
#            ⇒ 他闭嘴后 **约 0.6 秒**听见思考音。
EARLY_FIRST = float(os.environ.get('SPK_FILLER_FIRST_EARLY', '0.25'))
# ★★ 「推出去 → 真出声」这条腿的实测（2026-09-22 深夜，全静音轨，屋里没声）：
#     设备自己的毫秒日志（不是估算）：
#       21:21:03.380  cmdProcess(0x601)      ← ihwplayer 收到命令
#       21:21:03.398  st:0x1 Preparing       +0.018
#       21:21:03.921  st:0x2 Prepared        +0.541   ← 这一次是**假数**
#       21:21:03.930  st:0x3 Playing         +0.550
#     ⇒ 那条 0.52 秒是"刚重启完 ihwplayer 的第一次取流"造成的复现不出来的异常
#       （复测三次：0.126 / 0.094 / 0.117）。**别拿它当"补静音让文件变大所以慢"
#       的证据去降码率** —— 各档大小（76KB~305KB）都在 0.07~0.14 秒。
#     稳定值：设备侧 收到命令→出声 **0.1 秒**；我们这侧 推→设备收到 **0.25 秒**
#       （netd 轮询 + `play601.sh` 里 `dbus-send --reply-timeout=200` 的 0.24 秒）。
#     ⇒ 合计 **0.35 秒**。老的耗时账里写的 0.185 是**偏乐观的旧数**，已作废。

# ★★★★★ 自听回灌闸门（`_is_self_echo`）—— 2026-09-22 晚：主人只问了一句"几点了"，
#   音箱回了【三个】回答。真凶是念完之后飘回麦克风的我们自己的尾音（见那个方法的注释）。
#   `ECHO_WINDOW`：从"念完那一刻"到【判据跑起来】的秒数。
#     ★ 实测回灌那一轮是 2.0 秒（念完了 20:07:41 → ASR 20:07:43，中间是
#       0.4 秒静默 + VAD 等 0.7 秒 + ASR 0.3 秒），所以 4.0 是**两倍余量**。
#       再放宽只会多收误判：回灌在物理上不可能持续太久 —— 麦克风里那点我们的
#       余音顶多拖两三秒就散干净了。
#   `ECHO_TAIL_CHARS`：只拿我们那句话的【最后】这么多字去比 —— 回灌回来的永远是尾巴。
#   `ECHO_MIN_CHARS`：比这还短的不判（"嗯""对""好"这种单字既可能是回灌、
#     更可能是他在应答 —— 宁可放行，也别把他一个"嗯"吞掉）。
ECHO_WINDOW = float(os.environ.get('SPK_ECHO_WINDOW', '4.0'))
ECHO_TAIL_CHARS = int(os.environ.get('SPK_ECHO_TAIL_CHARS', '16'))
ECHO_MIN_CHARS = int(os.environ.get('SPK_ECHO_MIN_CHARS', '2'))
# 两把尺子（"有多少字能在我们说过的话里找到"才算回灌）。
#   `ECHO_MIN_FRAC`：老判据（上一句**回答**）用 —— **就是 `spk_barge` 的默认值，
#     一个字都没动**，音箱那边这条路的性子跟以前完全一样。
#   `SAID_MIN_FRAC`：登记簿（**思考音** + 求救那条线）用，**更严**。
#   ★★★ 为什么登记簿必须更严（2026-09-24，离线验证台当场抓出来的误杀）：
#     思考词是**日常口语** —— "…再慢慢跟你说啊。"、"这个得琢磨琢磨。"。
#     拿 0.6 去比，主人一句再自然不过的
#         `嗯，你说`   → 撞上"跟你说"（2/3 = 67%）⇒ 被吞
#         `哎我跟你说` → 撞出 60%                      ⇒ 被吞
#     而 **`嗯，你说` 恰恰是主人最常说的话之一**（`spk_barge.YIELD_TEXT` 就是它）。
#   ★ 0.8 不是我拍的：另一个壳那边为**同一类误伤**实测校准过 —— 真回声整句都是
#     我们的话（frac≈1.0），而主人引用只有两三个字 ⇒ 0.8 两边就分开了
#     （见 `spk_barge.is_self_echo` 的 `min_frac` 段）。
#   ★ 思考音那条**主要靠 `h in o`（原样包含）**：回灌回来的就是我们刚念的那几个字，
#     短句上包含判据够用，不靠模糊匹配。
ECHO_MIN_FRAC = float(os.environ.get('SPK_ECHO_MIN_FRAC', '0.6'))
SAID_MIN_FRAC = float(os.environ.get('SPK_ECHO_SAID_FRAC', '0.8'))

_filler_lock = threading.Lock()
_filler_cur = None          # 正在跑的那个阶梯 —— ★ 同一时刻只许有一个，见 _filler_go


class _FillerRun:
    """一次阶梯的句柄。`turn()` 拿它做三件事：等 → 放行 → 看它出过声没有。

    ★★ `n` / `detail` 是"这次到底出过声没有"的唯一判据 —— 也是要不要加衔接词的判据：
      **一条都没播过就别来一句"哦，有了"**，那会儿他根本没等。

    ★★★ 为什么是 `n` 这个计数器、而且由 push 那一层当场累加，**不是**用 `ladder()`
      的返回值：`ladder()` 的 recs 是**它自己的局部变量**，只在**函数返回时**才交出来。
      而 `turn()` 是在 `join(FILLER_JOIN)` 之后**立刻**读这个判据 —— 一旦 join **超时**
      （那条推流卡住、长句还挂着），`ladder()` 根本还没返回 ⇒ 交出来的东西是空的 ⇒
      `spoke()` 回 False ⇒ **衔接词不加**。而那恰恰是他等得最久、最该有一句过渡的情况。
      ⇒ 真相必须**在推成功的那一刻**就记下来，不等任何人返回。
      ★ 这是同一类错误的第三次：**判据取的时机比事实晚了一步，而失败时静默。**

    ★★★ 2026-09-22 又加一层（第四次的预防）：`turn()` 现在**在 `ready.set()` 之后
      【立刻】**读这个判据，而不是等 `join()` 返回 —— 因为答案一到就得拼衔接词、开始
      合成（见 `turn()` 里那段）。于是冒出一个新窗口：**阶梯正卡在 `dlna.push()` 里
      那 0.8~1.1 秒时答案到了**，`n` 还没累加 ⇒ 读到 0 ⇒ **衔接词被吞**，
      而那条思考音其实推出了。又是"判据比事实晚一步"。
      ⇒ 修法不是"等一会儿再读"，而是**把记账挪到 push 之前、并且跟读共用一把锁**
        （`do_push` 里 `ready` 检查和 `n += 1` 原子完成）。
        这样 ready 一旦置位，任何在它之前开始的 push 都已经记过账了 ——
        读完即终值，**结构上不存在窗口**，不是靠"够快"。
      ★ 方向是有意选的：万一读的时候那一推正飞在半路、而它最终失败，
        `turn()` 会多拼一句衔接词。**宁可多一句，绝不许少一句**
        （少一句 = 衔接词被吞 —— 那正是本类前三次栽的同一个坑）。
    """

    __slots__ = ('t', 'ready', 'stop', 'n', 'detail', 'lock')

    def __init__(self):
        # ★★★ 2026-09-22 主人改口「我觉得思考词是可以被真回复打断的」「因为真回复加了
        #   衔接 就不会显得那么突兀」⇒ 置位后阶梯**当场**让路，不再等当前这条人声说完。
        #   想取回旧行为：`SPK_FILLER_WAIT_EOS=1`（见 `spk_filler.WAIT_EOS`）。
        self.ready = threading.Event()   # 答案好了 ⇒ 阶梯立刻让路（思考词可被打断）
        self.stop = threading.Event()    # 硬中止 ⇒ 立刻收工，一个字都不再发
        self.n = 0                       # ★ 推出去几条（当场累加，见类注释）
        self.detail = []                 # 阶梯返回的明细（tier/at/end），只给人看
        # ★★★ `lock` 守的是"记账 `n`"和"读 `n`"这两件事，见下面 `spoke()` 的注释。
        self.lock = threading.Lock()
        self.t = None

    def spoke(self):
        """出过声没有。★ 读的时候拿锁 —— 理由见 `do_push` 里那段：
        `ready` 置位【之后】读到的 `n` 才是终值，而 `turn()` 现在正是这么用的。
        """
        with self.lock:
            return self.n > 0


def _warm_filler():
    """启动时把思考词缓存备好 —— **绝不许落在某一轮里**。

    ★★ 为什么非预热不可：`filler.build()` 在"缓存跟当前嗓子对不上"或"改了短语"
      时会**把十几条短语全部重合成，实测 30 秒**。这要是发生在他问完一句话之后，
      思考音比他等的还久 —— 那还不如不做。启动时先花掉，他开口时缓存已经在磁盘上。
    ★ 2026-09-22 起**不再拼那条 30 秒的轨**（阶梯改成一条一条推，见 `spk_filler.ladder`
      上面那段），所以这里只剩"把缓存备好"一件事，比原来还轻 —— 而且顺带把
      每条的「人声结束位置」也算好了存进索引，推流那一刻就不用再跑 ffmpeg。
    ★ 失败不影响任何事：阶梯起不来，整条链退化成"没有思考音"，照常回答。
    """
    if not FILLER_ON:
        return
    try:
        import spk_filler as fil
        t0 = time.time()
        T = fil.tiers()
        log('💭 思考词备好了（1秒档 %d / 2秒档 %d / 5秒档 %d，查缓存 %.1fs）',
            len(T['short']), len(T['mid']), len(T['long']), time.time() - t0)
        if not (T['short'] and T['mid']):
            log('   ⚠ 档位不全，阶梯可能一句话都推不出去')
    except Exception as e:                              # noqa: BLE001
        log('💭 思考词没备成（照常说，只是等的时候没动静）：%s: %s', type(e).__name__, e)


_filler_texts = None


def _filler_key(path):
    """mp3 → **归一化的对照键**：basename 去掉 `.mp3`，再取最后一段。

    ★★★ 2026-09-24 当天补的第二个坑（第一个坑是"进程比改动早"，见
      `_filler_text` 的 docstring）。索引里存的是
      `filler/<hash>.mp3`（相对代码目录），可**真正推出去的那一份**是
      `spk_ai_dlna` 的暂存件 `/tmp/spk601_p45_<hash>.mp3`
      （`spk_ai_dlna.py:1000`：`'spk601_p%d_%s' % (PAD_SECS, basename)`）。
      **目录不同、前缀也不同** ⇒ 按整条路径查**永远查不到**。
      实测：当天真实推过的 6 条思考音，`idx.get(p)` **6/6 全空**
      ⇒ `note('')` 在第 78 行直接 return False ⇒ 登记簿一条 filler 都没进去
      ⇒ 闸门依旧失明，症状一字未变（"重启一下就好了"是错觉）。

    ★ 只取尾段 hash 两边就都对得上；对不上的（音乐推流
      `spk601_p45_The_truth_...-139774.mp3` 之类）自然回空串 ⇒ 不登记，
      正是我们要的。
    """
    b = os.path.basename(str(path or ''))
    if b.lower().endswith('.mp3'):
        b = b[:-4]
    return b.rsplit('_', 1)[-1]


def _filler_text(path):
    """思考音 mp3 → **它念的那句话**。

    ★★ 2026-09-24 加：回灌闸门要拿"我们说过的话"当对照，而思考音这条路的
      `push()` 只拿到一个 mp3 路径（文本在缓存索引里）。所以在这儿补一次翻译。
    ★ 读的是**缓存索引**（`load_index()`），**绝不调 `build()`** ——
      回灌是热路径，绝不能因为查一句话就把整批思考音重建一遍（那是
      `spk-filler-index-shared-trap` 那件真事故的路）。
    ★ 读不到就回空串 ⇒ 不登记 ⇒ 退化成老行为，不报错、不抛。
    ★ 两边都用 `_filler_key()` 归一化 —— 索引的键和推出去的名字**不是同一条路径**。
    """
    global _filler_texts
    if _filler_texts is None:
        try:
            import spk_filler as fil      # ★★★ 局部 import —— 本文件一贯这么写
                                          #   （1052/1142/1242 三处都在函数体里）。
                                          #   ★ 原来这里直接写 `fil.load_index()`，
                                          #   而 `fil` **不是模块级名字** ⇒ 抛
                                          #   `NameError` ⇒ 被下面那口 `except` 吞掉
                                          #   ⇒ `_filler_texts={}` ⇒ **永远回空串**、
                                          #   一个字都不登记，症状与没修**一模一样**。
                                          #   （离线台当场抓到：`_filler_texts 装了 0 条`，
                                          #   而索引明明有 14 条。）
            d = fil.load_index() or {}
            _filler_texts = {_filler_key(it['path']): (it.get('text') or '')
                             for it in (d.get('items') or []) if it.get('path')}
        except Exception as e:                           # noqa: BLE001
            # ★ 仍然 fail-open（本类铁律：判据坏了顶多退化成老行为），
            #   但**必须留一声**：上一版就是在这儿静默失配，害得
            #   "以为修好了"却一字未变。只报一次，不刷热路径。
            _filler_texts = {}
            log('⚠ 思考音文案表没装载（登记簿会退化，回灌闸门对思考音仍失明）：%s: %s',
                type(e).__name__, e)
    return _filler_texts.get(_filler_key(path)) or ''


def _filler_go(adopt=None, first=None):
    """起一条【阶梯】线程，立刻返回 `_FillerRun`。返回 None = 这次没有思考音。

    ★ 调用点只有一处，在【ASR 出字之后、brain() 之前】—— 那是唯一的位置：
      早了（录完就推）会在人还没说完 / ASR 还没出字时就出声，那是插嘴；
      晚了（brain() 之后）等于大半段等待根本没盖住。
      ★ 2026-09-22 深夜起这条被**部分推翻**了：录完（"说完"那一刻）就开始起跑是
        **可以**的 —— 只要配上回灌窗口那道闸（见 `FILLER_EARLY` 那段）。
        于是现在有两个起点：`turn()` 里 `record()` 之后的**早跑**（`first=EARLY_FIRST`），
        和这里的老起点（ASR 之后，`first=None` ⇒ 用 `spk_filler.FIRST_DELAY`）。
        早跑那个阶梯会被这里的 `adopt=` **原地认领**，绝不重启（重启＝时钟归零、
        白等一遍，早跑就白跑了）。

    ★★ 跟旧版最要紧的差别：**同一时刻只许有一个阶梯在推。**
      旧版是"推一条 30 秒的轨、线程推完就结束"，天然不会重入；新版的阶梯能活十几秒
      （等答案期间一直在推），而 `turn()` 是会被连着调的 —— 两个阶梯同时推同一台设备，
      它们各自以为"我知道现在在播什么"，其实都不知道，结果是互相掐。
      所以进门前先把上一个停掉 —— 这是新设计【必须】配的一道锁。

    ★ 阶梯负责的事到"答案好了、当前这条也说完"为止就结束：**它从不推回答**。
      什么时候推回答仍然只有 `speak()` 一件事说了算，不在这里分叉。
    """
    global _filler_cur
    if not FILLER_ON:
        return None
    with _filler_lock:
        # ★★ 认领：早跑那个还活着且没被叫停 ⇒ 直接用它。**必须在同一个锁里判**，
        #   否则两次调用能各自"没看到对方"而起两条（那正是这道锁要防的事）。
        if adopt is not None and adopt is _filler_cur and not adopt.stop.is_set():
            return adopt
    try:
        import spk_filler as fil
        T = fil.tiers()
    except Exception as e:                              # noqa: BLE001
        log('   💭 思考词取不到（不影响回答）：%s: %s', type(e).__name__, e)
        return None
    if not (T.get('short') or T.get('mid') or T.get('long')):
        return None

    with _filler_lock:
        if _filler_cur is not None:                     # ★ 停掉上一个，绝不许两条并行
            _filler_cur.stop.set()
            if _filler_cur.t is not None:
                _filler_cur.t.join(0.5)
        run = _FillerRun()
        _filler_cur = run

    def go():
        def do_push(p):
            # ★★ `abort=run.stop` 是防掐回答的那道闸（push 在发 Play 前会再看一眼）。
            #   push 自己的 `deadline` 是【单次调用内】的时长，阶梯每次只花 0.2 秒，
            #   对它没意义 —— 真正管用的是这个 abort。
            # ★★ 成功就【当场】记账，绝不等 ladder() 返回（理由见 `_FillerRun` 注释）。
            # ★★★ 但"检查 ready"和"记账"必须在【同一把锁】里 —— 否则 `turn()` 在
            #   ready.set() 之后读到 0 时，可能有一条 push 正卡在下面那 0.8~1.1 秒里，
            #   它马上会把 n 变成 1 ⇒ 衔接词被吞。锁一上，情况变成：
            #     · push 先拿到锁 ⇒ 它看到的 ready 还没置位 ⇒ 记完账才轮到读 ⇒ 读到 1
            #     · 读先拿到锁 ⇒ ready 已置位 ⇒ push 从此再也记不了账 ⇒ 读到 0 也是终值
            #   两头都自洽。★ 顺带把"答案已到就别推了"也放进来 —— 这跟 `ladder()`
            #   的退出条件是同一条判据（`ready() and now >= spoken_end`），
            #   只是把那个零点几毫秒的缝也堵上了。
            with run.lock:
                if run.ready.is_set():
                    log('   💭 阶梯：答案已到，第 %d 条不推了', run.n + 1)
                    return False
                run.n += 1               # 先记后推，理由见上
            ok = dlna.push(p, tries=4, abort=run.stop)
            if ok:
                # ★★★ 2026-09-24：把【这条思考音念的是哪句话】登记出去。
                #   `push()` 只拿到一个 mp3 路径，文本在这儿翻回来（`_filler_text`）。
                #   ★ 登记在**推成功之后**：判据跟 `spoke()` 一致 ——
                #     "他真听到过"才算数，没推出去的东西不会有回声。
                #   为什么必须记：思考音走的是 `dlna.push`，**从不写 `_last_said`**
                #   ⇒ 它的回声原来一路无人认领。08:33:26 推「让我想想啊」，
                #   08:33:28 被当成主人 ⇒ 08:33:29 答「好，你慢慢想，我在这儿等着」。
                spk_said.note(_filler_text(p), 'filler')
            if not ok:
                # ★ 没推出去 ⇒ 退账。`spoke()` 的语义是"**他真听到过**思考音"，
                #   不是"我们试过" —— 试了没成还硬加一句"哦，有了"，比不加更怪。
                # ★ 这一退确实留了个理论缝：万一 `turn()` 恰好在这一推飞行途中读了 `n`，
                #   它会读到 1（加了衔接词），而这一推随后失败。代价是**在设备已经
                #   推不动的情况下多一句衔接词** —— 那头本来就一个字都放不出来，
                #   而他此刻也听不到任何区别。**宁可这样，也不许反过来少记**
                #   （少记 = 衔接词被吞，那正是本类前三次栽的同一个坑）。
                with run.lock:
                    run.n -= 1
            return ok

        try:
            run.detail = fil.ladder(
                push=do_push,
                ready=run.ready.is_set, stop=run.stop,
                log=lambda f, *a: log('   ' + f, *a),
                deadline=FILLER_DEADLINE, first=first)
        except Exception as e:                          # noqa: BLE001
            # ★ 铁律同打断那条链：思考音是"更好"，说话是"这次要命" ——
            #   它推不出去也必须照常回答，绝不许把异常抛进主循环。
            log('   💭 思考音推失败（不影响回答）：%s: %s', type(e).__name__, e)

    run.t = threading.Thread(target=go, name='filler', daemon=True)
    run.t.start()
    return run


def _filler_drop(run):
    """这一轮被丢掉了（ASR 空 / 判成回灌）⇒ 把【早跑】那条阶梯当场叫停。

    ★ 为什么必须有：早跑的阶梯是在"这一轮算不算数"还没定的时候就出门的。
      不叫停的话，它会在没人跟它说话的这几秒里接着往下推第二条、第三条
      —— 那就成了"明明没跟它说话，它自己在念思考词"，正是回灌最气人的形态。
    ★ 已经飞在半路的那一推拦不住（`push()` 发 Play 之前会看 `stop`，但网络往返
      撤不回来），所以还能听见一声"唔……"。这是早跑这条路**认下的代价**，
      换来的是他每次开口都早 0.3~1.3 秒听见回应。完全不想要就 `SPK_FILLER_EARLY=0`。
    """
    if run is None:
        return
    run.stop.set()
    if run.t is not None:
        run.t.join(0.5)
    log('   💭 这一轮不算数 ⇒ 把早跑的思考音停下来（已推出去的拦不住了）')


def _bridge():
    """取一句衔接词（由调用方拼在回答前面）。取不到就回空串。

    ★ 主人 2026-09-22 加的这道：「有回复后 再播一个 想到了 这样 是这样 等衔接过渡词
      然后再开始说真回复」。为什么是拼文本、不是单独推一条流 —— 见 `spk_filler.BRIDGES`
      上面那段（一句话：两条流推同一台设备会互相掐，而它跟回答本来就必须连着说）。
    ★ 失败绝不抛：一句过渡话取不到，回答照说。铁律同思考音那条链。
    """
    try:
        import spk_filler as fil
        return fil.bridge()
    except Exception:                                   # noqa: BLE001
        return ''


# ★ 预热合成的取件上限。合成实测 0.35~0.8 秒，8 秒够宽 ——
#   真卡到超时说明常驻连接出事了，那就退回同步合成（慢，但一定成）。
TTS_PRE_WAIT = float(os.environ.get('SPK_TTS_PRE_WAIT', '8.0'))


class _TtsJob:
    """后台把一句话合成好，跟"等思考词说完"**并行**跑。

    ★★ 为什么值得单开一个线程：`join()` 等思考词说完的那一两秒**本来是空转的**，
      而 TTS 合成实测要 0.73 秒（常驻连接、第一句）。串着做的后果是 ——
      思考词的人声说完了，还要再静默 0.73 秒才轮到推流，他听到的就是"断了一下"。
      实测口径：末条思考音人声结束 → 回答开口 = TTS 0.73 + 推流 0.60 ≈ **1.35 秒**，
      其中那 0.73 秒**纯粹是我们自己排错了顺序**，不是物理限制。

    ★ 它是**纯本机动作**：只往 /tmp 写一个 mp3，**不碰设备、不出声、不推流**。
      所以它可以在思考音还在响的时候跑，屋里听不出任何区别。

    ★★ 失败绝不许影响说话（铁律同思考音那条链）：合成没成 / 超时 ⇒ `mp3()` 回 None
      ⇒ 调用方照老路**同步再合成一遍**。它是"更快"，说话是"这次要命"。
    """

    __slots__ = ('t', '_text', '_mp3', '_err')

    def __init__(self, text):
        self._text = text
        self._mp3 = None
        self._err = None
        self.t = threading.Thread(target=self._run, name='tts-pre', daemon=True)
        self.t.start()

    def _run(self):
        try:
            self._mp3 = dlna.tts(self._text)
        except Exception as e:                          # noqa: BLE001
            self._err = e

    def mp3(self):
        """取合成结果。没合成完就等它（有界）。拿不到回 None ⇒ 调用方走同步合成。"""
        self.t.join(TTS_PRE_WAIT)
        if self._err is not None:
            log('   ✗ 预热合成没成（改回同步合成）：%s: %s',
                type(self._err).__name__, self._err)
            return None
        if self._mp3 is None:
            log('   ✗ 预热合成 %.1f 秒没回来（改回同步合成）', TTS_PRE_WAIT)
        return self._mp3


# ★★ 打断的观察者（边播边听）—— 默认【关】，用 SPK_BARGE=1 打开。
#
#   为什么默认关：它一旦误判就会【把音箱自己正在说的话掐断】—— 那是个一听就出来的
#   坏体验，而且会让人以为"音箱坏了"。判据（声纹）已经在离线自测里验过，但
#   "真房间里、主人真插话时会不会误触发"没有验过 —— 那需要一次出声的对话测试。
#   所以：代码先接好、离线验过，开关留在手上，等那一轮验证再打开。
#
#   ★ 打开之后它也只活在 speak() 这一个窗口里（"我正在说话"那几秒），
#     不碰等唤醒词、不碰录音 —— 影响面就是这一句话。
BARGE_ON = os.environ.get('SPK_BARGE', '0') == '1'


# ★★★★★ 流式 TTS（2026-09-23）：回答边合成边推给设备。
#   ★ 它赚的是什么，说清楚（实测账在 `spk_tts_stream.py` 文件头）：
#     音箱的**首声**一直是思考音（"说完"+0.6~0.75 秒），那个没变、也不需要变。
#     变的是**思考音放完之后到回答出声之间那段空档** —— 现役要等整段合成
#     （短句 0.83 / 长句 3.41 秒），流式只要首包 ~0.37 秒。
#     ⇒ 短句省约 0.4 秒（本来就快被思考音盖住），**长句省约 3.5 秒**（那才是真收益）。
#   ★ `SPK_TTS_STREAM=0` 一键退回整段合成（老路，一行环境变量，不改代码）。
#   ★ 只走 601 那条路：流式端点推的就是 0x601。
STREAM_ON = os.environ.get('SPK_TTS_STREAM', '1') == '1'
# ★ 等落盘的上限。★ 这个等待是**并行**的（URL 早推出去了、设备已经在播），
#   所以它**不是延迟**；等不到也不许说"没念成"（见 `_say601` 里那段）。
STREAM_WAIT = float(os.environ.get('SPK_TTS_STREAM_WAIT', '12.0'))


def _stream_start(text):
    """试流式 TTS。回 `(url, probe, wait_path)`；任何一步不成就回三个 None。

    ★ 契约跟 `_TtsJob` 那条一致：**失败要安静**，调用方原地退回整段合成 ——
      铁律「快路不许成为唯一的路」，快路失败绝不许变成"说不出话"。
    ★★★★★ 2026-09-23 修（真事故）：这里曾经写成 `ts.start(text)`（**进程内**）。
      流因此开在 **spk_ear 自己那份 `STREAMS`** 里，而设备拿到 URL 后敲的是
      `192.168.1.100:8896` —— 那是 **systemd 那个服务进程**，它那份表是空的 ⇒
      **每次都是 404「没有这条流」**，设备被就地打发走。
      ★ 它伪装成"断网"：我们这边报「✗ 它没来取这条流」，设备那边报
        `net is disconnect!`，两条都指向网络 —— 实测网络全好（设备取流
        http=200 / 732378 字节 / 0.11 秒拿完）。
      ★ 判据（累计型）：`journalctl -u spk-ear | grep -c 设备来取流` = **0**。
      ⇒ 现在一律走 `start_remote()`（HTTP 到服务进程开流）。
    ★ `probe` = "设备来取了吗" —— 本进程看不见那条流，所以走 `/tts/<sid>/info`
      问服务进程（`pulled_remote`）。读不到回 False，照旧走补发那条老路。
    ★ `wait_path` = 等落盘齐 —— 那一等是并行的，只为把完整文件交给自听判据
      和 `_PLAY['secs']`（回声闸门靠它）。★ 落盘文件在 `/tmp`、两边同为 `nick`
      ⇒ **跨进程照样看得见**，`wait_file()` 原样可用（它本来就有"文件还在就回它"的兜底）。
    """
    try:
        import spk_tts_stream as ts       # 延迟 import：这个模块 import 期零副作用
        sid, url, first = ts.start_remote(text)   # ★ 让服务进程开流（≤1.5s 首包），拿到才推
        if not sid:
            log('   ⚡ 流式没起来（%s）—— 退回整段合成', first)
            return None, None, None
        log('   ⚡ 流式已起 %s（首包 %.2fs）', sid, first)
        return (url,
                lambda: ts.pulled_remote(sid),
                lambda: ts.wait_file(sid, timeout=STREAM_WAIT))
    except Exception as e:                                      # noqa: BLE001
        log('   ⚡ 流式起不来（%s: %s）—— 退回整段合成', type(e).__name__, e)
        return None, None, None


def speak(text, mic_q=None, pre=None):
    """把一句话念出来。返回那个打断观察者（没起就是 None）—— 下一步要用它的预卷。

    ★ 为什么要 `mic_q` 传进来、而不是在这里找个全局：观察者要的是【同一路麦克风
      音频】，而 spk_ear 里那一路就是 `Mic.q`。传进来，它就能在自测里被喂假队列。
    ★ 返回值从"无"变成观察者，是给第三件事（让路：说"嗯你说"、拿预卷接着听）留的口子。
      现在 ①② 做完，那个口子还没人用 —— 但契约先立在这儿，免得下次又改一遍签名。
    ★ `pre` = 已经在后台合成好的那一份（`_TtsJob`，见 `turn()` 里那段）：
      **合成跟"等思考词说完"并行跑，省掉 0.73 秒静默。**
      它失败 / 超时回 None ⇒ 这里照老路同步合成一遍 ——
      **绝不因为"想快"而说不出话。**（`pre or` 那个短路就是这个兜底。）
    """
    if not text:
        return None
    log('🔊 回：%s', text)
    # ★★ 流式优先，整段合成兜底（2026-09-23）。
    #   ★ `mp3 = None` 是有意的：流式路**不等**本地那份合成（等它就等于没优化）。
    #     那份兜底由 `pre`（`turn()` 里早就在后台跑的 `_TtsJob`）照旧准备着 ——
    #     它跟流式**并行**，白跑一趟不影响延迟，只当"流式挂了还有话说"的保险。
    url = probe = wait_path = None
    if STREAM_ON and dlna.PLAY_VIA == '601':
        url, probe, wait_path = _stream_start(text)
    if url is None:
        mp3 = (pre.mp3() if pre is not None else None) or dlna.tts(text)
    else:
        # ★ 流式成功时**也不许**在这里等 `pre.mp3()`（那是 0.83~3.41 秒）。
        #   ★★ 说清一件事：URL 一旦推出去，就**没有"退回整段合成"的机会了**
        #      （再推一条会掐掉正在播的那条）。所以"兜底"只发生在**推之前** ——
        #      `_stream_start()` 拿不到首包时压根不推，原地走老路（那才是主要兜底）。
        #      推出去之后再失败，`_say601` 只能老实报"没念成"，这是必须接受的边界。
        mp3 = None
    b = None
    if BARGE_ON and mic_q is not None:
        try:
            # ★ 延迟 import：spk_barge 会拉起声纹模型（28MB ONNX），
            #   不该让 spk_ear 每次开机都先付这笔钱 —— 不开打断的人一分钱不花。
            import spk_barge
            b = spk_barge.Barge(mic_q, mp3, on_fire=dlna.set_abort)
            b.start()
            log('   👂 边播边听已起（%s）', '声纹判据')
        except Exception as e:                          # noqa: BLE001
            # ★ 铁律：观察者是"更好"，说话是"这次要命" —— 它起不来也必须照常说。
            log('   ✗ 打断观察者没起起来（不影响这次说话）：%s: %s', type(e).__name__, e)
            b = None
    try:
        # ★★★ 2026-09-23：`url`/`probe`/`wait_path` 是上面 `_stream_start()` 算出来的，
        #   之前**算完就丢了** —— 流式那条路于是白起：`mp3=None` 又没传 url，
        #   等于一个字节都没推出去（而且还会在 `say()` 开头 `basename(None)` 炸掉）。
        #   三个一起传才对上 `_say601` 留的口子；走老路时这三个都是 None，
        #   `say()` 那边逐字节不变。
        ok = dlna.say(mp3, url=url, probe=probe, wait_path=wait_path)
    finally:
        if b is not None:
            b.stop()
    if ok == 'cut':
        log('   ✋ 被主人打断 —— 让路（%s）', b.stats() if b else '观察者没起')
    elif ok:
        log('   ✓ 念完了')
    else:
        # ★★★★★ 2026-09-25 夜改：这里原来写的是【猜的】——「没念成（夜间禁声 / 音箱没来取文件）」。
        #   当晚 21:39 那一轮它猜错了，而且错得把排查带偏：设备 21:39:20 明明【来取了】
        #   文件（上一行日志就是 `★ 音箱在取了`）、当时也不是夜间。真因是设备自带的网易
        #   引擎插了一嘴（`0x701` 播它自己的提示音 + 云 TTS「没有音乐可以推送」），
        #   总控的 ContinuePlay 把 playerId 1 按住 ⇒ 我们的话被静音着压掉。
        #   而真因**本来就打在上一行**（`✗ 补发 N 次后仍停在 0x4`），是这个括号盖住了它。
        #   ⇒ 规矩：**判据说不出来就别替它编**。`dlna.LAST_ERR` 是 `say()` 出口处记下的
        #     真实原因（每一句开头清零，不会串味），有就照抄，没有就老实说不知道。
        why = (getattr(dlna, 'LAST_ERR', '') or '').strip()
        log('   ✗ 没念成：%s', why if why else '（dlna 没说原因 —— 见上面几行它的打印）')
    return b


# ------------------------------------------------- 抄答案：把设备引擎的唤醒事件接过来
class WakeTap:
    """收设备【自己唤醒引擎】认出来的唤醒事件，当成我们的一次唤醒。

    ★★★ 为什么需要它（2026-09-21 晚实测定的案，不是猜的）：
      我们的 KWS 吃的是 mictap 在 `snd_pcm_readi` 上抓的【AEC/波束成形之前】的原始单麦；
      设备自己的 duilite 有 4 麦远场波束成形 + AEC。同一段时间里：

          设备引擎  醒了 8 次，8 次全认出（`Duilite_wakeup_cb`，confidence 0.545/0.542/0.579，它的门限 0.52）
          我们的 KWS 醒了 2 次

      主人三次喊"嘀嗒嘀嗒"（20:54:38 / 20:55:51 / 20:55:55）**设备每次都认出来了**，
      我们一次都没认出来 —— 他那边看到的就是"根本没回应"。

    ★ 这一条是【硬件差距】，不是调参能补的。两条路都已经实测否掉：
        · 电平：真机录音放大 100 倍仍不命中（模型做 CMVN 归一化，增益被吃掉）
        · 音素：扫了 14 种声调/拼法，只有 keywords.txt 里原有那两条命中
      ⇒ 结论：**抄它的答案**。它认出来了，我们就当自己被唤醒。

    ★ 反向仍然成立：**它没认出来的时候，我们自己的 KWS 还在** —— 两条并存，不是替换。

    ★ 传输：设备侧 `waketap.sh` 盯 netease_voice 的日志，命中一行就
      `echo WAKE | nc <大脑那台机> 9997`。设备上那个 nc 是 busybox 精简版，**只支持 TCP**，
      所以这边也必须是 TCP（不是我们偷懒用 UDP）。ufw 对你的局域网段全放行，不用改防火墙。

    ★ 铁律照抄 `spk_voiceprint.py`：这条路**绝不许拖挂嗓子**。绑不上、读出错、
      任何异常 ⇒ 记一行日志然后当它不存在，**绝不抛到主循环**。
      让音箱因为"抄不到答案"而说不出话，比抄不到糟得多。
    """

    def __init__(self, ear, port=WAKE_PORT):
        self.ear, self.port = ear, port
        threading.Thread(target=self._serve, name='wake-tap', daemon=True).start()

    def _serve(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', self.port))
            s.listen(4)
        except OSError as e:
            log('⚠ 设备唤醒口 TCP %d 绑不上（%s）⇒ 只靠我们自己的 KWS', self.port, e)
            return
        log('👂 设备唤醒口 TCP %d 已开（设备引擎认出来就抄它的）', self.port)
        while True:
            try:
                c, a = s.accept()
                try:
                    d = c.recv(64)
                finally:
                    c.close()
                if d.startswith(b'WAKE'):
                    log('🔔 设备引擎认出了唤醒（来自 %s）', a[0])
                    self.ear.wake_from_device()
            except Exception as e:
                # ★ 这里绝不能让异常冒出去打断 accept 循环，更不能冒到主循环
                log('⚠ 设备唤醒口出错：%s: %s', type(e).__name__, e)
                time.sleep(1)


# ============================================================ 求助结果的播报
# 主人 2026-09-23 定的规格（大意）：
#   「另一条线发起的能力学习 给音箱也发一份 但音箱那边规则睡眠期一样亮绿灯就行
#     但免唤醒期间 没在说话则说一句我刚刚学习了新能力 我现在可以怎么怎么样了
#     而说话时 就要等话说完 然后衔接一下 比如 对了 我刚刚学会了一项新能力 XXX
#     告诉主人怎么用」
#
# ★★ 角度和情景是**两个正交的维度**，别混成一张表（主人专门纠正过）：
#   【角度】音箱永远是**当事人**，第一人称"我" —— 不管这事是另一条线发起的还是它
#          自己发起的。装的本事就长在它身上（`ext/*.py` 跑在 spk-ear 进程里），
#          所以它说"我学会了"，不说"主人那边装好了"。另一个壳那边同理：**它不当
#          传声筒**（那半边的账见那个壳）。
#   【情景】决定**何时说、拿什么话头**：
#     睡眠期         → 不出声，亮绿灯（主人不在跟前，说了没人听）
#     免唤醒 + 空闲   → 直接开口（人就在跟前，且没在说话）→ 话头 `idle`
#     免唤醒 + 正说   → 不抢，等这句说完**衔接**上          → 话头 `join`
#
# ★★★ 为什么这句话不交给大模型现编（想清楚再改）：
#   `brain()` → `skills.run()` 会把**全部工具**交给模型，播报这一轮它完全可能顺手
#   调 `play_music`（半夜突然放歌，而且这是我们**主动开口**的轮次，主人连话都没说，
#   出了事无从解释）。而播报这件事没有任何需要判断的地方：要念的就是 `announce`
#   那一句（Claude 装完时现写的人话，由求助后端生成）。
#   ⇒ 现成话头 + announce：零工具风险、零合成延迟。
#   ★ 谁要改成现编，**必须先给 `run()` 一个"这一轮不给工具"的开关**。
ANNOUNCE_LEAD = {
    'join': os.environ.get('SPK_ANNOUNCE_LEAD_JOIN', '对了，'),
    'idle': os.environ.get('SPK_ANNOUNCE_LEAD_IDLE', '那个，'),
}

# ---------------------------------------------------------------- 安抚音
# 主人 2026-09-23 定名并定规格，原话：
#   「其实准确的说 这部分应该是安抚音 因为并不是音箱思考 而是等待」
#   「因为期间是可以对话的 所以寂静一段时间才需要思考音 说明用户在安静的等
#     而有对话 则可能是问别的或者问这事 那就回答就行 不用思考音了」
#
# ★★★ 所以它跟「思考音」是两种东西，别混：
#   · 思考音（`spk_filler`）= **音箱在想**，几秒级，主人说完话等着听回答时响
#   · 安抚音（这里）      = **音箱在替主人等**，分钟级，主人在旁边干等时响
#   前者是语气词（「嗯…」），后者是**播报式**的一句话 —— 因为要传达的是
#   "有人在替你办"，不是"我在想"。嗓音**不换**（还是 qwen·Maia）：区别靠说的是什么，
#   不靠换一个人来配音。★ 换嗓子=换响度，绝不碰那条链。
#
# ★★★ 触发判据是 **「屋里有动静之后静默了多久」**，不是「求助交上去多久了」：
#   「动静」= `max(_speech_at, _said_at)` —— 主人开口 / 音箱出声都算。
#   ⇒ 主人一问一答，静默时钟一路被打断 ⇒ **安抚音根本轮不到**（这就是主人要的
#     "有对话就不用"，不用额外写规则，是判据本身长出来的）。
#   ⇒ 反过来说：**安抚音只在让路检查点播**（跟播报共用一个点，见 `record()`）——
#     那个点保证"主人都还没开口"，两边是同一个事实的两面。
COMFORT_ON = os.environ.get('SPK_COMFORT', '1') == '1'
# 递增间隔（秒）：播第 1 句要求静默满 8 秒；播第 2 句要求**再**静默满 20 秒……
# ★ 越往后越稀疏是有意的 —— 一直安静说明主人可能就是不想说话（在忙别的），
#   催得越勤越烦。第 1 档 8 秒不能再短：主人刚听完回答，1 秒后就跟一句
#   "我还在等哦"会显得聒噪。
# ★★ 档位必须**全部落在 `SESSION_IDLE`（2 分钟）之内** —— 超时一到就收工了，
#   排在它后面的档位永远播不到。所以 2026-09-23 加了静默超时之后，这里从 4 档
#   收到 3 档（原第 4 档 90 秒，离告别只剩 30 秒，连说两句太赶）。
COMFORT_GAP = (8.0, 20.0, 45.0)
# ★ 额度留代码里（防呆，不做成可调）：一条求助最多安抚这么多句，之后闭嘴。
COMFORT_MAX = len(COMFORT_GAP)
# 只安抚"正在办"的状态。★ 刻意**不含 `proposed`** —— 那时方案已经出来了，
# 主人在别处看审批卡片，音箱再念叨就是噪音。
COMFORT_LIVE = ('new', 'pending', 'asking', 'running')
# 短语按主人定的口吻写：**第三方、但仍是音箱的角度**（是这只音箱在跟你说
# "我去搬救兵了"，不是换了个人来配音）。★ 自带话头，所以不加 `ANNOUNCE_LEAD`。
COMFORT_PHRASES = [
    '我问一下我的助手，稍等啊',
    '还在查呢，马上就好',
    '这个得让大佬研究一下，你稍等会儿',
    '还在查，别急啊',
]

# ------------------------------------------------- 「我还听着」提示
# 主人 2026-09-23 追加，原话：
#   「音箱免唤醒期间 如果一直寂静到10分钟太长了 做一个间隔一定时间提问的
#     让用户知道他还听着」
#
# ★★★ 它**不是**在改那条"不做静默超时"的规矩（`session_loop` docstring 里
#   ①②③，主人 2026-09-21 定的）：会话该开 10 分钟还是 10 分钟，一个字没改。
#   加的只是"安静久了**说一句**"—— 让主人知道**不用再喊唤醒词**，它还听着。
#   （正因为"不结束会话"是主人的选择，这里更不能反过来替他决定收工。）
#
# ★ 跟安抚音**互斥**、共用同一套机制：判据相同（静默够久了吗），只是说的话
#   不同 —— 有求助在办说「我问问我的助手」，没事在办说「我还听着呢」。
#   两套间隔也不同：安抚音 8 秒起（主人在**等你办事**，该早点出声），
#   这个 30 秒起（主人可能只是在**想事情**，催急了就是打断）。
IDLE_KEY = '@idle'          # `played` 里的键；★ 带 @ ⇒ 绝不会跟 qid 撞
# 递增间隔（秒），**按"静默到这一刻"算**（不是"每隔 N 秒"，跟安抚音同一套口径）。
# ★★ 铺满这段窗口是这里的重点：主人说"寂静到 10 分钟太长了"，而 2026-09-23
#   又加了静默超时 ⇒ 实际窗口 = `SESSION_IDLE`（2 分钟），不再是 10 分钟。
#   ⇒ 两句分别落在 30 秒 / 80 秒，最后留 40 秒安静，再告别（"我先撤了"）。
#   ★ 档位必须**落在 `SESSION_IDLE` 之内**（排在后面的永远播不到，就是死代码）。
#   越往后越稀疏是有意的（跟安抚音同理）：一直安静说明主人可能在忙别的。
IDLE_GAP = tuple(float(x) for x in
                 os.environ.get('SPK_IDLE_GAP', '30,80').split(','))
IDLE_PHRASES = [
    '我还听着呢，你慢慢想',
    '不急，我在呢',
    '想好了跟我说就行',
]

# ---------------------------------------------------------------- 静默超时
# ★★★★★ 主人 2026-09-23 **改了口**，原话：
#   「静默超时还是要做 并且2分钟后一直没有用户回复 就恢复睡眠状态
#     并说一句 我先撤了有事再找我」
#
# ⇒ 这一条**推翻了** `session_loop` docstring 里 2026-09-21 那条
#   「**不做静默超时**：屋里没人说话时它就那么安静地开着，那是主人的选择」。
#   那条已经照本次指令改掉了（去读那段的 ④）—— ★ 所以别再拿它当规矩。
#
# ★★★ 判据是「**主人**多久没开口」，**不是**「屋里多久没动静」—— 这是这里
#   最容易搞错的一处：音箱自己每 30/80 秒就说一句"我还听着"，`_said_at` 一路
#   被刷新 ⇒ 拿"动静"当基准的话，超时**永远不会到**（自己给自己续命）。
#   ⇒ 所以另立一个只在**人声**出现时才刷新的戳：`Ear._last_user_at`。
# ★ 到了就：先说完那句告别，**再**收工（顺序不能反，见 `session_loop` 里的调用点）。
# ★ 另一个壳**不跟这条**：那边沉默两分钟可能只是主人在听/在忙，挂断的代价
#   比音箱这边大一个量级 —— 那边有自己的挂断逻辑，一个字没动。
SESSION_IDLE = float(os.environ.get('SPK_SESSION_IDLE', '120'))
SESSION_BYE = os.environ.get('SPK_SESSION_BYE', '我先撤了，有事再找我')

# ★★★ 确定性收工词 —— 主人 2026-09-24 要的兜底（"我说没事了 拜拜 应该都进到睡眠状态"）。
#
#   为什么光有 `end_session` 不够：那是个**工具**，调不调由模型自己决定，而实测它会
#   「嘴上说要睡、手上不调工具」—— 2026-09-24 21:36:28 日志实测：
#       主人：没事了   →   它回：好，那我先歇着，有事喊我。
#     而这一轮里**一行 `⏹ end_session(...)` 都没有**，会话照旧开着（21:38 还在
#     「本段已开 193 秒」），麦克风继续听屋里。★ 它 21:14 那次是调了的 ⇒ 是**漏调**。
#   而「主人明确说 没有/没事了/别听了 ⇒ 立刻停止倾听」是主人 **09-21 自己定的规矩第 3 条**
#   ⇒ 那它就不该交给模型去记 —— **这是规则，规则要落在代码里**（同 `spk-brain-first-principle`
#     那条：额度/门限/安全必须留代码里）。
#
#   ★★ 判据是【整句话就是它】，**不是包含**：去掉首尾标点、剥掉开头的纯语气词之后
#      必须**逐字相等**。理由：`没事了` 在"那个菜没事了""我没事了"里都会出现，
#      子串匹配会误杀；整句相等则基本只有一个读法。
#   ★★ 刻意**不收** `没有` / `不用了` / `没了`：「给我放首周杰伦」→「不用了」是
#      "不用放这首"，做整句时那种读法更常见，误杀的代价是**当场把耳朵关上**。
#      这几个仍然写在 `end_session` 的工具说明里，留给模型自己判断。
#   ★★ 命中就**只改控制信号**：不再调一次模型、**绝不额外出声**。主人这一轮该听的话
#      模型已经说过了；再补一句"那我先睡了"正是他烦的那种"自言自语"。
#   ★ 关掉它：`SPK_HARD_STOP=off`（真出问题时的第一个开关）。
_HS_RAW = os.environ.get('SPK_HARD_STOP', '没事了,没什么事了,没你事了,拜拜,拜拜了,再见,'
                                         '别听了,不听了,不聊了,先这样,就这样,就这样吧,'
                                         '去忙吧,你忙吧,挂了')
HARD_STOP = () if _HS_RAW.strip().lower() in ('off', '0', 'none') else tuple(_HS_RAW.split(','))
HARD_STOP_EDGE = ' \t，。！？、,.!?~…—－-·'      # 首尾要剥掉的标点/空白
HARD_STOP_HEAD = '嗯哦呃哎诶'                     # 开头的纯语气词（不改变意思）


def is_hard_stop(text):
    """主人这【整句】就是在收工吗。回命中的那个词（真值）或 ''。

    ★ 只做字符串相等，不做任何语义猜测、不调模型、不出声 —— 见上面 `HARD_STOP`。
    """
    if not HARD_STOP:
        return ''
    t = (text or '').strip().strip(HARD_STOP_EDGE)
    t = t.lstrip(HARD_STOP_HEAD).strip(HARD_STOP_EDGE)
    return t if t in HARD_STOP else ''

# ★★★ 灯码**从来没有被实测过**：整张 buscmd 表是从反汇编里推出来的，`ring_probe.sh`
#   自己那句注释白纸黑字写的是「★**应该**是绿色」。⇒ 第一次试必须【主人当场看着
#   眼睛确认】、【一次只发一个码】，先拿 2569（只读查询）当阴性对照。
#   ⇒ 所以留了总开关：`SPK_HELP_LED=0` 一个字节都不发。
#   ★★★ 绝不要试 2562（唤醒）—— 它会打开免唤醒窗，等于让音箱当场开始听屋里说话。
LED_ON = os.environ.get('SPK_HELP_LED', '1') == '1'
LED_DONE = int(os.environ.get('SPK_HELP_LED_DONE', '2560'))    # 绿：装成了
LED_FAIL = int(os.environ.get('SPK_HELP_LED_FAIL', '2561'))    # 红：没装成
# ★ `. /tmp/dbus_env.sh` 现取会话总线地址 —— 设备上现成在用的一行
#   （`device/spk_dbus_play.sh` 第一行就是它，而且验证过）。总线地址每次开机随机，
#   所以必须在**设备上**现取，不能从本机这边算好了传过去。
# ★ 5 个参数一个都不能少（少一个设备回 `Error InvalidArgs`）：
#   flag / playerId / cmd / jsonLen / json。
LED_CMD = ('. /tmp/dbus_env.sh; dbus-send --session --print-reply --reply-timeout=400 '
           '--dest=netease.ihw.controller /netease/ihw/controller '
           'netease.ihw.SmartAudio.API uint32:3 uint32:1 uint32:%d uint32:2 string:"{}"')


class Announcer(threading.Thread):
    """盯求助队列：装成了的、还没跟**音箱这条线**报过的，负责报出去。

    ★ 为什么非有这个线程：装能力从头到尾是**异步**的（主人什么时候点头、
      Claude 什么时候装完，都不由音箱决定），而音箱是靠"唤醒一次、聊一轮"活着的
      —— 它没有任何一处会主动回头看结果。2026-09-23 那条线通了 246 秒的那一次，主人
      连问三遍"怎么样了"、音箱只能一路答"还没回话"，根子就在这儿。
    ★★ 它**只发现、不开口**。开口的时机全部交给 `session_loop`（见 `_announce_*`）：
      线程不知道音箱此刻是不是正说着，而 `session_loop` 知道 —— 主人那句
      "说话时就要等话说完再衔接"只能落在那里。
    ★ 两条线各自记账（`told_speaker_at` / `told_phone_at`）：装的是**音箱的本事**，
      所以那个壳发起的那条**也要给音箱报一份**（主人 2026-09-23 原话）；但两条线的
      出口完全不同（音箱 0x601 / 那个壳自己的音频通道），谁报过谁记账，互不顶替。
    """

    POLL = float(os.environ.get('SPK_ANNOUNCE_POLL', '4.0'))

    def __init__(self, ear):
        super().__init__(name='announcer', daemon=True)
        self.ear = ear
        self.lock = threading.Lock()
        self.todo = []          # 会话中待播报的（session_loop 取走）
        self.stop = threading.Event()
        # ★ 安抚音进度：qid → 已经插了几句。★ 只活在内存里（重启就重来一次，
        #   最多多一句"还在查"，无害）—— 不落盘是因为**音箱这条线才播安抚音**，
        #   而条目文件是两条线共用的，往上面加字段会污染另一条线那边的账。
        #   ★ `IDLE_KEY` 也在这一份里（"我还听着"的额度），换个会话就清（见 `_maybe_comfort`）。
        self.played = {}
        self._idle_session = 0.0    # 上次看到的会话开始时刻（换会话 ⇒ 额度归零）

    # ---- 主线程（session_loop / record）用的两个口 ----
    def has(self):
        with self.lock:
            return bool(self.todo)

    def take(self):
        with self.lock:
            todo, self.todo = self.todo, []
        return todo

    # ---- 线程体 ----
    def run(self):
        while not self.stop.wait(self.POLL):
            # ★ 安抚音 /「我还听着」先查（跟播报共用这一个线程和一个轮询 ——
            #   都是"看看有没有该主动开口的"，分成两个线程只会多一份竞态）。
            #   它们跟播报一样**只发现、不开口**：挂进 `todo`，由 `session_loop`
            #   挑时机说（那儿才知道音箱此刻是不是正说着）。
            if COMFORT_ON:
                try:
                    self._maybe_comfort()
                except Exception as e:                      # noqa: BLE001
                    log('✗ 安抚音检查出错（下轮再来）：%s: %s', type(e).__name__, e)
            try:
                pend = helpq.pending_announce('speaker')
            except Exception as e:                          # noqa: BLE001
                log('✗ 查求助队列出错（下轮再来）：%s: %s', type(e).__name__, e)
                continue
            if not pend:
                continue
            if self.ear.in_session:
                # ★ 会话中 ⇒ 不抢话，挂进待办，等 `session_loop` 挑时机
                #   （它知道音箱现在是不是正说着）。
                with self.lock:
                    have = {d.get('id') for d in self.todo}
                    for d in pend:
                        if d.get('id') not in have:
                            self.todo.append(d)
                            log('📣 有新本事要报（%s）—— 等音箱这轮说完就接上', d.get('id'))
            else:
                # ★ 睡着 ⇒ 只亮灯、不出声（主人不在跟前，说了没人听）。
                for d in pend:
                    self._led(d)
                    helpq.mark_told(d.get('id'), 'speaker')

    def _led(self, d):
        """睡眠期报信 —— 亮灯，不出声。

        ★ 只在**睡眠**这一支发（看 `run()`）：会话里走的是"说一句"，不亮灯。
          顺带这也是安全的 —— 亮灯和 TTS/播放共用 `~/.spk-net/cmd` 那个**单槽**
          （取走即清），而睡着的时候槽是空的。
        ★ 用 `spk_netd.post` 不用 `send`：亮灯是"投递即忘"的事，主人看得见，
          `post` 不撤单（设备上线就执行）。`send` 超时会**撤单**，等于白投。
        """
        if not LED_ON:
            return
        ok = d.get('status') == 'done'
        try:
            import spk_netd                           # 延迟 import：这条链不许拖挂东西
            spk_netd.post(LED_CMD % (LED_DONE if ok else LED_FAIL))
            log('💡 睡眠期报信（%s）：亮%s灯 —— 命令已投给设备',
                d.get('id'), '绿' if ok else '红')
        except Exception as e:                          # noqa: BLE001
            log('✗ 亮灯没投出去（照样记账，不反复试）：%s: %s', type(e).__name__, e)

    def _since_open(self):
        """这一轮会话是什么时候开的 —— 静默计时的**下界**。

        ★ 为什么需要它：会话刚开的那一刻，`_speech_at`/`_said_at` 还停在**上一段
          会话**（或者进程刚起，就是 0），算出来的"静默"可能是好几分钟 ⇒ 一唤醒
          就蹦出一句"我还听着呢"。会话刚开、还没人说过话，那不是"主人安静地在等"，
          是"还没开始"。
        ★ 取 `Ear` 上那个进程内的戳（`session_loop` 开会话时填）—— 不比读
          `session/current.json`，那个文件每轮结束才落盘，会话头十几秒里是旧的。
        ★ 兜底读文件：万一哪天 `_session_open_at` 的填写点被挪掉，这里还认得路
          （读不到就返回 0 —— 退化成"按上一句话算"，最多早说一句，不会哑掉）。
        """
        o = getattr(self.ear, '_session_open_at', 0.0)
        if o:
            return float(o)
        try:
            return float((sess.load_last() or {}).get('opened') or 0.0)
        except Exception:                                   # noqa: BLE001
            return 0.0

    def _quiet_for(self, open_at):
        """屋里安静了多久 —— 主人开口 / 音箱出声 / 会话开始，**取最近的那个**。

        ★ `getattr` 兜底：那两个是 `Ear` 的内部字段，万一哪天改名了也不该把这个
          线程炸掉 —— 这条链是"锦上添花"，绝不值得为它挂掉整个播报。
        """
        ear = self.ear
        return time.time() - max(getattr(ear, '_speech_at', 0.0),
                                 getattr(ear, '_said_at', 0.0), open_at)

    def _offer(self, key, gaps, phrases, quiet, what):
        """按"静默够不够久"决定要不要挂一句 —— 安抚音和"我还听着"共用这一份。

        ★ 判据只有一条：**静默够久了吗**（`quiet` 由调用方算好）。
        ★ 进度记在 `self.played[key]`：两条链 key 不同（qid / `IDLE_KEY`），各记各的。
        ★ 已经在待办里的不叠第二条 —— 同一句话连说两遍最讨厌。
        ★ 在"挂上"时就记进度、不是等播出时：`todo` 被 `take()` 取走后基本都会播
          （让路检查点已经在那个时刻了），这里少记一次反而会让同一句反复排队。
          万一真没播成，代价只是少一句。
        """
        n = self.played.get(key, 0)
        if n >= len(gaps):
            return False                    # 额度用完 ⇒ 闭嘴（防呆）
        if quiet < gaps[n]:
            return False                    # 还没静够 —— 主人可能正在说话
        with self.lock:
            if any(d.get('id') == key for d in self.todo):
                return False
            self.todo.append({'id': key, 'kind': 'comfort',
                              'text': phrases[n % len(phrases)]})
        self.played[key] = n + 1
        log('💬 安静了 %.1f 秒 ⇒ %s（第 %d 句）', quiet, what, n + 1)
        return True

    def _maybe_comfort(self):
        """安静久了就主动说一句 —— **有求助在办说安抚音，没有就说"我还听着"**。

        ★★★ 两者判据完全相同（屋里静了多久），只是说的话不同：
          · 有求助在办 ⇒「我问一下我的助手，稍等啊」（主人在**等你办事**，8 秒起）
          · 手上没事   ⇒「我还听着呢，你慢慢想」（主人可能只是在**想事情**，30 秒起）
          ⇒ 互斥，所以不会一句话说两遍，也不会两个都不说。
        ★★★ 判据全在"静默了多久"上（理由见文件头 `COMFORT_*` 那段），不是定时器：
          主人一问一答，`max(_speech_at, _said_at)` 一路被刷新 ⇒ 够不到第一档
          ⇒ **有对话就自动不播**。主人 2026-09-23 那句
          「有对话……那就回答就行 不用思考音了」是**这个判据长出来的结果**，
          不是一条要单独写的规则。
        ★ 只在 `in_session` 时挂：睡着了说明主人已经走了（或会话结束了），
          在他不在跟前的屋里出声没有意义 —— 跟播报那边"睡眠 ⇒ 只亮灯"同理。
          （夜间禁声另有 `dlna` 底层那道 HP/PO 闸门兜着，这里不重复做。）
        """
        ear = self.ear
        if not ear.in_session:
            return
        open_at = self._since_open()
        quiet = self._quiet_for(open_at)
        live = [d for d in helpq.all_open()
                if d.get('kind') == 'capability' and d.get('status') in COMFORT_LIVE]
        if live:
            for d in live:
                qid = d.get('id')
                if qid:
                    self._offer(qid, COMFORT_GAP, COMFORT_PHRASES, quiet, '插一句安抚')
            return
        # ---- 手上没事 ⇒ "我还听着"。额度是**按会话**算的，换了会话归零 ----
        if open_at != self._idle_session:
            self._idle_session = open_at
            self.played.pop(IDLE_KEY, None)
        self._offer(IDLE_KEY, IDLE_GAP, IDLE_PHRASES, quiet, '说一句"我还听着"')


# ---------------------------------------------------------------- 主循环
class Ear:
    def __init__(self, guard=True):
        self.mic = Mic()
        self.kws = None
        self.asr = None
        self.vad = None
        self._live = None           # ★ 流式识别那条路（`load()` 里建，见 LiveAsr）
        self.guard = guard          # 自己说话时把耳朵闭上，免得听见自己
        self.speaking = False
        self.pending = None         # ★ 被打断时主人开口那半句（预卷 + 等待期收的），
                                    #   挂在 self 上交给【下一轮】record() 拼在最前面
        self._hit = ''              # 这次唤醒命中的是哪句唤醒词（session_loop 打日志用）
        # ★ 抄设备引擎答案那条线（见 WakeTap）。`_ext_wake` 是它递过来的"刚醒了一次"。
        #   ★ 分流的责任在 `wake_from_device` 那边（按 `in_session`）：
        #     会话中途又醒 ⇒ 躲应答、**不置这个标志**（否则会凭空多出一轮"醒了没人说话"）；
        #     不在会话里 ⇒ 置上，此时我们一定是待在 listen() 里等它。
        #   ★ 所以 listen() 里【不要】清它 —— 清了会吞掉"会话刚收工那一下"进来的真唤醒。
        self.waketap = None
        self._ext_wake = False
        # ★ 唤醒后躲设备那声应答的截止时刻（`_drain` 认它）。
        self._ack_until = 0.0
        # ★ 会话跑着的时候为真 —— 会话中途又醒的那一下该躲应答、但不该攒成"唤醒"。
        self.in_session = False
        self._idle_log = 0.0        # 会话里"候着"那行心跳的上次时间（见 turn）
        # ★★★★★ 自听回灌闸门的底账（见 `_is_self_echo`）：
        #   `_last_said` = 上一轮我们念出去的字；`_said_at` = 念完那一刻。
        #   ★ 绝不挂到 session 上 —— 它是"这一句刚说完"的短期状态，跟会话记忆无关。
        self._last_said = ''
        self._said_at = 0.0
        # ★★★★★ 这一轮【人声是从哪一刻开始的】（`record()` 里填，0 = 这一段还没人声）。
        #   早跑那道闸门就靠它判"是不是自家回灌"，理由见 `FILLER_EARLY` 那一段 ——
        #   回灌必然在我们【还在播】的时候就开始，所以"人声起点晚于 `_said_at`"
        #   就等于"这是主人说的"，比老的"念完 4 秒内一律不起跑"准得多也快得多。
        self._speech_at = 0.0
        # ★★★★★ 这一段会话【什么时候开的】（`session_loop` 里填，0 = 没在会话）。
        #   给「我还听着」当静默计时的**下界** —— 上面那两个时刻在一段会话刚开时
        #   还停在**上一段**（或者进程刚起，就是 0），拿它们当基准会算出一个"已经
        #   安静好几分钟"的假象 ⇒ 一唤醒就蹦出一句"我还听着呢"。
        #   ★ 用进程内这个戳、不去读 `session/current.json`：那个文件是**每一轮
        #     结束才落盘**（`session_loop` 里的 `session.save()`），会话刚开的那
        #     十几秒里它写的还是上一段会话的 `opened` —— 正好是这里要防的那个假象。
        self._session_open_at = 0.0
        # ★★★★★ **主人**最后一次开口是什么时候（跟 `_speech_at` 不是一回事）。
        #   `_speech_at` 是"**这一轮**人声从哪一刻开始"，每一轮 `record()` 开头
        #   都清零 ⇒ 轮与轮之间是 0，做不了"多久没开口"的账。
        #   静默超时（`SESSION_IDLE`）**只能用这个戳** —— 拿 `max(_speech_at,
        #   _said_at)` 当基准的话，音箱自己每 30 秒一句"我还听着"就把时钟刷新了，
        #   超时永远不会到（= 自己给自己续命）。
        self._last_user_at = 0.0
        # ★★ 最近一次**播报结果**是什么时候（`_announce_speak` 里只有播报分支填）。
        #   给静默超时当"再等等"的缓冲：播报那一刻主人多半早就超时了，不给缓冲
        #   就变成"……你试试。我先撤了"两句连着说。★ 安抚音/「我还听着」**不填**
        #   这个戳 —— 那两句要是也能续命，超时就永远到不了（见 `_idle_over`）。
        self._announced_at = 0.0
        # ★ 「因为手里有活所以先不撤」这句日志只报一次用的（见 `_idle_over`）。
        self._held_for_help = False
        # ★ 认人的状态机，【一段会话一个】（在 session_loop 里新建）。
        #   关着的时候永远是 None ⇒ `_observe_who` 交回空 ctx ⇒ 一个字都不加。
        #   ★★ 它【绝不能挂到 session 上】：`session.save()` 会把声纹向量写进
        #     `session/current.json`（明文、权限默认）—— 那是生物特征。
        self.ident = None
        # ★★ 求助结果的播报（见 `Announcer`）。★ 只在这条**常驻**路上建（`main()`），
        #   三条离线自测路在 `Ear()` 之前就 return 了 ⇒ 自测永远不碰队列、
        #   不亮灯、不出声。这里给 None 是让 `record()` / `_announce_speak()` 有得判。
        self.announcer = None
        # ★ 这一轮的播报该用哪个话头（见 `ANNOUNCE_LEAD`）：
        #   `record()` 让路时置位 ⇒ 主人没在说话 ⇒ 音箱主动开口，要个起头（idle）；
        #   没置位就是"音箱刚说完一轮" ⇒ 顺着话头接（join）。读完即清。
        self._gave_way = False

    def load(self):
        t0 = time.time()
        self.kws = make_kws()
        self.asr = make_asr()
        self.vad = make_vad()
        # ★ 2026-09-23：流式那条路（见 LiveAsr 上面那段定案）。
        #   与 asr/vad 同一批建好 —— 它不是"每一轮新建"的东西，是【一条可复用的流】，
        #   每轮 `record()` 开头 reset() 一次（丢弃，不产出）。
        self._live = LiveAsr(self.asr)
        log('引擎就绪 %.1fs（KWS chunk-%d thr=%.2f / ASR zipformer-int8 / silero VAD）',
            time.time() - t0, KWS_CHUNK, KWS_THR)
        # ★ 声纹模型丢后台预热：它值 0.8 秒，而"喊一声→它应你"那一下最不能愣。
        #   放最后起，绝不挡上面三件正事的日志。
        if WHO_ON:
            th = threading.Thread(target=_warm_who, name='who-warm', daemon=True)
            th.start()
        # ★ 思考音的轨也要预热，理由比声纹更硬：它最坏要拼 18.87 秒（缓存对不上时
        #   会把 7 条短语全部重合成），万一落在他问完一句话之后，思考音比他等的还久。
        #   同样丢后台 —— 预热慢一点没关系，它只要赶在他开口之前好就行。
        if FILLER_ON:
            th = threading.Thread(target=_warm_filler, name='filler-warm', daemon=True)
            th.start()
        # ★★ TTS 常驻连接：**每句话**都要用的东西，所以启动就先建一条（≈1.1 秒，一次）。
        #   两个唤醒点也会各 warm 一次（那两次是【真合成一句】，会赶上"录音 + ASR"）。
        #   这里只建连、不真发：连接空转 45 秒就被服务端掐，开机时焐热的那点温度
        #   活不到第一次唤醒，真发那一句纯粹是白花（理由见 spk_tts.WARM_REAL）。
        #   ★ 失败不影响任何事：spk_tts 里所有异常都只让它自己退回命令行那条慢路。
        tts.set_logger(log)                   # 它的日志走我们这个 log（时间戳一致，好对照）
        tts.warm(real=False)
        # ★ 抄设备引擎答案的那条线 —— 起在最后，绝不挡上面三件正事的日志。
        #   它绑不上也只是少一条路（自己那个 KWS 照常），不会让耳朵起不来。
        self.waketap = WakeTap(self)

    # ---- 拿一段音频（不足 chunk 就先攒着）
    def _drain(self, timeout=0.5):
        """★ 这里是音频离开队列的【唯一出口】—— 所以唤醒后躲应答也放在这儿：
        listen()、record()、预卷、_drop_until 全都从这儿拿，谁在收都躲得开。
        放在某个调用方里就会漏掉别的调用方（原来只在 session_loop 开头躲，中途那次就漏了）。

        ★★★ 窗口里【按电平扔】，不按时间扔（2026-09-21 22:24 现场抓到：时间窗口
        把主人唤醒词后面紧接着那句一起吞了）。只有峰值 > ACK_LVL 的帧才当应答丢掉，
        安静的原样交出去 —— 应答哑着的时候（现在就是）一个采样都不丢。
        """
        t_end = time.time() + timeout
        while True:
            if time.time() >= self._ack_until:
                try:
                    return self.mic.q.get(timeout=max(0.01, t_end - time.time()))
                except queue.Empty:
                    return None
            # 还在躲应答的窗口里：只扔【真的响】的那几帧
            if time.time() >= t_end:
                return None
            try:
                a = self.mic.q.get(timeout=min(0.05, max(0.01, t_end - time.time())))
            except queue.Empty:
                continue
            if a is not None and float(np.abs(a).max()) > ACK_LVL:
                continue        # 这一帧就是设备那声应答 ⇒ 扔
            return a            # 安静 ⇒ 是主人在说话 ⇒ 放行

    def _flush(self):
        """把队列里攒着的音频全丢掉，返回丢了多少帧。
        ★ 为什么必须有：say() 是阻塞的，最长能耗 25 秒（推流 + 自听判定）。
           那 25 秒里麦克风一直在进包 —— 而且进的主要是【我们自己刚念出去的声音】。
           不清掉的话，回到 listen() 时先要空转消化这 25 秒旧音频，
           这期间人说话是听不见的（延迟越滚越大）。"""
        n = 0
        while True:
            try:
                n += len(self.mic.q.get_nowait())
            except queue.Empty:
                return n

    def _drop_until(self, deadline):
        """把队列里到 deadline 为止的音频全丢掉（唤醒应答那声"叮"就靠这个躲开）。"""
        n = 0
        while time.time() < deadline:
            a = self._drain(timeout=min(0.2, max(0.01, deadline - time.time())))
            if a is not None:
                n += len(a)
        return n

    def _is_self_echo(self, text):
        """这句 ASR 出来的话，是不是【我们自己刚念出去的尾音】飘回来了？

        ★★★★★ 2026-09-22 晚实测（主人的原话：「我就问了一句几点了 回了好几次」）：
          念完之后麦克风里还飘着我们自己的尾音（这条路上音频有延迟），追问窗口
          把它当成新问题收走，ASR 只截到最后那几个字，大脑就一本正经地又答一遍
          —— **一句问话变成三个回答**：

            20:07:19  ASR「您说几点了」→ 回「…现在晚上八点零七分，星期二。」
            20:07:29  念完了 → 20:07:30 听见了（0.68 秒）→ ASR「星期二」→ 又答一遍
            20:07:41  念完了 → 20:07:42 听见了（0.52 秒）→ ASR「星期二」→ 再答一遍

          ★ 判据是【内容】不是时间：问"他说的这些字，有多少能在我们刚念的那句
            **最后 16 个字**里找到"（复用打断那条路上已经在用的 `spk_barge.is_self_echo`，
            只是把对照文本截成尾巴 —— 理由见 `ECHO_TAIL_CHARS` 那段）。
          ★ 为什么不去调"念完→开耳"那个静默窗（`speak()` 后面那个 0.4 秒）：
            回灌的延迟不是固定的，实测 0.4 秒**不够**；而加到 1 秒以上，
            主人跟话的**开头**就会被削掉（他会觉得音箱"没在听"）。
            文本判据零延迟、判的是内容 —— 结构上比调时间窗稳。
            这一条【不替代】那个 0.4 秒窗，是叠在它上面的第二道。
          ★ 判成回灌的动作只是【丢掉这一轮】：不进大模型、不出声、回去接着听。
            所以误判的代价是"他得重说一遍"，而漏判的代价是"音箱自问自答" ——
            和打断那条路上 `is_self_echo` 的取舍**完全一致**，宁可让他重说。

        ★★ 已知的误判形态（自测时验出来的，别当它是 bug 再"修"掉）：
          我们那句话的**句尾**如果正好是个选项词，而主人用那个词作答 ——
          例如我们问「你要定个星期二的**闹钟**，还是就是念叨一下？」，他答「闹钟」——
          那两个字确实在我们句尾里，会被判成回灌而丢掉。
          这一条【认了】：要压掉它就得放宽到"整句相似"，而整句相似会把真正的回灌
          （永远只是一小段尾巴）冲淡成漏判 —— 两头只能选一头，选的是"宁可让他重说"。
          真嫌吵就调 `SPK_ECHO_TAIL_CHARS`（调小＝只认更贴近句尾的）或
          `SPK_ECHO_WINDOW`（调小＝只认更快回来的）。

        ★★★ 2026-09-24 补上第二条对照 —— 原来这套判据只认【自己用 `speak()` 说的
          那一句回答】（`_last_said`），而屋里还有两条路在出声，**都不写它**：

            ① **思考音**：filler 阶梯走 `dlna.push`，不是 `speak()`
            ② **求救进度**：求助后端的播报 → `dlna.say`，**另一个进程**

          ⇒ 这两条路的回声对闸门结构性失明。主人原话「安装成功后 他回答了两遍」，
            现场就是它俩各挨了一次：

              08:33:26  推思考音「让我想想啊」
              08:33:28  📝 ASR：让我想想啊      ← 听见自己
              08:33:29  🔊 回：嗯，有了啊。好，你慢慢想，我在这儿等着。   ← 自问自答①
              08:33:47  📝 ASR：奥尔斯沃尔克斯伯顿TERCOSG来的（英文乱码）
              08:33:48  🔊 回：嗯，是这样。这句我还是没听懂…            ← 自问自答②

            同一轮里**回答的尾音被拦下了两回** —— 闸门没坏，是它不认识那两条路。

          ★ 现在变成"**逐条比对多个对照**"：老判据（上一句回答）一字未动，
            另加 `spk_said` 登记簿里最近 `ECHO_WINDOW` 秒内的每一条。
            任一条判成回灌就丢这一轮。
          ★ 取舍跟原来**完全一致**（宁可让他重说一遍，也不自问自答）；
            fail-open 也照旧：登记簿读不到、判据抛异常 ⇒ 一律放行。
        """
        # ★★ 局部变量原来**就叫 `said`**，本次没用它当模块别名（改叫 `spk_said`），
        #   这里仍改名 `last`：`spk_ear.py` 里 `brain()` 已经有一个局部 `said`
        #   （`said, ctl = skills.run(...)`），别名一撞就是"只在某条路径上才现形"的
        #   UnboundLocalError。名字分开，整类坑一起消掉。
        last, text = self._last_said, (text or '').strip()
        if not text or len(text) < ECHO_MIN_CHARS:
            return False

        # ---- 对照表：① 上一句回答（老判据）② 登记簿（思考音 + 求救那条线）
        #   ★ 每条自带一把尺子（`min_frac`）—— 两条路的"像"标准不一样，见 `SAID_MIN_FRAC`。
        cands = []
        if last:
            age = time.time() - self._said_at
            if age <= ECHO_WINDOW:
                cands.append((last[-ECHO_TAIL_CHARS:],
                              '上一句回答；念完才 %.1f 秒' % age, ECHO_MIN_FRAC))
        try:
            for ref, why in spk_said.candidates(ECHO_WINDOW, ECHO_TAIL_CHARS):
                cands.append((ref, why, SAID_MIN_FRAC))
        except Exception as e:                          # noqa: BLE001
            log('   ⚠ 出声登记簿读不到（只按老判据走）：%s: %s', type(e).__name__, e)
        if not cands:
            return False
        try:
            import spk_barge
            for ref, why, frac in cands:
                echo, why2 = spk_barge.is_self_echo(text, ref, frac)
                if echo:
                    log('   🔁 %r 判成【我们自己刚念的尾音】飘回来了'
                        '（%s；%s）⇒ 丢掉这一轮，不自问自答', text, why2, why)
                    return True
        except Exception as e:                          # noqa: BLE001
            # ★ 判据自己出错 ⇒ 一律放行。宁可让音箱偶尔自问自答一句，
            #   也绝不因为一个判据崩了就把主人说的话整句吞掉（本类错误的血教训）。
            log('   ⚠ 回灌判据出错（放行这一轮）：%s: %s', type(e).__name__, e)
            return False
        return False

    def wake_from_device(self):
        """设备引擎认出来了（`WakeTap` 的线程调）。

        ★ 只有"我们正在等唤醒词"时才算数 —— 那才是主人刚喊了唤醒词。
          我们自己说话时它可能被我们自己的声音骗醒（它有 4 个麦，就在喇叭边上），
          那种一律不算：`listen()` 进门会把这个标志清掉，所以攒不下来。
        """
        if self.speaking and self.guard:
            log('   （设备引擎在我们说话时醒了 —— 多半是听见我们自己，不算）')
            return
        if self.in_session:
            # ★ 会话中途又醒了一次（多半是主人又喊了一遍唤醒词，因为上一轮没理他）。
            #   ★★★ 2026-09-21 22:24 实测改口径：原先躲的是"设备那声应答"，可它早被我们
            #   顶哑了（voicemute 盖掉固件 S003），真正被躲掉的是【主人唤醒词后面紧接着
            #   说的那句话】—— 他 22:24:49 开口、22:24:50 设备才认出来，躲避一开始就把
            #   他「几点了」整段吞了。现在 `_drain` 按电平扔：应答真响就躲得掉，哑着一个
            #   采样都不丢，窗口留着不碍事。
            #   ★ 仍然【不能】置 `_ext_wake`：那会让本轮收工后凭空多出一轮"醒了却没人说话"。
            self._ack_until = time.time() + ACK_SKIP
            log('   （会话中途又醒了 —— 躲应答 %.1fs：只扔 >%.2f 的响帧，安静的放行）',
                ACK_SKIP, ACK_LVL)
            return
        self._ext_wake = True

    def listen(self):
        """守到唤醒词。命中就返回，KWS 流重置过，下一轮是干净的。

        ★ 两个来源，谁先到算谁：
          ① 我们自己的 KWS（吃原始单麦）
          ② 设备引擎认出来了（`WakeTap`，吃 4 麦波束成形）—— 实测它认得出我们认不出的那些

        ★★★ 这里【绝不能】再清 `_ext_wake`（原来进门有一行，2026-09-21 21:48:25 出过事）：
          那天主人喊了一声，日志里只留下 WakeTap 那行 `🔔 设备引擎认出了唤醒`，
          然后**什么都没有** —— 因为那次唤醒正好落在"上一段会话收工 → run() 又调一次
          listen()"的空档里，被进门那行抹掉了。他看到的就是"喊了没反应"。
          `wake_from_device` 现在用 `in_session` 把"会话中途又醒"和"真的在等唤醒词"分开了：
          后者才置这个标志 ⇒ **它能被置上时，就一定是我们在等**，清它只会吞掉真唤醒。
        """
        s = self.kws.create_stream()
        carry = np.zeros(0, np.float32)
        t_last = time.time()
        while True:
            if self._ext_wake:
                self._ext_wake = False
                self.kws.reset_stream(s)
                return '设备引擎'
            a = self._drain()
            if a is None:
                if time.time() - t_last > 30 and self.mic.last and \
                        time.time() - self.mic.last > 30:
                    log('⚠ 30 秒没收到麦克风音频 —— 音箱那边 mictap 还在吗？')
                    t_last = time.time()
                continue
            t_last = time.time()
            if self.speaking and self.guard:
                continue
            carry = np.concatenate((carry, a)) if len(carry) else a
            while len(carry) >= CHUNK:
                s.accept_waveform(SR, carry[:CHUNK])
                carry = carry[CHUNK:]
                while self.kws.is_ready(s):
                    self.kws.decode_stream(s)
                hit = self.kws.get_result(s)
                if hit:
                    self.kws.reset_stream(s)
                    return hit

    def record(self, prepend=None):
        """从唤醒之后开始录，VAD 判断"说完了"就收。返回 (音频, 说明)。

        ★ `prepend`：上一次说话【被打断】时主人开口那 1.5 秒（预卷）。
          打断之后主人通常是【接着说】下去的 —— 那 1.5 秒就是他这句话的开头，
          不接上去，他等于要重说一遍（"你先听我说"只听到"说"）。
        """
        self.vad.reset()
        # ★ 2026-09-23：流式那条路也在这里开一条新流 —— 与 vad.reset() 【同一处】，
        #   因为两者的生命期必须【一模一样】：都是从"这一轮开始听"到"这一轮取字"。
        #   ★ 只有 why == '说完' 才会去取它的结果（见 turn()）；其余三个出口
        #     （超时 / 没等到人说话 / 预卷）手里的音频不是完整一句，取它没意义，
        #     而下一轮进来时这一条自然被 reset 丢掉 —— 所以【不需要】在每个
        #     return 上补一次 drop()，漏一处就是一个隐形 bug。
        if self._live is None:      # 兜底：理论上来不到（load() 里一定建过）
            self._live = LiveAsr(self.asr)
        self._live.reset()
        t0 = time.time()
        buf, speech_at = [], None
        self._speech_at = 0.0

        def done(audio, why):
            """★ 出口只此一个：预卷一律拼在最前面。
            为什么收在函数里而不是各个 return 上：出口有四个，
            漏掉任何一个，那条路上主人的话就被吞了，而且【看不出任何异常】。"""
            if prepend is None or not len(prepend):
                return audio, why
            j = np.asarray(prepend, np.float32)
            if audio is not None and len(audio):
                j = np.concatenate((j, np.asarray(audio, np.float32)))
            log('   ★ 接上被打断时的预卷 %.2fs（主人这句话的开头）',
                len(prepend) / float(SR))
            return j, why

        while True:
            # ★★★★★ 让路给播报 —— 主人 2026-09-23 规格里那句"免唤醒期间 没在说话
            #   则说一句我刚刚学习了新能力"就落在这儿（见 `Announcer` 那一大段）。
            #   ★ 判据是 `speech_at is None`：**主人都还没开口**。他要是正说着，
            #     这里就不让路 —— 这一轮照常走完，播报在 `turn()` 返回后**衔接**
            #     （见 `session_loop`）。⇒「空闲」和「正说」两种情景共用这一个
            #     检查点，不需要第二处代码，也不可能互相打架。
            #   ★ 手里有预卷时也让路：那半句是主人被打断时说的话，`done()` 会把
            #     预卷拼回返回值 —— 那就不是"让路"了，是把他那半句当成一轮新话
            #     喂给模型（而它多半是半句，ASR 出来就是残的）。
            #   ★★ 这个循环本来就每 0.3 秒转一圈（`_drain` 的 timeout），所以
            #     **不需要**任何"叫醒"机制：播报线程只管填标志，这儿自己看得见。
            #   ★★ **安抚音和「我还听着」走的是同一个检查点**
            #     （`Announcer._maybe_comfort` 往同一个 `todo` 上挂）。
            #     它们同样只在"主人没开口"时才说 —— 而反过来，"主人一直在说话"
            #     恰恰就是这两句该闭嘴的条件（主人 2026-09-23：
            #     「有对话……那就回答就行 不用思考音了」）。
            #     ⇒ 一个判据同时管住两件事，不需要第二处代码。
            if (speech_at is None and not (prepend is not None and len(prepend))
                    and self.announcer is not None and self.announcer.has()):
                log('   📣 有话要主动说（播报 / 安抚 / 我还听着），主人没在说话 ⇒ 让路')
                self._gave_way = True
                return done(None, '让路播报')
            a = self._drain(timeout=0.3)
            if a is None:
                # ★ "等不到人说话"那道 6 秒的兜底【也必须在这条路上查】。
                #   原来它只写在"收到帧"那条路上 ⇒ 麦克风一死（一帧都不来）
                #   就永远走不到它，要干等满 LISTEN_MAX=15 秒才走，
                #   日志上只写"超时"，看不出是麦克风没了。
                #   ★ 这里必须用 done() 出去：手里可能还攥着预卷。
                if speech_at is None and time.time() - t0 > WAIT_SPEECH:
                    return done(None, '没等到人说话')
                if time.time() - t0 > LISTEN_MAX:
                    return done(np.concatenate(buf) if buf else None, '超时')
                continue
            buf.append(a)
            self.vad.accept_waveform(a)
            detected = self.vad.is_speech_detected()
            # ★ 2026-09-23：同一包同时喂给流式那条路（见 LiveAsr 的规矩②）。
            #   必须用【同一个 `a` 对象】、在 vad 之后立刻喂 —— 这样 cut() 那一刻
            #   识别器吃掉的样本逐样本等于 np.concatenate(buf)，也就是下面
            #   seg_audio() 切的那条流。「转出来的字不变」是这么保证的。
            self._live.feed(a, detected)
            if detected and speech_at is None:
                speech_at = time.time()
                self._speech_at = speech_at      # ★ 早跑闸门要读它（见 FILLER_EARLY）
                # ★★ 静默超时的账也在这里记（`SESSION_IDLE` 那一段）：
                #   "主人最后一次开口"= 这一轮人声的**起点**，不是"这轮结束的时刻"。
                #   ★ 放在这一处而不是 turn() 里：这里才是"VAD 认定有人说话"的
                #     唯一现场，且**早于** ASR —— 主人说了但没转出字，也算他开过口。
                #   ★ 咳嗽/磕碰也会走到这儿（非人声那关在 turn() 里）：那正是我们
                #     要的 —— 屋里有动静就说明主人在跟前，不该倒计时收工。
                #   ★★ 已知的一个偏差，**方向是有意选的**：我们自己那句话的尾音
                #     偶尔会漏进麦克风、把 VAD 碰响 ⇒ 这一戳被多刷一次，收工就会
                #     比 2 分钟晚几秒。宁可晚，不可早 —— 判早了的代价是"主人刚开口
                #     就被一句'我先撤了'截断"，那比多开一会儿会话糟得多。
                self._last_user_at = speech_at
                log('   👂 听见了，录…')
            while not self.vad.empty():
                seg = self.vad.front
                self.vad.pop()
                # ★ 用 VAD 切出来的干净片段（掐掉了首尾静音）。
                #   ★★★ 但【绝不读 seg.samples 的值】—— 那个属性会非确定性地返回
                #   未初始化内存（sherpa_onnx 1.13.8 的 bug，实测 8 次里坏 1 次、
                #   数值能到 8.5e+37）。改成拿 `seg.start` + 长度去切我们自己攒的流，
                #   实测 corr = 1.0000。详见 seg_audio() 的注释。
                return done(seg_audio(seg, np.concatenate(buf) if buf else None), '说完')
            now = time.time()
            if speech_at is None and now - t0 > WAIT_SPEECH:
                # ★ 手里有预卷时【不能返回 None】：主人那半句我们已经拿到了，
                #   VAD 没认出来是 VAD 的事，不能连他说过的话一起丢掉。
                if prepend is not None and len(prepend):
                    return done(None, '预卷里有人说话（VAD 没认出来）')
                return None, '没等到人说话'
            if now - t0 > LISTEN_MAX:
                return done(np.concatenate(buf) if buf else None, '超时')

    def _start_ident(self, force=False):
        """一段会话开始 ⇒ 造一个新的身份状态机。关着就一直是 None。

        ★ `force=True` 只给【离线自测】用（`--wav --who`）：那条路不碰麦克风、
          不改线上行为，是验这条链的唯一入口 —— 要是也被 `SPK_WHO` 挡着，
          就只能靠"打开线上开关"来验，那是本末倒置。

        ★ 为什么【一段会话一个】而不是全局一个：`cur`（这一段认下的人）、
          `asks`（这一段问过几次）、`await_name`（报名窗口）全都是**会话级**的。
          全局一个的话，"这个会话从此不再问"会变成"从此永远不再问"。
          **册子**才是跨会话的（每次读盘）。
        """
        self.ident = None
        if not (WHO_ON or force):
            return
        try:
            import spk_speaker as spk
            self.ident = spk.Identity()
            log('👤 认人开着 —— 册子里 %d 个人：%s',
                len(self.ident.book), '、'.join(self.ident.book) or '（空，第一轮静默建"主人"）')
        except Exception as e:                              # noqa: BLE001
            log('✗ 认人起不来（照常说，不影响）：%s: %s', type(e).__name__, e)
            self.ident = None

    def _observe_who(self, audio, pre):
        """听这一轮音频 → 造 `TurnCtx`。★ 绝不抛：认人这条链【不许拖挂嗓子】。

        ★ 交付物只有 ctx 一件事。**没有第二个出口** —— 待绑定的声纹放在
          `ctx.claim_voice` 里往下递（`dispatch` 那一头），所以它【活不过这一轮】，
          也就没有"上一轮的向量忘了清、绑到下一轮的 remember 上"这种可能。
          （计划里原本要一个 `self.claim` 再在每个 return 口清一遍 ——
           现在这条路由构造消掉了，比"记得清"稳。）
        """
        empty = skills.TurnCtx()
        if self.ident is None:
            return empty
        try:
            import spk_speaker as spk
            # ★★ 预卷非空 ⇒ 这一轮的 audio 是【两个人接在一起】的
            #   （上次被打断时主人那半句 + 这次的），embed 出来两个人都像也都不像。
            #   一律不判。打断之后接着说的多半还是同一个人，粘性正好是对的答案。
            off = '预卷混音' if (pre is not None and len(pre)) else ''
            obs = spk.observe(audio, self.ident.book, off=off)
            kind, nm, score, _margin, vec = spk.unpack(obs)
            state = self.ident.step(obs)
            # ★★ 判成生人时 `ctx.who` 必须是 None —— `Identity.who()` 是【粘性】的
            #   （`stranger` 不动 `cur`），照抄它就会把**上一个人的名字**交给这一轮：
            #   于是 `brief(who)` 把上一个人的私有记忆喂进一个陌生人的回合。
            #   主人拍板的边界是"只喂公共 + 本人"，生人这一轮公共就是全部。
            #   （粘性本身是对的 —— `unsure` 就该沿用上一次，那是"这次没听准"。）
            ctx = skills.TurnCtx(who=None if state == 'stranger' else self.ident.who(),
                                 state=state, score=score, ident=self.ident)
            # ★ 只有"我们正在等他报名"那一两轮，向量才跟着 ctx 往下走。
            #   其余时候它是 None —— 三个闸门里的第一道就守在这儿。
            if state == 'answering':
                ctx.claim_voice = vec
            if state != 'unknown':
                log('👤 %s（%s%s）→ %s', nm or '—', kind,
                    ' %.3f' % score if score else '', state)
            return ctx
        except Exception as e:                              # noqa: BLE001
            log('✗ 认人出错（照常说）：%s: %s', type(e).__name__, e)
            return empty

    def turn(self, session, waited=False):
        """录一句 → ASR → 脑子 → 说出来。返回 (控制信号, 说了什么)。

        ★ 这是从原来那个 cycle() 里剥出来的：剥掉的是"唤醒"那一半。
          唤醒只在一次会话的开头发生一次；后面每一句都走这里，不喊唤醒词。
          这就是免唤醒窗口的全部秘密 —— 没有别的东西，就是把 listen() 那一步
          从循环里拿掉。

        ★ `waited` 只影响日志：第一次没人说话值得记一笔，剩下 9 分钟里每 6 秒
          记一次会把日志淹掉（而"安静"本来就是连着聊模式的常态，不是异常）。
        """
        pre, self.pending = self.pending, None
        audio, why = self.record(pre)
        if audio is None or len(audio) < SR * 0.2:
            if not waited:
                log('   —— %s，继续听着', why)
            elif time.time() - self._idle_log > 30:
                # ★ 会话里没人说话是常态，但【一声不吭是错的】：日志全静的时候，
                #   "它在安静地候着"和"它已经死了"从外面看一模一样 ——
                #   2026-09-21 21:48 那次就是这么查了半天（最后靠 py-spy 抓栈才分清）。
                #   每 30 秒留一行心跳，代价是一行日志，换来"看得出它还活着"。
                self._idle_log = time.time()
                log('   （候着 —— %s；本段已开 %s）', why, session.age_str())
            return None
        log('   %s，%.2f 秒', why, len(audio) / SR)
        # ★★★★★ 非人声判据（咳嗽 / 磕碰 / 放杯子）—— 主人 2026-09-22：
        #   「非人声就别回复了 咳嗽也回思考音」。打分**无条件打印**（拿来校正门槛，
        #   别拿估的当实测）；它拦两件事：① 早跑那条思考音 ② 只转出一两个字的噪声轮。
        #   ★ 位置只能在【早跑之前】：早跑是梯子，一出门前就得判完。见 `speech_like()`。
        like = speech_like(audio)
        log('   🎚 人声判据：%.0f%%（阈 %.0f%%）', like * 100, SPEECH_LIKE * 100)
        # ★★★★★ 思考音【早跑】：VAD 一判"说完"就出门，不等 ASR（省 0.3~1.3 秒）。
        #   ★ 两道闸的理由、数字、以及怎么退回去，全写在 `FILLER_EARLY` 那段里。
        #   ★ 闸门① = 「这段人声起于我们还在播的时候」⇒ 当回灌，不早跑。
        #     老版是"念完 4 秒内一律不起跑"，实测把他每次追问都拖慢 1 秒 ⇒ 已作废
        #     （账都在 `EARLY_ECHO_MARGIN` 那段）。
        #   ★ 闸门②（2026-09-22 深夜）= 「不像人声」⇒ 不早跑。它不是"丢掉这一轮"，
        #     只是**不出声**：字真转出来了，下面那条迟到的 `_filler_go(adopt=early)`
        #     照样会推（adopt 拿到 None ⇒ 新起一条）—— 所以真人说话最多损失早跑那
        #     0.3~1.3 秒，**绝不会因为判错而听不到回答**。
        early = None
        if why == '说完' and FILLER_EARLY and like < SPEECH_LIKE:
            log('   🤫 不像人声（%.0f%%）⇒ 不出思考音，先看它转出字来没有', like * 100)
        elif why == '说完' and FILLER_EARLY and self._speech_at > 0 and \
                self._speech_at > self._said_at + EARLY_ECHO_MARGIN:
            early = _filler_go(first=EARLY_FIRST)
        elif why == '说完' and FILLER_EARLY:
            log('   💭 这段人声起于 %.2fs（我们念完那一刻之前）⇒ 当回灌，不早跑',
                self._speech_at - self._said_at)
        try:
            w = wave.open('/tmp/spk_ear_last.wav', 'wb')
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
            w.close()
        except Exception:
            pass

        t0 = time.time()
        # ★★★★★ 2026-09-23：流式那条路（定案见 LiveAsr 上面那一大段）。
        #   三条都满足才走它：
        #     · 开关开着、这条流没出过错（`lv.ok`）
        #     · `why == '说完'` —— 只有这条路上手里的音频是【完整一句】
        #     · ★【没有预卷】—— 有预卷时手里的 `audio` 是 `prepend + 这一句`，
        #       而流里只有"这一句"。用流式结果 = 把主人被打断时说的那半句开头丢掉，
        #       正是"你先听我说"只剩"说"那个老 bug（见 record() 的 done()）。
        #   ★★ 【唯一的保险丝】：流式转空就退回整段那条老路 —— 代价只在极少数失败轮
        #      上付，而正确性由一条已知能用的路兜底。
        lv = self._live
        use_live = (SPK_ASR_LIVE and why == '说完' and lv is not None and lv.ok
                    and (pre is None or not len(pre)))
        text = lv.cut() if use_live else ''
        if text:
            log('📝 ASR(%+.2fs 流)：%s', time.time() - t0, text)
        else:
            if use_live:
                log('   ⚠ 流式没转出字（%s）⇒ 退回整段那条老路', lv.why_not or '未知')
            t0 = time.time()
            text = asr_text(self.asr, audio)
            log('📝 ASR(%+.1fs 整)：%s', time.time() - t0, text or '（空）')
        if not text:
            # ★ 认人的观测放在这道门【之后】：ASR 都没转出字来，这一轮根本不会走到
            #   模型那儿，也就没有 Remember 可绑 —— 白算一次 embed（几百毫秒）不值当。
            _filler_drop(early)
            return None

        # ★★★★★ 非人声 + 幻觉字 ⇒ 当噪声，不回（主人：「非人声就别回复了」）。
        #   实测的正是这一种：一声咳嗽 ⇒ VAD"说完" ⇒ ASR 吐一个「爱」⇒ 回一句
        #   "嗯？没太听清，你是想说什么呀？"。两条判据必须**同时**成立才丢：
        #     · 周期性判据说它不像人声（咳嗽是宽带噪声）
        #     · 而且只转出 ≤NONSPEECH_CHARS 个字（ASR 对噪声的幻觉就这个长度）
        #   ⇒ 真人那句"嗯/好/停"是浊音，过不了第一条 ⇒ **确认流不会被误伤**。
        if like < SPEECH_LIKE and len(text) <= NONSPEECH_CHARS:
            _filler_drop(early)
            log('   🤫 不像人声（%.0f%%）+ 只转出 %d 个字「%s」⇒ 当噪声，不回',
                like * 100, len(text), text)
            return None

        # ★★★★★ 自听回灌闸门 —— 位置必须在这里，理由有二：
        #   ① 在 `_filler_go()` 【之前】：思考音也是【出声】的。判晚了，
        #      这一轮虽然丢了，思考音已经推出去了 —— 他照样听见一声莫名其妙的
        #      "唔……"，而这正是回灌最气人的形态（明明没跟它说话，它在回应自己）。
        #      ★★ 2026-09-22 深夜补一句（别再照旧理解）：早跑那条梯子**确实**
        #        在判据之前就出门了 —— 但它只在【回灌窗口之外】才起跑（见上面
        #        `FILLER_EARLY` 那段），而回灌只发生在刚念完的那几秒。两道闸是
        #        "窗口内不起跑" + "判到就当场叫停（`_filler_drop`）" —— 不是取消这道闸。
        #   ② 在 `brain()` 之前：回灌的文本要是真喂进去，大模型就会答它自己，
        #      还白白花掉一次调用。见 `_is_self_echo` 的 docstring。
        if self._is_self_echo(text):
            # ★ 早跑那条阶梯也要一起停 —— 它的第一声可能已经推出去了，但绝不许
            #   它接着往下推（见 `_filler_drop`）。
            _filler_drop(early)
            return None

        # ★★ 思考音：字转出来了，接下来是大模型 0.8 秒 + TTS 2.7~3.2 秒 + 推流 0.2 秒
        #   （2026-09-22 实测）—— ★ 里面最贵的已经是 TTS，不是大模型了，
        #   在他听来是一片死寂。先推一条"嗯……我想想"把这段盖住。
        #   ★ 位置只有这一个 —— 早了是插嘴，晚了就白做。见 FILLER_ON 上面那段。
        #   ★★ 2026-09-22 深夜：`adopt=early` —— 早跑那个还活着就**原地认领**，
        #     不重启（重启＝时钟归零，等于把早跑省下的那 0.3~1.3 秒又还回去）。
        fil = _filler_go(adopt=early)

        # ★★ 认人：判这一句是谁说的。位置就在【录完、ASR 完、进模型之前】——
        #   判定必须发生在注册之前，而注册只在轮末（模型调 remember 时）才可能发生。
        #   顺序反了的话，他这一轮就会以 1.0 的分数认出自己，stranger 分支消失、
        #   报名窗口永远关不上。
        #   ★ `pre` 传进去：预卷非空时观测一律 skip（混音）。
        ctx = self._observe_who(audio, pre)

        try:
            ans, ctl = brain(text, session=session, ctx=ctx)
        except Exception as e:
            log('✗ 大脑出错：%s: %s', type(e).__name__, e)
            ans, ctl = '我这边出了点问题，没想出来。', None
        self.speaking = True
        b = None
        try:
            # ★★ 回答推出去【之前】，必须先把思考音那道阶梯收干净。
            #   两条流推的是同一台设备，后 Play 的掐掉先 Play 的 —— 它要是拖到这会儿
            #   才发 Play，**回答会被掐成半句**（偶发、听过一遍才知道、极难查）。
            #   ★★★ 2026-09-22 **主人改口了**，这里跟着换了语义（别再照旧注释理解）：
            #     · 旧：「如果这时结果回来了 那也不要直接中断思考音 让思考音说完 然后再自然衔接」
            #     · 新：「我觉得思考词是可以被真回复打断的」「因为真回复加了衔接 就不会
            #       显得那么突兀」⇒ `ready` 一置位阶梯**当场**让路，**不等这条人声说完**。
            #   ⇒ 这个 join 正常路径下几乎瞬时，现在只兜"阶梯卡在 push 里出不来"。
            #   ★ 敢掐的**唯一理由**是下面那句 `ans = _bridge() + ans`：答案流的开头
            #     就是衔接词，它本身就是"接话"的信号。**所以那一句不是装饰，是这里的
            #     前提 —— 谁要是把衔接词去掉，这个 join 就必须改回等 `spoken_end`。**
            #   ★ 等不到（推流卡住了）就 set(stop) 硬中止 —— push() 在发 Play 之前
            #     会再看一眼，设了就一个字都不发。等不等得到都绝不会掐回答。
            # ★★★ 答案一到就【当场】把话说全（含衔接词）并开始合成 ——
            #   ★★ 关键是把【合成】和【推流】拆开：**等思考词说完才轮得到的是推流，
            #      不是合成。** 这两件事原来是串在一起的，白白丢掉 0.73 秒
            #      （实测 TTS 耗时），而那 0.73 秒本来正好是 join 在等思考词说完的空档。
            #      主人原话就是「回复到位直接拼衔接词 然后等思考词播完的一个时机推过去」。
            #   ⇒ 合成丢给后台线程（`_TtsJob`），join 一返回音频已经在手上，直接推。
            spoke = False
            pre = None
            if fil is not None:
                fil.ready.set()
                # ★★ 就在这一刻读 —— 不是 join 之后。`ready` 置位之后这个值就是终值，
                #   结构上不存在"读到 0、其实马上要推"的窗口（见 `_FillerRun.lock`）。
                spoke = fil.spoke()
                if spoke and ans:
                    # ★★ 衔接词（主人加的那一道）：思考音是"在想"、回答是"想好了"，
                    #   从前者直接跳到后者，听感上像话被截断。一句"哦，有了"交代这次转折。
                    #   ★ 只在他**真等过**的时候加 —— 答案秒回、一条思考音都没播出声时
                    #     来一句"哦，有了"反而莫名其妙（他根本没觉得在等）。
                    ans = _bridge() + ans
                if ans:
                    pre = _TtsJob(ans)          # ★ 后台合成，跟下面这个 join 并行
                fil.t.join(FILLER_JOIN)
                fil.stop.set()
                fil = None
            # ★★★★★ 记账：这一轮我们【念出去的字】—— 下一轮的"自听回灌"闸门
            #   （`_is_self_echo`）拿它当对照文本。位置必须在 speak() 之前：
            #   它要是半路抛了，尾音照样已经飘进麦克风了，账得先记上。
            self._last_said = ans or ''
            # ★ 把麦克风队列交给 speak()：打断的观察者要在"我正在说"那几秒里听。
            b = speak(ans, self.mic.q, pre=pre)
        finally:
            # ★★★★★ 回灌窗口的零点 = 【念完的那一刻】，不是开口那一刻。
            #   尾音是"念完之后"才飘回来的 —— 锚在这儿窗口才收得紧、不误杀他的跟话。
            self._said_at = time.time()
            # ★★★★★ 念完那一刻的锚点 = 【设备真的说完】那一刻，不是我们推完那一刻。
            #   主人原话：「我感觉他的回答 也当成用户说话了」—— 对，就是这么回事。
            #   在这之前这个锚是"我们推完那一刻"，而喇叭真正起播比推晚 0.35 秒；
            #   撞上 CC 的 ContinuePlay 要补发 0x601 那一轮，实测晚到 **4 秒**
            #   （21:26:18 推、21:26:22.6 才响、人声到 25.8）。
            #   锚错了以后，下游两道屏蔽**全部失效**（它们都以 `_said_at` 为基准）：
            #     · `_is_self_echo` 的年龄窗口 `now - _said_at` —— 锚早了 4 秒，
            #       我们自己的尾音算出来"已经是 4 秒前的事"⇒ 判成主人刚说的；
            #     · 早跑闸门 `_speech_at > _said_at + margin` —— 同样被绕过。
            #   `dlna.audible_end()` 给的是**设备那条时间线**上的"响完"（见它 docstring），
            #   把它当锚之后，`_last_said` 那道文字屏蔽**不用改一个字**就恢复了。
            end = 0.0
            try:
                end = dlna.audible_end()
            except Exception as e:                          # noqa: BLE001
                log('   （量不到"真响完"的时刻：%s: %s ⇒ 按老办法算）',
                    type(e).__name__, e)
            if end > 0:
                self._said_at = max(self._said_at, end)
            # ★★★★★ 铁律（主人 2026-09-22 立的）：**说话的时候不关麦克风。**
            #   人能打断 —— 耳朵必须一直听着。所以这里【只挪锚点，绝不闭麦】：
            #   只有下面那个 0.4 秒静置（躲房间余音）+ 清掉已经攒下的旧音频，
            #   没有"守着秒表一直不录"那回事。
            #   我们自己的声音靠两道**判据**挡（`_is_self_echo` 文字 + 早跑闸门），
            #   不靠把耳朵蒙上 —— 蒙上了就顺带把主人的插话也蒙掉了。
            time.sleep(0.4)           # 尾音还没散干净就开耳，会把自己后半句当成新语音
            self.speaking = False
            if b is not None and b.fired:
                # ★★ 被打断时【绝不能清队列】：那里面是主人正在【接着】说的话。
                #    前半句（预卷）已经在 Barge 手里，这半句清掉 —— 两半都丢，
                #    他就等于白说了一遍。让路的全部意义就在这半句上。
                log('   （被打断 —— 队列里是主人正接着说下去的，保留）')
            else:
                n = self._flush()
                if n:
                    log('   丢掉说话期间攒下的 %.1f 秒旧音频', n / float(SR))
        # ★★★ 被打断之后：停（dlna 已做）→ 让路 → 倾听。三件事，见 spk_barge 文件头。
        if b is not None and b.fired:
            import spk_barge
            # ---- ① 预卷先过闸门 ------------------------------------------
            # 那 1.55 秒是"我们正大声说 + 主人插话"的【混音】（相关 0.86 就是证据）。
            # 直接拼进下一轮，ASR 很可能转出【我们自己刚说的那句话】——
            # 那样脑子就会一本正经地回应它自己。宁可他重说一遍，也不自问自答。
            pre = b.preroll
            if pre is not None and len(pre):
                heard = asr_text(self.asr, np.asarray(pre, np.float32))
                echo, why2 = spk_barge.is_self_echo(heard, ans)
                log('   ✋ 被打断 —— 预卷 %.2fs 转出：%r（%s）',
                    b.preroll_secs, heard or '（空）', why2)
                if echo:
                    log('      ⇒ 是我们自己 ⇒ 丢掉预卷，不自问自答')
                    pre = None
                else:
                    log('      ⇒ 判成主人 ⇒ 留住，拼到下一轮开头')
            # ---- ② 等主人停顿，再播那句固定片段 --------------------------
            # ★ 顺序不能反：门是"只在没人说话时开口"。主人还在说就压着不发 ——
            #   而这句话【不可打断】（见下），先发出去就等于不可阻挡地压住他。
            try:
                got, frames, _st = spk_barge.wait_quiet_and_collect(self.mic.q)
            except Exception as e:                          # noqa: BLE001
                log('   ✗ 等停顿出错（这次不让路，也不丢音频）：%s: %s',
                    type(e).__name__, e)
                got, frames = False, []
            if got:
                try:
                    clip = spk_barge.yield_clip()
                    # ★★ mic_q=None ⇒ 不起观察者 ⇒ 这句【不可打断】。
                    #   它是"交还发言权"这个动作本身，是协议帧不是内容 ——
                    #   被截断的交还等于没交；而且新观察者会把一次误判
                    #   变成"嗯你说→被掐→嗯你说"的连环。
                    dlna.say(clip)
                    # ★ 躲开它自己的声音：片段播的这几秒麦克风里全是我们。
                    #   不丢掉就会被当成主人下一句的开头（唤醒应答那声"叮"同理）。
                    self._flush()
                    self._drop_until(time.time() + 0.4)
                    log('   ✔ 已让路（%r，%.2fs）', spk_barge.YIELD_TEXT,
                        spk_barge.clip_secs())
                except Exception as e:                      # noqa: BLE001
                    log('   ✗ 让路片段没播成（不影响接着听）：%s: %s',
                        type(e).__name__, e)
            # ---- ③ 主人这句话的两半拼起来，交给下一轮 --------------------
            # 前半句是预卷（他开口那 1.5 秒），后半句是等停顿期间收下的。
            # 只有两半都在，他说的才是一句完整的话。
            parts = [np.asarray(pre, np.float32)] if pre is not None and len(pre) else []
            parts += [f for f in frames if len(f)]
            if parts:
                self.pending = np.concatenate(parts) if len(parts) > 1 else parts[0]
                log('   ★ 攒下主人这句话 %.2fs，交给下一轮开头',
                    len(self.pending) / float(SR))
        # ★★★ 确定性兜底（主人 2026-09-24 要的，见 `HARD_STOP` 那一段）：
        #   模型漏调 `end_session` 时，只要主人这一整句就是收工词，照样收工。
        #   ★ 位置选在**这个唯一出口**上（turn() 里所有走完的路最后都汇到这儿）：
        #     那几条早退的 `return None` 都发生在模型之前 —— 它们本来就是"这一轮不算数"，
        #     不该由兜底来收工，也不该让它们跳过这里。
        #   ★ 只在 `ctl is None` 时兜：模型自己说了 `ask`（它还在等回答）就别抢。
        if ctl is None:
            hit = is_hard_stop(text)
            if hit:
                log('⏹ 确定性兜底：主人整句就是「%s」，而模型没调 end_session ⇒ 直接收工', hit)
                ctl = 'end'
        return ctl

    def session_loop(self):
        """唤醒一次 → 连着聊 → 收工。主人 2026-09-21 定的规矩：

          ① 10 分钟只是【最长】的窗口，不是定时器
          ② 模型觉得"这句没听懂 / 不像在跟我说话"→ 主动问"你是在跟我说话吗"
          ③ 主人明确说"没有 / 没事了"→ 立刻停止倾听
          ④ ★★★ 2026-09-23 **主人改了口**：**静默超时要做了** ——
             「静默超时还是要做 并且2分钟后一直没有用户回复 就恢复睡眠状态
               并说一句 我先撤了有事再找我」
             ⇒ 主人 2 分钟没开口（判据见 `_last_user_at` 那段）⇒ 说完 `SESSION_BYE`
               就收工回"等唤醒词"。★ 这不是"替主人决定"了，是他自己要的。
             ★ 改口之前这里写的是"**不做**静默超时：屋里没人说话时它就那么安静地
               开着，那是主人的选择" —— **那条已作废**，别再拿它当规矩。

        ★ 代价说清楚：这 10 分钟里麦克风一直开着，屋里所有声音都会送进模型
          （电视里的、别人聊天的）。主人认这个代价 —— 换来的是"不用每句都喊
          唤醒词"，以及那条 ask_user 出口。④ 那条超时是他给这个代价打的补丁。
        """
        log('🔔 唤醒：%s', self._hit)
        # ★★ TTS 常驻连接：**唤醒这一刻**在后台把它焐热 —— 而且必须【真合成一句】，
        #   不能只建连：连接空转 45 秒会被服务端掐，而**服务端掐掉的连接在本地
        #   看还是"活着"的**（`ws.closed` 是 False）⇒ 老版那个"已连上就返回"的
        #   预热对着死连接是个空操作。实测数字与那 4.9 秒死寂见 `spk_tts.WARM_REAL`。
        #   ★ 它要 ~3.9 秒（重连 1.56 + 首句 2.30），而接下来是"录音 + ASR"那 1~3 秒
        #     —— 正好把它藏进去，回答那句就只要 0.34 秒（净省约 2 秒）。
        tts.warm()
        self.speaking = True          # 这声"叮"（还有设备自己的应答）不该被我们自己听见
        # ★★★ 2026-09-21 夜：这里原来是 `_drop_until(now + WAKE_SKIP)` —— 无条件盲丢 1.0 秒。
        #   它就是主人三次「我问几点了 他没回答」的真凶（详见 WAKE_SKIP 常量那一段）。
        #   现在抄 wake_from_device【会话中途】那条已验证的口径：
        #   ① `_flush()` 只清队列里【已经攒着】的唤醒词尾音 —— 不设任何未来窗口；
        #   ② `_ack_until` 让 `_drain` 往后按【电平】扔：设备那声应答真响才丢，安静的一律放行。
        #   主人的话是安静的（0.06~0.11），设备的响（1.0 削顶），0.40 这条线把两者分得开。
        skipped = self._flush()
        self._ack_until = time.time() + ACK_SKIP
        self.speaking = False
        log('   唤醒后：清队列 %d 帧（唤醒词尾音）；往后 %.1fs 只扔 >%.2f 的响帧，安静的放行',
            skipped, ACK_SKIP, ACK_LVL)

        session = sess.Session()
        session.add('user', '（主人喊了唤醒词）')   # 让账本从 user 开头，模型看得懂
        # ★ 认人的状态机【一段会话一个】：`cur`（这一段认下的人）、`asks`（问过几次）、
        #   `await_name`（报名窗口）全是会话级的 —— 做成全局，"这个会话不再问"
        #   就变成了"从此永远不再问"。册子是跨会话的（每次读盘）。
        self._start_ident()
        first = True
        # ★ 2026-09-23：会话一开就把【保温】挂上 —— 见 spk_tts 里 `keepalive()` 那段定案。
        #   它补的正是上面那个 `warm()` 够不着的空档：`warm()` 一天只在【唤醒这一刻】
        #   跑一次，而会话里两轮之间动辄隔几十秒，服务端 45 秒就把连接掐了
        #   ⇒ 14 天里 58 次建连有 **30 次**是"合成中途断了被迫重连"。
        #   ★ 它一个字都不出声：合成的字节直接丢掉，不落盘、不碰 dlna、不碰设备。
        tts.keepalive(True)
        self.in_session = True
        # ★ 会话开始的那一刻 —— 「我还听着」的静默计时下界（见 `Ear.__init__` 那段）。
        #   必须在 `in_session = True` 【之后】紧接着填：播报线程 4 秒转一圈，
        #   中间留空档的话它会拿上一段会话的旧戳算出一个假的"安静很久"。
        self._session_open_at = time.time()
        # ★★ 静默超时的起算点 = **唤醒这一刻**：主人喊了唤醒词，就算他"刚开过口"，
        #   不然一唤醒就会立刻判超时（进程刚起时这个戳是 0 ⇒ 差值是个天文数字）。
        #   ★ 用唤醒时刻而不是"第一次真的听到人声"：主人喊完唤醒词可能要想 10 秒，
        #     那 10 秒当然算在 2 分钟里 —— 他确实在跟前。
        self._last_user_at = self._session_open_at
        # ★ 这两个也归零：它们都是"上一段会话留下的账"，留着虽然被 `_last_user_at`
        #   压住（唤醒那一刻总是最新的），但清零之后这段状态只有一个含义 ——
        #   "这一段会话还没播报过、还没因为手里有活而推迟过收工"，排查时不用猜。
        self._announced_at = 0.0
        self._held_for_help = False
        try:
            while True:
                ctl = self.turn(session, waited=not first)
                session.save()
                if ctl == 'end':
                    log('⏹ 收工（模型说完了），回到等唤醒词')
                    break
                if session.expired():
                    log('⏹ 收工（到 %s 上限了），回到等唤醒词', session.age_str())
                    break
                # ★★★ 播报（主人 2026-09-23 的规格）。位置是**算出来的**，别挪：
                #   · 上面两个 break 走掉时**不播** ⇒ 待播报留着，落到"睡眠"那一支
                #     去亮灯（`Announcer.run`）—— 主人正要走，说给他听也没人听。
                #   · 走到这儿 = 音箱**刚说完一轮**（或者刚为播报让了路，见
                #     `record()` 那个检查点）⇒ 正是"等话说完、然后衔接一下"的时机。
                self._announce_speak()
                # ★★★★ 静默超时（主人 2026-09-23 改口要的，见文件头 `SESSION_IDLE`）。
                #   ★ 位置是**算出来的**，别挪：
                #     · 摆在 `_announce_speak()` **之后** —— 已经攒着的播报/安抚先说完，
                #       再告别。反过来的话那两句就被这个 break 吞掉了（会话一关，
                #       `todo` 里剩下的要等下一次唤醒才有人念）。
                #     · 摆在两个 break **之下** —— 模型自己收工、或者到了 10 分钟上限，
                #       都有各自的话说了，不需要再补一句"我先撤了"。
                #     · 走到这儿 = 这一轮刚结束（`turn()` 返回了）。★ 主人正在说话
                #       中途的话根本走不到这儿（`record()` 阻塞在录音里）⇒ 这句告别
                #       **不可能打断他**，不需要额外的让路判据。
                if self._idle_over():
                    self._say_bye()
                    break
                if first:
                    log('   （连着聊：接下来不用再喊唤醒词，最长 %s）',
                        '%.0f 分钟' % (sess.MAX_SECS / 60))
                    first = False
        finally:
            # ★ 必须 finally：中途抛异常而把这个标志留在 True 的话，
            #   wake_from_device 从此再也不置 `_ext_wake` ⇒ **唤醒词永远叫不醒它**。
            self.in_session = False
            # ★ 会话戳一起清零（跟 `in_session` 同一个 finally、同样为真）：留着旧戳
            #   本身无害（`_maybe_comfort` 先看 `in_session`），但清掉之后这段状态
            #   就只剩一个含义 —— "没在会话"，排查时不用去分辨戳是哪一段留下的。
            self._session_open_at = 0.0
            # ★ 2026-09-23：会话结束就【关掉保温】—— 会话之外一律不发（见 spk_tts 那段定案）。
            #   与 `in_session` 同一个 finally，理由也一样：中途抛异常也必须合上。
            tts.keepalive(False)
        self.settle(session)

    def _help_busy(self):
        """手里还有"正在办"的求助吗 —— 有就不撤（主人 2026-09-23 选定）。

        ★ 判据**复用安抚音那一份 `COMFORT_LIVE`**，不新开第二张状态表：
          这个项目在"两份真话来源"上栽过好几次（部署脚本的对比表、`nightmute.saved`
          钉音量、两个 dnsmasq），状态表尤其不能有两份。
        ★ 刻意**不含 `proposed`**（方案出来了、等主人点头）：那时主人在**手机上**
          看审批卡片，音箱这边撤不撤对他没有影响。含进去的话，一条主人忘了点的
          求助能把会话钉满 10 分钟 —— 而这 10 分钟麦克风是开着的。
        ★ 读队列出错 ⇒ 当"没有在办"（允许撤）：为一次目录读失败把会话吊着，
          代价比撤掉一次会话大 —— 静默超时本来就是"该收工了"的兜底。
        """
        try:
            return any(d.get('kind') == 'capability'
                       and d.get('status') in COMFORT_LIVE
                       for d in helpq.all_open())
        except Exception as e:                              # noqa: BLE001
            log('✗ 查"在办的求助"出错（当没有在办）：%s: %s', type(e).__name__, e)
            return False

    def _idle_over(self):
        """主人是不是已经太久没开口了（静默超时，见文件头 `SESSION_IDLE`）。

        ★★★ 判据用 `_last_user_at`（**只有人声**才刷新），**不是**
          `max(_speech_at, _said_at)` —— 后者会被音箱自己的话刷新，而"我还听着"
          每 30 秒就说一句 ⇒ 超时永远不会到，等于自己给自己续命。
          （安抚音/「我还听着」那边用 `_said_at` 是对的：那边问的是"屋里还有没有
           动静"；这里问的是"**主人**还在不在"—— 两个问题，两个戳。）
        ★★ 两道"先不撤"的闸门（都是主人 2026-09-23 定的）：
          ① **有求助在办**（`_help_busy`）⇒ 一直等到出结果。主人在等答案，
             这时候撤了，那句"装好了"就只能落到睡眠期去亮绿灯了。
             ★ 天花板仍在：`sess.MAX_SECS` 10 分钟到了照样收工。
          ② **刚播报过结果**（`_announced_at`）⇒ 再给他 `SESSION_IDLE` 反应时间。
             没有这条会出现"……你试试。我先撤了，有事再找我"两句连着说 —— 因为
             播报的那一刻 `_last_user_at` 很可能早就过 2 分钟了。
             ★ 只有**播报**算（安抚音/「我还听着」**不算**）：那两句是"我还在"，
               不是"有结果了"，让它们续命的话超时就永远到不了（自己给自己续命，
               正是上面那个坑的另一种写法）。且播报条数有限 ⇒ 这道闸门有界。
        ★ `SPK_SESSION_IDLE=0`（或负数）⇒ 永远为假 = 回到 09-21 那个老行为。
        """
        if SESSION_IDLE <= 0:
            return False
        if self._help_busy():
            if not self._held_for_help:
                # ★ 只报一次（每 6 秒一轮，每轮一行会把日志淹掉）
                self._held_for_help = True
                log('⏳ 主人 %.0f 秒没开口，但手里还有活没交 ⇒ 先不撤，等出结果',
                    time.time() - (self._last_user_at or self._session_open_at))
            return False
        self._held_for_help = False
        idle = time.time() - max(self._last_user_at or self._session_open_at,
                                 self._announced_at)
        if idle < SESSION_IDLE:
            return False
        log('😴 主人 %.0f 秒没开口（上限 %.0f）⇒ 该收工了', idle, SESSION_IDLE)
        return True

    def _say_bye(self):
        """收工前的那句告别 ——「我先撤了，有事再找我」。

        ★ 走的就是播报那一套（`speak()` + `_anchor_after_say()`），所以：
          · 嗓音、音量、增益全不用管 —— 跟平时回答是**同一条链**
          · 夜间静音（`dlna` 底层 HP/PO 那道闸门）白拿：夜里它一个字都出不来，
            会话照样收工（该睡觉了，本来就不该出声）
        ★ 念不成了也照样收工：主人 2 分钟没在跟前，为一句告别把会话吊着没道理。
        """
        log('👋 告辞：%s', SESSION_BYE)
        self.speaking = True
        try:
            self._last_said = SESSION_BYE
            speak(SESSION_BYE)
        except Exception as e:                              # noqa: BLE001
            log('✗ 告别没念成（照样收工）：%s: %s', type(e).__name__, e)
        finally:
            self._anchor_after_say('告别')

    def _anchor_after_say(self, what='说话'):
        """说完一句之后的收尾 —— 播报和安抚共用这一份。

        ★★★ 为什么必须共用：`_said_at` 是自听回灌闸门和早跑闸门**共同的基准**
          （`turn()` 里那段注释写得很细，别只看这里）。锚错了以后两道闸门
          **一起失效** —— 音箱会把自己的话当成主人说话，然后一本正经地回自己。
          ⇒ 两处各抄一遍 = 两处会漂移；抄成一份最省心。
        ★ `turn()` 那段更长（还要处理"被打断时绝不清队列"等），**没有并进来** ——
          那是经过大量实测的成熟代码，不动它比"整齐"重要。
        """
        self._said_at = time.time()
        try:
            end = dlna.audible_end()
        except Exception:                                   # noqa: BLE001
            end = 0.0
        if end > 0:
            self._said_at = max(self._said_at, end)
        time.sleep(0.4)      # 尾音还没散干净就开耳，会把自己后半句当成新语音
        self.speaking = False
        n = self._flush()
        if n:
            log('   丢掉%s期间攒下的 %.1f 秒旧音频', what, n / float(SR))

    def _announce_speak(self):
        """把攒着的话念出去 —— **播报 / 安抚 / "我还听着"** 都走这儿。

        · 播报：`announce` 字段（Claude 装完时现写的人话）+ 话头
        · 安抚：`text` 字段（现成短语，自带话头），主人**等你办事**等得安静了 ⇒ 插一句
        · "我还听着"：同样走 `text`，主人**手上没事**、安静得久了 ⇒ 说一句让他知道
          ★★ 后两者判据完全相同（屋里静了多久），只是说的话不同，见 `_maybe_comfort`。
        ★ 两者**共用让路检查点**（`record()` 那个），因为"能不能开口"是同一个事实：
          主人没在说话。区别只在说什么、说多少。
        ★ 位置只此一处（`session_loop` 里 `turn()` 返回之后、两个 break 之下）。
          它同时覆盖两种情景，靠 `self._gave_way` 分：
            · `turn()` 正常返回 ⇒ 音箱刚说完一轮 ⇒ 顺着话头接（`join`）
            · `turn()` 是让路返回的（`record` 那个检查点）⇒ 主人没在说话
              ⇒ 音箱主动开口，得有个起头（`idle`）
          ★ 话头只管播报 —— 安抚音的短语自带来头（「我问一下我的助手」）。
        ★★ **不可打断**（`speak()` 不传 `mic_q`）：播报是一句"告知"不是对话，
          而且短（一句话三秒）。主人真要插话，下一轮 `record()` 会收到。
          这与 `turn()` 里那句"让路片段不可打断"是同一个理由 —— 被截断的
          "告知"等于没告知。安抚音同理，而且它更没必要被打断：它本来就是
          "你忙你的，我盯着"的意思。
        """
        an = self.announcer
        if an is None:
            return
        todo = an.take()
        if not todo:
            return
        lead = ANNOUNCE_LEAD['idle'] if self._gave_way else ANNOUNCE_LEAD['join']
        self._gave_way = False
        for d in todo:
            if d.get('kind') == 'comfort':
                # ---- 安抚音 / "我还听着"：主人在安静地等，说一句"我盯着呢" ----
                #   ★ 记账**不落条目文件**（进度在 `Announcer.played` 里）——
                #     这两句只有音箱这条线播，往两条线共用的条目文件上写会污染
                #     另一个壳那边的账（那个壳有自己的 `told_phone_at`）。
                #   ★ 两者都走这条路（都是现成短语、都自带话头、都不记账），
                #     区别只在 `text` 说了什么、`id` 是 qid 还是 `IDLE_KEY`。
                line = (d.get('text') or '').strip()
                if not line:
                    continue
                log('💬 %s：%s',
                    '我还听着' if d.get('id') == IDLE_KEY else '安抚',
                    line)
                self.speaking = True
                try:
                    self._last_said = line
                    speak(line)
                except Exception as e:                      # noqa: BLE001
                    log('✗ 安抚没念成：%s: %s', type(e).__name__, e)
                finally:
                    self._anchor_after_say('安抚')
                continue
            body = (d.get('announce') or '').strip()
            if not body:
                # ★ 没有"念给人听的那一句" ⇒ 宁可不念（`finish(announce=…)` 的规矩：
                #   `result` 里混着技术诊断，念出来主人只会发懵）。照样记账 ——
                #   不然每轮都重试同一件说不出口的事。
                log('   （%s 没有可念的话，跳过）', d.get('id'))
                helpq.mark_told(d.get('id'), 'speaker')
                continue
            line = lead + body
            log('📣 播报（%s）：%s', d.get('id'), line)
            self.speaking = True
            try:
                self._last_said = line
                speak(line)
            except Exception as e:                          # noqa: BLE001
                log('✗ 播报没念成：%s: %s', type(e).__name__, e)
            finally:
                self._anchor_after_say('播报')
            helpq.mark_told(d.get('id'), 'speaker')
            # ★★ 播报过结果 ⇒ 给静默超时一道缓冲（见 `_idle_over` 的闸门②）。
            #   ★ 只在**播报**这条路上填：安抚音/「我还听着」填了的话，超时就被
            #     它们无限续命了（"自己给自己续命"那个坑的另一种写法）。
            #   ★ 摆在 `mark_told` 后面（念没念成都算数）：不然后面那两行一抛异常，
            #     缓冲就白设了。
            self._announced_at = time.time()

    def settle(self, session):
        """一次会话收工 → 固化记忆（归档原文 + 让模型挑重要的留下）。

        ★ 走 spk_memory 的后台线程，这里绝不阻塞：主人刚说完"没事了"，
          他要的是音箱【闭嘴】，不是等我们写完日记。固化慢一秒都不该让他等着。
        ★ 再包一层 try：记忆是"下次更好"，说话是"这次要命"，不能让前者掀翻后者。
        """
        try:
            mem.settle(session)
        except Exception as e:                     # noqa: BLE001
            log('✗ 固化没跑起来（不影响下次说话）：%s: %s', type(e).__name__, e)

    def cycle(self):
        """一次完整的"唤醒→回答"。★ 保留它只为自测/单句场景；
        主循环走 session_loop()（它才是主人要的那个"唤醒一次连着聊"）。"""
        hit = self.listen()
        log('🔔 唤醒：%s', hit)
        tts.warm()                                    # ★ 同 session_loop：唤醒就在后台把连接备好
        self.speaking = True
        skipped = self._flush()                       # ★ 同 session_loop：不盲丢，只清尾音
        self._ack_until = time.time() + ACK_SKIP
        log('   唤醒后：清队列 %d 帧（唤醒词尾音）；往后 %.1fs 只扔 >%.2f 的响帧，安静的放行',
            skipped, ACK_SKIP, ACK_LVL)
        self.speaking = False
        # ★ 自测这一路也要起认人 —— 否则 `--wav` + `SPK_WHO=1` 永远验不到这条链
        #   （ident 是 None ⇒ `_observe_who` 直接交空 ctx，看起来"跑通了"，其实一步没走）。
        self._start_ident()
        return self.turn(sess.Session()) is not None

    def run(self):
        log('👂 耳朵挂上 UDP %d（转发 → %s），等"嘀嗒嘀嗒"…', self.mic.port, self.mic.relay)
        while True:
            try:
                self._hit = self.listen()
                self.session_loop()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                log('✗ 这一轮崩了：%s: %s', type(e).__name__, e)
                traceback.print_exc()
                time.sleep(1)


# ---------------------------------------------------------------- 自测
def read_wav16k(path):
    w = wave.open(path)
    a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    if w.getnchannels() == 2:
        a = a.reshape(-1, 2).mean(axis=1)
    if w.getframerate() != SR:
        m = int(len(a) * SR / w.getframerate())
        a = np.interp(np.linspace(0, len(a) - 1, m), np.arange(len(a)), a).astype(np.float32)
    return np.ascontiguousarray(a, np.float32)


def selftest_wav(path, speak_it, who=False):
    """把 wav 整条走一遍 KWS→VAD→ASR→[认人]→大脑。用来验"听→懂"，不用人开口。

    ★ `who=True` 时顺便把"这一句是谁说的"也走一遍（`_observe_who` 那条链），
      并把判定打进日志。**这是离线验认人的唯一入口** —— 不用麦克风、不用出声。
      `SPK_VP_MODEL=/nonexistent` 再跑一次，就能验"没有声纹时照样答出来"。
    """
    kws, asr = make_kws(), make_asr()
    a = read_wav16k(path)
    s = kws.create_stream()
    hit_at, hit = None, None
    for i in range(0, len(a), CHUNK):
        s.accept_waveform(SR, a[i:i + CHUNK])
        while kws.is_ready(s):
            kws.decode_stream(s)
        # ★ 只能取一次：这个版本的 get_result 调第二遍就空了（所以先存下来再打日志）
        hit = kws.get_result(s)
        if hit:
            hit_at = i
            break
    log('KWS 命中：%s（第 %.2f 秒）', hit or '——', (hit_at or 0) / SR)
    if hit_at is None:
        return 1
    text = asr_text(asr, a[hit_at:])
    log('ASR：%s', text)
    ctx = None
    if who:
        # ★ 只要 `_observe_who` 这一个零件，所以拿 __new__ 造一只空耳朵
        #   （照 /tmp/test_who_turn.py 的样）—— 不碰麦克风、不碰唤醒词。
        bare = Ear.__new__(Ear)
        bare.ident = None
        bare._start_ident(force=True)          # ★ 离线自测：不受 SPK_WHO 开关限制
        ctx = bare._observe_who(a[hit_at:], None)
        log('认人：state=%s who=%r score=%.3f（册子 %d 个人）',
            ctx.state, ctx.who, ctx.score, len(bare.ident.book) if bare.ident else 0)
        log('   提示词里那段：%s',
            (skills._who_note(ctx) or '（一个字都不加）').strip().replace('\n', ' ⏎ '))
    if text:
        # ★ 这里原本把 brain() 的返回值当字符串用 —— 它从 2026-09-21 起是
        #   `(说了什么, 会话控制)` 的二元组，于是 `--wav`/`--text` 不带 --dry 时
        #   会把一个元组丢给 speak()。自测路也要跟着契约走。
        ans, _ctl = brain(text, dry=not speak_it, ctx=ctx)
        if speak_it:
            speak(ans)
        else:
            log('（--dry，不念）会是：%s', ans)
    return 0


def selftest_text(text, speak_it):
    ans, _ctl = brain(text, dry=not speak_it)
    if speak_it:
        speak(ans)
    else:
        log('（--dry，不念）会是：%s', ans)
    return 0


def selftest_pipe(wake_wav, text, dry):
    """整条链，但"用户"是喇叭自己演的：先播唤醒词，再播命令，让音箱的麦克风听进去。
    ★ 这是唯一能全自动跑通的端到端测法 —— 不用人开口，也不用人帮忙。"""
    import subprocess
    tmp = '/tmp/spk_ear_pipe'
    os.makedirs(tmp, exist_ok=True)
    # DLNA 只吃 mp3，所以先合成：唤醒词直接借 kwtest 的 mp3，命令现合
    wake_mp3 = wake_wav.replace('.wav', '.mp3')
    cmd_mp3 = os.path.join(tmp, 'cmd.mp3')
    if not dry:
        # ★★★ 命令句是**借音箱的嗓子演"用户"** —— 用户的声音**【不该】被音箱的
        #   软件增益削**。`tts()` 内部走 `_voice.chain()`，而 `chain()` 是给"音箱自己
        #   说话"用的（那是它的嗓子，该受 `.spk_gain` 管，现役 -28dB）。
        #   不盖这一下，两段素材差 **22.6dB**：唤醒词是原始 mp3（-22.7/-5.5），
        #   命令句过了 chain 只剩 -45.3/-30.0 ⇒ 在房间里等于耳语 ⇒ 只录到
        #   `rms 0.00808`（要 ×24.8 才够 ASR 门限）⇒ **ASR 吐幻觉**
        #   （实测「没有音乐可以推送」，应为"现在几点了"）⇒ 表面像"ASR 坏了/
        #   链路坏了"，其实只是测试素材电平不匹配。
        #   ⇒ 2026-09-22 记下的待办，2026-09-24 补上（同一个函数，两种身份）。
        _old_ln = os.environ.get('SPK_LOUDNORM')
        os.environ['SPK_LOUDNORM'] = 'anull'
        try:
            dlna.tts(text, out=cmd_mp3)
        finally:
            if _old_ln is None:
                os.environ.pop('SPK_LOUDNORM', None)
            else:
                os.environ['SPK_LOUDNORM'] = _old_ln
    else:
        subprocess.run(['edge-tts', '--text', text, '--write-media', cmd_mp3],
                       check=True, capture_output=True)
    # 唤醒词和命令之间留一口气：设备那声"叮"要从中间挤进来
    med = os.path.join(tmp, 'gap.mp3')
    subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono',
                    '-t', '2.0', '-b:a', '128k', med], check=True, capture_output=True)
    cat = os.path.join(tmp, 'all.mp3')
    with open(cat, 'wb') as out:
        for f in (wake_mp3, med, cmd_mp3):
            out.write(open(f, 'rb').read())
    log('♪ 从喇叭里播：%s + 2 秒静音 + "%s"', os.path.basename(wake_mp3), text)
    log('   现在请让 spk_ear 在另一个终端里跑着（它能听见这段），本命令只负责"说话"')
    return dlna.say(cat)


def main():
    ap = argparse.ArgumentParser(description='音箱的耳朵和脑子')
    ap.add_argument('--wav', help='拿一个 wav 走 听→懂（不用麦克风）')
    ap.add_argument('--text', help='拿一句话走 懂→说')
    ap.add_argument('--pipe', help='唤醒词wav:命令文本 —— 用喇叭演"用户"，端到端自测')
    ap.add_argument('--dry', action='store_true', help='只算不念')
    ap.add_argument('--no-guard', action='store_true', help='自己说话时也开着耳朵')
    ap.add_argument('--who', action='store_true',
                    help='--wav 时顺便走一遍认人（离线验认人的唯一入口）')
    args = ap.parse_args()

    k = agent._key()
    if not k:
        log('✗ 没找到大模型 key（DS_KEY / spk.key / settings.json 三处都没有）')
        return 1
    os.environ.setdefault('DS_KEY', k)

    if args.text:
        return selftest_text(args.text, not args.dry)
    if args.wav:
        return selftest_wav(args.wav, not args.dry, who=args.who)
    if args.pipe:
        wav, _, text = args.pipe.partition(':')
        return 0 if selftest_pipe(wav, text or '现在几点了', args.dry) else 1

    ear = Ear(guard=not args.no_guard)
    if not ear.mic.start():
        return 1
    ear.load()
    # ★ 求助结果的播报线程（见 `Announcer`）。★ 只在这条**常驻**路上起 ——
    #   上面三条自测路（`--text` / `--wav` / `--pipe`）在 `Ear()` 之前就 return 了，
    #   所以离线自测**永远不会**碰队列、不会亮灯、不会出声。
    ear.announcer = Announcer(ear)
    ear.announcer.start()
    try:
        ear.run()
    except KeyboardInterrupt:
        log('收工')
    finally:
        ear.announcer.stop.set()
        ear.mic.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
