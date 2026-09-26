#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音箱指令层 —— 让音箱【做事】（播/停/音量），不是让它说话。

与 spk_ai_dlna 的分工：
    spk_ai_dlna = "嘴"：合成 → 推流 → 自听确认，一次性的，推完就完
    spk_ctl     = "手"：播放控制 / 状态查询 / 音量，可反复调，是常驻能力
底层共用同一套 DLNA（discover/soap 直接 import，不重写一份）。

★ 为什么走 DLNA 而不是 DBus
  ★★★ 2026-09-22 更正：下面这段"player 只认 controller 的身份 ⇒ 那条路是死的"
     **是错的，而且是错在一个很隐蔽的读法上。** 实测（同一天，用设备上的
     `device/play601.sh` 发 0x601/0x603）那条路**完全是通的** ——
     命令上总线 → 设备来取流 → `music_st:0x3` Playing → 0x603 → `0x0` Idle，
     全程 185ms，一天里跑了十几轮没失手。当时那句"伪造不了身份"的依据是
     dbus-send 打出来的
         Error org.freedesktop.DBus.Error.NoReply   （rc=1）
     —— 可**这恰恰是投递成功时的输出**：播放器是"只收不回"的，它收到 method_call
     就干活、从不发 reply，所以 dbus-send 干等满 reply-timeout 之后必然吐这句。
     把"NoReply + rc=1"读成"它拒绝了我们"，才推出"身份伪造不了"这个不存在的机制。
     ★ 判据错一次的代价：一条好路被写死成"死路"，此后半年没人再看它一眼。
       **判成败要读被控方的日志（ihwplayer 的 cmdId / music_st），不要读发送方的返回码。**
  ⇒ DBus 那条路（见 device/play601.sh）现在是【说话】的主路（spk_ai_dlna 的
     SPK_PLAY_VIA=601）；DLNA 留着当回退，也仍是这一层"做事"的现役实现。
     ★ 但 DLNA 会静默卡死（KPlayer 进程活着、日志一行错没有、一个端口都不监听），
       哪天这一层要提速或救活，照 play601.sh 的样子做成 0x601/0x603 即可。
  DLNA 是音箱【自己开放的官方接口】，不需要伪造任何身份。实测状态机是真跟着走的：
      STOPPED → PLAYING → PAUSED_PLAYBACK → STOPPED
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cfg import PORT_MP3           # noqa: E402
import spk_ai_dlna as dlna          # discover / soap / OUR_IP / AV / RC 都从这里复用

AV = dlna.AV                        # urn:schemas-upnp-org:service:AVTransport:1
RC = dlna.RC                        # urn:schemas-upnp-org:service:RenderingControl:1
HTTP_PORT = os.environ.get('SPK_HTTP_PORT', PORT_MP3)   # 静态服务器根目录 = /tmp

_target = None                      # 缓存 (base, udn)
_fail_at = 0.0                      # ★ 上次发现失败的墙钟时刻（见 FAIL_COOLDOWN）

# ★★★ 失败也要缓存 —— 这是 2026-09-22 逮到的一个【每轮白等 6 秒】的根因。
#
#   `target()` 原来只在**成功**时写 `_target`，失败时它保持 None
#   ⇒ **下一次调用又重发现一遍**。设备一旦不在（DLNA 退役后 SSDP 0/6 是常态），
#     这就从"缓存一次"变成了"每次调用都付全价"。
#
#   ★ 实测代价：`spk_skills.system_prompt()` 每轮 **6.002 秒**，连做两次都是 6.001/6.002
#     —— 里面正是 `now_playing()` + `status()` 各一次 3.0 秒的 SSDP 超时。
#     而真正的模型（deepseek-v4-flash、thinking disabled）只花 **0.65 秒**。
#     ⇒ 主人那句「一个现在几点了 大模型花了6秒？」的答案就在这儿：
#       **那 6 秒一个字都不是模型花的，是两次注定失败的服务发现在等超时。**
#
#   ⇒ 失败后在 FAIL_COOLDOWN 秒内【立刻抛】，连一次发现都不做。
#   ★ 取 60 秒：设备万一回来，最多一分钟后自动恢复，不需要重启任何东西。
#   ★ 要当场强制重试仍然可以：`target(force=True)`（离线自测在用）。
#   ★ 这条缓存对**所有**调用方生效 —— `spk_skills`（提示词 + 两个工具）、
#     `spk_agent`、`spk_alarm` 都走 `_soap()` ⇒ `target()`，一处修、处处不再白等。
FAIL_COOLDOWN = float(os.environ.get('SPK_CTL_FAIL_COOLDOWN', '60.0'))


def target(force=False):
    """拿 (base, udn)。★ 必须缓存：SSDP 一轮要 3~6 秒，每个动作都重发现会让指令慢得没法用。
    设备重启后 UDN/端口可能变，所以提供 force=True 重新发现。
    ★★ **失败也缓存**（`FAIL_COOLDOWN`）—— 否则"设备不在"会让每次调用都白等一遍，
      在 DLNA 已退役的现状下那就是每轮 6 秒。"""
    global _target, _fail_at
    if _target is None or force:
        if not force and _fail_at and (time.time() - _fail_at) < FAIL_COOLDOWN:
            raise RuntimeError('没找到音箱（SSDP 无回应；%.0f 秒内不再重试）'
                               % (FAIL_COOLDOWN - (time.time() - _fail_at)))
        base, udn = dlna.discover()
        if not base:
            _fail_at = time.time()
            raise RuntimeError('没找到音箱（SSDP 无回应）—— 它是 DLNA 渲染器吗？在线吗？')
        _target = (base.rstrip('/'), udn)
        _fail_at = 0.0
    return _target


def _soap(svc, ns, action, args, force=False):
    base, udn = target(force)
    r = dlna.soap(base, udn, svc, ns, action, args)
    # ★ 失败时 DLNA 回的是 s:Fault；成功了是 XXXResponse。别把 Fault 当成成功。
    if '<s:Fault' in r or 'UPnPError' in r:
        m = re.search(r'<errorDescription>(.*?)</errorDescription>', r)
        raise RuntimeError('%s 被拒：%s' % (action, m.group(1) if m else r[:200]))
    return r


def _src(src):
    """本地路径 → 音箱能取的 URL。★ 8899 静态服务器的根就是 /tmp，
    所以 /tmp/x.mp3 对应的 URL 就是 http://我们:8899/x.mp3。"""
    if src.startswith('http://') or src.startswith('https://'):
        return src
    if not os.path.isabs(src):
        src = os.path.abspath(src)
    if not src.startswith('/tmp/'):
        raise ValueError('本地文件必须在 /tmp 下（静态服务器只暴露那儿）：%s' % src)
    return 'http://%s:%s/%s' % (dlna.OUR_IP, HTTP_PORT, os.path.basename(src))


# ---------------------------------------------------------------- 出口：两条路
# ★★★ 2026-09-23：DLNA 退役之后**这一层整层是死的** —— play/pause/resume/stop
#   全走 SOAP，而设备早就不应 SSDP 了（0/6 是常态，见 `target()` 那条失败缓存）。
#   表现就是主人 14:02 遇到的那一次：它说"我放上了"，屋里一点声音没有；
#   说"停"，也停不了 —— **而每一环都报成功**（工具照常返回"开始放"）。
#
#   出口换成 0x601/0x603（设备自己的 ihwplayer，走设备上常驻的 `play601.sh`）：
#   跟【说话】那条路同一个出口、同一把开关，实测命令→出声 185ms。
#   ★ 开关读的就是 `SPK_PLAY_VIA`，跟 `spk_ai_dlna` 共用一个值 —— 这个项目里
#     "第二份真话来源"已经咬过好几次（嗓子的默认值、思考音的索引、播放器号），
#     出口绝不能再出现第二个开关名。
#   ★ 回退仍然是一行环境变量（`SPK_PLAY_VIA=dlna`），不用改代码、不用重推脚本。
PLAY_VIA = os.environ.get('SPK_PLAY_VIA', '601')     # '601' | 'dlna'


def via601():
    return PLAY_VIA == '601'


# ★ 上一次播的是哪个源 —— 601 那条路没有"暂停/续播"，`resume()` 只能从头再放，
#   所以至少要记得**放的是什么**。（位置记不住，见 `pause()` 那一大段。）
_last = {'src': None}

# 设备自己报的 music_st → 这一层对外承诺的那套词（`spk_alarm` 的响铃循环认的就是它）
#   0:Idle 1:Preparing 2:Playing? 3:Playing 4:Paused（见 spk_ai_dlna 的注释）
_ST_MAP = {'0x0': 'STOPPED', '0x1': 'TRANSITIONING', '0x2': 'TRANSITIONING',
           '0x3': 'PLAYING', '0x4': 'PAUSED_PLAYBACK'}


# ---------------------------------------------------------------- 动作
def play(src, meta=''):
    """设定播放源并开始播。src 可以是 http(s) URL，或 /tmp 下的本地文件。"""
    if via601():
        target, note = src, ''
        if not (src.startswith('http://') or src.startswith('https://')):
            # ★ 本地文件必须过 `_stage601` 补长 —— 这是设备固件的行为不是我们的选择：
            #   `threadGetPos` 里 `duration > 15000 && (duration − curPos) > 15000`
            #   才跳过 Next ⇒ 总长不到 15 秒的轨**立刻**触发 Next（实测 190ms），
            #   而够长的轨也在**最后 15 秒**被切。不补 = 音乐结尾永远被吃掉一截。
            #   ★ 补的是"人声结束 + PAD_SECS(45)"，所以长曲子尾部会多一段静音，
            #     设备的守卫落在静音里 —— 那正是想要的（它不会去掐正在响的部分）。
            target, note = dlna._stage601(src)
        uri = _src(target)
        if note:
            print('   ♪ 601 准备：%s' % note)
        dlna._play601(uri)
        _last['src'] = src
        return uri
    uri = _src(src)
    _soap('AVTransport', AV, 'SetAVTransportURI',
          '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>'
          '<CurrentURIMetaData>%s</CurrentURIMetaData>' % (uri, meta))
    _soap('AVTransport', AV, 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
    _last['src'] = src
    return uri


def pause():
    """★ 601 模式下**没有"暂停"这条命令**，这里退化成"停"，而且是刻意的：
      设备侧只有 `play601.sh` 的 play/stop 两个动作（设备上曾有三个 dbus 实验脚本，
      都已被它取代）。真暂停要发 0x602 —— CC 自己一直在发，但**我们没验过**，
      而验它按规矩得用静音轨（见 memory「静音轨实验法」）。
      ⇒ 在那之前，宁可"停"也不假装暂停成功：DLNA 那一版的全部教训就是
        **每一环都报成功、主人在屋里等一个永远不会响的音箱**。
      ⇒ 调用方（`spk_skills` 的 `pause_audio`）必须如实告诉主人：接着放是从头放。
    """
    if via601():
        return dlna._stop601()
    _soap('AVTransport', AV, 'Pause', '<InstanceID>0</InstanceID>')


def resume():
    """继续播。★ 601 模式下是【从头再放】—— 位置没有存的地方（见 `pause()`）。"""
    if via601():
        if not _last['src']:
            raise RuntimeError('不知道刚才放的是什么，没法接着放')
        return play(_last['src'])
    _soap('AVTransport', AV, 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')


def stop():
    if via601():
        return dlna._stop601()
    _soap('AVTransport', AV, 'Stop', '<InstanceID>0</InstanceID>')


def seek(seconds):
    """跳到第 N 秒。★ 601 模式下发不了（要 0x604，见 `pause()` 那段）⇒ 明确报错，
    绝不静默变成 no-op —— "看起来成功其实没动"是这个项目最怕的形状。"""
    if via601():
        raise RuntimeError('601 出口没有定位能力（0x604 未验证）；'
                           '要 0x604 得先把设备侧的脚本补上')
    _soap('AVTransport', AV, 'Seek',
          '<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>'
          '<Target>%02d:%02d:%02d</Target>' % (seconds // 3600, seconds % 3600 // 60, seconds % 60))


def status():
    """当前状态：STOPPED / PLAYING / PAUSED_PLAYBACK / TRANSITIONING_* """
    if via601():
        # ★ 判据必须来自**被控方自己**报的状态（设备 ihwplayer 日志里的 music_st），
        #   不是我们的发送返回码 —— `spk_ctl.py` 文件头那条"判成败要读被控方的日志"
        #   的教训，在这里是同一个道理。
        #   读不到回 '?'（与 DLNA 版一致），调用方（`spk_alarm` 的响铃循环）看到
        #   非 PLAYING 就收工 —— 「读不到就放行」在这里的方向是安全的：大不了早停。
        try:
            st = dlna._play_and_mute()[0]
        except Exception as e:                          # noqa: BLE001
            print('   （问播放状态没问成：%s: %s）' % (type(e).__name__, e))
            return '?'
        return _ST_MAP.get(st, '?')
    r = _soap('AVTransport', AV, 'GetTransportInfo', '<InstanceID>0</InstanceID>')
    m = re.search(r'<CurrentTransportState>(\w+)</CurrentTransportState>', r)
    return m.group(1) if m else '?'


def now_playing():
    """(当前 URI, 已播秒数, 总秒数)。"""
    r = _soap('AVTransport', AV, 'GetPositionInfo', '<InstanceID>0</InstanceID>')
    uri = re.search(r'<TrackURI>(.*?)</TrackURI>', r)
    dur = re.search(r'<TrackDuration>([\d:]+)</TrackDuration>', r)
    pos = re.search(r'<RelTime>([\d:]+)</RelTime>', r)

    def sec(s):
        if not s or s == 'NOT_IMPLEMENTED':
            return None
        try:
            h, m, x = s.split(':')
            return int(h) * 3600 + int(m) * 60 + int(x)
        except Exception:
            return None
    return (uri.group(1) if uri else None, sec(pos.group(1) if pos else None),
            sec(dur.group(1) if dur else None))


def volume(n=None):
    """不带参数 = 读当前音量；带 0~100 = 设定。
    ⚠️ 已知限制（2026-09-21 实测）：SetVolume 返回成功、但设备报的值不变。
       这台设备的 DLNA 音量是【影子值】，不驱动真实输出 —— 与既有结论一致
       （"DLNA 推流完全绕过设备音量"）。真实音量的两个杠杆见 README 注释。"""
    if n is None:
        r = _soap('RenderingControl', RC, 'GetVolume',
                  '<InstanceID>0</InstanceID><Channel>Master</Channel>')
        m = re.search(r'<CurrentVolume>(\d+)</CurrentVolume>', r)
        return int(m.group(1)) if m else None
    n = max(0, min(100, int(n)))
    _soap('RenderingControl', RC, 'SetVolume',
          '<InstanceID>0</InstanceID><Channel>Master</Channel>'
          '<DesiredVolume>%d</DesiredVolume>' % n)
    return n


def volume_effective():
    """真实输出音量的两个杠杆（DLNA 音量管不着它）：
      1. 合成 mp3 时的软件增益 —— 我们完全可控，不走设备；语音助手的音量档就是它
      2. 设备 ALSA 的 DAC volume（numid19）—— 要 adb/root，是 DAC 那一级的真音量
    这里如实返回"当前 DLNA 音量 vs 设备实际"，避免把影子值当成真的。
    ★ 别拿 headphone/lineout volume 当音量：实测挪 43dB 喇叭那头只动 0.7dB（装饰）。
      夜间禁声走的是输出通断开关（numid105/106），不是音量，见 device/nightmute.sh。"""
    return {'dlna': volume(), 'device': '需 amixer 查（见 netease-vbox-night-quiet）'}


# ---------------------------------------------------------------- 自测
if __name__ == '__main__':
    import json
    base, udn = target()
    print('找到音箱: %s   udn=%s' % (base, udn))
    print('状态    : %s' % status())
    print('正在播  : %s' % (now_playing(),))
    print('DLNA音量: %s   （⚠ 影子值，不驱动真实输出）' % volume())
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'play' and len(sys.argv) > 2:
            print('播放    : %s' % play(sys.argv[2]))
        elif cmd == 'pause':
            pause(); print('已暂停')
        elif cmd == 'resume':
            resume(); print('已继续')
        elif cmd == 'stop':
            stop(); print('已停止')
        elif cmd == 'vol' and len(sys.argv) > 2:
            print('设音量  : %s' % volume(int(sys.argv[2])))
        print('之后状态: %s' % status())
