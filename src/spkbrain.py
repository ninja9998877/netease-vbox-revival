#!/usr/bin/env python3
"""音箱大脑 —— 把网易三音云音箱接到你自己的云端 LLM。

一台小盒子（网口接局域网 + 2.4G 无线开热点 + 蓝牙）上跑这一个程序，
从蓝牙配网开始全自动，你只需要插电、插网线：

  ① 蓝牙找到音箱 → 把【本盒子热点】的 SSID/密码推给它
  ② 音箱连上本盒子的热点 ⇒ 本盒子就成了它的网关和 DNS
  ③ 盒子的 DNS 把 vbox-asr / vbox-tts 指回自己
  ④ 音箱唤醒 → 音频进盒子 → 网易识别出文字 → 问 DeepSeek → 合成语音
     → 把"该念什么"替换掉，让它念出来

为什么用「盒子开热点」而不是改家里的路由器：
  音箱一连上本盒子的热点，它的网关和 DNS 就全是我们的了 ——
  不必碰上游路由器、不必知道家里的 WiFi 密码、不必 ARP 欺骗。
  （音箱只支持 2.4G，所以盒子用网口上网、2.4G 无线开热点，两条路互不打架。）

分工（这一条是主人定的）：
  网易答得上来的（放歌、暂停、报时间）→ 原样放行，连语音都转发真服务器取原版；
  网易答不上来的（"我暂时听不懂"）    → 换成 DeepSeek 的；
  网易压根不吭声的                    → 把响应改写掉，逼它开口念 DeepSeek 的。
"""
import asyncio
import base64
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

# ─────────────────────────────── 配置 ───────────────────────────────
# 全部可用环境变量覆盖；写死在文件里只是为了少配一样东西。
CFG = {
    'ap_ssid':   os.environ.get('SPK_AP_SSID', 'SpeakerBrain'),   # 盒子开的热点名
    'ap_pwd':    os.environ.get('SPK_AP_PWD', 'spk12345678'),     # 音箱配网要用，≥8位
    'self_ip':   os.environ.get('SPK_SELF_IP', '192.168.50.1'),   # 盒子在热点这侧的 IP
    'ap_iface':  os.environ.get('SPK_AP_IFACE', 'wlan0'),         # 拿来开热点的无线网卡
    # nm  = 热点的 DHCP/DNS 交给 NetworkManager 自带的 dnsmasq（劫持写在 dnsmasq-shared.d 里）—— 默认
    # own = 自己答 DNS（hostapd 等场合，没人替我们答时用）
    'dns':       os.environ.get('SPK_DNS', 'nm'),
    'upstream':  os.environ.get('SPK_UPSTREAM', '223.5.5.5'),     # own 模式下其他域名转发给它
    'ble_name':  os.environ.get('SPK_BLE_NAME', '三音云音箱'),     # 按名字前缀认音箱（MAC 每台不同）
    'cert':      os.environ.get('SPK_CERT', '/etc/spkbrain/spoof.pem'),
    'keyfile':   os.environ.get('SPK_KEYFILE', '/etc/spkbrain/deepseek.key'),
    'ds_base':   os.environ.get('DS_BASE', 'https://api.deepseek.com/anthropic'),
    'ds_model':  os.environ.get('DS_MODEL', 'deepseek-v4-flash'),
    'voice':     os.environ.get('SPK_VOICE', 'zh-CN-liaoning-XiaobeiNeural'),
    'edge_tts':  os.environ.get('SPK_EDGE_TTS', 'edge-tts'),
    'rewrite_wait': float(os.environ.get('SPK_REWRITE_WAIT', '8')),  # 改写最多等 LLM 多久
}
# 只劫持这两个。别的网易域名（LBS、link 等）照常放行 —— 音箱靠它们做正常启动流程。
HIJACK = ('vbox-asr.3.163.com', 'vbox-tts.3.163.com')
UP_PORT = 443
CACHE = {}                 # responseid -> {'ev':Event,'mp3':bytes,'reply':str}
LOCK = threading.Lock()

# 网易答不上来时蹦的那几句。命中 ⇒ 这次轮到 DeepSeek 说话。
# ★ 只收"听不懂/超出能力"这类明确的认输话，绝不能把"我好像没找到这个时间的闹钟"
#   这种正经的功能性回答卷进来 —— 那是网易答对了，该听它的。
FALLBACK_PAT = re.compile(
    r'听不懂|没听清|没听懂|听不太清|听不清|超出.{0,6}能力|奋力追赶|不能理解|'
    r'不明白你|无法回答|还不会|换个说法|没学会|暂时无法')


# ---------------------------------------------------------------- 会话记忆
# 语音助手跟聊天窗口不一样，两件事必须一起解决：
#   ① 得记得住 —— 不然"那它呢""还有呢""这首叫什么"这类追问全废（每句都当全新的一句问）；
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


def log(m):
    with LOCK:
        print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def load_key():
    k = (os.environ.get('DS_KEY') or '').strip()
    if k:
        return k
    try:
        with open(CFG['keyfile']) as f:
            return f.read().strip()
    except Exception:
        return None


DS_KEY = load_key()


# ═══════════════════════════ 一、蓝牙配网 ═══════════════════════════
# 协议从官方 APK 反编译 + 实测双重确认：
#   12 字节小端头 magic(0xEF) ver(1) rev(0) seq cmdid(u16) checksum(u16) length(u32)
#   checksum = 全头(校验位先填 0)+payload 逐字节和取 u16
#   payload  = {"ssid":"<base64>","pwd":"<base64>","account":""}
#             ★ 键名是 pwd 不是 password，SSID 和密码都必须 Base64 —— 错了设备回 0xF004
CMD_WIFI_REQUEST = 4097
CMD_WIFI_RESPONSE = 4098
LAST_SEQ = 255
GATT_SVC = '0000fff0-0000-1000-8000-00805f9b34fb'
GATT_WRITE = '0000fff1-0000-1000-8000-00805f9b34fb'
GATT_NOTIFY = '0000fff2-0000-1000-8000-00805f9b34fb'
BLE_CODES = {
    0: '成功', 0xF003: 'JSON 格式错误', 0xF004: '参数错误（键名/Base64 写错了）',
    0xF005: '找不到该 SSID（格式对，是热点没开或不在 2.4G）', 0xF006: 'WiFi 密码错',
    0xF007: 'WiFi 忙', 0xF008: '连接超时', 0xF011: '账号错误', 0xFFFF: '失效',
}


def ble_frame(seq, cmdid, payload=b''):
    ln = 12 + len(payload)
    h = bytearray([0xEF, 1, 0, seq]) + struct.pack('<H', cmdid) + b'\x00\x00' \
        + struct.pack('<I', ln)
    h[6:8] = struct.pack('<H', (sum(h) + sum(payload)) & 0xFFFF)
    return bytes(h) + payload


def parse_ble_frame(buf):
    """返回 (seq, cmdid, payload)；buf 不够一个完整帧就返回 None。"""
    if len(buf) < 12 or buf[0] != 0xEF:
        return None
    ln = struct.unpack('<I', buf[8:12])[0]
    if ln < 12 or len(buf) < ln:
        return None
    return buf[3], struct.unpack('<H', buf[4:6])[0], bytes(buf[12:ln])


def ap_has_client(iface):
    """热点的无线侧有没有设备连上来？有 ⇒ 音箱已经配好网了，就不必再扫蓝牙。

    ★ 这不只是省事：盒子多半是 WiFi/蓝牙二合一的芯片，蓝牙一直扫会和热点抢射频，
      热点会变慢。已经连上的时候就别扫了。
    """
    try:
        out = subprocess.run(['iw', 'dev', iface, 'station', 'dump'],
                             capture_output=True, text=True, timeout=5).stdout
        return 'Station' in out
    except Exception:
        return False


async def provision(ssid, pwd, name_pat, iface):
    """蓝牙找到音箱，把热点的 SSID/密码推给它。

    找不到就一直等（音箱不进配网模式，谁也推不进去），但**不空转扫**：
    发现音箱已经连上热点了就歇两分钟再看。
    """
    from bleak import BleakClient, BleakScanner
    log('蓝牙：等「%s」进配网模式（长按音箱配网键到指示灯闪）' % name_pat)
    while True:
        if iface and ap_has_client(iface):
            await asyncio.sleep(120)
            continue
        try:
            found = await BleakScanner.discover(timeout=6.0, return_adv=True)
        except Exception as e:
            log('蓝牙：扫描出错 %r，10 秒后再来' % e)
            await asyncio.sleep(10)
            continue
        dev = nm = None
        for d, adv in found.values():
            nm = adv.local_name or d.name or ''
            if name_pat in nm:
                dev = d
                break
        if dev is None:
            await asyncio.sleep(30)
            continue
        log('蓝牙：找到 %s（%s），开始连' % (nm, dev.address))

        pay = json.dumps({
            'ssid': base64.b64encode(ssid.encode()).decode(),
            'pwd': base64.b64encode(pwd.encode()).decode(),
            'account': '',
        }, ensure_ascii=False).encode()

        got = []
        for attempt in range(1, 6):
            try:
                def on_notify(_h, data):
                    f = parse_ble_frame(bytearray(data))
                    if f and f[1] == CMD_WIFI_RESPONSE:
                        got.append(json.loads(f[2].decode('utf-8', 'replace') or '{}'))

                async with BleakClient(dev, timeout=15.0) as cli:
                    await cli.start_notify(GATT_NOTIFY, on_notify)
                    log('蓝牙：已连上（第 %d 次尝试），推配置' % attempt)
                    await cli.write_gatt_char(
                        GATT_WRITE, ble_frame(LAST_SEQ, CMD_WIFI_REQUEST, pay), response=False)
                    for _ in range(40):                  # 最多等 20 秒
                        if got:
                            break
                        await asyncio.sleep(0.5)
                    if got:
                        code = got[0].get('code', got[0].get('errcode', -1))
                        log('蓝牙：设备回 %s —— %s' % (code, BLE_CODES.get(code, '未知')))
                        if code == 0:
                            return True
                        break                            # 参数之类的问题，重推也没用，退出去重扫
                    # 没回 ACK 往往是好事：设备忙着去连热点了
                    log('蓝牙：推完没回话 —— 一般说明它去连热点了')
                    return True
            except Exception as e:
                # 音箱只在联网模式接受连接，扫得到不一定连得上 ⇒ 就是要重试
                log('蓝牙：第 %d 次没连上（%s），2 秒后再试'
                    % (attempt, e.__class__.__name__))
                await asyncio.sleep(2)
        await asyncio.sleep(30)


# ═══════════════════════════ 二、DNS 劫持 ═══════════════════════════
def dns_query_a(name, server=None):
    """自己拼个查询去问上游要 A 记录。

    ★ 为什么不用系统的解析器：在这台盒子上 vbox-asr / vbox-tts 已经被我们劫持成自己了，
      一个不小心就解析回自己、然后自己连自己。所以显式指定上游问。
    """
    q = b'\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
    for lab in name.split('.'):
        q += bytes([len(lab)]) + lab.encode()
    q += b'\x00\x00\x01\x00\x01'
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.sendto(q, (server or CFG['upstream'], 53))
        data = s.recvfrom(4096)[0]
    except Exception:
        return None
    finally:
        s.close()
    i = 12
    while i < len(data) and data[i]:
        i += data[i] + 1
    i += 5                                   # 跳过问题段（结尾的 0 + qtype + qclass）
    for _ in range(struct.unpack('>H', data[6:8])[0]):
        if i + 12 > len(data):
            break
        if data[i] & 0xC0 == 0xC0:           # 名字是压缩指针
            i += 2
        else:
            while i < len(data) and data[i]:
                i += data[i] + 1
            i += 1
        if i + 10 > len(data):
            break
        typ = struct.unpack('>H', data[i:i + 2])[0]
        rdlen = struct.unpack('>H', data[i + 8:i + 10])[0]
        if typ == 1 and rdlen == 4:
            return socket.inet_ntoa(data[i + 10:i + 14])
        i += 10 + rdlen
    return None


UP_IP = {}


def up_ip(host):
    """真服务器的地址，问一次记下来；连不上就把记录清掉下次重问。

    ★ 解析回本机要当成失败：那说明上游的 DNS 也在劫持同一批域名
      （比如盒子的网关就是当初做过劫持的那台），照转就成了自己连自己 ——
      自己的 443 再转发给自己，套娃。宁可这次只听 LLM 的。
    """
    ip = UP_IP.get(host)
    if not ip:
        ip = dns_query_a(host)
        if not ip:
            log('⚠ %s 解析不出来 —— 这次没法转发（检查盒子的上网）' % host)
            return None
        if ip in local_ips():
            log('⚠ %s 被上游解析成了本机(%s) —— 上游也在劫持它，转不出去；'
                '这次只能听 LLM 的，听不到网易原版' % (host, ip))
            return None
        log('上游 %s → %s' % (host, ip))
        UP_IP[host] = ip
    return ip


def local_ips():
    """本机所有 IPv4 地址。"""
    ips = {CFG['self_ip'], '127.0.0.1'}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        out = subprocess.run(['ip', '-4', '-o', 'addr', 'show'],
                             capture_output=True, text=True, timeout=5).stdout
        for ln in out.splitlines():
            f = ln.split()
            if len(f) > 3:
                ips.add(f[3].split('/')[0])
    except Exception:
        pass
    return ips


def dns_reply(query, ip):
    i = 12
    while i < len(query) and query[i]:
        i += query[i] + 1
    qend = i + 5
    if qend > len(query):
        return None
    hdr = query[:2] + b'\x81\x80' + query[4:6] + b'\x00\x01\x00\x00\x00\x00'
    ans = (b'\xc0\x0c\x00\x01\x00\x01' + struct.pack('>I', 30)
           + b'\x00\x04' + socket.inet_aton(ip))
    return hdr + query[12:qend] + ans


# 音箱是我们热点的客户，它的 DNS 天生就是盒子 —— 所以只需要一个极简 DNS：
# 问到 vbox-asr / vbox-tts 就答"我就是"，别的照常转发上游。
# （默认这个不用起：NetworkManager 的 shared 模式自带 dnsmasq，劫持写在它的 conf 里更省事。）
def dns_serve():
    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.settimeout(3)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # ★ 只绑热点这一侧的地址，不绑 0.0.0.0 —— 否则会和系统上的 systemd-resolved
    #   (127.0.0.53:53) 抢端口，起不来。音箱问的就是这个地址，够用。
    s.bind((CFG['self_ip'], 53))
    log('DNS：起来了，%s → %s；其余转发 %s'
        % ('/'.join(HIJACK), CFG['self_ip'], CFG['upstream']))
    while True:
        try:
            data, addr = s.recvfrom(1500)
            i, parts = 12, []
            while i < len(data) and data[i]:
                n = data[i]
                parts.append(data[i + 1:i + 1 + n].decode('latin1'))
                i += n + 1
            name = '.'.join(parts).lower()
            if name in HIJACK:
                r = dns_reply(data, CFG['self_ip'])
                if r:
                    s.sendto(r, addr)
                continue
            up.sendto(data, (CFG['upstream'], 53))
            try:
                s.sendto(up.recvfrom(4096)[0], addr)
            except socket.timeout:
                pass
        except Exception as e:
            log('DNS 出错: %r' % e)


# ═══════════════════════════ 三、脑子与嘴 ═══════════════════════════
def sys_prompt(note=''):
    """note 是网易判定的意图，见 intent_note()。

    ★ 这里的分寸全是踩出来的：第一版没写能力边界 ⇒ 它会随口答应做不到的事；
      第二版凭想象写了边界 ⇒ 又把「设闹钟」错列进"做不到"，于是主人让它取消闹钟时
      它答"我没有闹钟功能"，而闹钟其实真的取消了。所以现在的写法是：
      以系统判定为准 —— 不许自行否认，也不许自行断言结果。
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


CMD_DESC = {2001: '普通问答', 1002: '播放音乐', 3003: '暂停/停止音乐'}


def intent_note(cmd, det):
    """把网易判定的意图翻成一句人话喂给 LLM。

    ★ 最靠得住的线索是网易自己的 tts 字段：那是它对这次请求的意图摘要
      （"暂停" = 它要去暂停音乐；"好听的英文歌来啦" = 它要去放歌）。
      硬编码 cmd 码表只能覆盖见过的几种，用它的 tts 则天然覆盖没见过的指令。
    """
    tts = (det.get('tts') or '').strip()
    what = CMD_DESC.get(cmd) or ('意图编号 cmd=%s' % cmd if cmd is not None else '未给出意图')
    if tts:
        return ('系统判定：%s。它原本准备念的是「%s」——这件事它真的会去做，你顺着说就行。'
                % (what, tts))
    return '系统判定：%s。' % what


def ask_llm(q, note=''):
    """★ 带上最近几轮对话。没有它，"那它呢""这首叫什么""再讲一个"这类追问全接不上——
    此前每句都当成全新的一句在问。"""
    qn = hist_norm(q)
    msgs = hist_msgs()
    # judge() 已经把这句记进历史了，正常不用再补；但自检那条路是直接调这里的，
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
        CFG['ds_base'] + '/v1/messages',
        data=json.dumps({'model': CFG['ds_model'], 'max_tokens': 2000,
                         'system': sys_prompt(note),
                         'messages': msgs}).encode(),
        headers={'Content-Type': 'application/json', 'x-api-key': DS_KEY,
                 'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    return ''.join(c.get('text', '') for c in d.get('content', [])
                   if c.get('type') == 'text').strip()


def make_mp3(text, out):
    """合成语音。ffmpeg 那步做响度归一化 —— 少了它音箱听起来会很轻。"""
    raw = out + '.raw'
    subprocess.run([CFG['edge_tts'], '--voice', CFG['voice'], '--text', text,
                    '--write-media', raw], check=True, capture_output=True, timeout=60)
    p = subprocess.run(['ffmpeg', '-y', '-i', raw, '-af',
                        'loudnorm=I=-14:TP=-1.5:LRA=11', '-b:a', '128k', out],
                       capture_output=True)
    if p.returncode != 0 or not os.path.exists(out):
        os.replace(raw, out)
    else:
        os.unlink(raw)
    return out


def run_answer(q, cmd, det, slot, tag, t0=None):
    t0 = t0 or time.time()
    try:
        reply = ask_llm(q, intent_note(cmd, det))
        slot['reply'] = reply
        mp3 = make_mp3(reply, '/tmp/spk_reply_%d.mp3' % int(time.time() * 1000))
        slot['mp3'] = open(mp3, 'rb').read()
        log('%s ★ LLM 答：%r（%.1fs，语音 %d 字节）'
            % (tag, reply, time.time() - t0, len(slot['mp3'])))
    except Exception as e:
        log('%s ✗ LLM/TTS 出错: %r' % (tag, e))
    finally:
        slot['ev'].set()


# ═══════════════════════ 四、接管音箱的耳和嘴 ═══════════════════════
class WSParser:
    """流式切 WebSocket 帧，同时给出原始字节（透传用）和解出的内容（判断用）。"""

    def __init__(self):
        self.buf = b''

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 2:
                break
            b0, b1 = self.buf[0], self.buf[1]
            op, ln, off = b0 & 0x0f, b1 & 0x7f, 2
            if ln == 126:
                if len(self.buf) < 4:
                    break
                ln, off = struct.unpack('>H', self.buf[2:4])[0], 4
            elif ln == 127:
                if len(self.buf) < 10:
                    break
                ln, off = struct.unpack('>Q', self.buf[2:10])[0], 10
            if b1 & 0x80:
                if len(self.buf) < off + 4:
                    break
                off += 4
            if len(self.buf) < off + ln:
                break
            py = self.buf[off:off + ln]
            raw = self.buf[:off + ln]
            self.buf = self.buf[off + ln:]
            out.append((op, py, raw))       # 上行帧带掩码，但我们只用下行文本帧
        if len(self.buf) > 4 << 20:
            self.buf = b''
        return out


def encode_frame(op, payload):
    """构造服务端→设备的帧（不带掩码，FIN=1）。"""
    n = len(payload)
    h = bytes([0x80 | op])
    if n < 126:
        h += bytes([n])
    elif n < 65536:
        h += bytes([126]) + struct.pack('>H', n)
    else:
        h += bytes([127]) + struct.pack('>Q', n)
    return h + payload


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
    prepare —— 原样放行，但后台把 LLM 的答案备好，等设备来要语音时掉包
    rewrite —— 拦下来，把 cmd 改成 2001 逼设备开口，念 LLM 的答案
    """
    got = parse_asr(py)
    if not got:
        return 'forward'                        # ack / 中间结果，不碰
    q, rid, cmd, det, tts, _ = got
    hist_add('user', q)                         # ★ 三条路都要记，见文件头上那段说明
    log('%s ★ 它听到：%r' % (tag, q))
    log('%s   网易判定 cmd=%s tts=%r' % (tag, cmd, tts))
    if tts and not FALLBACK_PAT.search(tts):
        log('%s   ⇒ 网易答得上来的，听网易的（不干预）' % tag)
        return 'forward'
    if cmd == 2001:
        log('%s   ⇒ 网易只会说「%s」，听 LLM 的（等它来要语音时掉包）' % (tag, tts or '不吭声'))
        return 'prepare'
    log('%s   ⇒ 网易不打算开口(cmd=%s)，拦下来改写' % (tag, cmd))
    return 'rewrite'


def prepare_answer(py, tag):
    got = parse_asr(py)
    if not got:
        return
    q, rid, cmd, det, tts, _ = got
    slot = {'ev': threading.Event(), 'mp3': None, 'reply': None, 'q': q}
    if rid:
        CACHE[rid] = slot
    threading.Thread(target=run_answer, args=(q, cmd, det, slot, tag), daemon=True).start()


def build_reply_frame(py, reply):
    """把网易的响应改成"让设备念这句话"：cmd 一律 2001，tts 换成我们的回答。"""
    d = json.loads(py)
    slu = d.setdefault('slu', {})
    try:
        det = json.loads(slu.get('detail') or '{}')
    except Exception:
        det = {}
    det['cmd'] = 2001
    det['tts'] = reply
    det.pop('tts2', None)              # 别让它拐去"放音乐"那条路
    slu['cmd'] = 2001
    slu['detail'] = json.dumps(det, ensure_ascii=False)
    return encode_frame(1, json.dumps(d, ensure_ascii=False).encode())


def rewrite_and_send(py, dst, tag, lock):
    """拦下的响应：先问 LLM，再把 cmd 改成 2001 转发 —— 逼设备开口。

    ★ 安全阀：LLM 没赶上就把【原帧】放行。宁可这次它不吭声，
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
    th.join(timeout=CFG['rewrite_wait'])
    with lock:
        if slot['mp3']:
            dst.sendall(build_reply_frame(py, slot['reply']))
            log('%s   ★ 改写完成，逼它念 LLM 这句' % tag)
        else:
            dst.sendall(encode_frame(1, py))
            log('%s   ⚠ LLM 没赶上(%.1fs)，原样放行 —— 这次它不吭声'
                % (tag, time.time() - t0))


def pump(src, dst, direction, tag, lock):
    p = WSParser()
    try:
        while True:
            d = src.recv(65536)
            if not d:
                break
            if direction != 'DN':
                dst.sendall(d)                  # 上行：原样透传，一个字节不改
                continue
            frames = p.feed(d)
            out = b''
            for op, py, raw in frames:
                if op == 1:                     # 下行文本帧才可能被拦
                    what = judge(py, tag)
                    if what == 'rewrite':
                        threading.Thread(target=rewrite_and_send,
                                         args=(py, dst, tag, lock), daemon=True).start()
                        continue                # ★ 原帧丢掉，换成改写的
                    if what == 'prepare':
                        prepare_answer(py, tag)
                out += raw
            if out:
                # ★ 改写线程也在往同一个连接写帧，两边必须上同一把锁 ——
                #   否则两帧的字节能交错着写出去，WebSocket 就散了。
                with lock:
                    dst.sendall(out)
    except Exception as e:
        log('%s %s 转发结束: %r' % (tag, direction, e))


def tts_branch(ss, hdr, n):
    line = hdr.split(b'\r\n')[0].decode('latin1', 'replace')
    rid = want = ''
    try:
        ps = urllib.parse.parse_qs(urllib.parse.urlparse(line.split(' ')[1]).query)
        rid, want = ps.get('resId', [''])[0], ps.get('text', [''])[0]
    except Exception:
        pass
    slot = CACHE.get(rid)
    if slot:
        slot['ev'].wait(timeout=15)
        if slot.get('mp3'):
            log('#%d 【嘴】要念 %r ⇒ ★ 换成我们的：%r' % (n, want, slot.get('reply')))
            body = slot['mp3']
            ss.sendall(('HTTP/1.1 200 OK\r\nContent-Type: audio/mp3\r\n'
                        'Content-Length: %d\r\nAccept-Ranges: bytes\r\n'
                        'Connection: close\r\n\r\n' % len(body)).encode() + body)
            hist_add('assistant', slot.get('reply'))   # ★ 记的是音箱真念出去的那句
            return
    # 没有我们的份 ⇒ 转发真服务器，让设备听网易的原版
    log('#%d 【嘴】要念 %r ⇒ 不干预，转发真服务器' % (n, want))
    hist_add('assistant', want)                     # ★ 这句是网易答的，但音箱真念了它 ⇒ 也得记
    host = 'vbox-tts.3.163.com'
    try:
        ip = up_ip(host)
        if not ip:
            return
        up = ssl.create_default_context().wrap_socket(
            socket.create_connection((ip, UP_PORT), timeout=10), server_hostname=host)
        up.sendall(hdr)
        while True:
            d = up.recv(65536)
            if not d:
                break
            ss.sendall(d)
        up.close()
    except Exception as e:
        UP_IP.pop(host, None)                # 可能是它换 IP 了，下次重问
        log('#%d 【嘴】转发失败: %r' % (n, e))


def asr_branch(ss, hdr, n):
    tag = '#%d' % n
    lock = threading.Lock()          # 转发线程和改写线程都会往设备写帧
    up = None
    host = 'vbox-asr.3.163.com'
    try:
        ip = up_ip(host)
        if not ip:
            return
        up = ssl.create_default_context().wrap_socket(
            socket.create_connection((ip, UP_PORT), timeout=10), server_hostname=host)
        up.sendall(hdr)
        ss.sendall(read_until(up, b'\r\n\r\n'))
        t = threading.Thread(target=pump, args=(up, ss, 'DN', tag, lock), daemon=True)
        t.start()
        pump(ss, up, 'UP', tag, lock)
        t.join(timeout=5)
    except Exception as e:
        UP_IP.pop(host, None)                # 可能是它换 IP 了，下次重问
        log('%s 【耳】出错: %r' % (tag, e))
    finally:
        try:
            up.close()
        except Exception:
            pass


def read_until(s, sep, cap=65536):
    b = b''
    while sep not in b and len(b) < cap:
        d = s.recv(4096)
        if not d:
            break
        b += d
    return b


def serve_443():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CFG['cert'])
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1     # 音箱连 vbox-tts 用的是 TLSv1.0
    except Exception:
        pass
    ctx.set_ciphers('ALL:@SECLEVEL=0')
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', 443))
    srv.listen(8)
    log('443：起来了（音箱不校验证书，自签就行）')
    n = 0
    while True:
        cs, _ = srv.accept()
        n += 1

        def handle(cs=cs, n=n):
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
            except Exception as e:
                log('#%d 出错: %r' % (n, e))
            finally:
                try:
                    ss.close()
                except Exception:
                    pass

        threading.Thread(target=handle, daemon=True).start()


# ═══════════════════════════ 五、起飞 ═══════════════════════════
def main():
    log('音箱大脑启动')
    if not DS_KEY:
        log('✗ 没有 LLM 密钥：把 key 写进 %s（一行，chmod 600）' % CFG['keyfile'])
        return 1
    log('密钥已载入，模型 %s' % CFG['ds_model'])

    if CFG['dns'] == 'own':
        threading.Thread(target=dns_serve, daemon=True).start()
    else:
        log('DNS：交给 NetworkManager 的 dnsmasq 答（劫持写在 dnsmasq-shared.d 里）')
    threading.Thread(target=serve_443, daemon=True).start()

    # 配网在后台一直等着 —— 音箱什么时候进配网模式，什么时候推
    def _prov():
        try:
            asyncio.run(provision(CFG['ap_ssid'], CFG['ap_pwd'],
                                  CFG['ble_name'], CFG['ap_iface']))
        except Exception as e:
            log('配网出错: %r' % e)

    threading.Thread(target=_prov, daemon=True).start()

    log('====== 准备就绪，去对音箱说句话吧 ======')
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    sys.exit(main())
