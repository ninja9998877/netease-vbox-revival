#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tghelp.py —— 求救管道的 Telegram 收发（专给 spk_helpd 用）。

自定义测试：
    python3 tghelp.py --whoami          看借的是哪个 bot
    python3 tghelp.py --say "测试一下"   给自己推一张测试卡

★ 为什么不直接复用 lan-sentry/approve.py
  能复用的是【教训】，不是代码。它整套状态机围着 (kind, mac, ip) 转 ——
  "这台陌生设备要不要放行"，跟"音箱想求一首歌"没有一处能对齐。
  但下面三条教训一字不改地照搬了：
   ① 必须校验这条 callback 是【主人本人】按的 —— from.id 和 chat.id 都要比。
      只比 chat 不够：群里任何人都能让"消息来自这个 chat"成立。
   ② offset 必须落盘。Telegram 对【未被确认】的更新保留 24 小时，所以进程重启后
      offset 一旦落后，旧按钮会被重新投递一遍 —— 在别处那叫"重复消息"，
      在这里叫【凭空多出来一次授权】。
   ③ 同一个请求只认第一次答复。

★ 为什么要"借"一个只推不轮询的 bot
  getUpdates 是【按 bot 算的】。同一个 token 上只要有两个进程各持一个 offset
  轮询，更新就会被两边分着吃掉，用户的一次点击可能谁也收不到（而且看起来
  像"按钮没反应"）。所以：
      · 只推、从不轮询的 bot   ← ★ 这个才借得到
      · 已经有人在轮询的 bot   ← 碰了就是两边都收不到
  做法是**复用**已有的 token，而不是再复制一份 —— 单一真相源，
  哪天轮换了 token 也只有一处要改（见 `_read_var`）。
  ★ 但更干净的还是自己建一个，见下面那节。

★ 想换成专门的 bot（更干净）
  去 BotFather 建一个，然后 `HELP_BOT_TOKEN=xxx HELP_CHAT_ID=yyy` 起服务就行，
  一行代码都不用改。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, 'help')
OFFSET = os.path.join(DIR, '.tg_offset')

# ★ token 从哪儿来。留空 = **只认环境变量** `HELP_BOT_TOKEN`，不读任何文件。
#   想复用别处已有的 token，再把 `HELP_TOKEN_SRC` 指到那个源码文件去。
TOKEN_SRC = os.environ.get('HELP_TOKEN_SRC', '')
TOKEN_VAR = os.environ.get('HELP_TOKEN_VAR', 'MACRO_TG_BOT_TOKEN')
CHAT_VAR = os.environ.get('HELP_CHAT_VAR', 'MACRO_TG_CHAT_ID')

_API = 'https://api.telegram.org/bot%s/%s'

# 本机以前跑过 7890，环境里要还留着 http_proxy，这两个请求会一起死在
# "连不上 127.0.0.1"上，而报错看起来会像是 Telegram 不通。显式关掉。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_var(path, name):
    """从别人的源码里抠一个字面量出来。

    ★ 故意不 import 它：为了读一个字面量去执行别人的模块，是拿"读配置"
      换来了"跑任意代码"。正则抠一行就够，抠不到返回空，绝不猜。"""
    if not path or not name:
        return ''
    try:
        with open(path, encoding='utf-8') as f:
            txt = f.read()
    except OSError:
        return ''
    m = re.search(r'^\s*%s\s*=\s*[\'"]([^\'"]+)[\'"]' % re.escape(name), txt, re.M)
    return m.group(1).strip() if m else ''


def creds():
    """(token, chat_id)。环境变量优先，否则去 TOKEN_SRC 里抠。"""
    tok = os.environ.get('HELP_BOT_TOKEN', '').strip()
    chat = os.environ.get('HELP_CHAT_ID', '').strip()
    if TOKEN_SRC:
        tok = tok or _read_var(TOKEN_SRC, TOKEN_VAR)
        chat = chat or _read_var(TOKEN_SRC, CHAT_VAR)
    return tok, chat


# ---------------------------------------------------------------- Telegram
def _api(method, payload=None, tries=3, timeout=20):
    """调一次 Bot API。**永远不抛** —— 返回 {'ok': bool, ...}。

    推不出去不是致命的：这一轮没推成，下一轮还会推。抛出去会把守护的整轮
    循环带崩，那才是真麻烦。"""
    tok, _ = creds()
    if not tok:
        return {'ok': False, 'err': '没拿到 token（HELP_BOT_TOKEN 或 %s 里的 %s）'
                                    % (TOKEN_SRC, TOKEN_VAR)}
    url = _API % (tok, method)
    body = json.dumps(payload or {}).encode('utf-8')
    last = ''
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={'Content-Type': 'application/json'})
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            desc, wait = '', 0
            try:
                j = json.loads(e.read().decode('utf-8', 'replace'))
                desc = j.get('description') or ''
                wait = int((j.get('parameters') or {}).get('retry_after') or 0)
            except Exception:
                pass
            last = 'HTTP %s %s' % (e.code, desc)
            if e.code == 429 and wait:
                time.sleep(min(wait, 30))       # 撞限流要按它给的秒数退避，硬撞只会更久
                continue
            if 400 <= e.code < 500:
                break                            # 请求本身有问题（token/chat 错），重试一样
        except (urllib.error.URLError, OSError) as e:
            # ★ 每天 04:00 PassWall 更新规则会连带瞬断一次，报 Errno 101 / timed out
            #   是【正常现象】。照实记，别升级成告警 —— 假告警会让人开始无视真告警。
            last = '%s: %s' % (type(e).__name__, e)
        if i < tries - 1:
            time.sleep(2 ** i)
    return {'ok': False, 'err': last}


def send(text, buttons=None):
    """发一张卡片，返回 message_id（失败返回 None）。

    ★ 不设 parse_mode：请求内容里完全可能有 _ * [ 这些字符，Markdown 一解析就 400，
      而这条消息本身是最不该发失败的那条。"""
    _, chat = creds()
    payload = {'chat_id': chat, 'text': text, 'disable_web_page_preview': True}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    r = _api('sendMessage', payload)
    return (r.get('result') or {}).get('message_id') if r.get('ok') else None


def edit(msg_id, text):
    """把卡片改成"已决定"的样子 —— 手机上的记录就是这条审计线索。"""
    _, chat = creds()
    if not msg_id:
        return False
    return bool(_api('editMessageText',
                     {'chat_id': chat, 'message_id': msg_id, 'text': text,
                      'reply_markup': {'inline_keyboard': []}}).get('ok'))


def answer(cq_id, text=''):
    """必须调，否则手机上那个按钮会一直转圈（约 30 秒）。"""
    return _api('answerCallbackQuery',
                {'callback_query_id': cq_id, 'text': text[:200]})


# ---------------------------------------------------------------- offset
def _get_offset():
    """返回 int；**从没跑过返回 None**（这个区别很要紧，见 prime()）。"""
    try:
        with open(OFFSET, encoding='utf-8') as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def _set_offset(v):
    os.makedirs(DIR, exist_ok=True)
    tmp = OFFSET + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(str(int(v)))
    os.replace(tmp, OFFSET)


def prime():
    """第一次跑：把 offset 顶到"最新一条"，让积压的历史更新【全部作废】。

    ★ 不做这一步会怎样：offset=0 时 Telegram 会把【过去 24 小时】的更新一次全给你 ——
      里面包括主人以前按过的每一个按钮。逐条处理就是凭空多出来几次授权。
      lan-sentry 是靠"只认 5 分钟内的消息"绕开的；这里更干脆，一条都不要。
    ★ offset=-1 是 Telegram 的"只给我最后一条"，用它探边界，不处理它。"""
    if _get_offset() is not None:
        return
    r = _api('getUpdates', {'offset': -1, 'limit': 1, 'timeout': 0})
    last = 0
    if r.get('ok'):
        for u in (r.get('result') or []):
            last = max(last, int(u.get('update_id') or 0))
    _set_offset(last)


def authorized(from_id, chat_id):
    """★ 同意门禁唯一那把锁：这条 callback 是不是主人本人按的。

    from.id 和 chat.id 都比 —— 只比 chat 不够，群里任何人都能让
    "消息来自这个 chat"成立。"""
    _, want = creds()
    if not want:
        return False
    return str(from_id) == str(want) and str(chat_id) == str(want)


def poll(limit=20):
    """收一轮按钮点击。返回本次【新收到的、且确实是主人按的】决定列表：
        [{'qid': ..., 'ok': True/False, 'cq_id': ...}]
    ★ 不管认不认这条 callback，都要先 answer()，否则手机上按钮一直转圈；
      至于"这个 qid 是不是还有效"是调用方的事（状态机天然会拒掉重复点击）。"""
    st = _get_offset()
    if st is None:                     # 没 prime 过就先 prime，别一头撞进历史里
        prime()
        st = _get_offset() or 0
    r = _api('getUpdates', {'offset': int(st) + 1, 'timeout': 0, 'limit': limit,
                            'allowed_updates': ['callback_query']})
    if not r.get('ok'):
        return []                      # 04:00 那波或路由器重启 —— offset 一字节都不动
    out, newest = [], int(st)
    for u in (r.get('result') or []):
        # ★ offset 只在【处理完之后】推进。先推进再处理，中途崩了这条就永远丢了 ——
        #   丢一条普通消息无所谓，丢一条"主人批准了"就麻烦了。
        newest = max(newest, int(u.get('update_id') or 0))
        cq = u.get('callback_query') or {}
        cq_id = cq.get('id')
        parts = str(cq.get('data') or '').split('|')
        if len(parts) != 3 or parts[0] != 'h':
            if cq_id:
                answer(cq_id)
            continue
        frm = cq.get('from') or {}
        chat = (cq.get('message') or {}).get('chat') or {}
        if not authorized(frm.get('id'), chat.get('id')):
            # 谁都能看见卡片、谁都能按按钮。这里就是那把锁。
            answer(cq_id, '这事得主人自己定')
            continue
        answer(cq_id, '收到')
        out.append({'qid': parts[1], 'ok': parts[2] == 'ok', 'cq_id': cq_id})
    if newest != int(st):
        _set_offset(newest)
    return out


# ---------------------------------------------------------------- 自测
def main():
    import sys
    if '--whoami' in sys.argv:
        tok, chat = creds()
        print('token %s  chat %s' % ('有(%d 字符)' % len(tok) if tok else '没有',
                                     chat or '没有'))
        r = _api('getMe')
        u = r.get('result') or {}
        print('借的是 @%s 「%s」' % (u.get('username'), u.get('first_name'))
              if r.get('ok') else '✗ 拿不到身份：%s' % r.get('err'))
        print('offset', _get_offset())
        return 0
    if '--say' in sys.argv:
        i = sys.argv.index('--say')
        txt = ' '.join(sys.argv[i + 1:]) or '（空）'
        mid = send(txt, [[{'text': '✅ 干', 'callback_data': 'h|test0000-test|ok'},
                          {'text': '❌ 不干', 'callback_data': 'h|test0000-test|no'}]])
        print('推成功 msg_id=%s' % mid if mid else '✗ 推失败')
        return 0 if mid else 1
    if '--poll' in sys.argv:
        print(poll())
        return 0
    print(__doc__)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
