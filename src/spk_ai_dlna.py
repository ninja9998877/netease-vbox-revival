"""把 DeepSeek 接上这台音箱：问题 → DeepSeek → edge-tts → 音箱念出来。

★★ 说话有两条出口，由 SPK_PLAY_VIA 选（默认 601，见下面"播放出口：两条路"那节）：
   601  直接敲设备自己的播放器（0x601/0x603，走设备上常驻的 play601.sh）
        —— 不用发现、不碰音量、命令到出声实测 185ms。**代价：短音频必须补静音**
        （设备在 duration−15 秒处发 Next，总控就把它掐了），见 _stage601。
   dlna 原来的 DLNA 那条路（SSDP 发现 + KPlayer 的 MediaRenderer），留着当回退。

两个实测出来的硬约束，代码都是围着它们写的：
  ★1 音箱的 UPnP HTTP 端口是**动态**的（实测 5005→1344→1489→1422→1816，几十秒一变）
     ⇒ 每次推流都必须现用 SSDP 找端口，且发现后立刻用。
     ★ 这条【只对 dlna 那条路成立】—— 601 那条路根本不发现，所以它不受这条约束，
       也不受 SSDP 那 0.21~6.34 秒的抖动影响。
  ★2 这台设备对高频探测限流 ⇒ SSDP 要重试，不能问一次就放弃。
  ★3 "成功"的判据不是 SOAP 回了 200，而是**静态服务器日志里出现了它来取音频的 GET**。
     ★ 601 那条路判据完全一样（fetched_count）—— 换个出口不改判据。
  ★4 但取文件只证明链路走到了，**不证明喇叭响了** —— HP_L Mux 那次全哑事故里每一环都是
     "成功"的，喇叭一个字节都没出。所以 say() 还会【自己听】：推流时开着耳朵，
     从音箱自己的麦克风里确认真的有声。判据从"它来取文件了"升级成"我听见了"。
     ★★ 2026-09-21 深夜又降了一级：设备固件的 AEC 拿"我们自己正在放的"当参考消掉了，
       这条路上耳朵**听不到自己的声音**（交叉相关 0.0138 / 0.0573）⇒ 自听改成
       【只记，不判】。所以现在 ①②里真正管用的还是①，②只当参考。
密钥只从环境变量读，绝不落盘、绝不打印。
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
import warnings

# 包格式只允许在 mictap_sink.py 里定义一次，这里 import 过来用。拿不到就退化成"听不了"，
# 但绝不让 say() 因此失败（见下面 Ear.start 的注释）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cfg import (HOST, DEVICE_IP, DATA_DIR,                        # noqa: E402
                  DEVICE_DIR, DEVICE_TMP, PORT_MP3, PORT_MICTAP)
try:
    import mictap_sink as _mit
except Exception:
    _mit = None

import spk_voice as _voice          # ★ 嗓子的唯一定义处（回答 / 思考词 / 唤醒应答同一人）
import spk_tts as _tts_warm         # ★ 常驻连接的 TTS（快路）；失败就退回下面的命令行
import spk_tts_qwen as _qwen        # ★ Qwen 嗓子（2026-09-23 主人拍板「全屋一起换」）

# audioop 是 C 速度的 RMS，但它标了 deprecated 且 Python 3.13 已删 ⇒ 静音导入 + 有兜底。
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    try:
        import audioop as _audioop
    except Exception:
        _audioop = None

SPK_IP = DEVICE_IP
OUR_IP = HOST
AV = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
BASE = os.environ.get("DS_BASE", "https://api.deepseek.com/anthropic")

# ---------------------------------------------------------------- 模型与思考模式
#
# ★★ 2026-09-22 主人拍板：正式回答也用 flash、并且关掉思考。
#   实测（拿【真】提示词 + 真 17 个工具 + 10 条真实指令跑的，不是猜的）：
#
#       pro + 思考   1.75 秒/轮     判错 3/10
#       pro 关思考   1.28 秒/轮     判错 3/10
#       flash 关思考 0.71 秒/轮     判错 3/10
#
#   三档判错数【完全相同】，而且那 3 处是我期望写错了（"声音大一点"它调的是
#   adjust_volume{percent:8} —— 真工具、比 set_volume 更准），不是模型错。
#   ⇒ 关思考是纯赚，换 flash 再赚一倍。
#   ★ 换 flash 不是新发现：这个文件里早有证据 —— "让路"那句话一直用 flash，
#     注释写着"实测 2.2s vs pro 6.4s，而两句话一样好"。当时只敢用在那一句上。
#
# ★★ `{"type":"disabled"}` 才是【关】。`{"type":"enabled","budget_tokens":0}`
#   不是关 —— 实测反而更慢（4.36/4.93 秒），别用它。
#   DS_THINK=1 可以随时把思考开回来（要复核质量时用）。
MODEL = os.environ.get("DS_MODEL", "deepseek-v4-flash")
THINK = os.environ.get("DS_THINK", "0") == "1"


def _think_of(body):
    """把思考开关写进请求体。关的时候显式写 disabled，别靠"不写"默认。"""
    if not THINK:
        body["thinking"] = {"type": "disabled"}
    return body
# ★ 日志绝不能放 /tmp。这台机器 fs.protected_regular=2，/tmp 是 sticky 世界可写目录，
#   而这个静态服务器以 root 跑 —— root 在那种目录里以 O_CREAT 打开【属于别人的已存在文件】
#   会被内核直接 EACCES，systemd 报 "Failed to set up standard output: Permission denied"
#   （退出码 209/STDOUT），服务只会 2 秒一重启地刷失败。放这儿不受那条规则管。
#   顺带：这份日志本身就是"推流成没成"的判据，跨重启留着比在 /tmp 更该留。
SRVLOG = str(DATA_DIR / 'log' / 'spk_srv.log')


# ---------------------------------------------------------------- 推流预算
#
# ★★★ 2026-09-22 立的规矩：**失败路径必须有预算。**
#
# 主人问的是"第 7 步推流耗时跨度怎么这么大"。查出来的真相不是"抖"，是
# **失败路径根本没有上限**：
#
#   旧 discover() = 6 轮 ×（3 秒 recvfrom 超时 + 2 秒 sleep）= **最坏 30 秒/次**
#   旧 say(tries=8) = 8 次 × 30 秒                        = **最坏 240 秒**
#
# 2026-09-22 上午日志实证：10:12:37 → 10:16:07，每 30 秒一行，**3 分半一个字没念出来**。
# 而成功路径一直是 0.2 秒（缓存 + 一个 TCP 探活）。
#
# ★ 为什么会走到失败路径：DLNA 会【静默卡死】—— KPlayer 进程活着、日志一行错都没有、
#   DBus 心跳每 30 秒正常刷，但**一个端口都不监听**（那天从 08:37 起卡了近 3 小时）。
#   这时 SSDP 实测 0/6 应答、TCP 全 ConnectionRefused。我们的代码对此毫无察觉，
#   只会一遍遍重试到天亮。
#
# ★ 预算只卡【还没推出去】的阶段 —— 推出去之后"等它念完"不该被砍
#   （那是有效工作，不是空等）。
DISCOVER_BUDGET = float(os.environ.get("SPK_DISCOVER_BUDGET", "3.0"))
SAY_BUDGET = float(os.environ.get("SPK_SAY_BUDGET", "20.0"))


# ---------------------------------------------------------------- SSDP 发现
def discover(rounds=6, budget=None):
    """SSDP 找 DLNA 渲染器，回 (base_url, udn)。带重试（设备会限流）+ 总预算。

    ★ 正常情况（DLNA 活着）第 1 轮 0.2 秒就应答 ⇒ 加预算对它【一点没变慢】。
      预算只砍掉"根本没人在听"时的那几十秒白等。
    ★ 保留 rounds 和每轮 2 秒间隔的原形状，只是一律受 budget 约束 ——
      设备限流时"多试几轮"这个能力还在（3 秒内还能试 2~3 轮）。
    """
    if budget is None:
        budget = DISCOVER_BUDGET
    m = ('M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1900\r\n'
         'MAN:"ssdp:discover"\r\nMX:2\r\nST:ssdp:all\r\n\r\n').encode()
    t_start = time.time()
    for rnd in range(1, rounds + 1):
        left = budget - (time.time() - t_start)
        if left <= 0.2:
            break
        # 每轮最多等 2 秒（协议 MX:2 就是让它 2 秒内答），且不超过剩下的预算
        per = min(2.0, left)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(per)
        try:
            s.sendto(m, ('239.255.255.250', 1900))
            t0 = time.time()
            while time.time() - t0 < per:
                d, a = s.recvfrom(4096)
                # ★ SPK_IP 没配（`SPK_DEVICE_IP` 是空串）时**不筛** —— 空串会让
                #   `a[0] != ''` 恒真，把所有应答全拒掉 ⇒ 这条路静默永远不通、还不报错。
                #   取舍：配了 `SPK_DEVICE_IP` 就精确筛；没配就认**第一个应答者** ——
                #   网上有多台同类设备时可能选错，所以生产环境应该把它配上。
                if SPK_IP and a[0] != SPK_IP:
                    continue          # ★ SPK_IP 没配时不筛 —— 宁可选中一个，也不要静默永远不通
                txt = d.decode('utf-8', 'replace')
                loc = re.search(r'(?im)^Location:\s*(\S+)', txt)
                usn = re.search(r'(?im)^USN:\s*uuid:([0-9a-f-]+)', txt)
                if loc and usn:
                    return loc.group(1).rstrip('/'), usn.group(1)
        except Exception:
            pass
        finally:
            s.close()
        left = budget - (time.time() - t_start)
        if left > 0.5:
            time.sleep(min(2.0, left - 0.3))
    return None, None


# ---------------------------------------------------------------- SSDP 发现（带缓存）
#
# ★★★ 为什么非缓存不可（2026-09-21 深夜实测，不是猜的）：
#   设备对 M-SEARCH 是【限流】的 —— 连做 5 次 discover：
#
#       1.23s ✓   1.26s ✓   1.22s ✓   11.22s ✓   15.20s ✓
#
#   每次都要重试 2~3 轮，而每一轮失败要白等 **3 秒 recvfrom 超时 + 2 秒间隔**。
#   这一个数就是主人反复看到的"问完十几秒才有动静"的根。
#   ★ 而且 5 次拿回来的 base **全是 `http://192.168.1.50:5005`** —— 端口稳稳的没变。
#   所以：拿到一次就记下来，下次先用一个 **TCP 连接**（几毫秒）问它"还活着吗"，
#   活着就直接用；不活才真去 SSDP。这样把 60% 的快路径和 40% 的慢路径一起变成快路径，
#   而且设备换端口也能自愈（探不通就重新发现）。
_disc = {'base': None, 'udn': None, 'at': 0.0}


def _alive(base, timeout=0.6):
    """缓存里的 base 还通不通（一个 TCP 连接就够，几毫秒）。"""
    try:
        host, _, port = base.split('//', 1)[1].partition('/')[0].partition(':')
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _refused(base, timeout=0.6):
    """端口【明确拒绝】连接（RST）⇒ 那个端口上没人在听。

    ★★ 这是"DLNA 卡死"和"设备只是慢/在省电"之间唯一干净的判据：
      · 省电/限流 ⇒ TCP 是【超时】（要等满 timeout），SSDP 值得多试几轮
      · 服务卡死 ⇒ TCP 是【立刻 RST】（实测几毫秒），SSDP 也一定不应答

    实测（2026-09-22 上午，KPlayer 的 DLNA 模块卡死那次）：1837/5005/49152/49153
    全 ConnectionRefused，同一时刻 SSDP 组播 0/6、单播 0/3 ⇒ 两件事同源。

    ★ 但【不能凭它就跳过 SSDP】—— 设备换端口时旧端口同样是 RST，
      而那时重新发现是必须的。所以它只用来"把这次 SSDP 的预算调小"。
    """
    try:
        host, _, port = base.split('//', 1)[1].partition('/')[0].partition(':')
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return False
    except ConnectionRefusedError:
        return True
    except Exception:
        return False


def discover_fast():
    """先用缓存的 (base,udn)，探不通才真去 SSDP。回 (base, udn)。"""
    b, u = _disc['base'], _disc['udn']
    if b and _alive(b):
        return b, u
    # ★ 缓存的端口明确拒绝 ⇒ 这次 SSDP 只给一小段预算（够发现"换了端口"，
    #   不够"白等一个已经死掉的服务"）。分不出是哪种，所以两条路都留着。
    base, udn = discover(budget=1.5 if (b and _refused(b)) else None)
    if base:
        _disc.update(base=base, udn=udn, at=time.time())
    else:
        # ★ 找不到就把缓存清掉：留着一个探不通的旧值，只会让下一次先白等 0.6 秒。
        _disc.update(base=None, udn=None, at=0.0)
    return base, udn


def _forget():
    """把缓存的发现结果作废（SOAP 打脸的时候调）。"""
    _disc.update(base=None, udn=None, at=0.0)


# ---------------------------------------------------------------- SOAP

def soap(base, udn, svc, ns, action, args, timeout=6):
    b = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
         f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
         f'<u:{action} xmlns:u="{ns}">{args}</u:{action}></s:Body></s:Envelope>').encode()
    r = urllib.request.Request(f"{base}/{svc}/{udn}/control.xml", data=b,
                               headers={"Content-Type": 'text/xml; charset="utf-8"',
                                        "SOAPACTION": f'"{ns}#{action}"'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()
    except Exception as e:
        return f"<{type(e).__name__}>"


def fetched_count(fname):
    """音箱来取过这个文件几次（静态服务器日志里数）。"""
    try:
        with open(SRVLOG) as f:
            return f.read().count(f"GET /{fname} ")
    except OSError:
        return 0


# ---------------------------------------------------------------- 自听：听见没有
# ★ 用户要求（2026-09-21）："你以后让音箱说话 可以启一个临时监听 自己听 人不可能实时在"
#   ⇒ 判据从「它来取文件了」升级成「我听见了」。取文件只证明链路走到了，不证明喇叭响了。
# ★ 隐私边界（用户 2026-09-21 拍板 "验完就删"）：音频【只在内存里过一遍】，验过了就丢，
#   一个字节都不落盘。只有【该出声却没出声】时才写一份 wav —— 那正是需要证据的时候。
# ★ 铁律：这一层【永远不许影响播放】。耳朵起不来（端口被占 / mictap 没在推）就如实说
#   "没听成"，退回原来的判据，绝不能让 say() 因此不推流或者报失败。
MICTAP_PORT = int(os.environ.get('MICTAP_PORT', PORT_MICTAP))
SELFHEAR = os.environ.get('SPK_SELFHEAR', '1') == '1'
KEEP_DIR = os.environ.get('SPK_SELFHEAR_KEEP', str(DATA_DIR / 'log'))  # ''=从不落盘
KEEP_MAX = 3            # 最多留几份 unheard-*.wav，多的滚掉
# ★★★ 门限是【实测标定】的（2026-09-21），不是拍脑袋。当天实测：
#     安静房间本底 0.0004（8 秒里最大的一次扰动 0.0012）；喇叭念话时 mic 峰值 0.015~0.15。
#     ⇒ 信噪比 40~900 倍，门限 0.003 稳稳落在"噪声之上、说话之下"的空档正中间。
#   ★ 峰值那个跨度是真的（同一句话 0.022 / 0.12 / 0.15 都出现过），别拿单次测量当常数；
#     判据是【比例】不是绝对值，所以那头怎么飘都不影响结论。
#   本底取"最安静那四分之一"的 25 分位 —— 取中位数会被说话本身抬上去。
# ★ 已知边界（老实写在这儿）：这是【电平】判据，不是音频比对。房间里持续有别的响动
#   （电视/说话）把本底抬到跟喇叭声一个量级时，会判成"没响"（假阴性）。标定现场的本底
#   只有 0.0004、信噪比 40~900 倍，离这条边界很远。真撞上了，下一步不是调门限，是拿
#   这条音频自己的包络去跟录音做对齐 —— 那才分得清"是它在说"还是"屋里有别人在说"。
#   日志每次都打本底和信噪比，就是为了让这种事一眼看得出来，而不是变成查不出的怪毛病。
NOISE_MULT = 5.0        # 峰值至少要高出本底这么多倍
# ★ 绝对地板【随软件增益缩放】（2026-09-21 修）。上面那句标定是对着"最大声"那天做的
#   （喇叭念话峰值 0.015~0.15）。加了软件增益之后，同一句话的峰值等比缩小，门限却还
#   钉在 0.003 ⇒ 音量调小之后会把"念了"误判成"没念"（假阴性，日志说谎）。
#   按"比标定基准低多少分贝"等比缩。
#   ★★★ 2026-09-21 20:36 实测把基准改正了（这次有铁证，不是估的）：
#     增益 -28dB 时它念了一句，自听峰值只有 **0.0008**，老门限 0.0015 判成"没念成"。
#     可把那份 unheard-*.wav 拿去 ASR，转出来的正是我们让它说的那句
#     （"没太听清你是在跟我说话吗"）⇒ **它念了，是门限说谎**。
#     ⇒ 反推喇叭峰值 ≈ 0.0008 × 10^((g+28)/20)：g=0 时约 0.02、g=-20 时约 0.002。
#     旧式子把基准记成 -20（等于假定 g=-20 时就有 0.003），**高估了约 10dB**。
#     所以基准改到 **0 dB**，绝对地板压到 0.0003（只当"麦克风整条死掉"的兜底，
#     正常情况都由上面那个 NOISE_MULT×本底 的活判据说话）。
#   ★ 必须现算，不能在模块加载时算成常量 —— 那又是"值冻在进程启动那一刻"的老坑。
GAIN_REF_DB = 0.0       # ★ 标定基准（2026-09-21 20:36 实测反推，原来是 -20，高估 10dB）
ABS_MIN_BASE = 0.003
ABS_MIN_FLOOR = 0.0003  # ★ 只兜底"麦克风整条死掉"；正常都由 NOISE_MULT×本底 判


def abs_min():
    """当前的绝对地板（dB → 线性缩放，见上面注释）。"""
    try:
        g = _voice.gain_db()
    except Exception:
        return ABS_MIN_BASE
    return max(ABS_MIN_BASE * min(1.0, 10.0 ** ((g - GAIN_REF_DB) / 20.0)), ABS_MIN_FLOOR)


LOUD_MIN_RATIO = 0.45   # 有声时长至少要到"该有的人声长度"的这个比例。实测真念是 66~72%，
                        # 留一半余量；宁严勿松 —— 假报"念了"正是这套东西要消灭的那个毛病。
MIN_LOUD = 0.25         # 有声时长的绝对下限（秒）。比例判据遇到极短的分母会失灵，这条兜底。
MIN_SPEAK = 0.5         # 源音频自己人声短于这个 ⇒ 它本来就是静音，没什么可听的
                        # ★ 2026-09-24 从 1.0 降到 0.5：1.0 把**合法的短句**也拦死了。
                        #   让路那句「嗯，你说」只有三个字，实测人声 0.988s（源文件）／
                        #   0.850s（补静音后的产物）—— 两条路都 < 1.0 ⇒ 判据**永远**说它
                        #   "几乎没有人声"。后果不是"没响"（链路是通的：日志里 `★ 音箱在取了`
                        #   照样出现），而是**拿不到 `audible_end()`** ⇒ 关麦窗口退化成锚在
                        #   "推完"而不是"真响完"（正是 `self-echo-deaf-window` 那个坑）。
                        #   ★ 真正的空音频只有 0.04s（09-22 那次），0.5 照样拦得住；
                        #     而"极短的分母"本来就有上面那条 `MIN_LOUD = 0.25` 在兜底。


def mp3_span(path, default=4.0):
    """量一条音频：(全长, 人声总长, 人声结束的位置)。

    ★★ 三个数各有各的用处，一个都不能拿去顶另一个：
      · `total` —— 设备那条切换线按【它】算（`duration − 15 秒` 就发 Next）。
      · `speak` —— 自听判据的分母（"该念多久"），**不是"念到哪儿"**。
      · `end`   —— **这条音频的实质内容到哪儿为止**。补静音必须按它补。
    ★★ 拿 `speak` 当 `end` 会错得很隐蔽：思考音那条 29.5 秒的轨里头到尾都在说话
      （结尾静音只有 0.44 秒），speak=14.3 而 **end=29.0** —— 按 speak 算会得出
      "够长、不用补"，而实际的守卫落在 14.5 秒，**正好把它拦腰切断**。
    """
    try:
        p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'csv=p=0', path], capture_output=True, timeout=10)
        total = float(p.stdout.decode().strip())
    except Exception:
        return default, default, default
    starts, durs = [], []
    try:
        # ★ 别加 -v error：silencedetect 是 INFO 级的日志，会被一起吃掉（踩过，
        #   表现是"这条 mp3 里一点静音都没有"，跟真相反着）。
        q = subprocess.run(['ffmpeg', '-hide_banner', '-i', path, '-af',
                            'silencedetect=n=-50dB:d=0.30', '-f', 'null', '-'],
                           capture_output=True, timeout=30)
        starts = [float(x) for x in re.findall(rb'silence_start:\s*([0-9.]+)', q.stderr)]
        durs = [float(x) for x in re.findall(rb'silence_duration:\s*([0-9.]+)', q.stderr)]
    except Exception:
        pass
    sil = sum(durs)
    # ★ 结尾那段静音从哪开始，就是人声结束的位置。判"文件末尾是不是静音"用
    #   0.15 秒容差：silencedetect 的最后一段会一直算到文件尾。
    if starts and durs and starts[-1] + durs[-1] >= total - 0.15:
        end = starts[-1]
    else:
        end = total                    # 结尾就是人声，没得让
    # ★ 不给下限兜底：全静音的文件人声就该是 0，撑到 0.3 会造出一个假分母，
    #   让一次房间杂音就凑够比例（实测就是这么把一条纯静音判成"念了"的）。
    return total, max(total - sil, 0.0), end


def mp3_speech(path, default=4.0):
    """这条音频【真正有人声】的部分有多长。回 (文件秒数, 人声秒数)。

    ★ 分母必须是【人声长度】，不是文件长度：实测同一条 4.73s 的音频，真正的人声只有
      3.45s（尾巴上一截 0.86s 的静音、中间一个 0.42s 的句读停顿）。拿文件长度当分母，
      "整条一个字没漏地念完了"会被算成"只念了七成"。
    ★ 要【人声结束的位置】用 mp3_span()，别拿这里的第二个数顶替（见那条注释）。
    """
    total, speak, _end = mp3_span(path, default)
    return total, speak


class Ear:
    """边推流边听：后台收 mictap 的 UDP 包，按 50ms 一格算 RMS。

    ★ 全程只在内存里。用户定的边界是"验完就删" —— 那就干脆别写。
    ★ 起不来不抛异常，只把原因记在 self.err，让调用方如实报"没听成"。
    """

    SLOT = 0.05

    def __init__(self, port=MICTAP_PORT):
        self.port = port
        self.err = None
        self.pkts = 0
        self.fmt = None
        self.t0 = None
        self._raw = []          # 只在"没听见"时才被写成 wav；正常路径验完就随对象丢掉
        self._acc = {}          # 格子号 -> [平方和, 样本数]
        self._lk = threading.Lock()
        self._stop = threading.Event()
        self._th = None
        self._sock = None

    def start(self):
        if _mit is None:
            self.err = 'import 不到 mictap_sink.py，包格式不知道'
            return False
        # ★ 两个候选端口：先试正经的 mictap 端口；绑不上说明【常驻耳朵 spk_ear.py 已占着】，
        #   那就退到它的转发口（它会把每个包原样再发一份到 127.0.0.1:port+1）。
        #   为什么不用 SO_REUSEADDR 硬抢：UDP 上两个带它的 socket 能同时绑同一端口，
        #   而包只会进其中一个 —— 那是"耳朵明明起来了却一个包都收不到"的怪毛病，
        #   比绑不上难查一万倍。所以这里的规矩是：撞上了就换号，绝不静默抢包。
        err = None
        for cand in (self.port, self.port + 1):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)  # 96k 立体声能到 384KB/s
                s.bind(('0.0.0.0', cand))
                s.settimeout(0.3)
            except OSError as e:
                err = e
                continue
            self.port = cand
            self._sock = s
            break
        else:
            self.err = 'UDP %d/%d 都绑不上（%s）—— 有别的进程在收？' % (self.port, self.port + 1, err)
            return False
        self.t0 = time.time()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        return True

    def _loop(self):
        s = self._sock
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            # 这是麦克风音频，哪怕在局域网里也不该谁都能往这儿灌（同 mictap_sink 的规矩）
            if not _mit.allowed(addr[0]):
                continue
            if len(data) < _mit.HDR.size:
                continue
            magic, ver, seq, frames, rate, ch, fmt, flags = _mit.HDR.unpack_from(data)
            if magic != _mit.MAGIC:
                continue
            payload = data[_mit.HDR.size:]
            w = _mit.FMT_WIDTH.get(fmt, 2)
            n = len(payload) // w * w
            if n <= 0:
                continue
            self.pkts += 1
            self.fmt = (rate, ch, fmt)
            self._raw.append(payload)
            # 按样本数加权累加平方和 —— 包大小不一时结果仍等于整段的真 RMS
            ns = n // w
            ms = self._meansq(payload[:n], w, ns)
            k = int((time.time() - self.t0) / self.SLOT)
            with self._lk:
                a = self._acc.get(k)
                if a is None:
                    a = self._acc[k] = [0.0, 0]
                a[0] += ms * ns
                a[1] += ns

    @staticmethod
    def _meansq(buf, w, ns):
        """一段裸 PCM 的均方值（0..1，按满量程归一）。"""
        fs = float(1 << (8 * w - 1))
        if _audioop is not None:
            return (_audioop.rms(buf, w) / fs) ** 2
        # 兜底（Python ≥3.13 删了 audioop）：只取每个样本的最高字节做粗估，够判"有没有声"
        hi = buf[w - 1::w]
        tot = 0
        for b in hi:
            v = b - 256 if b > 127 else b
            tot += v * v
        return (tot / len(hi) / (128.0 ** 2))

    def series(self):
        """到现在为止的 (秒, RMS) 序列。没收到的格子补 0 —— 时间轴不能被压缩。"""
        with self._lk:
            if not self._acc:
                return []
            kmax = max(self._acc)
            out = []
            for k in range(kmax + 1):
                a = self._acc.get(k)
                out.append(((k + 0.5) * self.SLOT,
                            0.0 if not a or not a[1] else (a[0] / a[1]) ** 0.5))
            return out

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        return self.series()

    def dump(self, path):
        """把收到的音频写成 wav。★ 只在"没听见"时才会被调用。"""
        if not self._raw or self.fmt is None:
            return None
        rate, ch, fmt = self.fmt
        w = _mit.FMT_WIDTH.get(fmt, 2)
        # 设备报的是 S24_LE（4 字节装 24 位），这里按 4 字节原样落盘 ⇒ 当成 s32le 播会整体
        # 偏小 256 倍，纯听人声够用。要精确还得多一步移位，诊断用不着。
        with wave.open(path, 'wb') as f:
            f.setnchannels(ch)
            f.setsampwidth(w)
            f.setframerate(rate)
            f.writeframes(b''.join(self._raw))
        return path


def judge(series, speak_secs, t_push):
    """判「我听见了没有」。回 (True/False/None, 说明, 详情)。

    ★ None 是"判不了"，跟 False 的"没听见"【必须分开】：mictap 一挂就被判成哑巴的话，
      这个功能本身就变成了新的狼来了。
    """
    d = {}
    if not series:
        return None, '一个包都没收到（mictap 没在推？）', d
    if len(series) < 4:
        return None, '只收到 %d 格，样本太少' % len(series), d
    vals = sorted(v for _, v in series)
    floor = vals[len(vals) // 4]                    # 最安静那 1/4 的 25 分位 = 本底
    thr = max(floor * NOISE_MULT, abs_min())
    after = [(t, v) for t, v in series if t >= t_push]
    if not after:
        return None, '推流之后没录到东西', d
    peak = max(v for _, v in after)
    t_peak = min(t for t, v in after if v == peak)
    loud_t = [t for t, v in after if v > thr]
    loud = len(loud_t) * Ear.SLOT
    snr = (peak / floor) if floor > 0 else 999.0
    d.update(floor=floor, thr=thr, peak=peak, t_peak=t_peak, loud=loud, snr=snr,
             t_first=loud_t[0] if loud_t else None, t_last=loud_t[-1] if loud_t else None)
    head = ('本底 %.4f / 门限 %.4f / 峰值 %.4f（%.0fx 本底，第 %.1fs 处）'
            % (floor, thr, peak, snr, t_peak))
    if peak <= thr:
        return False, '喇叭没响 —— %s，推完之后一直没过门限' % head, d
    if loud < max(MIN_LOUD, LOUD_MIN_RATIO * speak_secs):
        return False, ('响了但不够长 —— %s，有声 %.1fs / 该有 %.1fs' % (head, loud, speak_secs)), d
    return True, '听见了 —— %s，有声 %.1fs / 该有 %.1fs' % (head, loud, speak_secs), d


def _keep(ear, mp3_path):
    """没听见时才落盘留证据（验过了就删，不通过才留）。"""
    if not KEEP_DIR:
        return None
    try:
        os.makedirs(KEEP_DIR, exist_ok=True)
        p = os.path.join(KEEP_DIR, 'unheard-%s.wav' % time.strftime('%m%d-%H%M%S'))
        ear.dump(p)
        for old in sorted(glob.glob(os.path.join(KEEP_DIR, 'unheard-*.wav')))[:-KEEP_MAX]:
            os.remove(old)
        print('         （留证：%s，源 %s）' % (p, os.path.basename(mp3_path)))
        return p
    except Exception as e:
        print('         （想留证没留成：%s: %s）' % (type(e).__name__, e))
        return None


def _wait_and_judge(ear, mp3_path, t_push, base=None, udn=None):
    """等它念完，然后出结论。

    ★ 返回三态，多出来的那个是 `'cut'`（被主人打断）。这一路【绝不能】落到
      `_keep()` 上去：`unheard-*.wav` 是"我该出声却没出成"的证据，
      被打断是成功让路 —— 混进去等于用假证据去调自听门限。
    """
    global LAST_ERR
    total, speak = mp3_speech(mp3_path)
    if speak < MIN_SPEAK:
        # ★ 源音频自己就没声 ⇒ 播了也是静音。这【不是耳朵的问题，是这条音频的问题】，
        #   所以要判 False，不能像"判不了"那样退回老判据放行。
        ear.stop()
        print('         👂 ✗ 这条音频本身几乎没有人声（人声 %.2fs / 全长 %.2fs）——播了也听不见'
              % (speak, total))
        LAST_ERR = '这条音频本身几乎没有人声（人声 %.2fs）—— 播了也听不见' % speak
        return False
    t_rel = t_push - ear.t0
    # 上限：推流到开口有 0.2~3 秒的发现/命令开销，再加整条【人声】，再留点余量。
    # ★★ 用 speak 不用 total（2026-09-22 改）。601 那条路要把音频补到"人声+17 秒"，
    #   拿 total 算就等于每句话白等 17 秒（上限 25 秒一撞，每次都等满 25 秒）。
    #   对没补过静音的 DLNA 老路是【逐字节的 no-op】：total 只比 speak 多出尾部
    #   那点静音，而这个循环本来就靠"有声活动横跨了大半条人声"提前收工。
    deadline = time.time() + min(speak + 6.0, 25.0)
    ok, note, d = None, '', {}
    while time.time() < deadline:
        time.sleep(0.25)
        # ★ 打断优先于一切判定。★ 先喊停喇叭（那是响着的、最要紧的），再收耳朵。
        #   收耳朵用 ear.stop() 拿序列但**不判、不 keep** —— 见上面那节的道理。
        if abort_on():
            _stop_playback(base, udn)
            ear.stop()
            print('         ✋ 被打断 —— 不判"听没听见"（这不是没念成，是让路）')
            return 'cut'
        ok, note, d = judge(ear.series(), speak, t_rel)
        # ★ 提前收工要同时满足两条：验过了，【而且】有声活动已经横跨了大半条人声。
        #   只看"验过了"会在句中那个 0.4 秒的停顿上误判成"念完了"，把 SPEAKING
        #   提前清掉（server.py 拿它判"说话中又被叫"）。
        #   跨度用 0.9 倍：句中那个停顿不会让"末次减首次"变长，所以它挡不住提前退出，
        #   只有真正的"后面没词了"才会满足。
        if ok is True and d.get('t_first') is not None \
                and d['t_last'] - d['t_first'] >= 0.9 * speak:
            break
    # ★ 停下来之后【无条件重判一次】：上面那些轮判用的是半截序列，直接采纳会把
    #   "有声 1.6s"这种被截断的数报出去（实际是 2.5s），证据就成了假的。
    ok, note, d = judge(ear.stop(), speak, t_rel)
    print('         👂 %s' % note)
    if ok is True:
        return True
    # ★★★★★ 2026-09-21 深夜：`ok is False` 这一支【不再算失败】。理由是一条硬证据，
    #   不是"大概不准"：
    #     把真回答和自听录音做交叉相关，两次实测 —— **0.0138 / 0.0573**。
    #   录音里那点动静（峰值能到 265x 本底、有声 0.6 秒）**跟我们的回答没有任何关系**。
    #   原因是结构性的：设备固件的 AEC 拿"我们自己正在放的"当参考信号消掉了
    #   （`2ref` 那条），所以**这条路上耳朵听不到自己的声音** —— 不是门限没调好，
    #   调门限永远调不出来。
    #   ⇒ **留着一个坏判据比没有判据更糟**：它会把"明明响了"在日志里写成"没念成"
    #     （2026-09-21 一整晚都在误导排查方向），还会把屋里的录音当"证据"落到
    #     `log/unheard-*.wav` 上 —— 那些录音里是主人家的说话声，不是资产。
    #   ⇒ 降级成【只记，不判】：读数照打（万一哪天换了设备还有用），但成败
    #     只认"它来取文件了没有"（取不到文件在上面就 return False 了）。
    print('         （自听只作参考 —— 这条路上耳朵听不到自己的声音，'
          '按"它来取文件了"算成功）')
    return True


# ---------------------------------------------------------------- 夜间禁声
# 用户要求（2026-09-20）：21:00–07:00 之间音箱【绝对不出声】——家里人都睡了。
# ★ 闸门设在 say() 里，因为它是【唯一没有旁路】的出口：server.py 的 reply()、
#   tts() 手动测试、以及我临时调用，全都经过它。
# ★ 但这只是【软闸门】（我们这边不推流）。设备自己出声的（唤醒"叮"、云端 TTS、
#   闹钟、手机蓝牙/AirPlay 投屏）它挡不住 —— 那道硬闸门在设备上的 nightmute.sh，
#   同一时段把 codec 耳机输出档压到 -63dB 真静音。两道都在，才叫"保证"。
# ★★ 2026-09-22：时段从【写死的常量】改成【运行时配置】—— 主人用嘴就能改。
#   权威文件 `~/.spk_quiet`（跟 ~/.spk_net_token / ~/.spk_gain 同风格），一行两种形态：
#       21 7    ← 时段：起、止（含起不含止；跨零点走"或"，跟原来一样）
#       off     ← 永久关闭夜间静音
#   ★ 设备侧 `/mnt/UDISK/spk/quiet.conf` 是【同一份内容的副本】，设置时推过去，
#     设备只读不写 ⇒ 单向，没有"两份谁说了算"的问题。
#   ★ 下面这两个常量【降级成默认值】，env 仍可覆盖（老部署零迁移）。
NIGHT_FROM = int(os.environ.get('SPK_NIGHT_FROM', '21'))    # 默认起（含）
NIGHT_TO = int(os.environ.get('SPK_NIGHT_TO', '7'))         # 默认止（不含）
NIGHT_QUIET = os.environ.get('SPK_NIGHT_QUIET', '1') == '1'
# ★ 本文件里【没有 HOME 常量】（核实过，grep 零命中），所以必须 expanduser ——
#   别照抄 spk_netd.py 的 os.path.join(HOME, ...)。
QUIET_FILE = os.environ.get('SPK_QUIET_FILE', os.path.expanduser('~/.spk_quiet'))


def quiet_range():
    """当前夜间静音时段 (起, 止)；已被主人关掉 ⇒ (None, None)。

    ★★ 任何解析失败一律回默认 21/7 —— 这是 fail-closed 的方向：
      **配置写坏时"多静音一会儿"是对的，"从此不静音"是反的。**
    ★ `起 == 止` 是【零长度时段】，也回默认：它作为"永不静音"的隐式写法
      太容易被一个笔误撞上（把 7 打成 21 就得到它），要关就必须明写 `off`。
    """
    try:
        with open(QUIET_FILE) as f:
            parts = f.read().split()
    except OSError:
        return NIGHT_FROM, NIGHT_TO
    # ★ `off` 必须【独自一行】。`off 7` 这种畸形写法不能当成"关闭静音" ——
    #   "关闭"是危险方向（从此夜里会开口），畸形配置要往"照常静音"那边倒。
    if parts == ['off']:
        return None, None
    # ★ 必须【恰好两个】token。`2 1 .5` / `21 7 9` 都是畸形的；
    #   只取前两个会把一个写坏的配置当成有效值用下去。
    if len(parts) != 2:
        return NIGHT_FROM, NIGHT_TO
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return NIGHT_FROM, NIGHT_TO
    if not (0 <= a <= 23 and 0 <= b <= 23) or a == b:
        return NIGHT_FROM, NIGHT_TO
    return a, b


def quiet_text():
    """当前时段，给人/模型看的一句话。"""
    a, b = quiet_range()
    return '已关闭（不静音）' if a is None else '%d:00–%d:00' % (a, b)


# ---- 下面是把时段【改掉】的那一半（2026-09-22 加，给 set_quiet 那三个工具用）----
DEV_QUIET = DEVICE_DIR + '/quiet.conf'      # 设备侧那份副本（只读）
# ★ 设备侧的到期阀有【两份】，跟 nightmute.sh 的 _ov() 一一对应：
#   /tmp 那份扛不住重启，UDISK 那份扛得住 ⇒ 两个都写，跟 nightmute.sh 的姿态一致。
DEV_VALVES = (DEVICE_TMP + '/nightmute.off', DEVICE_DIR + '/nightmute.off')
BLIP_SECS = 40          # 破例出声的窗口：够说一句确认话，之后设备 ≤20 秒自己收回


# ★ 给设备【拉】的那一份。8899 那个静态服务器的根就是 /tmp（见 spk_ctl.py 的注释），
#   所以往 /tmp/spk_quiet 写一个文件，设备 GET `本机:8899/spk_quiet` 就拿到了 ——
#   不需要在 spk_netd 上另开一条 HTTP 路由，跟 nightmute 取 spk_ringing 是同一个套路。
#
#   为什么非要有它：`set_quiet` 是【推】的，可推的那一刻设备完全可能不在线
#   （主人半夜改了时段、音箱正好断过网）。那时工具会如实回一句
#   "没送到设备，它上线后会自己拉取补上" —— 这一份就是那句承诺的兑现物。
#   ★ 权威永远是 QUIET_FILE，这一份是【只读副本】，方向跟设备侧那份一致。
STAGE_FILE = os.environ.get('SPK_QUIET_STAGE', '/tmp/spk_quiet')


def _write_quiet(text):
    """写权威文件（原子写，别让读者看到半截），顺带更新给设备拉的那一份。"""
    tmp = QUIET_FILE + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, QUIET_FILE)
    # ★ 这一份写不成【不影响本机生效】—— 本机读的永远是 QUIET_FILE。
    #   所以吞掉异常：为了给设备留个副本，把主人这次改设置整个搞失败，不值当。
    try:
        tmp2 = STAGE_FILE + '.tmp'
        with open(tmp2, 'w') as f:
            f.write(text)
        os.replace(tmp2, STAGE_FILE)
    except OSError:
        pass


def _push(cmd, wait=6.0):
    """经 spk_netd 推一条命令给设备，等它的回音 ⇒ (ok, 说明)。

    ★ 设备侧是 `[ -z "$C" ] || sh -c "$C"` ⇒ 推过去的一定是**要执行的命令**，
      不能是"说明文字"。回音走 /ret，所以这里的 ok 是真的执行过了。
    ★ 设备不在线 ⇒ (False, …) —— **绝不谎报成功**。本机侧已经生效了，
      设备侧等它上线由 spk_net.sh 拉取补上（见 spk_net.sh 的 /spk_quiet 那条）。
    """
    try:
        import spk_netd                        # 延迟 import：这条链不许拖挂任何东西
    except Exception as ex:                    # noqa: BLE001
        return False, '命令通道不可用（%s）' % type(ex).__name__
    out = spk_netd.send(cmd, wait=wait)
    if out is None:
        return False, '设备 %g 秒内没回话 —— 它多半不在线' % wait
    return True, out.strip()[:200]


def _kv_pair(a, b):
    """校验一对小时数 ⇒ (a, b) 或抛 ValueError。"""
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        raise ValueError('起止都得是整点的小时数')
    if not (0 <= a <= 23 and 0 <= b <= 23):
        raise ValueError('小时数要在 0 到 23 之间')
    if a == b:
        raise ValueError('起点和终点不能是同一个点 —— 那样等于没有静音时段')
    return a, b


def set_quiet_range(a, b):
    """设置时段。⇒ (ok, 给模型看的结果)。"""
    a, b = _kv_pair(a, b)
    _write_quiet('%d %d\n' % (a, b))
    ok, msg = _push('printf %%s\\n %d %d > %s' % (a, b, DEV_QUIET))
    dev = '设备已跟上' if ok else '★ 设备没回音（%s）—— 它上线后会自己拉取补上' % msg
    return ok, '夜间静音改成 %d:00–%d:00 了；%s。' % (a, b, dev)


def set_quiet_off():
    """永久关掉夜间静音。⇒ (ok, 给模型看的结果)。"""
    _write_quiet('off\n')
    ok, msg = _push('printf %%s\\n off > %s' % DEV_QUIET)
    dev = '设备已跟上' if ok else '★ 设备没回音（%s）—— 它上线后会自己拉取补上' % msg
    return ok, '夜间静音关掉了，以后夜里都出声；%s。' % dev


def release_until(ts):
    """放行到 ts（unix 秒）—— 两侧都写到期阀。

    ★ 「今晚放行」和「夜里改完破例回一句」是**同一件事**，只是 ts 不同：
      今晚放行 = 明天早上那个点；破例 = 40 秒后。不需要两套机制。
    """
    ts = int(ts)
    try:
        with open(NIGHT_VALVE, 'w') as f:
            f.write(str(ts))
    except OSError as ex:
        return False, '本机阀写不进去（%s）' % ex
    # ★★ 命令里【同时】写阀和直接开开关，两条腿：
    #   ① 阀让 mode_now() 立刻返回 day ⇒ 主循环下一次（≤20 秒）自己 go_day
    #   ② 直接 cset on 是"抢跑" —— 不等那 20 秒，当场有声
    #   ② 有可能被 guard_night 扳回去（它只在 mode_now()==STATE==night 时跑，
    #     而写阀之后 mode_now() 已经是 day 了 ⇒ 实际只在"恰好在那一瞬间"才会撞上），
    #     就算撞上，① 也会在下一轮把它开回来 ⇒ 最坏多等 20 秒。
    #   ★ 顺序要紧：先写阀再开开关 —— 反过来的话 guard_night 正好在中间跑就白开了。
    sw = '; '.join("amixer -c 0 cset name='%s' on" % n
                   for n in ('Headphone Switch', 'Phoneout Switch'))
    cmd = '; '.join(['echo %d > %s' % (ts, p) for p in DEV_VALVES] + [sw])
    ok, msg = _push(cmd)
    return ok, ('设备已跟上' if ok else '★ 设备没回音（%s）' % msg)


def tonight_until():
    """「今晚」的截止时刻：下一个 7:00（不跨过它自己）。

    ★ 用【明天的那个 7 点】：主人说"今晚别静音"时如果已经是凌晨 3 点，
      那"今晚"指的仍是【今天早上 7 点】—— 所以取"下一个 7 点"而不是"明天 7 点"。
    """
    lt = time.localtime()
    end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1))
    if end <= time.time():
        end += 86400
    return end


def night_blip():
    """夜里改完设置，破例让音箱能回一句话。

    ★ 只给 BLIP_SECS 秒，到期自动收回（设备侧主循环 ≤20 秒内自己把开关扳回去）
      —— 「放行只该有到期这一种死法」这条规矩在这里同样适用。
    """
    return release_until(time.time() + BLIP_SECS)

# ★ 临时放行的【截止时刻】(unix 秒)：**到点自动失效，不需要任何定时任务**。
#   起因：2026-09-21 深夜的放行是一个「不设截止」的 drop-in —— 它自己的注释就写着
#   "不设截止"，还提到一个从没建过的 restore.timer ⇒ 没人记得删就永远放着，
#   主人第二天问"还原了吗"才发现它还在。**放行只该有到期这一种死法。**
#   两条来源，取较晚的那个（不是两份真话，是"谁晚听谁的"）：
#     · 环境变量 SPK_NIGHT_UNTIL —— 单个服务专用。测试时一行就能验：
#         SPK_NIGHT_UNTIL=$(( $(date +%s) - 1 )) python …  ⇒ 必须落回静音
#     · 阀文件 /tmp/spk_night_until —— **所有进程共享，且改它不用重启任何服务**
#       （in_night() 是每次现算的，不是模块顶层算一次）。跟设备侧 nightmute 的
#       "带截止时刻的阀"同构：**过期即失效、fail-closed**。
#   两条都没有（或都过期）⇒ 老老实实按 21:00–07:00 静音。设 0 ⇒ 逐字节等于从前。
NIGHT_UNTIL = float(os.environ.get('SPK_NIGHT_UNTIL', '0') or 0)
NIGHT_VALVE = os.environ.get('SPK_NIGHT_VALVE', '/tmp/spk_night_until')


def night_until():
    """放行到什么时候（unix 秒）；0 = 没放行。

    ★ 任何异常（文件不在、读不动、内容是垃圾）一律返回 0 ⇒ **fail-closed**：
      拿不准的时候是"该静音就静音"，绝不会因为读不到阀而把夜里放开。
    """
    try:
        with open(NIGHT_VALVE) as f:
            v = float(f.read().strip() or 0)
    except Exception:                                       # noqa: BLE001
        v = 0.0
    return max(v, NIGHT_UNTIL)


def in_night(now=None):
    """现在是不是夜间禁声时段。跨零点，所以是"或"不是"与"。

    ★ 2026-09-22 起时段是【每次现算】的（读 ~/.spk_quiet）⇒ 改时段不用重启任何服务，
      跟上面那道放行阀同一个道理。文件很小、有页缓存，热路径上的开销可忽略。
    """
    if night_until() > time.time():
        return False                    # ★ 在放行窗口内（到点自动落回下面的正常判断）
    a, b = quiet_range()
    if a is None:
        return False                    # 主人把夜间静音关了
    h = (now or time.localtime()).tm_hour
    # ★★ 必须分两种。原来只有 `h >= a or h < b` 一行 —— 那对【跨零点】的时段
    #   （21→7）是对的，但对【不跨零点】的时段会把 [0, a) 也算进去：
    #   设成「1 点到 6 点」，0 点会被误判成夜间。
    #   写死 21/7 的年代永远跨零点 ⇒ 这个 bug 一直没露头，
    #   一旦允许主人设任意时段，它当场就活了（离线测试抓到的就是这条）。
    if a < b:
        return a <= h < b               # 不跨零点：只有中间那一段
    return h >= a or h < b              # 跨零点（a > b；a == b 已被 quiet_range 挡掉）


# ---------------------------------------------------------------- 打断（中断出口）
# ★ 为什么需要：say() 是阻塞的，最长能耗 25 秒（推流 + 自听判定）。这 25 秒里主循环
#   是【聋的】—— 而 spk_barge 的观察者同时在听。它一旦判出主人在插话，就必須有人能让
#   say() 立刻收手：**既停下喇叭里的声音，也别把"没念完"当成失败。**
#   这个 Event 就是那个出口（观察者在它自己的线程里 set 它，say() 在等待循环里看见就走）。
#
# ★ 为什么是三态 True / False / 'cut'，而不是两个：
#   'cut' 和 False 是【两件事】，混在一起会污染自听判据 ——
#     False = "我该出声却没出成" ⇒ 要留证 unheard-*.wav（喇叭哑了/没连上）；
#     'cut' = "我正说着，主人让我停" ⇒ 成功让路，一个字都不该留证。
#   混在一起，被打断的那些会被记成"喇叭哑了"，自听那套判据就开始说谎了。
_ABORT = threading.Event()


def set_abort(flag=True):
    """让当前正在播的那一条立刻收手。给 spk_barge 的 on_fire 调（任意线程）。"""
    if flag:
        _ABORT.set()
    else:
        _ABORT.clear()


def abort_on():
    return _ABORT.is_set()


def _stop_playback(base, udn):
    """喊音箱立刻闭嘴。

    ★ 光让 say() 返回是不够的 —— 声音还在它喇叭里放着。say() 只是"我们不管了"，
      这里才是"让它停"。两件事都要做。
    ★ DLNA 那条路用已经发现的 base/udn，不再 discover 一次：打断是要快的事，
      SSDP 那几百毫秒在这里等不起（声音还响着）。
    ★★ 601 那条路【不需要 base/udn】—— 它根本不发现，直接敲设备（见 _stop601）。
    """
    if PLAY_VIA == '601':
        return _stop601()
    try:
        soap(base, udn, "AVTransport", AV, "Stop", "<InstanceID>0</InstanceID>", timeout=3)
        return True
    except Exception as e:                              # noqa: BLE001
        print('   （喊停没喊成：%s: %s）' % (type(e).__name__, e))
        return False


# ================================================================ 播放出口：两条路
# ★★★ 2026-09-22：说话的路改成【0x601 直连】，DLNA 降级成回退。
#
#   为什么换（当天实测，不是偏好）：
#     ① DLNA 那条路依赖 SSDP 发现，实测抖得厉害：连做 5 次 0.21 / 0.23 / 1.22 /
#        5.12 / 6.34 秒；而 KPlayer 还会静默卡死（进程活着、日志一行错没有、
#        一个端口都不监听，那天卡了近 3 小时，我们只会一遍遍重试到天亮）。
#     ② 0x601 直接敲设备自己的播放器：命令 → 取流 → 出声实测 185ms，
#        而且【根本不用发现】—— 设备主动来拉我们的命令（见 spk_netd.py）。
#     ③ 0x601 那条路【不碰音量】。DLNA 每次开口前先 SetVolume(100)，
#        而主人立的规矩是"一律用音箱当前的音量"。
#
#   ★ 代价说清楚：设备播放器在 `duration − 15000ms` 处发 Next，总控收到就去云端
#     取歌（域名被劫持、443 拒连）⇒ 重试 ⇒ 发 0x602 暂停 ⇒ **短音频会被掐**。
#     所以走这条路【必须补静音】，见 _stage601。
#
#   ★ 回退是一行环境变量（SPK_PLAY_VIA=dlna），不用改代码、不用重推脚本。
PLAY_VIA = os.environ.get('SPK_PLAY_VIA', '601')        # '601' | 'dlna'
PLAY601 = os.environ.get('SPK_PLAY601', DEVICE_DIR + '/play601.sh')
PAD_GUARD = 15.0        # 设备那条切换线：duration − 15 秒
# ★★★★★ 补到多长 —— 2026-09-22 深夜把这个数从 17 改成 45，理由是一次真机复盘：
#
#   垫时长的**唯一目的**是让"总长 > 15 秒"（不到 15 秒的轨 190 毫秒就被 Next 掐掉）。
#   但超过 15 秒的轨，设备必定在 `总长 − 15 秒` 处发一发 **Next**，而这一发 Next 会
#   拽出总控的一整条链（实测 21:52:27，毫秒不差）：
#       playerId:1 st:0x7(Next)  →  总控 status info err  →  post err,try 1/2
#       →  cmdId:0x602 {"playerId":1}（放弃恢复 ⇒ 强停）  →  wifi_player is interrupted
#   老值 17 秒把这个雷【精准埋在"人声结束后 2 秒"】—— 正是我们下一句要推的时刻：
#       第一轮回答赶在 Next 之前推上去（把那条轨顶掉，Next 随之作废）⇒ 出声了；
#       第二轮晚了一步（思考音 17.47s，雷在 +2.47s 就炸）⇒ 连取流都没取，全哑。
#   ⇒ 结论：**雷炸得越晚越安全**。垫到 45 秒，雷落在"人声结束后 30 秒"：
#     对话还在继续时，我们早就用新轨把它顶掉了（Next 随轨一起消失）；
#     它只在真的静默 30 秒以上之后才炸，而那一刻没有我们的推流可连坐。
#   ★ 补的每多一秒都是**静音**：不占带宽（本地文件）、不影响听感、不影响 `audible_end()`
#     （那儿用的是人声结束的**位置** `end`，不是这个全长）。
PAD_SECS = float(os.environ.get('SPK_PAD_SECS', '45.0'))
PUSH_WAIT = float(os.environ.get('SPK_PUSH_WAIT', '3.0'))
# ★ 设备不来取流时，_say601 最多补发几次 0x601（见那里的一大段：补发是把它从
#   Paused 里拽出来的唯一便宜手段）。0 = 退回旧行为（白等 6.3 秒再报失败）。
PLAY_RESEND = int(os.environ.get('SPK_PLAY_RESEND', '2'))
STAGE_DIR = '/tmp'      # ★ 8899 静态服务器的根就是它（见 spk_ctl.py）

# ★★★★★ 这一句【在人耳里响完】的本机时刻 —— 治"自己的回答被当成主人说话"。
#   见 `audible_end()` 那一大段。两个数各由一处填：
#     `secs` = 这条音频的人声到哪儿为止（`_stage601` 量原始文件时记，含补静音前的长度）
#     `at`   = 设备【真的起播】的本机时刻（`_ensure_playing` 看见 0x3 那一刻反推）
_PLAY = {'at': 0.0, 'secs': 0.0}

# ★★★★★ 2026-09-25 夜：这一句【为什么没念成】的**真原因**，供调用方照实打。
#
#   起因：`spk_ear.py` 里那句 `没念成（夜间禁声 / 音箱没来取文件）` 是**猜的**，
#   而且猜错了 —— 当晚 21:39:26 的真实情况是：设备 21:39:20 明明来取了文件
#   （`★ 音箱在取了`）、当时也不是夜间，真凶是**设备自带的网易引擎插话**
#   （`0x701` 播 `/rom/…/voice/*.mp3` 和云 TTS「没有音乐可以推送」），
#   总控的 `ContinuePlay` 把 playerId 1 按住 ⇒ 我们补发两次仍停在 `0x4`。
#   真因本来就打在上一行（`✗ 补发 2 次后仍停在 0x4`），却被那个括号盖过去。
#   ⇒ 规矩：**判据说不出来就别装知道**。出口处把真因记在这儿，调用方逐字打。
LAST_ERR = ''


def _stage601(mp3_path):
    """给 601 那条路准备一份【能播完】的音频。回 (路径, 说明)。

    ★ 非补不可，这是设备固件的行为不是我们的选择：`threadGetPos` 里
      `duration > 15000 && (duration − curPos) > 15000` 才跳过 Next ——
      **总长不到 15 秒的轨会立刻触发 Next**（实测 190ms 发 Next、448ms 被 Paused）。
      而我们的回答通常只有 3~10 秒、让路那句「嗯，你说」只有 1.37 秒
      ⇒ 不补就等于每句话只念开头那两秒。
    ★ JSON 里那两个看着该管这事的键（`duration` / `ignorePause`）**实测都无效**：
      声明 duration=600000 时播放器照样报 6048ms；ignorePause=1 照样 190ms 发 Next。
      **把音频补长是唯一杠杆。**
    ★ 补到【人声结束的位置 + `PAD_SECS`】：老值 17 秒把设备的 Next 精准埋在人声结束后
      2 秒 —— 那正是我们下一句要推的时刻，一撞就整条链连坐（0x602 强停当前播放器）。
      现在 45 秒 ⇒ 雷落在人声之后 30 秒，对话还在继续时新轨早把它顶掉了。详见 `PAD_SECS`。
      ★★ 判据和补的基准都是 `end`（人声结束的**位置**），不是 `speak`（人声**总长**）
        —— 这两个数在"中间有停顿"或"前后都有静音"时差得很远，理由见 mp3_span()。
    ★ 一律落到 /tmp：8899 的根就是它，而让路那句在 audio/ 下（不在 /tmp），
      不搬过去设备根本取不到文件。
    ★ 够长的原样播，一个字节都不动。
    """
    total, speak, end = mp3_span(mp3_path)
    # ★★★ 记账：这一句的**人声到哪儿为止** —— `audible_end()` 要用它算"真响完"的时刻。
    #   记在《量原始文件》这一步，所以缓存命中、"本来就有"那两条早退路也都记上了。
    _PLAY['secs'] = end
    if os.path.dirname(os.path.abspath(mp3_path)) == STAGE_DIR and end + PAD_SECS <= total:
        return mp3_path, '本来就有 %.1fs（人声到 %.1fs），不用补' % (total, end)
    out = os.path.join(STAGE_DIR, 'spk601_p%d_%s' % (int(PAD_SECS), os.path.basename(mp3_path)))
    # ★★★★★ 文件名里必须带垫的秒数（2026-09-22 深夜的教训）：缓存只按【源文件 mtime】
    #   判新旧，于是把 PAD_SECS 从 17 改成 45 之后，思考音照旧命中【当年 17 秒垫出来的】
    #   那份缓存 —— 雷照旧埋在 2.5 秒处（设备日志 21:52:26 `st:0x7` → `0x4 Paused
    #   cur:2781ms duration:17472ms` ⇒ 总控 0x602 ⇒ 回答三条 0x601 一条都没人取）。
    #   把秒数写进名字，"改了垫时长"这件事自己就让缓存作废，不靠人去记得清 /tmp。
    try:
        # ★ 缓存：源文件没动过就直接用（让路那句是固定片段，只该转一次）。
        #   判据是 mtime —— 嗓子的增益一变，yield_clip() 会重建源文件，
        #   mtime 跟着变，缓存自然作废。
        if os.path.exists(out) and os.path.getsize(out) > 0 \
                and os.path.getmtime(out) >= os.path.getmtime(mp3_path):
            return out, '用缓存 %s' % os.path.basename(out)
    except OSError:
        pass
    if not shutil.which('ffmpeg'):
        shutil.copy(mp3_path, out)
        return out, '✗ 本机没有 ffmpeg，原样搬过来（%.1fs 短于 15 秒，会被掐）' % total
    pad = max(0.5, end + PAD_SECS - total)
    p = subprocess.run(['ffmpeg', '-y', '-i', mp3_path, '-af',
                        'apad=pad_dur=%.3f' % pad, '-b:a', '128k', out],
                       capture_output=True)
    if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        shutil.copy(mp3_path, out)
        return out, '✗ 补静音没成（%s），原样搬过来（会被掐）' % p.stderr.decode()[-70:]
    return out, '补静音：人声到 %.1fs / 全长 %.1f→%.1fs' % (end, total, total + pad)


# ★★★★★ 说话用哪个 playerId —— 2026-09-22 深夜的哑巴真凶，**绝不能写死 1**。
#
#   设备总控（netease_control_center）心里记着一个 `curPlayer`：**只有它认为是"当前"
#   的那个播放器才许响**，别的播放器一出声，它 11 毫秒内就把它按成 Paused：
#       [W] Here maybe some issue, Player(1) not the curPlayer(2)
#           or player been interruppted (true) but in playing!#PV1-521
#       [D] cmdId:0x602 len:42 {"playerId":1,...}
#   实测（21:40:46 / 21:41:17 / 21:41:25 / 21:41:27 …）：我们每推一句，日志就是
#       playerId:1 st:0x1（Preparing）→ **11 毫秒后** st:0x4（Paused），`duration:1ms`
#   ——文件根本没读完。主人的感受就是"**它不回复了**"（21:42 原话），而每一环都"成功"。
#
#   为什么 `curPlayer` 会变成 2：**放过音乐**（主人 21:39:56 要了轻音乐，
#   那条路走 DLNA→总控，总控就把 curPlayer 设成 2 了）。它**不会自己变回来** ——
#   音乐播完/停了也一直挂在 2 上 ⇒ **从那以后我们一句话都说不出来**。
#   ★ `device/play601.sh` 文件头早就写着这条规矩（"playerId 必须等于总控的 curPlayer"），
#     只是我们把它写死成 1 了 —— 那时候 curPlayer 正好是 1，看不出问题。
#
#   ⇒ 修法：**每次推之前问一句"现在该用哪个"**，答案就在总控自己的日志里
#     （它掐我们之前总要把理由写下来）。真机静音轨实测（2026-09-22 21:42）：
#         pid=1 → st:0x1 → st:0x4（被掐）      pid=2 → st:0x2 → **st:0x3（真在播）**
#   ★ 代价说清楚：音乐在播时我们把话说到 pid 2 上 ⇒ **音乐会那条轨被顶掉**。
#     这是"抢同一个播放器"的必然结果，比"一句话都说不出来"好得多。
_PID_TTL = float(os.environ.get('SPK_PID_TTL', '30.0'))
# ★★★★★ 2026-09-22 深夜：**"问空了"也要冷冻** —— 这是"说完要等 8~10 秒才出思考音"
#   的真凶（主人原话）。探针是拿 `grep` 在总控日志里找那两行，**通常一行都没有**
#   （那两行只在出过事的时候才写），于是设备照样执行、只是没输出 —— 而旧版
#   `spk_netd.send` 把"空输出"当成"没回话"，**白等满 `wait`**（实测 8.04 秒/次 ×4）。
#   更毒的是这里：`pid` 拿不到 ⇒ 不写 `at` ⇒ **下一句又问、又白等**，一直问到
#   总控日志里恰好出现那两行为止。主人感受到的就是"有时候挺快、有时候足足十秒"。
#   ⇒ 两道修：① `spk_netd.send` 认 mtime（空回复当场回）；② 这里把"问空了"记下来
#     冷冻 `_PID_MISS_TTL` 秒，期间直接用缓存值（本来就只能这么办，白等没意义）。
#   ★ 为什么敢冷冻：真问错了有兜底 —— `_ensure_playing` 推不动时会 `force=True`
#     重问（那条路不看冷冻），缓存也会当场作废。
_PID_MISS_TTL = float(os.environ.get('SPK_PID_MISS_TTL', '20.0'))
# ★ 探针的等待：**8.0 → 2.0**。设备侧实测回一条命令 0.10~0.20 秒（`echo`/`date` 各一次），
#   8 秒是照"设备可能不在线"留的，对"设备在线但没输出"就是纯白等（见上）。
#   真不在线时也有 2 秒兜底 —— 而这条路本来就是"问不到就按缓存走"，不是关键路径。
_PID_PROBE_WAIT = float(os.environ.get('SPK_PID_PROBE_WAIT', '2.0'))
_pid_cache = {'v': 1, 'at': 0.0, 'miss': 0.0}
_CUR_PLAYER_PROBE = (
    'C=$(ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1); '
    'grep -a "not the curPlayer" "$C" | tail -1; '
    'grep -a "cmdId:0x601" "$C" | tail -1')


def _cur_player(force=False):
    """总控认为"当前该响的是哪个播放器" —— 我们说话必须用它。读不出来回 1。

    ★ 两条线索，**`curPlayer` 那条优先**（它说的就是"此刻"）；退而取总控自己最后
      发过的 0x601 的 `playerId`（那是把 curPlayer 设成某个值的那个动作）。
    ★ 结果缓存 `_PID_TTL` 秒：这是**每句话都要跑**的一条路，不能每次都多花一次往返
      （0.2 秒）。缓存失效有两条路：到期，或者**推失败了**由 `_ensure_playing` 作废
      （见那里 —— 判据错了要当场认，不能抱着旧答案等 30 秒）。
    ★ 读不出来【回缓存值（初值 1）】，绝不让"读不到"变成"不说话"。
      ★★ 2026-09-22 深夜改：**"读不到"现在也会冷冻 `_PID_MISS_TTL` 秒**（老行为是
        "不缓存失败"）—— 因为"读不到"的常见原因不是设备离线，而是**总控日志里
        恰好没有那两行**，那是个会持续好几分钟的状态；不冷冻就是每句话白等一遍
        探针（8 秒 → 现在 2 秒），主人听到的是"有时候要等十秒才出思考音"。
    """
    now = time.time()
    if not force and _pid_cache['at'] and now - _pid_cache['at'] < _PID_TTL:
        return _pid_cache['v']
    if not force and _pid_cache['miss'] and now - _pid_cache['miss'] < _PID_MISS_TTL:
        return _pid_cache['v']          # ★ 刚问空过 ⇒ 别再白等一遍（见 `_PID_MISS_TTL`）
    pid = 0
    try:
        import spk_netd
        out = spk_netd.send(_CUR_PLAYER_PROBE, wait=_PID_PROBE_WAIT) or ''
        m = re.search(r'curPlayer\((\d+)\)', out)
        if not m:
            m = re.search(r'"playerId"\s*:\s*(\d+)', out)
        if m:
            pid = int(m.group(1))
    except Exception as e:                              # noqa: BLE001
        print('   （问不到该用哪个播放器：%s: %s ⇒ 按 1 试）' % (type(e).__name__, e))
    if pid > 0:
        if pid != _pid_cache['v'] or not _pid_cache['at']:
            print('   ♻ 说话改走 playerId=%d（总控的 curPlayer）' % pid)
        _pid_cache['v'] = pid
        _pid_cache['at'] = now
        _pid_cache['miss'] = 0.0
        return pid
    _pid_cache['miss'] = now            # ★ 问空了：冷冻一会儿，别每句话都白等
    return _pid_cache['v']


def _play601(url, wait=None, pid=None):
    """0x601 播一条 URL（走 spk_netd → 设备上的 play601.sh），`pid` 默认问当前该用哪个。"""
    p = _cur_player() if pid is None else int(pid)
    return _push('%s play %s %d' % (PLAY601, url, p),
                 wait=PUSH_WAIT if wait is None else wait)


def _stop601(pid=None):
    """喊音箱立刻闭嘴（0x603）。

    ★ 用 post() 不用 send()：打断时喇叭【还响着】，而 send() 要一直等到设备把
      输出送回来。post() 投完就走，命令由设备那条常驻的长轮询在几十毫秒内取走。
    ★ 不等回音就放弃了"上报失败"—— 但这是安全的：槽位是【先进先出的一条】，
      后投的 play 永远排在这条 stop 后面，不会出现"晚到的 stop 掐掉新回答"。
      真正的兜底是取走时的 TTL（120 秒），早就没人等的命令不会在两小时后凭空执行。
    """
    try:
        import spk_netd                       # 延迟 import：这条链不许拖挂任何东西
        if pid is None:
            pid = _cur_player()               # ★ 停也要停对播放器，见 `_cur_player()`
        spk_netd.post('%s stop %d' % (PLAY601, int(pid)))
        return True
    except Exception as e:                              # noqa: BLE001
        print('   （喊停没喊成：%s: %s）' % (type(e).__name__, e))
        return False


# ★ 补发上限：CC 的 ContinuePlay 是【每轮 TTS 一次性】的，补发正好落在它之后，
#   实测一发就够；留 2 发是给"连着两轮 TTS"（唤醒应答 + 它自己的 TTS）那种情况。
PLAY_RETRY = int(os.environ.get('SPK_PLAY_RETRY', '2'))
# 明确表示"没在播"的状态（ihwplayer 的 music_st：0:Idle 1:Preparing 4:Paused）
DEAD_ST = ('0x0', '0x1', '0x4')
_PLAYER1 = 'playerId:%d st:0x'
# ★★★★★ CC 的静音态 —— 主人那句"回答是从一半开始播的"的真凶（2026-09-22 夜结案）。
#
#   【症状与证据】设备日志（自带毫秒时间戳）：
#     20:13:04.942  我们推 0x601
#     20:13:05.460  CC: `Set mute true`    ← 它的唤醒应答 TTS 恰好在这一刻完成
#     20:13:05.633  playerId:1 st:0x3      ← 我们在【静音里】开始播，cur:1ms 从头走的
#     20:13:07.123  CC: `Set mute false`
#   而这条 mp3 的人声在 0.20~2.17 秒（`ffmpeg silencedetect` 量的）⇒ 07.123 松开时
#   已经放到 **1.49 秒** ⇒ **开头 1.29 秒的话被它吃掉了**（"现在晚上八点"没了，
#   他听到的就是"从一半开始播"）。
#
#   【它写的不是 ALSA】同一时刻 `DAC volume=[150,150]`、所有输出开关都是 `on`
#   （20:20 静音着的时候现场量的），而 dacguard（每 5 秒守一次 numid 19）那一分钟
#   里一个脚印都没有。二进制里跟静音有关的串只有 `SetMute`(3) / `SmartAudio`(6) /
#   `/dev/ttyS2`(1) ⇒ **它走 MCU 串口或 SmartAudio 服务**。那条串口是【禁写】的
#   （同一颗 MCU 还管音量 ADC 和触摸键，见 [[netease-vbox-led-matrix]]）——
#   所以修法只能走**时序**，绝不去强拆静音。
#
#   【它的语义】拿一条 19 秒【全静音】的 mp3 走同一条路实测出来的（全程没出声）：
#     `true`  = "我手里没东西在播"（空闲态 —— 实测能连挂 7 分钟）
#     `false` = "有轨在播"，而**一条新推的轨会让它 0.13 秒内就松开**
#               （CC 静音 7 分钟后我们推流，`Set mute false` 落在推流那一刻 +0.126 秒）
#   ⇒ 所以修法就是：**它在我们播着的时候【刚】静音着 ⇒ 当场补发 0x601**。
#     补发是一条新轨，它自己就会松开静音 ⇒ 后面那一整句都听得见。
#   ★★ 补发【不可能】造成"重复听一半"：CC 说静音，就说明前面那几秒本来就没出声。
#      这也正是"补发"和"掐掉重来"在这条路上的区别 —— 前者没有可听的损失。
#   ★★★ 当晚主人反馈「反应有点慢」之后又补了两条（两条都是"判据"的坑，不是新机制）：
#     ① **必须判新鲜**（`_fresh`）：`tail -1` 拿到的"最后一条"永远有值，而它可能是
#        几分钟前空闲态留下的 `true` ⇒ 被当成"此刻正静音" ⇒ 白等 3 秒。
#        实测 20:32:27 推流、20:32:29 报静音、等到 20:32:31 才补发 —— 那 2 秒纯白等。
#     ② **不当场等，直接补发**：把因果搞反了 —— 让它松开的动作就是补发本身
#        （0.126 秒），干等什么都不发生。现在只留一条规则：
#        **刚静音 ∧ 还在开头（< `HEAD_MS`）⇒ 补发；已经播进去了 ⇒ 不动它**。
_MUTE_MARK = '===CC==='
_CC_LOG = ('C=$(ls -t /tmp/netease_control_center_*.log 2>/dev/null | head -1); '
           'tail -c 200000 "$C" 2>/dev/null | grep -a "Set mute" | tail -1; '
           'N=$(date +%s); echo "FRESH=$(date +%H:%M:%S),'
           '$(date -d @$((N-1)) +%H:%M:%S),$(date -d @$((N-2)) +%H:%M:%S)"')
# ★ 探针命令在设备上跑：取最新的 ihwplayer 日志看 playerId:1 最后一次状态变更，
#   顺带取 CC 日志里最后一条 `Set mute`，**外加设备此刻的墙上时间**（判新鲜用）。
#   **三条线索合成一次往返**（各读各的 tail，成本一样），别拆成多次 ——
#   每次往返 0.2 秒，而这是每轮回答都要跑的。
#   ★ `tail -c 200000` 是有意的：CC 日志被那个 `buscmd 61696` 节假日重试刷得
#     每秒一行，200KB 大约覆盖 3 分钟。太久远的那条本来也不该拿来判"此刻"。
#   ★★ 2026-09-22 深夜加的 `FRESH=`：**没有它的那一版会读出一条 5 分钟前的
#     `Set mute true` 并当成"此刻正静音"**，于是白等 3 秒才补发（20:32:27 推流、
#     20:32:29 报静音、等到 20:32:31 才补发 —— 主人当场的感觉就是"反应慢"）。
#     判据读到的必须是【刚刚写的】那一行，见 `_fresh()`。
_ST_PROBE = ('L=$(ls -t /tmp/ihwplayer_*.log 2>/dev/null | head -1); '
             'grep -a "playerCallbackHandler(755).*playerId:@PID@ st:0x" "$L" | tail -1; '
             'echo "%s"; %s' % (_MUTE_MARK, _CC_LOG))
# ★★ 播放器号用【字符串替换】填，不用 `%` —— 这段命令里 `_CC_LOG` 自带
#   `date +%H:%M:%S` 那三个 `%`，一旦拿它去跑 `%` 格式化就会炸成
#   "not enough arguments for format string"，而 `_play_state` 的兜底是
#   "读不到就放行" ⇒ **探针恒瞎、判据恒真、哑了也看不出来**（2026-09-22 深夜真踩到）。
_ST_PROBE_PID_MARK = '@PID@'
# ★ 判"这条日志是刚写的吗"的窗口（秒）。**只认 3 秒内**：CC 的静音是一条
#   **状态变更**日志，不是心跳 —— 它不写新行就说明状态没变。所以"最后一行"
#   可能是几分钟前的，而那不代表此刻（见 `_fresh`）。
MUTE_FRESH = float(os.environ.get('SPK_MUTE_FRESH', '3.0'))
# ★ "开头"的界限：播放位置还在这之内才允许补发。
#   过了这条线的处理是【不动它】—— 那时候他开头已经听见了，补发只会让他
#   听见"这句重来一遍"，比丢尾巴更烦人（丢尾巴他不知道，重来一定知道）。
HEAD_MS = int(os.environ.get('SPK_HEAD_MS', '1000'))
_TS_RE = re.compile(r'\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.')
_FRESH_MARK = 'FRESH='


def _fresh(line, out):
    """这条日志是不是**刚刚**（`MUTE_FRESH` 秒内）写的？

    ★★★ 这是这一版修的核心坑：`tail -1` 拿到的"最后一条"**永远有值**，而它可能
      是几分钟前的。老版直接读它的值 ⇒ 一条陈旧的 `Set mute true`（空闲态、
      上一次播完留下的）会被当成"此刻正静音" ⇒ 白等 3 秒（实测 20:32 那一轮）。
    ★ 设备上 `date -d @epoch` 可用（busybox 支持），所以新鲜用【设备自己的钟】
      判 —— 不依赖本机与设备的时钟同步，只比字符串（`HH:MM:SS` 是否落在
      刚刚那几秒里），连跨分钟/跨小时都不用管。
    """
    m = _TS_RE.search(line or '')
    if not m:
        return False
    secs = set()
    if _FRESH_MARK in out:
        head = out.split(_FRESH_MARK, 1)[1].splitlines()[0]
        secs = set(x.strip()[-8:] for x in head.split(',') if len(x.strip()) >= 8)
    return m.group(1)[-8:] in secs


def _play_and_mute():
    """一次往返读三件事：playerId:1 的 music_st、播到第几毫秒、CC 此刻是不是静音。

    回 (状态如 '0x3' / None, 静音 True/False/None, 播到的毫秒 int/None, 原始输出)。
    ★ 读不到一律回 None，由调用方"不判，放行" —— 绝不因为判不了就把话说成失败。
    ★★ 陈旧的一律当【读不到】（`_fresh`）：状态、位置、静音三者同一个口径 ——
      "最后一行的值"不等于"此刻的值"，这一条在这台设备上咬过一次（白等 3 秒）。

    ★ 为什么要绕到设备上去读：**取到流 ≠ 播出来**。判据必须是"被控方"自己报的
      状态（spk_ctl 的那条教训），既不是发送方的返回码，也不能用自听 ——
      这条路设备 AEC 拿我们自己正在放的声音当参考消掉了，耳朵听不到自己
      （见 _wait_and_judge 末尾那条禁令）。
    """
    try:
        import spk_netd
        pid = _cur_player()
        # ★★ 读的是【我们真的推上去的那个播放器】的日志 —— 以前写死 playerId:1，
        #   2026-09-22 深夜改成跟着 `_cur_player()` 走：现在说话可能落在 pid 2 上
        #   （主人放过音乐之后就是这样），读 1 的话探针恒回 None，而"读不到就放行"
        #   会把"其实被掐了"静静地吞掉（这正是当晚哑了十几分钟没被发现的形状）。
        out = spk_netd.send(_ST_PROBE.replace(_ST_PROBE_PID_MARK, str(pid)), wait=8.0) or ''
    except Exception as e:                              # noqa: BLE001
        return None, None, None, '%s: %s' % (type(e).__name__, e)
    # ★ 播放器那一行在标记【之前】（`echo` 是后打的）。
    before = out.split(_MUTE_MARK, 1)[0] if _MUTE_MARK in out else out
    pl = [ln for ln in before.strip().splitlines() if ln.strip()]
    pline = pl[-1] if pl else ''
    st, cur = None, None
    m = re.search(r'%s([0-9a-f]+)' % (_PLAYER1 % pid), pline)
    # ★ 捕获组【只收十六进制数字】，`0x` 由 _PLAYER1 自带 —— 两边都写 `0x`
    #   会得到 `playerId:1 st:0x0x4` 这种永远匹配不上的模式，探针恒回 None，
    #   而这会被"读不到就放行"默默吞掉（自测时真踩过一次）。
    if m and _fresh(pline, out):
        st = '0x' + m.group(1)
        mc = re.search(r'cur:(\d+)ms', pline)
        cur = int(mc.group(1)) if mc else None
    muted = None
    if _MUTE_MARK in out:
        # ★★★ 取【第一条非空行】，不是 `splitlines()[0]` —— `echo "===CC==="` 打出来
        #   的标记后面紧跟一个换行，`[0]` 会取到空串，判据当场恒为 False，
        #   而这会以"CC 从没静音过"的样子静静地什么都不做（自测时真踩到了）。
        lines = [ln for ln in out.split(_MUTE_MARK, 1)[1].strip().splitlines() if ln.strip()]
        if lines and 'Set mute' in lines[0] and _fresh(lines[0], out):
            muted = 'Set mute true' in lines[0]
    return st, muted, cur, out.strip()[-200:]


def _ensure_playing(url, t_push):
    """确认它【真的在播、而且真的听得见】；否则补发 0x601。回 (bool, 说明, t_push)。

    ★ 它治的是什么（2026-09-22 晚实测，那天"完全没声音"就死在这）：
      控制中心每轮 TTS 播完都跑 ContinuePlay（恢复背景音乐），而它【必须先向云端
      要歌曲信息】：`vbox-server.3.163.com/vbox/music/recommend/song/hwget`。
      那域名被我们劫持到 192.168.1.100，而 443 上没有任何东西在听 ⇒ connection refused
      ⇒ 重试 2 次耗尽 ⇒ 它【放弃恢复】并 `forcePausePlayer` 把 playerId 1 强停、
      CurPos 归零（CC 自己的日志把病因写出来了："暂时没办法为你继续播放了" /
      "获取当前歌曲信息失败"）。我们的回答也挂在 playerId 1 上 ⇒ 被连坐：
      `Preparing` 之后【2 毫秒】就被按成 Paused，**从没到过 Playing(0x3)**。
      对照成功轮是完整走完 `Preparing → Prepared → Playing`。
    ★ 而 8899 的访问日志显示设备【确实取了流】(GET ... 200) ⇒ 老的 fetched_count
      判据报"★ 音箱在取了" = 成功，其实一个字都没播。**取到流 ≠ 播出来。**
    ★ 这是【时序撞车】不是稳定故障 ⇒ 这才是"时好时坏"的真正解释：
      0x601 落进 CC 的 resume 窗口就死，没落进就活。
    ★ 为什么补发有效：CC 的 ContinuePlay 是每轮 TTS【一次性】的，补发落在它之后。
    ★ 只补"死态"（Idle/Preparing/Paused）：过渡态(0x2 Prepared)和 0x7(Next) 不补
      —— 那两种情况下音频多半已经在走，补发会让人听到重复的开头。
    ★ 读不到状态（通道没活/超时）⇒ 【放行】。绝不因为"判不了"就把话说成失败。

    ★★★ 2026-09-22 深夜补的第二道闸：**"在播"不等于"听得见"。** 见 `_MUTE_MARK`
      那一大段 —— CC 会在我们起播之后 0.5 秒甩一条 `Set mute true` 出来（它自己的
      应答 TTS 完成），把我们的开头吃掉。所以判据从"music_st 是 0x3"变成
      "**0x3 而且 CC 没静音**"。
    ★★★ 当晚第二次修正（主人：「反应有点慢」）：静音着的时候**当场补发，不再等**。
      老版是"先等它松开（最多 3 秒）再补发" —— 那是把因果搞反了：**让它松开的
      动作就是补发本身**（空闲态实测 0.126 秒），干等的那几秒什么都不会发生。
      再加上"陈旧日志被当成此刻"那个假阳性（见 `_fresh`），20:32 那一轮白等了 2 秒。
      ⇒ 现在只有一条规则：**判到【刚写的】静音 + 位置还在开头（< `HEAD_MS`）⇒ 当场补发。**
    """
    global LAST_ERR
    # ★★ 起播时刻的默认值 = `t_push`（"就当它推出去就响了"）—— 读不到状态那几条
    #   放行路走的就是它。真正看见 0x3 时下面会用 `cur` 反推一个准的盖上。
    _PLAY['at'] = t_push
    # ★★★★★ 2026-09-22 深夜：静音那条分支的重试预算改成**按时间算（默认 6 秒）**。
    #   实测（21:59:28 那一轮）CC 的 `Set mute true` **只按 1.95 秒就自己松**：
    #       21:59:29.989  Set mute true
    #       21:59:31.941  Set mute false      ← 它自己会松！
    #   而旧版"补发 2 次"在 ~1.5 秒内就撞完了 ⇒ **次次都死在松手之前那零点几秒**，
    #   日志写"补发 2 次都没让它松开，这一句多半没出声"，其实再等 0.5 秒就响了
    #   （设备侧随后真的 `st:0x3 duration:51984ms`）。主人感受到的就是"回答时有时无"。
    #   ★ 为什么会静音：CC 每轮自己要播一条 TTS（`0x701` + `/rom/…/voice/*.mp3`，
    #     接着还有一条云 TTS「没有音乐可以推送」→ 取 `vbox-tts.3.163.com` 失败），
    #     播它自己那条时把 player 按住 ⇒ **静音窗的长度由它那条失败的重试决定**，
    #     我们算不出来，只能等。
    #   ★★★ 2026-09-25 夜【推翻】了上面那条老注释「死态是 ContinuePlay 连坐，补发本身
    #     才是解药，多等没用」—— **"多等"恰恰就是这一夜缺的那味药**。
    #     当晚 21:39 那一轮实测：设备 21:39:20 起了**它自己**的网易引擎提示音
    #     （`0x701`：S003 → b-h-52 → C-yc-1 → 云 TTS「没有音乐可以推送」），
    #     21:39:26 才 `PLAYBACK_COMPLETE`。我们的话在 21:39:21 被判 0x4，
    #     **补发 2 次只花了约 1.5 秒就放弃**，而它的提示音窗口有 **6 秒**。
    #     ⇒ 我们每次补发都正好砸在它自己的提示音上，再被按回去 ⇒ 主人一个字没听见。
    #     判据：`✗ 补发 2 次后仍停在 0x4（没在播）` 之后 spk-ear 打出「没念成」
    #     —— 可文件明明取了（`★ 音箱在取了`）、天也没黑。**不是取不到，是被压着。**
    #   ⇒ 现在死态分支也**按时间算**（`DEAD_WAIT`，默认 8 秒 > 那个 6 秒窗口）：
    #     只要还没等到窗口过去，就一轮一轮地补，而不是数满 `PLAY_RETRY` 次就走。
    MUTE_WAIT = float(os.environ.get('SPK_MUTE_WAIT', '6.0'))
    DEAD_WAIT = float(os.environ.get('SPK_DEAD_WAIT', '8.0'))
    _t0 = time.time()
    _n_resend = 0
    _last_cur = None      # ★ 上一轮读到的播放位置 —— 用来判"它其实放过"（见死态分支）
    # ★ 循环上限只防"判据永远回同一句"的死循环；真正的出口全是上面那些**时间**判据。
    #   静音路每轮 ~0.4s，死态路每轮还要跑一次 `_play601` ⇒ 按最费的那条算才够。
    for i in range(PLAY_RETRY + 2 + int(max(MUTE_WAIT, DEAD_WAIT) / 0.4) + 1):
        _prev_cur = _last_cur          # ★ 先留上一轮的值，再覆盖 —— 顺序反了差就恒为 0
        st, muted, cur, raw = _play_and_mute()
        if cur is not None:
            _last_cur = cur
        if st is None and muted is None:
            return True, '读不到设备播放状态（不判，放行）：%s' % raw[:80], t_push
        if muted:
            if cur is not None and cur >= HEAD_MS:
                # ★ 已经播进去了：这时候补发＝让他听见"这句重来一遍"。
                #   他丢的是尾巴（而且 CC 未必真静着），比重来一遍轻得多 ⇒ 不动它。
                return True, ('CC 报静音，但它已经播到第 %.1fs 了 ⇒ 不动它'
                              '（补发会让他听见重来一遍）' % (cur / 1000.0)), t_push
            if time.time() - _t0 > MUTE_WAIT:
                LAST_ERR = ('CC 一直静音着（Set mute true）：等了 %.1f 秒、补发 %d 次'
                            '都没让它松开' % (MUTE_WAIT, _n_resend))
                return False, ('设备在播，但 CC 一直静音着（Set mute true）—— 等了 %.1f 秒'
                               '（含 %d 次补发）都没让它松开，这一句多半没出声'
                               % (MUTE_WAIT, _n_resend)), t_push
            print('         ↻ CC 刚静音着（music_st:%s，才播到 %sms）⇒ 开头正被它吃掉，'
                  '当场补发（已等 %.1fs）' % (st, cur if cur is not None else '?',
                                              time.time() - _t0))
            # ★★ 补发前【当场重问一次总控认谁】：被掐=最可能 pid 变了，抱着旧答案补发
            #    只是再撞一次同一面墙（2026-09-22 深夜实测：pid 会被我们自己的推流带得来回摆）。
            _play601(url, wait=8.0, pid=_cur_player(force=True))
            _pid_cache['at'] = 0.0     # 补发了 ⇒ 缓存作废，下一轮重新问
            _pid_cache['miss'] = 0.0   # ★ 连"问空了"的冷冻一起解掉（真问一次，别拿冷冻当答案）
            _n_resend += 1
            t_push = time.time()          # 音频从头重来 ⇒ 基准跟着走
            time.sleep(0.4)
            continue
        if st == '0x3':
            # ★★★ 看见 0x3 的这一刻，它已经在播第 `cur` 毫秒了 ⇒ 反推真正的起播时刻。
            #   这一行就是"回答被当成主人说话"那个病的药：它让 `audible_end()` 说的
            #   是**设备**的时间线，而不是我们推出去的时间线（两者在撞上 ContinuePlay、
            #   要补发时差到 4 秒，见 `audible_end()`）。
            _PLAY['at'] = time.time() - (cur or 0) / 1000.0
            return True, '设备确认在播（music_st:0x3，CC 未静音）', t_push
        if st is None:
            # ★ 播放器那行是旧的/没读到，而静音判据也没话说 ⇒ 不判，放行。
            return True, '读不到此刻的播放状态（不判，放行）：%s' % raw[:80], t_push
        if st not in DEAD_ST:
            return True, '状态 %s 是过渡态，不补发' % st, t_push
        # ★★ 保险（2026-09-25 加）：把补发从"数 2 次"放宽到"等 8 秒"之后，
        #   万一设备其实在播、只是我们连着几轮都读到 0x4，就会一秒一次地把它
        #   拽回开头 ⇒ 主人听见的是**同一句话反复重来**（比少说一句更糟）。
        #   `cur` 与 `st` 取自【同一行】新鲜日志（`_fresh`），而暂停中的播放器
        #   位置不会自己走 ⇒ **"位置往前走了"就是"它真的放过"的硬证据**，
        #   这时候停手。门限 300ms 足够避开日志精度/取整的抖动。
        if cur is not None and _prev_cur is not None and cur - _prev_cur >= 300:
            return True, ('位置从 %dms 走到了 %dms ⇒ 它其实放过（这条 %s 读数不作数），'
                          '不补发' % (_prev_cur, cur, st)), t_push
        if i >= PLAY_RETRY and time.time() - _t0 > DEAD_WAIT:
            _PLAY['at'] = 0.0          # 没响成 ⇒ 没有"还在响"这回事，别把关麦窗口拉长
            # ★★ 判据当场作废：这次没响成，最可能的原因就是"该用的 playerId 变了"
            #    （主人刚放过音乐 / 刚响过闹钟）。不在这里作废的话，下一轮还要抱着
            #    这个错的答案再哑一句 —— 缓存 TTL 是 30 秒，够哑五六句。
            _pid_cache['at'] = 0.0
            _pid_cache['miss'] = 0.0   # ★ 同上：作废就是真作废，冷冻也解掉
            LAST_ERR = ('补发 %d 次、等了 %.1f 秒，设备仍停在 %s（没在播）——'
                        '多半被网易引擎自己的提示音压着' % (_n_resend, DEAD_WAIT, st))
            return False, ('补发 %d 次、等了 %.1f 秒后仍停在 %s（没在播）'
                           % (_n_resend, DEAD_WAIT, st)), t_push
        print('         ↻ 设备停在 %s ⇒ 被 ContinuePlay 连坐，补发 0x601'
              '（已等 %.1fs / 预算 %.1fs）' % (st, time.time() - _t0, DEAD_WAIT))
        # ★★ 同上：补发前当场重问 pid。我们的每一次推流都会让设备把 musicStatus
        #    报给总控 ⇒ 总控的 curPlayer 会跟着摆（实测 1→2→1），30 秒缓存经常是错的。
        _pid_cache['at'] = 0.0
        _pid_cache['miss'] = 0.0       # ★ 同上：作废就是真作废，冷冻也解掉
        time.sleep(0.5)
        _play601(url, wait=8.0, pid=_cur_player(force=True))
        _n_resend += 1
        t_push = time.time()          # 音频从头重来 ⇒ 基准跟着走
        _PLAY['at'] = t_push          # ★ 起播基准跟着补发走（这一段没有 0x3 可看）
        time.sleep(0.5)
    return True, '', t_push


def audible_end():
    """这一句【在人耳里响完】的本机时刻（`time.time()` 基准）；判不出来回 0。

    ★★★★★ 为什么需要它（2026-09-22 夜，主人原话「我感觉他的回答 也当成用户说话了」）：
      在这之前，"念完那一刻"（`spk_ear._said_at`）= **我们自己推完那一刻**。
      可喇叭真正开始响，比推晚 0.35 秒（正常）到 **4 秒**（撞上 CC 的 ContinuePlay、
      要补发 0x601 那一轮，实测 21:26:18 推、21:26:22.6 才响）——
      于是开耳时音箱【还在说】，我们自己的后半句被录进去送 ASR，
      转出来就是「我在说吧」「想让我做点什么」「记下了」这些，
      脑子一本正经地回应它自己（当晚 21:26~21:29 整整绕了十几轮）。
      ⇒ 唯一的正解是：**关麦窗口按设备的时间线算**，不是按我们的。

    ★ 为什么不需要跟设备对表：`at` 是我们在【本机时钟】上看⻅ 0x3 的那一刻反推的
      （`time.time() - cur`），`secs` 是人声长度 —— 两个都是本机基准的秒数，
      只有"差多少秒"有意义，所以设备/本机的钟差根本不进来。
    ★ 判不出来（读不到状态、没推成）回 0 —— 调用方按老办法（推完 + 0.4 秒）处理，
      **绝不因为"量不到"就把耳朵关死。**
    """
    if not _PLAY['at'] or not _PLAY['secs']:
        return 0.0
    return _PLAY['at'] + _PLAY['secs']


def _say601(ear, mp3_path, url=None, probe=None, wait_path=None):
    """601 那条路的 say：推命令 → 等它取流 → 交给 _wait_and_judge 下结论。

    三态跟 DLNA 那条路逐字一致（True / False / 'cut'），调用方不用分叉。

    ★★ `url` / `probe` 是给【流式 TTS】留的口子（2026-09-23），两个都**默认 None
      = 老路，逐字节不变**。那条路只有两处跟这里不一样，所以只开这两个口子，
      其余（补发 0x601、等取流、`_ensure_playing`、`_wait_and_judge`）**全部复用**：
        · `url`   —— 流式端点自己带 URL（`http://192.168.1.100:8896/tts/<sid>`），
                     不是 8899 上那个静态文件；**它边吐边等，设备边下边播**。
        · `probe` —— "设备来取了吗"。老路数的是 mp3srv(8899) 的取流日志
                     （`fetched_count`），而 8896 的日志它数不到 ⇒ 得由调用方给一个
                     可调用的判据（`spk_tts_stream` 的 `pulled` 标志）。
      ★ `mp3_path` 在流式路上**照样要传**：端点会把人声+静音垫落一份到 /tmp，
        那条早退判据（"已在 STAGE_DIR 且够长 ⇒ 原样返回"）会命中 ⇒ `_stage601`
        一个字节不动，而它**顺手填了 `_PLAY['secs']`** ——
        回声闸门（`audible_end()`）的记账因此跟老路完全一致，不必另写一份。
        这正是"只开两个口子"能成立的原因。

    ★ 判据【没有变】：还是"它来取文件了没有"（fetched_count）+ 自听（只记不判）。
      取流是设备真的来 HTTP 拉这条 mp3 —— 链路走通了的硬证据。
    ★ `t_push` 记在【推之前】：_wait_and_judge 拿它当"推完之后"的时间基准。
      记在推之后的话，设备可能已经开口了，基准就落到了人声中间 ⇒ 前 0.3 秒的
      有声活动被判丢，短句会被误判成"响了但不够长"。
    """
    global LAST_ERR
    _PLAY['at'] = 0.0              # ★ 每一句都从零记账：绝不把上一句的"还在响"算到这一句头上
    staged, note = None, ''
    if url is None:
        staged, note = _stage601(mp3_path)  # 这一步填 `_PLAY['secs']`（人声到哪儿）
        fname = os.path.basename(staged)
        before = fetched_count(fname)
        url = 'http://%s:%d/%s' % (OUR_IP, PORT_MP3, fname)
    else:
        # ★★ 流式路：【先推、后备文件】—— 顺序跟老路**正好相反**，而这正是要省的
        #   那 3.4 秒。我们推 URL 时人声才刚开始生成，设备边下边播；
        #   等它来取之后（下面）才 `wait_path()` 把落盘等齐、补跑 `_stage601`。
        #   ★ 那份落盘文件自带 45 秒静音垫 ⇒ `_stage601` 的早退判据命中、秒回，
        #     而且 `_PLAY['secs']` 与 `_wait_and_judge` 拿到的都是**完整的那一份**
        #     （绝不能拿"刚生成一半"的份去判 —— 那会把长句误判成"没念成"）。
        fname, before = '这条流', 0
    if abort_on():
        if ear is not None:
            ear.stop()
        print("         ✋ 还没开口就被打断，不推了")
        return 'cut'
    if note:                       # ★ 流式路这时还没有 note（备文件排在推之后）
        print('   ♪ %s' % note)
    t_push = time.time()
    ok, why = _play601(url, wait=8.0)
    if not ok:
        if ear is not None:
            ear.stop()
        print('   ✗ 播放命令没送到设备：%s' % why)
        return False
    # 等它来取文件（实测 命令→取流 185ms，给足 6 秒；顺便每 0.7 秒看一眼有没有被打断）
    # ★★★★★ 2026-09-22 深夜：**不取就当场补发 0x601**，不再白等满 6 秒。
    #   当晚实测的哑巴形态（21:35）：上一条是思考音（全长 17.5 秒），它在
    #   `duration − 15000` = **2.5 秒**处撞上设备的 Next 规则 ⇒ `st:0x7` →
    #   `st:0x4` Paused（`cur:2799ms`）⇒ 播放器接着卡在
    #   `playerControl(1088):wait for audio play.`。于是我们这条 0x601 **送到了、编号也回了**，
    #   但设备一个字节都不来取（`Set mute true` 挂在那儿）。
    #   旧代码对此只有一个动作：**白等 6.3 秒，然后报"没人来拉这条流"** ——
    #   在主人那儿就是"一整句话没了"，而且等得越久他越以为是音箱死了。
    #   ⇒ 机器的脾气（见 `_ensure_playing` 里那段）：**再推一条命令能把它从 Paused
    #     里拽出来**（CC 从 `Set mute true` 到松手实测 0.126 秒）。所以 1.4 秒没动静
    #     就【当场再推一次】，2.8 秒还没动静再推一次；总预算不变（还是 6.3 秒）。
    got = False
    resent = 0
    # ★ "设备来取了吗" —— 老路数 8899 的取流日志，流式路用调用方给的判据。
    #   定义一次放循环外：`probe()` 那头是 HTTP 查询，别在循环里反复构造闭包。
    gone = probe if probe is not None else (lambda: fetched_count(fname) > before)
    # ★★★★★ 2026-09-22 深夜：**6.3 秒 → 3.5 秒**（`range(9)` → `range(5)`）。
    #   主人原话：「现在我说完 等很久才听到思考音 这中间干什么呢」—— 他问的这一段，
    #   大头就在这里：这一句播不出去时，耳朵是**聋着**的（录音循环被说话卡住，
    #   攒下的音频回头当"旧音频"整段丢掉，见 spk_ear 的「丢掉说话期间攒下的 X 秒」）。
    #   实测两轮：21:46:18 推 → 21:46:25 认输 = **7 秒**；21:52:26 推 → 21:52:36 = **10 秒**。
    #   而健康时"命令→取流"只要 185 毫秒 ⇒ 6.3 秒是 34 倍余量，纯白等。
    #   ★ 两次补发（1.4s / 2.8s）一个不少 —— 那才是真能把它从 Paused 拽出来的动作；
    #     实测两次补发都没动静时，再等下去也从没有过第三次奏效。
    for k in range(5):
        time.sleep(0.7)
        if abort_on():
            _stop_playback(None, None)
            if ear is not None:
                ear.stop()              # ★ 不判、不留证：这不是"没听见"，是让路
            print("         ✋ 被打断 —— 喊停播放")
            return 'cut'
        if gone():
            got = True
            break
        if k in (1, 3) and resent < PLAY_RESEND:
            resent += 1
            print('   ↻ %.1f 秒没来取 ⇒ 补发第 %d 次 0x601（设备多半停在 Paused）'
                  % ((k + 1) * 0.7, resent))
            _play601(url, wait=2.0)
    if not got:
        if ear is not None:
            ear.stop()
        # ★ 补发过几次要说出来 —— "补了几次都没用"和"一次都没补"在日志上必须一眼分得开。
        print('   ✗ 它没来取 %s —— 命令送到了，但没人来拉这条流（补发过 %d 次）'
              % (fname, resent))
        LAST_ERR = '音箱没来取文件（%s）：命令送到了，补发 %d 次也没人来拉这条流' % (fname, resent)
        return False
    print('         ★ 音箱在取了')
    # ★ 流式路：**它已经在播了**，这一等是并行的 —— 主人听不出任何区别。
    #   等的只是"落盘齐"，好让 `_stage601` 填对 `_PLAY['secs']`、
    #   好让 `_wait_and_judge` 拿完整文件去判。等不到就退回传进来的那份
    #   （`wait_path()` 回 None ⇒ 用 mp3_path），**绝不许因为等不到就说没念成**。
    if staged is None:
        p = (wait_path() if wait_path is not None else None) or mp3_path
        if p is None:
            # ★ 流式那份没落盘、调用方也没给兜底文件。**这时候它多半真没念成**
            #   （我们推的 URL 那条流死了，设备拿到的只有半截）。
            #   ⇒ 老实报"没成"，让调用方/日志看得见；`_PLAY['secs']` 就填不上了，
            #     但既然没出声，关麦窗口也就无所谓。
            if ear is not None:
                ear.stop()
            print('   ✗ 流式那份没落盘、也没有兜底文件 —— 这一句判不了"听没听见"')
            LAST_ERR = '流式那份没落盘、也没有兜底文件 —— 判不了"听没听见"'
            return False
        staged, note = _stage601(p)
        print('   ♪ %s' % note)
    # ★★★★★ 2026-09-22 晚：取到流【不等于】播出来 ⇒ 必须再确认"真的在播"。
    #   当晚就是这么全哑的：设备取了流（GET 200）、判据报成功，而播放器在
    #   Preparing 之后 2ms 被 ContinuePlay 的 forcePausePlayer 按成 Paused，
    #   一个字都没出声。理由全在 _ensure_playing 的 docstring 里。
    playing, note2, t_push = _ensure_playing(url, t_push)
    print('         %s %s' % ('✓' if playing else '✗', note2))
    if not playing:
        if ear is not None:
            ear.stop()
        return False
    if ear is None:
        return True
    return _wait_and_judge(ear, staged, t_push, None, None)


# ---------------------------------------------------------------- 播放
def _send_uri(base, udn, fname):
    """把「放这个文件」整条命令发给音箱：设音量 → SetAVTransportURI → Play。
    回 Play 之前的时刻（判"推完之后"的基准）。

    ★ 从 say() 里抽出来的，给 push() 共用 —— 各写一份的话，改了一边忘了另一边，
      表现是"思考音好好的、回答不响"这种极难查的不一致。
    """
    soap(base, udn, "RenderingControl", RC, "SetVolume",
         "<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>100</DesiredVolume>")
    uri = f"http://{OUR_IP}:{PORT_MP3}/{fname}"
    didl = ('&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
            '&lt;item id="1" parentID="0" restricted="1"&gt;&lt;dc:title&gt;AI&lt;/dc:title&gt;'
            '&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
            f'&lt;res protocolInfo="http-get:*:audio/mpeg:*"&gt;{uri}&lt;/res&gt;'
            '&lt;/item&gt;&lt;/DIDL-Lite&gt;')
    r = soap(base, udn, "AVTransport", AV, "SetAVTransportURI",
             f"<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI>"
             f"<CurrentURIMetaData>{didl}</CurrentURIMetaData>")
    if "<u:SetAVTransportURIResponse" not in r:
        print(f"          SetAVTransportURI 没成: {r[:90]}")
        # ★ 多半是这个 base 已经不新鲜了（设备换了 UPnP 端口）⇒ 把缓存作废，
        #   下一次调用会重新去 SSDP 找。不作废的话，每一轮都要先白等一次探测。
        _forget()
    t_push = time.time()          # 记在 Play 之前：后面判"推完之后"用这个基准
    soap(base, udn, "AVTransport", AV, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    return t_push


def push(mp3_path, tries=2, deadline=None, abort=None):
    """推一条就撒手 —— 不等它来取、不自听、不判。回 True=命令发出去了。

    ★ 给【思考音】用（2026-09-21 深夜接的）。它要在主人还在等的时候尽快出声，
      盖住大模型 + TTS 那几秒静音（601 路上就是 0.8 + 2.7 秒，2026-09-22 实测）；
      判成败的事归后面那条正式的 say()。
    ★ 为什么不复用 say()：say() 会起自听耳朵、等它来取文件、最多耗 25 秒 ——
      思考音要是走那条路，等于"为了报一句嗯……先卡 25 秒"，比不做还糟。
    ★ 实测 discover() 抖得很厉害（连做 5 次：0.21 / 0.23 / 1.22 / 5.12 / 6.34 秒），
      所以这里只试两轮；试不到就算了 —— 思考音本来就是个锦上添花的东西。

    ★★ `abort`（一个 threading.Event）是【防掐回答】的那道闸，不是省时间：
      两条流推的是同一台设备，**后 Play 的会掐掉先 Play 的**。
      回答那条路一开始推，就会 set 这个事件 —— 这里在真正发 Play **之前**再看一眼，
      设了就一个字都不发。超时（`deadline`）是同一件事的另一半：两者都保留。
    ★ 夜间不推。闸门故意设在这一层（唯一的出口），以后谁调都绕不过去。
    """
    if NIGHT_QUIET and in_night():
        return False
    t0 = time.time()

    def _give_up():
        return (abort is not None and abort.is_set()) or \
               (deadline is not None and time.time() - t0 > deadline)

    # ★★ 601 那条路：不用发现、不用重试（设备主动来取命令）—— 实测抖的那 0.21~6.34 秒
    #   整个消失了。这里只保留"该不该推"的两个闸门（夜里 / 回答已经要开始了）。
    if PLAY_VIA == '601':
        if _give_up():
            print(f"   （{time.time() - t0:.1f} 秒还没轮到推 / 回答已经要开始了，这条不推了）")
            return False
        staged, note = _stage601(mp3_path)
        url = 'http://%s:%d/%s' % (OUR_IP, PORT_MP3, os.path.basename(staged))
        ok, why = _play601(url)
        print('   ♪ 思考音 %s —— %s' % (note, '发出去了' if ok else '没发出去（%s）' % why))
        return ok

    fname = os.path.basename(mp3_path)
    for _ in range(max(1, tries)):
        if _give_up():
            print(f"   （{time.time() - t0:.1f} 秒还没找到音箱 / 回答已经要开始了，这条不推了）")
            return False
        base, udn = discover_fast()
        if base:
            # ★★ 最后一道：就在发 Play 之前再看一眼。这一眼是整条链上唯一
            #   "绝不会掐掉回答"的保证 —— 上面那些检查都还隔着网络往返。
            if _give_up():
                return False
            _send_uri(base, udn, fname)
            return True
    return False


def say(mp3_path, tries=8, verify=None, url=None, probe=None, wait_path=None):
    """把 mp3 推给音箱念出来，并且【自己听】确认它真的出声了。

    两级判据，缺一不可：
      ① 它来取了文件（链路通）—— 老判据
      ② 我从麦克风里听见了（喇叭真响了）—— 新判据
    ★ 只有 ② 才算成功。①只证明链路走到了：HP_L Mux 那次全哑事故里每一环都是"成功"的。
    ★ 返回时机也跟着变了：以前是"它一取文件就返回"，那会儿它还在说话，调用方却以为
      空闲了（server.py 的 SPEAKING 就是这么被提前清掉的）。现在撑到念完为止。

    ★ 夜间禁声时段直接返回 False，并且【一个网络动作都不做】。

    ★ 返回值是三态（2026-09-21 起）：True=念完了 / False=该出声没出成 /
      'cut'=被主人打断（成功让路，**不留证** —— 见上面"打断"那节）。
    """
    global LAST_ERR
    LAST_ERR = ''                  # ★ 每一句都从零记账：绝不让上一句的失败原因串到这一句头上
    if NIGHT_QUIET and in_night():
        print("   ★ 夜间禁声（%s），不推流" % quiet_text())
        LAST_ERR = '夜间禁声（%s），一个网络动作都没做' % quiet_text()
        return False
    # ★ 每次开口前先清掉上一轮残留的打断信号。不清的话，主人打断过一次之后，
    #   后面每一句都会"刚开口就被打断"，而且日志上看起来像是声纹误判 ——
    #   查半天会查到声纹头上去，其实是一个没清的标志位。
    #
    # ★ 这个 clear 会不会把【这一轮刚起来的观察者】误清掉？不会，而且不是运气：
    #   speak() 是先 b.start() 再调 say()，两者之间只有几微秒；而观察者要攒够
    #   ≥2.5 秒音频才量得出延迟、再连续 3 窗（0.75s）才 fire —— 它【物理上】
    #   不可能在这几微秒里响。所以"清在入口"既收拾了上一轮的残留，又不会误伤本轮。
    #   （哪天有人把观察者改成"一有音频就判"，这条推理就不成立了，得改成
    #     "退出时消费"而不是"入口清除"。）
    _ABORT.clear()
    if verify is None:
        verify = SELFHEAR
    ear = None
    if verify:
        e = Ear()
        if e.start():
            ear = e
            print("   👂 自听已起（UDP %d，只在内存里）" % e.port)
        else:
            print("   👂 自听起不来：%s —— 只按老判据来" % e.err)
    # ★★★ 流式路（2026-09-23 修）：流式起来时 `mp3_path` **就是 None** —— 调用方不许在这里
    #   等那份兜底合成（等它就等于没优化，见 `spk_ear.speak()`）。可这两行在够到下面的
    #   601 分叉**之前**就会 `basename(None)` 抛 TypeError ⇒ 现象是【流式一旦成功，
    #   一开口就炸】，而且炸之前已经起了自听耳朵、没人 stop。
    #   `_say601` 那边本来就兼容 None（见它里面"调用方也没给兜底文件"那段），
    #   漏的只是这里这个 guard。DLNA 那条路一定拿得到真路径（`speak()` 只在那条路上
    #   才合成），所以 guard 只影响流式路，老路逐字节不变。
    #   ★ 写成 `mp3_path or ''` 而不是 if/else：对**任何字符串**入参，这跟原来的
    #     `basename(mp3_path)` 逐字节等价（`basename('')` 也是 `''`），只有原先会抛异常的
    #     None 被接住 ⇒ 老路确实一个字节没动。
    fname = os.path.basename(mp3_path or '')
    before = fetched_count(fname)
    # ★★ 601 那条路从这里分叉：它不做 SSDP 发现、不重试、不读 base/udn。
    #   上面那些准备（夜里闸门 / 清打断标志 / 起耳朵）两条路共用，所以分叉点放在这儿。
    if PLAY_VIA == '601':
        # ★ `url`/`probe` 只在 601 这条路上有意义（流式端点走的就是 0x601）：
        #   DLNA 那条路推的是 8899 上那个静态文件，塞不进别的 URL。不传 = 老行为。
        return _say601(ear, mp3_path, url=url, probe=probe, wait_path=wait_path)
    # ★★ 总预算（2026-09-22）：只卡"还没推出去"的阶段。推出去之后等它念完不算
    #   （那是有效工作）。旧版没有预算 ⇒ DLNA 卡死时这里要空转 8 × 30 = 240 秒，
    #   而调用方（server.py）一直以为它在说话。详见上面"推流预算"那节。
    t_start = time.time()
    miss = 0
    for k in range(1, tries + 1):
        if time.time() - t_start > SAY_BUDGET:
            print(f"   ★ 推流总预算 {SAY_BUDGET:.0f} 秒用完还没推出去 —— 放弃")
            break
        base, udn = discover_fast()
        if not base:
            miss += 1
            # ★★ 连着两次全空就认定 DLNA 不在，不再重试。
            #   重试是给"设备限流/省电"准备的，那是【偶发】；连着两次全空
            #   说明服务根本没起来（实测卡死时连做 6 次 SSDP 全 0 应答）。
            #   不认这个判据的话，剩下的重试只是把预算耗光而已。
            if miss >= 2:
                print("   ★★ 连着两次 SSDP 全空 —— DLNA 多半不在，不再重试")
                break
            print(f"   第{k}次: SSDP 没应答，再试")
            continue
        miss = 0
        # ★ 还没推就被打断了（观察者在构造/发现期间就判出来了）：那就一个字都别推。
        if abort_on():
            if ear is not None:
                ear.stop()
            print("         ✋ 还没开口就被打断，不推了")
            return 'cut'
        print(f"   第{k}次: 发现 {base}，推流…")
        t_push = _send_uri(base, udn, fname)
        # 等它来取文件 —— 链路通了的标志
        for _ in range(8):
            time.sleep(0.7)
            if abort_on():
                print("         ✋ 被打断 —— 喊停播放")
                _stop_playback(base, udn)
                if ear is not None:
                    ear.stop()              # ★ 不判、不留证：这不是"没听见"
                return 'cut'
            if fetched_count(fname) > before:
                print(f"         ★ 音箱在取了（{base}）")
                if ear is None:
                    return True
                return _wait_and_judge(ear, mp3_path, t_push, base, udn)
        print("         它没来取，端口大概又换了，重新发现")
    if ear is not None:
        ear.stop()
    LAST_ERR = LAST_ERR or 'DLNA 那条路：%d 次都没推成（发现不到 / 它没来取）' % tries
    return False


# ---------------------------------------------------------------- 主流程
def ask(question):
    """问 DeepSeek，回一句话文本。给足 max_tokens。"""
    key = os.environ["DS_KEY"]
    req = urllib.request.Request(
        f"{BASE}/v1/messages",
        data=json.dumps(_think_of({"model": MODEL, "max_tokens": 2000,
                                   "messages": [{"role": "user", "content": question}]})).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    ans = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
    if not ans:
        print(f"   ⚠ 没 text 块 stop_reason={d.get('stop_reason')} usage={d.get('usage')} "
              f"块类型={[c.get('type') for c in d.get('content', [])]}")
    return ans


def ask_ex(messages, system=None, tools=None, max_tokens=2000, timeout=90, model=None):
    """带 tools 的调用 —— 给 spk_skills 的工具循环用。

    ★ 2026-09-21 实测这个端点【支持】tool calling（三发三中）：
        ① 带 tools + 顶层 system → stop_reason='tool_use'，content 里有标准 tool_use 块
        ② 带 tools 不带 system     → 同样 tool_use
        ③ 只带 system 不带 tools   → 正常 end_turn，【不 404】
      所以 memory 里那条"顶层 system 会触发路由到 /anthropic 端点导致
      404"的教训，【在这个端点上不成立】—— 那条是 Gemini 那边的。
      多轮回传也闭环：把 tool_result 塞回 user 消息，模型接着用中文说话。
      回传 assistant 那轮时【只回 tool_use 块就行】（实测去掉 thinking 块一样通），
      不必原样搬运 thinking —— 省事，也免得它的 signature 过期。

    返回 (content_blocks, stop_reason)。★ 这里【只负责说话，不负责动手】：
    任何 tool_use 都原样交出去，由 spk_skills 的白名单决定能不能执行。
    """
    # ★ model 可以逐次覆盖（调用方要临时换个模型时用）。模块级的 MODEL 现在是
    #   flash —— 2026-09-22 主人拍板，理由与实测见文件顶部"模型与思考模式"那节。
    body = _think_of({"model": model or MODEL, "max_tokens": max_tokens,
                      "messages": messages})
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"{BASE}/v1/messages", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": os.environ["DS_KEY"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d.get("content", []), d.get("stop_reason")


def tts(text, out="/tmp/spk_ai.mp3"):
    """合成一句话。**三条路，逐级退化，最后一条一定成。**

        ① Qwen（2026-09-23 起的主路）—— 嗓子的现役来源，见 spk_voice.use_qwen()
        ② edge 常驻连接（快路）—— 换引擎前的唯一主路，现在当兜底
        ③ edge 命令行（慢路）—— 起进程 2.2 秒，但它不依赖任何常驻状态

    ★★ 为什么要这么多条（2026-09-22 实测）：命令行那条路**2 个字的「好。」也要 2.2 秒**
      —— 那 2.1 秒全是"起进程 + DNS + TLS + WS 握手"，合成本身才零点几秒。
      常驻连接把这段摊掉之后每句只要 0.35~0.8 秒。详见 spk_tts.py 顶部。
    ★ 但**快路不许成为唯一的路**：它是"更好"，说话是"这次要命"。
      Qwen 那条同样只是"更好" —— 它要走公网 API，还会撞限流。
      `_qwen.synth()` 任何失败都只返回 False（不抛）；`spk_tts.synth_raw()` 同理。
      一个都不要 `check=True` 就往下走，最底下那条是 edge 命令行。
      `SPK_TTS_WARM=0` 只关掉 ②；`SPK_TTS_ENGINE=edge` 关掉 ①（整条嗓子链的回退开关）。
    ★ 合成出来的音频**必须逐帧一样**才敢换 —— 已验：24kHz/单声道/48kbps，
      过完滤镜链时长分毫不差（`python3 spk_tts.py` 可复现）。
    ★ 响度不用为换引擎操心：三条路出来的都要过下面同一串 `_voice.chain()`，
      实测 Qwen 与 edge 同句整合响度**完全一致**（都是 -18.9 LUFS）。
    """
    raw = "/tmp/spk_ai_raw.mp3"
    ok = False
    if _voice.use_qwen():
        try:
            ok = _qwen.synth(text, raw, instructions=_qwen.INSTR_DEFAULT)
        except Exception as e:                      # noqa: BLE001
            # synth() 自己已经把所有失败都吞成 False 了；这里再兜一层是防它将来改坏，
            # 也防 open() 写 /tmp 失败这种它没覆盖到的例外 —— 出声这件事不许被一行异常带走。
            print('   ⚠ Qwen 合成抛了异常（%s: %s）—— 这句退回 edge 嗓子'
                  % (type(e).__name__, e), flush=True)
            ok = False
        if not ok:
            print('   ⚠ Qwen 没合成出来 ⇒ 这句是 edge 的嗓子（会听出换人）', flush=True)
    if not ok and not _tts_warm.synth_raw(text, raw):
        subprocess.run(["edge-tts"] + _voice.edge_args() + ["--text", text,
                        "--write-media", raw], check=True, capture_output=True, timeout=90)
    # 这台设备"没听到"的历史真凶是振幅太低 ⇒ 响度顶满
    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-y", "-i", raw, "-af",
                            _voice.chain(), "-b:a", "128k", out],
                           capture_output=True)
        if p.returncode != 0 or not os.path.exists(out):
            shutil.copy(raw, out)
    else:
        shutil.copy(raw, out)
    return out


def main():
    # 诊断用：只听不出声，把麦克风的 RMS 包络打出来
    # （标定 judge 的门限、确认 mictap 还活着，都靠它）
    #   python3 spk_ai_dlna.py --ear 10
    if len(sys.argv) > 1 and sys.argv[1] == '--ear':
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
        e = Ear()
        if not e.start():
            print('✗ %s' % e.err)
            return 1
        print('👂 听 %.1f 秒（UDP %d，只在内存里）…' % (secs, e.port))
        time.sleep(secs)
        s = e.stop()
        if not s:
            print('✗ 一个包都没收到 —— mictap 在推吗？（设备上查 /proc/<pid>/maps 有没有 mictap）')
            return 1
        pk = max(v for _, v in s) or 1.0
        print('收到 %d 包，格式 %s' % (e.pkts, e.fmt))
        for t, v in s:
            print('  %6.2fs  %.4f  %s' % (t, v, '#' * min(60, int(v / pk * 60))))
        vals = sorted(v for _, v in s)
        print('本底(25分位) %.4f   峰值 %.4f   门限会是 %.4f'
              % (vals[len(vals) // 4], vals[-1],
                 max(vals[len(vals) // 4] * NOISE_MULT, abs_min())))
        return 0

    q = os.environ.get(
        "SPK_ASK",
        "请用一句简短自然的中文口语跟我打个招呼，并说明你是哪个大模型。"
        "总长不要超过30个字，不要用任何表情符号和markdown。")
    print(f"1) 问 DeepSeek：{q}")
    try:
        ans = ask(q)
    except Exception as e:
        print(f"   ✗ 调用失败 {type(e).__name__}: {e}")
        return 1
    print(f"   ★ 回答：{ans!r}")
    if not ans:
        return 1
    mp3 = tts(ans)
    print(f"2) 语音 {os.path.getsize(mp3)} 字节")
    print("3) 推给音箱：")
    ok = say(mp3)
    print("=== " + ("★ 音箱念出来了" if ok else "✗ 没推成功") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
