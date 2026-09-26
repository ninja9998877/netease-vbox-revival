#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_memory.py —— 会话结束后固化下来、下次还认得主人。

主人 2026-09-21 的原话：
    「结束后就把本次会话固化记忆落盘以备后续使用 当然落盘的要大模型挑重要的记」
    「第三个你可以自己按最主流的方案设计」

★ 为什么不能"把整段对话存下来下次塞回去"
  那是最直觉的做法，也是最快烂掉的做法：聊十天就有十天的流水，token 撑爆、
  重点被淹，而且**越老的越占地方**——它恰恰是最不重要的。
  主流做法（mem0 / Letta 那一派）分三层，这里照做：

    ① **会话层**（L1）—— 当下这一段说了什么。活在内存里，收工即弃。
       在 `spk_session.py`，已经做好了。
    ② **核心记忆**（L2）—— 从会话里**挑出来的、值得长期记住的事实**。
       常驻提示词，所以必须**短**（这里封 700 字）。就是本模块管的。
    ③ **归档**（L3）—— 每一次会话的原文，append-only 的 jsonl。
       **永不进提示词**，是给人看的、给以后翻账用的。

  「挑重要的记」这件事本身交给大模型（`extract()`），但**它挑出来的东西要过
  一道 key 覆盖**：同一个 key 只留最新一条。没有这道，问三次"我喜欢喝什么"
  就会攒三条互相矛盾的记忆，而且模型下次看到三条会自己编一个折中答案。

  ★ 还有第 ④ 件事（2026-09-22 深夜加）：**外面（agent）只能投队列**。
    两个 json 的写者永远只有音箱一个 —— 投进 `pending.jsonl`，收工时并入。
    见 `drain_pending()`。读那一侧是 `mem_view.py`、投这一侧是 `mem_put.py`。

★ 铁律：**它绝不在主链路上添乱**。收工固化跑在后台、全部 try 包住、
  失败只写日志。记不住顶多是"下次不认得"，而卡住嗓子是"这次就不理人"——
  前者可忍，后者不可忍。
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ★ `SPK_MEM_DIR` 是给【离线测试】用的出口：写死 HERE/mem 的话，一测注册/记忆路径就
#   真的写进册子，那就没人敢跑第二遍。`spk_speaker.DIR` 认同一个变量，两边必须一致。
DIR = os.environ.get('SPK_MEM_DIR') or os.path.join(HERE, 'mem')
ARCHIVE = os.path.join(DIR, 'archive.jsonl')     # L3：每次会话原文，只追加
FACTS = os.path.join(DIR, 'facts.json')          # L2：挑出来的核心记忆
# ★ agent（大脑那台机上的 Claude）投进来的家庭事实，排队等收工并入 —— 见 drain_pending()。
#   两个 json 的**唯一写者必须只有音箱自己**，所以外面只能往这条队列里投。
PENDING = os.path.join(DIR, 'pending.jsonl')
PENDING_LOCK = os.path.join(DIR, 'pending.lock')
# ★★ facts.json 的【独立】锁（2026-09-23 加）。非有不可的理由：现在有**两个进程**
#   会在收工时抽记忆 —— 音箱（spk-ear）和电话线那套。而 `remember()` 是
#   【读全部 → 改 → 写回】，两头同时来就是"后写的把先写的整段覆盖"，
#   而且**两边都不会报错**（归档写了、facts 少了一条，没人会去查）。
#   ★★★ 绝不能复用 `PENDING_LOCK`：`drain_pending()` 持着它、并在里面调 `remember()`，
#     同一进程里对同一个文件再 `flock(LOCK_EX)` 会**自死锁**（等自己放锁，永远等下去）。
FACTS_LOCK = os.path.join(DIR, 'facts.lock')
MAX_PENDING = int(os.environ.get('SPK_MEM_PENDING_MAX', '500'))

BRIEF_CHARS = int(os.environ.get('SPK_MEM_CHARS', '700'))    # 进提示词的字数上限
MAX_FACTS = int(os.environ.get('SPK_MEM_FACTS', '60'))       # 最多留几条
MIN_CHARS = int(os.environ.get('SPK_MEM_MIN', '12'))         # 会话太短不值得固化
# ★★ 每条事实最多留几种【被顶掉的旧说法】（2026-09-24 加，见 `remember()` 的失效不删除）。
#   取 4 是**跟档案柜的 `VALS_MAX` 对齐** —— 同一个取舍，不要两处两套数字。
#   为什么封顶而不是"只增不删"：主人反复改口会被撑爆（改 20 次 20 条），
#   而最老的那几种恰恰是他早改过的。留最近几条，够回答"以前是什么"。
MAX_PREV = int(os.environ.get('SPK_MEM_PREV', '4'))

EXTRACT_SYSTEM = """你在替一只智能音箱整理它跟主人的一段对话。

你的任务：从对话里挑出【以后还用得上】的信息，别的全扔掉。

要挑的：
· 主人本人的习惯、偏好、住处安排（几点起床、爱听什么、常去哪儿）
· 主人明确交代过的、以后还会生效的事（"以后叫我起床都提前十分钟"）
· 主人家里固定的人和物（谁是谁、哪台设备在哪个房间）
· 主人纠正过音箱的地方（"别叫我老板"）

不要挑的：
· 这一次的一次性操作（"把音量调到 38"、"现在放什么"）—— 过去就过去了
· 任何一次性的时间（"今天"、"刚才"、"明天早上七点"）
· 音箱自己说的话、客套、以及"主人问了什么"这类没有信息量的记录
· 密码、账号、身份证号这类敏感信息 —— 一个字都不要记

★ 宁可少挑，也别凑数。一段对话挑不出东西是【很正常】的，那就交空数组。
  记错一条会一直错下去，而漏一条只是下次再问一遍。

★★ 只输出 JSON，不要任何解释、不要 markdown 代码块：
[{"key": "四个字以内的短标签", "value": "一句话的事实"}]
或
[]
"""

# ★★ 只有在【这段对话真的标了说话人】时才追加这一节（判据见 `extract(attribute=)`）。
#   为什么不能无条件要 who —— 这是个会**静默废掉整个记忆功能**的坑：
#   认人关着（`SPK_WHO=0`）时，账本里每句用户消息都渲染成「主人：…」，
#   模型就会老老实实填 `who="主人"` ⇒ 记忆全被存进 `主人/` 命名空间，
#   而认人关着时提示词取的是 `brief(None)`（**只喂公共**）⇒ **一条都读不出来，
#   而且不报错**。判据必须是数据（"这段对话里真有 who 字段吗"），不是开关 ——
#   开关和实际行为分头走，正是这个项目反复栽的那种坑。
WHO_ADDENDUM = """
★★ 这段对话里每句话前头写着**谁说的**，多输出一格 `who` 照抄那个名字：
  · 说这句的人叫「某人」就填 "某人"，叫「主人」就填 "主人"
  · 说话人没认出来（前头就写着「主人」，但这句明显是别人说的）⇒ 留空
  · ★ 拿不准就留空。**留空只是"当成全家公共的"，填错了是把一个人的私事
    记到另一个人头上** —— 后者不可见、也没人会去查，比前者糟得多。

只输出 JSON：
[{"who": "某人", "key": "四个字以内的短标签", "value": "一句话的事实"}]
或
[]
"""


def log(fmt, *a):
    print('%s  [mem] %s' % (time.strftime('%H:%M:%S'), fmt % a if a else fmt),
          flush=True)


# ---------------------------------------------------------------- L3 归档
def archive(session):
    """把一次会话的原文追加到 jsonl。★ 只追加、不改写 ——
    归档的价值就在于它是一本流水账，能被改写的东西就不是账了。"""
    try:
        os.makedirs(DIR, exist_ok=True)
        row = {'opened': session.opened, 'closed': time.time(),
               'why': session.why, 'secs': round(time.time() - session.opened, 1),
               'msgs': session.transcript()}
        with open(ARCHIVE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        return True
    except OSError as ex:
        log('✗ 归档失败：%s', ex)
        return False


# ---------------------------------------------------------------- L2 读写
def _load():
    try:
        with open(FACTS, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = FACTS + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FACTS)
        return True
    except OSError as ex:
        log('✗ 写记忆失败：%s', ex)
        return False


def _clean_who(who):
    """把"现在说话的人"洗成一个能当 key 前缀用的标识。洗不干净 ⇒ `''`（**当公共**）。

    ★ 三条硬规矩，缺一条就会串台：
      ① **不许含 `/`** —— 它就是命名空间的分隔符，名字里带一个就能把别人的记忆串过来。
      ② 不许含空白、不许超长 —— 跟 `spk_speaker._clean_name` 同一套（名字来自册子，
         而它同时还是**提示词的注入面**）。
      ③ 洗不干净时**回公共**，不猜、也不拒 —— 记忆宁可少一层归属，不可归错人。
    """
    w = str(who or '').strip()
    if not w or len(w) > 12 or '/' in w or any(c.isspace() for c in w):
        return ''
    return w


def _norm(k, v):
    """一条事实的键/值规范化 + 长度闸门。洗不干净 ⇒ 回 `('', '')`（当【记不成】）。

    ★★ 为什么单独抠出来：`remember()` 写的时候用它，队列的**终态判据**
      （`_holds`）读的时候也要用它 —— 两处各写一遍必然漂移，
      而漂移的表现是"明明并进去了，却每次都判成没并进去"，**不报错**，
      只是队列越积越多。判据同源是这里唯一的要求。
    """
    k = ' '.join(str(k or '').split()).replace('/', '')[:24]
    v = ' '.join(str(v or '').split())[:120]
    # ★ 短于 MIN_CHARS//3（默认 4 字）的值会被静默丢掉 ——「美短」就正好卡在门外。
    #   这是事实层的既有闸门，不在这儿放宽；要记短东西请走档案柜的 attr。
    if not k or len(v) < MIN_CHARS // 3:
        return '', ''
    return k, v


def remember(pairs, who=''):
    """把 [(key, value)] 并进核心记忆。★ 同 key 覆盖旧的（supersede）——
    这是这张表不烂掉的唯一原因，也是"偏好变了"能被记住的机制。

    ★★ `who` = 说话的人（第 3 步加）。**key 会加上 `who/` 前缀**，理由是一句大白话：
      主人说"我口味清淡"、某人说"我口味重" —— 而字典里 `d['口味']` **只能有一条**，
      没有前缀就是后来的把先来的**整个覆盖掉**，而且覆盖之后 `_load()` 返回的字典
      **看不出这是两个人**。有前缀才谈得上"各存各的"。
      `who=''` ⇒ 公共（**旧数据零迁移**：它们本来就没有前缀、也没有 `who` 字段）。
    """
    if not pairs:
        return 0
    w = _clean_who(who)
    # ★★ 【读-改-写】整段持锁（2026-09-23）。锁的边界就是这条流水线本身，
    #   不能只锁 `_save` —— 那样两个进程仍会各自读到同一份旧状态，然后依次覆盖。
    with _facts_flock():
        d = _load()
        now = time.time()
        n = 0
        for k, v in pairs:
            # ★★ key 里必须去掉 `/`：它是命名空间分隔符，模型取的 key 带一个就会让
            #   **一条公共记忆看起来像"某人名下的"** ⇒ `forget_who` 会连它一起删掉。
            #   （过滤本身是按 `who` 字段走的，所以不会串台；但删除是按前缀走的。）
            #   ★ 规范化与长度闸门都在 `_norm` 一处 （← 队列的"终态判据"要跟它一致）。
            k, v = _norm(k, v)
            if not k:
                continue
            full = '%s/%s' % (w, k) if w else k
            old = d.get(full)
            if old and old.get('value') == v:
                old['at'] = now          # 又说了一遍同一件事 —— 更新鲜度，不加条数
                continue
            # ★ 公共记录【不写】`who` 字段（保持跟旧数据一模一样的形态，`brief` 认它作公共）
            new = {'value': v, 'at': now, 'who': w} if w else {'value': v, 'at': now}
            # ★★★ 2026-09-24：被顶掉的那条**不再抹掉，压进 `prev` 留着**（失效不删除）。
            #
            #   为什么改：原来是纯覆盖 —— `d[full] = {…}` 一写，旧值**一个字都不剩**。
            #   后果不是"记错了"，是**没法发现记错了**：「主人改喝美式了」记进来之后，
            #   "他以前喝拿铁"这件事在整个系统里**没有任何痕迹**，连人都查不出来。
            #   而档案柜（`spk_entities.set_attr`）对同一个问题**早就是"两条都留"**了
            #   —— 同一个代码库里两套政策，这一处是把它拉齐。
            #
            #   ★★ 形状是**纯增量**，这是它能安全落地的全部原因：
            #     `value`/`at`/`who` 三个键的含义**一个字节都没变** ⇒
            #     `brief()`（按 at 倒序喂提示词）、`_holds()`（拿 value 判终态）、
            #     `mem_view`、`--show` **全都不用改**；老记录没有 `prev` ⇒ 零迁移。
            #     `prev` 是"被顶掉的历史"，**没有任何一条读路径会自动碰它** ——
            #     想看得显式调 `history()`（`--history <key>`）。
            #
            #   ★ 它【不是】完整的双时间：两侧都只有 transaction-time（`from` 是
            #     "旧值什么时候记下的"、`to` 是"什么时候被顶掉的"），**没有 valid-time**
            #     （这事在世界上什么时候成立）—— 因为 `at` 本来就只记"何时记下"。
            #     要 valid-time 得先动抽取提示词，那是另一件事、要主人拍板。
            if old and old.get('value'):
                prev = list(old.get('prev') or [])
                prev.append({'value': old['value'],
                             'from': old.get('at') or 0, 'to': now})
                new['prev'] = prev[-MAX_PREV:]
            d[full] = new
            n += 1
        # ★ 超上限就丢最旧的。可预测、无副作用；不做"让模型自己合并"那一步 ——
        #   那要再调一次模型，而收益是省几条位置，不值当。
        #   ★★ 2026-09-24：**丢的时候要说话**。原来是纯 `d.pop`，一个字都不写 ——
        #      于是"一条记忆被挤掉了"在所有地方都没有痕迹，`at` 最老的那条
        #      （往往是"家里有谁、住哪儿"这种最早的、也最要紧的）就这么没了，
        #      而现象只是"它怎么不记得了"。`--health` 数得出来，但得先有日志
        #      才能知道**是什么时候、丢的是哪条**。
        if len(d) > MAX_FACTS:
            for dk in sorted(d, key=lambda x: d[x].get('at', 0))[:len(d) - MAX_FACTS]:
                log('⚠ 核心记忆满了（上限 %d），挤掉最旧的一条：[%s] %s',
                    MAX_FACTS, dk, (d.get(dk) or {}).get('value', ''))
                d.pop(dk, None)
        _save(d)
    return n


def brief(who=None):
    """核心记忆 → 提示词里那一小段。★ 封顶 BRIEF_CHARS 字：它常驻每一次调用，
    长了就是在给每一句话加钱，而且会把真正要紧的规矩挤下去。

    ★ `who` = 现在说话的人（主人拍板："只喂公共 + 本人"）。
      ★★ **判据是记录里的 `who` 字段，不是 key 上的 `名字/` 前缀。**
        前缀只是为了"两个人的同名 key 能各存各的"（字典里 key 唯一）；
        判断归属一律看字段 —— 这样模型取的 key 里万一混进 `/` 也不会串台，
        而且**旧数据（没有字段、也没有前缀）天然就是公共**，零迁移。
        两侧的规矩必须一致：写方 `remember` 也只在 `who` 非空时才写字段。

    ★★★ `who=None` 是【公共】，不是【全部】。—— 这是本次改的一处，理由：
      `who=None` 有四条路走到这儿，**四条要的都是"只喂公共"**：
        · 空册子 / 认人关着 / 太短 / 太安静 / 夜间（`skip`）
        · 判成生人（`stranger`，主人拍板"只喂公共"）
        · 技术性失败认不出来（主人拍板同上）
        · 问够了之后（`unknown`）
      一句"不过滤"就把上面四条全部变成"把别人的私有记忆喂给一个陌生人"。
      ⇒ 默认值必须取【最小权限】：拿不准就不给。要看全部请走 `brief_all()`。
    """
    d = _load()
    if not d:
        return ''
    # 公共（没有 who 的老记录，全是没有归属的公共事实）+ 他本人名下的。
    # 别人的【一条都不给】—— 这是主人拍板的边界，不是优化。
    if who:
        def keep(v):
            return v.get('who') in (None, '', who)
    else:
        def keep(v):
            return v.get('who') in (None, '')
    d = {k: v for k, v in d.items() if keep(v)}
    rows = sorted(d.items(), key=lambda kv: kv[1].get('at', 0), reverse=True)
    out, used = [], 0
    for k, v in rows:
        # ★★ 2026-09-24：`v['value']` → `v.get('value')` + 跳过空值。原来是**硬下标** ——
        #   一条缺 `value` 的坏记录会让 `brief()` 整个抛 KeyError，而唯一的调用方
        #   （`spk_skills.py`）是 `try/except: pass` 包住的 ⇒ 结果是
        #   **整张记忆表一条都不喂，而且不报错**。一条坏记录毒死全表，
        #   现象只是"它今天好像不记得我了"。
        #   ⇒ 这里跳过它（其余照常喂），坏的交给 `health()` 去报 —— 自检要能报，
        #     前提是它自己先不炸。
        val = v.get('value')
        if not val:
            continue
        line = '· %s' % val
        if used + len(line) > BRIEF_CHARS:
            break
        out.append(line)
        used += len(line)
    if not out:
        return ''
    # ★ 表头【不嵌 `who`】。这一段是要进提示词的，而这个名字来自册子的 key ——
    #   要嵌就得在这儿再洗一次，而"谁在说话"本来就是 `_who_note` 的活（它洗过）。
    #   同一件事说两遍，只会多一个注入面和一个会跟人说法打架的第二处真话来源。
    head = '【你记得的事】' if who else '【你记得关于主人的事】'
    return head + '\n' + '\n'.join(out) + '\n'


def brief_all():
    """【不过滤】的版本 —— 只给 `--show` 那种"人看"的场合，**绝不许进提示词**。

    ★ 留一个口子，是为了让"过滤"这件事本身可验证：`brief(None)` 只给公共，
      要是没有这个函数，就没法在测试里断言"到底漏掉了哪几条"。
    """
    d = _load()
    if not d:
        return ''
    rows = sorted(d.items(), key=lambda kv: kv[1].get('at', 0), reverse=True)
    # ★ 同 `brief()`：坏记录跳过，别让它把整份输出带崩（那会让"看全部"变成"看不了"）
    return '【全部记忆】\n' + '\n'.join(
        '· %s' % v.get('value') for _k, v in rows if v.get('value')) + '\n'


def history(key):
    """某一条事实的**说法变迁**。返回 `[(值, 起, 止)]`，现在生效的那条 `止=None`。

    ★★ 这是"失效不删除"唯一的**出口** —— `remember()` 把被顶掉的值压进 `prev`
      之后，除了这里没有任何一条读路径会碰它（`brief` 永远不会把旧说法喂给模型，
      那正是要的：**旧说法不该进提示词，但也不该消失**）。

    ★ 返回空列表有两种可能，调用方要分得清：**没这条 key**，和**这条 key 从没改过口**。
      后者也会返回 1 行（就是当前值）—— 所以 `len(rows) == 1` 是"一直没变过"，
      不是"查不到"。查不到请用 `_load()` 判。
    """
    rec = _load().get(key)
    if not isinstance(rec, dict):
        return []
    out = [(p.get('value'), p.get('from') or 0, p.get('to'))
           for p in (rec.get('prev') or []) if isinstance(p, dict)]
    out.append((rec.get('value'), rec.get('at') or 0, None))
    return out


def forget_who(who):
    """删掉某个人名下的【全部】记忆（公共的一条不动）。返回删了几条。

    ★★ 这是"删掉某个人"的【一半】。另一半在 `spk_entities.forget()`（档案里的那个实体）。
      主人说"删掉某人"时想的是**这个人**，不是某一个文件 —— 调用方必须两样都做，
      只做一半的后果是：档案里人没了，可他一说话提示词里还念着"你记得她口味重"。
    """
    w = _clean_who(who)
    if not w:
        return 0
    d = _load()
    pre = w + '/'
    gone = [k for k in d if k.startswith(pre)]
    for k in gone:
        d.pop(k, None)
    if gone:
        _save(d)
    return len(gone)


# ------------------------------------------------------- 外界投递（agent 的唯一写入口）
#  ★★★★★ 主人 2026-09-22 深夜拍板的第 3 条：**家庭事实只有一条写路径**。
#  音箱自己写（收工抽取 + 对话中的 remember/add/set_attr 工具）；agent 想写
#  ⇒ 投进 `pending.jsonl` 排队，由这里并入。两个 json 的写者永远只有音箱一个，
#  于是 `remember` 的 supersede、`MAX_FACTS` 淘汰、`add()` 的别名红线，
#  全在**写的那一刻**判 —— 一个绕不过去的写者，才谈得上"这几条规矩成立"。
#
#  ★ 队列的四种行（kind）：
#      {"kind":"fact",   "who":"", "key":"…", "value":"…"}
#      {"kind":"entity", "name":"小白", "type":"猫", "aka":["我的猫"], "who":""}
#      {"kind":"attr",   "name":"小白", "attr":"品种", "value":"美短", "who":""}
#    ★ 没有 `forget`：删家庭记忆是危险动作，agent 不该有能力排一条删除进队列
#      （要删请人来说，由音箱的工具当场执行）。
#    ★ 行里**不带时间戳**：`at` 记的是"音箱什么时候把它记下来的"，不是"这话什么时候
#      说的"。隔几小时并入就写几小时后的时刻 —— 那正是它该有的含义。
#      多一个字段就多一条会错的路径，而收益只是显示上差几小时。
@contextlib.contextmanager
def _flock():
    """队列的排他锁。★★ 非有不可：并入是【读全部 → 逐条写 → 重写队列】，
    而投递是【追加一行】—— 没有锁的话，在"读完"和"重写"之间投进来的那一条
    **会被重写静默覆盖**，而投递方以为成功了。（两端都在大脑那台机上，flock 够用。）"""
    os.makedirs(DIR, exist_ok=True)
    fd = os.open(PENDING_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _facts_flock():
    """`facts.json` 的排他锁。★★ 与 `_flock()` 是**两个不同的文件**，这是刻意的：
    并入队列那条路（`drain_pending` → `_apply` → `remember`）会在**持有
    `PENDING_LOCK` 的同时**要这把锁。共用一个文件就是自己等自己。

    ★ 判据：这把锁只包住 `remember()` 的【读-改-写】，里面不许再调任何会要它的东西
      （`_load` / `_norm` / `_save` 都是纯的），否则同样是自死锁。"""
    os.makedirs(DIR, exist_ok=True)
    fd = os.open(FACTS_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _lines():
    """队列的原始行（跳过空行）。★ 返回【原文】不只是解析结果：读不懂的行
    要原样写回去，不能让一次并入把它悄悄吃掉。"""
    try:
        with open(PENDING, encoding='utf-8') as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def _write_lines(lines):
    tmp = PENDING + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(''.join(ln.rstrip('\n') + '\n' for ln in lines))
    os.replace(tmp, PENDING)


def pending_read():
    """队列内容（给 `mem_put --list` / `mem_view` 显示用）。"""
    out = []
    for ln in _lines():
        try:
            it = json.loads(ln)
            out.append(it if isinstance(it, dict) else None)
        except ValueError:
            out.append(None)
    return out


def pending_add(items):
    """投几条家庭事实进队列。返回 `(ok, 给 agent 看的一句话)`。

    ★ **投递时就把能查的错查掉** —— 否则错要等到下一次收工并入才暴露，
      而那时候投递方早走了、没人看着。队列是异步的，异步的错必须往前挪：
        · fact 太短（`_norm` 会丢掉它）⇒ 当场说"改用 attr"
        · entity 已经记着了 ⇒ 当场说"要补信息请用 attr"
        · attr 挂的东西不存在 ⇒ 当场说"先投 entity"
      这三条正好覆盖了 agent 最容易投错的三种情况。
    """
    import spk_entities as ent
    if not items:
        return False, '没说要记什么'
    have = _lines()
    if len(have) + len(items) > MAX_PENDING:
        return False, '队列已积 %d 条（上限 %d）—— 先让音箱收个工把它们并进去' % (
            len(have), MAX_PENDING)
    seen = set(have)
    add = []
    for it in items:
        kind = it.get('kind')
        if kind == 'fact':
            w = _clean_who(it.get('who'))
            k, v = _norm(it.get('key'), it.get('value'))
            if not k:
                return False, ('这条 fact 记不成：键是空的，或者值太短'
                               '（少于 %d 字会被核心记忆丢掉）。'
                               '短东西请用 attr 挂到档案上。' % (MIN_CHARS // 3))
            it = {'kind': 'fact', 'who': w, 'key': k, 'value': v}
        elif kind == 'entity':
            name = ent.clean(it.get('name'))
            if not name:
                return False, '这个名字不合适（空的、带空格、或超过 %d 字）' % ent.NAME_MAX
            if ent.find(name):
                return False, ('「%s」档案里已经有了 —— 要补新信息请用 attr（走已有条目），'
                               '别新建。' % name)
            it = {'kind': 'entity', 'name': name, 'type': ent.clean(it.get('type'),
                                                                   ent.ATTR_MAX),
                  'aka': [a for a in (it.get('aka') or []) if a],
                  'who': _clean_who(it.get('who'))}
        elif kind == 'attr':
            name = ent.clean(it.get('name'))
            if not ent.find(name):
                return False, ('档案里还没有「%s」—— 先投一条 entity 把它建起来，'
                               '再记它的信息。' % (it.get('name') or ''))
            if not ent.clean(it.get('attr'), ent.ATTR_MAX) or not str(it.get('value') or '').strip():
                return False, '这条 attr 记不成（属性名或内容不合适）'
            it = {'kind': 'attr', 'name': name,
                  'attr': ent.clean(it.get('attr'), ent.ATTR_MAX),
                  'value': str(it.get('value')).strip(),
                  'who': _clean_who(it.get('who'))}
        else:
            return False, '不认识的类型「%s」—— 只有 fact / entity / attr 三种' % kind
        ln = json.dumps(it, ensure_ascii=False)
        if ln in seen:                      # ★ 重投同一条不重复排队（重试很常见）
            continue
        seen.add(ln)
        add.append(ln)
    if not add:
        return True, '这几条队列里已经有了，没重复投'
    with _flock():
        _write_lines(have + add)
    return True, '投了 %d 条进队列，音箱下次收工时并进去' % len(add)


def _holds(it):
    """这条排队项**想要的状态**现在成立了吗 —— 成立就算落地了。返回 `(bool, 说明)`。

    ★★ **说明文字的约定（踩过坑）**：`ok=True` 时说明**必须是空的**。
      调用方把"ok 且带说明"读作「并掉了、但这条注定不落地」并记进日志 ——
      所以正常落地/正常重放若带一句话，就会**每次重放都刷一条假警告**
      （`_holds` 一开始就是这么写的：`return 条件, '核心记忆里没有这条'`，
      两个分支共用一句说明 ⇒ 成功那次也在报"没有这条"）。

    ★★ 判据是"终态"而不是"这次调用的返回值"，两个理由都踩过：
      ① `add()` 碰到已存在的实体会**返回失败**（那是它的红线：不许静默合并），
         可终态本来就是我们想要的 —— 照返回值判，会把一条早已落地的记录
         永远留在队列里，每次收工重试一遍。**崩溃后重放、agent 重投都会走到这儿。**
      ② `set_attr` 的"这个属性原来记的是别的，两条都留着了"同理。
      判终态 ⇒ **重放安全（幂等）**，而且不依赖任何一句提示文字的措辞 ——
      靠措辞判的那种，改一个字就静默失效。
    """
    import spk_entities as ent
    kind = it.get('kind')
    if kind == 'fact':
        w = _clean_who(it.get('who'))
        k, v = _norm(it.get('key'), it.get('value'))
        if not k:
            # ★ 过不了长度闸门 ⇒ 它**注定**记不成（重试一万次也一样）⇒ 划掉，
            #   但**带回一句说明**：drain 会把 ok 带说明的当"并掉了但没落地"记日志。
            #   留它在队列里等于留一个永远并不过去的堵塞，而堵塞比丢一条更糟。
            return True, ('这条 fact 注定记不成：值太短（少于 %d 字，核心记忆会丢掉它）'
                          '—— 短东西该走 attr' % (MIN_CHARS // 3))
        full = '%s/%s' % (w, k) if w else k
        if (_load().get(full) or {}).get('value') == v:
            return True, ''                 # ★ 落地 ⇒ 说明**必须为空**（见函数头的约定）
        return False, '核心记忆里没有这条'
    if kind == 'entity':
        # ★ ok 一律回空说明：非空的说明专留给"并掉了、但那条注定不落地"。
        return bool(ent.find(it.get('name'))), ''
    if kind == 'attr':
        hit = ent.find(it.get('name'))
        if not hit:
            return False, '档案里还没有「%s」' % it.get('name')
        e = hit[1]
        a = ent.clean(it.get('attr'), ent.ATTR_MAX)
        v = ' '.join(str(it.get('value') or '').split())[:ent.VAL_MAX]
        cell = (e.get('attrs') or {}).get(a) or {}
        if v in (cell.get('v') or []):
            return True, ''
        return False, '「%s」的%s还没有这个值' % (hit[0], a)
    return False, '不认识的行：%s' % kind


def _apply(it):
    """真去写。★ 只调**已有的公开接口**（remember / add / set_attr）——
    队列不自己碰 json，也不自己判 supersede／别名红线：那些规矩只有一处实现。"""
    import spk_entities as ent
    kind, who = it.get('kind'), _clean_who(it.get('who'))
    if kind == 'fact':
        remember([(it.get('key'), it.get('value'))], who=who)
    elif kind == 'entity':
        return ent.add(it.get('name'), it.get('type') or '', it.get('aka') or [],
                       attrs=None, who=who)
    elif kind == 'attr':
        return ent.set_attr(it.get('name'), it.get('attr'), it.get('value'), who=who)
    return False, '不认识的行'


def drain_pending():
    """把队列并入。返回 `(并入几条, 剩几条, 说明)`。

    ★ 一批只写**一次**队列文件（tmp + os.replace）—— 中途崩了最坏是"重放一遍"，
      而重放是幂等的（见 `_holds`），所以没有任何一步需要事务。
    ★ 判成"没落地"的行**原地留着**，不丢。宁可队列积着、看得见，
      也不能让一条主人说过的事实**没有痕迹地消失**。
    """
    merged, notes = 0, []
    with _flock():
        raw = _lines()
        if not raw:
            return 0, 0, []
        rest = []
        for ln in raw:
            try:
                it = json.loads(ln)
                if not isinstance(it, dict):
                    raise ValueError('不是一个对象')
            except ValueError:
                rest.append(ln)                 # 读不懂 ⇒ 原样留着（不静默吃）
                notes.append('有一行读不懂：%s' % ln[:60])
                continue
            ok, why = _holds(it)
            if ok:                              # 已经落地过（重放）⇒ 直接划掉
                merged += 1
                if why:                         # ★ ok 还带说明 = 并掉了但注定不落地
                    notes.append(why)           #   （正常落地/重放都是空说明，不刷日志）
                continue
            try:
                _apply(it)
            except Exception as ex:             # noqa: BLE001
                notes.append('%s 写的时候出错：%s: %s' % (it.get('kind'),
                                                        type(ex).__name__, ex))
                rest.append(ln)
                continue
            ok, why = _holds(it)                # ★ 写完再验一次终态，不信返回值
            if ok:
                merged += 1
            else:
                rest.append(ln)
                notes.append('%s 没落地（%s）' % (it.get('kind'), why))
        if merged:
            try:
                _write_lines(rest)
            except OSError as ex:
                # ★ 队列没写回去 ⇒ 那几条会被重放一遍。它们幂等，所以这不是数据事故，
                #   只是一次白干 —— 如实记下来，别让它变成"以为并完了"。
                log('✗ 队列写回失败（下次会重放这几条，幂等无害）：%s', ex)
        left = len(rest)
    if merged or notes:
        log('队列：并入 %d 条，剩 %d 条%s', merged, left,
            ('；' + '；'.join(notes[:3])) if notes else '')
    return merged, left, notes


# ---------------------------------------------------------------- 抽取
def extract(transcript_text, timeout=90, attribute=False):
    """把一段对话交给模型，让它挑出值得长期记住的。返回 `[(who, key, value)]`。

    ★ 用 ask_ex 而不是另起一套 HTTP —— key、模型、端点只该有一处定义。
    ★ 它【不带 tools】：这是纯文本活，给了工具反而会让它去调工具。
    ★★ `attribute=True` 才让它归人。判据由调用方按**数据**给出
      （`settle()` 看这段对话里到底有没有 `who` 字段），**不看开关** ——
      开关和实际行为分头走，就会出现"要了 who 却没人读它"那类静默失效。
      `attribute=False` 时不仅不提示，拿到之后还**一律抹成公共**（fail-closed）：
      模型自作主张多填一格，也不该把一条记忆藏进一个没人会去读的命名空间。
    """
    import spk_ai_dlna as dlna
    system = EXTRACT_SYSTEM + (WHO_ADDENDUM if attribute else '')
    msgs = [{'role': 'user', 'content': '对话如下：\n\n' + transcript_text}]
    blocks, _ = dlna.ask_ex(msgs, system=system, timeout=timeout)
    raw = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
    pairs = parse(raw)
    if not attribute:
        pairs = [('', k, v) for _w, k, v in pairs]
    return pairs


def parse(raw):
    """从模型回复里抠出 JSON 数组，返回 `[(who, key, value)]`。
    ★ 模型时不时会裹一层 ```json —— 所以先剥围栏，再退一步用正则找第一个 [...]。
    两种都失败就交空，【绝不猜】—— 猜出来的记忆是错的，而且会一直错下去。

    ★★ 第 3 步：多一维 `who`。**没有 `who` 的老输出照常收**（当空 = 公共）——
      这一格是"多一个可选字段"，不是新格式；模型哪次忘了填，代价只是
      "这条记成公共的"，而不是"这条丢了"。
    """
    t = (raw or '').strip()
    m = re.search(r'```(?:json)?\s*(.+?)```', t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        arr = json.loads(t)
    except ValueError:
        m = re.search(r'\[.*\]', t, re.S)
        if not m:
            log('⚠ 抽不出 JSON，这次不记。原文前 200 字：%s', t[:200])
            return []
        try:
            arr = json.loads(m.group(0))
        except ValueError:
            log('⚠ JSON 解析失败，这次不记。原文前 200 字：%s', t[:200])
            return []
    out = []
    for it in (arr if isinstance(arr, list) else []):
        if isinstance(it, dict) and it.get('key') and it.get('value'):
            # ★ 清洗交给 `_clean_who` 一个人做（这里不另设一套判据）——
            #   洗不干净它自己会回空 = 公共，不需要这里判。
            out.append((_clean_who(it.get('who')), it['key'], it['value']))
    return out


# ---------------------------------------------------------------- 收工
def settle(session, async_=True):
    """一次会话收工：归档原文 → 让模型挑重要的 → 并进核心记忆。

    ★ 默认扔后台线程：收工那一刻主人可能正等着音箱闭嘴，
      为了"记住"让他多等两秒是本末倒置。归档很快（本地写），也在里面。
    ★ 全程 try 住：这里任何异常都不许冒到主循环去。
    """
    def work():
        try:
            # ★★ 先并队列（agent 投进来的家庭事实）。
            #   位置必须在**所有提前 return 之前** —— 下面几处"这次没什么可固化"
            #   的出口（空会话／太短／抽不出东西）恰恰是最常见的收工形态，
            #   放在后面等于"绝大多数收工都不会并队列"，而队列是给主人的事实用的。
            try:
                drain_pending()
            except Exception as ex:            # noqa: BLE001
                log('✗ 并队列出错（不影响说话）：%s: %s', type(ex).__name__, ex)
            if not session.msgs:
                return
            archive(session)
            text = render(session)
            if len(text) < MIN_CHARS:
                log('会话太短（%d 字），不值得固化', len(text))
                return
            # ★★ 要不要让模型归人，判据是【这段对话里到底有没有 who 字段】——
            #   不是 `SPK_WHO` 那个开关。认人关着时账本里一句 who 都没有
            #   （`Session.add` 不传就不写这个键），那时**一个字都不该往
            #   任何人的命名空间里放**：提示词走的是 `brief(None)`（只喂公共），
            #   放进去就等于这条记忆再也读不出来，而且不报错。
            #   ★ 认人开着但这一整段都没认出人（空册子/太短/太安静/生人）时同理。
            has_who = any(m.get('who') for m in session.msgs)
            pairs = extract(text, attribute=has_who)
            if not pairs:
                log('这次没什么值得记的（%d 字对话）', len(text))
                return
            # ★★ 第 3 步：**按人分组**再落盘。不分组的话，某人聊了十分钟的口味
            #   会被记进【公共】—— 下次主人一开口，提示词里就念着"某人口味重"，
            #   而且**没有任何人会去查**。这是"不可见的污染"，只做对一半等于没做。
            #   ★ 分组放到这里而不是 `remember` 里：`remember` 的 `who` 是【谁在说话】
            #     的命名空间，一次调用只能有一个 —— 让它在内部按 pair 再分一次，
            #     就等于多出第二处"谁归谁"的判断。
            by = {}
            for w, k, v in pairs:
                by.setdefault(w, []).append((k, v))
            n = 0
            for w, ps in by.items():
                n += remember(ps, who=w)
            log('固化 %d 条新记忆（共 %d 条）：%s',
                n, len(_load()),
                '；'.join('%s%s=%s' % ('%s:' % w if w else '', k, v)
                          for w, k, v in pairs[:4]))
        except Exception as ex:                    # noqa: BLE001
            log('✗ 固化出错（不影响说话）：%s: %s', type(ex).__name__, ex)

    if async_:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()


def render(session):
    """给模型读的那份对话（人名用"主人/音箱"，跟提示词里一致）。"""
    import spk_session as sess
    return sess.render(session.transcript())


# ---------------------------------------------------------------- 体检
def _tail_scan(path, limit=1 << 20):
    """读文件**末尾**一小段，返回 `(最后一行, 文件末尾是不是 \\n)`。空文件 `(None, True)`。

    ★ 只读尾巴：归档会长大，而这里唯一关心的是"最后写下去的那条完整不完整"。
    ★★ 多字节字符被切在半路 ⇒ 解不出来，返回 `(None, …)` = **这次不判**。
      宁可漏判也不能误报 —— 一个会喊狼来了的体检，第二次就没人看了。
    """
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            n = f.tell()
            if n == 0:
                return None, True
            back = min(n, limit)
            f.seek(n - back)
            raw = f.read(back)
    except OSError:
        return None, True
    ends_nl = raw.endswith(b'\n')
    try:
        txt = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None, ends_nl
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    return (lines[-1] if lines else None), ends_nl


def health():
    """记忆系统体检（**只读**）。返回 `(problems, lines)`。

    `problems` = `[(等级, 一句话)]`，等级 `'E'` 严重 / `'W'` 警告；`lines` = 现状几行。

    ★★★ 三条纪律，写这个函数时最容易破：
      ① **只读**：不修、不写、不删、不并队列、不调模型。它敢随时跑就因为它什么都不改
         —— 一个会顺手"修一下"的自检，是没人敢在半夜跑的。
      ② **绝不读 `archive.jsonl` 的内容**，只验末尾结构（最后一行是不是合法 JSON、
         文件是不是以 `\\n` 收尾）。那是这个系统的隐私硬边界，自检不是绕过它的口子。
      ③ **判据只看数据本身**，不依赖任何一句日志的措辞。

    ★ 为什么需要它：这批记忆的退化**全是静默的** ——
      事实被挤掉不报错、排不进预算不报错、队列不排空不报错、文件写坏了
      `_load()` 也只是返回 `{}`。唯一能发现的途径就是有人定期去数。
    """
    problems, lines = [], []
    now = time.time()

    # ------------------------------------------------ L2 核心记忆
    size = -1
    try:
        size = os.path.getsize(FACTS)
    except OSError:
        problems.append(('E', 'facts.json 不存在 —— 核心记忆是空的（%s）' % FACTS))
    if size == 0:
        # ★★★ 最高危的一条，而且它**伪装成一切正常**：`_load()` 把空文件当
        #     ValueError 吞掉、返回 `{}` ⇒ 全部记忆静默消失，现象只是
        #     "它今天怎么不认得我了"。0 字节几乎只有一个成因：上次写被打断。
        problems.append(('E', 'facts.json 是 0 字节 —— 记忆被清空了（写入被打断的指纹）'))

    d = _load()
    if size > 0 and not d:
        problems.append(('E', 'facts.json 有 %d 字节却解析不出任何记忆 —— JSON 坏了' % size))

    bad = [k for k, v in d.items() if not isinstance(v, dict) or not v.get('value')]
    if bad:
        problems.append(('E', '%d 条记录缺 value（读出来是空的）：%s'
                         % (len(bad), '、'.join(bad[:5]))))

    noat = [k for k, v in d.items()
            if isinstance(v, dict) and not isinstance(v.get('at'), (int, float))]
    if noat:
        # 没有 at ⇒ 排"最新优先"时沉到底 ⇒ 几乎永远轮不到进提示词，而它并不因此不重要
        problems.append(('W', '%d 条没有 at，会永远排在最后、轮不到进提示词：%s'
                         % (len(noat), '、'.join(noat[:5]))))
    future = [k for k, v in d.items() if isinstance(v, dict)
              and isinstance(v.get('at'), (int, float)) and v['at'] > now + 86400]
    if future:
        problems.append(('W', '%d 条的 at 在未来（时钟跳过？它会一直霸着最新那条）：%s'
                         % (len(future), '、'.join(future[:5]))))

    mism = []
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        fw = v.get('who') or ''
        kw = k.split('/', 1)[0] if '/' in k else ''
        if fw != kw:
            mism.append(k)
    if mism:
        # ★★ 两处真话来源打架：`brief()` 按【who 字段】判归属，`forget_who()` 按
        #    【key 前缀】删。不一致 ⇒ 删某个人时要么漏删要么误删。
        problems.append(('W', '%d 条的归属【前缀与 who 字段对不上】—— 删人时会删错：%s'
                         % (len(mism), '、'.join(mism[:5]))))

    seen, dup = {}, []
    for k, v in d.items():
        t = (v.get('value') or '') if isinstance(v, dict) else ''
        if not t:
            continue
        if t in seen:
            dup.append('%s≈%s' % (seen[t], k))
        else:
            seen[t] = k
    if dup:
        problems.append(('W', '%d 组【不同 key 记着同一句话】（白占位置）：%s'
                         % (len(dup), '、'.join(dup[:4]))))

    b = brief()
    shown = sum(1 for ln in b.splitlines() if ln.startswith('· '))
    withprev = [v for v in d.values() if isinstance(v, dict) and v.get('prev')]
    lines.append('核心记忆 %d 条 / 上限 %d；其中 %d 条带旧说法（共 %d 个，--history <key> 看）'
                 % (len(d), MAX_FACTS, len(withprev),
                    sum(len(v.get('prev') or []) for v in withprev)))
    lines.append('认不出人时进提示词 %d 条 / %d 字（预算 %d 字）'
                 % (shown, len(b), BRIEF_CHARS))
    if shown and shown < len(d):
        # ★★★ 这一项最说明问题：**"记了几条"和"它真能看到几条"是两回事**。
        #    `brief()` 按 at 倒序累到 700 字就 break ⇒ 排在后面的**永久不可见**，
        #    而它们不是"不重要"，只是"记下得早"。
        problems.append(('W', '有 %d 条记忆【永远进不了提示词】（%d 字预算被前 %d 条占满）'
                         % (len(d) - shown, BRIEF_CHARS, shown)))
    if d:
        oldest = min((v.get('at') or 0) for v in d.values() if isinstance(v, dict)) or 0
        if oldest:
            lines.append('最早一条记于 %s'
                         % time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest)))

    # ------------------------------------------------ 投递队列
    try:
        q = pending_read()
    except OSError:
        q = []
    lines.append('投递队列 %d 条 / 上限 %d' % (len(q), MAX_PENDING))
    badq = sum(1 for it in q if it is None)
    if badq:
        problems.append(('W', '队列里 %d 行读不懂 —— 会一直卡着、每次收工重试一遍' % badq))
    if len(q) > MAX_PENDING // 2:
        problems.append(('W', '队列积了 %d 条（上限 %d）—— 音箱是不是一直没收工？'
                         % (len(q), MAX_PENDING)))

    # ------------------------------------------------ L3 归档（只看结构，不看内容）
    try:
        asz = os.path.getsize(ARCHIVE)
    except OSError:
        asz = -1
    lines.append('归档 %s' % ('（还没有）' if asz < 0 else '%.1f KB' % (asz / 1024.0)))
    if asz > 0:
        last, ends_nl = _tail_scan(ARCHIVE)
        if not ends_nl:
            # 每次归档都是 `json + '\n'` 写下去的 ⇒ 末尾没有 \n = 写了一半断了
            problems.append(('E', '归档末尾没有换行收尾（写入被打断，最后一次会话丢了）'))
        elif last is not None:
            try:
                json.loads(last)
            except ValueError:
                problems.append(('E', '归档最后一行不是合法 JSON（写入被打断）'))

    # ------------------------------------------------ 档案柜
    try:
        import spk_entities as ent
        e = ent.load()
    except Exception as ex:                    # noqa: BLE001
        e = None
        problems.append(('W', '档案柜读不出来：%s: %s' % (type(ex).__name__, ex)))
    if isinstance(e, dict):
        lines.append('档案柜 %d 个实体' % len(e))
        seen_a, clash = {}, []
        for k, ee in e.items():
            for a in (ee.get('aka') or []):
                if a in seen_a and seen_a[a] != k:
                    clash.append('%s（%s 与 %s）' % (a, seen_a[a], k))
                elif a not in seen_a:
                    seen_a[a] = k
        if clash:
            problems.append(('W', '%d 个别名同时挂在两个实体上（`find` 会认错东西）：%s'
                             % (len(clash), '、'.join(clash[:3]))))
        nconf = sum(1 for ee in e.values()
                    for cell in (ee.get('attrs') or {}).values()
                    if len([x for x in (cell.get('v') or []) if x]) > 1)
        if nconf:
            lines.append('其中 %d 个属性有多个说法对不上（等主人确认，不是故障）' % nconf)

    return problems, lines


# ---------------------------------------------------------------- 自测
def main():
    args = sys.argv[1:]
    if '--health' in args:
        problems, lines = health()
        for ln in lines:
            print('  %s' % ln)
        if problems:
            print('\n发现 %d 个问题：' % len(problems))
            for lv, t in problems:
                print('  [%s] %s' % ('严重' if lv == 'E' else '警告', t))
        else:
            print('\n一切正常。')
        # ★ 退出码有意义：有"严重"才回 1 —— 将来挂 cron / TG 告警靠它，
        #   而"警告"（比如预算用光）不该把人半夜叫起来。
        return 1 if any(lv == 'E' for lv, _ in problems) else 0
    if '--history' in args:
        i = args.index('--history')
        key = args[i + 1] if len(args) > i + 1 else ''
        if key not in _load():
            print('没有「%s」这条记忆。现有：%s' % (key, '、'.join(_load()) or '（空）'))
            return 1
        rows = history(key)
        print('「%s」的说法变迁（%d 条，旧的在上）：' % (key, len(rows)))
        for v, f, t in rows:
            print('  %s → %s   %s'
                  % (time.strftime('%m-%d %H:%M', time.localtime(f)) if f else '  ?  ',
                     time.strftime('%m-%d %H:%M', time.localtime(t)) if t else '现在 ',
                     v))
        if len(rows) == 1:
            print('  （从没改过口）')
        return 0
    if '--show' in args or not args:
        d = _load()
        print('核心记忆 %d 条（上限 %d，进提示词 %d 字）：' % (len(d), MAX_FACTS, BRIEF_CHARS))
        for k, v in sorted(d.items(), key=lambda kv: kv[1].get('at', 0), reverse=True):
            print('  [%s] %s   (%s)' % (k, v.get('value') or '【这条缺 value】',
                                       time.strftime('%m-%d %H:%M', time.localtime(v['at']))))
        # ★ 这里给人看，所以两版都打：`brief()` 是**认不出人时**真正会进提示词的那份
        #   （只有公共），`brief_all()` 是全部。两份不一样就说明"按人分流"在生效。
        print('\n--- 认不出人时进提示词的那段（%d 字，只含公共）---' % len(brief()))
        print(brief() or '（空的）')
        if brief_all() != brief():
            print('\n--- 全部（含各人名下的，这一份【不进提示词】）---')
            print(brief_all())
        return 0
    if '--forget' in args:
        i = args.index('--forget')
        key = args[i + 1] if len(args) > i + 1 else ''
        d = _load()
        if key in d:
            d.pop(key)
            _save(d)
            print('忘了：%s' % key)
        else:
            print('没有这个 key。现有：%s' % '、'.join(d) or '（空）')
        return 0
    if '--drain' in args:
        m, left, notes = drain_pending()
        print('并入 %d 条，剩 %d 条' % (m, left))
        for n in notes:
            print('  · %s' % n)
        return 0
    if '--queue' in args:
        q = pending_read()
        if not q:
            print('队列是空的（%s）' % PENDING)
            return 0
        print('队列 %d 条（%s）：' % (len(q), PENDING))
        for it in q:
            print('  · %s' % (json.dumps(it, ensure_ascii=False) if it else '【读不懂的一行】'))
        return 0
    if '--extract' in args:
        # 直接从文件读一段"主人：…／音箱：…"的对话来试抽取，不碰音箱
        i = args.index('--extract')
        path = args[i + 1] if len(args) > i + 1 else ''
        text = open(path, encoding='utf-8').read()
        # ★ 命令行试抽取一律按【有说话人标签】处理 —— 这个入口的约定格式就是
        #   "主人：…／某人：…"一行一句（`render()` 的产物）。想试"不归人"的样子，
        #   把文件里的名字全写成「主人」再看一遍就行（那样 who 只会是主人或空）。
        pairs = extract(text, attribute=True)
        print('挑出 %d 条：' % len(pairs))
        for w, k, v in pairs:
            print('  [%s][%s] %s' % (w or '公共', k, v))
        return 0
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main())
