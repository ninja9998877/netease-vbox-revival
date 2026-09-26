#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒充 mpv —— 把 ncm-cli 交给播放器的**音频 URL** 截下来。

★★★ 为什么需要它：ncm-cli 的 `play` 走一条 `PlayerDaemon`（unix socket），
   daemon 起播放器时**URL 不在 argv 里** —— 它按 mpv 的规矩开
   `--input-ipc-server=<sock>`，再用 JSON IPC 把 `loadfile <URL>` 发进去。
   所以只把 mpv 换成"打印 argv 的脚本"是**截不到 URL 的**（我踩过），
   必须真的在那个 socket 上**说 mpv 的协议**。

★ 它什么也不播 —— 没有音频输出，本机的喇叭永远保持哑（主人铁律）。
  收到的每个 URL 追加进 log/ncm_url.log，供 spk_alarm 取用。
"""
import datetime
import json
import os
import socket
import sys
import time

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'ncm_url.log')
URL_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log', 'ncm_url.json')


def out_json():
    """这次的 URL 往哪儿写。

    ★ 默认还是那个**共享**的老文件（行为一个字不变）。但取地址的那一方
      （`spk_alarm.fetch_url`）可以塞一个 `NCM_URL_OUT` 进来 —— 这个环境变量
      会**一路传到这儿**：play 进程 → PlayerDaemon → 播放器（daemon 是 play
      的子进程、播放器是 daemon 的子进程，env 一路继承下来）。
    ★★ 为什么非要有这个口：三个进程（spk-alarm 响铃 / 预热 / 另一个壳点播）
      共用一个文件 ⇒ 谁后写谁赢，**可能拿 B 的地址缓存成《A》**。
      各写各的口之后，这个形状**从根上没有了** —— 连锁都不再需要。
    """
    return os.environ.get('NCM_URL_OUT') or URL_JSON


def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def sock_path(argv):
    for a in argv:
        if a.startswith('--input-ipc-server='):
            return a.split('=', 1)[1]
    return None


# mpv 那套属性，daemon 会来问。回一个"正在正常播放"的样子，
# ★ 但**绝不能回 idle-active=True / eof-reached=True** —— 那会让 daemon
#   以为放完了去放下一首，白白多截几个 URL。
STATE = {'time-pos': 0.0, 'duration': 200.0, 'pause': False,
         'idle-active': False, 'eof-reached': False, 'path': '',
         'media-title': '', 'core-idle': False, 'volume': 100.0}


def answer(cmd, rid):
    """按 mpv 的 JSON IPC 回一条。规则：能答的答，答不了的回 success+null。

    ★ 宁可回 null 也**不能不回** —— daemon 那边在等，不回就是它日志里那句
      `daemon 无响应（3s 超时）`。"""
    name = cmd[0] if cmd else ''
    if name == 'get_property' and len(cmd) > 1:
        return {'data': STATE.get(cmd[1]), 'error': 'success', 'request_id': rid}
    if name == 'set_property':
        if len(cmd) > 2:
            STATE[cmd[1]] = cmd[2]
        return {'data': None, 'error': 'success', 'request_id': rid}
    if name == 'observe_property' or name == 'unobserve_property':
        return {'data': None, 'error': 'success', 'request_id': rid}
    if name == 'loadfile':
        url = cmd[1] if len(cmd) > 1 else ''
        STATE['path'] = url
        STATE['time-pos'] = 0.0
        STATE['eof-reached'] = False
        log('♪ URL 到手：%s' % url)
        try:
            op = out_json()
            os.makedirs(os.path.dirname(op), exist_ok=True)
            with open(op, 'w') as f:                       # 给 spk_alarm 一个"机器读"的口
                json.dump({'url': url, 'ts': time.time(), 'pid': os.getpid()}, f)
        except OSError:
            pass
        return {'data': None, 'error': 'success', 'request_id': rid}
    if name in ('quit', 'stop'):
        return {'data': None, 'error': 'success', 'request_id': rid}
    return {'data': None, 'error': 'success', 'request_id': rid}


def main():
    argv = sys.argv[1:]
    sp = sock_path(argv)
    log('假 mpv 起来（socket=%s）' % sp)
    if not sp:
        return 0
    try:
        os.unlink(sp)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sp)
    srv.listen(4)
    srv.settimeout(120)          # 没人来就自己退，别变成常驻僵尸
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            break
        conn.settimeout(60)
        buf = b''
        try:
            while True:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode('utf-8', 'replace'))
                    except ValueError:
                        continue
                    rid = msg.get('request_id')
                    out = answer(msg.get('command') or [], rid)
                    conn.sendall((json.dumps(out) + '\n').encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
    try:
        srv.close()
        os.unlink(sp)
    except OSError:
        pass
    log('假 mpv 收工')
    return 0


if __name__ == '__main__':
    sys.exit(main())
