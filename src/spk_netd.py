#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spk_netd —— 无线敲设备命令的那条通道（用来取代 USB 上的 adb）。

    python3 spk_netd.py                起服务（systemd 跑这个）
    python3 spk_netd.py "amixer -c 0 cget numid=105"     无线执行一条，等结果

★ 为什么要它（2026-09-21 实测教训，血的）
  `adb tcpip 5555` 在正经 Android 上很好使，在这台音箱上报"restarting in TCP mode
  port 5555"，然后 5555 死活连不上（iptables 挡了 / adbd 根本没真监听），
  ★★ 而且【连 USB 那条也一起断了】—— 设备当场失联，只能人去重插线。
  所以 adbd 的 TCP 模式这条路，在这台设备上是死的，别再试。

★ 方向反过来：让【设备主动来拉】，不是本机往里推
  设备上有 curl（/usr/bin/curl），所以真正的通道是：
      设备 --轮询--> 本机:8898 /cmd    问：有活儿吗
      设备 --上报--> 本机:8898 /ret    答：干完了，这是输出
  三个好处，缺一不可：
    ① 设备上【不开任何入站端口】—— 不用碰它的 iptables，也不给局域网里
       任何一台机器留一个无认证的 shell（adb 5555 恰好就是这种东西）
    ② 不依赖 adbd、不依赖 USB、不依赖点对点组网打洞，全走同一个局域网
    ③ 设备那边不用多起进程 —— 挂在那条已经常驻的守护循环上顺带做

★ 口令
  设备带一个 token 来。不是防外人（局域网里都是自己人），是防"网段里某台被
  感染的设备把这条通道当跳板"。token 在 ~/.spk_net_token（0600，一行）。
  ★ 绝不写进代码、绝不打印、绝不进日志。

★ 边界
  只监听 `HOST`（示例 192.168.1.100，LAN），跟 [[web-services-lan-only]] 那条规矩一致 ——
  非 LAN 的源连 TCP 都建不起来。通道本身【不做命令白名单】：能敲的人已经
  坐在本机上了，在这儿拦只是自欺欺人；真正该拦的是"谁能进本机"。

★ 过期闸门（2026-09-22 加，血的）
  投出去的命令【不保证立刻被取走】—— 设备离线 / 通道死掉的时候，它会静静地
  躺在槽里。实测栽过：一条 09-21 17:3x 投的命令躺了【近 17 小时】，
  通道一活就被执行了。那次侥幸是只读的 `amixer cget`；要是写命令
  （音量 / nightmute / reboot），就会在一个完全不可预期的时刻生效，
  而你早就认为它失败了、甚至已经重投过一条。

  两道防线：
    ① 【取走时查 TTL】（`CMD_TTL`，默认 120 秒，`SPK_NET_CMD_TTL` 可覆盖）。
       超龄 ⇒ 丢弃 + 往 ret 写一条说明 + 记日志。
       ★★ 丢弃时【必须回空】—— 设备侧是 `[ -z "$C" ] || sh -c "$C"`，
          回任何非空文本都会被当命令执行（哪怕写的是"命令已过期"）。
    ② 【send() 超时主动撤单】—— 调用方都放弃了，就别留着它。
       ★ 先核对"槽里还是我们那条"，期间被别人重投过就不动它。

  ★ 为什么拿 cmd 文件自己的 mtime 当投递时刻，而不是另写一个 .at 时间戳：
    投递方不止一个（`send()` / 别的工具直接写文件 / 手工 `printf >`），
    **mtime 是"谁写都算数"的唯一判据** —— 零额外文件、零调用方改动、防漏。
"""
import argparse
import http.server
import os
import sys
import time
import urllib.parse

from _cfg import HOST, PORT_NETD

HOME = os.path.expanduser('~')
# ★ SPK_NET_DIR 可覆盖：离线测"过期闸门"时必须能把槽位挪走，
#   否则测试会写真实的 cmd/ret（跟 SPK_MEM_DIR 是同一个理由）。
DIR = os.environ.get('SPK_NET_DIR') or os.path.join(HOME, '.spk_net')
CMD = os.path.join(DIR, 'cmd')          # 待设备来取的一条命令
RET = os.path.join(DIR, 'ret')          # 设备回传的最近一次输出
TOKENF = os.path.join(HOME, '.spk_net_token')
BIND = os.environ.get('SPK_NET_BIND', HOST)
PORT = int(os.environ.get('SPK_NET_PORT', PORT_NETD))
# 一条命令在槽里的最长寿命。正常设备 2 秒就来取，120 秒是 60 倍余量
# （够扛设备重启和一次网络抖动），又远小于"躺 17 小时"那种事故。
CMD_TTL = float(os.environ.get('SPK_NET_CMD_TTL', '120'))

# ★★ 长轮询（2026-09-22 加）：设备是每 2 秒来问一次（spk_net.sh 的 GAP=2），
#   而"停"这种事等不起 2 秒 —— 打断的时候喇叭还响着。这里把 /cmd 请求【挂住】
#   最多 HOLD 秒，命令一到就立刻回 ⇒ 送达延迟从"最多 2 秒"变成几十毫秒。
#   设备侧一个字都不用改：它那条 curl 本来就是 `-m 5`，挂 1.2 秒完全受得住。
#   ★ 实测（同一台设备、同一时刻）：不长轮询时 `stop` 往返 4.9 秒，
#     长轮询之后 0.1~0.3 秒（见 spk_ai_dlna.py 的 _stop601 注释）。
#   ★ 挂住期间设备不能问 /ping —— 那是它自己那条循环的代价，本机活着就无所谓。
HOLD = float(os.environ.get('SPK_NET_HOLD', '3.0'))


def token():
    try:
        with open(TOKENF) as f:
            return f.read().strip()
    except OSError:
        return ''


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ''


def _write(p, s):
    os.makedirs(DIR, exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        f.write(s)
    os.replace(tmp, p)


def _age(p):
    """文件年龄（秒）；读不到 mtime 就给 None。
    ★ None 是【不可信】不是【不过期】—— 调用方要按"丢弃"处理（fail-closed）：
      读不到时间戳说明文件正被换/删，那一刻的内容可能是半截的，
      宁可让主人重投一条，也不要执行一条来路不明的命令。"""
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return None


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, *a):
        pass                                     # 设备每 2 秒来一趟，别刷屏

    def _auth(self, q):
        t = token()
        if not t:
            return True                          # 还没设口令就不拦（首次部署方便）
        got = urllib.parse.parse_qs(q).get('t', [''])[0]
        if got != t:
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        return True

    def _ok(self, body=b''):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not self._auth(u.query):
            return
        if u.path == '/cmd':
            s = _read(CMD)
            # ★★ 空手回去之前先【挂一会儿】（长轮询，见文件头 HOLD）。
            #   设备每 2 秒才问一次，"停"这种命令原来最坏要等 2 秒才送到 ——
            #   打断时喇叭还响着，这 2 秒是听得出来的。挂住之后命令一到就走。
            if not s.strip() and HOLD > 0:
                t0 = time.time()
                while time.time() - t0 < HOLD:
                    time.sleep(0.05)
                    s = _read(CMD)
                    if s.strip():
                        break
            if not s.strip():
                self._ok(b'')
                return
            age = _age(CMD)
            if age is None or age > CMD_TTL:
                # —— 过期（或时间戳不可信）⇒ 丢弃，绝不执行 ——
                why = '时间戳读不到' if age is None else '投递于 %.1f 分钟前' % (age / 60.0)
                _write(CMD, '')                  # 清掉，否则每次轮询都来过一遍期
                _write(RET, '[spk-netd] 这条命令【已过期，未执行】：%s。\n'
                             'TTL=%g 秒。投递方多半早就放弃了 —— 要执行请重投。\n'
                             % (why, CMD_TTL))
                _write(RET + '.at', str(time.time()))
                print('⚠ 丢弃过期命令（%s）：%s' % (why, s.strip()[:200]), flush=True)
                self._ok(b'')                    # ★ 必须回空：非空会被设备 sh -c 执行
                return
            _write(CMD, '')                      # ★ 取走就清，绝不重复投递
            self._ok(s.encode())
            return
        if u.path == '/ping':
            self._ok(b'spk-netd ok')
            return
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self._auth(u.query):
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n else b''
        if u.path == '/ret':
            _write(RET, body.decode('utf-8', 'replace'))
            _write(RET + '.at', str(time.time()))
        self._ok(b'ok')


def serve():
    os.makedirs(DIR, exist_ok=True)
    srv = http.server.ThreadingHTTPServer((BIND, PORT), H)
    print('spk-netd 听着 %s:%d（token %s）' % (BIND, PORT, '已设' if token() else '未设'),
          flush=True)
    srv.serve_forever()


def post(cmd):
    """投一条命令【就走】—— 不等回音、不撤单。

    ★ 给"停"这种要快的事：`send()` 会一直等到设备把输出送回来，而打断的时候
      喇叭还响着，那几秒等不起（say() 是同步调的，等回音就是让它卡住）。
    ★ 不等回音意味着【放弃上报失败】：设备不在线时这条命令会躺在槽里，等它上线
      再执行。这是安全的，因为槽是【先进先出的一条】—— 后投的 play 永远排在
      先投的 stop 后面，不会出现"晚到的 stop 掐掉新回答"。
    ★ 真正的兜底是取走时的 TTL 闸门（默认 120 秒）：投递方早就放弃的命令
      不会在两小时后凭空执行。
    """
    _write(CMD, cmd.strip() + '\n')


def send(cmd, wait=30.0):
    """发一条命令，等设备来取、执行、把输出送回来。超时回 None。
    ★ 每次先把 ret 清空 —— 否则设备还没回来，我们就把【上一条】的输出当成
      这一条的答案交出去了。那种错误最难发现：看起来永远"成功"，内容是旧的。
    ★ 超时【撤单】（见文件开头「过期闸门」）—— 调用方都放弃了，就别留着它
      在几小时后凭空执行。撤单和 TTL 是两道独立防线：这条盖住"投递方还活着
      但放弃了"，TTL 盖住"进程被 kill / 手工投递 / 压根没人等"。
    ★★★★★ 【空回复也是回复】—— 2026-09-22 深夜的真凶，主人感受原话：
      「等了非常久 大概有8到10秒了 才回思考音」。旧版判据是 `if r:`，
      于是**命令跑了但没输出**（设备侧照样 POST 了一个空 body）在我们这里
      跟"设备压根没回话"长得一模一样 ⇒ 白等满 wait。
      实测（本机，同一条命令，设备在线）：
          原样（grep 没命中 ⇒ 空输出）→ 8.04s 回 None
          末尾加 `echo DONE`（同一条命令，只是必有输出）→ 0.20s 回 'DONE'
      ⇒ 真相是**空结果被当成超时**。`_cur_player()` 的探针正是"通常没有输出"
        的那种命令（CC 日志里没有那两行），偏偏它 wait=8.0 —— 于是**每轮静默
        超过 30 秒之后的第一推，凭空多等 8 秒**。
      ⇒ 修法：拿 `ret` 的 **mtime** 当"设备回话了"的判据（设备是用 os.replace
        写的，mtime 必然变新），内容为空就当场回 ''。"""
    _write(RET, '')
    try:
        t_clear = os.path.getmtime(RET)
    except OSError:
        t_clear = 0.0
    body = cmd.strip()
    _write(CMD, body + '\n')
    t0 = time.time()
    try:
        while time.time() - t0 < wait:
            time.sleep(0.1)
            r = _read(RET)
            if r:
                return r
            try:
                # ★ 内容空 + mtime 变新 = 设备执行完了、只是没输出 ⇒ 当场交答案，
                #   绝不能让它跟"没回话"混为一谈（混了就是上面那 8 秒）。
                if os.path.getmtime(RET) > t_clear:
                    return ''
            except OSError:
                pass
        return None
    finally:
        # ★ 先核对"槽里还是我们那条"：期间别人重投过就别动（那是他的命令）。
        #   正常取走过的情况下槽早就空了 ⇒ 不匹配 ⇒ 什么也不做。
        if _read(CMD).strip() == body:
            _write(CMD, '')


def main():
    ap = argparse.ArgumentParser(description='无线敲设备命令')
    ap.add_argument('cmd', nargs='*', help='要执行的命令；不写就起服务')
    ap.add_argument('--wait', type=float, default=30.0)
    a = ap.parse_args()
    if not a.cmd:
        serve()
        return 0
    out = send(' '.join(a.cmd), wait=a.wait)
    if out is None:
        print('✗ 设备没来回话（%g 秒）—— 看设备上 spk_net.sh 是不是活着' % a.wait)
        return 1
    sys.stdout.write(out if out.endswith('\n') else out + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
