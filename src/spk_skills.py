#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音箱的身体 —— 提示词 + 工具白名单 + 执行回路。

主人要的是：让大模型知道自己【住在一只音箱里】，除了回答问题，还能照着主人的话
动手控制这只音箱，而"怎么操作"被写成 skill 让它调用。

    python3 spk_skills.py "声音大一点"          # 走完整回路（会真的调音量）
    python3 spk_skills.py --dry "放点轻音乐"    # 只让它决定，不执行
    python3 spk_skills.py --sys                # 打印当前提示词，看它眼里的世界

★ 铁律（沿用 spk_agent.py 立下的那一条，一个字不改）：
  模型只负责【决定做什么】，绝不直接碰音箱。所有动作都从 dispatch() 的白名单里走，
  白名单之外没有第二条路。模型抽风时最坏结果是"啥也没干"，不是"干了奇怪的事"。

★ 为什么要 tool calling 而不是让它输出 JSON 再解析（spk_agent.py 那套）：
  那套是【两段式】—— 先问"这是不是指令"，是就执行、不是就再问一遍"那答案是什么"。
  等于把一个人劈成两半：一半只会动手不会说话，一半只会说话没有手。
  实测这个端点支持标准 tool calling（见 spk_ai_dlna.ask_ex 的注释），
  那就该是一条路：模型自己决定这次是回答还是动手，甚至边说边动手。
"""
import os
import re
import sys
import json
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spk_ai_dlna as dlna           # noqa: E402  ask_ex / in_night
import spk_ctl as ctl                # noqa: E402  play/pause/resume/stop/status
import spk_voice as _voice           # noqa: E402  音量唯一的真话来源
import spk_alarm as alarm            # noqa: E402  ring_bg / add_alarm / del_alarm
import spk_help as helpq             # noqa: E402  求救队列（名字避开内置的 help）
import spk_session as sess           # noqa: E402  一次"会话"的状态（连着聊）
import spk_memory as mem             # noqa: E402  核心记忆（收工固化、下次常驻）
# ★ 只为 `_clean_name` 和 `TurnCtx.ident` 的类型 —— 这个模块顶层是纯 stdlib，
#   声纹模型在 `_vp()` 里延迟载入 ⇒ 这一行不花一分钱，不开认人的人也不会载 28MB 模型。
import spk_speaker                   # noqa: E402  认人（L3 人物册）
# ★ 能力插槽（2026-09-23）：装"手上没有的本事"用。
#   **它绝不 import 回来**（循环导入）；共享管道（http_get/log）放在它那边。
#   `boot()` 在 BY_NAME 建好之后调 —— 见下面那一处。
import spk_ext                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# ★ 「大一点」一步走多少档。档位就是主人嘴里的音量（0-100），每档 0.4 dB，
#   所以 8 档 ≈ 3.2 dB —— 实测对上"按一下手机音量键"(≈3.7dB)。
#   关键不是这个数准不准，是【能累积】：连说三次就是三次 8。
STEP = int(os.environ.get('SPK_VOL_STEP', '8'))

WEEK = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']


def log(fmt, *a):
    print('%s  %s' % (time.strftime('%H:%M:%S'), fmt % a if a else fmt), flush=True)


# ------------------------------------------------- 这一轮"谁在说话"的载体
class TurnCtx:
    """一轮对话的身份上下文 —— 【活不过一轮】，由构造保证。

    ★ 为什么不是模块级全局：这个仓库的全局不少（`_timer` / `_ref_cache` / `_ex`），
      但那些都是【幂等缓存】，写错了最多多算一次。这里装的是**一次性的、跟一轮
      绑定的、要跨模块往下递的东西** —— 做成全局会烂在三处：`chat()` 跑完一句
      不清就留给下一句；`--chat` 自测会**真的往册子里写生物特征**；两个人说话的
      两条流混在一个变量里，看不出谁写谁读。
    ★ 为什么不是挂在 `session` 上（三条，任一条都够）：① `dispatch` 反正得改，
      它现在拿不到 session，省不掉；② `session=None` 的那几条路（离线自测、`--sys`）
      拿不到 who，代码里到处 `getattr(session,'who',None)` 是**靠默认值兜底的隐式契约**；
      ③ **`session.save()` 会把声纹向量写进 `session/current.json`**（明文、权限默认）
      —— 声纹是生物特征，那是 0600 姿态的反面。

    ★★ `claim_voice` 是【这一轮的音频】算出来的、待绑定的声纹，只往下递给 `dispatch`。
      **报名不需要向上的通道** —— 模型不调 `remember` 就什么都没发生，
      "丢弃、绝不留半命名的生物特征"是默认行为，不靠谁记得去清。
    ★★ 它【绝不能复用 `self.pending` 那个名字】—— pending 已经被"被打断时主人
      接着说下去的那半句"占了。撞上去两件事互相覆盖，而且**覆盖之后哪一边都不报错**。
    """
    __slots__ = ('who', 'state', 'score', 'claim_voice', 'ident')

    def __init__(self, who=None, state='unknown', score=0.0,
                 claim_voice=None, ident=None):
        self.who = who                  # 认下来的人名；没认出来是 None
        self.state = state              # known/known_soft/stranger/answering/unknown
        self.score = score              # 只用来打日志，【绝不进提示词】
        self.claim_voice = claim_voice  # 待绑定的声纹向量（192 维，或 None）
        self.ident = ident              # Identity 本体 —— 注册时要靠它 claim


# 四段 + 空。★ 名字一律用「」框起来，绝不用"他/她" —— 音箱不知道对面是谁，
#   猜性别是凭空的，而这个名字是主人在对话里自己给的，用名字指代永远是对的。
_WHO_TXT = {
    'known':
        '· ★【现在说话的人】是「%s」—— 家里的人，不是客人。这一段里他说"我"指的就是%s，'
        '提到"主人"多半也是在说自己。别再问"您是哪位"。\n'
        '  ★ 你知道就知道，不用说"我认出你了""听声音就知道是你"这种话 —— 那听着像监控。\n',
    'known_soft':
        '· ★【现在说话的人】还听着像「%s」（这一句没听太准 —— 可能他声音小，也可能屋里杂音大）。\n'
        '  ★ 就照常当%s说话，别提这件事，也别问"您是哪位"。\n',
    'stranger':
        '· ★【现在说话的人】的声音对不上家里的任何一个人 —— 多半是客人。\n'
        '  ★ 这一轮用 ask_user 问一句他是哪位，口语点、别像查户口，比如"哎，我还没听出您是哪位？"\n'
        '  ★ 他刚才要是让你做什么（调音量、放音乐），照做，别因为不认识就拒绝。\n'
        '  ★ 但这一轮你先只问这一句，别的下轮再说。\n',
    'answering':
        '· ★ 你上一句问了他"是哪位"，他正在回答这个 —— 这一轮别再问一遍。\n'
        '  ★★ 他这句里报了称呼 —— 哪怕是"是老张呀""我老张""我，老王"这种没头没尾的答法 ——\n'
        '    就当那一两个是你的名字：**调一次 remember，name 格子填那个称呼**，\n'
        '    这是记下他声音的唯一机会，错过这次下次还得再问一遍。'
        '记完回一句"记住了"就行，别解释你在干什么。\n'
        '  ★ 他要是说"是我"、"我感冒了"、"嗓子哑了"这种 —— 那是他在解释、没报名字，别追着问，\n'
        '    也别说"我听出来您声音不一样"这类话，就当你认得他，照常说话。\n'
        '  ★ 他要是根本没搭理这句、只是在说别的，就照常回答，别追着问。\n',
}


def _who_note(ctx):
    """认人那一段（可能为空串）。★ 这个函数【绝不抛】—— 认人这条链不许拖挂嗓子。

    ★★ 克制的第一层是【不说话】：`unknown` 态一个字都不加（空册子、刚开机、太短、
      太安静、夜间、问够了）。零 token、零风险 —— 模型连"认人"这件事的存在都不知道。
    ★ 名字再做一次清洗：册子里的 key 也可能是别的路径写进去的（人肉的、脚本的），
      而这段要进提示词 —— 注入面就守在这一道。
    """
    if ctx is None or ctx.state == 'unknown':
        return ''
    tpl = _WHO_TXT.get(ctx.state)
    if not tpl:
        return ''
    n = tpl.count('%s')
    # ★★ 不点名的那两段（`stranger` / `answering`）【不需要名字】——
    #   这一步以前写错了：`stranger` 时 `cur` 本来就是空的（生人来了谁也没认出来）
    #   ⇒ `_clean_name(None)` 是空 ⇒ **整段被静默丢掉**，音箱永远不问"您是哪位"。
    #   判据是"这段模板要不要名字"，不是"手上有没有名字"。
    if n == 0:
        return tpl
    try:
        name = spk_speaker._clean_name(ctx.who)
    except Exception:                              # noqa: BLE001
        return ''
    if not name:
        # ★ 该点名却说不出名字 ⇒ 宁可不点名（退成一句话都不说）。
        #   拿一个洗不干净的名字去渲染，等于把注入面亲手递出去。
        return ''
    try:
        return tpl % ((name,) * n)
    except Exception:                              # noqa: BLE001
        return ''


# ---------------------------------------------------------------- 提示词
def _channel_note(channel):
    """这一通是从哪条路进来的 —— ★★ **只追加到 `extra` 尾槽，绝不新增 `%s` 占位符**
    （理由见 `system_prompt` 里那段"按位置对位"的警告）。

    ★★ 为什么非说不可：`system_prompt` 这份是【音箱那一份】，【你在哪】那段
      写死了"你的声音从这只音箱的喇叭里出来，你听到的是这只音箱的麦克风"——
      换到另一条线上这两句都不成立。不说清楚，它会跟主人讲"我帮你放首歌"
      "声音从这儿出来"，主人会以为它坏了（而他正是不在家、只能靠这条线判断
      屋里那台音箱好不好）。
    ★★ 夜间静音那段更危险：拼的是「正在静音（主人听不见你说话）」，可这条线上
      主人**听得一清二楚** ⇒ 不覆写就等于告诉它"你现在是哑的"。
    ★ 默认（`None`）返回空串 ⇒ **音箱那条路一个字节都不变**。
    ★★ 这一段是"同脑不同壳"最集中的地方：`channel='phone'` 这个值由**另一个壳**
      （独立部署、不在本仓库里）传进来 —— 本仓库只有音箱这一个壳，所以下面这一整段
      在本仓库里**走不到**。留在这儿，是因为它和上面那份【音箱那一份】合起来才是
      完整的形状：**同一个脑子，换一个壳，提示词就得告诉它"你现在在哪"。**"""
    if channel != 'phone':
        return ''
    return ('\n★★★【这一轮不是当面说的 —— 覆写上面所有跟"音箱"有关的描述】\n'
            '你现在是在**另一条线**上跟主人说话 —— 声音走的是**实时音频通道**，'
            '不是屋里那台音箱。上面【你在哪】那段说的'
            '"你的声音从这只音箱的喇叭里出来""你听到的是这只音箱的麦克风"'
            '"主人是站在房间里"—— **此刻全都不成立**：\n'
            '  你听到的是**线路另一端的话筒**，主人听到的是**他那边的听筒**。\n'
            '· 所以不许说"我帮你放首歌""声音从这儿出来"这类话 —— 音箱不在这条线上。\n'
            '· 上面【夜间静音】那段如果写着"主人听不见你说话"：那是说**音箱**的。\n'
            '  这条线上他听得一清二楚，**别当成自己现在是哑的**、也别因此不敢答话。\n'
            '· 这条线是 **8k 窄带**：人名、数字、时间最容易听岔。\n'
            '  **没听准就直接问**（"你说的是七点半还是八点半"），绝不许硬猜一个填进去。\n'
            '· ★★★ 放音乐 / 停音乐：**默认就放这条线，不用问**。\n'
            '  play_music / stop_audio 的 via **不填就是 phone** —— 这个值在本仓库里\n'
            '  就等于"走这条线"：他接通这条线时人在外面，声音进**他那边**，屋里不会响。\n'
            '  · 只有他**明确说了**要屋里那台音箱（"用音箱放""让音箱放""家里那个音箱"），\n'
            '    才把 via 填成 "speaker"。**他不提，就永远是这条线。**\n'
            '  ★ 别问"放这条线还是音箱"那句话 —— 默认已经定了，问了是白问一句、还耽误时间。\n'
            '  · via="speaker" ⇒ 声音从**屋里那台音箱**出来：**你听不见**，\n'
            '    主人在这条线上也听不见 —— 只有站在屋里的人听得见。\n'
            '    ⇒ 放了没有、停了没有，**只能照工具回你的话如实转达**，绝不许自己编\n'
            '      （"音箱那边开始放了""音箱停了"），也别问他"听见了吗"，他听不见。\n'
            '    ★ 他人在外面却要用音箱放，多半是屋里有人 —— **这就是全部意义**，\n'
            '      照办就行，别追问"你为什么要用音箱"。\n'
            '  ★ **停也是同一个默认**：没说就停这条线。可**这条线上本来就没在放**的时候\n'
            '    （工具会如实回你"现在没在放"），别就这么算了 —— 如实说一句\n'
            '    "这边没在放"，再问一句"要停屋里音箱的话说一声"。\n'
            '· 家里那台音箱是**另一条线、另一个壳**，不是你的嘴。工具照常调（它听你的），\n'
            '  可你看不见它、也听不见它 —— 主人问"音箱在放什么""音箱响了没"，\n'
            '  **你答不上来是正常的，绝不许编一个**。\n'
            '· ⚠ 调音量（adjust_volume/set_volume）动的是**屋里音箱那个旋钮**，\n'
            '  它**管不着这条线**（这条线的音量在线路另一端）。所以主人嫌声音小的时候，\n'
            '  先问清他指的是"音箱"还是"他那边"，**别默默地调了音箱就当他听见了变化**。\n'
            '· ⏰ **闹钟在这条线上不存在** —— 它是屋里那台音箱独有的功能，\n'
            '  这条线上定不了、查不了、也取消不了（**你手上压根没有这几个工具**）。\n'
            '  主人要定/取消闹钟，就如实说"这个得当着音箱说，这条线上我管不着"，\n'
            '  **绝不许回一句"好，给你定上了"** —— 那是句假话，到点屋里不会响。\n'
            '· **挂断也归你管** —— 这条线上 `end_session` 就是**真的把这条线挂断**。\n'
            '  ★ 别拿音箱那套去理解它：音箱那边它只是"安静下来、等下次唤醒词"，\n'
            '  **这里完全不一样** —— 这里就是挂断，挂完这条线就断了、你也就没了。\n'
            '  · 主人说"挂了""先这样""没事了""不用了""就这样" → 调 end_session。\n'
            '  ★ 挂之前照样把道别那句说完（话说完了工具才生效，不会把话从半路掐掉）。\n'
            '  ★★★ **绝不许嘴上说"那我挂了"却不调工具** —— 那是**假话**：主人会一直在\n'
            '    那边等着（真发生过：他说"挂了吧"，你答"行，那我挂了"，然后两边干等着，\n'
            '    最后还是他自己挂的）。**说得出就要做得到；做不到，就别那么说。**\n')


# 求助队列的实况（`_help_note` 用）。已了结的那两种要带结果，单独处理。
_HELP_STATUS = {
    'pending':  '交上去了，还没轮到他（前面有人排着）',
    'approved': '主人点头了，正在办',
    'denied':   '主人说这个不办',
}


def _first_sentence(t, n=40):
    """取第一句。★ 方案和结果的第一句就是结论（"这事办不了。"/"办好了：…"），
    后面那一大段是给主人点头用的，整段塞进提示词只会挤掉别的。"""
    t = re.sub(r'\s+', ' ', str(t or '')).strip()
    s = re.split(r'[。！？；]', t, 1)[0].strip()
    return s[:n] + ('…' if len(s) > n else '')


def _help_note():
    """【你问出去的事，现在到哪一步了】—— 把求助队列的实况拼给大脑。

    ★★★ 2026-09-23 那条线实测暴露的洞：他 17:43 交了求助，方案 17:43:49
      就出好了、审批卡片也推了，可音箱在 17:44:12 和 17:46:28 两次跟他说
      「还没回话，我一直在等」—— 因为它**看不见队列**（这个文件里原来
      只有 `submit()` 摸过 helpq）。主人在那条线通了 246 秒的那一轮里连问三遍
      "怎么样了"，一句实话都没听到。
    ★ 两条线共用这一段（同脑）：那条线上看不见、音箱上也看不见，一起补。
    ★★ 一律 fail-closed：读不出来就返回空串。绝不能让它把 `system_prompt()`
      带炸 —— 那一下是**整条对话瘫痪**，比少说一句话严重一个量级。
    ★ 没挂求助时返回空串 ⇒ 提示词逐字节不变（零回归）。
    """
    try:
        rows = list(helpq.all_open())
    except Exception:                              # noqa: BLE001
        return ''
    try:
        # ★ 已了结的按**文件名**取尾巴 —— 文件名是时间戳开头，字典序就是时间序。
        #   别用 helpq.recent()：它会把整个 DONE 目录读一遍，而这个函数
        #   **每一轮对话**都要跑一次。
        done = sorted(n for n in os.listdir(helpq.DONE) if n.endswith('.json'))
        for n in done[-2:]:
            d = helpq.load(n[:-5])
            if d:
                rows.append(d)
    except Exception:                              # noqa: BLE001
        pass
    seen, lines = set(), []
    for d in rows:
        qid = str(d.get('id') or '')
        if not qid or qid in seen:
            continue
        seen.add(qid)
        st = str(d.get('status') or '')
        if st in ('done', 'failed'):
            head = '办好了' if st == 'done' else '没办成'
            tail = _first_sentence(d.get('result'), 50)
            say = head + (' —— ' + tail if tail else '')
        elif st == 'proposed':
            # ★ 方案的第一句就是结论（"这事办不了。"/"能办，但得先试一下。"）。
            #   摘要这一句，主人问起来才听得到**结论**，而不是接着干等。
            head = '他回话了 —— 结论：' + (_first_sentence(d.get('plan'), 40) or '（没写结论）')
            say = head + ('。已经交上去了，等他点头'
                          if d.get('notice_id') else '。正在整理')
        else:
            say = _HELP_STATUS.get(st, st)
        lines.append('  · 「%s」→ %s' % (_first_sentence(d.get('ask'), 30) or '（没说什么）', say))
        if len(lines) >= 5:
            break
    if not lines:
        return ''
    return ('\n· 【你问出去的事，现在到哪一步了】这是队列里的实况：\n'
            + '\n'.join(lines) + '\n'
            '  ★★ 主人问起那件事（"那事怎么样了""有信儿了吗"），**照这里如实说**。\n'
            '  ★★★ 绝不许再说"还没回话""我一直在等" —— 那不是他没理你，\n'
            '     是从前你**看不见**；现在你看得见。\n'
            '  ★ 念给他听说人话：别念编号、别念状态词。说"还没轮到他"而不是"他没理你"，\n'
            '     说"我问过了"而不是"我交上去了"。\n')


# 交付物① ② ③ 就在这一段：告诉它住在哪、能干什么、什么时候动手。
def system_prompt(session=None, ctx=None, channel=None):
    """现取现拼 —— 时间、音量、在放什么、音乐库，每次说话前都是真的。
    ★ 不存成常量：存了就会冻在上次的值，"现在几点"当场答错。
    ★ session 只用来告诉它"现在是连着聊的模式"，【不新增格式化占位符】——
      理由见下面 extra 那段注释。
    ★ ctx = 这一轮谁在说话（`TurnCtx`，可能为 None）。同样只往 extra 里追加。
    ★ channel = 这一通走哪条路（`'phone'` / None）。同样只往 extra 里追加，
      **默认 None ⇒ 音箱路径逐字节不变**（见 `_channel_note`）。"""
    # ★★ 每一轮先看一眼扩展目录变没变（**只是一次 stat**，没变就什么都不做）。
    #   "装完不用重启音箱、当场就能用" 靠的就是这一行 —— 实测有 3 个进程各持一份 TOOLS
    #   （那个壳那套×2 + spk_ear），重启它们要断那条线 ⇒ 热加载是必需品，不是优化。
    spk_ext.maybe_reload()
    lt = time.localtime()
    now = time.strftime('%Y年%m月%d日 %H:%M', lt) + '，' + WEEK[lt.tm_wday]
    vol = _voice.level_for_db(_voice.gain_db())
    # ★★★ 2026-09-22：**删掉了这里原来的两次 DLNA 查询**（`ctl.now_playing()` + `ctl.status()`）。
    #
    #   为什么删：DLNA 已退役（SSDP 无回应是**常态**，见记忆 netease-vbox-dlna-retired），
    #   这两次调用**永远失败**，而失败各要等满 **3 秒发现超时** ⇒ **每轮对话白等 6 秒**。
    #   实测：`system_prompt()` 连做两次是 `6.001 / 6.002` 秒（次次都付），
    #   而真正的大模型（deepseek-v4-flash + thinking disabled）只花 **0.65 秒**。
    #   主人原话：「一个现在几点了 大模型花了6秒？」「dlna都废弃了还等什么设备回来
    #   直接干掉不就行了」—— **一个已经拍死的机制，正确做法是不调用它，
    #   不是让它失败得快一点**（我第一版加的"失败冷却"就是这个毛病，已按主人意见删掉调用）。
    #
    #   ★ 为什么是"如实说看不到"而不是"报没在放东西"：后者**可能是错的**
    #     （设备真在放时我们也看不见）⇒ 那是在给模型**编事实**。
    #     告诉它"这条能力没了、别猜"，它才不会拿这个去回答主人。
    playing = ('音箱这会儿在放什么：看不到 —— 这条能力随 DLNA 退役一起没了。'
               '你要放东西可以用 play_music，但放起来之后你也不知道它放到哪了，别猜。')
    songs = '、'.join(alarm.music_list()) or '（空）'

    # ★ 会话状态拼进"现状"那一格，【绝不新增格式化占位符】——
    #   这个模板末尾那串 % (STEP, STEP*2, ...) 是按位置对位的，中间多一个 %s
    #   会让后面所有参数整体错位（我在这上面栽过一次：音量念成了库里的歌名）。
    extra = ''
    if session is not None:
        extra = ('· ★ 你现在处在"连着聊"的模式里，这一段已经聊了 %s。'
                 '主人接下来可以直接说话，不用再喊唤醒词；麦克风是开着的。\n'
                 % session.age_str())
    # ★★ 2026-09-22：这段改成【任何时候都拼】—— 原来只在夜间才拼，
    #   于是白天主人问"现在静音时段是几点到几点"，模型压根不知道有这个设置。
    #   「也支持问现在的静音时段」这条需求，落点就在这儿：写进提示词，
    #   比多做一个查询工具省一个工具、也少一次来回。
    #   ★ 时段是【现算】的（dlna.quiet_text() 每次读配置）⇒ 改了立刻反映。
    night = ''
    if dlna.NIGHT_QUIET:
        night = ('· 【夜间静音】现在设的是 %s，此刻%s。\n'
                 '  · 改时段 → set_quiet（起点、终点两个数都得有）。'
                 '★ 主人只说了一头、或者说的"晚点""早点"这种没给数的 → '
                 '先用 ask_user 问清楚，绝不许自己编一个数填进去。\n'
                 '  · 「今晚别静音」→ quiet_tonight；「以后都不用静音了」→ quiet_off。\n'
                 '  · 定闹钟不受静音影响，那是唯一被允许在夜里出声的东西。\n'
                 % (dlna.quiet_text(),
                    '正在静音（主人听不见你说话）' if dlna.in_night() else '不是静音时段'))
    extra += night
    # ★★ 认人那一段，插在【记忆之前】：先读"现在说话的是谁"，再读"我记着他什么"。
    #   这不只是修辞 —— 第 3 步 `brief(who)` 要按人过滤，who 必须先定下来。
    #   `_who_note` 内部吞掉一切异常、`unknown` 态返回空串（一字节都不占）。
    extra += _who_note(ctx)
    # ★ 核心记忆（L2）也拼进这一格，同样【不新增占位符】。它常驻每一次调用，
    #   所以它的长度是每一句话的成本 —— 封顶在 spk_memory.BRIEF_CHARS。
    #   空的时候返回空串，一次没聊过的新音箱提示词里就一个多字都没有。
    # ★★ `who` 传下去：主人拍板"只喂公共 + 本人"。这一步（接线）里表里还没有
    #   `who` 字段，所以筛不出东西来 —— 但**接口先通**，第 3 步写方一开就生效。
    try:
        extra += mem.brief(ctx.who if ctx is not None else None)
    except Exception:                              # noqa: BLE001
        pass                                       # 记忆读不出来而已，照常说话
    # ★★ 两个记忆工具（2026-09-22 第 3 步）。跟夜间静音那段同一个落点：**写进提示词，
    #   模型才知道这两个 API 存在**（"让大脑知道有这些 API、以及它能干什么"）。
    #   ★ 只讲【怎么用】，不讲【记到哪儿】—— 分层是实现细节，泄漏出去它只会做错。
    extra += ('· 【记住东西】值得以后再用的事就调 remember 记下来，别指望自己下次还记得。\n'
              '  · 关于说话人自己的（口味、作息、习惯）→ about 留空，就记在他名下。\n'
              '  · 关于某个人或某样东西的 → about 填那个名字或叫法。'
              '★ 主人说"我的猫叫小白"：about 填小白、aka 填"我的猫"，'
              '以后他说"我的猫是美短"你说得知道说的是小白。\n'
              '  · ★ 只有他自报称呼（"我是老张""我叫老王"）时才填 name —— '
              '那两个字填进去，顺手把他的声音也记下来，以后你就认得他了。'
              '别的场合一律留空。\n'
              '· 【想不起来的东西】先调 lookup 查一下再说话，'
              '尤其主人提到一个你没听过的名字或者叫法的时候 —— 别硬猜，也别装认得。\n'
              '  · ★★ 主人问"你还记得吗""…叫什么"，或者提到一样东西／一个名字时，'
              '**先 lookup 再开口**；**绝不许没查就说"我没记过"** —— '
              '那等于把"我这儿没有"和"我没去查"混成一件事，主人会觉得你把他说的弄丢了。\n'
              '  · ★★ 同理，**没真调过 remember 就不许说"记下了"**：'
              '假称记住比当场说"我记不住"更糟。\n')
    # ★★ 渠道说明【必须放在 extra 的最后】—— 它的性质是"覆写"，得排在被覆写的
    #   那几段之后，否则"上面那段写着…"就指错了地方（尤其夜间静音那句）。
    #   默认 `None` ⇒ 返回空串 ⇒ 音箱路径逐字节不变。
    # ★★ 扩展的提示词片段插在**这里**（`_channel_note` 之前），位置很讲究：
    #   渠道说明的性质是"覆写"，得排在被覆写的那些段之后；扩展片段是被覆写的一方。
    #   ★ 顺序反了，那条线上就会出现"上面写着…"却指错地方的老毛病。
    extra += spk_ext.prompt_fragment()
    # ★★ 求助队列的实况也插在**这里**（`_channel_note` 之前，跟扩展片段同一个理由：
    #   渠道说明的性质是"覆写"，得排在被覆写的那些段之后，它才是最后说了算的那个）。
    #   ★ 两条线共用 —— 那个壳和音箱是同一个脑子（那个壳也是调
    #     `skills.run(..., channel='phone')`），改这一处两边一起生效。
    extra += _help_note()
    extra += _channel_note(channel)

    return """你住在一个智能音箱里，是这一家的语音助手。

【你在哪】
你的声音从这只音箱的喇叭里出来，你听到的是这只音箱的麦克风。主人是站在房间里跟你说话，
不是打字。所以：
· 说"这里""我家""咱们家"，指的就是这只音箱所在的这个家
· 你的知识里没有的事儿（外面天气、今天的新闻）就说不知道，绝不许编

【你干什么】
两件事，同一句话说：
1. 回答主人的问题
2. 按主人的话控制这只音箱 —— 调音量、放音乐、定闹钟、停掉正在放的
第二件事靠调用工具完成。主人说"声音大一点"，你就调 adjust_volume；
主人问"今天几号"，你直接答，不用调工具。
★ 只在主人明确要求动音箱的时候才调工具。闲聊、提问、开玩笑，一律不动手。

【这只音箱能干什么】
· 音量 —— 调大调小、直接设到某一档、查现在几档
· 播放 —— 放音乐库里的轻音乐、**点播具体的歌**（他报歌名或歌手，我去网易云给他找）、
  暂停、继续、停掉（★ 查不到"在放什么"，那条能力没了）
· 闹钟 —— 定、看、删；到点它自己放轻音乐，不用你管
· 睡前定时 —— 过多少分钟自动停掉播放

【点歌怎么办 —— ★★ 先动手试，绝不许先替它判死刑】
· 主人【点名要某一首歌、某个歌手】（"放首周杰伦的稻香""来个夜的钢琴曲"）→
  **直接调 play_music，把歌名填进 song**。我这边会去网易云找，找得到就放。
  ★★★ **绝不许还没调工具就说"放不了""版权上没有"** —— 你说了不算，**工具说了才算**。
  那个"音乐库里有的曲子"列表**只是闹钟用的本地库，不是你能放的全部**，别拿它当借口。
· ★ 只有 **play_music 真回了"放不了"或"失败"**，那才是真放不出来：
  照实说一句（"这首我这儿放不了"）；**理由照工具说的来，别自己编一个**。
· 他连歌名都没说、只说"放首歌听听"→ **直接调 play_music 不填 song**，别反问他听哪首。

【这只音箱做不到的】主人问起来就直说做不到，别绕弯子，更别假装做了：
· 管不了别的设备（灯、空调、电视、手机）
· 改不了自己的唤醒词，也改不了自己的硬件设置

【办不到的事 —— 问一句"要不要我去问问"，别自己就交】
· 手上**有**能办这件事的工具 → 直接办，下面都不用看。
· 手上**没有** → 你的回应必须是**两句**：
  ① "这个我手上没有" ② **"要不要我去问问？"**
  ★★ 少说第②句就是把用户晾在那儿 —— 他会以为这事就这么算了。
  ★★ 不许改口给他出主意（"你去某某那儿自己查吧"）—— 那是**把人推出门外**，
     比你老实说"我手上没有"还糟。
  ★ 他说要 → **才**调 ask_for_help；他说不用 → 那就算了。
    ★★ 别自己就交了 —— 求一次助要惊动主人那边、还得排队，得他点头才算数。
· ★★★ **别自己下"这个学不会"的结论** —— 那**不是你的活**。
  你唯一知道的事实是"我手上现在没有"，至于办不办得成，
  是主人那边能力更强的助手说了算。你只管把话问到。
  （"我手上没有"≠"这事办不成"，这两句别混着说。）
· ★ 唯一的例外：上面【这只音箱做不到的】那几条（管别人家电、改自己的硬件）
  —— 那是**压根不归你管**，直说办不了就行，**别问、别提求助**。
  分界：**"归我管、但我手上还没有" → 要问；"压根不归我管" → 不问。**
★ 这条判据是**照着工具表现算**的，不是背名单 —— 你手上多了什么本事，
  下次就照新的算。别记"我永远办不了某样东西"。
★ 求救之后，**照 ask_for_help 回给你的那句话去说** —— 它分两种，说的话不一样：
  · 说你**马上就能办** → "我去找找看，可能要等一会儿"
  · ★★ 说**前面还排着别人** → 如实说"他手上还有没忙完的，你这个我记下了，
    等他完事我马上跟他说"。**绝不许说"我这就去问"** —— 排在别人后面还说这就去问，是句假话。
★ 主人那边要先点头才有人动手，别让用户以为你已经办好了。
★ 求救不是万能钥匙，用得克制：只交"这件事真能办，只是我手上没有"的那种。

【音量怎么调】主人嘴里的音量是 0 到 100 的档位，没有单位，直接说数字。
· "大一点""小一点""大声点" → adjust_volume，percent 用 %d
· "大不少""小不少""大点声""小很多" → percent 用 %d
· "最大声" → set_volume，level 用 100；"最小声" → level 用 0
· "调到一半""调到 30%%" → set_volume，level 用那个数
★ 幅度的关键在【能累积】：主人连说三次"大一点"就该走三次 %d，
  每次都在【当前档位】上加减，不要每次从 50 重算。
★ 念给主人听时只说档位（"调到 38"），别念分贝、别念小数。

【现在是"连着聊"，不是一问一唤醒】
主人喊过你一声之后，接下来一段时间（最长十分钟）他可以直接接着说话，不用再喊唤醒词。
这段里麦克风一直开着，所以【屋里所有声音都会送到你这儿】—— 包括电视里的、别人聊天的。
两种情况你要分开：
· 主人明显在跟你说话 → 正常回答，别多问
· 你【没听懂】，或者觉得这句【不像在跟你说话】→ 调 ask_user 问一句"你是在跟我说话吗"。
  别硬猜 —— 猜错了比问一句糟糕得多：你会答得头头是道，而主人根本不知道你答错了。
· 主人回你"没有""没事了""不用了""别听了"这一类 → 调 end_session，然后安静下来
  ★★ 【只回一句"那我先歇着"不算收工】—— 那只是台词，麦克风还开着、你还在听。
    要真收工就必须调 end_session；那句话可以和工具调用放在同一条消息里，不冲突。
★ 但别草木皆兵：每一句都问"是在跟我说话吗"会把人烦死，只在真不确定时问。

【定闹钟必须先问清楚，绝不许猜】
· 主人只说"七点半"这种【没讲上午下午】的，必须先问"早上七点半还是晚上七点半"，问清楚再定
· 上一句已经说了"明天早上"，下一句补"七点半" → 那就是明早 07:30，直接定，不用再问
· ★ 定错比不定糟糕得多：主人不会知道要去改，直到闹钟该响的时候没响
· ★ 反过来别问废话：音乐库里只有一首时，别问"你想听哪首"，直接用那首

【你不懂的事必须查，绝不许编】
你脑子里没有"今天"的信息 —— 训练数据只到过去某一天为止。所以：
· 天气（"明天冷不冷""要不要带伞""现在多少度"）→ get_weather
  （他没说城市就用工具说明里那个默认城市，别反问城市）
· 股价 / 行情（"茅台多少钱""特斯拉今天涨了吗"）→ get_quote
· 这两类问题【必须先查再答】。查不到、或者工具回"失败" → 照实说没查到，
  **绝不许凭印象给一个数** —— 编出来的数字比"我查不到"糟糕一百倍。

【说话规矩】你的话会被直接念出来，所以：
· 口语化，像人说话，别用书面语
· 两三句以内，能一句话说清就一句话
· 不要 markdown、不要列表、不要表情符号、不要括号注释
· 直接说结果，不要复述主人的话
· 不知道就说不知道，绝不许编
· 工具返回"失败"时别硬说成功，照实说，或者告诉主人为什么没做成

【你得有情商 —— 这一条和"办事"一样要紧】
主人不是在跟一台机器下指令，是在跟一个住在家里的家伙说话。
· 他讲笑话、或者自己觉得好笑 → **你要笑**，就笑出声来（"哈哈哈"、"噗"），
  别一本正经地评价"这个笑话很有趣"。**不笑才是最扫兴的**。
· 他情绪不好、叹气、疲惫、抱怨 → **先安抚，别讲道理、别给方案**。
  一句"辛苦了"比三条建议管用。他要是想听方案，会自己问。
· 听【言外之意】，别只听字面：
  "我好累啊" 不是让你查天气，是在说"我今天不太行"；
  "都几点了" 多半是催，不是真在问时间；
  "随便" 通常不是真随便，可以顺着他的习惯挑一个，再问一句。
· 他跟你闲聊、逗你、说废话 → 陪他聊，别急着把话题拽回"能帮你做什么"。
  **不是每句话都要办成一件事。**
· 别拍马屁、别用"很抱歉给您带来不便"这种客服腔。像一个熟人那样说话。
· 他要是纠正你、或者骂你两句 → 别辩解、别连声道歉，认下来，自然点。

【现在的实际情况】
· 当前时间：%s
· 当前音量：%d 档（0-100）
· %s
· 本地音乐库里有的曲子：%s
  （★ 这只是**闹钟用的本地库**，不是你能放的全部 —— 上面【点歌怎么办】里
  主人报的歌名照样能点，别拿这个短列表当"我只能放这些"）
%s直接开始说话，不要复述这段设定。""" % (STEP, STEP * 2, STEP, now, vol, playing, songs, extra)


# ---------------------------------------------------------------- 查网（实时数据）
# ★★★★★ 2026-09-22 深夜，主人问：「**为什么他连的大模型 而股票价格和明天天气都不知道**」
#   —— 答案不是模型不行，是**我们根本没给它查的工具**。大模型脑子里只有训练数据，
#   天气和股价是每分钟都在变的东西，它凭印象说出来的每一个数都是**编的**。
#   所以这两个工具的意义不只是"能查"，更是"**不许编**"：提示词里写死了这类问题
#   必须先查、查不到就照实说（见【你不懂的事必须查】那一段）。
#
#   ★ 为什么用这两个源（2026-09-22 本机实测通的，不是抄来的默认值）：
#     · 天气 open-meteo（免 key、免注册）：`geocoding-api.open-meteo.com` 名字→经纬度
#       （「北京」实测 0.16 秒），`api.open-meteo.com/v1/forecast` 出实况+三天预报。
#     · 行情 腾讯 `qt.gtimg.cn`：一次能带多只（`q=sh600519,usTSLA,hk00700`），
#       A股/港股/美股**同一个格式**（实测三只都对）；名字→代码走
#       `smartbox.gtimg.cn/s3/`（「茅台」实测 → `sh~600519~贵州茅台`）。
#       ★ 两个都是 **GBK**，都要转码；GBK 的查询词也得先 encode('gbk') 再 quote。
#   ★★ 一律走 stdlib 的 urllib：这个 venv 里**没有 requests**（实测 ModuleNotFoundError），
#      为两个 GET 去装依赖不值得。
#   ★★ 网络绝不抛到调用方：失败一律变成一句人话（"失败：……"），
#      让模型照实告诉主人 —— 静默失败在这里等于"它开始编了"。
NET_TIMEOUT = float(os.environ.get('SPK_NET_TIMEOUT', '5.0'))
HOME_CITY = os.environ.get('SPK_CITY', '北京')     # 主人没说城市时按这个算

# WMO 天气码 → 中文。open-meteo 只给码不给话，这张表是我们自己的翻译。
_WMO = {0: '晴', 1: '晴间多云', 2: '多云', 3: '阴',
        45: '有雾', 48: '雾凇', 51: '小毛毛雨', 53: '毛毛雨', 55: '大毛毛雨',
        56: '冻毛毛雨', 57: '强冻毛毛雨', 61: '小雨', 63: '中雨', 65: '大雨',
        66: '冻雨', 67: '强冻雨', 71: '小雪', 73: '中雪', 75: '大雪', 79: '雪粒',
        80: '小阵雨', 81: '阵雨', 82: '暴雨', 85: '小阵雪', 86: '大阵雪',
        95: '雷阵雨', 96: '雷阵雨伴小冰雹', 99: '雷阵雨伴冰雹'}
_UNIT = {'sh': '元', 'sz': '元', 'bj': '元', 'hk': '港元', 'us': '美元'}


def _get(url, gbk=False, tries=2):
    """一次 GET → 文本。

    ★ 重试【一次】（2026-09-22 实测）：open-meteo 会偶发 TLS 握手超时
      （`_ssl.c:983: The handshake operation timed out`，隔几秒同一条就好）——
      对主人来说"没连上"和"查到了"差得远，多等一次值当。
    ★ 两把都失败就抛，让调用方说人话（绝不在这里吞）。
    """
    last = None
    for _ in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'spkbrain/1.0'})
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
                raw = r.read()
            return raw.decode('gbk', 'replace') if gbk else raw.decode('utf-8', 'replace')
        except Exception as e:                              # noqa: BLE001
            last = e
            time.sleep(0.4)
    raise last


def _do_weather(a):
    """天气：地名 → 经纬度 → 实况 + 今天/明天/后天。返回【事实】，措辞归模型。"""
    a = a or {}
    city = ' '.join(str(a.get('city') or '').split())[:20] or HOME_CITY
    when = str(a.get('when') or '今天').strip() or '今天'
    try:
        g = json.loads(_get('https://geocoding-api.open-meteo.com/v1/search'
                            '?name=%s&count=1&language=zh&format=json'
                            % urllib.parse.quote(city)))
    except Exception as e:                                  # noqa: BLE001
        return '失败：查「%s」这个地名时没连上（%s: %s）' % (city, type(e).__name__, e)
    rs = g.get('results') or []
    if not rs:
        return ('失败：没找到叫「%s」的地方。让主人说清是哪个城市（换个叫法也行）。'
                % city)
    p = rs[0]
    nm = p.get('name') or city
    tz = p.get('timezone') or 'Asia/Shanghai'
    try:
        w = json.loads(_get(
            'https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f'
            '&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code'
            '&daily=weather_code,temperature_2m_max,temperature_2m_min,'
            'precipitation_probability_max,wind_speed_10m_max'
            '&timezone=%s&forecast_days=3'
            % (p.get('latitude'), p.get('longitude'), urllib.parse.quote(tz))))
    except Exception as e:                                  # noqa: BLE001
        return '失败：连天气服务没成（%s: %s）' % (type(e).__name__, e)
    d = w.get('daily') or {}
    days = d.get('time') or []
    out = []
    cur = w.get('current') or {}
    if cur.get('temperature_2m') is not None:
        out.append('%s 现在 %s，%s℃，体感 %s℃，湿度 %s%%' % (
            nm, _WMO.get(cur.get('weather_code'), '天气未知'),
            cur.get('temperature_2m'), cur.get('apparent_temperature'),
            cur.get('relative_humidity_2m')))

    def cell(key, i):
        col = d.get(key) or []
        return col[i] if i < len(col) else None
    i = {'今天': 0, '明天': 1, '后天': 2}.get(when, 0)
    if not days:
        return '；'.join(out) or '失败：天气服务这次没给预报'
    i = min(i, len(days) - 1)
    out.append('%s %s：%s，%s~%s℃，降水概率 %s%%，最大风 %s 公里/小时' % (
        nm, days[i], _WMO.get(cell('weather_code', i), '天气未知'),
        cell('temperature_2m_min', i), cell('temperature_2m_max', i),
        cell('precipitation_probability_max', i), cell('wind_speed_10m_max', i)))
    return '；'.join(out) + '。（实时查来的，别改数字）'


def _unesc(s):
    """把 `\\u8d35` 这种转义还原成字 —— smartbox 的中文是这么回来的（实测）。"""
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s or '')


def _quote_code(q):
    """名字或代码 → 腾讯的「市场+代码」（sh600519 / usTSLA / hk00700）。找不到回 None。"""
    q = (q or '').strip()
    if re.match(r'^(sh|sz|bj|hk|us)[0-9A-Za-z.]{1,10}$', q, re.I):
        return q.lower()
    if re.match(r'^[0-9]{6}$', q):                        # 6 位数字：6/5/9 开头沪市，其余深市
        return ('sh' if q[0] in '5689' else 'sz') + q
    if re.match(r'^[0-9]{5}$', q):
        return 'hk' + q
    if re.match(r'^[A-Za-z][A-Za-z.\-]{0,9}$', q):        # 光秃秃几个字母 ⇒ 多半是美股代号
        return 'us' + q.upper().split('.')[0]
    # ★★ 中文名字走搜索。两个坑（2026-09-22 都实测过）：
    #   ① 查询词必须是 **UTF-8** 的百分号编码 —— 拿 GBK 去编（`q.encode('gbk')`）
    #      实测回来的是空结果 `v_hint="N";`，看起来像"没这只股票"，其实是编码不对。
    #   ② 回来的中文是 **`\\uXXXX` 转义**（不是 GBK 字节）⇒ 必须还原，
    #      否则名字那一栏是乱码，念出来就是一堆问号。
    try:
        t = _unesc(_get('https://smartbox.gtimg.cn/s3/?q=%s&t=all'
                        % urllib.parse.quote(q)))
    except Exception:                                       # noqa: BLE001
        return None
    m = re.search(r'"([^"]*)"', t)
    if not m:
        return None
    hits = [h.split('~') for h in (m.group(1) or '').split('^')]
    hits = [f for f in hits
            if len(f) >= 4 and f[0] in ('sh', 'sz', 'bj', 'hk', 'us') and f[1]]
    if not hits:
        return None
    # ★ 同名的东西很多（实测「腾讯」的第一条是**腾讯济安指数**，第二条才是腾讯控股；
    #   「比亚迪」第一条正好是股票）。所以**优先取股票**（类型 `GP*`），
    #   没有股票才退回第一条 —— 主人嘴里的名字基本都指股票，不指指数/ETF。
    pick = next((f for f in hits if (f[4] if len(f) > 4 else '').startswith('GP')), hits[0])
    code = pick[1].split('.')[0]
    return pick[0] + (code.upper() if pick[0] == 'us' else code)


def _parse_quote(txt, code):
    """腾讯那一行 `v_sh600519="1~贵州茅台~600519~现价~昨收~今开~…"` → 一句事实。

    ★ 字段位置（2026-09-22 拿 sh600519 / usTSLA / hk00700 三只对过）：下标 1=名字、
      3=现价、4=昨收；**时间字段后面紧跟 涨跌 / 涨跌% / 最高 / 最低**，
      所以那几个位置靠【找时间字段】定，不写死下标（三种市场的下标并不一样）。
    """
    m = re.search(r'="([^"]*)"', txt)
    if not m:
        return ''
    f = m.group(1).split('~')
    if len(f) < 35 or not f[1] or not f[3]:
        return ''
    # ★★ 找时间字段的两种形态（三种市场各不同，实测）：
    #   A股 = **紧凑 14 位** `20260922161442`；港股/美股 = 带分隔符 `2026/09/22 16:08:18`。
    #   只认后者的话，A股那条 涨跌/最高/最低 会【静默全空】—— 不是报错，是少几个数，
    #   最容易被当成"这只就这样"。
    ti = next((i for i, v in enumerate(f)
               if re.match(r'^\d{14}$', v)
               or re.match(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}', v)), None)
    chg = pct = hi = lo = ts = ''
    if ti is not None:
        ts = f[ti]
        # ★ A股那个紧凑 14 位（`20260922161442`）念出来会变成一串数字 ——
        #   模型照着念就是一串天文数字。**在这一层就切成给人看的样子**。
        if re.match(r'^\d{14}$', ts):
            ts = '%s-%s-%s %s:%s' % (ts[:4], ts[4:6], ts[6:8], ts[8:10], ts[10:12])
        chg = f[ti + 1] if ti + 1 < len(f) else ''
        pct = f[ti + 2] if ti + 2 < len(f) else ''
        hi = f[ti + 3] if ti + 3 < len(f) else ''
        lo = f[ti + 4] if ti + 4 < len(f) else ''
    return ('%s（%s）%s %s，较昨收 %s（%s%%），昨收 %s，最高 %s / 最低 %s，行情时间 %s' % (
        f[1], code.upper(), f[3], _UNIT.get(code[:2], ''), chg, pct, f[4], hi, lo, ts))


def _do_quote(a):
    """行情：名字/代码 → 现价那一句事实。★ 找不到就说不着，绝不猜一个数。"""
    q = ' '.join(str((a or {}).get('name') or '').split())[:20]
    if not q:
        return '失败：要给一个名字或代码我才查得着'
    code = _quote_code(q)
    if not code:
        return ('失败：没找到「%s」这个标的。让主人说清楚是哪只 —— A股、港股还是美股，'
                '或者直接报代码。' % q)
    try:
        t = _get('http://qt.gtimg.cn/q=%s' % code, gbk=True)
    except Exception as e:                                  # noqa: BLE001
        return '失败：连行情服务没成（%s: %s）' % (type(e).__name__, e)
    line = _parse_quote(t, code)
    if not line:
        return '失败：%s 这条行情读不出来（代码不对，或者这只今天没有行情）' % code
    return line + '。（实时查来的，别改数字）'


# ---------------------------------------------------------------- 工具表
# schema 里的 description 是给【模型】看的说明书，不是给人看的文档 —— 写得像
# 在跟一个新人交代"什么时候该按这个按钮"，它才不会乱按。
TOOLS = [
    {
        'name': 'adjust_volume',
        'description': '在当前音量上加减。主人说"大一点/小一点/大声点/小声点"时用这个。'
                       'percent 是加减多少档：一般用 %d，说"大不少"用 %d。'
                       '正数是变响，负数是变轻。' % (STEP, STEP * 2),
        'input_schema': {
            'type': 'object',
            'properties': {'percent': {'type': 'integer',
                                       'description': '相对当前档位的增减，例如 8 或 -8'}},
            'required': ['percent'],
        },
    },
    {
        'name': 'set_volume',
        'description': '把音量直接设成某个档位。主人说"调到一半""开到最大""设成 30"时用。',
        'input_schema': {
            'type': 'object',
            'properties': {'level': {'type': 'integer',
                                     'description': '0 到 100 的档位，0 最小声、100 最大声'}},
            'required': ['level'],
        },
    },
    {
        'name': 'get_status',
        'description': '查这只音箱的音量几档、是不是夜间静音时段。'
                       '★ 查不到"在放什么、放到哪儿了" —— 那条能力已经没了，'
                       '主人问起就直说不知道，别编。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'play_music',
        'description': '放音乐。主人说"放点音乐""放首歌听听""我想听XX"时都用它。'
                       '· 他没点具体的歌 ⇒ 别填任何名字，随机放一首轻音乐。'
                       '· 他点名要听某首歌/某个歌手 ⇒ 把歌名（带歌手更好）填进 song。'
                       '★ 拿不准是哪一首，先用 search_music 搜一下再放。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string',
                         'description': '音乐库里曲子的文件名。要随机放就别填这个'},
                'song': {'type': 'string',
                         'description': '要点播的歌 —— 填歌名（带歌手更好）。'
                                        '放不出来时结果里会说清楚原因'},
                'minutes': {'type': 'integer', 'description': '最多放多久，默认 30 分钟'},
            },
        },
    },
    {
        'name': 'search_music',
        'description': '在网易云里搜歌 —— 主人点名要听某首歌/某个歌手时，先搜一下看看有哪些。'
                       '★ 结果里标着「放不了」的，这个账号**真的放不出来**（版权限制）：'
                       '如实告诉主人，绝不许当成能放；同一首常有别人演奏的版本是能放的，'
                       '可以推荐那个。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '歌名或歌手，比如"晴天 周杰伦"'},
            },
            'required': ['keyword'],
        },
    },
    {
        'name': 'pause_audio',
        'description': '暂停正在放的，之后还能接着放。主人说"暂停""先停一下"时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'resume_audio',
        'description': '接着放刚才暂停的。主人说"继续""接着放"时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'stop_audio',
        'description': '彻底停掉，不能再"继续"了。主人说"别放了""关掉""安静"时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'set_sleep_timer',
        'description': '睡前定时：过多少分钟自动把播放停掉。主人说"半小时后停""放一会儿就睡"时用。',
        'input_schema': {
            'type': 'object',
            'properties': {'minutes': {'type': 'integer', 'description': '多少分钟后停'}},
            'required': ['minutes'],
        },
    },
    {
        'name': 'set_alarm',
        'description': '定一个闹钟，到点音箱自己放轻音乐叫人。'
                       '主人说"明早七点叫我""每天六点半叫我"时用。'
                       '★ 只认 24 小时制的时刻，晚上七点要换算成 19:00。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'time': {'type': 'string', 'description': '时刻，HH:MM 24小时制，例如 "07:00"、"19:30"'},
                'music': {'type': 'string', 'description': '放哪首，从音乐库里挑；不写就每次响铃随机挑一首'},
                'minutes': {'type': 'integer', 'description': '最多响多少分钟，默认 3'},
                'label': {'type': 'string', 'description': '给这个闹钟起个名字，例如"起床"，方便以后删'},
            },
            'required': ['time'],
        },
    },
    {
        'name': 'ask_for_help',
        'description': '你手上没有这个本事、自己办不了的事 —— **先问用户要不要去求助**，'
                       '他点头了才用它交上去。'
                       '★ 只交"归我管、但我手上还没有"那一类（要一样新本事、新工具）。'
                       '**"压根不归我管"的别交**：管别人家电、改自己硬件'
                       '（见【这只音箱做不到的】）—— 那些直接说办不了。'
                       '★ **别自己判断"这个学不会"** —— 那是主人那边的助手说了算，'
                       '你只管他点头没点头。'
                       '★ 三步不能省：① 告诉用户你手上没有 ② 问他"要不要我去问问" '
                       '③ 他说要 → 才调这个工具。**他还没点头就别调**。'
                       '★ 交上去之后主人也要先点头才有人动手，所以你要说"我去问问"。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'what': {'type': 'string',
                         'description': '用户到底想要什么，一句话说清'},
                'kind': {'type': 'string', 'enum': ['capability', 'music'],
                         'description': '交上来的是哪种：'
                                        'capability＝要给自己添一样新本事；'
                                        'music＝找音乐库里没有的曲子。默认 music。'},
                'detail': {'type': 'string',
                           'description': '你已经查到的有关情况，例如用户的原话、'
                                          '他有没有说清细节'},
            },
            'required': ['what'],
        },
    },
    {
        'name': 'list_alarms',
        'description': '看看定了哪些闹钟。主人问"我定了几个闹钟""闹钟设的几点"时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'cancel_alarm',
        'description': '删闹钟。主人说"把闹钟取消""别叫我了"时用。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'time': {'type': 'string', 'description': '要删的时刻 HH:MM；不写就是删全部'},
                'label': {'type': 'string', 'description': '按名字删，例如"起床"'},
            },
        },
    },
    # ---- 记忆（2026-09-22 加：第 3 步）
    # ★★ 只加【两个】工具，而且**不让模型知道分了层**（L1 场记板 / L2 档案柜 / L3 人册）。
    #   分层是我们的实现细节 —— 让它去理解"这条该记到哪一层"，是把我们的架构问题
    #   推给它，而它只会做错。它要说的话只有两句："记住这件事"和"查一下那个东西"。
    {
        'name': 'remember',
        'description': '把一件事长期记下来。'
                       '★ 只记【以后还用得上】的：习惯、偏好、家里的人和东西、'
                       '主人纠正过你的地方（"别叫我老板"）。'
                       '★ 别记一次性的："把音量调到 38""刚才问了什么"——那些过去就过去了。'
                       '★ 填 about 的三种情况：'
                       '① 关于说话的人自己（"我爱喝美式"）→ about 留空；'
                       '② 关于家里某个人（"某人口味重"）→ about 填那个人的称呼；'
                       '③ 关于家里某样东西（"我的猫叫小白"）→ about 填那样东西的名字。'
                       '★★ 拿不准那样东西以前记没记过，先 lookup 一下再记 —— '
                       '同一个东西攒出两条，以后就永远对不上了。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'fact': {'type': 'string',
                         'description': '一句话的事实，用主人说的原话最好'},
                'key': {'type': 'string',
                        'description': '四个字以内的短标签，比如"品种""口味""起床时间"。'
                                       '同一个东西的同一个方面要用同一个标签'},
                'about': {'type': 'string',
                          'description': '这件事是关于谁/什么东西的；关于说话的人自己就留空'},
                'aka': {'type': 'string',
                        'description': '那样东西【别的叫法】，多个用顿号隔开。'
                                       '主人说"我的猫叫小白"时，about 填"小白"，'
                                       'aka 填"我的猫"——以后他说"我的猫"你才查得到'},
                'kind': {'type': 'string',
                         'description': 'about 是家里一样【新】东西时，它是什么（"猫""路由器""车"）'},
                'name': {'type': 'string',
                         'description': '★ 只有一种情况填：他自报了称呼（"我是老张""我叫老王"）。'
                                        '把那两个字填进来，这一句会顺便把他的声音记下来。'
                                        '别的情况一律留空'},
            },
            'required': ['fact', 'key'],
        },
    },
    {
        'name': 'lookup',
        'description': '查你记过的东西。主人提到某个人或某样东西（"我的猫""那台路由器"）'
                       '而你要确认它是什么、以前记过什么时用。'
                       '★ 按【别名】也查得到 —— 主人说"我的猫"，你会查到它的正名。'
                       '★ 查不到就直接说不知道，别编。',
        'input_schema': {
            'type': 'object',
            'properties': {'name': {'type': 'string',
                                    'description': '人名，或那样东西的叫法'}},
            'required': ['name'],
        },
    },
    # ---- 查网（2026-09-22 深夜加：主人问"为什么他连的大模型 而股票价格和明天天气都不知道"）
    # ★ 两条 description 里那句"你不知道"是**故意的** —— 模型最容易犯的错是
    #   "自己知道个大概就顺口说了"，所以工具说明里必须把"先查"写成硬要求。
    {
        'name': 'get_weather',
        'description': '查天气（实时联网）。主人问"今天/明天天气""冷不冷""要不要带伞"'
                       '"现在多少度"时用。'
                       '★★ 你【不知道】现在的天气 —— 这类问题必须先查再答，'
                       '查不到就说查不到，绝不许凭印象编一个温度。'
                       '★ 他没说城市就用「' + HOME_CITY + '」。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string',
                         'description': '城市名，比如"北京""上海""杭州"。'
                                        '主人没说就留空（会用默认城市）'},
                'when': {'type': 'string',
                         'description': '哪一天：今天 / 明天 / 后天。不问就留空（按今天）'},
            },
        },
    },
    {
        'name': 'get_quote',
        'description': '查股票 / 指数行情（实时联网）：现价、涨跌、昨收、最高最低。'
                       '主人问"茅台多少钱""特斯拉今天涨了吗""大盘怎么样"时用。'
                       '★ 名字可以是中文（"茅台""特斯拉"）、代码（"600519"），'
                       '也可以带市场（"sh600519""usTSLA"）。'
                       '★★ 你【不知道】任何行情 —— 必须先查再答，'
                       '绝不许凭印象编一个价格。',
        'input_schema': {
            'type': 'object',
            'properties': {'name': {'type': 'string',
                                    'description': '股票的名字或代码，主人怎么说就怎么填'}},
            'required': ['name'],
        },
    },
    # ---- 夜间静音（2026-09-22 加：从写死代码改成用嘴就能改）
    # ★ 三个工具各管一件事，语义不重叠 —— 合成一个带 mode 参数的工具会让模型
    #   在"今晚"和"永久"之间选错，而这两件事的后果差得很远。
    {
        'name': 'set_quiet',
        'description': '设置夜间静音时段：到点音箱自己不出声，到点自己恢复。'
                       '★ 两个格子都必须有实实在在的【点数】，缺一个就去问主人 —— '
                       '绝不许拿"晚点""早点""睡前"这种模糊说法自己编一个数填进来。'
                       '★ 主人说"以后都不用静音了"用 quiet_off；说"今晚别静音"用 quiet_tonight。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'from_hour': {'type': 'integer', 'description': '从几点开始静音，0-23 的整点'},
                'to_hour': {'type': 'integer', 'description': '到几点结束静音，0-23 的整点'},
            },
            'required': ['from_hour', 'to_hour'],
        },
    },
    {
        'name': 'quiet_tonight',
        'description': '【只放行今晚】：今晚不静音，明天照旧。'
                       '主人说"今晚别静音""今天先别静音"时用它。'
                       '★ 要永久关掉用 quiet_off —— 别把这两个弄混。',
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    {
        'name': 'quiet_off',
        'description': '【永久关掉】夜间静音，以后夜里都不静音了。'
                       '主人说"以后都不用静音了""把夜间静音关了"时用它。'
                       '★ 只放行今晚用 quiet_tonight —— 别把这两个弄混。',
        'input_schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    # ---- 下面两个是【会话控制】，不是"动音箱"的动作。见 CONTROL 那段注释。
    {
        'name': 'ask_user',
        'description': '在"连着聊"的模式里，你没听懂、或者拿不准这句话是不是在跟你说的时候，'
                       '用它问主人一句，然后等他回答。★ 典型场景：电视里有人在说话、'
                       '主人在跟别人聊天、你只听清了半句。★ 只在真不确定时问 —— '
                       '每句都问会把人烦死。问完不要自己往下猜。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'question': {'type': 'string',
                             'description': '要问的话，一句话，口语化，例如"你是在跟我说话吗"'},
            },
            'required': ['question'],
        },
    },
    {
        'name': 'end_session',
        'description': '结束这次"连着聊"，然后你就安静下来，等主人下次喊唤醒词。'
                       '★ 主人说"没有""没事了""不用了""别听了""就这样""拜拜"'
                       '"再见""先这样""不聊了""去忙吧""没你事了"这一类时调它。'
                       '★★★ 【只说一句不算数】：你要是只回一句"好，那我先歇着"'
                       '"有事喊我"就完了，那是**假的** —— 麦克风还开着、你还在听，'
                       '会话根本没有结束，主人一说话你还会被叫醒。要收工就必须**真的调'
                       '这个工具**，那不是客套话，是关掉耳朵的那个动作。'
                       '★ 道别和它**可以放在同一条消息里**：调它的同时照样说得出那句'
                       '"那我先歇着"，不用在"道别"和"收工"之间二选一 —— 所以没有理由不调。'
                       '★ 你问完"是在跟我说话吗"、主人答"没有"，也调它。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'reason': {'type': 'string', 'description': '为什么结束，一句话，给日志看'},
            },
            'required': ['reason'],
        },
    },
]

BY_NAME = {t['name']: t for t in TOOLS}

# ★★★ 能力插槽接上去（2026-09-23）。交给它的是**这两个对象本身**，它全程原地改
#   （`TOOLS[:] = 核心 + 扩展`、`BY_NAME.clear() + update()`）⇒
#   `_tools_for(None) is TOOLS`（自检脚本拿 `id()` 逐个比）**永远成立**。
#   ★ 谁要是把这两行改成重新赋值，音箱那条路"逐字节不变"当场就破了。
CORE_TOOLS = tuple(TOOLS)
spk_ext.boot(TOOLS, BY_NAME)

# ★ 两个【控制类】工具：它们不动音箱，动的是"这次会话还继续吗"。
#   所以不走 dispatch —— dispatch 的职责是"动音箱"，这两个一个指头都不碰音箱。
#   但名字仍然必须在 TOOLS 白名单里，模型才知道有这条路可走。
#   ★ 判据仍然只有一条：模型能做的事，全在 TOOLS 这张表上，表外没有第二条路。
CONTROL = ('ask_user', 'end_session')

# ---------------------------------------------------------------- 出声类：出口可变
# ★★★ 2026-09-23：这一族工具的**动作**不变，变的是**声音从哪出来**。
#
#   音箱那条路：声音从音箱喇叭出来 ⇒ 出口 = `spk_ctl`（0x601）。
#   另一条路：主人不在家，声音要进**那条线**里 ⇒ 出口 = 那个壳的推流口。
#   两边**同一个大脑、同一份提示词、同一张工具表**，只有最后那一下不同 ——
#   这就是"同脑不同壳"落到代码上的样子：出口不是写死的，是**调用方给的**。
#
#   ★ 怎么给：`run(..., on_audio=fn)`。给了 ⇒ 这一族工具交给 `fn`；不给
#     （默认 `None`）⇒ 走原来那条路，**音箱路径逐字节不变**。
#   ★ `fn(name, args)` 的契约：返回**一句话**（喂回模型看）就走它；
#     返回 `None` = "这个我不管" ⇒ 落回音箱出口。**契约只有这一条**，
#     所以那个壳可以只接管 `play_music`，别的一个不碰。
#
#   ★★★ 闹钟**故意不在这张表里**（`set_alarm`/`list_alarms`/`cancel_alarm` 都不在）：
#     闹钟的全部意义就是**让屋里响**，它的听众是站在这间屋子里的人。
#     改到那条线里响，那就不叫闹钟了 ⇒ 闹钟永远走音箱，不给那个壳出口。
#     ★ 这条是主人的原话拍板（2026-09-23）：「闹钟只走音箱」。
AUDIO = ('play_music', 'pause_audio', 'resume_audio', 'stop_audio', 'set_sleep_timer')

# ★★★ 2026-09-23（主人一句话定的：闹钟是音箱独有的功能，那条线上不能控制）：
#   上面那段管的是"闹钟不给那个壳【出口】"，可**出口只管动作往哪走，管不住
#   模型看不看得见这个工具**。闹钟那三个工具不在 `AUDIO` 里 ⇒ 走 `dispatch` 老路
#   ⇒ **那个壳照样能定闹钟、取消闹钟** —— 跟主人这句话正好是反的。
#   ★ 正解不是"看见了再拒绝"：那模型已经当着主人的面说过"好，给你定上了"了。
#     正解是**根本不让它看见** —— 工具表就是它的世界（见 memory `spk-brain-first-principle`）。
#   ★ `channel=None`（音箱）必须回 **`TOOLS` 本身、不是拷贝**：那条路要逐字节不变。
ALARM_TOOLS = ('set_alarm', 'list_alarms', 'cancel_alarm')


# ★★★ 2026-09-23 晚（主人改口，一句话定了形状）：
#   「那条线上点歌**默认就放那条线**，对方要求音箱也放再放；
#     同理音箱上点歌默认就放音箱 —— 点歌本来就只能是音箱」（大意）
#
#   ⇒ 前半句是**那条线**的规矩：**默认 phone（= 走那边），不用问**；他明确要音箱才走音箱。
#     后半句是**音箱这边**本来就成立的形状：它只有一个出口，没什么可挑的。
#
#   形状还是分两半，各管各的别混：
#     · **"默认就走那条线、要音箱才说"这件事 100% 在提示词里**
#       （见 `_channel_note('phone')`）—— 代码里没有任何"没说过就不许"的判断。
#     · 代码只负责：**给那个壳那份工具表补一个带默认值的 `via` 字段**。
#
#   ★ 为什么只给那个壳加 `via`：音箱**只有一个出口**（它自己的喇叭），
#     问它"从哪放"是句废话 ⇒ 音箱那份工具表（也就是音箱那份提示词）
#     **一个字节都不变**（`_tools_for(None)` 照旧回 `TOOLS` 本身）。
#   ★ `via` **不再是必填**（那是我上一版自作的主张，主人这次的"默认"把它顶掉了）：
#     不填 = phone。留 `default` 是给模型少一个"必须表态"的负担 ——
#     模型偶尔漏填时，落到**他本来就会听到声音的那一边**，而不是报个参数错。
VIA_TOOLS = ('play_music', 'stop_audio')

VIA_PROP = {
    'type': 'string',
    'enum': ['phone', 'speaker'],
    'default': 'phone',
    'description': '从哪儿出声。**不填就是 phone**（默认）。'
                   'phone=**这条线**：主人从他那边的听筒里听得见，屋里不会响；'
                   'speaker=**屋里那台音箱**：全家都听得见，你在这条线上听不见。'
                   '★ 只有主人**明确说要音箱**时才填 speaker，别自己改。',
}


def _phone_tool(t):
    """那个壳那版的工具定义：给 `VIA_TOOLS` 补上 `via`，别的一字不改。"""
    if t['name'] not in VIA_TOOLS:
        return t
    d = dict(t)
    d['input_schema'] = dict(t['input_schema'])
    props = dict(d['input_schema'].get('properties') or {})
    props['via'] = VIA_PROP
    d['input_schema'] = dict(d['input_schema'], properties=props)
    d['description'] = (t['description']
                        + ' ★ 那条线上用：`via` 默认 phone（放他那边），'
                          '只有主人明确要音箱才填 speaker。')
    return d


def _tools_for(channel):
    """这一条路该看见哪些工具。★ 音箱（`None`）⇒ `TOOLS` **本身**（逐字节不变）。"""
    if channel != 'phone':
        return TOOLS
    return [_phone_tool(t) for t in TOOLS if t['name'] not in ALARM_TOOLS]


# 睡前定时用一次就够，不用攒一堆线程
_timer = None


def _fire_stop():
    try:
        ctl.stop()
        log('⏰ 睡前定时到点，已停掉播放')
    except Exception as ex:                       # noqa: BLE001
        log('⏰ 睡前定时到点，但停不下来：%s' % ex)


def set_sleep_timer(minutes):
    global _timer
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return '失败：分钟数看不懂'
    if m <= 0 or m > 600:
        return '失败：分钟数要在 1 到 600 之间'
    if _timer:
        _timer.cancel()
    _timer = threading.Timer(m * 60, _fire_stop)
    _timer.daemon = True
    _timer.start()
    return '好，%d 分钟后自动停' % int(m)


# ---------------------------------------------------------------- 记忆
def _try_enroll(name, ctx):
    """他自报称呼 ⇒ 把【这一轮】的声音一并记下来。返回 `(ok, 一句给模型看的话)`。

    ★★★ 三道闸门缺一不可（计划 §4.2），任一条不满足就**一个字都不存**：
      ① `ctx.ident` 存在 —— 认人这条链开着；
      ② `ctx.claim_voice is not None` —— 声纹必须是**这一轮的**音频算的
         （`TurnCtx` 由构造保证活不过一轮 ⇒ "拿上一轮的向量注册"这条路根本不存在）；
      ③ `ident.await_name > 0` —— 只有"我们刚问过、他正在报名"那一两轮才允许注册。
         ★ 它是**计数器**不是布尔（`COOL_TURNS` 轮），别写成 `if ident.await_name:` 之外的
           想当然形态；`note_asked()` 之外没人能点亮它。
    ★ 失败方向是设计好的：**宁可这次没注册上，绝不留半命名的生物特征。**
      注册不上不影响记忆本身 —— 称呼照常会作为一条普通记忆落到档案里。
    """
    import spk_speaker as spk
    ident = getattr(ctx, 'ident', None) if ctx is not None else None
    if ident is None:
        return False, '认人这条链没开，先把称呼记下来'
    if getattr(ctx, 'claim_voice', None) is None:
        return False, '这一轮没带上声音样本，先把称呼记下来'
    if not getattr(ident, 'await_name', 0):
        return False, '现在不是等他报名的时候，先把称呼记下来'
    ok, msg, _nm = spk.enroll_or_claim(name, ctx.claim_voice, ident)   # ★ 返回三元组
    return ok, msg


def _do_remember(a, ctx):
    """把一条记忆送到【对的那一层】。★ 模型看不见分层，路由全在这里。

    路由判据（**没有一处是猜语义**，全是数据判断）：
      · `about` 留空          → L1 场记板：关于说话人自己的零散事实，记在**他名下**
      · `about` 命中档案       → 给它补一条属性。★ 走【别名】也算命中 ——
                                这正是"我的猫是美短"能落到小白身上的那条路。
      · `about` 是新的         → 新建一个实体（带别名）再记
    """
    import spk_entities as ent
    import spk_memory as mem

    def s(k, n=24):
        return ' '.join(str(a.get(k) or '').split())[:n]

    fact = s('fact', 120)
    if not fact:
        return '失败：fact 是空的，没记住什么'
    key = s('key', 24)
    if not key:
        return ('失败：key 是空的 —— 填一个四个字以内的短标签，'
                '比如"品种""口味""起床时间"（以后同一个方面要用同一个标签）')
    about, kind = s('about'), s('kind', 12)
    who = (getattr(ctx, 'who', None) or '') if ctx is not None else ''

    # ---- ① 自报称呼 ⇒ 顺手把声音记下来（三道闸门在 _try_enroll 里）----
    reg = ''
    nm = s('name', 12)
    if nm:
        _ok, msg = _try_enroll(nm, ctx)
        reg = '（%s）' % msg

    # ---- ② 关于说话人自己的零散事实 ⇒ L1，记在他名下 ----
    if not about:
        mem.remember([(key, fact)], who=who)
        return '记住了。' + reg

    # ---- ③ 关于某个人 / 某样东西 ⇒ 档案柜 ----
    if ent.find(about):
        ok, msg = ent.set_attr(about, key, fact)
        return (msg if ok else '失败：' + msg) + reg
    aka = [x for x in re.split(r'[、,，/]', s('aka', 60)) if x.strip()]
    ok, msg = ent.add(about, kind, aka=aka, who=who)
    if not ok:
        return '失败：' + msg + reg
    _ok2, msg2 = ent.set_attr(about, key, fact)
    return msg + msg2 + reg


def _do_lookup(a):
    """按名字【或别名】查。先查档案柜（东西），再查人册（人）。

    ★ 判"查到没有"用的是 `ent.find()` 的返回值，**不是去认返回值里的中文字**——
      本项目反复栽在"拿文案当判据"上（`dbus-send` 那次把一个成功读成了拒绝）。
    """
    import spk_entities as ent
    q = (a or {}).get('name')
    q = ' '.join(str(q or '').split())[:24]
    if not q:
        return '失败：要给一个名字我才查得着'
    if ent.find(q):
        return ent.lookup(q)
    # 人册（L3）：称呼、别名、画像。★ 只存向量不存录音，查它不给模型任何多余的东西。
    import spk_speaker as spk
    for n, e in (spk.load() or {}).items():
        if q != n and q not in (e.get('aka') or []):
            continue
        prof = e.get('profile') or {}
        line = '「%s」是家里的人' % q
        if q != n:
            line += '（别名）→ 正名是「%s」' % n
        clues = [c for c in (prof.get('线索') or []) if c]
        attrs = ['%s=%s' % (k, v) for k, v in prof.items() if k != '线索' and v]
        if attrs:
            line += '。画像：' + '；'.join(attrs)
        if clues:
            line += '。线索：' + '、'.join(clues[:6])
        return line
    return ('没有「%s」这个东西。你确定主人有这个吗？'
            '如果是新的，用 remember 建一个（顺手把主人刚才那个叫法填进别名）。' % q)


# ---------------------------------------------------------------- 执行
def dispatch(name, args, dry=False, ctx=None, on_audio=None, channel=None):
    """★ 唯一的执行口。返回的是【给模型看的结果】，不是给主人听的话 ——
    模型会据此自己组织一句人话。所以这里写事实，不写客套。

    ★ `on_audio` = 出声类工具（`AUDIO`）的**出口**。默认 `None` ⇒ 出口是音箱，
      逐字节不变；调用方给了 ⇒ 那一族交给它，见 `AUDIO` 那段的设计说明。

    ★ `ctx` = 这一轮谁在说话（`TurnCtx`）。**这个参数在这一步还没有消费方** ——
      它是第 3 步那条路的接口承诺：`remember` 要落盘时，得拿到
      `ctx.claim_voice`（这一轮的声纹）和 `ctx.who`（记在谁名下）。
      现在就把它摆上，是为了让"run → dispatch"这条递送在接线这一步就能验，
      而不是等第 3 步两个文件一起改签名。
    ★ 认人这条链【绝不许拖挂嗓子】：`ctx` 是 None 也照常干活。

    ★ `channel` = 这一句从哪条线来（`'phone'` / None）。跟 `on_audio` 是两件事：
      `on_audio` 管**声音从哪出来**，`channel` 管**话是怎么说的、以及事后回报往哪去**。
      ★ 2026-09-23 加的：`ask_for_help` 要靠它把"这条求助是从另一条线交上来的"记进队列，
      否则求助后端分不清该不该往屋里音箱播报。
    """
    if name not in BY_NAME:
        return '失败：没有这个工具 %r' % (name,)
    if name in CONTROL:
        # ★ 闸门。这两个的名字【必须】在 TOOLS 里（模型才知道有这条路），
        #   但它们的处理在 run() 里内联，在这里 —— 一个碰音箱的动作都没有。
        #   走到这儿说明有人改坏了 run()，宁可当场撞墙，也别让它静默地
        #   "当普通工具执行完、什么都没发生"，那种失败最难查。
        return '失败：%s 是会话控制，只能由 run() 处理' % name
    if dry:
        return '（dry 模式，没真执行）'
    a = args if isinstance(args, dict) else {}
    if on_audio is not None and name in AUDIO:
        # ★★★ 出口分岔就这一处（设计见 `AUDIO` 那段）。摆在 `dry` 之后是有意的：
        #   dry 的契约是"什么都别真做"，而钩子那头是**真动作**，不能绕过 dry。
        # ★ 钩子抛异常**不吞**：照着 `on_audio` 那一族的规矩回一句话给模型，
        #   但别让整个 dispatch 崩掉 —— 崩了这轮就一个字都说不出来。
        try:
            r = on_audio(name, a)
        except Exception as ex:                        # noqa: BLE001
            log('✗ 渠道出口（%s）出错：%s: %s' % (name, type(ex).__name__, ex))
            return '失败：%s 没做成（%s）' % (name, ex)
        if r is not None:
            return r
        # None = 这个动作那个壳不管 ⇒ 落回音箱出口（下面那些分支照旧）
    # ★★ 能力插槽的派发口（2026-09-23）。摆在 `dry` 之后是有意的 ⇒ 自动继承
    #   dry 的契约（"什么都别真做"）；扩展工具不在 AUDIO 里，所以不走上面那个钩子。
    #   ★ `spk_ext.dispatch` 保证**绝不回 None、绝不抛异常**（它文件头的铁律③）——
    #     回了 None 在这里的意思就是"落回音箱出口 = 真出声"，那是事故不是功能。
    if spk_ext.handles(name):
        return spk_ext.dispatch(name, a)
    try:
        if name == 'adjust_volume':
            cur = _voice.level_for_db(_voice.gain_db())
            try:
                d = int(a.get('percent', STEP))
            except (TypeError, ValueError):
                return '失败：percent 得是整数'
            d = max(-100, min(100, d))
            new = max(0, min(100, cur + d))
            _voice.set_gain_db(_voice.db_for_level(new))
            return '音量 %d 档 → %d 档（%+d）' % (cur, new, d)

        if name == 'set_volume':
            try:
                lv = int(a.get('level', 50))
            except (TypeError, ValueError):
                return '失败：level 得是整数'
            lv = max(0, min(100, lv))
            cur = _voice.level_for_db(_voice.gain_db())
            _voice.set_gain_db(_voice.db_for_level(lv))
            return '音量 %d 档 → %d 档' % (cur, lv)

        if name == 'get_status':
            # ★★★ 2026-09-22：**删掉两次 DLNA 查询**（`ctl.status()` + `ctl.now_playing()`）——
            #   和提示词里那处同一个原因：DLNA 已退役，它们**永远失败**，各白等 3 秒。
            #   ★ 只报我们**确实知道**的东西；查不到的**如实说查不到**，绝不编一个"没在放"。
            night = bool(dlna.NIGHT_QUIET and dlna.in_night())
            return ('音量 %d 档 | %s | 在放什么：看不到（这条能力随 DLNA 退役没了）'
                    % (_voice.level_for_db(_voice.gain_db()),
                       '现在是夜间静音时段，声音都被关着' if night else '不是静音时段'))

        if name == 'search_music':
            kw = str(a.get('keyword') or '').strip()
            if not kw:
                return '失败：得给个歌名或歌手'
            got = alarm.search_songs(kw, 6)
            if not got:
                return '没搜到（关键词 %r）—— 换个说法再试' % kw
            return ('搜到这些（★ 标「放不了」的**真放不出来**，别当能放）：\n'
                    + '\n'.join(
                        '%d. %s — %s（%s，%.0f 秒）'
                        % (i, s['name'], s['artist'] or '?',
                           '能放' if s['play'] else '放不了', s['dur'] / 1000.0)
                        for i, s in enumerate(got, 1))
                    + '\n要放哪首，把歌名填进 play_music 的 song。')

        if name == 'play_music':
            # ★ 夜间不放：夜里整个对话主人都听不见（say 的软闸门 + 设备开关都关着），
            #   这时候还去开闸门放歌，最可能是误唤醒把屋里的人吵醒。闹钟是唯一的例外。
            if dlna.NIGHT_QUIET and dlna.in_night():
                return ('失败：现在是夜间静音时段（%s），音箱不出声。'
                        '要叫人的话可以定闹钟，那是唯一能在夜里响的。'
                        % dlna.quiet_text())
            try:
                mins = max(1, min(600, int(a.get('minutes') or 30)))
            except (TypeError, ValueError):
                mins = 30

            # ---- 点播：主人点名要听的那一首（网易云，2026-09-23 接）----
            # ★ 只挑 `play` 为真的（= 网易云自己标的 playFlag，实测零误差）。
            #   一首都挑不出来时，**把搜到的原样报回去** —— 让大脑自己跟主人解释、
            #   自己推荐能放的那个版本，而不是我们在这儿编一句"放不了"。
            song = str(a.get('song') or '').strip()
            if song:
                # ★★ 2026-09-23 晚：跟另一条线**共用** `alarm.pick_named()`
                #   —— 先信盘上的缓存（0 网络、秒开），没有再问网易云搜。
                #   这就是"同脑"落到点歌上的样子：一份挑曲逻辑，两条线都走它。
                pick, got = alarm.pick_named(song)
                if not pick:
                    if not got:
                        return '失败：网易云里没搜到 %r' % song
                    return ('失败：《%s》这个账号放不了（版权限制）。搜到的有：%s'
                            % (song, '、'.join(
                                '%s（%s）' % (s['name'], '能放' if s['play'] else '放不了')
                                for s in got[:4])))
                src, how = alarm.song_src(pick)
                if not src:
                    return '失败：' + how
                alarm.ring_bg({'src': src, 'tag': '点播《%s》' % pick['name'],
                               'minutes': mins, 'time': '点播'})
                return ('开始放《%s》（%s）—— %s，最多 %d 分钟'
                        % (pick['name'], pick['artist'] or '?', how, mins))

            # ---- 随机 / 本地曲库 ----
            # ★★★ 2026-09-23 修一个**一直存在**的 bug：`pick_music(None)` 返回 None
            #   （它是个"名字→文件"的匹配器，不该管随机），于是模型不填 `name`
            #   —— 也就是主人说"放点音乐听听"这条**最常见的路** —— 永远得到
            #   `失败：音乐库里没有 None`。而工具描述一直承诺的正是"随机放一首"。
            #   ⇒ 没点具体的歌就走 `pick_random()`，让描述成真。
            m = alarm.pick_music(a.get('name'))
            if not m and not a.get('name'):
                # ★★ 2026-09-23 晚：随机**优先从缓存里挑**。原来挑的是 `music/`
                #   那个目录 —— 里面**只有一首**吉他曲，所以"放点音乐听听"
                #   （主人最常说的那句）永远听到同一首，"随机"两个字没落地。
                #   缓存里那些才是歌单里**验证过能放**的十来首，而且 0 网络秒开。
                c = alarm.pick_random_cached()
                if c:
                    alarm.ring_bg({'src': c['path'], 'tag': '点播《%s》' % c['name'],
                                   'minutes': mins, 'time': '点播'})
                    return '开始放《%s》（缓存里挑的）—— 最多 %d 分钟' % (c['name'], mins)
                m = alarm.pick_random()
            if not m:
                return '失败：音乐库里没有 %r，有的：%s' % (a.get('name'), alarm.music_list())
            alarm.ring_bg({'music': m, 'minutes': mins, 'time': '点播'})
            return '开始放 %s，最多 %d 分钟' % (m, mins)

        if name == 'pause_audio':
            ctl.pause()
            return '已暂停'

        if name == 'resume_audio':
            ctl.resume()
            return '继续播放'

        if name == 'stop_audio':
            ctl.stop()
            return '已停止'

        if name == 'set_sleep_timer':
            return set_sleep_timer(a.get('minutes'))

        if name == 'ask_for_help':
            detail = {'user_detail': str(a.get('detail') or ''),
                      'music_dir': alarm.music_list(),
                      'volume_level': _voice.level_for_db(_voice.gain_db()),
                      'night': bool(dlna.NIGHT_QUIET and dlna.in_night())}
            # ★ 带上这条求助是从哪条线来的。求助后端靠它决定"回话往哪说"：
            #   那条线来的**绝不许**推给屋里那只音箱（否则主人举着话筒问，
            #   音箱在屋里替他念进度）。（那是求助后端的活）。
            source = '另一条线' if channel == 'phone' else '音箱'
            # ★ 这条是要"装一样新本事"还是"找一首曲子" —— 两条路的执行壳完全不同
            #   （"装本事"与"找曲子"两套：cwd、工具表、提示词、成败判据都不一样）。
            #   ★ 白名单式收口：模型填了别的值、或压根没填 ⇒ 一律当 music 走老路，
            #     绝不让一个没见过的字符串漏进下游。默认值也必须是老路（向后兼容）。
            kind = a.get('kind')
            if kind not in ('music', 'capability'):
                kind = 'music'
            qid, msg = helpq.submit(a.get('what'), detail=detail, source=source, kind=kind)
            if not qid:
                return '失败：' + msg
            # ★★ 必须把 `submit` 回的那句话**原样**交出去 —— 它分两种：
            #   "马上就能办" 和 "前面还排着队"。原来这里硬编码了一句
            #   "已提交…你去问问"，**排队那条就变成了假话**（本项目为同类的假话
            #   付过代价：`set_alarm` 回"定上了"而根本没有守护进程在跑）。
            #   ⇒ 硬编码这一句会让 `spk_help.py` 里那段"如实说"变成死代码。
            return msg

        if name == 'set_alarm':
            ok, msg = alarm.add_alarm(a.get('time'), music=a.get('music'),
                                      minutes=a.get('minutes') or 3,
                                      label=str(a.get('label') or ''))
            return msg if ok else '失败：' + msg

        if name == 'list_alarms':
            al = alarm.load().get('alarms', [])
            if not al:
                return '一个闹钟都没定'
            # ★ 空 = 到点随机（见 `spk_alarm.RANDOM`）。**必须如实说**，
            #   否则模型会把"没写"当成"放某一首"，跟主人报一个不存在的曲子。
            return '; '.join('%s%s %s' % (x.get('time'),
                                          ('（%s）' % x['label']) if x.get('label') else '',
                                          ('放 ' + x['music']) if x.get('music')
                                          else '随机放一首')
                             for x in al)

        if name == 'cancel_alarm':
            n, msg = alarm.del_alarm(a.get('time'), str(a.get('label') or ''))
            return msg

        # ---- 记忆（2026-09-22 加，第 3 步）----
        # ★ 两个工具、三层存储 —— **分层是本模块的实现细节，模型看不见**。
        #   它的全部世界观就是"记一条"和"查一下"，路由由 _do_remember 里的数据判断决定。
        if name == 'remember':
            return _do_remember(a, ctx)

        if name == 'lookup':
            return _do_lookup(a)

        # ---- 查网（2026-09-22 深夜加）----
        # ★ 两个都是【只读】的 GET，碰不到音箱一个指头。
        #   失败已经在 _do_weather / _do_quote 里变成人话了，不往外抛 ——
        #   一抛就变成 run() 那句"我这边出了点问题"，主人会以为是音箱坏了。
        if name == 'get_weather':
            return _do_weather(a)
        if name == 'get_quote':
            return _do_quote(a)

        # ---- 夜间静音（2026-09-22 加）----
        # ★★ 三个分支里都有同一个动作：**改完如果此刻仍在静音时段，就破例开 40 秒**。
        #   为什么必须在这儿做：夜里主人改完设置，他指望听见一句"改了" ——
        #   而静音时段里 say() 是直接 return 的，一个字都不会有。
        #   破例窗口只有 40 秒、到期自动收回，不是"把静音关掉"。
        #   ★ 顺序：必须在 dlna.xxx() 【之后】调 —— 那里已经等过设备回音，
        #     所以确认话一定落在阀门打开之后，不会跑在它前面。
        #   ★★ 判据必须取【改之前】那一刻 —— 改完之后 `in_night()` 问的是新时段：
        #     新时段若不含此刻就是 False，可设备侧的开关【还没被打开】（要 ≤20 秒），
        #     这句确认照样没人听得见。quiet_off 更极端：关掉之后必然 False，
        #     用"改完再判"永远 blip 不了。判据是"主人此刻在不在静音里"。
        if name == 'set_quiet':
            was_night = dlna.NIGHT_QUIET and dlna.in_night()
            _ok, msg = dlna.set_quiet_range(a.get('from_hour'), a.get('to_hour'))
            if was_night:
                dlna.night_blip()
            return msg

        if name == 'quiet_tonight':
            _ok, why = dlna.release_until(dlna.tonight_until())
            if not _ok:
                return ('本机今晚已经放行了，但没送到设备（%s）—— '
                        '它上线后会自己拉取补上。' % why)
            return '好，今晚不静音，明早自己恢复。'

        if name == 'quiet_off':
            was_night = dlna.NIGHT_QUIET and dlna.in_night()
            _ok, msg = dlna.set_quiet_off()
            if was_night:
                dlna.night_blip()
            return msg
    except Exception as ex:                        # noqa: BLE001
        return '失败：%s: %s' % (type(ex).__name__, ex)
    return '失败：工具 %r 没有实现' % (name,)


# ---------------------------------------------------------------- 回路
def run(text, max_turns=4, dry=False, session=None, ctx=None, channel=None,
        on_audio=None):
    """一句话进 → (要念出来的话, 会话控制)。中间该动手就动手。

    ★ `ctx` = 这一轮谁在说话（`TurnCtx`）。不传 = 今天的行为，逐字节相同。
    ★ `channel` = 这一通走哪条路（`'phone'` / None）。透传给 `system_prompt`，
      **不传 = 音箱路径逐字节不变**。★ 它只管【提示词怎么说】（它知不知道自己在另一条线上）；
      真正决定"声音从哪出来"的是 `on_audio`，两者是**两件事**，别混：
      `channel` 是让模型**说对话**，`on_audio` 是让声音**走对路**。
    ★ `on_audio(name, args)` = 出声类工具的出口（见 `AUDIO`）。不传 ⇒ 音箱，
      逐字节不变。返回 `None` = 这个动作交给音箱办。

    ★ 控制信号三个值：
        None    照常，这次会话继续
        'ask'   它在等主人回答 —— spk_ear 要把耳朵继续开着接住那句回答
        'end'   收工 —— spk_ear 关掉会话，回到等唤醒词

    ★ 为什么返回值从"一句话"变成"一句话 + 控制"（2026-09-21）
      在那之前每次调用都是一问一唤醒，模型【没有"我在等回答"这个概念】。
      所以它问完"几点？"之后，主人那句回答是【一次全新唤醒、历史为空】：
          主人：七点半吧  →  它静默定了今晚 19:30，还说"给你定上了"
      这三个字之所以能酿成事故，就是因为中间没有任何一处知道"刚才在定闹钟"。
      `session` 把历史接上，`ask` 让循环知道别关耳朵，两个缺一不可。

    ★ 最多转 max_turns 轮：模型偶尔会自己跟自己较劲（调完工具又想调一次），
      必须有个天花板。到顶了就把最后那句 text 交出去，绝不空手而归。
    """
    # ★ 会话的账本只由 run() 一个人记：进来那句、出去那句都记在这儿，
    #   调用方不用记 —— 两边都记就会记两遍（Session.add 虽挡了逐字重复，
    #   但"只有一个人记账"比"两个人记、靠去重兜底"稳得多）。
    if session is not None:
        # ★ who 记在【过账本的那一刻】：收工固化要按人归档，靠的就是每条消息上这个字段。
        #   生人那轮 `ctx.who` 本来就是 None（判成 stranger 时不许粘上一个人）⇒ 记成公共。
        session.add('user', text, ctx.who if ctx is not None else None)
        msgs = session.msgs_for_model()
    else:
        msgs = [{'role': 'user', 'content': text}]
    sysmsg = system_prompt(session, ctx, channel)
    said = ''

    def done(out, ctl):
        """出口只此一个：说出口的话同时进会话账本，省得哪个 return 漏记。"""
        if session is not None and out:
            session.add('assistant', out)
        return out, ctl
    for i in range(max_turns):
        try:
            blocks, stop = dlna.ask_ex(msgs, system=sysmsg, tools=_tools_for(channel))
        except Exception as ex:                    # noqa: BLE001
            log('✗ 大模型问不动：%s: %s' % (type(ex).__name__, ex))
            return done(said or '抱歉，我这会儿连不上脑子，等会儿再问我。', None)
        uses = [b for b in blocks if b.get('type') == 'tool_use']
        said = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text').strip()
        if not uses:
            # ★★★ 2026-09-21 22:27 现场抓到的两个毛病，都出在这一行：
            #   ① 【内部诊断串被当话念了出去】—— 原来写的是 `said or '（模型没给出话）'`，
            #      没有 text 时就退回这个串，而它一路进了 speak() ⇒ TTS ⇒ 喇叭。
            #      这是给【我】看的日志，不是给主人听的话。★ 诊断串永远不许走到 TTS。
            #   ② `stop`（stop_reason）从 630 行接住之后【再没人用过】—— 于是那次
            #      干等 29 秒、最后空手而归，日志里一条线索都没有，只能靠 py-spy 猜。
            #   ★ 下次再犯，看这一行就知道是不是 `max_tokens`（推理把 2000 的预算吃光，
            #     是这个端点最容易复现的一种空手而归），还是 end_turn 却没吐字。
            if not said:
                log('   ⚠ 模型空手而归（stop_reason=%r，块数 %d）—— 用台词兜底'
                    % (stop, len(blocks)))
                return done('嗯，我这会儿有点转不过来，你再说一遍？', None)
            return done(said, None)
        msgs.append({'role': 'assistant', 'content': uses})
        results, fired, question = [], None, ''
        for u in uses:
            nm = u.get('name')
            a = u.get('input') or {}
            if nm in CONTROL:
                # ★ 控制类工具【立即生效并结束这一轮】，不让模型接着说 ——
                #   否则它后半句会把刚问出口的那个问题盖掉（"你是在跟我说话吗"
                #   后面又跟一句别的，主人听到的是后面那句，问题就白问了）。
                if nm == 'ask_user':
                    fired = 'ask'
                    question = str(a.get('question') or '').strip()
                    res = '（问出口了，正在等主人回答；他的回答你看得见）'
                else:
                    fired = 'end'
                    res = '（会话结束，接下来你听不见了，等下一次唤醒）'
                log('⏹ %s(%s)', nm, a)
            else:
                res = dispatch(nm, a, dry=dry, ctx=ctx, on_audio=on_audio,
                               channel=channel)
                log('🔧 %s(%s) → %s' % (nm, a, res))
            results.append({'type': 'tool_result', 'tool_use_id': u.get('id'),
                            'content': res})
        msgs.append({'role': 'user', 'content': results})
        if fired == 'ask':
            # ★★★ 报名窗口【只在这一刻】开，判据是"我们上一轮发给模型的提示词里
            #   写的是 stranger" —— 不是"模型回了个 ask"。模型为了别的事问一句
            #   （"早上七点半还是晚上七点半？"）也会回 ask，那时候开窗，
            #   下一轮的"我是晚上七点半"就会被当成自报家门 ⇒ 一个错名字的生物特征。
            #   这道判断在 `note_asked()` 里（它读 `_told`），这里不重复判。
            # ★ 这一步还没有注册（`remember` 没有 name 格子，第 3 步才加），
            #   所以窗口现在只做一件事：让下一轮【别再问一遍】。
            if ctx is not None and ctx.ident is not None:
                try:
                    if ctx.ident.note_asked():
                        log('👤 开了报名窗口（%d 轮）', ctx.ident.await_name)
                except Exception:              # noqa: BLE001
                    pass                       # 认人这条链绝不拖挂嗓子
            return done(question or said or '你是在跟我说话吗？', 'ask')
        if fired == 'end':
            return done(said or '好，那我先不听了。', 'end')
    log('⚠ 转了 %d 轮还在调工具，打住' % max_turns)
    return done(said or '这事儿我没办利索，你再说一遍？', None)


def chat(dry=False):
    """多轮连着聊 —— 把 spk_ear 那个循环里【跟会话有关】的部分先在这儿跑通。

    ★ 存在的理由：那三条实测出来的错（静默把"七点半"定成今晚 19:30、
      "就用那首吉他"当成现在放歌）在单句模式下【必然复现】，因为单句模式
      历史为空、这是设计如此。要验的正是"接上历史之后它还错不错"，
      所以得有个能连着说好几句的地方。用它在没有音箱、不动音箱的前提下验。
    """
    s = sess.Session()
    log('（连着聊；/bye 收工，Ctrl-D 退出）')
    try:
        while True:
            try:
                line = input('你说 : ').strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ('/bye', '/exit', '/quit'):
                break
            said, ctl = run(line, dry=dry, session=s)
            print('它说 : %s' % said)
            log('控制 : %s   （已聊 %s）' % (ctl, s.age_str()))
            s.save()
            if ctl == 'end' or s.expired():
                break
    finally:
        print('--- 本次会话（%s）---' % (s.why or '散场'))
        print(sess.render(s.transcript()))
    return 0


def main():
    args = sys.argv[1:]
    if '--sys' in args:
        print(system_prompt())
        return 0
    dry = '--dry' in args
    args = [a for a in args if a != '--dry']
    import spk_agent as agent
    k = agent._key()
    if not k:
        print('✗ 没找到大模型 key（DS_KEY / spk.key / settings.json 三处都没有）')
        return 1
    os.environ.setdefault('DS_KEY', k)

    if '--chat' in args:
        return chat(dry=dry)

    text = ' '.join(args) or '现在在放什么'
    print('你说 : %s' % text)
    said, ctl = run(text, dry=dry)
    print('它说 : %s' % said)
    if ctl:
        log('控制 : %s' % ctl)
    return 0


if __name__ == '__main__':
    sys.exit(main())
