#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻连接的 edge-tts —— 把每句话固定要花的那两秒砍掉。

★ 为什么值得单独一个模块（2026-09-22 实测，不是猜的）：
  走 `edge-tts` **命令行**时，**2 个字的「好。」也要 2.13~2.22 秒** ——
  说明花的不是合成的钱，是"起一个新进程 + DNS + TLS + WebSocket 握手"的钱。
  逐段量过：

      解释器 + import edge_tts   0.42~0.46s
      DNS（冷的）                0.49s（热了 0.001s）
      TLS 握手                   0.70~0.73s
      WS 握手 + 首个音频字节     ~0.5s
      ————————————————————————————————
      合成本身（26 个字）        1.3s（每字约 0.01s）

  **固定开销比合成还贵。**

★★ 一条连接能连着合成多句 —— 协议本来就支持（官方代码里那个
  `last_duration_offset` 就是留给"下一次 SSML 请求"用的）。
  实测同一条连接连做 4 轮：建连 1.11 秒（**一次性**），之后每轮 0.39~0.77 秒。
  ⇒ TTS 从 2.7~3.2 秒掉到 0.7~1.1 秒（含调用方那步 ffmpeg）。
  ★ 音频与命令行那条路**逐帧一致**：都是 24kHz / 单声道 / 48kbps，
    过完滤镜链时长分毫不差（实测 1.896s vs 1.896s）。

★★★ 两条硬约束：

  ① **空转会被掐。** 实测空转 45 秒后再说，直接
     `Cannot write to closing transport`。所以：
       · `warm()` 由**唤醒那一刻**在后台调 —— 把 1.1 秒的建连藏进
         "录音 + ASR" 那 1~3 秒里，他听不出来；
       · **用坏了就重连重试一次**，不指望它一直活着。

  ② **绝不拖挂嗓子。** 这个模块的任何失败都只让调用方**退回原来的命令行那条路**
     （`spk_ai_dlna.tts()` 里的兜底），绝不抛出去。
     宁可慢两秒，不能说不出话 —— 跟声纹/打断那条链是同一条铁律。

★ 开关：`SPK_TTS_WARM=0` 一句话退回命令行那条路（不用改代码、不用重推任何东西）。
"""
import asyncio
import os
import threading
import time

ENABLED = os.environ.get('SPK_TTS_WARM', '1') == '1'
CONNECT_TIMEOUT = float(os.environ.get('SPK_TTS_CONNECT', '8.0'))
TURN_TIMEOUT = float(os.environ.get('SPK_TTS_TURN', '30.0'))
RECV_TIMEOUT = float(os.environ.get('SPK_TTS_RECV', '20.0'))

_log = lambda *a: None          # 由 spk_ai_dlna 换成真日志（这里不 import 它，免得成环）


def set_logger(fn):
    """调用方把自己的 log 递进来 —— 这个模块不 import 任何项目内的东西。"""
    global _log
    _log = fn


class _Conn:
    """一条常驻连接。**只在它自己的 loop 线程里被碰**，外面一律经 run_coroutine_threadsafe。"""

    def __init__(self, loop):
        self.loop = loop
        self.session = None
        self.ws = None
        self.turns = 0

    async def drop(self):
        for x in ('ws', 'session'):
            o = getattr(self, x)
            setattr(self, x, None)
            if o is not None:
                try:
                    await o.close()
                except Exception:                       # noqa: BLE001
                    pass

    async def ensure(self):
        """没有连接就建一条（含 speech.config）。★ 建过就直接返回，所以唤醒时随便调。"""
        if self.ws is not None and not self.ws.closed:
            return
        await self.drop()
        import aiohttp
        from edge_tts.communicate import (
            WSS_URL, WSS_HEADERS, SEC_MS_GEC_VERSION, _SSL_CTX, connect_id,
            date_to_string)
        from edge_tts.drm import DRM
        t0 = time.time()
        self.session = aiohttp.ClientSession(trust_env=True)
        self.ws = await asyncio.wait_for(self.session.ws_connect(
            f"{WSS_URL}&ConnectionId={connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
            compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_SSL_CTX,
        ), timeout=CONNECT_TIMEOUT)
        # ★ speech.config 只跟连接有关、跟文本无关 —— 所以能在预热时就发掉，
        #   真正要说话时只剩"发 SSML + 等音频"。
        await self.ws.send_str(_cfg_msg(date_to_string))
        self.turns = 0
        _log('🔈 TTS 常驻连接建好了（%.2fs，之后每句就不用再建了）', time.time() - t0)

    async def synthesize(self, text):
        """在常驻连接上合成一条，返回 mp3 字节。★ 只试两次：坏连接重连一次。"""
        from edge_tts.communicate import (connect_id, date_to_string, mkssml,
                                          ssml_headers_plus_data, get_headers_and_data)
        ssml = ssml_headers_plus_data(connect_id(), date_to_string(), mkssml(_cfg(), text))
        last = None
        for attempt in (1, 2):
            try:
                await self.ensure()
                await self.ws.send_str(ssml)
                buf = bytearray()
                while True:
                    m = await asyncio.wait_for(self.ws.receive(), timeout=RECV_TIMEOUT)
                    if m.type == 1:                     # TEXT
                        raw = m.data.encode()
                        p, _ = get_headers_and_data(raw, raw.find(b'\r\n\r\n'))
                        if p.get(b'Path') == b'turn.end':
                            break
                    elif m.type == 2:                   # BINARY
                        # ★★ 官方切帧是 `header_length + 2`（那 2 是头后面那个空行）。
                        #  我第一版写成 `header_length`，整条音频流错位 2 字节 ——
                        #  文件大小只差几十字节，**ffprobe 报的时长还正常**，
                        #  但 ffmpeg 只解得出一部分 ⇒ 声音被掐短 0.34 秒。
                        #  "差不多对"的字节流是最难查的一类错，所以这里照抄官方。
                        _, data = get_headers_and_data(
                            m.data, int.from_bytes(m.data[:2], 'big'))
                        buf += data
                    else:                               # CLOSED / ERROR / 其它
                        raise RuntimeError('ws %s' % m.type)
                if not buf:
                    raise RuntimeError('这一段一个字节的音频都没收到')
                self.turns += 1
                return bytes(buf)
            except Exception as e:                      # noqa: BLE001
                last = e
                await self.drop()
                if attempt == 2:
                    break
                _log('🔈 TTS 常驻连接断了（%s: %s）—— 重连再试一次',
                     type(e).__name__, e)
        raise last if last else RuntimeError('合成失败')


def _cfg_msg(date_to_string):
    return ('X-Timestamp:%s\r\nContent-Type:application/json; charset=utf-8\r\n'
            'Path:speech.config\r\n\r\n'
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
            % date_to_string())


def _cfg():
    """嗓子参数**从 spk_voice 现取** —— 绝不在这个文件里存成常量。

    ★ 这是个踩过的坑的形状：滤镜串存成常量会"冻住"（回答和思考词一个响一个轻）。
      嗓子的 rate/pitch 同理 —— 主人哪天改 `SPK_VOICE`/`SPK_RATE`，这里必须跟着变。
    """
    from edge_tts.data_classes import TTSConfig
    import spk_voice as v
    return TTSConfig(voice=v.VOICE, rate=v.RATE or '+0%',
                     volume='+0%', pitch=v.PITCH or '+0Hz',
                     boundary='SentenceBoundary')


# ------------------------------------------------------------------ 门面
_loop = None
_conn = None
_start_lock = threading.Lock()
_call_lock = threading.Lock()       # 一条连接不许两句同时用
_born = 0.0
_fails = 0
_last_ok = 0.0                      # ★ 上一次【成功合成】的时刻（保温的心跳判据读它）


def _ensure_loop():
    """起那条后台 loop（只起一次）。★ 只起线程，**不建连** —— 建连要 1.1 秒，不能挡路。"""
    global _loop, _conn, _born
    with _start_lock:
        if _loop is not None:
            return _loop
        _loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        threading.Thread(target=run, name='tts-warm', daemon=True).start()
        _conn = _Conn(_loop)
        _born = time.time()
        return _loop


# ★★★★★ 2026-09-22 深夜：预热必须【真发一句】—— 一次静默实测的结论（只往 /tmp 写、不出声）。
#
#     冷连接建连                    1.56 秒
#     刚建好连的【第一句】          2.30 秒   ← 连接级的握手，跟文本长短无关
#     连着说（隔 3 秒）             0.34 秒
#     空闲 45 秒后再来              服务端已掐 ⇒ 重连 1.56 + 首句 1.91 = 3.5 秒
#
#   ⇒ **人隔几十秒再唤醒是常态，所以"每轮唤醒时那条连接必是死的"。**
#     而老版 `warm()` 只查本地 `_conn.ws.closed` —— 服务端悄悄关掉的连接在本地看
#     还是"活着"的 ⇒ **它对着死连接是个空操作**（文件头那句"唤醒那一刻调它最合适"
#     的意图因此一直没兑现）。真发一句同时做两件事：① 把死连接试出来并重连；
#     ② 把它从"冷"焐到"热"。
#   ★ 这是主人那句「没有思考音」的一半真凶（设备日志 20:13 那一轮）：
#     回答那句才去重连 ⇒ 2.2 + 2.7 = **4.9 秒死寂**，而思考音阶梯在"回答文本好了"
#     那一刻就让路了（文本 1 秒就回来），没人盖那 4.9 秒。
#   ★ 代价是每次唤醒多一次短合成（一句话的流量）；`_call_lock` 保证它不会跟
#     回答那句同时用连接。`SPK_TTS_WARM_REAL=0` 一句话退回老行为。
WARM_REAL = os.environ.get('SPK_TTS_WARM_REAL', '1') == '1'
WARM_TEXT = os.environ.get('SPK_TTS_WARM_TEXT', '在的。')
# ★ 预热自己那条合成的上限。**必须比 `synth_raw` 那条短**：它俩共用 `_call_lock`，
#   而 `_call_lock` 是【没有超时的普通锁】⇒ 预热要是挂在网络上，回答那次合成会
#   一直在门口等着。12 秒是"够它重连（1.6）+ 首句（2.3）再翻三倍"的余量。
#   ★ 超时后的残留交给 `synth_raw` 那条路自己兜：它读不到就回 False ⇒ `dlna.tts`
#     退回命令行那条慢路，**照样说得出话**（铁律：快路是"更好"，说话是"这次要命"）。
WARM_MAX = float(os.environ.get('SPK_TTS_WARM_MAX', '12.0'))


def _warm_real_locked():
    """★ 调用方必须【已经持有 `_call_lock`】—— 见 `_warm_real()` 与 `_ka_tick()`。

    拆出这一层是因为 `threading.Lock` 不可重入：保温那个心跳线程必须用
    `acquire(blocking=False)` 抢锁（抢不到就跳过，绝不排队），
    而它要做的事与 `_warm_real()` 里那段完全相同 —— 直接把那段提出来共用。
    """
    global _last_ok
    loop = _ensure_loop()
    t0 = time.time()
    data = asyncio.run_coroutine_threadsafe(
        _conn.synthesize(WARM_TEXT), loop).result(
            timeout=min(CONNECT_TIMEOUT + TURN_TIMEOUT, WARM_MAX))
    # ★ 成功才更新。判据是"距上次【成功】多久"，不是"距上次尝试多久" ——
    #   失败就一路失败的话，我们要的是【每一拍都再试】，不是越退越远。
    _last_ok = time.time()
    return t0, data


def _warm_real():
    """在常驻连接上真合成一句、**结果丢掉**。回 True/False，绝不抛。

    ★ 走 `_Conn.synthesize` 而不是自己发协议：那条路上已经带着"坏了就 drop 重连
      一次"的逻辑（见它的 attempt 1/2），不用抄第二遍。
    """
    try:
        with _call_lock:
            t0, data = _warm_real_locked()
        _log('🔈 TTS 预热：真合成一句（%.2f 秒，%d 字节，丢掉）—— 连接由冷转热，'
             '回答那句不必再付这笔', time.time() - t0, len(data))
        return True
    except Exception as e:                              # noqa: BLE001
        _log('🔈 TTS 预热没成（不影响说话，回答那句自己会重连）：%s: %s',
             type(e).__name__, e)
        return False


def warm(real=None):
    """后台把连接建好；`real` 时**还在这条连接上真合成一句**。立刻返回，绝不阻塞。

    ★★ `real` 的默认值是 `WARM_REAL`（默认开）—— 为什么必须"真发一句"而不是
      "只建连"，连着实测数字和那 4.9 秒死寂一起写在 `WARM_REAL` 上面那段里。
    ★ 失败绝不影响任何事：所有异常只让它自己退回命令行那条慢路。
    """
    if not ENABLED:
        return
    real = WARM_REAL if real is None else real
    try:
        def _go():
            try:
                loop = _ensure_loop()
                with _call_lock:
                    fresh = _conn.ws is None or _conn.ws.closed
                if fresh:                       # 本地就看得出是空的 ⇒ 先建起来
                    asyncio.run_coroutine_threadsafe(
                        _conn.ensure(), loop).result(CONNECT_TIMEOUT + 2.0)
                if real:
                    # ★ 本地"看着活着"但服务端已经掐掉的那种，只有真写一次才知道
                    #   —— `synthesize` 里那次 drop + 重连就是为它准备的。
                    _warm_real()
            except Exception as e:                      # noqa: BLE001
                _log('🔈 TTS 预热没成（不影响说话，会走慢的那条）：%s: %s',
                     type(e).__name__, e)
        threading.Thread(target=_go, name='tts-warm', daemon=True).start()
    except Exception as e:                              # noqa: BLE001
        _log('🔈 TTS 预热没成（不影响说话，会走慢的那条）：%s: %s', type(e).__name__, e)


def synth_raw(text, out_path):
    """合成到 `out_path`（**原始 mp3，不过滤镜** —— 滤镜归调用方那一步）。

    成功返回 True；**任何失败都返回 False**，由调用方退回命令行那条路。
    """
    global _fails, _last_ok
    if not ENABLED or not text:
        return False
    try:
        loop = _ensure_loop()
        with _call_lock:
            t0 = time.time()
            data = asyncio.run_coroutine_threadsafe(
                _conn.synthesize(text), loop).result(timeout=CONNECT_TIMEOUT + TURN_TIMEOUT)
            _last_ok = time.time()          # ★ 保温的心跳判据读它（见 _ka_tick）
            with open(out_path, 'wb') as f:
                f.write(data)
        _log('🔈 合成 %.2fs（常驻连接第 %d 句，%d 字节）',
             time.time() - t0, _conn.turns, len(data))
        return True
    except Exception as e:                              # noqa: BLE001
        _fails += 1
        _log('🔈 常驻连接这条路没成（%s: %s）—— 退回命令行（慢两秒，但照样说得出话）',
             type(e).__name__, e)
        return False


# ★★★★★ 2026-09-23：连接保温（keepalive）—— 补上「唤醒时焐一次」够不着的那个空档
#
#   14 天 journal 的账：
#       `TTS 常驻连接断了`    30 次
#       `TTS 常驻连接建好了`  58 次
#   ⇒ **58 次建连里有 30 次是"合成中途断了被迫重连"** —— 大约一半的合成撞上的是死连接。
#   原因就是文件头那条硬约束：**服务端空闲 45 秒就掐**，而主人一轮一轮之间隔几十秒
#   是常态。`warm()` 一天只在【唤醒】那一刻跑一次，会话里两轮之间那段空档没人管
#   ⇒ **每轮回答时那条连接大概率已经是死的。这不是偶发，是默认状态。**
#   表现就是 `🔈 合成` 的中位 0.98s、p75 **2.10s**、最长 3.41s，
#   而健康连接上只要 ~0.6s、连着说只要 0.34s。
#
#   做法：**不加新子系统**，只是把已经验证过的 `_warm_real()` 在会话里【按需重跑】。
#   一个 daemon 心跳线程，每 `KEEP_TICK`(5s) 醒一次，四个条件全满足才动手：
#     · `ENABLED`（总开关）且 keepalive 开着（**只在会话里** —— 不做 7×24 心跳，
#       那是白烧流量）
#     · 距上次【成功】合成 ≥ `KEEP_IDLE`(20s) —— 远小于服务端那 45s
#     · ★★★ **`_call_lock` 是空的**（`acquire(blocking=False)`）——
#       **心跳绝不许挡住一次真回答的合成。** 抢不到就跳过这一拍，5 秒后再来。
#       抢不到其实等于"这条连接刚刚被用过"⇒ 本来也不需要保温。
#
#   ★ 一个字都不出声：合成的字节直接丢掉，不落盘、不碰 dlna、不碰设备。
#
#   ★★ 如实说清不确定性：**每 20 秒一次微合成会不会被服务端限流 / 反噬连接，
#      我不知道，没测过。** 所以默认开、有开关、上线后【看两个数】：
#      `stats()['fails']` 与 `🔈 合成` 的耗时分布。要是 `fails` 变多、或 p75 没降下来
#      ⇒ `SPK_TTS_KEEPALIVE=0`，一行环境变量退回今天的行为。
KEEPALIVE = os.environ.get('SPK_TTS_KEEPALIVE', '1') == '1'
KEEP_IDLE = float(os.environ.get('SPK_TTS_KEEP_IDLE', '20.0'))
KEEP_TICK = float(os.environ.get('SPK_TTS_KEEP_TICK', '5.0'))

_ka_on = False
_ka_started = False
_ka_n = 0                           # 发过几次保温（stats() 里看得到）


def _ka_tick():
    """一次心跳。★ 抢不到 `_call_lock` 就【立刻跳过】——
    这是这个线程能存在的前提：它绝不许拖慢一次真回答。"""
    global _ka_n
    if not (ENABLED and KEEPALIVE) or not _ka_on:
        return
    if time.time() - _last_ok < KEEP_IDLE:
        return
    if not _call_lock.acquire(blocking=False):
        return
    try:
        if time.time() - _last_ok < KEEP_IDLE:
            return                  # 等锁这一小会儿刚好有人合成完了 ⇒ 不用保温了
        t0, data = _warm_real_locked()
        _ka_n += 1
        _log('🔈 TTS 保温：距上次合成 %.0fs，真合成一句（%.2fs，%d 字节，丢掉）',
             t0 - _last_ok, time.time() - t0, len(data))
    except Exception as e:                          # noqa: BLE001
        _log('🔈 TTS 保温没成（不影响说话，回答那句自己会重连）：%s: %s',
             type(e).__name__, e)
    finally:
        _call_lock.release()


def _ka_loop():
    """★ 这个循环【绝不许死】—— 它死了就没有保温，而且没有任何提示。"""
    while True:
        try:
            _ka_tick()
        except Exception as e:                      # noqa: BLE001
            _log('🔈 TTS 保温线程出错（已吞掉，继续）：%s: %s', type(e).__name__, e)
        time.sleep(KEEP_TICK)


def keepalive(on):
    """会话开始/结束时开合保温。**只开关，不建连、不发包、立刻返回。**

    ★ 线程只在第一次开到 on 时起来，之后一直活着（空转时一拍什么都不做，
      代价是一次 `time.time()`）。会话之外一律不发 —— 见上面那段定案。
    """
    global _ka_on, _ka_started
    _ka_on = bool(on) and ENABLED and KEEPALIVE
    if not _ka_on:
        return
    if _ka_started:
        return
    _ka_started = True
    threading.Thread(target=_ka_loop, name='tts-keep', daemon=True).start()


def stats():
    return {'enabled': ENABLED, 'turns': (_conn.turns if _conn else 0),
            'fails': _fails, 'up': bool(_conn and _conn.ws is not None
                                        and not _conn.ws.closed),
            'ka_on': _ka_on, 'ka_n': _ka_n,
            'idle': (time.time() - _last_ok) if _last_ok else -1.0}


def _selftest():
    """离线自检：合成两句，跟命令行那条路**比时长**。不出声、不碰设备。"""
    import subprocess
    import sys
    ok = True

    def dur(p):
        o = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'csv=p=0', p],
                           capture_output=True, text=True).stdout.strip()
        return float(o or 0)

    for i, txt in enumerate(['好。', '现在下午一点十四分。'], 1):
        a = '/tmp/_tts_selftest_warm_%d.mp3' % i
        b = '/tmp/_tts_selftest_cli_%d.mp3' % i
        t0 = time.time()
        got = synth_raw(txt, a)
        tw = time.time() - t0
        subprocess.run(['edge-tts', '--voice=zh-CN-XiaoxiaoNeural', '--rate=-8%',
                        '--pitch=-10Hz', '--text', txt, '--write-media', b],
                       check=True, capture_output=True)
        if not got:
            print('  ✗ 第%d句 常驻连接没合成出来' % i)
            ok = False
            continue
        dw, dc = dur(a), dur(b)
        same = abs(dw - dc) < 0.05
        print('  %s 第%d句 %-12s 常驻 %.2fs / 音频 %.3fs  命令行 %.3fs  —— 时长%s'
              % ('✓' if same else '✗', i, txt, tw, dw, dc,
                 '一致' if same else '★ 不一致！'))
        ok = ok and same
    print('  %s' % ('✓ 常驻连接的音频与命令行那条路一致' if ok else '✗ 有出入，别上线'))
    return 0 if ok else 1


if __name__ == '__main__':
    set_logger(lambda f, *a: print('  ' + (f % a if a else f)))
    raise SystemExit(_selftest())
