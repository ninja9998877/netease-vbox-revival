#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音箱回归台总入口 —— 一条命令跑完所有【不出声】的自测。

★★ 为什么要有它
    改完一行想确认"没弄坏别的"，以前得记住几个脚本各自的路径和调用方式，
    而其中两个**原来住在 `/tmp`**（`test_who_turn.py` / `spk_e2e_live.py`）——
    重启或 tmp 清理就没了，只有记忆里记着它们长什么样。这里把它们收进项目并串起来。

★★★ 这个台子【只装零声音的测试】
    家里的规矩是「会让屋里出声的测试**默认不做、先问**」（见 `dont-disturb-family`）。
    所以册子里**一条出声的都没有**：要么全程打桩，要么纯离线算。
    需要设备 / 需要出声的另列在下面的"不在册"里，**别混进来**。
    ★ 唯一的例外是**要联网**：「能力插槽」那一套里新闻/搜索/天气的扩展自测会出去拿数据。
      它不出声、不碰设备，只是网络一抖就会红 —— 红了先看异常类型再下结论
      （**一条 flaky 测试会训练人无视 ✗**，所以这一点写在这儿，必要时单独跑它）。

★★ 它已经证明过自己（2026-09-24）
    从 `/tmp` 救回来的 `spk_test_who_turn.py` 一进册就是**坏的** ——
    代码往前走了（`turn()` 先后加了 `_live` 和回灌闸门 `_last_said`），
    而那个测试是 09-22 写的，用 `__new__` 造裸耳朵 ⇒ 两次 AttributeError。
    **这就是回归台存在的理由**：没人跑的测试会静默腐烂。

用法：
    .venv/bin/python spk_regress.py           # 跑全部
    .venv/bin/python spk_regress.py who       # 只跑名字里含 who 的
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, '.venv', 'bin', 'python')
if not os.path.exists(PY):
    PY = sys.executable

# ★ 电话线是**另一个项目**（独立部署、不在本仓库里），
#   但"一条命令跑完全部自测"这条不能因为跨项目就断 ⇒ 册子允许带 cwd。
#   ★ 两个项目共用同一个解释器（本仓库的 `.venv`）—— 声纹/ASR 的依赖都在那儿。
#   ★ 下面两套电话线用例要靠 `SPK_PHONE_DIR` 指到那套代码的目录；
#     不设就只能落到本目录 ⇒ 那两套会报"找不到文件"（本仓库里确实没有它们）。
PHONE_DIR = os.environ.get('SPK_PHONE_DIR') or HERE

# ── 册子：(名字, 命令行, 这一套在验什么[, cwd]) ──
#   ★ 顺序按"依赖从下到上"：状态机 → 接线 → 真音频 → 另一个壳。坏哪一层一眼看得出。
SUITES = [
    ('状态机',   ['spk_speaker.py', '--selftest'],
     'Identity 的状态迁移（喂的是合成元组，不碰音频）'),
    ('收工兜底', ['spk_test_hard_stop.py'],
     '★ 主人整句说"没事了/拜拜"就该收工（模型漏调 end_session 时那条确定性兜底）'),
    ('认人接线', ['spk_test_who_turn.py'],
     'turn() 里 verdict→ctx→brain 的接线（observe 整个打桩）'),
    ('认人真声', ['spk_test_who_real.py'],
     '★ 真音频→真声纹→真状态机 跨轮次（补的是三层之间那条缝）'),
    ('电话认人', ['spk_test_phone_who.py'],
     '★ 电话线那套：过**电话信道**的真音频 → 认人 + 与 spk_ear 逐条对齐（防副本分家）',
     PHONE_DIR),
    ('电话插话', ['spk_test_phone_barge.py'],
     '★ 电话线那套：插话闸门（门限是拿主人那通真录音量的）', PHONE_DIR),
    # ★ 这一套是【模型侧】的，跟上面四套的"耳朵/嗓子/电话线"不是一个轴，
    #   所以单列在最后。★★ 它要联网（新闻/搜索/天气三套扩展的自测都要出去拿数据）
    #   ⇒ 红了先看是不是网没通，别急着当代码坏了（输出里会带异常类型）。
    ('能力插槽', ['ext/_selftest.py'],
     '每个扩展 ext/*.py 的离线自测 + 插槽三条不变量（★ 要联网；不出声、不碰设备）'),
    # ★ 这一套是【设备侧脚本逻辑】，跟上面"耳朵/嗓子/电话线/模型"都不是一个轴：
    #   它验的是发到音箱上那几段 sh 的判据（用假命令 + 真文件里抽出来的函数），
    #   离线、不用设备、不出声。★ 独立成轴的理由：那类脚本坏了【不报错】，
    #   只会悄悄退化（这套守的是"省电悄悄开着、延迟翻倍"）。
    ('省电守护', ['spk_test_netguard_pm.py'],
     '★ netguard 里每 20 秒关一次 Wi-Fi 省电那段（run.sh 那行因开机时序从没生效过）'),
]

# ── 不在册：留个明白，免得下次有人以为漏了 ──
NOT_LISTED = [
    ('影子 Ear 端到端', 'spk_e2e_shadow.py',
     '要设备（adb 注入 + 真唤醒）、要常驻 spk-ear 停掉；**不出声**但动设备'),
    ('--pipe 端到端', 'spk_ear.py --pipe <唤醒wav>:<命令>',
     '**真出声**（喇叭演用户）⇒ 必须先问主人'),
]


def run(name, argv, timeout, cwd=None):
    cmd = [PY] + argv
    try:
        p = subprocess.run(cmd, cwd=cwd or HERE, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return None, '(超时 %ds)' % timeout, ''
    out = (p.stdout or '') + (p.stderr or '')
    # 只留最后一行结论（各测试自己会印 ✓/✗ 汇总）
    tail = [l for l in out.strip().split('\n') if l.strip()]
    return p.returncode, (tail[-1] if tail else '(无输出)'), out


def main():
    args = sys.argv[1:]
    picked = [s for s in SUITES if not args or any(a in s[0] for a in args)]
    if not picked:
        print('没有匹配的测试。册子：%s' % ' / '.join(s[0] for s in SUITES))
        return 1

    print('音箱回归台（全部不出声）—— %d 套\n' % len(picked))
    bad = []
    for s in picked:
        name, argv = s[0], s[1]
        cwd = s[3] if len(s) > 3 else None
        rc, tail, full = run(name, argv, timeout=600, cwd=cwd)
        ok = (rc == 0)
        print('  %s  %-10s %s' % ('✓' if ok else '✗', name, tail[:96]))
        if not ok:
            bad.append((name, argv, full))

    print()
    if bad:
        for name, argv, full in bad:
            print('=' * 60)
            print('✗ %s 的失败详情（%s）：' % (name, ' '.join(argv)))
            print('\n'.join(full.strip().split('\n')[-25:]))
        print('=' * 60)
        print('✗ %d/%d 套失败' % (len(bad), len(picked)))
        return 1
    print('✓ %d/%d 套全过' % (len(picked), len(picked)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
