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
import avdb_classify as classifier   # noqa: E402  (2026-09-27: 逐条自动分类)

API = os.environ.get('IMPORT_API', 'http://127.0.0.1:5081')
LEDGER = os.environ.get('AVDB_WATCH_DB', '/var/lib/avdb-watch/avdb_watch.db')
LOG = os.environ.get('AVDB_WATCH_LOG', '/var/log/avdb_watch.log')
INTERVAL = int(os.environ.get('AVDB_WATCH_INTERVAL', '60'))     # 读 avdb 本地库的间隔(秒)
CATEGORY = os.environ.get('AVDB_WATCH_CATEGORY', 'av')           # 入库分类 (av/fc2/sw/cn/ea/lf/auto)
AUTO_CATEGORY = 'auto'                                          # 逐条判定 (见 avdb_classify.py)
# 手动提交的磁力(avdb「手动提交链接」)没有番号: 改成「等 115 落点目录出现」再认领。
# 这两个值只管等待的节奏, 避免每 60 秒都去列一次 115 目录。
WAIT_TTL = int(os.environ.get('AVDB_WATCH_WAIT_TTL', '10800'))       # 最长等 3 小时
# 等了 N 次 (N × WAIT_RECHECK) 仍没落点 → 面板/日志加「疑似重复提交」提示 (不改状态)
DUPLICATE_HINT_TRIES = int(os.environ.get('AVDB_WATCH_DUP_HINT_TRIES', '12'))
WAIT_RECHECK = int(os.environ.get('AVDB_WATCH_WAIT_RECHECK', '300'))  # 每 5 分钟重看一次

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
    # 2026-09-27: 等待态用的列 (手动提交的磁力: 落点目录还没出现时记 waiting)
    cols = [d[1] for d in c.execute('PRAGMA table_info(seen)').fetchall()]
    for col, ddl in (('tries', 'INTEGER DEFAULT 0'), ('wait_since', 'TEXT'), ('last_try', 'TEXT')):
        if col not in cols:
            c.execute('ALTER TABLE seen ADD COLUMN %s %s' % (col, ddl))
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
        # 已提交过任务, 或已判定为无需处理(missing/skipped/expired) → 不再重复
        return bool(r and (r[0] or r[1] in ('missing', 'skipped', 'expired')))
    finally:
        c.close()


def ledger_wait(dl_id, note=''):
    """记一次「等待落点出现」(手动提交的磁力): tries+1, 首次写 wait_since, 每次写 last_try。
    返回 (tries, wait_since)"""
    c = ledger_conn()
    try:
        r = c.execute('SELECT tries, wait_since FROM seen WHERE dl_id=?', (int(dl_id),)).fetchone()
        tries = int((r[0] if r else 0) or 0) + 1
        ws = (r[1] if r else None)
        if ws:
            c.execute("""UPDATE seen SET tries=?, last_try=datetime('now','localtime'),
                         status='waiting', note=? WHERE dl_id=?""", (tries, note[:200], int(dl_id)))
        else:
            c.execute("""UPDATE seen SET tries=?, wait_since=datetime('now','localtime'),
                         last_try=datetime('now','localtime'), status='waiting', note=?
                         WHERE dl_id=?""", (tries, note[:200], int(dl_id)))
        c.commit()
        return tries, ws
    finally:
        c.close()


def _ledger_ts(s):
    if not s:
        return 0.0
    try:
        return time.mktime(time.strptime(str(s)[:19], '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return 0.0


def _waiting_state(dl_id):
    """等待态判定: None=不在等待(正常处理) / 'backoff'=还没到重看时间 / 'expired'=等太久了"""
    c = ledger_conn()
    try:
        cols = [d[1] for d in c.execute('PRAGMA table_info(seen)').fetchall()]
        if 'wait_since' not in cols:
            return None
        r = c.execute('SELECT status, wait_since, last_try FROM seen WHERE dl_id=?', (int(dl_id),)).fetchone()
    finally:
        c.close()
    if not r or (r[0] or '') != 'waiting':
        return None
    now = time.time()
    ws, lt = _ledger_ts(r[1]), _ledger_ts(r[2])
    if ws and now - ws > WAIT_TTL:
        return 'expired'
    if lt and now - lt < WAIT_RECHECK:
        return 'backoff'
    return None


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


def _resolve_category(row, category):
    """分类解析 (2026-09-27)
    category == 'auto' → 逐条判定 (avdb_classify: 先回查 article 帖子的
    section/category, 再用番号/标题规则兜底); 其他值 → 固定分类 (旧行为)。
    返回 (cat, reason)"""
    if str(category).strip().lower() != AUTO_CATEGORY:
        return category, '固定分类'
    info = classifier.classify_download_row(row)
    return info['category'], info['reason']


def adopt(row, category=CATEGORY, dry_run=False, retries=3):
    """把一条 avdb 下载记录交给入库侧。返回 (ok, task_id, msg)"""
    dl_id = row['id']
    number = (row.get('number') or '').strip()
    manual = str(row.get('source') or '').startswith('manual')
    # 2026-09-27: avdb「手动提交链接」的行 number/tid 全空, 以前一律 skipped → 片子躺在
    # 115 上永远不入库(用户报障)。现在: 交给入库侧按 115 落点目录认领; 落点还没出现就记
    # waiting, 之后每 WAIT_RECHECK 秒重看一次(不阻塞后续记录)。
    # 2026-09-28: 「有帖但没番号」的行(欧美片: BangBus/Blacked 这类, article.number 为空)
    # 以前也走 skipped —— 但它们的 resource_name 就是磁力文件名 = 115 目录名, 完全可以按名字
    # 认领。现在只要能从 resource_name/title 里凑出候选名, 就交给入库侧认领, 不再跳过。
    if not number and not manual:
        cands = src.name_candidates(row.get('resource_name') or row.get('title'))
        if not cands:
            ledger_mark(dl_id, status='skipped', note='记录无番号(手动下载/测试记录)')
            log.info('跳过 download_log.id=%s: 无番号', dl_id)
            return True, '', 'no-number'
        log.info('id=%s 无番号但有候选名 %s → 交给入库侧按 115 目录认领', dl_id, cands)
    cat, why = _resolve_category(row, category)
    payload = {'id': dl_id, 'category': cat}
    if number:
        payload['number'] = number
    if dry_run:
        payload['dry_run'] = True
    last = ''
    for i in range(max(1, retries)):
        data, code = api_post('/api/import/adopt', payload)
        if code == 200 and (data.get('task_id') or data.get('dry_run')):
            if dry_run:
                log.info('[dry-run] id=%s %s → 落点=%s (不迁移) 视频=%s个/%.2fGB 大视频=%s 分类=%s(%s)',
                         dl_id, number or (data.get('number') or '(按目录认领)'), data.get('dir_115'),
                         len(data.get('videos') or []), data.get('total_gb') or 0, data.get('big_count'),
                         cat, why)
                return True, '', 'dry-run'
            tid = data.get('task_id')
            num_show = number or (data.get('number') or '')
            ledger_mark(dl_id, number=num_show, dir_name=data.get('thread_id') or '',
                        task_id=tid, status='submitted',
                        note='cat=%s(%s) src=%s target=%s' % (cat, why, data.get('src_115'), data.get('target_115')))
            log.info('已提交入库: id=%s %s 分类=%s(%s) task_id=%s (src=%s)',
                     dl_id, num_show, cat, why, tid, data.get('src_115'))
            return True, tid, 'ok'
        if data.get('waiting'):
            # 手动提交的磁力: 115 上还没落地目录 (不是失败, 也不是终态)
            note = (data.get('error') or '等待 115 落点出现')[:200]
            if dry_run:
                log.info('[dry-run] id=%s 手动提交 → %s (save_path=%s)', dl_id, note, data.get('save_path'))
                return True, '', 'dry-run'
            ledger_mark(dl_id, status='waiting', note=note)
            tries, ws = ledger_wait(dl_id, note)
            # 2026-09-28: 反复等不到落点, 多半是「重复提交」—— 115 按 info_hash 去重, 同一磁力
            # 不会新建任务 (实例: id=607 重发 9/27 已入库的 BigTitsAtWork 磁力 → 永远等不到新目录)。
            if tries >= DUPLICATE_HINT_TRIES:
                hint = (note + '；等了 %d 分钟仍无新落点, 疑似重复提交'
                              '(同一磁力 115 按 info_hash 去重, 不会新建任务)' % (tries * max(1, WAIT_RECHECK) // 60))[:200]
                ledger_wait(dl_id, hint)
                note = hint
            log.info('id=%s 手动提交的磁力: %s → 记 waiting(第 %d 次, %s 起), 每 %d 分钟重看',
                     dl_id, note, tries, ws or '现在', max(1, WAIT_RECHECK // 60))
            return True, '', 'waiting'
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
    blocked = False        # 有行停在 waiting → 水位不推进(但后续行照常处理)
    for row in rows:
        dl_id = row['id']
        if not dry_run:
            if ledger_done(dl_id):
                if not blocked:
                    state_set('watermark', dl_id)
                continue
            st = _waiting_state(dl_id)          # None / 'backoff' / 'expired'
            if st == 'expired':
                ledger_mark(dl_id, status='expired',
                            note='等待 115 落点出现超时(%d 小时), 放弃' % max(1, WAIT_TTL // 3600))
                log.warning('id=%s 等待 115 落点超时, 放行不再重试', dl_id)
                if not blocked:
                    state_set('watermark', dl_id)
                continue
            if st == 'backoff':
                blocked = True
                continue
        sock = os.path.exists('/tmp/avdb_watch.pause')     # 手动暂停: touch 这个文件即暂停入库
        if sock:
            log.info('检测到暂停标志 /tmp/avdb_watch.pause, 本轮不提交')
            return n
        ok, tid, msg = adopt(row, category=category, dry_run=dry_run)
        if ok or msg in ('no-number',) or msg.startswith('missing:'):
            n += 1
            if msg == 'waiting':
                blocked = True       # 这一行还没落地: 水位留在它身上, 每 5 分钟再看一次
            elif not blocked:
                state_set('watermark', dl_id)
        else:
            break      # 提交失败(网络/服务侧): 水位不推进, 下一轮重试
    return n


def main():
    ap = argparse.ArgumentParser(description='avdb 下载 → 一键入库 连接器守护 (纯事件驱动)')
    ap.add_argument('--interval', type=int, default=INTERVAL, help='读 avdb 本地库的间隔秒 (默认 60)')
    ap.add_argument('--category', default=CATEGORY,
                    help='入库分类 av/fc2/sw/cn/ea/lf, 或 auto=逐条自动判定 (默认 av)')
    ap.add_argument('--explain', metavar='NUMBER', help='打印某个番号的分类判定理由后退出')
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
    if args.explain:
        rows = src.fetch_downloads(after_id=0, limit=1000)
        hit = [r for r in rows if src.same_number((r.get('number') or ''), args.explain)]
        if not hit:
            print('avdb 记录里没有番号', args.explain, '—— 只做规则判定:')
            print('   →', classifier.classify(number=args.explain))
            return
        for r in hit[-3:]:
            cat, why = _resolve_category(r, AUTO_CATEGORY)
            print('id=%s number=%s title=%s' % (r.get('id'), r.get('number'), (r.get('title') or '')[:60]))
            print('   → 分类=%s  理由=%s' % (cat, why))
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
