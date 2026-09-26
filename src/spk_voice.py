#!/usr/bin/env python3
"""音箱嗓子的【唯一定义处】—— 回答、思考词、唤醒应答，三处必须同一个人。

★ 为什么要有这个文件
  嗓子原先在三个地方各写一份：spkbrain-macmini.py 的 make_mp3、spk_filler.py 的思考词、
  spk_ai_dlna.py 的 tts()。spk_filler.py 里还留着一句注释「必须与 spkbrain-macmini.py
  保持一字不差」—— 靠注释维持一致的东西迟早不一致，而不一致的后果是"音箱换了一个人"，
  听感上立刻不对，查起来却要翻三个文件。

  现在改嗓子只改这里。要给某个服务单独试别的嗓子，在 systemd 单元里设环境变量覆盖即可：
      Environment=SPK_VOICE=zh-CN-XiaoyiNeural
      Environment=SPK_RATE=-12%
      Environment=SPK_PITCH=-20Hz

★ 现役嗓子（2026-09-21 主人从 7 条试听里选定「01」）
  晓晓 + 播音轻：降速 8% + 降调 10Hz。
  背景：edge-tts 免费端点只有 4 个 zh-CN 女声，Azure 真正的播报嗓（晓秋/晓睿/晓墨…）
  逐个试合成全部取不到 ⇒ 播音腔只能从晓晓上调出来。试听档案见 /tmp/voice_samples/。
"""
import os
import sys

# 空串 = 不调那一项（edge-tts 不接受空值参数，见 edge_args）
VOICE = os.environ.get('SPK_VOICE', 'zh-CN-XiaoxiaoNeural')
RATE = os.environ.get('SPK_RATE', '-8%')
PITCH = os.environ.get('SPK_PITCH', '-10Hz')

# ── 音量 ──────────────────────────────────────────────────────────────────
# ★ 先说清这台设备的音量到底由谁说了算（2026-09-21 又踩了一次，写在最前面）：
#    UPnP 的 SetVolume 是【影子值】—— 返回成功、设备报的值也不变，不驱动真实输出。
#    真正管用的杠杆，缺一不可：
#      ① 这里的【软件增益】—— 改的是 mp3 的采样，谁都绕不过去，完全在我们手里
#         ★ 语音助手调的"音量 0-100 档"就是它（spk_skills.adjust_volume/set_volume）。
#      ② 设备那一侧 —— 要 adb/root，而且【分两层，别再混】：
#         · DAC volume（numid19）是【音量】，在 DAC 那一级，实测管得住 DLNA 推流
#           （150→100 = -37.5dB）
#         · Headphone/Phoneout Switch（numid105/106）是【通断】，在 DAC 之后 ——
#           夜间禁声用的就是它（见 device/nightmute.sh）
#         ⚠️ 而 headphone/lineout volume 那几条是【装饰】，实测挪 43dB 只动 0.7dB
#
# ★ 教训：上一轮只动了 ②（50→20）就以为调小了，可 ① 一直顶在 I=-14 ——
#   那是 EBU R128 的广播/流媒体标准，对床头这种小喇叭就是"最大声"，
#   2026-09-21 把人吵醒了。两个杠杆必须一起看。
#
# ★ 为什么把 NORM 和 GAIN 分开，而不是直接把 I 调小：
#    I 负责"把所有回答拉齐到同一条基准线"——回答、思考词、唤醒应答三处合成点
#    必须落在同一条线上，否则拼起来音量会跳一下，一耳朵就听出是两段。
#    GAIN 负责"这条基准线整体抬多高"。分开之后，调音量不会破坏那个一致性。
NORM = 'loudnorm=I=-14:TP=-1.5:LRA=11'

# ── 运行时音量档 ─────────────────────────────────────────────────────────
# ★ 为什么要单独搞一份"运行时"的：上面那串是模块加载时算死的，改一次就得重启服务。
#   可"嫌吵"是随时发生的（尤其半夜），那时候没人会去重启进程。
#   所以增益落到一个档案里，运行时可改 —— 语音指令"小声点/大声点"走的就是这条路。
#
# ★ 唯一入口是 chain()：它每次现读档案。三个合成点（spk_ai_dlna.tts /
#   spkbrain-macmini.make_mp3 / spk_filler._tts）都调它。
#   【千万别把它的返回值存成模块常量或局部名】—— 那会把音量冻在进程启动那一刻，
#   之后语音说"小声点"只有一半的合成点会变，回答和思考词一个响一个轻。
#   2026-09-21 就是这么踩的（spk_filler 那处冻住了），所以这里干脆不再导出常量。
GAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.spk_gain')
GAIN_DEFAULT = -20.0                # 2026-09-21 定：比广播级 -14 低 20 dB
GAIN_MIN, GAIN_MAX = -45.0, -4.0    # ★ 最响封在 -4 dB：再也不许回到"最大声"


def gain_db():
    """当前软件增益（dB，负数）。档案没有就用兜底值。"""
    try:
        with open(GAIN_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return GAIN_DEFAULT


def _compose(db):
    return '%s,volume=%.1fdB' % (NORM, db) if db else NORM


def set_gain_db(db):
    """落盘。返回夹紧后的实际值。

    ★ 只写档案、不在内存里缓存 —— 因为调音量的可能是另一个进程
      （我在命令行调 / 语音指令在 spk-ear 进程里调），缓存一份就必然有
      一边看到旧值。chain() 每次现读，谁都立刻生效、不用重启服务。"""
    db = max(GAIN_MIN, min(GAIN_MAX, float(db)))
    try:
        with open(GAIN_FILE, 'w') as f:
            f.write('%.1f\n' % db)
    except Exception:
        pass
    return db


def chain():
    """当前完整的响度滤镜串 ——【三个合成点都用它，别在 import 时存成常量】。

    ★ 这里就是"音量"这件事唯一的真话来源：想改音量只改 .spk_gain，
      改完不用重启任何服务。存成模块常量的话，值会冻在进程启动那一刻，
      而"嫌吵"是随时发生的 —— 半夜没人会去重启进程。
      （2026-09-21 踩过：三处合成点里 spk_filler 那处是 import 时取的，冻住了。）"""
    return os.environ.get('SPK_LOUDNORM') or _compose(gain_db())


def db_for_level(v):
    """口语里的 0~100 音量档 → dB。★ 以当前默认档为 50 居中，越大越响。
    100 档也只到 GAIN_MAX（-4dB）—— 说"最大声"也回不到当初那个吵醒人的电平。"""
    return max(GAIN_MIN, min(GAIN_MAX, GAIN_DEFAULT + (float(v) - 50.0) * 0.4))


def level_for_db(db):
    """反查：dB → 0~100 档位（status 报给用户听的数）。"""
    return int(round((float(db) - GAIN_DEFAULT) / 0.4 + 50))

# 输出格式。现役回答实测 48000 Hz / 1 ch / 128 kbps（ffprobe /tmp/spk_reply.mp3）。
SR, CH, BR = '48000', '1', '128k'


def edge_args():
    """edge-tts 的嗓子参数。

    ★ 必须用 `--rate=-8%` 这种【等号】写法，不能用 `['--rate', '-8%']`：
      argparse 看见以 - 开头的值会当成另一个选项，直接报错。实测踩过。
    ★ RATE/PITCH 为空串时不传 —— 传空值同样报错。
    """
    a = ['--voice=%s' % VOICE]
    if RATE:
        a.append('--rate=%s' % RATE)
    if PITCH:
        a.append('--pitch=%s' % PITCH)
    return a


def label():
    """一行描述现役嗓子，给日志和缓存键用。

    ★ 缓存键必须用它而不是 VOICE：spk_filler.py 拿这个算思考词的缓存哈希，
      只比 VOICE 的话，改了降速/降调而嗓子名没变，缓存不会失效 —— 思考词还是老声音。
    """
    bits = [VOICE]
    if RATE:
        bits.append('速%s' % RATE)
    if PITCH:
        bits.append('调%s' % PITCH)
    return ' · '.join(bits)


# ── Qwen 嗓子（2026-09-23 主人拍板「全屋一起换」，音箱也换）──────────────────
# ★★ 为什么 Qwen 音色必须定义在**这个文件**：
#   Qwen 音色原先在 `spk_tts_qwen.py:78` 自己写了一份 `'Cherry'` ——
#   那就是第二条真话来源。后果不是报错，而是"回答是 Maia、思考音是 Cherry"
#   这种**只有耳朵能发现**的错（主人 2026-09-23 就是这么听出来的：
#   「感觉思考音不是一个嗓子」）。嗓子名一散开，迟早散成两个人。
# ★ 环境变量名 `SPK_QWEN_VOICE` 沿用旧的：另一个壳那两条早就在用它选音色
#   （house=Maia / 人设=Ethan）⇒ 音箱进程不设它，自然落到这里的默认值。
QWEN_VOICE = os.environ.get('SPK_QWEN_VOICE', 'Maia')

# ── 合成引擎开关 ─────────────────────────────────────────────────────────
# ★ 一个开关换整条嗓子链（回答 / 思考音 / 唤醒应答三处都读它）：
#       SPK_TTS_ENGINE=edge     ⇒ 一键退回 edge 晓晓那条老路
#   默认 qwen（2026-09-23 起全屋一个嗓子）。
# ★★ 缓存键必须把引擎带进去（见 `spk_filler._key`）—— 否则换引擎后键不变，
#   思考音会继续播 edge 时代生成的那批老文件，听起来"换了引擎却只有回答变了"。
TTS_ENGINE = (os.environ.get('SPK_TTS_ENGINE') or 'qwen').strip().lower()


def use_qwen():
    """现役合成引擎是不是 Qwen。

    ★ **每次现读环境变量**，与 `chain()` 同一条纪律：可切换的东西绝不冻在
      import 那一刻（2026-09-21 把 gain 冻住过一次，三个合成点里只有一个跟着变）。
    """
    return (os.environ.get('SPK_TTS_ENGINE') or 'qwen').strip().lower() != 'edge'


def qwen_voice():
    """现役 Qwen 音色（现读，同上）。"""
    return os.environ.get('SPK_QWEN_VOICE') or QWEN_VOICE


def qwen_label():
    """Qwen 嗓子的一行描述 —— 与 `label()` 同用途：日志、以及**缓存键**。"""
    return 'qwen · %s' % qwen_voice()


def voice_label():
    """【现役引擎】的嗓子标签。要拼缓存键、要在日志里写"现在是谁在说话"，用这个。

    ★ 别拿 `label()` 当万能标签：它描述的是 edge 那套（晓晓/降速/降调），
      换成 Qwen 之后它一个字都不会变 ⇒ 缓存不失效、日志也在撒谎。
    """
    return qwen_label() if use_qwen() else label()


if __name__ == '__main__':
    # 查 / 调音量：  python3 spk_voice.py          查
    #               python3 spk_voice.py -26       设成 -26 dB
    # 调完不用重启服务 —— set_gain_db() 就地刷新，下一次合成就是新音量。
    if len(sys.argv) > 1:
        print('软件增益: %.1f dB → %.1f dB' % (gain_db(), set_gain_db(sys.argv[1])))
    print('现役嗓子: %s' % label())
    print('edge-tts 参数: %s' % ' '.join(edge_args()))
    print('软件增益: %.1f dB   档位范围: %.0f ~ %.0f dB' % (gain_db(), GAIN_MIN, GAIN_MAX))
    print('输出: %s Hz / %sch / %s' % (SR, CH, BR))
    print('滤镜串: %s' % chain())
