#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen 实时流式 TTS —— 首包 0.28s 就出声，边收边推。

★ 为什么要有这个文件（2026-09-23 实测）
  非流式那条路（`spk_tts_qwen.synth`）是「整句合成完才给你」：实测 1.2~1.5s，
  而且与字数成正比（≈0.5s + 0.055s/字）。换到另一条线上，这就是主人抱怨的「3 秒多」里最大的一块。
  实时 WebSocket（真端点，三句不同长度）：

      建连 0.06s → session.update 0.035s → commit → **首包 0.25~0.29s**

  ★★★★★ 决定性的一条：**音频生成速率 4~6× 实时、需预缓冲 0.000s**
  —— 到得比播得快，所以「边收边推」不会饿死断音。这条不成立的话流式毫无意义。

★ 两个硬事实（量出来的，别照文档猜）
  1. ★★★★★ **一条连接只能配置一次 session**。第一轮 response 结束后再发
     `session.update`，服务端回 `invalid_value: session already started or finished
     or failed`（已排空到 `response.done` 复现过 ⇒ 不是收尾没干净）。
     ⇒ **一条连接服务一轮**；逐轮换语气只能靠新建连接（建连 0.06s，可忽略）。
  2. **`sample_rate: 8000` 服务端接受** ⇒ 拿到手就是 AudioSocket 要的格式，
     **省掉一次 ffmpeg 转码**（非流式那边是 24k wav，必须转）。

★ 事件流（照抄一遍，改的时候对着看）
  `session.created` →【客户端】`session.update` → `session.updated`
  →【客户端】`input_text_buffer.append` + `input_text_buffer.commit`
  → `input_text_buffer.committed` → `response.created` → `response.output_item.added`
  → `response.content_part.added` → `response.audio.delta`(base64)×N
  → `response.audio.done` → `response.content_part.done`
  → `response.output_item.done` → `response.done`

★ 设计约束（与 spk_tts_qwen 一致）
  · 只 import 兄弟模块 `spk_tts_qwen` 拿 key/音色/默认指令（**key 只有一处定义**）
  · **失败一律返回 {'ok': False}，绝不抛出去** —— 调用方据此退回非流式、再退 edge-tts。
    铁律：**宁可慢一点，不能说不出话。**
"""
import asyncio
import base64
import json
import os
import time

import aiohttp

import spk_tts_qwen as q                    # key / 音色 / 默认指令 / OPTIMIZE

# ★ 复用兄弟模块定下的地域（`_HOST` 是那边按 SPK_QWEN_REGION 算好的）。
#   北京与新加坡的 key 不通用，这里绝不能自己写死域名。
WS_URL = ('wss://%s/api-ws/v1/realtime?model=qwen3-tts-instruct-flash-realtime'
          % q._HOST)

CONNECT_TIMEOUT = float(os.environ.get('SPK_QWEN_WS_CONNECT', '5.0'))
FIRST_TIMEOUT = float(os.environ.get('SPK_QWEN_WS_FIRST', '6.0'))    # 等首包
IDLE_TIMEOUT = float(os.environ.get('SPK_QWEN_WS_IDLE', '8.0'))      # 包与包之间
TOTAL_TIMEOUT = float(os.environ.get('SPK_QWEN_WS_TOTAL', '30.0'))   # 一轮天花板

_log = lambda *a: None                      # 调用方换成真日志（本模块不 import 项目内的东西）


def set_logger(fn):
    global _log
    _log = fn


def available():
    """有没有 key。没 key 就别调了，白等一次建连。"""
    return bool(q.KEY)


def _fail(why):
    _stat['fail'] += 1
    _log('🔈 流式 TTS 没成：%s', why)
    return {'ok': False, 'err': why, 'first': None, 'done': None, 'bytes': 0}


def _brief(d):
    """把服务端的 error 事件压成一行 —— 光看 'error' 三个字排查不动。"""
    e = (d or {}).get('error') or {}
    return ('%s: %s' % (e.get('code') or d.get('type') or '?',
                        e.get('message') or ''))[:180]


async def _recv(ws, timeout):
    """收一条事件。★ 不是 TEXT 的都归一成 `_xxx`，调用方只需认一种形状。"""
    m = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if m.type == aiohttp.WSMsgType.TEXT:
        try:
            return json.loads(m.data)
        except ValueError:
            return {'type': '_raw'}
    if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                  aiohttp.WSMsgType.CLOSE):
        return {'type': '_closed', 'code': ws.close_code}
    if m.type == aiohttp.WSMsgType.ERROR:
        return {'type': '_error', 'msg': str(ws.exception())}
    return {'type': '_other'}


async def stream(text, on_pcm, instructions=None, voice=None, sample_rate=8000):
    """合成 `text`，**每收到一块 8k PCM 就立刻调一次 `on_pcm(bytes)`**。

    ★ `on_pcm` 是**同步**回调，直接在事件循环里调 ⇒ 它必须只做「塞队列」这种
      不阻塞的事（另一个壳那边就是它自己的推流口）。它返回后我们才继续收下一块，
      所以推流端不会因为我们收包而被饿着 —— 反过来也一样（`await` 会让出控制权）。

    返回 {'ok', 'first', 'done', 'bytes', 'chars', 'conn', 'upd', 'err'}：
      · `first` = **commit 到首块 PCM 的秒数** —— 这才是「对方多久听见」里 TTS 的那一栏
      · `done`  = commit 到收完的秒数（此时音频往往还在播，别拿它当延迟）
      · `ok=True` 但 `trunc=True` ⇒ 中途断了，**已经推出去的音频撤不回来**，
        调用方该知道这轮是半句（记日志，别当正常轮）。

    任何失败都返回 `ok=False`，**绝不抛**。
    """
    if not q.KEY:
        return _fail('没配 key')
    if not text:
        return _fail('空文本')

    instr = q.INSTR_DEFAULT if instructions is None else instructions
    cfg = {'voice': voice or q.VOICE,
           'response_format': 'pcm',
           'sample_rate': sample_rate}
    if instr:
        cfg['instructions'] = instr
        if q.OPTIMIZE:                      # 默认关：纯白等 2 秒，见 spk_tts_qwen 头部
            cfg['optimize_instructions'] = True

    t0 = time.monotonic()
    n = 0
    first = None
    t_conn = t_upd = None
    done_seen = False
    try:
        timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT,
                                        sock_connect=CONNECT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            ws = await s.ws_connect(
                WS_URL, headers={'Authorization': 'Bearer %s' % q.KEY},
                max_msg_size=0)             # 音频 delta 可能很大，别让默认 4MB 截断
            t_conn = time.monotonic() - t0
            try:
                if (await _recv(ws, CONNECT_TIMEOUT)).get('type') != 'session.created':
                    return _fail('没等到 session.created')
                await ws.send_json({'type': 'session.update', 'session': cfg})
                d = await _recv(ws, CONNECT_TIMEOUT)
                if d.get('type') != 'session.updated':
                    return _fail('session.update 被拒 → %s' % _brief(d))
                t_upd = time.monotonic() - t0

                await ws.send_json({'type': 'input_text_buffer.append', 'text': text})
                await ws.send_json({'type': 'input_text_buffer.commit'})

                while True:
                    d = await _recv(ws, FIRST_TIMEOUT if first is None else IDLE_TIMEOUT)
                    ty = d.get('type')
                    if ty == 'response.audio.delta':
                        chunk = base64.b64decode(d.get('delta') or '')
                        if not chunk:
                            continue
                        if first is None:
                            first = time.monotonic() - t0
                        n += len(chunk)
                        on_pcm(chunk)       # ★★ 到了就推，绝不攒着等整句
                    elif ty == 'response.done':
                        done_seen = True
                        break
                    elif ty in ('_closed', '_error', '_other', '_raw', 'error'):
                        # ★ 已经出过声就当好的一半收下 —— 音频推出去就撤不回来了，
                        #   这时候报失败只会让调用方再合成一遍、把这段话重复播两次。
                        if n == 0:
                            return _fail(_brief(d) if ty == 'error' else ty)
                        _log('🔈 流式 TTS 中途断（已收到 %d 字节）：%s', n, ty)
                        break
                    # 其余（committed / created / added / done 之类）不用管
            finally:
                try:
                    await ws.close()
                except Exception:                   # noqa: BLE001
                    pass
    except asyncio.TimeoutError:
        if n == 0:
            return _fail('超时（%.1fs 没等到首包）' % (time.monotonic() - t0))
    except Exception as e:                          # noqa: BLE001
        if n == 0:
            return _fail('%s: %s' % (type(e).__name__, e))

    if n == 0:
        return _fail('零音频')

    _stat['ok'] += 1
    if not done_seen:
        _stat['trunc'] += 1
    return {'ok': True, 'first': first, 'done': time.monotonic() - t0,
            'bytes': n, 'chars': len(text), 'conn': t_conn, 'upd': t_upd,
            'trunc': not done_seen, 'err': ''}


_stat = {'ok': 0, 'fail': 0, 'trunc': 0}


def stats():
    return dict(_stat)


if __name__ == '__main__':
    # 离线自测（**不出声**，只写 /tmp）：
    #   /usr/bin/python3 spk_tts_qwen_stream.py '哎，是我啊，好久没联系了。'
    import sys
    set_logger(lambda f, *a: print(f % a if a else f))
    if len(sys.argv) < 2:
        print('端点 %s' % WS_URL)
        print('统计 %s' % json.dumps(stats(), ensure_ascii=False))
        sys.exit(0)

    async def _main():
        out = open('/tmp/qwen_stream_test.pcm', 'wb')
        total = [0]

        def sink(b):
            out.write(b)
            total[0] += len(b)

        for i, txt in enumerate(sys.argv[1:], 1):
            r = await stream(txt, sink, instructions='热络亲切，语速中等偏快，带笑意。')
            print('  第%d句 %r → %s' % (i, txt[:16], json.dumps(
                {k: (round(v, 3) if isinstance(v, float) else v)
                 for k, v in r.items()}, ensure_ascii=False)))
        out.close()
        print('共 %d 字节 → /tmp/qwen_stream_test.pcm' % total[0])

    asyncio.run(_main())
