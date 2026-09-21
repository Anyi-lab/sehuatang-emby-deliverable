# -*- coding: utf-8 -*-
"""
avdb_source.py — avdb 下载侧数据源 (只读, 零网络)
================================================================
用途: 一键入库侧的「avdb 连接器」读取源。
      只读 avdb 的本地 sqlite (/root/avdb/data/avdb.db, mode=ro),
      拿到"用户新提交的下载记录" → 解析出该片在 115 的落点目录。

设计要点 (2026-09-19):
  - 全程本地文件读取, 不向 avdb 发任何请求, 也不碰 115; 因此不存在风控面。
  - avdb 的 download_log.status 恒为 'submitted' (实测 9/9), avdb 从不更新完成状态,
    所以"下好了没有"只能由 115 侧判定 (见 import_api._wait_adopt_ready)。
  - download_log 无 magnet 列, 磁力/分类需按 number 回查 article 表。
  - save_path 是相对 avdb 下载根的相对路径 (模板 {section}/{category}),
    实际 115 路径 = AVDB_LIB + '/' + save_path + '/' + <番号目录名>。

115 实测落点示例:
  download_log.save_path = '未分区/未分类'
  → /sehuatang/AVDB/未分区/未分类/HMN-511-C
"""
import os
import re
import sqlite3
import logging

log = logging.getLogger('avdb-source')

AVDB_DB = os.environ.get('AVDB_DB', '/root/avdb/data/avdb.db')
AVDB_LIB = os.environ.get('AVDB_LIB', '/sehuatang/AVDB')   # avdb 下载在 115 的落点根


def _conn():
    """只读打开 avdb 库 (avdb 在写, 必须 ro + 短事务)"""
    if not os.path.exists(AVDB_DB):
        return None
    try:
        return sqlite3.connect('file:%s?mode=ro' % AVDB_DB, uri=True, timeout=5)
    except Exception as e:
        log.warning('打开 avdb 库失败: %s', str(e)[:120])
        return None


def normalize_number(n):
    """番号归一化: 去 [xxx] 修饰、去空格下划线、转大写"""
    s = (n or '').upper().strip()
    s = re.sub(r'\[[^\]]*\]', '', s)
    s = re.sub(r'[\s_]+', '', s)
    return s


_NOISE_TAIL = re.compile(
    r'(?:'
    r'4K60FPS|60FPS|FPS|'                                # 画质后缀
    r'4K\d*S?\d*|'                                       # 4K / 4KS / 4KS1 / 4K60
    r'UNCENSORED|UNCENSOR|LEAK|UHD|FHD|HD|'              # 来源/清晰度
    r'MP4|MKV|WMV|AVI|TS|'                               # 容器扩展名
    r'C|U'                                               # 字幕/版本单字母缀 (MIDV-586-C/U)
    r')$')


def _squeeze(n):
    """只留字母数字并大写: 't38-042' -> 'T38042'"""
    return re.sub(r'[^A-Z0-9]', '', (n or '').upper())


def _strip_noise(z):
    """去掉名字尾巴上的画质/来源/扩展名修饰: 'SQTE7014KS' -> 'SQTE701'。
    只削「尾巴且前面还留得下 >=5 个字符」的情形 —— 否则会误伤真番号
    (反面教材: CRNX265 曾把 X265 当编码后缀削成 CRN)"""
    changed = True
    while changed and len(z) > 5:
        changed = False
        m = _NOISE_TAIL.search(z)
        if m and m.start() >= 5:
            z = z[:m.start()]
            changed = True
    return z


def number_key(n):
    """番号拆成 (系列, 数字) 便于模糊匹配。比旧版多容忍三类现实命名:
      'SQTE-701_4KS'                -> ('SQTE','701')      去画质尾巴
      'MOND-306_4K60FPS'            -> ('MOND','306')
      '390JNT-104-uncensored-HD'    -> ('JNT','104')        数字开头 → 整串里取第一组
      '第一會所新片@SIS001@HNDS-079'   -> ('HNDS','079')       CJK/宣传前缀 → 同上
      '[4K]…NIMA-035…吊带.mp4'       -> ('NIMA','035')       [xxx] 先被 normalize 去掉
      'MIDV-586-C'                  -> ('MIDV','586')"""
    s = _strip_noise(_squeeze(normalize_number(n)))
    if not s:
        return '', ''
    m = re.match(r'^([A-Z]+)[-_]?(\d+)', s)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'([A-Z]+)(\d+)', s)          # 数字开头 / CJK 前缀 → 取整串里第一组
    if m:
        return m.group(1), m.group(2)
    return s, ''


def _needle_in(key, other):
    """(系列,数字) 连写是否作为「完整数字段」出现在 other 里。
    末尾必须非数字, 避免 SQTE-645 误命中 SQTE-6450_4KS"""
    letters, digits = key
    if not letters or not digits or len(letters + digits) < 4:
        return False
    hay = _strip_noise(_squeeze(other))
    for d in {digits, digits.lstrip('0') or digits}:    # 容忍 HNDS-79 / HNDS-079 零填充差
        needle = letters + d
        i = hay.find(needle)
        while i >= 0:
            j = i + len(needle)
            if j >= len(hay) or not hay[j].isdigit():
                return True
            i = hay.find(needle, i + 1)
    return False


def same_number(a, b):
    """宽松同番号判定 (2026-09-20 加固)。
    视为同一部:  MIDV-586 / MIDV-586-C / midv586 / MIDV-586-U
                SQTE-701 / SQTE-701_4KS
                JNT-104  / 390JNT-104-uncensored-HD
                HNDS-079 / 第一會所新片@SIS001@HNDS-079
                MIUM-1198 / 300MIUM-1198
                T-38042  / t38-042
    仍判不同:    SQTE-645 / SQTE-6450_4KS   (末位数字不同)
                HNDS-079 / HNDS-078"""
    sa, sb = _squeeze(a), _squeeze(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True                              # t38-042 == T-38042
    ka, kb = number_key(a), number_key(b)
    if ka[1] and ka == kb:
        return True                              # MIDV-586 == midv586-C
    return _needle_in(ka, b) or _needle_in(kb, a)


def fetch_downloads(after_id=0, limit=100):
    """取 download_log 里 id > after_id 的记录 (升序)。返回 list[dict]

    2026-09-20 补: 手动下载(avdb 网页「下载」按钮)写进 download_log 的行
    number 为空、save_path 是用户手填的路径 —— 订阅流程的行才有 number。
    这类行以前被判 no_number 直接跳过, 导致 200GANA-3456 这种已下载好的片子
    永远躺在暂存区 (它有 tid, article 表里就是同一贴, 番号/magnet 都在)。
    这里用 tid 左连 article 回填 number/title; 回填过的行打 number_src='article'。"""
    c = _conn()
    if c is None:
        return []
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT d.*, a.number AS _a_number, a.title AS _a_title "
            "FROM download_log d "
            "LEFT JOIN (SELECT tid, MAX(number) AS number, MAX(title) AS title "
            "           FROM article GROUP BY tid) a ON a.tid = d.tid "
            "WHERE d.id > ? ORDER BY d.id ASC LIMIT ?",
            (int(after_id), int(limit))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop('_a_number', None)
            d.pop('_a_title', None)
            a_num = r['_a_number']
            a_tit = r['_a_title']
            if not (d.get('number') or '').strip() and (a_num or '').strip():
                d['number'] = a_num
                d['number_src'] = 'article'
            if not (d.get('title') or '').strip() and (a_tit or '').strip():
                d['title'] = a_tit
            out.append(d)
        return out
    except Exception as e:
        log.warning('读 download_log 失败: %s', str(e)[:120])
        return []
    finally:
        c.close()


def max_download_id():
    """当前 download_log 最大 id (守护初始水位用)"""
    c = _conn()
    if c is None:
        return 0
    try:
        r = c.execute('SELECT MAX(id) FROM download_log').fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0
    finally:
        c.close()


def fetch_article(number):
    """按番号回查 article 表补 magnet/size/section/category。
    同番号可能多行(不同压制/字幕), 取 size 最大的一行 (通常是最全的版本)。
    返回 dict 或 None"""
    c = _conn()
    if c is None:
        return None
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT tid, number, title, magnet, size, section, category, detail_url, preview_images "
            "FROM article WHERE number = ? ORDER BY size DESC LIMIT 5", (number or '',)).fetchall()
        if not rows:
            return None
        for r in rows:
            d = dict(r)
            if d.get('magnet'):
                return d
        return dict(rows[0])
    except Exception as e:
        log.warning('回查 article 失败: %s', str(e)[:120])
        return None
    finally:
        c.close()


def list_avdb_dirs(p, save_path=''):
    """列出 avdb 落点目录下的一级子目录名 (p = Push115 实例)。
    save_path 为空 → 列 AVDB_LIB 本身。异常返回 []"""
    root = AVDB_LIB.rstrip('/')
    if save_path:
        root = root + '/' + str(save_path).strip('/')
    try:
        items = p.list_dir(root, maxdepth=1, use_cache=False)
    except Exception as e:
        log.warning('列 avdb 落点失败 %s: %s', root, str(e)[:120])
        return [], root
    dirs = [it[1] for it in items if it[4]]
    return dirs, root


def match_dir(p, save_path, number, extra_depth=True):
    """在 avdb 落点里找该番号的目录。
    先按 save_path 直接列; 找不到(或 save_path 为空)时退回扫 AVDB_LIB 两层。
    返回 (115 目录全路径, 目录名) 或 (None, None)"""
    cands = []
    dirs, root = list_avdb_dirs(p, save_path)
    for d in dirs:
        if same_number(d, number):
            return root + '/' + d, d
        cands.append((root, d))
    if extra_depth:
        # 两层兜底: save_path 变了/为空时, 扫 AVDB_LIB 的一级子目录再找
        top, _ = list_avdb_dirs(p, '')
        for t in top:
            sub_dirs, sub_root = list_avdb_dirs(p, t)
            for d in sub_dirs:
                if same_number(d, number):
                    return sub_root + '/' + d, d
    return None, None


def resolve(number, p, save_path=''):
    """一站式解析: 番号 → {number, magnet, title, dir_115, dir_name, src_root}
    p = Push115 实例 (由调用方传入, 避免本模块依赖 import_api)"""
    art = fetch_article(number) or {}
    dir_115, dir_name = match_dir(p, save_path, number)
    return {
        'number': number,
        'magnet': art.get('magnet') or '',
        'article_title': art.get('title') or '',
        'section': art.get('section') or '',
        'category': art.get('category') or '',
        'size_mb': art.get('size') or 0,
        'dir_115': dir_115,
        'dir_name': dir_name,
    }
