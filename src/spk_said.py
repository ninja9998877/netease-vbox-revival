#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""我们【刚说出去的话】的共用登记簿 —— 跨进程的一份小本子。

★ 为什么要有它（2026-09-24，主人原话：「安装成功后 他回答了两遍」）：

  回灌闸门（`spk_ear._is_self_echo`）原来只认【自己用 `speak()` 说的那一句回答】
  —— 它比对的是 `_last_said`。可屋里还有两条路在出声，**都不写 `_last_said`**：

    ① **思考音**：`spk_ear` 的 filler 阶梯走 `dlna.push`，不是 `speak()`
    ② **求救进度**：`spk_helpd.speak()` → `dlna.say`，**另一个进程**，够都够不着

  ⇒ 这两条路的回声对闸门是**结构性失明**。2026-09-24 08:33 的完整现场：

    08:33:26  思考音「让我想想啊」推出去
    08:33:28  📝 ASR：让我想想啊                        ← 它听见了自己
    08:33:29  🔊 回：嗯，有了啊。好，你慢慢想，我在这儿等着。   ← 自问自答①
    08:33:44  👂 听见了，录…                            ← 求救那边念的【英文】飘回来
    08:33:48  🔊 回：嗯，是这样。这句我还是没听懂…            ← 自问自答②

  同一轮里**回答的尾音被拦下了两回**（日志里那两条 `🔁 … 不自问自答`）——
  闸门没坏，是**它不认识那两条路**。

★ 形状：tmpfs 上一行一个 JSON（`{"t": 秒, "text": ..., "src": ...}`）。
  放 `/tmp` 是有意的：重启即净，绝不留下跨开机的陈旧对照（陈旧对照会误杀主人真说的话）。
  读写都上 flock；`note()` 顺手把过期的行剪掉（本子永远只有几十行）。

★ 铁律：**这里出的任何错都只当"本子是空的"**，绝不往上抛。
  它挂在音箱的主循环上 —— 判据崩了顶多退化成老行为（偶尔自问自答一句），
  绝不能因为本子读不到就把主人说的话整句吞掉（本类错误的血教训，见
  `spk_ear._is_self_echo` 里那段 fail-open）。

★ 登记的是**谁**（刻意只登记两条盲路）：
  · 回答（`spk_ear.speak()`）**不登记** —— 它本来就写 `_last_said`，那条判据一字未动。
  · 已知仍未覆盖的第三条路：电话线那套走「音箱」出口时（`via='speaker'`）。
    那是电话那个壳，至今没有回声问题；**没坏就别动**，记在这儿免得忘。
"""
import fcntl
import json
import os
import time

PATH = os.environ.get('SPK_SAID_LOG', '/tmp/spk_said.jsonl')
# 超过这么久的行直接剪掉。★ 定 60 秒而不是"按窗口剪"：窗口是调用方给的
# （现在 ECHO_WINDOW=4.0），本子留宽一点，将来调窗口不用同步改这里。
KEEP = float(os.environ.get('SPK_SAID_KEEP', '60'))
MAX_LINES = 200                     # 兜底红线：本子绝不许长过这个
MAX_TEXT = 400                      # 单条最长留这么多字（回灌只比尾巴，够用）
# 时间戳允许"落在未来"多少秒（见 `candidates()` 里那段）：只为吃掉 `round()`
# 进位那几百微秒。再大的就当真坏数据 —— 否则一条坏时间戳会永远赖在对照表里。
FUTURE_SLACK = float(os.environ.get('SPK_SAID_FUTURE', '5'))

_SRC = {'filler': '思考音', 'helpd': '求救那条线'}


def _parse(s):
    """解析成一串 {t, text, src}。**坏行一律跳过，绝不抛。**"""
    out = []
    for ln in (s or '').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:                                        # noqa: BLE001
            continue                    # 半行 / 被截断的尾行 —— 只当没有
        if not isinstance(r, dict):
            continue
        t, txt = r.get('t'), r.get('text')
        if isinstance(t, (int, float)) and isinstance(txt, str) and txt:
            out.append({'t': float(t), 'text': txt, 'src': str(r.get('src') or '')})
    return out


def note(text, src=''):
    """记一句【我们刚说出去的】话。回 True/False，**绝不抛**。"""
    t = ' '.join(str(text or '').split())
    if not t:
        return False
    try:
        now = time.time()
        with open(PATH, 'a+', encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                rows = _parse(f.read())
                rows.append({'t': round(now, 3), 'text': t[:MAX_TEXT],
                             'src': str(src)[:16]})
                rows = [r for r in rows if now - r['t'] <= KEEP][-MAX_LINES:]
                f.seek(0)
                f.truncate()
                f.write('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def candidates(window, tail=16, limit=6):
    """最近 `window` 秒内【我们说过的】话，给回灌闸门当对照。

    返回 `[(对照文本, 给人看的说明)]`；**读不到就回空表**（调用方自然退回老判据）。
    ★ 只回最新的 `limit` 条：对照越多，误杀主人真话的机会越大 —— 这个方向要收着。
    """
    try:
        with open(PATH, encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                rows = _parse(f.read())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:                                            # noqa: BLE001
        return []
    now = time.time()
    out = []
    for r in reversed(rows):                    # 新的在前 —— 回灌总是最近那条
        # ★★ 别写 `age < 0: continue` —— 那会让**刚登记的那句立刻读不回来**：
        #   `note()` 存的是 `round(now, 3)`，四舍五入**会进位到未来**几百微秒，
        #   于是 age 是 -0.0004，被判成"时间戳在未来"丢掉。（离线验证台当场抓到。）
        #   正解：小负值一律夹到 0（当"就是此刻"）；只有**明显**在未来的
        #   （时钟被 NTP 大步调整过）才当坏数据跳过。
        age = now - r['t']
        if age < -FUTURE_SLACK:
            continue
        age = max(0.0, age)
        if age > window:
            continue
        out.append((r['text'][-tail:],
                    '刚才念的%s（%.1f 秒前）' % (_SRC.get(r['src'], '话'), age)))
        if len(out) >= limit:
            break
    return out


def clear():
    """清空（调试用）。"""
    try:
        os.remove(PATH)
        return True
    except Exception:                                            # noqa: BLE001
        return False


if __name__ == '__main__':
    import sys
    if '--clear' in sys.argv:
        print('清了' if clear() else '没啥好清的')
    else:
        w = float(sys.argv[1]) if len(sys.argv) > 1 else KEEP
        for c, why in candidates(w, tail=999):
            print('  %s' % why)
            print('    %s' % c)
