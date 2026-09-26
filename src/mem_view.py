#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_view.py —— 家庭事实层的【只读】入口（给 agent / 给人看）。

主人 2026-09-22 深夜拍板的两条边界，这个文件就是它们的落地：
  ① **共享层以音箱的 `mem/` 为主**（`facts.json` + `entities.json` 是权威），
     agent 侧只读 —— 不直接改那两个 json。
  ② **只读结构化事实，不读会话原文**（`archive.jsonl` 里有家里人的闲聊：
     生人那轮、电视声、拌嘴）⇒ 本文件**永远不碰 archive.jsonl**，
     那是硬边界，不是"暂时没做"。

★ 为什么要有这个文件、而不是让 agent 直接 `cat` 那两个 json：
  · 那两个文件的形状是**实现细节**（`facts` 是 {key: {value, at}}、
    `entities` 是 {名字: {type, aka, attrs, who, at}}），直读等于把内部结构
    焊进调用方 —— 以后加一层（比如给 attrs 加来源/置信度）就会静默读错。
    这里做一次规整，外面只认"一个人/一样东西 + 它的属性"。
  · 顺带把 `who` 空值、缺字段、文件不存在这些**边界**收在一处。

★ 使用（agent 侧就一行）：
    python3 mem_view.py
    python3 mem_view.py --json
    ... --who 主人        只看某人名下的事实
    ... --name 我的猫      查一个名字或别名（跟音箱的 lookup 同一条路）

★ 写入路径**故意不在这里**：agent 要写家庭事实，走 `pending.jsonl` 队列，
  由音箱收工时并入（唯一写路径 —— 两条写路径迟早互相覆盖）。
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get('SPK_MEM_DIR') or os.path.join(HERE, 'mem')
FACTS = os.path.join(DIR, 'facts.json')          # L2 核心记忆（键值 + who）
ENTITIES = os.path.join(DIR, 'entities.json')    # 档案柜（人/物 + 别名 + 属性）
ARCHIVE = os.path.join(DIR, 'archive.jsonl')     # ★ 只在 --paths 里报个名字，内容永不读
PENDING = os.path.join(DIR, 'pending.jsonl')     # agent 投进来、还没并入的（只数条数）


def pending_n():
    """队列里还排着几条。★ **只数行**、不解析、不显示内容 —— 这里要的是
    "有没有积住"这一个信号，而它正是这套异步写路径的安全网：
    并入判终态、失败原地留着，所以**积压就是"有事没成功"的唯一表现**。
    看不见的积压会一直积着（而且没有任何人会觉得不对）。"""
    try:
        with open(PENDING, encoding='utf-8') as f:
            return len([ln for ln in f.read().splitlines() if ln.strip()])
    except OSError:
        return 0


def _load(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _ago(at):
    """时间戳 → "3 分钟前"。★ 缺失/非法一律显示 `?`，绝不猜一个时间出来。"""
    try:
        d = time.time() - float(at)
    except (TypeError, ValueError):
        return '?'
    if d < 0:
        return '刚刚'
    # ★ 除数必须是【上一个单位的秒数】：秒→1 分钟→60 小时→3600。
    #   先前写成 n/60 是错的（小时那档会算成 d/1440 ⇒ 1.2 小时报"3 小时前"），
    #   而"时间显示错了"这种事没人会去核对——所以这里明写，不玩公式推导。
    for n, u, div in ((60, '秒', 1), (3600, '分钟', 60), (86400, '小时', 3600)):
        if d < n:
            return '%d %s前' % (d / div, u)
    return '%d 天前' % (d / 86400)


def facts(who=None):
    """L2 核心记忆 → [{who, key, value, at}]。★ 按人筛在【这里】做，别让调用方自己滤。"""
    out = []
    d = _load(FACTS)
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        # ★ 两种形状并存（`spk_memory.remember` 写的：公共记录【只有】裸键，
        #   归人的记录同时有 `who` 字段和 `名字/` 前缀）。
        #   ★★ 判据跟 spk_memory 的 `brief()` 保持一致：**先认 `who` 字段，
        #   字段没有再退回前缀** —— 两侧不一致就会出现"看得见却筛不着"。
        w = v.get('who') or ''
        key = k
        if '/' in k:
            w2, _, key2 = k.partition('/')
            w, key = (w or w2), (key2 or k)
        if who and w and w != who:
            continue
        out.append({'who': w, 'key': key, 'value': v.get('value'), 'at': v.get('at')})
    out.sort(key=lambda x: (-(x['at'] or 0)))
    return out


def entities(who=None):
    """档案柜 → [{name, type, aka, attrs, who, at}]。"""
    out = []
    for name, e in _load(ENTITIES).items():
        if not isinstance(e, dict):
            continue
        if who and (e.get('who') or '') not in ('', who):
            continue
        attrs = {}
        for k, v in (e.get('attrs') or {}).items():
            if isinstance(v, dict):
                vv = v.get('v')
                attrs[k] = vv[0] if isinstance(vv, list) and vv else vv
            else:
                attrs[k] = v
        out.append({'name': name, 'type': e.get('type') or '',
                    'aka': list(e.get('aka') or []), 'attrs': attrs,
                    'who': e.get('who') or '', 'at': e.get('at')})
    out.sort(key=lambda x: (-(x['at'] or 0)))
    return out


def find(q):
    """按名字【或别名】找一个实体 —— 跟音箱侧 `lookup` 同一条判据（别名表，不猜语义）。"""
    q = (q or '').strip()
    if not q:
        return None
    for e in entities():
        if e['name'] == q or q in e['aka']:
            return e
    return None


def render(fs, es, who=''):
    """人话版。★ 事实在前、档案在后：事实是"他本人说的"，档案是"家里的东西"。
    ★ 表头跟着 `--who` 走 —— 写死"主人名下"会在筛别人时撒谎。"""
    L = ['家庭事实层（音箱的 mem/，我只读）',
         '  facts.json  %d 条 · entities.json %d 个' % (len(fs), len(es))]
    n = pending_n()
    if n:
        L.append('  ★ 队列里还有 %d 条没并进去（`mem_put.py --list` 看是哪些）' % n)
    if fs:
        L.append('')
        L.append('【%s】' % ('%s名下 + 公共的事实' % who if who else '全部事实'))
        for f in fs:
            # ★ 全量视图里把归属顶在键名前面（`某人·忌口`）—— 不筛人的时候，
            #   两个人各有一条「忌口」就全靠这一格区分，不显示等于丢信息。
            k = ('%s·%s' % (f['who'], f['key'])) if (f['who'] and not who) else f['key']
            L.append('  · %s = %s%s' % (k, f['value'],
                                        '（%s）' % _ago(f['at']) if f['at'] else ''))
    if es:
        L.append('')
        L.append('【档案柜】')
        for e in es:
            head = e['name'] + ('（%s）' % e['type'] if e['type'] else '')
            if e['aka']:
                head += ' 又叫：' + '、'.join(e['aka'])
            L.append('  · ' + head)
            for k, v in e['attrs'].items():
                L.append('      %s = %s' % (k, v))
    if not fs and not es:
        L.append('')
        L.append('  （空的 —— 主人还没告诉过音箱什么）')
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description='家庭事实层只读视图（不读会话原文）')
    ap.add_argument('--json', action='store_true', help='机器可读')
    ap.add_argument('--who', default='', help='只看某个人名下的事实（如 主人）')
    ap.add_argument('--name', default='', help='查一个名字或别名，如 我的猫')
    ap.add_argument('--paths', action='store_true', help='只报文件在哪（给排错用）')
    a = ap.parse_args()

    if a.paths:
        print('待并入 %d 条' % pending_n())
        for p in (FACTS, ENTITIES, ARCHIVE):
            print('%s  %s' % ('存在' if os.path.exists(p) else '没有', p))
        print('（archive.jsonl 只是报个位置 —— 它的内容按边界永不读）')
        return 0

    if a.name:
        e = find(a.name)
        if not e:
            print(json.dumps({'found': False, 'q': a.name}, ensure_ascii=False)
                  if a.json else '没有「%s」这样东西。' % a.name)
            return 1
        if a.json:
            print(json.dumps({'found': True, 'entity': e}, ensure_ascii=False, indent=1))
        else:
            print('「%s」%s%s' % (a.name, '（别名）→ ' if e['name'] != a.name else '',
                                 e['name']))
            for k, v in e['attrs'].items():
                print('  %s = %s' % (k, v))
        return 0

    fs, es = facts(a.who), entities(a.who)
    if a.json:
        print(json.dumps({'facts': fs, 'entities': es}, ensure_ascii=False, indent=1))
    else:
        print(render(fs, es, a.who))
    return 0


if __name__ == '__main__':
    sys.exit(main())
