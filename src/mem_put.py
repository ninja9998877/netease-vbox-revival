#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_put.py —— agent 写家庭事实的**唯一入口**（投进队列，不直接写）。

主人 2026-09-22 深夜拍板的第 3 条：**家庭事实只有一条写路径**。
音箱自己写（收工抽取 + 对话工具）；agent 想写 ⇒ 投进 `mem/pending.jsonl`，
由音箱收工时并入。理由一句话：

    两个 json 的写者永远只有音箱一个 ⇒ `remember` 的同键覆盖、`MAX_FACTS`
    淘汰、`add()` 的别名红线，全在**写的那一刻**判 —— 一个绕不过去的写者，
    才谈得上"这几条规矩成立"。两条写路径迟早互相覆盖，而且不报错。

★ 读那一侧是 `mem_view.py`；这两个文件一个是"只读"、一个是"只投"，
  谁也不越界 —— 边界写在代码里，不靠自觉。

用法：
    mem_put.py fact  --key 称呼 --value 我叫张三
    mem_put.py entity --name 咪咪 --type 猫 --aka 我的猫
    mem_put.py attr  --name 咪咪 --attr 品种 --value 美短
    mem_put.py --list          # 看队列里排着什么（还没并进去的）
    mem_put.py --drain         # 立刻并入，不等收工（补漏用，正常不用调）

★★ `--who` 默认**空 = 公共**，这是有意的：认人（`SPK_WHO`）关着的时候，
  写成 `主人/称呼` 会进一个**没人读的命名空间** —— 提示词只喂公共（`brief(None)`），
  于是这条记忆**再也读不出来、而且不报错**。这就是"要了 who 却没人读它"那个坑。
  只有确认认人开着、且这句话确实**不是**主人说的，才用 `--who 某人`。

★★ 短值记不进去：核心记忆有 4 字下限（`MIN_CHARS//3`），「美短」正好卡在门外
  ⇒ 短东西走 `attr` 挂到档案上。这条闸门在你投递时就会告诉你，不会等到并入才丢。
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description='投递家庭事实进队列（音箱收工时并入）')
    sub = ap.add_subparsers(dest='cmd')

    f = sub.add_parser('fact', help='一条键值事实（进核心记忆，常驻提示词）')
    f.add_argument('--key', required=True)
    f.add_argument('--value', required=True)
    f.add_argument('--who', default='', help='★ 留空=公共；见文件开头那条警告')

    e = sub.add_parser('entity', help='新建一个档案（人/物）')
    e.add_argument('--name', required=True)
    e.add_argument('--type', default='')
    e.add_argument('--aka', action='append', default=[], help='又叫什么（可给多次）')
    e.add_argument('--who', default='')

    a = sub.add_parser('attr', help='给已有档案记一条属性（短值走这条）')
    a.add_argument('--name', required=True)
    a.add_argument('--attr', required=True)
    a.add_argument('--value', required=True)
    a.add_argument('--who', default='')

    ap.add_argument('--list', action='store_true', help='看队列')
    ap.add_argument('--drain', action='store_true', help='立刻并入（正常不用调）')
    ns = ap.parse_args()

    import spk_memory as mem

    if ns.list:
        q = mem.pending_read()
        if not q:
            print('队列是空的 —— 没有等着并进去的东西。')
            return 0
        print('队列 %d 条（%s）：' % (len(q), mem.PENDING))
        for it in q:
            if not it:
                print('  · 【读不懂的一行】')
            elif it.get('kind') == 'fact':
                print('  · 事实 %s%s = %s' % ('%s·' % it['who'] if it.get('who') else '',
                                             it['key'], it['value']))
            elif it.get('kind') == 'entity':
                print('  · 档案 %s%s%s' % (it['name'],
                                          '（%s）' % it['type'] if it.get('type') else '',
                                          ' 又叫：' + '、'.join(it.get('aka') or [])
                                          if it.get('aka') else ''))
            elif it.get('kind') == 'attr':
                print('  · 属性 %s 的 %s = %s' % (it['name'], it['attr'], it['value']))
            else:
                print('  · %s' % it)
        return 0

    if ns.drain:
        m, left, notes = mem.drain_pending()
        print('并入 %d 条，剩 %d 条' % (m, left))
        for n in notes:
            print('  · %s' % n)
        return 0 if not left else 1

    if ns.cmd == 'fact':
        items = [{'kind': 'fact', 'who': ns.who, 'key': ns.key, 'value': ns.value}]
    elif ns.cmd == 'entity':
        items = [{'kind': 'entity', 'name': ns.name, 'type': ns.type,
                  'aka': ns.aka, 'who': ns.who}]
    elif ns.cmd == 'attr':
        items = [{'kind': 'attr', 'name': ns.name, 'attr': ns.attr,
                  'value': ns.value, 'who': ns.who}]
    else:
        ap.print_help()
        return 1

    ok, msg = mem.pending_add(items)
    print(('✓ ' if ok else '✗ ') + msg)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
