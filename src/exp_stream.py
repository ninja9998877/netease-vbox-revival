#!/usr/bin/env python3
"""流式音频端点的**可行性实验** —— 只吐静音，屋里绝不会出声。

要回答的问题（都是设备固件的未知行为，猜不得）：
  ① 设备认不认**不带 Content-Length** 的响应（靠关连接表示结束）？
  ② duration 未知时，固件那个 `duration > 15000 && (duration − curPos) > 15000`
     的 Next 判据会怎么走？（`_stage601` 的注释：总长不到 15 秒的轨**立刻** Next）
  ③ 设备是**边下边播**（按实时速度拉）还是**先整段缓冲**（全速拉完）？
  ④ prebuffer 多久才出声（= 首声里那一截我们控制不了的延迟）？

★ 判据：`tcpdump -i <你的网卡名> tcp port 8898`（拉取节奏 + 连接活了多久）
        ＋ 本文件 stderr 的时间戳日志。
★ 为什么不读设备日志：`_play_and_mute()` 会占 `~/.spk_net/cmd` 单槽 8 秒，
  第一步先只用零副作用的抓包。

用法：
    python3 exp_stream.py            # 起在 192.168.1.100:8898（地址以你的实际部署为准）
    curl -s localhost:8898/s?mode=nolen -o /dev/null   # 本机自测

三种响应形态（`?mode=`），各自是一次独立的实验：
    nolen    —— 无 Content-Length + Connection: close（**真实流式 TTS 的形状**）
    len      —— 声明真实长度（对照组：没道理不放，用来证明"路本身是通的"）
    chunked  —— Transfer-Encoding: chunked（如果 nolen 不行，看这个行不行）
"""
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _cfg import HOST

SRC = '/tmp/exp_silent60.mp3'
# ★★ 8897，**不是 8898** —— 8898 是 `spk_netd` 的命令总线（设备在那儿长轮询取命令），
#    8899 是 `mp3srv` 的音频静态根。占住 8898 = 设备再也取不到命令，是**最坏**的踩法
#    （起服务时报 `Address already in use` 才知道，见 2026-09-23 那次）。
BIND = (HOST, 8897)
BPS = 16000          # 128kbps ⇒ 每秒 16000 字节（实时吐的节奏就是按它算的）


def log(msg):
    """★ 格式**故意**跟 mp3srv 不一样 —— 那边 `GET /<名> ` 是 `fetched_count()`
    的判据，这里绝不能长得像它，否则实验会污染真账。"""
    sys.stderr.write('[exp %s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    sys.stderr.flush()


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'expstream/0.1'

    def log_message(self, fmt, *args):
        pass                                    # 走我们自己的 log()

    def do_GET(self):
        path, _, query = self.path.partition('?')
        mode = 'nolen'
        for kv in query.split('&'):
            if kv.startswith('mode='):
                mode = kv[5:]
        if path not in ('/s', '/s/'):
            self.send_error(404)
            return
        with open(SRC, 'rb') as f:
            data = f.read()
        log('GET %s  mode=%s  UA=%r  Range=%r'
            % (self.path, mode, self.headers.get('User-Agent'),
               self.headers.get('Range')))
        try:
            if mode == 'nolen':
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Connection', 'close')
                self.end_headers()
                self._drip(data)
            elif mode == 'len':
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self._drip(data)
            elif mode == 'chunked':
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self._drip(data, chunked=True)
            else:
                self.send_error(400, 'mode?')
        except (BrokenPipeError, ConnectionResetError) as e:
            log('  ↯ 客户端断了（%s）—— 吐到第 %.1f 秒处'
                % (type(e).__name__, getattr(self, '_sent_secs', -1)))

    def _drip(self, data, chunked=False):
        """按**实时节奏**吐（每秒 BPS 字节）—— 模拟"边合成边吐"。
        设备若全速拉完，说明它先整段缓冲；若按我们的节奏慢慢拉，说明它边下边播。"""
        step = BPS // 20                        # 50ms 的量
        t0 = time.time()
        sent = 0
        while sent < len(data):
            n = min(step, len(data) - sent)
            body = data[sent:sent + n]
            if chunked:
                self.wfile.write(b'%x\r\n' % n + body + b'\r\n')
            else:
                self.wfile.write(body)
            self.wfile.flush()
            sent += n
            self._sent_secs = sent / float(BPS)
            want = t0 + self._sent_secs         # 吐的节奏 ≈ 音频该有的时长
            d = want - time.time()
            if d > 0:
                time.sleep(d)
        if chunked:
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        log('  ✓ 吐完 %d 字节 / %.1fs' % (sent, time.time() - t0))


if __name__ == '__main__':
    if not os.path.exists(SRC):
        sys.exit('先做素材：ffmpeg -f lavfi -i anullsrc=r=48000:cl=mono -t 60 '
                 '-c:a libmp3lame -b:a 128k -y %s' % SRC)
    srv = ThreadingHTTPServer(BIND, H)
    srv.daemon_threads = True
    log('实验端点起来 —— http://%s:%d/s?mode=nolen|len|chunked  素材 %s（%d 字节）'
        % (BIND[0], BIND[1], SRC, os.path.getsize(SRC)))
    srv.serve_forever()
