#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_scrape.py — 存量补刮: 把 115 上【已经存在、但不是流水线推上去】的目录接进刮削管线。

它只做三件事(顺序与 import_api 完全相同):
  1. 用 115 MCP 拿 pickcode -> 为每个视频生成 .strm 到对应 watch 目录
     (strm 内容 = http://192.168.2.238:11500/d/<pickcode>/<url编码文件名>)
  2. 什么都不用触发: strm 一落进 watch 目录, MDCng 的 watcher 自动开始刮削
     产物(硬链接/移动)进 已刮削/<分类>/
  3. 完成后刷 Emby/Jellyfin

用法:
  # 先看它打算做什么(不写任何文件)
  python3 backfill_scrape.py --dir "/我的资源/ABC-123" --category av --dry-run

  # 真跑
  python3 backfill_scrape.py --dir "/我的资源/ABC-123" --category av

  # 自定义本地一级目录名 / 过滤小文件 / 等刮削更久
  python3 backfill_scrape.py --dir "/x/y" --category lf --name "某合集" --min-size-mb 100 --wait 600
"""
import argparse
import os
import sys
import time
import urllib.parse

SRV = '/root/clacky_workspace/sehuatang-emby-deliverable/src/server'
for p in (SRV, '/root/clacky_workspace'):
    if p not in sys.path:
        sys.path.insert(0, p)

import import_api as ia                      # 复用 CATEGORY_MAP / media_refresh / 完整性检查
from mcp115 import MCP115, MEDIA_EXTS, STRM_HOST

# 自己手动存的存量资源专用根 (与流水线的 待看/sehuatang_* 分开)
# MDCng watch: /media/待看/自存 -> /media/已刮削/自存
SELF_STRM_ROOT = '/mnt/g/srtm/待看/自存'
SELF_TARGET_ROOT = '/mnt/g/srtm/已刮削/自存'


def collect_videos(m, path, min_size_mb=0, limit=0, max_depth=0, exts=None):
    """递归收集视频: [{'rel': 相对路径, 'fn', 'pc', 'size'}]
    max_depth: 相对 --dir 的下钻层数; 0=不限, 1=只看 --dir 本层文件(不进子目录)。
    exts: 自定义扩展名集合(如 {'.mp4','.iso'}), None = MEDIA_EXTS。
    """
    exts = exts or MEDIA_EXTS
    if m.resolve_cid(path) is None:
        raise SystemExit(f'115 上不存在这个目录(或不是目录): {path}')
    out = []
    base = path.strip('/')

    def walk(p, depth=0):
        if limit and len(out) >= limit:
            return
        cid = m.resolve_cid(p)
        if cid is None:
            return
        for e in m.list_entries(cid):
            sub = p.rstrip('/') + '/' + e['fn']
            if e['is_dir']:
                if not max_depth or depth + 1 < max_depth:
                    walk(sub, depth + 1)
                continue
            if os.path.splitext(e['fn'])[1].lower() not in exts:
                continue
            if not e['pc']:
                print(f'  [skip] 无 pickcode: {sub}')
                continue
            if min_size_mb and e['size'] < min_size_mb * 1024 * 1024:
                print(f'  [skip] 小于 {min_size_mb}MB ({e["size"]/2**20:.1f}MB): {e["fn"]}')
                continue
            rel = sub[len(base):].strip('/') if base else sub.lstrip('/')
            out.append({'rel': rel, 'fn': e['fn'], 'pc': e['pc'], 'size': e['size']})
            if limit and len(out) >= limit:
                return

    walk(path.strip('/') or '')
    return out


def local_root_for(args):
    """115 路径 -> 本地 strm 一级目录。
    --self-store : 待看/自存/<--name>             (自己存的存量, 与 sehuatang 分开)
    - /sehuatang/... 或 /sehuatang_tv/...  : 复用流水线自身规则(分类层该留留/该剥剥)
    - 其它任意存量路径                     : <watch根>/<--name 或 115 目录名>
    """
    p = args.dir.strip('/')
    if getattr(args, 'self_store', False):
        nm = (args.name or (p.split('/')[-1] if p else '')).strip('/').replace('..', '').strip('/')
        return os.path.join(SELF_STRM_ROOT, nm) if nm else SELF_STRM_ROOT
    if p.startswith('sehuatang_tv/') or p.startswith('sehuatang/'):
        return ia._local_strm_dir(args.dir, args.category)
    name = args.name or (p.split('/')[-1] if p else 'backfill')
    name = name.replace('/', '_').strip() or 'backfill'
    return os.path.join(ia.category_strm_root(ia.norm_category(args.category)), name)


def wait_mdc(local_dir, target_root, name, timeout):
    """轮询 MDCng 是否刮完: 本地原地有 nfo+图, 或产物已进 已刮削/<分类>/<name>"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        ok, nfo, img = ia._has_local_metadata(local_dir)
        if ok:
            return True, f'MDCng 原地刮削完成 ({local_dir}): {nfo} nfo, {img} 图'
        cand = os.path.join(target_root, name)
        ok2, n2, i2 = ia._has_local_metadata(cand)
        if ok2:
            return True, f'MDCng 刮削完成并移入: {cand} ({n2} nfo, {i2} 图)'
        time.sleep(5)
    return False, f'等待 {timeout}s 未见刮削产物(可能刮不出/仍在队列): {local_dir}'


def main():
    ap = argparse.ArgumentParser(description='存量补刮: 115 已有目录 -> strm -> MDCng 刮削 -> Emby', formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('--dir', required=True, help='115 上的目录路径, 如 /我的资源/ABC-123')
    ap.add_argument('--category', default='av', choices=list(ia.CATEGORY_KEYS), help='分类(决定 watch 目录与 已刮削/<分类>)')
    ap.add_argument('--self-store', action='store_true', help='落到 待看/自存/ 而不是流水线的 待看/sehuatang_<分类>/（自己存的存量资源用这个）')
    ap.add_argument('--name', default='', help='本地一级目录名, 默认取 115 目录名')
    ap.add_argument('--min-size-mb', type=int, default=0, help='小于该体积的视频跳过(挡广告小文件), 0=不过滤')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少个视频, 0=不限')
    ap.add_argument('--max-depth', type=int, default=0, help='下钻层数: 0=不限(默认), 1=只处理 --dir 本层文件, 不进子目录')
    ap.add_argument('--dry-run', action='store_true', help='只列清单与落点, 不写文件不刷新')
    ap.add_argument('--wait', type=int, default=300, help='等 MDCng 刮削的秒数, 0=不等')
    ap.add_argument('--no-refresh', action='store_true', help='结束时不动 Emby')
    args = ap.parse_args()

    cat = ia.norm_category(args.category)
    local_dir = local_root_for(args)
    if getattr(args, 'self_store', False):
        watch_root, target_root = SELF_STRM_ROOT, SELF_TARGET_ROOT
        cat_label = '自存（与 sehuatang 分开）'
    else:
        watch_root, target_root = ia.category_strm_root(cat), ia.category_target_root(cat)
        cat_label = f'{cat} ({ia.category_name(cat)})'
    print(f'115 源目录 : {args.dir}')
    print(f'分类       : {cat_label}')
    print(f'watch 根   : {watch_root}')
    print(f'本地落点   : {local_dir}')
    print(f'刮削产物   : {target_root}/<...>')
    print('-' * 72)

    m = MCP115()
    vids = collect_videos(m, args.dir, args.min_size_mb, args.limit, args.max_depth)
    total_gb = sum(v['size'] for v in vids) / 2 ** 30
    print(f'视频 {len(vids)} 个, 合计 {total_gb:.2f} GB')
    if not vids:
        raise SystemExit('没找到可生成的视频(检查扩展名/是否为空目录)。')

    plan = []
    for v in vids:
        rel_dir = os.path.dirname(v['rel'])
        stem = os.path.splitext(v['fn'])[0]
        sp = os.path.join(local_dir, rel_dir, stem + '.strm') if rel_dir else os.path.join(local_dir, stem + '.strm')
        plan.append((sp, f'{STRM_HOST}/d/{v["pc"]}/{urllib.parse.quote(v["fn"])}'))

    exists = [p for p, _ in plan if os.path.exists(p)]
    if exists:
        print(f'其中已有 strm(会跳过重写) : {len(exists)} 个')
    for sp, _ in plan[:10]:
        print('   ->', sp)
    if len(plan) > 10:
        print(f'   ... 共 {len(plan)} 个')

    if args.dry_run:
        print('\n[dry-run] 未写任何文件。去掉 --dry-run 真跑。')
        return

    written = 0
    for sp, url in plan:
        if os.path.exists(sp):
            continue
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        try:
            with open(sp, 'w', encoding='utf-8') as f:
                f.write(url)
            written += 1
        except OSError as e:
            print(f'   [fail] 写 {sp} 失败: {e}(文件名过长? 试试 --name 缩短一级目录)')
    print(f'\n[strm] 写入 {written} 个 (跳过已存在 {len(exists)})')
    print('[strm] 已交给 MDCng watcher, 开始刮削 ...')

    if args.wait:
        t0 = time.time()
        ok, msg = wait_mdc(local_dir, target_root, args.name or os.path.basename(local_dir), args.wait)
        print(f'[{int(time.time()-t0)}s] {msg}')
        if not ok:
            print('提示: MDCng 靠番号/文件名识别, 存量目录若命名混乱可能刮不出; 可在帖子里用油猴脚本补充元数据。')

    if not args.no_refresh:
        try:
            code = ia.media_refresh(timeout=30)
            print(f'[emby] 刷新返回 {code}')
        except Exception as e:
            print(f'[emby] 刷新失败(不影响已刮削文件): {e}')


if __name__ == '__main__':
    main()
