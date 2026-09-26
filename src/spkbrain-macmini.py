#!/usr/bin/env python3
"""音箱大脑接管器 —— 一个 443 端口，按 HTTP 请求路径分流：

★★★ 【第一代】做法，**已被否证、不推荐照做**。留在这里只为一件事：
    让后来的人少走我们走过的这条弯路（详见 `docs/open-source/08-路线更正表.md`）。

    它为什么死了：这条路要先让音箱的流量**经过我们**（假 DNS + 自签证书 MITM）。
    技术上能跑通，但极其脆弱 —— 设备一旦换了网络路径（走中继、走别的出口），
    我们这边**一个请求都收不到，而且没有任何报错**。
    第三代改成了"钩住设备自己的麦克风 + 借它自己的播放器出声"，
    **完全不需要截流量**，那才是现役方案（见 03 / 05 章）。

★ 要用这个文件，你得自己生成一对证书并让设备信任它。
  本文件**不含任何证书或密钥** —— 路径全走下面那几个环境变量。


  /websocket            (Host: vbox-asr.3.163.com)
        → MITM 转发给真 ASR。★上行音频/下行结果全部【原样透传，一个字节不改】，
          所以设备那边的时序和连真服务器时完全一样，不会超时。
          我们只是"偷看"最终识别文本，拿到就去问 DeepSeek。

  /vbox/tts/transform   (Host: vbox-tts.3.163.com)
        → ★ 设备来要语音时，我们按 resId 找到 DeepSeek 的回答，
          现场合成 MP3 发给它 —— 于是它嘴里念的是我们的话。

密钥只从环境变量或 ~/.claude/settings.json 读进内存，绝不打印、绝不落盘。
"""
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cfg import HOST, DEVICE_DIR          # noqa: E402

# ★ 你自己生成的那对证书（本文件不带证书，见文件头）。
CERT = os.environ.get('SPK_MITM_CERT', os.path.join(DEVICE_DIR, 'certs', 'spoof.pem'))
KEY = os.environ.get('SPK_MITM_KEY', os.path.join(DEVICE_DIR, 'certs', 'spoof.key'))
BIND = (HOST, 443)
# ★ 上游真 ASR 的地址 —— 这是**你自己抓包抓出来的**那个，不是通用常量。
UP_HOST = os.environ.get('SPK_UP_ASR_HOST', '')
UP_PORT = 443
OUT = os.environ.get('SPK_ASR_CAPTURE', '/tmp/spk_asr_capture')
KEYFILE = os.environ.get('SPK_KEYFILE', '')
DS_BASE = os.environ.get('DS_BASE', 'https://api.deepseek.com/anthropic')
DS_MODEL = os.environ.get('DS_MODEL', 'deepseek-v4-flash')
# ★ 嗓子的唯一定义处在 spk_voice.py（回答 / 思考词 / 唤醒应答三处必须同一个人）。
#   原先这里硬编码，spk_filler.py 那边靠一句注释维持一致 —— 那种一致迟早会漂。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spk_voice as _voice          # noqa: E402
VOICE = _voice.VOICE
# ★ edge-tts 若装在某个**普通用户**的家目录（`pip --user` 默认就这么装），
#   而本进程以 root 跑 —— root 的 `~` 是 `/root` ⇒ 直接跑会 ModuleNotFoundError。
#   办法：给出 `EDGE_TTS` 的绝对路径，并以 root 运行时用 `EDGE_TTS_USER`
#   填那个用户名、借它的身份执行。不需要借身份就把 `EDGE_TTS_USER` 留空。
EDGE_TTS = os.environ.get('EDGE_TTS') or (shutil.which('edge-tts') or 'edge-tts')
_EDGE_USER = (os.environ.get('EDGE_TTS_USER') or '').strip()
EDGE = ([EDGE_TTS] if (os.geteuid() != 0 or not _EDGE_USER)
        else ['sudo', '-n', '-u', _EDGE_USER, EDGE_TTS])
OPC = {0: 'cont', 1: 'text', 2: 'bin', 8: 'close', 9: 'ping', 10: 'pong'}
LOCK = threading.Lock()
CACHE = {}          # responseid -> {'ev':Event,'mp3':bytes,'reply':str,'q':str}
# ★ 最近一次备好的答案，不按 id 索引。存在的理由：TTS 那一跳是按设备请求 URL 里的
#   resId 查 CACHE 的，而 resId 是【网易在 TTS 那一步现铸的】（`<哈希>_<毫秒>`，实测
#   fa1af382ae8f7a81_1789915473237 那个毫秒数正好是它生成这句话的时刻），多半对不上
#   ASR 响应里的 responseid。对不上就会静默掉包失败，所以留一手：只要最近刚备过答案，
#   设备来要的那句 TTS 就是冲它来的，直接拿这份顶上。
LAST_PREPARE = {}   # {'slot':..., 'q':..., 't':...}
# 上面那份答案最多算"新鲜"多久。超过就别认了 —— 宁可信网易，也不能拿一句过期的回答
# 去顶掉用户刚问的另一件事。（judge 判到功能性时还会直接清空，这是第二道保险。）
STALE_PREPARE = float(os.environ.get('SPK_PREPARE_STALE', '25'))


# ---------------------------------------------------------------- 会话记忆
# 语音助手跟聊天窗口不一样，两件事必须一起解决：
#   ① 得记得住 —— 不然"那它呢""还有呢""这首叫什么"这类追问全废（此前就是这样，
#      每句都当全新的一句问 DeepSeek）；
#   ② ★得有边界 —— 语音场景里没有"新开一个对话"这个动作。主人早上问天气、
#      晚上问"那明天呢"，历史要是一直累积，模型会把晚上这句接到早上的话题上，
#      而且答得头头是道 —— 你根本听不出来错。所以隔太久就当新会话，历史清空。
#
# ★ 还有一条是这台设备特有的：主人听到的对话里，**有很多轮是网易自己答的**
#   （咱们定的规矩是"功能性的听网易的"）。那些轮也得记进去，否则
#   "放首周杰伦" → "这首叫什么"，DeepSeek 不知道"这首"是哪首。
#   所以历史里的 assistant 一律记【音箱实际说出去的话】，不管是网易说的还是我们说的。
HIST = []                                                   # [{'role','content'}]
HIST_LAST = [0.0]                                           # 上次说话的时刻
HIST_TURNS = int(os.environ.get('SPK_HIST_TURNS', '6'))     # 最多留几轮
HIST_GAP = float(os.environ.get('SPK_HIST_GAP', '10')) * 60  # 隔这么久算新会话
HIST_CAP = 200                                              # 单条最长留多少字
HLOCK = threading.Lock()


def hist_norm(t):
    """跟记进历史时用同一套规整 —— ask_llm 要拿它回头比"这句记过没有"。"""
    return ' '.join((t or '').split())[:HIST_CAP]


def hist_add(role, text):
    """往会话记忆里记一句。★ 太久了先清空 —— 理由见上面。"""
    text = hist_norm(text)
    if not text:
        return
    now = time.time()
    dropped = None
    with HLOCK:
        if HIST and HIST[-1]['role'] == role and HIST[-1]['content'] == text:
            return          # 同一条又发了一遍（ASR 会重发最终结果），别记两遍
        if HIST and HIST_LAST[0] and now - HIST_LAST[0] > HIST_GAP:
            dropped = (now - HIST_LAST[0]) / 60
            del HIST[:]
        HIST_LAST[0] = now
        HIST.append({'role': role, 'content': text})
        while len(HIST) > HIST_TURNS * 2:      # 只留最近几轮（一轮=user+assistant）
            HIST.pop(0)
    if dropped:
        log('   （距上次说话 %.1f 分钟 ⇒ 开新会话，历史清空）' % dropped)


def hist_msgs():
    """取出历史。连续同角色的并成一条 —— 某轮我们没答上时会出现连着两条 user，
    而那套 messages 接口不喜欢这个。"""
    with HLOCK:
        out = []
        for m in HIST:
            if out and out[-1]['role'] == m['role']:
                out[-1]['content'] += '\n' + m['content']
            else:
                out.append(dict(m))
    return out


def sys_prompt(note=''):
    """每次现算 —— 服务会长跑，时间不能在启动时算死。note 是网易判定的意图，见 intent_note()。

    这里的分寸全是踩出来的：第一版没写能力边界 ⇒ DeepSeek 随口答应做不到的事；
    第二版我凭想象写了边界 ⇒ 又把「设闹钟」错列进"做不到"，于是主人让它取消闹钟时
    它答"我没有闹钟功能"，而闹钟其实真的被取消了。所以现在的写法是：
    ★以系统判定为准，不许自行否认，也不许自行断言结果。
    """
    wd = '日一二三四五六'[int(time.strftime('%w'))]
    return (
        '你是一台网易三音云智能音箱。主人刚对你说完一句话，你的回答马上会被音箱念出来。'
        '现在是%s 星期%s。\n'
        '%s\n'
        '【怎么说】一句自然、简短的中文口语，不超过25个字。'
        '不要 markdown、不要表情符号、不要括号旁白、不要念出符号本身。\n'
        '【★关于对话记录】前面可能带着刚过去几轮的对话，那是这个房间里真实发生过的：\n'
        '  · 里面标着 assistant 的那几行，是音箱【已经念出去】的话，是既成事实——'
        '别否认、别道歉、别把同一句话再念一遍；\n'
        '  · ★但它不一定都是你答的：这台音箱有一部分话是它自带的功能在应答'
        '（放歌、设闹钟、暂停这些走的是它自己的系统）。那些也当作你说过的话，接着往下聊就行；\n'
        '  · 有些轮次音箱压根没出声，所以上下文看起来对不上号是正常的，不用提这件事；\n'
        '  · ★你只需要回答主人最后这一句，别的轮次一概不用管、不用复述、不用总结。\n'
        '【★关于能力】这台音箱确实会放音乐、设闹钟、放提醒——上面"系统判定"就是它接下来'
        '真会去做的事。所以：\n'
        '  · 顺着说就好（系统要放歌，你就说"好嘞，给你放几首"）；\n'
        '  · ★绝不要说"我没有这个功能""我做不到"，除非是打电话、发消息、'
        '控制空调电灯等家电、查实时天气路况股价——这些确实做不到，照实说；\n'
        '  · ★也不要编造具体结果（"已经帮你设好一点零五的闹钟了"）——'
        '你到底办没办成并不知道，说句"好的"就够了。'
        % (time.strftime('%Y年%m月%d日 %H点%M分'), wd, note))


def log(m):
    with LOCK:
        print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


os.makedirs(OUT, exist_ok=True)


def load_key():
    """优先级：环境变量 DS_KEY → 专用 key 文件（0600，一行）→ Claude Code 的 settings.json。"""
    k = (os.environ.get('DS_KEY') or '').strip()
    if k:
        return k
    try:
        with open(KEYFILE) as f:
            k = f.read().strip()
        if k:
            return k
    except Exception:
        pass
    # ★ 最后的兜底：去 Claude Code 的 settings.json 里找（它把 key 放在 env 块里）。
    #   用 `SPK_CC_SETTINGS` 指定；不给就是当前用户的 ~/.claude/settings.json。
    p = (os.environ.get('SPK_CC_SETTINGS')
         or os.path.expanduser('~/.claude/settings.json'))
    try:
        with open(p) as f:
            return (json.load(f).get('env') or {}).get('ANTHROPIC_AUTH_TOKEN')
    except Exception:
        return None


DS_KEY = load_key()


# ---------------------------------------------------------------- LLM / TTS
CMD_DESC = {2001: '普通问答', 1002: '播放音乐', 3003: '暂停/停止音乐'}

# ★ 网易这几个码是【真要去动手】，不是回答问题。分类器万一把"放首歌"看成闲聊，
#   我们就会一边放歌一边插嘴，场面很乱。所以它们对"闲聊"判定有一票否决权：
#   宁可放过一句闲聊，也不许抢走一个真动作。（码表只收见证实测过的，不猜。）
ACTION_CMD = {1002, 3003}


def intent_note(cmd, det):
    """把网易判定的意图翻成一句人话，喂给 LLM。

    没有它，DeepSeek 只能凭想象猜自己有什么功能 —— 实测它会否认真的有的功能
    （主人让它关音乐，它答"我没有这个功能"，而网易那边其实真的会去暂停）。

    ★ 最靠得住的线索是网易自己的 tts 字段：那是它对这次请求的意图摘要
      （"暂停" = 它要去暂停音乐；"好听的英文歌来啦" = 它要去放歌；
        "现在是中午12点40分" = 它就是回答这句话）。
      硬编码 cmd 码表只能覆盖见过的几种，用它的 tts 则天然覆盖所有没见过的指令。
    """
    tts = (det.get('tts') or '').strip()
    what = CMD_DESC.get(cmd) or ('意图编号 cmd=%s' % cmd if cmd is not None else '未给出意图')
    if tts:
        return ('系统判定：%s。它原本准备念的是「%s」——这件事它真的会去做，你顺着说就行。'
                % (what, tts))
    return '系统判定：%s。' % what


def chat_note(det):
    """闲聊（大模型接管）时喂给 LLM 的说明。

    ★ 这里绝不能沿用 intent_note —— 它会说"网易原本准备念的是「X」——这件事它真的会
      去做，你顺着说就行"。放在旧架构里没错（那会儿只在网易搪塞【功能】时才接管），
      但分流器上线后闲聊也走这条路，这句话就变成了【让 DeepSeek 跟着网易一起认输】：
      实测主人问"你觉得孤独吗"，网易准备念「忽然不知道怎么回答，换个话题吧」，
      DeepSeek 于是乖乖答了句"别说这个啦，我陪你聊点高兴的" —— 一个把问题绕开的回答。
      看起来像模型冷漠，其实是我们的提示词把它推过去的。
    """
    tts = (det.get('tts') or '').strip()
    return ('系统判定：这句话它自己答不了（它原本只会说「%s」），现在整句都交给你。'
            '按你自己的判断正面回答，不要敷衍，也不要转移话题。' % tts)


def ask_llm(q, note=''):
    """★ 带上最近几轮对话。没有它，"那它呢""这首叫什么""再讲一个"这类追问全接不上——
    此前每句都当成全新的一句在问。"""
    qn = hist_norm(q)
    msgs = hist_msgs()
    # judge() 已经把这句记进历史了，正常不用再补；但 --selftest 是直接调这里的，
    # 而且历史里连着两条 user 会被并成一条 ⇒ 判据用"末尾是不是就是这句"，不是相等。
    us = [m for m in msgs if m['role'] == 'user']
    if qn and not (us and us[-1]['content'].endswith(qn)):
        if msgs and msgs[-1]['role'] == 'user':
            msgs[-1]['content'] += '\n' + qn   # 并进上一条，别出现连着两条同角色
        else:
            msgs.append({'role': 'user', 'content': qn})
    if not msgs or msgs[-1]['role'] != 'user':
        msgs.append({'role': 'user', 'content': qn or '(没听清)'})
    req = urllib.request.Request(
        DS_BASE + '/v1/messages',
        data=json.dumps({'model': DS_MODEL, 'max_tokens': 2000, 'system': sys_prompt(note),
                         'messages': msgs}).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': DS_KEY,
                 'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return "".join(c.get('text', '') for c in d.get('content', [])
                   if c.get('type') == 'text').strip()


# ---------------------------------------------------------------- 分流器：这句该谁答
# ★ 主人定的架构（2026-09-20）：【大模型先处理】—— 功能性的收敛出来交给云端，
#   非功能性的大模型自己答。在此之前是反过来的：先让网易答，答完再看它是不是在
#   搪塞，于是"认输话"得一条条往词表里补（补了"没办法回答"，紧接着就漏了"没研究过"），
#   永远追不上网易换话术的速度。分流器把这件事从【事后猜】变成【事前定】。
#
# 网易的 cmd 和话术因此降级成【安全网】：分类器没在时限内给出结论时才用它们兜底，
# 保证大模型这边的任何故障都不会让音箱变成哑巴。
CLASSIFY_SYS = """你是智能音箱的分流器。判断用户这句话该由【音箱自带功能】处理，还是属于【开放闲聊】。

回 command 的三类：
1. 设备功能 —— 播放/暂停/上一首/下一首/音量/点播某首歌或某位歌手、闹钟/提醒/计时器/倒计时、连接与配网、蓝牙、灯效音效、固件升级、你能干什么
2. 固定信息查询 —— 时间、日期、天气、限行、股票汇率这类一句话就有标准答案的查询
3. 现成内容 —— 新闻、电台、有声书、听书

回 chat 的（其余全部，包括看着像功能其实是创作的）：
- 闲聊、情感、看法、人生、关系（例：你觉得孤独吗）
- 知识问答、原理、为什么（例：人为什么会唱歌）
- 讲个笑话/故事/谜语、编一段、写一首、起个名
- 翻译、算数、写文案、出主意
- 让音箱扮演角色、陪你聊

拿不准就回 command（宁可漏给音箱，不可抢走它的功能）。
只回一个词：command 或 chat。不要标点、不要解释、不要复述题目。"""

# 分类器的两个时限。WAIT 是【最多让设备等多久】—— 它直接加在唤醒之后，所以卡得比
# ASR 那一步紧。超时不用慌：退回协议判据就行，音箱不会哑（见 judge）。
CLASSIFY_WAIT = float(os.environ.get('SPK_CLS_WAIT', '2.5'))
CLASSIFY_TIMEOUT = float(os.environ.get('SPK_CLS_TIMEOUT', '6'))


def _pick_kind(txt):
    """把模型的回话抠成 'command' / 'chat'，抠不出来返回 None（宁可判不出，不许猜）。"""
    t = (txt or '').strip().lower().strip('`"\'。. \n\r\t')
    if t in ('command', 'chat'):
        return t
    words = re.findall(r'[a-z]+', t)
    if len(words) == 1 and words[0] in ('command', 'chat'):
        return words[0]
    return None


def classify(q, tag=''):
    """问大模型：这句是功能性还是闲聊。返回 'command' / 'chat' / None(没结论)。

    ★ 必须带硬时限。urllib 的 timeout 只管单次 socket 读写、管不住总时长，所以放进
      线程里 join —— 超时就当没结论，绝不让设备干等（这一点是踩出来的：truncate 与
      注释里那些"等不到的响应"是同一类坑）。
    """
    box = {}

    def run():
        try:
            req = urllib.request.Request(
                DS_BASE + '/v1/messages',
                data=json.dumps({'model': DS_MODEL, 'max_tokens': 512, 'temperature': 0,
                                 'system': CLASSIFY_SYS,
                                 'messages': [{'role': 'user', 'content': q}]}).encode(),
                headers={'Content-Type': 'application/json', 'x-api-key': DS_KEY,
                         'anthropic-version': '2023-06-01'})
            with urllib.request.urlopen(req, timeout=CLASSIFY_TIMEOUT) as r:
                d = json.loads(r.read().decode())
            box['out'] = "".join(c.get('text', '') for c in d.get('content', [])
                                 if c.get('type') == 'text')
        except Exception as e:
            box['err'] = e

    t0 = time.time()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=CLASSIFY_WAIT)
    dt = time.time() - t0
    if th.is_alive():
        log('%s   [分流] 大模型 %.1fs 没回话 ⇒ 退回协议判据' % (tag, dt))
        return None
    if 'err' in box:
        log('%s   [分流] 大模型出错 %r ⇒ 退回协议判据' % (tag, box['err']))
        return None
    kind = _pick_kind(box.get('out'))
    if kind is None:
        log('%s   [分流] 大模型回的是 %r，抠不出结论 ⇒ 退回协议判据' % (tag, box.get('out')))
        return None
    log('%s   [分流] %.1fs ⇒ %s' % (tag, dt, kind))
    return kind


def make_mp3(text, out):
    raw = out + '.raw'
    subprocess.run(EDGE + _voice.edge_args() + ['--text', text,
                           '--write-media', raw], check=True, capture_output=True, timeout=60)
    # ★ 响度串必须每次现取 _voice.chain()（唯一定义处），别再硬编码、也别存成常量。
    #   原先这里写死 'loudnorm=I=-14:TP=-1.5:LRA=11' —— 值恰好与 spk_voice 的默认
    #   一模一样，所以一直没露馅；但那样"调音量"要改两处，漏一处就变成回答和思考词
    #   响度不同、拼起来音量跳一下（spk_filler 的缓存键当初就是这个病，已修）。
    #   2026-09-21 起 chain() 会带上软件增益，所以必须现取，否则语音调音量对它无效。
    p = subprocess.run(['/usr/bin/ffmpeg', '-y', '-i', raw, '-af',
                        _voice.chain(), '-b:a', '128k', out],
                       capture_output=True)
    if p.returncode != 0 or not os.path.exists(out):
        os.replace(raw, out)
    else:
        os.unlink(raw)
    return out


# ---------------------------------------------------------------- ASR 旁听
class WSParser:
    def __init__(self):
        self.buf = b''

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 2:
                break
            b0, b1 = self.buf[0], self.buf[1]
            op = b0 & 0x0f
            ln = b1 & 0x7f
            off = 2
            if ln == 126:
                if len(self.buf) < 4:
                    break
                ln = struct.unpack('>H', self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                if len(self.buf) < 10:
                    break
                ln = struct.unpack('>Q', self.buf[2:10])[0]
                off = 10
            if b1 & 0x80:
                if len(self.buf) < off + 4:
                    break
                off += 4
            if len(self.buf) < off + ln:
                break
            py = self.buf[off:off + ln]
            raw = self.buf[:off + ln]        # 完整帧字节（透传时原样发这坨）
            self.buf = self.buf[off + ln:]
            # 注意：上行帧带掩码，但我们只用下行文本帧（服务端→设备，不带掩码），
            # 上行的 binary 音频帧只数长度、不解内容，所以这里不需要解掩码。
            out.append((op, py, raw))
        if len(self.buf) > 4 << 20:
            self.buf = b''
        return out


# 网易答不上来时蹦的那几句。命中 ⇒ 这次轮到 DeepSeek 说话。
# ★ 只收"听不懂/超出能力"这类明确的认输话，绝不能把"我好像没找到这个时间的闹钟"
#   这种正经的功能性回答卷进来 —— 那是网易答对了，该听它的。
FALLBACK_PAT = re.compile(
    r'听不懂|没听清|没听懂|听不太清|听不清|超出.{0,6}能力|奋力追赶|不能理解|'
    r'不明白你|无法回答|还不会|换个说法|没学会|暂时无法|'
    # ★ 下面这几条是 2026-09-20 22:44 实测补的。当时音箱问"人为什么会唱吗？"，
    #   网易回的是「抱歉【没办法回答】你，看来你的好奇心已经【突破了我的知识极限】」——
    #   明明是一句认输话，词表里却只有"无法回答"、没有"没办法回答"，于是被判成
    #   "网易答得上来的"，音箱当场把网易的认输话念了出来。这类模板还会再变，
    #   所以每次识别都打【网易的判定】那行 tts，词表照着它长。
    r'没办法回答|没办法帮你|知识极限|不知道怎么回答|无能为力|没研究过')

# ★★ 网易"答不上来"的意图码（2026-09-20 两次实测定出来的结构性判据）。
#
# 为什么不能只靠上面那张词表：网易的兜底话术是【无穷无尽】的比喻句，实测两次就换了
# 两个毫不相干的说法 ——
#   「抱歉没有办法回答你，看来你的好奇心已经突破了我的知识极限」
#   「糟糕，这个我还真没研究过」
# 我照着补词，主人就要照着试，永远追不上（第一版补完"没办法回答"，紧接着就漏了
# "没研究过"）。但这两次回来的协议字段【一模一样】：
#     cmd=9998   cloudId="251550052"   modeId=3385   ctrl="01"
# 话术会变，意图码不会 —— 所以主线认 cmd，词表只留作安全网。
#
# ⚠ 这是可证伪的假设，不是定论：历史日志里总共只见过 cmd=0（心跳，56 次）和
#   cmd=9998（认输，2 次），【一条"功能性回答"的样本都没有】，所以反证不了
#   "9998 只用于闲聊兜底"。万一哪天放歌/查时间这类正经功能也被判成 9998，
#   judge 那行【网易的判定】会明明白白印着 cmd=9998 —— 届时把这个数改成 0
#   （或 SPK_COPOUT_CMD=0）就退回纯词表，一行的事。
COP_OUT_CMD = int(os.environ.get('SPK_COPOUT_CMD', '9998'))
REWRITE_WAIT = float(os.environ.get('SPK_REWRITE_WAIT', '8'))

# 诊断用：每个 tag（一场 ASR）已经打过几条下行原文。见 judge() 里那段注释。
DBG_RAW = {}
DBG_MAX = 30


def parse_asr(py):
    """从 3100 响应里取出 (问句, responseid, cmd, detail, 网易的tts, 原始dict)。
    不是最终识别结果（ack / 中间帧）就返回 None。"""
    try:
        d = json.loads(py)
    except Exception:
        return None
    if d.get('ret_code') != 3100:
        return None
    q = (d.get('asr') or '').strip()
    if not q:
        return None
    slu = d.get('slu') or {}
    try:
        det = json.loads(slu.get('detail') or '{}')
    except Exception:
        det = {}
    return q, slu.get('responseid') or '', det.get('cmd'), det, (det.get('tts') or '').strip(), d


def judge(py, tag):
    """这段下行文本该怎么处理？

    forward —— 原样放行一个字不动（网易答得上来的，就听网易的）
    prepare —— 原样放行，但后台把 DeepSeek 的答案备好，等设备来要语音时掉包
    rewrite —— 拦下来，把 cmd 改成 2001 逼设备开口，念 DeepSeek 的答案
    """
    got = parse_asr(py)
    if not got:
        # ★ 诊断（2026-09-20 加，看清形状后可以撤）：parse_asr 只认 ret_code==3100
        #   且 asr 非空，两条不满足就【静默】返回 None，judge 于是返回 forward 且一个字不打。
        #   后果是「结果根本没到」和「结果到了但形状对不上」在日志里完全同形 ——
        #   22:23:26 那次整场 ASR 就是栽在这上面，白等了一轮。
        #   把原文打出来，下次说一句话就能定论。每场只打前 DBG_MAX 帧，中间结果不刷屏。
        k = DBG_RAW.get(tag, 0) + 1
        DBG_RAW[tag] = k
        if k <= DBG_MAX:
            log('%s   [下行原文 %d] %s' % (tag, k, py[:300]))
        elif k == DBG_MAX + 1:
            log('%s   [下行原文已到 %d 帧上限，后面只数不打]' % (tag, DBG_MAX))
        return 'forward'                        # ack / 中间结果，不碰
    q, rid, cmd, det, tts, _ = got
    hist_add('user', q)                         # ★ 三条路都要记，见文件头上那段说明
    log('%s ★ 它听到：%r' % (tag, q))
    log('%s   【网易的判定】cmd=%s  tts=%r  %s'
        % (tag, cmd, tts, json.dumps(det, ensure_ascii=False)[:400]))
    # ★★ 主线：先问大模型这句该谁答（主人定的架构，见 CLASSIFY_SYS 处的说明）。
    #    这里会阻塞最多 CLASSIFY_WAIT 秒 —— 代价就是设备多等这一会儿，换来的是
    #    "网易的认输话根本轮不到出口"。判出来之后立刻放行，后面的等待（DeepSeek 出
    #    答案）发生在设备来要语音那一跳，而不是卡在这条下行流上。
    kind = classify(q, tag)
    if kind is None:
        # 大模型没结论 ⇒ 退回协议判据（实测攒出来的两条，当安全网用）。
        hit = bool(FALLBACK_PAT.search(tts))
        kind = 'chat' if (cmd == COP_OUT_CMD or hit) else 'command'
        log('%s   [分流] 退回协议判据 ⇒ %s（cmd=%s，词表%s）'
            % (tag, kind, cmd, '命中' if hit else '没中'))
    elif kind == 'command' and cmd == COP_OUT_CMD:
        # ★ 两边打架，而且这条最值得记下来：分类器说是功能性，网易却给了认输码。
        #   放行网易（宁可漏给音箱，不可抢走它的功能），但这行日志正是【9998 到底是不是
        #   闲聊专用】的判据 —— 攒够几次就能把 COP_OUT_CMD 这条假设定案或推翻。
        log('%s   ⚠ 分类器说功能性、网易却给认输码 %s —— 放行网易，这行留着定案'
            % (tag, COP_OUT_CMD))
    if kind == 'chat' and cmd in ACTION_CMD:
        # ★ 一票否决，理由见 ACTION_CMD 的定义处。
        log('%s   ⚠ 分类器说是闲聊，可 cmd=%s 是网易要动手的动作 ⇒ 放行网易' % (tag, cmd))
        kind = 'command'
    if kind == 'command':
        # ★ 功能性 ⇒ 立刻把"最近备好的答案"作废。否则上一轮闲聊留下的那份会顶掉网易
        #   这次正经的回答（"现在几点了"的 tts 被换成上一句闲聊的答案 —— 这是分流器
        #   上线后新出现的风险，旧架构不会有，因为旧架构只在认输话上备答案）。
        #   CACHE 一起清：resId 现在能按前缀查到，留着过期条目只会误伤。
        CACHE.clear()
        LAST_PREPARE.clear()
        log('%s   ⇒ 功能性，交给音箱自己的功能（不干预；已作废上一轮备的答案）' % tag)
        return 'forward'
    if tts:
        # "有 tts"就等于"设备马上要开口念这句"，那就能等它来要语音时掉包 ——
        # 不拖设备的时间，也不用拦帧改写。（22:44 改的：原先按 cmd==2001 判，
        # 而网易认输给的是 9998，永远判不中。）
        log('%s   ⇒ 闲聊，大模型来答（网易那句「%s」等设备来要语音时掉包）' % (tag, tts))
        return 'prepare'
    log('%s   ⇒ 闲聊，可网易压根没给 tts(cmd=%s)，只能拦下来改写' % (tag, cmd))
    return 'rewrite'


def run_answer(q, cmd, det, slot, tag, t0=None, note=None):
    """问 DeepSeek + 合成语音，塞进 slot（供 TTS 那一跳取用）。

    note 由调用方给：闲聊走 chat_note（见那边，为什么不能用 intent_note 写在函数里）。
    留 None 的默认值是为了老的调用点不至于炸，此时才退回 intent_note。
    """
    t0 = t0 or time.time()
    try:
        reply = ask_llm(q, chat_note(det) if note is None else note)
        slot['reply'] = reply
        mp3 = make_mp3(reply, '/tmp/spk_reply_%d.mp3' % int(time.time() * 1000))
        slot['mp3'] = open(mp3, 'rb').read()
        log('%s ★ DeepSeek 答：%r（%.1fs，语音 %d 字节）'
            % (tag, reply, time.time() - t0, len(slot['mp3'])))
    except Exception as e:
        log('%s ✗ LLM/TTS 出错: %r' % (tag, e))
    finally:
        slot['ev'].set()


def prepare_answer(py, tag):
    """原样放行，但后台把答案备好 —— 设备等会儿来要语音时掉包。"""
    got = parse_asr(py)
    if not got:
        return
    q, rid, cmd, det, tts, _ = got
    slot = {'ev': threading.Event(), 'mp3': None, 'reply': None, 'q': q}
    if rid:
        CACHE[rid] = slot
    # 除了按 id 存，再留一份"最近备好的"给 LAST_PREPARE（理由见它的定义处）。
    LAST_PREPARE['slot'] = slot
    LAST_PREPARE['q'] = q
    LAST_PREPARE['t'] = time.time()      # 新鲜度；太旧的宁可不用（见 tts_branch）
    log('%s   备答案中（responseid=%r，等设备来要语音时掉包）' % (tag, rid))
    threading.Thread(target=run_answer, args=(q, cmd, det, slot, tag), daemon=True).start()


def encode_frame(op, payload):
    """构造服务端→设备的 WS 帧（不带掩码，FIN=1）。"""
    n = len(payload)
    h = bytes([0x80 | op])
    if n < 126:
        h += bytes([n])
    elif n < 65536:
        h += bytes([126]) + struct.pack('>H', n)
    else:
        h += bytes([127]) + struct.pack('>Q', n)
    return h + payload


def build_reply_frame(py, reply):
    """把网易的响应改成"让设备念 DeepSeek 这句"：cmd 一律 2001，tts 换成回答。"""
    d = json.loads(py)
    slu = d.setdefault('slu', {})
    try:
        det = json.loads(slu.get('detail') or '{}')
    except Exception:
        det = {}
    det['cmd'] = 2001
    det['tts'] = reply
    det.pop('tts2', None)          # 别让它拐去"放音乐"那条路
    slu['cmd'] = 2001
    slu['detail'] = json.dumps(det, ensure_ascii=False)
    return encode_frame(1, json.dumps(d, ensure_ascii=False).encode())


def rewrite_and_send(py, dst, tag, lock):
    """拦下的响应：先问 DeepSeek，再把 cmd 改成 2001 转发 —— 逼设备开口。

    ★ 安全阀：DeepSeek 没赶上就把【原帧】放行。宁可这次它不吭声，
      也不能因为我们的脑子慢，让它整个卡在那儿。
    """
    got = parse_asr(py)
    if not got:
        with lock:
            dst.sendall(encode_frame(1, py))
        return
    q, rid, cmd, det, tts, _ = got
    slot = {'ev': threading.Event(), 'mp3': None, 'reply': None, 'q': q}
    if rid:
        CACHE[rid] = slot
    t0 = time.time()
    th = threading.Thread(target=run_answer, args=(q, cmd, det, slot, tag, t0), daemon=True)
    th.start()
    th.join(timeout=REWRITE_WAIT)
    try:
        if slot['mp3']:
            with lock:
                dst.sendall(build_reply_frame(py, slot['reply']))
            log('%s   ★ 已把改写结果交给设备（cmd→2001，念的是 DeepSeek 那句）' % tag)
        else:
            with lock:
                dst.sendall(encode_frame(1, py))
            log('%s   ⚠ DeepSeek 没赶上(%.1fs)，原样放行 —— 这次它不会吭声'
                % (tag, time.time() - t0))
    except Exception as e:
        log('%s ✗ 改写发送失败: %r' % (tag, e))


def pump(src, dst, direction, tag, fh, stats, lock):
    p = WSParser()
    nf = n1 = 0        # 本方向收到多少帧 / 其中文本帧几条（诊断用，见函数末的收工日志）
    try:
        while True:
            d = src.recv(65536)
            if not d:
                break
            frames = p.feed(d)
            if direction != 'DN':
                dst.sendall(d)                  # 上行：原样透传，不改一个字节
                for op, py, raw in frames:
                    stats[0] += 1
                    if op == 2:
                        fh.write(py)
                        fh.flush()
                        stats[1] += len(py)
                continue
            # 下行：默认原样透传；只有"网易答不上来"的才拦
            out = b''
            for op, py, raw in frames:
                stats[0] += 1
                nf += 1
                if op == 1:
                    n1 += 1
                    what = judge(py, tag)
                    if what == 'rewrite':
                        threading.Thread(target=rewrite_and_send,
                                         args=(py, dst, tag, lock), daemon=True).start()
                        continue                # ★ 原帧丢掉，换成我们改写的
                    if what == 'prepare':
                        prepare_answer(py, tag)
                out += raw
            if out:
                dst.sendall(out)
    except Exception as e:
        log('%s %s 转发结束: %r' % (tag, direction, e))
    if direction == 'DN':
        # ★ 收工也要说话。原来正常收工（对端关连接）是一个字都不打的，于是
        #   「一帧都没收到」和「收到了但 judge 没认出来」在日志里长得一模一样 ——
        #   22:23:26 那次 ASR 就是被这一点糊住的：上行音频 220160 字节转发完了，
        #   之后一行都没有，而根本分不清是哪种。这个数就是分界线。
        log('%s DN 收工：共 %d 帧，其中文本帧 %d' % (tag, nf, n1))


def asr_branch(ss, hdr, n):
    tag = '#%d' % n
    stats = [0, 0]
    lock = threading.Lock()          # 保留：pump() 的签名还要它（本函数已不再调 pump）
    fh = open(os.path.join(OUT, 'audio_%d_%d.pcm' % (n, int(time.time()))), 'wb')
    try:
        # ★ 出站已掐断（2026-09-21）：不再连 vbox-asr.3.163.com，一个字节都不发给网易。
        #   原先这里把音频【原样转发】给真网易 —— 那是本地 ASR 还没接上时的中间人脚手架。
        #   它违反红线；而验证唤醒根本用不着它：唤醒的判据是"设备主动连上这个
        #   /websocket"，站在这儿看就够了（连上即说明证书门与整条链路都通了）。
        #   识别改由本机做（sherpa-onnx）。所以这里只做一件事：把音频收下来存盘。
        while True:
            b = ss.recv(65536)
            if not b:
                break
            fh.write(b)
            stats[1] += len(b)
    except Exception as e:
        log('%s ASR 出错: %r' % (tag, e))
    finally:
        fh.close()
    log('%s ASR 收工：上行音频 %d 字节' % (tag, stats[1]))


# ---------------------------------------------------------------- TTS 掉包
def tts_branch(ss, hdr, n):
    line = hdr.split(b'\r\n')[0].decode('latin1', 'replace')
    rid = want = ''
    try:
        q = urllib.parse.urlparse(line.split(' ')[1])
        ps = urllib.parse.parse_qs(q.query)
        rid = ps.get('resId', [''])[0]
        want = ps.get('text', [''])[0]
    except Exception:
        pass
    slot = CACHE.get(rid)
    if not slot and rid and '_' in rid:
        # ★ resId 的形状是 `<哈希>_<毫秒时间戳>`，那个时间戳正好是【网易生成这句话的时刻】
        #   （实测：fa1af382ae8f7a81_1789915473237 → 22:44:33.237）。所以它多半是网易
        #   在 TTS 那一步现铸的，而 ASR 响应里的 responseid 很可能只有前面那截哈希。
        #   若真如此，按整串查 CACHE 永远查不到，每次掉包都白搭。多试一次前缀。
        slot = CACHE.get(rid.split('_', 1)[0])
        if slot:
            log('#%d 【TTS】resId(%s) 整串没查到，按前缀 %s 查到了 ⇒ 掉包'
                % (n, rid, rid.split('_', 1)[0]))
    if not slot:
        # ★ 文本兜底 —— 判据在 2026-09-20 分流器上线时【换掉了】。原本看的是"设备要念的
        #   这句是不是认输话"，那是旧架构的产物：旧架构只抢认输话，所以认输话=该抢。
        #   现在只要是闲聊就归大模型答，而「你觉得孤独吗」网易答得挺像样、一个词表词
        #   都不命中 —— 再按老判据，这类就全漏了，等于分流器白装。
        #   新判据：我们最近刚备好一份答案 ⇒ 这句 TTS 就是冲它来的。
        #   只在 judge 判过 command 时清空（见那边），再加一道新鲜度保险。
        s = LAST_PREPARE.get('slot')
        if s and time.time() - LAST_PREPARE.get('t', 0) < STALE_PREPARE:
            slot = s
            log('#%d 【TTS】resId(%s) 没对上 CACHE ⇒ 用最近备好的那份（对应问题 %r）'
                % (n, rid, LAST_PREPARE.get('q')))
        elif s:
            log('#%d 【TTS】最近那份答案已经放了 %.0fs，太旧不敢用 ⇒ 转发网易的原版'
                % (n, time.time() - LAST_PREPARE.get('t', 0)))
    if slot:
        slot['ev'].wait(timeout=15)
        if slot.get('mp3'):
            log('#%d 【TTS】设备要我念：%r ⇒ ★ 掉包成 DeepSeek 的：%r'
                % (n, want, slot.get('reply')))
            body = slot['mp3']
            ss.sendall(('HTTP/1.1 200 OK\r\nContent-Type: audio/mp3\r\n'
                        'Content-Length: %d\r\nAccept-Ranges: bytes\r\n'
                        'Connection: close\r\n\r\n' % len(body)).encode() + body)
            log('#%d 【TTS】已发 %d 字节' % (n, len(body)))
            hist_add('assistant', slot.get('reply'))   # ★ 记的是音箱真念出去的那句
            return
    # 没有我们的份 ⇒ 原样转发给真服务器，让设备听网易的原版语音。
    # （这一跳本来是我们的强项，但主人定的规矩是"功能性听网易的"——
    #   那就得是真·原版，不是我拿 edge-tts 重念一遍网易的话。）
    log('#%d 【TTS】设备要我念：%r ⇒ 不干预，转发真服务器取网易原版' % (n, want))
    hist_add('assistant', want)                     # ★ 这句是网易答的，但音箱真念了它 ⇒ 也得记
    try:
        up = ssl.create_default_context().wrap_socket(
            socket.create_connection((UP_HOST, UP_PORT), timeout=10),
            server_hostname='vbox-tts.3.163.com')
        up.sendall(hdr)
        total = 0
        while True:
            d = up.recv(65536)
            if not d:
                break
            ss.sendall(d)
            total += len(d)
        up.close()
        log('#%d 【TTS】转发完毕，%d 字节' % (n, total))
    except Exception as e:
        log('#%d 【TTS】转发失败: %r' % (n, e))


# ---------------------------------------------------------------- 分流
def read_until(s, sep, cap=65536):
    b = b''
    while sep not in b and len(b) < cap:
        d = s.recv(4096)
        if not d:
            break
        b += d
    return b


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
try:
    ctx.minimum_version = ssl.TLSVersion.TLSv1
except Exception:
    pass
ctx.set_ciphers('ALL:@SECLEVEL=0')


def handle(cs, addr, n):
    ss = None
    try:
        ss = ctx.wrap_socket(cs, server_side=True)
        hdr = read_until(ss, b'\r\n\r\n')
        line = hdr.split(b'\r\n')[0].decode('latin1', 'replace')
        if not line:
            return
        if 'tts' in line:
            tts_branch(ss, hdr, n)
        elif '/websocket' in line:
            asr_branch(ss, hdr, n)
        else:
            log('#%d 没见过的请求: %s' % (n, line))
    except Exception as e:
        log('#%d 出错: %r' % (n, e))
    finally:
        try:
            ss.close()
        except Exception:
            pass


def serve():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(BIND)
    srv.listen(8)
    log('★ 大脑接管器 v2：ASR 原样转发 + TTS 掉包成 DeepSeek 的语音')
    log('   代码：%s' % os.path.abspath(__file__))     # 跑的是哪份代码，别靠 /proc 猜
    log('   密钥：%s / 模型：%s' % ('已载入' if DS_KEY else '✗ 没有！', DS_MODEL))
    n = 0
    while True:
        cs, addr = srv.accept()
        n += 1
        threading.Thread(target=handle, args=(cs, addr, n), daemon=True).start()


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        # 拿生产代码本身走一遍"听到→问 LLM→合成语音"，确认整条链能通
        t0 = time.time()
        r = ask_llm('你好，用一句话介绍你自己')
        print('LLM %.1fs -> %r' % (time.time() - t0, r))
        t0 = time.time()
        p = make_mp3(r, '/tmp/spk_selftest.mp3')
        print('TTS %.1fs -> %s（%d 字节）' % (time.time() - t0, p, os.path.getsize(p)))
    else:
        serve()
