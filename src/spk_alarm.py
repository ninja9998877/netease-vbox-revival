#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闹钟 —— 到点自己放轻音乐。

★ 为什么不用 say()：say() 里有夜间软闸门（夜里直接 return False，连 mp3 都不生成），
  而闹钟要的恰恰就是"夜里能响"。所以这条走 spk_ctl.play()，绕开那个闸门 ——
  这是【唯一】一个被允许在夜间出声的东西，绕开是有意的，不是漏了。

响铃期间要跟两道"我们自己装上去的"闸门打交道：
  ① 本机侧 say() 的软闸门        → 绕开（走 spk_ctl.play）
  ② 设备侧 nightmute 的硬闸门        → 谈好：开关 off + 每 20 秒守护一次
     ⇒ 响铃时：adb 建 /tmp/spk/ringing（nightmute 的守护看到就撒手）+ 把开关扳 on
     ⇒ 播完：清标记 + 开关扳回 off（只在夜间才需要扳回，白天本来就是开的）

⚠️ 已知未处理：响铃时 spk-ear 的 KWS 会听到音乐，可能误判唤醒词。
   真发生了就给它加"响铃期间不喂音频"，先观察。
"""
import datetime
import fcntl
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spk_ctl                       # noqa: E402  play/stop/status
import spk_ai_dlna as dlna           # noqa: E402  NIGHT_QUIET / in_night

HERE = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(HERE, 'music')
TABLE = os.environ.get('SPK_ALARM_TABLE', os.path.join(HERE, 'alarms.json'))
LOGF = os.environ.get('SPK_ALARM_LOG', os.path.join(HERE, 'log', 'alarm.log'))

DEV_RING = '/tmp/spk/ringing'        # ★ 设备侧的标记（nightmute 在设备上跑，读的是这个）
SW_IDS = (105, 106)                  # Headphone Switch / Phoneout Switch

PICKUP_SECS = 15 * 60                # 没人管的话最多响这么久，别响一整天
POLL = 5.0

# ---------------- 在线曲源：网易云官方 CLI（2026-09-23 接入）----------------
# ★ 为什么是 CLI 而不是自己调 API：官方开放平台对**个人**入驻关掉了
#   `/openapi/music/basic/song/{playurl,downloadurl}` 那一套（FAQ 原话
#   「个人场景暂不支持……仅可使用 ncm-cli」）。所以 `ncm-cli` 是个人唯一的
#   官方通道，凭据（App ID + Private Key + 扫码登录的 accessToken）都在
#   `~/.config/ncm-cli/` 的加密库里。
NCM_DIR = os.path.join(HERE, 'ncmcli')
NCM_BIN = os.path.join(HERE, 'ncm-bin')          # ★ 影子 mpv 所在（PATH 前置）
NCM_CLI = os.path.join(NCM_DIR, 'node_modules',
                       '@music163', 'ncm-cli', 'dist', 'index.js')
NODE = shutil.which('node') or '/usr/local/bin/node'
URL_JSON = os.path.join(HERE, 'log', 'ncm_url.json')   # 影子 mpv 把 URL 写这儿（老共享口）
_URL_SEQ = 0            # 本次取地址的序号（每次取地址换一个口，见 `_url_out()`）
TRACKS_F = os.path.join(HERE, 'log', 'ncm_tracks.json')
CACHE_DIR = os.path.join(HERE, 'music', 'cache')
# ★ 闹钟放什么歌，由**你自己**的网易云歌单决定 —— 填歌单 ID。
#   怎么拿：网页版打开那张歌单，地址栏里 `music.163.com/#/playlist?id=<这一串>`。
#   ★ 不填 = 没歌可放（闹钟照响，只是没音乐），所以第一次用记得设上。
PLAYLIST = os.environ.get('SPK_ALARM_PLAYLIST', '')
TRACKS_TTL = 24 * 3600               # 曲目表缓存一天，别每次都问网易云

# ---- 缓存的大小红线（主人 2026-09-23：「**注意大小 别撑爆了硬盘**」）----
# 三道闸，各自堵一种撑爆的姿势，三个都能用环境变量调：
CACHE_MAX = int(os.environ.get('SPK_ALARM_CACHE_MB', '300')) * 1024 * 1024
#   ① 整个缓存目录封顶（默认 300 MB ≈ 60 首，够一张歌单转三圈）——超了删最旧的
CACHE_ONE_MAX = int(os.environ.get('SPK_ALARM_CACHE_ONE_MB', '40')) * 1024 * 1024
#   ② **单首**封顶（默认 40 MB）：一份 3 小时的播客 mp3 能有 200 MB，
#     一首就能把①整条红线吃掉。超了就**不缓存**（照样能流式放，只是每次现取）
DISK_FLOOR = int(os.environ.get('SPK_ALARM_DISK_FLOOR_MB', '2048')) * 1024 * 1024
#   ③ **盘上剩多少**的红线（默认留 2 GB 不碰）：这台机器上还跑着别的项目，
#     缓存再重要也不能把盘吃到见底 —— 只剩这点空间就**一首都不缓存**
PART_TTL = 3600                      # 半截的 `.part` 躺过一小时就当垃圾收掉

DEAD_F = os.path.join(HERE, 'log', 'ncm_dead.json')   # 撞过"没版权"这堵墙的曲目
DEAD_TTL = 24 * 3600        # ★ 只记一天：版权会变、歌单会改，拉黑了就不能是一条死路
_DEAD_LOCK = threading.Lock()

URL_LOCK_F = os.path.join(HERE, 'log', 'ncm_url.lock')   # "取地址"那把**跨进程**的锁
NCM_APP_LOG = os.path.expanduser('~/.config/ncm-cli/app.log')   # 官方 CLI 自己的日志


def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGF), exist_ok=True)
        if os.path.exists(LOGF) and os.path.getsize(LOGF) > 262144:
            os.replace(LOGF, LOGF + '.1')
        with open(LOGF, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def dev(cmd, timeout=20):
    return subprocess.run(['adb', 'shell', cmd], capture_output=True, timeout=timeout)


def sw_set(v):
    for n in SW_IDS:
        dev('amixer -c 0 cset numid=%d %s' % (n, v))


def sw_show():
    out = []
    for n in SW_IDS:
        p = dev('amixer -c 0 cget numid=%d' % n)
        v = '?'
        for ln in p.stdout.decode('utf-8', 'replace').splitlines():
            if ln.strip().startswith(': values='):
                v = ln.split('=', 1)[1].strip()
        out.append(v)
    return '/'.join(out)


def ring_flag(on):
    if on:
        dev('mkdir -p %s' % DEV_RING)
    else:
        dev('rmdir %s 2>/dev/null' % DEV_RING)


def stage(path):
    """把音乐摆渡到 /tmp —— ★ spk_ctl._src() 强制要求本地文件在 /tmp 下
    （8899 静态服务器的根就是那儿，见 spk_ctl.py 的注释），放别处直接抛错。
    每次响铃都拷一遍：/tmp 是内存盘，重启就没了，指望它常驻不靠谱。"""
    dst = os.path.join('/tmp', os.path.basename(path))
    try:
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(path):
            shutil.copy2(path, dst)
    except OSError as ex:
        log('   摆渡到 /tmp 失败（%s），直接试原路径' % ex)
        return path
    return dst


def _ncm_env():
    """跑官方 CLI 要的环境。★ 两件事，都不能省：
    ① `PATH` 前面塞 `ncm-bin` —— CLI 的 PlayerDaemon 从 PATH 里找 `mpv`，
       找到的是**影子 mpv**（只把 URL 截下来，绝不发声，见 `ncm_player.py`）。
    ② 去掉 `NETEASE_PRIVATE_KEY` —— 这机器的凭据是落在加密库里的，
       留个环境变量在那儿只会让"配置到底从哪来"多一个说不清的分支。"""
    env = dict(os.environ)
    env.pop('NETEASE_PRIVATE_KEY', None)
    env['PATH'] = NCM_BIN + os.pathsep + env.get('PATH', '')
    return env


def _ncm(args, timeout=60):
    """跑一次 CLI，返回它那段 JSON（拿不到就 None）。"""
    try:
        p = subprocess.run([NODE, NCM_CLI] + args, capture_output=True,
                           timeout=timeout, env=_ncm_env(), cwd=NCM_DIR)
    except (OSError, subprocess.SubprocessError) as ex:
        log('   ncm-cli 跑不起来：%s' % ex)
        return None
    txt = p.stdout.decode('utf-8', 'replace')
    i = txt.find('{')
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(txt[i:])[0]
    except ValueError:
        return None


def tracks(force=False):
    """歌单曲目表。★ 带 24 小时缓存（主人说「缓存就行了」），
    别每次响铃都去问一遍网易云。"""
    if not force:
        try:
            with open(TRACKS_F) as f:
                d = json.load(f)
            if time.time() - d.get('ts', 0) < TRACKS_TTL and d.get('songs'):
                return d['songs']
        except (OSError, ValueError):
            pass
    j = _ncm(['playlist', 'tracks', '--playlistId', PLAYLIST, '--limit', '500'])
    data = (j or {}).get('data')
    songs = data if isinstance(data, list) else []
    # ★★ `play` = 网易云自己标的 `playFlag` —— **"这首账号到底能不能放"**。
    #    2026-09-23 实测 10 首对照（歌单 6 + 搜索 4）**零误差**：能放的
    #    （夜的钢琴曲五·石进 / what for? / Mariage D'Amour…）全是 True，
    #    放不了的（Flower Dance / Summer / 雷米克斯版…）全是 False。
    #    ⇒ 挑曲先用它筛，**根本不必花 4 秒去撞版权那堵墙**。
    #    `is not False` 而不是 `== True`：字段缺了就当能放，绝不误杀。
    out = [{'name': s.get('name') or '?', 'id': s.get('id'),
            'orig': s.get('originalId'), 'dur': s.get('duration') or 0,
            'play': s.get('playFlag') is not False}
           for s in songs if s.get('id') and s.get('originalId')]
    if out:
        try:
            os.makedirs(os.path.dirname(TRACKS_F), exist_ok=True)
            with open(TRACKS_F, 'w') as f:
                json.dump({'ts': time.time(), 'songs': out}, f, ensure_ascii=False)
        except OSError:
            pass
        return out
    try:                                   # 拉不到就退回上次那份，宁可旧别没有
        with open(TRACKS_F) as f:
            return json.load(f).get('songs') or []
    except (OSError, ValueError):
        return []


def _lock_url(wait):
    """拿"取地址"这把锁（★ **跨进程**，`flock`）。返回文件句柄表示拿到了。

    三种返回，调用方都要认：
      · 文件句柄 —— 拿到了，用完 `close()` 就是解锁
      · `None`   —— 等了 `wait` 秒也没轮到
      · `False`  —— 锁文件压根打不开（权限之类）⇒ **不锁，硬着头皮取**
        （宁可偶尔撞一次，也不要因为一个锁文件让主人的闹钟取不到地址）
    """
    deadline = time.time() + wait
    while True:
        try:
            fh = open(URL_LOCK_F, 'a+')
        except OSError as ex:
            log('   ⚠ 锁文件打不开（%s）—— 这次不锁，直接取' % ex)
            return False
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            fh.close()
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def _ncm_said_dead(tid):
    """网易云是不是**明说**这首放不了 —— 去它自己的日志里把那句话捞出来。

    ★ 凭据是**加密 ID**（就是我们传给 CLI 的那个 `--encrypted-id`），
      日志里那句是 `[_fetchUrl] 获取失败 <ID> (<ID>): 该歌曲暂无音源或暂无播放权限`
      ⇒ 就算别人同时在取别的歌，也不会认错人。
    ★ 捞不到（文件不在、没这条）就回空串 ⇒ 调用方**不拉黑**。
      「没证据」和「证明放不了」是两件事，混在一起就是自己造一堵假的版权墙。
    """
    try:
        with open(NCM_APP_LOG, 'rb') as f:
            f.seek(0, os.SEEK_END)
            n = min(f.tell(), 262144)          # 只看尾巴 256K，够一次取地址的量
            f.seek(-n, os.SEEK_END)
            txt = f.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    key = str(tid)
    for line in reversed(txt.splitlines()):
        if key in line and '获取失败' in line:
            return line.rsplit(':', 1)[-1].strip() or '放不了'
    return ''


def _url_out():
    """这次取地址**自己**的那个口（`NCM_URL_OUT` 指给影子 mpv）。

    ★ 每取一次换一个名字（同进程里预热是连着取的，光用 pid 会撞）。
    """
    global _URL_SEQ
    _URL_SEQ += 1
    return os.path.join(HERE, 'log', 'ncm_url.%d.%d.json' % (os.getpid(), _URL_SEQ))


def _ppid_map():
    """`{pid: 父pid}` —— 直接从 `/proc` 读，不依赖 psutil。"""
    m = {}
    try:
        pids = [n for n in os.listdir('/proc') if n.isdigit()]
    except OSError:
        return m
    for n in pids:
        try:
            with open('/proc/%s/stat' % n, 'rb') as f:
                raw = f.read().decode('utf-8', 'replace')
        except OSError:
            continue
        r = raw.rfind(')')          # ★ comm 里可能有空格和括号（`(node)`、`(sh)`）
        if r < 0:
            continue                #   ⇒ 必须从**最后一个** ')' 之后切
        parts = raw[r + 2:].split()
        try:
            m[int(n)] = int(parts[1])      # parts[0]=状态 parts[1]=父pid
        except (IndexError, ValueError):
            continue
    return m


def _kill_family(pid):
    """把一个进程**连同它整棵子树**杀掉。

    ★★ 为什么要连子树（2026-09-23 晚现场读出来的）：`ncm-cli play`
      **double-fork 自己** —— 我们 spawn 的那个直接子进程当场就退了
      （所以 `p.wait()` 立刻返回、`killpg(我们那个组)` 打空），真正干活的是
      一个 `setsid()` 出来的新会话：`play`(自己的组) → `PlayerDaemon`(又自己的组)
      → 影子 mpv(在 daemon 的组里)。只杀最上面那个 ⇒ 剩下的 daemon 照样占着
      `mpv.sock`，下一次取地址就**永久拿不到地址**。
    """
    m = _ppid_map()
    kids = {}
    for c, p in m.items():
        kids.setdefault(p, []).append(c)
    fam, stack = [], [pid]
    while stack:
        x = stack.pop()
        if x in fam:
            continue
        fam.append(x)
        stack.extend(kids.get(x, []))
    for x in fam:                       # 先按组（daemon 那组里有影子 mpv）
        try:
            os.killpg(x, signal.SIGKILL)
        except OSError:
            pass
    for x in fam:                       # 再逐个（防它自己的组也没盖住）
        try:
            os.kill(x, signal.SIGKILL)
        except OSError:
            pass
    return fam


def _ours(orig):
    """扫 `/proc`，找出命令行里带 `--original-id <orig>` 的 `ncm-cli play` 进程。

    ★ 指纹用这一首的**原始 ID** —— 它是**这一次请求独有**的，所以绝不会误伤
      同事在跑的 `search` 或别人的 `play`。
    ★★ `/proc/<pid>/cmdline` 是 **NUL 分隔**的（不是空格！）：`play\\0--song\\0
      --original-id\\01976422507\\0…`。头一版我拿 `'--original-id 123'`（带空格）
      去子串匹配，**一个都匹配不上**，于是杀进程这套整个是空转的
      （残留 3 个，而测试"看起来"通过了进程检查那项是因为我数的口径不对）。
      ⇒ 必须按 NUL 切开、按**参数对**比。
    """
    out = []
    want = str(orig)
    try:
        pids = [n for n in os.listdir('/proc') if n.isdigit()]
    except OSError:
        return out
    for n in pids:
        try:
            with open('/proc/%s/cmdline' % n, 'rb') as f:
                parts = f.read().decode('utf-8', 'replace').split('\x00')
        except OSError:
            continue
        if 'play' not in parts or '--original-id' not in parts:
            continue
        i = parts.index('--original-id')
        if i + 1 < len(parts) and parts[i + 1] == want:
            out.append(int(n))
    return out


def _reap(p, orig=None):
    """把这一趟 `ncm-cli play` 的人马**收干净**。

    ★★★ 为什么不是一句 `terminate()` 就完事（2026-09-23 晚实测踩了三轮）：
      ① 原来只 `terminate()`、**从不 `wait()`** ⇒ 留下活进程。现场抓到一个
         活了 220 秒的 `play`：它 spawned 的 daemon 也跟着活，而 daemon 会
         **继续托管影子 mpv、继续往那个共享的 URL 文件里写** —— 也就是
         "取地址"这条路上一直挂着一个**别人的**写手。顺手还占着 `mpv.sock`。
      ② 加上 `wait()` 与 `killpg` 还是不够：`play` 是 double-fork + `setsid`
         出去的（见 `_kill_family`），我们那个组里**一个人都没有**。
      ③ ⇒ 正解是**按指纹找人、连子树一锅端**（`_ours` + `_kill_family`）。
      ④ 另外：`mpv.sock` 是**全机唯一**的，漏下一个 daemon 会让
         取地址**永久拿不到地址**（兜底只有本地那一首吉他曲 —— 又是一个
         "每一环都报成功、主人听着同一首歌"的形状）。所以这里是硬要求。
    """
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass
    left = []
    if orig:
        for pid in _ours(orig):
            left += _kill_family(pid)
    # ★ 孤儿影子 mpv（父进程已经不在）也没用了：它只会占着 socket，
    #   而且自己 120 秒后也会退。不碰有爹的（那是同事在途的请求）。
    m = _ppid_map()
    for n in list(m):
        if m.get(n) != 1:
            continue
        try:
            with open('/proc/%s/cmdline' % n, 'rb') as f:
                cmd = f.read().decode('utf-8', 'replace')
        except OSError:
            continue
        if 'ncm_player.py' in cmd:
            left += _kill_family(n)
    return left


def _read_url(path):
    try:
        with open(path) as f:
            return (json.load(f) or {}).get('url') or ''
    except (OSError, ValueError):
        return ''


def sweep_stale_ncm(min_age=180):
    """扫掉**上一次人生**留下的 `ncm-cli` / 影子 mpv（按 pid，不用 `pkill -f`）。

    ★★★ 为什么这不是洁癖（2026-09-23 晚实测到的形状）：
      `PlayerDaemon` 和影子 mpv 都写在**同一个** `~/.config/ncm-cli/mpv.sock` 上。
      一个漏下来的 daemon（OOM、`systemctl restart`、拔电……都能留下）会
      **占着那个 socket**：下一次取地址时，新起的 daemon 的播放器绑不上，
      而 `loadfile` 被**旧的那个**接走 ⇒ 旧播放器按**它自己的** `NCM_URL_OUT`
      写文件 ⇒ 我们这边**什么都等不到**（实测：`没等到地址（CLI 退出码 0）`）。
      后果是"在线曲源**永久**拿不到地址"，而兜底只有本地那一首吉他曲 ——
      又是一个"每一环都报成功、主人听着同一首歌"的形状。
    ★ 只收**够老**的（默认 3 分钟）：正常一次取地址不到 10 秒，
      所以这绝不会误杀别人的在途请求（这台机器上还有别的进程也会取）。
    """
    now = time.time()
    killed = []
    try:
        pids = [n for n in os.listdir('/proc') if n.isdigit()]
    except OSError:
        return killed
    for n in pids:
        try:
            with open('/proc/%s/cmdline' % n, 'rb') as f:
                cmd = f.read().decode('utf-8', 'replace')
            age = now - os.stat('/proc/%s' % n).st_mtime
        except OSError:
            continue
        mine = NCM_CLI in cmd or NCM_BIN in cmd or 'ncm_player.py' in cmd
        if not mine or age < min_age:
            continue
        # ★ 连子树一锅端：只杀最上面那个，`PlayerDaemon` 和影子 mpv 会活下来
        #   继续占着 `mpv.sock`（那就等于什么都没扫）。
        killed += _kill_family(int(n))
    if killed:
        log('   ⌫ 收掉上次留下的 ncm 进程 %d 个（占了 mpv.sock 会让取地址永久失效）'
            % len(set(killed)))
        try:                       # 顺手把它那个口也清了，免得下回读到旧地址
            os.unlink(URL_JSON)
        except OSError:
            pass
    return killed


def fetch_url(t, timeout=30, wait=20):
    """让官方 CLI 解析这首歌的播放地址 → `(url, 原因, 是不是"这首放不了")`。

    ★★ 为什么要绕这么一圈（实测踩出来的）：URL **不在 CLI 的命令行参数里**。
      daemon 起播放器时按 mpv 的规矩开 `--input-ipc-server=<sock>`，
      再用 **JSON IPC** 把 `loadfile <URL>` 发进去 ⇒ 打印 argv 的假播放器
      **截不到 URL**（只会看到 `--no-video --idle` 那一串）。
      必须让"播放器"是我们的人、会说 mpv 的协议 —— 那就是 `ncm_player.py`。
    ★ 这条路上**真 mpv 一次都不会被启动**（PATH 里前面就是影子），
      所以本机一秒都不会出声。

    ★★★ 第三个返回值是「**能不能把这首拉黑**」，别拿它当小事：
      只有网易云**明说**"这首没音源/没权限"（`_ncm_said_dead`）才敢记进
      `ncm_dead.json`。"没抢到锁""CLI 起不来""等超时"都是**我们这边**的事，
      把账算到歌头上 ⇒ 24 小时里没人再试它 = 自己造了一堵不存在的版权墙。

    ★★★ 为什么还要一把**跨进程**的锁（2026-09-23 晚开预热才暴露的）：
      影子 mpv 原来把 URL 写在**唯一一个**文件里，而会取地址的有**三个进程**
      （响铃与预热、spk-ear、电话线）。两个同时取 ⇒ 谁后写谁赢，
      **两边都可能读到同一个 URL** ⇒ 拿 B 的地址缓存成《A》：那首从此"有缓存"
      却**从没被验证过**，响铃放的也不是它。
    ★★ 2026-09-23 晚更进一步：现在每次取地址都通过 `NCM_URL_OUT` 给影子 mpv
      一个**只属于这一次**的口（见 `_url_out()`）⇒ 读错人的形状从根上没有了。
      锁**留着**（`NCM_URL_OUT` 万一没传到位，退回的还是那个共享口，
      那时锁就是唯一的保障 —— 实测那条退路也真的会被走到）。
    """
    if not os.path.exists(NCM_CLI):
        return '', '官方 CLI 不在（%s）' % NCM_CLI, False

    out = _url_out()
    fh = _lock_url(wait)
    if fh is None:
        return '', '别人正在取地址（等了 %d 秒没轮到）' % wait, False
    try:
        for p in (out, URL_JSON):
            try:
                os.unlink(p)
            except OSError:
                pass
        env = _ncm_env()
        env['NCM_URL_OUT'] = out            # ★ 一路传到影子 mpv（play→daemon→player）
        try:
            p = subprocess.Popen([NODE, NCM_CLI, 'play', '--song',
                                  '--encrypted-id', t['id'],
                                  '--original-id', str(t['orig'])],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 env=env, cwd=NCM_DIR, start_new_session=True)
        except OSError as ex:
            log('   起 ncm-cli play 失败：%s' % ex)
            return '', 'CLI 起不来（%s）' % ex, False
        t0 = time.time()
        url, code, via = '', None, ''
        try:
            while time.time() - t0 < timeout:
                time.sleep(0.4)
                u = _read_url(out)          # ★ 先看自己那个口
                if u:
                    via = 'out'
                elif time.time() - t0 > 2.0:
                    u = _read_url(URL_JSON)  # ★ 退路：老那个共享口（有锁护着）
                    if u:
                        via = 'shared'
                if u.startswith('http'):
                    url = u
                    break
                if p.poll() is not None and time.time() - t0 > 4:
                    code = p.returncode
                    break              # 进程都退了还没截到，再等也没用
        finally:
            _reap(p, t.get('orig'))
            if via == 'shared':
                log('   ⓘ 影子 mpv 没写进自己那个口（`NCM_URL_OUT` 没传到位？）'
                    '—— 这次是共享口兜的（有锁护着，不会读错人）')
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
        if fh is not False:            # `False` = 压根没锁上，没得解
            try:
                fh.close()             # 关掉就是解锁
            except OSError:
                pass
    if url:
        return url, '', False
    said = _ncm_said_dead(t['id'])
    if said:
        return '', said, True
    return '', ('没等到地址（CLI 退出码 %s，等了 %.0f 秒）'
                % (code, time.time() - t0)), False


def _safe(s):
    return re.sub(r'[^0-9A-Za-z一-鿿._-]', '_', str(s))[:50]


def cache_of(t):
    return os.path.join(CACHE_DIR, '%s-%s.mp3' % (_safe(t['name']), t['orig']))


def cache_report():
    """缓存现在多大、盘上还剩多少 —— 主人要的「注意大小」得**看得见**才算数。"""
    try:
        fs = [os.path.getsize(os.path.join(CACHE_DIR, n))
              for n in os.listdir(CACHE_DIR) if n.endswith('.mp3')]
    except OSError:
        fs = []
    try:
        free = shutil.disk_usage(CACHE_DIR).free / 1073741824.0
    except OSError:
        free = -1
    return '缓存 %d 首 %.1f MB（上限 %d MB，盘上还剩 %.1f GB）' % (
        len(fs), sum(fs) / 1048576.0, CACHE_MAX // 1048576, free)


def trim_cache():
    """把缓存压回红线以内 —— 超了删最旧的，顺手收掉下崩了的半截文件。

    ★★ 为什么"顺手收 .part"这句不是凑数：`trim_cache` 原来只认 `.mp3`，
      而 `curl` 被掐死时（`systemctl restart`、OOM、拔电）留下的是
      `xxx.mp3.part` —— **它对红线和删除都是隐形的**，谁也不会来收。
      开预热之后后台天天在下载，这条路是会真的攒起来的。
    """
    now = time.time()
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    fs = []
    for n in names:
        p = os.path.join(CACHE_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if n.endswith('.fill'):
            continue           # 下载锁，不是缓存（空文件，别算进账、也别当最旧的删）
        if '.part' in n:       # ★ 认 `xxx.mp3.part` **和** `xxx.mp3.part.<pid>`
            if now - st.st_mtime > PART_TTL:
                try:
                    os.unlink(p)
                    log('   ⌫ 收掉半截文件 %s（%.1f MB）'
                        % (n, st.st_size / 1048576.0))
                except OSError:
                    pass
            continue
        fs.append((st.st_mtime, st.st_size, p))

    total = sum(x[1] for x in fs)
    if total <= CACHE_MAX:
        return
    log('   ⌫ 缓存 %d 首共 %.0f MB 越过上限 %d MB ⇒ 从最旧的开始删'
        % (len(fs), total / 1048576.0, CACHE_MAX // 1048576))
    for _, sz, f in sorted(fs):            # mtime 升序 = 最旧的先删
        if total <= CACHE_MAX:
            break
        try:
            os.unlink(f)
            total -= sz
            log('   ⌫ 删 %s（%.1f MB）' % (os.path.basename(f), sz / 1048576.0))
        except OSError:
            pass
    if total > CACHE_MAX:
        # ★ 走到这儿说明**单首就超了整个上限** —— 说出来，别假装红线还管用
        log('   ⚠ 还有 %.0f MB 超着：单首就比整个上限大（见 SPK_ALARM_CACHE_MB）'
            % (total / 1048576.0))


def _fill_lock(path):
    """「这首歌正在被下」那把锁（**跨进程**，跟着目标文件走）。

    三种返回，跟 `_lock_url` 一个规矩：句柄 = 拿到了；`None` = 别人正在下；`False` = 锁文件开不了。

    ★★★ 为什么非要有（2026-09-23 晚实测抓到的真事故）：
      同一条下载路一天会被**好几个**调用方踩到 —— spk-alarm 的响铃、预热、
      电话线点播，而且 `song_src()` 自己起一个线程、电话侧 `_audio_hook`
      又起一个（同一个 URL、同一个目标文件）⇒ **同一首歌下两遍**。
      两个 `curl -o` 写同一个 `.part`：各自按自己的偏移写，**内容交叉**，
      先收工的那个把这份**坏文件** `os.replace` 成正式缓存。
      `online_src()` 从此坚信"这首缓存过、真能放"（大小也过线），
      放出来是杂音 —— 而且是**永久**的，除非有人手动删缓存。
      日志里的指纹就是一对相邻的行：`⤓ 已缓存 X` 紧跟
      `缓存失败（No such file: X.part）`（后收工的那个找不到 .part 了）。
    """
    try:
        fh = open(path + '.fill', 'a+')
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def cache_fill(url, path):
    """★ 主人 2026-09-23：「不要下载，直接流式播放+缓存就行了」——
    所以这是**边播边存**：设备那头正在流式拉，我们顺手落一份，
    下次轮到这首就**直接放本地**（0 网络、断网也响、秒开）。
    丢在后台线程里跑，绝不挡响铃。

    ★★ 下之前先过两道闸（主人 2026-09-23：「**注意大小 别撑爆了硬盘**」）：
      盘上剩的不到 `DISK_FLOOR` ⇒ 一首都不下（这台机器上还有别的项目在用同一个盘）；
      单首超过 `CACHE_ONE_MAX` ⇒ 下完就删、不留下（**流式播放不受影响**，
      只是这首每次都得现取地址）。两道都只是"不缓存"，**绝不影响这一铃响不响**。
    ★★ 再往前还有两道**去重**：已经在盘上 ⇒ 直接走人；别人正在下这首 ⇒ 让给他。
      见 `_fill_lock` 的注释（不做的话是"下两遍 + 缓存里躺一个坏文件"）。
    """
    try:
        if os.path.getsize(path) > 65536:
            return                              # 已经缓存好了，不必再来一遍
    except OSError:
        pass
    fh = _fill_lock(path)
    if fh is None:
        return                                  # 别人正在下这一首 ⇒ 别抢
    # ★ 临时名带 pid：就算锁没锁上（`.fill` 文件开不了那条路），
    #   两个进程也不会写同一个文件 —— 各写各的，最后 `os.replace` 是原子的，
    #   留下的那一份一定是**完整的**。
    tmp = '%s.part.%d' % (path, os.getpid())
    try:
        free = shutil.disk_usage(CACHE_DIR).free
        if free < DISK_FLOOR:
            log('   ⚠ 盘上只剩 %.1f GB，低过红线 %d MB（SPK_ALARM_DISK_FLOOR_MB）'
                '⇒ 这首不缓存了' % (free / 1073741824.0, DISK_FLOOR // 1048576))
            return
        p = subprocess.run(['curl', '-sL', '--max-time', '240', '--noproxy', '*',
                            '-o', tmp, url], capture_output=True, timeout=260)
        if p.returncode == 0 and os.path.getsize(tmp) > 65536:
            sz = os.path.getsize(tmp)
            if sz > CACHE_ONE_MAX:
                log('   ⓘ 这首 %.1f MB 超过单首上限 %d MB ⇒ 不放缓存（照样能放）'
                    % (sz / 1048576.0, CACHE_ONE_MAX // 1048576))
                os.unlink(tmp)
                return
            os.replace(tmp, path)
            log('   ⤓ 已缓存 %s（%.1f MB）'
                % (os.path.basename(path), sz / 1048576.0))
            trim_cache()
        else:
            os.unlink(tmp)
    except (OSError, subprocess.SubprocessError) as ex:
        log('   缓存失败（%s）' % ex)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    finally:
        if fh is not False:
            try:
                fh.close()                      # 关掉就是解锁
            except OSError:
                pass


def _dead_map():
    """读"放不了"那张表 → `{原始ID: 记下的时刻}`。"""
    try:
        with open(DEAD_F) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_dead(t):
    """这首是不是刚撞过墙（★ 只看一天内的记录）。"""
    ts = _dead_map().get(str(t.get('orig')))
    return bool(ts) and time.time() - ts < DEAD_TTL


def _mark_dead(t):
    """记下"这首放不了"。

    ★★ 为什么必须有这张表（实测逼出来的）：个人账号在这张歌单上
      **25 首只有 10 首能放**（版权，CLI 原话「该歌曲暂无音源或暂无播放权限」）。
      不记的话，每一声铃都有 60% 概率去撞同一堵墙 —— 而撞墙那一趟要
      **4 秒**，撞完还得再挑一首，主人的闹钟就成了"先静默 4 秒、然后往往
      掉回本地那唯一一首吉他曲"。
    ★ 只记一天（`DEAD_TTL`）：版权是活的，拉黑了绝不能是一条死路。
    ★★ 2026-09-23 晚收紧了**谁有资格调它**：只有网易云**明说**
      「该歌曲暂无音源或暂无播放权限」（`_ncm_said_dead`）才算数。
      锁没轮到、超时、CLI 起不来 —— 那些是**我们这边**的毛病，不许记在歌头上。
    """
    with _DEAD_LOCK:
        d = _dead_map()
        d[str(t.get('orig'))] = time.time()
        try:
            os.makedirs(os.path.dirname(DEAD_F), exist_ok=True)
            tmp = DEAD_F + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(d, f)
            os.replace(tmp, DEAD_F)
        except OSError:
            pass


def _mark_alive(t):
    """这首拿到了地址 ⇒ 把它从"放不了"里摘掉（版权回来了）。"""
    k = str(t.get('orig'))
    with _DEAD_LOCK:
        d = _dead_map()
        if k in d:
            d.pop(k)
            try:
                tmp = DEAD_F + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(d, f)
                os.replace(tmp, DEAD_F)
            except OSError:
                pass


def cached_of(t):
    """这首的缓存文件路径 —— 有（且够大）才算命中。"""
    p = cache_of(t)
    try:
        return p if os.path.getsize(p) > 65536 else ''
    except OSError:
        return ''


def cached_tracks():
    """缓存目录里**已经下好的**那些 → 还原成"曲目"的样子（`path` 指到盘上的文件）。

    ★ 为什么要还原：文件名就是 `_safe(歌名)-原始ID.mp3`（见 `cache_of`），
      歌名和 ID 都在里面 —— 够我们**不去问网易云任何事**就把它放出来。
    ★ 名字是 `_safe()` 过的（空格变下划线、截 50 字），所以只配给
      `cached_by_name()` 做**宽容匹配**用，绝不拿去当精确歌名往外报。
    """
    out = []
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith('.mp3'):
            continue
        stem, _, orig = n[:-4].rpartition('-')
        if not stem or not orig:
            continue
        p = os.path.join(CACHE_DIR, n)
        try:
            if os.path.getsize(p) <= 65536:
                continue
        except OSError:
            continue
        out.append({'name': stem.replace('_', ' ').strip(), 'orig': orig,
                    'id': '', 'dur': 0, 'play': True, 'path': p, 'artist': ''})
    return out


def _nrm(s):
    """比名字之前先归一：只留小写字母数字汉字（空格、括号、标点全不要）。"""
    return re.sub(r'[^0-9a-z一-鿿]', '', str(s or '').lower())


def cached_by_name(q):
    """按主人说的名字，在**已经缓存好的**曲子里找 → 命中就 0 网络、0 等待。

    ★★★ 为什么值得单独做：`search_songs()` 是**一次官方 CLI 调用（实测 1.5~3 秒）**，
      而缓存命中时我们**根本不需要**问网易云 —— 歌已经在盘上、而且已经验证过能放。
      实测：同一首曲子第二次点（"夜的钢琴曲五"），走这儿 **0.1 秒**出声，
      不走这儿是 **2.98 秒**（那 2.9 秒全花在搜索上）。
    ★★ 为什么只认**唯一**命中：宁可慢一次，也**不能放错歌**。
      "夜的钢琴曲"在缓存里能同时匹配《夜的钢琴曲五》和《夜的钢琴曲 (DJ版)》
      ⇒ 这是模棱两可，回 None 让搜索那条路去定夺（它知道原版/别的版本）。
    """
    nq = _nrm(q)
    if len(nq) < 2:
        return None
    hit = None
    for t in cached_tracks():
        nt = _nrm(t['name'])
        if not nt:
            continue
        if nt == nq:
            return t                     # 一模一样 ⇒ 就是它
        if nq in nt or nt in nq:
            if hit:
                return None              # 不止一个候选 ⇒ 别猜
            hit = t
    return hit


def pick_named(q, limit=6):
    """点播一首**点名**的歌 → `(挑中的那首, 搜到的全部)`。

    ★ 两条路，**盘上有就先信盘**：缓存里命中就直接给（0 网络），
      没有才去问网易云搜（1.5~3 秒，还得挑一个 `playFlag` 为真的）。
    ★ 电话和音箱**共用这一个函数** —— 这就是"同脑"落到点歌上的样子，
      省得两边各写一份、日后各自长歪。
    """
    fast = cached_by_name(q)
    if fast:
        return fast, [fast]
    got = search_songs(q, limit)
    return next((s for s in got if s['play']), None), got


def pick_random_cached():
    """随机一首**已经缓存的**（缓存空 ⇒ None）。

    ★ 为什么轮得到它（2026-09-23 晚）：主人说"放首歌听听"，原来走到的是
      `music/` 那个目录 —— 里面**只有一首**吉他曲，所以"随机"等于"又放这首"。
      而缓存里躺着的是歌单里**验证过能放**的十来首 ⇒ 用它才算真随机，
      而且 0 网络、秒开。缓存空（刚开机、盘紧）时照旧退回本地曲库。
    """
    got = cached_tracks()
    return random.choice(got) if got else None


def song_src(t):
    """**指定的**那一首 → 能直接播的源。返回 `(src, 说明)`，拿不到 `(None, 原因)`。

    ★ 跟 `online_src()` 的分工：那个是"随便来一首"（闹钟要的），
      这个是"我就要这一首"（主人点歌要的）。
    ★ `src` 两种，调用方两种都要认：`http(s)://…` 或本地缓存路径。
    ★ `t['path']` 优先：那来自 `cached_tracks()`（名字是 `_safe()` 过的、
      再 `_safe()` 一次**不一定**还原出同一个文件名）⇒ 有现成的路径就直接用。
    """
    cp = t.get('path') or cache_of(t)
    try:
        if os.path.getsize(cp) > 65536:
            return cp, '缓存《%s》' % t['name']
    except OSError:
        pass
    url, why, dead = fetch_url(t)
    if not url:
        if dead:
            _mark_dead(t)
            return None, ('《%s》拿不到播放地址 —— 网易云说「%s」，'
                          '这首这个账号放不了' % (t['name'], why))
        # ★ 说不出"放不了"就别栽赃版权：如实说这次没取到，让它过会儿再点
        return None, '《%s》这次没取到地址（%s）—— 过一会儿再点一次' % (t['name'], why)
    _mark_alive(t)
    threading.Thread(target=cache_fill, args=(url, cp), daemon=True).start()
    return url, '在线《%s》' % t['name']


def online_src():
    """在线铃声源（网易云官方 CLI）。返回 `(src, 说明)`；拿不到就 `(None, 原因)`。

    `src` 有两种，**调用方两种都要认**：
      · `http(s)://…` —— 现取的播放直链，**设备自己去流式拉**（不落盘）
      · 本地路径     —— 早就缓存好的（`music/cache/`），`ring()` 会摆渡到 /tmp

    ★★ 挑曲顺序是被实测改过的，不是随手写的：
      ① **有缓存的优先** —— 缓存过就证明**这首真能放**，而且 0 网络、秒开、
         断网也响。`warm_cache()` 把这条路提前铺好，所以正常情况永远走这支。
      ② 没缓存才现取，**跳过刚撞过墙的**，而且一首不行**接着试下一首**
         （不是试一首就放弃 —— 那等于把 60% 的失败率直接摊给主人）。
      ③ 全都不行 ⇒ 如实回 None，让 `ring()` 兜到本地。

    ★ 拿不到就**如实回原因**，绝不写一个"假装成功"的返回 —— 那会让日志
      看着像在线一直正常工作，而其实每一声都是本地，是这个项目最怕的形状。
    """
    if not os.path.exists(NCM_CLI):
        return None, '官方 CLI 不在（%s）' % NCM_CLI
    got = tracks()
    if not got:
        return None, '歌单曲目取不到'

    have = [t for t in got if cached_of(t)]
    if have:
        t = random.choice(have)
        return cached_of(t), '缓存《%s》' % t['name']

    alive = [t for t in got if not _is_dead(t)] or got   # 全被拉黑 ⇒ 是时候重探一遍
    # ★★ 先用 `playFlag` 筛一遍（实测零误差）：这样挑中的那首**基本不会撞墙**，
    #    省掉那 4 秒。剩下的漏网（playFlag 说能、实际取不到）由黑名单兜。
    can = [t for t in alive if t.get('play', True)]
    if can:
        alive = can
    random.shuffle(alive)
    tried, last = 0, ''
    for t in alive[:3]:
        tried += 1
        url, why, dead = fetch_url(t)
        if url:
            _mark_alive(t)
            threading.Thread(target=cache_fill,
                             args=(url, cache_of(t)), daemon=True).start()
            return url, '在线《%s》' % t['name']
        last = why or '没拿到地址'
        if dead:
            _mark_dead(t)
            log('   ⓘ 《%s》放不了（%s），换下一首' % (t['name'], why))
        else:
            # ★ 我们这边的问题（锁没轮到/超时/CLI 起不来）⇒ **不拉黑**：
            #   记进黑名单等于替主人认定"这首没了"，而其实一次都没试成。
            log('   ⓘ 《%s》这次没取到地址（%s）—— 不拉黑，下回还能试'
                % (t['name'], why))
    return None, '试了 %d 首都没拿到播放地址（最后一次：%s）' % (tried, last)


def search_songs(kw, limit=6):
    """搜网易云的歌 → 候选表（带"能不能放"）。

    给语音助手"点歌"用（主人 2026-09-23：「给音箱也加个工具调用网易云的各种
    cli 能力」）。

    ★★ 每首**如实带上 `play`**，而不是在这儿偷偷把放不了的滤掉 —— 这是主人
      一贯的形状（见 memory `spk-brain-first-principle`：**能变成一条提示词，
      就别写逻辑**）：把真相喂给大脑，让它自己挑、自己跟主人说"这首放不了，
      换一首要不要"。代码替它决定，就再也说不清"为什么它从不提那几首"。
    """
    if not os.path.exists(NCM_CLI):
        return []
    j = _ncm(['search', 'song', '--keyword', kw, '--limit', str(limit)], timeout=40)
    recs = ((j or {}).get('data') or {}).get('records')
    if not isinstance(recs, list):
        return []
    out = []
    for s in recs:
        if not (s.get('id') and s.get('originalId')):
            continue
        out.append({'name': s.get('name') or '?', 'id': s.get('id'),
                    'orig': s.get('originalId'), 'dur': s.get('duration') or 0,
                    'play': s.get('playFlag') is not False,
                    'artist': '/'.join(a.get('name') or ''
                                       for a in (s.get('artists') or [])[:2])})
    return out


def warm_cache():
    """闲时把歌单里**能放**的那些**提前缓存成本地文件**。

    ★★★ 为什么非要有这个后台（实测逼出来的）：个人账号在这张歌单上
      **25 首只有 10 首能放**。响铃那一刻现取 —— 中的那首 1.2 秒、
      没中的那首 4 秒，还得再挑一次。闹钟最不能接受的就是"到点了先静默
      四秒，然后大概率掉回本地那唯一一首吉他曲"。
      ⇒ 提前把能放的全存下来，响铃时**必定命中本地文件**：0 网络、秒开、
        断网照响。而"缓存目录里有它"这件事本身，就等于一张
        **"这些歌真能放"的白名单**，比任何元数据都可靠。

    ★ 三条纪律：
      · **串行 + 每首之间歇一下** —— 别把带宽打满（并行大流量会拖慢同网的其他设备）。
      · **绝不挡响铃** —— 整个跑在 daemon 线程里，`ring()` 根本不看它；
        真人在取地址时它抢不到锁就**让路**（`fetch_url` 的 `wait` 那一段）。
      · **不吵** —— 一首一行日志，够复盘就行。

    ★★ 2026-09-23 晚主人拍板**开**（原话「**开预热吧**，边播边缓存
      但**注意大小 别撑爆了硬盘**」）⇒ 这是他明确要的，不是我自己开的。
      大小那三道红线的说明见文件头 `CACHE_MAX` 那一段。
    """
    time.sleep(30)                     # 让服务先站稳，别一开机就跟别人抢网络
    while True:
        try:
            trim_cache()               # ★ 先按红线收一遍（含上回下崩留下的 .part）
            got = tracks()
            # ★ 只下 `playFlag` 说能放的（见 `tracks()` 那段）：下了半天发现是
            #   版权墙，纯属白花主人的流量。
            todo = [t for t in got if not cached_of(t) and not _is_dead(t)
                    and t.get('play', True)]
            if todo:
                log('♫ 后台预热：还有 %d 首没缓存，开始慢慢下（%s）'
                    % (len(todo), cache_report()))
            for t in todo:
                if cached_of(t):
                    continue
                cp = cache_of(t)
                url, why, dead = fetch_url(t)
                if url:
                    _mark_alive(t)
                    cache_fill(url, cp)          # 这里**同步**做，天然串行
                elif dead:
                    _mark_dead(t)
                    log('   ⓘ 预热时《%s》放不了（%s），跳过' % (t['name'], why))
                else:
                    # ★ 真人正在点歌/响铃时抢不到锁是**正常**的，我们让路
                    log('   ⓘ 预热时《%s》没取到地址（%s），下轮再说'
                        % (t['name'], why))
                time.sleep(2)
        except Exception as ex:                      # noqa: BLE001
            log('预热线程出错：%s' % ex)
        time.sleep(6 * 3600)             # 一轮跑完歇六小时，再看有没有新歌/新版权



def sw_show_safe():
    """`sw_show()` 的兜底版：**读不到就写"?"，绝不抛**。理由见 `ring()` 里 ② 那段。"""
    try:
        return sw_show()
    except Exception as ex:                       # noqa: BLE001
        return '?（读不到：%s）' % ex


def ring(alarm):
    """响一次铃。返回实际响了多少秒。

    ★★★ 两条"必须活着"的规矩，都是为了让这一铃**真的响出来**：
      ① 铃声来源 = **主路在线、兜底本地**（主人 2026-09-23 拍板「可以加兜底」）。
         在线那条路要网络 + 接口活着 —— 断了闹钟就不响，而闹钟的全部意义就是
         "一定会响"（这正是它跟"放音乐"最大的区别）⇒ 拿不到在线源就退回本地。
      ② **闸门那几步全部包 try**：它们要 adb，而 adb 掉线跟"这一铃该不该响"
         没有半点关系。原来 adb 一抛，整次 `ring()` 就被掀掉，日志里只剩一句
         `循环里出错` —— **主人看到的就是"闹钟没响"**。
         （★ 601 播放本身走 spk-netd，不经过 adb ⇒ adb 挂了白天照样能响，
           只有夜间开闸门那一步真需要它。）
    """
    limit = float(alarm.get('minutes', 3)) * 60

    # ---- 挑源 ----
    # ★★ 2026-09-23 多了"**点播**"这条路：`src` 由调用方**指定**（主人点名要听的
    #    那一首，见 `spk_skills` 的 `play_music(song=…)` 和 `song_src()`）。
    #    ⇒ 指定了就照办，不再随机挑。
    #    ★ 随机挑是**闹钟**要的（同一条"每天七点"现挑才天天换曲子），
    #      点播要的正好相反 —— "我就要这一首"。
    src, tag = None, ''
    preset = alarm.get('src')
    if preset:
        if preset.startswith('http://') or preset.startswith('https://'):
            src, tag = preset, alarm.get('tag') or '点播'
        else:
            src, tag = stage(preset), alarm.get('tag') or '点播'
    else:
        # ---- 闹钟：在线优先；拿不到 ⇒ 退回本地（★ 保证"一定有声音"）----
        try:
            on, why = online_src()
        except Exception as ex:                   # noqa: BLE001
            # ★★ 在线源要走**网络**，它抛异常必须被吞在这一步 ——
            #    否则"兜底"根本兜不住：异常会在挑源这里就把整次 `ring()` 掀掉，
            #    连本地那首都不会放。
            on, why = None, '在线源出错（%s: %s）' % (type(ex).__name__, ex)
        if on:
            if on.startswith('http://') or on.startswith('https://'):
                src, tag = on, why or '在线'
            else:
                # ★ 缓存命中的是 `music/cache/` 下的**本地文件**，而 `spk_ctl`
                #   只认 /tmp 下的路径（8899 静态服务器的根就是 /tmp，见 `_src()`）
                #   ⇒ 必须摆渡一次，否则直接抛 ValueError。
                src, tag = stage(on), why or ('缓存 ' + os.path.basename(on))
        else:
            log('   ⓘ 在线源跳过：%s ⇒ 兜底走本地' % why)
            # ★ `music` 空（或压根没这个字段）= 到点现挑一首，见 `RANDOM` 那段。
            name = alarm.get('music') or pick_random()
            if not name:
                log('★ 音乐库是空的，响不了（在线源又没接上）')
                return 0
            path = os.path.join(MUSIC_DIR, name)
            if not os.path.exists(path):
                log('★ 音乐文件不在，跳过：%s' % path)
                return 0
            src, tag = stage(path), '本地 %s' % name

    # 只有夜间才需要动闸门；白天开关本来就是 on，别去碰人家的状态
    night = bool(dlna.NIGHT_QUIET and dlna.in_night())
    log('★ 响铃 %s  %s  （%s）  %s'
        % (alarm.get('time', '?'), tag,
           '夜间 · 开闸门' if night else '白天 · 闸门本来就开着',
           '开关 %s' % sw_show_safe()))

    if night:
        try:
            ring_flag(True)          # ★ 顺序要紧：先让 nightmute 撒手，再开开关
            time.sleep(0.2)          #    （反过来的话守护正好在那一刻把它压回去）
            sw_set('on')
            time.sleep(0.3)
            log('   闸门已开（开关 %s）' % sw_show_safe())
        except Exception as ex:                   # noqa: BLE001
            log('★★ 夜间闸门打不开（%s）—— 这一铃**大概率是哑的**，adb 掉线了？'
                % ex)

    t0 = time.time()
    try:
        spk_ctl.play(src)
        while time.time() - t0 < limit:
            time.sleep(POLL)
            st = (spk_ctl.status() or '').upper()
            # ★★ 2026-09-23：`?` = **问不出来**，不是"停了"。闹钟的 whole point 是
            #   把人叫醒 ⇒ 判据瞎了的时候绝不能当成"响完了"。601 出口是一条
            #   设备往返探针（`music_st`），它偶尔读不到；原来这一行会把读不到
            #   当成 STOPPED ⇒ **闹钟响 5 秒就收工**，而主人只会觉得"闹钟没响"。
            #   ⇒ 只有【它明确说自己不在播】才收工，否则一直守到 limit。
            if st and st != '?' and 'PLAYING' not in st and 'TRANSITION' not in st:
                break
    except Exception as ex:                       # noqa: BLE001
        log('★ 播放失败：%s' % ex)
    finally:
        try:
            spk_ctl.stop()
        except Exception:                         # noqa: BLE001
            pass
        if night:
            # ★ 收闸门也不能抛：它在 finally 里，一抛会盖掉上面所有信息，
            #   而且会把闸门**留在开着**的状态（夜里那是不该有的）。
            try:
                sw_set('off')
                ring_flag(False)
                log('   闸门已收（开关 %s）' % sw_show_safe())
            except Exception as ex:               # noqa: BLE001
                log('★★ 夜间闸门收不回来（%s）—— 得手动把开关扳回 off' % ex)

    dur = time.time() - t0
    log('   响完，历时 %.0f 秒' % dur)
    return dur


def ring_bg(alarm):
    """后台响铃 —— 给"放点轻音乐"用（语音助手要它立刻回话，不能陪着等播完）。
    ring() 会一直轮询到播完（最长 minutes 分钟），所以必须丢进线程。"""
    t = threading.Thread(target=ring, args=(alarm,), daemon=True)
    t.start()
    return t


def load():
    try:
        with open(TABLE) as f:
            return json.load(f)
    except (OSError, ValueError) as ex:
        log('闹钟表读不了（%s）：%s' % (TABLE, ex))
        return {'alarms': []}


def save(d):
    """写回表。先写临时文件再 replace —— 服务端每 5 秒就要读一次这张表，
    绝不能让它读到写了一半的 JSON（读不动就整张表用不了，等于闹钟全丢）。"""
    tmp = TABLE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TABLE)


def music_list():
    try:
        return sorted(n for n in os.listdir(MUSIC_DIR)
                      if n.lower().endswith(('.mp3', '.m4a', '.wav', '.flac')))
    except OSError:
        return []


def pick_music(name):
    """把模型给的名字对上真实文件。★ 模型很爱省略扩展名或只给个大概，
    这里做一次宽容匹配（完全相等 → 去扩展名相等 → 子串），
    但【绝不】接受路径分隔符 —— 否则就是拿模型的话去拼路径，那正是要防的事。"""
    if not name:
        return None
    name = os.path.basename(str(name).strip())
    if '/' in name or '\\' in name or name.startswith('.'):
        return None
    got = music_list()
    if name in got:
        return name
    stem = os.path.splitext(name)[0].lower()
    for n in got:
        if os.path.splitext(n)[0].lower() == stem:
            return n
    for n in got:
        if stem and stem in n.lower():
            return n
    return None


# ★★★ 2026-09-23：`music` 字段存这个值 = **到点现挑**。
#   主人的原方案是「到了播放网易云里的随机一个轻音乐」—— 之前这里是
#   `(music_list() or [None])[0]`，**取的永远是第一首**，"随机"两个字根本没落地。
#   ★ 现挑（放在 ring 里）而不是定闹钟时挑死：同一条"每天七点"的闹钟，
#     每天的曲子都不一样。挑死的话主人天天听同一首，跟随机是两回事。
RANDOM = ''


def pick_random():
    """从音乐库里随机挑一首。库是空的就回 None。"""
    got = music_list()
    return random.choice(got) if got else None


def norm_time(s):
    """把"7:5" "07:05" "7点5分" 之类收敛成 HH:MM。收不了就回 None。"""
    m = re.match(r'^\s*(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*[分]?\s*$', str(s or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return '%02d:%02d' % (h, mi)


def add_alarm(time_s, days=None, music=None, minutes=3, label=''):
    """加一条闹钟。返回 (成功?, 给人看的一句话)。

    `music` 不写 ⇒ 存 `RANDOM`，**每次响铃现挑一首**（主人要的"随机轻音乐"）。
    """
    hhmm = norm_time(time_s)
    if not hhmm:
        return False, '时间没看懂：%r（要 HH:MM 这种）' % (time_s,)
    if not music_list():
        return False, '音乐库是空的'
    if music:
        m = pick_music(music)
        if not m:
            return False, '音乐库里没有 %r，有的：%s' % (music, '、'.join(music_list()))
    else:
        m = RANDOM                       # 空 = 到点现挑（见 ring / RANDOM）
    try:
        mins = max(1, min(60, int(minutes or 3)))
    except (TypeError, ValueError):
        mins = 3
    d = load()
    al = [a for a in d.get('alarms', [])
          if not (a.get('time') == hhmm and a.get('label', '') == label)]
    al.append({'time': hhmm, 'music': m, 'minutes': mins, 'label': label})
    al.sort(key=lambda a: a.get('time', ''))
    d['alarms'] = al
    save(d)
    when = '每天 ' if not days else '周%s ' % ','.join(str(x) for x in days)
    what = ('放 %s' % m) if m else '到点随机挑一首轻音乐'
    return True, '%s%s 定了，%s，最多响 %d 分钟' % (when, hhmm, what, mins)


def del_alarm(time_s=None, label=''):
    """删闹钟：给时刻就删那个时刻的，给 label 就删那个 label 的，都不给就全删。"""
    hhmm = norm_time(time_s) if time_s else None
    if time_s and not hhmm:
        return 0, '时间没看懂：%r' % (time_s,)
    d = load()
    old = d.get('alarms', [])
    if not hhmm and not label:
        keep = []
    else:
        keep = [a for a in old
                if not ((hhmm and a.get('time') == hhmm)
                        or (label and a.get('label') == label))]
    d['alarms'] = keep
    save(d)
    n = len(old) - len(keep)
    return n, ('删掉 %d 条闹钟' % n) if n else '没找到要删的闹钟'


def day_ok(a, now):
    """days 里放 0=周一…6=周日；没写 days 就每天都算。"""
    days = a.get('days')
    if not days:
        return True
    return now.weekday() in [int(d) % 7 for d in days]


def main():
    log('闹钟服务起来 —— 表=%s  音乐目录=%s' % (TABLE, MUSIC_DIR))
    log('   %s' % cache_report())
    # ★★ 开机先扫掉**上一次人生**留下的 ncm 进程 —— 见 `sweep_stale_ncm` 的注释：
    #   漏下来的 daemon 占着 `mpv.sock`，会让**取地址永久拿不到**（而兜底只有
    #   本地那一首吉他曲，主人听到的是"在线源悄悄没了"）。
    #   只收够老的（3 分钟），不会误杀别的进程的在途请求。
    sweep_stale_ncm()
    # ★★ 预热线程**默认开** —— 主人 2026-09-23 晚拍板：
    #   「**开预热吧**，边播边缓存 但**注意大小 别撑爆了硬盘**」。
    #   （他上午的原话是「不要下载 直接流式播放+缓存就行了」，晚上改了口
    #     —— 因为响铃那一刻现取地址要 1.2~4 秒，而闹钟最怕"到点了先静默几秒"。）
    #   ⇒ 现在：闲时**串行**把能放的曲子存下来，响铃时必定命中本地：0 网络、秒开。
    #   ★ 大小由 `CACHE_MAX` / `CACHE_ONE_MAX` / `DISK_FLOOR` 三道红线看着，
    #     每缓存一首都会 `trim_cache()` 一次，日志里能看到删了谁。
    #   ★ 要关：`systemctl edit spk-alarm` → `Environment=SPK_ALARM_WARM=0`
    if os.environ.get('SPK_ALARM_WARM', '1') == '1':
        threading.Thread(target=warm_cache, daemon=True).start()
        log('   （预热已开：闲时会把能放的曲子缓存到本地）')
    fired = set()
    while True:
        try:
            now = datetime.datetime.now()
            hhmm = now.strftime('%H:%M')
            for a in load().get('alarms', []):
                key = (now.strftime('%Y-%m-%d'), a.get('time'), a.get('music', ''))
                if key in fired or a.get('time') != hhmm or not day_ok(a, now):
                    continue
                fired.add(key)
                ring(a)
            if len(fired) > 64:                  # 只留当天，别越攒越多
                today = now.strftime('%Y-%m-%d')
                fired = {k for k in fired if k[0] == today}
        except Exception as ex:                   # noqa: BLE001 —— 常驻进程绝不能因为一次异常死掉
            log('循环里出错：%s' % ex)
        time.sleep(POLL)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--ring':
        # 手动试响一次（不写表、不改 fired），用来验证整条路
        a = {'time': sys.argv[2] if len(sys.argv) > 2 else 'manual',
             'music': sys.argv[3] if len(sys.argv) > 3 else 'alarm-guitar.mp3',
             'minutes': 1}
        ring(a)
    elif len(sys.argv) > 1 and sys.argv[1] == '--list':
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    else:
        main()
