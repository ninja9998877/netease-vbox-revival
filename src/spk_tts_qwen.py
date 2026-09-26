#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里云百炼 Qwen-TTS —— 换掉 edge-tts 那个「一听就是 AI」的嗓子。

★ 为什么要有这个文件（2026-09-23）
  主人原话："这个语音太像AI了 一点不像人 说话没有情绪 没有抑扬顿挫 一个速度 一个语调"。
  实测把原因钉死了（见 /tmp/prosody_probe.py 那套静默探针）：

    · edge-tts 的 `--rate` / `--pitch` 是**整体挪移**，改不了句内起伏形状 ——
      同一个声音改前改后 F0 半音 σ 差不到 0.5；**"逐句给不同参数"实测无效**（反而更平）。
    · 起伏大小是**声音本身**的属性（云健 7.60 vs 晓晓 4.39）。
    · edge-tts **没有情绪参数**，这是引擎级天花板，调不出来。

  ⇒ 想要"有情绪"只能换引擎。全球自然度第一档（Inworld / Cartesia / ElevenLabs）
    **中文都还不成熟**（Cartesia 自家 changelog 把中文标为 work in progress），
    而 Qwen-Audio-3.0-TTS 在 Artificial Analysis 上 Elo ~1237、是全球前三，**中文原生**。

★ 这个文件的两条设计约束
  ① **独立模块、零新依赖**（手写 urllib，不引 requests / dashscope 包）。
     ★ 2026-09-23 晚更新：这条原写作「**不 import 项目内任何东西**」，理由是
       "只给电话线那套用、不许碰音箱"。当天主人拍板「全屋一起换（音箱也换 Maia）」
       ⇒ 那条约束的前提没有了，而且本文件原话就是"将来音箱要换，在 spk_tts 里
       加一行分发即可"—— 现在正是那个将来。
       现在**只 import 一个 `spk_voice`**：它是嗓子的唯一定义处，
       纯常量 + 纯函数、import 时零副作用（只 `import os, sys`）。
       音色从那里读，见下面 `VOICE`。**仍然不 import 别的、仍然零新依赖。**
  ② **失败一律返回 False，绝不抛出去**。调用方（电话线那套）据此退回 edge-tts。
     铁律与 spk_tts 一致：**宁可嗓子差一点，不能说不出话。**

★ 为什么用 urllib 手写，不用官方 dashscope SDK
  venv 里本来没有 requests/dashscope，而这个请求体就三层 JSON，
  手写能**零新依赖**，也不用去动音箱共用的那个 venv。跟 spk_tts 手写 WebSocket 是同一个取舍。

★ 三个容易踩的坑
  1. **key 分地域**：北京 `dashscope.aliyuncs.com` 与新加坡 `dashscope-intl.aliyuncs.com`
     的 API Key **不通用**。默认走北京（国内直连，不走代理）。
  2. **`instructions` 只认 Instruct 系列**：模型名必须是 `qwen3-tts-instruct-flash`，
     用 `qwen3-tts-flash` 传 instructions 是**静默无效**的（不报错，但语气不听你的）。
  3. **音频可能内联也可能给 url，两种都要接**：开着 `optimize_instructions` 时
     `output.audio.data` 是 base64 音频；**关掉它（现在的默认）时 `data` 是个空串、
     音频只在 `output.audio.url` 里**。所以判据是"`data` 解出来有没有东西"，
     不能假定有内联。（下载那一跳实测 0.05s，可忽略。）
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

import spk_voice as _vdef          # ★ 嗓子唯一定义处（见文件头 ① 的更新说明）

# ── 开关与参数 ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
KEYFILE = os.environ.get('SPK_QWEN_KEYFILE', os.path.join(_HERE, '.qwen_key'))


def _load_key():
    """key 从哪来：环境变量优先，其次同目录的 `.qwen_key`（0600）。

    ★ 为什么要支持文件：电话线那套是**手动起的**（没有 systemd）。key 只走环境变量的话，
      它会进 shell 历史、也会出现在 `ps` 的输出里。文件 + 0600 干净得多，
      而且这个文件是**这台机器自己的**，不经过任何人的剪贴板。
    """
    k = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    if k:
        return k
    try:
        with open(KEYFILE) as f:
            return f.read().strip()
    except OSError:
        return ''


KEY = _load_key()

MODEL_INSTRUCT = 'qwen3-tts-instruct-flash'     # 只有它认 instructions
MODEL_PLAIN = 'qwen3-tts-flash'                 # 不要语气时用

REGION = os.environ.get('SPK_QWEN_REGION', 'beijing').lower()
_HOST = ('dashscope-intl.aliyuncs.com' if REGION.startswith('sing')
         else 'dashscope.aliyuncs.com')
URL = 'https://%s/api/v1/services/aigc/multimodal-generation/generation' % _HOST

# ★★ 音色的唯一定义处是 `spk_voice.py`（2026-09-23 收回）。
#    这里原先自己写着 `os.environ.get('SPK_QWEN_VOICE', 'Cherry')` —— 那就是
#    **第二条真话来源**：改嗓子要改两个文件，改漏一个的后果是"回答 Maia、
#    思考音 Cherry"这种**只有耳朵能发现的错**（主人当天就是这么听出来的）。
#    ★ 下面几处用 `_vdef.qwen_voice()` **现读**，而不是把这个值存成常量 ——
#      与 `spk_voice.chain()` 同一条纪律：可切换的东西别冻在 import 那一刻。
VOICE = _vdef.QWEN_VOICE       # 仅作兜底/自检用；真正取值一律走 qwen_voice()

# ★ optimize_instructions：让服务端**先把口语化指令重写一遍**再喂给模型（多一次模型调用）。
#   实测【纯白等 2 秒，语气一点没变好】：同一句话 3.24s → 1.25s（快 2.6×），
#   而关掉后 instructions 照样灵 —— F0 中位仍随指令从 88.4 摆到 115.5 Hz（≈4.5 半音）、
#   时长差 20%。**所以默认关**。想自己对比就 `SPK_QWEN_OPTIMIZE=1`。
#   ★ 副作用（已处理）：开着时音频走 `audio.data`（base64 内联），关掉后走 `audio.url`
#     （data 是**空串**）—— 多下载一跳，实测只要 0.05s，可忽略。
OPTIMIZE = os.environ.get('SPK_QWEN_OPTIMIZE', '0').strip().lower() not in (
    '0', '', 'false', 'no', 'off')

# ★ 默认语气指令：**故意写成"是个活人在打电话"，而不是"播报得更好听"**。
#   电话这条线的病根是"播音腔"，所以指令里要把"念稿感"明确按下去。
#   1600 token 上限，这里几十个字，够用。
INSTR_DEFAULT = os.environ.get(
    'SPK_QWEN_INSTR',
    '像真人打电话聊天那样自然，语速中等偏快，句子短，'
    '语调有自然的起伏和停顿，偶尔带点笑意，不要播音腔，不要念稿子。')

TIMEOUT = float(os.environ.get('SPK_QWEN_TIMEOUT', '20.0'))

_log = lambda *a: None          # 由调用方换成真日志（这个模块不 import 项目内的东西）


def set_logger(fn):
    """调用方把自己的 log 递进来 —— 与 spk_tts 同一套约定。"""
    global _log
    _log = fn


def available():
    """有没有 key。**没有 key 时调用方应该直接走 edge-tts**，别白等一次超时。"""
    return bool(KEY)


def synth(text, out_path, instructions=None, voice=None, timeout=None):
    """文本 → wav 写到 `out_path`。成功 True；**任何失败都 False**。

    调用方拿到 False 必须退回 edge-tts（见文件头那条铁律）。
    """
    ok = _synth(text, out_path, instructions, voice, timeout)
    _last['ok' if ok else 'fail'] += 1        # 成败都计数，上线后靠它看健康度
    _last['voice'] = voice or _vdef.qwen_voice()   # 记【上次真正用的】音色，别让 stats 报默认值
    return ok


def _synth(text, out_path, instructions=None, voice=None, timeout=None):
    global _last_ok
    if not KEY:
        _log('🔈 Qwen-TTS 没配 key（DASHSCOPE_API_KEY）—— 走 edge-tts')
        return False
    if not text:
        return False

    instr = INSTR_DEFAULT if instructions is None else instructions
    body = {
        'model': MODEL_INSTRUCT if instr else MODEL_PLAIN,
        'input': {
            'text': text,
            'voice': voice or _vdef.qwen_voice(),
            'language_type': 'Chinese',
        },
    }
    if instr:
        body['input']['instructions'] = instr
        if OPTIMIZE:
            body['input']['optimize_instructions'] = True

    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer %s' % KEY,
                 'Content-Type': 'application/json'},
        method='POST')

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            payload = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        # ★ 把服务端的话原样带出来 —— 401/403 是 key 或地域不对，429 是限流，
        #   光看 "HTTPError" 三个字排查不动。
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:300]
        except Exception:                               # noqa: BLE001
            pass
        _log('🔈 Qwen-TTS HTTP %s：%s', e.code, detail)
        return False
    except Exception as e:                              # noqa: BLE001
        _log('🔈 Qwen-TTS 没连上（%s: %s）—— 走 edge-tts', type(e).__name__, e)
        return False

    audio = ((payload.get('output') or {}).get('audio') or {})
    raw = _decode(audio.get('data'))
    if raw is None:
        # 兜底：有些返回只给 url。多一跳，但总比没声音强。
        url = audio.get('url')
        if not url:
            _log('🔈 Qwen-TTS 返回里没有音频：%s',
                 json.dumps(payload, ensure_ascii=False)[:300])
            return False
        raw = _download(url)
        if raw is None:
            return False

    if not _looks_like_audio(raw):
        _log('🔈 Qwen-TTS 拿到的不是音频（前 16 字节：%r）', raw[:16])
        return False

    with open(out_path, 'wb') as f:
        f.write(raw)
    _last_ok = time.time()          # 保温/统计判据（与 spk_tts 对齐，留给将来用）
    _log('🔈 Qwen 合成 %.2fs（%d 字节，%.1f 字）',
         time.time() - t0, len(raw), len(text))
    return True


def _decode(data):
    """base64 → bytes。DashScope 有时带 `data:audio/wav;base64,` 前缀。"""
    if not data or not isinstance(data, str):
        return None
    if data.startswith('data:'):
        i = data.find(',')
        if i < 0:
            return None
        data = data[i + 1:]
    try:
        return base64.b64decode(data)
    except Exception:                                   # noqa: BLE001
        return None


def _download(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.read()
    except Exception as e:                              # noqa: BLE001
        _log('🔈 Qwen-TTS 音频下载失败：%s: %s', type(e).__name__, e)
        return None


def _looks_like_audio(b):
    """轻量校验：别把一段 JSON 错误页当成音频写进去，让调用方的 ffmpeg 去撞。

    wav='RIFF' / mp3='ID3' 或同步字 0xFFEx / ogg='OggS'。
    """
    if len(b) < 16:
        return False
    if b[:4] == b'RIFF' or b[:4] == b'OggS' or b[:3] == b'ID3':
        return True
    return b[0] == 0xFF and (b[1] & 0xE0) == 0xE0


_last = {'ok': 0, 'fail': 0, 'voice': _vdef.qwen_voice()}
_last_ok = 0.0


def stats():
    return {'ok': _last['ok'], 'fail': _last['fail'], 'last_ok': _last_ok,
            'key': bool(KEY), 'voice': _last['voice'], 'model': MODEL_INSTRUCT,
            'optimize': OPTIMIZE}


if __name__ == '__main__':
    # 手动试：  DASHSCOPE_API_KEY=sk-xxx python spk_tts_qwen.py '你好啊'
    import sys
    set_logger(lambda f, *a: print(f % a if a else f))
    if len(sys.argv) < 2:
        print('现役：%s' % json.dumps(stats(), ensure_ascii=False))
        print('指令：%s' % INSTR_DEFAULT)
        print('端点：%s' % URL)
        sys.exit(0)
    out = sys.argv[2] if len(sys.argv) > 2 else '/tmp/qwen_tts_test.wav'
    ok = synth(sys.argv[1], out)
    print('结果：%s → %s' % (ok, out))
    sys.exit(0 if ok else 1)
