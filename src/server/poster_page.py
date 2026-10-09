# -*- coding: utf-8 -*-
"""poster_page.py — 海报体检页 (GET /posters, 2026-09-28)

背景（用户方向 2026-09-27 傍晚定稿）：
  「欧美」「无码」两库的 poster.jpg 要用**完整横图**（缩略图风格），不要裁成 2:3 窄竖条。
  Emby 的卡片形状由 Primary 图的长宽比决定：AR >= 1.4 → 宽卡；AR < 1.4 → 窄卡
  （竖条在卡片上只显示中段，标题被切）。MDC 的 download.crop_type_uncensored 会无条件
  把无码海报裁成 2:3，已在全局改成 None；入库钩子（import_api._poster_wide_fix）再兜一层。
  本页是**第三只眼睛**：把两库现状摊开看，可修的一键修，修完让 Emby 重算形状。

本模块只做三件事，全部不依赖 import_api（避免循环 import）：
  1. render_poster_page()  —— 页面 HTML（暗色，沿用面板风格）
  2. poster_audit(emby)    —— 扫描两库目录 + 读 Emby 侧 AR，出报告
  3. poster_fix(...) / poster_refresh(...) —— 整改（含备份）与定向刷新

⚠️ 整改规则与下面两处必须保持一致（三处同源，改一处要同步改）：
     /root/tools/poster_to_wide.py                 （命令行工具, 存量批处理）
     import_api.py 里的 _poster_wide_fix            （入库完成后就地整改钩子）
   规则：poster 是竖的(AR < 1.4) 且 目录里有横图(thumb > fanart > backdrop, AR > 1.4)
         且 该横图宽度 >= 原竖海报宽度（防止越换越糊）→ 旧 poster 备份到 TRASH，
         横图 copy 成 poster.jpg。幂等：已经是横图就什么都不做。

Emby 侧的对接（读库 AR / 定向刷新 / 全库扫）由调用方以 `emby` 适配字典注入，
见 import_api.py 的 _poster_emby_* —— 本模块不碰 token、host、前缀探测那些事。
"""

import json
import os
import shutil
import subprocess
import time

# 两库根（宿主路径；容器里的 /media 就是 /mnt/g/srtm）
POSTER_ROOTS = (
    ('欧美', '/mnt/g/srtm/已刮削/欧美'),
    ('无码', '/mnt/g/srtm/已刮削/无码'),
)
POSTER_TRASH = '/mnt/g/srtm/_归档/_trash_海报换缩略图_20260927'   # 与命令行工具/钩子同一处备份区

WIDE_AR = 1.4                                    # Emby 卡片形状分界：AR >= 1.4 算宽卡
# Emby 的 poster(primary) 槽位文件名；扩展名 jpg/jpeg/png/webp 都认（MDC 有时写 .jpeg）
POSTER_NAMES = ('poster.jpg', 'poster.jpeg', 'poster.png', 'poster.webp',
                'folder.jpg', 'folder.jpeg', 'folder.png')
CAND_ORDER = ('thumb.jpg', 'thumb.jpeg', 'thumb.png', 'thumb.webp',
              'fanart.jpg', 'fanart.jpeg', 'fanart.png', 'fanart.webp',
              'backdrop.jpg', 'backdrop.jpeg', 'backdrop.png',
              'landscape.jpg', 'landscape.jpeg', 'landscape.png')
# 目录像不像一个「条目」（防止往纯图目录里凭空造 poster；空目录直接不算条目）
ENTRY_HINTS = ('.strm', '.nfo', '.mp4', '.mkv', '.avi', '.wmv', '.ts', '.iso', '.srt')


# ---------------------------------------------------------------- 基础工具

def img_size(path):
    """读图片宽高：先 PIL，退化到 ffprobe；都失败返回 (0, 0)"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        pass
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                              '-show_entries', 'stream=width,height', '-of', 'csv=p=0', path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if ',' in out:
            w, h = out.split(',')[:2]
            return int(w), int(h)
    except Exception:
        pass
    return 0, 0


def _ar(w, h):
    return round(w / h, 3) if w and h else 0.0


def _first_img(dirpath, names):
    """按给定顺序找第一个存在的图片，返回 (文件名, 完整路径, w, h) 或 None"""
    for n in names:
        p = os.path.join(dirpath, n)
        if os.path.isfile(p):
            w, h = img_size(p)
            return n, p, w, h
    return None


def _path_variants(prefix):
    """同一路径的多种写法（Emby 跑在 Windows 上，Path 回 G:\\srtm\\...，我们传 /mnt/g/...）"""
    norm = (prefix or '').replace('\\', '/').rstrip('/')
    out = {norm}
    m = None
    if norm.startswith('/mnt/') and len(norm) > 6:
        drive = norm[5:6].upper()
        rest = norm[7:]
        out.add('%s:/%s' % (drive, rest))
        out.add('%s:\\%s' % (drive, rest.replace('/', '\\')))
        m = True
    if not m and len(norm) > 2 and norm[1] == ':':
        out.add('/mnt/%s/%s' % (norm[0].lower(), norm[2:].lstrip('/')))
    return [v.rstrip('/') for v in out if v]


def _iter_entries():
    """遍历两库里所有「条目目录」（含 poster 或候选横图、且像条目的目录）

    产出 (库名, 根, 目录绝对路径, 相对 '已刮削' 的路径, 目录内文件集)
    注意：系列目录（欧美/无码B 系列）自己不产出条目，只有当它直接放着 poster/图 时才算。
    """
    seen = set()
    for lib, root in POSTER_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过 _归档/_trash 之类的下划线目录（Emby 不认，也别去动）
            dirnames[:] = [d for d in dirnames if not d.startswith('_') and not d.startswith('.')]
            low = {f.lower() for f in filenames}
            has_img = bool(low & (set(POSTER_NAMES) | set(CAND_ORDER)))
            if not has_img:
                continue
            if dirpath in seen:
                continue
            seen.add(dirpath)
            rel = os.path.relpath(dirpath, '/mnt/g/srtm/已刮削')
            yield lib, root, dirpath, rel, low


def _is_entry_dir(files):
    """目录里有没有"条目"该有的东西（strm/nfo/视频/字幕）"""
    return any(os.path.splitext(f)[1].lower() in ENTRY_HINTS for f in files)


def inspect_dir(dirpath):
    """体检单个条目目录，返回状态字典（不做任何写操作）"""
    lib = ''
    for l, r in POSTER_ROOTS:
        if dirpath == r or dirpath.startswith(r.rstrip('/') + os.sep):
            lib = l
            break
    try:
        files = os.listdir(dirpath)
    except Exception:
        files = []
    is_entry = _is_entry_dir(files)
    poster = _first_img(dirpath, POSTER_NAMES)
    cand = _first_img(dirpath, CAND_ORDER)
    info = {
        'lib': lib, 'dir': dirpath, 'rel': os.path.relpath(dirpath, '/mnt/g/srtm/已刮削'),
        'poster': poster[0] if poster else '', 'poster_size': '', 'ar': 0.0,
        'cand': cand[0] if cand else '', 'cand_size': '', 'cand_ar': 0.0,
        'status': 'noposter', 'note': '', 'is_entry': is_entry,
    }
    if poster:
        _, _, pw, ph = poster
        info['poster_size'] = '%dx%d' % (pw, ph)
        info['ar'] = _ar(pw, ph)
    if cand:
        _, _, cw, ch = cand
        info['cand_size'] = '%dx%d' % (cw, ch)
        info['cand_ar'] = _ar(cw, ch)

    # 可用横图：thumb > fanart > backdrop 里第一个够宽的
    usable = None
    for n in CAND_ORDER:
        p = os.path.join(dirpath, n)
        if not os.path.isfile(p):
            continue
        w, h = img_size(p)
        if h and w / h > WIDE_AR:
            usable = (n, w, h)
            break
    if usable:
        info['cand'] = usable[0]
        info['cand_size'] = '%dx%d' % (usable[1], usable[2])
        info['cand_ar'] = _ar(usable[1], usable[2])

    if not poster:
        if usable and is_entry:
            info['status'] = 'fixable'
            info['note'] = '没有 poster，用 %s 建一个' % usable[0]
        else:
            info['status'] = 'noposter'
            info['note'] = '目录里没有 poster 图' + ('' if is_entry else '（且不像条目目录）')
        return info
    if info['ar'] >= WIDE_AR:
        info['status'] = 'wide'
        info['note'] = '已是横图'
        return info
    if usable and (not poster[2] or usable[1] >= poster[2]):
        info['status'] = 'fixable'
        info['note'] = '%s → poster.jpg（旧图备份）' % usable[0]
    else:
        info['status'] = 'nochange'
        info['note'] = '目录内没有更宽的横图（候选也偏竖或更糊）→ 只能从视频抽帧重做'
    return info


def _trash_backup_path(dirpath):
    """旧 poster 的备份落点：TRASH/<库>/<系列>/<条目>/poster.jpg（与命令行工具/钩子同一布局）"""
    dirpath = dirpath.rstrip('/')
    lib = root = ''
    for l, r in POSTER_ROOTS:
        r = r.rstrip('/')
        if dirpath == r or dirpath.startswith(r + os.sep):
            lib, root = l, r
            break
    if lib:
        rel = os.path.join(lib, os.path.relpath(dirpath, root))
    else:
        rel = dirpath.strip('/').replace('/', '_')
    if rel.startswith('..') or os.path.isabs(rel):
        rel = dirpath.strip('/').replace('/', '_')
    bak_dir = os.path.join(POSTER_TRASH, rel)
    return bak_dir, os.path.join(bak_dir, 'poster.jpg')


def fix_dir(dirpath):
    """把一个条目的竖条 poster 换成横图（或补一个缺失的 poster）。返回 (changed, note)。

    与 import_api 的钩子/命令行工具同规则；幂等，可反复调用。
    """
    dirpath = (dirpath or '').rstrip('/')
    if not dirpath or not os.path.isdir(dirpath):
        return False, '目录不存在'
    if not any(dirpath == r.rstrip('/') or dirpath.startswith(r.rstrip('/') + os.sep)
               for _, r in POSTER_ROOTS):
        return False, '不在 已刮削/欧美|无码 范围内，跳过'

    try:
        files = os.listdir(dirpath)
    except Exception:
        files = []
    poster = _first_img(dirpath, POSTER_NAMES)
    if poster and _ar(poster[2], poster[3]) >= WIDE_AR:
        return False, '已是横图，无需整改'

    cand = None
    for n in CAND_ORDER:
        p = os.path.join(dirpath, n)
        if not os.path.isfile(p):
            continue
        w, h = img_size(p)
        if h and w / h > WIDE_AR:
            cand = (n, p, w, h)
            break
    if not cand:
        return False, '没有可用的横图'
    c_name, c_path, cw, ch = cand

    if not poster:
        if not _is_entry_dir(files):
            return False, '目录不像条目目录，跳过'
        try:
            shutil.copy2(c_path, os.path.join(dirpath, 'poster.jpg'))
        except Exception as e:
            return False, '补 poster 失败: %s' % str(e)[:120]
        return True, '原本没有 poster，用 %s (%dx%d) 建了一个' % (c_name, cw, ch)

    p_name, p_path, pw, ph = poster
    if pw and cw < pw:
        return False, '横图 %dx%d 比原 poster 还窄，不换（防降质）' % (cw, ch)

    bak_dir, bak = _trash_backup_path(dirpath)
    if os.path.abspath(bak) == os.path.abspath(p_path):   # 防呆：绝不覆盖源文件
        bak_dir = os.path.join(POSTER_TRASH, dirpath.strip('/').replace('/', '_'))
        bak = os.path.join(bak_dir, 'poster.jpg')
    try:
        os.makedirs(bak_dir, exist_ok=True)
        shutil.copy2(p_path, bak)
        target = os.path.join(dirpath, 'poster.jpg')      # 统一写成 poster.jpg（与命令行工具/钩子一致）
        shutil.copy2(c_path, target)
        if os.path.abspath(p_path) != os.path.abspath(target):
            os.remove(p_path)      # 去掉旧 poster.<别的扩展名>，免得两个 poster.* 并存被抢先读
    except Exception as e:
        return False, '整改失败: %s' % str(e)[:120]
    return True, '%s (%dx%d) → poster.jpg (%dx%d)，旧图备份 %s' % (c_name, cw, ch, pw, ph, bak)


# ---------------------------------------------------------------- 对外三个入口

def poster_audit(emby=None):
    """扫描两库 + 读 Emby 侧 AR，返回报告 dict（只读）"""
    libs = []
    items = []
    for lib, root in POSTER_ROOTS:
        n = {'key': lib, 'root': root, 'total': 0, 'wide': 0, 'tall': 0,
             'fixable': 0, 'nochange': 0, 'noposter': 0}
        libs.append(n)
    lib_of = {n['key']: n for n in libs}

    for lib, root, dirpath, rel, _low in sorted(_iter_entries()):
        it = inspect_dir(dirpath)
        n = lib_of.get(it['lib'])
        if n:
            n['total'] += 1
            if it['status'] == 'wide':
                n['wide'] += 1
            else:
                n['tall'] += 1
                if it['status'] in ('fixable', 'nochange', 'noposter'):
                    n[it['status']] += 1
        items.append(it)

    # Emby 侧 AR（用于对照文件层是否已经生效）
    emby_meta = {'ok': False, 'error': '', 'ts': ''}
    if emby and emby.get('lib_items'):
        emby_meta['ts'] = time.strftime('%F %T')
        try:
            for lib_id, lib_name in emby.get('libraries', ()):
                for eit in emby['lib_items'](lib_id):
                    ep = (eit.get('Path') or '').replace('\\', '/')
                    for it in items:
                        if it['lib'] != lib_name:
                            continue
                        if any(ep == v or ep.startswith(v + '/')
                               for v in _path_variants(it['dir'])):
                            it['emby_id'] = eit.get('Id')
                            it['emby_ar'] = round(float(eit.get('AR') or 0), 3) or 0.0
                            it['emby_stale'] = bool(
                                it['emby_ar'] and abs(it['emby_ar'] - it['ar']) > 0.05)
                            break
            emby_meta['ok'] = True
        except Exception as e:
            emby_meta['error'] = str(e)[:200]

    return {'ts': time.strftime('%F %T'), 'trash': POSTER_TRASH, 'wide_ar': WIDE_AR,
            'libs': libs, 'items': items, 'emby': emby_meta,
            'fixable': sum(1 for i in items if i['status'] == 'fixable'),
            'tall': sum(1 for i in items if i['status'] != 'wide')}


def poster_fix(dirs=None, mode='', emby=None, refresh='auto'):
    """整改。dirs=[目录] 指定；mode='fixable' 表示全部可修项。返回结果 dict。

    refresh='auto' 表示只刷新本条目的 Emby 记录（无 query 的 /Items/{id}/Refresh，
    让它重读本地文件重算 PrimaryImageAspectRatio，不重下 provider 图片）。
    """
    if not dirs:
        if mode == 'fixable':
            dirs = [i['dir'] for i in poster_audit().get('items', [])
                    if i['status'] == 'fixable']
            if not dirs:
                return {'ok': True, 'fixed': [], 'skipped': [], 'refreshed': 0,
                        'msg': '没有可整改项'}, 200
        else:
            return {'error': '需要 dirs 或 mode=fixable'}, 400

    fixed, skipped, changed_dirs = [], [], []
    for d in dirs:
        ok, note = fix_dir(d)
        rec = {'dir': d, 'name': os.path.basename(d.rstrip('/')), 'note': note}
        if ok:
            fixed.append(rec)
            changed_dirs.append(d)
        else:
            skipped.append(rec)

    refreshed = 0
    if refresh == 'auto' and changed_dirs and emby and emby.get('refresh_item'):
        # 先查 Emby 条目 id：复用 audit 的路径映射
        rep = poster_audit(emby=emby)
        for it in rep.get('items', []):
            if it['dir'] in changed_dirs and it.get('emby_id'):
                try:
                    emby['refresh_item'](it['emby_id'])
                    refreshed += 1
                except Exception:
                    pass
    return {'ok': True, 'fixed': fixed, 'skipped': skipped,
            'refreshed': refreshed, 'trash': POSTER_TRASH}, 200


def poster_refresh(dirs=None, all_libs=False, emby=None):
    """让 Emby 重读条目/全库，重算卡片形状。dirs 为空且 all_libs=False 时刷新两库全部。"""
    if not emby or not emby.get('refresh_item'):
        return {'error': 'Emby 未接入'}, 503
    n, errs = 0, []
    targets = []
    if dirs:
        rep = poster_audit(emby=emby)
        want = set(dirs)
        targets = [i for i in rep.get('items', []) if i['dir'] in want and i.get('emby_id')]
    else:
        rep = poster_audit(emby=emby)
        targets = [i for i in rep.get('items', []) if i.get('emby_id')]
    for i, it in enumerate(targets):
        try:
            emby['refresh_item'](it['emby_id'])
            n += 1
        except Exception as e:
            errs.append('%s: %s' % (it['rel'], str(e)[:60]))
        if i and i % 10 == 0:
            time.sleep(1.0)        # 温柔点，别把 Emby 打满
    lib_scan = ''
    if all_libs and emby.get('refresh_all'):
        try:
            lib_scan = str(emby['refresh_all']())
        except Exception as e:
            lib_scan = 'ERR:%s' % str(e)[:80]
    return {'ok': True, 'refreshed': n, 'total': len(targets),
            'errors': errs[:10], 'lib_scan': lib_scan}, 200


# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>海报体检</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:50}
.header h1{font-size:19px;color:#a371f7}
.header .tag{font-size:12px;color:#8b949e;background:#21262d;padding:3px 10px;border-radius:12px}
.header .spacer{flex:1}
.container{max-width:1400px;margin:0 auto;padding:20px 24px}
.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 12px;font-size:13px;cursor:pointer;font-family:inherit}
.btn:hover:enabled{background:#30363d;border-color:#8b949e}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.sm{padding:5px 10px;font-size:12px;text-decoration:none;display:inline-block}
.btn.ok{background:#238636;border-color:#2ea043;color:#fff}
.btn.ok:hover:enabled{background:#2ea043}
.btn.warn{background:#21262d;border-color:#9e6a03;color:#d29922}
.btn.ghost{background:#21262d;border-color:#30363d}
.nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.nav a.btn{background:#21262d;border-color:#30363d;color:#c9d1d9}
.nav a.btn:hover{background:#30363d;border-color:#8b949e;color:#c9d1d9}
.nav a.btn.cur{background:#193656;border-color:#58a6ff;color:#58a6ff}
.nav a.btn.avdb.cur{background:#483600;border-color:#d29922;color:#d29922}
.nav a.btn.posters.cur{background:#3b2a56;border-color:#a371f7;color:#a371f7}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}
.card .lbl{font-size:12px;color:#8b949e}
.card .num{font-size:22px;font-weight:700;margin-top:3px}
.card.wide .num{color:#3fb950}
.card.fixable .num{color:#d29922}
.card.nochange .num{color:#f85149}
.card.total .num{color:#58a6ff}
.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px 18px;margin-bottom:14px}
.panel h2{font-size:15px;color:#a371f7;margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel h2 .sub{font-size:12px;color:#8b949e;font-weight:400}
.panel h2 .right{margin-left:auto;font-weight:400}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.toolbar label.chk{font-size:12px;color:#8b949e;display:flex;align-items:center;gap:5px;cursor:pointer}
.live{margin-left:auto;font-size:12px;color:#8b949e;white-space:nowrap}
.live b{color:#c9d1d9}
.live .good{color:#3fb950}.live .warn{color:#d29922}.live .bad{color:#f85149}
table.pt{width:100%;border-collapse:collapse;font-size:12.5px}
table.pt th{text-align:left;color:#8b949e;font-weight:500;padding:6px 10px;border-bottom:1px solid #30363d;white-space:nowrap}
table.pt td{padding:7px 10px;border-top:1px solid #21262d;vertical-align:middle}
table.pt tr:hover td{background:#11161d}
.pt .name{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c9d1d9}
.pt .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#8b949e}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;white-space:nowrap}
.b-wide{background:#132e1c;color:#3fb950;border:1px solid #238636}
.b-fix{background:#3d2e00;color:#d29922;border:1px solid #9e6a03}
.b-no{background:#3d1418;color:#f85149;border:1px solid #4d2c2c}
.b-none{background:#21262d;color:#8b949e;border:1px solid #30363d}
.stale{color:#d29922}
.log{margin-top:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;
     color:#8b949e;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px;
     max-height:200px;overflow:auto;display:none;white-space:pre-wrap}
.log.on{display:block}
.log .ok{color:#3fb950}.log .warn{color:#d29922}.log .bad{color:#f85149}
select{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:5px 8px;font-size:12px;font-family:inherit}
.empty{color:#8b949e;font-size:13px;padding:14px 2px}
</style>
</head>
<body>
<div class="header">
    <h1>🖼️ 海报体检</h1>
    <span class="tag">已刮削/欧美 · 已刮削/无码 &nbsp;|&nbsp; AR &lt; 1.4 会被 Emby 当窄竖卡（标题被切）</span>
    <div class="spacer"></div>
    <div class="nav">
        <a class="btn sm" href="/">🚀 一键入库</a>
        <a class="btn sm" href="/tasks">📋 任务监控</a>
        <a class="btn sm avdb" href="/avdb">🔗 avdb 连接器</a>
        <a class="btn sm posters cur" href="/posters">🖼️ 海报体检</a>
    </div>
</div>
<div class="container">
    <div class="cards" id="cards"></div>

    <div class="panel">
        <h2>操作 <span class="sub">整改 = 把目录里的横图复制成 poster.jpg（旧图进备份区）；刷新 = 让 Emby 重读条目、重算卡片形状</span>
            <span class="right live" id="live"></span>
        </h2>
        <div class="toolbar">
            <button class="btn ok" id="btnFix" onclick="fixAll()">🛠 一键整改全部可修项</button>
            <button class="btn warn" id="btnRefresh" onclick="refresh()">🔄 刷新 Emby（两库条目）</button>
            <button class="btn ghost" id="btnScan" onclick="scan()">🔍 重新扫描</button>
            <label class="chk"><input type="checkbox" id="autoFix" checked>整改后自动刷新对应条目</label>
        </div>
        <div class="log" id="log"></div>
    </div>

    <div class="panel">
        <h2>明细 <span class="sub" id="sub"></span>
            <span class="right">
                <select id="filter" onchange="render()">
                    <option value="tall">只看有问题的（竖图）</option>
                    <option value="fixable">只看可整改</option>
                    <option value="nochange">只看无解</option>
                    <option value="wide">只看已是横图</option>
                    <option value="all">全部</option>
                </select>
            </span>
        </h2>
        <table class="pt">
            <thead><tr>
                <th>库</th><th>条目</th><th>poster 尺寸</th><th>AR</th>
                <th>可用横图</th><th>Emby AR</th><th>状态</th><th>说明</th><th></th>
            </tr></thead>
            <tbody id="rows"></tbody>
        </table>
        <div class="empty" id="empty" style="display:none"></div>
    </div>
</div>
<script>
var DATA = null;

function log(msg, cls){
  var el = document.getElementById('log');
  el.classList.add('on');
  var t = new Date().toTimeString().slice(0,8);
  el.innerHTML += '<div class="' + (cls||'') + '">[' + t + '] ' + msg + '</div>';
  el.scrollTop = el.scrollHeight;
}
function busy(on){
  ['btnFix','btnRefresh','btnScan'].forEach(function(id){
    document.getElementById(id).disabled = !!on;
  });
}
function fmtSize(it){
  return it.poster_size || '-';
}
function card(cls, lbl, num, hint){
  return '<div class="card ' + cls + '"><div class="lbl">' + lbl + '</div><div class="num">' + num +
         '</div><div class="lbl">' + hint + '</div></div>';
}

function scan(){
  busy(true);
  fetch('/api/posters/audit').then(function(r){return r.json()}).then(function(d){
    if(d.error){ log('扫描失败: ' + d.error, 'bad'); busy(false); return; }
    DATA = d;
    var tb = d.libs.reduce(function(a,l){return a + l.total}, 0);
    var tw = d.libs.reduce(function(a,l){return a + l.wide}, 0);
    var tf = d.fixable;
    var tn = d.libs.reduce(function(a,l){return a + l.nochange + l.noposter}, 0);
    document.getElementById('cards').innerHTML =
      card('total','条目总数', tb, d.libs.map(function(l){return l.key+' '+l.total}).join(' / ')) +
      card('wide','已是横图', tw, 'AR ≥ ' + d.wide_ar) +
      card('fixable','可整改', tf, '有更宽横图可换') +
      card('nochange','无解', tn, '需从视频抽帧重做');
    document.getElementById('sub').innerHTML =
      d.ts + ' · 备份区 ' + d.trash +
      (d.emby && d.emby.ok ? ' · Emby 已对照' : (d.emby && d.emby.error ? ' · Emby 对照失败: ' + d.emby.error : ''));
    render();
    busy(false);
  }).catch(function(e){ log('扫描异常: ' + e, 'bad'); busy(false); });
}

function render(){
  if(!DATA) return;
  var f = document.getElementById('filter').value;
  var rows = DATA.items.filter(function(it){
    if(f === 'all') return true;
    if(f === 'tall') return it.status !== 'wide';
    return it.status === f;
  });
  var html = rows.map(function(it, idx){
    var badge = it.status === 'wide' ? '<span class="badge b-wide">已是横图</span>' :
                it.status === 'fixable' ? '<span class="badge b-fix">可整改</span>' :
                it.status === 'noposter' ? '<span class="badge b-none">无 poster</span>' :
                '<span class="badge b-no">无解</span>';
    var arCls = it.ar && it.ar < DATA.wide_ar ? 'style="color:#d29922"' : '';
    var emby = it.emby_ar ? (it.emby_stale ? '<span class="stale">' + it.emby_ar + ' ⚠️待刷新</span>' : it.emby_ar) : '<span class="mono">-</span>';
    var btn = it.status === 'fixable' ?
      '<button class="btn sm warn" onclick="fixOne(\'' + encodeURIComponent(it.dir) + '\')">整改</button>' : '';
    return '<tr><td>' + it.lib + '</td>' +
      '<td class="name" title="' + it.dir + '">' + it.rel + '</td>' +
      '<td class="mono" ' + arCls + '>' + fmtSize(it) + '</td>' +
      '<td class="mono" ' + arCls + '>' + (it.ar || '-') + '</td>' +
      '<td class="mono">' + (it.cand ? it.cand + ' ' + it.cand_size : '-') + '</td>' +
      '<td class="mono">' + emby + '</td>' +
      '<td>' + badge + '</td>' +
      '<td class="mono">' + (it.note || '') + '</td>' +
      '<td>' + btn + '</td></tr>';
  }).join('');
  document.getElementById('rows').innerHTML = html;
  var em = document.getElementById('empty');
  if(!rows.length){ em.style.display = 'block'; em.textContent = '当前筛选下没有条目。'; }
  else em.style.display = 'none';
  var fixes = DATA.items.filter(function(i){return i.status==='fixable'}).length;
  document.getElementById('live').innerHTML =
    '共 <b>' + DATA.items.length + '</b> 条 · 可整改 <b class="warn">' + fixes + '</b>';
}

function doFix(dirs){
  var body = dirs ? {dirs:dirs} : {mode:'fixable'};
  body.refresh = document.getElementById('autoFix').checked ? 'auto' : 'none';
  return fetch('/api/posters/fix', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(body)}).then(function(r){return r.json()});
}

function fixOne(enc){
  var dir = decodeURIComponent(enc);
  busy(true);
  doFix([dir]).then(function(d){
    if(d.error){ log('整改失败: ' + d.error, 'bad'); }
    else{
      d.fixed.forEach(function(x){ log('✔ ' + x.name + ' — ' + x.note, 'ok'); });
      d.skipped.forEach(function(x){ log('· ' + x.name + ' — ' + x.note, ''); });
      if(d.refreshed) log('Emby 已定向刷新 ' + d.refreshed + ' 条', 'ok');
    }
    scan();
  }).catch(function(e){ log('异常: ' + e, 'bad'); busy(false); });
}

function fixAll(){
  var n = DATA ? DATA.fixable : 0;
  if(!n){ log('没有可整改项。', 'warn'); return; }
  if(!confirm('整改 ' + n + ' 条？（旧 poster 会备份到 ' + DATA.trash + '）')) return;
  busy(true);
  log('开始整改 ' + n + ' 条…');
  doFix(null).then(function(d){
    if(d.error){ log('整改失败: ' + d.error, 'bad'); busy(false); return; }
    d.fixed.forEach(function(x){ log('✔ ' + x.name + ' — ' + x.note, 'ok'); });
    d.skipped.forEach(function(x){ log('· ' + x.name + ' — ' + x.note, 'warn'); });
    log('完成：整改 ' + d.fixed.length + ' 条 / 跳过 ' + d.skipped.length +
        '，Emby 定向刷新 ' + d.refreshed + ' 条', 'ok');
    scan();
  }).catch(function(e){ log('异常: ' + e, 'bad'); busy(false); });
}

function refresh(){
  if(!confirm('刷新两库所有条目的 Emby 记录（重读本地文件、重算卡片形状）？')) return;
  busy(true);
  log('刷新 Emby 中…（约 1 条/0.1s）');
  fetch('/api/posters/refresh', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({all_libs:true})}).then(function(r){return r.json()}).then(function(d){
    if(d.error){ log('刷新失败: ' + d.error, 'bad'); }
    else{
      log('✔ Emby 条目刷新 ' + d.refreshed + '/' + d.total + ' 条' +
          (d.lib_scan !== '' ? '，扫库返回 ' + d.lib_scan : ''), 'ok');
      if(d.errors && d.errors.length) d.errors.forEach(function(e){ log('! ' + e, 'warn'); });
    }
    setTimeout(scan, 8000);
    busy(false);
  }).catch(function(e){ log('异常: ' + e, 'bad'); busy(false); });
}

scan();
</script>
</body>
</html>
"""


def render_poster_page():
    return PAGE
