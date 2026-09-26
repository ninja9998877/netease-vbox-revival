#!/usr/bin/env python3
"""mictap 接收端 —— 接住音箱那边 mictap.so 截出来的麦克风音频。

    音箱 netease_voice ──snd_pcm_readi──┬──> 真 ALSA（它自己照常用）
      （被 mictap.so 钩住）             └──UDP:9998──> 本进程

包格式（32 字节头 + 裸 PCM，全部小端）：
    u32 magic('MTAP'=0x5041544d) ver seq frames rate ch fmt flags

★ 为什么头里要带 rate/ch/fmt：这样接收端不用猜。设备是 2ch@96k 还是别的，
  让它自己报 —— 我们猜错一次就是几个小时的"听不清"排查。
★ 不引第三方库：这是要长期常驻的小服务，标准库够了。

用法：
    python3 mictap_sink.py                    # 只看统计
    python3 mictap_sink.py --wav /tmp/mic.wav # 同时存成 wav
    python3 mictap_sink.py --secs 10          # 跑 10 秒自动退出（测试用）
"""
import argparse
import ipaddress
import os
import socket
import struct
import sys
import time
import wave

from _cfg import lan_networks      # 白名单网段（见 config/spk_config.py 的 LAN_CIDR）

PORT = int(os.environ.get('MICTAP_PORT', '9998'))
MAGIC = 0x5041544D
HDR = struct.Struct('<8I')      # magic ver seq frames rate ch fmt flags

# ★ 只收自家网：跟 server.py 同一条规矩（私有网段，见 `config/spk_config.py` 的 `LAN_CIDR`）。
#   这是麦克风音频，哪怕在局域网里也不该谁都能往这儿灌。
# ★ 回环也算自家：常驻耳朵 spk_ear.py 独占 9998 之后会把每个包【原样转发一份到
#   127.0.0.1:9999】，say() 的自听耳朵就退到那儿。源地址是 127.0.0.1，
#   不在白名单里的话它会把转发的包当成外人全丢掉——症状正是"9998 明明在收，
#   9999 一个包都没有"。回环比局域网更可信，放进来不放松任何边界。
ALLOW = lan_networks()

# ALSA snd_pcm_format_t → 每样本字节数（跟 mictap.c 的 fmt_width 是同一张表）
FMT_WIDTH = {0: 1, 1: 1, 2: 2, 3: 2, 4: 2, 5: 2, 6: 4, 7: 4, 8: 4, 9: 4,
             10: 4, 11: 4, 12: 4, 13: 4, 14: 4, 15: 4, 16: 8, 17: 8,
             18: 4, 19: 4, 32: 3, 33: 3, 34: 3, 35: 3, 36: 3, 37: 3, 38: 3, 39: 3}
FMT_NAME = {0: 'S8', 1: 'U8', 2: 'S16_LE', 3: 'S16_BE', 4: 'U16_LE', 5: 'U16_BE',
            6: 'S24_LE', 7: 'S24_BE', 8: 'U24_LE', 9: 'U24_BE',
            10: 'S32_LE', 11: 'S32_BE', 12: 'U32_LE', 13: 'U32_BE',
            14: 'FLOAT_LE', 15: 'FLOAT_BE', 32: 'S24_3LE', 33: 'S24_3BE'}

# 包头 flags 的约定（跟 mictap.c 的 MT_F_DECIM / MT_RATE_SH 是同一套）
F_DECIM = 1
RATE_SH = 8


def allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in ALLOW)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wav', help='把收到的音频存成 wav（不指定就只统计）')
    ap.add_argument('--secs', type=float, default=0, help='跑这么多秒就退出（0=一直跑）')
    ap.add_argument('--port', type=int, default=PORT)
    args = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # 收包缓冲开大点：96kHz 立体声是 384KB/s，默认 208KB 会丢
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind(('0.0.0.0', args.port))

    wf = None
    fmt_seen = None
    pkts = 0
    bytes_in = 0
    lost = 0
    # ★ 序号必须按【来源】分开记。同一个 IP 上跑两个发送端（比如网易那边还挂着
    #   旧 .so，同时我在用 arecord 做测试）时，两条流的序号混在一起算 gap，
    #   会得出"丢了 429 亿个包"这种天文数字 —— 踩过。
    srcseq = {}
    first_t = None
    last_t = None
    reported_fmt = set()
    rejected = set()
    t_switch = 0.0

    print('mictap_sink 监听 UDP :%d  %s' % (
        args.port, ('→ %s' % args.wav) if args.wav else '（只统计）'), flush=True)

    # 用超时轮询，这样 --secs 和统计行都能按时走
    s.settimeout(1.0)
    t_next_report = time.time() + 2.0

    while True:
        if args.secs and first_t and time.time() - first_t > args.secs:
            break
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            data = None

        if data:
            if not allowed(addr[0]):
                if addr[0] not in rejected:
                    rejected.add(addr[0])
                    print('  ✗ 拒绝非自家网来源 %s' % addr[0], flush=True)
                data = None

        if data:
            if len(data) < HDR.size:
                continue
            magic, ver, seq, frames, rate, ch, fmt, flags = HDR.unpack_from(data)
            if magic != MAGIC:
                continue

            pkts += 1
            payload = data[HDR.size:]
            bytes_in += len(payload)
            now = time.time()
            if first_t is None:
                first_t = now
                print('  ★ 第一个包到了（来自 %s）' % addr[0], flush=True)
            last_t = now

            prev = srcseq.get(addr)
            if prev is not None:
                gap = (seq - prev) & 0xFFFFFFFF
                if 1 < gap < 100000:          # 大到离谱的"gap"是两条流串了，不算丢包
                    lost += gap - 1
            srcseq[addr] = seq

            key = (rate, ch, fmt)
            if key not in reported_fmt:
                reported_fmt.add(key)
                extra = ''
                if flags & F_DECIM:
                    extra = '  ← 滤过+抽过，原始 %d Hz' % (flags >> RATE_SH)
                print('  格式：%d Hz / %d 声道 / %s（fmt=%d，每样本 %d 字节）%s' % (
                    rate, ch, FMT_NAME.get(fmt, '?'), fmt, FMT_WIDTH.get(fmt, 2), extra),
                    flush=True)

            want = (rate, ch, fmt)
            if fmt_seen != want:
                if wf:
                    wf.close()
                    # 两个发送端交替时这里会疯狂刷屏 —— 一秒最多说一次
                    if now - t_switch > 1.0:
                        t_switch = now
                        print('  ★ 格式变了，wav 另起一个：%s' % args.wav, flush=True)
                fmt_seen = want
                if args.wav:
                    wf = wave.open(args.wav, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(FMT_WIDTH.get(fmt, 2))
                    wf.setframerate(rate)
            if wf:
                wf.writeframes(payload)

        if time.time() >= t_next_report:
            t_next_report = time.time() + 2.0
            if pkts:
                span = max(0.001, (last_t or 0) - (first_t or 0))
                print('  %d 包 / %d 字节 / 丢 %d / 约 %.0f KB/s%s' % (
                    pkts, bytes_in, lost, bytes_in / span / 1024.0,
                    '' if (last_t and time.time() - last_t < 3) else '  ⚠ 已静默 %.1fs' % (
                        time.time() - last_t)), flush=True)
            elif first_t is None and args.secs:
                print('  …还没收到任何包', flush=True)

    if wf:
        wf.close()
    print('收尾：%d 包 / %d 字节 / 丢 %d' % (pkts, bytes_in, lost), flush=True)
    if pkts and first_t:
        span = max(0.001, last_t - first_t)
        print('跨 %.1fs，平均 %.0f KB/s' % (span, bytes_in / span / 1024.0), flush=True)
    return 0 if pkts else 1


if __name__ == '__main__':
    sys.exit(main())
