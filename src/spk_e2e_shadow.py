#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机端到端：真实音频从设备耳朵进去，走完 唤醒→应答→提问→思考词→回复。

★★★ 为什么要有这个"影子 Ear"而不是直接用生产的 spk-ear：
    生产的 push/say 会把【真音频】推给音箱 ⇒ 屋里出声。而主人要求「会出声的测试
    默认不做、先问」。这个进程跟生产【逐字同一个 spk_ear.py】，只把两个出口
    （`dlna.push` / `dlna.say`）换成【等长静音替身】——
      · 等长：`anullsrc` CBR128k，字节数与真轨只差 1 帧（2026-09-22 验过的方法）
      · 走完整流程：真合成、真 0x601 推流、真设备取流、真设备守卫/暂停
    ⇒ **时序全真，声音全无**。其余每一个零件都是生产的原件：
      设备麦克风 → fespatap/mictap → UDP 9998 → silero VAD → zipformer ASR
      → 设备引擎的唤醒（waketap → TCP 9997）→ DeepSeek → edge-tts 常驻连接。

★ 应答那一声"叮"是【设备固件自己】放的（`/rom/.../S003.mp3`），不走本进程，
  而它早被 `voicemute.sh` 顶哑了（实测峰值 0.0076，见 spk_ear.py:100）⇒ 也不出声。

★ 记账落在 EVENTS 里（墙钟 + 阶段 + 明细），stdout 是 spk-ear 自己的日志。
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import spk_ai_dlna as dlna                                       # noqa: E402
import spk_ear as E                                              # noqa: E402

# ★★★ 为什么要补这一段（2026-09-22 踩到）：
#   生产 spk-ear 走的是 spk_ear.py 的 main()，那里有一句
#       k = agent._key();  os.environ.setdefault('DS_KEY', k)      （:1504-1508）
#   而影子 Ear 是直接 `E.Ear(...)`，【绕过了 main()】⇒ 环境里没有 DS_KEY
#   ⇒ 大脑调用里 `spk_ai_dlna.py:1243  key = os.environ["DS_KEY"]` 抛 KeyError
#   ⇒ 回复退化成兜底语「我这会儿连不上脑子」。这里补上同一句（仍是三级回退，
#   env → spk.key → settings.json），**一个字都不打印**。
import spk_agent as _agent                                       # noqa: E402
if not os.environ.get('DS_KEY'):
    _k = _agent._key()
    if _k:
        os.environ.setdefault('DS_KEY', _k)
        print('  [e2e] DS_KEY 已按三级回退载入（值不打印）', flush=True)
    else:
        print('  [e2e] ✗ 三级回退都没找到 key —— 回复会退化成兜底语', flush=True)

EVENTS = os.environ.get('SPK_EVENTS', '/tmp/spk_e2e_live.events')
TWIN_DIR = '/tmp/spk_e2e_twins'
_LOCK = threading.Lock()
_TWINS = {}


def ev(stage, detail=''):
    with _LOCK:
        with open(EVENTS, 'a') as f:
            f.write('%.3f\t%s\t%s\n' % (time.time(), stage, detail))
    print('  ▸▸ [e2e] %s %s' % (stage, detail), flush=True)


# ── 思考词文本反查（文件名是 hash，文本在 index.json 里）──
_TXT = {}


def _load_txt():
    import json
    try:
        d = json.load(open('filler/index.json'))
        for it in d.get('items', []):
            p = it.get('path') or ''
            if p:
                _TXT[os.path.basename(p)] = it.get('text', '')
    except Exception as e:                                        # noqa: BLE001
        print('  [e2e] 思考词文本表没读上（%s）—— 只报文件名' % e, flush=True)


def text_of(path):
    b = os.path.basename(path)
    if b in _TXT:
        return _TXT[b]
    # `_stage601` 会改名成 spk601_xxx.mp3
    for k, v in _TXT.items():
        if b.endswith(k) or k in b:
            return v
    return ''


# ── 静音替身 ──
def twin_of(path):
    """给一条 mp3 造一个【等长但没声音】的替身。

    ★★★ fail-closed：造不出来就返回 None，调用方【绝不退回原文件】——
      "替身失败 ⇒ 推真轨 ⇒ 屋里突然出声"是这里唯一不可接受的失败方向。
    """
    with _LOCK:
        if path in _TWINS:
            return _TWINS[path]
    try:
        total = dlna.mp3_span(path)[0]
        if not total or total <= 0:
            return None
        os.makedirs(TWIN_DIR, exist_ok=True)
        out = os.path.join(TWIN_DIR, os.path.basename(path))
        subprocess.run(
            ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi',
             '-i', 'anullsrc=r=48000:cl=mono', '-t', '%.3f' % total,
             '-c:a', 'libmp3lame', '-b:a', '128k', '-write_xing', '1', out],
            check=True, capture_output=True)
        if not os.path.exists(out) or dlna.mp3_span(out)[0] <= 0:
            return None
        with _LOCK:
            _TWINS[path] = out
        return out
    except Exception as e:                                        # noqa: BLE001
        print('  [e2e] ✗ 替身造失败 %s：%s' % (path, e), flush=True)
        return None


_real_push, _real_say = dlna.push, dlna.say


def _wrap(fn, stage):
    def w(path, *a, **k):
        txt = text_of(path)
        tw = twin_of(path)
        if tw is None:
            ev('★★替身失败-不推', '%s（宁可不推，绝不推真轨）' % os.path.basename(path))
            return False
        dur = dlna.mp3_span(path)[0]
        ev(stage, '「%s」 %.2fs → 静音替身 %s' % (txt or '?', dur, os.path.basename(tw)))
        return fn(tw, *a, **k)
    return w


dlna.push = _wrap(_real_push, '思考词推送')
dlna.say = _wrap(_real_say, '回复推送')

_load_txt()
ev('影子Ear启动', 'pid=%d  push/say 已换成静音替身' % os.getpid())

ear = E.Ear(guard=True)
if not ear.mic.start():
    ev('★麦克风没起来', '放弃')
    sys.exit(1)
ear.load()
ev('引擎就绪', '开始等唤醒（真实设备麦克风）')
try:
    ear.run()
except KeyboardInterrupt:
    ev('收工', 'KeyboardInterrupt')
finally:
    try:
        ear.mic.stop()
    except Exception:                                             # noqa: BLE001
        pass
ev('影子Ear退出', '')
