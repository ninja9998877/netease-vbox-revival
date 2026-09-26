"""配置入口 —— src/ 下的脚本统一从这里取配置。

存在的理由：让 src/ 里的文件只写一行 `from _cfg import ...`，
不用每处都算"config 目录在哪"。

★ 真值在仓库根的 config/spk.env（不进仓库）。见 config/spk.env.example。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'config'))

from spk_config import (  # noqa: E402,F401
    ROOT, HOST, DEVICE_IP, DATA_DIR,
    PORT_MP3, PORT_NETD, PORT_TTS, PORT_MICTAP,
    LLM_BASE, LLM_MODEL, TTS_ENGINE, TTS_VOICE,
    DEVICE_DIR, DEVICE_TMP, LAN_CIDR, lan_networks, url,
)
