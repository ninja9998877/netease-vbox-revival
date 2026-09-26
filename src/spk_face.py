#!/usr/bin/env python3
"""9×9 单色点阵表情 GIF 生成器 —— 给音箱正面那块点阵屏用。

规范（2026-09-22 实测自出厂图标，别改）：
  · 恰好 9×9 像素
  · 只有 黑(000000)底 + 白(ffffff)图 两色 —— 点阵是【单色屏】
    （证据：IconTixing001.jpg 只有 020202/ffffff；IconZhuangtai003.gif 只有 000000/ffffff）
  · GIF 格式，可多帧动画（出厂 Iconstart002.gif 37 帧、IconZhuangtai003.gif 90 帧）
  · 约 3 fps 观感自然

用法:
    python3 spk_face.py                 # 列出所有表情
    python3 spk_face.py smile           # 生成 /tmp/face_smile.gif
    python3 spk_face.py smile /tmp/x.gif
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

W = H = 9
FPS = 3
FFMPEG = shutil.which('ffmpeg') or '/usr/bin/ffmpeg'

# 每个表情 = 帧列表；每帧 = 9 行 9 字符（'#'=亮 / '.'=灭）
FACES = {
    # 微笑 → 中笑 → 张嘴大笑 → 中笑
    'smile': [
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.",".#.....#.","..#####.."],
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#######.",".#.....#.","..#####.."],
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.",".#.....#.","..#####.."],
    ],
    # 大笑：嘴一直张着，最后闭上
    'laugh': [
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#######.",".#.....#.","..#####.."],
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#######.",".#######.","..#####.."],
        ["#.......#",".........", "..#...#..","..#...#..",".........",".........",".#######.",".#.....#.","..#####.."],
    ],
    # 眨眼：右眼闭上
    'wink': [
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#..##..","..#......",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.","..#...#..","...###..."],
    ],
    # 心形（脉动）
    'heart': [
        [".........","..##.##..",".#######.",".#######.",".#######.","..#####..","...###...","....#....","........."],
        [".........","..##.##..",".#######.",".#######.",".#######.","..#####..","...###...","....#....","........."],
        [".........",".........", "..#.#.#..","..#####..","..#####..","...###...","....#....",".........","........."],
    ],
    # 哭：眼泪往下掉
    'cry': [
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........","#........", "..#...#..","..#...#..","#........",".........",".#.....#.","..#...#..","...###..."],
        [".........","#.......#", "..#...#..","..#...#..","#.......#",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#...#..","..#...#..","#.......#",".........",".#.....#.","..#...#..","...###..."],
    ],
    # 眨眼（双眼同时）
    'blink': [
        [".........",".........", "..#...#..","..#...#..",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#...#..",".........",".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", ".........", ".........", ".........",".........",".#.....#.","..#...#..","...###..."],
        [".........",".........", "..#...#..",".........",".........",".........",".#.....#.","..#...#..","...###..."],
    ],
    # 惊讶：圆嘴
    'wow': [
        [".........",".........", "..#...#..","..#...#..",".........",".........", "...###...","...#.#...","...###..."],
        [".........",".........", "..#...#..","..#...#..",".........",".........", "...###...","...###...","...###..."],
    ],
    # 生气：眉毛下压
    'angry': [
        [".........",".........", ".#.....#.","..#...#..",".........",".........",".#######.",".#.....#.","..#####.."],
        [".........",".........", "#.......#","..#...#..",".........",".........",".#######.",".#.....#.","..#####.."],
    ],
}


def _write_ppm(path, grid):
    """写 P6 PPM：'#' → 白，'.' → 黑。grid = H 行，每行 W 个字符。"""
    if len(grid) != H:
        raise ValueError('帧必须 %d 行，收到 %d' % (H, len(grid)))
    with open(path, 'wb') as f:
        f.write(b'P6\n%d %d\n255\n' % (W, H))
        for row in grid:
            if len(row) != W:
                raise ValueError('每行必须 %d 字符，收到 %d：%r' % (W, len(row), row))
            for ch in row:
                f.write(b'\xff\xff\xff' if ch == '#' else b'\x00\x00\x00')


def render(face, out=None, fps=FPS):
    """把 FACES[face] 渲染成 9×9 单色 GIF，返回输出路径。"""
    if face not in FACES:
        raise SystemExit('没有这个表情：%s（可用：%s）' % (face, ', '.join(sorted(FACES))))
    out = out or '/tmp/face_%s.gif' % face
    tmp = tempfile.mkdtemp(prefix='spkface_')
    try:
        for i, grid in enumerate(FACES[face], 1):
            _write_ppm(os.path.join(tmp, 'f%02d.ppm' % i), grid)
        cmd = [FFMPEG, '-y', '-v', 'error', '-framerate', str(fps),
               '-i', os.path.join(tmp, 'f%02d.ppm'),
               '-vf', 'split[a][b];[a]palettegen=max_colors=2:reserve_transparent=0[p];'
                      '[b][p]paletteuse=dither=none',
               '-loop', '0', out]
        subprocess.run(cmd, check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def verify(path):
    """回验：尺寸 / 帧数 / 是否纯黑白。返回 (ok, 说明)。"""
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries',
         'stream=width,height,nb_frames,pix_fmt', '-of', 'default=nw=1', path],
        capture_output=True, text=True).stdout
    info = dict(l.split('=', 1) for l in probe.strip().splitlines() if '=' in l)
    if info.get('width') != str(W) or info.get('height') != str(H):
        return False, '尺寸不是 %dx%d：%s' % (W, H, info)
    tmp = tempfile.mkdtemp(prefix='spkverify_')
    try:
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', path,
                        os.path.join(tmp, 'v%02d.ppm')], check=True)
        for p in sorted(glob.glob(os.path.join(tmp, 'v*.ppm'))):
            parts = open(p, 'rb').read().split(b'\n', 3)
            w, h = map(int, parts[1].split())
            px = parts[3]
            cnt = Counter(px[i * 3:i * 3 + 3] for i in range(w * h))
            bad = [c for c in cnt if c not in (b'\x00\x00\x00', b'\xff\xff\xff')]
            if bad:
                return False, '非纯黑白：%s' % [c.hex() for c in bad]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True, '%s 帧 / %sx%s / 纯黑白' % (info.get('nb_frames', '?'), W, H)


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print('可用表情：')
        for k in sorted(FACES):
            print('  %-8s %d 帧' % (k, len(FACES[k])))
        print('\n用法: python3 spk_face.py <表情> [输出路径]')
        raise SystemExit(0)
    p = render(args[0], args[1] if len(args) > 1 else None)
    ok, why = verify(p)
    print('%s %s  (%d 字节)  %s' % ('✅' if ok else '❌', p, os.path.getsize(p), why))
    raise SystemExit(0 if ok else 1)
