# -*- coding: utf-8 -*-
"""
scrape_sehuatang.py — sehuatang → 115 刮削器 (v2)
================================================================
流程: 对已推送的 thread, 用 Playwright(有头, 过 CF) 抓取主题页
      → 提取 标题/正文简介/前2张图片
      → 生成 Emby nfo + poster.jpg + fanart.jpg(本地临时目录)
      → 修复/清理 115 目录:
          * 递归进入种子自带的子文件夹(不再只扫 thread 根)
          * 修复被误改名的文件夹(目录名带媒体扩展名 → 去掉)
          * 删除宣传视频(不含主编号)与垃圾文件(.html/.txt)
          * 正片文件名去域名前缀 (例: 前缀@FC2PPV-x.mp4 -> FC2PPV-x.mp4)
      → nfo/图片上传到 正片所在子文件夹 (SmartStrm copy 同步到本地)
      → SmartStrm 增量任务(copy_ext: nfo/jpg/png) 自动同步到本地 strm 目录

用法:
  python scrape_sehuatang.py --thread 3642661            # 刮削指定 thread
  python scrape_sehuatang.py --pending                   # 刮削所有已推送未刮削的
  python scrape_sehuatang.py --pending --list-only       # 只列清单不抓取
  python scrape_sehuatang.py --thread 3642661 --dry-run  # 抓取+生成, 不传 115
  python scrape_sehuatang.py --thread 3642661 --clean-only  # 只清理/修复目录, 不抓取

凭证: 115push/creds.json (SmartStrm 115 open token)
记录: scrape_log.db (本机, 防重复刮削)
"""
import json, os, sys, time, argparse, re, sqlite3, asyncio, urllib.request, ssl, shutil
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUSH_DIR = os.path.join(BASE_DIR, '..', '115push') if os.path.basename(BASE_DIR) == '115push' else os.path.join(os.path.dirname(os.path.abspath(__file__)), '115push')
if os.path.isdir(os.path.join(BASE_DIR, '115push')):
    PUSH_DIR = os.path.join(BASE_DIR, '115push')

LOG_DB = os.path.join(BASE_DIR, 'scrape_log.db')
PUSH_LOG_DB = os.path.join(PUSH_DIR, 'push_log.db')
CREDS = os.path.join(PUSH_DIR, 'creds.json')
APP_ID = 100197651
SITE = 'https://sehuatang.net'

DB_CFG = dict(host='127.0.0.1', port=3308, user='root', password='<DB_PASSWORD>',
              database='sehuatang', charset='utf8mb4')

MEDIA_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.m4v', '.ts', '.wmv', '.rmvb', '.rm', '.webm'}
# 元数据文件扩展名: 清理时保留 (不能当垃圾删, 否则 nfo/海报会丢)
KEEP_EXTS = {'.nfo', '.srt', '.ass', '.ssa', '.idx', '.sub'}
# Emby 标准命名图片: 属于元数据保留; 其他图片(如 安卓二维码.png 广告图)删除 (用户规则 2026-08-07)
META_IMGS = {'poster', 'fanart', 'thumb', 'folder', 'backdrop', 'landscape', 'logo', 'banner', 'clearart', 'disc', 'keyart', 'tvshow'}
# 明确垃圾扩展名: 清理时删除
JUNK_EXTS = {'.html', '.htm', '.txt', '.url', '.lnk', '.torrent', '.nfo_junk'}
# 压缩包扩展名: 种子自带的 rar/zip 等 → 清理时整包删除(已有解压后的正片, 用户确认不需要)
ARCHIVE_EXTS = {'.rar', '.zip', '.7z', '.tar', '.gz', '.bz2', '.iso', '.cab'}
# 正片阈值: 只保留 >500MB 的视频, 其余视频一律视为广告/小视频删除 (用户规则 2026-08-07)
KEEP_VIDEO_MIN = 500 * 1024 * 1024
PREFIX_RE = re.compile(r'^[A-Za-z0-9.\-]+\.com@')

# ---------- 本地记录 ----------
def log_db_init():
    c = sqlite3.connect(LOG_DB)
    c.execute('''CREATE TABLE IF NOT EXISTS scrape_log(
        thread_id TEXT PRIMARY KEY, title TEXT, status TEXT, msg TEXT,
        scraped_at TEXT DEFAULT (datetime('now','localtime')))''')
    c.commit(); c.close()

def scrape_done(thread_id):
    c = sqlite3.connect(LOG_DB)
    r = c.execute('SELECT status FROM scrape_log WHERE thread_id=?', (str(thread_id),)).fetchone()
    c.close()
    return r and r[0] == 'ok'

def scrape_record(thread_id, title, status, msg=''):
    c = sqlite3.connect(LOG_DB)
    c.execute('INSERT OR REPLACE INTO scrape_log(thread_id, title, status, msg) VALUES(?,?,?,?)',
              (str(thread_id), (title or '')[:300], status, msg[:300]))
    c.commit(); c.close()

# ---------- 任务收集 ----------
def list_pending_threads():
    c = sqlite3.connect(PUSH_LOG_DB)
    rows = c.execute("SELECT DISTINCT thread_id FROM push_log WHERE status='ok' ORDER BY pushed_at DESC").fetchall()
    c.close()
    pending = []
    for (tid,) in rows:
        if not scrape_done(tid):
            pending.append(str(tid))
    return pending

def get_thread_title(thread_id):
    try:
        import pymysql
        conn = pymysql.connect(**DB_CFG, cursorclass=pymysql.cursors.DictCursor)
        cur = conn.cursor()
        cur.execute('SELECT title FROM threads WHERE thread_id=%s LIMIT 1', (str(thread_id),))
        row = cur.fetchone()
        conn.close()
        return row['title'] if row else ''
    except Exception:
        return ''

# ---------- 115 (挂载文件系统, CD2) ----------
from fs115 import FS115


class Scraper115:
    """基于 CD2 挂载文件系统操作 115（不走 WebDAV / 不依赖 open token）"""

    def __init__(self):
        self.fs = FS115()

    def _fs_path_to_fid(self, path):
        return None  # 挂载 FS 无 fid 概念; 保留签名兼容

    def _walk(self, fid, path, out, maxdepth=5, depth=0):
        return None  # 见 walk()

    def list_all(self, remote_dir):
        """递归列出 remote_dir 下所有项 [(relpath, fn, None, size, is_dir)]"""
        return [(r, n, None, s, d) for r, n, d, s in self.fs.walk(remote_dir)]

    def _find_children(self, remote_dir):
        return self.fs.listdir(remote_dir)

    def fix_and_clean(self, remote_dir, keep_small=False):
        """
        修复+清理 thread 目录, 返回 (正片路径列表[relpath], 正片所在目录[relpath])
        1. 修复: 目录名带媒体扩展名 → rename 去掉 (修复误改文件夹)
        2. 清理: 只保留 >500MB 的视频 (用户规则 2026-08-07); 其余视频(广告/小视频)、
                 非媒体文件(html/txt/rar等)一律删除; 元数据(nfo/图片/字幕)保留
                 keep_small=True (非番号/剧集化, 用户规则 2026-08-12): 小视频也是正片分集,
                 全部保留, 不删 <500MB
        3. 清洗: 正片文件名去域名前缀
        """
        tid = os.path.basename(remote_dir).replace('thread_', '')

        videos = []       # [(relpath, fn, None)]
        media_dir = None  # 正片所在目录 relpath
        rar_dirs = []     # 压缩包目录(如 same-012-C.rar/) → 待删
        rar_files = []    # 压缩包文件 → 待删

        def fix_dir(relpath):
            nonlocal media_dir
            full = f'{remote_dir}/{relpath}' if relpath else remote_dir
            for fn, is_dir, fsize in self._find_children(full):
                child_rel = f'{relpath}/{fn}' if relpath else fn
                child_full = f'{remote_dir}/{child_rel}'
                if is_dir:
                    # 压缩包目录(115 把 rar 文件显示成目录, 如 same-012-C.rar/) → 记录待删, 不递归
                    if os.path.splitext(fn)[1].lower() in ARCHIVE_EXTS:
                        print(f'    [DEL-RAR-DIR?] {child_full} (压缩包目录, 待删)')
                        rar_dirs.append(child_full)
                        continue
                    new_name = None
                    # 修复误改名文件夹: 目录名带媒体扩展名
                    if os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                        new_name = os.path.splitext(fn)[0]
                        dst = f'{remote_dir}/{relpath}/{new_name}' if relpath else f'{remote_dir}/{new_name}'
                        if self.fs.rename(child_full, dst):
                            print(f'    [FIX-DIR] {child_full} -> {new_name}')
                            child_rel = f'{relpath}/{new_name}' if relpath else new_name
                        else:
                            print(f'    [FIX-DIR-FAIL] {child_full}')
                            new_name = None
                        time.sleep(0.8)
                    fix_dir(child_rel)
                else:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in ARCHIVE_EXTS:
                        print(f'    [DEL-RAR?] {child_full} (压缩包文件, 待删)')
                        rar_files.append(child_full)
                        continue
                    if ext in MEDIA_EXTS:
                        # 用户规则(2026-08-07): 只保留 >500MB 的视频, 其余一律删除(广告/小视频)
                        # 用户规则(2026-08-12): keep_small=True 时小视频也当正片保留(非番号/剧集化)
                        if (fsize and fsize >= KEEP_VIDEO_MIN) or keep_small:
                            # 正片: 去域名前缀
                            new_fn = PREFIX_RE.sub('', fn)
                            if new_fn != fn:
                                dst = f'{remote_dir}/{relpath}/{new_fn}' if relpath else f'{remote_dir}/{new_fn}'
                                if self.fs.rename(child_full, dst):
                                    print(f'    [RENAME] {child_full} -> {new_fn}')
                                    child_rel = f'{relpath}/{new_fn}' if relpath else new_fn
                                    child_full = f'{remote_dir}/{child_rel}'
                                else:
                                    print(f'    [RENAME-FAIL] {child_full}')
                                time.sleep(0.8)
                            videos.append((child_rel, os.path.basename(child_rel), None))
                            media_dir = os.path.dirname(child_rel) or ''
                        else:
                            if keep_small:
                                # keep_small: 小视频保留, 也做去域名前缀清洗
                                new_fn = PREFIX_RE.sub('', fn)
                                if new_fn != fn:
                                    dst = f'{remote_dir}/{relpath}/{new_fn}' if relpath else f'{remote_dir}/{new_fn}'
                                    if self.fs.rename(child_full, dst):
                                        print(f'    [RENAME] {child_full} -> {new_fn}')
                                        child_rel = f'{relpath}/{new_fn}' if relpath else new_fn
                                        child_full = f'{remote_dir}/{child_rel}'
                                    else:
                                        print(f'    [RENAME-FAIL] {child_full}')
                                    time.sleep(0.8)
                                print(f'    [KEEP-SMALL] {child_full} ({fsize/1024/1024 if fsize else 0:.1f}MB, keep_small 保留)')
                                videos.append((child_rel, os.path.basename(child_rel), None))
                                media_dir = os.path.dirname(child_rel) or ''
                            else:
                                ok = self.fs.delete(child_full)
                                print(f'    [DEL-SMALL] {child_full} ({fsize/1024/1024 if fsize else 0:.1f}MB < 500MB, 广告/小视频) -> {"OK" if ok else "FAIL"}')
                                time.sleep(0.8)
                    else:
                        # 非媒体文件: 只保留元数据(nfo/字幕/标准命名图片), 其余全部删除 (用户规则: 其他文件全删)
                        # keep_small=True(非番号/剧集化, 用户规则 2026-08-12): 所有图片当元数据保留 —
                        # 压缩包解压出来的包内图片也是元数据的一部分, 不能当广告图删
                        stem, ext = os.path.splitext(fn.lower())
                        is_img = ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif')
                        if ext in KEEP_EXTS or (is_img and (stem in META_IMGS or keep_small)):
                            print(f'    [KEEP] {child_full} (元数据/字幕/图片, 保留)')
                            continue
                        ok = self.fs.delete(child_full)
                        print(f'    [DEL-JUNK] {child_full} (非媒体文件) -> {"OK" if ok else "FAIL"}')
                        time.sleep(0.8)

        fix_dir('')
        # 删除压缩包目录/文件 (仅当 thread 内有正片视频保留, 防止只有 rar 的唯一资源被误删)
        if videos:
            for p in rar_dirs + rar_files:
                ok = self.fs.delete(p)
                print(f'    [DEL-RAR] {p} -> {"OK" if ok else "FAIL"}')
                time.sleep(0.8)
        else:
            for p in rar_dirs + rar_files:
                print(f'    [KEEP-RAR] {p} (thread 内无其他视频, 保守保留)')
        return videos, media_dir

    def upload_file(self, local_path, remote_dir, filename):
        return self.fs.upload(local_path, remote_dir, filename)

# ---------- 抓取/提取 ----------
def extract_thread_meta(html, tid):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    title = ''
    ts = soup.select_one('#thread_subject')
    if ts:
        title = ts.get_text(strip=True)
    if not title:
        h1 = soup.select_one('h1.ts')
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        t = soup.find('title')
        if t:
            title = t.get_text(strip=True).split(' - ')[0]

    post = soup.select_one(f'#postmessage_{tid}') or soup.select_one('.t_f')
    plot = ''
    imgs = []
    if post:
        for tag in post.select('.quote, .blockquote, .attach_nopermission, .jammer, .tip, .gif_wrap_btn'):
            tag.decompose()
        plot = post.get_text('\n', strip=True)[:2000]
        for img in post.find_all('img'):
            cls = ' '.join(img.get('class') or [])
            src = img.get('file') or img.get('data-original') or img.get('src') or ''
            if not src or 'smiley' in cls or 'smilies' in src:
                continue
            if src.startswith('//'):
                src = 'https:' + src
            # 过滤无效/装饰图: 必须是完整 http(s) URL, 排除论坛分隔线/图标等 (2026-08-11)
            if not src.lower().startswith(('http://', 'https://')):
                continue
            low = src.lower()
            if any(k in low for k in ('hrline', '/static/image/', '/common/', 'smil', 'logo', 'icon', 'btn_', 'recommend')):
                continue
            if src not in imgs:
                imgs.append(src)
    return title, plot, imgs

class PageGrabber:
    """Playwright 有头抓取(不过滤图片, 需要下载 poster/fanart)"""
    def __init__(self):
        from playwright.async_api import async_playwright
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._pw_mod = async_playwright

    @staticmethod
    def _find_chromium():
        """服务以 SYSTEM 身份运行时 USERPROFILE 指向 systemprofile, Playwright 默认找不到
        Administrator 的浏览器 → 显式指定可执行文件路径 (SYSTEM 对 Administrator 目录有读权限)"""
        import os
        env = os.environ.get('LOCALAPPDATA', '')
        cands = []
        for ver in ['chromium-1228', 'chromium-1140', 'chromium-1134', 'chromium-1117']:
            for sub in ['chrome-win64', 'chrome-win', 'chrome-windows']:
                if env:
                    cands.append(os.path.join(env, 'ms-playwright', ver, sub, 'chrome.exe'))
                cands.append(rf'C:\Users\Administrator\AppData\Local\ms-playwright\{ver}\{sub}\chrome.exe')
        for c in cands:
            if c and os.path.exists(c):
                return c
        return None

    async def start(self):
        self.pw = await self._pw_mod().__aenter__()
        exe = self._find_chromium()
        kwargs = dict(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        if exe:
            kwargs['executable_path'] = exe
        try:
            self.browser = await self.pw.chromium.launch(**kwargs)
        except Exception as e:
            # SYSTEM 会话可能无法创建可见窗口 → 兜底 headless
            print(f'  [browser] 有头启动失败({str(e)[:120]}), 尝试 headless')
            kwargs['headless'] = True
            self.browser = await self.pw.chromium.launch(**kwargs)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        self.page = await self.context.new_page()

    async def pass_cloudflare(self):
        await self.page.goto(f"{SITE}/", wait_until="domcontentloaded", timeout=60000)
        for i in range(45):
            await asyncio.sleep(1)
            try:
                title = await self.page.title()
                html = await self.page.content()
            except Exception:
                continue
            if "请稍候" in title or "Just a moment" in title:
                continue
            if "满18岁" in html or "enter-btn" in html:
                try:
                    btn = await self.page.query_selector("a.enter-btn")
                    if btn:
                        await btn.click()
                        await asyncio.sleep(3)
                except Exception:
                    pass
                continue
            if len(html) > 15000:
                print(f'[CF] Passed ({i+1}s)')
                return True
        print('[CF] FAILED to pass Cloudflare')
        return False

    async def grab_thread(self, tid):
        url = f"{SITE}/thread-{tid}-1-1.html"
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        for i in range(15):
            await asyncio.sleep(1)
            try:
                html = await self.page.content()
                title = await self.page.title()
            except Exception:
                continue
            if len(html) > 5000 and "请稍候" not in title:
                return html
        return None

    async def download_image(self, url, save_path):
        try:
            resp = await self.context.request.get(url, headers={'Referer': f'{SITE}/'}, timeout=30000)
            if resp.ok:
                data = await resp.body()
                if len(data) > 500:
                    with open(save_path, 'wb') as f:
                        f.write(data)
                    return True
                print(f'    [IMG-FAIL] {url[:70]} -> 数据过小({len(data)}B)')
            else:
                print(f'    [IMG-FAIL] {url[:70]} -> HTTP {resp.status}')
        except Exception as e:
            print(f'    [IMG-FAIL] {url[:70]} -> {str(e)[:80]}')
        return False

    async def stop(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass

# ---------- 生成 nfo ----------
def make_nfo(title, plot, year, poster='poster.jpg', fanart='fanart.jpg', tags=None):
    import xml.sax.saxutils as su
    t = su.escape(title or '')
    p = su.escape(plot or '')
    art = ''
    if poster:
        art += f'    <poster>{su.escape(poster)}</poster>\n'
    if fanart:
        art += f'    <fanart>{su.escape(fanart)}</fanart>\n'
    tag_xml = ''.join(f'  <tag>{su.escape(g)}</tag>\n' for g in (tags or []))
    return f'''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>{t}</title>
  <originaltitle>{t}</originaltitle>
  <sorttitle>{t}</sorttitle>
  <year>{year}</year>
  <overview>{p}</overview>
  <plot>{p}</plot>
  <outline>{p}</outline>
  <genre>sehuatang</genre>
{tag_xml}  <art>
{art}  </art>
</movie>
'''

# ---------- 刮削单个 thread ----------
def _remote_thread(tid):
    """thread 115 目录: 优先剧集媒体库 /sehuatang_tv/, 否则电影目录 /sehuatang/"""
    try:
        fs = FS115()
        tv = f'/sehuatang_tv/thread_{tid}'
        if fs.exists(tv):
            fs.close()
            return tv
        fs.close()
    except Exception:
        pass
    return f'/sehuatang/thread_{tid}'

def run_scrape(thread_ids, dry_run=False, clean_only=False, force=False, keep_small=False, local_only=False):
    s115 = Scraper115()

    if clean_only:
        for tid in thread_ids:
            print(f'\n=== thread_{tid} (仅清理/修复) ===')
            remote = _remote_thread(tid)
            videos, media_dir = s115.fix_and_clean(remote, keep_small=keep_small)
            print(f'  正片: {[v[0] for v in videos]}')
            print(f'  正片所在目录: {media_dir or "(thread 根)"}')
        return

    grabber = PageGrabber()

    async def _main():
        await grabber.start()
        if not await grabber.pass_cloudflare():
            print('无法通过 CF, 终止')
            return
        try:
            for tid in thread_ids:
                tid = str(tid)
                print(f'\n=== thread_{tid} ===')
                remote = _remote_thread(tid)

                # 第一步: 修复/清理 115 目录结构
                videos, media_dir = s115.fix_and_clean(remote, keep_small=keep_small)
                print(f'  正片: {[(v[0]) for v in videos]}')
                print(f'  正片所在目录: {media_dir or "(thread 根)"}')

                if clean_only:
                    continue
                if not dry_run and not force and scrape_done(tid):
                    print(f'  [{tid}] 已刮削过, 跳过抓取')
                    continue

                html = await grabber.grab_thread(tid)
                if not html:
                    print(f'  [{tid}] 抓取失败(CF/超时)')
                    if not dry_run:
                        scrape_record(tid, '', 'fail', 'grab failed')
                    continue
                title, plot, imgs = extract_thread_meta(html, tid)
                print(f'  标题: {title[:80]}')
                print(f'  正文: {len(plot)} 字符 | 图片: {len(imgs)} 张')
                if not title:
                    title = get_thread_title(tid)
                    print(f'  标题(来自库): {title[:80]}')

                # per-thread 临时目录: 保留每个 thread 的 nfo/图片缓存, 供 import_api 4.6 覆盖用
                tmp = os.path.join(BASE_DIR, 'tmp_scrape', f'thread_{tid}')
                os.makedirs(tmp, exist_ok=True)
                poster = fanart = None
                if imgs:
                    p1 = os.path.join(tmp, 'poster.jpg')
                    if await grabber.download_image(imgs[0], p1):
                        poster = p1
                    if len(imgs) > 1:
                        f1 = os.path.join(tmp, 'fanart.jpg')
                        if await grabber.download_image(imgs[1], f1):
                            fanart = f1
                        if fanart is None and poster:
                            fanart = poster
                if poster is None and fanart:
                    # 首图下载失败: 用 fanart 兜底作 poster, 保证 Emby 有 Primary (2026-08-11)
                    poster = fanart
                    print(f'  [{tid}] 首图下载失败, 用 fanart 兜底作 poster')
                if poster is None:
                    print(f'  [{tid}] 无可用图片, 仅上传 nfo')

                year = datetime.now().year
                # 非番号标签: 正片文件名非番号 → nfo 加 <tag>非番号</tag> (用户需求 2026-08-11)
                tags = []
                try:
                    from merge_series import is_fanhao_name
                    if videos and all(not is_fanhao_name(v[1]) for v in videos):
                        tags = ['非番号']
                except Exception:
                    pass
                nfo_path = os.path.join(tmp, 'movie.nfo')
                with open(nfo_path, 'w', encoding='utf-8') as f:
                    f.write(make_nfo(title, plot, year,
                                     poster='poster.jpg' if poster else '',
                                     fanart='fanart.jpg' if fanart else '',
                                     tags=tags))

                if dry_run:
                    print(f'  [DRY] nfo/poster/fanart 已生成于 {tmp}, 未上传 115')
                    continue

                # nfo 命名: 多文件(-1/-2 同一部) → movie.nfo(目录级, Emby 合并); 单文件 → 视频同名
                if videos and len(videos) == 1:
                    base = os.path.splitext(videos[0][1])[0]
                    nfo_name = base + '.nfo'
                else:
                    nfo_name = 'movie.nfo'
                print(f'  nfo: {nfo_name} -> {remote}/{media_dir}/')

                target = f'{remote}/{media_dir}' if media_dir else remote

                # ---- 2026-08-14 方案: 115 只存视频 ----
                # local_only: nfo/图片直写本地 strm 目录(不传 115); tmp per-thread 缓存保留供 4.6 覆盖
                if local_only:
                    if remote.startswith('/sehuatang_tv'):
                        local_path = '/opt/media/strm/tv' + remote[len('/sehuatang_tv'):]
                    else:
                        local_path = '/opt/media/strm/emby' + remote[len('/sehuatang'):]
                    target_local = f'{local_path}/{media_dir}' if media_dir else local_path
                    os.makedirs(target_local, exist_ok=True)
                    ok = True
                    for name, path in [(nfo_name, nfo_path),
                                       ('poster.jpg', poster),
                                       ('fanart.jpg', fanart)]:
                        if not path:
                            continue
                        try:
                            shutil.copy2(path, os.path.join(target_local, name))
                            print(f'  ✅ 写入本地 {name} -> {target_local}/{name}')
                        except Exception as e:
                            print(f'  ❌ 写入本地 {name} 异常: {str(e)[:150]}')
                            ok = False
                    scrape_record(tid, title, 'ok' if ok else 'fail', 'local' if ok else 'local write failed')
                    continue

                ok = True
                for name, path in [(nfo_name, nfo_path),
                                   ('poster.jpg', poster),
                                   ('fanart.jpg', fanart)]:
                    if not path:
                        continue
                    try:
                        if s115.upload_file(path, target, name):
                            print(f'  ✅ 上传 {name} -> {target}/{name}')
                        else:
                            print(f'  ❌ 上传 {name} 失败')
                            ok = False
                    except Exception as e:
                        print(f'  ❌ 上传 {name} 异常: {str(e)[:150]}')
                        ok = False
                    time.sleep(1.5)
                scrape_record(tid, title, 'ok' if ok else 'fail', 'uploaded' if ok else 'upload failed')

        finally:
            await grabber.stop()

    asyncio.run(_main())


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='sehuatang → 115 刮削器 v2')
    ap.add_argument('--thread', help='指定 thread id 刮削')
    ap.add_argument('--pending', action='store_true', help='刮削所有已推送未刮削的')
    ap.add_argument('--list-only', action='store_true', help='只列出待刮削清单')
    ap.add_argument('--dry-run', action='store_true', help='抓取+生成, 不传 115')
    ap.add_argument('--clean-only', action='store_true', help='只清理/修复 115 目录, 不抓取')
    ap.add_argument('--force', action='store_true', help='强制重新刮削(忽略已刮削记录)')
    ap.add_argument('--keep-small', action='store_true', help='保留 <500MB 小视频(非番号/剧集化, 不删小视频)')
    ap.add_argument('--local-only', action='store_true', help='元数据直写本地 strm 目录, 不上传 115 (2026-08-14 方案)')
    ap.add_argument('--limit', type=int, default=10, help='--pending 最多处理数量')
    args = ap.parse_args()

    log_db_init()

    thread_ids = []
    if args.thread:
        thread_ids = [args.thread]
    elif args.pending:
        thread_ids = list_pending_threads()[:args.limit]
        print(f'待刮削(未刮削的已推送): {len(thread_ids)} 个')
    else:
        ap.error('需要 --thread 或 --pending')

    if args.list_only:
        for t in thread_ids:
            print(t, get_thread_title(t)[:60])
        sys.exit(0)

    if not thread_ids:
        print('没有待刮削的 thread')
        sys.exit(0)

    run_scrape(thread_ids, dry_run=args.dry_run, clean_only=args.clean_only, force=args.force, keep_small=args.keep_small, local_only=args.local_only)
