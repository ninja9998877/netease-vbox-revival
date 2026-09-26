#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_session.py —— 一次"会话"的状态（唤醒 → 连续聊 → 收工）。

★ 为什么需要它：这是音箱从"机器"变"人"的那条分界线
  在这之前，每一句话都是一次全新唤醒、一次全新的模型调用、历史为空。于是：
      主人：给我定个闹钟   → 它问"几点？"
      主人：七点半吧       → 它【静默定了今晚 19:30】还说"给你定上了"
      主人：就用那首吉他   → 它当成"现在放歌"，【直接开播】
  ★ 这三条都是我 2026-09-21 实测出来的，不是推演。
  每一句都答得头头是道，错得你根本听不出来 —— 这比答不上来危险得多。

★ 会话怎么结束：主人定的规矩，一个字都不许自作主张
   ① 10 分钟只是【最长】的窗口，不是定时器
   ② 模型觉得"这句没听懂"或"这不像在跟我说话" ⇒ 主动问"你是在跟我说话吗"
   ③ 主人明确说"没有 / 没事了 / 别听了" ⇒ 立刻停止倾听
  ★ 所以这里【不做】静默超时判断。那是我原本想加的，主人否了 —— 理由是
    "模型自行判断"。不设的代价是：屋里没人说话时，麦克风就那么开着直到 10 分钟。
    安静、无害，只是"开着"。真要收，那是主人的选择，不是这里替他决定。

★ 为什么"问一句"比任何门限都好
  近场门限（声音够近才算）要标定、会漏、会误杀；静默计时器会把自然的停顿
  当成结束。而"我不确定，我问你"是这个模型本来就会做的事，只需要给它一个
  出口（ask_user）和一个终点（end_session）。一个机制，同时解决了
  "什么时候停"和"这句话是不是冲我说的"两个问题。

★ 会话记忆的上一代
  `spkbrain.py:72` 那套（HIST_TURNS=6 / HIST_GAP=10分钟）是【历史保留 10 分钟】，
  每一句还是要先喊唤醒词，而且从不落盘、换到 spk_ear 这一代整个没带过来。
  这个模块是把它该有的样子补上，不是把它搬过来。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, 'session')
CUR = os.path.join(DIR, 'current.json')

# 最长窗口（秒）。★ 是【上限】不是定时器 —— 到点就收，但正常都是模型先收。
MAX_SECS = float(os.environ.get('SPK_SESSION_MAX', str(10 * 60)))
# 留给模型的最近轮数（一轮 = 你说一句 + 它答一句）。太多会挤掉提示词里更要紧的东西。
TURNS = int(os.environ.get('SPK_SESSION_TURNS', '12'))
CAP = 300                    # 单条最长留多少字，防一句 ASR 结果撑爆上下文


def _who_ok(w):
    """这个"说话人"标识能不能进账本。★ 这是【拒绝】，不是【清洗】。

    ★★ 为什么故意只做减法：清洗规则一旦有两份，就会出现"两边各洗各的、
      结果不一样"—— 这个项目在"第二份真话来源"上栽过好几次（部署脚本的对比表、
      `nightmute.saved` 钉音量、两个 dnsmasq）。
      判据跟 `spk_memory._clean_who` / `spk_speaker._clean_name` **逐条一样**
      （无空白、无斜杠、不长），所以**它们接受的这里必然也接受**，
      不可能出现"一边收一边不收"。归不了人就当公共 —— 不猜。
    ★ `/` 尤其要紧：它是 `facts.json` 里命名空间的分隔符。
    """
    w = str(w or '')
    if not w or len(w) > 12 or '/' in w or any(c.isspace() for c in w):
        return ''
    return w


class Session:
    """一次会话。spk_ear 每次唤醒 new 一个，收工就丢。"""

    def __init__(self):
        self.msgs = []           # [{'role': 'user'/'assistant', 'content': str, 'at': ts, 'who': str}]
        self.opened = time.time()
        self.last = self.opened
        self.why = ''            # 收工原因，写进日志和固化记录

    # ------------------------------------------------------------ 生命周期
    def add(self, role, text, who=None):
        """记一句。★ 空的、重复的都不记 —— ASR 会把最终结果重发一遍，
        记两遍会让模型以为主人把同一句话说两次。

        ★ `who` = 这一句是谁说的（认人那条链给出来的，可能为 None）。
          它有两个去处，两个都要紧：① 提示词按人过滤（`brief(who)`）
          ② 收工固化时**按人归档** —— 没有它，某人聊十分钟的口味会被
          记进【公共】，下次主人一说话提示词里就念着某人的口味，而且
          **没有任何人会去查**。这一步（记录）不做，后面全白搭。
        """
        t = ' '.join((text or '').split())[:CAP]
        if not t:
            return
        if self.msgs and self.msgs[-1]['role'] == role and self.msgs[-1]['content'] == t:
            return
        self.last = time.time()
        row = {'role': role, 'content': t, 'at': self.last}
        w = _who_ok(who)
        if w:
            row['who'] = w
        self.msgs.append(row)

    def expired(self):
        return time.time() - self.opened > MAX_SECS

    def idle(self):
        return time.time() - self.last

    def close(self, why):
        self.why = why
        return self.transcript()

    def transcript(self):
        return list(self.msgs)

    # ------------------------------------------------------------ 给模型看
    def msgs_for_model(self):
        """拼成 /v1/messages 要的 messages。
        ★ 只留最近 TURNS 轮，且【必须从 user 开头】—— 从 assistant 开头的话，
          模型的 messages 接口会认为"上一句是我说的、那你在回什么？"。
        ★ 连续同角色的并成一条：模型没答上时会出现连着两条 user，
          那套接口不喜欢这个。"""
        rows = self.msgs[-TURNS * 2:]
        while rows and rows[0]['role'] != 'user':
            rows = rows[1:]
        out = []
        for m in rows:
            if out and out[-1]['role'] == m['role']:
                out[-1]['content'] += '\n' + m['content']
            else:
                out.append({'role': m['role'], 'content': m['content']})
        return out

    def age_str(self):
        return '%.0f 秒' % (time.time() - self.opened)

    # ------------------------------------------------------------ 落盘
    def save(self):
        """把当前会话写到磁盘。★ 不是因为要恢复它，是为了【崩了能看见】——
        会话状态只在内存里的话，进程一挂就什么都查不到，只能靠猜。"""
        try:
            os.makedirs(DIR, exist_ok=True)
            tmp = CUR + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'opened': self.opened, 'why': self.why,
                           'msgs': self.msgs}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CUR)
        except OSError:
            pass


def load_last():
    """上一次会话（含已收工的）。给固化/排查用。"""
    try:
        with open(CUR, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def render(msgs):
    """把一段对话渲染成给模型读的纯文本。固化、复盘、日志都用它。

    ★★ 说话的人优先取消息上的 `who`（第 3 步加）—— 它【直接进固化那次模型调用的
      提示词】，所以这是"某人说的话被记到某人名下"的唯一通路。
      没带 who 的（旧数据、老调用方、音箱自己那句）落回「主人」/「音箱」，
      **行为逐字节不变**（`who` 是可选键，不是新格式）。
    """
    who = {'user': '主人', 'assistant': '音箱'}
    out = []
    for m in msgs:
        if not m.get('content'):
            continue
        name = who.get(m.get('role'), '?')
        if m.get('role') == 'user':
            name = _who_ok(m.get('who')) or name
        out.append('%s：%s' % (name, m['content']))
    return '\n'.join(out)
