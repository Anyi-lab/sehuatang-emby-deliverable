# -*- coding: utf-8 -*-
"""
import_api.py — 云主机一键入库服务 (<SERVER_IP>:5081): 磁力/电驴入库 → 115 → strm → MDC 刮削/油猴补充 → Emby
================================================================
设计背景:
  - 刮削需要访问 sehuatang 网站抓首楼正文/海报, 网站有 Cloudflare 限制,
    只有本机 Windows 的 Playwright 有头浏览器能稳定过 CF (Linux 容器不行)
  - 因此一键入库(含完整刮削)放到本机: 推115 → 等文件落地 → 刮削(抓首楼+海报,
    nfo/图片上传115) → SmartStrm webhook 生成 strm → Emby 扫库

端口: 5081 (独立于 5080 旧版爬虫搜索)
访问: http://<LAN_IP>:5081/
"""
import json, os, sys, time, re, sqlite3, threading, queue, urllib.request, urllib.error, urllib.parse, ssl, uuid, logging, subprocess, shutil, socket
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from cd2grpc import MOUNT_PREFIX   # /115open, CD2 API 落地检测 (2026-08-19)
import avdb_source as avdb_src     # avdb 下载侧数据源(只读本地 sqlite, 2026-09-19)
from avdb_page import AVDB_PAGE       # avdb 连接器独立面板 (GET /avdb, 2026-09-19)
from index_page import render_index_page   # 首页 (批量入库页, 2026-09-20 抽出)
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('import-api')

BASE = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(BASE, 'creds.json')
COOKIES = os.path.join(BASE, 'cookies.json')
LOG_DB = os.path.join(BASE, 'import_log.db')

# 2026-08-20: import_log 增加 thread_url 列 (任务监控页跳转原帖做油猴元数据补充)
# 云主机无 MySQL (threads 表在 108), 帖子链接以入库时保存的真实 URL 为主, 模板兜底
try:
    _cc = sqlite3.connect(LOG_DB)
    _cols = [r[1] for r in _cc.execute('PRAGMA table_info(import_log)').fetchall()]
    if 'thread_url' not in _cols:
        _cc.execute('ALTER TABLE import_log ADD COLUMN thread_url TEXT')
        _cc.commit()
    # 2026-09-19: avdb adopt 任务记录 115 源目录 (服务重启后按原目录续跑)
    if 'src_115' not in _cols:
        _cc.execute('ALTER TABLE import_log ADD COLUMN src_115 TEXT')
        _cc.commit()
    # 2026-09-19: 任务来源 (''=色花堂网页/磁力, 'avdb'=avdb 下载联动) —— 任务监控页按来源区分
    if 'origin' not in _cols:
        _cc.execute("ALTER TABLE import_log ADD COLUMN origin TEXT DEFAULT ''")
        # 一次性回填历史 adopt 任务: adopt 的 thread_id 是番号/目录名(非数字), 且带 src_115
        _cc.execute("UPDATE import_log SET origin='avdb' WHERE COALESCE(origin,'')='' "
                    "AND src_115 IS NOT NULL AND src_115<>'' "
                    "AND (thread_id IS NULL OR thread_id NOT GLOB '[0-9]*')")
        _cc.commit()
        log.info('import_log 回填历史 avdb 来源任务: %d 条', _cc.total_changes)
    _cc.close()
except Exception as _e:
    log.warning('import_log 迁移 thread_url 列失败: %s', str(_e)[:100])

def _thread_url_of(thread_id, saved_url=''):
    """帖子链接: 入库保存的真实 URL 优先, 否则按色花堂模板兜底 (thread-{tid}-1-1.html)
    非数字 thread_id (avdb adopt 用番号目录名当 task 标识) 不生成假链接。"""
    tid = str(thread_id or '').strip()
    if saved_url and str(saved_url).startswith('http'):
        return str(saved_url)
    if tid.isdigit():
        return f'https://sehuatang.net/thread-{tid}-1-1.html'
    return ''

# 115 扫码全局登录(可选; Linux 版不强制, 缺 p115client 时 stub)
try:
    import global_login_115
    LOGIN_STATE = global_login_115.LOGIN_STATE
    QR_PNG = global_login_115.QR_PNG
except Exception as _e:
    LOGIN_STATE = {'status': 'unavailable', 'msg': f'login module unavailable: {_e}', 'updated_at': ''}
    QR_PNG = ''

# 2026-08-20: 架构已完全不用 MySQL (threads/links 表在 108), 入库所需字段(thread_id/title/thread_url/magnet/kind)
# 均由油猴脚本从网页端提交; 帖子链接以入库保存的真实 URL 为主, 模板兜底
APP_ID = 100197651
UID = '<115_UID>'

SMARTSTRM_WEBHOOK = 'http://127.0.0.1:8024/webhook/<WEBHOOK_TOKEN>'   # 本地版不再使用 (strm 由 mcp115 直接生成)
SMARTSTRM_TASK = 'emby'
SMARTSTRM_TASK_TV = 'tv'   # 剧集任务 (storage_path=/sehuatang_tv -> 本地 /strm/tv)
# ===== 媒体服务器适配 (2026-09-16): Emby / Jellyfin 通用 + 多台广播 =====
# 为什么「一套代码能触发两种服务器」:
#   Emby 的 REST 路由原生就是 /emby/<控制器>/<方法>, Jellyfin 是 Emby 3.5.2 的 fork,
#   路由由控制器反射生成, 早期版本连 /emby 这个旧前缀一起保留 (新版可能已废弃该前缀,
#   所以不赌它 —— 见下面的 404 回退)。两台真正对得上的其实是「接口名」:
#   Library/Refresh、Items、Items/{id}/PlaybackInfo 三边同名同义。
#   实测差异只有三处, 全部在适配层内消化, 调用点看到的仍是同一个 media_refresh():
#     ① 令牌不通用 —— 各自后台「高级 → API 密钥」生成, 所以 key 是按服务器存的, 不是全局一个
#     ② PlaybackInfo: 部分 Emby 版本强制要 UserId, Jellyfin 不要 (传了也不报错) -> 按家族决定
#     ③ 家族识别: /System/Info/Public 的 ProductName="Jellyfin Server" 即 Jellyfin;
#        Emby 4.10 干脆不返回该字段 -> 字段缺失判 Emby
#        顺带把「前缀」也学了: 先试 /emby/System/Info/Public, 404/405 就改用无前缀版 ——
#        于是所有路径都只写接口名 (/Library/Refresh、/Items、/Users), 由 _media_path() 拼前缀,
#        对接新版 Jellyfin(已删 /emby 旧前缀)时不会再有一处漏改
#   另外加一层路由兜底: 万一某台既不认 /emby 前缀(404/405)也不认家族探测结论,
#   自动改打无前缀 /Library/Refresh, 并把「哪条路走通了」记住, 下次直接走。
#
# 多台同时触发 (用户 2026-09-16): MEDIA_SERVERS="url1|token1,url2|token2" 逗号/分号分隔;
#   未配置时退回单机 MEDIA_SERVER_URL/EMBY_URL + MEDIA_SERVER_TOKEN/EMBY_TOKEN。
#   扫库是广播 (逐台 POST, 任一台 204 即算成功), 其余读操作(Items/PlaybackInfo)走第一台。
_MEDIA_DEFAULT_URL = 'http://172.25.224.1:8096'
_MEDIA_DEFAULT_TOKEN = '08ba96f52aaa4b8cbb262ffdf1102b54'   # Emby 后台「高级→API 密钥」生成的专用 key
_MEDIA_ENV_FILE = '/etc/default/sehuatang-import'           # 实际生效值写这里 (systemd EnvironmentFile)


def _load_media_env_file(path=_MEDIA_ENV_FILE):
    """读 systemd 用的环境文件, 让命令行 `--media-check` 和服务进程看到同一套配置。
    只填 os.environ 里没有的键, 真实环境变量优先级更高。"""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k in ('MEDIA_SERVERS', 'MEDIA_SERVER_URL', 'MEDIA_SERVER_TOKEN',
                             'EMBY_URL', 'EMBY_TOKEN') and not os.environ.get(k):
                        os.environ[k] = v
    except Exception as e:
        log.debug('[media] 读 %s 失败: %s', path, str(e)[:80])


_load_media_env_file()


def _parse_media_servers():
    """解析 MEDIA_SERVERS -> [{'url','token','flavor','user_id','at','route','name'}, ...]"""
    raw = (os.environ.get('MEDIA_SERVERS') or '').strip()
    out = []
    for part in re.split(r'[;,\n]+', raw):
        part = part.strip()
        if not part:
            continue
        url, tok = (part.split('|', 1) + [''])[:2] if '|' in part else (part, '')
        url = url.strip().rstrip('/')
        if not url:
            continue
        out.append({'url': url, 'token': tok.strip() or _MEDIA_DEFAULT_TOKEN,
                    'flavor': '', 'prefix': None, 'user_id': '', 'at': 0.0, 'route': '', 'name': url})
    if not out:
        out.append({'url': (os.environ.get('MEDIA_SERVER_URL') or os.environ.get('EMBY_URL')
                            or _MEDIA_DEFAULT_URL).rstrip('/'),
                    'token': (os.environ.get('MEDIA_SERVER_TOKEN') or os.environ.get('EMBY_TOKEN')
                              or _MEDIA_DEFAULT_TOKEN),
                    'flavor': '', 'prefix': None, 'user_id': '', 'at': 0.0, 'route': '', 'name': 'primary'})
    return out


MEDIA_SERVERS = _parse_media_servers()
MEDIA_PRIMARY = MEDIA_SERVERS[0]
MEDIA_SERVER_URL = MEDIA_PRIMARY['url']       # 单机兼容/其它位置仍按这两个变量读
MEDIA_SERVER_TOKEN = MEDIA_PRIMARY['token']
EMBY_URL = MEDIA_SERVER_URL
EMBY_TOKEN = MEDIA_SERVER_TOKEN
MEDIA_FLAVOR_TTL = 600            # 家族探测缓存 10 分钟, 避免每个任务打一次 Info
MEDIA_CLIENT = 'sehuatang-import'
MEDIA_LAST_REFRESH = []           # [(name, code/ERR), ...] 最近一次广播结果 (自检页可读)


def _srv(srv=None):
    return srv or MEDIA_PRIMARY


def _media_headers(srv=None):
    """三种认证头一次写全: Emby 认 X-Emby-Token / X-Emby-Authorization, Jellyfin 三个都认
    (外加调用点统一携带的 api_key 查询参数, 两服务器都支持)。"""
    tok = _srv(srv)['token']
    return {
        'Content-Type': 'application/json',
        'X-Emby-Token': tok,
        'X-MediaBrowser-Token': tok,
        'X-Emby-Authorization': (f'MediaBrowser Token="{tok}", Client="{MEDIA_CLIENT}", '
                                 f'Device="WSL", DeviceId="{MEDIA_CLIENT}", Version="1.0"'),
    }


def _media_url(path, srv=None, **params):
    """拼完整 URL, 自动带 api_key"""
    s = _srv(srv)
    q = {'api_key': s['token']}
    q.update({k: v for k, v in params.items() if v not in (None, '')})
    return s['url'] + path + '?' + urllib.parse.urlencode(q)


def _media_req(path, data=None, method='GET', srv=None, **params):
    return urllib.request.Request(_media_url(path, srv=srv, **params), data=data,
                                  headers=_media_headers(srv), method=method)


def media_server_flavor(force=False, srv=None):
    """'jellyfin' / 'emby' / ''(探测失败)。按服务器缓存, 探测失败沿用上次结果。"""
    s = _srv(srv)
    now = time.time()
    if not force and s['flavor'] and s.get('prefix') is not None and now - s['at'] < MEDIA_FLAVOR_TTL:
        return s['flavor']
    # 前缀与家族一次探完: 先试带 /emby 前缀 (Emby 与老 Jellyfin 都认), 404/405 再试无前缀
    # (新版 Jellyfin 可能已删掉 /emby 这个旧前缀) —— 探到哪个就把它记下来, 后续所有路径复用。
    for path, pfx in (('/emby/System/Info/Public', '/emby'), ('/System/Info/Public', '')):
        try:
            with urllib.request.urlopen(urllib.request.Request(s['url'] + path),
                                        timeout=8, context=ctx) as r:
                d = json.loads(r.read().decode('utf-8', 'replace'))
        except Exception as e:
            log.debug('[media] 家族探测 %s%s 失败: %s', s['name'], path, str(e)[:80])
            continue
        prod = str(d.get('ProductName') or '').strip()
        low = (prod or json.dumps(d, ensure_ascii=False)).lower()
        s['flavor'] = 'jellyfin' if 'jellyfin' in low else 'emby'
        s['prefix'] = pfx
        s['at'] = now
        break
    return s['flavor']


def _media_prefix(srv=None):
    """这台服务器认的路径前缀: '/emby' 或 ''(无前缀)。未探测过就探一次。"""
    s = _srv(srv)
    if s.get('prefix') is None:
        media_server_flavor(force=True, srv=s)
    return s.get('prefix') or ''


def _media_path(path, srv=None):
    """把「接口名」按这台的实际情况拼成完整路径 —— 前缀由 _media_prefix() 决定,
    所以调用点只写 /Library/Refresh、/Items 这种与服务器无关的名字。"""
    pfx = _media_prefix(srv)
    if pfx:
        return path if path.startswith(pfx + '/') else pfx + path
    if path.startswith('/emby/'):
        return path[len('/emby'):]
    return path


def media_user_id(srv=None):
    """取一个可用 UserId (Emby 的 PlaybackInfo 有时强制要求)。取不到返回 ''。"""
    s = _srv(srv)
    if s['user_id']:
        return s['user_id']
    try:
        with urllib.request.urlopen(_media_req(_media_path('/Users', srv=s), srv=s),
                                    timeout=10, context=ctx) as r:
            users = json.loads(r.read().decode('utf-8', 'replace'))
        if isinstance(users, list) and users:
            s['user_id'] = users[0].get('Id') or ''
            log.info('[media] %s 取到 UserId=%s (%s)', s['name'], s['user_id'][:8], users[0].get('Name'))
    except Exception as e:
        log.warning('[media] %s 取 UserId 失败: %s', s['name'], str(e)[:100])
    return s['user_id']


def _media_refresh_one(s, timeout=30):
    """单台扫库: 上次成功的路由优先, 否则 /emby/Library/Refresh -> (404/405) /Library/Refresh。"""
    last = None
    pfx = _media_prefix(s)
    paths, seen = [], set()
    for p in (s['route'], pfx + '/Library/Refresh' if pfx else '/Library/Refresh',
              '/emby/Library/Refresh', '/Library/Refresh'):
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    for path in paths:
        try:
            with urllib.request.urlopen(_media_req(path, data=b'', method='POST', srv=s),
                                        timeout=timeout, context=ctx) as resp:
                if s['route'] != path:
                    log.info('[media] %s 扫库路由 = %s (HTTP %s)', s['name'], path, resp.status)
                s['route'] = path
                return resp.status
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 405):     # 不认这条路由, 换下一条
                continue
            return e.code                 # 401/500 等: 路由对, 是别的问题, 不再换路
        except Exception as e:
            last = e
            return 'ERR:%s' % str(e)[:60]
    raise last or RuntimeError('无可用扫库路由')


def media_refresh(timeout=30):
    """触发扫库 (广播到 MEDIA_SERVERS 里每一台)。任一台成功(204)即返回 204。
    全失败时抛最后一个异常 —— 与改造前单台行为一致, 调用点不用改。"""
    global MEDIA_LAST_REFRESH
    codes, last = [], None
    for s in MEDIA_SERVERS:
        try:
            codes.append((s['name'], _media_refresh_one(s, timeout=timeout)))
        except Exception as e:
            last = e
            codes.append((s['name'], 'ERR:%s' % str(e)[:60]))
    MEDIA_LAST_REFRESH = codes
    if any(c == 204 for _, c in codes):
        log.info('[media] 扫库广播 OK: %s', codes)
        return 204
    log.warning('[media] 扫库广播失败: %s', codes)
    raise last or RuntimeError('扫库广播失败: %s' % (codes,))


def media_playback_url(item_id, srv=None):
    """PlaybackInfo URL: Emby 带 UserId (部分版本强制), Jellyfin 不带。"""
    uid = '' if media_server_flavor(srv=srv) == 'jellyfin' else media_user_id(srv=srv)
    return _media_url(_media_path(f'/Items/{item_id}/PlaybackInfo', srv=srv),
                      srv=srv, IsPlayback='true', UserId=uid)


def media_server_status(probe=False):
    """给自检/监控页用: 每台的家族、UserId、扫库返回码。probe=True 时才真的打网。"""
    rows = []
    for s in MEDIA_SERVERS:
        row = {'name': s['name'], 'url': s['url'],
               'token': s['token'][:4] + '...' + s['token'][-4:],
               'flavor': media_server_flavor(force=probe, srv=s) or '?',
               'prefix': _media_prefix(s) or '(无)',
               'user_id': s['user_id'] or ''}
        if probe:
            try:
                row['refresh'] = _media_refresh_one(s, timeout=15)
            except urllib.error.HTTPError as e:
                row['refresh'] = e.code
            except Exception as e:
                row['refresh'] = 'ERR:%s' % str(e)[:60]
        row['route'] = s['route'] or '(未探测)'
        rows.append(row)
    return rows
# 本地 strm 根目录 (MDCng watch: 容器 /media/待看/sehuatang <-> G:\srtm\待看\sehuatang)
LOCAL_STRM_ROOT = '/mnt/g/srtm/待看/sehuatang'
# MDCng 刮削输出目录 (watch 待看/sehuatang -> target /media/已刮削/AV)
MDC_TARGET_ROOT = '/mnt/g/srtm/已刮削/AV'

# ===== 入库分类 (2026-09-06): 推送时选分类, 本地 strm 与 MDC 刮削输出按分类分流 =====
# key -> (本地 strm watch 目录, MDC 刮削输出目录=已刮削/<分类>, 显示名)
# 与用户手工创建的 G:\srtm\已刮削\<分类> 一一对应; MDCng watch_dirs 每分类一条,
# 刮削完成后自动落入对应 已刮削/<分类>。默认 av = 旧行为(全部进 AV), 行为不变可回退。
CATEGORY_MAP = {
    'av':   ('/mnt/g/srtm/待看/sehuatang',       '/mnt/g/srtm/已刮削/AV',       'AV'),
    'fc2':  ('/mnt/g/srtm/待看/sehuatang_fc2',   '/mnt/g/srtm/已刮削/FC2',      'FC2'),
    'sw':   ('/mnt/g/srtm/待看/sehuatang_sw',    '/mnt/g/srtm/已刮削/丝袜',     '丝袜'),
    'cn':   ('/mnt/g/srtm/待看/sehuatang_cn',    '/mnt/g/srtm/已刮削/国产自拍', '国产自拍'),
    'ea':   ('/mnt/g/srtm/待看/sehuatang_ea',    '/mnt/g/srtm/已刮削/欧美',     '欧美'),
    'lf':   ('/mnt/g/srtm/待看/sehuatang_lf',    '/mnt/g/srtm/已刮削/里番',     '里番'),
}
DEFAULT_CATEGORY = 'av'
# 分类 key -> 合法值 (api 校验 / 恢复路径使用)
CATEGORY_KEYS = tuple(CATEGORY_MAP.keys())

def norm_category(cat):
    """归一化分类: 空/非法 -> 默认 av; 合法 key 原样返回 (大小写不敏感, 容忍首尾空白)"""
    if cat is None:
        return DEFAULT_CATEGORY
    c = str(cat).strip().lower()
    if c in CATEGORY_MAP:
        return c
    return DEFAULT_CATEGORY

def category_strm_root(cat):
    """分类 -> 本地 strm watch 根目录"""
    return CATEGORY_MAP.get(cat, CATEGORY_MAP[DEFAULT_CATEGORY])[0]

def category_target_root(cat):
    """分类 -> MDC 刮削输出根目录 (已刮削/<分类>)"""
    return CATEGORY_MAP.get(cat, CATEGORY_MAP[DEFAULT_CATEGORY])[1]

def category_name(cat):
    """分类 -> 显示名"""
    return CATEGORY_MAP.get(cat, CATEGORY_MAP[DEFAULT_CATEGORY])[2]

def _thread_115_path(thread_id, category=None):
    """影片的 115 落点: 2026-09-06 起按分类建目录 /sehuatang/<分类名>/thread_xxx;
    未指定分类/默认 av -> /sehuatang/AV/thread_xxx (写函数统一 add_task push 与断点检查)"""
    return f'{IMPORT_ROOT}/{category_name(category)}/thread_{thread_id}'

def category_of_local_dir(local_dir):
    """本地 strm 目录 -> 所属分类 key (按前缀匹配 watch 根), 默认 av"""
    for key, (sroot, _t, _n) in CATEGORY_MAP.items():
        if local_dir == sroot or local_dir.startswith(sroot.rstrip('/') + '/'):
            return key
    return DEFAULT_CATEGORY
FS115_ROOT = '/opt/media/clouddrive2/mnt/115open/115open'   # CD2 挂载的 115 根目录(本机)
# 2026-08-14 方案: 115 只存视频, 元数据不上传 115 (减少 115 访问规避风控)
# 默认 0 (新方案: scrape 直写本地); 设 META_UPLOAD_115=1 可回滚旧行为(上传115+a_task全扫同步)
META_UPLOAD_115 = os.environ.get('META_UPLOAD_115', '0') == '1'
SCRAPE_TMP_DIR = os.path.join(BASE, 'tmp_scrape')   # scrape 直写缓存目录 (per-thread: tmp_scrape/thread_{tid}/)
IMPORT_ROOT = '/sehuatang'
IMPORT_TV_ROOT = '/sehuatang_tv'   # 剧集媒体库目录(115): 超多视频的 thread 整个移动到此处
MEDIA_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.m4v', '.ts', '.wmv', '.rmvb', '.rm', '.webm'}
# 用户规则(2026-08-07): 只保留 >500MB 的视频, 其余文件(广告/小视频/rar/html等)全部删除
KEEP_VIDEO_MIN = 500 * 1024 * 1024

# ============================== 115 操作 (CD2: 挂载文件系统 + gRPC 离线推送) ==============================
class Push115:
    """建目录: CD2 挂载文件系统(fs115); 离线推送: CD2 gRPC AddOfflineFiles; 不再依赖 open token/网页 cookie"""
    def __init__(self):
        self._fs = None
        self._cd2 = None

    def _fs_client(self):
        from fs115 import FS115
        if self._fs is None:
            self._fs = FS115()
        # 2026-08-19: CD2 auth(GetToken)可能卡 60s+, 后台线程预热避免首个 wait 撞上; 不阻塞
        try:
            threading.Thread(target=lambda: self._cd2_client(), daemon=True).start()
        except Exception:
            pass
        return self._fs

    def _cd2_client(self):
        from cd2grpc import CD2Client
        if self._cd2 is None:
            self._cd2 = CD2Client()
            self._cd2.auth()
        return self._cd2

    # ---- 文件操作 (CD2 挂载文件系统, 无需 fid) ----
    def ensure_dir(self, path):
        # 云主机补丁: 用 CD2 create_folder 递归建目录 (与 add_offline 同侧, FS115 mkdir 建的目录 CD2 缓存不可见)
        try:
            cd2 = self._cd2_client()
            parts = [pp for pp in path.split('/') if pp]
            cur = MOUNT_PREFIX
            for pp in parts:
                cd2.create_folder(cur, pp)
                cur += '/' + pp
            return True
        except Exception as e:
            log.warning('ensure_dir(cd2) %s 失败: %s', path, str(e)[:120])
            return False

    def list_dir(self, path, maxdepth=3, use_cache=True, use_cd2=False):
        """递归列出, 返回 [(relpath, name, None, size, is_dir)]（兼容旧调用方, fid 位为 None）
        use_cache=False 强制真实读(用于 wait_video/断点检查, 避免缓存滞后漏新文件）
        use_cd2=True: 落地检测走 CD2 gRPC API(GetSubFiles force_refresh), 避开 FUSE Errno 131 (2026-08-19)"""
        if use_cd2:
            try:
                cd2 = self._cd2_client()
                items = cd2.walk_files(MOUNT_PREFIX + path, maxdepth=maxdepth, force_refresh=True)
                return [(rel, name, None, size, is_dir) for rel, name, is_dir, size in items]
            except Exception as e:
                log.warning('list_dir(cd2) %s 失败, 回退 FUSE: %s', path, str(e)[:120])
        try:
            items = self._fs_client().walk(path, maxdepth=maxdepth, use_cache=use_cache)
        except Exception as e:
            log.warning('list_dir %s 失败: %s', path, str(e)[:120])
            return []
        return [(rel, name, None, size, is_dir) for rel, name, is_dir, size in items]

    # ---- 离线推送 (CD2 gRPC, 按路径推送无需 fid; 支持 magnet 与 ed2k) ----
    @staticmethod
    def link_hash(link):
        """提取链接的唯一 hash: magnet → btih; ed2k → 文件hash(32位hex)。提取不到返回 ''"""
        if not link:
            return ''
        m = re.search(r'btih:([0-9a-fA-F]{32,40})', link)
        if m:
            return m.group(1).lower()
        m = re.search(r'\|([0-9a-fA-F]{32})\|', link)  # ed2k://|file|name|size|hash|/
        if m:
            return m.group(1).lower()
        return ''

    def add_task(self, link, savepath):
        try:
            resp = self._cd2_client().add_offline(link, '/115open' + savepath)
            return resp.success, resp.errorMessage or ''
        except Exception as e:
            return False, str(e)[:200]

    def push_magnet(self, link, savepath):
        # 规范化: 磁力一律按 btih 重建标准链接, 裁掉 "🎬 入库"/&dn= 等粘贴杂质 (2026-08-28)
        if link and 'magnet' in link.lower():
            h = self.link_hash(link)
            if h:
                link = f'magnet:?xt=urn:btih:{h}'
        try:
            ok_dir = self.ensure_dir(savepath)
            if not ok_dir:
                return False, '建目录失败(CD2 挂载)', None
            ok, msg = self.add_task(link, savepath)
            if ok:
                # 新增链接后同步本地缓存 (用户规则: 新增 ed2k/磁力要对应更新缓存)
                _fs_cache_sync(savepath, tag='push 后')
                return True, 'ok', None
            # "任务已存在" = 之前推送过 → 自动删除旧任务并重新推送
            # (用户规则: 遇到任务重复不要跳过, 直接删旧任务重推, 保证文件落地/清理/刮削全流程重走)
            if '任务已存在' in msg or '10008' in msg or '重复' in msg or 'already' in msg.lower():
                h = self.link_hash(link)
                if h:
                    try:
                        rm = self._cd2_client().remove_offline([h], delete_files=False)
                        log.info('任务重复 → 已删除旧任务 %s: %s', h, getattr(rm, 'success', rm))
                        time.sleep(3)
                        ok2, msg2 = self.add_task(link, savepath)
                        if ok2:
                            return True, 'deleted-old-and-repushed', None
                        return False, f'删除旧任务后重推仍失败: {msg2}', None
                    except Exception as e:
                        return False, f'删除旧任务失败: {str(e)[:150]}', None
                return False, msg, None
            return False, msg, None
        except Exception as e:
            return False, str(e)[:200], None

    def wait_video(self, savepath, timeout=600, stable_rounds=3, interval=10, hashes=None):
        """等待视频落地, 且视频数量连续 stable_rounds 轮(每轮 interval 秒)不再变化才返回,
        确保所有文件(含正片)下载完成, 避免只检测到广告就先清理而误删正在下载的大文件。
        限流优化(2026-08-13): 轮询间隔 5s→10s; list_dir 失败(限频/超时)不抛异常, 降级等待重试。
        2026-08-19: hashes 提供时优先查 CD2 离线任务状态(全部 status=2 FINISHED 即落地),
        免每轮递归 force_refresh 列目录(慢+费 115 API+限频时空返回); 查不到再回退目录轮询。"""
        t0 = time.time()
        # ---- 快路径: CD2 离线任务状态 ----
        if hashes:
            hs = [h.lower() for h in hashes if h]
            cd2_done = False
            cd2_missing = False
            qerr = 0
            while time.time() - t0 < timeout:
                try:
                    cd2 = self._cd2_client()
                    done = True
                    missing = []
                    for h in hs:
                        st = cd2.get_offline_status(h, max_pages=1)
                        if st is None:
                            missing.append(h)
                        elif st[0] != 2:  # OFFLINE_FINISHED
                            done = False
                    if done and not missing:
                        log.info('[wait] CD2 离线任务全部 FINISHED(%d 条), 列目录确认', len(hs))
                        cd2_done = True
                        break
                    if missing:
                        log.info('[wait] %d 个任务查不到(老任务/已移除), 回退目录轮询: %s', len(missing), missing[:2])
                        cd2_missing = True
                        break
                except Exception as e:
                    qerr += 1
                    if qerr >= 3:
                        log.warning('[wait] CD2 任务状态查询连续失败 %d 次, 回退目录轮询: %s', qerr, str(e)[:100])
                        break
                    log.warning('[wait] CD2 任务状态查询失败(%d/3), %ds 后重试: %s', qerr, interval, str(e)[:100])
                    time.sleep(interval)
                    continue
                # 2026-08-19: 降频避免打爆 115 API (21:01/21:20 曾触发服务端超时)
                log.info('[wait] CD2 离线任务下载中, %ds 后重查', interval * 2)
                time.sleep(interval * 2)
            if cd2_done and not cd2_missing:
                try:
                    items = self.list_dir(savepath, maxdepth=4, use_cache=False, use_cd2=True)
                    videos = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
                    dirs = [it for it in items if it[4]]
                    if videos:
                        log.info('[wait] CD2 确认落地: %d 视频 (耗时 %ds)', len(videos), int(time.time() - t0))
                        return [v[0] for v in videos], dirs
                    log.warning('[wait] CD2 任务 FINISHED 但目录未见视频, 回退轮询等待缓存同步')
                except Exception as e:
                    log.warning('[wait] CD2 确认列目录失败, 回退轮询: %s', str(e)[:100])
        # ---- 原逻辑: 目录轮询 ----
        last_n = -1
        last_videos = []
        last_dirs = []
        stable = 0
        while time.time() - t0 < timeout:
            try:
                items = self.list_dir(savepath, maxdepth=4, use_cache=False, use_cd2=True)
            except Exception as e:
                log.warning('[wait] list_dir %s 失败(可能限频), 降级等待: %s', savepath, str(e)[:120])
                time.sleep(interval + 5)
                continue
            videos = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
            dirs = [it for it in items if it[4]]
            n = len(videos)
            if n > 0:
                if n != last_n:
                    log.info('[wait] %ds: %d 视频, %d 子目录', int(time.time() - t0), n, len(dirs))
                    stable = 0
                else:
                    stable += 1
                last_n = n
                last_videos = videos
                last_dirs = dirs
                if stable >= stable_rounds:
                    log.info('[wait] %ds: 视频数稳定(%d), 返回', int(time.time() - t0), n)
                    return [v[0] for v in last_videos], last_dirs
            elif last_n > 0:
                # 2026-08-19 修复: CD2 列目录抖动(DEADLINE_EXCEEDED/INTERNAL)返回空但文件已落地,
                # 此前已见过视频时不因单次空列目录重置 stable(否则 3→0→3→0 抖动永不稳定)
                log.warning('[wait] %ds: 列目录返回空但此前已见 %d 视频, 视为列目录抖动, 保留上次结果(stable=%d)',
                            int(time.time() - t0), last_n, stable)
            time.sleep(interval)
        # 2026-08-19: 超时也返回上次所见视频, 防止列目录抖动导致丢失已落地文件
        if last_videos:
            log.warning('[wait] 等待落地超时, 但此前已见 %d 视频, 返回上次结果', len(last_videos))
            return [v[0] for v in last_videos], last_dirs
        return [], []

    def wait_download_settle(self, savepath, timeout=300, stable_rounds=2, interval=8, hashes=None):
        """等待下载真正完成: 目录内所有文件大小总和连续 stable_rounds 轮不再增长。
        防止正片还在下载(大小<500M)时被清理误删。返回 True 表示已稳定, False 表示超时。
        2026-08-19: hashes 提供时优先查 CD2 离线任务状态, 全部 FINISHED 即返回 True(免大小轮询)。"""
        t0 = time.time()
        if hashes:
            hs = [h.lower() for h in hashes if h]
            try:
                cd2 = self._cd2_client()
                done = True
                missing = []
                for h in hs:
                    st = cd2.get_offline_status(h, max_pages=1)
                    if st is None:
                        missing.append(h)
                    elif st[0] != 2:
                        done = False
                if done and not missing:
                    log.info('[settle] CD2 离线任务全部 FINISHED, 无需大小稳定轮询')
                    return True
                if missing:
                    log.info('[settle] %d 个任务查不到, 回退大小稳定轮询', len(missing))
            except Exception as e:
                log.warning('[settle] CD2 任务状态查询失败, 回退大小稳定轮询: %s', str(e)[:100])
        prev_total = -1
        stable = 0
        while time.time() - t0 < timeout:
            try:
                items = self.list_dir(savepath, maxdepth=4, use_cache=False, use_cd2=True)
                files = [it for it in items if not it[4]]
                total = sum(it[3] or 0 for it in files)
                n = len(files)
                if total == 0 and prev_total > 0:
                    # 2026-08-19 修复: 列目录抖动返回空但文件已存在, 不重置稳定计数
                    log.warning('[settle] %ds: 列目录返回空但此前总大小 %.0fMB, 视为抖动, 保留上次结果',
                                int(time.time() - t0), prev_total / 1024 / 1024)
                elif total != prev_total:
                    log.info('[settle] %ds: %d 文件, 总大小 %.0fMB (增长中/变化中)',
                             int(time.time() - t0), n, total / 1024 / 1024)
                    prev_total = total
                    stable = 0
                else:
                    stable += 1
                    if stable >= stable_rounds:
                        log.info('[settle] %ds: 文件大小稳定 (%.0fMB, %d 文件)', int(time.time() - t0),
                                 total / 1024 / 1024, n)
                        return True
            except Exception as e:
                log.warning('[settle] walk %s 失败: %s', savepath, str(e)[:120])
            time.sleep(interval)
        log.warning('[settle] 等待下载稳定超时 %ds, 继续执行', timeout)
        return False

    def _delete_with_retry(self, remote, retries=3, delay=1.5):
        """FUSE 删除失败(Errno 131 瞬时抖动)重试 3 次。
        2026-08-19 根因: SNOS-332 广告删除失败不重试 → 广告残留 115 + 生成垃圾 strm/Emby 条目"""
        for i in range(retries):
            try:
                if self._fs_client().delete(remote):
                    return True
            except Exception:
                pass
            time.sleep(delay)
        return self._fs_client().delete(remote)

    def clean_keep_large_videos(self, savepath, keep_min=KEEP_VIDEO_MIN):
        """用户规则(2026-08-07): 离线下载完成后, 只保留 >keep_min 的视频 + 元数据(nfo/标准命名图片/字幕),
        其余文件(广告小视频/广告图片/rar/html/txt等)全部删除。返回 (保留列表, 删除列表)"""
        meta_exts = {'.nfo', '.srt', '.ass', '.ssa', '.idx', '.sub'}
        # 仅保留 Emby 标准命名图片 (poster/fanart/thumb 等); 广告图(如 安卓二维码.png)不属于元数据 → 删除
        meta_imgs = {'poster', 'fanart', 'thumb', 'folder', 'backdrop', 'landscape', 'logo', 'banner', 'clearart', 'disc', 'keyart', 'tvshow'}
        kept, removed = [], []
        try:
            items = self._fs_client().walk(savepath, maxdepth=4)
        except Exception as e:
            log.warning('clean walk %s 失败: %s', savepath, str(e)[:120])
            return [], []
        for rel, name, is_dir, size in items:
            if is_dir:
                continue
            full = f'{savepath}/{rel}' if rel else f'{savepath}/{name}'
            stem, ext = os.path.splitext(name.lower())
            if ext in MEDIA_EXTS and size and size >= keep_min:
                kept.append((rel, size))
                continue
            if ext in meta_exts:
                kept.append((rel, size))
                continue
            if ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif') and stem in meta_imgs:
                kept.append((rel, size))
                continue
            ok = self._delete_with_retry(full)
            log.info('[clean] %s (%sMB, %s) -> %s', full,
                     f'{size/1024/1024:.1f}' if size else '?', ext or '(no-ext)', 'DEL' if ok else 'FAIL')
            if ok:
                removed.append(rel)
            time.sleep(0.5)
        return kept, removed

def _fs_cache_sync(savepath, tag=''):
    """同步 115 目录缓存 (新增链接后调用)。返回 True=真做了同步, False=本环境无缓存(no-op)。

    2026-09-22 修: 本地版 fs115._cache() 永远返回 None —— 这是刻意的降级占位
    (见 fs115.py 顶部「本环境无 fs_cache 模块, 安全降级为无缓存」), 不是配置错误。
    但老写法 `_fs_cache().sync_path(savepath)` 没判空, 每次都抛 AttributeError,
    被 except 吞成一条 warning「push 后缓存同步失败: 'NoneType' object has no attribute
    'sync_path'」(实测 2026-09-22 20:32 批量入库时刷了 7+ 条, 掩盖真故障)。
    这里显式判空: 无缓存安静跳过, 只有真异常才告警。
    """
    try:
        from fs115 import _cache as _fs_cache
    except Exception as e:
        log.warning('%s缓存模块导入失败: %s', tag, str(e)[:100])
        return False
    c = _fs_cache()
    if c is None:
        return False
    try:
        c.sync_path(savepath)
        return True
    except Exception as e:
        log.warning('%s缓存同步失败: %s', tag, str(e)[:100])
        return False


# ============================== 任务记录 ==============================
def init_log_db():
    c = sqlite3.connect(LOG_DB)
    c.execute('''CREATE TABLE IF NOT EXISTS import_log(
        task_id TEXT PRIMARY KEY, thread_id TEXT, magnet TEXT, title TEXT,
        status TEXT, step TEXT, msg TEXT, kind TEXT, category TEXT, origin TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')))''')
    c.execute('''CREATE TABLE IF NOT EXISTS import_log_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, status TEXT, step TEXT,
        msg TEXT, ts TEXT DEFAULT (datetime('now','localtime')))''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_hist_task ON import_log_history(task_id)')
    # 老库迁移: 补 kind 列 (2026-08-13 断点重续需要恢复 剧集/影片 流程)
    try:
        c.execute('ALTER TABLE import_log ADD COLUMN kind TEXT')
    except Exception:
        pass
    # 老库迁移: 补 category 列 (2026-09-06 推送选分类; 旧任务留空=AV 默认)
    try:
        c.execute('ALTER TABLE import_log ADD COLUMN category TEXT')
    except Exception:
        pass
    c.commit(); c.close()

def save_task(task_id, **kw):
    c = sqlite3.connect(LOG_DB)
    cols = ', '.join(kw.keys())
    ph = ', '.join(['?'] * len(kw))
    # UPSERT: 只更新传入列, 保留其他列 (2026-08-13: 断点重续复用 task_id 时不覆盖 kind 等历史字段)
    sql = (f'INSERT INTO import_log({cols}, task_id, updated_at) VALUES({ph}, ?, datetime(\'now\',\'localtime\')) '
           f'ON CONFLICT(task_id) DO UPDATE SET '
           f'{", ".join(f"{k}=excluded.{k}" for k in kw)}, updated_at=datetime(\'now\',\'localtime\')')
    c.execute(sql, list(kw.values()) + [task_id])
    try:
        c.execute('INSERT INTO import_log_history(task_id, status, step, msg) VALUES(?,?,?,?)',
                  (task_id, kw.get('status', ''), kw.get('step', ''), (kw.get('msg') or '')[:400]))
    except Exception:
        pass
    c.commit(); c.close()
    # 库存徽章缓存失效 (2026-09-21): 入库成功 → 让面板上该番号的 ❓ 立刻翻 ✅ (不等 TTL)。
    # 只在该任务的番号上失效, 不做全清 (全清会让下次打开帖子页重新把 115 打满)。
    if kw.get('status') == 'done' and _INV_CACHE:
        _inv_invalidate(task_id)

def get_task(task_id):
    c = sqlite3.connect(LOG_DB)
    cols = [d[1] for d in c.execute('PRAGMA table_info(import_log)').fetchall()]
    r = c.execute('SELECT * FROM import_log WHERE task_id=?', (task_id,)).fetchone()
    c.close()
    if not r:
        return None
    return dict(zip(cols, r))

# ============================== SmartStrm / Emby 触发 ==============================
def _smartstrm_task(savepath):
    """按 115 路径选择 SmartStrm 任务: /sehuatang_tv/* -> tv 任务, 其余 -> emby 任务"""
    if (savepath or '').startswith(IMPORT_TV_ROOT + '/'):
        return SMARTSTRM_TASK_TV
    return SMARTSTRM_TASK

def _trigger_smartstrm(savepath, category=None):
    """本地版: 用 115 MCP pickcode 直接生成 strm (指向 115-Desktop 302 服务), 替代 SmartStrm webhook。
    2026-09-06: category 决定 strm 落地到 待看/sehuatang_<分类>/ (分类分流, MDC 刮削后进对应已刮削/<分类>)。
    category 未显式传入时按已有本地目录自动探测 (rescrape 等存量调用点正确回落到原分类)。"""
    try:
        from mcp115 import MCP115
        m = MCP115()
        if category is None:
            name = savepath.rstrip('/').split('/')[-1]
            for key, (sroot, _t, _n) in CATEGORY_MAP.items():
                if os.path.isdir(os.path.join(sroot, name)):
                    category = key
                    break
        n = m.gen_strm(savepath, category_strm_root(norm_category(category)),
                       drop_cat_dir=(category_strm_root(norm_category(category)) != LOCAL_STRM_ROOT))
        log.info('[strm] 本地生成 strm %d 个: %s (category=%s)', n, savepath, norm_category(category))
        return {'generated': n}
    except Exception as e:
        log.warning('[strm] 本地生成 strm 失败: %s', str(e)[:150])
        return {'generated': 0}

def _trigger_smartstrm_sync(savepath):
    """本地版: 115 附加文件(nfo/图片)无需同步到本地 (MDCng/油猴负责元数据), noop"""
    return {'synced': []}

# ---- 增量 strm 触发 (2026-08-12, 被后续部署覆盖后恢复) ----
# 同一 thread 下多个磁力 → 多个子文件夹, 每次新磁力入库若对整个 thread 目录触发
# SmartStrm 会全量递归扫描全部子目录 (115 API 调用多/易限频, 已生成 strm 也重复重建)。
# 改为: 以"一级单元"(子目录名 / 根视频文件名) 为粒度, 只对【本地尚未生成 strm】的单元触发。

def _strm_done_units(local_dir):
    """返回本地 strm 目录下已生成 strm 的单元集合:
    子目录名(该子目录内存在 .strm) 或 ''(根目录存在 .strm)。"""
    done = set()
    if not os.path.isdir(local_dir):
        return done
    try:
        for name in os.listdir(local_dir):
            p = os.path.join(local_dir, name)
            if os.path.isdir(p):
                try:
                    if any(f.lower().endswith('.strm') for f in os.listdir(p)):
                        done.add(name)
                except OSError:
                    pass
            elif name.lower().endswith('.strm'):
                done.add('')
    except OSError as e:
        log.warning('_strm_done_units 读取 %s 失败: %s', local_dir, str(e)[:100])
    return done

def _video_units(video_paths):
    """由视频路径列表(相对 savepath)计算涉及的一级单元集合:
    'FC2PPV-xxx/xx.mp4' -> 'FC2PPV-xxx';  'xx.mp4'(直接落在根) -> ''(根单元)
    2026-09-10 加固: 容忍传入 list_dir 原始条目 (rel, name, None, size, is_dir) 或 JSON 化后的
    list 形态 —— 曾因重扫路径直接透传 tuple 导致单元名变成 "('BMW-345', False, ...)" 拼接进
    115 路径 (thread_3102110/BWM-345 未生成 strm, 整单未入库事故)。
    2026-09-20 修: 根级文件(无 '/')归一成 '' —— 与 _strm_done_units 对根单元的表示一致。
    旧写法对根文件返回文件名本身, 与 done 集合里的 '' 永远不相等, 于是每个根级视频
    (正片 + 广告小视频 + 已被清理掉但仍留在 videos 里的广告) 都被当成独立单元去
    gen_strm(文件路径) -> 恒返回 0 个, 每次入库白跑 N 次 115 往返 (N=根文件数)。"""
    units = set()
    for vp in video_paths:
        if isinstance(vp, (tuple, list)):
            vp = vp[0] if vp else ''
        if vp is None:
            continue
        s = str(vp).replace('\\', '/').strip('/')
        if not s:
            continue
        seg = s.split('/')[0]
        units.add('' if seg == s else seg)
    return units

def _unit_path(savepath, unit):
    """单元 -> 115 路径。根单元('') 就是 savepath 本身(不要再拼 '/')。"""
    base = savepath.rstrip('/')
    return base if unit == '' else base + '/' + unit

def _smartstrm_new_units(savepath, video_paths, category=None):
    """计算需要触发 SmartStrm 的新单元列表(子目录路径或根视频文件路径)。
    视频列表为空(限频/未检测到) → 返回 [], 不兜底整个目录, 避免加重 115 限频。"""
    if not video_paths:
        log.warning('[strm] 视频列表为空, 跳过增量触发(避免整个 thread 全扫): %s', savepath)
        return []
    local_dir = _local_strm_dir(savepath, category)
    done = _strm_done_units(local_dir)
    units = _video_units(video_paths)
    new_units = sorted(units - done)
    if not new_units:
        log.info('[strm] 无需触发: %s 的所有视频单元已生成过 strm (done=%d)', savepath, len(done))
    return new_units

def _trigger_smartstrm_sync_units(savepath, units):
    """对指定单元逐个触发 a_task 同步(copy_ext), 避免整个 thread 全扫。
    单元为空(全部已生成) → 不触发, 返回空结果。"""
    if not units:
        return {'synced': []}
    results = []
    for u in units:
        sub = _unit_path(savepath, u)
        r = _trigger_smartstrm_sync(sub)
        results.append({'unit': u, 'resp': r})
        time.sleep(1)
    return {'synced': results}

def _shorten_long_names(p, remote_dir, limit=200):
    """重命名 115 目录下超长文件名(>limit 字节)为短名, 返回重命名数量。

    原因: SmartStrm 写本地 strm/nfo 时受 ext4 单文件名 255 字节限制,
    超长文件名报 Errno 36 (Filename too long) 导致 strm/nfo 写不进去,
    表现为云端有元数据但 Emby 无条目。在触发 SmartStrm 前先重命名缩短。"""
    fs = p._fs_client()
    try:
        items = fs.listdir(remote_dir)
    except Exception as e:
        log.warning('listdir %s 失败: %s', remote_dir, str(e)[:100])
        return 0
    cnt = 0
    for name, is_dir, _size in items:
        if is_dir:
            continue
        b = len(name.encode('utf-8'))
        if b <= limit:
            continue
        # 2026-08-21: 先去全部 www.98t.la@ 广告标识 (不区分大小写, 含 98t.la@ 变体), 再截断
        new = re.sub(r'(?:www\.)?98t\.la@', '', name, flags=re.I)
        ext = os.path.splitext(new)[1]
        stem = new[:-len(ext)] if ext else new
        # 按字符截断, 保证 stem 字节 <= 110 (留扩展名余量, 总名 < 200B)
        chars = list(stem)
        cut = len(chars)
        while cut > 10 and len(''.join(chars[:cut]).encode('utf-8')) > 110:
            cut -= 1
        stem = ''.join(chars[:cut])
        new = stem + ext
        if new == name:
            continue
        try:
            ok = fs.rename(f'{remote_dir}/{name}', f'{remote_dir}/{new}')
            if ok:
                cnt += 1
                log.info('超长文件名重命名(%dB->%dB): %s...', b, len(new.encode('utf-8')), new[:40])
        except Exception as e:
            log.warning('重命名失败 %s...: %s', name[:40], str(e)[:100])
    if cnt:
        log.info('[import] 超长文件名重命名 %d 个 (dir=%s)', cnt, remote_dir)
    return cnt

def _warmup_strms(savepath, wait=90, concurrency=3, timeout=20):
    '''起播预热: 对本 thread 新生成的 strm URL 发 Range 请求(前1MB),
    触发 SmartStrm 302 fid_mode -> 115 拉流, 让 SmartStrm downurl 缓存 + 115 CDN 热,
    改善首次起播体验(用户规则 2026-08-14: 新入库视频均做一次预热, 在库不预热)。
    发现 strm 文件: 影片 emby/thread_id/**(目录名即 thread_id); 剧集标题目录
    按最近修改 + URL 内容含 thread_id 匹配。失败不影响入库。返回 dict(total/ok/fail/elapsed)'''
    import glob
    import subprocess
    from concurrent import futures
    thread_name = (savepath or '').rstrip('/').split('/')[-1]
    t0 = time.time()
    if not thread_name:
        return {'total': 0, 'ok': 0, 'fail': 0, 'elapsed': 0}
    # ---- 发现本 thread 的 strm 文件 ----
    files = []
    deadline = time.time() + wait
    while time.time() < deadline:
        # 快速路径: 影片 emby/thread_id/**(子目录) (目录名即 thread_id)
        files = glob.glob(f'/opt/media/strm/emby/{thread_name}/**/*.strm', recursive=True)
        if files:
            break
        # 剧集标题目录: emby_tv 下 strm 量小(数十个), 直接 glob 全部 + URL 内容含 thread_id
        try:
            files = []
            for p_ in glob.glob('/opt/media/strm/emby_tv/**/*.strm', recursive=True):
                try:
                    if thread_name in open(p_, encoding='utf-8', errors='replace').read(400):
                        files.append(p_)
                except Exception:
                    pass
            if files:
                break
        except Exception:
            pass
        time.sleep(3)
    if not files:
        log.warning('[warmup] %s 等待 %ds 未发现本地 strm, 跳过预热', thread_name, int(wait))
        return {'total': 0, 'ok': 0, 'fail': 0, 'elapsed': int(time.time() - t0)}
    urls = []
    for f in files[:80]:
        try:
            u = open(f, encoding='utf-8', errors='replace').read().strip()
            if u.startswith('http'):
                urls.append(u)
        except Exception:
            pass
    if not urls:
        log.warning('[warmup] %s 无有效 strm URL(%d 个文件), 跳过预热', thread_name, len(files))
        return {'total': 0, 'ok': 0, 'fail': 0, 'elapsed': int(time.time() - t0)}

    def _one(u):
        try:
            r = subprocess.run(['curl', '-skL', '-o', '/dev/null', '-r', '0-1048575',
                                '--max-time', str(timeout), '-w', '%{http_code}', u],
                               capture_output=True, text=True, timeout=timeout + 5)
            code = (r.stdout or '').strip()
            return code in ('206', '200')
        except Exception:
            return False

    with futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(_one, urls))
    ok = sum(1 for x in results if x)
    return {'total': len(urls), 'ok': ok, 'fail': len(urls) - ok, 'elapsed': int(time.time() - t0)}
def _ensure_dir_nfo(local_dir):
    """Emby 电影识别需要【目录同名 nfo】才把目录当电影单元并分配 poster.jpg。
    2026-08-19 根因: SNOS-332 只有 strm 同名 nfo(xxx.(mp4).nfo) → Emby 无图;
    WAAA-679 有目录同名 nfo(WAAA-679-C.nfo) → 有图。缺则从 strm 同名 nfo 复制补写。
    返回是否补写"""
    if not os.path.isdir(local_dir):
        return False
    base = os.path.basename(local_dir.rstrip('/'))
    dir_nfo = os.path.join(local_dir, base + '.nfo')
    if os.path.exists(dir_nfo):
        return False
    for fn in sorted(os.listdir(local_dir)):
        if re.search(r'\.\(\w+\)\.nfo$', fn):   # xxx.(mp4).nfo
            try:
                shutil.copy2(os.path.join(local_dir, fn), dir_nfo)
                log.info('[nfo] 补写目录同名 nfo: %s (来自 %s)', dir_nfo, fn)
                return True
            except Exception as e:
                log.warning('[nfo] 补写目录同名 nfo 失败 %s: %s', dir_nfo, str(e)[:100])
                return False
    return False

def _local_strm_dir(savepath, category=None):
    """115 路径 -> 本地 strm 目录 (路径规则与 mcp115.gen_strm 的 drop_cat_dir 完全一致):
      /sehuatang/AV/thread_xxx   -> 待看/sehuatang/AV/thread_xxx      (av: 本地根镜像整个 115 /sehuatang, 保留分类层)
      /sehuatang/FC2/thread_xxx  -> 待看/sehuatang_fc2/thread_xxx     (其他分类: 本地根已按分类专用, 剥掉分类层)
      /sehuatang_tv/thread_xxx   -> 待看/sehuatang/thread_xxx         (剧集, 同 av 根, 无分类层)
    category 未显式传入时按已有本地目录自动探测 (rescrape/prewarm 等存量调用点正确回落到原分类)。"""
    if category is None:
        name = savepath.rstrip('/').split('/')[-1]
        for key, (sroot, _t, cname) in CATEGORY_MAP.items():
            if os.path.isdir(os.path.join(sroot, name)) or \
               os.path.isdir(os.path.join(sroot, cname, name)):
                category = key
                break
    cat = norm_category(category)
    root = category_strm_root(cat)
    rel = savepath.strip('/')
    if rel.startswith('sehuatang_tv/'):
        rel = rel[len('sehuatang_tv/'):]
    elif rel.startswith('sehuatang/'):
        rel = rel[len('sehuatang/'):]
        # 本地根 == av 通用根(待看/sehuatang) 时保留分类层(镜像 115 /sehuatang 结构);
        # 专用根(待看/sehuatang_<分类>)时剥掉 <分类名>/ 一层, 避免 sehuatang_lf/里番/ 重复嵌套
        if root != LOCAL_STRM_ROOT and '/' in rel:
            rel = rel.split('/', 1)[1]
    return os.path.join(root, rel)

def _has_local_metadata(local_dir):
    """本地 strm 目录是否已有 nfo/图片 (MDCng 原地整理产物 / SmartStrm 同步产物)"""
    nfo = img = 0
    if not os.path.isdir(local_dir):
        return False, 0, 0
    for root, _, files in os.walk(local_dir):
        for fn in files:
            fl = fn.lower()
            if fl.endswith('.nfo'):
                nfo += 1
            elif fl.endswith(('.jpg', '.jpeg', '.png')):
                img += 1
    return (nfo > 0 and img > 0), nfo, img

MDC_NFO_RE = re.compile(r'\((?:mp4|MP4)\)')

def _detect_mdc_nfo(local_dir):
    """检测本地 nfo 是否为 MDCng 刮削产物: MDCng 的 <title> 会把文件名+扩展名原样写入
    (如 'WWW.98T.LA@xxx.(MP4) WWW-003 ...'), 含 '(MP4)' 特征; 网页爬取 nfo 的 title 是帖子标题(无此特征)"""
    if not os.path.isdir(local_dir):
        return False
    for fn in os.listdir(local_dir):
        if not fn.lower().endswith('.nfo'):
            continue
        try:
            with open(os.path.join(local_dir, fn), 'r', encoding='utf-8', errors='replace') as fh:
                head = fh.read(3000)
            m = re.search(r'<title>(.*?)</title>', head, re.S)
            if m and MDC_NFO_RE.search(m.group(1)):
                return True
        except Exception:
            continue
    return False

def _overwrite_local_meta(savepath, thread_id):
    """用网页刮削产物 覆盖写本地 strm 目录的 MDCng 残留 nfo/图片 (不删除本地文件,
    避免 MDCng watcher 检测到 nfo 缺失而重新刮削覆盖)。
    优先: 本地 scrape 缓存 (tmp_scrape/thread_{tid}/, 2026-08-14 方案);
    兼容: 115 上仍有的 nfo/图片 (旧模式/存量未清理)。
    nfo 文件名对齐本地 strm 命名 (xxx.(mp4).nfo), 保证 Emby 按同名匹配读取。
    返回是否覆盖了至少一个文件"""
    local_dir = _local_strm_dir(savepath)
    if not os.path.isdir(local_dir):
        log.warning('overwrite_local_meta: 本地目录缺失 %s', local_dir)
        return False

    # 1. 本地 scrape 缓存优先 (2026-08-14 方案: scrape 直写本地, 缓存保留在 tmp_scrape)
    cache_dir = os.path.join(SCRAPE_TMP_DIR, f'thread_{thread_id}')
    if os.path.isdir(cache_dir):
        n = 0
        strm_names = [f for f in os.listdir(local_dir) if f.endswith('.strm')]
        for fn in sorted(os.listdir(cache_dir)):
            if not fn.lower().endswith(('.nfo', '.jpg', '.jpeg', '.png')):
                continue
            src = os.path.join(cache_dir, fn)
            if not os.path.isfile(src):
                continue
            dst_name = fn
            if fn.lower().endswith('.nfo'):
                # nfo 对齐本地 strm 命名 (movie.nfo / xxx.nfo -> xxx.(mp4).nfo), 保证 Emby 同名匹配
                for sn in strm_names:
                    base = sn[:-5]                      # 去 .strm
                    m = re.search(r'\.\((\w+)\)$', base)
                    if m:
                        stem = base[:m.start()]         # xxx.(mp4).strm -> xxx
                        dst_name = stem + '.(%s).nfo' % m.group(1)
                        break
            try:
                shutil.copy2(src, os.path.join(local_dir, dst_name))
                n += 1
            except Exception as e:
                log.warning('[overwrite] 缓存复制失败 %s: %s', fn, str(e)[:100])
        if n:
            log.info('[import] 用本地 scrape 缓存覆盖 %d 个文件: %s', n, cache_dir)
            return True

    # 2. 兼容: 115 上仍有的 nfo/图片 (旧模式/存量未清理)
    remote_dir = FS115_ROOT + savepath          # /opt/.../115open/sehuatang/thread_x
    if not os.path.isdir(remote_dir):
        log.warning('overwrite_local_meta: 115 目录也缺失 %s, 无法覆盖', remote_dir)
        return False
    strm_names = [f for f in os.listdir(local_dir) if f.endswith('.strm')]
    ok = False
    for name in os.listdir(remote_dir):
        if not name.lower().endswith(('.nfo', '.jpg', '.jpeg', '.png')):
            continue
        src = os.path.join(remote_dir, name)
        if not os.path.isfile(src):
            continue
        dst_name = name
        if name.lower().endswith('.nfo'):
            for sn in strm_names:
                base = sn[:-5]                      # 去 .strm
                m = re.search(r'\.\((\w+)\)$', base)
                if m:
                    stem = base[:m.start()]         # xxx.(mp4).strm -> xxx
                    if name.startswith(stem + '.'):
                        dst_name = stem + '.(%s).nfo' % m.group(1)
                        break
        try:
            import shutil as _sh
            _sh.copy2(src, os.path.join(local_dir, dst_name))
            log.info('[import] 覆盖本地元数据: %s -> %s', name, dst_name)
            ok = True
        except Exception as e:
            log.warning('覆盖本地元数据失败 %s: %s', name, str(e)[:120])
    return ok

def _upload_local_meta_to_115(p, local_dir, savepath):
    """MDCng 原地刮削产物(本地 nfo/标准命名图片) 上传到 115 对应目录, 保持相对路径一致。
    只上传 nfo + Emby 标准命名图片(poster/fanart/thumb等), 广告图不上传。
    返回上传成功文件数"""
    n = 0
    if not os.path.isdir(local_dir):
        return 0
    meta_imgs = {'poster', 'fanart', 'thumb', 'folder', 'backdrop', 'landscape', 'logo', 'banner', 'clearart', 'disc', 'keyart', 'tvshow'}
    fs = p._fs_client()
    for root, _, files in os.walk(local_dir):
        for fn in files:
            fl = fn.lower()
            stem, ext = os.path.splitext(fl)
            if fl.endswith('.nfo'):
                pass
            elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif') and stem in meta_imgs:
                pass
            else:
                continue
            rel = os.path.relpath(root, local_dir)
            remote_dir = f'{savepath}/{rel}' if rel != '.' else savepath
            local_path = os.path.join(root, fn)
            if fs.upload(local_path, remote_dir, fn):
                n += 1
                log.info('[upload-meta] %s -> %s/%s', local_path, remote_dir, fn)
            time.sleep(0.3)   # 限流优化(2026-08-13): 上传节流, 避免一次刮削 100+ 文件瞬间打满配额
    return n

# ===== 本地版 MDCng 等待 (2026-08-27): MDCng 把 strm 从 待看/sehuatang 刮削后
# 移动到 已刮削/AV/<系列>/<番号>/, 轮询目标区找 含 strm+nfo+图片 且匹配番号的新目录。
_FANHAO_RE = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,8}-?\d{2,6})(?![A-Za-z0-9])')

def _fanhao_key(x):
    """番号归一化 (2026-09-19): 本地文件名抠出的番号与 MDCng 目录名写法常不一致,
    裸子串比对必然失配 → 白等 300s + 误报"刮削失败"。
    例: VRKM01741 / VRKM-01741 → VRKM1741;  SAVR00721 / SAVR-721 → SAVR721。"""
    x = re.sub(r'[^A-Z0-9]', '', (x or '').upper())
    return re.sub(r'([A-Z])0+(\d)', r'\1\2', x)

def _extract_fanhao_candidates(local_dir):
    """从本地 strm 文件名提取番号候选 (HUNTC-094 / MKBD-S127 / FC2PPV-123456)"""
    cands = set()
    if not os.path.isdir(local_dir):
        return cands
    for dp, _dns, fns in os.walk(local_dir):
        for fn in fns:
            if not fn.lower().endswith('.strm'):
                continue
            s = os.path.splitext(fn)[0].upper()
            for m in _FANHAO_RE.finditer(s):
                cands.add(m.group(1))
    return cands

# 手动入库补番号 (2026-09-23): 手填磁力/ed2k 没有帖子上下文, 标题只能写"手动入库",
# 监控页里一排"手动入库"没法辨认。用 115 落地文件名抠番号回填标题。
# 文件名常见前缀杂质: www.98T.la@ / 489155.com@ / hhd800.com@ / [javdb.com] / [98t.tv]
_FANHAO_STRIP_PREFIX_RE = re.compile(r'^(?:\[[^\]]*\]|[^@\[\]]*@)+')
_FANHAO_DIGIT_PREFIX_RE = re.compile(r'(?<![A-Z0-9])(\d{2,6}[A-Z]{2,10}-?\d{2,6})(?![A-Z0-9])')
_FANHAO_FC2_RE = re.compile(r'(FC2[-_]?PPV[-_]?\d{3,7})')

def _fanhao_from_names(names):
    """从 115 落地文件名抠番号 (2026-09-23)。支持三种写法:
    LUXU-907 / VRKM01741 (纯字母前缀) · 259LUXU-907 (数字前缀) · FC2PPV-123456"""
    for n in names or []:
        base = os.path.basename(str(n))
        base = os.path.splitext(base)[0]
        base = _FANHAO_STRIP_PREFIX_RE.sub('', base).upper()
        m = (_FANHAO_RE.search(base) or _FANHAO_DIGIT_PREFIX_RE.search(base)
             or _FANHAO_FC2_RE.search(base))
        if m:
            return m.group(1).upper()
    return ''

def _find_mdc_output(cands, recent_min=20, root=None):
    """在 MDC_TARGET_ROOT 下找已刮削完成的目录 (strm+nfo+图片齐全, 目录名含番号候选 或 近期创建)。
    2026-09-06: root 可按分类传入 (已刮削/<分类>), 默认 AV。"""
    root = root or MDC_TARGET_ROOT
    if not os.path.isdir(root):
        return False, ''
    now = time.time()
    for dp, dns, fns in os.walk(root):
        if dp == root:
            continue
        if dp[len(root):].count(os.sep) > 4:
            dns[:] = []
            continue
        has_strm = any(f.lower().endswith('.strm') for f in fns)
        has_nfo = any(f.lower().endswith('.nfo') for f in fns)
        has_img = any(f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) for f in fns)
        if not (has_strm and has_nfo and has_img):
            continue
        base = os.path.basename(dp).upper()
        try:
            mt = os.path.getmtime(dp)
        except Exception:
            mt = 0
        if cands and any(_fanhao_key(c) in _fanhao_key(base) for c in cands):
            return True, dp
        if not cands and now - mt < recent_min * 60:
            return True, dp
    return False, ''

_MDC_RETRY_PREFIX = '.mdc-retry-'

# 2026-09-20: 「待补元数据」判据 (done 但没走到 scan = MDC 没刮全, 等油猴补)
# 附带的 NOT EXISTS: 同一帖 (thread_id) 若已有更新的成功任务 (done/scan), 旧记录不再计入
# —— 面板行内「🔁 重刮」成功后清单自动收敛, 无需手工清理。
PENDING_MD_SQL = """(status='done' AND step<>'scan' AND NOT EXISTS (
    SELECT 1 FROM import_log n WHERE n.thread_id=import_log.thread_id AND n.status='done'
      AND n.step='scan' AND n.created_at>import_log.created_at))"""

def _retry_token(n=None):
    """把整数编码成【纯字母】字符串 (base26)。重触发文件名的唯一性后缀必须不含数字。

    2026-09-20 修复: 旧实现用 int(time.time()) 十进制后缀, MDCng 番号解析器里 91 系规则
    `(?i)91[a-z]{0,}-?(\\d{3,})` 没有左边界约束, epoch '1789911528' 中段 '91'+'1528'
    被当成番号 91-1528, 真番号 SNOS-403 被顶掉 → 重触发任务全部秒挂
    (mdc_ng.db task 1128/1129/1130: stage=200 元数据校验失败,缺少'番号', 各 5.0s)。
    同类污染还有 825 'RETRY-1789384841'。改纯字母后 MDC 仍能解析出原番号
    (实测 task 1131: …SNOS-403.mdc-retry-fuqwlri.strm → 番号 SNOS-403, 22.22s 成功)。
    用 time_ns() 而非秒级 epoch, 保证同秒内多次重触发也拿到不同文件名 (默认参数)。"""
    if n is None:
        n = time.time_ns()
    s = ''
    n = int(n)
    while n > 0:
        n, r = divmod(n, 26)
        s = chr(97 + r) + s
    return s or 'a'

def _trigger_mdc_retry(local_dir):
    """MDCng 监控器按 source_path 去重: 同名 strm 覆盖写不触发重新刮削 (2026-09-06 实测,
    08:59 失败任务同路径 16:59 覆盖写完全无反应; 删掉再重建同路径同样无反应)。复制一个含番号的
    唯一文件名 strm 强制触发监控器(模拟新增文件, 新路径必入队)。返回 redo 路径或 None。
    唯一性后缀必须是【纯字母】(_retry_token), 否则会被 MDCng 番号解析器读成番号 (2026-09-20 见上)。
    2026-09-10 修复: 递归查找 strm (此前只看 local_dir 顶层, 母带 strm 位于 thread_x/<unit>/
    子目录时永远找不到 → 重触发静默失效, task=17f8b55aa915 MKMP-668 卡满 300s 即此因)。"""
    try:
        if not os.path.isdir(local_dir):
            return None
        strms = []
        for root, _dirs, files in os.walk(local_dir):
            for f in files:
                if not f.lower().endswith('.strm'):
                    continue
                fp = os.path.join(root, f)
                # 清理本目录旧 redo 残留 (多任务共用目录时由调用方负责, 影片目录粒度足够)
                if _MDC_RETRY_PREFIX in f:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
                else:
                    strms.append(fp)
        if not strms:
            log.warning('[mdc-retry] %s 及其子目录下无 strm, 无法重触发', local_dir)
            return None
        src = sorted(strms)[0]
        stem, ext = os.path.splitext(os.path.basename(src))
        redo_name = f'{stem}{_MDC_RETRY_PREFIX}{_retry_token()}{ext}'
        redo_path = os.path.join(os.path.dirname(src), redo_name)
        with open(src, 'rb') as fsrc:
            data = fsrc.read()
        if not data:
            return None
        with open(redo_path, 'wb') as fdst:
            fdst.write(data)
        return redo_path
    except Exception as e:
        log.warning('[mdc-retry] 创建重触发文件失败: %s', str(e)[:100])
        return None

def _clean_mdc_retry(redo_paths):
    """删除重触发用 redo 文件 (MDCng 硬链接产物在目标区, 源头 redo 只是哨兵, 需清理)"""
    for p in redo_paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    redo_paths.clear()

def _wait_mdc_scrape(local_dir, timeout=300):
    """本地版: 等 MDCng 刮削完成。两种形态都覆盖, 返回 (ok, msg, mdc_dir):
    ① 原地刮削 (nfo/图片写进 local_dir) → mdc_dir=local_dir
    ② 移动式/硬链接整理 (刮削产物在 已刮削/<分类>/<系列>/<番号>/) → mdc_dir=目标区匹配目录
    mdc_dir 供后续完整性/中文标题检查使用 (硬链接模式本地待看区只有 strm, 元数据在目标区)。
    2026-09-06: 目标区按 local_dir 所属分类 (已刮削/<分类>) 扫描。
    2026-09-06 增强: 等待 60s 仍无刮削痕迹时自动创建唯一文件名 strm 强制触发 MDCng
    (同路径旧任务去重不重刮的场景自救), 触发文件成功后清理。"""
    cands = _extract_fanhao_candidates(local_dir)
    target_root = category_target_root(category_of_local_dir(local_dir))
    deadline = time.time() + timeout
    t0 = time.time()
    redo_paths = []
    retried = False
    try:
        while time.time() < deadline:
            # ① 原地刮削
            ok, n, i = _has_local_metadata(local_dir)
            if ok:
                _clean_mdc_retry(redo_paths)
                return True, f'MDCng 原地刮削完成: {n} nfo, {i} 图片', local_dir
            # ② 移动式: 目标区出现匹配番号的刮削结果
            ok2, found = _find_mdc_output(cands, root=target_root)
            if ok2:
                _clean_mdc_retry(redo_paths)
                return True, f'MDCng 刮削完成并移入: {found}', found
            # ③ 60s 仍无痕迹 → 强制重触发 (MDCng 对同路径旧文件去重)
            if not retried and time.time() - t0 > 60:
                rp = _trigger_mdc_retry(local_dir)
                if rp:
                    redo_paths.append(rp)
                    retried = True
                    log.warning('[mdc-retry] MDCng 60s 未处理 %s, 已创建重触发文件 %s', local_dir, rp)
            time.sleep(5)
    finally:
        _clean_mdc_retry(redo_paths)
    _, n, i = _has_local_metadata(local_dir)
    _ok2, found = _find_mdc_output(cands, root=target_root)
    extra = f'; 目标区未匹配' if not found else f'; 目标区: {found}'
    return False, f'MDCng 等待超时({timeout}s): 本地 {n} nfo / {i} 图片{extra} (候选番号: {", ".join(sorted(cands))[:120] or "无"})', None

def _mdc_pipeline_healthy(timeout=15):
    """本地版 MDCng 链路健康检查 (2026-08-27): ① mdc 容器运行 ② mdc API 9208 可达
    (本地无 SmartStrm, strm 由 mcp115 直接生成, 无卡死概念)"""
    try:
        # ① mdc 容器在运行
        r = subprocess.run(['docker', 'ps', '--filter', 'name=^mdc$', '--format', '{{.Names}}'],
                           capture_output=True, text=True, timeout=timeout)
        if not r.stdout.strip():
            return False, 'MDCng 容器未运行'
        # ② mdc API 9208 可达
        r = subprocess.run(['curl', '-s', '--max-time', '5', '-o', '/dev/null', '-w', '%{http_code}',
                            'http://127.0.0.1:9208/'],
                           capture_output=True, text=True, timeout=timeout)
        if r.stdout.strip() != '200':
            return False, f'MDCng API 9208 不可达 (http={r.stdout.strip() or "无响应"})'
        return True, '链路正常'
    except Exception as e:
        return False, f'健康检查异常: {str(e)[:100]}'

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')

def _mdc_title_has_chinese(local_dir):
    """检查 MDCng 刮削的 nfo 标题是否含中文字符。
    用户规则(2026-08-11): MDCng 刮削结果标题无中文字符 → 视为刮错/不准 → 判失败走网页兜底。
    返回 True=含中文(可信), False=无中文或无 nfo(不可信)"""
    if not os.path.isdir(local_dir):
        return False
    for root, _, files in os.walk(local_dir):
        for fn in files:
            if not fn.lower().endswith('.nfo'):
                continue
            try:
                with open(os.path.join(root, fn), 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                m = re.search(r'<title>(.*?)</title>', content, re.S)
                if m and _CJK_RE.search(m.group(1)):
                    return True
            except Exception:
                continue
    return False

def _remove_mdc_metadata(p, local_dir, savepath):
    """删除 MDCng 刮削的元数据: 本地 strm 目录 + 115 目录 (只删 nfo 和 Emby 标准命名图片,
    不动 strm/视频文件, 防止 SmartStrm 二次同步把错误元数据又 copy 回来)。返回删除文件数"""
    meta_imgs = {'poster', 'fanart', 'thumb', 'folder', 'backdrop', 'landscape', 'logo', 'banner', 'clearart', 'disc', 'keyart', 'tvshow'}
    n = 0

    def is_meta(fn):
        fl = fn.lower()
        stem, ext = os.path.splitext(fl)
        return fl.endswith('.nfo') or (ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif') and stem in meta_imgs)

    # 本地
    if os.path.isdir(local_dir):
        for root, _, files in os.walk(local_dir):
            for fn in files:
                if not is_meta(fn):
                    continue
                try:
                    os.remove(os.path.join(root, fn))
                    n += 1
                except Exception as e:
                    log.warning('[rm-mdc-meta] 本地删除失败 %s: %s', fn, str(e)[:100])
    # 115 (MDCng 成功后可能已上传过, 一并清掉)
    try:
        fs = p._fs_client()
        items = fs.walk(savepath, maxdepth=4)
        for rel, name, is_dir, size in items:
            if is_dir or not is_meta(name):
                continue
            full = f'{savepath}/{rel}' if rel else f'{savepath}/{name}'
            if fs.delete(full):
                n += 1
            time.sleep(0.4)
    except Exception as e:
        log.warning('[rm-mdc-meta] 115 清理失败: %s', str(e)[:120])
    return n

def _trigger_emby_scan():
    """扫库触发 (Emby/Jellyfin 通用, 见上方适配层)"""
    return media_refresh(timeout=30)

# ============================== 新入库预热 (2026-08-19) ==============================
# 用户规则: 新入库视频做一次预热(首播秒开), 存量影片不做预热改造。
# 机制: Emby 扫库后按本地 strm 目录找到新条目, 逐个 POST PlaybackInfo(IsPlayback=true)
#       → Emby 用修复后的 ffprobe(已注入 -tls_verify 0) 探测 strm URL → 媒体信息入缓存,
#         同时探测请求走 SmartStrm 302 链路, 顺带填热 downurl 缓存 → 首播 PlaybackInfo 秒回。
# 安全: 后台线程执行不阻塞入库队列; 探测间隔 60s(分钟级); 限频窗口自动跳过/中止;
#       单任务最多 PREWARM_MAX_ITEMS 条, 失败自动重试。
PREWARM_MAX_ITEMS = 10        # 单任务最多预热条数 (剧集多集截断, 其余首播时再探测)
PREWARM_SPACING_S = 60        # 探测间隔(秒) — 用户规则: 间隔分钟级
PREWARM_POLL_TIMEOUT_S = 180  # 等 Emby 索引到新条目的最长等待(秒)
PREWARM_PROBE_RETRY = 2       # 探测失败(空媒体信息)后的额外重试次数
PREWARM_ITEM_LOCK = threading.Lock()   # 全局串行化探测, 防并发打爆 115

def _media_path_variants(prefix):
    """同一路径的多种写法: Emby 跑在 Windows 上, Path 返回 G:\\srtm\\..., 而我们传的是
    /mnt/g/srtm/... —— 不归一化就一条都匹配不上。返回所有等价写法。"""
    norm = (prefix or '').replace('\\', '/').rstrip('/')
    out = {norm}
    m = re.match(r'^/mnt/([a-zA-Z])/(.*)$', norm)
    if m:
        drive, rest = m.group(1).upper(), m.group(2)
        out.add('%s:\\%s' % (drive, rest.replace('/', '\\')))
        out.add('%s:/%s' % (drive, rest))
    m2 = re.match(r'^([a-zA-Z]):[\\/](.*)$', norm)
    if m2:
        out.add('/mnt/%s/%s' % (m2.group(1).lower(), m2.group(2)))
    return [v.rstrip('/') for v in out if v]


def _emby_items_by_path_prefix(prefix, srv=None):
    """媒体服务器中 Path 以 prefix 开头的 Movie/Episode 条目 (全量查询 ~1.2MB, 本地过滤)。
    路径按 Windows/WSL 两种写法归一化后比较。"""
    url = _media_url(_media_path('/Items', srv=srv), srv=srv,
                     Recursive='true', IncludeItemTypes='Movie,Episode',
                     Fields='Path', Limit='5000')
    req = urllib.request.Request(url, headers=_media_headers(srv))
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        d = json.loads(resp.read().decode('utf-8', 'replace'))
    variants = _media_path_variants(prefix)
    hit = []
    for it in d.get('Items', []):
        p = (it.get('Path') or '').replace('\\', '/').rstrip('/')
        if any(p == v or p.startswith(v + '/') for v in variants):
            hit.append(it)
    return hit

def _prewarm_probe_one(item_id, path):
    """对单个条目 POST PlaybackInfo(IsPlayback=true), 触发媒体探测并写入缓存。
    返回 True 表示已拿到媒体信息(Container/流 非空)。"""
    url = media_playback_url(item_id)
    req = urllib.request.Request(url, data=b'{}', headers=_media_headers(), method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            d = json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        # Emby 若反过来抱怨 UserId (部分版本要求 UserId 为空) -> 去掉参数重试一次
        if e.code == 400 and 'UserId=' in url:
            url = _media_url(_media_path(f'/Items/{item_id}/PlaybackInfo'), IsPlayback='true')
            req = urllib.request.Request(url, data=b'{}', headers=_media_headers(), method='POST')
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                d = json.loads(resp.read().decode('utf-8', 'replace'))
        else:
            raise
    ms = (d.get('MediaSources') or [{}])[0]
    streams = ms.get('MediaStreams') or []
    ok = bool(ms.get('Container')) or any(s.get('Type') in ('Video', 'Audio') for s in streams)
    log.info('[prewarm] item %s %s: Container=%s streams=%d -> %s',
             item_id, (path or '')[-40:], ms.get('Container'), len(streams), 'OK' if ok else 'EMPTY')
    return ok

def _prewarm_items(local_dir, task_id, title):
    """本地版 (2026-08-27): Jellyfin 路径与云端 Emby 不同, 跳过 PlaybackInfo 预热。
    任务直接标记 done (扫库由 _trigger_emby_scan 完成)。"""
    label = (title or os.path.basename(local_dir.rstrip('/')))[:60]
    log.info('[prewarm] 本地模式跳过预热 (task=%s, %s)', task_id, label)
    save_task(task_id, status='done', step='scan',
              msg=f'入库完成: {label} (Jellyfin 扫库已触发, 本地跳过预热)',
              title=(title or '')[:300])
    return
def _start_prewarm(local_dir, task_id, title):
    """新入库预热入口: 后台线程执行, 不阻塞入库队列"""
    try:
        threading.Thread(target=_prewarm_items, args=(local_dir, task_id, title), daemon=True).start()
    except Exception as e:
        log.warning('[prewarm] 启动失败: %s', str(e)[:120])

# ============================== 一键入库 worker ==============================
def run_scrape_process(thread_id, keep_small=False, local_only=False, force=False):
    """本地版 (2026-08-27): 元数据由 MDCng 刮削 (或油猴 /api/metadata 补充), 跳过网页刮削。
    保留原返回契约 (rc, lines) 供调用方兼容。"""
    log.info('[scrape] 本地模式: 跳过网页刮削 (thread=%s), 元数据由 MDCng/油猴负责', thread_id)
    return 0, ['local: MDCng 负责刮削, 跳过 scrape_sehuatang']

def _run_import_dl(task_id, thread_id=None, magnet=None, title=None, thread_url=None, kind=None, resume=False, category=None):
    """离线下载阶段 (DL_QUEUE, 可并发 IMPORT_DL_CONCURRENT): 推磁力 → 等视频落地 → 等下载稳定。
    完成后写 DL_CTX 并转 POST 处理队列 (清理/刮削/strm 严格串行)。"""
    category = norm_category(category)
    p = Push115()
    try:
        # 从 thread_url 提取 thread_id (网页一键入库时 URL 必然含 thread-xxx 或 tid=xxx,
        # 保证浏览器兜底刮削可用; DB 里没有该帖也不影响网页磁力入库)
        if not thread_id and thread_url:
            m = re.search(r'thread-(\d+)', thread_url) or re.search(r'[?&]tid=(\d+)', thread_url)
            if m:
                thread_id = m.group(1)

        # 网页抓取的磁力/ed2k 链接: 支持多条, 用换行分隔 (magnet/ed2k URI 不含换行符)
        web_magnets = [m.strip() for m in re.split(r'\n+', magnet or '') if m.strip()]

        # 非番号 → 剧集 (用户规则 2026-08-12): 油猴选择非番号即直接归为剧集, 即使只有 1 个视频
        #   剧集: 推→等→重命名 S01E01..→浏览器刮削→SmartStrm tv 任务→Emby 剧集库
        # 番号 → 电影: 推→等→清小文件→MDC 刮削(失败浏览器兜底)→SmartStrm→Emby 影片库
        # 不再做多视频合并/建合集
        to_tv_mode = (kind == 'non_fanhao' and bool(thread_id))

        # 手动链接(磁力/ed2k): 2026-08-20 起不再反查 MySQL, 所有字段由油猴脚本从网页端提交

        save_task(task_id, status='running', step='push', msg='创建入库任务',
                  thread_id=str(thread_id or ''), title=(title or '')[:300])

        # 先确定 savepath (提前计算, 供断点重续检查使用;
        #   2026-08-17: 115 目录已有视频时无需磁力即可续跑, 避免磁力选择失败阻塞恢复)
        if thread_id:
            savepath = f'{IMPORT_TV_ROOT}/thread_{thread_id}' if to_tv_mode else _thread_115_path(thread_id, category)
        elif web_magnets:
            lh = Push115.link_hash(web_magnets[0])
            mn = f'manual_{lh[:8]}' if lh else f'manual_{str(abs(hash(web_magnets[0])))[:8]}'
            savepath = f'{IMPORT_ROOT}/{category_name(category)}/{mn}'
        else:
            save_task(task_id, status='failed', step='push', msg='缺少磁力/ed2k 链接',
                      thread_id=thread_id, title=title)
            return

        # 0. 断点重续 (2026-08-13): 仅对"限频失败后 resume 的任务"启用——
        #    若 115 目录已有视频(之前推送/落地成功), 跳过推送与等待, 从清理/刮削/strm 继续;
        #    全新任务(同 thread 新增链接等)即使目录已有视频也必须推送自己的磁力,
        #    否则后续链接的视频永远不会下载 (2026-08-13 修: 3685628 三链接只落地 1 个视频)
        #    提前到磁力选择之前 (2026-08-17 修: 原逻辑磁力选择失败会直接 return,
        #    即使已有视频也无法断点续跑, 见 2156138/2708729)
        skip_push = False
        resume_videos = None
        try:
            _items0 = p.list_dir(savepath, maxdepth=4, use_cache=False)
            resume_videos = [it for it in _items0 if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
        except Exception as e:
            log.warning('[import] 断点检查失败(按新任务处理): %s', str(e)[:120])
        if resume and resume_videos:
            skip_push = True
            log.info('[import] %s 已有 %d 个视频, 断点重续: 跳过推送/等待', savepath, len(resume_videos))

        # 磁力选择 (2026-08-20 去 MySQL: 磁力/标题/链接均由油猴网页端提交, 不再依赖 threads/links 表)
        if thread_id:
            if not web_magnets and not skip_push:
                save_task(task_id, status='failed', step='push', msg='未提供磁力/ed2k 链接(请用油猴脚本从帖子页提交入库)',
                          thread_id=thread_id, title=title)
                return
            title = title or f'thread_{thread_id}'
            magnet_list = web_magnets
        else:
            magnet_list = web_magnets
            title = title or '手动入库'

        # 1. 推送 115
        if skip_push:
            save_task(task_id, status='running', step='wait',
                      msg=f'断点重续: 115 已有 {len(resume_videos)} 个视频, 跳过推送/等待',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            pushed = len(resume_videos or [])
        else:
            save_task(task_id, status='running', step='push', msg=f'推送 {len(magnet_list)} 条链接到 115',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            pushed = 0
            for m in magnet_list:
                ok, msg, fid = p.push_magnet(m, savepath)
                if ok:
                    pushed += 1
                else:
                    log.warning('push fail %s: %s', m[:50], msg)
                time.sleep(2)
            if pushed == 0:
                if _ratelimit_active():
                    save_task(task_id, status='failed', step='push',
                              msg='115 限频中推送失败, 限频恢复后监控将自动断点续传',
                              thread_id=str(thread_id or ''), title=(title or '')[:300])
                else:
                    save_task(task_id, status='failed', step='push', msg='115 推送失败(全部失败), 请检查凭证/网络',
                              thread_id=str(thread_id or ''), title=(title or '')[:300])
                return
            save_task(task_id, status='running', step='wait', msg=f'115 已接收 {pushed} 条, 等待文件落地',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])

        # 2. 等待视频文件落地 (最多 10 分钟, 每轮打印进度)
        if skip_push:
            videos, dirs = resume_videos, []
            hash_list = []
        else:
            hash_list = [Push115.link_hash(m) for m in magnet_list]
            videos, dirs = p.wait_video(savepath, timeout=600, hashes=hash_list)

        # 2.25 手动入库补番号 (2026-09-23): 无帖上下文时标题只有"手动入库", 监控页无从辨认。
        #      用刚落地视频的文件名抠番号回填 title -> 后续 msg 里的 label 也随带番号。
        if videos and (title or '').strip() in ('', '手动入库'):
            _fh = _fanhao_from_names(videos)
            if _fh:
                title = _fh
                log.info('[import] 手动入库补番号: %s (task=%s, %d 个视频)', _fh, task_id, len(videos))

        # 2.3 等下载大小稳定 (剧集/电影都要; 防正片未下完被误删/改名; 2026-08-15 提前到下载阶段)
        try:
            p.wait_download_settle(savepath, timeout=300, hashes=hash_list)
        except Exception as e:
            log.warning('等待下载稳定失败(继续): %s', e)

        # 2.4 未下载到视频: 直接失败, 不再空转后续处理 (2026-08-20 修: 3704481 无视频仍报成功)
        #    可能磁力无速度/下载极慢, 10 分钟超时未落地。用户可去 115 网盘确认视频落地后,
        #    在任务详情点「✅ 手工确认继续」, 系统跳过推送/等待从处理阶段续跑。
        if not videos:
            DL_CTX[task_id] = {'savepath': savepath, 'title': title, 'kind': kind,
                               'to_tv_mode': to_tv_mode, 'videos': [], 'category': category}
            save_task(task_id, status='failed', step='wait_video',
                      msg='离线下载超时(10分钟)未检测到视频文件，可能磁力慢/无速度。请到 115 网盘确认视频是否落地，确认后点任务详情「✅ 手工确认继续」',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            return

        # 离线下载完成 → 记录上下文, 转 POST 处理队列 (严格串行)
        DL_CTX[task_id] = {'savepath': savepath, 'title': title, 'kind': kind,
                           'to_tv_mode': to_tv_mode, 'videos': videos, 'category': category}
        save_task(task_id, status='running', step='wait',
                  msg=f'离线下载完成({len(videos)} 个视频), 排队等待处理(串行)...',
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        _submit_post(_run_import_post, (task_id, thread_id, magnet, title, thread_url, kind, category))
    except Exception as e:
        log.exception('import dl failed')
        save_task(task_id, status='failed', step='error', msg='离线下载异常: ' + str(e)[:300],
                  thread_id=str(thread_id or ''), title=(title or '')[:300])

def _run_import_post(task_id, thread_id=None, magnet=None, title=None, thread_url=None, kind=None, category=None):
    """处理阶段 (POST_QUEUE, 并发 POST_CONCURRENT=3; 2026-09-19 前为严格串行 1 worker):
    剧集化/清理/文件名/strm/刮削/元数据/预热/Emby 扫库。
    离线下载阶段(DL_QUEUE)完成后自动转此队列; 服务重启恢复处理阶段任务时 DL_CTX 可能缺失,
    此时用 _find_thread_path 重新定位 115 目录并重新扫描视频。"""
    category = norm_category(category)
    ctx = DL_CTX.pop(task_id, {})
    # adopt 任务(有 src_115): 落点就是 avdb 原目录 (2026-09-20 起不再迁移),
    # 服务重启后 DL_CTX 丢失时按 src_115 续跑, 比 _find_thread_path 猜目录可靠
    savepath = (ctx.get('savepath') or (get_task(task_id) or {}).get('src_115')
                or _find_thread_path(str(thread_id or '')))
    if not savepath:
        save_task(task_id, status='failed', step='error',
                  msg='无法定位 115 目录(manual 任务重启后无法恢复), 请重新提交',
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        return
    to_tv_mode = ctx.get('to_tv_mode', (kind == 'non_fanhao' and bool(thread_id)))
    videos = ctx.get('videos') or []
    # category: 优先上下文 (新任务), 否则从 DB 恢复 (服务重启后恢复任务用, 2026-09-06)
    category = norm_category(ctx.get('category') or category)
    # 与 _run_import_dl 保持一致的解析: 后续判断 (thread_id or web_magnets) 需要该变量
    web_magnets = [m.strip() for m in re.split(r'\n+', magnet or '') if m.strip()]
    p = Push115()
    try:
        # 2.4 非番号 → 剧集: 直接走剧集流程 (重命名 → 浏览器刮削 → SmartStrm tv 任务 → Emby 剧集库)
        # 剧集不清理小文件/不走 MDC/不合并; 下载稳定已在 DL 阶段完成
        if to_tv_mode:
            # 2026-08-20 用户规则: 剧集入库只进行到"等待任务落地", 后续(重命名S01E/生成strm/移入Emby)
            # 由用户先在 115 网盘手工整理视频(删除广告/杂项、确认分集等), 再点任务页「🔄整理」触发。
            # 影片(fanhao)不受影响, 仍全自动。
            save_task(task_id, status='pending_manual', step='wait',
                      msg=f'视频已落地({len(videos)} 个). 请先在 115 网盘手工整理视频(删除广告/确认分集), 整理完成后到任务页点「🔄整理」继续: 重命名S01E接续 → 生成strm → 移入Emby剧集库',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            return
        # 重新扫描视频 (重启恢复/上下文缺失时保证 videos 可用)
        if not videos:
            try:
                items = p.list_dir(savepath, maxdepth=4, use_cache=False)
                # 2026-09-10 修复: 取 rel 相对路径 (it[0]) 而非整条 tuple ——
                # 后者会被 _video_units 当成路径拼接, 生成 "thread_x/('BMW-345', ...)" 无效单元
                videos = [it[0] for it in items
                          if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
            except Exception as e:
                log.warning('[import] 重新扫描视频失败: %s', str(e)[:100])
        if videos:
            save_task(task_id, status='running', step='scrape',
                      msg=f'检测到 {len(videos)} 个视频文件, 开始刮削',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
        else:
            save_task(task_id, status='running', step='scrape',
                      msg='未检测到视频(可能部分文件未落地), 直接刮削',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])

        # 2.5 清理 115 目录: 番号资源才删 <500MB 广告小视频/杂项 (用户规则 2026-08-07)
        # 非番号资源跳过清理 (用户规则 2026-08-12): 非番号的小视频可能是正片片段/系列分集,
        # <500MB 容易被误删, 所以全部保留
        skip_clean = False
        if kind == 'non_fanhao':
            skip_clean = True
            log.info('[import] 用户指定非番号, 跳过 <500MB 清理')
        elif kind == 'fanhao':
            skip_clean = False
        else:
            # kind 缺失/非法: 已取消自动判定, 防御性按番号流程处理 (入口已强制校验, 正常不可达)
            log.warning('[import] kind=%r 非法(已取消自动判定), 按番号流程清理', kind)
        if skip_clean:
            save_task(task_id, status='running', step='wait',
                      msg='非番号资源: 跳过广告小视频清理, 保留所有文件',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
        else:
            # 下载稳定已在离线下载阶段(DL)等待过, 这里直接清理
            try:
                kept, removed = p.clean_keep_large_videos(savepath)
                log.info('[import] 清理完成: 保留 %d 个大视频, 删除 %d 个其他文件', len(kept), len(removed))
                save_task(task_id, status='running', step='wait',
                          msg=f'清理完成: 保留 {len(kept)} 个>500MB视频, 删除 {len(removed)} 个广告/小文件',
                          thread_id=str(thread_id or ''), title=(title or '')[:300])
            except Exception as e:
                log.warning('清理 115 目录失败: %s', e)
                save_task(task_id, status='running', step='wait', msg='清理 115 目录失败(继续): ' + str(e)[:120],
                          thread_id=str(thread_id or ''), title=(title or '')[:300])

        # 2.6 文件名长度检查: ext4 单文件名 255 字节, 超长文件名(>200B)会导致
        # SmartStrm 写本地 strm/nfo 报 Errno 36, 表现为云端有元数据但 Emby 无条目
        # (2026-08-11 thread_3473924 事故根因)。提前重命名缩短, 保证 strm 能生成。
        try:
            _shorten_long_names(p, savepath)
        except Exception as e:
            log.warning('超长文件名处理失败(继续): %s', e)

        # 3. 先触发 SmartStrm webhook 生成 strm (让 MDCng 有 strm 可刮削)
        # 增量(2026-08-12): 同 thread 多磁力→多子目录, 只对新单元触发, 避免整个 thread 全扫触发 115 限频
        try:
            new_units = _smartstrm_new_units(savepath, videos, category)
            if new_units:
                trig = []
                for u in new_units:
                    sub = _unit_path(savepath, u)
                    _trigger_smartstrm(sub, category)
                    trig.append(sub)
                    time.sleep(1)
                r = {'triggered': trig}
                save_task(task_id, status='running', step='strm',
                          msg='SmartStrm 增量触发 ' + str(len(trig)) + ' 个新单元: ' + ', '.join(trig)[:150],
                          thread_id=str(thread_id or ''), title=(title or '')[:300])
            else:
                r = {'triggered': []}
                save_task(task_id, status='running', step='strm',
                          msg='SmartStrm 无需触发: 所有视频单元已生成过 strm',
                          thread_id=str(thread_id or ''), title=(title or '')[:300])
        except Exception as e:
            save_task(task_id, status='failed', step='strm', msg='SmartStrm 触发失败: ' + str(e)[:150],
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            return

        # 3.5 清理本地 strm 残留: 115 上已被刮削清理(广告/rar等)的文件, SmartStrm 增量同步不会删本地
        try:
            removed = p._fs_client().sync_strm_with_115(savepath)
            if removed:
                log.info('本地残留 strm 清理 %d 个: %s', len(removed), removed[:5])
        except Exception as e:
            log.warning('本地 strm 残留清理失败: %s', e)

        # 3.54 起播预热已移除(2026-08-16): SmartStrm 自带新 strm 首播优化(异步获取媒体编码+302触发), 我们预热重复且额外消耗 115 API(downurl 生成+拉流), 有 770004 限频风险; 实测 SmartStrm 不缓存 downurl, 预热无持久效果

        # 3.55 是否跳过 MDCng (用户规则 2026-08-11/2026-08-12): 非番号 → 跳过 MDCng 直接网页刮削
        # (MDCng 对无番号资源刮削不准/易刮错, 直接走网页爬取兜底更稳)
        # 已取消自动判定: 必须由用户手工指定 kind, 'non_fanhao' 强制非番号 / 'fanhao' 强制番号
        skip_mdc = False
        if kind == 'non_fanhao':
            skip_mdc = True
            log.info('[import] 用户指定非番号, 跳过 MDCng 直接网页刮削')
        elif kind == 'fanhao':
            skip_mdc = False
            log.info('[import] 用户指定番号, 走 MDCng 刮削')
        else:
            # kind 缺失/非法: 已取消自动判定, 防御性按番号流程处理 (入口已强制校验, 正常不可达)
            log.warning('[import] kind=%r 非法(已取消自动判定), 按番号流程走 MDCng', kind)

        # 3.6 等 MDCng 原地刮削 (监控 strm 目录自动刮削, 直接生成本地 nfo/图片 → Emby 直接可读)
        # 不依赖 thread_id: 网页磁力/manual 入库同样等 MDCng watcher 刮削
        mdc_ok = False
        mdc_msg = ''
        if (thread_id or web_magnets) and not skip_mdc:
            local_dir = _local_strm_dir(savepath, category)
            save_task(task_id, status='running', step='nfo', msg='等待 MDCng 刮削(原地整理, 最长180s)...',
                      thread_id=thread_id, title=(title or '')[:300])
            # 2026-08-17 加固: MDCng/SmartStrm 链路不健康时直接降级网页兜底, 不白等 180s
            ok_h, hmsg = _mdc_pipeline_healthy()
            if not ok_h:
                log.warning('[import] MDCng/SmartStrm 链路不健康, 跳过等待直接网页兜底: %s', hmsg)
                mdc_ok, mdc_msg = False, f'MDCng/SmartStrm 链路不健康, 跳过等待(网页兜底): {hmsg}'
            else:
                # 2026-08-19 修复: MDCng watcher 以本地 strm 为输入, 等待前先确保 strm 就位
                # (SmartStrm 生成+同步到本地); strm 未就位则跳过 MDCng 等待直接网页兜底, 不傻等 180s。
                # 之前 strm 生成(4.9c)在刮削之后, 顺序颠倒导致 MDCng 无输入必超时。
                def _count_local_strm(d):
                    if not os.path.isdir(d):
                        return 0
                    return sum(1 for _r, _ds, _fs in os.walk(d) for f in _fs if f.lower().endswith('.strm'))
                strm_cnt = _count_local_strm(local_dir)
                if strm_cnt == 0:
                    log.warning('[import] 本地 strm 为空, 触发 SmartStrm 生成+同步 strm')
                    try:
                        _trigger_smartstrm(savepath, category)
                        time.sleep(5)
                        _trigger_smartstrm_sync(savepath)
                        time.sleep(5)
                    except Exception as e:
                        log.warning('[import] strm 生成触发失败: %s', str(e)[:100])
                    strm_cnt = _count_local_strm(local_dir)
                if strm_cnt == 0:
                    log.warning('[import] strm 仍未就位(SmartStrm 可能失败), 跳过 MDCng 直接网页兜底')
                    mdc_ok, mdc_msg, mdc_dir = False, 'strm 未就位(SmartStrm 失败?), 跳过 MDCng 直接网页兜底', None
                else:
                    mdc_ok, mdc_msg, mdc_dir = _wait_mdc_scrape(local_dir)
            # 完整性检查: 每个影片必须 nfo+图片都齐全, 缺一不可 (用户规则 2026-08-07)
            # 硬链接/移动整理模式下 MDC 产物在已刮削区 (mdc_dir), 本地待看区只有 strm,
            # 完整性/中文标题检查必须针对 mdc_dir, 否则误判"未刮削" (2026-08-31 修复)
            meta_dir = mdc_dir or local_dir
            ok2, n2, i2 = _has_local_metadata(meta_dir)
            if mdc_ok and not ok2:
                mdc_msg = f'MDCng 刮削不完全: {meta_dir} {n2} nfo / {i2} 图片 (需两者齐全), 走浏览器兜底补全'
                mdc_ok = False
            elif mdc_ok and not _mdc_title_has_chinese(meta_dir):
                # 用户规则(2026-08-11): MDCng 刮削结果标题无中文字符 → 视为刮错/不准,
                # 删除 MDCng 元数据(本地+115), 改为网页方式爬取
                rm = _remove_mdc_metadata(p, local_dir, savepath)
                mdc_msg = f'MDCng 刮削标题无中文字符(可能刮错), 已删除其元数据{rm}个, 改为网页方式爬取'
                mdc_ok = False
            elif mdc_ok:
                if META_UPLOAD_115:
                    # MDCng 刮削齐全 → 把本地 nfo/图片上传到 115 (MDCng 原地整理只写本地, 需补传 115)
                    try:
                        up = _upload_local_meta_to_115(p, local_dir, savepath)
                        mdc_msg += f'; 已上传 {up} 个 nfo/图片到 115'
                    except Exception as e:
                        mdc_msg += f'; ⚠️ 上传 115 失败: {str(e)[:100]}'
                else:
                    # 2026-08-14 方案: 115 只存视频, 元数据仅本地 (SmartStrm 增量模式不清本地)
                    mdc_msg += '; 元数据仅本地(115 只存视频)'
            save_task(task_id, status='running', step='nfo', msg=mdc_msg,
                      thread_id=thread_id, title=(title or '')[:300])
            log.info('[import] %s', mdc_msg)
        elif thread_id or web_magnets:
            # 非番号: 跳过 MDCng, mdc_ok=False → 走第 4 步待用户油猴补充元数据 (用户规则 2026-08-11)
            mdc_ok = False
            mdc_msg = '非番号资源, 跳过 MDCng, 待油猴补充元数据'
            save_task(task_id, status='running', step='nfo', msg=mdc_msg,
                      thread_id=thread_id, title=(title or '')[:300])
            log.info('[import] %s', mdc_msg)
        else:
            save_task(task_id, status='running', step='nfo', msg='手动磁力入库, 跳过网页刮削',
                      thread_id='', title=(title or '')[:300])

        # 4. MDCng 失败 → 不再自动浏览器爬取兜底 (云主机无浏览器/xvfb, CF 拦截, 刮削不可用) (2026-08-20)
        #    由用户用油猴脚本在帖子页补充元数据: 上传 /api/metadata 自动写 nfo/图片 + 触发 Emby 刷新 + 预热。
        #    strm 已生成, 用户补充后 Emby 条目才出现 (成功路径: 5/6 验证 + Emby 扫库 + 预热)。
        if thread_id and not mdc_ok:
            ttl = (title or '')[:300]
            try:
                cc = sqlite3.connect(LOG_DB)
                rr = cc.execute("SELECT title FROM import_log WHERE thread_id=? AND title IS NOT NULL AND title!='' ORDER BY created_at DESC LIMIT 1",
                                (thread_id,)).fetchone()
                cc.close()
                if rr and rr[0]:
                    ttl = rr[0][:300]
            except Exception:
                pass
            save_task(task_id, status='done', step='nfo',
                      msg=f'MDC 未刮削或元数据不完整: 请在帖子页用油猴脚本补充元数据 (thread #{thread_id}), 补充后自动触发 Emby 刷新+预热',
                      thread_id=thread_id, title=ttl)
            log.info('[import] thread=%s MDC 刮削失败/不完整, 待用户油猴补充元数据 (task=%s)', thread_id, task_id)
            return

        # 5. 验证刮削结果: 115 目录里是否有正片视频 (防误删/未落地)
        if thread_id:
            try:
                items = p.list_dir(savepath, maxdepth=4, use_cache=False)
                vids = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
                if not vids:
                    # 限频/CD2 抖动时列目录可能瞬时为空 → 等 10s 重试一次, 避免误判失败 (用户规则 2026-08-13)
                    log.warning('[import] 首次验证无视频, 10s 后重试 (可能限频/CD2 抖动)')
                    time.sleep(10)
                    items = p.list_dir(savepath, maxdepth=4)
                    vids = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
                if not vids:
                    save_task(task_id, status='failed', step='scrape', msg='⚠️ 刮削后 115 目录无视频文件(可能被误删或未落地), 请检查',
                              thread_id=thread_id, title=(title or '')[:300])
                    return
                nfos = [it for it in items if it[1].lower().endswith('.nfo')]
                save_task(task_id, status='running', step='scan',
                          msg=f'验证通过: {len(vids)} 视频, {len(nfos)} nfo',
                          thread_id=thread_id, title=(title or '')[:300])
            except Exception as e:
                log.warning('verify after scrape failed: %s', e)

        # 5.9 补目录同名 nfo (Emby 电影识别/poster 归属, 2026-08-19)
        try:
            _ensure_dir_nfo(_local_strm_dir(savepath, category))
        except Exception as _e:
            log.warning('ensure_dir_nfo failed: %s', str(_e)[:100])

        # 6. 等 nfo/图片同步到本地 strm 目录, 再 Emby 扫库 (避免扫库时 nfo 还没同步 → 元数据缺失)
        time.sleep(15)
        try:
            code = _trigger_emby_scan()
            save_task(task_id, status='done', step='scan', msg=f'Emby 扫库已触发({code}), strm 已生成, 稍后在 Emby 中可见',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
        except Exception as e:
            save_task(task_id, status='done', step='scan', msg='strm 已生成; Emby 扫库触发失败: ' + str(e)[:150],
                      thread_id=str(thread_id or ''), title=(title or '')[:300])

        # 6.5 新入库预热 (2026-08-19): Emby 扫库后对新条目做媒体信息预探测 → 首播秒开
        #     只对本次新入库条目; 存量影片不做 (用户规则 2026-08-19)
        try:
            _start_prewarm(_local_strm_dir(savepath, category), task_id, title or '')
        except Exception as _e:
            log.warning('[prewarm] 启动失败: %s', str(_e)[:120])
    except Exception as e:
        log.exception('import post failed')
        save_task(task_id, status='failed', step='error', msg='处理异常: ' + str(e)[:300],
                  thread_id=str(thread_id or ''), title=(title or '')[:300])

# ============================== 全局任务队列 (2026-08-13) ==============================
# ============================== 全局任务队列 (2026-08-15 双队列流水线) ==============================
# 用户规则 (2026-08-15): 入库流程优化 —— 离线下载不用等待前一个任务完成,
# 除离线下载外的其他环节 (清理/刮削/strm/元数据/Emby 扫库) 保持串行。
# 实现:
#   DL 阶段    离线下载: 推送磁力 + 等视频落地 + 等下载稳定, **完全不限制并发**
#              (2026-08-15 二次优化: 每个任务独立线程立即执行, 不再有 IMPORT_DL_CONCURRENT 上限)。
#              add_offline 是 115 服务器任务, 并发推送/等待不抢 115 API 配额, 各任务下载互不等待。
#   POST_QUEUE 处理队列: 剧集化/清理/文件名/strm/刮削/元数据/预热/Emby 扫库,
#              严格 1 个 worker 串行, 避免刮削/strm 互相干扰与 115 限频。
# 两个队列都感知限频 (2026-08-13): monitor_ratelimit2.py 把限频状态写入
# RATELIMIT_STATE_FILE, 限频期间 worker 不取任务 (任务保持排队状态), 恢复后自动继续。
# (旧 IMPORT_MAX_CONCURRENT 已废弃, 保留读取仅为兼容 systemd 环境变量, 不再影响并发)
_ = os.environ.get('IMPORT_MAX_CONCURRENT', '1')
IMPORT_DL_CONCURRENT = max(1, int(os.environ.get('IMPORT_DL_CONCURRENT', '3') or '3'))  # 2026-08-15 二次优化: 已废弃, 离线下载不再限制并发 (保留仅为兼容 systemd 环境变量)

# 2026-09-19: post 处理阶段并发 (此前写死 1 个 worker 严格串行)。
# 实测瓶颈: post 单部 ~75s → 34-48 部/小时; 而 MDCng 单部仅 16s 且支持 4-5 并发,
# 串行喂导致 MDCng 95% 时间闲置。提到 3 后 post 吞吐 ~70-100 部/小时。
# 仍受 115 限频护栏 (_ratelimit_active) 保护; 可用 IMPORT_POST_CONCURRENT 覆盖。
POST_CONCURRENT = max(1, int(os.environ.get('IMPORT_POST_CONCURRENT', '3') or '3'))
RATELIMIT_STATE_FILE = '/tmp/115push_ratelimit_state.json'   # 本地版: 无 monitor_ratelimit2, 文件不存在即不限频
RATELIMIT_STATE_STALE_S = 600   # state 文件超过 10 分钟未更新视为监控失效, 不再阻塞

DL_QUEUE = queue.Queue()
POST_QUEUE = queue.Queue()
_DL_WORKERS = []
_POST_WORKERS = []
_POST_WORKERS_LOCK = threading.Lock()   # 2026-09-19: 并发补齐 worker 时的互斥 (防多线程同时补出超额 worker)
_POST_WORKER_SEQ = 0                    # 2026-09-19: worker 线程命名序号 (便于日志区分是哪个 worker)
DL_CTX = {}   # task_id -> 离线下载阶段上下文 (savepath/title/kind/to_tv_mode/videos), POST 阶段消费后删除

def _ratelimit_active():
    """限频窗口内返回 True (monitor_ratelimit2 写入的状态)"""
    try:
        with open(RATELIMIT_STATE_FILE, 'r', encoding='utf-8') as f:
            st = json.load(f)
        if not st.get('limited'):
            return False
        mtime = os.path.getmtime(RATELIMIT_STATE_FILE)
        if time.time() - mtime > RATELIMIT_STATE_STALE_S:
            log.warning('[queue] 限频状态文件已过期(%ds 未更新), 视为监控失效, 放行任务', int(time.time() - mtime))
            return False
        return True
    except Exception:
        return False

_DL_ACTIVE = 0
_DL_ACTIVE_LOCK = threading.Lock()

def _dl_task_wrapper(fn, args):
    """离线下载任务包装: 限频中等待, 然后执行。每个任务一个独立线程, 完全不限制并发。"""
    global _DL_ACTIVE
    with _DL_ACTIVE_LOCK:
        _DL_ACTIVE += 1
    try:
        while _ratelimit_active():
            log.info('[queue] 115 限频中, 下载任务暂停, 等恢复后自动继续')
            time.sleep(30)
        fn(*args)
    except Exception:
        log.exception('dl worker exception')
    finally:
        with _DL_ACTIVE_LOCK:
            _DL_ACTIVE -= 1

def _post_worker():
    """处理 worker (并发数 = POST_CONCURRENT, 默认 3; 2026-09-19 前为固定 1 个严格串行)。

    每个 worker 独立从 POST_QUEUE 取任务执行, 同一时刻最多 POST_CONCURRENT 部在跑。
    限频 (115) 期间不出队, 保持任务排队。
    """
    while True:
        if _ratelimit_active():
            log.info('[queue] 115 限频中, 处理任务暂停出队, 等恢复后自动继续')
            time.sleep(30)
            continue
        fn, args = POST_QUEUE.get()
        try:
            fn(*args)
        except Exception:
            log.exception('post worker exception')
        finally:
            POST_QUEUE.task_done()

def _submit_dl(fn, args):
    """离线下载完全不限制并发: 每个任务独立 daemon 线程立即执行; 立即返回"""
    threading.Thread(target=_dl_task_wrapper, args=(fn, tuple(args)), daemon=True).start()

def _submit_post(fn, args):
    """入队处理队列并确保 POST_CONCURRENT 个 worker 存活; 立即返回"""
    POST_QUEUE.put((fn, tuple(args)))
    _ensure_post_workers()

# (2026-08-15 二次优化: 离线下载不再限制并发, _ensure_dl_workers 已移除)

def _ensure_post_workers():
    """确保 post 处理 worker 数达到 POST_CONCURRENT (默认 3)。

    2026-09-19: 此前写死 1 个 worker 严格串行 → post 仅 34-48 部/小时,
    而 MDCng 侧单部 16s / 支持 4-5 并发基本闲置。改为按需补齐到 POST_CONCURRENT 个;
    存活的 worker 不重复创建, 死掉的自动补位。并发共享的 115 客户端有 _ratelimit_active 护栏,
    DB 写在 save_task 里每次独立连接 (sqlite3 timeout=5s), 无线程安全问题。
    """
    global _POST_WORKERS, _POST_WORKER_SEQ
    with _POST_WORKERS_LOCK:
        _POST_WORKERS = [w for w in _POST_WORKERS if w.is_alive()]
        while len(_POST_WORKERS) < POST_CONCURRENT:
            _POST_WORKER_SEQ += 1
            w = threading.Thread(target=_post_worker, daemon=True,
                                 name='post-worker-%d' % _POST_WORKER_SEQ)
            w.start()
            _POST_WORKERS.append(w)
            log.info('[queue] post worker 已启动: %s (当前并发 %d/%d, 队列积压 %d)',
                     w.name, len(_POST_WORKERS), POST_CONCURRENT, POST_QUEUE.qsize())

def queued_count():
    return _DL_ACTIVE + POST_QUEUE.qsize()

def start_import(thread_id=None, magnet=None, title=None, thread_url=None, kind=None, task_id=None, resume=False, category=None):
    category = norm_category(category)
    task_id = task_id or uuid.uuid4().hex[:12]
    if _ratelimit_active():
        qmsg = '115 限频中, 任务排队等待, 恢复后自动执行'
    else:
        qmsg = '任务已入队(离线下载不限并发), 立即执行'
    save_task(task_id, status='queued', step='init', msg=qmsg,
              thread_id=str(thread_id or ''), magnet=(magnet or '')[:200], title=(title or '')[:300], kind=kind or '',
              category=category, thread_url=(thread_url or '')[:500])
    _submit_dl(_run_import_dl, (task_id, thread_id, magnet, title, thread_url, kind, resume, category))
    return task_id

# ============================== 批量入库 (2026-09-20) ==============================
# 面板「多磁链一次提交」的后端: 一段文本里一行一条链接, 逐条建任务。
# 设计约束: 单条 /api/import 语义不动(油猴脚本在用), 批量只是外面套一层循环 + 解析。
BATCH_MAX_ITEMS = int(os.environ.get('BATCH_MAX_ITEMS', '100'))    # 单批上限
_MAGNET_RE = re.compile(r'^magnet:\?', re.I)
_ED2K_RE = re.compile(r'^ed2k://', re.I)
_BTIH_RE = re.compile(r'^[0-9a-fA-F]{40}$')                        # 裸 info_hash -> 自动补成 magnet


def _line_cat_override(tokens):
    """摘掉行尾的 "#分类" 覆盖标记。返回 (剩余 token, cat|None, err|None)
    只在行尾且以 # 开头时生效, 写错分类名报错而不是静默落回 av。"""
    if not tokens:
        return tokens, None, None
    last = tokens[-1]
    if last.startswith('#') and len(last) > 1:
        key = last[1:].strip().lower()
        if key not in CATEGORY_MAP:
            return tokens, None, f'未知分类 #{key} (可用: {"/".join(CATEGORY_KEYS)})'
        return tokens[:-1], key, None
    return tokens, None, None


def parse_link_lines(raw, default_kind=None, default_category=None):
    """把多行文本(或字符串数组)解析成批次条目。

    规则:
      - 主用法是一行一条链接, 但一行里有多条(空格分隔)也全部识别, 不会粘成一条废链接
      - 空行与以 # 开头的整行注释跳过
      - 支持 magnet:?xt=urn:btih:... / ed2k://... / 裸 40 位 BTIH(自动补 magnet)
      - 行尾可跟 "#<分类key>" 单条覆盖默认分类; 整行是 ed2k 时保留文件名里的空格(ed2k 常含空格)
      - 同一链接重复出现只取第一条, 其余进 errors(显式告诉用户被跳过)
    纯函数: 不碰 DB、不发网络请求, 供 POST /api/import/batch 与单测复用。
    返回 (items, errors); items=[{magnet,kind,category,line}], errors=[{line,raw,error}]"""
    if isinstance(raw, (list, tuple)):
        lines = [str(x) for x in raw]
    else:
        lines = str(raw or '').replace('\r', '\n').split('\n')
    items, errors, seen = [], [], {}
    for idx, ln in enumerate(lines, 1):
        s = ln.strip().strip('"').strip("'").strip()
        if not s or s.startswith('#'):
            continue
        toks = [t.strip('"').strip("'") for t in s.split()]
        toks, cat_ov, err = _line_cat_override(toks)
        if err:
            errors.append({'line': idx, 'raw': s[:120], 'error': err})
            continue
        if not toks:
            continue
        # ed2k 的文件名里常有空格("%20" 之外的写法也见得到) -> 整行当一条, 不按空格切;
        # 行里同时出现 magnet: 时按空格切(用户多半是贴了一串磁力), 不做整行合并。
        joined = ' '.join(toks)
        if 'magnet:' in joined:
            cands = toks
        elif _ED2K_RE.match(joined):
            cands = [joined]
        else:
            cands = toks
        good, junk = [], []
        for t in cands:
            link = t
            if _BTIH_RE.match(link):
                link = 'magnet:?xt=urn:btih:' + link.upper()
            # ed2k 结尾容错 (2026-09-21): 论坛/客户端有两种写法 —— 规范 "…|HASH|/" 与省略尾斜杠 "…|HASH|",
            # 油猴脚本 normLink() 产出的正是后者。老规则只认前者, 会让批量提交里的 ed2k 全被当成
            # "格式不完整" 拒掉(单条 /api/import 不走这里, 所以以前没暴露)。两种都收, 只挡真正被截断的行。
            if _ED2K_RE.match(link) and not (re.search(r'\|/?$', link) and link.count('|') >= 4):
                junk.append('ed2k 链接格式不完整 (应为 ed2k://|file|名字|大小|HASH|/ 或 …|HASH|)')
                continue
            if _MAGNET_RE.match(link) or _ED2K_RE.match(link):
                if link in seen:
                    junk.append(f'重复链接 (与第 {seen[link]} 行相同, 已跳过)')
                    continue
                seen[link] = idx
                good.append(link)
            else:
                junk.append('不是 magnet:? / ed2k:// 链接 (裸 BTIH 必须 40 位十六进制)')
        if not good:
            errors.append({'line': idx, 'raw': s[:120], 'error': junk[0] if junk else '无有效链接'})
            continue
        for j in junk:
            errors.append({'line': idx, 'raw': s[:120], 'error': j})
        for link in good:
            items.append({'magnet': link, 'kind': default_kind,
                          'category': cat_ov or default_category, 'line': idx})
    return items, errors


# 2026-09-22: 同链接防重 —— 面板重复点「批量入库」会把同一批链接建两遍任务。
# 单条入库允许"删旧重推"(用户规则, 见 push_magnet), 但【同一时刻】两条任务跑同一条链接
# 会互相删 115 离线任务: 实测 20:31 面板连点两次 → 28 条并发, 13 条被"任务已存在→删旧重推",
# 白跑一轮且 MCP 会话被并发重建 (add_offline_download 无响应 xN)。
# 规则: 建任务前查同 hash 是否已有 queued/running 任务; 有就跳过这一条, 不建第二个任务。
# 副作用(有意): 同一链接"同时按两个分类入库"也会被跳过 —— 需等前一个跑完再提交另一分类。
DUP_ACTIVE_WINDOW_S = int(os.environ.get('DUP_ACTIVE_WINDOW_S', '7200'))
# ↑ 活跃任务的"新鲜"窗口, 防服务重启遗留的陈旧 running 永久误挡 (2h 覆盖 adopt 的 2h 上限)


def _active_link_hashes(window_s=None):
    """最近 window_s 秒内 queued/running 的任务: {链接hash: (task_id, status, step)}。

    只读台账, 零 115 请求。查库失败时返回空 dict —— 宁可放过(旧行为)也不误挡用户提交。
    """
    window_s = int(window_s or DUP_ACTIVE_WINDOW_S)
    out = {}
    try:
        c = sqlite3.connect(LOG_DB)
        rows = c.execute(
            "SELECT task_id, magnet, status, step FROM import_log "
            "WHERE status IN ('queued','running') AND magnet != '' "
            "AND updated_at >= datetime('now','localtime', ?)",
            ('-%d seconds' % window_s,)).fetchall()
        c.close()
    except Exception as e:
        log.warning('[dup] 查活跃任务失败, 本轮不做防重: %s', str(e)[:120])
        return out
    for tid, link, status, step in rows:
        h = _inv_hash(link)
        if h:
            out.setdefault(h, (tid, status, step))
    return out


def batch_import(raw_links, default_kind=None, default_category=None, dry_run=False,
                 thread_id=None, title=None, thread_url=None):
    """批量解析 + 逐条建任务。dry_run=True 只回解析结果, 不入队。
    thread_id/title/thread_url (2026-09-21 新增, 默认 None 保持老调用兼容):
      不传的话 start_import 会退化成无帖上下文 —— 落点变成 /sehuatang/<分类>/manual_<hash8>
      (每条链接一个目录), 且 kind='non_fanhao' 走不到剧集模式(看 _run_import_dl 的 to_tv_mode)。
      面板批量提交时必须带帖上下文, 保证与单条入库完全同构: 同 thread_<tid> 落点 / 剧集模式 / 📤元数据能对上目录。
    返回 {submitted, failed, skipped, dup_skipped, items[], duplicates[], errors[]} 或 {'error': ...}
       skipped     = 解析阶段被跳过的行数 (重复行/不识别), 语义不变
       dup_skipped = 查重跳过数 (2026-09-22): 同链接已有 queued/running 任务, 不重复建
       duplicates  = 上面这些行的明细 [{line, raw, error, task_id}]
    注意: dry_run 预览不做查重 (预览只反映解析结果, 不读台账)。"""
    if default_kind not in ('fanhao', 'non_fanhao'):
        return {'error': 'kind 必填, 只能是 fanhao(影片/番号) 或 non_fanhao(剧集/非番号)'}
    items, errors = parse_link_lines(raw_links, default_kind, norm_category(default_category))
    if len(items) > BATCH_MAX_ITEMS:
        return {'error': f'单批最多 {BATCH_MAX_ITEMS} 条, 当前 {len(items)} 条; 请分两批提交'}
    # 帖上下文: 优先显式 thread_id, 否则从 thread_url 里抠 (与 /api/import 单条路径同一套规则)
    tid = str(thread_id or '').strip()
    if not tid and thread_url:
        m = re.search(r'thread-(\d+)', thread_url) or re.search(r'[?&]tid=(\d+)', thread_url)
        if m:
            tid = m.group(1)
    out, duplicates = [], []
    active = {} if dry_run else _active_link_hashes()   # {hash: (task_id, status, step)}
    for it in items:
        it['category'] = norm_category(it.get('category'))
        row = {'magnet': it['magnet'][:100], 'kind': it['kind'],
               'category': it['category'], 'line': it['line']}
        if dry_run:
            out.append(dict(row, ok=True, task_id=''))
            continue
        # 查重 (2026-09-22): 同链接已有任务在跑 → 跳过, 不建第二个任务
        h = _inv_hash(it['magnet'])
        hold = active.get(h) if h else None
        if hold:
            dtid, dst, dstep = hold
            reason = '同链接已有任务在跑(%s/%s, %s), 已跳过' % (dst, dstep, dtid)
            row.update(ok=False, task_id=dtid, skipped=True, skip_reason='dup_active', error=reason)
            duplicates.append({'line': it['line'], 'raw': it['magnet'][:100],
                               'error': reason, 'task_id': dtid})
            out.append(row)
            continue
        try:
            row['task_id'] = start_import(magnet=it['magnet'], kind=it['kind'], category=it['category'],
                                          thread_id=tid or None, title=title, thread_url=thread_url)
            row['ok'] = True
        except Exception as e:
            row['ok'] = False
            row['task_id'] = ''
            row['error'] = str(e)[:200]
            log.exception('[batch] 建任务失败: %s', it['magnet'][:80])
        out.append(row)
    ok = sum(1 for r in out if r['ok'])
    dup = len(duplicates)
    if not dry_run:
        log.info('[batch] 提交 %d 条 (失败 %d, 查重跳过 %d, 解析跳过 %d, thread %s): %s', ok,
                 len(out) - ok - dup, dup, len(errors), tid or '-',
                 ', '.join(r['task_id'] for r in out if r['ok'])[:300])
        for d in duplicates:
            log.info('[batch] 查重跳过: 第 %s 行 %s (%s)', d['line'], d['raw'][:70], d['error'])
    return {'dry_run': bool(dry_run), 'submitted': ok, 'failed': len(out) - ok - dup,
            'skipped': len(errors), 'dup_skipped': dup, 'thread_id': tid,
            'items': out, 'duplicates': duplicates, 'errors': errors}


# ============================== avdb adopt (2026-09-19) ==============================
# avdb 下载 → 一键入库 的「入库侧连接器」落地逻辑。
# 触发来源: avdb_watch.py 守护 (纯事件驱动, 只读 avdb 本地 sqlite, 平时对 115 零请求)
#         或手动 POST /api/import/adopt。
# 流程: 解析 avdb 下载记录 → 等这一部下完(退避轮询) → 就地按 avdb 原落点处理
#       → 交现有处理管线(清理<500MB广告/重命名/strm/MDC刮削/Emby扫库), 不重新下载。
# 2026-09-20 (用户决策): 取消"移到 /sehuatang/<分类>/thread_<番号>"那一步。
#   115 侧搬移对最终结果零贡献 —— strm 用 pickcode 直链(移动不影响播放), 分类由
#   本地 mdc-ng 的 watch_dirs(待看/sehuatang → 已刮削/AV) 决定, 且 115 侧最终只是
#   平铺 thread_* 目录(无系列/番号层级), 语义上只是换桶不是分类。省掉每部 1 次 115 rename。
AVDB_LIB = avdb_src.AVDB_LIB
ADOPT_TIMEOUT = int(os.environ.get('ADOPT_TIMEOUT', '7200'))   # 等下载完成上限(秒), 默认 2h


def _adopt_interval(elapsed):
    """等待轮询退避: 前10分钟每分钟, 10-60分钟每3分钟, 之后每5分钟 (控制 115 请求量)"""
    if elapsed < 600:
        return 60
    if elapsed < 3600:
        return 180
    return 300


def _adopt_scan(p, dir_115):
    """列 avdb 落点目录: 返回 (视频条目, 视频总字节, >=500MB 的大视频数)
    列一层+子目录(maxdepth=3), 不递归全树"""
    items = p.list_dir(dir_115, maxdepth=3, use_cache=False, use_cd2=True)
    vids = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
    total = sum(int(it[3] or 0) for it in vids)
    big = [it for it in vids if int(it[3] or 0) >= KEEP_VIDEO_MIN]
    return vids, total, big


def _adopt_wait_ready(p, task_id, dir_115, number, info_hash='', timeout=None):
    """等这一部下完。优先 CD2 离线任务状态(hash)精确判定; 任务已被清掉时退化
    「目录里有 >=500MB 视频 且 连续两轮总大小不变」。等待期间轮询退避, 只查这一个目录。"""
    timeout = timeout or ADOPT_TIMEOUT
    t0 = time.time()
    last_total = None
    rounds = 0
    while True:
        el = time.time() - t0
        if el > timeout:
            save_task(task_id, status='failed', step='adopt_wait',
                      msg=f'等待下载完成超过 {int(timeout / 60)} 分钟仍未检测到(可能磁力慢/无速度)。'
                          f'可稍后在 115 网盘确认后重新 adopt: {number}',
                      thread_id=str(number or ''), title=str(number or ''))
            return False
        done, why = False, ''
        if info_hash:
            try:
                cd2 = p._cd2_client()
                st = cd2.get_offline_status(info_hash, max_pages=1)
                if st and st[0] == 2:
                    done, why = True, '离线任务已完成'
                elif st:
                    why = f'离线任务下载中(status={st[0]})'
                else:
                    why = '离线任务已清理, 按目录大小确认'
            except Exception as e:
                log.warning('[adopt] 离线任务状态查询失败(退化列目录): %s', str(e)[:100])
                why = '离线任务查询失败, 按目录大小确认'
        else:
            why = '按目录大小确认'
        try:
            vids, total, big = _adopt_scan(p, dir_115)
        except Exception as e:
            log.warning('[adopt] 列目录失败: %s', str(e)[:100])
            vids, total, big = [], None, []
        rounds += 1
        if not done and big and rounds > 1 and last_total is not None and total == last_total:
            done, why = True, '目录大小两轮不变'
        last_total = total
        if done and big:
            save_task(task_id, status='running', step='adopt_wait',
                      msg=f'avdb 下载已完成({why}), 视频 {len(vids)} 个, 开始入库',
                      thread_id=str(number or ''), title=str(number or ''))
            return True
        if done and not big:
            log.warning('[adopt] %s 判定完成但未见 >=500MB 视频, 再等一轮', number)
            done = False
        save_task(task_id, status='running', step='adopt_wait',
                  msg=f'{number} 等待 avdb 下载完成({why or "下载中"}), 已等 {int(el / 60)} 分钟, '
                      f'视频 {len(vids)} 个 / {round((total or 0) / 2 ** 30, 2)}GB',
                  thread_id=str(number or ''), title=str(number or ''))
        time.sleep(_adopt_interval(el))


def _run_import_adopt(task_id, thread_id, number, src_dir, category, title, info_hash=''):
    """adopt 下载阶段: 等完成 → 就地交处理阶段(清理/strm/刮削/扫库)
    (2026-09-20 起不再迁移 115 目录, 见文件头 avdb adopt 段注释)"""
    category = norm_category(category)
    p = Push115()
    try:
        save_task(task_id, status='running', step='adopt_wait',
                  msg=f'avdb 落点: {src_dir}, 等待下载完成',
                  thread_id=str(thread_id or ''), title=(title or '')[:300], src_115=src_dir)
        if not _adopt_wait_ready(p, task_id, src_dir, number, info_hash):
            return
        # 2026-09-20 (用户决策): 不再把 avdb 落点搬到 /sehuatang/<分类>/thread_<番号>。
        #   ① strm 用 pickcode 直链 → 115 上搬到哪都不影响播放;
        #   ② 最终分类由本地 mdc-ng 的 watch_dirs(待看/sehuatang → 已刮削/AV) 决定,
        #      115 侧只是"平铺 thread_* 换平铺番号目录", 对 Emby 结果零贡献;
        #   ③ 省掉每部 1 次 115 rename, 少一份 770004 风控面。
        dst = src_dir
        save_task(task_id, status='running', step='adopt_ready',
                  msg='不迁移 115 目录, 直接按 avdb 原落点处理: %s' % src_dir,
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        DL_CTX[task_id] = {'savepath': dst, 'title': title, 'kind': 'fanhao',
                           'to_tv_mode': False, 'videos': [], 'category': category}
        save_task(task_id, status='running', step='wait',
                  msg='排队等待处理(清理/strm/刮削/扫库, 并发 %d)...' % POST_CONCURRENT,
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        _submit_post(_run_import_post, (task_id, thread_id, '', title, '', 'fanhao', category))
    except Exception as e:
        log.exception('adopt failed')
        save_task(task_id, status='failed', step='error', msg='adopt 异常: ' + str(e)[:300],
                  thread_id=str(thread_id or ''), title=(title or '')[:300])


def adopt_resolve(number=None, dl_id=None):
    """解析 avdb 下载记录 → 该片在 115 的落点(只读 avdb 本地库 + 列一层 115 目录)。
    返回 dict 或 {'error': ...}"""
    row = None
    if dl_id:
        for r in avdb_src.fetch_downloads(after_id=int(dl_id) - 1, limit=1):
            if int(r['id']) == int(dl_id):
                row = r
        if row is None:
            return {'error': f'avdb download_log 无 id={dl_id}'}
    elif number:
        rows = avdb_src.fetch_downloads(after_id=0, limit=1000)
        for r in reversed(rows):
            if avdb_src.same_number(r.get('number') or '', number):
                row = r
                break
        if row is None:
            return {'error': f'avdb download_log 无番号 {number} 的记录'}
    else:
        return {'error': '需要 number 或 id'}
    num = row.get('number') or ''
    save_path = row.get('save_path') or ''
    p = Push115()
    info = avdb_src.resolve(num, p, save_path)
    if not info.get('dir_115'):
        return {'error': f'115 未找到 {num} 的落点目录 (avdb 记录 save_path={save_path})',
                'number': num, 'dl_id': row['id'], 'save_path': save_path}
    info['dl_id'] = row['id']
    info['save_path'] = save_path
    info['resource_name'] = row.get('resource_name') or ''
    info['avdb_title'] = row.get('title') or ''
    info['create_time'] = row.get('create_time') or ''
    return info


def start_adopt(number=None, dl_id=None, category=None, title=''):
    """提交一个 avdb 入库任务(不重新下载)。返回 task_id 或抛异常"""
    info = adopt_resolve(number=number, dl_id=dl_id)
    if info.get('error'):
        raise RuntimeError(info['error'])
    category = norm_category(category)
    thread_id = info['dir_name']
    title = title or info.get('resource_name') or info.get('avdb_title') or info['number']
    task_id = uuid.uuid4().hex[:12]
    save_task(task_id, status='queued', step='adopt_init',
              msg=f'avdb 入库任务已入队(不重新下载): {info["dir_115"]}',
              thread_id=str(thread_id), magnet=(info.get('magnet') or '')[:200],
              title=title[:300], kind='fanhao', category=category, src_115=info['dir_115'],
              origin='avdb')
    _submit_dl(_run_import_adopt,
               (task_id, thread_id, info['number'], info['dir_115'], category,
                title, Push115.link_hash(info.get('magnet') or '')))
    return task_id


# ============================== avdb 连接器: 面板后端 ==============================
# 2026-09-19: avdb 下载联动有自己的一套状态 (守护水位 / 台账), 与色花堂入库任务分开显示,
# 面板走独立页面 /avdb + 独立接口 /api/avdb/*, 不混进 /tasks 主列表。
AVDB_LEDGER = os.environ.get('AVDB_WATCH_DB', '/var/lib/avdb-watch/avdb_watch.db')
AVDB_PAUSE_FILE = '/tmp/avdb_watch.pause'


def _avdb_ledger_conn(readonly=True):
    if not os.path.exists(AVDB_LEDGER):
        return None
    try:
        if readonly:
            return sqlite3.connect('file:%s?mode=ro' % AVDB_LEDGER, uri=True, timeout=5)
        return sqlite3.connect(AVDB_LEDGER, timeout=10)
    except Exception as e:
        log.warning('[avdb] 打开台账失败: %s', str(e)[:120])
        return None


def _avdb_state():
    """台账 state 表 (水位/心跳/间隔)"""
    c = _avdb_ledger_conn()
    if not c:
        return {}
    try:
        return {str(k): ('' if v is None else str(v)) for k, v in c.execute('SELECT k, v FROM state').fetchall()}
    except Exception:
        return {}
    finally:
        c.close()


def _avdb_seen(limit=300):
    """台账 seen 表 (最新在前)"""
    c = _avdb_ledger_conn()
    if not c:
        return []
    try:
        cols = [d[1] for d in c.execute('PRAGMA table_info(seen)').fetchall()]
        if not cols:
            return []
        rows = c.execute('SELECT %s FROM seen ORDER BY dl_id DESC LIMIT ?' % ','.join(cols),
                         (int(limit),)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []
    finally:
        c.close()


def _avdb_ledger_mark(dl_id, number='', dir_name='', task_id='', status='', note=''):
    """写台账 (手动补扫按守护的口径记账, 避免守护重复接手同一部)"""
    c = _avdb_ledger_conn(readonly=False)
    if not c:
        return False
    try:
        c.execute('''INSERT INTO seen(dl_id, number, dir_name, task_id, status, note) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(dl_id) DO UPDATE SET number=excluded.number, dir_name=excluded.dir_name,
                     task_id=COALESCE(NULLIF(excluded.task_id,''), seen.task_id),
                     status=excluded.status, note=excluded.note, updated_at=datetime('now','localtime')''',
                  (int(dl_id), number, dir_name, task_id, status, note[:200]))
        c.commit()
        return True
    except Exception as e:
        log.warning('[avdb] 台账写入失败: %s', str(e)[:120])
        return False
    finally:
        c.close()


def _avdb_task_map():
    """台账 task_id → 入库任务状态 (面板上点开能看到进度)"""
    ids = [r.get('task_id') for r in _avdb_seen() if r.get('task_id')]
    if not ids:
        return {}
    c = sqlite3.connect(LOG_DB)
    try:
        out = {}
        for i in range(0, len(ids), 200):
            ch = ids[i:i + 200]
            ph = ','.join(['?'] * len(ch))
            for r in c.execute(f'SELECT task_id, status, step, msg, thread_id FROM import_log WHERE task_id IN ({ph})', ch):
                out[r[0]] = {'status': r[1], 'step': r[2], 'msg': r[3], 'thread_id': r[4]}
        return out
    except Exception:
        return {}
    finally:
        c.close()


def avdb_status():
    """连接器面板数据。只读 avdb 本地 sqlite + 本地台账, **零网络请求**"""
    st = _avdb_state()
    seen = _avdb_seen()
    try:
        interval = int(st.get('interval') or 60)
    except Exception:
        interval = 60
    try:
        hb = float(st.get('heartbeat') or 0)
    except Exception:
        hb = 0.0
    age = int(time.time() - hb) if hb else None
    alive = bool(hb) and age is not None and age < max(180, interval * 3)
    try:
        wm = int(st.get('watermark'))
    except Exception:
        wm = None
    try:
        max_id = avdb_src.max_download_id()
    except Exception:
        max_id = None
    pending = []
    if wm is not None:
        try:
            for r in avdb_src.fetch_downloads(after_id=wm, limit=100):
                pending.append({'dl_id': r['id'], 'number': (r.get('number') or ''),
                                'title': (r.get('title') or '')[:90],
                                'save_path': r.get('save_path') or '',
                                'create_time': r.get('create_time') or ''})
        except Exception:
            pass
    by_status = {}
    for r in seen:
        k = r.get('status') or ''
        by_status[k] = by_status.get(k, 0) + 1
    tmap = _avdb_task_map()
    done_names = _imported_names() if any(not (r.get('task_id') or '') for r in seen) else []
    for r in seen:
        t = tmap.get(r.get('task_id') or '') or {}
        r['task_status'] = t.get('status', '')
        r['task_step'] = t.get('step', '')
        r['task_msg'] = (t.get('msg') or '')[:120]
        # 台账状态为 missing/skipped(落点已迁走) → 若该番号其实已入库, 面板显示「已在库里」
        r['imported_task'] = ''
        if not r.get('task_id') and r.get('number'):
            for nm, tid in done_names:
                if avdb_src.same_number(nm, r['number']):
                    r['imported_task'] = tid
                    break
    return {
        'guard': {'alive': alive, 'heartbeat': hb, 'age': age, 'interval': interval,
                  'paused': os.path.exists(AVDB_PAUSE_FILE),
                  'category': st.get('category') or 'av', 'pid': st.get('pid') or '',
                  'rounds': st.get('rounds') or ''},
        'watermark': wm, 'avdb_max_id': max_id,
        'ledger': {'total': len(seen), 'by_status': by_status},
        'pending': pending,
        'pending_count': len(pending),
        'seen': seen[:150],
        'ledger_path': AVDB_LEDGER,
        'avdb_lib': avdb_src.AVDB_LIB,
    }


def _imported_names():
    """已完成入库任务的番号/名称 → [(name, task_id)]，供补扫判定「已在库里」。"""
    out = []
    try:
        c = sqlite3.connect(LOG_DB)
        for tid, th, title, src in c.execute(
                "SELECT task_id, thread_id, title, src_115 FROM import_log WHERE status='done'"):
            for nm in (th, title, os.path.basename((src or '').rstrip('/'))):
                if nm:
                    out.append((str(nm), tid))
        c.close()
    except Exception as e:
        log.warning('_imported_names 查询失败: %s', str(e)[:120])
    return out


def avdb_scan(limit=40, do_adopt=False, category='av'):
    """手动补扫: 找 avdb 里「没进台账、但 115 上确实有落点」的漏网片。
    每个 save_path 只列一次 115 目录, 之后纯本地匹配 (不按片逐条查 115)。
    台账 status='missing' 的不再放行 (2026-09-20): 见循环里 has_ledger 的说明。"""
    rows = avdb_src.fetch_downloads(after_id=0, limit=1000)
    if limit:
        rows = rows[-int(limit):]
    led = {int(r['dl_id']): r for r in _avdb_seen(limit=1000) if r.get('dl_id') is not None}
    p = Push115()
    cache = {}

    def _pairs(sp):
        key = sp or ''
        if key in cache:
            return cache[key]
        try:
            dirs, root = avdb_src.list_avdb_dirs(p, sp)
        except Exception:
            dirs, root = [], (avdb_src.AVDB_LIB.rstrip('/') + ('/' + key.strip('/') if key else ''))
        cache[key] = [(root.rstrip('/') + '/' + d, d) for d in dirs]
        return cache[key]

    def _all_pairs(sp):
        out = list(_pairs(sp))
        for _path, top in _pairs(''):
            out += _pairs(top)
        return out

    out, n_adopted, n_cand = [], 0, 0
    done_rows = _imported_names()
    for r in rows:
        dl_id = int(r['id'])
        num = (r.get('number') or '').strip()
        sp = r.get('save_path') or ''
        item = {'dl_id': dl_id, 'number': num, 'save_path': sp,
                'title': (r.get('title') or '')[:90],
                'create_time': r.get('create_time') or '',
                'resource_name': (r.get('resource_name') or '')[:120],
                'status': '', 'dir_115': '', 'task_id': ''}
        prev = led.get(dl_id) or {}
        prev_status = (prev.get('status') or '').strip()
        # missing 不是终态 (2026-09-20 修): 原来把 missing 当已处理直接放行, 导致 20 部 / 184GB
        # 片子永远躺在 avdb 暂存区 (守护不重试 + 补扫也不查)。判 missing 的真实原因只有两类:
        #   ① 番号写法对不上 (SQTE-701 vs SQTE-701_4KS / JNT-104 vs 390JNT-104-uncensored-HD ...)
        #   ② 抢在 115 下载落地之前判定 (竞态)
        # 两类都值得复查 → 只放行「显式 skipped」和「已起过入库任务(task_id)」的。
        has_ledger = bool(prev.get('task_id') or prev_status == 'skipped')
        if not num:
            if has_ledger:
                item['status'] = 'ledger'
                item['ledger_status'] = prev.get('status') or ''
                item['task_id'] = prev.get('task_id') or ''
                item['dir_115'] = prev.get('dir_name') or ''
            else:
                item['status'] = 'no_number'
            out.append(item)
            continue
        # 已在库里 (入库任务已 done, 落点已被迁走) → 不是漏网, 不重复入库
        hit_tid = ''
        for nm, tid in done_rows:
            if nm and avdb_src.same_number(nm, num):
                hit_tid = tid
                break
        if hit_tid:
            item['status'] = 'done_imported'
            item['task_id'] = hit_tid
            item['dir_115'] = (prev.get('dir_name') or '')
            out.append(item)
            continue
        if has_ledger:
            item['status'] = 'ledger'
            item['ledger_status'] = prev.get('status') or ''
            item['task_id'] = prev.get('task_id') or ''
            item['dir_115'] = prev.get('dir_name') or ''
            out.append(item)
            continue
        hit_path = None
        for path, d in _all_pairs(sp):
            if avdb_src.same_number(d, num):
                hit_path = path
                break
        if not hit_path:
            item['status'] = 'not_on_115'
            out.append(item)
            continue
        item['status'] = 'candidate'
        item['dir_115'] = hit_path
        n_cand += 1
        if do_adopt:
            try:
                tid = start_adopt(number=num, dl_id=dl_id, category=category)
                item['status'] = 'adopted'
                item['task_id'] = tid
                n_adopted += 1
                _avdb_ledger_mark(dl_id, number=num, dir_name=os.path.basename(hit_path),
                                  task_id=tid, status='submitted',
                                  note='手动补扫 → src=%s' % hit_path)
            except Exception as e:
                item['status'] = 'adopt_failed'
                item['error'] = str(e)[:160]
        out.append(item)
    out.reverse()
    return {'scanned': len(rows), 'candidates': n_cand, 'adopted': n_adopted, 'items': out}


def _find_thread_path(thread_id):
    """定位 thread 的 115 目录: 优先剧集媒体库 /sehuatang_tv/, 然后分类目录 /sehuatang/<分类>/, 最后旧平铺 /sehuatang/"""
    p = Push115()
    try:
        fs = p._fs_client()
        if fs.exists(f'{IMPORT_TV_ROOT}/thread_{thread_id}'):
            return f'{IMPORT_TV_ROOT}/thread_{thread_id}'
        for key in CATEGORY_MAP:
            cand = f'{IMPORT_ROOT}/{category_name(key)}/thread_{thread_id}'
            if fs.exists(cand):
                return cand
        return f'{IMPORT_ROOT}/thread_{thread_id}'
    except Exception:
        return f'{IMPORT_ROOT}/thread_{thread_id}'


def _tv_ensure_strms(p, task_id, thread_id, savepath, title, items):
    """剧集目录重新刮削: 新增/未命名视频重命名为 S01E 续号, 触发 SmartStrm 生成 strm 并迁移到剧集目录。
    (2026-08-15: 重新刮削整合"新增文件的剧集重命名", 否则文件夹内多新视频后 Emby 识别不出)
    返回 show_dir (剧集目录路径) 或 None"""
    EP_RE = re.compile(r'\.S\d{2}E(\d{2,})\.', re.I)
    vids = sorted([it[1] for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS])
    named = {}
    unnamed = []
    prefix = None
    for name in vids:
        m = EP_RE.search(name)
        if m:
            named[int(m.group(1))] = name
            if prefix is None:
                prefix = name[:m.start()]
        else:
            unnamed.append(name)
    if not unnamed:
        return None
    if not prefix:
        prefix = _simplify_title(title, f'thread_{thread_id}')
    # 定位剧集目录 (优先已有目录, 防止 _simplify_title 与历史目录名不一致)
    show_dir = None
    try:
        for d in os.listdir(TV_STRM_ROOT):
            if d == prefix or d.startswith(prefix) or prefix.startswith(d):
                show_dir = os.path.join(TV_STRM_ROOT, d)
                break
    except Exception:
        pass
    if show_dir is None:
        show_dir = os.path.join(TV_STRM_ROOT, prefix)
        try:
            os.makedirs(show_dir, exist_ok=True)
        except Exception:
            pass
    next_ep = (max(named) + 1) if named else 1
    start_ep = next_ep
    target_nums = set()
    fs = p._fs_client()
    renamed = 0
    for name in unnamed:
        ext = os.path.splitext(name)[1].lower()
        newname = f'{prefix}.S01E{next_ep:02d}{ext}'
        if name != newname:
            try:
                if fs.rename(f'{savepath}/{name}', f'{savepath}/{newname}'):
                    renamed += 1
                    target_nums.add(next_ep)
                    log.info('[rescrape] 剧集新增重命名 %s -> %s', name[:40], newname[:80])
                    time.sleep(2)   # 115 风控: 慢一点
            except Exception as e:
                log.warning('[rescrape] 剧集新增重命名失败 %s: %s', name[:40], str(e)[:120])
        next_ep += 1
    if not renamed:
        # 2026-08-16 328115: rename 全失败(CD2 FUSE 缓存故障等), 但 115 真实结构可能已变化;
        # SmartStrm 直连 115 API(不走 FUSE), 仍触发对齐 strm, 由 4.9b 兜底迁移到剧集目录
        save_task(task_id, status='running', step='rename',
                  msg=f'剧集新增重命名失败({len(unnamed)} 个, 可能挂载缓存问题), 仍触发 SmartStrm 对齐 strm...',
                  thread_id=thread_id, title=title[:300])
        try:
            _trigger_smartstrm(savepath)
        except Exception as e:
            log.warning('[rescrape] 触发 SmartStrm 失败: %s', str(e)[:120])
        return show_dir
    save_task(task_id, status='running', step='rename',
              msg=f'剧集目录新增 {renamed} 个视频已重命名 S01E{start_ep:02d}..S01E{next_ep-1:02d}, 触发 SmartStrm 生成 strm...',
              thread_id=thread_id, title=title[:300])
    try:
        _trigger_smartstrm(savepath)
    except Exception as e:
        log.warning('[rescrape] 触发 SmartStrm 失败: %s', str(e)[:120])
    local_dir = _local_strm_dir(savepath)
    got = 0
    for _i in range(20):
        time.sleep(6)
        try:
            files = os.listdir(local_dir)
        except Exception:
            files = []
        nums = set()
        for f in files:
            m = re.search(r'\.S\d{2}E(\d{2,})\.', f)
            if m:
                nums.add(int(m.group(1)))
        got = len(target_nums & nums)
        if got >= len(target_nums):
            break
        if _i % 2 == 1:
            save_task(task_id, status='running', step='strm',
                      msg=f'等待 SmartStrm 生成新增 strm: {got}/{len(target_nums)} (第{(_i+1)*6}s)',
                      thread_id=thread_id, title=title[:300])
    if got < len(target_nums):
        # a_task 事件兜底 (会跑任务生成 strm)
        for _i in range(4):
            try:
                _trigger_smartstrm_sync(savepath)
            except Exception:
                pass
            time.sleep(6)
            try:
                files = os.listdir(local_dir)
            except Exception:
                files = []
            nums = set()
            for f in files:
                m = re.search(r'\.S\d{2}E(\d{2,})\.', f)
                if m:
                    nums.add(int(m.group(1)))
            got = len(target_nums & nums)
            if got >= len(target_nums):
                break
    moved = 0
    try:
        for fn in os.listdir(local_dir):
            if fn.startswith(prefix + '.S01E') and fn.endswith('.strm'):
                m = re.search(r'\.S\d{2}E(\d{2,})\.', fn)
                if m and int(m.group(1)) in target_nums:
                    try:
                        shutil.move(os.path.join(local_dir, fn), os.path.join(show_dir, fn))
                        moved += 1
                    except Exception as e:
                        log.warning('[rescrape] 移动 strm 失败 %s: %s', fn[:40], str(e)[:100])
    except Exception as e:
        log.warning('[rescrape] 迁移 strm 目录扫描失败: %s', str(e)[:100])
    save_task(task_id, status='running', step='strm',
              msg=f'新增 strm 就绪: {moved}/{len(target_nums)} (共 {renamed} 个新视频), 继续刮削...',
              thread_id=thread_id, title=title[:300])
    return show_dir


def _tv_ensure_showdir_final(p, task_id, thread_id, savepath, title, local_dir, show_dir):
    """剧集兜底迁移 (2026-08-16 328115): Emby 扫库前确保剧集目录(emby_tv)有 strm+元数据。
    - 递归扫描本地 strm 目录(结构异常时 nfo 可能在子目录)
    - strm 缺失则 move(幂等, 同名跳过), nfo/图片复制(覆盖), tvshow.nfo 生成/刷新
    返回 (moved_strm, n_meta, nfo_ok)"""
    if not (savepath or '').startswith(IMPORT_TV_ROOT + '/'):
        return 0, 0, False
    if not show_dir or not os.path.isdir(show_dir):
        show_dir = None
        try:
            prefix = _simplify_title(title, f'thread_{thread_id}')
            for d in os.listdir(TV_STRM_ROOT):
                if d == prefix or d.startswith(prefix) or prefix.startswith(d):
                    show_dir = os.path.join(TV_STRM_ROOT, d)
                    break
        except Exception:
            pass
        if show_dir is None:
            show_dir = os.path.join(TV_STRM_ROOT, _simplify_title(title, f'thread_{thread_id}'))
    try:
        os.makedirs(show_dir, exist_ok=True)
    except Exception:
        pass
    if not os.path.isdir(local_dir):
        return 0, 0, False
    moved = 0
    n_meta = 0
    nfo_src = None
    for root, _dirs, files in os.walk(local_dir):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            fl = fn.lower()
            if fl.endswith('.strm'):
                dst = os.path.join(show_dir, fn)
                if not os.path.exists(dst):
                    try:
                        shutil.move(full, dst)
                        moved += 1
                    except Exception as e:
                        log.warning('[rescrape] 兜底迁移 strm 失败 %s: %s', fn[:40], str(e)[:80])
            elif fl.endswith('.nfo'):
                if nfo_src is None or fl == 'movie.nfo':
                    nfo_src = full
                try:
                    shutil.copy2(full, os.path.join(show_dir, fn))
                    n_meta += 1
                except Exception as e:
                    log.warning('[rescrape] 兜底复制 nfo 失败 %s: %s', fn[:40], str(e)[:80])
            elif fl.endswith(('.jpg', '.jpeg', '.png')):
                try:
                    shutil.copy2(full, os.path.join(show_dir, fn))
                    n_meta += 1
                except Exception as e:
                    log.warning('[rescrape] 兜底复制图片失败 %s: %s', fn[:40], str(e)[:80])
    nfo_ok = False
    if nfo_src:
        try:
            show = os.path.basename(show_dir.rstrip('/')) or f'thread_{thread_id}'
            with open(os.path.join(show_dir, 'tvshow.nfo'), 'w', encoding='utf-8') as fh:
                fh.write(_make_tvshow_nfo(nfo_src, show))
            nfo_ok = True
        except Exception as e:
            log.warning('[rescrape] tvshow.nfo 生成失败: %s', str(e)[:120])
    return moved, n_meta, nfo_ok


def _tv_sync_meta_to_showdir(show_dir, local_dir):
    """把本地 strm 目录的 nfo/图片复制到剧集目录 (Emby 读取), 返回复制数
    (2026-08-16 328115: 递归遍历, 结构异常时 nfo 可能在子目录)"""
    n = 0
    if not show_dir or not os.path.isdir(local_dir):
        return 0
    try:
        os.makedirs(show_dir, exist_ok=True)
    except Exception:
        pass
    for root, _dirs, files in os.walk(local_dir):
        for fn in files:
            fl = fn.lower()
            if fl.endswith(('.nfo', '.jpg', '.jpeg', '.png')):
                try:
                    shutil.copy2(os.path.join(root, fn), os.path.join(show_dir, fn))
                    n += 1
                except Exception as e:
                    log.warning('[rescrape] 复制元数据失败 %s: %s', fn[:40], str(e)[:80])
    return n


def _ensure_strms_ready(p, task_id, thread_id, savepath, items, title):
    """Emby 扫库前 strm 就位兜底 (2026-08-16 影片版, 与剧集 4.9b 迁移互补):
    - 影片(/sehuatang): 本地 /strm/emby/thread_x 即 Emby 监视目录, strm 数 < 115 视频数
      时补触发 SmartStrm(cs_strm+a_task) 并等待, 避免"重新刮削 done 但 Emby 不显示"。
    - 剧集(/sehuatang_tv): strm 已由 4.9b 迁移到 emby_tv/<标题>, 校验该目录 strm 数;
      不足则补触发 SmartStrm 并再次迁移新生成的 strm (幂等)。
    返回 {'expected','found','triggered','waited'}"""
    vids = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
    expected = len(vids)
    if expected == 0:
        return {'expected': 0, 'found': 0, 'triggered': False, 'waited': 0}
    is_tv = (savepath or '').startswith(IMPORT_TV_ROOT + '/')
    def _count(d):
        if not os.path.isdir(d):
            return 0
        return sum(1 for _r, _ds, fs in os.walk(d) for f in fs if f.lower().endswith('.strm'))
    if is_tv:
        target = None
        try:
            prefix = _simplify_title(title, f'thread_{thread_id}')
            for d in os.listdir(TV_STRM_ROOT):
                if d == prefix or d.startswith(prefix) or prefix.startswith(d):
                    target = os.path.join(TV_STRM_ROOT, d)
                    break
        except Exception:
            pass
        if target is None:
            target = os.path.join(TV_STRM_ROOT, _simplify_title(title, f'thread_{thread_id}'))
    else:
        target = _local_strm_dir(savepath)
    found = _count(target)
    if found >= expected:
        return {'expected': expected, 'found': found, 'triggered': False, 'waited': 0}
    triggered = False
    waited = 0
    for _i in range(6):
        try:
            _trigger_smartstrm(savepath)
            triggered = True
            time.sleep(5)
            _trigger_smartstrm_sync(savepath)
            time.sleep(5)
        except Exception as e:
            log.warning('[rescrape] strm 就位补触发失败(第%d次): %s', _i + 1, str(e)[:100])
        waited += 10
        if is_tv:
            try:
                _tv_ensure_showdir_final(p, task_id, thread_id, savepath, title,
                                         _local_strm_dir(savepath), target)
            except Exception as e:
                log.warning('[rescrape] 补触发后剧集迁移失败: %s', str(e)[:100])
        found = _count(target)
        if found >= expected:
            break
    return {'expected': expected, 'found': found, 'triggered': triggered, 'waited': waited}


def run_rescrape(task_id, thread_id, kind='web'):
    """重新刮削已完成任务 (用户手工改了 115 目录内文件名后):
    kind='mdc' 走 MDCng watcher 重刮, kind='web' 走色花堂网页爬取刮削。
    不推磁力/不清理, 只做 刮削 -> 元数据 -> 同步 -> Emby 扫库。"""
    p = Push115()
    thread_id = str(thread_id or '').strip()
    if not thread_id:
        save_task(task_id, status='failed', step='error', msg='缺少 thread_id, 无法重新刮削')
        return
    savepath = _find_thread_path(thread_id)
    # 沿用原任务标题 (排除新任务自己)
    title = f'thread_{thread_id}'
    try:
        c = sqlite3.connect(LOG_DB)
        r = c.execute("SELECT title FROM import_log WHERE thread_id=? AND task_id<>? AND title IS NOT NULL AND title!='' ORDER BY created_at DESC LIMIT 1",
                      (thread_id, task_id)).fetchone()
        if r and r[0]:
            title = r[0]
        c.close()
    except Exception:
        pass
    way = 'MDCng' if kind == 'mdc' else '网页爬取'
    save_task(task_id, status='running', step='wait', msg=f'重新刮削开始 (thread_{thread_id}, 方式: {way})',
              thread_id=thread_id, title=title[:300])
    try:
        # 0. 校验 115 目录存在且有视频 (2026-08-16: 增加重试, CD2 FUSE 间歇故障
        # 曾导致一次读空直接失败, 如 thread_3695887 19:18 瞬时 FUSE 抖动)
        items = []
        vids = []
        for _r in range(4):
            try:
                items = p.list_dir(savepath, maxdepth=4, use_cache=False)
                vids = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
            except Exception as e:
                log.warning('[rescrape] 读取 115 目录失败(第%d次): %s', _r + 1, str(e)[:120])
            if vids:
                break
            if _r < 3:
                if _fs_cache_sync(savepath, tag='[rescrape] '):
                    log.info('[rescrape] 视频读取为空, 已同步缓存(第%d次), 5s 后重试', _r + 1)
                else:
                    log.info('[rescrape] 视频读取为空, 第%d次重试(本地版无 115 目录缓存可同步)', _r + 1)
                time.sleep(5)
        if not vids:
            save_task(task_id, status='failed', step='scrape',
                      msg=f'115 目录 thread_{thread_id} 无视频文件(不存在或未落地), 无法刮削',
                      thread_id=thread_id, title=title[:300])
            return
        save_task(task_id, status='running', step='scrape', msg=f'115 目录 {len(vids)} 个视频, 开始重新刮削',
                  thread_id=thread_id, title=title[:300])

        # 0.5 剧集目录: 新增视频重命名 + strm 生成迁移 (2026-08-15)
        show_dir = None
        if (savepath or '').startswith(IMPORT_TV_ROOT + '/'):
            try:
                show_dir = _tv_ensure_strms(p, task_id, thread_id, savepath, title, items)
            except Exception as e:
                log.warning('[rescrape] 剧集新增处理失败: %s', str(e)[:150])

        if kind == 'mdc':
            # ---- MDCng 重新刮削: 前置健康检查 -> 清旧元数据 -> 触发 SmartStrm -> 等 watcher 重刮 ----
            # (2026-08-17 加固: 8-17 SmartStrm 卡死 12h 导致 MDC rescrape 全部超时,
            #  且 rescrape 先删元数据后刮不回来, 丢失 16 个目录元数据; 链路不健康时
            #  拒绝清理旧元数据, 直接失败保留现有数据)
            local_dir = _local_strm_dir(savepath)
            ok_h, hmsg = _mdc_pipeline_healthy()
            if not ok_h:
                save_task(task_id, status='failed', step='scrape',
                          msg=f'MDCng/SmartStrm 链路不健康, 已中止重新刮削(保留现有元数据): {hmsg}',
                          thread_id=thread_id, title=title[:300])
                return
            try:
                rm = _remove_mdc_metadata(p, local_dir, savepath)
                save_task(task_id, status='running', step='nfo', msg=f'已清理旧元数据(本地+115) {rm} 个, 等待 MDCng 重刮...',
                          thread_id=thread_id, title=title[:300])
            except Exception as e:
                log.warning('[rescrape] 清理旧元数据失败: %s', e)
            try:
                r = _trigger_smartstrm(savepath)
                save_task(task_id, status='running', step='strm', msg='SmartStrm 已触发(对齐新文件名): ' + json.dumps(r, ensure_ascii=False)[:120],
                          thread_id=thread_id, title=title[:300])
            except Exception as e:
                save_task(task_id, status='running', step='strm', msg='SmartStrm 触发失败(继续等 MDCng): ' + str(e)[:120],
                          thread_id=thread_id, title=title[:300])
            mdc_ok, mdc_msg, mdc_dir = _wait_mdc_scrape(local_dir, timeout=240)
            meta_dir = mdc_dir or local_dir
            ok2, n2, i2 = _has_local_metadata(meta_dir)
            if mdc_ok and not ok2:
                mdc_ok = False
                mdc_msg = f'MDCng 刮削不完全: {meta_dir} {n2} nfo / {i2} 图片'
            elif mdc_ok and not _mdc_title_has_chinese(meta_dir):
                rm2 = _remove_mdc_metadata(p, local_dir, savepath)
                mdc_ok = False
                mdc_msg = f'MDCng 刮削标题无中文字符(可能刮错), 已删除元数据{rm2}个; 建议改用网页爬取'
            elif mdc_ok:
                if META_UPLOAD_115:
                    try:
                        up = _upload_local_meta_to_115(p, local_dir, savepath)
                        mdc_msg += f'; 已上传 {up} 个 nfo/图片到 115'
                    except Exception as e:
                        mdc_msg += f'; ⚠️ 上传 115 失败: {str(e)[:80]}'
                else:
                    mdc_msg += '; 元数据仅本地(115 只存视频)'
            save_task(task_id, status='running', step='nfo', msg=mdc_msg, thread_id=thread_id, title=title[:300])
            if not mdc_ok:
                save_task(task_id, status='failed', step='scrape', msg='MDCng 重新刮削未成功: ' + mdc_msg,
                          thread_id=thread_id, title=title[:300])
                return
        else:
            # ---- 网页爬取刮削: 清旧 MDCng 元数据 -> 爬色花堂 -> 同步 -> 最终校正 ----
            local_dir = _local_strm_dir(savepath)
            try:
                rm = _remove_mdc_metadata(p, local_dir, savepath)
                save_task(task_id, status='running', step='nfo', msg=f'已清理旧 MDCng 元数据 {rm} 个, 开始网页爬取...',
                          thread_id=thread_id, title=title[:300])
            except Exception as e:
                log.warning('[rescrape] 清理旧元数据失败: %s', e)
            save_task(task_id, status='running', step='scrape', msg='网页爬取刮削(过CF+抓首楼+直写本地nfo/图片)...',
                      thread_id=thread_id, title=title[:300])
            rc, lines = run_scrape_process(thread_id, keep_small=True, local_only=not META_UPLOAD_115, force=True)
            if rc != 0:
                save_task(task_id, status='failed', step='scrape',
                          msg=f'网页爬取进程退出码 {rc}, 刮削失败(查看 108 日志)',
                          thread_id=thread_id, title=title[:300])
                return
            # 二次同步循环: 把 115 上 nfo/图片 copy 到本地 (SmartStrm 重启/瞬断会丢 webhook)
            last_err = ''
            synced = False
            if not META_UPLOAD_115 and _has_local_metadata(_local_strm_dir(savepath))[0]:
                # 2026-08-14 方案: scrape 已直写本地, 无需 a_task 全扫(省 115 访问)
                save_task(task_id, status='running', step='scan',
                          msg='元数据已直写本地(115 只存视频), 跳过 SmartStrm 全扫同步',
                          thread_id=thread_id, title=(title or '')[:300])
                synced = True
            for _i in range(4):
                if synced:
                    break
                try:
                    r2 = _trigger_smartstrm_sync(savepath)
                    save_task(task_id, status='running', step='nfo',
                              msg='SmartStrm 二次同步已触发: ' + json.dumps(r2, ensure_ascii=False)[:120],
                              thread_id=thread_id, title=title[:300])
                    ok2, n2, i2 = _has_local_metadata(local_dir)
                    if ok2:
                        save_task(task_id, status='running', step='scan',
                                  msg=f'元数据已同步到本地: {n2} nfo, {i2} 图片',
                                  thread_id=thread_id, title=title[:300])
                        synced = True
                        break
                    last_err = f'本地元数据尚未出现(第{_i+1}次, {n2} nfo/{i2} 图片)'
                except Exception as e:
                    last_err = str(e)[:100]
                    save_task(task_id, status='running', step='scan',
                              msg=f'SmartStrm 二次同步触发失败(第{_i+1}次): ' + last_err,
                              thread_id=thread_id, title=title[:300])
                if _i < 3:
                    time.sleep(8)
            if not synced:
                save_task(task_id, status='running', step='scan',
                          msg='⚠️ 本地元数据未同步(可能 SmartStrm 未触发): ' + last_err,
                          thread_id=thread_id, title=title[:300])
            # 4.6 最终校正: MDCng watcher 若因 nfo 缺失重刮出 MDCng 风格 nfo, 用网页元数据覆盖(不删除)
            for _i in range(6):
                time.sleep(20)
                if _detect_mdc_nfo(local_dir):
                    if _overwrite_local_meta(savepath, thread_id):
                        save_task(task_id, status='running', step='scan', msg='已用网页元数据覆盖 MDCng 残留(nfo/图片)',
                                  thread_id=thread_id, title=title[:300])
                    break
                ok2, n2, i2 = _has_local_metadata(local_dir)
                if ok2:
                    break

        # 4.9 剧集: 同步 nfo/图片 到剧集目录
        if show_dir:
            try:
                n = _tv_sync_meta_to_showdir(show_dir, _local_strm_dir(savepath))
                if n:
                    save_task(task_id, status='running', step='scan',
                              msg=f'已同步 {n} 个 nfo/图片到剧集目录',
                              thread_id=thread_id, title=title[:300])
            except Exception as e:
                log.warning('[rescrape] 同步剧集元数据失败: %s', str(e)[:150])

        # 4.9b 剧集兜底迁移 (2026-08-16 328115): 无论新增重命名是否成功, 确保
        # emby_tv 剧集目录有 strm + 元数据 (SmartStrm 输出在 /strm/tv, Emby 不监视)
        if (savepath or '').startswith(IMPORT_TV_ROOT + '/'):
            try:
                _moved, _n_meta, _nfo_ok = _tv_ensure_showdir_final(
                    p, task_id, thread_id, savepath, title, _local_strm_dir(savepath), show_dir)
                if _moved or _n_meta or _nfo_ok:
                    save_task(task_id, status='running', step='scan',
                              msg=f'剧集目录就绪: 迁移 {_moved} strm, 同步 {_n_meta} 个元数据, tvshow.nfo {"✓" if _nfo_ok else "沿用"}',
                              thread_id=thread_id, title=title[:300])
            except Exception as e:
                log.warning('[rescrape] 剧集兜底迁移失败: %s', str(e)[:150])

        # 4.9c strm 就位兜底 (2026-08-16 影片版): 确保 Emby 扫库前本地已有 strm
        try:
            _sr = _ensure_strms_ready(p, task_id, thread_id, savepath, items, title)
            if _sr.get('triggered'):
                save_task(task_id, status='running', step='scan',
                          msg=f'strm 就位: 期望 {_sr["expected"]} 实际 {_sr["found"]}, 补触发 SmartStrm 等待 {_sr["waited"]}s',
                          thread_id=thread_id, title=title[:300])
        except Exception as e:
            log.warning('[rescrape] strm 就位兜底失败: %s', str(e)[:150])

        # 5. 验证 + Emby 扫库
        time.sleep(10)
        try:
            code = _trigger_emby_scan()
            save_task(task_id, status='done', step='scan',
                      msg=f'重新刮削完成({way}), Emby 扫库已触发({code}), 稍后刷新可见',
                      thread_id=thread_id, title=title[:300])
        except Exception as e:
            save_task(task_id, status='done', step='scan',
                      msg=f'重新刮削完成({way}); Emby 扫库触发失败: ' + str(e)[:120],
                      thread_id=thread_id, title=title[:300])
    except Exception as e:
        log.exception('rescrape failed')
        save_task(task_id, status='failed', step='error', msg='重新刮削异常: ' + str(e)[:200],
                  thread_id=thread_id, title=title[:300])

def start_rescrape(thread_id, kind='web'):
    task_id = uuid.uuid4().hex[:12]
    save_task(task_id, status='queued', step='init', msg='重新刮削任务已入队, 等待串行执行',
              thread_id=str(thread_id or ''), title=f'thread_{thread_id}', kind=kind or '')
    _submit_post(run_rescrape, (task_id, thread_id, kind))
    return task_id

# ============================== 归为剧集 (超多视频 -> 剧集) ==============================
TV_STRM_ROOT = '/mnt/g/srtm/待看/sehuatang_tv'   # 本地剧集 strm 根 (用户整理后移入; MDCng 只 watch 待看/sehuatang)

def _simplify_title(title, fallback):
    """帖子标题 -> 精简剧集名: 去站点/板块后缀、【115ed2k】【22g 24v 24配额】等标签, 消毒并截断"""
    t = (title or '').strip()
    if not t:
        return fallback
    # 去站点/板块后缀 (如 " - 资源出售区" / " - AI专区" / " - 色花堂")
    t = re.split(r'\s+-\s+(?:资源出售区|AI专区|色花堂|98堂|原色花堂|Free|综合区|讨论区|交流区|求片区|举报区|公告区|Powered by Discuz)', t)[0].strip()
    # 去掉任意位置含 ed2k/磁力/容量/配额/破解/清晰度 等特征的【标签】
    tag_re = re.compile(r'ed2k|ED2K|磁力|magnet|115|[0-9]+\s*[GgMm]|[0-9]+\s*[Vv]|配额|破解|增强|Lada|FHD|4K|1080|720')
    t = re.sub(r'【([^】]+)】', lambda m: '' if tag_re.search(m.group(1)) else m.group(0), t)
    t = re.sub(r'\s+', ' ', t).strip(' -_')
    t = re.sub(r'[\\/:*?"<>|]', '_', t)
    t = t[:60].strip(' -_') or fallback
    return t

def _tv_episode_match(tid, fn):
    """判断文件名是否为该剧集的集文件 (兼容旧格式 thread_{tid}_S01E01 和新格式 原名.thread_{tid}.S01E01)。
    Emby 通过文件名中的 SxxExx 提取集数, 两种格式都能识别。"""
    return bool(re.search(r'(?:^|[._])thread_%s[._]?S\d{2}E' % re.escape(str(tid)), fn, re.I))

def _tv_ep_new_name(tid, ep, ext, orig_name=''):
    """生成剧集集文件新名 (2026-08-20 用户规则): 保持原有文件名 + thread_{tid}.S01E{ep} 后缀,
    如 原名.thread_123.S01E01.mp4。原名消毒/截断(>100 字符), 原名缺失时退化为 thread_{tid}.S01E01。
    2026-08-21: 重命名时去除文件名中全部 www.98t.la@ 广告标识 (不区分大小写, 含 98t.la@ 变体)"""
    base = os.path.splitext(orig_name or '')[0] if orig_name else ''
    base = re.sub(r'(?:www\.)?98t\.la@', '', base, flags=re.I)
    base = re.sub(r'[\\/:*?"<>|]', '_', base)
    base = re.sub(r'\s+', ' ', base)
    base = base.strip().strip(' ._-')
    if len(base) > 100:
        base = base[:100].rstrip(' ._-')
    if base:
        return f'{base}.thread_{tid}.S01E{ep:02d}{ext}'
    return f'thread_{tid}.S01E{ep:02d}{ext}'

def _make_tvshow_nfo(nfo_path, show):
    """从网页爬取的电影 nfo (115 同步到本地的 movie.nfo 等) 提取简介, 生成 tvshow.nfo 内容
    (用户规则 2026-08-12: 剧集名称用精简标题 show, 不用原始帖子标题)"""
    title, plot, outline, premiered, year, rating = show, '', '', '', '', ''
    try:
        with open(nfo_path, 'r', encoding='utf-8', errors='replace') as fh:
            c = fh.read(20000)
        def grab(tag):
            m = re.search(r'<%s>(.*?)</%s>' % (tag, tag), c, re.S)
            return m.group(1).strip() if m else ''
        title = show   # 精简标题
        plot = grab('plot')
        outline = grab('outline') or plot
        premiered = grab('premiered')
        year = grab('year')
        rating = grab('rating')
    except Exception:
        pass
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tvshow>']
    def add(tag, val):
        if val:
            out.append('  <{0}>{1}</{0}>'.format(tag, val))
    add('title', title)
    add('plot', plot)
    add('outline', outline)
    add('premiered', premiered)
    add('year', year)
    add('rating', rating)
    out.append('</tvshow>')
    return '\n'.join(out)

def run_to_tv(task_id, thread_id, title=None):
    """把 thread 归为剧集 (用户规则 2026-08-12: 非番号=剧集, 即使只有 1 个视频也归为剧集):
    0) 整个 thread 文件夹移动到剧集媒体库目录 /sehuatang_tv/ (若还在 /sehuatang/ 下)
    1) 115 视频重命名 thread_{tid}_S01E01..S01E0N (剧集名=精简标题, 文件名用 thread_id 前缀)
    2) 不做网页爬取: 剧集元数据由用户油猴脚本补充 (MDC 无法刮削, 云主机无浏览器)
    3) SmartStrm tv 任务生成 strm 到 /strm/tv/thread_x + 同步附加文件到本地
    4) 新名 strm 移入 /opt/media/strm/emby_tv/<精简标题>/
    5) 任务完成待用户油猴补充元数据: 上传 /api/metadata 写 poster/tvshow.nfo + Emby 刷新 + 预热"""
    p = Push115()
    thread_id = str(thread_id or '').strip()
    if not thread_id:
        save_task(task_id, status='failed', step='error', msg='缺少 thread_id')
        return
    savepath = f'{IMPORT_TV_ROOT}/thread_{thread_id}'
    if not title:
        title = f'thread_{thread_id}'
        try:
            c = sqlite3.connect(LOG_DB)
            r = c.execute("SELECT title FROM import_log WHERE thread_id=? AND task_id<>? AND title IS NOT NULL AND title!='' ORDER BY created_at DESC LIMIT 1",
                          (thread_id, task_id)).fetchone()
            if r and r[0]:
                title = r[0]
            c.close()
        except Exception:
            pass
    show = _simplify_title(title, f'thread_{thread_id}')
    tv_dir = os.path.join(TV_STRM_ROOT, show)
    save_task(task_id, status='running', step='wait', msg=f'归为剧集开始 (thread_{thread_id}, 剧集名: {show})',
              thread_id=thread_id, title=title[:300])
    try:
        fs = p._fs_client()
        # 0.0 整个 thread 文件夹移动到剧集媒体库目录 (若还在影片目录 /sehuatang/ 或 /sehuatang/<分类>/ 下)
        #     _find_thread_path 兼容 2026-09-06 的分类目录布局
        cur_path = _find_thread_path(thread_id)
        if cur_path != savepath and fs.exists(cur_path) and not fs.exists(savepath):
            save_task(task_id, status='running', step='move',
                      msg=f'移动 thread 文件夹到剧集媒体库: {cur_path} -> {savepath}',
                      thread_id=thread_id, title=title[:300])
            if fs.rename(cur_path, savepath):
                log.info('[totv] 已移动: %s -> %s', cur_path, savepath)
                time.sleep(2)
            else:
                save_task(task_id, status='failed', step='move',
                          msg=f'移动 thread 文件夹失败: {cur_path} -> {savepath}',
                          thread_id=thread_id, title=title[:300])
                return
        elif not fs.exists(savepath):
            save_task(task_id, status='failed', step='move',
                      msg=f'115 目录不存在: {savepath} (旧位置 /sehuatang/<分类>/ 或 /sehuatang/ 下也不存在)',
                      thread_id=thread_id, title=title[:300])
            return
        # 0. 校验 115 目录有视频
        items = p.list_dir(savepath, maxdepth=4, use_cache=False)
        vids = sorted([it[1] for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS])
        if not vids:
            save_task(task_id, status='failed', step='scrape', msg=f'115 目录 thread_{thread_id} 无视频文件',
                      thread_id=thread_id, title=title[:300])
            return
        n = len(vids)
        save_task(task_id, status='running', step='rename', msg=f'{n} 个视频, 重命名为 thread_{thread_id}_S01E01..S01E{n:02d}...',
                  thread_id=thread_id, title=title[:300])
        # 1. 重命名 115 视频 (2026-08-13 修): 已 S01E 命名的保留原编号(不重复改名),
        #    未命名的视频从"已有最大集数+1"开始接续编号——
        #    同 thread 多链接分次入库时, 旧视频保持集数, 新视频追加到后面;
        #    此前按文件名排序全部重编号, 会把已发布的 S01E01 改乱 (3685628 案例)
        # 2026-08-20 改: 文件名用 {原名}.thread_{tid}_S01E{ep} 格式 (保持原有文件名 + thread+S01E 后缀,
        #    用户规则; 旧格式 thread_{tid}_S01E{ep} 不再生成, 已入库旧集保留不动; Emby 从 SxxExx 提取集数)
        EP_RE = re.compile(r'[._]S\d{2}E(\d{2,})\.', re.I)
        named_eps = {}   # 已有集数 -> 文件名
        unnamed = []     # 未命名视频
        for name in vids:
            m = EP_RE.search(name)
            if m:
                named_eps[int(m.group(1))] = name
            else:
                unnamed.append(name)
        next_ep = (max(named_eps) + 1) if named_eps else 1
        fs = p._fs_client()
        renamed = 0
        already = True
        for name in unnamed:
            ext = os.path.splitext(name)[1].lower()
            newname = _tv_ep_new_name(thread_id, next_ep, ext, name)
            next_ep += 1
            if name != newname:
                already = False
                try:
                    if fs.rename(f'{savepath}/{name}', f'{savepath}/{newname}'):
                        renamed += 1
                        log.info('[totv] rename %s -> %s', name[:50], newname)
                        time.sleep(2)   # 115 风控: 慢一点, 不频繁
                except Exception as e:
                    log.warning('[totv] rename 失败 %s: %s', name[:50], str(e)[:120])
        if already:
            save_task(task_id, status='running', step='scrape', msg='文件已是 S01E 命名, 跳过重命名',
                      thread_id=thread_id, title=title[:300])
        else:
            save_task(task_id, status='running', step='scrape', msg=f'115 重命名完成 {renamed}/{len(unnamed)}',
                      thread_id=thread_id, title=title[:300])
        # 2. 剧集元数据: 不做网页爬取 (云主机无浏览器/xvfb, CF 拦截; MDC 对剧集也无法刮削) (2026-08-20)
        #    由用户用油猴脚本在帖子页补充: 上传 /api/metadata 自动写 poster/tvshow.nfo + Emby 刷新 + 预热。
        save_task(task_id, status='running', step='scrape', msg='剧集元数据待用户油猴脚本补充(不自动刮削)...',
                  thread_id=thread_id, title=title[:300])
        # 3. SmartStrm 生成新 strm + 同步 nfo/图片到本地
        save_task(task_id, status='running', step='strm', msg='触发 SmartStrm 重新生成 strm...',
                  thread_id=thread_id, title=title[:300])
        try:
            _trigger_smartstrm(savepath)
        except Exception as e:
            log.warning('[totv] smartstrm trigger 失败: %s', str(e)[:120])
        local_dir = _local_strm_dir(savepath)
        # 3.1 等待新名 strm 生成 (cs_strm 处理有延迟; 旧 nfo/图片存在时不能提前 break)
        got_new = 0
        for _i in range(40):
            time.sleep(6)
            try:
                files = os.listdir(local_dir)
            except Exception:
                files = []
            new_strms = [f for f in files if _tv_episode_match(thread_id, f) and f.endswith('.strm')]
            if len(new_strms) >= n:
                got_new = len(new_strms)
                break
            if _i % 2 == 1:
                save_task(task_id, status='running', step='strm',
                          msg=f'等待 SmartStrm 生成新 strm: {len(new_strms)}/{n} (第{(_i+1)*6}s)',
                          thread_id=thread_id, title=title[:300])
        if got_new < n:
            # 再触发 a_task 同步事件兜底 (该事件也会触发任务运行生成 strm)
            for _i in range(6):
                try:
                    _trigger_smartstrm_sync(savepath)
                except Exception:
                    pass
                time.sleep(30)   # 2026-08-21: 拉大间隔避免打断 SmartStrm 复制图片
                try:
                    files = os.listdir(local_dir)
                except Exception:
                    files = []
                new_strms = [f for f in files if _tv_episode_match(thread_id, f) and f.endswith('.strm')]
                if len(new_strms) >= n:
                    got_new = len(new_strms)
                    break
        if got_new < n:
            save_task(task_id, status='failed', step='strm',
                      msg=f'SmartStrm 未生成新名 strm (仅 {got_new}/{n}), 请检查 SmartStrm webhook/日志',
                      thread_id=thread_id, title=title[:300])
            return
        save_task(task_id, status='running', step='nfo', msg=f'SmartStrm 已生成 {got_new} 个新 strm',
                  thread_id=thread_id, title=title[:300])
        # 3.2 剧集无服务器元数据 (不自动刮削), 无需 SmartStrm 附加文件同步 (2026-08-20)
        save_task(task_id, status='running', step='nfo', msg='剧集元数据待油猴补充, 跳过 SmartStrm 元数据同步',
                  thread_id=thread_id, title=title[:300])
        # 4. 移动新名 strm 到剧集目录 (无元数据: poster/tvshow.nfo 由用户油猴补充时写入)
        #    先清空旧剧集 strm (防新旧混合, emby_delete_sync 已禁用)
        if os.path.isdir(tv_dir):
            try:
                shutil.rmtree(tv_dir, ignore_errors=True)
                log.info('[totv] 已清空旧剧集目录: %s', tv_dir)
            except Exception as e:
                log.warning('[totv] 清空旧剧集目录失败 %s: %s', tv_dir, str(e)[:100])
        os.makedirs(tv_dir, exist_ok=True)
        moved = 0
        for fn in sorted(os.listdir(local_dir)):
            full = os.path.join(local_dir, fn)
            if not os.path.isfile(full):
                continue
            fl = fn.lower()
            if _tv_episode_match(thread_id, fn) and fl.endswith('.strm'):
                shutil.move(full, os.path.join(tv_dir, fn))
                moved += 1
        if moved == 0:
            save_task(task_id, status='failed', step='scan', msg='本地 strm 目录没有可移动的新名 strm 文件, 剧集化失败',
                      thread_id=thread_id, title=title[:300])
            return
        # 4.1 清理 local_dir 残留 .strm (旧名/上一轮名, 指向已改名的 115 文件 -> 失效;
        #     本地删除后 Emby 扫库移除条目, SmartStrm emby_delete_sync 联动删 115 旧路径(不存在)失败无害)
        removed = 0
        for fn in os.listdir(local_dir):
            if fn.endswith('.strm'):
                try:
                    os.remove(os.path.join(local_dir, fn))
                    removed += 1
                except Exception as e:
                    log.warning('[totv] 清理残留 strm 失败 %s: %s', fn[:40], str(e)[:80])
        if removed:
            log.info('[totv] 清理本地残留 strm %d 个', removed)
        # 5. 完成: 剧集 strm 已就位, 元数据等待用户油猴补充 (2026-08-20)
        #    用户上传 /api/metadata 时自动写 poster/tvshow.nfo + 触发 Emby 刷新 + 预热,
        #    因此这里不 Emby 扫库、不预热 (补充后条目才在 Emby 出现)。
        save_task(task_id, status='done', step='strm',
                  msg=f'剧集 strm 已就位: {show} 共 {moved} 集 (thread #{thread_id}), 请在帖子页用油猴脚本补充元数据, 补充后自动刷新 Emby+预热',
                  thread_id=thread_id, title=title[:300])
        log.info('[totv] 剧集 strm 就位, 待用户油猴补充元数据: %s (task=%s)', tv_dir, task_id)
    except Exception as e:
        log.exception('to_tv failed')
        save_task(task_id, status='failed', step='error', msg='归为剧集异常: ' + str(e)[:200],
                  thread_id=thread_id, title=title[:300])

def start_to_tv(thread_id):
    task_id = uuid.uuid4().hex[:12]
    save_task(task_id, status='queued', step='init', msg='归为剧集任务已入队, 等待串行执行',
              thread_id=str(thread_id or ''), title=f'thread_{thread_id}', kind='non_fanhao')
    _submit_post(run_to_tv, (task_id, thread_id))
    return task_id


# ============================== 剧集补充视频重跑整理 (2026-08-20) ==============================
# 用户需求: 往 115 剧集目录(/sehuatang_tv/thread_x)补充视频后, 点按钮重新跑"落地后流程":
#   1) 未命名视频重命名 thread_x_S01E{续集号} 接续编号 (已 S01E 命名的不动)
#   2) 触发 SmartStrm 重新生成 strm (全量)
#   3) 新增 strm 移入 emby_tv/<精简标题>/ (不清空旧目录, 不重建旧集)
#   4) Emby 刷新, 完成
def run_tv_refresh(task_id, thread_id, title=None):
    p = Push115()
    thread_id = str(thread_id or '').strip()
    if not thread_id:
        save_task(task_id, status='failed', step='error', msg='缺少 thread_id')
        return
    savepath = f'{IMPORT_TV_ROOT}/thread_{thread_id}'
    if not title:
        title = f'thread_{thread_id}'
        try:
            c = sqlite3.connect(LOG_DB)
            r = c.execute("SELECT title FROM import_log WHERE thread_id=? AND title IS NOT NULL AND title!='' ORDER BY created_at DESC LIMIT 1",
                          (thread_id,)).fetchone()
            if r and r[0]:
                title = r[0]
            c.close()
        except Exception:
            pass
    show = _simplify_title(title, f'thread_{thread_id}')
    tv_dir = os.path.join(TV_STRM_ROOT, show)
    save_task(task_id, status='running', step='wait',
              msg=f'剧集补充整理开始 (thread_{thread_id}, 剧集名: {show})',
              thread_id=thread_id, title=title[:300], kind='non_fanhao')
    try:
        fs = p._fs_client()
        # 目录兼容 (2026-08-20 + 2026-09-06): 落地后停住等手工整理的剧集若目录还在影片库
        # /sehuatang/ 或 /sehuatang/<分类>/ 下(旧任务), 先移到剧集媒体库
        cur_path = _find_thread_path(thread_id)
        if cur_path != savepath and fs.exists(cur_path) and not fs.exists(savepath):
            save_task(task_id, status='running', step='move',
                      msg=f'移动 thread 文件夹到剧集媒体库: {cur_path} -> {savepath}',
                      thread_id=thread_id, title=title[:300])
            if fs.rename(cur_path, savepath):
                log.info('[tvref] 已移动: %s -> %s', cur_path, savepath)
                time.sleep(2)
            else:
                save_task(task_id, status='failed', step='move',
                          msg=f'移动 thread 文件夹失败: {cur_path} -> {savepath}',
                          thread_id=thread_id, title=title[:300])
                return
        elif not fs.exists(savepath):
            save_task(task_id, status='failed', step='wait',
                      msg=f'115 目录不存在: {savepath} (旧位置 /sehuatang/<分类>/ 或 /sehuatang/ 下也不存在)',
                      thread_id=thread_id, title=title[:300])
            return
        # 1. 列出视频, 未命名视频重命名接续编号 (逻辑同 run_to_tv step1)
        items = p.list_dir(savepath, maxdepth=4, use_cache=False)
        vids = sorted([it[1] for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS])
        if not vids:
            save_task(task_id, status='failed', step='wait',
                      msg=f'115 目录 thread_{thread_id} 无视频文件',
                      thread_id=thread_id, title=title[:300])
            return
        EP_RE = re.compile(r'[._]S\d{2}E(\d{2,})\.', re.I)
        named_eps, unnamed = {}, []
        for name in vids:
            m = EP_RE.search(name)
            if m:
                named_eps[int(m.group(1))] = name
            else:
                unnamed.append(name)
        next_ep = (max(named_eps) + 1) if named_eps else 1
        renamed = 0
        for name in unnamed:
            ext = os.path.splitext(name)[1].lower()
            newname = _tv_ep_new_name(thread_id, next_ep, ext, name)
            next_ep += 1
            if name != newname:
                try:
                    if fs.rename(f'{savepath}/{name}', f'{savepath}/{newname}'):
                        renamed += 1
                        log.info('[tvref] rename %s -> %s', name[:50], newname)
                        time.sleep(2)   # 115 风控: 慢一点
                except Exception as e:
                    log.warning('[tvref] rename 失败 %s: %s', name[:50], str(e)[:120])
        if renamed:
            save_task(task_id, status='running', step='rename',
                      msg=f'新视频重命名完成 {renamed}/{len(unnamed)} (续集号接续)',
                      thread_id=thread_id, title=title[:300])
        else:
            save_task(task_id, status='running', step='rename',
                      msg='无需重命名 (视频已全部 S01E 命名)',
                      thread_id=thread_id, title=title[:300])
        # 2. SmartStrm 生成 strm (等待全部数量)
        save_task(task_id, status='running', step='strm', msg='触发 SmartStrm 生成新 strm...',
                  thread_id=thread_id, title=title[:300])
        try:
            _trigger_smartstrm(savepath)
        except Exception as e:
            log.warning('[tvref] smartstrm trigger 失败: %s', str(e)[:120])
        local_dir = _local_strm_dir(savepath)
        n = len(vids)
        got_new = 0
        for _i in range(40):
            time.sleep(6)
            try:
                files = os.listdir(local_dir)
            except Exception:
                files = []
            new_strms = [f for f in files if _tv_episode_match(thread_id, f) and f.endswith('.strm')]
            if len(new_strms) >= n:
                got_new = len(new_strms)
                break
            if _i % 2 == 1:
                save_task(task_id, status='running', step='strm',
                          msg=f'等待 SmartStrm 生成新 strm: {len(new_strms)}/{n} (第{(_i+1)*6}s)',
                          thread_id=thread_id, title=title[:300])
        if got_new < n:
            for _i in range(6):
                try:
                    _trigger_smartstrm_sync(savepath)
                except Exception:
                    pass
                time.sleep(30)   # 2026-08-21: 拉大间隔避免打断 SmartStrm 复制图片
                try:
                    files = os.listdir(local_dir)
                except Exception:
                    files = []
                new_strms = [f for f in files if _tv_episode_match(thread_id, f) and f.endswith('.strm')]
                if len(new_strms) >= n:
                    got_new = len(new_strms)
                    break
        if got_new < n:
            save_task(task_id, status='failed', step='strm',
                      msg=f'SmartStrm 未生成新名 strm (仅 {got_new}/{n}), 请检查 SmartStrm webhook/日志',
                      thread_id=thread_id, title=title[:300])
            return
        save_task(task_id, status='running', step='nfo', msg=f'SmartStrm 已生成 {got_new} 个 strm',
                  thread_id=thread_id, title=title[:300])
        # 3. 新增 strm 移入剧集目录 (不清空旧目录; 已存在同名则跳过)
        os.makedirs(tv_dir, exist_ok=True)
        moved = 0
        for fn in sorted(os.listdir(local_dir)):
            full = os.path.join(local_dir, fn)
            if not os.path.isfile(full):
                continue
            if _tv_episode_match(thread_id, fn) and fn.lower().endswith('.strm'):
                dst = os.path.join(tv_dir, fn)
                if os.path.exists(dst):
                    continue
                shutil.move(full, dst)
                moved += 1
        # 清理 local_dir 残留 strm (旧集已移走过, 重新生成的副本留在原地, 无害但清理)
        try:
            for fn in os.listdir(local_dir):
                if fn.endswith('.strm'):
                    try:
                        os.remove(os.path.join(local_dir, fn))
                    except Exception:
                        pass
        except Exception:
            pass
        if moved == 0:
            cur_n = len([f for f in os.listdir(tv_dir) if f.lower().endswith('.strm')]) if os.path.isdir(tv_dir) else 0
            save_task(task_id, status='done', step='strm',
                      msg=f'没有发现新增视频的 strm (可能已同步过), 剧集当前共 {cur_n} 集',
                      thread_id=thread_id, title=title[:300])
            return
        # 4. Emby 刷新
        save_task(task_id, status='running', step='scan',
                  msg=f'新增 {moved} 集 strm 已就位, 刷新 Emby...',
                  thread_id=thread_id, title=title[:300])
        try:
            _trigger_emby_scan()
        except Exception as e:
            log.warning('[tvref] emby refresh 失败: %s', str(e)[:120])
        save_task(task_id, status='done', step='scan',
                  msg=f'剧集补充整理完成: 新增 {moved} 集 → {show} (thread #{thread_id}); SmartStrm 生成 {got_new} 个 strm, Emby 已刷新; 若无元数据请油猴补充',
                  thread_id=thread_id, title=title[:300])
        log.info('[tvref] 剧集补充完成: %s +%d 集 (task=%s)', show, moved, task_id)
    except Exception as e:
        log.exception('tv_refresh failed')
        save_task(task_id, status='failed', step='error', msg='剧集补充整理异常: ' + str(e)[:200],
                  thread_id=thread_id, title=title[:300])


def start_tv_refresh(thread_id, title=None):
    task_id = uuid.uuid4().hex[:12]
    save_task(task_id, status='queued', step='init', msg='剧集补充整理任务已入队, 等待串行执行',
              thread_id=str(thread_id or ''), title=(title or f'thread_{thread_id}')[:300], kind='non_fanhao')
    _submit_post(run_tv_refresh, (task_id, thread_id, title))
    return task_id

# ============================== HTTP ==============================
# 首页 HTML 已抽到 index_page.py (2026-09-20): 批量入库页 = 多磁链 + 分类下拉 + 解析预览 + 批次进度。
# 分类表由 render_index_page(CATEGORY_MAP, DEFAULT_CATEGORY) 在请求时注入, 前端不硬编码分类名。


TASKS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>入库任务监控</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.header h1{font-size:20px;color:#58a6ff}
.header .tag{font-size:12px;color:#8b949e;background:#21262d;padding:3px 10px;border-radius:12px}
.header .spacer{flex:1}
.container{max-width:1440px;margin:0 auto;padding:20px 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.card .num{font-size:26px;font-weight:700;margin-top:4px}
.card .lbl{font-size:12px;color:#8b949e}
.card.total .num{color:#58a6ff}.card.queued .num{color:#8b949e}
.card.running .num{color:#58a6ff}.card.done .num{color:#3fb950}
.card.failed .num{color:#f85149}
.card.avdbsrc .num{color:#d29922}
.card.pendingmd .num{color:#d29922}
.bar{display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.bar input[type="text"]{flex:1;min-width:220px;padding:8px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;outline:none}
.bar input[type="text"]:focus{border-color:#58a6ff}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:7px 14px;background:#21262d;border:1px solid #30363d;border-radius:20px;color:#8b949e;font-size:13px;cursor:pointer;user-select:none}
.tab.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
/* ===== 按钮统一规范 (2026-09-20)：与 / 、/avdb 三页同一套尺寸/配色 ===== */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:34px;padding:0 15px;border:1px solid transparent;border-radius:6px;font-family:inherit;font-size:13px;font-weight:600;line-height:1;cursor:pointer;white-space:nowrap;color:#c9d1d9;text-decoration:none;transition:background .15s,border-color .15s,color .15s}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.sm{height:30px;padding:0 11px;font-size:12.5px;font-weight:600}
.btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn.primary:hover:enabled{background:#388bfd;border-color:#388bfd}
.btn.ghost{background:#21262d;border-color:#30363d;color:#c9d1d9}
.btn.ghost:hover:enabled{background:#30363d;border-color:#8b949e}
.btn.ok{background:#238636;border-color:#2ea043;color:#fff}
.btn.ok:hover:enabled{background:#2ea043}
.btn.warn{background:#21262d;border-color:#9e6a03;color:#d29922}
.btn.warn:hover:enabled{background:#3d2e00}
.btn.danger{background:#21262d;border-color:#4d2c2c;color:#f85149}
.btn.danger:hover:enabled{background:#3d1418;border-color:#f85149}
.btn.purple{background:#6e40c9;border-color:#6e40c9;color:#fff}
.btn.purple:hover:enabled{background:#8957e5}
.nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* 非当前页导航必须显式声明 color/background：.btn 基础样式没有 color，锚点会回退成浏览器默认
   链接色(未访问 #0000ee / 已访问 #551a8b)，在 #161b22 上只有 1.8:1，看着就是糊在背景上的暗字 */
.nav a.btn{background:#21262d;border-color:#30363d;color:#c9d1d9}
.nav a.btn:hover{background:#30363d;border-color:#8b949e;color:#c9d1d9}
.nav a.btn.cur{background:#193656;border-color:#58a6ff;color:#58a6ff}
.nav a.btn.avdb.cur{background:#483600;border-color:#d29922;color:#d29922}
.plink{color:#58a6ff;font-size:12px;text-decoration:none}
.plink:hover{text-decoration:underline}
.panel-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid #21262d}
.tools{display:flex;gap:8px;align-items:center;margin-left:auto}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d}
/* .tw 承担裁剪与圆角: table 上的 overflow:hidden 会让它自己变成 sticky 的定位容器, 操作列就贴不回屏幕右侧了 */
.tw{overflow-x:auto;border-radius:8px}
/* v1.15.4: 操作列固定在右侧 —— 列多时表格会横向溢出, 原先最右的「✅继续 / 重推115 / 整理」被推到屏幕外, 看不见也点不到 */
.tw th:last-child,.tw td:last-child{position:sticky;right:0;background:#161b22;box-shadow:-8px 0 8px -8px rgba(0,0,0,.75)}
.tw th:last-child{background:#21262d}
.tw tr:hover td:last-child{background:#1c2128}
th,td{padding:10px 10px;text-align:left;border-bottom:1px solid #21262d;font-size:13px;vertical-align:middle}
th{background:#21262d;color:#8b949e;font-weight:600;white-space:nowrap}
tr:hover td{background:#1c2128}
td .t{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td .tid{display:inline-block;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap}
.badge.queued{background:#21262d;color:#8b949e;border:1px solid #30363d}
.badge.running{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb}
.badge.pending_manual{background:#d2992222;color:#d29922;border:1px solid #d29922}
.badge.done{background:#23863622;color:#3fb950;border:1px solid #2ea043}
.badge.failed{background:#f8514922;color:#f85149;border:1px solid #f85149}
.progress{width:110px;height:8px;background:#21262d;border-radius:4px;overflow:hidden}
.progress .fill{height:100%;background:#58a6ff;border-radius:4px;transition:width .4s}
.progress .fill.done{background:#3fb950}
.progress .fill.failed{background:#f85149}
.progress .fill.pending{background:#d29922}
.pct{font-size:12px;color:#8b949e;margin-left:6px;white-space:nowrap}
.msg{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#8b949e}
.pager{display:flex;gap:8px;justify-content:center;align-items:center;margin-top:14px}
.pager span{color:#8b949e;font-size:13px}
.empty{text-align:center;color:#8b949e;padding:40px 0;font-size:14px}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;align-items:flex-start;justify-content:center;padding:40px 16px;overflow-y:auto}
.overlay.show{display:flex}
.dialog{background:#161b22;border:1px solid #30363d;border-radius:10px;max-width:760px;width:100%;padding:20px 24px}
.dialog h2{font-size:16px;color:#58a6ff;margin-bottom:12px}
.dialog .close{float:right;cursor:pointer;color:#8b949e;font-size:18px;background:none;border:none}
.dialog .meta{font-size:13px;color:#8b949e;line-height:1.9;word-break:break-all}
.dialog .meta b{color:#c9d1d9}
.timeline{margin-top:14px;border-left:2px solid #30363d;padding-left:14px;max-height:380px;overflow-y:auto}
.tl-item{position:relative;padding:6px 0 12px}
.tl-item::before{content:'';position:absolute;left:-19px;top:11px;width:10px;height:10px;border-radius:50%;background:#21262d;border:2px solid #8b949e}
.tl-item .ts{font-size:11px;color:#8b949e}
.tl-item .msg{font-size:13px;color:#c9d1d9;margin-top:2px;white-space:pre-wrap;max-width:100%}
.tl-item .bdg{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.tl-item .bdg.queued{background:#21262d;color:#8b949e;border:1px solid #30363d}
.tl-item .bdg.running{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb}
.tl-item .bdg.done{background:#23863622;color:#3fb950;border:1px solid #2ea043}
.tl-item .bdg.failed{background:#f8514922;color:#f85149;border:1px solid #f85149}
.autoflag{font-size:12px;color:#8b949e;display:flex;align-items:center;gap:6px;margin-left:8px}
input[type=checkbox]{accent-color:#1f6feb}
.rescrape{margin-top:14px;padding:12px;background:#21262d;border:1px solid #30363d;border-radius:8px}
.rescrape .rs-thread{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 12px;margin-bottom:10px}
.rescrape .rs-ttl{font-size:14px;color:#e6edf3;font-weight:600;line-height:1.5;max-height:42px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-all}
.rescrape .rs-tid{font-size:12px;color:#58a6ff;margin-top:4px}
.rescrape .rs-title{font-size:13px;color:#8b949e;margin-bottom:8px}
.rescrape .rs-btns{display:flex;gap:8px;flex-wrap:wrap}
.rs-btns button{display:inline-flex;align-items:center;justify-content:center;height:30px;padding:0 12px;border-radius:6px;border:none;cursor:pointer;font-weight:600;font-size:12.5px;line-height:1;color:#fff;font-family:inherit;transition:background .15s}
.rs-btns .rs-mdc{background:#6e40c9}
.rs-btns .rs-mdc:hover{background:#8957e5}
.rs-btns .rs-web{background:#1f6feb}
.rs-btns .rs-web:hover{background:#388bfd}
.rs-btns .rs-tv{background:#238636}
.rs-btns .rs-tv:hover{background:#2ea043}
.rs-btns .rs-repush{background:#d64045}
.rs-btns .rs-repush:hover{background:#f2555a}
.rs-btns .rs-confirm{background:#1a7f37}
.rs-btns .rs-confirm:hover{background:#238636}
.rescrape .rs-hint{font-size:12px;color:#8b949e;margin-top:6px}
/* ===== A/B/C 组增强 (2026-09-20) ===== */
.card{cursor:pointer;transition:border-color .15s,background .15s}
.card:hover{border-color:#8b949e}
.card.on{border-color:#58a6ff;background:#1f6feb14;box-shadow:0 0 0 1px #58a6ff inset}
.card .num{font-variant-numeric:tabular-nums}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:#c9d1d9}
th.sortable .ar{font-size:10px;margin-left:4px;color:#58a6ff}
tr.row-failed td{background:#f851490d}
tr.row-failed td:first-child{box-shadow:inset 3px 0 0 #f85149}
tr.row-done td{background:#2386360a}
.badge.running{animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.catbadge{display:inline-block;padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap;border:1px solid}
.catbadge.c-av{background:#1f6feb22;color:#58a6ff;border-color:#1f6feb}
.catbadge.c-fc2{background:#6e40c922;color:#a371f7;border-color:#6e40c9}
.catbadge.c-sw{background:#db61a222;color:#db61a2;border-color:#db61a2}
.catbadge.c-cn{background:#d2992222;color:#d29922;border-color:#d29922}
.catbadge.c-ea{background:#23863622;color:#3fb950;border-color:#2ea043}
.catbadge.c-lf{background:#f8514922;color:#f85149;border-color:#f85149}
.catbadge.c-none{background:#21262d;color:#8b949e;border-color:#30363d}
.sel{height:34px;padding:0 10px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-family:inherit;font-size:13px;outline:none;cursor:pointer}
.sel:focus{border-color:#58a6ff}
.bar2{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.bar2 .spacer{flex:1}
.bar{margin-bottom:10px}
.msgcell{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#8b949e;cursor:default}
.msgcell.open{white-space:normal;max-width:320px;word-break:break-all}
.msg-wrap{display:flex;gap:6px;align-items:flex-start}
.msg-more{background:none;border:none;color:#58a6ff;cursor:pointer;font-size:12px;padding:0;font-family:inherit;flex:none}
.empty .eacts{margin-top:14px;display:flex;gap:8px;justify-content:center}
.btn:focus-visible,.tab:focus-visible,.sel:focus-visible,.msg-more:focus-visible,.card:focus-visible,th.sortable:focus-visible{outline:2px solid #58a6ff;outline-offset:2px}
/* toast */
.toasts{position:fixed;top:16px;right:16px;z-index:200;display:flex;flex-direction:column;gap:10px;max-width:380px}
.toast{display:flex;gap:10px;align-items:flex-start;padding:11px 14px;border-radius:8px;font-size:13px;line-height:1.5;background:#161b22;border:1px solid #30363d;border-left:4px solid #8b949e;box-shadow:0 6px 22px rgba(0,0,0,.45);animation:tin .18s ease-out}
.toast.ok{border-left-color:#3fb950}
.toast.err{border-left-color:#f85149}
.toast.run{border-left-color:#58a6ff}
.toast.info{border-left-color:#8b949e}
.toast .tx{flex:1;word-break:break-all}
.toast .cls{background:none;border:none;color:#8b949e;cursor:pointer;font-size:14px;line-height:1;padding:0}
@keyframes tin{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}
.overlay.ask{z-index:150}
.dialog.askd{max-width:520px}
.dialog .askbody{font-size:13px;color:#c9d1d9;line-height:1.75;white-space:pre-wrap;word-break:break-all;margin-bottom:16px}
.askbtns{display:flex;gap:8px;justify-content:flex-end}
</style>
</head>
<body>
<div class="header">
  <h1>📦 入库任务监控</h1>
  <span class="tag" id="lastUpdate">-</span>
  <div class="spacer"></div>
  <div class="nav">
    <a class="btn sm" href="/">🚀 一键入库</a>
    <a class="btn sm cur" href="/tasks">📋 任务监控</a>
    <a class="btn sm avdb" href="/avdb">🔗 avdb 连接器</a>
  </div>
</div>
<div class="container">
  <div class="cards" id="cards"></div>
  <div class="bar">
    <div class="tabs" id="tabs">
      <span class="tab active" data-s="">全部</span>
      <span class="tab" data-s="queued">排队</span>
      <span class="tab" data-s="running">运行中</span>
      <span class="tab" data-s="done">已完成</span>
      <span class="tab" data-s="failed">失败</span>
    </div>
    <div class="tabs" id="tabsOrigin">
      <span class="tab active" data-o="">全部来源</span>
      <span class="tab" data-o="sehuatang">色花堂</span>
      <span class="tab" data-o="avdb">🔗 avdb</span>
    </div>
    <div class="tools">
      <label class="autoflag"><input type="checkbox" id="auto" checked> 自动刷新 5s</label>
      <button class="btn ghost" onclick="load(true)">🔄 刷新</button>
    </div>
  </div>
  <div class="bar2">
    <select class="sel" id="catSel"><option value="">全部分类</option></select>
    <input type="text" id="q" placeholder="搜索 thread / 标题 / task_id..." style="flex:1;min-width:220px;padding:8px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;outline:none">
    <button class="btn primary" onclick="searchNow()">搜索</button>
    <button class="btn ghost" id="btnExport" onclick="exportCsv()">⬇ 导出 CSV</button>
    <button class="btn danger" id="btnRetryAll" style="display:none" onclick="retryAll()"></button>
    <div class="spacer"></div>
    <label class="autoflag">每页
      <select class="sel" id="perPage">
        <option value="50">50</option><option value="100">100</option><option value="200">200</option>
      </select>
    </label>
  </div>
  <div class="tw">
  <table>
    <thead>
      <tr><th class="sortable" data-sort="created_at">创建时间<span class="ar"></span></th><th class="sortable" data-sort="thread_id">Thread<span class="ar"></span></th><th class="sortable" data-sort="title">标题<span class="ar"></span></th><th>类型</th><th>来源</th><th class="sortable" data-sort="category">分类<span class="ar"></span></th><th class="sortable" data-sort="status">状态<span class="ar"></span></th><th class="sortable" data-sort="progress">进度<span class="ar"></span></th><th>当前消息</th><th></th></tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  </div>
  <div class="empty" id="empty" style="display:none">
    <div id="emptyText">暂无任务记录</div>
    <div class="eacts">
      <button class="btn ghost sm" onclick="clearFilters()">清除筛选</button>
      <a class="btn primary sm" href="/">🚀 去首页提交</a>
    </div>
  </div>
  <div class="pager" id="pager"></div>
</div>
<div class="toasts" id="toasts"></div>

<div class="overlay ask" id="askOverlay">
  <div class="dialog askd">
    <h2 id="askTitle">确认操作</h2>
    <div class="askbody" id="askBody"></div>
    <div class="askbtns">
      <button class="btn ghost" id="askCancel">取消</button>
      <button class="btn primary" id="askOk">确定</button>
    </div>
  </div>
</div>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeDetail()">
  <div class="dialog">
    <button class="close" onclick="closeDetail()">✕</button>
    <h2 id="dTitle">任务详情</h2>
    <div class="meta" id="dMeta"></div>
    <div class="rescrape" id="dRescrape" style="display:none">
      <div class="rs-thread">
        <div class="rs-ttl" id="rsThreadTitle" title=""></div>
        <div class="rs-tid">thread <span id="rsThreadId"></span></div>
      </div>
      <div class="rs-title">🔄 元数据处理：</div>
      <div class="rs-btns">
        <button class="rs-mdc" onclick="rescrape('mdc')">🛠 MDC 刮削</button>
        <button class="rs-web" onclick="gotoMeta()">📤 油猴补充元数据</button>
      </div>
      <div class="rs-hint">MDC 刮削 = 本地 MDCng 按文件名自动刮削（会创建新任务显示进度）；油猴补充 = 打开色花堂帖子页，用油猴脚本📤上传图片/简介生成 Emby 海报。</div>
    </div>
    <div class="rescrape" id="dConfirmVideo" style="display:none">
      <div class="rs-title">⏳ 离线下载未检测到视频（可能磁力慢 / 下载超时）：</div>
      <div class="rs-btns">
        <button class="rs-confirm" onclick="confirmVideo(curTask.task_id)">✅ 已确认视频在网盘，继续处理</button>
      </div>
      <div class="rs-hint">请先到 115 网盘确认该帖目录下视频已下载完成，再点此按钮。系统将跳过推送/等待，直接从文件处理/刮削/strm 继续。</div>
    </div>
    <div class="rescrape" id="dTvRefresh" style="display:none">
      <div class="rs-title">🔄 剧集整理（落地后流程）：</div>
      <div class="rs-btns">
        <button class="rs-tv" onclick="tvRefresh()">🔄 整理</button>
      </div>
      <div class="rs-hint">请先在 115 网盘手工整理剧集目录(/sehuatang_tv/thread_x)：删除广告/确认分集、补充新视频。点「🔄 整理」后：未命名视频重命名为「原名.thread_x.S01E续集号」→ SmartStrm 生成 strm 软链接 → 移入 Emby 剧集目录并刷新。已 S01E 命名的旧集不动。</div>
    </div>
    <div class="rescrape" id="dRepush" style="display:none">
      <div class="rs-title">📤 重新推送 115（推送 115 失败的任务可断点重续重推）：</div>
      <div class="rs-btns">
        <button class="rs-repush" onclick="repush(curTask.task_id, true)">📤 重推 115</button>
      </div>
      <div class="rs-hint">复用原任务断点重续：若尚未推送成功会重新推送链接到 115；若已有文件落地则从下一步继续。</div>
    </div>
    <div class="timeline" id="dTimeline"></div>
  </div>
</div>

<script>
const STATUS = {queued:'排队', running:'运行中', pending_manual:'待整理', done:'已完成', failed:'失败'};
const KIND_LBL = {fanhao:'影片', non_fanhao:'剧集', metadata:'📤元数据'};
function kindLbl(k){return KIND_LBL[k] || (k ? esc(k) : '-');}
const ORIGIN_LBL = {'':'🀄 色花堂', 'avdb':'🔗 avdb'};
function originLbl(o){return ORIGIN_LBL[o||''] || esc(o);}
const STEP_PCT = {init:5, push:15, wait:40, scrape:60, nfo:80, strm:80, scan:92};
const STEP_LBL = {init:'创建', push:'推送', wait:'等待落地', wait_video:'等待落地', scrape:'刮削', nfo:'元数据', strm:'生成strm', scan:'扫库', move:'移动', error:'异常'};
let curStatus = '', curOrigin = '', curCategory = '', curPage = 1, curTask = null;
let sortKey = 'created_at', sortDir = 'desc';
let lastSig = '', lastKey = '', lastStats = '';
let perPage = (function(){ const v = parseInt(localStorage.getItem('tasksPerPage')||'50', 10); return [50,100,200].indexOf(v)>=0 ? v : 50; })();
const rowMap = new Map();          // task_id -> {tr, cache:{cell:html}}
const expanded = new Set();        // 消息列就地展开状态
const CATS = __CATS_JSON__;
const CAT_LBL = {}; CATS.forEach(c=>{ CAT_LBL[c.key] = c.name; });
const CELLS = ['time','tid','title','kind','origin','cat','status','prog','msg','act'];

function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function isPendingMeta(t){
  // done 但没走到 scan: 只有「MDC 未刮削完整, 等油猴补元数据」这一种终态
  // (import_api.py 兜底分支 save_task(status='done', step='nfo'); to_tv 分支为 step='strm')
  // 2026-09-20 修复: 此前 pct() 对 done 一律返回 100%, 面板显示「已完成/100%」却写「请补充元数据」
  // 2026-09-20 追加: t.superseded = 同一帖已有更新的成功任务 (行内「🔁 重刮」跑完) → 不再算待补
  return t.status==='done' && t.step!=='scan' && !t.superseded;
}
function pct(t){
  if(t.status==='done') return isPendingMeta(t) ? {p: (STEP_PCT[t.step]||80), cls:'pending'} : {p:100, cls:'done'};
  if(t.status==='failed') return {p:100, cls:'failed'};
  const p = STEP_PCT[t.step] || (t.status==='queued'?5:50);
  return {p, cls:''};
}
function statusCell(t){
  if(isPendingMeta(t)) return '<span class="badge pending_manual" title="MDC 刮削不完整: 请到帖子页用油猴脚本补充元数据">待补元数据</span>';
  if(t.status==='done' && t.step!=='scan') return '<span class="badge done" title="该帖此后已有新的成功任务 (已重刮), 旧记录不再计入待补">已完成</span>';
  return badge(t.status);
}
function fmt(s){return s?String(s).slice(0,19).replace('T',' '):'';}
function badge(st){return '<span class="badge '+esc(st)+'">'+(STATUS[st]||esc(st))+'</span>';}
function rel(s){
  if(!s) return '-';
  const t = Date.parse(String(s).replace(' ','T'));
  if(isNaN(t)) return esc(s);
  const d = Math.floor((Date.now()-t)/1000);
  if(d < 0) return '刚刚';
  if(d < 60) return d+' 秒前';
  if(d < 3600) return Math.floor(d/60)+' 分钟前';
  if(d < 86400) return Math.floor(d/3600)+' 小时前';
  if(d < 172800) return '昨天';
  if(d < 2592000) return Math.floor(d/86400)+' 天前';
  return esc(String(s).slice(5,16).replace('T',' '));
}
function catBadge(c){
  const k = (c||'').trim();
  if(!k) return '<span class="catbadge c-none">-</span>';
  const cls = CAT_LBL[k] ? ('c-'+k) : 'c-none';
  return '<span class="catbadge '+cls+'" data-cat="'+esc(k)+'" style="cursor:pointer" title="点击按「'+esc(CAT_LBL[k]||k)+'」筛选">'+esc(CAT_LBL[k]||k)+'</span>';
}
function toast(msg, type, ms){
  const box = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast '+(type||'info');
  el.innerHTML = '<div class="tx">'+esc(msg)+'</div><button class="cls" title="关闭">✕</button>';
  el.querySelector('.cls').onclick = ()=>el.remove();
  box.appendChild(el);
  const life = ms || (type==='err' ? 7000 : 3800);
  setTimeout(()=>{ el.style.transition='opacity .3s'; el.style.opacity='0'; setTimeout(()=>el.remove(), 320); }, life);
  return el;
}
function ask(title, body, okLabel, danger){
  return new Promise(res=>{
    const ov = document.getElementById('askOverlay');
    document.getElementById('askTitle').textContent = title || '确认操作';
    document.getElementById('askBody').textContent = body || '';
    const ok = document.getElementById('askOk'), cancel = document.getElementById('askCancel');
    ok.textContent = okLabel || '确定';
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    const done = v => { ov.classList.remove('show'); ok.onclick=null; cancel.onclick=null; document.removeEventListener('keydown', key); res(v); };
    const key = e => { if(e.key==='Escape') done(false); };
    ok.onclick = ()=>done(true); cancel.onclick = ()=>done(false);
    document.addEventListener('keydown', key);
    ov.classList.add('show');
    ov.onclick = e => { if(e.target===ov) done(false); };
  });
}
function clearFilters(){
  curStatus=''; curOrigin=''; curCategory=''; curPage=1;
  document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.toggle('active', !x.dataset.s));
  document.querySelectorAll('#tabsOrigin .tab').forEach(x=>x.classList.toggle('active', !x.dataset.o));
  document.getElementById('catSel').value = '';
  document.getElementById('q').value = '';
  load(true);
}

function cardCls(f){
  if(!f) return curStatus==='' && !curOrigin && !curCategory;
  if(f.startsWith('s:')) return curStatus === f.slice(2);
  if(f.startsWith('o:')) return curOrigin === f.slice(2);
  return false;
}
function renderCards(stats){
  const c = [
    ['total','总数',stats.total,''],
    ['queued','排队',stats.queued,'s:queued'],
    ['running','运行中',stats.running,'s:running'],
    ['pending_manual','待整理',stats.pending_manual||0,'s:pending_manual'],
    ['done','已完成',stats.done,'s:done'],
    ['pendingmd','⚠ 待补元数据',stats.pending_md||0,'s:pendingmd'],
    ['failed','失败',stats.failed,'s:failed'],
    ['avdbsrc','🔗 avdb 来源',stats.avdb||0,'o:avdb']
  ];
  document.getElementById('cards').innerHTML = c.map(x=>{
    const on = cardCls(x[3]);
    return '<div class="card '+x[0]+(on?' on':'')+'" data-f="'+x[3]+'" tabindex="0" title="'+
      (x[3] ? '点击筛选：'+x[1] : '点击清除筛选')+'"><div class="lbl">'+x[1]+'</div><div class="num">'+x[2]+'</div></div>';
  }).join('');
  const ra = document.getElementById('btnRetryAll');
  const n = stats.failed_push || 0;
  if(n > 0){ ra.style.display=''; ra.textContent = '🔁 重试全部推送失败 ('+n+')'; }
  else { ra.style.display='none'; }
}
function applyCardFilter(f){
  if(!f){
    curStatus=''; curOrigin=''; curCategory='';
    document.getElementById('catSel').value=''; document.getElementById('q').value='';
  }
  else if(f.startsWith('s:')){ curStatus = f.slice(2); }
  else if(f.startsWith('o:')){ curOrigin = f.slice(2); }
  document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.toggle('active', (x.dataset.s||'')===curStatus));
  document.querySelectorAll('#tabsOrigin .tab').forEach(x=>x.classList.toggle('active', (x.dataset.o||'')===curOrigin));
  curPage = 1; load(true);
}

function msgCell(t){
  const full = (t.msg||'').replace(/\n/g,' ').trim();
  if(full.length <= 80) return '<div class="msgcell" title="'+esc(full)+'">'+esc(full||'-')+'</div>';
  const open = expanded.has(t.task_id);
  return '<div class="msg-wrap"><div class="msgcell'+(open?' open':'')+'" title="'+esc(full)+'">'+
    esc(open ? full : full.slice(0,80)+'…')+'</div>'+
    '<button class="msg-more" data-more="'+esc(t.task_id)+'">'+(open?'收起':'展开')+'</button></div>';
}
function actCell(t){
  let h = '<button class="btn ghost sm" data-act="detail" data-id="'+esc(t.task_id)+'">详情</button>';
  if(t.kind==='non_fanhao' && t.thread_id) h += '<button class="btn ok sm" style="margin-left:6px" data-act="tv" data-id="'+esc(t.task_id)+'" data-tid="'+esc(t.thread_id)+'">🔄整理</button>';
  if(t.status==='failed' && t.step==='push') h += '<button class="btn danger sm" style="margin-left:6px" data-act="repush" data-id="'+esc(t.task_id)+'">重推115</button>';
  if(t.status==='failed' && t.step==='wait_video') h += '<button class="btn primary sm" style="margin-left:6px" data-act="cvideo" data-id="'+esc(t.task_id)+'">✅继续</button>';
  // 待补元数据: 一键重刮 (走 MDCng watcher, 重触发文件名现为纯字母后缀, 不会污染番号) 2026-09-20
  // 仅在 thread_id 为真实色花堂帖子号时提供; avdb 来源的 thread_id 是番号, 无目录可重刮
  if(isPendingMeta(t) && /^\d+$/.test(String(t.thread_id||''))) h += '<button class="btn warn sm" style="margin-left:6px" title="以 MDCng 重新刮削 thread_'+esc(t.thread_id)+' (创建新任务)" data-act="rescrape" data-id="'+esc(t.task_id)+'" data-tid="'+esc(t.thread_id)+'">🔁 重刮</button>';
  return h;
}
function rowCells(t){
  const p = pct(t);
  return {
    time: '<span title="'+esc(fmt(t.created_at))+'">'+esc(rel(t.created_at))+'</span>',
    tid: '<div class="tid" title="'+esc(t.thread_id||'')+'">'+(t.thread_url ? '<a href="'+esc(t.thread_url)+'" target="_blank" style="color:#58a6ff;text-decoration:none" title="打开原帖">'+esc(t.thread_id||'-')+' ↗</a>' : esc(t.thread_id||'-'))+'</div>',
    title: '<div class="t" title="'+esc(t.title||'')+'">'+esc(t.title||'-')+'</div>',
    kind: kindLbl(t.kind),
    origin: (t.origin==='avdb' ? '<span class="badge" style="background:#3d2e00;color:#d29922">🔗 avdb</span>' : '<span class="badge" style="background:#21262d;color:#8b949e">🀄 色花堂</span>'),
    cat: catBadge(t.category),
    status: statusCell(t),
    prog: '<div style="display:flex;align-items:center;gap:6px"><div class="progress"><div class="fill '+p.cls+'" style="width:'+p.p+'%"></div></div><span class="pct">'+p.p+'%</span></div>',
    msg: msgCell(t),
    act: actCell(t)
  };
}
const NOWRAP = {time:1, kind:1, origin:1, cat:1, status:1, prog:1, act:1};
function rowClsOf(t){ return t.status==='failed' ? 'row-failed' : (t.status==='done' ? 'row-done' : ''); }
function rowHTML(t){
  const c = rowCells(t);
  const cls = rowClsOf(t);
  return '<tr data-id="'+esc(t.task_id)+'"'+(cls?' class="'+cls+'"':'')+'>' +
    CELLS.map(k=>'<td data-c="'+k+'"'+(NOWRAP[k]?' style="white-space:nowrap"':'')+'>'+c[k]+'</td>').join('') + '</tr>';
}
function renderRows(tasks){
  const tb = document.getElementById('tbody');
  if(!tasks.length){
    tb.innerHTML=''; rowMap.clear(); lastKey='';
    const filtered = curStatus || curOrigin || curCategory || document.getElementById('q').value.trim();
    document.getElementById('emptyText').textContent = filtered ? '当前筛选条件下没有任务' : '暂无任务记录';
    document.getElementById('empty').style.display='block';
    document.getElementById('pager').innerHTML='';
    return;
  }
  document.getElementById('empty').style.display='none';
  const key = tasks.map(t=>t.task_id).join(',');
  if(key !== lastKey){
    tb.innerHTML = tasks.map(rowHTML).join('');
    rowMap.clear();
    Array.prototype.forEach.call(tb.children, tr=>{
      const cache = {};
      CELLS.forEach(c=>{ const td = tr.querySelector('[data-c="'+c+'"]'); cache[c] = td ? td.innerHTML : null; });
      const src = tasks.filter(t=>t.task_id===tr.dataset.id)[0] || {};
      rowMap.set(tr.dataset.id, {tr: tr, cache: cache, data: src});
    });
    lastKey = key;
    return;
  }
  tasks.forEach(t=>{
    const rec = rowMap.get(t.task_id); if(!rec) return;
    rec.data = t;
    const cells = rowCells(t);
    CELLS.forEach(c=>{
      if(cells[c] !== rec.cache[c]){
        const td = rec.tr.querySelector('[data-c="'+c+'"]');
        if(td){ td.innerHTML = cells[c]; rec.cache[c] = cells[c]; }
      }
    });
    const cls = rowClsOf(t);
    if((rec.tr.className||'') !== cls) rec.tr.className = cls;
  });
}
function toggleMsg(id){
  const rec = rowMap.get(id); if(!rec) return;
  if(expanded.has(id)) expanded.delete(id); else expanded.add(id);
  const html = msgCell(rec.data || {task_id: id});
  const td = rec.tr.querySelector('[data-c="msg"]');
  if(td){ td.innerHTML = html; rec.cache.msg = html; }
}

function renderPager(info){
  const el = document.getElementById('pager');
  if(!info || info.total_pages<=1){el.innerHTML='';return;}
  el.innerHTML = '<button class="btn ghost sm" '+(info.page<=1?'disabled':'')+' onclick="goPage('+(info.page-1)+')">‹ 上一页</button>'+
    '<span>第 '+info.page+' / '+info.total_pages+' 页 · 共 '+info.total+' 条</span>'+
    '<button class="btn ghost sm" '+(info.page>=info.total_pages?'disabled':'')+' onclick="goPage('+(info.page+1)+')">下一页 ›</button>';
}

function listUrl(extra){
  let url = '/api/import/list?page='+curPage+'&per_page='+perPage+'&sort='+sortKey+'&order='+sortDir;
  if(curStatus==='pendingmd') url += '&pending_md=1';
  else if(curStatus) url += '&status='+curStatus;
  if(curOrigin) url += '&origin='+curOrigin;
  if(curCategory) url += '&category='+encodeURIComponent(curCategory);
  const q = document.getElementById('q').value.trim();
  if(q) url += '&q='+encodeURIComponent(q);
  return url + (extra||'');
}
function setUpdate(txt){ document.getElementById('lastUpdate').textContent = txt; }
function load(force){
  const scope = listUrl('');
  let url = scope;
  if(!force && sigScope === scope && sigToken) url += '&sig='+encodeURIComponent(sigToken);
  fetch(url).then(r=>r.json()).then(d=>{
    if(d.error){ return; }
    const now = new Date().toLocaleTimeString();
    if(d.sig) sigToken = d.sig;
    sigScope = scope;
    if(d.unchanged){ setUpdate('无变化 · ' + now); return; }
    renderCards(d.stats||{});
    renderRows(d.tasks||[]);
    renderPager(d);
    setUpdate('更新于 ' + now);
  }).catch(()=>{ setUpdate('连接失败 · ' + new Date().toLocaleTimeString()); });
}
function searchNow(){ curPage=1; load(true); }
function goPage(p){ curPage=p; load(true); }
function csvCell(v){ return '"'+String(v==null?'':v).replace(/"/g,'""')+'"'; }
function exportCsv(){
  const tip = toast('正在生成 CSV…', 'info', 60000);
  const url = listUrl('').replace(/per_page=\d+/, 'per_page=5000');   // 导出当前筛选下的全部记录
  fetch(url).then(r=>r.json()).then(d=>{
    if(tip) tip.remove();
    if(d.error){ toast('导出失败: '+d.error, 'err'); return; }
    const head = ['task_id','创建时间','thread_id','标题','类型','来源','分类','状态','步骤','进度%','消息','原帖'];
    const rows = [head.map(csvCell).join(',')];
    (d.tasks||[]).forEach(t=>{
      rows.push([t.task_id, fmt(t.created_at), t.thread_id||'', t.title||'', kindLbl(t.kind),
        (t.origin || 'sehuatang'), (CAT_LBL[t.category] || t.category || ''),
        (isPendingMeta(t) ? '待补元数据' : (STATUS[t.status] || t.status || '')), (STEP_LBL[t.step] || t.step || ''), pct(t).p,
        (t.msg||'').replace(/\n/g,' '), t.thread_url||''].map(csvCell).join(','));
    });
    const blob = new Blob(['\ufeff'+rows.join('\r\n')], {type:'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'import_tasks_' + new Date().toISOString().slice(0,19).replace(/[:T]/g,'-') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href), 4000);
    toast('已导出 '+(d.tasks||[]).length+' 条记录', 'ok');
  }).catch(e=>{ if(tip) tip.remove(); toast('导出失败: '+e, 'err'); });
}
function retryAll(){
  fetch('/api/import/list?status=failed&step=push&per_page=5000').then(r=>r.json()).then(d=>{
    const list = d.tasks || [];
    if(!list.length){ toast('没有可重试的「推送 115 失败」任务', 'info'); return; }
    ask('重试全部推送失败', '共 '+list.length+' 个任务将重新推送到 115（复用原任务断点重续）。\n\n后台逐个创建重推任务，确认继续？', '开始重试 ('+list.length+')', true).then(ok=>{
      if(!ok) return;
      toast('开始重试 '+list.length+' 个失败任务…', 'run', 6000);
      let done = 0, bad = 0;
      const step = i => {
        if(i >= list.length){
          toast('重试完成：成功 '+done+' 个' + (bad ? '，失败 '+bad+' 个' : ''), bad ? 'err' : 'ok', 8000);
          curPage = 1; load(true);
          return;
        }
        fetch('/api/import/resume', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({task_id: list[i].task_id})})
          .then(r=>r.json()).then(j=>{ if(j.error) bad++; else done++; })
          .catch(()=>{ bad++; })
          .then(()=>setTimeout(()=>step(i+1), 250));
      };
      step(0);
    });
  }).catch(e=>toast('查询失败: '+e, 'err'));
}

document.getElementById('tabs').addEventListener('click', e=>{
  const t = e.target.closest('.tab'); if(!t) return;
  const v = t.dataset.s || '';
  curStatus = (curStatus === v) ? '' : v;      // 再点一次 = 取消该筛选
  document.querySelectorAll('#tabs .tab').forEach(x=>x.classList.toggle('active', (x.dataset.s||'')===curStatus));
  curPage = 1; load(true);
});
document.getElementById('tabsOrigin').addEventListener('click', e=>{
  const t = e.target.closest('.tab'); if(!t) return;
  const v = t.dataset.o || '';
  curOrigin = (curOrigin === v) ? '' : v;
  document.querySelectorAll('#tabsOrigin .tab').forEach(x=>x.classList.toggle('active', (x.dataset.o||'')===curOrigin));
  curPage = 1; load(true);
});
document.getElementById('q').addEventListener('keydown', e=>{if(e.key==='Enter')searchNow();});
// 统计卡 = 快捷筛选器 (A 组)
document.getElementById('cards').addEventListener('click', e=>{
  const c = e.target.closest('.card'); if(!c) return;
  applyCardFilter(c.dataset.f || '');
});
document.getElementById('cards').addEventListener('keydown', e=>{
  if(e.key!=='Enter' && e.key!==' ') return;
  const c = e.target.closest('.card'); if(!c) return;
  e.preventDefault(); applyCardFilter(c.dataset.f || '');
});
// 行内按钮 / 分类徽章 / 消息展开 (A+C 组, 事件委托替代内联 onclick)
document.getElementById('tbody').addEventListener('click', e=>{
  const more = e.target.closest('[data-more]');
  if(more){ toggleMsg(more.dataset.more); return; }
  const cb = e.target.closest('.catbadge[data-cat]');
  if(cb && cb.dataset.cat){
    curCategory = cb.dataset.cat; curPage = 1;
    document.getElementById('catSel').value = curCategory;
    load(true); return;
  }
  const b = e.target.closest('button[data-act]');
  if(!b) return;
  const id = b.dataset.id, act = b.dataset.act;
  if(act==='detail') detail(id);
  else if(act==='tv') tvRefreshQuick(id, b.dataset.tid);
  else if(act==='repush') repush(id);
  else if(act==='cvideo') confirmVideo(id);
  else if(act==='rescrape') rescrapeRow(b.dataset.tid, id);
});
// 分类筛选 / 每页条数 / 表头排序 (C 组)
(function(){
  const sel = document.getElementById('catSel');
  sel.innerHTML = '<option value="">全部分类</option>' + CATS.map(c=>'<option value="'+esc(c.key)+'">'+esc(c.name)+'</option>').join('');
  sel.addEventListener('change', e=>{ curCategory = e.target.value; curPage = 1; load(true); });
})();
(function(){
  const pp = document.getElementById('perPage');
  pp.value = String(perPage);
  pp.addEventListener('change', e=>{
    perPage = parseInt(e.target.value, 10) || 50;
    localStorage.setItem('tasksPerPage', String(perPage));
    curPage = 1; load(true);
  });
})();
document.querySelectorAll('th.sortable').forEach(th=>{
  th.addEventListener('click', ()=>{
    const k = th.dataset.sort;
    if(sortKey === k) sortDir = (sortDir === 'desc' ? 'asc' : 'desc');
    else { sortKey = k; sortDir = 'desc'; }
    updateSortArrows();
    curPage = 1; load(true);
  });
});
function updateSortArrows(){
  document.querySelectorAll('th.sortable').forEach(x=>{
    const ar = x.querySelector('.ar');
    ar.textContent = (x.dataset.sort === sortKey) ? (sortDir === 'desc' ? '▼' : '▲') : '';
  });
}
updateSortArrows();

function detail(taskId){
  Promise.all([
    fetch('/api/import/history?task_id='+encodeURIComponent(taskId)).then(r=>r.json()),
    fetch('/api/import/status?task_id='+encodeURIComponent(taskId)).then(r=>r.json())
  ]).then(([h, st])=>{
    const hist = (h && h.history) || [];
    const st2 = (st && !st.error) ? st : {};
    curTask = {task_id: taskId, thread_id: st2.thread_id || '', title: st2.title || '', status: st2.status || '', step: st2.step || '', kind: st2.kind || '', category: st2.category || ''};
    document.getElementById('dTitle').textContent = '任务详情 · ' + taskId;
    document.getElementById('dMeta').innerHTML =
      '<b>task_id:</b> '+esc(taskId)+'<br>'+
      '<b>类型:</b> '+kindLbl(st2.kind)+'<br>'+
      '<b>分类:</b> '+catBadge(st2.category)+'<br>'+
      '<b>thread_id:</b> '+esc(curTask.thread_id||'')+'<br>'+
      '<b>标题:</b> '+esc(curTask.title||'-')+'<br>'+
      (st2.thread_url ? '<b>原帖:</b> <a href="'+esc(st2.thread_url)+'" target="_blank" style="color:#58a6ff">打开帖子 ↗</a><br>' : '')+
      '<b>当前状态:</b> '+badge(curTask.status||'')+'<br>'+
      '<b>最新消息:</b> '+esc(st2.msg||'-');
    document.getElementById('dTimeline').innerHTML = hist.map(hh=>{
      const b = '<span class="bdg '+esc(hh.status)+'">'+(STATUS[hh.status]||esc(hh.status))+'</span>';
      return '<div class="tl-item"><span class="ts">'+fmt(hh.ts)+'</span>'+b+
        '<div class="msg">'+esc(hh.msg||'-')+'</div></div>';
    }).join('') || '<div class="empty">暂无历史记录(该任务早于历史记录功能)</div>';
    // 有 thread_id 的任务才显示重新刮削 (done/failed/running 均可重刮)
    const rsBox = document.getElementById('dRescrape');
    if (curTask.thread_id) {
      document.getElementById('rsThreadTitle').textContent = curTask.title && curTask.title !== ('thread_'+curTask.thread_id) ? curTask.title : '（无标题，仅 thread id）';
      document.getElementById('rsThreadTitle').title = curTask.title;
      document.getElementById('rsThreadId').textContent = curTask.thread_id;
      rsBox.style.display = 'block';
    } else {
      rsBox.style.display = 'none';
    }
    // 推送 115 失败的任务显示「重推115」按钮
    const rpBox = document.getElementById('dRepush');
    rpBox.style.display = (curTask.status==='failed' && curTask.step==='push') ? 'block' : 'none';
    // 剧集任务显示「重跑整理」按钮 (2026-08-20): 补充视频到 115 剧集目录后重跑落地后流程
    const tvBox = document.getElementById('dTvRefresh');
    tvBox.style.display = (curTask.kind==='non_fanhao' && curTask.thread_id) ? 'block' : 'none';
    // 离线下载超时未检测到视频的任务显示「手工确认继续」按钮 (2026-08-20)
    const cvBox = document.getElementById('dConfirmVideo');
    cvBox.style.display = (curTask.status==='failed' && curTask.step==='wait_video') ? 'block' : 'none';
    document.getElementById('overlay').classList.add('show');
  }).catch(()=>{});
}
function closeDetail(){curTask=null;document.getElementById('overlay').classList.remove('show');}
function confirmVideo(taskId){
  if(!taskId) return;
  ask('确认视频已下载完成', '你已确认 115 网盘中该帖视频已下载完成？\n\n将跳过推送/等待，直接从文件处理/刮削/strm 继续。', '确认并继续', false).then(ok=>{
    if(!ok) return;
    fetch('/api/import/confirm_video', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task_id: taskId})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('已确认，任务继续处理: '+d.task_id, 'ok');
      closeDetail(); curPage=1; load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}
function tvRefresh(){
  if(!curTask || !curTask.thread_id) return;
  const tid = curTask.thread_id, ttl = (curTask.title||'').slice(0,120);
  ask('执行剧集「落地后流程」？', 'thread_' + tid + '\n' + ttl +
    '\n\n请确认已在 115 网盘整理好视频(删除广告/补充新集)。将执行：未命名视频重命名「原名.thread_x.S01E续集号」→ 生成 strm → 移入 Emby 剧集目录并刷新。',
    '执行整理', false).then(ok=>{
    if(!ok) return;
    fetch('/api/tv/refresh', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({thread_id: tid, title: curTask.title || ''})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('剧集整理任务已创建: '+d.task_id, 'ok');
      closeDetail(); curPage=1; load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}
function tvRefreshQuick(taskId, threadId){
  ask('执行剧集「落地后流程」？', 'thread_' + threadId +
    '\n\n请确认已在 115 网盘整理好视频(删除广告/补充新集)。将执行：未命名视频重命名「原名.thread_x.S01E续集号」→ 生成 strm → 移入 Emby 剧集目录并刷新。',
    '执行整理', false).then(ok=>{
    if(!ok) return;
    fetch('/api/tv/refresh', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({thread_id: threadId})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('剧集整理任务已创建: '+d.task_id, 'ok');
      load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}
function gotoMeta(){
  if(!curTask || !curTask.thread_id) return;
  window.open('https://sehuatang.net/thread-' + curTask.thread_id + '-1-1.html', '_blank');
}
function repush(taskId, fromDetail){
  if(!taskId) return;
  ask('重新推送该任务到 115？', '复用原任务断点重续；尚未成功推送的链接会重新推送到 115。', '重新推送', false).then(ok=>{
    if(!ok) return;
    fetch('/api/import/resume', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task_id: taskId})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('已提交重推 115: '+d.task_id, 'ok');
      if(fromDetail) closeDetail();
      curPage=1; load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}
function rescrapeRow(tid, taskId){
  if(!tid) return;
  const rec = rowMap.get(taskId);
  const row = rec ? rec.data : null;
  const ttl = (row && row.title && row.title !== ('thread_'+tid)) ? row.title : ('thread_'+tid);
  ask('对该帖执行「MDC 刮削」重新刮削？', 'thread_' + tid + '\n' + ttl.slice(0, 120) +
    '\n\n会先清理旧元数据，创建新任务并显示进度。', '开始刮削', false).then(ok=>{
    if(!ok) return;
    fetch('/api/import/rescrape', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({thread_id: tid, kind:'mdc'})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('重新刮削任务已创建: '+d.task_id, 'ok');
      curPage=1; load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}
function rescrape(kind){
  if(!curTask || !curTask.thread_id) return;
  const way = kind==='mdc' ? 'MDC 刮削' : '网页爬取刮削';
  const ttl = (curTask.title && curTask.title !== ('thread_'+curTask.thread_id)) ? curTask.title : '(无标题)';
  ask('对以下资源执行「'+way+'」重新刮削？', 'thread_' + curTask.thread_id + '\n' + ttl.slice(0, 120) +
    '\n\n会先清理旧元数据，创建新任务并显示进度。', '开始刮削', false).then(ok=>{
    if(!ok) return;
    fetch('/api/import/rescrape', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({thread_id: curTask.thread_id, kind: kind})
    }).then(r=>r.json()).then(d=>{
      if(d.error){ toast('提交失败: '+d.error, 'err'); return; }
      toast('重新刮削任务已创建: '+d.task_id, 'ok');
      closeDetail(); curPage=1; load(true);
    }).catch(e=>toast('提交失败: '+e, 'err'));
  });
}

setInterval(()=>{if(document.getElementById('auto').checked)load();}, 5000);
// 深链: /tasks?task_id=xxx (avdb 连接器面板跳过来) → 自动筛选并打开详情
(function(){
  const tid = new URLSearchParams(location.search).get('task_id');
  if(tid){ document.getElementById('q').value = tid; if(typeof detail==='function') detail(tid); }
})();
load(true);
</script>
</body>
</html>"""


def render_tasks_page(category_map=None, default_category=DEFAULT_CATEGORY):
    """把分类表注入任务页 (单一来源: import_api.CATEGORY_MAP), 与首页 render_index_page 一致"""
    cats = [{'key': k, 'name': v[2]} for k, v in (category_map or CATEGORY_MAP).items()]
    return (TASKS_PAGE
            .replace('__CATS_JSON__', json.dumps(cats, ensure_ascii=False)))

# ============================== 磁链库存反查 (v1.13.0, 2026-09-21) ==============================
# 目标: 油猴面板上直接答"这条磁链的东西在我 115 里有没有" —— 不碰 9p 扫盘, 不等刮削入库, 不读 Emby。
#
# 数据源优先级 (越靠前越省钱):
#   ① 本地台账 hash→番号            0 请求
#   ② 本地台账 status='done' 直判在库 0 请求   ← 老帖整页可能一次 115 请求都不发
#      (排除 status='deleted' —— 用户明确删过那份内容, 台账不再可信, 必须回落实搜)
#   ③ 115 search_files 全盘搜番号     N 请求 (带缓存/并发/分片/限频护栏)
#
# 三态是核心 (静默假阴性防护, 与 2026-09-13 "MCP 掉线致 4 条任务推送失败" 同型):
#   in      在库   —— 搜到 ≥1 条且番号二次校验通过; 或台账里该 hash 已 done
#   out     不在库 —— search 干净返回 0 条 (不是 None)
#   unknown 未校验 —— MCP 掉线/超时(None) / 限频窗口内 / 番号没解析出来
#   ！！unknown 绝不可渲染成"不在库": MCP 一掉线全页显示"不在库"会让用户重复入库。
INV_TTL_IN_S    = 600   # 在库/命中结果缓存 10 分钟
INV_TTL_OUT_S   = 180   # 不在库缓存 3 分钟 (刚入库完要能马上翻牌)
INV_CONCURRENCY = 6     # 并发上限 (实测 10 路并发 3.13s 全成功, 不顶格)
INV_CHUNK       = 10    # 每片 ≤10 个番号
INV_CHUNK_GAP_S = 1.5   # 片间隔: 峰值从 3.1 req/s 压到 ~1 req/s
INV_LIMIT       = 30    # search limit (tool schema 默认 100, 白拉 3 倍数据)
INV_MAX_LINKS   = 40    # 单次请求最多反查几条磁链
INV_MAP_TTL_S   = 30    # 台账 hash→番号 映射重建间隔

_INV_CACHE = {}          # fanhao_key -> (ts, item)  查询结果缓存
_INV_CACHE_LOCK = threading.Lock()
# h2f: infohash->番号; done: 已成功入库; gone: 已被删除(台账不可信, 必须问 115 实测)
# f2done: 番号(归一键)->True, 见 _inv_ledger_map —— 台账按【番号】建的第二种索引,
#         给"链接没带番号、但页面上下文能抠出番号"的情况兜 0 请求直答。
_INV_MAP = {'ts': 0.0, 'h2f': {}, 'done': set(), 'gone': set(), 'f2done': {}}
_INV_MAP_LOCK = threading.Lock()


def _inv_hash(link):
    """链接唯一 hash (magnet→btih / ed2k→32hex)。惰性 import, 与项目既有风格一致。"""
    from mcp115 import MCP115
    return MCP115.link_hash(link)


def _inv_search_form(fanhao):
    """把抠出来的番号整成【带横线】的搜索形式: SNOS400 -> SNOS-400。

    实测 (2026-09-21): 搜 'SNOS400'(不带横线) 干净返回 0 条 → 搜索词必须带横线。
    不能拿 _fanhao_key() 去搜: 它故意吃掉横线和前导零 (VRKM-01741 → VRKM1741),
    那是【比对】用的归一键, 当搜索词用必然搜不到。
    """
    m = re.match(r'^([A-Za-z]{2,8})-?(\d{2,6})$', str(fanhao or '').strip())
    return (m.group(1) + '-' + m.group(2)).upper() if m else ''


def _inv_extract_fanhao(text):
    """从标题/文件名抠出第一个番号候选。

    _FANHAO_RE 允许无横线 (SNOS400 能抠出来), 这里统一整成带横线形式。
    纯数字关键词是垃圾场 (实测搜 400 → 30 条 MDBK-400/MJAD-400), 所以必须带字母前缀。
    """
    for m in _FANHAO_RE.finditer(str(text or '').upper()):
        f = _inv_search_form(m.group(1))
        if f:
            return f
    return ''


def _inv_fanhao_from_link(link):
    """从磁链/ed2k 链接本身抠番号 —— 只扫文件名部分。

    不要直接对整条 magnet 串跑正则: btih 的 16 进制尾巴 (…AB1234&dn=) 会长得像番号。
    """
    s = str(link or '')
    m = re.search(r'[?&]dn=([^&\s]+)', s)
    if m:
        try:
            s = urllib.parse.unquote(m.group(1))
        except Exception:
            s = m.group(1)
    elif s.lower().startswith('ed2k://'):
        m2 = re.match(r'ed2k://\|file\|([^|]*)', s)
        s = m2.group(1) if m2 else ''
    else:
        s = ''
    return _inv_extract_fanhao(s)


def _inv_fanhao_candidates(text):
    """抠出文本里【所有】番号候选 (带横线的搜索形式, 按归一化键去重, 保序)。

    用于"唯一候选才敢采信"的防呆 (A+B, 2026-09-21): 候选 > 1 说明这段文本里混了噪声
    —— 实测台账标题三种典型噪声: 'mtabs-009 …ハメ潮SEX20本番'(SEX20)、
    'cjob-147 …BEST52本番'(BEST52)、'第一會所新片@SIS001@HNDS-079'(SIS1)。
    这时宁缺勿错: 返回多用不上的候选, 由调用方按 len()==1 决定是否采信。
    """
    out, seen = [], set()
    for m in _FANHAO_RE.finditer(str(text or '').upper()):
        f = _inv_search_form(m.group(1))
        k = _fanhao_key(f) if f else ''
        if k and k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _inv_fanhao_from_ctx(ctxs):
    """从页面上下文里抠番号 (B 方案): 就近优先, 取第一个【恰好只含 1 个候选】的文本层。

    返回 (番号, 证据文本): 证据文本要带回去做跨条查重 —— 同一段文本若被 ≥2 条磁链
    共用 (合集帖的公共标题/正文), 那它就不可能是每一条各自的番号, 必须作废。
    """
    for s in (ctxs or []):
        if not isinstance(s, str):
            continue
        cands = _inv_fanhao_candidates(s)
        if len(cands) == 1:
            return cands[0], str(s)[:160]
    return '', ''


def _inv_ledger_map():
    """本地台账映射 (0 请求): 四件套 —— hash→番号 / 已入库 hash / 已删除 hash / 番号→已入库。

    355 行台账全扫 <10ms, 30s 重建一次。实测 257/355 (72%) 的磁链在这里就能拿到番号。
    被排除在"直答在库"之外的两种:
      - status='failed'  该 hash 从未成功入库
      - status='deleted' 用户明确删过这份内容 (台账现存 16 条, 全是"VR 已删除(无 VR 设备)")
        → 台账不再可信, 必须回落到 115 实搜

    f2done 是【番号】为键的第二张索引 (A 方案, 2026-09-21):
      原来 `if not h: continue` 在抠番号之前, 台账里 58 行 magnet 为空、拿不到 hash 的记录
      整行在第一步就被丢掉 —— 可它们的 title 里明明写着番号 (实测 47/58 恰好 1 个候选)。
      所以顺序反过来: 先抠番号装满 f2done, 再判 hash。
    语义差别: done/gone 是 hash 级 (这条磁链的内容入过库), f2done 是番号级
      (这个番号入过库, 可能是另一条磁链推的) —— 后者与 115 实搜是同一语义层级
      (_inv_verdict 的判据本来就是"115 里文件名含这个番号"), 故两者可互为缓存。
    """
    now = time.time()
    with _INV_MAP_LOCK:
        if now - _INV_MAP['ts'] < INV_MAP_TTL_S:
            return (_INV_MAP['h2f'], _INV_MAP['done'], _INV_MAP['gone'], _INV_MAP['f2done'])
    h2f, done, gone, f_done, f_gone = {}, set(), set(), set(), set()
    try:
        c = sqlite3.connect(LOG_DB)
        for magnet, title, status in c.execute('SELECT magnet, title, status FROM import_log'):
            # ① 先抠番号: title 里"恰好 1 个候选"才算唯一证据; 有噪声时退到链接文件名
            cands = _inv_fanhao_candidates(title)
            strict = cands[0] if len(cands) == 1 else ''
            link_f = _inv_fanhao_from_link(magnet)
            f = strict or link_f                      # f2done 只吃可信来源
            if f:
                k = _fanhao_key(f)
                if status == 'done':
                    f_done.add(k)
                elif status == 'deleted':
                    f_gone.add(k)
            # ② 再判 hash (这一句必须留在后面, 否则 58 行走不到上面)
            h = _inv_hash(magnet)
            if not h:
                continue
            if status == 'done':
                done.add(h)
            elif status == 'deleted':
                gone.add(h)
            # h2f 口径放宽一档: 严格候选 → 链接文件名 → 旧口径第一个候选。
            # 它只做展示+搜索词, 不参与"在库"判定, 所以可以容忍噪声。
            hf = strict or link_f or (cands[0] if cands else '')
            if hf:
                h2f[h] = hf
        c.close()
    except Exception as e:
        log.warning('[inv] 台账映射构建失败: %s', str(e)[:150])
    # deleted 优先 (与 done/gone 那条 `<h in done and h not in gone>` 同口径)
    f2done = {k: True for k in f_done if k not in f_gone}
    with _INV_MAP_LOCK:
        _INV_MAP.update({'ts': now, 'h2f': h2f, 'done': done, 'gone': gone, 'f2done': f2done})
    return h2f, done, gone, f2done


def _inv_hit_matches(name, key):
    """命中项是否真是这个番号: 抠出名字里的番号候选, 用 _fanhao_key 归一化比对。

    搜索是模糊的 —— 实测搜 SNOS-400 会返回 SNOS-403 / SNOS-406-U,
    裸子串或不校验必然误判"在库"。
    """
    for m in _FANHAO_RE.finditer(os.path.splitext(str(name or ''))[0].upper()):
        if _fanhao_key(m.group(1)) == key:
            return True
    return False


def _inv_verdict(form, res):
    """把 search 结果判成三态里的 in/out (res 必须非 None)。

    实测返回结构: list[dict]; 文件带 sha1/file_size/ico, 目录只带 file_id/file_name/parent_id/pick_code。
    所以 is_dir 靠"没有 sha1"判定 (目录名恰是番号也算在库证据, 覆盖"文件名不含番号"的漏判)。
    """
    key = _fanhao_key(form)
    hits = [e for e in (res or []) if _inv_hit_matches((e or {}).get('file_name', ''), key)]
    return {'fanhao': form, 'state': 'in' if hits else 'out', 'checked': '115',
            'count': len(hits),
            'video': len([e for e in hits if e.get('sha1')]),
            'dir': any(not e.get('sha1') for e in hits)}


def _inv_cache_get(form):
    key = _fanhao_key(form)
    with _INV_CACHE_LOCK:
        hit = _INV_CACHE.get(key)
    if not hit:
        return None
    ttl = INV_TTL_IN_S if (hit[1] or {}).get('state') == 'in' else INV_TTL_OUT_S
    if time.time() - hit[0] > ttl:
        return None
    c = dict(hit[1])
    c['cached'] = True
    return c


def _inv_cache_put(form, item):
    with _INV_CACHE_LOCK:
        _INV_CACHE[_fanhao_key(form)] = (time.time(), dict(item))


def _inv_invalidate(task_id):
    """入库成功 → 立刻让库存徽章从 ❓ 翻 ✅ (不等 TTL), 并让台账映射重建。

    只失效该任务对应的番号, 不做全清 —— 全清会让下一次打开帖子页重新把 115 打满。
    """
    try:
        t = get_task(task_id) or {}
        for f in (_inv_extract_fanhao(t.get('title')), _inv_fanhao_from_link(t.get('magnet'))):
            if f:
                with _INV_CACHE_LOCK:
                    _INV_CACHE.pop(_fanhao_key(f), None)
        with _INV_MAP_LOCK:
            _INV_MAP['ts'] = 0.0
    except Exception:
        pass


def _inv_query_many(pairs, out):
    """并发分片查 115, 结果写回 out[idx]; 返回实际发出的 115 请求数 (命中缓存的不计)。

    实测: 单条 0.13~0.70s (avg 0.31s), 10 路并发 3.13s 全成功,
    连续 40 请求无失败/无 770004 指纹/耗时零漂移。
    """
    if not pairs:
        return 0
    todo = []
    for idx, form in pairs:
        c = _inv_cache_get(form)
        if c:
            out[idx].update(c)
        else:
            todo.append((idx, form))
    if not todo:
        return 0
    if _ratelimit_active():
        # 限频窗口内一个请求都不发 (复用既有护栏: /tmp/115push_ratelimit_state.json 新鲜且 limited)
        log.warning('[inv] 限频窗口内, %d 个番号跳过查询(记 unknown)', len(todo))
        for idx, _f in todo:
            out[idx].update({'state': 'unknown', 'reason': 'ratelimit'})
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from mcp115 import MCP115
    m = MCP115()
    req = 0
    for s in range(0, len(todo), INV_CHUNK):
        if s:
            time.sleep(INV_CHUNK_GAP_S)   # 片间间隔, 压峰值
        chunk = todo[s:s + INV_CHUNK]
        with ThreadPoolExecutor(max_workers=min(INV_CONCURRENCY, len(chunk))) as ex:
            futs = {ex.submit(m.search_files, f, INV_LIMIT): (i, f) for i, f in chunk}
            for fu in as_completed(futs):
                idx, form = futs[fu]
                try:
                    res = fu.result()
                except Exception as e:
                    log.warning('[inv] search 异常 %s: %s', form, str(e)[:120])
                    res = None
                req += 1
                if res is None:
                    # MCP 掉线/超时 —— 绝不当成"不在库"; 且不落缓存 (瞬时故障不该被钉 3 分钟)
                    log.warning('[inv] %s 查询无响应 → unknown (MCP 掉线/超时)', form)
                    out[idx].update({'state': 'unknown', 'reason': 'mcp_down'})
                    continue
                item = _inv_verdict(form, res)
                out[idx].update(item)
                _inv_cache_put(form, item)
    return req


def _inv_clean_ctxs(raw, max_len=200, max_levels=8, max_total=60000):
    """清洗前端送来的 contexts: 必须是 list[list[str]], 每条截断, 总量封顶。

    上限按最坏情况留头: 40 条 × 8 层 × 200 = 64000 > 60000, 所以总封顶真能被触发。
    (前端实际只送 5 层 + 可能 1 层页面标题 = 40×6×200 = 48000, 不会误伤。)
    形状不对 → 返回 None, 上游据此整批丢弃。宁可全页 ⚠️, 也不要错位套上别人的番号。
    """
    if not isinstance(raw, list) or not raw:
        return None
    out, total = [], 0
    for row in raw:
        if isinstance(row, str):
            row = [row]
        if not isinstance(row, list):
            return None
        line = []
        for s in row[:max_levels]:
            if not isinstance(s, str):
                continue
            s = s.strip()[:max_len]
            if s:
                line.append(s)
                total += len(s)
        out.append(line)
    return out if total <= max_total else None


def inventory_lookup(links, title='', thread_id='', thread_url='', contexts=None, single=None):
    """磁链批量库存反查 (POST /api/import/lookup)。

    links: ['magnet:?xt=urn:btih:...', 'ed2k://|file|...']  (≤ INV_MAX_LINKS 条)
    title / thread_id / thread_url: 帖子上下文, 与入库 /api/import/batch 同一份来源
    contexts: list[list[str]], 与 links 严格同序; 每条是"就近 → 逐层放宽"的页面文本
    single: 客户端声明"本页只有这一条链接"。为 None 时按 len(links)==1 猜 (兼容老脚本),
            前端 v1.14.0+ 必须显式传 —— 41 条以上的帖子分片后, 第二片只剩 1 条,
            按长度猜会把整页标题套到它头上。
    出参: {'items': [与入参同序], 'req_115': 实际发出的 115 请求数, 'elapsed_ms': n}
    item: {link, link_hash, fanhao, state: in|out|unknown, reason?, checked?, src?,
           count, video, dir, cached?}
    checked: ledger (hash 命中台账, 强证据) / ledger_fanhao (番号命中台账) / 115
    src:     番号来源 ledger | magnet | ctx | title
    state=unknown 时 reason ∈ {no_fanhao, mcp_down, ratelimit} —— 前端必须按"未校验"渲染。
    """
    if not isinstance(links, list):
        return {'error': 'links 必须是数组'}
    links = [str(x).strip() for x in links if str(x or '').strip()][:INV_MAX_LINKS]
    if not links:
        return {'items': [], 'req_115': 0, 'elapsed_ms': 0}
    t0 = time.time()
    h2f, done, gone, f2done = _inv_ledger_map()
    # contexts 长度对不上 → 整批丢弃, 绝不"尽力对齐" (错位必然给出假 ✅)
    ctxs = contexts if (isinstance(contexts, list) and len(contexts) == len(links)) else None
    title = str(title or '')[:200]
    single = (len(links) == 1) if single is None else bool(single)

    out, pend, ev = [], [], {}
    for idx, link in enumerate(links):
        h = _inv_hash(link)
        it = {'link': link, 'link_hash': h, 'fanhao': '', 'state': '', 'src': ''}
        if h and h in done and h not in gone:
            # 台账里这条 hash 已成功入库过, 且没被删过 → 0 请求直答 (hash 级强证据)
            it.update({'fanhao': h2f.get(h, ''), 'src': 'ledger',
                       'state': 'in', 'checked': 'ledger'})
            out.append(it)
            continue
        form = h2f.get(h, '') if h else ''
        src = 'ledger' if form else ''
        if not form:
            form = _inv_fanhao_from_link(link)
            src = 'magnet' if form else ''
        if not form and ctxs:
            form, scope = _inv_fanhao_from_ctx(ctxs[idx])      # B: 页面上下文
            if form:
                src, ev[idx] = 'ctx', scope
        if not form and title and single:
            form, scope = _inv_fanhao_from_ctx([title])        # B: 页面标题兜底(仅单链接页)
            if form:
                src, ev[idx] = 'title', 'title:' + scope
        it.update({'fanhao': form, 'src': src})
        out.append(it)
        if not form:
            it.update({'state': 'unknown', 'reason': 'no_fanhao'})
            continue
        pend.append((idx, form))

    # 跨条查重 (B 防呆): 同一段证据文本供出 ≥2 条不同磁链 → 那是合集帖的公共文本
    # (标题/正文), 不可能是每一条各自的番号 → 该番号作废, 相关条退回 ⚠️。
    # 两头都对: 12 行各带各的番号 → 证据文本互不相同 → 不误杀;
    #           一个标题番号罩 12 条 → 12 个 hash 挤在同一组 → 全杀 (宁可 ⚠️ 不要假 ✅)。
    if ev:
        shared = {}
        for idx, scope in ev.items():
            shared.setdefault((out[idx]['fanhao'], scope), set()).add(out[idx]['link_hash'])
        for idx, scope in ev.items():
            if len(shared.get((out[idx]['fanhao'], scope), ())) > 1:
                out[idx].update({'fanhao': '', 'src': '',
                                 'state': 'unknown', 'reason': 'no_fanhao'})
        pend = [(i, f) for (i, f) in pend if out[i].get('state') != 'unknown']

    # A: 番号命中台账 → 0 请求直答 (番号级证据, 与 115 实搜同层级: 只证明这个番号入过库)
    query = []
    for idx, form in pend:
        if _fanhao_key(form) in f2done:
            out[idx].update({'state': 'in', 'checked': 'ledger_fanhao'})
        else:
            query.append((idx, form))
    req = _inv_query_many(query, out)
    return {'items': out, 'req_115': req,
            'elapsed_ms': int((time.time() - t0) * 1000),
            'counts': {
                'in': len([o for o in out if o.get('state') == 'in']),
                'out': len([o for o in out if o.get('state') == 'out']),
                'unknown': len([o for o in out if o.get('state') == 'unknown']),
            },
            'ledger': {'f2done': len(f2done), 'done': len(done)}}


class ImportHTTPServer(ThreadingHTTPServer):

    """2026-09-13: 单线程 HTTPServer → 多线程。

    事故: HTTPServer(socketserver.TCPServer) 一次只处理一个连接。当 Windows 侧
    (浏览器/工具经 WSL localhost 转发) 建立起一个只握手、不发请求的连接, 主线程被
    钉在 handle_one_request 的 readline 上, 之后所有请求 (面板健康检查 /health、
    油猴入库 /api/import) 只能排进 accept backlog (默认 5), backlog 一满 SYN 直接
    被丢弃 → 外面看到的就是"端口 5081 不通" (ss 显示 Recv-Q 堆积, 面板 967 一直
    SYN-SENT)。进程本身没死, worker 线程还在正常跑任务, 所以日志照常滚动,
    只有 HTTP 入口假死 —— 这次就是这样, 冻了一整段后被对端关掉才自己恢复。
    多线程 + daemon 线程 + 每连接 30s 超时后, 单个坏连接不再影响其他人。
    """
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

class Handler(BaseHTTPRequestHandler):
    # 2026-09-13 "端口不通" 事故修复①: 单连接读超时。
    # 旧代码未设 timeout, 只要有客户端建立了 TCP 但迟迟不发请求头 (Windows 经 WSL
    # localhost 转发进来的浏览器预连接/被动探测最常见), 处理线程就会永远卡在
    # readline 上。设 30s 后 socket 超时, 连接被丢弃, 不再钉住处理线程。
    timeout = 30

    def handle(self):
        """2026-09-13 修复②: 客户端中断/半开连接只记一行, 不再打整段 socketserver 堆栈"""
        try:
            super().handle()
        except (ConnectionError, socket.timeout, BrokenPipeError) as e:
            log.debug('[http] 连接中断: %s', e)

    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, status=200):
        body = html.encode('utf-8', errors='replace')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _parse_query(self):
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/':
            self._html(render_index_page(CATEGORY_MAP, DEFAULT_CATEGORY))
            return
        if path == '/health':
            # 云主机无 MySQL (threads 表在 108), 健康检查用本地 SQLite 即可 (2026-08-20)
            try:
                conn = sqlite3.connect(LOG_DB)
                conn.execute('SELECT 1')
                conn.close()
                self._json({'status': 'ok', 'db': 'connected'})
            except Exception as e:
                self._json({'status': 'error', 'db': str(e)}, 500)
            return
        # /api/search 磁力搜索已下线 (2026-08-20): 搜索页面已删除, 入库入口改为手动磁力/油猴脚本
        if path == '/api/import/status':
            params = self._parse_query()
            tid = params.get('task_id', [''])[0]
            if not tid:
                self._json({'error': 'missing task_id'})
                return
            t = get_task(tid)
            if not t:
                self._json({'error': 'task not found'})
                return
            tu = t.get('thread_url') or ''
            tu = _thread_url_of(t.get('thread_id'), tu)
            self._json({'task_id': tid, 'thread_id': t.get('thread_id', ''),
                        'title': t.get('title', ''),
                        'kind': t.get('kind', ''),
                        'category': t.get('category') or DEFAULT_CATEGORY,
                        'thread_url': tu,
                        'status': t['status'], 'step': t.get('step', ''), 'msg': t.get('msg', ''),
                        'created_at': t.get('created_at', ''), 'updated_at': t.get('updated_at', '')})
            return
        if path == '/api/import/list':
            params = self._parse_query()
            status_f = params.get('status', [''])[0]
            origin_f = params.get('origin', [''])[0].strip()
            q = params.get('q', [''])[0].strip()
            page = max(1, int(params.get('page', ['1'])[0]))
            per_page = max(1, min(5000, int(params.get('per_page', ['50'])[0])))
            # 2026-09-20 A/B/C 组: 分类筛选 / 步骤筛选 / 排序 / 无变化短路由
            cat_f = params.get('category', [''])[0].strip()
            step_f = params.get('step', [''])[0].strip()
            sort_f = params.get('sort', ['created_at'])[0].strip()
            order_f = params.get('order', ['desc'])[0].strip().lower()
            sig_in = params.get('sig', [''])[0].strip()
            sort_cols = {'created_at': 'created_at', 'thread_id': 'thread_id',
                         'title': 'title', 'status': 'status', 'category': 'category'}
            order_sql = 'ASC' if order_f == 'asc' else 'DESC'
            if sort_f == 'progress':
                prog_case = ("CASE WHEN status='done' THEN 100 WHEN status='failed' THEN 100 "
                             "WHEN step='init' THEN 5 WHEN step='push' THEN 15 "
                             "WHEN step IN ('wait','wait_video') THEN 40 WHEN step IN ('scrape') THEN 60 "
                             "WHEN step IN ('nfo','strm') THEN 80 WHEN step='scan' THEN 92 "
                             "WHEN status='queued' THEN 5 ELSE 50 END")
                order_by = f'{prog_case} {order_sql}, created_at DESC'
            else:
                order_by = f"{sort_cols.get(sort_f, 'created_at')} {order_sql}, created_at DESC"
            conn = sqlite3.connect(LOG_DB)
            try:
                where, args = [], []
                if status_f:
                    where.append('status=?'); args.append(status_f)
                if origin_f == 'avdb':
                    where.append("origin='avdb'")
                elif origin_f in ('sehuatang', 'web'):
                    where.append("(origin IS NULL OR origin<>'avdb')")
                if q:
                    where.append('(thread_id LIKE ? OR title LIKE ? OR task_id LIKE ?)')
                    args += [f'%{q}%', f'%{q}%', f'%{q}%']
                if cat_f:
                    where.append('IFNULL(category, ?)=?'); args += [DEFAULT_CATEGORY, cat_f]
                if step_f:
                    where.append('step=?'); args.append(step_f)
                # 2026-09-20: 「待补元数据」筛选 (done 但未走到 scan = MDC 刮削不完整等油猴补)
                # 且排除「同一帖已有更新的成功任务」的旧记录 —— 行内「🔁 重刮」重刮成功后,
                # 旧记录会自动从清单里消失, 不用手工清理 (SNOS-335 实测: 83e40fe980a4 done/scan)
                if params.get('pending_md', [''])[0].strip() in ('1', 'true'):
                    where.append(PENDING_MD_SQL)
                where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
                # B 组: 无变化短路由。sig = (全局总条数, 最后更新时间, 该筛选下条数)
                # 全部基于会在 save_task 时变化的列, 任何任务动一下都会让 sig 变。
                sig_row = conn.execute('SELECT COUNT(*), IFNULL(MAX(updated_at), "") FROM import_log').fetchone()
                sig = f'{sig_row[0]}:{sig_row[1]}'
                if sig_in and sig_in == sig:
                    self._json({'unchanged': True, 'sig': sig})
                    return
                total = conn.execute(f'SELECT COUNT(*) FROM import_log{where_sql}', args).fetchone()[0]
                # category 一并返回 (2026-09-20): 面板显示分类徽章 + 批量页复用同一接口
                rows = conn.execute(f'''SELECT task_id, thread_id, title, magnet, kind, status, step, msg, created_at, updated_at, thread_url, origin, category,
                                        EXISTS(SELECT 1 FROM import_log n WHERE n.thread_id=import_log.thread_id AND n.status='done'
                                               AND n.step='scan' AND n.created_at>import_log.created_at)
                                        FROM import_log{where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?''',
                                    args + [per_page, (page - 1) * per_page]).fetchall()
                cols = ['task_id', 'thread_id', 'title', 'magnet', 'kind', 'status', 'step', 'msg', 'created_at', 'updated_at', 'thread_url', 'origin', 'category', 'superseded']
                tasks = [dict(zip(cols, r)) for r in rows]
                # 帖子链接: 入库保存的真实 URL 优先, 无则按色花堂模板兜底 (2026-08-20, 已去 MySQL)
                for t in tasks:
                    t['thread_url'] = _thread_url_of(t.get('thread_id'), t.get('thread_url') or '')
                    t['origin'] = t.get('origin') or ''
                stats = {s: conn.execute('SELECT COUNT(*) FROM import_log WHERE status=?', (s,)).fetchone()[0]
                         for s in ('queued', 'running', 'pending_manual', 'done', 'failed')}
                stats['total'] = conn.execute('SELECT COUNT(*) FROM import_log').fetchone()[0]
                stats['avdb'] = conn.execute("SELECT COUNT(*) FROM import_log WHERE origin='avdb'").fetchone()[0]
                # 2026-09-20: done 但未走到 scan 的 = 「MDC 刮削不完整, 待油猴补元数据」终态
                # (兜底分支 save_task(status='done', step='nfo')), 面板需与真·完成区分
                stats['pending_md'] = conn.execute(
                    f"SELECT COUNT(*) FROM import_log WHERE {PENDING_MD_SQL}").fetchone()[0]
                stats['sehuatang'] = stats['total'] - stats['avdb']
                # A 组「重试全部推送失败」用: 可自动重续的失败任务数
                stats['failed_push'] = conn.execute(
                    "SELECT COUNT(*) FROM import_log WHERE status='failed' AND step='push'").fetchone()[0]
                self._json({'total': total, 'page': page, 'per_page': per_page,
                            'total_pages': max(1, (total + per_page - 1) // per_page),
                            'sort': sort_f, 'order': order_f, 'sig': sig,
                            'stats': stats,
                            'tasks': tasks})
            finally:
                conn.close()
            return
        if path == '/api/import/history':
            params = self._parse_query()
            tid = params.get('task_id', [''])[0]
            if not tid:
                self._json({'error': 'missing task_id'})
                return
            conn = sqlite3.connect(LOG_DB)
            try:
                rows = conn.execute('SELECT status, step, msg, ts FROM import_log_history WHERE task_id=? ORDER BY id', (tid,)).fetchall()
                self._json({'task_id': tid,
                            'history': [{'status': r[0], 'step': r[1], 'msg': r[2], 'ts': r[3]} for r in rows]})
            finally:
                conn.close()
            return
        if path == '/avdb':
            self._html(AVDB_PAGE)
            return
        if path == '/api/avdb/status':
            # avdb 连接器面板数据 (只读本地 sqlite + 台账, 零网络请求)
            self._json(avdb_status())
            return
        if path == '/tasks':
            self._html(render_tasks_page())
            return
        if path == '/api/login/qrcode':
            # 发起 115 扫码全局登录(后台线程): 生成二维码 -> 等扫码 -> 换open token -> 全局分发
            st = LOGIN_STATE.get('status')
            if st == 'running':
                self._json({'status': 'running', 'msg': LOGIN_STATE.get('msg', '登录进行中'), 'qr': '/api/login/qrcode.png'})
                return
            th = threading.Thread(target=global_login_115.do_global_login,
                                  kwargs={'timeout': 180}, daemon=True)
            th.start()
            time.sleep(1.5)  # 给二维码生成留时间
            self._json({'status': LOGIN_STATE.get('status', 'running'),
                        'msg': LOGIN_STATE.get('msg', '登录进行中'), 'qr': '/api/login/qrcode.png'})
            return
        if path == '/api/login/status':
            self._json({'status': LOGIN_STATE.get('status', 'idle'),
                        'msg': LOGIN_STATE.get('msg', ''),
                        'updated_at': LOGIN_STATE.get('updated_at', ''),
                        'qr': '/api/login/qrcode.png'})
            return
        if path == '/api/login/qrcode.png':
            if not os.path.exists(QR_PNG):
                self._json({'error': '二维码尚未生成'}, 404)
                return
            try:
                with open(QR_PNG, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({'error': str(e)}, 500)
            return
        self._json({'error': 'Not found'}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/import':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            thread_id = body.get('thread_id')
            magnet = body.get('magnet')
            title = body.get('title')
            thread_url = body.get('thread_url')
            # kind 必填: 油猴脚本已取消自动判定, 每个链接必须手工指定番号/非番号 (用户规则 2026-08-12)
            kind = body.get('kind')
            if kind not in ('fanhao', 'non_fanhao'):
                self._json({'error': 'kind 必填, 只能是 fanhao(影片/番号) 或 non_fanhao(剧集/非番号), 已取消自动判定'}, 400)
                return
            # category 可选 (2026-09-06): 推送时选分类, 空/非法回退 av(全进已刮削/AV)
            category = norm_category(body.get('category'))
            if thread_id or magnet:
                tid = start_import(thread_id=thread_id, magnet=magnet, title=title, thread_url=thread_url,
                                   kind=kind, category=category)
                self._json({'task_id': tid, 'thread_id': str(thread_id or ''), 'magnet': (magnet or '')[:60], 'category': category})
            else:
                self._json({'error': '需要 thread_id 或 magnet'}, 400)
            return
        if path == '/api/import/batch':
            # 多磁链批量入库 (2026-09-20): 面板一次提交多条。
            # body: {"links": "一行一条..." | ["magnet:...", ...], "kind": "fanhao",
            #        "category": "fc2", "dry_run": false,
            #        "thread_id": "1234567", "title": "...", "thread_url": "..."}
            # 行尾可写 "#<分类key>" 单条覆盖 category; dry_run=true 只回解析结果不入队 (供面板预览/自检)。
            # thread_id/title/thread_url (2026-09-21) 透传给每条任务, 否则落点退化成 manual_<hash8> 且非番号不走剧集模式。
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            raw = body.get('links')
            if raw is None:
                raw = body.get('items') or []
            res = batch_import(raw, body.get('kind'), body.get('category'),
                               dry_run=bool(body.get('dry_run')),
                               thread_id=body.get('thread_id'), title=body.get('title'),
                               thread_url=body.get('thread_url'))
            self._json(res, 400 if res.get('error') else 200)
            return
        if path == '/api/import/lookup':
            # 磁链库存反查 (v1.13.0, 2026-09-21): 油猴面板徽章"这条磁链在不在我 115 里"。
            # body: {"links": ["magnet:?xt=urn:btih:...", "ed2k://|file|...|"]}  ← 整个帖子的磁链一次带走
            # v1.14.0 起再加帖子上下文 (与入库 /api/import/batch 同一份来源):
            #   {"title": "SNOS-403 …", "thread_id": 123, "thread_url": "...",
            #    "contexts": [["就近文本","上一层","再上一层"], ...]}   ← 与 links 严格同序
            #   裸磁链 (没有 &dn=) 靠它抠番号, 否则只能显示 ⚠️ 未校验。
            # 响应: {"items":[{link, link_hash, fanhao, state: in|out|unknown, count, video, dir,
            #                  reason?, checked?, src?, cached?}], "req_115": n, "counts": {...}}
            # 三态语义见 inventory_lookup 顶部注释 —— unknown 绝不等于"不在库"。
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            res = inventory_lookup(
                body.get('links') or [],
                title=body.get('title') or '',
                thread_id=body.get('thread_id') or '',
                thread_url=body.get('thread_url') or '',
                contexts=_inv_clean_ctxs(body.get('contexts')),
                single=body.get('single'),
            )
            self._json(res, 400 if res.get('error') else 200)
            return
        if path == '/api/import/adopt':
            # avdb 下载 → 一键入库 (2026-09-19): 不重新下载, 把 avdb 已下好(或正在下)的片接进入库管线。
            # body: {"id": 9} 或 {"number": "MIDV-586"}; 可选 {"category": "av", "dry_run": true}
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            number = (body.get('number') or '').strip()
            dl_id = body.get('id') or body.get('dl_id')
            if not number and not dl_id:
                self._json({'error': '需要 number 或 id'}, 400)
                return
            try:
                info = adopt_resolve(number=number or None, dl_id=dl_id)
            except Exception as e:
                self._json({'error': '解析失败: ' + str(e)[:200]}, 500)
                return
            if info.get('error'):
                self._json(info, 404)
                return
            cat = norm_category(body.get('category'))
            if body.get('dry_run'):
                # 只解析不动手: 返回落点/目标/当前视频与大小, 供确认
                try:
                    vids, total, big = _adopt_scan(Push115(), info['dir_115'])
                except Exception as e:
                    log.warning('[adopt] dry_run 列目录失败: %s', str(e)[:120])
                    vids, total, big = [], 0, []
                info['dry_run'] = True
                info['thread_id'] = info['dir_name']
                # 2026-09-20 起 adopt 不再迁移 115 目录, 落点即 avdb 原路径
                info['target_115'] = info['dir_115']
                info['videos'] = [{'name': v[1], 'size': int(v[3] or 0)} for v in vids]
                info['total_gb'] = round((total or 0) / 2 ** 30, 2)
                info['big_count'] = len(big)
                self._json(info)
                return
            try:
                tid = start_adopt(number=number or None, dl_id=dl_id, category=cat)
            except Exception as e:
                self._json({'error': str(e)[:300]}, 400)
                return
            self._json({'task_id': tid, 'number': info['number'], 'dl_id': info.get('dl_id'),
                        'src_115': info['dir_115'], 'thread_id': info['dir_name'],
                        'target_115': info['dir_115'],   # 2026-09-20 起不再迁移, 落点即源
                        'category': cat, 'status_url': '/api/import/status?task_id=' + tid})
            return
        if path == '/api/avdb/scan':
            # 手动补扫 (2026-09-19): 找 avdb 里没进台账、但 115 上确实有落点的漏网片。
            # body: {"apply": false} 只预览; {"apply": true} 直接提交入库。会列 115 目录(手动触发才跑)
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception:
                body = {}
            try:
                limit = max(1, min(1000, int(body.get('limit') or 40)))
            except Exception:
                limit = 40
            try:
                self._json(avdb_scan(limit=limit, do_adopt=bool(body.get('apply')),
                                     category=norm_category(body.get('category'))))
            except Exception as e:
                log.exception('[avdb] 补扫失败')
                self._json({'error': '补扫失败: ' + str(e)[:200]}, 500)
            return
        if path == '/api/avdb/pause':
            # 暂停/恢复 avdb 守护 (用文件标志, 守护每轮检查 /tmp/avdb_watch.pause)
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception:
                body = {}
            on = bool(body.get('on'))
            try:
                if on:
                    open(AVDB_PAUSE_FILE, 'w').write('paused by panel\n')
                elif os.path.exists(AVDB_PAUSE_FILE):
                    os.remove(AVDB_PAUSE_FILE)
                self._json({'paused': os.path.exists(AVDB_PAUSE_FILE)})
            except Exception as e:
                self._json({'error': str(e)[:200]}, 500)
            return
        if path == '/api/import/resume':
            # 断点重续 (2026-08-13): 复用原 task_id 重新入队, run_import 幂等跳过已完成步骤
            # (已有视频跳过推送/等待, 从清理/刮削/strm 继续), 供限频监控恢复后自动调用
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            task_id = body.get('task_id')
            if not task_id:
                self._json({'error': '需要 task_id'}, 400)
                return
            conn = sqlite3.connect(LOG_DB)
            try:
                r = conn.execute('SELECT thread_id, magnet, title, status, kind, thread_url, category FROM import_log WHERE task_id=?',
                                 (task_id,)).fetchone()
            finally:
                conn.close()
            if not r:
                self._json({'error': f'任务 {task_id} 不存在'}, 404)
                return
            thread_id, magnet, title, old_status, kind, thread_url, category = r
            # kind 缺失(老任务)时按目录位置推断: /sehuatang_tv/ 下为剧集, 否则影片
            if kind not in ('fanhao', 'non_fanhao'):
                try:
                    sp = _find_thread_path(str(thread_id or ''))
                    kind = 'non_fanhao' if sp.startswith(IMPORT_TV_ROOT) else 'fanhao'
                except Exception:
                    kind = 'fanhao'
            tid = start_import(thread_id=str(thread_id or ''), magnet=(magnet or ''),
                               title=title or '', thread_url=(thread_url or ''), kind=kind, task_id=task_id, resume=True,
                               category=category)
            self._json({'task_id': tid, 'old_status': old_status, 'kind': kind, 'resumed': True})
            return
        if path == '/api/import/confirm_video':
            # 手工确认视频已下载 (2026-08-20): 离线下载超时(未检测到视频)失败的任务,
            # 用户到 115 网盘确认视频落地后点击, 复用断点重续逻辑:
            # 目录已有视频 → 跳过推送/等待, 直接从处理阶段(清理/刮削/strm)续跑。
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            task_id = body.get('task_id')
            if not task_id:
                self._json({'error': '需要 task_id'}, 400)
                return
            conn = sqlite3.connect(LOG_DB)
            try:
                r = conn.execute('SELECT thread_id, magnet, title, status, kind, thread_url, category FROM import_log WHERE task_id=?',
                                 (task_id,)).fetchone()
            finally:
                conn.close()
            if not r:
                self._json({'error': f'任务 {task_id} 不存在'}, 404)
                return
            thread_id, magnet, title, old_status, kind, thread_url, category = r
            if kind not in ('fanhao', 'non_fanhao'):
                try:
                    sp = _find_thread_path(str(thread_id or ''))
                    kind = 'non_fanhao' if sp.startswith(IMPORT_TV_ROOT) else 'fanhao'
                except Exception:
                    kind = 'fanhao'
            tid = start_import(thread_id=str(thread_id or ''), magnet=(magnet or ''),
                               title=title or '', thread_url=(thread_url or ''), kind=kind, task_id=task_id, resume=True,
                               category=category)
            self._json({'task_id': tid, 'old_status': old_status, 'kind': kind, 'confirmed': True})
            return
        if path == '/api/import/rescrape':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            thread_id = body.get('thread_id')
            kind = body.get('kind', 'web')  # 'mdc' MDCng 刮削 / 'web' 网页爬取刮削
            if not thread_id:
                self._json({'error': '需要 thread_id'}, 400)
                return
            if kind not in ('mdc', 'web'):
                self._json({'error': 'kind 只能是 mdc 或 web'}, 400)
                return
            tid = start_rescrape(thread_id=str(thread_id), kind=kind)
            self._json({'task_id': tid, 'thread_id': str(thread_id), 'kind': kind})
            return
        if path == '/api/import/to_tv':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            thread_id = body.get('thread_id')
            if not thread_id:
                self._json({'error': '需要 thread_id'}, 400)
                return
            tid = start_to_tv(str(thread_id))
            self._json({'task_id': tid, 'thread_id': str(thread_id)})
            return
        if path == '/api/tv/refresh':
            # 剧集补充视频后重跑落地后流程 (2026-08-20): 重命名续集 + 生成 strm + 移入 Emby 剧集目录
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            thread_id = body.get('thread_id')
            if not thread_id:
                self._json({'error': '需要 thread_id'}, 400)
                return
            title = body.get('title') or ''
            tid = start_tv_refresh(str(thread_id), title or None)
            self._json({'task_id': tid, 'thread_id': str(thread_id)})
            return

        if path == '/api/metadata':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8', 'replace')) if length else {}
            except Exception as e:
                self._json({'error': 'bad json: ' + str(e)}, 400)
                return
            try:
                data, status = _handle_metadata(body)
            except Exception as e:
                log.exception('[metadata] 处理异常')
                self._json({'error': 'metadata 处理失败: ' + str(e)}, 500)
                return
            self._json(data, status)
            return
        self._json({'error': 'Not found'}, 404)



# ---- /api/metadata 元数据补充 (油猴脚本上传帖子图片/简介, 2026-08-20) ----
EMBY_TV_ROOT = '/opt/media/strm/emby_tv'
EMBY_MOVIE_ROOT = '/opt/media/strm/emby'

def _norm_name(s):
    return re.sub(r'[\s【】\[\]\(\)（）:：,，.。\-—_·]+', '', s or '').lower()

def _match_emby_tv_dir(title, thread_id):
    """根据帖子标题/thread 定位 Emby 剧集库目录 (emby_tv/<标题>)。返回 (dir, how)。"""
    base = EMBY_TV_ROOT
    if not os.path.isdir(base):
        return None, 'no-base'
    dirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    nt = _norm_name(title)
    exact = fuzzy = None
    for d in dirs:
        nd = _norm_name(d)
        if not nd:
            continue
        if title and d == title:
            return os.path.join(base, d), 'exact'
        if nt and nd == nt:
            exact = os.path.join(base, d)
        elif nt and (nd.startswith(nt) or nt.startswith(nd)):
            fuzzy = os.path.join(base, d)
    if exact:
        return exact, 'fuzzy-eq'
    if fuzzy:
        return fuzzy, 'fuzzy'
    # 2026-08-20 新命名 thread_{tid}_S01E01.strm (剧集重命名已改为 thread 前缀):
    # 按 thread_id 精确匹配 emby_tv 下含对应 strm 的目录 (不依赖标题, 最可靠)
    if thread_id:
        # 2026-08-21: 新整理逻辑目录直接命名 thread_{tid} (emby_tv/thread_x),
        # strm 文件名为 {集标题}.thread_{tid}.S01E01.(mp4).strm (thread 前缀在中间)
        d_thread = os.path.join(base, f'thread_{thread_id}')
        if os.path.isdir(d_thread):
            return d_thread, 'dir'
        # 兼容旧命名 thread_{tid}_S01E01.(mp4).strm (前缀开头) 与中间命名
        pat = re.compile(rf'thread_{re.escape(thread_id)}[._]S\d{{1,2}}E')
        for d in dirs:
            dpath = os.path.join(base, d)
            try:
                for fn in os.listdir(dpath):
                    if pat.search(fn) and fn.endswith('.strm'):
                        return dpath, 'thread'
            except Exception:
                continue
    # 旧命名 {show}.S01E01.strm: 从本地 strm 文件名提取 unit 匹配 (正则兼容下划线分隔)
    local = _local_strm_dir(f'/sehuatang_tv/thread_{thread_id}')
    unit = None
    if os.path.isdir(local):
        for root, _, files in os.walk(local):
            for fn in files:
                if fn.lower().endswith('.strm'):
                    m = re.sub(r'[._]S\d{1,2}E\d{1,3}.*$', '', fn)
                    m = os.path.splitext(m)[0]
                    unit = m
                    break
            if unit:
                break
    if unit:
        nu = _norm_name(unit)
        for d in dirs:
            nd = _norm_name(d)
            if nd == nu or (nu and (nd.startswith(nu) or nu.startswith(nd))):
                return os.path.join(base, d), 'unit'
    return None, 'none'

def _handle_metadata(body):
    """保存海报/图片 + 写 tvshow.nfo + 触发 Emby 刷新。body: {thread_id,title,desc,kind,images}"""
    import base64 as _b64
    thread_id = str(body.get('thread_id') or '').strip()
    title = (body.get('title') or '').strip()
    desc = (body.get('desc') or '').strip()
    kind = body.get('kind')
    images = body.get('images') or []
    if not thread_id:
        return {'error': '需要 thread_id'}, 400
    if kind not in ('fanhao', 'non_fanhao'):
        try:
            conn = sqlite3.connect(LOG_DB)
            r = conn.execute('SELECT title, kind FROM import_log WHERE thread_id=? AND kind IN ("fanhao","non_fanhao") ORDER BY rowid DESC LIMIT 1',
                             (thread_id,)).fetchone()
            conn.close()
            if r:
                if not title:
                    title = r[0] or ''
                kind = r[1]
        except Exception as e:
            log.warning('[metadata] 查 import_log 失败: %s', str(e)[:100])
    if kind not in ('fanhao', 'non_fanhao'):
        kind = 'non_fanhao'

    def decode_img(data):
        if not data:
            return None
        try:
            if data.startswith('data:'):
                data = data.split(',', 1)[1]
            raw = _b64.b64decode(data)
            if len(raw) > 8 * 1024 * 1024:
                return None
            return raw
        except Exception:
            return None

    if kind == 'non_fanhao':
        target, how = _match_emby_tv_dir(title, thread_id)
        if not target:
            cands = []
            if os.path.isdir(EMBY_TV_ROOT):
                cands = [d for d in os.listdir(EMBY_TV_ROOT) if os.path.isdir(os.path.join(EMBY_TV_ROOT, d))][:20]
            log.warning('[metadata] 未匹配到 Emby 剧集目录: thread=%s title=%s cands=%d',
                        thread_id, title[:30], len(cands))
            return {'error': f'未匹配到 Emby 剧集目录 (thread={thread_id}, title={title[:30]})', 'candidates': cands}, 404
    else:
        target = os.path.join(EMBY_MOVIE_ROOT, f'thread_{thread_id}')
        if os.path.isdir(target):
            subs = [d for d in os.listdir(target) if os.path.isdir(os.path.join(target, d))]
            if len(subs) == 1:
                target = os.path.join(target, subs[0])
        how = 'movie'
    os.makedirs(target, exist_ok=True)
    saved = []
    poster_written = False
    fanart_written = False
    backdrop_idx = 0
    for idx, img in enumerate(images[:6]):
        raw = decode_img(img.get('data'))
        if raw is None:
            continue
        is_poster = bool(img.get('poster')) or (not poster_written and idx == 0)
        if is_poster:
            fn = 'poster.jpg'
            poster_written = True
        elif not fanart_written:
            fn = 'fanart.jpg'
            fanart_written = True
        else:
            backdrop_idx += 1
            fn = 'backdrop1.jpg' if backdrop_idx == 1 else f'backdrop{backdrop_idx}.jpg'
        p = os.path.join(target, fn)
        try:
            with open(p, 'wb') as f:
                f.write(raw)
            saved.append(fn)
        except Exception as e:
            log.warning('[metadata] 写图片失败 %s: %s', fn, str(e)[:100])
    if kind == 'non_fanhao' and poster_written:
        try:
            import shutil
            shutil.copyfile(os.path.join(target, 'poster.jpg'), os.path.join(target, 'tvshow.jpg'))
            saved.append('tvshow.jpg')
        except Exception:
            pass
    if kind == 'non_fanhao':
        import xml.sax.saxutils as _sax
        esc = lambda s: _sax.escape(s or '')
        nfo = ('<?xml version="1.0" encoding="utf-8"?>' + '\n' +
               '<tvshow>' + '\n' +
               f'  <title>{esc(title)}</title>' + '\n' +
               f'  <plot>{esc(desc)}</plot>' + '\n' +
               f'  <uniqueid type="sehuatang">{esc(thread_id)}</uniqueid>' + '\n' +
               '</tvshow>' + '\n')
        try:
            with open(os.path.join(target, 'tvshow.nfo'), 'w', encoding='utf-8') as f:
                f.write(nfo)
            saved.append('tvshow.nfo')
        except Exception as e:
            log.warning('[metadata] 写 nfo 失败: %s', str(e)[:100])
    refreshed = False
    try:
        refreshed = media_refresh(timeout=10) == 204
    except Exception as e:
        log.warning('[metadata] 媒体库刷新触发失败(Emby/Jellyfin): %s', str(e)[:100])
    # 预热 (2026-08-20): 用户补充元数据后对新条目做媒体信息预探测 → 首播秒开。
    # 影片成功路径 (MDC 刮削) 已在入库流程预热; 此处覆盖 MDC 失败/剧集等待油猴补充的场景。
    try:
        t_task, t_title = None, title or ''
        try:
            cc = sqlite3.connect(LOG_DB)
            rr = cc.execute('SELECT task_id, title FROM import_log WHERE thread_id=? AND title IS NOT NULL AND title!=\'\' ORDER BY created_at DESC LIMIT 1',
                            (thread_id,)).fetchone()
            cc.close()
            if rr:
                t_task = rr[0]
                if not t_title:
                    t_title = rr[1] or ''
        except Exception as _e:
            log.warning('[metadata] 查最近任务失败: %s', str(_e)[:100])
        if t_task:
            _start_prewarm(target, t_task, t_title or f'thread_{thread_id}')
    except Exception as e:
        log.warning('[metadata] 预热启动失败: %s', str(e)[:120])
    log.info('[metadata] thread=%s kind=%s dir=%s how=%s files=%s refresh=%s',
             thread_id, kind, target, how, saved, refreshed)
    # 写入任务监控 (2026-08-20): 独立一条记录, 用户可在 /tasks 页面看到每次元数据上传
    try:
        save_task(uuid.uuid4().hex[:12], status='done', step='metadata',
                  msg=f'📤 元数据补充: {len(saved)} 个文件' + ('，已触发 Emby 刷新' if refreshed else ''),
                  thread_id=thread_id, title=(title or f'thread_{thread_id}')[:300], kind='metadata')
    except Exception as e:
        log.warning('[metadata] 写监控记录失败: %s', str(e)[:100])
    return {'ok': True, 'dir': target, 'matched': how, 'files': saved, 'refreshed': refreshed}, 200


# ============================== Emby 删除 → 115 删除同步 (2026-08-20) ==============================
# 用户需求: Emby 删除影片 → 删除 115 对应 thread 目录(回收站);
#           Emby 删除剧集单集 → 只删 115 对应单集文件; 该 thread 在 115 只剩最后一集 → 删整个 115 目录。
# 机制: 轮询扫描 Emby 索引的两个 strm 库(emby/emby_tv), 发现消失的 strm(被 Emby 删除媒体文件)
#       → 从快照中解析其 115 路径(strm 内容里的 URL) → CD2 DeleteFile(进 115 回收站, 可恢复) 删除。
# 安全:
#   - 首轮只建快照不删任何东西
#   - 消失需连续 STRM_CONFIRM_ROUNDS 轮确认(滤掉 MDCng 整理/移动造成的本地路径变化)
#   - 删除前检查"115 位置是否仍被其他 strm 覆盖"(移动/重建则不删)
#   - 只处理 /sehuatang|/sehuatang_tv 前缀; 剧集单集解析不到 115 文件路径时保守跳过
STRM_SCAN_ROOTS = [EMBY_MOVIE_ROOT, EMBY_TV_ROOT]
STRM_SCAN_INTERVAL = 15          # 轮询间隔(秒)
STRM_CONFIRM_ROUNDS = 2          # 消失需连续确认轮数
STRM_DELETE_MAX_RETRY = 10       # 删除失败最大重试次数
STRM_DELETE_SYNC = os.environ.get('STRM_DELETE_SYNC', '0') == '1'   # 本地版默认关闭 (Emby 删除联动需 SmartStrm 路径映射)

_RE_115PATH = re.compile(r'/(sehuatang_tv|sehuatang)/[^/\s?]+(?:/[^/\s?]+)*')


def _parse_strm_115(content):
    """从 strm 内容 URL 解析 115 路径 → {'kind','root_115','file_115'} 或 None"""
    try:
        m = _RE_115PATH.search(content or '')
        if not m:
            return None
        path = urllib.parse.unquote(m.group(0))
        parts = [p for p in path.split('/') if p]          # [sehuatang, thread_x, ...]
        if len(parts) < 2:
            return None
        if parts[0] == 'sehuatang_tv':
            # 剧集: /sehuatang_tv/thread_x/...
            idx = 2
        else:
            # 影片分类布局 (2026-09-06): /sehuatang/<分类>/thread_x|manual_xxx/...
            # root_115 取到 thread/manual 目录, 避免分类根被整目录删除
            idx = 2
            for i in range(1, len(parts)):
                if parts[i].startswith(('thread_', 'manual_')):
                    idx = i + 1
                    break
            if idx < 2:
                idx = 2
        root = '/' + '/'.join(parts[:idx])
        return {'kind': 'tv' if parts[0] == 'sehuatang_tv' else 'movie',
                'root_115': root,
                'file_115': path if len(parts) > idx else None}
    except Exception as e:
        log.warning('[strm-del] 解析 115 路径失败: %s', str(e)[:100])
        return None


def _build_strm_snapshot():
    """扫描本地 strm 库 → {本地绝对路径: {kind, root_115, file_115}}"""
    snap = {}
    for root in STRM_SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith('.strm'):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    with open(p, encoding='utf-8', errors='replace') as f:
                        info = _parse_strm_115(f.read(1024))
                except Exception:
                    info = None
                if info:
                    snap[p] = info
    return snap


class StrmDeleteSync:
    """Emby 删除同步器: 轮询本地 strm 库, 发现 Emby 删除的 strm → 删除 115 对应目录/单集"""

    def __init__(self):
        self._prev = None          # 上一轮快照
        self._pending = {}         # local_path -> {'info':..., 'rounds':n, 'fails':n}
        self._push = Push115()     # 复用 CD2 客户端(auth 自动刷新)

    # ---- 主循环 ----
    def run(self):
        log.info('[strm-del] Emby 删除同步器启动: roots=%s interval=%ds confirm=%d',
                 STRM_SCAN_ROOTS, STRM_SCAN_INTERVAL, STRM_CONFIRM_ROUNDS)
        while True:
            try:
                snap = _build_strm_snapshot()
                if self._prev is None:
                    log.info('[strm-del] 首轮快照建立: %d 个 strm (本功能从下一轮开始生效)', len(snap))
                else:
                    self._process_diffs(self._prev, snap)
                    self._execute_pending(snap)
                self._prev = snap
            except Exception as e:
                log.warning('[strm-del] 轮询异常: %s', str(e)[:150])
            time.sleep(STRM_SCAN_INTERVAL)

    # ---- 差异处理 ----
    def _process_diffs(self, prev, now):
        for path, info in prev.items():
            if path in now:
                if path in self._pending:          # 重新出现(重建/恢复) → 取消
                    del self._pending[path]
                continue
            # 消失: 115 位置是否仍被其他 strm 覆盖(移动/整理, 非删除)
            if info['kind'] == 'movie':
                covered = any(k != path and v.get('root_115') == info['root_115']
                              for k, v in now.items())
            else:
                covered = any(k != path and v.get('file_115') and v['file_115'] == info['file_115']
                              for k, v in now.items())
            if covered:
                if path in self._pending:
                    del self._pending[path]
                continue
            pend = self._pending.setdefault(path, {'info': info, 'rounds': 0, 'fails': 0})
            pend['rounds'] += 1
            if pend['rounds'] == STRM_CONFIRM_ROUNDS:
                log.info('[strm-del] strm 连续 %d 轮消失: %s → 115:%s', STRM_CONFIRM_ROUNDS, path, info['root_115'])

    # ---- 执行删除 ----
    def _execute_pending(self, snap):
        for path, pend in list(self._pending.items()):
            if os.path.exists(path):               # 本地重新出现 → 取消
                del self._pending[path]
                continue
            info = pend['info']
            ok, msg = False, ''
            try:
                ok, msg = self._delete(info, snap)
            except Exception as e:
                msg = str(e)[:120]
            if ok:
                log.info('[strm-del] 已删 115: %s', msg)
                self._log_delete(info, f'🗑 已删 115: {msg}')
                del self._pending[path]
                try:
                    self._cleanup_local(path, info)
                except Exception as e:
                    log.warning('[strm-del] 本地清理失败: %s', str(e)[:100])
                time.sleep(1)                      # 限频: 串行删除间隔
            else:
                pend['fails'] += 1
                log.warning('[strm-del] 删除失败(%d/%d): %s | %s', pend['fails'], STRM_DELETE_MAX_RETRY, path, msg)
                if pend['fails'] >= STRM_DELETE_MAX_RETRY:
                    self._log_delete(info, f'🗑 删除失败已放弃: {msg}', failed=True)
                    del self._pending[path]

    def _delete(self, info, snapshot_now):
        """按规则删除 115。snapshot_now 为执行时刻最新快照(用于判断剧集剩余)"""
        cd2 = self._push._cd2_client()
        if info['kind'] == 'movie':
            target = '/115open' + info['root_115']
            # 删除前确认 115 目标存在(幂等; 不存在则视为已删除)
            if cd2.find_file(target) is None:
                return True, f'{info["root_115"]} (115 已不存在, 跳过)'
            return cd2.delete_file(target), f'{info["root_115"]} (影片目录)'
        # ---- 剧集 ----
        remain = [p for p, v in snapshot_now.items() if v.get('root_115') == info['root_115']]
        if remain:
            # 还有其他集 → 只删这一集文件
            if not info.get('file_115'):
                return False, f'{info["root_115"]}: 解析不到单集 115 路径, 保守跳过'
            target = '/115open' + info['file_115']
            if cd2.find_file(target) is None:
                return True, f'{info["file_115"]} (115 已不存在, 跳过)'
            return cd2.delete_file(target), f'{info["file_115"]} (剧集单集)'
        # 最后一集 → 删整个 115 thread 目录
        target = '/115open' + info['root_115']
        if cd2.find_file(target) is None:
            return True, f'{info["root_115"]} (115 已不存在, 跳过)'
        return cd2.delete_file(target), f'{info["root_115"]} (剧集最后一集, 整目录)'

    # ---- 本地清理(只删安全部分) ----
    def _cleanup_local(self, local_path, info):
        # 空目录向上清理
        d = os.path.dirname(local_path)
        for root in STRM_SCAN_ROOTS:
            if d != root and d.startswith(root + os.sep):
                try:
                    while d != root:
                        if os.listdir(d):
                            break
                        os.rmdir(d)
                        d = os.path.dirname(d)
                except Exception:
                    break
                break
        # 影片: thread 目录内已无任何 strm(只剩 nfo/图片残留) → 删整个本地目录
        if info['kind'] == 'movie':
            try:
                rel = os.path.relpath(os.path.dirname(local_path), EMBY_MOVIE_ROOT)
                if rel and rel != '.' and not rel.startswith('..'):
                    top = os.path.join(EMBY_MOVIE_ROOT, rel.split(os.sep)[0])
                    if os.path.isdir(top):
                        has_strm = any(f.lower().endswith('.strm') for _r, _d, fs in os.walk(top) for f in fs)
                        if not has_strm:
                            shutil.rmtree(top, ignore_errors=True)
                            log.info('[strm-del] 本地影片目录已无 strm, 清理: %s', top)
            except Exception as e:
                log.warning('[strm-del] 影片本地目录清理失败: %s', str(e)[:100])

    # ---- 监控日志 ----
    def _log_delete(self, info, msg, failed=False):
        try:
            tid = info.get('root_115', '').rsplit('/', 1)[-1]
            save_task(uuid.uuid4().hex[:12], status='failed' if failed else 'done', step='delete',
                      msg=msg, thread_id=tid, title=tid, kind='delete')
        except Exception as e:
            log.warning('[strm-del] 写监控记录失败: %s', str(e)[:100])


def _strm_delete_sync_main():
    if not STRM_DELETE_SYNC:
        log.info('[strm-del] STRM_DELETE_SYNC=0, Emby 删除同步器未启动')
        return
    try:
        StrmDeleteSync().run()
    except Exception as e:
        log.error('[strm-del] 同步器退出: %s', str(e)[:150])


def _requeue_stale_tasks():
    """服务启动恢复: 把 DB 里 status=queued/running 的任务重新入队 (2026-08-13)
    场景: 服务重启/崩溃时内存队列丢失, 任务卡在 queued 永远不执行。
    双队列恢复规则 (2026-08-15):
      - rescrape/to_tv 任务 → POST 处理队列 (串行)
      - import 任务按当前 step 判断:
          init/push/wait → 离线下载队列 (resume 断点续跑: 已有视频跳过推送)
          scrape/nfo/strm/scan → 处理队列 (跳过下载, 直接从清理/刮削继续)
    重新入队后 _run_import_dl/_run_import_post 幂等跳过已完成步骤, 天然断点续跑。
    magnet 传空: 有 thread_id 时走 DB links 表全量磁力, 避免截断磁力漏推。"""
    try:
        c = sqlite3.connect(LOG_DB)
        rows = c.execute("SELECT task_id, thread_id, magnet, title, kind, step, msg, category, src_115 FROM import_log "
                         "WHERE status IN ('queued','running') ORDER BY created_at").fetchall()
        c.close()
    except Exception as e:
        log.warning('[startup] 查询未完成任务失败: %s', e)
        return
    if not rows:
        log.info('[startup] 无未完成任务需恢复')
        return
    POST_STEPS = ('scrape', 'nfo', 'strm', 'scan', 'move', 'rename')
    for task_id, thread_id, magnet, title, kind, step, msg, category, src_115 in rows:
        m = msg or ''
        # avdb adopt 任务 (2026-09-19): 有 src_115 记录 → 重新走 adopt 流程 (等完成→处理)
        # 2026-09-20: adopt 不再迁移 115 目录 → 落点恒为 src_115, 重启后统一重走 adopt
        # (_adopt_wait_ready 会先快速确认下载已完成, 随即转处理队列); 不再用
        # _find_thread_path 猜目录 —— 那对"还没处理完"的任务会误判成"已迁移"。
        if src_115 and (step or '').startswith(('adopt', 'wait', 'push', 'init')):
            save_task(task_id, status='queued', step='adopt_init',
                      msg='服务重启, adopt 任务重新入队(按 avdb 原落点续跑)',
                      thread_id=str(thread_id or ''), kind=kind or 'fanhao', category=category, src_115=src_115)
            _submit_dl(_run_import_adopt, (task_id, str(thread_id or ''), str(thread_id or ''),
                                           src_115, category, title or '', Push115.link_hash(magnet or '')))
            log.info('[startup] 重新入队 adopt 任务 %s (src=%s)', task_id, src_115)
            continue
        # 按任务类型恢复: rescrape/to_tv 走各自流程, 其余走 import (断点续跑)
        if '重新刮削' in m:
            save_task(task_id, status='queued', step='init', msg='服务重启, 重新刮削任务重新入队(处理队列)',
                      thread_id=str(thread_id or ''), kind=kind or 'web')
            _submit_post(run_rescrape, (task_id, str(thread_id or ''), kind or 'web'))
            log.info('[startup] 重新入队重刮任务 %s (thread=%s)', task_id, thread_id or '-')
            continue
        if '归为剧集' in m:
            save_task(task_id, status='queued', step='init', msg='服务重启, 剧集化任务重新入队(处理队列)',
                      thread_id=str(thread_id or ''), kind='non_fanhao')
            _submit_post(run_to_tv, (task_id, str(thread_id or '')))
            log.info('[startup] 重新入队剧集化任务 %s (thread=%s)', task_id, thread_id or '-')
            continue
        if (step or '') in POST_STEPS:
            # 已在处理阶段 → 直接回处理队列 (跳过下载)
            save_task(task_id, status='queued', step='init',
                      msg=f'服务重启, 处理阶段任务({step or "?"})重新入队, 跳过下载直接处理',
                      thread_id=str(thread_id or ''), magnet='', title=(title or '')[:300], kind=kind or '')
            _submit_post(_run_import_post, (task_id, str(thread_id or ''), '', title or '', '', kind or '', category))
            log.info('[startup] 重新入队处理阶段任务 %s (thread=%s step=%s)', task_id, thread_id or '-', step or '?')
            continue
        # 下载阶段 (init/push/wait) → 回离线下载队列 (resume 断点续跑)
        # magnet 兜底传递 (2026-08-17): import_log 里 magnet 可能被截断到 200 字符, 但
        # 对 DB 无 thread 记录、只靠传入磁力推送的任务(油猴网页入库), 必须传 magnet 才能重推;
        # DB 有磁力时 _run_import_dl resume 分支会用 DB 全量磁力, 不受截断影响。
        save_task(task_id, status='queued', step='init',
                  msg=f'服务重启, 未完成任务({step or "?"})重新入队续跑(离线下载队列)',
                  thread_id=str(thread_id or ''), magnet=(magnet or '')[:200], title=(title or '')[:300], kind=kind or '')
        _submit_dl(_run_import_dl, (task_id, str(thread_id or ''), magnet or '', title or '', '', kind or '', True, category))
        log.info('[startup] 重新入队下载阶段任务 %s (thread=%s step=%s)', task_id, thread_id or '-', step or '?')

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--media-check':
        # 冒烟自检: 换 key / 加服务器后跑这个 (python3 import_api.py --media-check)
        print('配置来源 : %s' % ('MEDIA_SERVERS' if os.environ.get('MEDIA_SERVERS') else '单机 MEDIA_SERVER_URL/EMBY_URL'))
        print('服务器数 : %d' % len(MEDIA_SERVERS))
        for row in media_server_status(probe=True):
            print('  - %-34s 家族=%-8s 前缀=%-6s 扫库=%-6s UserId=%-10s 路由=%s'
                  % (row['name'], row['flavor'], row['prefix'] or '(无)', row['refresh'],
                     row['user_id'][:8] or '-', row['route']))
            print('      token=%s url=%s' % (row['token'], row['url']))
        try:
            items = _emby_items_by_path_prefix('/mnt/g/srtm/已刮削/AV')
            print('条目     : /mnt/g/srtm/已刮削/AV 前缀命中 %d 条 (第一台)' % len(items))
        except Exception as e:
            print('条目     : 失败 %s' % str(e)[:120])
        sys.exit(0)
    init_log_db()
    # Emby 删除 → 115 删除同步 (2026-08-20)
    threading.Thread(target=_strm_delete_sync_main, daemon=True, name='strm-delete-sync').start()
    # 服务重启恢复: 重新入队未完成任务 (必须在线程 worker 可用后调用)
    _requeue_stale_tasks()
    port = 5081
    srv = ImportHTTPServer(('0.0.0.0', port), Handler)
    log.info('Import API (本机完整刮削) running on 0.0.0.0:%s', port)
    srv.serve_forever()
