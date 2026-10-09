import os, json, sys
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
ROOT = '/mnt/g/srtm/已刮削'
out = {'horz': [], 'tiny': [], 'noimg': [], 'ok': 0}
for dp, dn, fn in os.walk(ROOT):
    if os.path.basename(dp).startswith('_trash') or '_backup' in dp or '_归档' in dp:
        continue
    has_strm = any(f.endswith('.strm') for f in fn)
    has_nfo = any(f.endswith('.nfo') for f in fn)
    if not (has_strm or has_nfo):
        continue
    pj = os.path.join(dp, 'poster.jpg')
    rel = os.path.relpath(dp, ROOT)
    if not os.path.exists(pj):
        out['noimg'].append(rel); continue
    try:
        with Image.open(pj) as im:
            w, h = im.size
    except Exception as e:
        out['noimg'].append(rel + ' [%s]' % e); continue
    r = w / h
    if r > 1.05:
        out['horz'].append([rel, w, h, round(r, 3)])
    elif w < 400:
        out['tiny'].append([rel, w, h])
    else:
        out['ok'] += 1
json.dump(out, open('/tmp/poster_scan_all.json', 'w'), ensure_ascii=False, indent=1)
print('正常竖版', out['ok'], '横版', len(out['horz']), '小图(<400宽)', len(out['tiny']), '无图', len(out['noimg']))
from collections import Counter
print('横版分布', Counter(x[0].split('/')[0] for x in out['horz']))
print('小图分布', Counter(x[0].split('/')[0] for x in out['tiny']))
print('无图分布', Counter(x.split('/')[0] for x in out['noimg']))
for x in out['horz']:
    print('  横', x)
