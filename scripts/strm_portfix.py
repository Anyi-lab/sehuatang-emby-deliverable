#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm_portfix.py —— 批量把 .strm 内容里的上游端口 11500 改成 11501（本地中转）

背景（2026-09-24 实测 A/B）：
  strm 内容 http://192.168.2.238:11500/d/<pickcode>/<name>
     → 115-Desktop 的 /d/ 是「下载」路由，恒 302 到 115 CDN 直链，直链签名绑定请求方 UA
     → Emby 取流用 RodelPlayer/ffprobe UA ≠ 取链 UA → 403 → Emby 返回 500 Forbidden（只有个别条目能播）
  strm 内容 http://192.168.2.238:11501/d/<pickcode>/<name>
     → 落到 strm_relay.py（固定 UA 取链 + 取流 + 断点续传 + 重取链）→ Emby 206 + video/mp4

本脚本只改端口，不动路径（/d/ 保留）、不动 host、不动文件名编码。
原地改（r+ 截断）→ 保留 inode，硬链接副本同步生效。
每次运行写一份 JSONL 日志（含改写前原文），可据此回滚。

用法：
  python3 scripts/strm_portfix.py --root /mnt/g/srtm                    # dry-run（默认）
  python3 scripts/strm_portfix.py --root /mnt/g/srtm --apply
  python3 scripts/strm_portfix.py --root /mnt/g/srtm --apply --from-port 11501 --to-port 11500   # 回滚
"""
import argparse
import json
import os
import re
import sys
import time

RE_URL = re.compile(r'^(?P<scheme>https?)://(?P<host>[^/:\s]+):(?P<port>\d+)(?P<rest>/.*)$', re.S)
JOURNAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def iter_strm(root, limit=500000):
    n = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.lower().endswith('.strm'):
                yield os.path.join(base, f)
                n += 1
                if n >= limit:
                    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', action='append', required=True, help='strm 根目录，可多次')
    ap.add_argument('--from-port', type=int, default=11500)
    ap.add_argument('--to-port', type=int, default=11501)
    ap.add_argument('--apply', action='store_true', help='缺省只 dry-run')
    ap.add_argument('--journal', default=None)
    a = ap.parse_args()

    t0 = time.time()
    journal_path = a.journal or os.path.join(
        JOURNAL_DIR, 'strm_portfix_%s.jsonl' % time.strftime('%Y%m%d-%H%M%S'))
    jf = open(journal_path, 'w', encoding='utf-8') if a.apply else None

    total = changed = skipped_nomatch = already = errors = 0
    hardlinked = 0
    by_authority = {}
    samples = []
    for root in a.root:
        for p in iter_strm(root):
            total += 1
            try:
                with open(p, encoding='utf-8', errors='replace') as f:
                    c = f.read()
            except OSError as e:
                errors += 1
                print('read-error', p, e, file=sys.stderr)
                continue
            m = RE_URL.match(c.strip())
            if not m:
                skipped_nomatch += 1
                if len(samples) < 8:
                    samples.append(('NOMATCH', p, c.strip()[:100]))
                continue
            key = '%s:%s' % (m.group('host'), m.group('port'))
            by_authority[key] = by_authority.get(key, 0) + 1
            if int(m.group('port')) != a.from_port:
                already += 1
                continue
            try:
                if os.stat(p).st_nlink > 1:
                    hardlinked += 1
            except OSError:
                pass
            new = '%s://%s:%d%s' % (m.group('scheme'), m.group('host'), a.to_port, m.group('rest'))
            if len(samples) < 8:
                samples.append(('CHANGE', p, c.strip()[:100] + '  ->  ' + new.strip()[:100]))
            if not a.apply:
                changed += 1
                continue
            try:
                with open(p, 'r+', encoding='utf-8') as f:   # 原地改，保 inode（硬链接同步）
                    f.seek(0)
                    f.write(new)
                    f.truncate()
                    f.flush()
                    os.fsync(f.fileno())
                changed += 1
                jf.write(json.dumps({'file': p, 'old': c}, ensure_ascii=False) + '\n')
            except OSError as e:
                errors += 1
                print('write-error', p, e, file=sys.stderr)

    if jf:
        jf.close()
    print(json.dumps({
        'root': a.root, 'apply': a.apply, 'from_port': a.from_port, 'to_port': a.to_port,
        'total_strm': total, 'changed': changed, 'already_target_port': already,
        'skip_not_url': skipped_nomatch, 'hardlinked_of_changed': hardlinked,
        'errors': errors, 'authorities': by_authority,
        'journal': (journal_path if a.apply else None),
        'elapsed_s': round(time.time() - t0, 1),
    }, ensure_ascii=False, indent=2))
    for kind, p, s in samples:
        print('[%s] %s\n    %s' % (kind, p, s))


if __name__ == '__main__':
    main()
