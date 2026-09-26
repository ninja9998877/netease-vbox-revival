#!/usr/bin/env python3
"""音箱嗓子用的静态服务器（192.168.1.100:8899，/tmp 目录）—— 带 **HTTP Range**。

★ 下面出现的地址/端口都是**示例值**（真值走 `config/spk.env`：
  `SPK_HOST` / `SPK_PORT_MP3`），以你的实际部署为准。

★★★ 为什么要自己写一个、不用 `python3 -m http.server`（2026-09-22 深夜换掉的原因）：
   `SimpleHTTPRequestHandler` **不支持 Range** —— 请求里带 `Range: bytes=100-200`
   它照样回 `200` + **整个文件**（实测 curl -r 得到 Content-Length: 160749）。
   而设备侧的总控（netease_control_center）每一轮音乐簿记都会发一发 seek：

       21:48:16.849  获取当前歌曲信息失败
       21:48:16.854  [W] resume pos when err play, seek to pos: 1
       21:48:16.854  cmdId:0x604 {"playerId":1,"skTime":1,"gain":0,...}
       21:48:16.863  [W] tina play seek err.          ← Seek 失败
       (播放器侧)     playerId:1 st:0x3 → st:0x48 duration:1ms
                      ↑ 刚 Playing 8 毫秒就被打死，人一个字也没听见

   ⇒ **这就是"回答时好时坏 / 完全不出声"的最后一环**：seek 拿不到 206 就报错、
   播放器当场从 Playing 掉到 0x48。修好 Range 之后那一发 seek 会成功，
   而它 seek 的目标是 **1 毫秒**（"从头重来"）—— 落在人声之前，
   **听感上等于没发生**（我们推的轨通常前 ~1 秒是静音垫，见 `_stage601` 的 HEAD）。

★ 三条老规矩原样继承（动这个文件之前先看清楚）：
  1. **访问日志就是"推流成没成"的判据本身**（`spk_ai_dlna.py` 的 `fetched_count`
     数的是 `GET /<文件名> ` 这一串）⇒ 日志格式、去向都不许改。
  2. **日志不能落 /tmp**（root + sticky 目录 + O_CREAT 已存在文件 ⇒ EACCES 209），
     系统日志由 unit 的 StandardOutput=append: 指到 log/spk_srv.log。
  3. **只绑大脑那台机的地址**（默认 `HOST`，示例 192.168.1.100），不暴露给局域网。

★ 行尾/长连接：用 HTTP/1.1 + Content-Length（Range 需要它），并显式 Connection: close
   兜底 —— 设备侧播放器是 HTTP/1.1 请求方，分片取流要的是"每次都有准数"。
"""

import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _cfg import HOST, PORT_MP3

ROOT = os.environ.get('MP3_ROOT', '/tmp')
BIND = os.environ.get('MP3_BIND', HOST)
PORT = int(os.environ.get('MP3_PORT', PORT_MP3))
CHUNK = 64 * 1024
CTYPE = {
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
    '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.flac': 'audio/flac',
}
_RANGE = re.compile(r'bytes=(\d*)-(\d*)\s*$')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'spk-mp3/1.0'

    # ---- 工具 ----------------------------------------------------------
    def _full_path(self):
        """把请求路径落到 ROOT 里；越界（../）一律 None。"""
        p = urllib.parse.unquote(self.path.split('?')[0])
        p = os.path.normpath(p).lstrip('/')
        full = os.path.abspath(os.path.join(ROOT, p))
        if full != os.path.abspath(ROOT) and not full.startswith(os.path.abspath(ROOT) + os.sep):
            return None
        return full

    def _range(self, size):
        """解析 Range 头。回 (start, end, 206?) 或 None（=不认，按整文件发）。"""
        h = self.headers.get('Range')
        if not h:
            return None
        m = _RANGE.match(h.strip())
        if not m:
            return None
        a, b = m.group(1), m.group(2)
        if a == '' and b == '':                       # bytes=- 无意义
            return None
        if a == '':                                   # 后缀式：最后 b 个字节
            n = int(b)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
            if end >= size:
                end = size - 1
        if start > end or start >= size:
            return 'unsatisfiable'
        return start, end

    def _send(self, code, ctype, size, start, end, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if code == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Last-Modified', self.date_time_string(os.path.getmtime(self._cur)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    # ---- 处理 ----------------------------------------------------------
    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _handle(self, head_only):
        full = self._full_path()
        if not full or not os.path.isfile(full):
            self.send_error(404, 'Not Found')
            return
        self._cur = full
        size = os.path.getsize(full)
        ctype = CTYPE.get(os.path.splitext(full)[1].lower(), 'application/octet-stream')
        rng = self._range(size)
        if rng == 'unsatisfiable':
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % size)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if rng:
            start, end = rng
            code = 206
        else:
            start, end = 0, max(0, size - 1)
            code = 200
        self._send(code, ctype, size, start, end)
        if head_only:
            return
        try:
            with open(full, 'rb') as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    d = f.read(min(CHUNK, left))
                    if not d:
                        break
                    self.wfile.write(d)
                    left -= len(d)
        except (BrokenPipeError, ConnectionResetError):
            pass                                   # 设备提前挂断：正常，别刷栈

    def log_message(self, fmt, *args):
        # ★ 格式必须与 http.server 一致：`IP - - [时间] "GET /x.mp3 HTTP/1.1" 200 -`
        #   （spk_ai_dlna.py 的 fetched_count 就是在这串里数 `GET /<文件名> `）
        sys.stderr.write('%s - - [%s] %s\n'
                         % (self.address_string(), self.log_date_time_string(),
                            fmt % args))


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write('spk-mp3: 听 %s:%d，根目录 %s（带 Range/206）\n' % (BIND, PORT, ROOT))
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == '__main__':
    main()
