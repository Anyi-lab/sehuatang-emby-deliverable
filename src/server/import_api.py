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
import json, os, sys, time, re, sqlite3, threading, queue, urllib.request, urllib.parse, ssl, uuid, logging, subprocess, shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from cd2grpc import MOUNT_PREFIX   # /115open, CD2 API 落地检测 (2026-08-19)
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
    _cc.close()
except Exception as _e:
    log.warning('import_log 迁移 thread_url 列失败: %s', str(_e)[:100])

def _thread_url_of(thread_id, saved_url=''):
    """帖子链接: 入库保存的真实 URL 优先, 否则按色花堂模板兜底 (thread-{tid}-1-1.html)"""
    tid = str(thread_id or '').strip()
    if saved_url and str(saved_url).startswith('http'):
        return str(saved_url)
    if tid:
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
# ===== 本地版适配 (2026-08-27): Emby -> Jellyfin (Windows 宿主, Emby 兼容 API) =====
EMBY_URL = 'http://172.25.224.1:8096'   # Jellyfin (Windows), 需 Jellyfin 运行中
EMBY_TOKEN = '<JELLYFIN_API_KEY>'       # 需在 Jellyfin 控制台 生成 API key 填入
# 本地 strm 根目录 (MDCng watch: 容器 /media/待看/sehuatang <-> G:\srtm\待看\sehuatang)
LOCAL_STRM_ROOT = '/mnt/g/srtm/待看/sehuatang'
# MDCng 刮削输出目录 (watch 待看/sehuatang -> target /media/已刮削/AV)
MDC_TARGET_ROOT = '/mnt/g/srtm/已刮削/AV'
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
                try:
                    from fs115 import _cache as _fs_cache
                    _fs_cache().sync_path(savepath)
                except Exception as e:
                    log.warning('push 后缓存同步失败: %s', str(e)[:100])
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

# ============================== 任务记录 ==============================
def init_log_db():
    c = sqlite3.connect(LOG_DB)
    c.execute('''CREATE TABLE IF NOT EXISTS import_log(
        task_id TEXT PRIMARY KEY, thread_id TEXT, magnet TEXT, title TEXT,
        status TEXT, step TEXT, msg TEXT, kind TEXT,
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

def _trigger_smartstrm(savepath):
    """本地版: 用 115 MCP pickcode 直接生成 strm (指向 115-Desktop 302 服务), 替代 SmartStrm webhook"""
    try:
        from mcp115 import MCP115
        m = MCP115()
        n = m.gen_strm(savepath, LOCAL_STRM_ROOT)
        log.info('[strm] 本地生成 strm %d 个: %s', n, savepath)
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
    'FC2PPV-xxx/xx.mp4' -> 'FC2PPV-xxx';  'xx.mp4'(直接落在根) -> 'xx.mp4'"""
    units = set()
    for vp in video_paths:
        parts = str(vp).replace('\\', '/').split('/')
        units.add(parts[0])
    return units

def _smartstrm_new_units(savepath, video_paths):
    """计算需要触发 SmartStrm 的新单元列表(子目录路径或根视频文件路径)。
    视频列表为空(限频/未检测到) → 返回 [], 不兜底整个目录, 避免加重 115 限频。"""
    if not video_paths:
        log.warning('[strm] 视频列表为空, 跳过增量触发(避免整个 thread 全扫): %s', savepath)
        return []
    local_dir = _local_strm_dir(savepath)
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
        sub = f'{savepath.rstrip("/")}/{u}'
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

def _local_strm_dir(savepath):
    """115 路径 -> 本地 strm 目录 (本地版: MDCng watch 根):
    /sehuatang/thread_xxx -> /mnt/g/srtm/待看/sehuatang/thread_xxx (影片)
    /sehuatang_tv/thread_xxx -> /mnt/g/srtm/待看/sehuatang/thread_xxx (剧集, 同 watch 根)"""
    name = savepath.rstrip('/').split('/')[-1]
    return f'{LOCAL_STRM_ROOT}/{name}'

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

def _find_mdc_output(cands, recent_min=20):
    """在 MDC_TARGET_ROOT 下找已刮削完成的目录 (strm+nfo+图片齐全, 目录名含番号候选 或 近期创建)"""
    root = MDC_TARGET_ROOT
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
        if cands and any(c in base for c in cands):
            return True, dp
        if not cands and now - mt < recent_min * 60:
            return True, dp
    return False, ''

def _wait_mdc_scrape(local_dir, timeout=300):
    """本地版: 等 MDCng 刮削完成。两种形态都覆盖:
    ① 原地刮削 (nfo/图片写进 local_dir) ② 移动式 (刮削后移入 已刮削/AV/<系列>/<番号>/)。"""
    cands = _extract_fanhao_candidates(local_dir)
    deadline = time.time() + timeout
    while time.time() < deadline:
        # ① 原地刮削
        ok, n, i = _has_local_metadata(local_dir)
        if ok:
            return True, f'MDCng 原地刮削完成: {n} nfo, {i} 图片'
        # ② 移动式: 目标区出现匹配番号的刮削结果
        ok2, found = _find_mdc_output(cands)
        if ok2:
            return True, f'MDCng 刮削完成并移入: {found}'
        time.sleep(5)
    _, n, i = _has_local_metadata(local_dir)
    _ok2, found = _find_mdc_output(cands)
    extra = f'; 目标区未匹配' if not found else f'; 目标区: {found}'
    return False, f'MDCng 等待超时({timeout}s): 本地 {n} nfo / {i} 图片{extra} (候选番号: {", ".join(sorted(cands))[:120] or "无"})'

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
    req = urllib.request.Request(f'{EMBY_URL}/emby/Library/Refresh?api_key={EMBY_TOKEN}', method='POST')
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.status

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

def _emby_items_by_path_prefix(prefix):
    """Emby 中 Path 以 prefix 开头的 Movie/Episode 条目 (全量查询 ~1.2MB, 本地过滤)"""
    url = (f'{EMBY_URL}/emby/Items?Recursive=true&IncludeItemTypes=Movie,Episode'
           f'&Fields=Path&Limit=5000&api_key={EMBY_TOKEN}')
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        d = json.loads(resp.read().decode('utf-8', 'replace'))
    return [it for it in d.get('Items', []) if (it.get('Path') or '').startswith(prefix)]

def _prewarm_probe_one(item_id, path):
    """对单个 Emby 条目 POST PlaybackInfo(IsPlayback=true), 触发媒体探测并写入缓存。
    返回 True 表示已拿到媒体信息(Container/流 非空)。"""
    url = (f'{EMBY_URL}/emby/Items/{item_id}/PlaybackInfo?IsPlayback=true&api_key={EMBY_TOKEN}')
    req = urllib.request.Request(url, data=b'{}',
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        d = json.loads(resp.read().decode('utf-8', 'replace'))
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

def _run_import_dl(task_id, thread_id=None, magnet=None, title=None, thread_url=None, kind=None, resume=False):
    """离线下载阶段 (DL_QUEUE, 可并发 IMPORT_DL_CONCURRENT): 推磁力 → 等视频落地 → 等下载稳定。
    完成后写 DL_CTX 并转 POST 处理队列 (清理/刮削/strm 严格串行)。"""
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
            savepath = f'{IMPORT_TV_ROOT}/thread_{thread_id}' if to_tv_mode else f'{IMPORT_ROOT}/thread_{thread_id}'
        elif web_magnets:
            lh = Push115.link_hash(web_magnets[0])
            savepath = f'{IMPORT_ROOT}/manual_{lh[:8]}' if lh else f'{IMPORT_ROOT}/manual_{str(abs(hash(web_magnets[0])))[:8]}'
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
                               'to_tv_mode': to_tv_mode, 'videos': []}
            save_task(task_id, status='failed', step='wait_video',
                      msg='离线下载超时(10分钟)未检测到视频文件，可能磁力慢/无速度。请到 115 网盘确认视频是否落地，确认后点任务详情「✅ 手工确认继续」',
                      thread_id=str(thread_id or ''), title=(title or '')[:300])
            return

        # 离线下载完成 → 记录上下文, 转 POST 处理队列 (严格串行)
        DL_CTX[task_id] = {'savepath': savepath, 'title': title, 'kind': kind,
                           'to_tv_mode': to_tv_mode, 'videos': videos}
        save_task(task_id, status='running', step='wait',
                  msg=f'离线下载完成({len(videos)} 个视频), 排队等待处理(串行)...',
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        _submit_post(_run_import_post, (task_id, thread_id, magnet, title, thread_url, kind))
    except Exception as e:
        log.exception('import dl failed')
        save_task(task_id, status='failed', step='error', msg='离线下载异常: ' + str(e)[:300],
                  thread_id=str(thread_id or ''), title=(title or '')[:300])

def _run_import_post(task_id, thread_id=None, magnet=None, title=None, thread_url=None, kind=None):
    """处理阶段 (POST_QUEUE, 严格串行 1 worker): 剧集化/清理/文件名/strm/刮削/元数据/预热/Emby 扫库。
    离线下载阶段(DL_QUEUE)完成后自动转此队列; 服务重启恢复处理阶段任务时 DL_CTX 可能缺失,
    此时用 _find_thread_path 重新定位 115 目录并重新扫描视频。"""
    ctx = DL_CTX.pop(task_id, {})
    savepath = ctx.get('savepath') or _find_thread_path(str(thread_id or ''))
    if not savepath:
        save_task(task_id, status='failed', step='error',
                  msg='无法定位 115 目录(manual 任务重启后无法恢复), 请重新提交',
                  thread_id=str(thread_id or ''), title=(title or '')[:300])
        return
    to_tv_mode = ctx.get('to_tv_mode', (kind == 'non_fanhao' and bool(thread_id)))
    videos = ctx.get('videos') or []
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
                videos = [it for it in items if not it[4] and os.path.splitext(it[1])[1].lower() in MEDIA_EXTS]
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
            new_units = _smartstrm_new_units(savepath, videos)
            if new_units:
                trig = []
                for u in new_units:
                    sub = f'{savepath.rstrip("/")}/{u}'
                    _trigger_smartstrm(sub)
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
            local_dir = _local_strm_dir(savepath)
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
                        _trigger_smartstrm(savepath)
                        time.sleep(5)
                        _trigger_smartstrm_sync(savepath)
                        time.sleep(5)
                    except Exception as e:
                        log.warning('[import] strm 生成触发失败: %s', str(e)[:100])
                    strm_cnt = _count_local_strm(local_dir)
                if strm_cnt == 0:
                    log.warning('[import] strm 仍未就位(SmartStrm 可能失败), 跳过 MDCng 直接网页兜底')
                    mdc_ok, mdc_msg = False, 'strm 未就位(SmartStrm 失败?), 跳过 MDCng 直接网页兜底'
                else:
                    mdc_ok, mdc_msg = _wait_mdc_scrape(local_dir)
            # 完整性检查: 每个影片必须 nfo+图片都齐全, 缺一不可 (用户规则 2026-08-07)
            ok2, n2, i2 = _has_local_metadata(local_dir)
            if mdc_ok and not ok2:
                mdc_msg = f'MDCng 刮削不完全: 本地 {n2} nfo / {i2} 图片 (需两者齐全), 走浏览器兜底补全'
                mdc_ok = False
            elif mdc_ok and not _mdc_title_has_chinese(local_dir):
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
            _ensure_dir_nfo(_local_strm_dir(savepath))
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
            _start_prewarm(_local_strm_dir(savepath), task_id, title or '')
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
RATELIMIT_STATE_FILE = '/tmp/115push_ratelimit_state.json'   # 本地版: 无 monitor_ratelimit2, 文件不存在即不限频
RATELIMIT_STATE_STALE_S = 600   # state 文件超过 10 分钟未更新视为监控失效, 不再阻塞

DL_QUEUE = queue.Queue()
POST_QUEUE = queue.Queue()
_DL_WORKERS = []
_POST_WORKERS = []
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
    """处理 worker (固定 1 个, 严格串行)"""
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
    """入队处理队列并确保 1 个串行 worker 存活; 立即返回"""
    POST_QUEUE.put((fn, tuple(args)))
    _ensure_post_workers()

# (2026-08-15 二次优化: 离线下载不再限制并发, _ensure_dl_workers 已移除)

def _ensure_post_workers():
    """确保有 1 个处理 worker 线程在跑 (严格串行)"""
    global _POST_WORKERS
    alive = [w for w in _POST_WORKERS if w.is_alive()]
    _POST_WORKERS = alive
    if not _POST_WORKERS:
        w = threading.Thread(target=_post_worker, daemon=True)
        w.start()
        _POST_WORKERS.append(w)

def queued_count():
    return _DL_ACTIVE + POST_QUEUE.qsize()

def start_import(thread_id=None, magnet=None, title=None, thread_url=None, kind=None, task_id=None, resume=False):
    task_id = task_id or uuid.uuid4().hex[:12]
    if _ratelimit_active():
        qmsg = '115 限频中, 任务排队等待, 恢复后自动执行'
    else:
        qmsg = '任务已入队(离线下载不限并发), 立即执行'
    save_task(task_id, status='queued', step='init', msg=qmsg,
              thread_id=str(thread_id or ''), magnet=(magnet or '')[:200], title=(title or '')[:300], kind=kind or '',
              thread_url=(thread_url or '')[:500])
    _submit_dl(_run_import_dl, (task_id, thread_id, magnet, title, thread_url, kind, resume))
    return task_id

def _find_thread_path(thread_id):
    """定位 thread 的 115 目录: 优先剧集媒体库 /sehuatang_tv/, 否则电影目录 /sehuatang/"""
    p = Push115()
    try:
        fs = p._fs_client()
        if fs.exists(f'{IMPORT_TV_ROOT}/thread_{thread_id}'):
            return f'{IMPORT_TV_ROOT}/thread_{thread_id}'
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
                try:
                    from fs115 import _cache as _fs_cache
                    _fs_cache().sync_path(savepath)
                    log.info('[rescrape] 视频读取为空, 已同步缓存(第%d次), 5s 后重试', _r + 1)
                except Exception as e:
                    log.warning('[rescrape] 缓存同步失败(第%d次): %s', _r + 1, str(e)[:100])
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
            mdc_ok, mdc_msg = _wait_mdc_scrape(local_dir, timeout=240)
            ok2, n2, i2 = _has_local_metadata(local_dir)
            if mdc_ok and not ok2:
                mdc_ok = False
                mdc_msg = f'MDCng 刮削不完全: 本地 {n2} nfo / {i2} 图片'
            elif mdc_ok and not _mdc_title_has_chinese(local_dir):
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
    old_savepath = f'{IMPORT_ROOT}/thread_{thread_id}'
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
        # 0.0 整个 thread 文件夹移动到剧集媒体库目录 (若仍在电影目录 /sehuatang/ 下)
        if fs.exists(old_savepath) and not fs.exists(savepath):
            save_task(task_id, status='running', step='move',
                      msg=f'移动 thread 文件夹到剧集媒体库: {old_savepath} -> {savepath}',
                      thread_id=thread_id, title=title[:300])
            if fs.rename(old_savepath, savepath):
                log.info('[totv] 已移动: %s -> %s', old_savepath, savepath)
                time.sleep(2)
            else:
                save_task(task_id, status='failed', step='move',
                          msg=f'移动 thread 文件夹失败: {old_savepath} -> {savepath}',
                          thread_id=thread_id, title=title[:300])
                return
        elif not fs.exists(savepath):
            save_task(task_id, status='failed', step='move',
                      msg=f'115 目录不存在: {savepath} (且旧位置 {old_savepath} 也不存在)',
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
        old_savepath = f'{IMPORT_ROOT}/thread_{thread_id}'
        # 目录兼容 (2026-08-20): 落地后停住等手工整理的剧集若目录还在影片库 /sehuatang/ 下(旧任务), 先移到剧集媒体库
        if fs.exists(old_savepath) and not fs.exists(savepath):
            save_task(task_id, status='running', step='move',
                      msg=f'移动 thread 文件夹到剧集媒体库: {old_savepath} -> {savepath}',
                      thread_id=thread_id, title=title[:300])
            if fs.rename(old_savepath, savepath):
                log.info('[tvref] 已移动: %s -> %s', old_savepath, savepath)
                time.sleep(2)
            else:
                save_task(task_id, status='failed', step='move',
                          msg=f'移动 thread 文件夹失败: {old_savepath} -> {savepath}',
                          thread_id=thread_id, title=title[:300])
                return
        elif not fs.exists(savepath):
            save_task(task_id, status='failed', step='wait',
                      msg=f'115 目录不存在: {savepath} (且旧位置 {old_savepath} 也不存在)',
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
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>一键入库</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.header h1{font-size:20px;color:#58a6ff}
.header .tag{font-size:12px;color:#8b949e;background:#21262d;padding:3px 10px;border-radius:12px}
.header .spacer{flex:1}
.header a{color:#58a6ff;text-decoration:none;font-size:14px}
.container{max-width:860px;margin:0 auto;padding:24px}
.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px 22px;margin-bottom:16px}
.panel h2{font-size:15px;color:#58a6ff;margin-bottom:14px}
.manual-box{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.manual-box label{color:#8b949e;font-size:13px;white-space:nowrap}
.manual-box input[type="text"]{flex:1;min-width:260px;padding:10px 14px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:14px;outline:none}
.manual-box input[type="text"]:focus{border-color:#58a6ff}
select{padding:9px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px;outline:none}
.btn{padding:10px 18px;border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:14px;font-weight:600;white-space:nowrap}
.btn-import{background:#1f6feb}
.btn-import:hover{background:#388bfd}
.btn-import:disabled{background:#30363d;cursor:default}
.btn-clear{background:#484f58}
.btn-clear:hover{background:#5b6672}
.btn-login{background:#6e40c9}
.btn-login:hover{background:#8957e5}
.hint{font-size:12px;color:#8b949e;margin-top:10px;line-height:1.7}
.hint a{color:#58a6ff}
.toast{position:fixed;top:20px;right:20px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;z-index:999;animation:fadein .3s}
.toast.err{background:#da3633}
@keyframes fadein{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.task-panel{position:fixed;bottom:20px;right:20px;width:380px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;z-index:99;box-shadow:0 4px 20px rgba(0,0,0,.4)}
.task-panel .panel-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.task-panel h3{font-size:14px;color:#58a6ff;margin:0}
.task-panel .panel-toggle{background:none;border:none;color:#8b949e;cursor:pointer;font-size:13px;padding:2px 8px;border-radius:4px}
.task-panel .panel-toggle:hover{background:#21262d;color:#c9d1d9}
.task-panel.collapsed{width:auto;min-width:120px;padding:10px 12px}
.task-panel.collapsed #taskList{display:none}
.task-item{font-size:13px;padding:6px 0;border-top:1px solid #30363d;display:flex;gap:8px;align-items:flex-start}
.task-item .st{flex:1}
.task-item .st .msg{color:#8b949e;font-size:12px}
.task-item .close{color:#8b949e;cursor:pointer;border:none;background:none;font-size:14px}
@media (max-width:768px){.task-panel{width:calc(100% - 16px);right:8px;bottom:8px;max-height:50vh;overflow-y:auto}}
</style>
</head>
<body>
<div class="header">
    <h1>🚀 一键入库</h1>
    <span class="tag">磁力/电驴 → 115 → strm → MDC刮削/油猴补充 → Emby</span>
    <div class="spacer"></div>
    <a href="/tasks">📋 任务监控</a>
    <button class="btn btn-login" onclick="openLogin()">📱 115 扫码登录</button>
</div>
<div class="container">
    <div class="panel">
        <h2>磁力/电驴直链入库</h2>
        <div class="manual-box">
            <label>链接:</label>
            <input type="text" id="manualMagnet" placeholder="粘贴 magnet:?xt=urn:btih:... 或 ed2k://|file|... 直接推送入库">
            <label>类型:</label>
            <select id="manualKind">
                <option value="fanhao">影片 (番号, MDC 刮削)</option>
                <option value="non_fanhao">剧集 (非番号, 油猴补充元数据)</option>
            </select>
            <button class="btn btn-clear" onclick="clearManual()" title="清空输入框">清空</button>
            <button class="btn btn-import" onclick="manualImport()">入库</button>
        </div>
        <div class="hint">
            💡 <b>影片</b>: 推磁力 → 等落地 → 清理广告小文件 → strm → MDC 刮削 → Emby 刷新 → 预热；MDC 刮削失败时在<b>帖子页用油猴脚本</b>补充元数据。<br>
            💡 <b>剧集</b>: 推磁力 → 等落地(任务显示<b>待整理</b>停住) → 先在 115 网盘手工整理视频 → 到任务页点<b>🔄整理</b> → 命名「原名.thread_x.S01E续集号」→ strm 生成 → 在<b>帖子页用油猴脚本</b>补充元数据 → 自动刷新 Emby + 预热。<br>
            📌 任务完成后请到 <a href="/tasks">任务监控页</a> 点击 thread 号直接打开原帖，用油猴「📤 上传元数据」补充缺失的元数据。
        </div>
    </div>
</div>
<div class="task-panel" id="taskPanel" style="display:none">
    <div class="panel-head">
        <h3>入库任务</h3>
        <button class="panel-toggle" id="taskToggle" onclick="toggleTaskPanel()">收起 ▾</button>
    </div>
    <div id="taskList"></div>
</div>
<script>
var pollTimers = {};

function toast(msg, isErr) {
    var t = document.createElement('div');
    t.className = 'toast' + (isErr ? ' err' : '');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function(){ t.remove(); }, 4000);
}

function esc(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showTaskPanel() {
    var p = document.getElementById('taskPanel');
    p.style.display = 'block';
    p.classList.remove('collapsed');
}
function toggleTaskPanel() {
    var p = document.getElementById('taskPanel');
    p.classList.toggle('collapsed');
    document.getElementById('taskToggle').textContent = p.classList.contains('collapsed') ? '展开 ▸' : '收起 ▾';
}
function addTaskRow(taskId, label, status, msg) {
    showTaskPanel();
    var list = document.getElementById('taskList');
    var row = document.createElement('div');
    row.className = 'task-item';
    row.id = 'task-' + taskId;
    row.innerHTML = '<div class="st"><div><b>' + esc(label) + '</b> <span class="badge"></span></div><div class="msg"></div></div>' +
        '<span class="close" onclick="closeTask('' + taskId + '')">✕</span>';
    list.appendChild(row);
    updateTaskRow(taskId, status, msg);
}
function updateTaskRow(taskId, status, msg) {
    var row = document.getElementById('task-' + taskId);
    if (!row) return;
    row.querySelector('.badge').textContent = status || '';
    row.querySelector('.msg').textContent = msg || '';
    if (status === 'done' || status === 'failed') {
        row.querySelector('.badge').style.color = status === 'done' ? '#3fb950' : '#f85149';
    }
}
function closeTask(taskId) {
    var row = document.getElementById('task-' + taskId);
    if (row) row.remove();
    if (pollTimers[taskId]) { clearInterval(pollTimers[taskId]); delete pollTimers[taskId]; }
}
async function pollTask(taskId) {
    var res = await fetch('/api/import/status?task_id=' + encodeURIComponent(taskId));
    var data = await res.json();
    if (data.error) { clearInterval(pollTimers[taskId]); delete pollTimers[taskId]; return; }
    updateTaskRow(taskId, data.status, data.msg);
    if (data.status === 'done' || data.status === 'failed') {
        clearInterval(pollTimers[taskId]);
        delete pollTimers[taskId];
    }
}

function clearManual() {
    document.getElementById('manualMagnet').value = '';
}

async function manualImport() {
    var magnet = document.getElementById('manualMagnet').value.trim();
    var kind = document.getElementById('manualKind').value;
    if (!magnet) { toast('请粘贴磁力或电驴链接', true); return; }
    var isMagnet = magnet.indexOf('magnet:') === 0;
    var isEd2k = magnet.indexOf('ed2k://') === 0;
    if (!isMagnet && !isEd2k) { toast('仅支持 magnet: 或 ed2k:// 链接', true); return; }
    showTaskPanel();
    try {
        var res = await fetch('/api/import', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({magnet: magnet, kind: kind})
        });
        var data = await res.json();
        if (data.error) { toast('入库失败: ' + data.error, true); return; }
        addTaskRow(data.task_id, isEd2k ? '手动电驴' : '手动磁力', 'queued', '任务已创建');
        pollTimers[data.task_id] = setInterval(function(){ pollTask(data.task_id); }, 3000);
        toast('已提交入库');
    } catch(e) { toast('请求失败: ' + e.message, true); }
}
</script>
<!-- 115 扫码全局登录弹窗 -->
<div id="loginModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:999;align-items:center;justify-content:center">
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:340px;text-align:center">
        <h3 style="color:#58a6ff;margin-bottom:8px">115 扫码全局登录</h3>
        <div style="color:#8b949e;font-size:13px;margin-bottom:16px">用手机 115 App 扫一扫, 登录后凭证将自动分发</div>
        <div id="qrBox" style="background:#fff;border-radius:8px;padding:8px;margin:0 auto 12px;width:240px;height:240px;display:flex;align-items:center;justify-content:center;color:#8b949e;font-size:13px">生成中...</div>
        <div id="loginStatus" style="color:#c9d1d9;font-size:13px;min-height:20px;margin-bottom:12px">请稍候...</div>
        <div style="display:flex;gap:8px;justify-content:center">
            <button onclick="closeLogin()" style="padding:8px 20px;background:#30363d;border:none;border-radius:6px;color:#c9d1d9;cursor:pointer">关闭</button>
            <button id="btnLoginAgain" onclick="startLogin()" style="display:none;padding:8px 20px;background:#6e40c9;border:none;border-radius:6px;color:#fff;cursor:pointer">重新生成二维码</button>
        </div>
    </div>
</div>
<script>
var loginTimer = null;

function openLogin() {
    document.getElementById('loginModal').style.display = 'flex';
    startLogin();
}

function closeLogin() {
    document.getElementById('loginModal').style.display = 'none';
    if (loginTimer) { clearInterval(loginTimer); loginTimer = null; }
}

function startLogin() {
    var box = document.getElementById('qrBox');
    var st = document.getElementById('loginStatus');
    box.innerHTML = '生成中...';
    st.textContent = '正在生成二维码...';
    document.getElementById('btnLoginAgain').style.display = 'none';
    fetch('/api/login/qrcode').then(function(r){return r.json()}).then(function(d){
        box.innerHTML = '<img src="/api/login/qrcode.png" style="width:220px;height:220px">';
        st.textContent = (d.msg || '请用 115 App 扫码确认') + ' (180秒内有效)';
    }).catch(function(e){ st.textContent = '生成失败: ' + e.message; });
    if (loginTimer) clearInterval(loginTimer);
    loginTimer = setInterval(pollLogin, 2500);
}

function pollLogin() {
    fetch('/api/login/status').then(function(r){return r.json()}).then(function(d){
        var st = document.getElementById('loginStatus');
        if (d.status === 'done') {
            st.innerHTML = '<span style="color:#7ee787">✅ ' + (d.msg || '全局登录成功') + '</span>';
            if (loginTimer) { clearInterval(loginTimer); loginTimer = null; }
        } else if (d.status === 'failed') {
            st.innerHTML = '<span style="color:#f85149">' + (d.msg || '登录失败') + '</span>';
            document.getElementById('btnLoginAgain').style.display = 'inline-block';
            if (loginTimer) { clearInterval(loginTimer); loginTimer = null; }
        } else if (d.status === 'running') {
            st.textContent = d.msg || '等待扫码...';
        } else {
            st.textContent = '状态: ' + d.status;
        }
    }).catch(function(e){});
}
</script>
</body>
</html>"""


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
.header a{color:#58a6ff;text-decoration:none;font-size:13px;margin-left:12px}
.container{max-width:1280px;margin:0 auto;padding:20px 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.card .num{font-size:26px;font-weight:700;margin-top:4px}
.card .lbl{font-size:12px;color:#8b949e}
.card.total .num{color:#58a6ff}.card.queued .num{color:#8b949e}
.card.running .num{color:#58a6ff}.card.done .num{color:#3fb950}
.card.failed .num{color:#f85149}
.bar{display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.bar input[type="text"]{flex:1;min-width:220px;padding:8px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;outline:none}
.bar input[type="text"]:focus{border-color:#58a6ff}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:7px 14px;background:#21262d;border:1px solid #30363d;border-radius:20px;color:#8b949e;font-size:13px;cursor:pointer;user-select:none}
.tab.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn{padding:8px 16px;background:#238636;border:1px solid #2ea043;border-radius:6px;color:#fff;font-weight:600;cursor:pointer;font-size:13px}
.btn:hover{background:#2ea043}
.btn.ghost{background:#21262d;border-color:#30363d;color:#c9d1d9}
.btn.ghost:hover{border-color:#8b949e}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #21262d;font-size:13px;vertical-align:middle}
th{background:#21262d;color:#8b949e;font-weight:600;white-space:nowrap}
tr:hover td{background:#1c2128}
td .t{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
.pct{font-size:12px;color:#8b949e;margin-left:6px;white-space:nowrap}
.msg{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#8b949e}
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
.rs-btns button{padding:8px 14px;border-radius:6px;border:none;cursor:pointer;font-weight:600;font-size:13px;color:#fff}
.rs-btns .rs-mdc{background:#6e40c9}
.rs-btns .rs-mdc:hover{background:#8957e5}
.rs-btns .rs-web{background:#1f6feb}
.rs-btns .rs-web:hover{background:#388bfd}
.rs-btns .rs-tv{background:#238636}
.rs-btns .rs-tv:hover{background:#2ea043}
.repush{background:#d64045;border-color:#d64045;color:#fff}
.repush:hover{background:#f2555a}
.rs-btns .rs-repush{background:#d64045}
.rs-btns .rs-repush:hover{background:#f2555a}
.rs-btns .rs-confirm{background:#1a7f37}
.rs-btns .rs-confirm:hover{background:#238636}
.rescrape .rs-hint{font-size:12px;color:#8b949e;margin-top:6px}
</style>
</head>
<body>
<div class="header">
  <h1>📦 入库任务监控</h1>
  <span class="tag" id="lastUpdate">-</span>
  <div class="spacer"></div>
  <label class="autoflag"><input type="checkbox" id="auto" checked> 自动刷新 5s</label>
  <button class="btn ghost" onclick="load()">🔄 刷新</button>
  <a href="/">← 一键入库</a>
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
    <input type="text" id="q" placeholder="搜索 thread / 标题 / task_id...">
    <button class="btn" onclick="searchNow()">搜索</button>
  </div>
  <table>
    <thead>
      <tr><th>创建时间</th><th>Thread</th><th>标题</th><th>类型</th><th>状态</th><th>进度</th><th>当前消息</th><th></th></tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none">暂无任务记录</div>
  <div class="pager" id="pager"></div>
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
const STEP_PCT = {init:5, push:15, wait:40, scrape:60, nfo:80, strm:80, scan:92};
const STEP_LBL = {init:'创建', push:'推送', wait:'等待落地', wait_video:'等待落地', scrape:'刮削', nfo:'元数据', strm:'生成strm', scan:'扫库', move:'移动', error:'异常'};
let curStatus = '', curPage = 1, curTask = null;

function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function pct(t){
  if(t.status==='done') return {p:100, cls:'done'};
  if(t.status==='failed') return {p:100, cls:'failed'};
  const p = STEP_PCT[t.step] || (t.status==='queued'?5:50);
  return {p, cls:''};
}
function fmt(s){return s?String(s).slice(0,19).replace('T',' '):'';}
function badge(st){return '<span class="badge '+esc(st)+'">'+(STATUS[st]||esc(st))+'</span>';}

function renderCards(stats){
  const c = [
    ['total','总数',stats.total],['queued','排队',stats.queued],
    ['running','运行中',stats.running],['pending_manual','待整理',stats.pending_manual||0],
    ['done','已完成',stats.done],['failed','失败',stats.failed]
  ];
  document.getElementById('cards').innerHTML = c.map(x=>'<div class="card '+x[0]+'"><div class="lbl">'+x[1]+'</div><div class="num">'+x[2]+'</div></div>').join('');
}

function renderRows(tasks){
  const tb = document.getElementById('tbody');
  if(!tasks.length){
    tb.innerHTML='';
    document.getElementById('empty').style.display='block';
    document.getElementById('pager').innerHTML='';
    return;
  }
  document.getElementById('empty').style.display='none';
  tb.innerHTML = tasks.map(t=>{
    const p = pct(t);
    const msg = (t.msg||'').replace(/\n/g,' ').slice(0,80);
    return '<tr>'+
      '<td style="white-space:nowrap">'+fmt(t.created_at)+'</td>'+
      '<td style="white-space:nowrap">'+(t.thread_url ? '<a href="'+esc(t.thread_url)+'" target="_blank" style="color:#58a6ff;text-decoration:none" title="打开原帖">'+esc(t.thread_id||'-')+' ↗</a>' : esc(t.thread_id||'-'))+'</td>'+
      '<td><div class="t" title="'+esc(t.title||'')+'">'+esc(t.title||'-')+'</div></td>'+
      '<td style="white-space:nowrap">'+kindLbl(t.kind)+'</td>'+
      '<td>'+badge(t.status)+'</td>'+
      '<td style="white-space:nowrap"><div style="display:flex;align-items:center;gap:6px"><div class="progress"><div class="fill '+p.cls+'" style="width:'+p.p+'%"></div></div><span class="pct">'+p.p+'%</span></div></td>'+
      '<td><div class="msg" title="'+esc(t.msg||'')+'">'+esc(msg||'-')+'</div></td>'+
      '<td style="white-space:nowrap"><button class="btn ghost" style="padding:4px 10px;font-size:12px" onclick="detail(\''+esc(t.task_id)+'\')">详情</button>'+
      (t.kind==='non_fanhao' && t.thread_id ? '<button class="btn ghost" style="padding:4px 10px;font-size:12px;margin-left:6px;color:#3fb950;border-color:#2ea043" onclick="tvRefreshQuick(\''+esc(t.task_id)+'\',\''+esc(t.thread_id)+'\')">🔄整理</button>' : '')+
      (t.status==='failed' && t.step==='push' ? '<button class="btn ghost repush" style="padding:4px 10px;font-size:12px;margin-left:6px" onclick="repush(\''+esc(t.task_id)+'\')">重推115</button>' : '')+
      (t.status==='failed' && t.step==='wait_video' ? '<button class="btn ghost rs-confirm" style="padding:4px 10px;font-size:12px;margin-left:6px" onclick="confirmVideo(\''+esc(t.task_id)+'\')">✅继续</button>' : '')+
      '</td>'+
    '</tr>';
  }).join('');
}

function renderPager(info){
  const el = document.getElementById('pager');
  if(!info || info.total_pages<=1){el.innerHTML='';return;}
  el.innerHTML = '<button class="btn ghost" '+(info.page<=1?'disabled':'')+' onclick="goPage('+(info.page-1)+')">‹ 上一页</button>'+
    '<span>第 '+info.page+' / '+info.total_pages+' 页 · 共 '+info.total+' 条</span>'+
    '<button class="btn ghost" '+(info.page>=info.total_pages?'disabled':'')+' onclick="goPage('+(info.page+1)+')">下一页 ›</button>';
}

function load(){
  const q = document.getElementById('q').value.trim();
  let url = '/api/import/list?page='+curPage+'&per_page=50';
  if(curStatus) url += '&status='+curStatus;
  if(q) url += '&q='+encodeURIComponent(q);
  fetch(url).then(r=>r.json()).then(d=>{
    if(d.error){return;}
    renderCards(d.stats||{});
    renderRows(d.tasks||[]);
    renderPager(d);
    document.getElementById('lastUpdate').textContent = '更新于 ' + new Date().toLocaleTimeString();
  }).catch(()=>{});
}

function searchNow(){curPage=1;load();}
function goPage(p){curPage=p;load();}

document.getElementById('tabs').addEventListener('click', e=>{
  const t = e.target.closest('.tab'); if(!t) return;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  curStatus = t.dataset.s || ''; curPage = 1; load();
});
document.getElementById('q').addEventListener('keydown', e=>{if(e.key==='Enter')searchNow();});

function detail(taskId){
  Promise.all([
    fetch('/api/import/history?task_id='+encodeURIComponent(taskId)).then(r=>r.json()),
    fetch('/api/import/status?task_id='+encodeURIComponent(taskId)).then(r=>r.json())
  ]).then(([h, st])=>{
    const hist = (h && h.history) || [];
    const st2 = (st && !st.error) ? st : {};
    curTask = {task_id: taskId, thread_id: st2.thread_id || '', title: st2.title || '', status: st2.status || '', step: st2.step || '', kind: st2.kind || ''};
    document.getElementById('dTitle').textContent = '任务详情 · ' + taskId;
    document.getElementById('dMeta').innerHTML =
      '<b>task_id:</b> '+esc(taskId)+'<br>'+
      '<b>类型:</b> '+kindLbl(st2.kind)+'<br>'+
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
  if(!confirm('你已确认 115 网盘中该帖视频已下载完成？\n\n将跳过推送/等待，直接从文件处理/刮削/strm 继续。')) return;
  fetch('/api/import/confirm_video', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({task_id: taskId})
  }).then(r=>r.json()).then(d=>{
    if(d.error){alert('提交失败: '+d.error);return;}
    alert('已确认，任务继续处理: '+d.task_id);
    closeDetail(); curPage=1; load();
  }).catch(e=>alert('提交失败: '+e));
}
function tvRefresh(){
  if(!curTask || !curTask.thread_id) return;
  if(!confirm('执行剧集「落地后流程」？\n\nthread_' + curTask.thread_id + '\n' + (curTask.title||'').slice(0,120) + '\n\n请确认已在 115 网盘整理好视频(删除广告/补充新集)。将执行：未命名视频重命名「原名.thread_x.S01E续集号」→ 生成 strm → 移入 Emby 剧集目录并刷新。')) return;
  fetch('/api/tv/refresh', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({thread_id: curTask.thread_id, title: curTask.title || ''})
  }).then(r=>r.json()).then(d=>{
    if(d.error){alert('提交失败: '+d.error);return;}
    alert('剧集整理任务已创建: '+d.task_id);
    closeDetail(); curPage=1; load();
  }).catch(e=>alert('提交失败: '+e));
}
function tvRefreshQuick(taskId, threadId){
  if(!confirm('执行剧集「落地后流程」？\n\nthread_' + threadId + '\n\n请确认已在 115 网盘整理好视频(删除广告/补充新集)。将执行：未命名视频重命名「原名.thread_x.S01E续集号」→ 生成 strm → 移入 Emby 剧集目录并刷新。')) return;
  fetch('/api/tv/refresh', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({thread_id: threadId})
  }).then(r=>r.json()).then(d=>{
    if(d.error){alert('提交失败: '+d.error);return;}
    alert('剧集整理任务已创建: '+d.task_id);
    load();
  }).catch(e=>alert('提交失败: '+e));
}
function gotoMeta(){
  if(!curTask || !curTask.thread_id) return;
  window.open('https://sehuatang.net/thread-' + curTask.thread_id + '-1-1.html', '_blank');
}
function repush(taskId, fromDetail){
  if(!taskId) return;
  if(!confirm('重新推送该任务到 115？\n\n（复用原任务断点重续；尚未成功推送的链接会重新推送到 115）')) return;
  fetch('/api/import/resume', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({task_id: taskId})
  }).then(r=>r.json()).then(d=>{
    if(d.error){alert('提交失败: '+d.error);return;}
    alert('已提交重推 115: '+d.task_id);
    if(fromDetail) closeDetail();
    curPage=1; load();
  }).catch(e=>alert('提交失败: '+e));
}
function rescrape(kind){
  if(!curTask || !curTask.thread_id) return;
  const way = kind==='mdc' ? 'MDC 刮削' : '网页爬取刮削';
  const ttl = (curTask.title && curTask.title !== ('thread_'+curTask.thread_id)) ? curTask.title : '(无标题)';
  if(!confirm('对以下资源执行「'+way+'」重新刮削？\n\nthread_' + curTask.thread_id + '\n' + ttl.slice(0, 120) + '\n\n（会先清理旧元数据，创建新任务并显示进度）')) return;
  fetch('/api/import/rescrape', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({thread_id: curTask.thread_id, kind: kind})
  }).then(r=>r.json()).then(d=>{
    if(d.error){alert('提交失败: '+d.error);return;}
    alert('重新刮削任务已创建: '+d.task_id);
    closeDetail(); curPage=1; load();
  }).catch(e=>alert('提交失败: '+e));
}

setInterval(()=>{if(document.getElementById('auto').checked)load();}, 5000);
load();
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
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
            self._html(HTML_PAGE)
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
                        'thread_url': tu,
                        'status': t['status'], 'step': t.get('step', ''), 'msg': t.get('msg', ''),
                        'created_at': t.get('created_at', ''), 'updated_at': t.get('updated_at', '')})
            return
        if path == '/api/import/list':
            params = self._parse_query()
            status_f = params.get('status', [''])[0]
            q = params.get('q', [''])[0].strip()
            page = max(1, int(params.get('page', ['1'])[0]))
            per_page = min(100, int(params.get('per_page', ['50'])[0]))
            conn = sqlite3.connect(LOG_DB)
            try:
                where, args = [], []
                if status_f:
                    where.append('status=?'); args.append(status_f)
                if q:
                    where.append('(thread_id LIKE ? OR title LIKE ? OR task_id LIKE ?)')
                    args += [f'%{q}%', f'%{q}%', f'%{q}%']
                where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
                total = conn.execute(f'SELECT COUNT(*) FROM import_log{where_sql}', args).fetchone()[0]
                rows = conn.execute(f'''SELECT task_id, thread_id, title, magnet, kind, status, step, msg, created_at, updated_at, thread_url
                                        FROM import_log{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?''',
                                    args + [per_page, (page - 1) * per_page]).fetchall()
                cols = ['task_id', 'thread_id', 'title', 'magnet', 'kind', 'status', 'step', 'msg', 'created_at', 'updated_at', 'thread_url']
                tasks = [dict(zip(cols, r)) for r in rows]
                # 帖子链接: 入库保存的真实 URL 优先, 无则按色花堂模板兜底 (2026-08-20, 已去 MySQL)
                for t in tasks:
                    t['thread_url'] = _thread_url_of(t.get('thread_id'), t.get('thread_url') or '')
                stats = {s: conn.execute('SELECT COUNT(*) FROM import_log WHERE status=?', (s,)).fetchone()[0]
                         for s in ('queued', 'running', 'pending_manual', 'done', 'failed')}
                stats['total'] = conn.execute('SELECT COUNT(*) FROM import_log').fetchone()[0]
                self._json({'total': total, 'page': page, 'per_page': per_page,
                            'total_pages': max(1, (total + per_page - 1) // per_page),
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
        if path == '/tasks':
            self._html(TASKS_PAGE)
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
            if thread_id or magnet:
                tid = start_import(thread_id=thread_id, magnet=magnet, title=title, thread_url=thread_url, kind=kind)
                self._json({'task_id': tid, 'thread_id': str(thread_id or ''), 'magnet': (magnet or '')[:60]})
            else:
                self._json({'error': '需要 thread_id 或 magnet'}, 400)
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
                r = conn.execute('SELECT thread_id, magnet, title, status, kind, thread_url FROM import_log WHERE task_id=?',
                                 (task_id,)).fetchone()
            finally:
                conn.close()
            if not r:
                self._json({'error': f'任务 {task_id} 不存在'}, 404)
                return
            thread_id, magnet, title, old_status, kind, thread_url = r
            # kind 缺失(老任务)时按目录位置推断: /sehuatang_tv/ 下为剧集, 否则影片
            if kind not in ('fanhao', 'non_fanhao'):
                try:
                    sp = _find_thread_path(str(thread_id or ''))
                    kind = 'non_fanhao' if sp.startswith(IMPORT_TV_ROOT) else 'fanhao'
                except Exception:
                    kind = 'fanhao'
            tid = start_import(thread_id=str(thread_id or ''), magnet=(magnet or ''),
                               title=title or '', thread_url=(thread_url or ''), kind=kind, task_id=task_id, resume=True)
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
                r = conn.execute('SELECT thread_id, magnet, title, status, kind, thread_url FROM import_log WHERE task_id=?',
                                 (task_id,)).fetchone()
            finally:
                conn.close()
            if not r:
                self._json({'error': f'任务 {task_id} 不存在'}, 404)
                return
            thread_id, magnet, title, old_status, kind, thread_url = r
            if kind not in ('fanhao', 'non_fanhao'):
                try:
                    sp = _find_thread_path(str(thread_id or ''))
                    kind = 'non_fanhao' if sp.startswith(IMPORT_TV_ROOT) else 'fanhao'
                except Exception:
                    kind = 'fanhao'
            tid = start_import(thread_id=str(thread_id or ''), magnet=(magnet or ''),
                               title=title or '', thread_url=(thread_url or ''), kind=kind, task_id=task_id, resume=True)
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
        req = urllib.request.Request(f'{EMBY_URL}/emby/Library/Refresh?api_key={EMBY_TOKEN}',
                                     data=b'', method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            refreshed = resp.status == 204
    except Exception as e:
        log.warning('[metadata] Emby 刷新触发失败: %s', str(e)[:100])
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
        root = '/' + '/'.join(parts[:2])
        return {'kind': 'tv' if parts[0] == 'sehuatang_tv' else 'movie',
                'root_115': root,
                'file_115': path if len(parts) > 2 else None}
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
        rows = c.execute("SELECT task_id, thread_id, magnet, title, kind, step, msg FROM import_log "
                         "WHERE status IN ('queued','running') ORDER BY created_at").fetchall()
        c.close()
    except Exception as e:
        log.warning('[startup] 查询未完成任务失败: %s', e)
        return
    if not rows:
        log.info('[startup] 无未完成任务需恢复')
        return
    POST_STEPS = ('scrape', 'nfo', 'strm', 'scan', 'move', 'rename')
    for task_id, thread_id, magnet, title, kind, step, msg in rows:
        m = msg or ''
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
            _submit_post(_run_import_post, (task_id, str(thread_id or ''), '', title or '', '', kind or ''))
            log.info('[startup] 重新入队处理阶段任务 %s (thread=%s step=%s)', task_id, thread_id or '-', step or '?')
            continue
        # 下载阶段 (init/push/wait) → 回离线下载队列 (resume 断点续跑)
        # magnet 兜底传递 (2026-08-17): import_log 里 magnet 可能被截断到 200 字符, 但
        # 对 DB 无 thread 记录、只靠传入磁力推送的任务(油猴网页入库), 必须传 magnet 才能重推;
        # DB 有磁力时 _run_import_dl resume 分支会用 DB 全量磁力, 不受截断影响。
        save_task(task_id, status='queued', step='init',
                  msg=f'服务重启, 未完成任务({step or "?"})重新入队续跑(离线下载队列)',
                  thread_id=str(thread_id or ''), magnet=(magnet or '')[:200], title=(title or '')[:300], kind=kind or '')
        _submit_dl(_run_import_dl, (task_id, str(thread_id or ''), magnet or '', title or '', '', kind or '', True))
        log.info('[startup] 重新入队下载阶段任务 %s (thread=%s step=%s)', task_id, thread_id or '-', step or '?')

if __name__ == '__main__':
    init_log_db()
    # Emby 删除 → 115 删除同步 (2026-08-20)
    threading.Thread(target=_strm_delete_sync_main, daemon=True, name='strm-delete-sync').start()
    # 服务重启恢复: 重新入队未完成任务 (必须在线程 worker 可用后调用)
    _requeue_stale_tasks()
    port = 5081
    srv = HTTPServer(('0.0.0.0', port), Handler)
    log.info('Import API (本机完整刮削) running on 0.0.0.0:%s', port)
    srv.serve_forever()
