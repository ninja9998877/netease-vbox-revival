#!/usr/bin/env python3
"""spkbrain 服务端 —— 音箱的"脑子"跑在大脑那台机上。

       音箱侧 spkclient ──NDJSON/TCP:9999──> 本进程
                                              ├ 聚合 dbus-monitor 的行 → 认出唤醒/识别结果
                                              ├ DeepSeek（带会话记忆）
                                              ├ edge-tts + loudnorm
                                              └ DLNA 推回音箱，让它自己念出来

为什么解析放在这边（而不是音箱上）：音箱的 NAND 写入寿命有限、重启会打断服务、
协议一变不该重新部署音箱。所以音箱侧只当哑管道，一个字节都不解析。

★ 为什么不用 443 MITM（spkbrain-macmini.py 那条路）：实测音箱对 vbox-server 校验证书，
  我们一被连就被 bad_certificate 拒。它里面那套 LLM 层（sys_prompt/intent_note/hist_*）
  是踩了很多坑写出来的，本文件直接把它们加载复用，一个字都不改。
"""
import importlib.util
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time

from _cfg import lan_networks      # 白名单网段（见 config/spk_config.py 的 LAN_CIDR）

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN = ('0.0.0.0', int(os.environ.get('SPK_PORT', '9999')))
DRY = os.environ.get('SPK_DRY') == '1'          # 1 = 只打印不播（调试用）
# 只让自家网进来：LAN(私有网段，见 `config/spk_config.py` 的 `LAN_CIDR`) 和点对点组网段(100.64.0.0/10)。
# ★ 必须真做 CIDR 判断，不能拿字符串前缀凑：组网段是 100.64.0.0/10，
#   覆盖 100.64–100.127，音箱自己也落在这一段里 —— 用 '100.64.' 前缀匹配
#   会把它一并拒掉（我第一版就是这么写的，日志里明晃晃一条"拒绝非自家网来源"）。
ALLOW = lan_networks()


def allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in ALLOW)


def log(m):
    print("%s %s" % (time.strftime('%H:%M:%S'), m), flush=True)


# ---------------------------------------------------------------- 复用现成的两个模块
# 文件名带连字符的没法直接 import，用 importlib 按路径加载。
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


brain = _load('brain', os.path.join(HERE, 'spkbrain-macmini.py'))   # LLM/记忆/TTS 那套
dlna = _load('dlna', os.path.join(HERE, 'spk_ai_dlna.py'))          # SSDP + SOAP + say()


# ---------------------------------------------------------------- DBus 消息聚合
# dbus-monitor 的输出长这样（一条消息 = 一行 header + 若干行缩进的 body）：
#   signal time=... sender=:1.8 -> destination=(null destination) serial=1302 \
#       path=/netease/ihw/controller; interface=netease.ihw.SmartAudio; member=Notify
#      uint32 0
#      uint32 4294967295
#      uint32 2570
#      string "{...}"          ← ★ string 里可能含换行，会占好几行
# 所以不能按行独立解析：攒着，等【下一个 header 出现】才把上一条交出去。
# ★ 注意 destination 有两种写法：点名服务时是 `netease.ihw.bt`，
#   而广播是 `(null destination)` —— 带空格！用 \S+ 会匹配失败，
#   于是整条 Notify 被静默丢掉（我一开始就是这么写的，统计里 Notify 一条都没有才发现）。
HDR = re.compile(
    r'^(signal|method call|method return|error)\s+time=([\d.]+)\s+'
    r'sender=(\S+)\s+->\s+(.+?)\s+serial=(\d+)\s+'
    r'path=(\S+?);\s+interface=(\S+?);\s+member=(\S+)')
U32 = re.compile(r'^\s+uint32\s+(\d+)\s*$')
STR = re.compile(r'^\s+string\s+"(.*)$')


def parse_msg(lines):
    """把一条消息的行列表解析成 dict。解析不动的地方原样留着，别丢信息。"""
    m = HDR.match(lines[0])
    if not m:
        return None
    kind, ts, sender, dest, serial, path, iface, member = m.groups()
    body, i = [], 1
    while i < len(lines):
        ln = lines[i]
        u = U32.match(ln)
        if u:
            body.append(int(u.group(1)))
            i += 1
            continue
        s = STR.match(ln)
        if s:
            # string 可能跨行（JSON 里的换行会被原样打出来）。已实测的两种：
            #   单行:  `   string "{...}"`     → 首段就以 " 收尾
            #   多行:  `   string "{` / `\t"year":\t126` / `}"`  → 攒到某行以 " 收尾
            parts = [s.group(1)]
            while not parts[-1].endswith('"') and i + 1 < len(lines):
                i += 1
                parts.append(lines[i])
            raw = '\n'.join(parts)
            if raw.endswith('"'):
                raw = raw[:-1]
            try:
                body.append(json.loads(raw))
            except Exception:
                body.append(raw)
            i += 1
            continue
        i += 1
    return {'kind': kind, 'ts': float(ts), 'sender': sender, 'dest': dest,
            'path': path, 'iface': iface, 'member': member, 'body': body}


def svc_of(path):
    return path.rsplit('/', 1)[-1]


# ---------------------------------------------------------------- 脑子
SPEAKING = threading.Event()        # 正在出声（用于判断"说话中又被叫"）
LAST_UTTER = [0.0]


def reply(text, why=''):
    """出嗓子：edge-tts → loudnorm → DLNA 推给音箱念。

    ★ 夜间禁声（21:00–07:00，见 spk_ai_dlna.py 的 in_night）：连 TTS 都不生成 ——
      生成要调 edge-tts，是 2~3 秒的网络往返，夜里白白耗掉没意义。
      say() 那边还有一道闸门兜着，这里是省事 + 让日志说人话。
    """
    text = (text or '').strip()
    if not text:
        return
    if dlna.NIGHT_QUIET and dlna.in_night():
        log("  ★ 夜间禁声，这句不念（已记进历史，明早能接上）：%s" % text)
        brain.hist_add('assistant', text)
        LAST_UTTER[0] = time.time()
        return
    log("  → 念：%s%s" % (text, ('  (%s)' % why) if why else ''))
    if DRY:
        log("  (SPK_DRY=1，不真播)")
        return
    try:
        mp3 = brain.make_mp3(text, '/tmp/spk_reply.mp3')
        SPEAKING.set()
        ok = dlna.say(mp3)
        log("  %s" % ('★ 音箱念出来了' if ok else '✗ 没推成功'))
    except Exception as e:
        log("  ✗ 出声失败 %s: %s" % (type(e).__name__, e))
    finally:
        SPEAKING.clear()
        brain.hist_add('assistant', text)     # ★ 记的是"音箱实际说出去的话"
        LAST_UTTER[0] = time.time()


def on_question(q, extra=None):
    """收到一句识别出来的话：问 DeepSeek，然后念出去。"""
    q = (q or '').strip()
    if not q:
        return
    log("★ 听到：%s" % q)
    brain.hist_add('user', q)
    note = ''
    if extra:
        try:                                   # 网易判定的意图（有就喂给模型，没有就算了）
            note = brain.intent_note(extra.get('cmd'), extra)
        except Exception:
            pass
    try:
        t0 = time.time()
        ans = brain.ask_llm(q, note)
        log("  DeepSeek 用了 %.1fs" % (time.time() - t0))
    except Exception as e:
        log("  ✗ LLM 失败 %s: %s" % (type(e).__name__, e))
        return
    threading.Thread(target=reply, args=(ans,), daemon=True).start()


# ★ 服务ID 表（2026-09-20 从 Notify 的 body[0] 实测读出，不是猜的）：
#   0=controller 1=alarm 3=voice_engine 4=player 6=ota 7=wifi 8=bt 9=kplayer 11=splayer 18=skins
#   DBus 侧连接：controller=:1.8  voice_engine=:1.11  bt=:1.6  alarm=:1.2 …
# 发命令时第二个 uint32 是【目标位掩码 1<<服务ID】：给 voice_engine 就是 1<<3=8。
SVC = {0: 'controller', 1: 'alarm', 3: 'voice_engine', 4: 'player', 6: 'ota',
       7: 'wifi', 8: 'bt', 9: 'kplayer', 11: 'splayer', 18: 'skins'}

# 已知的周期性噪音：cmd != 0 但只是例行上报，不值得打日志。
# 0xf100 = alarm 每秒问 controller 一次；0x0a0a = controller 每 30 秒上报音乐状态；
# 0x200d = bt 回蓝牙地址；0x200c = controller 问蓝牙地址。
NOISE = {0xf100, 0x0a0a, 0x200d, 0x200c, 0x0a14}
noise_seen = {}


# 我们【看懂】的成员只有这两个：Notify（signal）和 API（method call），
# 它们的 body[2] 都是 cmd。别的成员签名不一样，不许拿同一条规则去猜。
KNOWN = {'Notify', 'API'}
# 总线上"有连接加入/离开"这一类。偶尔出现是有用的 —— 它能告诉你设备上哪个服务
# 重启了/崩了；但不该每次都喊。这类走低频档：第一次 + 之后每 60 次提一句。
QUIET_MEMBERS = {'NameAcquired', 'NameLost', 'NameOwnerChanged'}
quiet_seen = {}


def on_event(msg):
    """DBus 上看到一条消息。

    ★ 判据只有一条：Notify/API 的 body[2] 就是 cmd，
      cmd==0 且 json 带 PING 就是心跳（各服务每 5 秒一次）——其余都是真事件。
      voice_engine 的事件（唤醒/识别结果）就藏在这里，等一句话就能现形。

    ★ 不认识的成员原样打出来，不套 cmd 规则。实测踩到：controller 上有 GOAPI，
      它的 body 是 [3, 1, 1789915243, 528708, 2580, 14] —— 那个"第 3 个整数"
      是【时间戳】，按 cmd 打就成了 cmd=0x6aaff06b 这种看着像回事、其实唬人的值。
    """
    body = msg['body']
    svc = svc_of(msg['path'])
    if msg['member'] not in KNOWN:
        if msg['member'] in QUIET_MEMBERS:
            k = msg['member']
            quiet_seen[k] = quiet_seen.get(k, 0) + 1
            if quiet_seen[k] % 60 == 1:
                log("(例行) %s %s ×%d" % (svc, k, quiet_seen[k]))
            return
        log("★陌生成员 [%s] %s %s  %r" % (svc, msg['kind'], msg['member'], body))
        return
    if len(body) < 3 or not isinstance(body[2], int):
        return
    cmd = body[2]
    if cmd == 0:
        return                                  # 心跳
    js = next((x for x in body if isinstance(x, (dict, str))), None)
    if cmd in NOISE:
        noise_seen[cmd] = noise_seen.get(cmd, 0) + 1
        if noise_seen[cmd] % 60 == 1:           # 只偶尔提一句，证明链路活着
            log("(例行) %s cmd=0x%04x ×%d" % (svc, cmd, noise_seen[cmd]))
        return
    # ★ 非例行事件：全打出来。识别结果长什么样就看这里。
    log("★事件 [%s] %s cmd=0x%04x  %s" % (svc, msg['member'], cmd,
          json.dumps(js, ensure_ascii=False) if js is not None else body))


# ---------------------------------------------------------------- TCP 服务
def handle(conn, addr):
    ip = addr[0]
    if not allowed(ip):
        log("拒绝非自家网来源 %s" % ip)
        conn.close()
        return
    log("音箱连上了 %s:%d" % (ip, addr[1]))
    f = conn.makefile('rwb')
    pending = []          # 攒着当前这条消息的行
    n = 0

    def flush():
        if pending:
            msg = parse_msg(pending)
            if msg:
                on_event(msg)
        del pending[:]

    while True:
        line = f.readline()
        if not line:
            break
        try:
            ev = json.loads(line.decode('utf-8', 'replace'))
        except Exception:
            continue
        t = ev.get('t')
        if t == 'dbus':
            ln = ev.get('l', '')
            if HDR.match(ln):
                flush()               # ★ 见到新 header 才把上一条交出去
                pending.append(ln)
            elif pending:
                pending.append(ln)
        elif t == 'hello':
            log("hello: bus=%s pid=%s" % (ev.get('bus'), ev.get('pid')))
        elif t == 'ping':
            n += 1
            if n % 15 == 1:
                log("心跳 #%d（链路正常）" % n)
    flush()
    log("音箱断开了 %s" % ip)
    conn.close()


def serve():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(4)
    log("监听 %s:%d%s" % (LISTEN[0], LISTEN[1], '  [DRY 只打印不播]' if DRY else ''))
    while True:
        c, a = srv.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()


if __name__ == '__main__':
    if not brain.DS_KEY:
        log("!! 没读到 DeepSeek key（DS_KEY / %s / settings.json 三处都没有）" % brain.KEYFILE)
        sys.exit(1)
    log("key 已载入（长度 %d，不打印内容）" % len(brain.DS_KEY))
    serve()
