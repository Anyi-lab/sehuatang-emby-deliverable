#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm_relay.py v2 —— 115 strm 本地中转（带预读缓冲，吸收 115 CDN 的 TLS 抖动）

背景（2026-09-24/25 实测，详见 scripts/strm_relay.md）：
  1) strm 内容原本是 http://192.168.2.238:11500/d/<pickcode>/<文件名>
     115-Desktop 的“视频代理服务器”会 302 到 https://cdnfhnfile.115cdn.net/...，
     且直链签名【绑定请求方的 User-Agent】——换 UA 立刻 403 invalid signature。
     Emby 取流时不发 UA（中继日志里 ua=-），所以必须由中继替它带一个固定 UA。
  2) 115 CDN 会偶发 TLS 抖动：SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC / 中途截断
     （Content-Length 说 9MB，只给 1.5MB 就断）。

v1 → v2 改了什么（v1 保留在 scripts/strm_relay.py.bak-20260925）：
  * 【预读缓冲】独立读手线程按 --buffer-mb（默认 32MB）提前把上游数据读进队列，
    客户端从队列消费。CDN 抖动时客户端吃缓冲继续播，而不是当场断流 —— 这是顿卡的主要来源。
  * 【读手内部续传】截断/TLS 错误在读手内部用 Range 续传，客户端连接不断、header 不重发。
  * 【直链复用】pickcode → 直链 的缓存（默认 45s），重试与「探针/播放」并发请求不再反复取链；
    CDN 返回 403 时立即丢弃缓存重新取链。
  * 【重试更快】去掉了固定 sleep 0.3~0.5s，改成 0.15s 起的指数退避（上限 1.5s），
    并加了 TCP_NODELAY，首字节更快；「还没发出任何 header」的首字节阶段用更快的退避
    （0.1/0.2/0.3/0.5/0.8s），抖动时拖进度条不用干等十几秒。
  * 【可观测】日志新增缓冲高水位、续传次数，便于回头查“为什么还顿”。

用法：
  python3 strm_relay.py --port 11501 --upstream http://192.168.2.238:11500
"""

import argparse
import http.client
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = 'http://192.168.2.238:11500'
RELAY_UA = 'Lavf/60.16.100'        # 两跳必须同一个 UA（115 把直链绑到 UA）
MAX_ATTEMPTS = 12                  # 读手内部「取链 + 取数」轮数上限
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
CHUNK = 128 * 1024
BUFFER_MB = 32                     # 预读缓冲上限（≈ 1080p 十几秒到几十秒的数据）
LINK_TTL = 45                      # 直链缓存秒数
BACKOFF = (0.15, 0.3, 0.6, 1.2, 1.5)      # 流中断后续传用（要压住重试频率）
FIRST_BACKOFF = (0.1, 0.2, 0.3, 0.5, 0.8)  # 首字节前用（客户端还什么都没拿到，越快越好）
LOG_LOCK = threading.Lock()
RID = iter(range(1, 10 ** 9))


class ClientGone(Exception):
    """客户端主动断开（播放器 seek/停止），不是错误。"""


def log(msg):
    with LOG_LOCK:
        sys.stdout.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
        sys.stdout.flush()


def split_url(url):
    u = urllib.parse.urlsplit(url)
    return (u.hostname,
            u.port or (443 if u.scheme == 'https' else 80),
            u.scheme == 'https',
            u.path + (('?' + u.query) if u.query else ''))


def open_conn(url, timeout=CONNECT_TIMEOUT):
    host, port, is_tls, _ = split_url(url)
    if is_tls:
        c = http.client.HTTPSConnection(host, port, timeout=timeout,
                                        context=ssl.create_default_context())
    else:
        c = http.client.HTTPConnection(host, port, timeout=timeout)
    return c


def nodelay(conn):
    """关掉 Nagle，避免小包被攒着发。"""
    try:
        s = getattr(conn, 'sock', None)
        if s is not None:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 直链缓存：pickcode+文件名 -> Location
# ----------------------------------------------------------------------
class LinkCache:
    def __init__(self, ttl):
        self.ttl = ttl
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            loc, exp = v
            if time.time() > exp:
                self._d.pop(key, None)
                return None
            return loc

    def put(self, key, loc):
        with self._lock:
            self._d[key] = (loc, time.time() + self.ttl)
            if len(self._d) > 500:
                now = time.time()
                for k in [k for k, (_, e) in self._d.items() if e < now]:
                    self._d.pop(k, None)

    def drop(self, key):
        with self._lock:
            self._d.pop(key, None)


LINKS = LinkCache(LINK_TTL)


def resolve_cdn_url(pickcode, filename, use_cache=True):
    """向 11500 要直链，返回 (状态码, Location, 是否命中缓存)。"""
    key = (pickcode, filename)
    if use_cache:
        loc = LINKS.get(key)
        if loc:
            return 302, loc, True
    up = '%s/d/%s/%s' % (UPSTREAM.rstrip('/'),
                         urllib.parse.quote(pickcode, safe=''),
                         urllib.parse.quote(filename, safe=''))
    c = open_conn(up)
    try:
        c.request('GET', split_url(up)[3], headers={'User-Agent': RELAY_UA, 'Accept': '*/*'})
        r = c.getresponse()
        loc = r.getheader('Location')
        r.read(0)
        if loc and r.status in (301, 302, 307):
            LINKS.put(key, loc)
        return r.status, loc, False
    finally:
        c.close()


def parse_range(hdr):
    """bytes=A-B / bytes=A- → (A, B|None)；多段/后缀写法不支持，返回 None。"""
    if not hdr or not hdr.startswith('bytes=') or ',' in hdr:
        return None
    spec = hdr[6:].strip()
    if '-' not in spec:
        return None
    a, b = spec.split('-', 1)
    try:
        if not a:
            return None
        return int(a), (int(b) if b else None)
    except ValueError:
        return None


def length_from_headers(status, cl, cr):
    if status == 206 and cr and cr.startswith('bytes ') and '/' in cr:
        rng = cr.split(' ')[1].split('/')[0]
        try:
            a, b = rng.split('-')
            return int(b) - int(a) + 1
        except Exception:
            return None
    try:
        return int(cl) if cl else None
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 预读读手：把上游数据提前灌进队列
# ----------------------------------------------------------------------
class Prefetch(threading.Thread):
    def __init__(self, rid, pickcode, name, start, end, resp, conn):
        super().__init__(daemon=True)
        self.rid = rid
        self.pickcode = pickcode
        self.name = name
        self.begin = start             # 不能叫 self.start：会盖掉 Thread.start 方法
        self.end = end                 # None = 客户端没给上界（读到 EOF 为止）
        self.resp = resp               # 已经打开、header 已读的第一跳响应
        self.conn = conn
        self.q = queue.Queue(maxsize=max(4, int(BUFFER_MB * 1024 * 1024 / CHUNK)))
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.err = None
        self.pos = self.begin
        self.bytes_read = 0
        self.resumes = 0
        self.truncs = 0
        self.high_water = 0

    def _put(self, buf):
        while not self.stop.is_set():
            try:
                self.q.put(buf, timeout=0.5)
                if self.q.qsize() > self.high_water:
                    self.high_water = self.q.qsize()
                return True
            except queue.Full:
                continue
        return False

    def _drop_conn(self):
        for c in (self.resp, self.conn):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.resp = None
        self.conn = None

    def run(self):
        MAXQ = self.q.maxsize
        attempt = 0
        wait = BACKOFF[0]
        try:
            while not self.stop.is_set():
                try:
                    if self.resp is None:
                        attempt += 1
                        if attempt > MAX_ATTEMPTS:
                            self.err = self.err or 'attempts exhausted'
                            break
                        status, loc, cached = resolve_cdn_url(
                            self.pickcode, self.name, use_cache=(attempt > 1))
                        if not loc:
                            self.err = 'upstream %s (no location)' % status
                            time.sleep(wait)
                            wait = min(wait * 2, BACKOFF[-1])
                            continue
                        rng = 'bytes=%d-%s' % (self.pos,
                                               self.end if self.end is not None else '')
                        conn = open_conn(loc)
                        conn.request('GET', split_url(loc)[3],
                                     headers={'User-Agent': RELAY_UA, 'Accept': '*/*',
                                              'Connection': 'close', 'Range': rng})
                        nodelay(conn)
                        resp = conn.getresponse()
                        if resp.status not in (200, 206):
                            body = resp.read(200)
                            self.err = 'cdn %s %s' % (resp.status,
                                                      body[:60].decode('utf-8', 'replace'))
                            log('CDN-%s #%d %s try=%d range=%s cache=%s' % (
                                resp.status, self.rid, self.pickcode, attempt, rng, cached))
                            if resp.status == 403:
                                LINKS.drop((self.pickcode, self.name))
                            resp.close()
                            conn.close()
                            time.sleep(wait)
                            wait = min(wait * 2, BACKOFF[-1])
                            continue
                        if resp.status == 200 and self.pos > 0:
                            # CDN 忽略了 Range：不能当作续传，重试
                            self.err = 'cdn 200 ignored range at pos=%d' % self.pos
                            resp.close()
                            conn.close()
                            time.sleep(wait)
                            wait = min(wait * 2, BACKOFF[-1])
                            continue
                        self.resp, self.conn = resp, conn
                        wait = BACKOFF[0]
                        if attempt > 1:
                            self.resumes += 1
                            log('RESUME #%d %s pos=%d try=%d' % (self.rid, self.pickcode,
                                                                 self.pos, attempt))

                    resp = self.resp
                    got = 0
                    want = length_from_headers(resp.status, resp.getheader('Content-Length'),
                                               resp.getheader('Content-Range'))
                    while True:
                        if self.stop.is_set():
                            return
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        if not self._put(buf):
                            return
                        self.pos += len(buf)
                        self.bytes_read += len(buf)
                        got += len(buf)
                        if self.end is not None and self.pos > self.end:
                            break

                    if self.end is not None and self.pos > self.end:
                        break                          # 客户端要的已经给全
                    if want is not None and got < want:
                        self.truncs += 1               # 上游截断 → 续传
                        self.err = 'truncated got=%d want=%d at pos=%d' % (got, want, self.pos)
                        log('TRUNC #%d %s got=%d want=%d pos=%d' % (self.rid, self.pickcode,
                                                                    got, want, self.pos))
                        self._drop_conn()
                        time.sleep(wait)
                        wait = min(wait * 2, BACKOFF[-1])
                        continue
                    break                              # 正常读完
                except Exception as e:                 # noqa: BLE001 上游任何异常都重试
                    self.err = '%s: %s' % (type(e).__name__, e)
                    log('RETRY #%d %s try=%d pos=%d err=%s' % (self.rid, self.pickcode,
                                                               attempt, self.pos, self.err[:110]))
                    self._drop_conn()
                    if self.stop.is_set():
                        break
                    time.sleep(wait)
                    wait = min(wait * 2, BACKOFF[-1])
        finally:
            self._drop_conn()
            self.finished.set()
            log('PREFETCH-END #%d %s read=%d pos=%d resumes=%d truncs=%d hw=%d/%d %s' % (
                self.rid, self.pickcode, self.bytes_read, self.pos, self.resumes,
                self.truncs, self.high_water, MAXQ, self.err or 'ok'))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'strm-relay/2.0'

    def log_message(self, fmt, *a):
        pass

    def _parse(self):
        path = urllib.parse.urlsplit(self.path).path
        parts = [urllib.parse.unquote(p) for p in path.split('/') if p]
        if len(parts) >= 2 and parts[0] in ('d', 'play', 'strm'):
            parts = parts[1:]
        if len(parts) < 2:
            return None, None
        return parts[0], '/'.join(parts[1:])

    def _json(self, code, msg):
        body = ('{"status":%d,"message":"%s"}' % (code, str(msg)[:200].replace('"', "'"))).encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except Exception:
            pass
        self.close_connection = True

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    # ------------------------------------------------------------------
    def _serve(self, head_only):
        rid = next(RID)
        if urllib.parse.urlsplit(self.path).path.rstrip('/') == '/health':
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        pickcode, name = self._parse()
        if not pickcode:
            self._json(404, 'bad path')
            return

        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        client_range = self.headers.get('Range')
        parsed = parse_range(client_range)
        start = parsed[0] if parsed else 0
        end = parsed[1] if parsed else None
        log('REQ #%d %s range=%s ua=%s' % (rid, pickcode, client_range or '-',
            (self.headers.get('User-Agent') or '-')[:28]))

        t0 = time.time()
        pref = None
        first_conn = None
        headers_sent = False
        sent = 0
        try:
            # ---- 第一跳：拿到状态码/长度，再给客户端发 header ----
            attempt = 0
            resp = None
            while True:
                attempt += 1
                if attempt > MAX_ATTEMPTS:
                    self._json(502, 'relay failed: no upstream')
                    return
                status, loc, cached = resolve_cdn_url(pickcode, name)
                if not loc:
                    wait = FIRST_BACKOFF[min(attempt - 1, len(FIRST_BACKOFF) - 1)]
                    log('UPSTREAM-%s #%d %s try=%d' % (status, rid, pickcode, attempt))
                    time.sleep(wait)
                    continue
                rng = 'bytes=0-0' if head_only else 'bytes=%d-%s' % (start, end if end is not None else '')
                first_conn = open_conn(loc)
                first_conn.request('GET', split_url(loc)[3],
                                   headers={'User-Agent': RELAY_UA, 'Accept': '*/*',
                                            'Connection': 'close', 'Range': rng})
                nodelay(first_conn)
                resp = first_conn.getresponse()
                if resp.status in (200, 206):
                    if resp.status == 200 and start > 0:
                        resp.close()
                        first_conn.close()
                        first_conn = None
                        time.sleep(FIRST_BACKOFF[min(attempt - 1, len(FIRST_BACKOFF) - 1)])
                        continue
                    break
                body = resp.read(200)
                log('CDN-%s #%d %s try=%d range=%s body=%s' % (resp.status, rid, pickcode,
                    attempt, rng, body[:60].decode('utf-8', 'replace')))
                if resp.status == 403:
                    LINKS.drop((pickcode, name))
                resp.close()
                first_conn.close()
                first_conn = None
                time.sleep(FIRST_BACKOFF[min(attempt - 1, len(FIRST_BACKOFF) - 1)])

            total = length_from_headers(resp.status, resp.getheader('Content-Length'),
                                        resp.getheader('Content-Range'))
            cr = resp.getheader('Content-Range') or ''
            if head_only:
                if '/' in cr and cr.split('/')[-1].isdigit():
                    total = int(cr.split('/')[-1])
                resp.close()
                first_conn.close()
                self.send_response(200)
                self.send_header('Content-Type', resp.getheader('Content-Type') or 'video/mp4')
                self.send_header('Content-Length', str(total or 0))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Connection', 'close')
                self.close_connection = True
                self.end_headers()
                log('HEAD #%d ok %dms size=%s' % (rid, (time.time() - t0) * 1000, total))
                return

            self.send_response(resp.status)
            for hk in ('Content-Type', 'Content-Length', 'Content-Range',
                       'Last-Modified', 'ETag'):
                v = resp.getheader(hk)
                if v:
                    self.send_header(hk, v)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers()
            headers_sent = True
            log('HDR #%d status=%s CL=%s CR=%s' % (rid, resp.status,
                resp.getheader('Content-Length'), resp.getheader('Content-Range')))

            # ---- 读手 + 消费循环 ----
            pref = Prefetch(rid, pickcode, name, start, end, resp, first_conn)
            first_conn = None
            pref.start()
            expected = total
            empty_polls = 0
            while True:
                try:
                    buf = pref.q.get(timeout=0.5)
                except queue.Empty:
                    if pref.finished.is_set() and pref.q.empty():
                        break
                    # 读手在重试/退避：客户端先吃缓冲，不主动断
                    try:
                        self.wfile.flush()
                    except Exception:
                        raise ClientGone()
                    empty_polls += 1
                    if empty_polls > 240 and pref.q.empty():    # ~2 分钟彻底没数据
                        break
                    continue
                empty_polls = 0
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    raise ClientGone()
                sent += len(buf)

            if expected is not None and sent >= expected:
                log('OK #%d %s bytes=%d %dms read=%d resumes=%d truncs=%d hw=%d' % (
                    rid, pickcode, sent, (time.time() - t0) * 1000, pref.bytes_read,
                    pref.resumes, pref.truncs, pref.high_water))
            else:
                log('SHORT #%d %s sent=%d/%s read=%d resumes=%d truncs=%d last=%s' % (
                    rid, pickcode, sent, expected, pref.bytes_read, pref.resumes,
                    pref.truncs, pref.err))
        except ClientGone:
            log('CLIENT-GONE #%d %s sent=%d' % (rid, pickcode, sent))
        except Exception as e:                          # noqa: BLE001
            log('ERRSRV #%d %s sent=%d err=%s: %s' % (rid, pickcode, sent,
                                                      type(e).__name__, e))
            if not headers_sent:
                self._json(502, 'relay failed: %s' % e)
        finally:
            if pref is not None:
                pref.stop.set()
            if first_conn is not None:
                try:
                    first_conn.close()
                except Exception:
                    pass
            self.close_connection = True


def main():
    global UPSTREAM, RELAY_UA, BUFFER_MB, LINK_TTL
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=11501)
    ap.add_argument('--upstream', default=UPSTREAM)
    ap.add_argument('--ua', default=RELAY_UA)
    ap.add_argument('--buffer-mb', type=int, default=BUFFER_MB)
    ap.add_argument('--link-ttl', type=int, default=LINK_TTL)
    a = ap.parse_args()
    UPSTREAM, RELAY_UA = a.upstream, a.ua
    BUFFER_MB, LINK_TTL = a.buffer_mb, a.link_ttl
    LINKS.ttl = LINK_TTL
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    log('strm_relay v2 启动 %s:%d upstream=%s ua=%s buffer=%dMB link_ttl=%ds' % (
        a.host, a.port, UPSTREAM, RELAY_UA, BUFFER_MB, LINK_TTL))
    srv.serve_forever()


if __name__ == '__main__':
    main()
