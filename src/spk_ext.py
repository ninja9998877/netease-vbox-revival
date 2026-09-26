#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力插槽 —— 给音箱装"手上没有的本事"，而**不碰核心那张工具表**。

★★★ 为什么要有这个文件（2026-09-23 主人提的需求）：
  主人对音箱说「你搜一下最新新闻」→ 它说没有这个功能 → 主人说「你去增加这个功能」
  → 它求助 → 我（外部的 Claude）把本事写好 → 它当场就能用。
  核心诉求是**装完不用重启音箱**（实测 ≥3 个进程各持一份 `TOOLS`：
  另一个壳那套×2 + spk_ear；重启就得断那条线），所以这里必须支持 mtime 热加载。

★★★ 设计上的四条铁律（改这个文件之前先读，每条都有具体的事故背景）：

  ① **绝不重新赋值 `TOOLS`，只能原地改**：`TOOLS[:] = 核心 + 扩展`。
     因为 `_tools_for(None) is TOOLS` 是"音箱那条路逐字节不变"的判据，
     自检脚本是拿 `id()` 逐个比对的。重新赋值会让那个判据当场失效。
     `BY_NAME` 同理 —— 它是字典，`clear()` + `update()` 原地改。

  ② **本模块绝不 import `spk_skills`** —— 循环导入。
     加载发生在 `spk_skills.py` 模块级（`BY_NAME` 刚建好那会儿），那时 `spk_skills`
     只初始化到一半，回头 import 它拿到的是半成品。
     ⇒ 共享管道（`http_get`/`log`/超时）都放在**这里**，扩展来 import 本模块。

  ③ **`dispatch()` 绝不许返回 `None`、绝不许抛异常**。
     `None` 在 `spk_skills.dispatch` 里是有含义的：**落回音箱出口 = 真出声**
     （`:1164-1166`）—— 扩展回 None 等于让它念出奇怪的东西。异常则会打断整轮。

  ④ **扩展绝不许自己出声**：不许 import `spk_ai_dlna`/`spk_ctl`、不许起播放、不许写音量。
     出声是 `spk_ear`/另一个壳那套的事，扩展只回**字符串**给大脑，由大脑决定怎么念。

★ 开关：`SPK_EXT=0` ⇒ 一个扩展都不加载（回退成一个字节都不改的原始行为）。
"""

import os
import sys
import time
import json
import traceback
import importlib.util
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.environ.get('SPK_EXT_DIR') or os.path.join(HERE, 'ext')

ON = os.environ.get('SPK_EXT', '1') == '1'

# ---------------------------------------------------------------- 额度（★ 必须留代码里）
#   额度/门限/安全一律不许交给模型决定（见记忆 spk-brain-first-principle）。
#   这几条是防呆上限，不是可调参数 —— 撞上了说明扩展写歪了。
MAX_EXTS = int(os.environ.get('SPK_EXT_MAX', '8'))          # 最多装几个扩展
MAX_TOOLS_PER_EXT = int(os.environ.get('SPK_EXT_MAX_TOOLS', '6'))
MAX_PROMPT_CHARS = int(os.environ.get('SPK_EXT_MAX_PROMPT', '1200'))  # 片段总长上限
MAX_TOOL_DESC = 900            # 单条 description 上限（撑爆 schema 会让每轮都变慢）

NET_TIMEOUT = float(os.environ.get('SPK_NET_TIMEOUT', '5.0'))
UA = 'spkbrain/1.0'


# ---------------------------------------------------------------- 日志 / 网络（给扩展用）
def log(msg, *a):
    """跟项目其他地方一个路子：打 stdout，由 systemd 收。"""
    try:
        sys.stdout.write(('  🧩 ' + (msg % a if a else msg)) + '\n')
        sys.stdout.flush()
    except Exception:                                        # noqa: BLE001
        pass


def http_get(url, timeout=None, headers=None, data=None):
    """扩展用的 HTTP GET/POST。**纯 stdlib** —— 这个 venv 里没有 requests（实测）。

    ★ 这一份是 `spk_skills._get()` 的同构复制，**故意不共享**：共享就得 import
      `spk_skills`，那是循环导入（见文件头铁律②）。改一边记得看另一边。
    出错就抛，由扩展自己决定怎么跟用户说 —— 这里不替它吞。
    """
    h = {'User-Agent': UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout or NET_TIMEOUT) as r:
        return r.read()


# ---------------------------------------------------------------- 状态
_tools = None                  # ★ 就是 spk_skills.TOOLS 那个列表对象本身（不复制）
_by_name = None                # ★ 就是 spk_skills.BY_NAME 那个字典对象本身
_core_tools = ()               # 核心工具的快照（装载扩展时用它打底）
_exts = []                     # [{'name':…, 'prompt':…, 'tools':[…], 'mod':…}]
_owners = {}                   # 工具名 → 那个扩展的 dispatch
_sig = None                    # 上次加载时的目录指纹（mtime）
_note = '还没 boot'            # 人看的加载说明（status() 里回）


def boot(tools, by_name):
    """由 `spk_skills` 在模块级调一次：把插槽接上去。

    ★ 收的是**两个对象本身**（TOOLS 列表 + BY_NAME 字典），全程原地改，绝不重新赋值。
    """
    global _tools, _by_name, _core_tools
    _tools, _by_name = tools, by_name
    _core_tools = tuple(tools)
    load(force=True)
    return status()


def _dir_sig():
    """目录指纹 = 每个 .py 的 (名字, mtime_ns, size)。没有 .py（或没目录）⇒ None。

    ★★ 刻意【不看目录自己的 mtime】—— 这是个实测抓到的真缺陷：
      第一次加载扩展会在这里生成 `ext/__pycache__/`，**那会把 ext/ 的 mtime 改掉**，
      ⇒ 紧接着下一次 `maybe_reload()` 必判"变了"，白重载一遍
      （现象：日志里"装了 1 个扩展"打两遍，`warm()` 被白叫两次）。
      而"新增/删除文件"本来就已被下面的 items 覆盖（文件名就在里面）
      ⇒ 目录 mtime 这个分量从一开始就是多余的，去掉它反而更准。
    """
    try:
        items = []
        with os.scandir(EXT_DIR) as it:
            for e in it:
                if e.name.endswith('.py') and not e.name.startswith('_'):
                    st = e.stat()
                    items.append((e.name, st.st_mtime_ns, st.st_size))
    except OSError:              # FileNotFoundError 也是它（目录不存在 = 零扩展）
        return None
    if not items:
        return None
    items.sort()
    return tuple(items)


def maybe_reload():
    """每轮拼提示词/派发前调一次：目录变了就重载。**没变时只是一次 stat。**"""
    if not ON or _tools is None:
        return False
    if _dir_sig() == _sig:
        return False
    load()
    return True


# ---------------------------------------------------------------- 加载
def load(force=False):
    """扫 `ext/*.py` 全量重载。**fail-closed**：坏掉的扩展只记日志，绝不连累别人。"""
    global _exts, _owners, _sig, _note
    if not ON or _tools is None:
        return
    sig = _dir_sig()
    if sig is None:                      # 目录不存在 = 零扩展，这是合法状态
        _exts, _owners, _sig, _note = [], {}, None, '没有 ext/ 目录（零扩展）'
        _apply()
        return
    if not force and sig == _sig:
        return
    _sig = sig

    loaded, skipped = [], []
    for name, _mt, _sz in sig:
        if len(loaded) >= MAX_EXTS:
            skipped.append('%s（超过 %d 个上限）' % (name, MAX_EXTS))
            continue
        try:
            e = _load_one(name)
        except Exception as ex:                              # noqa: BLE001
            skipped.append('%s（%s: %s）' % (name, type(ex).__name__, ex))
            continue
        if e is None:
            continue
        loaded.append(e)

    # 核心优先：扩展跟核心重名 ⇒ 拒载那个工具（绝不覆盖核心行为）
    owners, names = {}, set()
    for e in loaded:
        for t in e['tools']:
            n = t.get('name')
            if n in _core_tools_names():
                skipped.append('%s：工具 %s 跟核心重名' % (e['name'], n))
                continue
            if n in owners:
                skipped.append('%s：工具 %s 跟别的扩展重名' % (e['name'], n))
                continue
            owners[n] = e['dispatch']
            names.add(n)
        e['tools'] = [t for t in e['tools'] if t.get('name') in owners]

    _exts, _owners = loaded, owners
    _note = ('装了 %d 个扩展 / %d 个工具%s'
             % (len(_exts), len(_owners),
                ('；跳过：' + '、'.join(skipped)) if skipped else ''))
    # ★ 只在"真装了东西"或"跳过过东西"时吭一声：零扩展是合法且常见的状态，
    #   每个进程 import 就打印一行会白占日志。
    if loaded or skipped:
        log(_note)
    _apply()


_core_names_cache = None


def _core_tools_names():
    global _core_names_cache
    if _core_names_cache is None:
        _core_names_cache = {t.get('name') for t in _core_tools}
    return _core_names_cache


def _load_one(fname):
    """加载一个扩展文件，校验它的 ABI。不合格就抛（由 load() 兜住）。"""
    path = os.path.join(EXT_DIR, fname)
    modname = 'spk_ext_' + fname[:-3]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('加载不了')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    name = getattr(mod, 'NAME', None) or fname[:-3]
    tools = getattr(mod, 'TOOLS', None)
    disp = getattr(mod, 'dispatch', None)
    prompt = getattr(mod, 'PROMPT', '') or ''

    if not isinstance(tools, list) or not tools:
        raise RuntimeError('TOOLS 必须是 list 且非空')
    if not callable(disp):
        raise RuntimeError('没有可调用的 dispatch(name, args)')
    if len(tools) > MAX_TOOLS_PER_EXT:
        raise RuntimeError('TOOLS 超过 %d 个' % MAX_TOOLS_PER_EXT)
    for t in tools:
        if not isinstance(t, dict) or not t.get('name'):
            raise RuntimeError('TOOLS 里有不合法的条目（每个都得是带 name 的 dict）')
        if len(str(t.get('description', ''))) > MAX_TOOL_DESC:
            raise RuntimeError('工具 %s 的 description 太长了' % t['name'])
        # ★★★ 键名是 `input_schema`（Anthropic 格式），**不是 `parameters`**。
        #   2026-09-23 实测：写成 `parameters` 会被模型 API **静默忽略**，然后回
        #       400 Invalid schema for function '<名字>':
        #           null is not of types "boolean", "object"
        #   ★★ 这不是"那个工具用不了"，是**每一次模型调用全 400** ——
        #      音箱当场变成"我这会儿连不上脑子"，整条对话瘫痪。
        #   ⇒ 这里必须 fail-closed 卡住，把"运行期全挂"降级成"这个扩展不加载 + 一句原因"。
        if 'input_schema' not in t:
            raise RuntimeError(
                '工具 %s 少了 input_schema%s'
                % (t['name'],
                   '（★ 写成 parameters 了 —— 那个键名会被 API 静默忽略，'
                   '后果是每一次模型调用全部 400，改回 input_schema）'
                   if 'parameters' in t else ''))
        if not isinstance(t['input_schema'], dict) \
                or t['input_schema'].get('type') != 'object':
            raise RuntimeError('工具 %s 的 input_schema 必须是 {"type": "object", ...}'
                               % t['name'])

    # ★ 预热：有的扩展（要联网/要合成素材的）希望第一次调用别付冷启动的钱。
    #   失败**不算加载失败** —— 预热只是"更好"，不是"要命"。
    warm = getattr(mod, 'warm', None)
    if callable(warm):
        try:
            warm()
        except Exception as ex:                              # noqa: BLE001
            log('预热失败（不影响加载）：%s: %s' % (type(ex).__name__, ex))

    return {'name': name, 'prompt': str(prompt), 'tools': tools,
            'dispatch': disp, 'mod': mod}


def _apply():
    """把 `核心 + 扩展` 原地写回 TOOLS / BY_NAME。

    ★ 全程原地改（铁律①）：`[:]` 和 `clear()+update()`，一次都不重新赋值，
      所以 `_tools_for(None) is TOOLS` 永远成立。
    """
    ext_tools = [t for e in _exts for t in e['tools']]
    _tools[:] = list(_core_tools) + ext_tools
    _by_name.clear()
    _by_name.update({t['name']: t for t in _tools})
    _prompt_cache.clear()


# ---------------------------------------------------------------- 给 spk_skills 调的四个口子
def handles(name):
    """这个工具名归扩展管吗？"""
    if not ON or _tools is None:
        return False
    maybe_reload()
    return name in _owners


def dispatch(name, args):
    """派发给扩展。**绝不返回 None、绝不抛异常**（铁律③）。"""
    try:
        fn = _owners.get(name)
        if fn is None:
            return '失败：没有这个工具 %r' % (name,)
        r = fn(name, args if isinstance(args, dict) else {})
    except Exception as ex:                                  # noqa: BLE001
        log('扩展 %s 出错：%s: %s' % (name, type(ex).__name__, ex))
        for ln in traceback.format_exc().strip().splitlines()[-4:]:
            log('   %s', ln)
        return '失败：%s 没做成（%s: %s）' % (name, type(ex).__name__, ex)
    if r is None:
        # ★★ 铁律③：None 在 spk_skills.dispatch 里 = 落回音箱出口 = 真出声。
        log('✗ 扩展 %s 回了 None —— 那是"落回音箱出口"的意思，绝不许。已改成一句失败话术', name)
        return '失败：%s 没返回结果（扩展写错了）' % (name,)
    return r if isinstance(r, str) else str(r)


_prompt_cache = {}


def prompt_fragment():
    """扩展要告诉大脑的那段话 —— 拼进 `system_prompt()` 的 `extra` 尾槽。

    ★★ 位置很讲究：**必须排在 `_channel_note(channel)` 之前**。
      渠道说明的性质是"覆写"，得排在被覆写的那些段之后；扩展片段是被覆写的一方。
    """
    if not ON or _tools is None or not _exts:
        return ''
    key = tuple(e['name'] for e in _exts)
    if key in _prompt_cache:
        return _prompt_cache[key]

    parts, total = [], 0
    for e in _exts:
        p = (e['prompt'] or '').strip()
        if not p:
            continue
        if total + len(p) > MAX_PROMPT_CHARS:
            log('提示词片段超上限（%d 字），扩展 %s 这段不拼了' % (MAX_PROMPT_CHARS, e['name']))
            continue
        total += len(p)
        parts.append(p)
    out = ('\n' + '\n'.join(parts) + '\n') if parts else ''
    _prompt_cache[key] = out
    return out


def status():
    """给体检/自测看的状态 —— 判据要"看它留下的状态"，不是"看它怎么说"。"""
    return {
        'on': ON,
        'dir': EXT_DIR,
        'note': _note,
        'exts': [e['name'] for e in _exts],
        'tools': sorted(_owners),
        'core_tools': len(_core_tools),
        'prompt_chars': len(prompt_fragment()),
        'in_place': (_tools is not None and _by_name is not None),
    }


def selftest():
    """所有扩展的离线自测。返回 [(扩展名, ok, 说明)]。**不碰设备、不出声。**"""
    out = []
    for e in _exts:
        fn = getattr(e['mod'], 'SELFTEST', None)
        if not callable(fn):
            continue
        try:
            r = fn()
            out.append((e['name'], bool(r), '' if r else 'SELFTEST 回了假'))
        except Exception as ex:                              # noqa: BLE001
            out.append((e['name'], False, '%s: %s' % (type(ex).__name__, ex)))
    return out


if __name__ == '__main__':                                   # 手动看状态用
    print(json.dumps({'dir': EXT_DIR, 'sig': str(_dir_sig()), 'on': ON},
                     ensure_ascii=False, indent=1))
