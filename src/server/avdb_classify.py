#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avdb_classify.py — avdb 下载记录 → 入库分类 自动判定器 (只读, 零网络)
================================================================================
背景 (2026-09-27):
  订阅(演员/合集/排行榜)下载的片子全部落在 avdb 的同一批目录里
  (save_path 模板对订阅解析不出 section/category → '未分区/未分类'),
  而入库侧连接器 avdb_watch.py 的分类是**启动参数里的固定值**
  (AVDB_WATCH_CATEGORY=av) —— 于是一律按 AV 入库, 欧美/FC2/国产 全混进 AV。

本模块做的事: 给每条 download_log 记录判一个入库分类 (av/fc2/sw/cn/ea/lf)。
判定优先级 (先准后糙):
  1. article 回查  —— avdb 库里 article 表有帖子的 section(版块) + category(主题分类),
                     这是人工发帖时选好的, 最准。按 tid 优先, 再按 number。
  2. 番号规则     —— FC2-PPV-xxx / 纯数字编号 → fc2; 西文厂牌 → ea;
                     丝袜关键词 → sw (默认关闭, --with-sw 打开)。
  3. 标题语言     —— 无番号且标题几乎全拉丁字符 → ea (低置信)。
  4. 兜底         —— av。

用法:
  python3 avdb_classify.py --history 400          # 干跑: 最近 400 条 download_log 的判定报告
  python3 avdb_classify.py --history 400 --json   # 同上, 出 JSON
  python3 avdb_classify.py --number MIDV-586      # 单条判定
  python3 avdb_classify.py --with-sw              # 打开「丝袜」关键词规则

不改任何东西: 只读 avdb 的 sqlite (mode=ro), 不写台账, 不碰 115。
"""
import argparse
import json
import os
import re
import sqlite3
import sys

AVDB_DB = os.environ.get('AVDB_DB', '/root/avdb/data/avdb.db')

# ---------------------------------------------------------------- 分类词表
# 入库分类 key (与 import_api.CATEGORY_MAP 一一对应)
CATS = ('av', 'fc2', 'sw', 'cn', 'ea', 'lf')
CAT_NAMES = {'av': 'AV', 'fc2': 'FC2', 'sw': '丝袜', 'cn': '国产自拍', 'ea': '欧美', 'lf': '里番'}

# 版块(section) → 分类  (实测 avdb article 表 section 全集, 2026-09-27)
SECTION_TO_CAT = {
    'FC2': 'fc2',
    '欧美无码': 'ea',
    '国内成人': 'cn',
    '韩国主播': 'cn',
    '动漫原创': 'lf',
    # 以下一律进 AV: 亚洲有码 / 亚洲无码 / 素人有码 / 中文字幕 / 4K原版 / VR视频区 / MGS / 三级写真
}

# 主题分类(category) 关键词 → 分类 (优先级高于 section)
CATEGORY_KEYWORD_TO_CAT = (
    ('fc2', ('FC2PPV', 'FC2-PPV', 'FC2PPV')),          # 亚洲无码/FC2PPV (20926 条)
    ('ea', ('欧美',)),                                  # 欧美* 主题分类
)

# 三级写真里的欧美子类 (美国三级/美国四级) —— 要不要进「欧美」由 --softcore-ea 决定
SOFTCORE_WESTERN_CATEGORIES = ('美国三级', '美国四级')

# 确认「就是 AV」的版块 (有码/无码破解/字幕/4K/VR 都在养同一个库)
AV_SECTIONS = ('亚洲有码', '亚洲无码', '素人有码', '中文字幕', '4K原版', 'VR视频区', 'MGS')

# 「够不上高置信」的版块: 仍进 AV, 但报告里列出来让用户拍板
SOFT_SECTIONS = ('三级写真',)

# ---------------------------------------------------------------- 番号 / 标题规则
RE_FC2 = re.compile(r'FC2[-_\s]?PPV|^FC2\d|^FC2$|FC2[-_]?PPV|^FC2[-_\s]?\d{4,8}$', re.I)
RE_FC2_DIGITS = re.compile(r'^\d{6,7}$')                # FC2 编号风格 1234567
RE_CENSORED_JAV = re.compile(r'[A-Z]{2,6}[-_]?\d{2,5}', re.I)   # 常规 JAV 番号
RE_KANA = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff]')  # 假名/汉字

WESTERN_STUDIOS = (
    'brazzers', 'tushy', 'vixen', 'blacked', 'reality kings', 'realitykings',
    'bangbros', 'evil angel', 'digital playground', 'naughty america',
    'mofos', 'pornhub', 'onlyfans', 'netvideogirls', 'passion-hd', 'passionhd',
    'babes.com', 'dorcel', 'private.com', '21sextury', 'milfed', 'wowgirls',
    'familyporn', 'stepsis', 'family stroked', 'daddys lil angel',
)
RE_WESTERN_NUMBER = re.compile(r'^(?:[A-Z]{2,}|[A-Z]+)\.?\d{2}\.\d{2}\.\d{2}$', re.I)

SW_KEYWORDS = (
    '丝袜', '連裤袜', '连裤袜', '裤袜', 'パンスト', 'ストッキング', 'タイツ',
    '黒パンスト', '美脚', 'ヒール', 'stocking', 'pantyhose', 'tights',
)


def _norm_num(n):
    return re.sub(r'[\s_]+', '', (n or '')).upper()


def _latin_ratio(text):
    t = re.sub(r'\s+', '', text or '')
    if not t:
        return 0.0
    latin = len(re.findall(r'[A-Za-z0-9\W]', t, re.UNICODE))
    # 只统计 ASCII 可见字符占比
    ascii_visible = len(re.findall(r'[\x20-\x7e]', t))
    return ascii_visible / len(t)


# ---------------------------------------------------------------- 分类核心
def classify(number='', title='', section=None, category=None,
             with_sw=False, softcore_ea=True):
    """返回 (cat, reason, source, confidence)

    source: article | number | title | default
    confidence: high | mid | low
    """
    number = (number or '').strip()
    title = (title or '').strip()
    section = (section or '').strip()
    category = (category or '').strip()

    # ---- 1) 帖子(article)元数据: 最准
    if section or category:
        for cat, keys in CATEGORY_KEYWORD_TO_CAT:
            for k in keys:
                if k.lower() in category.lower():
                    return cat, f'article:{section}/{category} → 主题分类含「{k}」', 'article', 'high'
        if section in SECTION_TO_CAT:
            cat = SECTION_TO_CAT[section]
            return cat, f'article:版块「{section}」', 'article', 'high'
        if softcore_ea and category in SOFTCORE_WESTERN_CATEGORIES:
            return 'ea', f'article:版块「{section}」主题「{category}」(欧美软色情)', 'article', 'mid'
        if section in AV_SECTIONS:
            return 'av', f'article:版块「{section}」' + (f'/{category}' if category else ''), 'article', 'high'
        if section in SOFT_SECTIONS:
            return 'av', f'article:版块「{section}」/{category or "-"}(纯写真, 暂归 AV)', 'article', 'mid'
        # 版块存在但没映射 → 继续往下走番号规则, 并在 reason 里带上版块
        art_note = f'article:{section}/{category or "-"} 未覆盖'
    else:
        art_note = ''

    # ---- 2) 番号规则
    n = _norm_num(number)
    if RE_FC2.search(n):
        return 'fc2', _join(art_note, f'番号「{number}」命中 FC2-PPV'), 'number', 'high'
    if RE_FC2_DIGITS.match(n):
        return 'fc2', _join(art_note, f'番号「{number}」是纯数字编号(FC2 风格)'), 'number', 'mid'
    low_title = title.lower()
    for s in WESTERN_STUDIOS:
        if s in low_title:
            return 'ea', _join(art_note, f'标题命中欧美厂牌「{s}」'), 'title', 'high'
    if RE_WESTERN_NUMBER.match(n):
        return 'ea', _join(art_note, f'番号「{number}」是欧美式编号'), 'number', 'mid'
    if with_sw:
        for k in SW_KEYWORDS:
            if k.lower() in low_title or k in title:
                return 'sw', _join(art_note, f'标题命中丝袜关键词「{k}」'), 'title', 'mid'

    # ---- 3) 标题语言兜底
    if not RE_CENSORED_JAV.search(n or 'x') and _latin_ratio(title) > 0.9 and len(title) > 8:
        return 'ea', _join(art_note, '无番号且标题几乎全西文'), 'title', 'low'

    # ---- 4) 兜底
    return 'av', _join(art_note, '无匹配规则, 默认 AV'), 'default', 'low'


def _join(*parts):
    return ' · '.join(p for p in parts if p)


# ---------------------------------------------------------------- avdb 只读
def _conn():
    if not os.path.exists(AVDB_DB):
        return None
    return sqlite3.connect('file:%s?mode=ro' % AVDB_DB, uri=True, timeout=5)


def article_meta(number='', tid=None):
    """回查 article 表的 section/category。按 tid 优先, 再按 number。"""
    c = _conn()
    if c is None:
        return {}, ''
    try:
        c.row_factory = sqlite3.Row
        if tid:
            r = c.execute('SELECT section, category, number, title FROM article WHERE tid=? '
                          'ORDER BY size DESC LIMIT 1', (str(tid),)).fetchone()
            if r and (r['section'] or r['category']):
                return dict(r), 'tid'
        if number:
            r = c.execute('SELECT section, category, number, title FROM article WHERE number=? '
                          'ORDER BY size DESC LIMIT 1', (number,)).fetchone()
            if r:
                return dict(r), 'number'
        return {}, ''
    except Exception:
        return {}, ''
    finally:
        c.close()


def classify_download_row(row, **kw):
    """给一条 download_log 记录定分类 (row = dict)"""
    number = (row.get('number') or '').strip()
    tid = row.get('tid')
    meta, via = article_meta(number=number, tid=tid)
    if not number and meta.get('number'):
        number = meta['number']
    cat, reason, source, conf = classify(
        number=number,
        title=(row.get('title') or meta.get('title') or ''),
        section=meta.get('section'),
        category=meta.get('category'),
        **kw)
    if via:
        reason = _join(reason, f'(article via {via})')
    return {'id': row.get('id'), 'number': number, 'title': (row.get('title') or '')[:60],
            'category': cat, 'category_name': CAT_NAMES[cat],
            'reason': reason, 'source': source, 'confidence': conf,
            'section': meta.get('section') or '', 'art_category': meta.get('category') or ''}


# ---------------------------------------------------------------- CLI
def _history(limit, **kw):
    c = _conn()
    if c is None:
        print('打不开 avdb 库:', AVDB_DB)
        return
    c.row_factory = sqlite3.Row
    rows = c.execute('SELECT id, tid, number, title FROM download_log '
                     'ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()
    c.close()
    out = [classify_download_row(dict(r), **kw) for r in rows]
    return out


def main():
    ap = argparse.ArgumentParser(description='avdb 下载记录 → 入库分类 判定器 (只读)')
    ap.add_argument('--history', type=int, default=0, help='干跑最近 N 条 download_log')
    ap.add_argument('--number', default='', help='单条判定: 番号')
    ap.add_argument('--title', default='', help='单条判定: 标题')
    ap.add_argument('--json', action='store_true', help='JSON 输出')
    ap.add_argument('--with-sw', action='store_true', help='打开丝袜关键词规则')
    ap.add_argument('--no-softcore-ea', action='store_true', help='三级写真里的美国三级/四级 不算欧美')
    ap.add_argument('--show', type=int, default=40, help='明细打印条数 (默认 40)')
    a = ap.parse_args()
    kw = {'with_sw': a.with_sw, 'softcore_ea': not a.no_softcore_ea}

    if a.number or a.title:
        print(classify(number=a.number, title=a.title, **kw))
        return
    if not a.history:
        ap.print_help()
        return

    out = _history(a.history, **kw)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 统计
    from collections import Counter
    by_cat = Counter(r['category'] for r in out)
    by_src = Counter(r['source'] for r in out)
    by_conf = Counter(r['confidence'] for r in out)
    print(f'=== 样本 {len(out)} 条 (download_log 最近 {a.history}) ===')
    print('分类分布:', '  '.join(f'{CAT_NAMES[c]}={n}' for c, n in by_cat.most_common()))
    print('依据分布:', dict(by_src))
    print('置信分布:', dict(by_conf))
    print()
    print(f'--- 明细 (最近 {min(a.show, len(out))} 条) ---')
    for r in out[:a.show]:
        print(f"[{r['category']:>3}] id={r['id']:<4} {r['number']:<16} {r['category_name']:<5} "
              f"({r['confidence']}) {r['reason']}")
    low = [r for r in out if r['confidence'] == 'low']
    if low:
        print()
        print(f'--- 低置信 {len(low)} 条 (建议人工过一眼) ---')
        for r in low[:20]:
            print(f"[{r['category']:>3}] id={r['id']:<4} {r['number']:<16} {r['reason']}")


if __name__ == '__main__':
    main()
