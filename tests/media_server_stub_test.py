#!/usr/bin/env python3
# 三后端离线验证 (2026-09-16): ①Emby(必须 /emby 前缀, PlaybackInfo 强制 UserId)
#                                ②Jellyfin 老版(两种前缀都认)  ③Jellyfin 新版(任何 /emby 都 404)
import json, os, sys, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HITS = {}


class H(BaseHTTPRequestHandler):
    flavor = 'emby'
    mode = 'emby'          # emby | jf_both | jf_bare
    need_userid = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = b'' if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bare(self, p):
        return p[5:] if p.startswith('/emby/') else p

    def _hit(self, m, p):
        HITS.setdefault(self.server.server_port, []).append((m, p))

    def _ctx(self):
        p = self.path.split('?')[0]
        if self.mode == 'jf_bare' and p.startswith('/emby/'):
            self._send(404, {'error': 'no legacy prefix'})
            return None
        if self.mode == 'emby' and not p.startswith('/emby/'):
            self._send(404, {'error': 'prefix required'})
            return None
        return p

    def do_POST(self):
        raw = self.path.split('?')[0]
        self._hit('POST', raw)
        p = self._ctx()
        if p is None:
            return
        if self._bare(p) == '/Library/Refresh':
            return self._send(204)
        if self._bare(p).startswith('/Items/') and p.endswith('/PlaybackInfo'):
            if self.need_userid and 'UserId=' not in self.path:
                return self._send(400, {'error': 'UserId required'})
            return self._send(200, {'MediaSources': [{'Container': 'mkv',
                                                      'MediaStreams': [{'Type': 'Video'}]}]})
        return self._send(404)

    def do_GET(self):
        raw = self.path.split('?')[0]
        self._hit('GET', raw)
        p = self._ctx()
        if p is None:
            return
        b = self._bare(p)
        if b == '/System/Info/Public':
            d = {'ServerName': 'STUB'}
            if self.flavor == 'jellyfin':
                d['ProductName'] = 'Jellyfin Server'
            return self._send(200, d)
        if b == '/Users':
            return self._send(200, [{'Id': 'user-abc-123', 'Name': 'stub'}])
        if b == '/Items':
            return self._send(200, {'Items': [{'Id': 'i1', 'Path': 'G:\\srtm\\已刮削\\AV\\FNS-257\\x.mkv'},
                                              {'Id': 'i2', 'Path': '/mnt/g/srtm/已刮削/AV/MIDA-787\\y.mkv'}]})
        return self._send(404)


def mk(mode, flavor, need_userid):
    return type('H_%s' % mode, (H,), {'mode': mode, 'flavor': flavor, 'need_userid': need_userid})


def serve(cls, port):
    srv = ThreadingHTTPServer(('127.0.0.1', port), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


serve(mk('emby', 'emby', True), 8101)              # 真 Emby(模拟本机 4.x)
serve(mk('jf_both', 'jellyfin', False), 8102)      # Jellyfin: 两种前缀都认
serve(mk('jf_bare', 'jellyfin', False), 8103)      # Jellyfin 新版: /emby 一律 404

os.environ['MEDIA_SERVERS'] = ('http://127.0.0.1:8101|embykey,'
                               'http://127.0.0.1:8102|jfkey1,'
                               'http://127.0.0.1:8103|jfkey2')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'server'))
import import_api as ia

ok = True
def chk(label, cond, extra=''):
    global ok
    print(('  PASS  ' if cond else '  FAIL  ') + label + ('   ' + str(extra) if extra else ''))
    ok = ok and cond

S = ia.MEDIA_SERVERS
chk('解析出 3 台', len(S) == 3, [(x['name'], x['token']) for x in S])

# 1) 家族 + 前缀一起探
chk('① 家族=emby', ia.media_server_flavor(force=True, srv=S[0]) == 'emby')
chk('② 家族=jellyfin', ia.media_server_flavor(force=True, srv=S[1]) == 'jellyfin')
chk('③ 家族=jellyfin', ia.media_server_flavor(force=True, srv=S[2]) == 'jellyfin')
chk('① 前缀=/emby', S[0]['prefix'] == '/emby', S[0]['prefix'])
chk('② 前缀=/emby', S[1]['prefix'] == '/emby', S[1]['prefix'])
chk('③ 前缀=(无) —— 自动学到不带 /emby', S[2]['prefix'] == '', repr(S[2]['prefix']))

# 2) _media_path: 调用点只写接口名
chk('_media_path 对 ① 加前缀', ia._media_path('/Library/Refresh', srv=S[0]) == '/emby/Library/Refresh')
chk('_media_path 对 ② 加前缀', ia._media_path('/Library/Refresh', srv=S[1]) == '/emby/Library/Refresh')
chk('_media_path 对 ③ 去掉前缀', ia._media_path('/Library/Refresh', srv=S[2]) == '/Library/Refresh')
chk('_media_path 对 ③ 去 Items 前缀', ia._media_path('/Items/i1/PlaybackInfo', srv=S[2]).startswith('/Items/'))

# 3) 广播扫库: 三台全中
code = ia.media_refresh(timeout=10)
chk('广播返回 204', code == 204, ia.MEDIA_LAST_REFRESH)
chk('① 路由', S[0]['route'] == '/emby/Library/Refresh', S[0]['route'])
chk('② 路由', S[1]['route'] == '/emby/Library/Refresh', S[1]['route'])
chk('③ 路由(无前缀)', S[2]['route'] == '/Library/Refresh', S[2]['route'])
chk('③ 前缀已学到, 扫库一次命中不打 /emby',
    [p for m, p in HITS[8103] if m == 'POST'] == ['/Library/Refresh'],
    [p for m, p in HITS[8103] if m == 'POST'])

# 4) 第二次直接走已学到的路由
n = len(HITS[8103])
ia.media_refresh(timeout=10)
chk('③ 第二次一发命中', [p for m, p in HITS[8103][n:] if m == 'POST'] == ['/Library/Refresh'],
    [p for m, p in HITS[8103][n:] if m == 'POST'])

# 5) UserId: Emby 取到, Jellyfin 不取
chk('① UserId 取到', ia.media_user_id(srv=S[0]) == 'user-abc-123')
chk('② 老 Jellyfin 也取到了 UserId(但不会进 URL)', ia.media_user_id(srv=S[1]) == 'user-abc-123')
u1 = ia.media_playback_url('i1', srv=S[0])
u3 = ia.media_playback_url('i1', srv=S[2])
chk('① PlaybackInfo 带 UserId 且带 /emby', 'UserId=user-abc-123' in u1 and '/emby/Items/' in u1, u1)
chk('③ PlaybackInfo 不带 UserId 且无 /emby', 'UserId' not in u3 and '/emby/' not in u3, u3)
chk('③ /Users 也走无前缀', ia.media_user_id(srv=S[2]) == 'user-abc-123'
    and [p for m, p in HITS[8103] if m == 'GET' and p.endswith('/Users')] == ['/Users'],
    [p for m, p in HITS[8103] if m == 'GET' and p.endswith('/Users')])
emby_tries = [p for m, p in HITS[8103] if p.startswith('/emby/')]
chk('③ 只探测阶段试过一次 /emby/System/Info/Public(404 后弃用), 其余全走无前缀',
    emby_tries == ['/emby/System/Info/Public'], [p for m, p in HITS[8103]])

# 6) 真打一发 PlaybackInfo(prewarm 那条路)
chk('① prewarm 探测成功(带 UserId)', ia._prewarm_probe_one('i1', '/mnt/g/srtm/已刮削/AV/x.mkv') is True)

# 7) Items 列表 + 路径归一化 (Windows 写法 <-> WSL 写法)
items = ia._emby_items_by_path_prefix('/mnt/g/srtm/已刮削/AV', srv=S[2])
chk('③ Items 走无前缀并命中 2 条(两种路径写法)',
    len(items) == 2 and [p for m, p in HITS[8103] if m == 'GET' and p == '/Items'],
    [i['Path'] for i in items])

# 8) status 汇总
rows = ia.media_server_status(probe=True)
chk('status 三台全 204', [r['refresh'] for r in rows] == [204, 204, 204], rows)
chk('status 前缀字段正确', [r['prefix'] for r in rows] == ['/emby', '/emby', '(无)'],
    [r['prefix'] for r in rows])

# 9) 单机回退兼容
os.environ.pop('MEDIA_SERVERS')
os.environ['MEDIA_SERVER_URL'] = 'http://127.0.0.1:8101'
os.environ['MEDIA_SERVER_TOKEN'] = 'singlekey'
import importlib
ia2 = importlib.reload(ia)
chk('单机模式仍读旧变量', ia2.MEDIA_SERVER_URL == 'http://127.0.0.1:8101' and len(ia2.MEDIA_SERVERS) == 1)
chk('单机模式扫库 204', ia2.media_refresh(timeout=10) == 204)

print('\n总结:', 'ALL PASS' if ok else 'HAS FAIL')
sys.exit(0 if ok else 1)
