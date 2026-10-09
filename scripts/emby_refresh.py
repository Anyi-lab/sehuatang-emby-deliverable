import json, urllib.request
EM = 'http://172.25.224.1:8096'
KEY = '08ba96f52aaa4b8cbb262ffdf1102b54'
# key 统一从 /root/tools/emby_key.txt 读（改 key 只需改那一个文件）；读不到才用上面的兜底
try:
    for _l in open('/root/tools/emby_key.txt', encoding='utf-8'):
        _l = _l.strip()
        if _l.startswith('EMBY_KEY=') and _l.split('=', 1)[1].strip():
            KEY = _l.split('=', 1)[1].strip()
        elif _l.startswith('EMBY_HOST=') and _l.split('=', 1)[1].strip():
            EM = _l.split('=', 1)[1].strip()
except OSError:
    pass
def req(m, p, b=None):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(EM + p, data=d, method=m,
        headers={'X-Emby-Token': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=120) as x:
        t = x.read().decode()
        return x.status, (json.loads(t) if t.strip().startswith(('{', '[')) else t)
s, vf = req('GET', '/emby/Library/VirtualFolders')
for v in vf:
    print(v['Name'], v['ItemId'], v.get('Locations'))
for v in vf:
    if v['Name'] in ('欧美', '无码'):
        st, txt = req('POST', '/emby/Items/%s/Refresh' % v['ItemId'], {
            'Recursive': True, 'ImageRefreshMode': 'FullRefresh',
            'MetadataRefreshMode': 'None', 'ReplaceAllImages': False, 'ReplaceAllMetadata': False})
        print('刷新', v['Name'], st, txt)
