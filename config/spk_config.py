"""集中配置 —— 所有"因环境而异"的值都从这里取。

★ 设计原则
  1. **默认值只保证"能 import、看起来像那么回事"**，不保证能连上你的设备。
     真正要填的值：把 `spk.env.example` 抄成 `spk.env` 放同目录，改掉。
  2. **优先级：环境变量 > spk.env 文件 > 这里的默认值。**
  3. `spk.env` **绝不进仓库**（.gitignore 已拦）。

用法：
    from spk_config import HOST, PORT_MP3, DATA_DIR
"""

import os
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent


def _load_env_file():
    """把同目录的 spk.env 读进环境变量（不覆盖已存在的）。

    ★ 用 setdefault 而不是直接赋值：**环境变量优先**。
    这样 systemd 里设的值不会被文件盖掉。
    """
    p = _HERE / 'spk.env'
    if not p.is_file():
        return
    for raw in p.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── 仓库根目录（不要在别处写死绝对路径）──────────────────────
ROOT = _HERE.parent

# ── 跑"大脑"的那台机器 ──────────────────────────────────────
# ★ 默认 127.0.0.1 = "就在本机"。设备要能连上它。
HOST = os.environ.get('SPK_HOST', '127.0.0.1')

# ── 端口 ────────────────────────────────────────────────────
PORT_MP3 = _int('SPK_PORT_MP3', 8899)       # 静态 mp3 / 音频文件的 HTTP 服务
PORT_NETD = _int('SPK_PORT_NETD', 8898)      # 设备侧主动来拉命令的通道
PORT_TTS = _int('SPK_PORT_TTS', 8896)       # 流式 TTS 端点
PORT_MICTAP = _int('SPK_PORT_MICTAP', 9998)  # 麦克风截流的 UDP 落点

# ── 你那台音箱的地址（不用就留空）───────────────────────────
DEVICE_IP = os.environ.get('SPK_DEVICE_IP', '')

# ── 数据目录（记忆、会话、日志都落这儿）─────────────────────
DATA_DIR = pathlib.Path(os.environ.get('SPK_DATA_DIR', str(ROOT)))

# ── 大模型（"脑子"）────────────────────────────────────────
# ★ 只写端点，**密钥走环境变量，绝不落文件**。
LLM_BASE = os.environ.get('DS_BASE', 'https://api.deepseek.com/anthropic')
LLM_MODEL = os.environ.get('SPK_LLM_MODEL', 'deepseek-chat')

# ── 语音合成（"嗓子"）──────────────────────────────────────
# 可换任意一家；引擎名见 src/spk_voice.py
TTS_ENGINE = os.environ.get('SPK_TTS_ENGINE', 'qwen')
TTS_VOICE = os.environ.get('SPK_QWEN_VOICE', 'Maia')

# ── 设备侧路径（设备上的绝对路径，一般不用改）───────────────
DEVICE_DIR = os.environ.get('SPK_DEVICE_DIR', '/mnt/UDISK/spk')
DEVICE_TMP = os.environ.get('SPK_DEVICE_TMP', '/tmp/spk')

# ── 只允许哪些来源连本机的服务（CIDR，逗号分隔）─────────────
# ★ 默认 = 本机 + 三段私有网 + 运营商级 NAT 段。
#   想收紧就改成你自己的网段，例如 `192.168.1.0/24`。
# ★ 为什么默认给得比较宽：这是**通用默认值**，写死某一段就等于
#   "别人照抄之后连不上，而且不报错"。真要安全靠的是"这些服务只绑内网"，
#   不是靠这张表 —— 它只是第二道。
LAN_CIDR = os.environ.get(
    'SPK_LAN_CIDR',
    '127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10')


def lan_networks():
    """把 LAN_CIDR 拆成 ipaddress 网络对象列表（给白名单判断用）。

    写不出来的那几段**静默跳过** —— 一个有笔误的网段不该让整个服务起不来。
    """
    import ipaddress
    out = []
    for s in LAN_CIDR.split(','):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(ipaddress.ip_network(s))
        except ValueError:
            pass
    return out


def url(port, path=''):
    """拼一个指向本机某个服务的 URL。"""
    return 'http://%s:%d/%s' % (HOST, port, path.lstrip('/'))


if __name__ == '__main__':
    for k in ('ROOT', 'HOST', 'PORT_MP3', 'PORT_NETD', 'PORT_TTS',
              'PORT_MICTAP', 'DEVICE_IP', 'DATA_DIR', 'LLM_BASE', 'TTS_ENGINE'):
        print('%-12s = %s' % (k, globals()[k]))
