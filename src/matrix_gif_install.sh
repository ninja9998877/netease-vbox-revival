#!/bin/bash
# ==============================================================
#  点阵屏(9x9) 自定义 GIF 安装器   matrix_gif_install.sh
# --------------------------------------------------------------
#  把本机（跑"大脑"的那台）上生成好的 9×9 单色 GIF 装进音箱，覆盖/新增点阵图标。
#
#  用法：
#    bash matrix_gif_install.sh <gif> [键名]          # 默认 dry，只打印计划
#    bash matrix_gif_install.sh <gif> [键名] --go     # ★真装
#    bash matrix_gif_install.sh --rollback --go       # 从最近备份恢复
#    bash matrix_gif_install.sh --list                # 看设备上现有的坑位
#
#  键名默认 p703（= 出厂「状态动画 IconZhuangtai003.gif」那个坑），
#  想新增而不是覆盖，传一个出厂的没有的键名，例如 px01。
#
#  ★ 机制（2026-09-22 静态实证，见记忆 [[netease-vbox-led-matrix]]）
#    skins.db 三表 skin/res/upgrade：
#      res (ID, resVersion, localPath, serverURL, md5Chksum)  ← 文件登记
#      skin(ID, keyName, resType, priority, resID, resReadme) ← 键名→res 映射
#    取资源 SQL：skin ⋈ res ON resId=ID，ResCheck(localPath, md5Chksum, resType)
#                判 resOK，ORDER BY priority DESC LIMIT 1
#    ⇒ 只要 priority 比出厂高、md5 对得上，就能盖掉出厂图。
#    ⇒ SQL 里【只有 DROP TABLE upgrade】，skin/res 不会被重建 ⇒ 能活过重启。
#
#  ★★ 一个必须知道的坑：adb push 会【换 inode】（实测 21→193）。
#     而 CC 在 SkinInit 时 ATTACH 了 skins.db，fd 指着旧 inode
#     ⇒ push 完当前进程读不到新内容，必须等/触发 CC 重启才生效。
#     本脚本不主动重启 CC（那是命脉，见铁律），装完只做提示。
#
#  安全保证：
#    · 只动 skins.db 的 skin/res 两表 + /mnt/UDISK/resources/image/ 下的文件
#    · 每次都先备份 skins.db 到 /mnt/UDISK/skins.db.bak-<时间戳>
#    · 不碰串口 / 不改音量 / 不碰网络蓝牙 OTA 闹钟云信 / 不杀 CC
# ==============================================================
set -u

RES_DIR=/mnt/UDISK/resources/image
DB=/mnt/UDISK/skins.db
GO=0
ROLLBACK=0
LIST=0
GIF=""
KEY=""

for a in "$@"; do
  case "$a" in
    --go) GO=1 ;;
    --rollback) ROLLBACK=1 ;;
    --list) LIST=1 ;;
    -*) echo "未知参数: $a" >&2; exit 2 ;;
    *) if [ -z "$GIF" ]; then GIF="$a"; elif [ -z "$KEY" ]; then KEY="$a"; fi ;;
  esac
done

die() { echo "❌ $*" >&2; exit 1; }
sh_() { adb shell "$1" 2>&1 | tr -d '\r'; }

adb get-state >/dev/null 2>&1 || die "adb 连不上音箱"

# ─────────────────────────── --list ───────────────────────────
if [ "$LIST" = "1" ]; then
  echo "设备 $DB 现有坑位（keyName / resType / priority / resID）："
  adb pull "$DB" /tmp/_skins_list.db >/dev/null 2>&1 || die "pull 失败"
  /usr/bin/python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/tmp/_skins_list.db')
print('  -- 表 --')
for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print('   ', n)
print('  -- skin 表（前 40 行）--')
try:
    for r in c.execute("SELECT ID,keyName,resType,priority,resID FROM skin ORDER BY ID LIMIT 40"):
        print('    %-4s %-8s resType=%-3s prio=%-5s resID=%s' % r)
except Exception as e:
    print('    ', e)
print('  -- res 表行数 --')
try:
    print('   ', c.execute("SELECT COUNT(*) FROM res").fetchone()[0])
except Exception as e:
    print('    ', e)
PY
  echo
  echo "设备上 $RES_DIR 内容："
  sh_ "ls -la $RES_DIR 2>/dev/null || echo '(目录不存在)'"
  exit 0
fi

# ─────────────────────────── --rollback ───────────────────────────
if [ "$ROLLBACK" = "1" ]; then
  BAK=$(sh_ "ls -t /mnt/UDISK/skins.db.bak-* 2>/dev/null | head -1")
  [ -n "$BAK" ] || die "设备上没有 skins.db.bak-* 备份"
  echo "将从备份恢复：$BAK"
  if [ "$GO" != "1" ]; then echo "（dry；加 --go 真恢复）"; exit 0; fi
  sh_ "cp -f $BAK $DB && chmod 644 $DB && md5sum $DB $BAK"
  echo "✅ 已恢复。注意：仍需 CC 重启才生效（inode 变了）。"
  exit 0
fi

# ─────────────────────────── 安装 ───────────────────────────
[ -n "$GIF" ] || die "用法: bash matrix_gif_install.sh <gif> [键名] [--go]"
[ -f "$GIF" ] || die "找不到文件：$GIF"
KEY="${KEY:-p703}"
BASE=$(basename "$GIF")
DEST="$RES_DIR/$BASE"

echo "════════════ 点阵 GIF 安装计划 ════════════"
echo "  源文件   : $GIF  ($(stat -c%s "$GIF") 字节)"
echo "  设备落地 : $DEST"
echo "  目标键名 : $KEY   (resType=2 点阵图, priority=999 压过出厂)"
echo

# --- 1) 本机校验：9×9 / 纯黑白 ---
echo "▶ 1/5  校验 GIF 规格"
/usr/bin/python3 - "$GIF" <<'PY' || die "GIF 规格不合格，已中止（未碰设备）"
import subprocess, sys, tempfile, glob, shutil, os
from collections import Counter
p = sys.argv[1]
info = dict(l.split('=',1) for l in subprocess.run(
    ['ffprobe','-v','error','-show_entries','stream=width,height,nb_frames',
     '-of','default=nw=1',p], capture_output=True, text=True).stdout.strip().splitlines() if '=' in l)
if info.get('width') != '9' or info.get('height') != '9':
    print('  ❌ 尺寸不是 9x9：', info); sys.exit(1)
t = tempfile.mkdtemp()
try:
    subprocess.run(['ffmpeg','-y','-v','error','-i',p,os.path.join(t,'v%02d.ppm')], check=True)
    for f in sorted(glob.glob(os.path.join(t,'v*.ppm'))):
        d = open(f,'rb').read()
        parts = d.split(b'\n', 3)
        w,h = map(int, parts[1].split()); px = parts[3]
        bad = [c.hex() for c in Counter(px[i*3:i*3+3] for i in range(w*h))
               if c not in (b'\x00\x00\x00', b'\xff\xff\xff')]
        if bad:
            print('  ❌ 非纯黑白，出现：', bad[:6]); sys.exit(1)
finally:
    shutil.rmtree(t, ignore_errors=True)
print('  ✅ 9x9 / 纯黑白 / %s 帧' % info.get('nb_frames','?'))
PY

# --- 2) 备份 ---
echo "▶ 2/5  备份设备 skins.db"
TS=$(date +%Y%m%d-%H%M%S)
echo "   → $DB.bak-$TS"
[ "$GO" = "1" ] && sh_ "cp -f $DB $DB.bak-$TS && chmod 644 $DB.bak-$TS && ls -la $DB.bak-$TS"

# --- 3) 推 GIF ---
echo "▶ 3/5  推送 GIF 到 $DEST"
[ "$GO" = "1" ] && { sh_ "mkdir -p $RES_DIR"; adb push "$GIF" "$DEST" >/dev/null 2>&1 && sh_ "chmod 644 $DEST && ls -la $DEST"; }

# --- 4) 改 db ---
echo "▶ 4/5  写 skins.db 的 res + skin 表"
MD5=$(md5sum "$GIF" | cut -d' ' -f1)
echo "   md5 = $MD5"
if [ "$GO" = "1" ]; then
  adb pull "$DB" /tmp/_skins_work.db >/dev/null 2>&1 || die "pull 失败"
  /usr/bin/python3 - "$KEY" "$DEST" "$MD5" <<'PY' || die "db 写入失败"
import sqlite3, sys
key, path, md5 = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect('/tmp/_skins_work.db')
cols = lambda t: [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
sc, rc = cols('skin'), cols('res')
print('   skin 列:', sc); print('   res  列:', rc)

# res：先清掉同一个 localPath 的旧行（避免重复），再插
c.execute("DELETE FROM res WHERE localPath=?", (path,))
rid = None
if 'resVersion' in rc and 'serverURL' in rc:
    cur = c.execute("INSERT INTO res (resVersion,localPath,serverURL,md5Chksum) VALUES (?,?,?,?)",
                    (1, path, '', md5))
    rid = cur.lastrowid
else:
    print('   ⚠ res 表结构与预期不同，请人工看列名'); sys.exit(1)
print('   res 新行 ID =', rid)

# skin：同 keyName 的旧行 priority 降下去，插我们的高优先级行（不删出厂行，可回滚）
c.execute("UPDATE skin SET priority=0 WHERE keyName=?", (key,))
if 'resReadme' in sc:
    c.execute("INSERT INTO skin (keyName,resType,priority,resID,resReadme) VALUES (?,?,?,?,?)",
              (key, 2, 999, rid, 'spkbrain custom'))
else:
    c.execute("INSERT INTO skin (keyName,resType,priority,resID) VALUES (?,?,?,?)",
              (key, 2, 999, rid))
c.commit()

# ★ 自检：用 CC 取资源 SQL 的形状复算一遍（本机没有 C 库的 ResCheck，
#   所以去掉 ResCheck、自己比对 md5 —— 这是主人不在场时唯一能验 SQL 对错的办法）
q = ("SELECT s.keyName, s.resType, s.priority, r.localPath, r.md5Chksum "
     "FROM skin s, res r WHERE s.keyName=? AND s.resID=r.ID "
     "ORDER BY s.priority DESC LIMIT 1")
row = c.execute(q, (key,)).fetchone()
if not row:
    print('   ❌ 自检失败：CC 的取资源 SQL 查不到这一行（skin.resID 没对上 res.ID？）'); sys.exit(1)
print('   自检命中: keyName=%s resType=%s priority=%s' % row[:3])
print('             localPath=%s' % row[3])
if row[4] != md5:
    print('   ❌ 自检失败：db 里的 md5 与文件不一致'); sys.exit(1)
print('             md5 ✅ 与文件一致（ResCheck 会返回 0 = 通过）')
print('   ✅ db 写入完成')
PY
  adb push /tmp/_skins_work.db "$DB" >/dev/null 2>&1 && sh_ "chmod 644 $DB && md5sum $DB"
fi

# --- 5) 提示 ---
echo "▶ 5/5  生效说明"
cat <<EOF

────────────────────────────────────────────────────────
$( [ "$GO" = "1" ] && echo "✅ 已装入设备。" || echo "（dry：以上都没真做。加 --go 执行。）" )

★ 生效需要 CC 重启（adb push 换了 inode，CC 的 ATTACH fd 还指旧文件）。
  本脚本不替你重启 CC（命脉，见铁律）。两个选择：
   · 省事：不管它，等音箱下次自然重启后自动生效；
   · 立刻：主人在场时手动重启 CC，并盯着 0x601/灯环是否正常。
────────────────────────────────────────────────────────
回滚：bash matrix_gif_install.sh --rollback --go
查看：bash matrix_gif_install.sh --list
EOF
