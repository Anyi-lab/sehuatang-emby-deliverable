#!/usr/bin/env python3
"""给 欧美 / 无码 库里的横版海报补裁竖版。
优先让 mdc-ng 自己做（删掉 poster 后触发该条目重新刮削，mdc 按当前配置重新裁）；
若该条目在 mdc 里没有对应任务记录（或记录路径与现在不一致），退回本地按同一算法裁。
dry-run: python3 poster_fix_batch.py
apply  : python3 poster_fix_batch.py --apply
"""
import os, sys, json, shutil, time, urllib.request
from PIL import Image

APPLY = '--apply' in sys.argv
BASE = 'http://127.0.0.1:9208'
RATIO = 2.12 / 3.0
BAK_ROOT = '/mnt/g/srtm/_归档/_backup_20260927_poster_crop23'
LIBS = {'欧美': '/mnt/g/srtm/已刮削/欧美', '无码': '/mnt/g/srtm/已刮削/无码'}

def get(p):
    with urllib.request.urlopen(BASE + p, timeout=30) as r:
        return json.loads(r.read().decode())

def post(p, body=None):
    d = json.dumps(body).encode() if body is not None else b'{}'
    r = urllib.request.Request(BASE + p, data=d, method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, resp.read().decode()

# 任务表：dir -> id
tasks = {}
d = get('/api/tasks?page=1&page_size=200')
pages = d['num_pages']
rows = list(d['data'])
for p in range(2, pages + 1):
    rows += get('/api/tasks?page=%d&page_size=200' % p)['data']
for t in rows:
    tp = t.get('target_path')
    if tp:
        tasks[os.path.dirname(tp.replace('/media/', '/mnt/g/srtm/'))] = t

def local_crop(dp, bakdir):
    src = os.path.join(dp, 'poster.jpg')
    if not os.path.exists(src):
        src = os.path.join(dp, 'thumb.jpg')
    with Image.open(src) as im:
        w, h = im.size
        cw = int(round(h * RATIO))
        if cw >= w:
            return None
        out = im.convert('RGB').crop(((w - cw) // 2, 0, (w - cw) // 2 + cw, h))
    os.makedirs(bakdir, exist_ok=True)
    if not os.path.exists(os.path.join(bakdir, 'poster.jpg')):
        shutil.copy2(src, os.path.join(bakdir, 'poster.jpg'))
    tmp = os.path.join(dp, 'poster.jpg.tmp')
    out.save(tmp, 'JPEG', quality=95, subsampling=0)
    os.replace(tmp, os.path.join(dp, 'poster.jpg'))
    return out.size

plan = []
for lib, root in LIBS.items():
    for dp, dn, fn in os.walk(root):
        if 'poster.jpg' not in fn:
            continue
        with Image.open(os.path.join(dp, 'poster.jpg')) as im:
            w, h = im.size
        rel = os.path.relpath(dp, root)
        if w / h <= 1.05:
            continue                      # 已是竖版
        t = tasks.get(dp)
        mode = 'mdc_restart' if (t and t['status'] in (0, 1, 2, 3)) else 'local_crop'
        plan.append(dict(lib=lib, rel=rel, dir=dp, task_id=t['id'] if t else None,
                         task_status=t['status'] if t else None, mode=mode, before=[w, h],
                         bakdir=os.path.join(BAK_ROOT, lib + '_' + rel.replace('/', '_').replace(' ', ''))))

json.dump(plan, open('/tmp/poster_fix_plan%s.json' % ('' if APPLY else '_dry'), 'w'), ensure_ascii=False, indent=1)
from collections import Counter
print('待处理', len(plan), Counter(p['mode'] for p in plan), Counter(p['lib'] for p in plan))

if not APPLY:
    for x in plan:
        print('%-3s %-38s %-12s task=%s %s' % (x['lib'], x['rel'][:38], x['mode'], x['task_id'], x['before']))
    sys.exit(0)

# ---------- 执行 ----------
results = []
# 1) 本地补裁
for x in [p for p in plan if p['mode'] == 'local_crop']:
    try:
        sz = local_crop(x['dir'], x['bakdir'])
        x['after'] = list(sz) if sz else None
        x['result'] = 'ok' if sz else 'fail'
    except Exception as e:
        x['result'] = 'error: %s' % e
    results.append(x)
    print('裁剪', x['lib'], x['rel'], x.get('after'), x.get('result'))

# 2) mdc 重启重刮
mdc_items = [p for p in plan if p['mode'] == 'mdc_restart']
for x in mdc_items:
    try:
        os.makedirs(x['bakdir'], exist_ok=True)
        pj = os.path.join(x['dir'], 'poster.jpg')
        if not os.path.exists(os.path.join(x['bakdir'], 'poster.jpg')):
            shutil.copy2(pj, os.path.join(x['bakdir'], 'poster.jpg'))
        os.remove(pj)
        st, txt = post('/api/tasks/%d/restart' % x['task_id'], {"overwrite_id": None, "url_override": None})
        x['result'] = 'queued(%d)' % st
    except Exception as e:
        x['result'] = 'error: %s' % e
    print('重刮', x['lib'], x['rel'], x.get('result'))

# 等 mdc 跑完
deadline = time.time() + 900
pending = [x for x in mdc_items if x.get('result', '').startswith('queued')]
while pending and time.time() < deadline:
    time.sleep(10)
    still = []
    for x in pending:
        pj = os.path.join(x['dir'], 'poster.jpg')
        if os.path.exists(pj):
            try:
                with Image.open(pj) as im:
                    w, h = im.size
                if w / h <= 1.05:
                    x['after'] = [w, h]; x['result'] = 'ok'
                    continue
            except Exception:
                pass
        still.append(x)
    pending = still
for x in mdc_items:
    if x.get('result', '').startswith('queued'):
        x['result'] = 'timeout/未变竖版'
    results.append(x)

for x in results:
    print('%-10s %-40s %-8s %s -> %s' % (x['lib'], x['rel'][:40], x['result'], x['before'], x.get('after')))
json.dump(results, open('/tmp/poster_fix_result.json', 'w'), ensure_ascii=False, indent=1)
print('可逆性：所有原图备份在', BAK_ROOT)
