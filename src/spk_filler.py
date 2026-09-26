"""思考词（filler）—— 把"犹豫"拼成一条流，盖住等 LLM 的那 3 秒。

为什么必须是 concat，而不是"先推思考词、再推回答"：
  ★ DLNA 的 AVTransport 一次只认一个 URI。推第二次 = 重新 SSDP 发现（这台设备几十秒
    就换一次 UPnP 端口，实测要 1~3 秒）+ SetAVTransportURI + 它重新来取文件。
    那 1~3 秒的断口比原来那 3 秒静音还难受。所以【合成一条流】是唯一干净的做法。

★ 三条硬约束（错了就听着像两个人 / 出爆音）：
  1. 同一嗓子：全部来自 spk_voice.py（唯一定义处，回答/思考词/唤醒应答同一个人）。
  2. 同一响度：跑同一个 loudnorm，否则缝上音量会跳一下，一耳朵就听出是两段。
  3. 同一采样率/声道：现役回答实测 48000Hz/单声道/128kbps，这里硬编成同一套。
     mp3 是定长帧，拼接处只要参数一致就不会爆音。

★ concat 不用 `-c copy`：每个 mp3 头部都有编码器延迟（约 1105 采样）和尾部 padding，
  直接拷帧拼起来，缝上会留一小段静音或一声咔哒。**解码成 PCM → 拼 → 重编一次**，
  缝才是干净的。这条是"合成一条流"能不能听的前提。

合成出来的东西长这样（时间轴向右）：
    [ 嗯…… ][ 让我想想啊 ][ 这个……怎么说呢 ][ 回答的正文…… ]
     └────────── 一条 mp3，一次推送，中间没有断口 ──────────┘
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ★★ 2026-09-23：缓存目录可以用 `SPK_FILLER_DIR` 换掉 —— 给**电话人设线**隔离用。
#   为什么非有这个开关不可：`index.json` 是【全局单例】，而人设线（那个换人设的开关）
#   用的是另一个嗓子（Ethan）。两边共用一份索引的后果是**互相覆盖** ——
#   谁先接通谁就发现"缓存参数与当前不符"⇒ 把整批素材重建成**自己**的嗓子 ⇒
#   另一个人下一次读到的是别人的声音。这台机器上已经出过一次同形状的事故：
#   2026-09-23 一通电话把音箱的思考音从 `qwen · Maia` 拽回了 edge。
#   ⇒ 人设线在自己的启动脚本里把 `SPK_FILLER_DIR` 指到另一个目录，两条线各建各的。
#   ★ 不设这个变量时路径**一个字节都没变** ⇒ 音箱侧零影响。
CACHE = os.environ.get('SPK_FILLER_DIR') or os.path.join(HERE, 'filler')
INDEX = os.path.join(CACHE, 'index.json')

# ★ 嗓子 / 响度 / 输出格式全部来自 spk_voice.py —— 唯一定义处。
#   原先这里写着「必须与 spkbrain-macmini.py 保持一字不差」，靠注释维持一致的东西
#   迟早不一致，而不一致 = 思考词和回答是两个人的声音，一耳朵就听出来。
sys.path.insert(0, HERE)
import spk_voice as _voice          # noqa: E402
import spk_tts_qwen as _qwen        # noqa: E402   Qwen 嗓子（2026-09-23 主人拍板全屋换）
SR, CH, BR = _voice.SR, _voice.CH, _voice.BR
# ★ 原先这里有 `VOICE = _voice.VOICE`（edge 的嗓子名），**删掉了**：
#   它冻在 import 那一刻，换引擎后日志会印出一个早已过期的名字。
#   现在一律用 `_voice.voice_label()` 现取 —— 它带引擎标记（`qwen · Maia` /
#   `zh-CN-XiaoxiaoNeural · 速-8% · 调-10Hz`），既能写日志也能当缓存键。

# ★ 思考音的语气指令（**只 Qwen 引擎用**，edge 没有这个参数）。
#   ★★ 故意与回答的 `spk_tts_qwen.INSTR_DEFAULT`（"像真人打电话聊天那样自然"）不同：
#      思考音是**自言自语、边想边念叨**，不是对着人讲话。用回答那条会念出一种
#      "在跟你说话"的腔调，而它此刻其实是在主人面前"思考出声"。
#   ★ 这条主人还没听过 —— 觉得不对就改这个环境变量，不用动代码：
#      SPK_FILLER_INSTR='……'
FILLER_INSTR = os.environ.get(
    'SPK_FILLER_INSTR',
    '像一个人自己琢磨事情那样，小声地、慢慢地念叨，带点犹豫和思考的停顿，'
    '不要用对着别人说话的语气。')


def _norm():
    """取【当前】的响度滤镜串。

    ★ 必须每次现取，不能在 import 时存成局部名 —— 那样就把值冻在进程启动那一刻了，
      之后改音量（语音说"小声点"、或我在命令行调）这里不会跟着变，
      思考词和回答会一个响一个轻。
      （2026-09-21 就是这么踩的：三处合成点里只有这一处冻住了。）"""
    return _voice.chain()

# ★ 段尾留白的上限（毫秒）。实测 edge-tts 念完"。"和"……"会留 0.9~1.0 秒静音，
#   直接拼起来段间就是 ~1.1 秒死气。但【不能全删】—— "嗯……"那个停顿本身就是犹豫，
#   删了就变成干巴巴的一声"嗯"。所以是【限到 450ms】，不是削平。
PAUSE_MS = int(os.environ.get('SPK_FILLER_PAUSE_MS', '450'))
# 段首留白只留这么一点（毫秒），免得两段黏在一起听不出断句
LEAD_MS = int(os.environ.get('SPK_FILLER_LEAD_MS', '60'))

EDGE = os.environ.get('EDGE_TTS', 'edge-tts')
FFMPEG = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/usr/bin/ffprobe'

# 犹豫时人真的会说的话 —— 故意长短不一，拼起来才不像复读机。
# 挑的时候避开"稍等/马上"这类承诺时间的词，等久了会显得在骗人。
#
# ★★ 2026-09-22 按三档重排（主人：「分成1秒 2秒和5秒的」）。档位不是写死的，
#    `tiers()` 按【实测 dur】现分 —— 换嗓子会整批重建，硬编码文件名就烂了。
FILLERS = [
    # —— 1 秒档：他说完先应一声，代表"听到了"。
    #   ★ 只是语气词，不是"好/行"这种答应词：主人可能是在【问问题】，
    #     回一句"诶，好"很怪；语气词对"提问"和"指令"都成立。
    #   ★ 五条而不是一条 —— 只有一条就没法"随机说一句"，每次都"嗯……"像复读机。
    '嗯……',
    '诶……',
    '哎……',
    '哦……',
    '唔……',
    # —— 2 秒档：★★ 主人 2026-09-22 指出「1 秒已经说过拟声词了，2 秒和 5 秒就直接说内容」。
    #   所以这一档往后 **一条都不许以语气词开头** —— "嗯……我想想"就是那句重复。
    #   而且开场词**不跨档复用**（"这个……"曾经占了两档，连着听像卡带）。
    '让我想想啊。',
    '我得想想啊。',
    '得好好想想。',
    '这个得琢磨琢磨。',
    '我再理一理啊。',
    # —— 5 秒档：等了 10 秒以上才用。★ 主人要的"让我再想想 / 确实有点复杂 之类的"——
    #   说的是【承认这事值得花时间】，不是对问题本身下判断（他要是问"几点了"，
    #   回一句"这题太难"就很荒谬）。★ 同样一个承诺时间的词都没有（"马上/稍等"会让
    #   等久了显得在骗人）。
    '这个确实得好好想想，我再慢慢捋一捋。',
    '说起来话有点长啊，我一点一点慢慢理。',
    '这里头弯弯绕绕还挺多，我一点一点捋清楚。',
    '我得把它想周全了，再慢慢跟你说啊。',
]


def _log(m):
    print(m, flush=True)


def dur_of(path):
    p = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True)
    try:
        return float(p.stdout.decode().strip())
    except ValueError:
        return 0.0


def silences(path, thresh=-45, d=0.35):
    """回 [(起, 止), …]。★ 这是"哪里有停顿"的唯一判据，别拿感觉猜。"""
    p = subprocess.run([FFMPEG, '-i', path, '-af',
                        'silencedetect=noise=%ddB:d=%s' % (thresh, d), '-f', 'null', '-'],
                       capture_output=True)
    txt = p.stderr.decode('utf-8', 'replace')
    st = [float(m) for m in re.findall(r'silence_start: ([\d.]+)', txt)]
    en = [float(m) for m in re.findall(r'silence_end: ([\d.]+)', txt)]
    return list(zip(st, en))


def speech_span(path):
    """这句话【真的有声音】的起止（秒）。没有停顿就当整条都是话。"""
    total = dur_of(path)
    s = silences(path, d=0.12)
    if not s:
        return 0.0, total
    start = s[0][0] if s[0][0] < 0.5 else 0.0          # 头部留白：起手就是静音才算
    end = total
    for a, b in reversed(s):
        if abs(b - total) < 0.15:                       # 尾部留白：一直静到结尾才算
            end = a
        else:
            break
    return start, end



# ---------------------------------------------------------------- 思考词缓存
def _key(text):
    """缓存键含【引擎+嗓子】/loudnorm/留白档 —— 任一项变了旧缓存都自动失效，
    不会新旧参数混在同一条流里。"""
    # ★★ 用 `_voice.voice_label()`（**现取**）：它把**引擎**也带了进来 ——
    #    edge 时是 `zh-CN-XiaoxiaoNeural · 速-8% · 调-10Hz`，Qwen 时是 `qwen · Maia`。
    #    ⇒ 换引擎 / 换音色 / 改降速降调，缓存**全部自动失效**。
    #    2026-09-23 换 Qwen 就是靠这一条：旧 edge 素材一条都没被误用，
    #    也不需要手工清 `filler/` 目录。
    # ★ 别退回 `_voice.label()`：那是 edge 那一套的描述，换成 Qwen 后它一个字都不变
    #    ⇒ 缓存不失效 ⇒ 思考音会继续播 edge 时代的老文件（"换了引擎却只有回答变了"）。
    h = hashlib.md5(('%s|%s|%s|%d|%d' % (_voice.voice_label(), _norm(), text, PAUSE_MS, LEAD_MS))
                    .encode()).hexdigest()
    return h[:10]


def _synth_qwen(text, out):
    """用 Qwen 合成一条思考词到 `out`（mp3）。

    ★ 失败**抛异常**，与上面 edge 那条 `check=True` 的行为对齐 —— 交给 `build()`
      既有的错误处理，**绝不在这里吞掉**：静默留下半条素材，主人听到的是
      "嗯"说了一半就没了，而日志一个字都不会说。
    ★ 语气走 `FILLER_INSTR`（不是回答那条 INSTR_DEFAULT，理由见那里的注释）。
    ★ `spk_tts_qwen.synth` 的契约是"失败一律返回 False，绝不抛" ⇒ 这里自己判。
    """
    if not _qwen.synth(text, out, instructions=FILLER_INSTR):
        raise RuntimeError('Qwen 合成失败（%s）：%s' % (_voice.qwen_label(), text[:24]))


def _tts(text, out):
    """一条思考词：**引擎 → loudnorm → 掐掉多余的段尾留白**。

    ★ 最后那步掐留白是这条流水线的要害（实测结论，别删）：
      edge-tts 念完"。"和"……"会在尾巴上留 0.9~1.0 秒静音，直接拼起来段间
      就是 ~1.1 秒死气 —— 一条 2.5 秒的短语里只有 ~1.4 秒是真话。
      限到 PAUSE_MS（450ms）之后：既保住了犹豫的"停顿感"，又不白送时间。

    ★★ 2026-09-23 更正一句**写错过的话**：这步原来注着"Qwen 也有同样的段尾留白"。
      **没有。** 14 条 Qwen 素材实测 12 条 `end == dur`（末尾零留白），
      edge 那批则是每条 `dur - end ≈ 0.47`。
      ⇒ 所以这步对 Qwen 实际是**空转**（`keep_end = min(total, s1+0.45)`，
        `s1 ≈ total` ⇒ 保留到结尾，一个字节没掐）。

      ★ 为什么**不补足**到 450ms（想过，没做）：动它就得动 edge 那条老路的后处理，
        而"换引擎只换嗓子、后处理一个字节不变"是这次改动的纪律。
        **而且生产路径根本不吃这段留白** —— `ladder()` 是【一条一段】推的，
        段间间隔由 `gap_after(k)` + 抖动算，不靠素材自带尾巴。
        ⇒ 补留白只对**诊断用**的 `concat()`（`--track` / `--demo`）有意义：
          那两条路拼出来的音频会比 edge 时代黏一点。**诊断结论别拿它当生产听感。**

    ★★ 2026-09-23：这里原先**写死 edge-tts**。现在只把**第一段**按引擎分发，
      后面（滤镜串、掐留白、格式）两条路**完全共用** —— 这样"换引擎"换的
      只是嗓子，音频后处理一个字节都没变，响度对齐是白拿的。
      （旧素材靠 `_key()` 里的引擎标记自动失效，不用手工清 `filler/`。）
    """
    raw = out + '.raw'
    nrm = out + '.nrm'
    if _voice.use_qwen():
        _synth_qwen(text, raw)
    else:
        subprocess.run([EDGE] + _voice.edge_args() + ['--text', text, '--write-media', raw],
                       check=True, capture_output=True, timeout=60)
    # ★★ `-f mp3` 一个字都不能少 —— 输出文件叫 `<out>.nrm`，而 **ffmpeg 靠扩展名猜容器格式**，
    #    `.nrm` 它不认 ⇒ 直接 `rc=234 Unable to choose an output format` ⇒ 滤镜整条没跑。
    #    2026-09-21 深夜抓到的老 bug：这条流水线**从第一天起就没成功过**，而下面那句
    #    `shutil.copy` 把 rc=234 静默吞了 ⇒ 思考音一直是 edge-tts 的**原始电平**（-20.4dB），
    #    比回答（-45dB：回答走 spk_ai_dlna.tts，输出 /tmp/spk_ai.mp3，扩展名对、滤镜正常）
    #    **响 25dB** —— 主人听到的「回答和思考词一个响一个轻」就是它。
    #    ★ 教训：我"手动复刻 ffmpeg 命令证明滤镜能用"时把输出写成了 /tmp/xxx.mp3，
    #      扩展名一对，复刻的就**不是同一条路径**了 ⇒ 验出一个假的"没问题"。
    #      跟唤醒词那次同源：自建用例与真实输入不同源，就永远测不出错。
    p = subprocess.run([FFMPEG, '-y', '-i', raw, '-af', _norm(),
                        '-ar', SR, '-ac', CH, '-b:a', BR, '-f', 'mp3', nrm],
                       capture_output=True)
    if p.returncode != 0 or not os.path.exists(nrm):
        # ★ 失败必须出声：静默退回 = 悄悄换成一个响 25dB 的东西，而日志一个字都没有。
        _log('   ★★ 滤镜失败 rc=%d，这条思考音退回原始电平（会明显偏响）：%s'
             % (p.returncode, (p.stderr or b'').decode('utf-8', 'replace').strip()[-200:]))
        shutil.copy(raw, nrm)

    # 掐头留尾：头部只留 LEAD_MS，尾部留白压到不超过 PAUSE_MS
    total = dur_of(nrm)
    s0, s1 = speech_span(nrm)
    cut = max(0.0, s0 - LEAD_MS / 1000.0)
    keep_end = min(total, s1 + PAUSE_MS / 1000.0)
    q = subprocess.run([FFMPEG, '-y', '-ss', '%.3f' % cut, '-t', '%.3f' % (keep_end - cut),
                        '-i', nrm, '-ar', SR, '-ac', CH, '-b:a', BR, out], capture_output=True)
    if q.returncode != 0 or not os.path.exists(out):
        _log('   ★ 掐留白失败 rc=%d，这条思考音直接用整段：%s'
             % (q.returncode, (q.stderr or b'').decode('utf-8', 'replace').strip()[-200:]))
        shutil.copy(nrm, out)
    for t in (raw, nrm):
        if os.path.exists(t):
            os.unlink(t)
    return out


def load_index():
    try:
        with open(INDEX) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    # 参数变了就当没缓存（宁可重生成，也不让两套响度/嗓子/留白混在一条流里）
    # ★★ 比的是 `_voice.voice_label()`（**带引擎标记**），跟 `_key()` 同一个理由：
    #   窄一格就会静默失败 —— 只比嗓子名，改了 rate/pitch 而名字没变 ⇒ 判"没变"
    #   ⇒ 缓存全留着 ⇒ 一条都不重建，思考词继续用老调子念（踩过）。
    #   同理：换成 Qwen 后 `_voice.label()` 一个字都不变 ⇒ 会把整批 edge 老素材
    #   当成"参数没变"接着用。
    if d.get('voice') != _voice.voice_label() or d.get('norm') != _norm() or d.get('sr') != SR \
            or d.get('pause_ms') != PAUSE_MS or d.get('lead_ms') != LEAD_MS:
        _log('   (缓存参数与当前不符，全部重建)')
        return None
    if len(d.get('items', [])) != len(FILLERS):
        return None
    # ★★ 比【文本列表】本身，不是只比条数 —— 改了一句的措辞（条数没变）时，
    #   只比条数会判"没变"⇒ 缓存全留着 ⇒ **那句老话继续念，而且没有任何报错**。
    #   这是同一个坑在这个文件里第二次出现（第一次是 rate/pitch，见上一条注释）；
    #   两次的形状一样：**判据比实际语义窄了一格，失败时静默**。
    if [it.get('text') for it in d.get('items', [])] != list(FILLERS):
        _log('   (思考词文本变了，全部重建)')
        return None
    for it in d['items']:
        if not os.path.exists(it['path']):
            return None
    # ★ `end`（人声真正结束的位置）是阶梯唯一的取数，而算它要跑一次 ffmpeg
    #   ⇒ **必须在 build 时算好存进来**，绝不能在推流那一刻现算：
    #   那 150ms 会落在关键路径上，把第一声"嗯"从 0.6 秒推到 0.8 秒。
    if any('end' not in it for it in d['items']):
        _log('   (索引里没有人声结束位置，重建一次)')
        return None
    return d


def build(force=False):
    """把每条思考词生成一次并永久缓存。回 index。"""
    os.makedirs(CACHE, exist_ok=True)
    if not force:
        d = load_index()
        if d:
            return d
    # ★ 现取（不是 `VOICE` 那个冻住的常量）—— 换引擎后这行日志要如实说出
    #   "现在是谁在念"，否则排查"思考音没换过来"时会被自己的日志误导。
    _log('   生成 %d 条思考词（%s / 引擎 %s / 留白上限 %dms）…'
         % (len(FILLERS), _voice.voice_label(),
            'qwen' if _voice.use_qwen() else 'edge', PAUSE_MS))
    items = []
    for t in FILLERS:
        path = os.path.join(CACHE, '%s.mp3' % _key(t))
        if force or not os.path.exists(path):
            _tts(t, path)
        # ★ `end` = 人声真正结束的位置。★ 必须用这个、**不是** `dur`：
        #   `spk_ai_dlna._stage601()` 会把短音频补静音到「end + 17 秒」，
        #   所以一条 0.91 秒的思考音在设备上是个 17.45 秒的文件 ——
        #   拿 `dur` 当"说到哪了"会错 16 秒（实测）。
        items.append({'text': t, 'path': path, 'dur': round(dur_of(path), 3),
                      'end': round(speech_span(path)[1], 3)})
        _log('     %-18s %5.2fs  人声止 %5.2fs  %s'
             % (t, items[-1]['dur'], items[-1]['end'], os.path.basename(path)))
    d = {'voice': _voice.voice_label(), 'norm': _norm(), 'sr': SR,
         'pause_ms': PAUSE_MS, 'lead_ms': LEAD_MS, 'items': items}
    with open(INDEX, 'w') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return d


# ---------------------------------------------------------------- concat（核心）
def concat(paths, out):
    """把若干 mp3 顺序拼成一条。

    ★ 不是 `-c copy`（理由见文件头）：全部解码成 PCM、顺序接起来、再编一次 mp3。
      代价是要重编码，收益是缝上不留静音/咔哒 —— 这条流是要"听着像一个人连续说话"的。
    """
    if not paths:
        raise ValueError('concat: 没有输入')
    if len(paths) == 1:
        shutil.copy(paths[0], out)
        return out
    lst = out + '.list'
    with open(lst, 'w') as f:
        for p in paths:
            # concat demuxer 的单引号转义：' → '\''
            f.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
    try:
        p = subprocess.run([FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', lst,
                            '-ar', SR, '-ac', CH, '-c:a', 'libmp3lame', '-b:a', BR, out],
                           capture_output=True)
    finally:
        if os.path.exists(lst):
            os.unlink(lst)
    if p.returncode != 0:
        raise RuntimeError('concat 失败: ' + p.stderr.decode('utf-8', 'replace')[-400:])
    return out


def sequence(seconds, first=None):
    """挑一串思考词凑够 seconds 秒（返回 mp3 路径列表，不拼接）。

    ★ 返回【列表】而不是拼好的文件，是为了后面能做流式：一条一条往外喂，
      一旦回答好了就停在短语边界上，不会把整段思考词硬念完。
    """
    d = build()
    items = d['items']
    if not items:
        return []
    order, out, total, k = list(range(len(items))), [], 0.0, 0
    random.shuffle(order)
    last = first
    while total < seconds:
        i = order[k % len(order)]
        if k and k % len(order) == 0:       # 一轮用完就重洗，免得每轮顺序一样
            random.shuffle(order)
        k += 1
        if len(items) > 1 and last is not None and items[i]['text'] == last:
            i = order[k % len(order)]
            k += 1
        out.append(items[i]['path'])
        total += items[i]['dur']
        last = items[i]['text']
    return out


def thinking_track(seconds, out):
    """凑够 seconds 秒的思考词，拼成一条 mp3。"""
    return concat(sequence(seconds), out)


def with_answer(prefix_paths, answer_mp3, out):
    """[思考词…] + [回答] 拼成一条 —— 这就是最终要推给音箱的那条流。"""
    return concat(list(prefix_paths) + [answer_mp3], out)


# ---------------------------------------------------------------- 衔接词（2026-09-22 主人加）
#
# 主人原话：
#   「再加一道 有回复后 再播一个 想到了 这样 是这样 等衔接过渡词 然后再开始说真回复」
#
# ★ 为什么需要它：思考音是"在想"，回答是"想好了" —— 直接从前者跳到后者，
#   听感上像话被截断。一句"哦，有了"把这次转折交代掉，人就觉得顺。
#
# ★★ 为什么【不】做成一条单独的音频（像思考音那样一条一条推）：
#   两条流推同一台设备，后 Play 掐先 Play ⇒ 要么等衔接词播完再推回答（多一次
#   掐断风险 + 白等零点几秒），要么被掐。而**衔接词跟回答本来就必须连着说**，
#   没有"中途让路"的需求 —— 那是思考音才有的需求。
#   ⇒ 直接拼进 TTS 的**文本**，跟回答一次合成：零额外延迟（常驻连接上多 5 个字
#     约 0.1 秒），而且同一个 TTS 会话里念出来，停顿天然自然。
#
# ★ 内容只描述"我想出来了"，**不评价问题本身**（他要是问"几点了"，
#   回一句"这题好难"就很荒谬），也不承诺时间。
BRIDGES = [
    '哦，有了。',
    '嗯，是这样。',
    '诶，我知道了。',
    '哦，想明白了。',
    '嗯，有了啊。',
]


def bridge():
    """挑一句衔接词。★ 只回文本 —— 它跟回答一起合成（见上）。"""
    return random.choice(BRIDGES)


# ================================================================ 阶梯（2026-09-22）
#
# ★★ 为什么不再拼一条 30 秒的长轨：
#   文件头那条"必须 concat 成一条"的理由（DLNA 一次只认一个 URI、推第二次要重新
#   SSDP 发现 1~3 秒）**已经不成立** —— 现役出口是 0x601（`netease.ihw.player` 的
#   D-Bus 直连，实测命令→出声 185ms，不发现、不重试）。而长轨的代价实测很重：
#   12 轮里「思考音推出 → 回答开始」的间隔是 1~12 秒（中位 4.0），
#   **每一轮都把那条轨腰斩在第 4 秒左右**。
#
# ★★★ 主人 2026-09-22 定的做法：**一句一段**，分 1 秒 / 2 秒 / 5 秒三档，而且
#   「你决定让音箱说思考词 说什么你知道长短 你就能等这个时间之后再决定让他说
#   思考词还是结果」—— 即：
#
#       只在【这一条的人声已经播完】的时刻，才决定推下一条还是让位给回答。
#
#   这把"不腰斩"从**估算时间**变成了**结构性保证**：推任何东西的时刻，永远
#   ≥ 上一条的人声结束时刻 ⇒ 物理上不可能把人一句话切两半。
#
# ★ 为什么用「人声结束」而不是「文件结束」：`spk_ai_dlna._stage601()` 会把短音频
#   **补静音到「人声 + 17 秒」**（设备固件在 `duration−15s` 处发 Next，不补长就每句
#   只念开头两秒）。所以一条 0.9 秒的思考音在设备上是个 19 秒的文件 ——
#   按文件总长估算"播到哪了"会**错 18 秒**。
#
# ★★ 间隔封顶 2.0 秒是【设备给的】，不是我们挑的：补长的基准是「人声结束 + 17 秒」，
#   而守卫在 `duration − 15 秒` ⇒ **设备自己在人声说完后 2 秒把播放器暂停**
#   （发 Next → 总控去云端取歌 → 域名被劫持取不到 → 重试 → 发 0x602 暂停）。
#   推送落在人声结束后的这 2 秒里，先 Play 已经只剩静音 ⇒ 后 Play 掐先 Play 听感无缝。
#   超过 2 秒就要面对"设备已经暂停"这个没验过的局面，所以 GAPS 封顶 2.0。
#
# ★ 与 `sequence()`/`thinking_track()` 的关系：那两个是**流式的原意**（docstring 里
#   写着"一条一条往外喂，一旦回答好了就停在短语边界上"），但从没被实装过
#   （唯一调用方 `thinking_track()` 把它们 concat 成一条）。本函数就是那个原意的
#   实装。那两个保留给 `--track` / `--demo` 自检，**生产链不再调**。

TIER_SHORT, TIER_MID, TIER_LONG = 'short', 'mid', 'long'

# 档位【由实测 dur 现分】—— ★ 绝不写死文件名：改 `SPK_VOICE`/`SPK_RATE` 会整批重建缓存，
# 硬编码的文件名当场就烂了。实测现库：语气词 0.91~1.0 / 中档 1.6~2.2 / 长句 4~5。
SHORT_MAX = float(os.environ.get('SPK_FILLER_SHORT_MAX', '1.3'))
LONG_MIN = float(os.environ.get('SPK_FILLER_LONG_MIN', '3.0'))

# 等超过这么久 ⇒ 换长句（主人选的"混合：先短句，超 10 秒才上长句"）
XL_AFTER = float(os.environ.get('SPK_FILLER_XL_AFTER', '10.0'))

# 三条间隔：第 1 条之后 / 第 2 条之后 / 第 3 条及之后。
#
# ★★ 2026-09-22 实测：旧注释里那条"上界"是**假的**。
#   旧说法是"设备在人声说完后 2 秒自己发 0x602 暂停（补长的守卫），所以我们推流的
#   时刻必须落在那 2 秒窗口【里面】，否则撞上没验过的'设备已暂停'"。
#   那天用**静音轨**（零声音）在真机上把它验了，结论是 **越窗零代价**：
#
#       设备状态（由构造保证，不是猜的）        推 → 设备来取流
#       PLAYING（长轨才播 4 秒，守卫在 49 秒）   0.598 秒
#       已暂停（守卫已经把它停掉了）            0.601 秒      ← 差 3 毫秒
#
#   ⇒ **0x601 根本不关心播放器当时是什么状态**（它是"设 URI + Play"，不是"续播"）。
#     所以 `PUSH_LEAD + gap ≤ 2.0` 这条约束**不存在**，间隔只由听感决定。
#   ★ 验法留在 /tmp/spk_stage601_exp.py。当时 DLNA 已退役，所以判据不是 SOAP 查状态，
#     而是**抓包** `sudo tcpdump -tt -nn -l -A -i <你的网卡名> 'tcp port 8899'`
#     看设备几时来取流（微秒精度）。
#
# ★ 人耳听到的间隔 = `PUSH_LEAD + gap`（**不是 gap**）：两条流各付一遍"推→出声"的
#   延迟，延迟自己两头抵消，但 `PUSH_LEAD` 是**加进推流时刻**的，它会留下来。
#   代入主人原话「1秒的说完**等1秒**再说个2秒的…说完**再等两秒左右**再说5秒」：
#       gap=0.6 ⇒ 人耳 1.0 秒（第 1→2 条）✓
#       gap=1.2 ⇒ 人耳 1.6 秒（第 2→3 条）
#       gap=1.6 ⇒ 人耳 2.0 秒（第 3 条及以后，封顶）✓
GAPS = tuple(float(x) for x in
             os.environ.get('SPK_FILLER_GAPS', '0.6,1.2,1.6').split(','))

# 他说完到第一声"嗯"。★ 0.6 而不是 1.0：判端点本身还要吃掉约 0.4 秒尾静音，
# 主人**感知**到的就是"等 1 秒"——跟他原话一致。★ 这一条前面没有东西在播。
FIRST_DELAY = float(os.environ.get('SPK_FILLER_FIRST', '0.6'))

# 推出去到【人声开始】的估计延迟。★ 它唯一的职责是**防腰斩**：算小了，我们就会以为
# "上一条已经说完了"而提前推下一条 ⇒ 切字。
#
# ★★★ 但**判据不是 `lead ≥ 真实延迟`**（旧注释这么写，是错的）。下一条要掐掉上一条，
#   **它自己也得先付一遍同样的延迟** ⇒ 两头抵消。真正的不等式是
#       `lead + gap + L(下一条) > L(上一条)`   ⟺   `lead + gap > 延迟的抖动`
#   （`L` = "调 push" → "设备来取流"）实测：0.598 / 0.601 / 0.826 秒 ⇒ 抖 **0.23 秒**，
#   而 `lead + gap ≥ 0.4 + 0.6 = 1.0 秒` ⇒ **余量 4 倍**。
#   ★ 所以**千万别**照旧注释去把 lead 调到"实测延迟"那么大（0.6+）—— 那只会让人耳间隔
#     白白长 0.3 秒，防腰斩一点没多。
#   ★ 口径注：旧注释写的"实测 185ms"跟这里不是一个口径（多半是设备侧 dbus-send→出声
#     那一小段），**别拿它跟"调 push→取流"比** —— 比了就会得出"lead 不够"的错结论。
PUSH_LEAD = float(os.environ.get('SPK_FILLER_LEAD', '0.4'))

# ★★★ 间隔随机化（2026-09-22 主人：「每个思考词之间至少间隔1秒吧…最好有个随机间隔」）
#
#   ★ 只往上加，**绝不下探** —— 这样人耳间隔的下限永远是
#     `PUSH_LEAD + min(GAPS) = 0.4 + 0.6 = 1.0` 秒，随机只让它变长，
#     永远跌不破主人要的那条线。
#   ⇒ 人耳间隔 = 1.0~1.4 / 1.6~2.0 / 2.0~2.4 秒（前两次 / 第三次及以后）。
#
#   ★ 为什么不取 `uniform(-J, +J)` 的对称抖动：那会让人耳间隔掉到 0.6 秒 ——
#     正好是主人这次提意见要修的那个数。**下界是他给的，不能随机掉。**
GAP_JITTER = float(os.environ.get('SPK_FILLER_JITTER', '0.4'))

# ★★★ 2026-09-22 主人改口：「我觉得思考词是可以被真回复打断的」
#   「因为真回复加了衔接 就不会显得那么突兀」
#   ⇒ 原设计"答案到了也要**等当前这条人声说完**才让路"**作废**，改成**答案一到当场让路**。
#
#   ★ 为什么敢掐：答案那条流的**开头就是我们自己拼上去的衔接词**
#     （`spk_ear.turn()` 里 `ans = _bridge() + ans`，只要思考音出过声就一定会加）
#     ⇒ 它本身就是一句"接话"的信号，掐在哪儿都不像话被截断。
#
#   ★★★ 但**两类"掐"必须分开，别一起放开**：
#     · **思考词掐思考词** = 纯损失，没有任何东西为它兜底 ⇒ **红线照旧**（见 `ladder()` 的
#       内部不变量：推下一条的时刻永远 ≥ 上一条的人声结束）。
#     · **答案掐思考词** = 有衔接词兜底 ⇒ **本条放开的只有这一种**。
#
#   ★ 收益不是常数（别拿它当"提速"卖）：`turn()` 里合成（实测约 2.08 秒）与这个等待
#     **是并行的** ⇒ 人声剩余 < 合成耗时的那些轮次本来就不花钱，改了零收益；
#     只有答案落在一条长句刚推出去时（人声剩余最长 4.46 秒）才真省下 (剩余 − 合成)。
#
#   ★ `SPK_FILLER_WAIT_EOS=1` 退回旧行为（等这条说完）—— 真机上万一听着别扭，一键回退。
WAIT_EOS = os.environ.get('SPK_FILLER_WAIT_EOS', '0') == '1'


def gap_after(k):
    """刚推完第 k 条（0 起）之后的**基准**间隔。越等越长，封顶 GAPS[-1]。

    ★ 只是基准 —— 真正用的间隔还要加一个 [0, GAP_JITTER] 的随机量（见 `ladder(jit=)`）。
      本函数保持纯函数，好让测试拿固定值断言骨架。
    """
    return GAPS[min(k, len(GAPS) - 1)]


def tiers():
    """按【实测 dur】把思考词分三档。空的那档就是空列表（长句曾经是空的）。"""
    t = {TIER_SHORT: [], TIER_MID: [], TIER_LONG: []}
    for it in sorted(build()['items'], key=lambda x: x['dur']):
        k = (TIER_SHORT if it['dur'] < SHORT_MAX
             else (TIER_MID if it['dur'] < LONG_MIN else TIER_LONG))
        t[k].append(it)
    return t


def pick_tier(n, elapsed):
    """默认档位：第 1 条 = "听到了"；10 秒内 = "在想"；超 10 秒 = 长句。"""
    if n == 0:
        return TIER_SHORT
    return TIER_LONG if elapsed >= XL_AFTER else TIER_MID


def pick_one(T, tier, used):
    """从这一档里挑一条**这次还没用过的**；一轮用完就放开重来。

    回那条的 item（含 `path` 和 `end`），不是光回路径 —— 阶梯要用 `end` 算"说到哪了"，
    而路径本身给不出这个数。
    """
    pool = T.get(tier) or []
    if not pool:                       # 这一档是空的 ⇒ 退到最接近的一档，别一个字不说
        for alt in (TIER_MID, TIER_SHORT, TIER_LONG):
            if T.get(alt):
                pool = T[alt]
                break    # noqa: E701
        if not pool:
            return None
    fresh = [it for it in pool if it['path'] not in used]
    it = random.choice(fresh or pool)
    used.add(it['path'])
    return it


def ladder(push, ready, pick=None, stop=None, log=None, clock=None, sleep=None,
           deadline=20.0, first=None, lead=None, poll=0.05, T=None, jit=None):
    """阶梯式思考音 —— **一条一段**，每条的【人声播完】之后才决定下一条还是收工。

    依赖全部注入（时钟、sleep、push、ready、pick）⇒ 能拿假时钟跑几百种时序，
    **零声音、零设备**。这是本次唯一能"证明不腰斩"的地方。

      push(path) -> bool     推一条（生产里是 `spk_ai_dlna.push`）
      ready()    -> bool     答案好了没（生产里是一个 `threading.Event.is_set`）
      pick(n, t) -> 'short'|'mid'|'long'|None    第 n 条（0 起）要哪一档
      stop       -> Event    硬中止（`turn()` 的 join 超时后才 set）；None = 没有
      T          -> tiers()  档位表（测试注入，免得每次去读盘）

    ★★ 阶梯内部的**红线**（一字不改）：**推【下一条思考词】的时刻，
       永远 ≥ 上一条的人声结束时刻** ⇒ 思考词之间绝不会自己掐自己
       （那没有任何东西为它兜底，是纯损失）。

    ★★★ 但**退出**是另一回事（2026-09-22 主人改口）：
       **答案可以打断正在播的思考词** —— 因为答案那条流的开头就是衔接词
       （`turn()` 里 `ans = _bridge() + ans`），它本身就是"接话"的信号。
       ⇒ 退出条件是 `ready()` 一条，不再等 `spoken_end`。
       ★ 两类"掐"的区别必须留着：**思考词掐思考词 = 纯损失；
         答案掐思考词 = 有衔接词兜底。只有后者被放开**（见 `WAIT_EOS`）。

    ★ 本函数【只推思考词，从不推回答】。答案好了它就退出，把设备让给调用方 ——
      这样"什么时候推回答"仍然只有 `speak()` 一件事说了算，不在这里分叉。

    返回每次推送的记录（`at` = 推的时刻，`end` = 这一条人声说完的时刻；都是相对起点）。
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    pick = pick or pick_tier
    first = FIRST_DELAY if first is None else first
    lead = PUSH_LEAD if lead is None else lead
    T = tiers() if T is None else T
    # 间隔抖动。★ 注入点：测试传 `jit=0`（或任何数）就退化成确定性。
    if jit is None:
        jit = lambda: random.uniform(0.0, GAP_JITTER)            # noqa: E731
    elif not callable(jit):
        _jv = float(jit)
        jit = lambda: _jv                                        # noqa: E731

    def lg(f, *a):
        if log is None:
            _log(f % a if a else f)
        else:
            log(f, *a)

    used, recs = set(), []
    t0 = clock()
    spoken_end = t0            # 还没有东西在播 ⇒ "没人正在说话"从现在起就成立
    t_next = t0 + first
    n = 0

    while True:
        if stop is not None and stop.is_set():
            break
        now = clock()
        # ★★ 决策点。
        #   ★ 2026-09-22 主人改口后：**答案一到就当场让路**（理由见 `WAIT_EOS` 上面那段）——
        #     要退回"等这条人声说完"就设 `SPK_FILLER_WAIT_EOS=1`。
        if ready() and (not WAIT_EOS or now >= spoken_end):
            break
        if now >= t_next:
            el = now - t0
            if el > deadline:                      # 到点不再出声，静默等答案
                if el > deadline * 2:              # 兜底：调用方连 stop 都没设的极端情况
                    break
                sleep(poll)
                continue
            tier = pick(n, el)
            it = pick_one(T, tier, used)
            if it is None:
                break
            # ★★ `end` 来自 build 时算好的索引 —— 绝不在这一刻现算：
            #   算它要跑一次 ffmpeg（约 150ms），而这里正是关键路径，
            #   会把第一声"嗯"从 0.6 秒推到 0.8 秒。老索引（没有 end）才兜底现算。
            end = it.get('end')
            if end is None:
                end = speech_span(it['path'])[1]
            if not push(it['path']):
                lg('   💭 阶梯：第 %d 条没推出去，收工', n + 1)
                break
            spoken_end = now + lead + end
            recs.append({'n': n, 'tier': tier, 'path': it['path'],
                         'at': round(now - t0, 3), 'end': round(spoken_end - t0, 3)})
            n += 1
            g = gap_after(n - 1) + jit()                         # ★ 基准 + 随机（只加不减）
            t_next = spoken_end + g
            recs[-1]['gap'] = round(g, 3)
            # ★ 日志里报的是【gap】，不是人耳间隔 —— 人耳还要加 PUSH_LEAD。
            #   实测口径：`gap 0.60 → 日志"说到 1.44 / 推在 2.05"`，而人耳是 1.00 秒。
            lg('   💭 思考音 #%d（%s：%s）—— 推在 %.2fs，说到 %.2fs，隔 %.2fs（人耳 %.2fs）',
               n, tier, it['text'], now - t0, spoken_end - t0, g, g + lead)
            continue
        sleep(poll)
    return recs


# ---------------------------------------------------------------- 自检
def _merge(iv, gap=0.20):
    """把挨得近的区间合并（跨缝时 A 的尾静音和 B 的头静音会连成一段，这是正常的）。"""
    out = []
    for a, b in sorted(iv):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def check(out, parts):
    """给一条合成流对账。

    判据不是"有没有长静音"——【长静音本来就该有】（"嗯……"的停顿、句号后的留白）。
    真正的判据是：**合成流里的停顿，必须能逐一对上各段自己的停顿**。
    对不上的那一段，才是拼接缝上冒出来的伪影。

    回 (时长, 未对上的停顿列表)
    """
    total = dur_of(out)
    want = sum(dur_of(p) for p in parts)
    exp, off = [], 0.0
    for p in parts:
        exp += [(a + off, b + off) for a, b in silences(p)]
        off += dur_of(p)
    exp_m = _merge(exp)
    got = silences(out)

    unmatched = []
    for a, b in got:
        if not any(a >= ea - 0.25 and b <= eb + 0.25 for ea, eb in exp_m):
            unmatched.append((a, b))

    print('   时长 %.2fs（各段之和 %.2fs，差 %+.3fs）%s'
          % (total, want, total - want,
             '✓' if abs(total - want) < 0.25 else '✗ 差太多，可能有整段丢了'))
    print('   停顿 %d 处，其中应由各段自己解释的 %d 处  %s'
          % (len(got), len(exp_m),
             '✓ 全部对得上' if not unmatched else '✗ %d 处对不上（疑似缝上伪影）' % len(unmatched)))
    for a, b in unmatched:
        print('      ★ %5.2f→%5.2f (%.2fs) 对不上任何一段' % (a, b, b - a))
    return total, unmatched


def _main(argv):
    if '--build' in argv:
        _log('① 生成思考词缓存 → %s' % CACHE)
        d = build(force='--force' in argv)
        tot = sum(i['dur'] for i in d['items'])
        _log('   共 %d 条，合计 %.2fs，最短 %.2fs / 最长 %.2fs'
             % (len(d['items']), tot, min(i['dur'] for i in d['items']),
                max(i['dur'] for i in d['items'])))
        return 0

    secs = 6.0
    for a in argv:
        if a.startswith('--secs='):
            secs = float(a.split('=', 1)[1])

    if '--track' in argv or '--demo' in argv:
        _log('① 思考词缓存')
        d = build()
        _log('   %d 条可用' % len(d['items']))
        _log('② 挑一串凑够 %.1fs 并 concat' % secs)
        seq = sequence(secs)
        for p in seq:
            txt = next((i['text'] for i in d['items'] if i['path'] == p), '')
            print('     + %-16s %.2fs' % (txt, dur_of(p)))
        out = '/tmp/spk_filler_track.mp3'
        concat(seq, out)
        _log('③ 对账 %s' % out)
        check(out, seq)
        if '--demo' in argv:
            _log('④ 再拼一句假回答，看整条流')
            fake = '/tmp/spk_filler_fake_answer.mp3'
            _tts('好的，我明白了。', fake)
            both = '/tmp/spk_filler_with_answer.mp3'
            with_answer(seq, fake, both)
            check(both, list(seq) + [fake])
        return 0

    print(__doc__)
    print('用法: python3 spk_filler.py --build | --track [--secs=6] | --demo')
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
