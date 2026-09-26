#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音箱流式 TTS 端点 —— 设备**边下边播**，回答首声从 ~3.8s 压到 ~0.6s。

## 为什么要有它（2026-09-23）

主人问过：另一条线上已经很流畅了，音箱这条链路和那边一样吗、延时差多少？——
**大脑/提示词/工具/记忆全同一份，差异只在"声音怎么出来"这一截**。实测账：

    音箱（现役）：整段合成 短句 0.83s / 长句 3.41s ＋ 推→出声 0.35s ⇒ **首声 1.18 / 3.76s**
    另一条线（已流式）：首包 **0.38s**
⇒ **音箱慢的主因是 TTS 不是流式**，跟"路"没关系。

## 凭什么可行（静音轨实验，2026-09-23，屋里无声）

家里有人时唯一合法的验法（memory「静音轨实验法」）：吐**数字静音**，设备走完整
流程而屋里听不见。`tcpdump tcp port 8897` 抓到的（素材 60s，我们按 16000 B/s 实时吐）：

    811.76 设备主动连 → GET；811.82 开始吐 800B/50ms
    859.83 **48 秒后连接依然活着**，已吐 768939 字节 ＝ 48.1 秒音频
            ★ 设备接收窗口全程只有 229~661 字节（不急着要数据＝缓冲区满着＝在实时播）

★★★ 结论：设备**接受"无 Content-Length、靠关连接结束"的响应**，而且**边下边播**，
固件那个 Next 判据（`duration > 15000 && (duration − curPos) > 15000`）**没掐它**。
⇒ **不必预先算 Content-Length 上界**，方案因此简单一大截。

## 形态

    spk-ear ──POST /new {text}──> 本服务
                                   ├─ 起 qwen WS 流式（首包 0.28s，4~6× 实时）
                                   ├─ 边收 PCM 边喂 ffmpeg → mp3（**过 voice.chain()**）
                                   ├─ ★ 等首包到手才回响应
                                   └─ 回 {sid, url}
    spk-ear ── spk_ctl.play(url) ──> 设备
    设备 ──GET /tts/<sid>──> 本服务 ── 边吐边等 ──> 人声吐完 ＋ 45s 静音垫 ──> 关连接

★ **为什么 POST 要等首包**：等到了才推 URL ⇒ 流是活的；**首包失败 ⇒ 一个字都没推
  出去**，调用方原地退回整段合成（`spk_ai_dlna.tts`），主人听不出差别。
  铁律「**快路不许成为唯一的路**」。
★ **为什么必须过 `voice.chain()`**：思考音和回答走同一条响度线是这个项目的老规矩
  （没走的那次差 25dB）。chain() 是**纯滤镜串**且每次现读 `.spk_gain`
  ⇒ 直接当 ffmpeg 的 `-af`，音量旋钮照旧即时生效。
★ **为什么要垫 45 秒静音**：`_stage601` 的同一条道理 —— 不补长，固件的 Next 判据
  会把回答拦腰切掉。静音垫**立刻全吐**（模拟现役那条路的"完整文件被全速下载"），
  好让设备尽快把 duration 算长。
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cfg import HOST, PORT_TTS                                   # noqa: E402
import spk_tts_qwen as q                                          # noqa: E402
import spk_tts_qwen_stream as qs                                  # noqa: E402
import spk_voice as voice                                         # noqa: E402

OUR_IP = os.environ.get('SPK_TTS_STREAM_IP', HOST)
PORT = int(os.environ.get('SPK_TTS_STREAM_PORT', PORT_TTS))
# ★ 静音垫时长：跟 `spk_ai_dlna.PAD_SECS` 同一个数（改一处要一起改）。
PAD_SECS = float(os.environ.get('SPK_PAD_SECS', '45.0'))
PAD_MP3 = '/tmp/spk_tts_pad.mp3'
TTL = float(os.environ.get('SPK_TTS_STREAM_TTL', '300'))     # 流跑完留多久（供重连/查账）
FIRST_WAIT = float(os.environ.get('SPK_TTS_STREAM_FIRST', '1.5'))   # 等首包的上限
SRC_SR = int(os.environ.get('SPK_TTS_STREAM_SRC_SR', '24000'))      # qwen 给的 PCM

STREAMS = {}
_lock = threading.Lock()


def log(fmt, *a):
    sys.stderr.write('[ttsstream %s] %s\n'
                     % (time.strftime('%H:%M:%S'), fmt % a if a else fmt))
    sys.stderr.flush()


class Stream:
    """一条正在生成的回答。生成线程写 `buf`，HTTP 端读 `buf`。"""

    def __init__(self, text, instructions):
        self.sid = uuid.uuid4().hex[:12]
        self.text = text
        self.instructions = instructions
        self.buf = bytearray()
        self.pcm_bytes = 0            # 人声 PCM 累计（算"人声多长"用）
        self.done = False             # 人声生成完（含 ffmpeg 收尾）
        self.err = ''
        self.fferr = b''
        self.cond = threading.Condition()
        self.t0 = time.time()
        self.first = None             # 首块 mp3 到手的秒数（相对 t0）
        self.first_evt = threading.Event()
        self.voice_secs = 0.0         # 人声时长（秒），生成完才有意义
        # ★★ 设备来取流了没有 —— 给 `spk_ai_dlna` 当"链路走通了"的**硬证据**。
        #   现役那条路靠数 mp3srv(8899) 的取流日志（`fetched_count`），而我们走 8896,
        #   它数不到 ⇒ 必须自己报。`_serve()` **一进门**就置位（跟"它真来拉了"同一刻）。
        self.pulled = False
        self.pull_at = None
        # ★★ 落盘一份【人声 + 静音垫】，跟 `spk_ai_dlna._stage601` 的产物**完全同形**
        #   ⇒ 那边"已在 STAGE_DIR 且够长就原样返回"的早退判据会命中：`_stage601`
        #   一个字节不动，而它顺手填了 `_PLAY['secs']` ⇒ 回声闸门（`audible_end()`）
        #   的记账跟老路逐字一致，不必另写一份 —— 那条闸门出过真事故，最不该碰。
        #   ★ 先写 `.part`，拼完垫再 `os.rename`：**rename 是原子的** ⇒
        #     调用方"看见 `.mp3` 就是齐了"，不需要额外的完成信号/接口。
        self.part = '/tmp/spk_tts_%s.part' % self.sid
        self.mp3_path = None
        self._fh = None

    # ---- 给 HTTP 端用
    def info(self):
        return {'sid': self.sid, 'chars': len(self.text), 'done': self.done,
                'err': self.err, 'mp3_bytes': len(self.buf),
                'voice_secs': round(self.voice_secs, 3),
                'first': None if self.first is None else round(self.first, 3),
                'pulled': self.pulled,
                'pull_at': None if self.pull_at is None else round(self.pull_at, 3),
                'mp3_path': self.mp3_path,
                'age': round(time.time() - self.t0, 2)}


def _ffmpeg_cmd():
    """PCM(s16le/24k/mono) → mp3(48k/128k)，**过 voice.chain()**。

    ★ `voice.SR/CH/BR` 是输出格式的唯一定义处（`spk_voice.py`），别在这儿写死。
    ★ `-write_xing 0`：管道输出本来就写不了 Xing，显式写上免得哪天输出变成可 seek 的
      文件时悄悄多出一个"新文件头"——那会让设备把它当成另一段音频。
    """
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 's16le', '-ar', str(SRC_SR), '-ac', '1', '-i', 'pipe:0',
            '-af', voice.chain(),
            '-ar', voice.SR, '-ac', voice.CH,
            '-c:a', 'libmp3lame', '-b:a', voice.BR,
            '-write_xing', '0',
            # ★★ `-flush_packets 1` 是首包快慢的关键：muxer 默认会在自己的
            #    AVIO 缓冲里攒一段才吐。首版没写它，实测首包 0.965s；
            #    写管道本来就该边编边吐，攒着毫无意义、只让我们白等。
            '-flush_packets', '1', '-f', 'mp3', 'pipe:1']


def _finish_file(st):
    """把 `.part`（人声）拼上静音垫、原子改名成 `.mp3`；回最终路径，失败回 None。

    ★ 为什么垫必须拼进**文件**（而不是只垫在 HTTP 流里）：`spk_ai_dlna._stage601`
      的早退判据是 `end + PAD_SECS <= total` —— 落盘这份得**自己就够长**，
      那条判据才命中、才不会拿这份文件去重新补垫。
      （补垫本身不慢，但补出来的东西跟设备**正在播的那条流**不是同一份，
        账就对不上了；而且白转一次。）
    ★ 垫的素材是 45.0 秒（跟 `PAD_SECS` 同），实测 ffmpeg 编出来 45.06 秒
      ⇒ `end + 45.0 <= total` 有 0.06 秒余量，不卡在浮点边界上（已实测命中）。
    """
    final = '/tmp/spk_tts_%s.mp3' % st.sid
    try:
        with open(st.part, 'rb') as a, open(final, 'wb') as o:
            shutil.copyfileobj(a, o)
            if os.path.exists(PAD_MP3):
                with open(PAD_MP3, 'rb') as p:
                    shutil.copyfileobj(p, o)
        os.unlink(st.part)
        return final
    except OSError as e:                                        # noqa: BLE001
        log('sid=%s 拼静音垫没成（出声不受影响，只是自听判据要退回整段合成）：%s',
            st.sid, e)
        try:
            os.unlink(st.part)
        except OSError:
            pass
        return None


def _gen(st):
    """生成线程：qwen 流式 → PCM → ffmpeg → mp3 → `st.buf`（同时落盘一份）。"""
    ff = None
    fh = None
    try:
        # ★ 落盘是【顺带】的：开不了也照样出声（播放走内存里的 `st.buf`），
        #   只影响后面"自听判据"那一步 ⇒ 失败只记日志，绝不中断生成。
        try:
            fh = open(st.part, 'wb')
            st._fh = fh
        except OSError as e:                                    # noqa: BLE001
            log('sid=%s 落盘开不了（只影响自听判据，不影响出声）：%s', st.sid, e)
        try:
            ff = subprocess.Popen(_ffmpeg_cmd(), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:                                  # noqa: BLE001
            with st.cond:
                st.err = 'ffmpeg 起不来：%s' % e
                st.done = True
                st.cond.notify_all()
            return

        def _drain_out():
            while True:
                # ★★ `read1` 而**不是** `read`：`read(n)` 会一直等到读满 n 字节才
                #    返回（管道上就是"攒够 4096 字节 = 0.256 秒音频"），首包因此
                #    白白晚 0.26 秒 —— 流式的全部意义就是**别攒**。
                b = ff.stdout.read1(4096)
                if not b:
                    break
                with st.cond:
                    st.buf.extend(b)
                    if st.first is None:
                        st.first = time.time() - st.t0
                        st.first_evt.set()
                    st.cond.notify_all()
                # ★ 落盘写在 `cond` 外面：那头是磁盘 IO，别占着锁把 HTTP 端堵住。
                if fh is not None:
                    try:
                        fh.write(b)
                    except OSError:
                        pass

        def _drain_err():
            # ★ 必须排空：stderr 满了会把 ffmpeg 卡死（然后 stdin 写阻塞、整条流僵住）
            try:
                st.fferr = ff.stderr.read(4000)
            except Exception:                                   # noqa: BLE001
                pass

        th_out = threading.Thread(target=_drain_out, daemon=True)
        th_err = threading.Thread(target=_drain_err, daemon=True)
        th_out.start()
        th_err.start()

        def on_pcm(chunk):
            # ★ 同步回调（在事件循环里）：mp3 编码远快于实时，不会把它卡住。
            st.pcm_bytes += len(chunk)
            try:
                ff.stdin.write(chunk)
            except (BrokenPipeError, ValueError):
                raise                                   # 让 qs.stream 收尾，别静默吞

        r = asyncio.run(qs.stream(st.text, on_pcm,
                                  instructions=st.instructions, sample_rate=SRC_SR))
        if not r.get('ok'):
            with st.cond:
                st.err = r.get('err') or '流式合成失败'
        elif r.get('trunc'):
            with st.cond:
                st.err = '中途断了（半句）'
        try:
            ff.stdin.close()
        except Exception:                                       # noqa: BLE001
            pass
        try:
            ff.wait(timeout=8)
        except subprocess.TimeoutExpired:
            ff.kill()
        th_out.join(timeout=5)
        th_err.join(timeout=2)
        # ★ 人声时长：PCM 是 16bit 单声道 ⇒ 字节 / (采样率 × 2)
        st.voice_secs = st.pcm_bytes / float(SRC_SR * 2)
        # ---- 落盘收尾：拼静音垫 → 原子改名。任何一步失败都不影响出声。
        if fh is not None:
            fh.close()
            st._fh = None
            st.mp3_path = _finish_file(st)
    except Exception as e:                                      # noqa: BLE001
        with st.cond:
            st.err = st.err or ('%s: %s' % (type(e).__name__, e))
    finally:
        # 异常路径下的兜底关文件（正常路径在收尾那步已关）—— 不关就是 fd 泄漏，
        # 这个服务是常驻的，攒够了会 EMFILE。
        if fh is not None and not fh.closed:
            try:
                fh.close()
            except OSError:
                pass
            # ★ 走到这儿还没产出 `.mp3` ⇒ 这条是半截的（合成失败/被截断），
            #   留着只会被人当成"一份完整的落盘" ⇒ 当场清掉，让调用方去走整段合成。
            if st.mp3_path is None:
                try:
                    os.unlink(st.part)
                except OSError:
                    pass
        with st.cond:
            st.done = True
            if st.fferr:
                log('sid=%s ffmpeg: %s', st.sid, st.fferr.decode('utf-8', 'replace')[:200])
            st.cond.notify_all()
        log('sid=%s 生成完 —— 人声 %.2fs / mp3 %d 字节 / 首包 %s / %s'
            % (st.sid, st.voice_secs, len(st.buf),
               'None' if st.first is None else '%.2fs' % st.first,
               st.err or 'ok'))


def start(text, instructions=None):
    """开一条流，**阻塞到首包到手**（≤FIRST_WAIT 秒）。

    回 `(sid, url, first)`；失败回 `(None, None, 原因)` —— 调用方据此**退回整段合成**。

    ★★★★★ **这个函数只有【服务进程自己】能调**（`do_POST` 里那处）。
      别的进程 import 本模块再调它，流会开在**它自己那份 `STREAMS`** 里，
      而设备敲的是服务进程的 8896 ⇒ 必然 404。外部调用一律走 `start_remote()`。
      真事故与判据见 `start_remote()` 上面那段注释。
    """
    if not text or not text.strip():
        return None, None, '空文本'
    if not qs.available():
        return None, None, '没配 key'
    st = Stream(text, q.INSTR_DEFAULT if instructions is None else instructions)
    with _lock:
        STREAMS[st.sid] = st
    threading.Thread(target=_gen, args=(st,), daemon=True).start()
    if not st.first_evt.wait(FIRST_WAIT):
        with st.cond:
            why = st.err or ('%.1fs 没等到首包' % FIRST_WAIT)
        return None, None, why
    return st.sid, url_of(st.sid), st.first


def url_of(sid):
    return 'http://%s:%d/tts/%s' % (OUR_IP, PORT, sid)


# --------------------------------------------------------------- 跨进程调用口
# ★★★★★ 真事故（2026-09-23）：`spk_ear.speak()` 曾经写成
#     `import spk_tts_stream as ts` + `ts.start(text)` —— 那是**在 spk_ear 自己的
#     进程里**开流，流进了**它自己那份 `STREAMS`**；而设备拿到 URL 后敲的是
#     `192.168.1.100:8896`，那是【systemd 那个服务进程】 ⇒ 它那份 `STREAMS` 是空的
#     ⇒ 每次都是 `GET /tts/<sid> —— 没有这条流` ⇒ **404 把设备打发走**。
#   ⇒ 两个进程两份表，**进程内 `start()` 必然取不到**。
#   ★ 当时的判据（两条一起看，都是累计型）：
#       journalctl -u spk-ear      | grep -c 设备来取流        # = 0（从没成功过）
#       journalctl -u spk-tts-stream | grep -c 没有这条流      # 一路涨
#     ★ 而且它**看起来像"设备断网"**：spk_ear 报「✗ 它没来取这条流」，设备那边
#       ihwplayer 报 `net is disconnect!` —— 两条都指向网络，其实网络是好的
#       （实测设备取这条流 http=200、732378 字节、0.11 秒拿完）。
#   ⇒ **要开流就走下面两个函数**；`start()` 只留给服务进程自己。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# ★ 显式空 ProxyHandler：踩过"写死的死代理把请求堵死"的坑，
#   环回调用绝不能走代理 —— 否则症状又是一次"明明服务活着却连不上"。


def start_remote(text, instructions=None, timeout=None):
    """`POST /new` 让【服务进程】开一条流，阻塞到首包。

    回 `(sid, url, first)`；任何一步不成都回 `(None, None, 原因)` ——
    **跟 `start()` 的契约逐字一致**，调用方照旧原地退回整段合成。
    """
    body = json.dumps({'text': text, 'instructions': instructions}).encode('utf-8')
    req = urllib.request.Request('http://%s:%d/new' % (OUR_IP, PORT), data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with _OPENER.open(req, timeout=timeout or (FIRST_WAIT + 3.5)) as r:
            j = json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:                 # 503 = 服务端开了但没首包
        try:
            j = json.loads(e.read() or b'{}')
        except Exception:                               # noqa: BLE001
            j = {}
        return None, None, j.get('err') or ('HTTP %s' % e.code)
    except Exception as e:                              # noqa: BLE001
        return None, None, '%s: %s' % (type(e).__name__, e)
    if not j.get('ok'):
        return None, None, j.get('err') or '端点没开成流'
    return j.get('sid'), j.get('url'), j.get('first')


def pulled_remote(sid, timeout=1.5):
    """问【服务进程】：设备来取这条流了没有。

    ★ 调用方（`spk_ai_dlna._say601` 的补发闸门）拿它当"链路走通了"的硬证据。
      本进程看不见那条流 ⇒ 只能问。**读不到一律回 False**（照旧走"没取就补发"
      那条老路，绝不因为量不到就改行为）。
    """
    try:
        with _OPENER.open('http://%s:%d/tts/%s/info' % (OUR_IP, PORT, sid),
                          timeout=timeout) as r:
            return bool(json.loads(r.read() or b'{}').get('pulled'))
    except Exception:                                   # noqa: BLE001
        return False


def wait_file(sid, timeout=15.0):
    """等这条流**落盘齐了**（人声 + 静音垫），回路径；超时回 None。

    ★ 给 `spk_ear.speak()` 用，而且这一等是**并行的**：URL 早就推给设备了、
      它已经在播，等的这段时间主人听不出任何区别。等的目的只有两个：
        ① 把 mp3 交给 `_wait_and_judge` 判"到底听没听见"（`say()` 的老判据）；
        ② 让 `_stage601` 填 `_PLAY['secs']` —— 回声闸门 `audible_end()` 靠它。
      ⇒ **超时不是失败**：回 None 时调用方照常算"这次说成了"，只是自听那步退回
        （老路本来就允许 `ear is None`）。绝不许因为等不到文件就说"没念成"。
    """
    end = time.time() + timeout
    p = '/tmp/spk_tts_%s.mp3' % sid
    while True:
        st = STREAMS.get(sid)
        if st is not None and st.mp3_path:
            return st.mp3_path
        if os.path.exists(p):            # 流已被 sweep 清掉，但文件还在
            return p
        if time.time() >= end:
            return None
        time.sleep(0.1)


def _sweep():
    while True:
        time.sleep(20)
        now = time.time()
        with _lock:
            for sid, st in list(STREAMS.items()):
                if st.done and now - st.t0 > TTL:
                    STREAMS.pop(sid, None)


def _ensure_pad():
    """静音垫素材：48k/mono/128k，**不带 Xing**（要跟人声 mp3 首尾相接）。

    ★ 用 48000 和 128k 是跟 `voice.SR/BR` 对齐 —— 拼接处格式不一致设备可能要重配。
    """
    if os.path.exists(PAD_MP3) and os.path.getsize(PAD_MP3) > 0:
        return True
    try:
        p = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
             '-f', 'lavfi', '-i', 'anullsrc=r=%s:cl=mono' % voice.SR,
             '-t', '%.1f' % PAD_SECS, '-c:a', 'libmp3lame', '-b:a', voice.BR,
             '-write_xing', '0', PAD_MP3],
            capture_output=True, timeout=60)
        ok = p.returncode == 0 and os.path.getsize(PAD_MP3) > 0
        log('静音垫素材 %s（%.1fs）%s' % (PAD_MP3, PAD_SECS, '好了' if ok else '没做成'))
        return ok
    except Exception as e:                                      # noqa: BLE001
        log('静音垫素材没做成：%s: %s', type(e).__name__, e)
        return False


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'spkttsstream/0.1'

    def log_message(self, fmt, *args):
        pass                                    # 走我们自己的 log()

    # ---------------------------------------------------------- POST /new
    def do_POST(self):
        if self.path.split('?')[0] not in ('/new', '/new/'):
            self.send_error(404)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:                                  # noqa: BLE001
            self._json(400, {'ok': False, 'err': 'body 看不懂：%s' % e})
            return
        sid, url, extra = start(body.get('text') or '',
                                body.get('instructions'))
        if not sid:
            log('POST /new 开流失败：%s', extra)
            self._json(503, {'ok': False, 'err': extra})
            return
        st = STREAMS[sid]
        log('POST /new sid=%s %d 字 → 首包 %.2fs', sid, len(body.get('text') or ''), extra)
        self._json(200, {'ok': True, 'sid': sid, 'url': url,
                         'first': round(extra, 3), 'info_url': url + '/info'})

    # ---------------------------------------------------------- GET /tts/<sid>
    def do_GET(self):
        path = self.path.split('?')[0]
        if path.rstrip('/') in ('', '/health'):
            self._json(200, {'ok': True, 'streams': len(STREAMS)})
            return
        parts = [p for p in path.split('/') if p]
        if len(parts) < 2 or parts[0] != 'tts':
            self.send_error(404)
            return
        sid = parts[1]
        st = STREAMS.get(sid)
        if st is None:
            log('GET %s —— 没有这条流（可能已经过期清了）', path)
            self.send_error(404)
            return
        if len(parts) > 2 and parts[2] == 'info':
            self._json(200, st.info())
            return
        self._serve(st)

    def _serve(self, st):
        """★★ 无 Content-Length + `Connection: close`，靠连接结束表示流结束 ——
        设备实测吃这一套（见文件头）。"""
        # ★ 置位要跟"它真来拉了"同一刻 —— 调用方拿它当链路通的判据，晚一拍都可能
        #   让它以为没人来取、进而补发 0x601（那是会打断正在播的这条的）。
        with st.cond:
            if not st.pulled:
                st.pulled = True
                st.pull_at = time.time() - st.t0
            n_have = len(st.buf)
        log('sid=%s 设备来取流（已生成 %d 字节）', st.sid, n_have)
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Connection', 'close')
        self.end_headers()
        pos, t0 = 0, time.time()
        try:
            while True:
                with st.cond:
                    if pos >= len(st.buf) and not st.done:
                        st.cond.wait(1.0)
                    chunk = bytes(st.buf[pos:])
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    pos += len(chunk)
                elif st.done:
                    break
            log('  sid=%s 人声吐完 %d 字节 / %.2fs，接静音垫', st.sid, pos, time.time() - t0)
            if os.path.exists(PAD_MP3):
                with open(PAD_MP3, 'rb') as f:
                    self.wfile.write(f.read())      # ★ 静音垫立刻全吐（模拟"全速下载"）
                self.wfile.flush()
            log('  sid=%s 收工（共 %.2fs）%s', st.sid, time.time() - t0,
                ('  ⚠ ' + st.err) if st.err else '')
        except (BrokenPipeError, ConnectionResetError) as e:
            log('  sid=%s 客户端断了（%s）于第 %d 字节', st.sid, type(e).__name__, pos)

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == '__main__':
    qs.set_logger(lambda f, *a: log(f, *a))
    if not _ensure_pad():
        log('⚠ 没有静音垫素材 —— 回答会被设备的 Next 判据拦腰切（照旧出声，但不完整）')
    threading.Thread(target=_sweep, daemon=True).start()
    srv = ThreadingHTTPServer((OUR_IP, PORT), H)
    srv.daemon_threads = True
    log('流式 TTS 端点起来 —— http://%s:%d/  嗓子=%s 地域=%s 响度链=%s',
        OUR_IP, PORT, q.VOICE, q._HOST, voice.chain())
    srv.serve_forever()
