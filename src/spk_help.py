#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""求救管道 —— 音箱的脑子办不了的事，上报给大脑那台机上的 Claude。

★ 为什么需要这条管道
  音箱里那个模型的全部能力边界，就是我们写死的那 11 个工具。用户说"放点欢快的音乐"，
  它打开音乐库一看只有一首慢吉他 —— 这时候它【既不该编，也不该光说做不到】。
  它该说的是"我去找找看"，然后把请求交给能力更强的那个（大脑那台机上的 Claude）。
  ★ 用户要的"你能力强、知识广、限制少"，落到代码上就是这一条通道。

★ 三方，各管一段，谁都不越界
  ① 音箱模型（spk_skills.ask_for_help）—— 判断"我办不了"，交上来，回一句"我去问问"
  ② 守护进程 —— 出方案、推给主人、等主人按按钮；★ 批准了才动
  ③ 能力更强的那个模型（`claude -p`）—— 出方案、真去干活（下载、整理、放好）

★ 本仓库带的是 ①（音箱这一侧）与 ②③ 之间那份**契约** —— 就是下面这个状态机
  加上这套文件队列。②那个守护进程**不随本仓库分发**：它是作者那台的私人后端，
  连着作者自己的推送渠道与审批按钮。你要接，照这个状态机写一个就行：
  `submit()` 交上来 → 你出方案后推到 `proposed` → 主人点头改成 `approved` →
  干完写 `done` 并 `archive()`。
  ★ 别改字段名：①③ 都按下面每个函数的 docstring 读（那两份才是契约的真身）。

★ 主人是最高决策者。没有他按那一下，② 一步都不动 —— 这条不是礼貌，是纪律：
  管道另一头连着"往家里下载东西"这件有后果的事。

★ 状态机（写在文件里，三方都读同一份，不靠内存传话）
  pending  → 音箱刚交上来
  proposed → 方案已出、已推给主人，等他按按钮
  approved → 主人点了同意，正在干
  denied   → 主人点了不同意
  done     → 干完了，结果在 result
  failed   → 干砸了，原因在 result

★ 为什么用文件队列而不是 socket / 消息队列：这三方里有【被进程唤起就退出】的
  （claude -p），有跑在 systemd 里的，还有随时重启的音箱脑子。文件是唯一一个
  重启之后还在、谁都能读、出问题能直接 cat 出来看的东西。这台机器上就三个进程，
  引一个 broker 只为了传几十个字节，不值当。
"""
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get('SPK_HELP_DIR', os.path.join(HERE, 'help'))
QUEUE = os.path.join(DIR, 'queue')
DONE = os.path.join(DIR, 'done')

# 一个请求最多活这么久（秒）。超过就没人再等它了 —— 主人可能在外面没看手机，
# 音箱那边的线程不能无限轮询下去。
TTL = int(os.environ.get('SPK_HELP_TTL', str(6 * 3600)))

MAX_OPEN = 5          # 同时挂着这么多条就不再收新的，防音箱抽风刷屏把主人淹了


def _ensure():
    os.makedirs(QUEUE, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)


def new_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-%03d' % int(time.time() * 1000 % 1000)


def _path(qid):
    """★ qid 要过一遍白名单正则：它会进路径。虽然现在 qid 都是自己生成的，
    但 decide 那条路上有机会从审批文本里解析 id —— 那种地方一旦拼进路径就是洞。"""
    if not re.match(r'^[0-9a-zA-Z\-]{8,40}$', str(qid or '')):
        raise ValueError('qid 不合法：%r' % (qid,))
    return os.path.join(QUEUE, qid + '.json')


def load(qid):
    try:
        with open(_path(qid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(d):
    """原子写。★ 不是洁癖：守护和音箱线程可能同一秒都在读写同一个文件，
    半截 JSON 会让两边同时以为"这条请求没了"。"""
    _ensure()
    p = _path(d['id'])
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return d


def submit(ask, detail=None, source='音箱', kind='music'):
    """音箱交一条请求上来。返回 (id 或 None, 给人/模型看的一句话)。

    `kind` 决定这条走哪套执行壳（求助后端里按它分岔）：
      · `'music'`（默认）—— 老路，一字不动：找曲子/下载，cwd 锁在音乐库
      · `'capability'` —— 给音箱装一样新本事（写 `ext/*.py`），cwd 是 spkbrain
    ★ 默认必须是 `music`：库里早就躺着的历史条目没有这个字段，读出来是 `None`，
      一律按老路解释才对（`helpd` 那边同样用 `.get('kind')` 兜底）。
    """
    _ensure()
    opens = all_open()
    if len(opens) >= MAX_OPEN:
        return None, ('已经挂着 %d 条没处理完的请求了，等主人回话再说。'
                      '先跟用户说"这个我得问问主人，可能要等一会儿"。' % len(opens))
    ahead = len(opens)          # 前面还排着几条（大佬只有一个，一次只跑得动一件）
    d = {
        'id': new_id(),
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'pending',
        'source': source,
        'kind': kind if kind in ('music', 'capability') else 'music',
        'ask': str(ask or '').strip()[:2000],
        'detail': detail if isinstance(detail, dict) else {},
        'plan': '',
        'progress': [],
        'result': '',
        # ★ 念给主人听的那一句（`finish(announce=…)` 填）。播报只认它，不认 result。
        'announce': '',
        # ★★ 两条线各自记账："这条我报过主人了没有"。
        #   为什么要分开记：装能力是**音箱的本事**（`ext/*.py` 跑在 spk-ear 里），
        #   所以那个壳发起的也要给音箱报一份（主人 2026-09-23 原话）；
        #   而两条线的出口完全不同（音箱 0x601 / 那个壳自己的音频通道），
        #   谁报过谁记账，互不顶替 —— 否则报了那条线就漏了音箱。
        'told_speaker_at': '',
        'told_phone_at': '',
        'notice_id': None,
        'decision': '',
        'decided_at': '',
    }
    save(d)
    if ahead:
        # ★★ 排上队 ≠ 马上办。原来这里不管排没排队，一律回"请求已交给主人那边了…
        #   跟用户说一句我去问问" —— 排在别人后面时，**那是句假话**。
        #   本项目为同类的假话付过代价（`set_alarm` 回"定上了"而根本没有守护进程在跑），
        #   所以这里必须把实情交给大脑，让它照实说。
        return d['id'], (
            '★ 排上了（编号 %s），**但前面还排着 %d 条没完** —— 大佬一次只跑得动一件。'
            '所以【绝不许】跟用户说"我这就去问"。要**如实**说：他手上还有没忙完的，'
            '你这个我记下了，等他完事我马上跟他说。' % (d['id'], ahead))
    return d['id'], '请求已交给主人那边了（编号 %s），跟用户说一句"我去问问"' % d['id']


def set_status(qid, status, **kw):
    d = load(qid)
    if not d:
        return None
    d['status'] = status
    for k, v in kw.items():
        d[k] = v
    return save(d)


def add_progress(qid, text):
    """记一条进度。★ 这是给【音箱】看的 —— 它会把这些话讲给用户听，
    所以写成能直接念的人话（"找到三首了，正在下载"），别写成日志。"""
    d = load(qid)
    if not d:
        return None
    d.setdefault('progress', []).append(
        {'at': time.strftime('%H:%M:%S'), 'text': str(text)[:300]})
    return save(d)


def finish(qid, result, ok=True, announce=''):
    """收工。

    `announce` = 【念给主人听的那一句】，跟 `result` 分开存。
    ★ 为什么要分开：`result` 里混着技术诊断（`cap_verdict` 的判定说明、
      文件清单这类），念出来主人只会莫名其妙 —— 而播报那张嘴要的是**一句人话**。
      音箱线 / 那个壳播报时**只读 `announce`**，读不到就不播（宁可不说，别念诊断）。
    """
    d = load(qid)
    if not d:
        return None
    d['status'] = 'done' if ok else 'failed'
    d['result'] = str(result)[:4000]
    if announce:
        d['announce'] = str(announce)[:500]
    d['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    d = save(d)
    return d


def archive(qid):
    """收进 done/ —— 队列目录只留还活着的，cat 一眼就能看明白。"""
    p = _path(qid)
    if not os.path.exists(p):
        return False
    _ensure()
    os.replace(p, os.path.join(DONE, os.path.basename(p)))
    return True


def all_open():
    """还没结束的请求（按时间排）。"""
    _ensure()
    out = []
    for n in sorted(os.listdir(QUEUE)):
        if not n.endswith('.json'):
            continue
        try:
            with open(os.path.join(QUEUE, n)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get('status') not in ('done', 'failed', 'denied'):
            out.append(d)
    return out


def recent(n=10):
    """最近处理过的（含已归档的），给 cmd_status 和音箱查"上次那事儿怎么样了"用。"""
    _ensure()
    rows = []
    for sub in (QUEUE, DONE):
        for name in os.listdir(sub):
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(sub, name)) as f:
                    rows.append(json.load(f))
            except (OSError, ValueError):
                continue
    rows.sort(key=lambda d: d.get('created', ''), reverse=True)
    return rows[:n]


def expired(d, now=None):
    try:
        t = time.mktime(time.strptime(d.get('created', ''), '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return False
    return (now or time.time()) - t > TTL


# ------------------------------------------------- 播报记账（两条线各自一份）
def _entry_path(qid):
    """条目在哪儿 —— `queue/` 里没有就去 `done/` 找。

    ★ 归档之后还要记账（求助后端装完就 `archive()`），所以两个地方都得认。
    ★ qid 仍然走 `_path()` 过一遍白名单正则 —— 它会进路径，没有例外。
    """
    p = _path(qid)
    if os.path.exists(p):
        return p
    p2 = os.path.join(DONE, os.path.basename(p))
    return p2 if os.path.exists(p2) else None


def mark_told(qid, who):
    """记一笔"这条我报过主人了"。`who` ∈ `'speaker'` / `'phone'`。

    ★ 为什么这笔记账非有不可：装能力是**音箱的本事**（`ext/*.py` 跑在 spk-ear 里），
      所以**两条线都要报一遍**（主人 2026-09-23 定：另一条线发起的能力学习，给音箱也发一份），
      而两条线的出口完全不同。没有记账就会"音箱报了三遍"或"那条线报了音箱不知道"。
    ★ 为什么落在文件上而不是内存里：`spk_ear` 和那个壳是两个进程、都会重启，
      内存里记 = 重启之后重复播报。
    """
    if who not in ('speaker', 'phone'):
        return False
    p = _entry_path(qid)
    if not p:
        return False
    try:
        with open(p) as f:
            d = json.load(f)
        d['told_%s_at' % who] = time.strftime('%Y-%m-%d %H:%M:%S')
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except (OSError, ValueError):
        return False


def told(qid, who):
    """这条报过没有。

    ★ 读不出来一律**当报过了** —— 宁可漏报一次，也别反复烦主人。
      真想知道状态的还有 `spk_skills._help_note()` 那条路兜着（主人问就答得出）。
    """
    p = _entry_path(qid)
    if not p:
        return True
    try:
        with open(p) as f:
            return bool(json.load(f).get('told_%s_at' % who))
    except (OSError, ValueError):
        return True


def pending_announce(who, scan=8, window=6 * 3600):
    """有哪些"装成了、还没跟我这条线报过"的。

    ★ 装完就 `archive()` ⇒ 主要看 `done/`。
    ★ 只扫**尾巴几个**（文件名是时间戳开头 ⇒ 字典序就是时间序）：这个函数会被
      两条线每隔几秒调一次，扫全目录迟早变成瓶颈。
    ★ `window` 另算（不是 `TTL`）：一条六小时前的旧闻不该现在才响。
    """
    if who not in ('speaker', 'phone'):
        return []
    _ensure()
    try:
        names = sorted(n for n in os.listdir(DONE) if n.endswith('.json'))[-scan:]
    except OSError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(DONE, n)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        # ★ 失败也要报（`failed` 照样进）—— 装砸了却不吭声，主人就会像
        #   2026-09-23 那条线上一样干等。话术由播报那边按 status 分岔。
        if d.get('status') not in ('done', 'failed') or d.get('kind') != 'capability':
            continue
        if not d.get('announce') or d.get('told_%s_at' % who):
            continue
        if expired(d) or (time.time() - _created_ts(d)) > window:
            continue
        out.append(d)
    return out


def _created_ts(d):
    try:
        return time.mktime(time.strptime(d.get('created', ''), '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return time.time()


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--new':
        qid, msg = submit(' '.join(sys.argv[2:]) or '（测试请求）')
        print(qid, msg)
    elif len(sys.argv) > 1 and sys.argv[1] == '--open':
        for d in all_open():
            print('%-20s %-9s %s' % (d['id'], d['status'], d['ask'][:60]))
        print('（没有挂着的请求）' if not all_open() else '')
    elif len(sys.argv) > 1 and sys.argv[1] == '--all':
        for d in recent(20):
            print('%-20s %-9s %s → %s' % (d['id'], d['status'],
                                          d['ask'][:40], (d.get('result') or '')[:40]))
    else:
        print(__doc__)
