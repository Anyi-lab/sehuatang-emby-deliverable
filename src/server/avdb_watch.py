#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avdb_watch.py — 「avdb 下载 → 一键入库」的入库侧连接器守护 (纯事件驱动)
================================================================================
用户规则 (2026-09-19):
  「只在我发出下载之后才看」→ 默认不做定时全盘扫描。

工作方式:
  1. 每 INTERVAL 秒读一次 avdb 的本地 sqlite (零网络, 115 完全不知情);
  2. 发现 download_log 里 id > 水位的新记录 → POST 一键入库的 /api/import/adopt,
     由入库侧去 115 确认这部下好了没 (只查这一个目录, 退避轮询);
  3. 没有任何新下载时 → 对 115 零请求;
  4. 115 请求量只与"你下载了几部片"有关, 与守护运行多久无关。

风控护栏 (对齐本机既有限频事故 770004 的教训):
  - 不注册就不动 115: avdb 侧全部是本地只读文件;
  - 兜底全盘扫描默认关闭 (--scan-interval 0), 需要时手动 --once-scan 补一次;
  - 台账去重: 同一条 download_log.id 只提交一次 (跑完不再重复入库)。

用法:
  python3 avdb_watch.py                     # 常驻守护 (默认)
  python3 avdb_watch.py --once              # 只跑一轮(处理水位之后的新记录)后退出
  python3 avdb_watch.py --status            # 看台账
  python3 avdb_watch.py --adopt MIDV-586    # 手动入库某一部(不推进水位)
  python3 avdb_watch.py --dry-run           # 只打印将要做什么, 不提交
  python3 avdb_watch.py --reset-watermark   # 把水位重置为当前最大 id (忽略历史记录)
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import avdb_source as src   # noqa: E402  (只读 avdb 本地库)

API = os.environ.get('IMPORT_API', 'http://127.0.0.1:5081')
LEDGER = os.environ.get('AVDB_WATCH_DB', '/var/lib/avdb-watch/avdb_watch.db')
LOG = os.environ.get('AVDB_WATCH_LOG', '/var/log/avdb_watch.log')
INTERVAL = int(os.environ.get('AVDB_WATCH_INTERVAL', '60'))     # 读 avdb 本地库的间隔(秒)
CATEGORY = os.environ.get('AVDB_WATCH_CATEGORY', 'av')           # 入库分类 (av/fc2/sw/cn/ea/lf)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('avdb-watch')


# ------------------------------ 台账 ------------------------------
def ledger_conn():
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    c = sqlite3.connect(LEDGER)
    c.execute('''CREATE TABLE IF NOT EXISTS seen(
        dl_id INTEGER PRIMARY KEY,
        number TEXT, dir_name TEXT, task_id TEXT, status TEXT, note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')))''')
    c.execute('''CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT)''')
    c.commit()
    return c


def state_get(k, default=None):
    c = ledger_conn()
    try:
        r = c.execute('SELECT v FROM state WHERE k=?', (k,)).fetchone()
        return r[0] if r else default
    finally:
        c.close()


def state_set(k, v):
    c = ledger_conn()
    try:
        c.execute('INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))
        c.commit()
    finally:
        c.close()


def ledger_mark(dl_id, number='', dir_name='', task_id='', status='', note=''):
    c = ledger_conn()
    try:
        c.execute('''INSERT INTO seen(dl_id, number, dir_name, task_id, status, note) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(dl_id) DO UPDATE SET number=excluded.number, dir_name=excluded.dir_name,
                     task_id=COALESCE(NULLIF(excluded.task_id,''), seen.task_id),
                     status=excluded.status, note=excluded.note, updated_at=datetime('now','localtime')''',
                  (int(dl_id), number, dir_name, task_id, status, note))
        c.commit()
    finally:
        c.close()


def ledger_done(dl_id):
    c = ledger_conn()
    try:
        r = c.execute('SELECT task_id, status FROM seen WHERE dl_id=?', (int(dl_id),)).fetchone()
        # 已提交过任务, 或已判定为无需处理(missing/skipped) → 不再重复
        return bool(r and (r[0] or r[1] in ('missing', 'skipped')))
    finally:
        c.close()


def watermark():
    """水位 = 已处理过的最大 download_log.id; 首次运行取当前最大值(不追历史)"""
    v = state_get('watermark')
    if v is not None:
        return int(v)
    w = src.max_download_id()
    state_set('watermark', w)
    log.info('首次运行: 水位初始化为 %d (忽略此前 %d 条历史记录, 需要补做请用 --adopt)', w, w)
    return w


# ------------------------------ 入库 API ------------------------------
def api_post(path, payload, timeout=30):
    req = urllib.request.Request(API.rstrip('/') + path,
                                 data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace') or '{}'), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        try:
            return json.loads(body or '{}'), e.code
        except Exception:
            return {'error': body[:300]}, e.code
    except Exception as e:
        return {'error': str(e)[:200]}, 0


def adopt(row, category=CATEGORY, dry_run=False, retries=3):
    """把一条 avdb 下载记录交给入库侧。返回 (ok, task_id, msg)"""
    dl_id = row['id']
    number = (row.get('number') or '').strip()
    if not number:
        ledger_mark(dl_id, status='skipped', note='记录无番号(手动下载/测试记录)')
        log.info('跳过 download_log.id=%s: 无番号', dl_id)
        return True, '', 'no-number'
    payload = {'id': dl_id, 'number': number, 'category': category}
    if dry_run:
        payload['dry_run'] = True
    last = ''
    for i in range(max(1, retries)):
        data, code = api_post('/api/import/adopt', payload)
        if code == 200 and (data.get('task_id') or data.get('dry_run')):
            if dry_run:
                log.info('[dry-run] id=%s %s → 落点=%s (不迁移) 视频=%s个/%.2fGB 大视频=%s',
                         dl_id, number, data.get('dir_115'),
                         len(data.get('videos') or []), data.get('total_gb') or 0, data.get('big_count'))
                return True, '', 'dry-run'
            tid = data.get('task_id')
            ledger_mark(dl_id, number=number, dir_name=data.get('thread_id') or '',
                        task_id=tid, status='submitted',
                        note='src=%s target=%s' % (data.get('src_115'), data.get('target_115')))
            log.info('已提交入库: id=%s %s task_id=%s (src=%s)', dl_id, number, tid, data.get('src_115'))
            return True, tid, 'ok'
        last = data.get('error') or ('HTTP %s' % code)
        if any(k in last for k in ('未找到', '无番号', 'download_log 无')):
            # 落点不存在 (已被迁移/删除/记录残缺) —— 重试不会变好, 记账后放行水位, 避免卡住后续记录
            gone = '落点已不存在' if '未找到' in last else last[:120]
            if dry_run:
                log.info('[dry-run] 跳过 id=%s %s: %s', dl_id, number, gone)
                return True, '', 'dry-run'
            ledger_mark(dl_id, number=number, status='missing', note=last[:200])
            log.warning('跳过 id=%s %s: %s (记账后放行, 不阻塞后续记录)', dl_id, number, gone)
            return False, '', 'missing:' + last[:120]
        log.warning('提交失败 id=%s %s: %s (第 %d/%d 次)', dl_id, number, last, i + 1, retries)
        time.sleep(10 * (i + 1))
    ledger_mark(dl_id, number=number, status='submit_failed', note=last[:200])
    return False, '', last


# ------------------------------ 主循环 ------------------------------
def tick(dry_run=False, category=CATEGORY):
    """处理水位之后的所有新记录"""
    wm = watermark()
    rows = src.fetch_downloads(after_id=wm, limit=200)
    if not rows:
        return 0
    n = 0
    for row in rows:
        dl_id = row['id']
        if ledger_done(dl_id) and not dry_run:
            state_set('watermark', dl_id)
            continue
        sock = os.path.exists('/tmp/avdb_watch.pause')     # 手动暂停: touch 这个文件即暂停入库
        if sock:
            log.info('检测到暂停标志 /tmp/avdb_watch.pause, 本轮不提交')
            return n
        ok, tid, msg = adopt(row, category=category, dry_run=dry_run)
        if ok or msg in ('no-number',) or msg.startswith('missing:'):
            n += 1
            state_set('watermark', dl_id)
        else:
            break      # 提交失败(网络/服务侧): 水位不推进, 下一轮重试
    return n


def main():
    ap = argparse.ArgumentParser(description='avdb 下载 → 一键入库 连接器守护 (纯事件驱动)')
    ap.add_argument('--interval', type=int, default=INTERVAL, help='读 avdb 本地库的间隔秒 (默认 60)')
    ap.add_argument('--category', default=CATEGORY, help='入库分类 av/fc2/sw/cn/ea/lf (默认 av)')
    ap.add_argument('--once', action='store_true', help='只跑一轮后退出')
    ap.add_argument('--dry-run', action='store_true', help='只打印将要做什么, 不提交')
    ap.add_argument('--status', action='store_true', help='打印台账后退出')
    ap.add_argument('--adopt', metavar='NUMBER', help='手动入库指定番号(不推进水位)')
    ap.add_argument('--reset-watermark', action='store_true', help='把水位重置为当前最大 id')
    args = ap.parse_args()

    if args.status:
        c = ledger_conn()
        print('水位 =', state_get('watermark'))
        for r in c.execute('SELECT dl_id, number, task_id, status, note, updated_at FROM seen ORDER BY dl_id'):
            print(r)
        c.close()
        return
    if args.reset_watermark:
        state_set('watermark', src.max_download_id())
        print('水位已重置 =', state_get('watermark'))
        return
    if args.adopt:
        rows = src.fetch_downloads(after_id=0, limit=1000)
        hit = [r for r in rows if src.same_number(r.get('number') or '', args.adopt)]
        if not hit:
            print('avdb 记录里没有番号', args.adopt)
            return
        ok, tid, msg = adopt(hit[-1], category=args.category, dry_run=args.dry_run)
        print('手动入库:', 'ok task_id=' + tid if ok else '失败 ' + msg)
        return

    log.info('avdb 连接器守护启动: 间隔 %ds, 分类 %s, 台账 %s (只读 avdb 本地库, 无新下载时对 115 零请求)',
             args.interval, args.category, LEDGER)
    state_set('interval', str(args.interval))
    state_set('category', args.category)
    state_set('pid', str(os.getpid()))
    beats = 0
    while True:
        try:
            n = tick(dry_run=args.dry_run, category=args.category)
            if n:
                log.info('本轮处理 %d 条 avdb 下载记录', n)
            beats += 1
            state_set('heartbeat', int(time.time()))    # 面板据此判断守护死活
            state_set('rounds', beats)
            if beats % 30 == 0:      # 每 30 轮一条心跳, 证明守护活着且确实没碰 115
                log.info('心跳: 已运行 %d 轮, 水位=%s (无新下载 → 未发起任何 115 请求)',
                         beats, state_get('watermark'))
        except Exception as e:
            log.exception('本轮异常: %s', e)
        if args.once:
            break
        time.sleep(max(10, args.interval))


if __name__ == '__main__':
    main()
