#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性收工兜底的自测 —— `spk_ear.is_hard_stop()`。

★ 为什么值一条测试：这是主人 2026-09-24 亲口要的规则
  （「我说没事了 拜拜 应该都进到睡眠状态」），它守着的是**麦克风关不关**。
  而它的判据只是一串字符串运算，谁顺手改一下"剥标点"或往里加个词，
  行为就变了、还**一声不响**（错的形态是"主人说了它不睡"或者"它突然不理人了"）。
  ⇒ 正例反例都得钉住。

★ 全程离线：只有字符串比较，不调模型、不出声、不碰设备、不联网。

★ 归 `spk_regress.py` 的册子跑。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import spk_ear                                  # 只取 is_hard_stop / HARD_STOP


def main():
    bad = []

    def eq(got, want, label):
        if got != want:
            bad.append('%s（得到 %r，应为 %r）' % (label, got, want))

    # ---- ④ 开关 `SPK_HARD_STOP=off`：★ 必须**先判**，因为它决定下面怎么验 ----
    #   ★ 判据是"看它留下的状态"（`HARD_STOP` 是空的），不是"看 env 写了什么" ——
    #     模块级常量在 import 那一刻就定死了，改了 env 不重启 spk-ear 是不生效的，
    #     那正是这个开关的实现方式。
    #   ★ 关掉时【不能】还拿正例去要它认 —— 那等于让测试跟开关打架。
    if not spk_ear.HARD_STOP:
        for t in ('没事了', '拜拜', '别听了', '嗯，没事了'):
            if spk_ear.is_hard_stop(t):
                bad.append('开关已关，却仍认出了 %r' % t)
        if bad:
            for b in bad:
                print('  ✗ %s' % b)
            print('✗ 收工兜底：%d 项不符' % len(bad))
            return 1
        print('✓ 收工兜底：开关已关（HARD_STOP 为空）⇒ 一条都不收，符合预期')
        return 0

    # ---- ① 正例：整句就是收工词（含标点/空白/语气词这些真实 ASR 会带的东西）----
    for t in ('没事了', '没事了。', '没事了！', ' 没事了 ', '嗯，没事了', '嗯没事了',
              '哦没事了', '拜拜', '拜拜！', '拜拜了', '再见', '先这样', '就这样吧',
              '不聊了', '别听了', '去忙吧', '挂了'):
        if not spk_ear.is_hard_stop(t):
            bad.append('该收工却没认出来：%r' % t)

    # ---- ② 反例：**绝不许**误杀（误杀的代价是当场把耳朵关上）----
    #   ★ 前两条是同一批字出现在【别的意思】里，这是本兜底唯一真正的风险。
    for t in ('那个菜没事了', '我没事了', '没事了然后呢', '没事', '没有', '不用了', '没了',
              '不用', '没事了没事了', '拜拜是什么意思', '好，拜拜你忙吧',
              '', '   ', None, '今天天气不错'):
        hit = spk_ear.is_hard_stop(t)
        if hit:
            bad.append('误杀了：%r（判成「%s」）' % (t, hit))

    # ---- ③ 设计契约：这三个**故意不收**（做整句时更常见的读法是"不用放这首"）----
    for t in ('没有', '不用了', '没了'):
        if t in spk_ear.HARD_STOP:
            bad.append('★ %r 不该进 HARD_STOP（见 spk_ear.py 里那段理由）' % t)

    if bad:
        for b in bad:
            print('  ✗ %s' % b)
        print('✗ 收工兜底：%d 项不符' % len(bad))
        return 1
    print('✓ 收工兜底全过 —— 17 正例认得、15 反例没误杀、开关语义对、不出声')
    return 0


if __name__ == '__main__':
    sys.exit(main())
