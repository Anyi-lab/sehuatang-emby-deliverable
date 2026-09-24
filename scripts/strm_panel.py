#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm_panel.py — 115 存量 → strm 生成面板 (http://127.0.0.1:5091)
====================================================================
在浏览器里点选 115 上的目录/视频 → 预览落点 → 一键生成 .strm，
之后 MDCng watcher 自动刮削、刷新 Emby。全程只写本地 .strm，不动 115 上任何文件。

  python3 strm_panel.py            # 默认 :5091
  python3 strm_panel.py --port 5091

接口:
  GET  /                    面板页面
  GET  /api/list?path=      列 115 一层目录
  POST /api/plan            干跑: 返回将写出的 strm 列表(只读)
  POST /api/run             真跑: 生成 strm + 等刮削 + 刷 Emby
  GET  /api/task?id=        任务日志/状态
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
SRV = '/root/clacky_workspace/sehuatang-emby-deliverable/src/server'
for p in (SRV, BASE, '/root/clacky_workspace'):
    if p not in sys.path:
        sys.path.insert(0, p)

import import_api as ia                            # noqa: E402
import backfill_scrape as bf                       # noqa: E402
import strm_tools as st                            # noqa: E402
from mcp115 import MCP115, MEDIA_EXTS, STRM_HOST   # noqa: E402

_lock = threading.Lock()
_tasks = {}
_cache = {}            # key -> (ts, payload)  短 TTL 缓存, 来回导航秒开
_cache_lock = threading.Lock()
CACHE_TTL = 180

# 自己手动存的存量资源专用根 —— 与流水线的 待看/sehuatang_* 完全分开
# MDCng watch: /media/待看/自存 -> /media/已刮削/自存
SELF_STRM_ROOT = '/mnt/g/srtm/待看/自存'
SELF_TARGET_ROOT = '/mnt/g/srtm/已刮削/自存'

# 视频扩展名过滤（对应 115-Desktop「视频文件后缀」）—— 留空 = 沿用 MEDIA_EXTS
DEFAULT_EXTS = sorted(e.lstrip('.').lower() for e in MEDIA_EXTS)
MANIFEST = st.Manifest()


# ---------------------------------------------------------------- helpers
def local_root_for(path, category, name='', source='pipeline', layout='flat'):
    """115 路径 -> 本地 strm 一级目录。
    source='self' + layout='flat' -> 待看/自存/<name>      (全部放进我指定的目录)
    source='self' + layout='auto' -> 待看/自存/[name/]<115 目录名>
                                      (按 115 结构自动分层, 每个勾选项一个文件夹)
    source='pipeline'             -> 与 backfill_scrape 同规则 (sehuatang_<分类> / sehuatang 镜像)
    """
    p = (path or '').strip('/')
    leaf = p.split('/')[-1] if p else ''
    if source == 'self':
        nm = (name or '').strip('/').replace('..', '').strip('/')
        if layout == 'auto' and leaf:
            return os.path.join(SELF_STRM_ROOT, nm, leaf) if nm else os.path.join(SELF_STRM_ROOT, leaf)
        nm = nm or leaf
        return os.path.join(SELF_STRM_ROOT, nm) if nm else SELF_STRM_ROOT
    if p.startswith('sehuatang_tv/') or p.startswith('sehuatang/'):
        return ia._local_strm_dir(path, category)
    nm = name or (p.split('/')[-1] if p else 'backfill')
    nm = nm.replace('/', '_').strip() or 'backfill'
    return os.path.join(ia.category_strm_root(ia.norm_category(category)), nm)


def strm_name(fn, pc=None, append_pickcode=False):
    """视频文件名 -> strm 文件名。
    append_pickcode（对应 115-Desktop「文件名追加提取码」）: 原名_<pickcode>.strm，
    同名视频不同 pickcode 时不会互相覆盖。"""
    stem = os.path.splitext(fn)[0]
    if append_pickcode and pc:
        stem = '%s_%s' % (stem, pc)
    return stem + '.strm'


def file_target(m, path, exts=None):
    """单个视频文件 -> {'fn','pc','size'}；不是视频/不存在返回 None"""
    exts = exts or MEDIA_EXTS
    rel = path.strip('/')
    parent, fn = os.path.dirname(rel), os.path.basename(rel)
    cid = m.resolve_cid(parent)
    if cid is None:
        return None
    for e in m.list_entries(cid):
        if not e['is_dir'] and e['fn'] == fn:
            if os.path.splitext(fn)[1].lower() not in exts or not e['pc']:
                return None
            return {'fn': e['fn'], 'pc': e['pc'], 'size': e['size']}
    return None


def _add_origin(origins, root, src, n):
    """同一落点目录可能被多个 115 目录喂（flat 布局），用 sources 全记下来"""
    o = origins.setdefault(root, {'origin': src, 'count': 0, 'sources': []})
    o['count'] += n
    if src not in o['sources']:
        o['sources'].append(src)
    o['origin'] = (o['sources'][0] if len(o['sources']) == 1
                   else '%s 等 %d 个来源目录' % (o['sources'][0], len(o['sources'])))


def build_plan(targets, category, name, max_depth, min_size_mb, source='pipeline', layout='flat',
               exts=None, append_pickcode=False):
    """targets: [{'path','is_dir'}] -> {'items':[{src,dst,pc,size,exists}], ...}
    exts: 自定义视频扩展名集合({'.mp4',...})，None = MEDIA_EXTS
    append_pickcode: strm 名后追加 pickcode，防同名覆盖（115-Desktop 同款开关）"""
    m = MCP115()
    cat = ia.norm_category(category)
    exts = exts or MEDIA_EXTS
    self_store = (source == 'self')
    items, skipped, origins = [], [], {}
    for t in targets:
        p = (t.get('path') or '').strip() or '/'
        if t.get('is_dir'):
            try:
                vids = bf.collect_videos(m, p, min_size_mb, 0, max_depth, exts=exts)
            except SystemExit as e:                 # 115 上不存在/不是目录
                skipped.append({'path': p, 'why': str(e)[:160]})
                continue
            root = local_root_for(p, cat, name, source, layout)
            for v in vids:
                rel_dir = os.path.dirname(v['rel'])
                fn = strm_name(v['fn'], v['pc'], append_pickcode)
                dst = os.path.join(root, rel_dir, fn) if rel_dir else os.path.join(root, fn)
                items.append({'src': p.rstrip('/') + '/' + v['rel'], 'dst': dst, 'pc': v['pc'],
                              'fn': v['fn'], 'size': v['size'], 'exists': os.path.exists(dst)})
            if self_store and vids:
                _add_origin(origins, root, p, len(vids))
            if not vids:
                skipped.append({'path': p, 'why': '没找到视频(或都被体积/扩展名过滤)'})
        else:
            fe = file_target(m, p, exts=exts)
            if not fe:
                skipped.append({'path': p, 'why': '不是视频 / 无 pickcode / 已不存在 / 扩展名被过滤'})
                continue
            src_dir = os.path.dirname(p.strip('/'))
            root = local_root_for(src_dir, cat, name, source, layout)
            dst = os.path.join(root, strm_name(fe['fn'], fe['pc'], append_pickcode))
            items.append({'src': p, 'dst': dst, 'pc': fe['pc'], 'fn': fe['fn'], 'size': fe['size'],
                          'exists': os.path.exists(dst)})
            if self_store:
                _add_origin(origins, root, '/' + src_dir.lstrip('/'), 1)
    return {'items': items, 'skipped': skipped,
            'source': source, 'layout': layout, 'origins': origins,
            'watch_root': SELF_STRM_ROOT if self_store else ia.category_strm_root(cat),
            'target_root': SELF_TARGET_ROOT if self_store else ia.category_target_root(cat),
            'category_name': '自存（与 sehuatang 分开）' if self_store else ia.category_name(cat),
            'total_gb': sum(i['size'] for i in items) / 2 ** 30,
            'new_count': sum(1 for i in items if not i['exists'])}


def log(tk, msg):
    _tasks[tk]['log'].append('[%s] %s' % (time.strftime('%H:%M:%S'), msg))


# 自存落点 -> 115 来源 的对照表（只记在面板这边，不写进 watch 目录，免得被 MDCng 刮进媒体库）
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
SELF_SOURCES = os.path.join(DATA_DIR, 'self_sources.json')


def load_origins():
    try:
        with open(SELF_SOURCES, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def record_origins(origins):
    """origins: {dst_root: {'origin': 115路径, 'count': n}}"""
    if not origins:
        return
    d = load_origins()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for root, info in origins.items():
        prev = d.get(root) or {}
        d[root] = {'origin': info['origin'], 'count': info.get('count', 0),
                   'sources': info.get('sources', []),
                   'ts': now, 'first_ts': prev.get('first_ts') or now}
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SELF_SOURCES + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SELF_SOURCES)


def run_task(tk, spec):
    """spec: targets/category/name/max_depth/min_size_mb/source/layout/exts/append_pickcode/
             url_host/wait/refresh/meta_exts"""
    t = _tasks[tk]
    try:
        with _lock:                     # 只有「问 115 要清单」这一步需要串行
            log(tk, '解析 115 目录 ...')
            plan = build_plan(spec['targets'], spec['category'], spec['name'],
                              spec['max_depth'], spec['min_size_mb'], spec.get('source', 'pipeline'),
                              spec.get('layout', 'flat'), exts=_norm_exts(spec.get('exts')),
                              append_pickcode=bool(spec.get('append_pickcode')))
        t['plan'] = {'items': plan['items'], 'total_gb': plan['total_gb']}
        todo = [i for i in plan['items'] if not i['exists']]
        log(tk, '视频 %d 个 / 合计 %.2f GB / 需生成 %d 个（已存在跳过 %d）'
            % (len(plan['items']), plan['total_gb'], len(todo), len(plan['items']) - len(todo)))
        if not todo:
            log(tk, '未发现新文件')
        meta_exts = [e.strip().lstrip('.').lower() for e in (spec.get('meta_exts') or []) if e and e.strip()]
        host = spec.get('url_host')
        written = meta_n = 0
        done, m = [], None
        for i in todo:
            if t.get('cancel'):
                log(tk, '已停止：本轮生成 %d 个后中断，剩余 %d 个未处理' % (written, len(todo) - written))
                break
            _ctl_wait(tk)               # 暂停点（对应 115-Desktop「暂停生成」）
            if t.get('cancel'):
                break
            url = st.build_strm_url(host, i['pc'], os.path.basename(i['src']))
            os.makedirs(os.path.dirname(i['dst']), exist_ok=True)
            with open(i['dst'], 'w', encoding='utf-8') as f:
                f.write(url)
            written += 1
            done.append(i)
            if meta_exts:
                try:
                    if m is None:
                        m = MCP115()
                    meta_n += sync_metadata(m, os.path.dirname(i['src'].strip('/')), i['fn'],
                                            os.path.dirname(i['dst']), meta_exts)
                except Exception as e:
                    log(tk, '元数据同步失败（忽略）: %s' % str(e)[:100])
            if written % 25 == 0:
                log(tk, '已生成 %d 个STRM%s' % (written, ('，%d 个元数据' % meta_n) if meta_n else ''))
        t['written'] = written
        if done:
            try:
                log(tk, '清单已记录 %d 条（增量更新 / 清理已删除用）' % MANIFEST.record(done))
            except Exception as e:
                log(tk, '清单记录失败（不影响生成）: %s' % e)
        if plan.get('source') == 'self' and plan.get('origins'):
            try:
                record_origins(plan['origins'])
                log(tk, '来源对照已记录 %d 条（面板可查）' % len(plan['origins']))
            except Exception as e:
                log(tk, '来源记录失败（不影响生成）: %s' % e)
        log(tk, '共写入 %d 个 strm%s，已交给 MDCng watcher 刮削'
            % (written, ('，%d 个元数据' % meta_n) if meta_n else ''))

        if spec.get('wait') and (todo or plan['items']):
            done_dirs = {os.path.dirname(i['dst']) for i in plan['items']}
            t0 = time.time()
            hit = set()
            while time.time() - t0 < spec['wait']:
                for d in sorted(done_dirs - hit):
                    ok, n, im = ia._has_local_metadata(d)
                    if ok:
                        hit.add(d)
                        log(tk, 'MDCng 刮削完成 %s (%d nfo / %d 图)' % (d, n, im))
                if len(hit) == len(done_dirs):
                    break
                time.sleep(5)
            log(tk, '刮削等待结束：完成 %d/%d 个目录（未完成的可能刮不出元数据或仍在队列）'
                % (len(hit), len(done_dirs)))

        if spec.get('refresh'):
            try:
                log(tk, 'Emby 刷新返回 %s' % ia.media_refresh(timeout=30))
            except Exception as e:
                log(tk, 'Emby 刷新失败（不影响文件）: %s' % e)
        t['state'] = 'done'
        log(tk, '完成')
    except (Exception, SystemExit) as e:
        t['state'] = 'error'
        log(tk, '出错: %s' % e)
        log(tk, traceback.format_exc().splitlines()[-1])


# ---------------------------------------------------------------- strm 工具箱后端
def _norm_exts(raw):
    """'mp4,mkv,iso' / ['mp4','.iso'] -> {'.mp4','.mkv','.iso'}；空 = None(用 MEDIA_EXTS)"""
    if not raw:
        return None
    if isinstance(raw, str):
        parts = [p for p in re.split(r'[,\s;]+', raw) if p]
    else:
        parts = list(raw)
    out = {('.' + p.strip().lstrip('.').lower()) for p in parts if p and p.strip()}
    return out or None


def _ctl_wait(tk):
    """暂停支持：面板点「暂停」后停在这里（对应 115-Desktop「暂停生成」）"""
    while _tasks.get(tk, {}).get('pause') and not _tasks.get(tk, {}).get('cancel'):
        time.sleep(0.5)


def _download(url, dst, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'strm-panel/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dst, 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)


def sync_metadata(m, src_dir, video_fn, dst_dir, meta_exts):
    """把 115 上与视频同名的元数据文件(nfo/jpg/png/srt/ass...)拉到本地同目录。
    对应 115-Desktop「元数据后缀 · 同步下载匹配的元数据文件」。
    注意：MDCng 自己会写 nfo/海报，本功能默认关；开了会覆盖同名文件。"""
    stem = os.path.splitext(video_fn)[0]
    cid = m.resolve_cid(src_dir)
    if cid is None:
        return 0
    want = {}
    for e in m.list_entries(cid):
        if e['is_dir'] or not e['pc']:
            continue
        ext = os.path.splitext(e['fn'])[1].lower().lstrip('.')
        if ext not in meta_exts:
            continue
        s = os.path.splitext(e['fn'])[0]
        if s == stem or s.startswith(stem + '.') or s.startswith(stem + '-'):
            want[e['fn']] = e['pc']
    n = 0
    for fn, pc in want.items():
        dst = os.path.join(dst_dir, fn)
        try:
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                continue
            _download(st.build_strm_url('current', pc, fn), dst)
            n += 1
        except Exception:
            pass
    return n


def cleanup_deleted(dry=True, max_dirs=30, on_log=None):
    """清理已删除（对应 115-Desktop「清理已删除」）：
    清单里记过的 115 源目录重新列一遍，源文件已不在 115 上的 strm 删掉（默认 dry-run 只报告）。
    安全：只删面板自己记录过的 strm，且只按 pickcode 比对，不碰 115 上任何文件。"""
    entries = MANIFEST.load()
    by_dir = {}
    for pc, v in entries.items():
        by_dir.setdefault(v.get('srcdir') or '', []).append((pc, v))
    dead, checked = [], 0
    m = MCP115()
    for srcdir, rows in sorted(by_dir.items(), key=lambda x: -len(x[1]))[:max_dirs]:
        cid = m.resolve_cid(srcdir) if srcdir else None
        alive = set()
        if cid is not None:
            for e in m.list_entries(cid):
                if not e['is_dir'] and e['pc']:
                    alive.add(e['pc'])
        checked += 1
        for pc, v in rows:
            if cid is None or pc not in alive:
                dead.append({'pc': pc, 'strm': v.get('strm'), 'src': v.get('src'),
                             'why': '115 上已没有这个文件' if cid is not None else '115 目录已不存在'})
        if on_log:
            on_log('核对 %s：清单 %d 条 / 115 现存 %d 个' % (srcdir or '/', len(rows), len(alive)))
    removed = 0
    if not dry:
        for d in dead:
            p = d.get('strm')
            try:
                if p and os.path.exists(p):
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
        MANIFEST.forget([d['pc'] for d in dead])
    return {'dry': dry, 'checked_dirs': checked, 'total_dirs': len(by_dir),
            'dead': dead[:200], 'dead_count': len(dead), 'removed': removed}


# ---------------------------------------------------------------- 定时任务（115-Desktop「定时生成」同款）
def _sched_runner(task):
    """定时任务到点 -> 造一个 task_id，走同一个 run_task，日志在面板可看"""
    tk = uuid.uuid4().hex[:8]
    spec = dict(task.get('spec') or {})
    spec.setdefault('source', 'self')
    spec.setdefault('layout', 'flat')
    spec.setdefault('category', 'av')
    _tasks[tk] = {'id': tk, 'state': 'running', 'log': [], 'written': 0,
                  'created': time.time(), 'sched': task.get('id'), 'title': task.get('title')}
    log(tk, '定时任务「%s」触发' % (task.get('title') or task.get('id')))
    run_task(tk, spec)
    return tk


SCHED = st.Scheduler(_sched_runner)


# ---------------------------------------------------------------- HTML
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>115 → strm 生成面板</title>
<style>
/* ===== 主题变量 ===== */
:root[data-theme="dark"]{
  --bg:#0d1017; --bg2:#0a0d13; --card:#151a24; --card2:#1b2230; --line:#263043;
  --line2:#2f3a52; --fg:#e7ecf5; --fg2:#c3cbd9; --dim:#8b97ad; --acc:#4c8dff;
  --acc2:#7aa8ff; --ok:#35c98a; --warn:#f0b429; --danger:#ff5f6d; --shadow:0 8px 24px rgba(0,0,0,.35);
}
:root[data-theme="light"]{
  --bg:#f4f6fa; --bg2:#eef1f7; --card:#ffffff; --card2:#f7f9fc; --line:#dfe4ee;
  --line2:#cbd3e2; --fg:#141a24; --fg2:#3a4453; --dim:#6b7688; --acc:#2f6fe4;
  --acc2:#4a86f0; --ok:#12a06a; --warn:#b57a08; --danger:#d9384a; --shadow:0 6px 20px rgba(20,30,60,.10);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font:13.5px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.mono,pre,.path{font-family:ui-monospace,"Cascadia Mono",Consolas,"SF Mono",monospace}
a{color:var(--acc);text-decoration:none}
button,input,select,textarea{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:6px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}

/* ===== 布局 ===== */
.app{max-width:1280px;margin:0 auto;padding:0 18px 40px}
.top{display:flex;align-items:center;gap:14px;padding:14px 0 12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--acc),#9b6bff);
  color:#fff;display:grid;place-items:center;font-weight:700;font-size:12px;letter-spacing:.5px}
h1{font-size:15px;margin:0;font-weight:650;letter-spacing:.2px}
.sub{margin:0;color:var(--dim);font-size:11.5px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto;align-items:center}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
  border-radius:999px;padding:3px 10px;font-size:11.5px;color:var(--fg2);white-space:nowrap}
.chip b{color:var(--fg);font-weight:600}
.chip.ok{border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
.chip.run{border-color:var(--acc)}
.chip.warn{border-color:color-mix(in srgb,var(--warn) 50%,var(--line))}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim);display:inline-block}
.dot.ok{background:var(--ok)}.dot.run{background:var(--acc);animation:pulse 1.4s infinite}
.dot.warn{background:var(--warn)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.icon-btn{background:var(--card);border:1px solid var(--line);color:var(--fg2);width:32px;height:32px;
  border-radius:8px;cursor:pointer;display:grid;place-items:center;font-size:14px}
.icon-btn:hover{border-color:var(--line2);color:var(--fg)}

/* ===== 标签页 ===== */
.tabs{display:flex;gap:2px;align-items:center;border-bottom:1px solid var(--line);
  position:sticky;top:0;background:var(--bg);z-index:20;padding-top:2px}
.tabs .tab{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--dim);
  padding:9px 14px;cursor:pointer;font-size:13px;border-radius:6px 6px 0 0;transition:color .12s}
.tabs .tab:hover{color:var(--fg2);background:var(--card)}
.tabs .tab.on{color:var(--fg);border-bottom-color:var(--acc);font-weight:600}
.tabs .tab .n{display:inline-grid;place-items:center;min-width:16px;height:16px;padding:0 4px;margin-left:6px;
  border-radius:999px;background:var(--card2);border:1px solid var(--line);font-size:10.5px;color:var(--dim)}
.tabs .tab.on .n{color:var(--acc);border-color:var(--acc)}
.spacer{flex:1}

/* ===== 卡片 ===== */
main{padding-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:14px;
  box-shadow:var(--shadow);overflow:hidden}
.card-head{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid var(--line);
  background:var(--card2);flex-wrap:wrap}
.card-head h2{font-size:13px;margin:0;font-weight:650;display:flex;align-items:center;gap:8px}
.card-head .hint{margin-left:auto}
.card-body{padding:14px}
.hint{color:var(--dim);font-size:11.5px}
.hint b{color:var(--fg2);font-weight:600}
.sec{display:none}.sec.on{display:block}

/* ===== 表单 ===== */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px 14px}
.grid.wide{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
.field>label{color:var(--dim);font-size:11.5px;font-weight:500;letter-spacing:.2px}
.field .tip{color:var(--dim);font-size:11px;line-height:1.45}
input[type=text],input[type=number],select,textarea{
  background:var(--bg2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:13px;
  width:100%;transition:border-color .12s,background .12s}
input[type=text]:hover,select:hover{border-color:var(--line2)}
input[type=text]:focus,select:focus{border-color:var(--acc);background:var(--card)}
input::placeholder{color:var(--dim);opacity:.75}
select{appearance:none;padding-right:26px;
  background-image:linear-gradient(45deg,transparent 50%,var(--dim) 50%),linear-gradient(135deg,var(--dim) 50%,transparent 50%);
  background-position:calc(100% - 14px) 52%,calc(100% - 9px) 52%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.fieldset{border:1px solid var(--line);border-radius:10px;padding:12px 14px 14px;margin:0 0 12px;min-width:0}
.fieldset>legend{padding:0 6px;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.8px}
.switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:12.5px}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .track{width:36px;height:20px;border-radius:999px;background:var(--line2);position:relative;transition:background .15s;flex:0 0 auto}
.switch .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;
  background:#fff;transition:transform .15s}
.switch input:checked+.track{background:var(--acc)}
.switch input:checked+.track::after{transform:translateX(16px)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}

/* ===== 按钮 ===== */
button{background:var(--card2);border:1px solid var(--line);color:var(--fg);border-radius:8px;
  padding:7px 13px;font-size:12.5px;cursor:pointer;transition:border-color .12s,background .12s,opacity .12s}
button:hover{border-color:var(--line2)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.primary:hover{background:var(--acc2);border-color:var(--acc2)}
button.ghost{background:transparent}
button.danger{background:var(--danger);border-color:var(--danger);color:#fff}
button.mini{padding:3px 9px;font-size:11.5px;border-radius:7px}
button:disabled{opacity:.4;cursor:not-allowed}
button.link{background:none;border:0;color:var(--acc);padding:2px 4px;font-size:11.5px}
button.link:hover{text-decoration:underline}

/* ===== 面包屑 / 落点条 ===== */
.crumb{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:12px;margin-bottom:10px}
.crumb .seg{background:var(--card2);border:1px solid var(--line);border-radius:7px;padding:2px 9px;
  cursor:pointer;color:var(--fg2)}
.crumb .seg:hover{border-color:var(--acc);color:var(--fg)}
.crumb .sep{color:var(--dim);font-size:11px}
.crumb .cur{color:var(--fg);font-weight:600}
.dest{display:flex;align-items:center;gap:10px;background:var(--bg2);border:1px dashed var(--line2);
  border-radius:10px;padding:8px 12px;font-size:12px;margin-bottom:10px;flex-wrap:wrap}
.dest .k{color:var(--dim);flex:0 0 auto}
.dest .v{font-size:12px;word-break:break-all}
.dest .sep{color:var(--dim)}

/* ===== 列表 ===== */
.list-head{display:flex;align-items:center;gap:10px;padding:6px 10px;color:var(--dim);font-size:11px;
  border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.5px}
.rowbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
ul.list{list-style:none;margin:0;padding:0;max-height:54vh;overflow:auto}
ul.list li{display:flex;align-items:center;gap:10px;padding:5px 10px;border-radius:8px;cursor:default}
ul.list li:nth-child(even){background:color-mix(in srgb,var(--card2) 55%,transparent)}
ul.list li:hover{background:var(--card2)}
ul.list li input[type=checkbox]{width:15px;height:15px;accent-color:var(--acc);flex:0 0 auto;cursor:pointer}
ul.list li .ic{width:16px;text-align:center;flex:0 0 auto;opacity:.9}
ul.list li .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
ul.list li .nm.dir{cursor:pointer;color:var(--warn)}
ul.list li .nm.dir:hover{text-decoration:underline}
ul.list li .nm.file{cursor:pointer}
ul.list li .nm.file:hover{color:var(--acc2)}
ul.list li .sz{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;flex:0 0 auto;min-width:74px;text-align:right}
ul.list li .rowact{opacity:0;transition:opacity .12s;flex:0 0 auto}
ul.list li:hover .rowact{opacity:1}
.list-foot{display:flex;align-items:center;gap:10px;padding:9px 12px;border-top:1px solid var(--line);
  background:var(--card2);flex-wrap:wrap}
.list-foot .grow{flex:1}

/* ===== 徽章 / 表格 ===== */
.badge{display:inline-flex;align-items:center;gap:4px;padding:1px 7px;border-radius:999px;font-size:10.5px;
  border:1px solid var(--line);color:var(--dim);white-space:nowrap;font-weight:500}
.badge.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
.badge.run{color:var(--acc);border-color:var(--acc)}
.badge.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.badge.err{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 45%,var(--line))}
.badge.off{color:var(--dim)}
.tbl-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--bg2)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.3px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  background:var(--card2);position:sticky;top:0;z-index:2}
tbody tr:hover td{background:var(--card)}
tbody tr:last-child td{border-bottom:0}
td.dst,td.p{word-break:break-all;color:var(--fg2);font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.empty{padding:26px 14px;text-align:center;color:var(--dim);font-size:12.5px}
.empty .big{font-size:22px;display:block;margin-bottom:6px;opacity:.6}

/* ===== 统计块 ===== */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat .k{color:var(--dim);font-size:11px;margin-bottom:3px}
.stat .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.25}
.stat .v small{font-size:11.5px;color:var(--dim);font-weight:400;margin-left:3px}

/* ===== 进度 / 日志 ===== */
.bar{height:5px;background:var(--line);border-radius:999px;overflow:hidden;flex:1;min-width:90px}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));
  border-radius:999px;transition:width .3s}
.bar.indet>i{width:35%;animation:slide 1.2s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
pre{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:11px 12px;margin:10px 0 0;
  max-height:36vh;overflow:auto;font-size:11.6px;line-height:1.65;white-space:pre-wrap;word-break:break-word;color:var(--fg2)}
pre.err{border-color:color-mix(in srgb,var(--danger) 40%,var(--line))}
details.help{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;
  margin-bottom:14px;font-size:12.3px}
details.help summary{cursor:pointer;color:var(--fg2);font-weight:600}
details.help table{margin-top:8px}
kbd{background:var(--card2);border:1px solid var(--line2);border-bottom-width:2px;border-radius:5px;
  padding:0 5px;font-size:11px;font-family:ui-monospace,Consolas,monospace}
.foot{color:var(--dim);font-size:11px;text-align:center;padding:10px 0 0}

/* ===== toast / 模态 / 忙碌 ===== */
.toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:60;max-width:380px}
.toast{background:var(--card);border:1px solid var(--line2);border-left:3px solid var(--acc);border-radius:9px;
  padding:9px 12px;font-size:12.5px;box-shadow:var(--shadow);animation:in .18s ease-out}
.toast.ok{border-left-color:var(--ok)}.toast.err{border-left-color:var(--danger)}
.toast.warn{border-left-color:var(--warn)}
.toast .t{font-weight:600;margin-bottom:2px}
.toast .d{color:var(--dim);font-size:11.5px;word-break:break-word}
@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.backdrop{position:fixed;inset:0;background:rgba(6,9,14,.62);display:grid;place-items:center;z-index:70;padding:20px}
.backdrop[hidden]{display:none}
.modal{background:var(--card);border:1px solid var(--line2);border-radius:14px;padding:18px;max-width:460px;width:100%;
  box-shadow:var(--shadow)}
.modal h3{margin:0 0 8px;font-size:14px}
.modal p{margin:0;color:var(--fg2);font-size:12.6px;white-space:pre-wrap}
.modal .ma{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.busy{position:fixed;top:0;left:0;right:0;height:2px;z-index:80;overflow:hidden}
.busy[hidden]{display:none}
.busy i{display:block;height:100%;width:35%;background:var(--acc);animation:slide 1s ease-in-out infinite}
.sk{height:14px;border-radius:6px;background:linear-gradient(90deg,var(--card2),var(--line),var(--card2));
  background-size:200% 100%;animation:sh 1.1s linear infinite}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ===== 窄屏 ===== */
@media (max-width:720px){
  .app{padding:0 10px 30px}
  .chips{order:3;width:100%;margin-left:0}
  .grid,.grid.wide{grid-template-columns:1fr}
  .tabs{overflow-x:auto}
  .card-body{padding:12px}
  ul.list li .sz{display:none}
  .top{padding:10px 0}
}





</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand">
      <span class="logo">115</span>
      <div>
        <h1>strm 生成面板</h1>
        <p class="sub">只写本地指针 · 交给 MDCng 刮削 · 进 Emby</p>
      </div>
    </div>
    <div class="chips" id="chips">
      <span class="chip" id="chipHost" title="115 直链地址（点一下复制）"><span class="dot"></span>…</span>
      <span class="chip" id="chipSched" title="定时任务 / 清单条数">…</span>
      <span class="chip" id="chipRun" title="任务状态"><span class="dot"></span>空闲</span>
    </div>
    <button class="icon-btn" id="btnTheme" onclick="toggleTheme()" title="切换深浅色（D）">◐</button>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab on" data-p="gen" role="tab" aria-selected="true">① 生成 strm</button>
    <button class="tab" data-p="sched" role="tab" aria-selected="false">② 定时任务<span class="n" id="nSched">0</span></button>
    <button class="tab" data-p="fix" role="tab" aria-selected="false">③ 地址修正</button>
    <button class="tab" data-p="misc" role="tab" aria-selected="false">④ 清单 &amp; 清理</button>
    <span class="spacer"></span>
    <button class="ghost mini" onclick="toggleHelp()" title="快捷键与说明">? 帮助</button>
  </nav>

  <details class="help" id="help" hidden>
    <summary>快捷键与说明</summary>
    <table>
      <tr><td style="width:120px">点击目录名</td><td>进入该目录；点击文件名 = 勾选/取消</td></tr>
      <tr><td><kbd>Backspace</kbd> / <kbd>Alt</kbd>+<kbd>←</kbd></td><td>回到上一级目录</td></tr>
      <tr><td><kbd>/</kbd></td><td>聚焦过滤框</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>清空已勾选</td></tr>
      <tr><td><kbd>D</kbd></td><td>切换深浅色</td></tr>
      <tr><td>不勾选直接点「生成 strm」</td><td>默认处理整个当前目录（递归按「下钻」设置）</td></tr>
      <tr><td>增量</td><td>已存在同名 strm 的会自动跳过，重复点不会重复写</td></tr>
    </table>
  </details>

  <main>

  <section id="p-gen" class="sec on">
    <div class="card">
      <div class="card-head">
        <h2>生成设置</h2>
        <span class="hint">参数会记住，刷新后仍在</span>
      </div>
      <div class="card-body">
        <fieldset class="fieldset">
          <legend>来源与目标</legend>
          <div class="grid">
            <div class="field">
              <label for="root">来源</label>
              <select id="root">
                <option value="pipeline">流水线（待看/sehuatang_&lt;分类&gt;）</option>
                <option value="self">自存（待看/自存 · 与 sehuatang 分开）</option>
              </select>
            </div>
            <div class="field">
              <label for="cat">分类</label>
              <select id="cat"></select>
            </div>
            <div class="field" id="layoutWrap" style="display:none">
              <label for="layout">自存落点布局</label>
              <select id="layout">
                <option value="auto">按 115 目录名分层（每个勾选项一个文件夹）</option>
                <option value="flat">全部放进下面指定的目录</option>
              </select>
            </div>
            <div class="field">
              <label for="name" id="nameLbl">本地目录名</label>
              <input type="text" id="name" placeholder="留空 = 用 115 目录名">
            </div>
            <div class="field">
              <label for="depth">下钻</label>
              <select id="depth">
                <option value="1">只处理本层文件（不递归）</option>
                <option value="0">递归全部子目录</option>
                <option value="2">递归 1 层</option>
              </select>
            </div>
            <div class="field">
              <label for="minmb">最小体积（MB）</label>
              <input type="text" id="minmb" value="0" placeholder="0 = 不过滤">
              <span class="tip">小于这个体积的视频不生成（过滤预告片、样片）</span>
            </div>
          </div>
        </fieldset>

        <fieldset class="fieldset">
          <legend>过滤与地址</legend>
          <div class="grid wide">
            <div class="field">
              <label for="exts">视频扩展名</label>
              <input type="text" id="exts" value="__EXTS__" spellcheck="false">
              <span class="tip">对应 115-Desktop「视频文件后缀」，逗号分隔；留空 = 默认集</span>
            </div>
            <div class="field">
              <label for="urlhost">strm 里的地址</label>
              <select id="urlhost">
                <option value="current">局域网（当前 __HOST__）</option>
                <option value="localhost">本机 127.0.0.1:11501（只有本机能播）</option>
                <option value="custom">自定义…</option>
              </select>
              <input type="text" id="hostcustom" placeholder="192.168.1.200 或 host:11501"
                     spellcheck="false" style="display:none">
            </div>
            <div class="field">
              <label for="metaexts">元数据后缀（默认关）</label>
              <input type="text" id="metaexts" placeholder="nfo,jpg,srt" spellcheck="false">
              <span class="tip">填了就从 115 拉同名元数据；MDCng 自己会写 nfo/海报，开了会互相覆盖</span>
            </div>
            <div class="field">
              <label>文件命名</label>
              <label class="switch" for="appendpc">
                <input type="checkbox" id="appendpc">
                <span class="track"></span>
                <span>文件名追加提取码</span>
              </label>
              <span class="tip">原名_&lt;pickcode&gt;.strm，同名不同片不会互相覆盖</span>
            </div>
          </div>
        </fieldset>

        <div class="actions">
          <button class="primary" id="btnRun" onclick="go()">生成 strm</button>
          <button onclick="preview()">预览（只读）</button>
          <button class="ghost" id="btnUp" onclick="up()">⬆ 上一级</button>
          <button class="ghost" onclick="load(cur, true)">刷新目录</button>
          <button class="ghost" onclick="resetSettings()">恢复默认</button>
          <span class="hint" id="selinfo">未选择</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>目录浏览</h2>
        <span class="hint" id="listinfo"></span>
      </div>
      <div class="card-body">
        <div class="crumb" id="crumb"></div>
        <div class="dest" id="rootinfo"></div>
        <div class="rowbar">
          <input type="text" id="filter" placeholder="过滤当前目录（输入即筛）" style="max-width:300px"
                 oninput="renderList()" spellcheck="false">
          <label class="switch" for="onlyNew" title="只留下还没生成 strm 的视频">
            <input type="checkbox" id="onlyNew" onchange="renderList()">
            <span class="track"></span><span>只看未生成</span>
          </label>
          <label class="hint" for="sortBy">排序</label>
          <select id="sortBy" onchange="renderList()" style="width:auto">
            <option value="dir">目录优先</option>
            <option value="name">按名称</option>
            <option value="size">按体积（大→小）</option>
          </select>
          <span class="hint" style="margin-left:auto" id="liststat"></span>
        </div>
        <div class="list-head">
          <span style="width:15px"></span>
          <span style="flex:1">名称</span>
          <span style="width:120px">状态</span>
          <span style="width:74px;text-align:right">大小</span>
        </div>
        <ul class="list" id="list"></ul>
        <div class="list-foot">
          <span class="hint" id="selinfo2">未勾选任何项 → 生成时默认处理整个当前目录</span>
          <span class="grow"></span>
          <button class="ghost mini" onclick="selAll(true)">全选本层</button>
          <button class="ghost mini" onclick="selOnlyNew()">只选未生成</button>
          <button class="ghost mini" onclick="selAll(false)">清空</button>
        </div>
      </div>
    </div>

    <div class="card" id="planCard" style="display:none">
      <div class="card-head">
        <h2>预览</h2>
        <span class="hint" id="planSum"></span>
        <button class="ghost mini" onclick="hide('planCard')">收起</button>
      </div>
      <div class="card-body" style="padding-top:10px">
        <div class="stats" id="planStats"></div>
        <div class="tbl-wrap" style="margin-top:12px;max-height:40vh"><table id="planTbl"></table></div>
      </div>
    </div>

    <div class="card" id="logCard" style="display:none">
      <div class="card-head">
        <h2>执行日志</h2>
        <span class="badge" id="state">运行中</span>
        <span class="bar" id="barWrap" style="max-width:220px"><i id="bar"></i></span>
        <span class="hint" id="barText"></span>
        <span class="hint" id="ctlinfo"></span>
        <button class="ghost mini" id="btnPause" onclick="ctl('pause')">⏸ 暂停</button>
        <button class="ghost mini" id="btnResume" onclick="ctl('resume')">▶ 继续</button>
        <button class="ghost mini" onclick="ctl('cancel')">■ 停止</button>
        <button class="ghost mini" onclick="hide('logCard')">收起</button>
      </div>
      <div class="card-body" style="padding-top:0">
        <pre id="log" aria-live="polite"></pre>
      </div>
    </div>
  </section>

  <section id="p-sched" class="sec">
    <div class="card">
      <div class="card-head">
        <h2>新建 / 编辑定时任务</h2>
        <span class="hint">到点自动增量生成 · 已存在的 strm 自动跳过</span>
      </div>
      <div class="card-body">
        <div class="grid">
          <div class="field">
            <label for="scTitle">标题</label>
            <input type="text" id="scTitle" placeholder="例如：佐山爱 追更">
          </div>
          <div class="field" style="grid-column:span 2">
            <label for="scSrc">115 源目录（可多个，逗号分隔）</label>
            <input type="text" id="scSrc" placeholder="/AV/女优合集/xxx/单体" spellcheck="false">
          </div>
          <div class="field">
            <label for="scRoot">来源</label>
            <select id="scRoot">
              <option value="self">自存（待看/自存）</option>
              <option value="pipeline">流水线</option>
            </select>
          </div>
          <div class="field">
            <label for="scCat">分类</label>
            <select id="scCat"></select>
          </div>
          <div class="field">
            <label for="scName">落点目录名</label>
            <input type="text" id="scName" placeholder="留空 = 用 115 目录名">
          </div>
          <div class="field">
            <label for="scLayout">布局</label>
            <select id="scLayout">
              <option value="auto">按 115 目录名分层</option>
              <option value="flat">全部放进同一目录</option>
            </select>
          </div>
          <div class="field">
            <label for="scIv">执行间隔</label>
            <div style="display:flex;gap:8px">
              <input type="text" id="scIv" value="1" style="width:70px">
              <select id="scUnit">
                <option value="minutes">分钟</option>
                <option value="hours">小时</option>
                <option value="days" selected>天</option>
              </select>
            </div>
          </div>
        </div>
        <div class="actions">
          <button class="primary" onclick="saveSched()">保存任务</button>
          <button class="ghost" onclick="clearSchedForm()">清空表单</button>
          <input type="hidden" id="scId">
          <span class="hint">体积 / 扩展名 / 追加提取码 / strm 地址 / 元数据 沿用「① 生成 strm」页当前设置</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>任务列表</h2>
        <span class="hint" id="schedInfo"></span>
        <button class="ghost mini" onclick="loadSched()">刷新</button>
      </div>
      <div class="card-body" style="padding:12px 14px">
        <div class="tbl-wrap"><table id="schedTbl"></table></div>
      </div>
    </div>

    <div class="card" id="runCard" style="display:none">
      <div class="card-head">
        <h2>最近任务</h2>
        <span class="hint">手动生成与定时任务都在这</span>
        <button class="ghost mini" onclick="loadRuns()">刷新</button>
      </div>
      <div class="card-body" style="padding:12px 14px">
        <div class="tbl-wrap"><table id="runTbl"></table></div>
      </div>
    </div>
  </section>

  <section id="p-fix" class="sec">
    <div class="card">
      <div class="card-head">
        <h2>strm 地址修正</h2>
        <span class="hint">对应 115-Desktop「STRM 地址修正」</span>
      </div>
      <div class="card-body">
        <div class="grid wide">
          <div class="field" style="grid-column:span 2">
            <label for="fxDir">要扫描的本地目录</label>
            <input type="text" id="fxDir" value="/mnt/g/srtm/待看/自存" spellcheck="false">
          </div>
          <div class="field">
            <label for="fxHost">新 host</label>
            <input type="text" id="fxHost" placeholder="192.168.2.238" spellcheck="false">
            <span class="tip">只填 host（域名/IP/主机名），<b>端口保持不变</b></span>
          </div>
        </div>
        <div class="actions">
          <button onclick="fxScan()">扫描现有地址</button>
          <button class="ghost" onclick="fxFix(true)">试算（不改）</button>
          <button class="primary" onclick="fxFix(false)">开始修正</button>
          <span class="hint">写入用临时文件 + 原子替换，中断也不会写坏</span>
        </div>
      </div>
    </div>
    <div class="card" id="fxCard" style="display:none">
      <div class="card-head">
        <h2>扫描结果</h2>
        <span class="hint" id="fxInfo"></span>
        <button class="ghost mini" onclick="hide('fxCard')">收起</button>
      </div>
      <div class="card-body" style="padding:12px 14px">
        <div class="tbl-wrap"><table id="fxTbl"></table></div>
        <pre id="fxLog" style="display:none"></pre>
      </div>
    </div>
  </section>

  <section id="p-misc" class="sec">
    <div class="card">
      <div class="card-head">
        <h2>strm 清单</h2>
        <span class="hint">增量更新与「清理已删除」的依据</span>
        <button class="ghost mini" onclick="loadManifest()">刷新</button>
      </div>
      <div class="card-body">
        <div class="stats" id="mfStats"></div>
        <div class="tbl-wrap" style="margin-top:12px"><table id="mfTbl"></table></div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>清理已删除</h2>
        <span class="hint">115 上已消失的源文件 → 删掉对应的本地 strm</span>
      </div>
      <div class="card-body">
        <div class="rowbar">
          <label class="hint" for="clDirs">最多核对目录数</label>
          <input type="text" id="clDirs" value="30" style="width:80px">
          <button class="ghost" onclick="doClean(true)">试算（不改）</button>
          <button class="danger" onclick="doClean(false)">执行清理</button>
          <span class="hint">只删面板自己记录过的 strm，不动 115 上的任何文件</span>
        </div>
        <pre id="clLog" style="display:none"></pre>
        <div class="tbl-wrap" style="margin-top:12px"><table id="clTbl"></table></div>
      </div>
    </div>
  </section>

  </main>
  <div class="foot">本地面板 · 只写 .strm 指针 · 生成后由 MDCng watcher 刮削并进入 Emby</div>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>
<div class="backdrop" id="modal" hidden>
  <div class="modal" role="dialog" aria-modal="true">
    <h3 id="modalTitle">确认</h3>
    <p id="modalBody"></p>
    <div class="ma">
      <button class="ghost" onclick="modalClose(false)">取消</button>
      <button class="danger" id="modalOk" onclick="modalClose(true)">确定</button>
    </div>
  </div>
</div>
<div class="busy" id="busy" hidden><i></i></div>



<script>
/* ========================= 基础工具 ========================= */
const $ = id => document.getElementById(id);
const CATS = __CATS__;
const DEF_HOST = '__HOST__';
const DEF_EXTS = '__EXTS__';
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, m => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[m]));
const gb = n => (n ? (n / 1073741824).toFixed(2) + ' GB' : '');
const clamp = (n, a, b) => Math.max(a, Math.min(b, n));

function toast(title, detail, kind) {
  const box = $('toasts');
  const d = document.createElement('div');
  d.className = 'toast ' + (kind || '');
  d.innerHTML = '<div class="t">' + esc(title) + '</div>' + (detail ? '<div class="d">' + esc(detail) + '</div>' : '');
  box.appendChild(d);
  const life = kind === 'err' ? 9000 : 4200;
  d.onclick = () => d.remove();
  setTimeout(() => { d.style.opacity = '0'; setTimeout(() => d.remove(), 250); }, life);
}
function busy(on) { $('busy').hidden = !on; }
function hide(id) { $(id).style.display = 'none'; }

let _modalResolve = null;
function askConfirm(title, body, okText) {
  $('modalTitle').textContent = title;
  $('modalBody').textContent = body || '';
  $('modalOk').textContent = okText || '确定';
  $('modal').hidden = false;
  return new Promise(res => { _modalResolve = res; });
}
function modalClose(v) {
  $('modal').hidden = true;
  if (_modalResolve) { _modalResolve(v); _modalResolve = null; }
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('modal').hidden) modalClose(false); });

function copyText(t, label) {
  const done = () => toast('已复制', label ? String(label).slice(0, 60) : String(t).slice(0, 60), 'ok');
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(done, () => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const ta = document.createElement('textarea');
  ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败', String(e), 'err'); }
  ta.remove();
}
function toggleHelp() { const h = $('help'); h.hidden = !h.hidden; }
function toggleTheme() {
  const root = document.documentElement;
  const next = root.dataset.theme === 'light' ? 'dark' : 'light';
  root.dataset.theme = next;
  try { localStorage.setItem('strmPanel.theme', next); } catch (e) {}
}
(function initTheme() {
  let t = null;
  try { t = localStorage.getItem('strmPanel.theme'); } catch (e) {}
  if (!t) t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
})();

/* ========================= 设置记忆 / 标签页 / 状态条 ========================= */
const SET_IDS = ['root', 'cat', 'layout', 'name', 'depth', 'minmb', 'exts', 'urlhost', 'hostcustom', 'metaexts', 'onlyNew', 'sortBy', 'fxDir', 'clDirs'];
function saveSettings() {
  const o = {};
  SET_IDS.forEach(id => { const el = $(id); if (!el) return; o[id] = el.type === 'checkbox' ? el.checked : el.value; });
  try { localStorage.setItem('strmPanel.form', JSON.stringify(o)); } catch (e) {}
}
function loadSettings() {
  let o = null;
  try { o = JSON.parse(localStorage.getItem('strmPanel.form') || 'null'); } catch (e) {}
  if (!o) return;
  SET_IDS.forEach(id => {
    const el = $(id); if (!el || o[id] === undefined) return;
    if (el.type === 'checkbox') el.checked = !!o[id];
    else if (id === 'cat' && !CATS.some(c => c[0] === o[id])) return;
    else el.value = o[id];
  });
}
function resetSettings() {
  try { localStorage.removeItem('strmPanel.form'); } catch (e) {}
  location.reload();
}
function bindPersist() {
  ['p-gen', 'p-fix', 'p-misc'].forEach(sec => {
    const el = $(sec); if (!el) return;
    el.addEventListener('change', saveSettings);
    el.addEventListener('input', e => { if (e.target.tagName === 'SELECT' || e.target.type === 'checkbox') saveSettings(); });
  });
}

let curTab = 'gen';
function activate(p) {
  curTab = p;
  document.querySelectorAll('.tabs .tab').forEach(b => {
    const on = b.dataset.p === p;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('main .sec').forEach(s => s.classList.toggle('on', s.id === 'p-' + p));
  try { localStorage.setItem('strmPanel.tab', p); } catch (e) {}
  if (p === 'sched') { loadSched(); loadRuns(); }
  if (p === 'misc') loadManifest();
}

let _statusTimer = null;
function statusTick() {
  fetch('/api/status').then(r => r.json()).then(s => {
    if (s.error) return;
    const h = $('chipHost');
    h.innerHTML = '<span class="dot"></span>115 直链 <b>' + esc(s.host) + '</b>';
    h.title = '点击复制：' + s.host;
    h.onclick = () => copyText('http://' + s.host, s.host);
    $('chipSched').innerHTML = '定时 <b>' + s.schedules + '</b> · 清单 <b>' + s.manifest + '</b>'
      + (s.strm_missing ? ' · <span class="dot warn"></span>丢失 ' + s.strm_missing : '');
    $('nSched').textContent = s.schedules;
    const r = s.run;
    const c = $('chipRun');
    if (!r) { c.className = 'chip'; c.innerHTML = '<span class="dot ok"></span>空闲'; }
    else if (r.state === 'running' && !r.pause) {
      c.className = 'chip run';
      c.innerHTML = '<span class="dot run"></span>运行中 ' + (r.total ? r.written + '/' + r.total : r.written + ' 个')
        + (r.title ? ' · ' + esc(String(r.title).slice(0, 18)) : '');
    } else if (r.pause) { c.className = 'chip warn'; c.innerHTML = '<span class="dot warn"></span>已暂停 ' + r.written + ' 个'; }
    else {
      const bad = r.state !== 'done';
      c.className = 'chip' + (bad ? ' warn' : '');
      c.innerHTML = '<span class="dot ' + (bad ? 'warn' : 'ok') + '"></span>' + (bad ? '上次出错' : '空闲')
        + ' · 最近写入 ' + r.written + ' 个';
    }
  }).catch(() => {});
}
function startStatus() { statusTick(); clearInterval(_statusTimer); _statusTimer = setInterval(statusTick, 6000); }

/* ========================= ① 生成 strm ========================= */
let cur = '', sel = new Set(), entries = [], view = [], reqSeq = 0, taskId = null, poll = null;
const MAXR = 400;

function urlHost() { return $('urlhost').value === 'custom' ? ($('hostcustom').value.trim() || 'current') : $('urlhost').value; }
function spec() {
  return {category: $('cat').value, name: $('name').value.trim(), source: $('root').value,
          layout: $('layout').value, max_depth: parseInt($('depth').value, 10),
          min_size_mb: parseInt($('minmb').value || '0', 10) || 0,
          exts: $('exts').value.trim(), append_pickcode: $('appendpc').checked,
          url_host: urlHost(), meta_exts: $('metaexts').value.trim()};
}
function syncLabels() {
  const self = $('root').value === 'self', auto = $('layout').value === 'auto';
  $('layoutWrap').style.display = self ? '' : 'none';
  $('nameLbl').textContent = !self ? '本地目录名' : (auto ? '前缀层（可选）' : '落点目录名');
  $('name').placeholder = !self ? '留空 = 用 115 目录名'
    : (auto ? '留空 = 直接用 115 目录名 · 填「橘玛丽」= 待看/自存/橘玛丽/<115目录名>'
            : '落点 待看/自存/<这个名字>（可带 / 分层）');
  $('hostcustom').style.display = $('urlhost').value === 'custom' ? '' : 'none';
}
$('root').onchange = () => { $('cat').disabled = $('root').value === 'self'; syncLabels(); load(cur); };
$('layout').onchange = () => { syncLabels(); load(cur, true); };
$('urlhost').onchange = syncLabels;

function targets() {
  const t = [];
  entries.forEach(e => { if (sel.has(e.path)) t.push({path: e.path, is_dir: e.is_dir}); });
  if (!t.length && cur) t.push({path: cur, is_dir: true});
  return t;
}
function info() {
  const txt = sel.size ? '已勾选 ' + sel.size + ' 项（点上面的按钮开始）'
    : (cur ? '未勾选 → 生成时默认处理整个当前目录' : '未选择任何目录');
  $('selinfo').textContent = txt;
  $('selinfo2').textContent = txt;
}

async function load(path, nocache) {
  cur = path || '';
  $('btnUp').disabled = !cur;
  const my = ++reqSeq;
  $('list').innerHTML = '<li><div class="sk" style="width:60%"></div></li><li><div class="sk" style="width:40%"></div></li><li><div class="sk" style="width:50%"></div></li>';
  $('listinfo').textContent = '加载中…';
  $('liststat').textContent = '';
  busy(true);
  const url = '/api/list?path=' + encodeURIComponent(cur) + '&category=' + $('cat').value
    + '&source=' + $('root').value + '&layout=' + $('layout').value
    + '&name=' + encodeURIComponent($('name').value.trim()) + '&max_depth=' + $('depth').value
    + (nocache ? '&nocache=1' : '');
  let r = {};
  try { r = await (await fetch(url)).json(); }
  catch (e) { busy(false); $('list').innerHTML = ''; toast('读取失败', String(e), 'err'); return; }
  busy(false);
  if (my !== reqSeq) return;                     /* 连点目录时只认最后一次响应 */
  if (r.error) { $('list').innerHTML = '<li class="empty">' + esc(r.error) + '</li>'; $('listinfo').textContent = '出错'; return; }
  entries = r.entries || [];
  renderCrumb(r);
  renderList();
  info();
  $('listinfo').textContent = (r.cached ? '缓存命中 · ' : (r.ms ? r.ms + 'ms · ' : '')) + '共 ' + entries.length + ' 项';
}
function renderCrumb(r) {
  const c = $('crumb'); c.innerHTML = '';
  const mk = (label, path) => { const b = document.createElement('button');
    b.className = 'seg'; b.textContent = label; b.onclick = () => load(path); return b; };
  const sep = () => { const s = document.createElement('span'); s.className = 'sep'; s.textContent = '/'; return s; };
  c.appendChild(mk('115 根', ''));
  const segs = (cur || '').split('/').filter(Boolean); let acc = '';
  segs.forEach((s, i) => { acc += '/' + s; c.appendChild(sep());
    if (i === segs.length - 1) { const t = document.createElement('span'); t.className = 'cur'; t.textContent = s; c.appendChild(t); }
    else c.appendChild(mk(s, acc)); });
  const d = $('rootinfo');
  d.innerHTML = '<span class="k">115</span><span class="v mono">' + esc(cur || '/') + '</span>'
    + '<span class="sep">→</span><span class="k">本地落点</span>'
    + '<span class="v mono">' + esc((r && r.root) || '—') + '</span>';
  const cp = document.createElement('button');
  cp.className = 'link'; cp.textContent = '复制落点';
  cp.onclick = () => copyText((r && r.root) || '', '本地落点');
  d.appendChild(cp);
  if (r && r.origin) {
    const b = document.createElement('span');
    b.className = 'badge ok';
    b.title = '这个落点目录的内容来自 115：' + r.origin.origin;
    b.textContent = '来源 ' + (r.origin.origin.split('/').filter(Boolean).pop() || '根') + ' · ' + r.origin.count;
    d.appendChild(b);
  }
}

/* ---- 列表渲染 / 勾选 ---- */
function renderList() {
  const q = ($('filter').value || '').trim().toLowerCase();
  const onlyNew = $('onlyNew').checked;
  const sort = $('sortBy').value;
  let rows = entries.slice();
  if (q) rows = rows.filter(e => e.name.toLowerCase().includes(q));
  let hidden = 0;
  if (onlyNew) {
    const before = rows.length;
    rows = rows.filter(e => !e.is_dir && !e.have_strm);
    hidden = before - rows.length;
  }
  const cmp = {
    dir: (a, b) => (a.is_dir !== b.is_dir) ? (a.is_dir ? -1 : 1) : a.name.localeCompare(b.name, 'zh'),
    name: (a, b) => a.name.localeCompare(b.name, 'zh'),
    size: (a, b) => (b.size || 0) - (a.size || 0) || a.name.localeCompare(b.name, 'zh')
  }[sort];
  if (cmp) rows.sort(cmp);
  view = rows.slice(0, MAXR);
  const ul = $('list'); ul.innerHTML = '';
  view.forEach(e => ul.appendChild(rowEl(e)));
  if (!view.length) {
    ul.innerHTML = '<li class="empty"><span class="big">∅</span>'
      + (q ? '没有匹配「' + esc(q) + '」的项' : (onlyNew ? '本层没有未生成的视频' : '空目录')) + '</li>';
  }
  const vids = entries.filter(e => e.video);
  $('liststat').textContent = '本层 ' + entries.length + ' 项 · 视频 ' + vids.length
    + ' · 已有 strm ' + vids.filter(e => e.have_strm).length
    + (hidden ? ' · 折叠 ' + hidden : '')
    + (entries.length > MAXR ? ' · 仅显示前 ' + MAXR + ' 项' : '');
}
function rowEl(e) {
  const li = document.createElement('li');
  const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = sel.has(e.path);
  if (!e.is_dir && !e.video) cb.disabled = true;
  cb.onclick = ev => { ev.stopPropagation(); toggle(e.path, cb.checked); };
  const ic = document.createElement('span'); ic.className = 'ic';
  ic.textContent = e.is_dir ? '📁' : (e.video ? '🎬' : '📄');
  const nm = document.createElement('span'); nm.className = 'nm ' + (e.is_dir ? 'dir' : 'file');
  nm.textContent = e.name; nm.title = e.path;
  nm.onclick = () => { if (e.is_dir) load(e.path); else { cb.checked = !cb.checked; toggle(e.path, cb.checked); } };
  li.appendChild(cb); li.appendChild(ic); li.appendChild(nm);
  const tags = document.createElement('span');
  tags.style.cssText = 'display:flex;gap:4px;flex:0 0 auto;min-width:118px;justify-content:flex-end';
  if (e.have_strm) { const b = document.createElement('span'); b.className = 'badge ok'; b.textContent = '已有 strm'; tags.appendChild(b); }
  if (e.self_origin) {
    const b = document.createElement('span'); b.className = 'badge';
    b.textContent = '来自 ' + (e.self_origin.split('/').filter(Boolean).pop() || '根');
    b.title = '115: ' + e.self_origin + (e.self_count ? '（' + e.self_count + ' 个视频）' : '');
    tags.appendChild(b);
  }
  li.appendChild(tags);
  const sz = document.createElement('span'); sz.className = 'sz'; sz.textContent = gb(e.size);
  li.appendChild(sz);
  const act = document.createElement('span'); act.className = 'rowact';
  const cpb = document.createElement('button'); cpb.className = 'link'; cpb.textContent = '复制路径';
  cpb.onclick = ev => { ev.stopPropagation(); copyText(e.path, e.name); };
  act.appendChild(cpb); li.appendChild(act);
  return li;
}
function toggle(p, on) { on ? sel.add(p) : sel.delete(p); info(); }
function selAll(on) {
  sel.clear();
  if (on) view.forEach(e => { if (e.is_dir || e.video) sel.add(e.path); });
  document.querySelectorAll('#list li input[type=checkbox]').forEach(cb => { if (!cb.disabled) cb.checked = on; });
  info();
}
function selOnlyNew() {
  sel.clear();
  view.forEach(e => { if (e.video && !e.have_strm) sel.add(e.path); });
  document.querySelectorAll('#list li input[type=checkbox]').forEach((cb, i) => {
    if (cb.disabled) return; const e = view[i]; cb.checked = !!(e && e.video && !e.have_strm);
  });
  info();
}
function up() {
  const segs = (cur || '').split('/').filter(Boolean);
  if (!segs.length) return;
  segs.pop();
  load(segs.length ? '/' + segs.join('/') : '');
}

/* ---- 预览 / 执行 ---- */
function tile(k, v, sub) {
  return '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v)
    + (sub ? '<small>' + esc(sub) + '</small>' : '') + '</div></div>';
}
async function preview() {
  busy(true);
  let r = {};
  try { r = await (await fetch('/api/plan', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(spec(), {targets: targets()}))})).json(); }
  catch (e) { busy(false); toast('预览失败', String(e), 'err'); return; }
  busy(false);
  if (r.error) { toast('预览失败', r.error, 'err'); return; }
  $('planCard').style.display = 'block';
  const todo = r.items.filter(i => !i.exists).length;
  $('planStats').innerHTML = tile('视频', r.items.length, '个')
    + tile('合计体积', r.total_gb.toFixed(2), 'GB')
    + tile('需生成', todo, '已存在 ' + (r.items.length - todo))
    + tile('落点', r.category_name, r.watch_root)
    + tile('刮削产物', r.target_root.split('/').slice(-2).join('/'), '');
  $('planSum').textContent = '预览 ' + new Date().toLocaleTimeString();
  let h = '<thead><tr><th style="width:40%">115 源</th><th>本地 strm 落点</th><th style="width:90px">大小</th></tr></thead><tbody>';
  r.items.slice(0, 300).forEach(i => {
    h += '<tr><td class="dst" title="' + esc(i.src) + '">' + esc(i.src) + '</td>'
      + '<td class="dst">' + esc(i.dst) + '</td>'
      + '<td class="num">' + gb(i.size) + (i.exists ? ' <span class="badge ok">已有</span>' : '') + '</td></tr>';
  });
  if (r.items.length > 300) h += '<tr><td colspan="3" class="hint">… 仅显示前 300 条</td></tr>';
  (r.skipped || []).forEach(s => {
    h += '<tr><td class="dst">' + esc(s.path) + '</td><td colspan="2" class="hint">跳过：' + esc(s.why) + '</td></tr>';
  });
  $('planTbl').innerHTML = h + '</tbody>';
  $('logCard').style.display = 'none';
}
async function go() {
  const ok = await askConfirm('确认生成 strm？',
    '会写入本地 .strm 并触发 MDCng 刮削 + Emby 刷新。\n不会修改 115 网盘上的任何文件。',
    '开始生成');
  if (!ok) return;
  $('btnRun').disabled = true;
  $('logCard').style.display = 'block';
  $('log').textContent = '提交中…';
  $('state').className = 'badge run'; $('state').textContent = '运行中';
  busy(true);
  let r = {};
  try { r = await (await fetch('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(spec(), {targets: targets(), wait: 600, refresh: true}))})).json(); }
  catch (e) { busy(false); $('btnRun').disabled = false; toast('提交失败', String(e), 'err'); return; }
  busy(false);
  if (r.error) { $('btnRun').disabled = false; toast('提交失败', r.error, 'err'); return; }
  taskId = r.task_id;
  toast('任务已开始', '任务号 ' + taskId);
  clearInterval(poll);
  poll = setInterval(() => pollTask(taskId), 2000);
  pollTask(taskId);
}
async function pollTask(id) {
  let t = {};
  try { t = await (await fetch('/api/task?id=' + id)).json(); } catch (e) { return; }
  if (t.error) { clearInterval(poll); $('btnRun').disabled = false; return; }
  $('log').textContent = (t.log || []).join('\n');
  $('log').scrollTop = $('log').scrollHeight;
  const total = (t.plan && t.plan.items) ? t.plan.items.length : 0;
  const w = t.written || 0;
  $('barWrap').classList.toggle('indet', t.state === 'running' && !total);
  $('bar').style.width = total ? clamp(Math.round(w / total * 100), 0, 100) + '%' : (t.state === 'running' ? '' : '100%');
  $('barText').textContent = total ? w + ' / ' + total : (w ? w + ' 个' : '');
  const map = {running: ['运行中', 'run'], done: ['完成', 'ok'], error: ['出错', 'err']};
  const m = map[t.state] || ['未知', 'off'];
  $('state').className = 'badge ' + m[1]; $('state').textContent = m[0];
  $('ctlinfo').textContent = t.pause ? '已暂停（点「继续」恢复）' : (t.cancel ? '停止中…' : '');
  $('btnPause').disabled = !!t.pause || t.state !== 'running';
  $('btnResume').disabled = !t.pause;
  if (t.state !== 'running') {
    clearInterval(poll);
    $('bar').style.width = '100%';
    $('btnRun').disabled = false;
    toast(m[0], '写入 ' + w + ' 个 strm', t.state === 'done' ? 'ok' : 'err');
    statusTick();
    load(cur);
  }
}
async function ctl(action) {
  if (!taskId) return;
  await fetch('/api/task_ctl', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: taskId, action})});
  $('ctlinfo').textContent = action === 'pause' ? '已暂停' : (action === 'cancel' ? '停止中…' : '继续中…');
  pollTask(taskId);
}

/* ========================= ② 定时任务 ========================= */
function cd(s) {                                   /* "09-24 22:58" -> "2 小时后" */
  if (!s) return '';
  const m = String(s).match(/^(\d\d)-(\d\d)\s+(\d\d):(\d\d)$/);
  if (!m) return s;
  const now = new Date();
  let t = new Date(now.getFullYear(), +m[1] - 1, +m[2], +m[3], +m[4]);
  if (t - now < -20 * 86400000) t = new Date(now.getFullYear() + 1, +m[1] - 1, +m[2], +m[3], +m[4]);
  let d = Math.round((t - now) / 1000);
  if (d < 0) return '已到期';
  if (d < 60) return d + ' 秒后';
  if (d < 3600) return Math.round(d / 60) + ' 分钟后';
  if (d < 86400) return (d / 3600).toFixed(1) + ' 小时后';
  return Math.round(d / 86400) + ' 天后';
}
async function loadSched() {
  let r = {};
  try { r = await (await fetch('/api/schedules')).json(); } catch (e) { return; }
  const ts = r.tasks || [];
  window._sched = ts;
  $('nSched').textContent = ts.length;
  $('schedInfo').textContent = ts.length + ' 个任务 · 启用 ' + ts.filter(t => t.enabled).length
    + ' · 自存根 ' + r.self_root;
  let h = '<thead><tr><th>标题</th><th>115 源</th><th>落点参数</th><th>间隔</th>'
    + '<th>下次运行</th><th>上次运行</th><th>状态</th><th style="width:210px">操作</th></tr></thead><tbody>';
  ts.forEach(t => {
    const sp = t.spec || {};
    const srcs = (sp.targets || []).map(x => x.path).join(' · ');
    const iv = (t.interval_value || 1) + ({minutes: ' 分钟', hours: ' 小时'}[t.interval_unit] || ' 天');
    h += '<tr><td>' + esc(t.title || t.id) + '<div class="hint mono">' + esc(t.id) + '</div></td>'
      + '<td class="dst" title="' + esc(srcs) + '">' + esc(srcs) + '</td>'
      + '<td class="hint">' + esc([sp.source, sp.layout, sp.name || '-'].join(' / ')) + '</td>'
      + '<td>' + esc(iv) + '</td>'
      + '<td>' + esc(cd(t.next_run)) + '<div class="hint">' + esc(t.next_run || '-') + '</div></td>'
      + '<td class="hint">' + esc(t.last_run || '从未') + '</td>'
      + '<td><span class="badge ' + (t.enabled ? 'ok' : 'off') + '">' + (t.enabled ? '启用' : '停用') + '</span>'
      + (t.last_result === 'FAIL' ? ' <span class="badge err">上次失败</span>' : '') + '</td>'
      + '<td><button class="ghost mini" onclick="schedAct(\'run\',\'' + t.id + '\')">立即跑</button> '
      + '<button class="ghost mini" onclick="schedAct(\'toggle\',\'' + t.id + '\',' + (t.enabled ? 'false' : 'true') + ')">'
      + (t.enabled ? '停用' : '启用') + '</button> '
      + '<button class="ghost mini" onclick="schedEdit(\'' + t.id + '\')">编辑</button> '
      + '<button class="ghost mini" onclick="schedAct(\'delete\',\'' + t.id + '\')">删除</button></td></tr>';
  });
  $('schedTbl').innerHTML = h + '</tbody>';
  if (!ts.length) $('schedTbl').innerHTML = '<tbody><tr><td class="empty">还没有定时任务，上面填一下就能建</td></tr></tbody>';
}
function schedEdit(id) {
  const t = (window._sched || []).find(x => x.id === id); if (!t) return;
  const sp = t.spec || {};
  $('scId').value = t.id; $('scTitle').value = t.title || '';
  $('scSrc').value = (sp.targets || []).map(x => x.path).join(', ');
  $('scRoot').value = sp.source || 'self'; $('scCat').value = sp.category || 'av';
  $('scName').value = sp.name || ''; $('scLayout').value = sp.layout || 'flat';
  $('scIv').value = t.interval_value || 1; $('scUnit').value = t.interval_unit || 'days';
  toast('已填入表单', t.title || t.id);
  window.scrollTo({top: 0, behavior: 'smooth'});
}
function clearSchedForm() {
  ['scId', 'scTitle', 'scSrc', 'scName'].forEach(i => $(i).value = '');
  $('scIv').value = '1'; $('scUnit').value = 'days';
}
async function saveSched() {
  const srcs = ($('scSrc').value || '').split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  if (!srcs.length) { toast('还差源目录', '请填 115 源目录路径（可多个，逗号分隔）', 'warn'); return; }
  const g = spec();
  const task = {id: $('scId').value || '', title: $('scTitle').value.trim(),
    interval_value: parseInt($('scIv').value || '1', 10) || 1, interval_unit: $('scUnit').value, enabled: true,
    spec: {targets: srcs.map(p => ({path: p, is_dir: true})), category: $('scCat').value,
           name: $('scName').value.trim(), source: $('scRoot').value, layout: $('scLayout').value,
           max_depth: g.max_depth, min_size_mb: g.min_size_mb, exts: g.exts,
           append_pickcode: g.append_pickcode, url_host: g.url_host, meta_exts: g.meta_exts,
           wait: 600, refresh: true}};
  let r = {};
  try { r = await (await fetch('/api/schedule', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'upsert', task})})).json(); }
  catch (e) { toast('保存失败', String(e), 'err'); return; }
  if (r.error) { toast('保存失败', r.error, 'err'); return; }
  toast('已保存', (r.task && r.task.title) || '定时任务');
  clearSchedForm(); loadSched(); loadRuns(); statusTick();
}
async function schedAct(action, id, enabled) {
  if (action === 'delete') {
    const ok = await askConfirm('删除这个定时任务？', '已经生成的 strm 不受影响。\n任务：' + id, '删除');
    if (!ok) return;
  }
  try {
    await fetch('/api/schedule', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, id, enabled})});
  } catch (e) { toast('操作失败', String(e), 'err'); return; }
  if (action === 'run') toast('已触发', '任务 ' + id + ' 立即执行一次');
  setTimeout(() => { loadSched(); loadRuns(); statusTick(); }, 400);
}
async function loadRuns() {
  let r = {};
  try { r = await (await fetch('/api/tasks')).json(); } catch (e) { return; }
  const ts = r.tasks || [];
  $('runCard').style.display = ts.length ? 'block' : 'none';
  if (!ts.length) { $('runTbl').innerHTML = ''; return; }
  let h = '<thead><tr><th>任务</th><th>状态</th><th style="width:70px">写入</th><th>日志尾部</th><th style="width:80px"></th></tr></thead><tbody>';
  ts.forEach(t => {
    const st = t.state === 'running' ? (t.pause ? ['暂停', 'warn'] : ['运行中', 'run'])
      : (t.state === 'done' ? ['完成', 'ok'] : ['出错', 'err']);
    h += '<tr><td>' + esc(t.title || (t.sched ? '定时 ' + t.sched : t.id))
      + '<div class="hint mono">' + esc(t.id) + '</div></td>'
      + '<td><span class="badge ' + st[1] + '">' + st[0] + '</span></td>'
      + '<td class="num">' + (t.written || 0) + '</td>'
      + '<td class="hint">' + esc((t.log_tail || []).join(' ｜ ')).slice(0, 200) + '</td>'
      + '<td><button class="link" onclick="viewLog(\'' + t.id + '\')">看日志</button></td></tr>';
  });
  $('runTbl').innerHTML = h + '</tbody>';
}
async function viewLog(id) {
  activate('gen');
  taskId = id;
  $('logCard').style.display = 'block';
  $('log').textContent = '加载中…';
  pollTask(id);
}

/* ========================= ③ 地址修正 ========================= */
async function fxScan() {
  const d = $('fxDir').value.trim();
  if (!d) { toast('还差目录', '请填要扫描的本地目录', 'warn'); return; }
  busy(true);
  let r = {};
  try { r = await (await fetch('/api/authorities?dir=' + encodeURIComponent(d))).json(); }
  catch (e) { busy(false); toast('扫描失败', String(e), 'err'); return; }
  busy(false);
  if (r.error) { toast('扫描失败', r.error, 'err'); return; }
  $('fxCard').style.display = 'block'; $('fxLog').style.display = 'none';
  const ks = Object.keys(r.counts || {});
  const total = ks.reduce((a, k) => a + r.counts[k].count, 0);
  $('fxInfo').textContent = ks.length + ' 种地址 · ' + total + ' 个 strm · 当前 ' + r.current;
  window._fx = r;
  let h = '<thead><tr><th>地址（host:port）</th><th style="width:90px">strm 数</th><th>示例文件</th><th style="width:110px"></th></tr></thead><tbody>';
  ks.forEach((k, i) => {
    h += '<tr><td class="mono">' + esc(k) + (k === r.current ? ' <span class="badge ok">当前</span>' : '') + '</td>'
      + '<td class="num">' + r.counts[k].count + '</td>'
      + '<td class="dst">' + esc(r.counts[k].sample) + '</td>'
      + '<td><button class="link" onclick="useAsHostIdx(' + i + ')">用它作为新 host</button></td></tr>';
  });
  $('fxTbl').innerHTML = h + '</tbody>';
  if (!ks.length) $('fxTbl').innerHTML = '<tbody><tr><td class="empty">这个目录里没有 .strm</td></tr></tbody>';
  if (!$('fxHost').value && ks.length) $('fxHost').value = r.current.split(':')[0];
  saveSettings();
}
function useAsHostIdx(i) {
  const ks = Object.keys((window._fx || {}).counts || {});
  if (!ks[i]) return;
  const hh = ks[i].split(':')[0];
  $('fxHost').value = hh;
  toast('已填入新 host', hh);
}

/* ========================= ④ 清单 & 清理 ========================= */
async function loadManifest() {
  let r = {};
  try { r = await (await fetch('/api/manifest')).json(); } catch (e) { return; }
  $('mfStats').innerHTML = tile('清单条数', r.total, '个 strm')
    + tile('覆盖 115 目录', r.src_dirs, '个')
    + tile('本地已丢失', r.strm_missing, r.strm_missing ? '文件被删了' : '都还在');
  let h = '<thead><tr><th>115 源目录</th><th style="width:110px">记录条数</th></tr></thead><tbody>';
  (r.dirs || []).forEach(d => {
    h += '<tr><td class="dst">' + esc(d[0] || '(根)') + '</td><td class="num">' + d[1] + '</td></tr>';
  });
  $('mfTbl').innerHTML = h + '</tbody>';
  if (!r.total) {
    $('mfTbl').innerHTML = '<tbody><tr><td class="empty"><span class="big">∅</span>'
      + '还没有记录 —— 生成一次 strm 后这里就有清单了</td></tr></tbody>';
  }
}
async function doClean(dry) {
  if (!dry) {
    const ok = await askConfirm('确定执行清理？',
      '会删除「115 上已不存在」的 strm 文件。\n只删面板自己记录过的本地 strm，不动 115 上的任何文件。', '执行清理');
    if (!ok) return;
  }
  busy(true);
  let r = {};
  try { r = await (await fetch('/api/cleanup', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dry, max_dirs: parseInt($('clDirs').value || '30', 10) || 30})})).json(); }
  catch (e) { busy(false); toast('清理失败', String(e), 'err'); return; }
  busy(false);
  if (r.error) { toast('清理失败', r.error, 'err'); return; }
  $('clLog').style.display = 'block';
  $('clLog').className = dry ? '' : 'err';
  $('clLog').textContent = (dry ? '[试算] ' : '[已执行] ') + '核对 ' + r.checked_dirs + '/' + r.total_dirs
    + ' 个源目录 · 判定已删除 ' + r.dead_count + ' 个' + (dry ? '' : ' · 实际删除 ' + r.removed + ' 个') + '\n'
    + (r.log || []).join('\n');
  let h = '<thead><tr><th>115 源</th><th>strm 路径</th><th>原因</th></tr></thead><tbody>';
  (r.dead || []).slice(0, 200).forEach(d => {
    h += '<tr><td class="dst">' + esc(d.src) + '</td><td class="dst">' + esc(d.strm) + '</td>'
      + '<td class="hint">' + esc(d.why) + '</td></tr>';
  });
  $('clTbl').innerHTML = h + '</tbody>';
  if (!r.dead_count) {
    $('clTbl').innerHTML = '<tbody><tr><td class="empty">没发现已删除的源' +
      (r.total_dirs ? '' : '（清单还是空的）') + '</td></tr></tbody>';
  }
  toast(dry ? '试算完成' : '清理完成', '判定 ' + r.dead_count + ' 个' + (dry ? '' : ' · 删除 ' + r.removed + ' 个'), 'ok');
  if (!dry) { loadManifest(); statusTick(); }
}

/* ========================= 初始化 ========================= */
CATS.forEach(c => {
  const o = document.createElement('option'); o.value = c[0]; o.textContent = c[1] + '（' + c[0] + '）';
  $('cat').appendChild(o);
  const o2 = document.createElement('option'); o2.value = c[0]; o2.textContent = c[1];
  $('scCat').appendChild(o2);
});
$('cat').value = 'av'; $('scCat').value = 'av';
loadSettings();
$('cat').disabled = $('root').value === 'self';
syncLabels(); bindPersist();

document.querySelectorAll('.tabs .tab').forEach(b => b.onclick = () => activate(b.dataset.p));
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === 'Backspace' || (e.altKey && e.key === 'ArrowLeft')) { e.preventDefault(); up(); }
  else if (e.key === '/') { e.preventDefault(); $('filter').focus(); }
  else if (e.key === 'Escape') { sel.clear(); info(); renderList(); }
  else if (e.key === 'd' || e.key === 'D') toggleTheme();
});
let t0 = null;
try { t0 = localStorage.getItem('strmPanel.tab'); } catch (e) {}
activate(t0 && ['gen', 'sched', 'fix', 'misc'].includes(t0) ? t0 : 'gen');
startStatus();
load('');

async function fxFix(dry) {
  const d = $('fxDir').value.trim(), h = $('fxHost').value.trim();
  if (!h) { toast('还差新 host', '只填 host，端口会保持不变', 'warn'); return; }
  if (!dry) {
    const ok = await askConfirm('确认修正地址？', '目录：' + d + '\n新 host：' + h + '（端口不变）\n只改本地 .strm 内容。', '开始修正');
    if (!ok) return;
  }
  busy(true);
  let r = {};
  try { r = await (await fetch('/api/fix', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dir: d, host: h, dry})})).json(); }
  catch (e) { busy(false); toast('修正失败', String(e), 'err'); return; }
  busy(false);
  if (r.error) { toast('修正失败', r.error, 'err'); return; }
  $('fxCard').style.display = 'block'; $('fxLog').style.display = 'block';
  let log = (dry ? '[试算] 将改 ' : '[已修正] 改了 ') + r.updated + ' 个，跳过 ' + r.skipped + ' 个\n';
  log += '原地址分布: ' + JSON.stringify(r.old, null, 1) + '\n';
  (r.files || []).forEach(f => { log += '\n' + f.file + '\n  - ' + f.old + '\n  + ' + f.new + '\n'; });
  $('fxLog').className = dry ? '' : 'err';
  $('fxLog').textContent = log;
  toast(dry ? '试算完成' : '修正完成', (dry ? '将改 ' : '改了 ') + r.updated + ' 个 strm', 'ok');
  if (!dry) fxScan();
}







</script>
</body>
</html>
"""


# ---------------------------------------------------------------- server
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        raw = body if isinstance(body, bytes) else str(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _list_dir(self, path, cat, name, source='pipeline', layout='flat'):
        """列一层 115 目录。判定「已有 strm」用一次 os.listdir 取整目录快照 ——
        /mnt/g 是 9p 网络盘, 单次 stat 3~4ms, 逐文件 exists 在大目录下会卡死。"""
        m = MCP115()
        cid = m.resolve_cid(path or '')
        if cid is None:
            raise RuntimeError('115 上不存在: ' + path)
        root = local_root_for(path, cat, name, source, layout)
        try:
            have_set = set(os.listdir(root))
        except OSError:
            have_set = set()
        origins = load_origins() if source == 'self' else {}
        out = []
        for e in m.list_entries(cid):
            p = '/' + (path.strip('/') + '/' + e['fn']).lstrip('/')
            is_video = (not e['is_dir']) and os.path.splitext(e['fn'])[1].lower() in MEDIA_EXTS
            src_root = local_root_for(p, cat, name, source, layout) if e['is_dir'] else root
            o = origins.get(src_root)
            out.append({'name': e['fn'], 'path': p, 'is_dir': e['is_dir'],
                        'size': e['size'] if is_video else 0, 'video': is_video,
                        'have_strm': is_video and (os.path.splitext(e['fn'])[0] + '.strm') in have_set,
                        'self_origin': (o or {}).get('origin'), 'self_count': (o or {}).get('count')})
        out.sort(key=lambda x: (not x['is_dir'], x['name']))
        return {'path': path, 'root': root, 'layout': layout, 'origin': origins.get(root),
                'entries': out}

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n).decode('utf-8', 'replace')) if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/':
            cats = [[k, ia.category_name(k)] for k in ia.CATEGORY_KEYS]
            html = (HTML.replace('__CATS__', json.dumps(cats, ensure_ascii=False))
                        .replace('__EXTS__', ','.join(DEFAULT_EXTS))
                        .replace('__HOST__', st.local_host()))
            return self._send(200, html, 'text/html')
        if u.path == '/api/list':
            path = (q.get('path', [''])[0] or '').strip()
            name = (q.get('name', [''])[0] or '').strip()
            cat = ia.norm_category(q.get('category', ['av'])[0])
            source = 'self' if q.get('source', ['pipeline'])[0] == 'self' else 'pipeline'
            layout = 'auto' if q.get('layout', ['flat'])[0] == 'auto' else 'flat'
            nocache = q.get('nocache', ['0'])[0] in ('1', 'true', 'yes')
            key = (path, cat, name, source, layout)
            t0 = time.time()
            try:
                with _cache_lock:
                    hit = _cache.get(key)
                if hit and not nocache and time.time() - hit[0] < CACHE_TTL:
                    return self._json(dict(hit[1], cached=True, ms=0))
                with _lock:
                    payload = self._list_dir(path, cat, name, source, layout)
                with _cache_lock:
                    _cache[key] = (time.time(), payload)
                ms = int((time.time() - t0) * 1000)
                if ms > 1500:
                    print('[slow] list %s category=%s %dms' % (path or '/', cat, ms), flush=True)
                return self._json(dict(payload, cached=False, ms=ms))
            except Exception as e:
                return self._json({'error': str(e)[:300]}, 500)
        if u.path == '/api/task':
            t = _tasks.get(q.get('id', [''])[0])
            return self._json(t or {'error': 'no such task'}, 200 if t else 404)
        if u.path == '/api/tasks':
            rows = [{'id': t.get('id'), 'state': t.get('state'), 'title': t.get('title'),
                     'sched': t.get('sched'), 'created': t.get('created'),
                     'written': t.get('written', 0), 'pause': bool(t.get('pause')),
                     'log_tail': (t.get('log') or [])[-3:]}
                    for t in sorted(_tasks.values(), key=lambda x: x.get('created') or 0, reverse=True)[:30]]
            return self._json({'tasks': rows})
        if u.path == '/api/authorities':
            d = (q.get('dir', [''])[0] or '').strip()
            if not d or not os.path.isdir(d):
                return self._json({'error': '目录不存在: %s' % d}, 400)
            try:
                return self._json({'dir': d, 'counts': st.scan_authorities(d), 'current': st.local_host()})
            except Exception as e:
                return self._json({'error': str(e)[:300]}, 500)
        if u.path == '/api/schedules':
            return self._json({'tasks': SCHED.list(), 'current_host': st.local_host(),
                               'self_root': SELF_STRM_ROOT, 'units': list(st.UNITS.keys())})
        if u.path == '/api/manifest':
            s = MANIFEST.stats()
            s['current_host'] = st.local_host()
            return self._json(s)
        if u.path == '/api/status':
            # 轻量状态（顶栏轮询用）：不调 115、不做 9p stat
            run = sorted(_tasks.values(), key=lambda x: x.get('created') or 0, reverse=True)
            r0 = run[0] if run else None
            if r0:
                r0 = {'id': r0.get('id'), 'state': r0.get('state'), 'title': r0.get('title'),
                      'written': r0.get('written', 0), 'pause': bool(r0.get('pause')),
                      'total': len((r0.get('plan') or {}).get('items') or [])}
            return self._json({'host': st.local_host(), 'self_root': SELF_STRM_ROOT,
                               'schedules': len(SCHED.list()), 'manifest': len(MANIFEST.load()),
                               'strm_missing': None, 'run': r0})
        self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            b = self._body()
        except Exception as e:
            return self._json({'error': 'bad json: %s' % e}, 400)
        if u.path in ('/api/plan', '/api/run'):
            targets = b.get('targets') or []
            if not targets:
                return self._json({'error': '没有目标：勾选条目，或先进入某个目录'}, 400)
            common = dict(category=b.get('category', 'av'), name=(b.get('name') or '').strip(),
                          max_depth=int(b.get('max_depth') or 0),
                          min_size_mb=int(b.get('min_size_mb') or 0),
                          source=b.get('source', 'pipeline'), layout=b.get('layout', 'flat'),
                          exts=_norm_exts(b.get('exts')), append_pickcode=bool(b.get('append_pickcode')))
            if u.path == '/api/plan':
                try:
                    with _lock:
                        return self._json(build_plan(targets, common['category'], common['name'],
                                                     common['max_depth'], common['min_size_mb'],
                                                     common['source'], common['layout'],
                                                     exts=common['exts'],
                                                     append_pickcode=common['append_pickcode']))
                except Exception as e:
                    return self._json({'error': str(e)[:300]}, 500)
            tk = uuid.uuid4().hex[:8]
            _tasks[tk] = {'id': tk, 'state': 'running', 'log': [], 'written': 0, 'created': time.time()}
            spec = dict(common, targets=targets, wait=int(b.get('wait') or 0),
                        refresh=bool(b.get('refresh')), url_host=b.get('url_host') or 'current',
                        meta_exts=b.get('meta_exts') or [])
            threading.Thread(target=run_task, args=(tk, spec), daemon=True).start()
            return self._json({'task_id': tk})
        if u.path == '/api/task_ctl':
            t = _tasks.get(b.get('id') or '')
            if not t:
                return self._json({'error': 'no such task'}, 404)
            act = b.get('action')
            if act == 'pause':
                t['pause'] = True
            elif act == 'resume':
                t['pause'] = False
            elif act == 'cancel':
                t['cancel'] = True
                t['pause'] = False
            return self._json({'ok': True, 'pause': bool(t.get('pause')), 'cancel': bool(t.get('cancel'))})
        if u.path == '/api/fix':
            d = (b.get('dir') or '').strip()
            if not os.path.isdir(d):
                return self._json({'error': '目录不存在: %s' % d}, 400)
            try:
                logs = []
                r = st.fix_authority(d, b.get('host') or '', dry=bool(b.get('dry', True)))
                r['log'] = logs
                return self._json(r)
            except Exception as e:
                return self._json({'error': str(e)[:300]}, 400)
        if u.path == '/api/cleanup':
            try:
                logs = []
                r = cleanup_deleted(dry=bool(b.get('dry', True)),
                                    max_dirs=int(b.get('max_dirs') or 30), on_log=logs.append)
                r['log'] = logs
                return self._json(r)
            except Exception as e:
                return self._json({'error': str(e)[:300]}, 500)
        if u.path == '/api/schedule':
            act = b.get('action') or 'upsert'
            try:
                if act == 'upsert':
                    task = dict(b.get('task') or {})
                    if not task.get('id'):
                        task['id'] = 'S-' + uuid.uuid4().hex[:6]
                    unit = task.get('interval_unit') if task.get('interval_unit') in st.UNITS else 'days'
                    task['interval_unit'] = unit
                    task['interval_value'] = max(1, int(task.get('interval_value') or 1))
                    task.setdefault('enabled', True)
                    nxt = time.time() + task['interval_value'] * st.UNITS[unit]
                    task['next_ts'] = nxt
                    task['next_run'] = time.strftime('%m-%d %H:%M', time.localtime(nxt))
                    SCHED.upsert(task)
                    return self._json({'ok': True, 'task': task})
                if act == 'delete':
                    SCHED.delete(b.get('id'))
                    return self._json({'ok': True})
                if act == 'run':
                    return self._json({'ok': True, 'task': SCHED.run_now(b.get('id'))})
                if act == 'toggle':
                    t = SCHED.get(b.get('id')) or {}
                    on = bool(b.get('enabled'))
                    kw = {'enabled': on}
                    if on:
                        iv = int(t.get('interval_value') or 1) * st.UNITS.get(t.get('interval_unit') or 'days', 86400)
                        kw['next_ts'] = time.time() + iv
                        kw['next_run'] = time.strftime('%m-%d %H:%M', time.localtime(time.time() + iv))
                    SCHED.mark(b.get('id'), **kw)
                    return self._json({'ok': True})
                return self._json({'error': '未知 action: %s' % act}, 400)
            except Exception as e:
                return self._json({'error': str(e)[:300]}, 500)
        self._json({'error': 'not found'}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5091)
    ap.add_argument('--host', default='0.0.0.0')
    a = ap.parse_args()
    SCHED.start()                      # 定时任务线程（115-Desktop「定时生成」同款）
    n = len(SCHED.list())
    print('strm panel on http://127.0.0.1:%d  （定时任务 %d 条 · strm host %s）'
          % (a.port, n, st.local_host()), flush=True)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == '__main__':
    main()
