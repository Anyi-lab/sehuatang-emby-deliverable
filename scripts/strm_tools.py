#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm_tools.py — 从 115-Desktop 搬过来的 STRM 工具箱（面板后端用）
====================================================================
对应 115-Desktop 内部的四个模块（能力逐条搬过来，适配我们的场景）：

  app/common/strm_utils.py              -> scan_authorities / fix_authority
      扫描本地 .strm 目录，统计用到的 host:port；把 host 批量改成新地址（端口不变）。

  app/services/strm_manifest.py         -> Manifest
      记录 pickcode <-> strm 落点 <-> 115 源目录，供「增量更新」和「清理已删除」用。

  app/services/strm_schedule_service.py -> Scheduler
      定时生成任务（源目录/参数快照/执行间隔 分-时-天 / 上次-下次运行）。

  app/components/strm_generator_dialog  -> （参数在 strm_panel.py 里）
      扩展名过滤 / 体积过滤 / 追加提取码 / 保持目录结构 / 增量 / 清理已删除。

不含「保存到远程 + 下载器(aria2/bitcomet/IDM)」那一组能力 —— 那套是给下载器推流的，
我们只写 strm 指针交给 MDCng/Emby，推下载器在这里没有意义，故不移植。
"""
import json
import os
import re
import socket
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
MANIFEST_PATH = os.path.join(DATA_DIR, 'strm_manifest.json')
SCHEDULE_PATH = os.path.join(DATA_DIR, 'strm_schedules.json')

# strm 内容形如 http://192.168.2.238:11501/d/<pickcode>/<url编码文件名>
#   (11501 = 本地中继 strm-relay；直发 :11500/d/ 恒 302 且签名绑 UA → Emby 403)
RE_AUTH = re.compile(r'(?P<scheme>https?://)(?P<host>[^/:\s]+)(?::(?P<port>\d+))?')
RE_PICKCODE = re.compile(r'/(?:d|play|strm)/([0-9a-z]{8,})/')


# ------------------------------------------------------------------ 地址扫描 / 修正
def _iter_strm(root, max_files=200000):
    n = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if not f.lower().endswith('.strm'):
                continue
            yield os.path.join(base, f)
            n += 1
            if n >= max_files:
                return


def scan_authorities(root):
    """递归扫 .strm，统计内容里用到的 host:port。
    -> {'host:port': {'count': n, 'sample': 一个文件路径}}"""
    counts = {}
    for p in _iter_strm(root):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                c = f.read(2000)
        except OSError:
            continue
        m = RE_AUTH.search(c)
        if not m:
            key = '(无法识别)'
        else:
            key = m.group('host') + (':' + m.group('port') if m.group('port') else '')
        it = counts.setdefault(key, {'count': 0, 'sample': p})
        it['count'] += 1
    return counts


def fix_authority(root, new_host, dry=True):
    """把 root 下所有 .strm 的 host 换成 new_host（端口保持原样）。
    dry=True 只统计不改；-> {'updated': n, 'skipped': n, 'old': {...}, 'files': [前若干] }"""
    new_host = (new_host or '').strip()
    if not new_host:
        raise ValueError('新 host 不能为空')
    if '://' in new_host:
        raise ValueError('只填 host（域名/IP/主机名），不要带 http://')
    if '/' in new_host or ':' in new_host:
        raise ValueError('只填 host（域名/IP/主机名），端口会保持不变')
    updated = skipped = 0
    old_counts = {}
    samples = []
    for p in _iter_strm(root):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                c = f.read()
        except OSError:
            skipped += 1
            continue
        m = RE_AUTH.search(c)
        if not m:
            skipped += 1
            continue
        key = m.group('host') + (':' + m.group('port') if m.group('port') else '')
        old_counts[key] = old_counts.get(key, 0) + 1
        if m.group('host') == new_host:
            skipped += 1
            continue
        new_c = c[:m.start('host')] + new_host + c[m.end('host'):]
        if dry:
            updated += 1
            if len(samples) < 5:
                samples.append({'file': p, 'old': c.strip()[:120], 'new': new_c.strip()[:120]})
            continue
        try:
            tmp = p + '.fixtmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_c)
            os.replace(tmp, p)
            updated += 1
        except OSError:
            skipped += 1
    return {'updated': updated, 'skipped': skipped, 'old': old_counts,
            'dry': dry, 'files': samples}


# ------------------------------------------------------------------ 清单 manifest
class Manifest:
    """{pickcode: {'strm': 本地路径, 'src': 115 文件路径, 'srcdir': 115 目录, 'size': n, 'ts': 时间}}"""

    def __init__(self, path=MANIFEST_PATH):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            return d.get('entries', {}) if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save(self, entries):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'entries': entries}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def record(self, items):
        """items: [{'pc','dst','src','size'}] -> 新增/更新条数"""
        if not items:
            return 0
        with self._lock:
            e = self.load()
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            n = 0
            for i in items:
                pc = i.get('pc')
                if not pc:
                    continue
                src = i.get('src') or ''
                sd = os.path.dirname(src.strip('/'))
                e[pc] = {'strm': i.get('dst'), 'src': src,
                         'srcdir': ('/' + sd) if sd else '',
                         'size': i.get('size'), 'ts': now}
                n += 1
            self.save(e)
            return n

    def forget(self, pcs):
        if not pcs:
            return 0
        with self._lock:
            e = self.load()
            n = 0
            for pc in pcs:
                if e.pop(pc, None):
                    n += 1
            if n:
                self.save(e)
            return n

    def stats(self):
        e = self.load()
        dirs = {}
        missing = 0
        for pc, v in e.items():
            d = v.get('srcdir') or ''
            dirs[d] = dirs.get(d, 0) + 1
            if v.get('strm') and not os.path.exists(v['strm']):
                missing += 1
        return {'total': len(e), 'src_dirs': len(dirs), 'strm_missing': missing,
                'dirs': sorted(((k, v) for k, v in dirs.items()), key=lambda x: -x[1])[:50]}


# ------------------------------------------------------------------ 定时任务
UNITS = {'minutes': 60, 'hours': 3600, 'days': 86400}


class Scheduler:
    """极简定时生成服务：每 20 秒扫一遍，到点就调 runner(task)。
    runner(task) 由面板提供，负责真正生成（内部再用 run_task 的日志机制）。"""

    def __init__(self, runner, path=SCHEDULE_PATH, tick=20):
        self.runner = runner
        self.path = path
        self.tick = tick
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._running_ids = set()

    # ---- 持久化
    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            return d.get('tasks', []) if isinstance(d, dict) else []
        except Exception:
            return []

    def save(self, tasks):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'tasks': tasks}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---- CRUD
    def list(self):
        with self._lock:
            return self.load()

    def upsert(self, task):
        with self._lock:
            tasks = self.load()
            tid = task.get('id')
            for i, t in enumerate(tasks):
                if t.get('id') == tid:
                    tasks[i] = dict(t, **task)
                    break
            else:
                tasks.append(task)
            self.save(tasks)
            return task

    def delete(self, tid):
        with self._lock:
            tasks = [t for t in self.load() if t.get('id') != tid]
            self.save(tasks)
            return True

    def mark(self, tid, **kw):
        with self._lock:
            tasks = self.load()
            for t in tasks:
                if t.get('id') == tid:
                    t.update(kw)
                    break
            self.save(tasks)

    def run_now(self, tid):
        t = self.get(tid)
        if not t:
            raise KeyError('no such task: %s' % tid)
        self._spawn(t)
        return t

    def get(self, tid):
        for t in self.list():
            if t.get('id') == tid:
                return t
        return None

    # ---- 循环
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print('[sched] tick 出错: %s' % e, flush=True)
            self._stop.wait(self.tick)

    def _tick(self):
        now = time.time()
        for t in self.list():
            if not t.get('enabled'):
                continue
            nxt = float(t.get('next_ts') or 0)
            if nxt and now < nxt:
                continue
            if t.get('id') in self._running_ids:
                continue
            self._spawn(t)

    def _spawn(self, t):
        tid = t.get('id')
        if tid in self._running_ids:
            print('[sched] %s 上一轮还在跑，跳过' % tid, flush=True)
            return
        self._running_ids.add(tid)
        interval = int(t.get('interval_value') or 1) * UNITS.get(t.get('interval_unit') or 'days', 86400)
        self.mark(tid, last_run=time.strftime('%Y-%m-%d %H:%M:%S'), last_ts=time.time(),
                  next_ts=time.time() + interval,
                  next_run=time.strftime('%m-%d %H:%M', time.localtime(time.time() + interval)))

        def _go():
            try:
                self.runner(t)
                self.mark(tid, last_result='OK', last_error='')
            except Exception as e:
                self.mark(tid, last_result='FAIL', last_error=str(e)[:200])
                print('[sched] %s 执行失败: %s' % (tid, e), flush=True)
            finally:
                self._running_ids.discard(tid)

        threading.Thread(target=_go, daemon=True).start()


# ------------------------------------------------------------------ 杂项
# 注意：2026-09-24 起 strm 默认端口改为 11501（本地中继 strm-relay），
# 因为 115-Desktop 直发地址 :11500/d/ 恒 302 且 CDN 直链签名绑 UA → Emby 403。
# 想切回直连（Jellyfin 客户端直连 / 神医助手独占模式）用环境变量 STRM_HOST 覆盖。
def local_host():
    """当前 strm 用的 host（默认走 115-Desktop 的局域网地址）"""
    try:
        import mcp115
        return mcp115.STRM_HOST.split('://', 1)[-1]
    except Exception:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip + ':11501'
        except Exception:
            return '192.168.2.238:11501'


def build_strm_url(host, pc, name):
    """host: 'current' | 'localhost' | 'x.x.x.x[:port]'"""
    import urllib.parse
    if host in (None, '', 'current'):
        base = local_host()
    elif host == 'localhost':
        base = '127.0.0.1:11501'
    else:
        base = host.strip()
    if '://' in base:
        base = base.split('://', 1)[1]
    if ':' not in base:
        base += ':11501'
    return 'http://%s/d/%s/%s' % (base, pc, urllib.parse.quote(name))
