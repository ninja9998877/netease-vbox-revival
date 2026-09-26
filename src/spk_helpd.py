#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_helpd.py —— 求救管道的那双手（常驻）。

    音箱说"我去问问" ──> spk_help.py 落一条 pending
                            │
                       ① 出方案      claude -p（★ 只出方案，一步都不许动手）
                            │
                       ② 推 TG 卡片  请求 + 方案 + 【✅ 干】【❌ 不干】
                            │
                       ③ 等主人按下 ★ 没有这一下，后面一步都不动
                            │
                       ④ 执行       claude -p（工具白名单锁死）
                            │
                       ⑤ 进度说话   音箱念给用户听（本机 tts + DLNA）
                            │
                       ⑥ 结果说话 + 播放新拿到的音乐

★ 主人是最高决策者，这条不是礼貌是纪律
  管道另一头连着"往家里下载东西"这件有后果的事。所以③是硬闸门：没有主人的那一下，
  ④一步都不走。而①②只读不写，所以它们可以自动跑。

★ 为什么用 `claude -p`（headless）而不是"等我在终端前"
  主人出差在外，音箱半夜求一首歌，不该没人接。`claude -p` 实测可用 ⇒
  "我"能被程序唤起，不必有人在终端前坐着。

★ 权限怎么收的（这是整个文件最要紧的一处 —— 2026-09-22 深夜**改对过一次**）

  ★★★ **先记住这个教训：`--allowedTools` 单独用根本拦不住。**
  我原先在这儿写着"`-p` 模式下没人能点允许，所以 `--allowedTools` 就是硬边界"——
  **是错的**，而且错得没有任何症状。实测（四条命令、白名单里只有 mem_view 一条）：
  `/bin/echo`、`touch /tmp/x`、以及 `白名单命令 && touch /tmp/x`，**全部照跑**，
  而 `permission_denials` 是**空的**。
  原因：`~/.claude/settings.json` 里有 `allow: ["Bash(*)"]` +
  `defaultMode: bypassPermissions`，而命令行给的 `--allowedTools` 是**往 allow 里加**、
  **不是替换**。`--settings` 同样是合并 ⇒ 也拦不住。
  （另两条死路：`--permission-mode default` 没用，settings 里那条 allow 仍然生效；
  `--disallowedTools 'Bash(*)'` 会把白名单里那一条**也一起拒掉**。）

  ⇒ 唯一有效的办法：**给自己一个独立的配置目录**（`CLAUDE_CONFIG_DIR`），
    里面放一份只有 `defaultMode: default`、`allow: []` 的 settings.json ——
    真 settings 里那两条**压根不会被读到**，`--allowedTools` 这时才成为边界。
    实测（同一条白名单，只换配置目录）：白名单内可用 ✓、`touch` 被拦 ✓、
    `&& touch` / `; touch` 都被拦 ✓。
    代价：读不到真 settings 的 `env` 块（端点／密钥）⇒ 由 `provider_env()`
    搬给子进程，**只搬、不打印、不落盘**。

  执行时只给：读、写、搜、抓网页、curl/ffmpeg/ffprobe/mkdir/ls/mv，
  加上家庭记忆那两条（读 mem_view／写 mem_put）。
  ★ 刻意不给 rm、不给 sudo、不给任何能碰服务/配置的东西 —— 这条管道跑的是
    "帮我下首歌"，它不该有能力改这台机器上的任何服务。
  ★★ 另外单独封了几样**一眼就值钱的读**（`DENY_TOOLS`）：

    · 家里人的会话原文 `mem/archive.jsonl` —— 隐私边界，没有例外
    · `mem/pending.jsonl` —— 队列只该"投"，读它等于绕过 mem_put 的校验
    · `~/.claude/settings.json`（模型密钥）、`~/.claude/CLAUDE.md`（常放着各种 Secret）
    · `~/.ssh/**`（私钥）、`~/.spk_net_token`（敲音箱命令的通道口令）

    ★ 写法有坑：绝对路径必须写成 `//…`（两个斜杠）。实测 `Read(/home/…)`
    是**静默无效**的（facts 和 archive 都能读，而你以为挡住了）。
    ★ 这**不是穷举** —— 它仍能读 "跑这个服务的账号能读的所有文件"；真要收干净得改成
    "只允许读 MUSIC 目录"，属于另一件事。残留口子如实记着：`Bash(curl:*)`
    可以 `curl file:///本地路径`，本身就是一个本地文件读取原语。

★ 为什么进度能让音箱说
  `claude -p --output-format stream-json` 会把模型的中间叙述流式吐出来，
  那些话本身就是"我正在干什么"，直接转给音箱念，比我自己编一套映射准得多。
  但要限流：25 秒最多一句，且只挑短的 —— 不然音箱会变成话痨。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import spk_ai_dlna as dlna
import spk_entities as ent
import spk_help as helpq
import spk_memory as mem
import spk_said
import tghelp

# ★ 绝对路径：systemd 的 PATH 很短，常常**不含 claude 装的那个 bin 目录** ——
#   "只有最后一步死"那种坑就是这么来的。拿不准就显式设 `SPK_HELP_CLAUDE`。
def _find_claude():
    p = (os.environ.get('SPK_HELP_CLAUDE') or '').strip()
    if p:
        return p
    return shutil.which('claude') or 'claude'


CLAUDE = _find_claude()
MUSIC = os.environ.get('SPK_HELP_MUSIC', os.path.join(HERE, 'music'))
LOG = os.path.join(helpq.DIR, 'helpd.log')

# --------------------------------------------------------- 权限边界（★★★ 见文件头）
# ★★★ `--allowedTools` **单独用是拦不住的**（2026-09-22 实测，四组对照）：
#   `~/.claude/settings.json` 里有 `allow: ["Bash(*)"]` + `defaultMode:
#   bypassPermissions`，而 `--allowedTools` 是**往 allow 里加**、不是替换 ⇒
#   实测 `/bin/echo`、`touch /tmp/x`、以及 `白名单命令 && touch /tmp/x`
#   全部照跑，`permission_denials` 还是空的。`--settings` 也是**合并**、不是替换。
#   死过的三条路：`--permission-mode default` 没用（settings 的 allow 仍然生效）、
#   `--disallowedTools 'Bash(*)'` 把白名单里那条也一起拒了。
#   ★ 唯一有效的办法：**给自己一个独立的配置目录**（`CLAUDE_CONFIG_DIR`），
#     里面放一份干净的 settings.json —— 真 settings 里的 Bash(*)／bypass 就**压根
#     不会被读到**，这时 `--allowedTools` 才真的成为边界。
#     实测（同一条白名单，只换配置目录）：白名单内可用 ✓、`touch` 被拦 ✓、
#     `&& touch`／`; touch` 都被拦 ✓。
#   ★★ 代价：独立配置目录 ⇒ 读不到 `~/.claude/settings.json` 的 `env` 块
#     （模型／端点／密钥都在那儿）⇒ 必须自己把那段环境变量搬给子进程。
#     搬，但**绝不落盘、绝不打印** —— 密钥仍然只有那一个 0600 文件里有。
CONFDIR = os.environ.get('SPK_HELP_CONFDIR', os.path.join(HERE, '.helpconf'))
USER_SETTINGS = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
PY = os.path.join(HERE, '.venv', 'bin', 'python')
MEM_VIEW_CMD = '%s %s' % (PY, os.path.join(HERE, 'mem_view.py'))
MEM_PUT_CMD = '%s %s' % (PY, os.path.join(HERE, 'mem_put.py'))

# ★ 家庭记忆：**读**是只读的（出方案阶段就能用），**写**只在主人点头之后（进 EXEC）。
MEM_READ = ['Bash(%s:*)' % MEM_VIEW_CMD]
MEM_WRITE = ['Bash(%s:*)' % MEM_PUT_CMD]

# ★★ 永不外读：家里人的会话原文。**写法有坑** —— 绝对路径必须写成 `//…`
#   （两个斜杠）。实测 `Read(/home/…)` 是**静默无效**的：facts 和 archive 都能读，
#   而你以为挡住了；`Read(//home/…)` 才真拦得住（且不影响读别的文件）。
DENY_TOOLS = ['Read(//%s)' % p.lstrip('/') for p in (
    mem.ARCHIVE,                                    # 家里人的会话原文（隐私边界）
    os.path.expanduser('~/.claude/settings.json'),  # ★ 模型密钥就在这个文件里
    os.path.expanduser('~/.claude/CLAUDE.md'),      # ★ 用户常把各种 Secret 写在这儿
    os.path.expanduser('~/.ssh/**'),                # 私钥
    os.path.expanduser('~/.spk_net_token'),         # ★ 敲音箱命令的那条通道的口令
)]
# ★ 队列（pending.jsonl）**故意没封**：封了是自欺 —— `mem_put.py --list` 本来就
#   把队列内容原样打给人看，读那一行跟读 Read 是一回事，没多给出任何东西。
# ★ 这不是一份穷举的清单 —— 它能读的范围仍然是"跑服务的账号能读的所有文件"。
#   今晚只把**一眼就值钱的那几样**（密钥／私钥／设备口令／家里人原话）封了。
#   真要收干净得改成"只允许读 MUSIC 目录"，那是另一件事。

GAP = float(os.environ.get('SPK_HELP_GAP', '4'))          # 主循环间隔
PLAN_TIMEOUT = float(os.environ.get('SPK_HELP_PLAN_T', '180'))
EXEC_TIMEOUT = float(os.environ.get('SPK_HELP_EXEC_T', '1800'))
SPEAK_MIN_GAP = float(os.environ.get('SPK_HELP_SPEAK_GAP', '25'))
SPEAK_MAX = int(os.environ.get('SPK_HELP_SPEAK_MAX', '110'))
# ★★★ 只念中文（2026-09-24，见 `_speakable`）：至少这么多汉字，且汉字占非空白字
#   的比例不低于这个数。两个都可调 —— 哪天门禁误伤了一句该念的中文，先把 RATIO 降下来。
SPEAK_CN_MIN = int(os.environ.get('SPK_HELP_SPEAK_CN_MIN', '4'))
#   ★★★ RATIO 定 **0.5（中文得占一半以上）而不是 0.25** —— 离线验证台当场抓的：
#     `Searching... 正在搜索` 这种"英文打头、中文收尾"的进度，0.25 会把整句放行，
#     于是 `Searching` 照样被念出来、照样会被中文 ASR 转成乱码飘回去 ——
#     那正是要治的病。0.5 把它挡住，而真正以中文为主的句子（"已经装好了
#     web_search 这个工具，接着验证"，中文占 57%）照常念。
SPEAK_CN_RATIO = float(os.environ.get('SPK_HELP_SPEAK_CN_RATIO', '0.5'))

# 执行时允许的工具。★ 这就是全部的权力边界（前提：走独立 CONFDIR，见上）。
#   ★ 残留风险（如实记着，没解决）：`Bash(curl:*)` 能 `curl file:///本地路径`
#     ⇒ 它其实是个**本地文件读取原语**，配上外发请求就是一条外带通道。
#     今晚没堵：堵了就没法下载音乐了。要堵得换成"只允许 curl 到指定域名"那种
#     更细的白名单，属于另一件事。
EXEC_TOOLS = ['Read', 'Write', 'Glob', 'Grep', 'WebSearch', 'WebFetch',
              'Bash(curl:*)', 'Bash(ffmpeg:*)', 'Bash(ffprobe:*)',
              'Bash(mkdir:*)', 'Bash(ls:*)', 'Bash(mv:*)'] + MEM_READ + MEM_WRITE
# ★ 出方案阶段只读：不写、不下载，但**可以查家庭记忆**（方案常常要知道家里有什么）。
PLAN_TOOLS = ['Read', 'Glob', 'Grep'] + MEM_READ

# ---------------------------------------------------------------- 装能力的壳（kind='capability'）
# ★★★ 为什么必须分岔、而不是在同一套提示词里加个分支：
#   老那套（`music`）从头到尾写着"音乐库在 %s""音箱只能放本地 mp3""下载来的文件
#   只能放进音乐库" —— 拿它去办"给音箱装个查快递的本事"，出来的方案会答非所问。
#   两条路的 **cwd、工具表、提示词、成败判据**全都不一样 ⇒ 按 `d['kind']` 分岔。
#
# ★★ 权力边界（如实记着）：`Write` + `Bash(python3:*)` 合起来就是**本机任意代码执行** ——
#   这是"让音箱自己长本事"这件事的本质代价，绕不开。它由三道**人工**闸门兜着：
#     ① 需求只来自主人（音箱说、主人点头才提交）
#     ② 方案要先推给主人、他按了 ✅ 才动手（`decide()`，全管道唯一的闸门）
#     ③ 事后比对核心文件的 sha256 —— 变了就判失败
#   ★ 代码能管的只有第③道；前两道是人。别把这条读成"技术上已经安全了"。
CAP_TIMEOUT = float(os.environ.get('SPK_HELP_CAP_T', '1800'))
EXT_DIR = os.environ.get('SPK_EXT_DIR') or os.path.join(HERE, 'ext')
# 出方案阶段：只读 + 能上网查"这本事上哪儿学"。★ 不写、不跑命令。
CAP_PLAN_TOOLS = ['Read', 'Glob', 'Grep', 'WebSearch', 'WebFetch'] + MEM_READ
# 动手阶段：能写新扩展、能跑自测。
#   ★ **不给 `Edit`** —— 那玩意能改核心文件；新建扩展只需要 `Write`，权力面小一截。
#   ★ **不给 `systemctl` / `pkill` / `rm`** —— 装扩展不用碰服务；而且装完的生效
#     根本不需要重启（`spk_ext.maybe_reload()` 让各进程下一轮自己发现）。
CAP_EXEC_TOOLS = ['Read', 'Write', 'Glob', 'Grep', 'WebSearch', 'WebFetch',
                  'Bash(curl:*)', 'Bash(ls:*)', 'Bash(mkdir:*)', 'Bash(mv:*)',
                  'Bash(cp:*)', 'Bash(python3:*)'] + MEM_READ + MEM_WRITE
CAP_EXTRA_DENY = ['Edit']       # 双保险：白名单里没给，再显式拒一次
# ★ 核心文件：装能力这条路上**唯一由代码把守**的那道闸门拿它们做判据。
CORE_FILES = ('spk_skills.py', 'spk_ext.py', 'spk_help.py', 'spk_helpd.py',
              'spk_ear.py', 'spk_voice.py', 'spk_ai_dlna.py')

BUSY = set()            # 正在处理的 qid，防止同一轮起两个线程
SPEAK_LOCK = threading.Lock()
_last_spoke = {}


def log(fmt, *a):
    os.makedirs(helpq.DIR, exist_ok=True)
    line = '%s %s' % (time.strftime('%m-%d %H:%M:%S'), (fmt % a) if a else fmt)
    print(line, flush=True)
    try:
        sz = os.path.getsize(LOG) if os.path.exists(LOG) else 0
        if sz > 262144:
            os.replace(LOG, LOG + '.1')
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


# --------------------------------------------- 念给主人听的那一句（announce）
def _announce_of(out, good):
    """从 Claude 的收工输出里挑出**念给主人听的那一句**。

    ★ 为什么不能整段念：`out` 是它的完整回答（可能带 markdown、列表、文件清单、
      验证命令的输出），念出来主人只会发懵 —— 而播报那张嘴要的是**一句人话**。
      `CAP_EXEC_PROMPT` 要求它把这句话**单独写在最后一行**，这里就取最后一行。
    ★ 失败那句由**代码写死** —— 不指望它在失败时还写出人话（真失败时它自己
      多半也懵着）。这条不是洁癖：主人 2026-09-23 那通电话里「一直没回我」的
      教训就是"结果不回人 = 这事没发生"，所以失败也必须有一句能出口的话。
    """
    if not good:
        return '你让我学的那件事没弄成，我回头再想想办法。'
    lines = [ln.strip() for ln in str(out or '').splitlines() if ln.strip()]
    if not lines:
        return ''
    s = re.sub(r'[#*`_>]+', '', lines[-1]).strip()       # 剥 markdown 记号
    s = re.sub(r'^[-·•\d.、)]+\s*', '', s).strip()       # 剥行首的列表/序号
    return s[:200]


# ---------------------------------------------------------------- 说话
def speak(text, force=False):
    """让音箱说一句。夜间 say() 自己会拒（不推流），这里不用再判一次。"""
    text = (text or '').strip()
    if not text:
        return False
    with SPEAK_LOCK:
        try:
            log('🔊 %s', text)
            # ★ 用独立的 mp3 路径：spk-ear 也在写 /tmp/spk_ai.mp3，
            #   两个进程共用同一个文件会互相把对方的声音截断。
            mp3 = dlna.tts(text, out='/tmp/spk_help.mp3')
            # ★★★ 2026-09-24：**响之前**登记（不是响之后）—— 回声落在"开始响"之后，
            #   登记早一拍，闸门那边才来得及对上。见 `spk_said` 文件头。
            #   ★ 这一句是"回答了两遍"那件事的另一半：求救这条线在**另一个进程**里
            #     出声，音箱那边的 `_last_said` 根本够不着它 ⇒ 它的回声一路无人认领。
            spk_said.note(text, 'helpd')
            return dlna.say(mp3, tries=1, verify=False)
        except Exception as ex:
            log('✗ 说不出话：%s: %s', type(ex).__name__, ex)
            return False


def _from_phone(qid):
    """这条求助是从电话线交上来的吗？"""
    d = helpq.load(qid) or {}
    return str(d.get('source') or '') == '电话'


def speak_for(qid, text, force=False):
    """按【这条求助是从哪来的】决定这句话往哪说。

    ★★★ 2026-09-23 主人说"我用手机测"时暴露的洞：`speak()` 一律走
      `dlna.say()` → 0x601 → **屋里那只音箱**。于是主人举着手机在另一头问，
      进度却由屋里的音箱念出来 —— 家里有人的时候这既诡异又扰民。
    ★ 电话那条线没有"回话"通道，它的回报路是 **TG 卡片**（方案·己 已定）
      ⇒ 电话来的就**只记不念**：进度照样写进队列（`add_progress`），
      卡片上和 `--show` 里都看得见，只是不出声。
    ★ 判据用这条请求自己的 `source`，**不是**"现在有没有电话在通"——
      一条求助从交上来到办完可能隔几十分钟，那时电话早挂了。看它自己的来源才准。
    """
    if _from_phone(qid):
        log('📱 %s（电话来的，不往音箱说）：%s', qid, text)
        helpq.add_progress(qid, text)
        return False
    return speak(text, force=force)


def _cn_chars(s):
    """数汉字（CJK 统一表意文字那段）。"""
    return sum(1 for c in s if '一' <= c <= '鿿')


def _speakable(t):
    """这句话**配不配念出来**？—— 只放中文过。

    ★★★ 2026-09-24（主人原话：「安装过程中他说了一堆英文 然后音箱自己回答自己」）：
      `claude -p` 的进度是**英文**的（`I'll start by exploring the extension
      directory and the existing news extension to match its pattern.`），
      而这里原来是**照单念**。后果有两层，第二层比第一层坏得多：

        ① 对着家里念一串英文 —— 主人听了一头雾水；
        ② ★ **中文 ASR 把它转成乱码**再飘回麦克风 ——
           08:33:47 `📝 ASR：奥尔斯沃尔克斯伯顿TERCOSG来的` ⇒ 大脑当成主人说话，
           08:33:48 又答一遍。**这就是"回答了两遍"的后半段。**

      ⇒ 两层一起治：**不是中文的进度，一个字都不念**（但照样进 progress，卡片上看得到）。

    ★ 判据用**两个数**，缺一不可：
      · 汉字数 ≥ `SPEAK_CN_MIN`：挡住"OK"/"done"这种短英文；
      · 汉字占非空白字的比例 ≥ `SPEAK_CN_RATIO`：挡住
        "web_search 装好了"这种中英混排里英文占大半的。
      ★ 有意**不**做成"含中文就念" —— 那样上面那条英文进度里只要有一个中文字
        就漏回来了，而它的要害恰恰在英文那半。
    """
    n = _cn_chars(t)
    body = len(re.sub(r'\s+', '', t))
    return n >= SPEAK_CN_MIN and body and (n / float(body)) >= SPEAK_CN_RATIO


def speak_progress(qid, text):
    """进度播报 —— 限流 + 只挑短的 + **只念中文**，别让音箱变话痨。"""
    t = re.sub(r'\s+', ' ', text or '').strip()
    if not t or len(t) > SPEAK_MAX * 2:
        return
    now = time.time()
    if now - _last_spoke.get(qid, 0) < SPEAK_MIN_GAP:
        return
    if len(t) > SPEAK_MAX:
        t = t[:SPEAK_MAX].rstrip('，,。.、') + '。'
    helpq.add_progress(qid, t)
    # ★★★ 语言闸门放在 `_last_spoke` **之前**（有意）：英文行不许把限流额度吃掉 ——
    #   否则一串英文刚过，紧跟的那句中文进度就被 25 秒的间隔挤掉了。
    #   而 `add_progress` 放在它**之前**，是为了英文也留痕（卡片上看得到干到哪了），
    #   只是不出声。
    if not _speakable(t):
        log('🤐 %s（这段不是中文，只记不念）：%s', qid, t[:80])
        return
    _last_spoke[qid] = now
    # ★ 电话来的只记不念 —— 注意 `add_progress` 在上面已经记过了，这里不重复记。
    if _from_phone(qid):
        log('📱 %s（电话来的进度，只记不念）：%s', qid, t)
        return
    speak(t)


# ---------------------------------------------------------------- claude -p
def ensure_confdir():
    """把那份"干净"的 settings.json 写好（每次起、每次跑之前都过一遍）。

    ★ 里面**只有** defaultMode=default、allow 为空：真正的白名单由每次调用的
      `--allowedTools` 给（出方案和执行给的不是同一份）。放在文件里的唯一目的，
      是让真 settings 里那两条（`Bash(*)` / `bypassPermissions`）**不被读到**。
    """
    try:
        os.makedirs(CONFDIR, exist_ok=True)
        p = os.path.join(CONFDIR, 'settings.json')
        want = json.dumps({'permissions': {'defaultMode': 'default', 'allow': []}},
                          ensure_ascii=False, indent=1)
        old = ''
        try:
            with open(p, encoding='utf-8') as f:
                old = f.read()
        except OSError:
            pass
        if old != want:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(want)
        return True
    except OSError as ex:
        log('✗ 独立配置目录写不了（%s）—— 权限边界会退回"形同虚设"，先别跑', ex)
        return False


def provider_env():
    """把真 settings.json 的 `env` 块（端点／模型／密钥）搬给子进程。

    ★ 独立配置目录读不到它，不搬就等于"没有钥匙"（模型会退到真 Anthropic 端点、
      然后认证失败，症状最难猜）。**只搬、不打印、不落盘。**
    """
    try:
        with open(USER_SETTINGS, encoding='utf-8') as f:
            d = json.load(f)
        e = (d or {}).get('env') or {}
        return {k: str(v) for k, v in e.items() if isinstance(v, (str, int, float))}
    except (OSError, ValueError) as ex:
        log('⚠ 读不到 %s 的 env 块（%s）—— 这轮多半会认证失败', USER_SETTINGS, ex)
        return {}


def claude_run(prompt, tools, cwd, timeout, on_progress=None, extra_deny=None):
    """跑一次 claude -p。返回 (ok, 文本)。

    ★ 用 stream-json 而不是普通 -p：普通模式要等它全部跑完才吐一个字，
      那期间音箱只能干等着；stream-json 能把模型的中间叙述实时拿出来当进度。
    """
    if not os.path.exists(CLAUDE):
        return False, '找不到 claude（%s）' % CLAUDE
    if not ensure_confdir():
        return False, '独立配置目录建不起来 —— 权限边界不可靠，宁可不跑'
    cmd = [CLAUDE, '-p', prompt, '--output-format', 'stream-json', '--verbose']
    if tools:
        cmd += ['--allowedTools'] + list(tools)
    # ★ 必须跟 --allowedTools 一起给：白名单里的 `Read` 是"能读任何文件"，
    #   家庭会话原文那条得单独封掉。`extra_deny` 是给装能力那条路加料的口子
    #   （它要额外拒掉 `Edit` —— 那是能改核心文件的工具）。
    deny = list(DENY_TOOLS) + list(extra_deny or [])
    if deny:
        cmd += ['--disallowedTools'] + deny
    env = dict(os.environ)
    env['PATH'] = os.path.expanduser('~/.npm-global/bin') + ':' + env.get('PATH', '')
    # ★★ 独立配置目录 + 手工搬 env：这两行是"白名单真的在拦"的全部代价，缺一不可。
    env['CLAUDE_CONFIG_DIR'] = CONFDIR
    env.update(provider_env())
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, text=True,
                             encoding='utf-8', errors='replace', bufsize=1)
    except OSError as ex:
        return False, '起不来 claude：%s' % ex

    # ★ 一定要有超时闸：没有它，一条卡住的 claude 会让这条请求永远挂在 "approved"，
    #   而外面看起来只是"主人还没批"。
    killer = threading.Timer(timeout, lambda: p.kill())
    killer.daemon = True
    killer.start()

    final, errbuf = '', []
    try:
        threading.Thread(target=lambda: errbuf.append(p.stderr.read() or ''),
                         daemon=True).start()
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue                       # 掺进来的非 JSON 行，跳过就好
            t = ev.get('type')
            if t == 'assistant' and on_progress:
                for b in (ev.get('message') or {}).get('content') or []:
                    if b.get('type') == 'text' and (b.get('text') or '').strip():
                        on_progress(b['text'].strip())
            elif t == 'result':
                final = (ev.get('result') or '').strip()
                if ev.get('is_error'):
                    return False, final or '它自己说这轮出错了'
    finally:
        killer.cancel()
        p.wait()

    if final:
        return True, final
    tail = ''.join(errbuf)[-400:].strip()
    return False, '它没给出结论（%s）' % (tail or '退出码 %s' % p.returncode)


# ---------------------------------------------------------------- 提示词
PLAN_PROMPT = """你是这台机器上的助手。有一台智能音箱住在主人家里，音箱里那个模型能力有限，\
它把自己办不到的事交上来了。请你出一份方案 —— ★ 现在只出方案，一步都不许动手。

音箱说：%s

音箱自己查到的情况：%s

一些事实：
- 音箱的音乐库在 %s，里面现在有：%s
- 音箱只能放本地 mp3（走 DLNA 推到它自己的喇叭），放不了任何在线音乐服务
- 你（在主人点头之后）能上网、能下载、能转码

请写一份给不懂技术的主人看的方案。硬要求：
- 口语化中文，不超过 180 字
- 不要 markdown、不要列表符号、不要表情、不要括号注释
- 说清楚三件事：去哪儿找、找什么、放到哪儿；大概要多久；有什么代价或风险
- 如果这事你其实也办不成（比如版权上根本拿不到），就直接说办不了并说明为什么 ——
  别硬凑一个看起来能行的方案，那比说"办不了"糟糕得多
- 只输出方案本身，不要任何开场白
""" % ('%s', '%s', MUSIC, '%s')   # 真正的替换在 plan_prompt() 里做

EXEC_PROMPT = """主人已经点了同意，现在动手。

原始的请求是：%s

已经批准的方案是：%s

硬性边界，越界就算失败：
- 下载来的文件只能放进 %s 这个目录，不许写到别的地方
  （例外：家庭记忆那条队列，见下面那段规矩 —— 那是唯一允许写的另一处）
- **绝对不许读 %s** —— 家里人的会话原文，隐私边界，没有例外
- 只下载【明确允许再分发】的音乐（CC0、CC-BY、公有领域，或作者明说可自由使用）。
  每下一首，把它的来源网址和授权协议追加记到 %s/SOURCES.md
- 绝对不要改动或删除该目录里已经存在的文件
- 不许碰这台机器上的任何服务、配置、定时任务

跑完之后：用一句口语化的中文说明结果（做了什么、拿到几首、大概是什么感觉）。
这句话会被音箱直接念给主人听 —— 所以不要 markdown、不要列表、不要表情、
不要念英文文件名、不要写路径。
"""


CAP_PLAN_PROMPT = """一台智能音箱想给自己长一样新本事，但它装不了，把请求交上来了。\
请你出一份方案 —— ★ 现在只出方案，一步都不许动手。

音箱说：%s

它自己查到的情况：%s

这台音箱是怎么回事：
- 它的"脑子"是 %s/spk_skills.py：一段系统提示词 + 一批工具，模型照着提示词决定按哪个按钮
- **给它长本事的唯一办法**：在 %s 目录下**新建一个扩展文件**（比如 ext/xxx.py）。
  加载器 %s/spk_ext.py 会自动认下来，下一轮对话它就有这个本事了
- ★ 现成的样板是 %s/ext/news.py（"查新闻"那个），照它的形状写
- ★★ **绝对不许改 spk_skills.py 这类核心文件** —— 只许往 ext/ 里加新文件

写方案时要卡的硬条件（主人定的，不满足就别提）：
- **免费、不用注册、不花钱、不用申请 key**
- 能用本机手搓就用本机手搓，别去接要注册的服务

请写一份给不懂技术的主人看的方案。硬要求：
- 口语化中文，不超过 180 字
- 不要 markdown、不要列表符号、不要表情、不要括号注释
- 说清楚三件事：这本事打哪儿来（哪个免费数据源、怎么实现）、大概要多久、有什么代价或风险
- ★ 如果这事**根本办不成**（要花钱、要注册、要买硬件、本地做不出来），
  就**直接说办不了并说明为什么** —— 别硬凑一个看起来能行的方案，那比说"办不了"糟糕得多
- ★ 如果它说的需求太含糊、你没法确定要做什么，就说"得先问清楚"，并把要问的问题列出来
- 只输出方案本身，不要任何开场白
""" % ('%s', '%s', HERE, EXT_DIR, HERE, HERE)

CAP_EXEC_PROMPT = """主人已经点了同意，现在给音箱装这个本事。

原始的请求是：%s

已经批准的方案是：%s

硬性边界，越界就算失败：
- **只许往 %s 这个目录里新建文件** —— 扩展都住这儿
- ★★★ **绝对不许改 %s 这些核心文件**（spk_skills.py、spk_ext.py、spk_help.py、
  spk_helpd.py、spk_ear.py…），**一个字节都不许动** —— 动了就判失败
- **绝对不许读 %s** —— 家里人的会话原文，隐私边界，没有例外
- 不许碰这台机器上的任何服务、配置、定时任务：不许 systemctl、不许 kill、
  不许重启任何东西（装完怎么生效不用你管，各进程下一轮自己会发现）

★ 你写的扩展要守这几条，**写错了会让音箱所有对话一起瘫**：
- 工具字典的键只有三个：`name` / `description` / **`input_schema`**
  ★★ **不是 `parameters`** —— 写成 parameters 会被模型 API **静默忽略**，
     后果是**每一次模型调用全部 400**，音箱当场变成"我这会儿连不上脑子"（真踩过）
- `dispatch(name, args)` **绝不许返回 None**、也绝不许抛异常 ——
  返回 None 的意思是"这个动作交给音箱自己办"，会**真的出声**；抛异常会打断整轮对话
- 扩展**绝不许自己出声**：不许 import 出声的模块、不许起播放、不许碰音量
- 这个 venv 里**没有 requests**，一律用 stdlib 的 `urllib`
- 工具的 description 要写清"什么时候该按这个按钮"，模型靠它决定要不要调

★ 写完**自己跑一遍验一下**（这步不能省）：
  cd %s && .venv/bin/python -c "import spk_ext; print(spk_ext.status())"
  要看到你的扩展出现在 exts 里、工具数从 22 涨上去了。写了 SELFTEST 就再跑一次它。

跑完之后：**最后单独写一行** —— 那就是音箱要念给主人听的话，原样念，别的都不念。
这一行是"音箱在跟主人说话"，不是你在写报告：
- **主语必须是"我"** —— 是这只音箱学会了本事，不是"主人那边装好了"、不是"用户现在可以"
- 要说清两件事：**我现在能干什么** + **主人怎么用**（直接问什么、说什么）
- 一句话，口语，不要 markdown、不要列表、不要表情、不要英文文件名、不要路径、不要编号
- 样子：「我现在能查农历了，你直接问我今天初几就行」

★ 这一行**之前**你想写多少技术说明都行（装了哪个文件、验了什么、有什么坑）——
  那些会进 `result` 给排错看，一个字都不会被念出来。
""" % ('%s', '%s', EXT_DIR, HERE, mem.ARCHIVE, HERE)


# 家庭记忆那段规矩，拼在两个提示词后面。原文（`%s` 都在下面一次填好）。
MEM_RULES = """

关于【家庭记忆】。音箱自己的记性有限，你可以帮它读、也可以帮它写，但要守规矩：

读（只读，随便查）：
· 跑 `%s` 就能看到它记住的家庭事实（谁、家里有什么、主人的习惯）。
  要机器可读的那份加 --json；查一样东西加 `--name 我的猫`。
· ★ 绝对不许去读 `%s` —— 那里是家里人的会话原文。
  这条没有例外，也不因为"主人可能想知道"而开口子。

写（投进队列，音箱收工时并入）：
· 记一条事实：`%s fact --key 称呼 --value 我叫张三`
· 新建一个档案（人／物）：`%s entity --name 咪咪 --type 猫 --aka 我的猫`
· 给已有的档案补属性：`%s attr --name 咪咪 --attr 品种 --value 美短`
· 短于四个字的值（比如"美短"）**记不进核心记忆**，必须走 attr —— 命令会当场告诉你。
· 默认就写进【公共】的，**不要加 --who**（加了主人反而看不见）。
· ★ 投完只能说"投进去了，音箱收工时记住"，**不许说"我记住了"** ——
  你只是排了队，真正记下来的是音箱。
""" % (MEM_VIEW_CMD, mem.ARCHIVE, MEM_PUT_CMD, MEM_PUT_CMD, MEM_PUT_CMD)


def music_list():
    try:
        return sorted(n for n in os.listdir(MUSIC) if n.lower().endswith('.mp3'))
    except OSError:
        return []


def mem_state():
    """家庭记忆的可观测指纹：(事实条数, 档案个数, 队列条数)。

    ★ 用来判"这轮到底办成事了没有" —— 跟"数音乐库多了几个文件"是同一个思路：
      看**它留下的状态**，不看它自己怎么说。★ 队列也算：投进去就是办成了，
      并入是音箱下一步的事。
    """
    try:
        return (len(mem._load()), len(ent.load()), len(mem._lines()))
    except Exception:                          # noqa: BLE001
        return (0, 0, 0)


def is_cap(d):
    """这条求助走哪套壳。★ 默认**老路**（`music`）：库里躺着的历史条目没有这个字段，
    读出来是 None，一律按老路解释才对。"""
    return (d or {}).get('kind') == 'capability'


def ext_files():
    """`ext/` 下每个扩展文件的大小。★ 只看 `.py` —— `__pycache__` 一变就会误判成
    "多装了东西"（同一个教训在 `spk_ext._dir_sig` 上已经吃过一次）。"""
    try:
        return {n: os.path.getsize(os.path.join(EXT_DIR, n))
                for n in os.listdir(EXT_DIR) if n.endswith('.py')}
    except OSError:
        return {}


def ext_loaded():
    """★ 另起一个**干净进程**去问加载器：它现在真认下了几个扩展、几个工具。

    ★★ 为什么不在本进程里 `import spk_ext`：那拿到的是 helpd 导入时的快照，
      等于问一个**从没看过新文件**的人。判据要"看它留下的状态"，就得换个
      没被自己污染过的视角 —— 而且新进程能看到"加载器认不认这个 ABI"
      （文件写出来了 ≠ 加载器收下了，这两件事必须分开验）。
    """
    # ★ 必须 `import spk_skills` —— `spk_ext.boot()` 是在它导入时才调的。
    #   只 import spk_ext 等于问一个**没上电**的加载器：`_exts` 还是空的，
    #   于是"加载器收下了没有"永远答"没收"（实测踩过，返回 [[], []]）。
    #   ★ 而且这样才**准**：常驻进程（spk-ear 等）走的正是这条导入路线。
    # ★ 带 `SPKEXT ` 前缀再认行：`import spk_skills` 自己会往 stdout 打
    #   `🧩 装了 N 个扩展`，取"最后一行"是在赌打印顺序。
    code = ('import sys,json;sys.path.insert(0,%r);'
            'import spk_skills;import spk_ext as e;st=e.status();'
            'print("SPKEXT "+json.dumps([st["exts"],st["tools"]],ensure_ascii=False))'
            % HERE)
    try:
        r = subprocess.run([PY, '-c', code], capture_output=True, text=True,
                           timeout=60, cwd=HERE)
        for ln in (r.stdout or '').splitlines():
            if ln.startswith('SPKEXT '):
                return json.loads(ln[7:])
        log('⚠ 问不了加载器，它的输出里没有 SPKEXT 行：%s',
            ((r.stdout or '') + (r.stderr or ''))[-200:].replace('\n', ' '))
        return None
    except Exception as ex:                                  # noqa: BLE001
        log('⚠ 问不了加载器：%s: %s', type(ex).__name__, ex)
        return None


def core_hashes():
    """核心文件的 sha256。★ 这是装能力这条路上**唯一由代码把守**的那道闸门：
    `Write` 权限一给，它物理上就能覆盖核心文件 —— 提示词里"不许改"是软的，
    事后比对 sha256 是硬的。"""
    out = {}
    for n in CORE_FILES:
        try:
            with open(os.path.join(HERE, n), 'rb') as f:
                out[n] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            out[n] = ''
    return out


# ---------------------------------------------------------------- 三个阶段
def card_text(d, plan, tail=''):
    det = d.get('detail') or {}
    cap = is_cap(d)
    L = ['🔧 音箱想长个本事' if cap else '🎵 音箱有事找你商量', '']
    L.append('它说：' + (d.get('ask') or '（没说什么）'))
    if det.get('user_detail'):
        L.append('它补充：' + str(det['user_detail'])[:200])
    L.append('')
    bits = []
    # ★ 音乐库有几首跟"装本事"半点关系没有 —— 照搬只会让卡片看起来答非所问。
    if not cap and det.get('music_dir') is not None:
        bits.append('音乐库 %s 首' % det['music_dir'])
    if det.get('volume_level') is not None:
        bits.append('音量档 %s' % det['volume_level'])
    bits.append('现在%s夜间时段' % ('' if det.get('night') else '不在'))
    L.append('它那边的情况：' + ' ｜ '.join(bits))
    L.append('')
    L.append('我的方案：')
    L.append(plan)
    L.append('')
    L.append('编号 ' + d['id'])
    if tail:
        L.append('')
        L.append(tail)
    return '\n'.join(L)


def buttons(qid):
    return [[{'text': '✅ 干', 'callback_data': 'h|%s|ok' % qid},
             {'text': '❌ 不干', 'callback_data': 'h|%s|no' % qid}]]


def propose(qid):
    """① 出方案 ② 推卡片。只读不写，所以这一步不需要主人先点头。"""
    try:
        d = helpq.load(qid)
        if not d:
            return
        det = d.get('detail') or {}
        info = json.dumps(det, ensure_ascii=False)[:800]
        if is_cap(d):
            # ★ 装本事那条路：cwd 换成 spkbrain 根（扩展、加载器、样板都在那儿），
            #   提示词换成 CAP 那套 —— 老那套满篇"音乐库在 %s""只能放本地 mp3"，
            #   拿它出"装个查快递的本事"的方案只会答非所问。
            prompt = (CAP_PLAN_PROMPT % (d.get('ask', ''), info)) + MEM_RULES
            tools, cwd = CAP_PLAN_TOOLS, HERE
        else:
            prompt = (PLAN_PROMPT % (d.get('ask', ''), info,
                                     ', '.join(music_list()) or '（空）')) + MEM_RULES
            tools, cwd = PLAN_TOOLS, MUSIC
        ok, plan = claude_run(prompt, tools, cwd, PLAN_TIMEOUT)
        if not ok or not plan:
            log('✗ %s 出方案失败：%s', qid, plan)
            helpq.finish(qid, '出方案失败：' + plan, ok=False)
            speak_for(qid, '这事我这边没想明白，先不弄了。')
            helpq.archive(qid)
            return
        plan = re.sub(r'[*#`]', '', plan).strip()[:1200]
        helpq.set_status(qid, 'proposed', plan=plan)
        push_card(qid)
    finally:
        BUSY.discard(qid)


def push_card(qid):
    """把卡片推给主人。推失败不算失败 —— 下一轮会重试，状态仍是 proposed。"""
    d = helpq.load(qid)
    if not d:
        return
    mid = tghelp.send(card_text(d, d.get('plan') or ''), buttons(qid))
    if mid:
        helpq.set_status(qid, 'proposed', tg_msg_id=mid)
        log('📨 %s 已推给主人，等他点头', qid)
    else:
        log('⚠ %s 卡片没推出去（网络？），下一轮重试', qid)


def cap_verdict(before_files, before_loaded, before_core):
    """装本事这轮到底办成没有。返回 (ok, 一句说明)。

    ★ 三道一起看，缺一不可：
      ① 新扩展文件真在那儿（文件系统）
      ② **加载器真认下了**（另起干净进程问）—— 「文件写出来了」≠「加载器收下了」：
         键名写成 `parameters` 那种，文件明明在、加载器拒收，等于白装
      ③ 核心文件一个字节没动（sha256）—— 这是唯一由代码把守的闸门
    """
    broken = [n for n, h in core_hashes().items()
              if h != (before_core or {}).get(n)]
    if broken:
        return False, '★ 它动了核心文件（%s）—— 越界，判失败' % '、'.join(sorted(broken))
    new = {k: v for k, v in ext_files().items() if k not in (before_files or {})}
    if not new:
        return False, '没有新扩展文件'
    now = ext_loaded()
    if now is None:
        return False, '问不了加载器，没法确认装上了没有'
    if now == before_loaded:
        return False, ('扩展文件是写了（%s），但**加载器没收** —— 多半 ABI 不合规'
                       '（比如工具字典的键名写成了 parameters）' % '、'.join(sorted(new)))
    return True, '新扩展 %s；加载器现有 %s、工具 %d 个' % (
        '、'.join(sorted(new)), '、'.join(now[0]) or '（无）', len(now[1]))


def execute(qid):
    """④ 执行 ⑤ 进度 ⑥ 结果。只有走过 decide() 那一关才会到这儿。"""
    try:
        d = helpq.load(qid)
        if not d:
            return
        cap = is_cap(d)
        before = set(music_list())
        before_mem = mem_state()
        # ★ 装本事那条路的判据要的三个"事前指纹"。前两个很便宜；
        #   `ext_loaded()` 要起一个子进程，所以老路不跑它（老路零额外开销）。
        before_files, before_core = ext_files(), core_hashes()
        before_loaded = ext_loaded() if cap else None
        speak_for(qid, '主人点头了，我这就去办，可能要几分钟。')
        if cap:
            prompt = (CAP_EXEC_PROMPT % (d.get('ask', ''), d.get('plan') or '')) + MEM_RULES
            tools, cwd, timeout, deny = CAP_EXEC_TOOLS, HERE, CAP_TIMEOUT, CAP_EXTRA_DENY
        else:
            prompt = (EXEC_PROMPT % (d.get('ask', ''), d.get('plan') or '',
                                     MUSIC, MUSIC, mem.ARCHIVE)) + MEM_RULES
            tools, cwd, timeout, deny = EXEC_TOOLS, MUSIC, EXEC_TIMEOUT, None
        ok, out = claude_run(prompt, tools, cwd, timeout,
                             on_progress=lambda t: speak_progress(qid, t),
                             extra_deny=deny)
        out = re.sub(r'[*#`]', '', out or '').strip()
        after = music_list()
        new = [n for n in after if n not in before]
        mem_new = mem_state() != before_mem
        if ok and cap:
            # ★ 判据换成"扩展装上了没有" —— 拿音乐库/家庭记忆量这一轮是拿错尺子。
            good, why = cap_verdict(before_files, before_loaded, before_core)
            # ★ `announce` = 【念给主人听的那一句】，跟 `result` 分头存：
            #   `result` 里混着 `why`（技术判定说明），那是给排错看的，念出来主人只会发懵。
            #   ★★ 成功和失败**都要留一句** —— 主人 2026-09-23 那通电话里
            #   「一直没回我」的教训就在这儿：结果不回人，等于这事没发生。
            #   失败那句由代码写死（见 `_announce_of`）。
            helpq.finish(qid, '%s ｜ %s' % (out, why), ok=good,
                         announce=_announce_of(out, good))
            # ★★★ 这里**不再出声** —— 原来这两行是 `speak_for(...)`。主人
            #   2026-09-23 定案：「电话聊的 当然走电话 不走音箱 音箱问的 走音箱呀」
            #   ⇒ 说话这件事由**各自那条线**自己办（出口本来就长在各自线上：
            #     音箱 0x601 / 电话 AudioSocket），守护进程不代劳 ——
            #     所以这里不需要造任何 IPC，两条线各自去 `done/` 里发现它。
            #   ⇒ 紧接着 `archive()` 就是让它们能发现：
            #     · 音箱线 `spk_ear.Announcer` —— 睡着⇒亮绿灯 / 免唤醒⇒说一句或衔接
            #     · 电话线（另一个壳）        —— 下一轮说给电话那头听
            #   ★★ 顺带修掉一个既有隐患：`speak()` 那条旁路推流**不更新** `spk_ear`
            #     的 `_said_at` 锚，而自听回灌闸门和早跑闸门都以它为基准
            #     ⇒ 存在"把 helpd 念的这句话当成主人说话"的可能。不走它就没这回事。
            helpq.archive(qid)
            return
        if ok:
            # ★ 判据是**状态变了没有**：音乐库多了文件，或者家庭记忆多了东西
            #   （事实／档案／队列任一变）。★ 只认音乐的话，一个"帮我把猫的品种
            #   记下来"会被判成"没弄成"并对着音箱道歉 —— 那是拿错尺子量。
            if not new and not mem_new:
                # ★ 它说成功了，但什么都没多 —— 照实说，别替它圆。
                helpq.finish(qid, '它说干完了，但音乐库和家庭记忆都没变化。它自己的说法：' + out,
                             ok=False)
                speak_for(qid, '我问过了，这事没弄成，抱歉。')
            else:
                bits = []
                if new:
                    bits.append('新文件：%s' % ', '.join(new))
                if mem_new:
                    bits.append('家庭记忆有更新')
                helpq.finish(qid, '%s ｜ %s' % (out, '；'.join(bits)), ok=True)
                speak_for(qid, out or '弄好了，给你放一首听听。')
                if new:                          # ★ 有音乐才放；纯记忆的活别乱响
                    time.sleep(1.5)
                    play(os.path.join(MUSIC, new[0]))
        else:
            helpq.finish(qid, '办砸了：' + out, ok=False)
            speak_for(qid, '这事我办砸了，抱歉。')
        helpq.archive(qid)
    except Exception as ex:
        log('✗ %s 执行时炸了：%s: %s', qid, type(ex).__name__, ex)
        helpq.set_status(qid, 'failed', result='执行时炸了：%s' % ex)
        helpq.archive(qid)
    finally:
        BUSY.discard(qid)
        _last_spoke.pop(qid, None)


def play(path):
    """放一首本地 mp3。★ verify=False + tries=1：自听判定是为"我说话"设计的，
    拿它判音乐只会误判成"没听见"然后重推 8 次。"""
    try:
        log('▶ 放：%s', os.path.basename(path))
        dlna.say(path, tries=1, verify=False)
    except Exception as ex:
        log('✗ 放不出来：%s: %s', type(ex).__name__, ex)


def decide(cb):
    """③ 主人按了按钮。★ 这是整条管道唯一的闸门。"""
    d = helpq.load(cb['qid'])
    if not d:
        log('· 收到 %s 的答复，但不是队列里的编号（自测卡？），忽略', cb['qid'])
        return
    if d.get('status') != 'proposed':
        # ★ 只认第一次答复。Telegram 会把没确认的旧按钮重新投递，
        #   在这里被吃掉 —— 否则就是【凭空多出来一次授权】。
        log('· %s 已经是 %s，这次点击作废', cb['qid'], d.get('status'))
        return
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    if cb['ok']:
        helpq.set_status(cb['qid'], 'approved', decision='ok', decided_at=now)
        tghelp.edit(d.get('tg_msg_id'), card_text(d, d.get('plan') or '',
                                                  '✅ 你点了同意，我去办了。'))
        log('✅ %s 主人授权了', cb['qid'])
    else:
        helpq.set_status(cb['qid'], 'denied', decision='no', decided_at=now)
        tghelp.edit(d.get('tg_msg_id'), card_text(d, d.get('plan') or '',
                                                  '❌ 你说了不干，我不动。'))
        log('❌ %s 主人否了', cb['qid'])
        speak_for(cb['qid'], '这个我问过了，暂时弄不了，抱歉。')
        helpq.archive(cb['qid'])


# ---------------------------------------------------------------- 主循环
def tick():
    tghelp.prime()
    for cb in tghelp.poll():
        decide(cb)
    for d in helpq.all_open():
        qid, st = d['id'], d.get('status')
        if qid in BUSY:
            continue
        if st in ('pending', 'proposed') and helpq.expired(d):
            helpq.set_status(qid, 'failed', result='等太久（超过 %d 小时）没人答，作废' % (helpq.TTL // 3600))
            log('⌛ %s 过期作废', qid)
            helpq.archive(qid)
            continue
        # ★★★ 大佬只有一个（2026-09-23 主人原话：「同一时间不能再求助大佬
        #   因为大佬只有一个」）—— 这道闸门原来**根本不存在**：
        #   `BUSY` 只防"同一条重复起线程"，队列里躺两条 `pending` 时，
        #   一轮 tick 就会起**两个 Claude 同时跑**；而 `EXEC_TOOLS` 里带 `Bash`，
        #   两个 Claude 同时动手写同一个目录就是互相踩。
        #   ⇒ 第二条**老实排队**（它停在 `pending` 不动，队列本来就有这个状态）。
        #   ★ 刻意**不另造一套 waiting 列表**：每条请求都该有自己的 id/plan/result，
        #     寄生在当前那条身上会让它没有身份、也没法单独被点开看。
        #   ★ 闸门只摆在**真会起 Claude 的两条路**上：`push_card` 那条（只推张卡片、
        #     不动手）不受影响，否则会平白把补推卡片推迟一轮。
        #   ★ 不会卡死：`BUSY.discard` 在两个 worker 的 `finally` 里（:452/:514），
        #     而两个 worker 都有超时（PLAN_TIMEOUT / execute 的 killer）。
        if st == 'pending':
            if BUSY:
                continue
            BUSY.add(qid)
            log('① %s 去出方案：%s', qid, (d.get('ask') or '')[:60])
            threading.Thread(target=propose, args=(qid,), daemon=True).start()
        elif st == 'proposed' and not d.get('tg_msg_id'):
            push_card(qid)                       # 上次没推出去的，补推
        elif st == 'approved':
            if BUSY:
                continue
            BUSY.add(qid)
            log('④ %s 开干', qid)
            threading.Thread(target=execute, args=(qid,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description='音箱求救管道 · 执行端守护')
    ap.add_argument('--once', action='store_true', help='只跑一轮就退出')
    ap.add_argument('--new', metavar='文字', help='手动塞一条请求（测试用）')
    ap.add_argument('--show', metavar='编号', help='打印一条请求的完整状态')
    a = ap.parse_args()

    if a.new:
        qid, msg = helpq.submit(a.new, detail={'user_detail': '（手动塞的测试请求，'
                                                              '不是音箱交上来的）',
                                               'music_dir': len(music_list()),
                                               'night': dlna.in_night()})
        print(qid, msg)
        return 0
    if a.show:
        d = helpq.load(a.show)
        print(json.dumps(d, ensure_ascii=False, indent=2) if d else '没有这条')
        return 0
    if a.once:
        # ★ 出方案和执行都跑在后台线程里，不等它们跑完就退出的话，
        #   守护线程会跟着进程一起没 —— 表现是"命令成功、卡片没来"，最难查。
        tick()
        t0 = time.time()
        while BUSY and time.time() - t0 < 900:
            time.sleep(1)
        log('（--once 跑完，还挂着 %d 条）', len(BUSY))
        return 0

    os.makedirs(MUSIC, exist_ok=True)
    os.makedirs(helpq.DIR, exist_ok=True)
    log('起来：claude=%s  音乐库=%s  轮询间隔=%gs', CLAUDE, MUSIC, GAP)
    idle = 0
    while True:
        try:
            tick()
            idle = 0
        except Exception as ex:
            # ★ 守护绝不能因为一轮出错就死 —— 死了就再也没人接音箱的求救，
            #   而且看起来跟"主人一直没批"一模一样，最难查。
            log('✗ 这一轮炸了：%s: %s', type(ex).__name__, ex)
        time.sleep(GAP)


if __name__ == '__main__':
    sys.exit(main())
