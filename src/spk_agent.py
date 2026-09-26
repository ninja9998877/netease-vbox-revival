#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一句话 → 大模型收敛成指令 → 音箱动作。

    python3 spk_agent.py "停一下"
    python3 spk_agent.py "大声点"       （先解析后执行）
    python3 spk_agent.py --dry "随便说点什么"   （只解析，不碰音箱）

★ 刻意不用正则做意图识别：用户的原话是"不要规则要 LLM"。
  正则能覆盖的句式永远比人说话的方式少，
  而模型错的时候是"听不懂"，正则是"听懂了却匹配错"——后者更难查。

★ 铁律：模型只负责【把话变成结构化指令】，绝不直接碰音箱。
  执行一律走 spk_ctl 里那几个动作函数 —— 白名单之外没有第二条路。
  这样模型抽风时最坏结果是"啥也没干"，不是"干了奇怪的事"。
"""
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spk_ctl as ctl
import spk_ai_dlna as dlna
import spk_voice as _voice

HERE = os.path.dirname(os.path.abspath(__file__))


def _key():
    """复用 spkbrain-macmini 的三级回退：环境 DS_KEY → spk.key → settings.json。
    ★ 不抄那五行逻辑而是加载原模块 —— 抄一份迟早跟定义处分家，而 server.py:58
      本来就是这么按路径加载它的（顶层无绑端口之类的副作用）。
    ★ 拿到后塞进 os.environ，因为 dlna.ask() 读的是 os.environ['DS_KEY']。"""
    try:
        spec = importlib.util.spec_from_file_location(
            '_brain_for_key', os.path.join(HERE, 'spkbrain-macmini.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.load_key()
    except Exception:
        return os.environ.get('DS_KEY')

PROMPT = """你是智能音箱的指令解析器。把用户说的话转成一条 JSON 指令。

可选动作（只能选这些，不许发明新的）：
  {"action":"play","src":"..."}   播放。src 是音频地址；用户没指明具体放什么就别用这个
  {"action":"pause"}              暂停 / 停一下 / 别放了（暂时）
  {"action":"resume"}             继续 / 接着放 / 播放
  {"action":"stop"}               停止 / 关掉 / 别放了
  {"action":"volume","value":N}   音量，N 是 0-100 的整数（大声点=70，小声点=30，最大=100）
  {"action":"status"}             查询：现在在放什么 / 什么状态 / 音量多少
  {"action":"unknown","reason":"..."}  跟音箱控制无关、或听不懂

只输出 JSON 本身，不要解释、不要 markdown 代码块、不要多余的字。"""


def understand(text):
    """一句话 → 指令 dict。解析失败一律回 unknown，绝不猜。"""
    raw = dlna.ask(PROMPT + '\n\n用户说：' + text)
    # ★ 模型偶尔会裹上 ```json 或加前后缀，抠出第一个 {...}
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return {'action': 'unknown', 'reason': '模型没给出 JSON：%s' % raw[:80]}
    try:
        cmd = json.loads(m.group(0))
    except Exception as e:
        return {'action': 'unknown', 'reason': 'JSON 解析失败 %s：%s' % (e, m.group(0)[:80])}
    if cmd.get('action') not in ('play', 'pause', 'resume', 'stop', 'volume', 'status', 'unknown'):
        return {'action': 'unknown', 'reason': '模型给的动作不在白名单里：%r' % cmd.get('action')}
    return cmd


def do(cmd):
    """执行指令，回一句人话。异常一律吞成结果文本 —— 上层不该因为音箱不在线就崩。"""
    a = cmd.get('action')
    try:
        if a == 'pause':
            ctl.pause()
            return '已暂停'
        if a == 'resume':
            ctl.resume()
            return '继续播放'
        if a == 'stop':
            ctl.stop()
            return '已停止'
        if a == 'volume':
            # ★ 2026-09-21 修：以前这里调 ctl.volume()，那是 UPnP 的【影子值】——
            #   返回成功、设备报的值也不变，喊"小声点"根本不会变。
            #   现在接到真正管用的杠杆：合成 mp3 时的软件增益（spk_voice）。
            #   改完不用重启任何服务，下一次合成就是新音量。
            v = int(cmd.get('value', 50))
            db = _voice.set_gain_db(_voice.db_for_level(v))
            return '音量调到 %d（合成增益 %.0f 分贝）' % (_voice.level_for_db(db), db)
        if a == 'status':
            st = ctl.status()
            uri, pos, dur = ctl.now_playing()
            db = _voice.gain_db()
            return '状态 %s%s%s | 音量 %d（合成增益 %.0f 分贝）' % (
                st,
                ('，正在放 %s' % os.path.basename(uri)) if uri else '',
                ('，%s/%s 秒' % (pos, dur)) if dur else '',
                _voice.level_for_db(db), db)
        if a == 'play':
            uri = ctl.play(cmd['src'])
            return '开始播放 %s' % uri
    except Exception as e:
        return '执行失败：%s: %s' % (type(e).__name__, e)
    return '没听懂：%s' % cmd.get('reason', '（模型也没说为什么）')


def main():
    args = sys.argv[1:]
    dry = '--dry' in args
    args = [a for a in args if a != '--dry']
    text = ' '.join(args) or '现在在放什么'
    k = _key()
    if not k:
        print('✗ 没找到大模型 key（DS_KEY / spk.key / settings.json 三处都没有）')
        return 1
    os.environ.setdefault('DS_KEY', k)
    cmd = understand(text)
    print('你问 : %s' % text)
    print('收敛 : %s' % json.dumps(cmd, ensure_ascii=False))
    if dry:
        return 0
    print('结果 : %s' % do(cmd))
    return 0


if __name__ == '__main__':
    sys.exit(main())
