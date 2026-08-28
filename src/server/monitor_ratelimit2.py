#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
115 限频监控 + 失败任务自动断点重续 (常驻版, 2026-08-13)
============================================================
背景: 115 open API 有访问上限 (770004), 曾因多个批量任务并发/每天 0 点全量扫描触发。
本脚本:
  1. 每 60s 检测 SmartStrm 日志新行中的 770004 → 进入/维持"限频窗口"
  2. 连续 RECOVER_CHECKS 次检查无 770004 → 判定恢复:
     - 自动 resume 限频窗口内失败的任务 (POST /api/import/resume, 断点续跑)
     - 恢复后自动验证一次播放链路 (可选)
  3. 周扫兜底 (每周日 05:00): 对 7 天内新增/修改的 thread 目录逐个触发 SmartStrm webhook
     (替代被移除的每日 0 点全量扫定时任务, 避免 742 目录全扫触发限频)
  4. 状态持久化到 STATE_FILE, 日志写 LOG_FILE
"""
import json, os, sys, time, sqlite3, urllib.request, datetime, logging, re, ssl, shutil

SMARTSTRM_LOG = '/opt/media/smartstrm/logs/smartstrm.log'
IMPORT_API = 'http://127.0.0.1:5081'
LOG_DB = '/opt/115push/import_log.db'
STATE_FILE = '/opt/115push/ratelimit_state.json'
LOG_FILE = '/var/log/monitor_ratelimit.log'
CHECK_INTERVAL = 60          # 检查间隔(秒)
RATE_WINDOW = 180            # 检测窗口(秒): 窗口内出现 770004 视为限频中
RECOVER_CHECKS = 3           # 连续 3 次检查无 770004 才算恢复 (~3 分钟)
PROBE_BACKOFF_S = 600        # 探测失败后的冷却(秒): 10 分钟内不重复探测 (原 1800s 会把恢复检测拖太久)
RESUME_LOOKBACK_H = 12       # 恢复后重续最近 12 小时内失败的任务
WEBHOOK = 'http://127.0.0.1:8024/webhook/<WEBHOOK_TOKEN>'
MOUNT_ROOT = '/opt/media/clouddrive2/mnt/115open/115open'
WEEKLY_DOW = 0               # 周扫: 周日
WEEKLY_HOUR = 5
WEEKLY_MTIME_DAYS = 7
PROBE_PATH = '/sehuatang/thread_105957'   # 恢复前探测用的已知存在目录 (幂等, 量小)

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('monitor-ratelimit')

TS_RE = re.compile(r'(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})')

# ===== Emby 删除同步: 115 残留目录事件驱动清理 (2026-08-13) =====
# 不扫描全库(避免触发 115 风控), 只在 SmartStrm 日志出现删除记录时精准处理被删目录:
#   - SmartStrm 删视频单文件后, 若目录已无任何视频 -> 删除整个 115 目录(含 nfo/图片/空子目录)
#   - Emby 删除事件名是 thread_xxx (目录条目, SmartStrm 常匹配失败) 时同样兜底
# 安全护栏: 目录仍有视频则保留; 有活跃入库任务(queued/running)的目录跳过
DEL_SUCCESS_RE = re.compile(r'成功删除远程文件: \[115\] (.+)$')
DEL_EVENT_RE = re.compile(r'收到 Emby 删除事件: 已删除了 Emby 中的 (thread_\d+)')
# 剧集单集删除事件 (2026-08-14): 事件名是 '标题.SxxExx.(ext)' 不含 thread 路径,
# SmartStrm 匹配不到 -> 115 残留; 由 _handle_tv_delete_event 通过剩余 strm 反推路径兜底
TV_DEL_EVENT_RE = re.compile(r'收到 Emby 删除事件: 已删除了 Emby 中的 (.+?\.S\d+E\d+\.\([^)]+\))$')
EMBY_STRM_ROOT = '/opt/media/strm/emby'      # 影片库本地 strm 根
TV_STRM_ROOT = '/opt/media/strm/emby_tv'     # 剧集库本地 strm 根
TV_STRM_ROOT2 = '/opt/media/strm/tv'         # 剧集库 thread 结构 strm 根 (兜底)
MEDIA_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.m4v', '.ts', '.wmv', '.rmvb', '.rm', '.webm'}
_recent_del = {}   # 已删除目录 -> ts (5 分钟去重)
_last_clean = 0    # 本地残留清理节流 (60s 内只跑一次, 纯本地无 API 成本)

def _load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(st):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(st, f)
    except Exception:
        pass

def _tail_new_lines(path, offset):
    """增量读取日志尾部; 返回 (新行列表, 新offset)"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:          # 日志轮转/被清空
        offset = 0
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    new_offset = size
    text = data.decode('utf-8', 'replace')
    return text.splitlines(), new_offset

def _line_ts(line, now):
    m = TS_RE.search(line)
    if not m:
        return None
    mm, dd, hh, mi, ss = map(int, m.groups())
    try:
        ts = datetime.datetime(now.year, mm, dd, hh, mi, ss).timestamp()
    except ValueError:
        return None
    if ts > now.timestamp() + 86400:      # 跨年: 去年
        ts = datetime.datetime(now.year - 1, mm, dd, hh, mi, ss).timestamp()
    return ts

def _has_ratelimit(lines, now, window):
    """窗口内是否有 770004"""
    for ln in lines:
        if '770004' not in ln:
            continue
        ts = _line_ts(ln, now)
        if ts is not None and now.timestamp() - ts <= window:
            return True
    return False

def _post_json(url, payload, timeout=30):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))

def _resume_failed_tasks(since_ts):
    """重续限频窗口内失败的任务 (断点续跑)"""
    since_str = datetime.datetime.fromtimestamp(since_ts).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = sqlite3.connect(LOG_DB)
        rows = conn.execute(
            "SELECT task_id, thread_id, title, msg FROM import_log "
            "WHERE status='failed' AND updated_at >= ? ORDER BY updated_at", (since_str,)).fetchall()
        conn.close()
    except Exception as e:
        log.error('查询失败任务出错: %s', e)
        return 0
    if not rows:
        log.info('限频恢复: 窗口内无失败任务需重续')
        return 0
    n = 0
    for r in rows:
        tid, thread_id, title, msg = r
        try:
            resp = _post_json(IMPORT_API + '/api/import/resume', {'task_id': tid})
            log.info('重续任务 %s (%s %s) → %s', tid, thread_id or '-', (title or '')[:40], resp.get('status', resp))
            n += 1
        except Exception as e:
            log.error('重续任务 %s 失败: %s', tid, str(e)[:150])
        time.sleep(2)
    return n

def _probe_115():
    """恢复前主动探测: 触发一个已知存在的目录, 确认 115 open API 真的可用
    (无任务时 SmartStrm 不产生 770004 日志, 光靠"窗口内无 770004"会假恢复)"""
    try:
        _post_json(WEBHOOK, {'event': 'cs_strm', 'savepath': PROBE_PATH, 'strmtask': 'emby'})
    except Exception as e:
        log.warning('恢复探测: 触发失败 %s', str(e)[:120])
        return False
    time.sleep(6)   # 等 SmartStrm 执行并写日志
    try:
        size = os.path.getsize(SMARTSTRM_LOG)
        with open(SMARTSTRM_LOG, 'rb') as f:
            f.seek(max(0, size - 8192))
            tail = f.read().decode('utf-8', 'replace')
        return '770004' not in tail
    except Exception as e:
        log.warning('恢复探测: 读日志失败 %s', str(e)[:120])
        return False

def _weekly_scan(now, st):
    """每周日 05:00 增量周扫: 对 7 天内新增/修改的 thread 目录触发 cs_strm
    限频窗口内跳过 (2026-08-13): 避免恢复前又批量触发 115 扫描"""
    if now.weekday() != WEEKLY_DOW or now.hour != WEEKLY_HOUR:
        return
    if st.get('limited'):
        log.info('限频窗口内, 跳过增量周扫')
        return
    last = st.get('last_weekly')
    today = now.strftime('%Y-%m-%d')
    if last == today:
        return
    st['last_weekly'] = today
    _save_state(st)
    log.info('开始增量周扫: 7 天内新增/修改的 thread 目录')
    total = 0
    for root_name, task_name in (('sehuatang', 'emby'), ('sehuatang_tv', 'tv')):
        root = os.path.join(MOUNT_ROOT, root_name)
        if not os.path.isdir(root):
            continue
        cutoff = time.time() - WEEKLY_MTIME_DAYS * 86400
        try:
            names = sorted(os.listdir(root))
        except OSError as e:
            log.error('周扫列目录失败 %s: %s', root, e)
            continue
        for name in names:
            p = os.path.join(root, name)
            try:
                if not os.path.isdir(p) or os.path.getmtime(p) < cutoff:
                    continue
            except OSError:
                continue
            savepath = f'/{root_name}/{name}'
            try:
                _post_json(WEBHOOK, {'event': 'cs_strm', 'savepath': savepath, 'strmtask': task_name})
                total += 1
                log.info('周扫触发 %s (%s)', savepath, task_name)
            except Exception as e:
                log.error('周扫触发 %s 失败: %s', savepath, str(e)[:120])
            time.sleep(2)
    log.info('增量周扫完成, 共触发 %d 个目录', total)

def _active_threads():
    """当前有活跃入库任务的 thread 集合 (避免删掉入库中的目录)"""
    try:
        conn = sqlite3.connect(LOG_DB)
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM import_log WHERE status IN ('queued','running') AND thread_id != ''").fetchall()
        conn.close()
        return {str(r[0]) for r in rows}
    except Exception as e:
        log.error('活跃任务查询失败: %s', e)
        return set()


def _fs_ok(path):
    """CD2/FUSE 路径 stat: 返回 (可靠?, 存在?)
    FileNotFoundError -> (True, False)  确定不存在
    OSError (限频/超时) -> (False, None) 不可靠, 调用方保守"""
    try:
        os.stat(path)
        return True, True
    except FileNotFoundError:
        return True, False
    except OSError as e:
        log.warning('[del-sync] CD2 stat 失败(保守跳过): %s %s', path, str(e)[:100])
        return False, None

def _has_video(dirpath, maxdepth=3):
    """目录内是否有视频文件 (只查单个目录树, 不扫全库)"""
    if not os.path.isdir(dirpath):
        return False
    try:
        for root, dirs, files in os.walk(dirpath):
            depth = root[len(dirpath):].count(os.sep)
            if depth >= maxdepth:
                dirs[:] = []
            for f in files:
                if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                    return True
    except OSError as e:
        log.warning('[del-sync] 目录遍历失败(保守视为有视频): %s %s', dirpath, str(e)[:100])
        return True
    return False

def _dir_has_strm(p):
    """递归检查目录树内是否存在 .strm 文件 (strm 可能在二级子目录)"""
    try:
        for _root, _dirs, _files in os.walk(p):
            for f in _files:
                if f.endswith('.strm'):
                    return True
    except OSError:
        return True  # 读取失败时保守保留, 不删
    return False

def _clean_local_orphans():
    """清理本地 strm 残留目录 (纯本地磁盘操作, 无 115 API, 不涉风控):
    - 仅当: ①递归无 .strm 文件 且 ②115 对应目录已不存在 (FUSE 本地路径判断)
    - 影片: /strm/emby/thread_x ; 剧集: /strm/emby_tv/<标题>
    60s 节流: 删除事件低频, 无需每次全扫"""
    if os.environ.get('ENABLE_LOCAL_ORPHAN_CLEAN') != '1':
        return
    global _last_clean
    now = time.time()
    if now - _last_clean < 60:
        return
    _last_clean = now
    for root, mnt_rel in ((EMBY_STRM_ROOT, '/sehuatang'), (TV_STRM_ROOT, '/sehuatang_tv')):
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if not os.path.isdir(p):
                continue
            if _dir_has_strm(p):
                continue
            # 115 对应目录若仍存在(挂载可见), 说明本地 strm 只是缺失, 不删目录(可重建)
            ok, _exists = _fs_ok(MOUNT_ROOT + mnt_rel + '/' + n)
            if not ok:
                log.info('[del-sync] 115 stat 不可靠, 保留(不删): %s', p)
                continue
            if _exists:
                log.info('[del-sync] 本地无 strm 但 115 目录仍存在, 保留(待重建): %s', p)
                continue
            log.info('[del-sync] 删除本地 strm 残留目录: %s', p)
            shutil.rmtree(p, ignore_errors=True)

def _maybe_delete_115_dir(relpath):
    """relpath 如 /sehuatang/thread_x: 目录无视频且非活跃任务 -> 删除整个目录 (含 nfo/图片/空子目录)
    删除成功后级联清理父目录 (thread 下多个子文件夹时, 删完最后一个子目录后清掉 thread 壳)"""
    if relpath in ('/sehuatang', '/sehuatang_tv', '/'):
        return
    p = MOUNT_ROOT + relpath
    ok, exists = _fs_ok(p)
    if not ok:
        log.info('[del-sync] CD2 stat 不可靠, 跳过: %s', relpath)
        return
    if not exists:
        return
    name = os.path.basename(p)
    if name.startswith('thread_'):
        tid = name[len('thread_'):]
        if tid in _active_threads():
            log.info('[del-sync] 跳过(活跃任务): %s', relpath)
            return
    now = time.time()
    if _recent_del.get(p, 0) > now - 300:
        return
    if _has_video(p):
        log.info('[del-sync] 目录仍有视频, 保留: %s', relpath)
        return
    _recent_del[p] = now
    log.info('[del-sync] 删除 115 残留目录: %s', relpath)
    shutil.rmtree(p, ignore_errors=True)
    # 2026-08-13 修复: 不再自动调用 _clean_local_orphans (全量 stat 触发风控 + 限频误判误删)
    # 级联: 父目录若也只剩空壳则继续删 (thread 根目录)
    parent = os.path.dirname(p)
    base = MOUNT_ROOT + '/sehuatang'
    base_tv = MOUNT_ROOT + '/sehuatang_tv'
    if parent not in (base, base_tv, MOUNT_ROOT):
        _maybe_delete_115_dir(os.path.relpath(parent, MOUNT_ROOT).replace('\\', '/'))

def _find_thread_from_strms(dirpath):
    """读目录内任意 .strm 文件内容, 提取 /sehuatang_tv/thread_xxx/ 路径
    strm 内容是 smartstrm_fid URL (含 115 路径; thread 段是 ASCII 不编码, 直接正则取)"""
    try:
        names = os.listdir(dirpath)
    except OSError:
        return None
    for n in names:
        if not n.endswith('.strm'):
            continue
        try:
            with open(os.path.join(dirpath, n), 'r', encoding='utf-8', errors='replace') as f:
                content = f.read(2000)
        except OSError:
            continue
        m = re.search(r'/sehuatang_tv/thread_\d+', content)
        if m:
            return m.group(0)
    return None

def _handle_tv_delete_event(fname):
    """剧集单集删除兜底: 事件名 '标题.SxxExx.(ext)' 不含 thread 路径
    通过同标题目录剩余 strm 内容反推 115 thread 路径 -> 精准删对应 115 文件
    安全护栏: 文件名必须含 .SxxExx 模式; CD2 stat 不可靠时跳过; 5 分钟去重"""
    m = re.search(r'\.S(\d+)E(\d+)\.\(([^)]+)\)$', fname)
    if not m:
        return
    season, ep, ext = m.group(1), m.group(2), m.group(3)
    base = fname[:m.start()]                       # 标题部分 (去掉 .SxxExx.(ext))
    f115 = f'{base}.S{season}E{ep}.{ext}'          # 115 文件名: 标题.S01E02.mp4 (去括号)
    now = time.time()
    # 去重 (同一文件 5 分钟内只处理一次; key 含集数, 同剧不同集互不干扰)
    key = 'tv:' + f115
    if _recent_del.get(key, 0) > now - 300:
        return
    _recent_del[key] = now
    for root in (TV_STRM_ROOT, TV_STRM_ROOT2):
        d = os.path.join(root, base)
        if not os.path.isdir(d):
            continue
        thread_path = _find_thread_from_strms(d)
        if not thread_path:
            log.info('[del-sync][tv] 标题目录 %s 内无剩余 strm, 无法定位 thread, 跳过', d)
            return
        p115 = MOUNT_ROOT + thread_path + '/' + f115
        ok, exists = _fs_ok(p115)
        if not ok:
            log.info('[del-sync][tv] CD2 stat 不可靠, 跳过: %s', p115)
            return
        if not exists:
            log.info('[del-sync][tv] 115 文件已不存在, 无需删: %s', p115)
            return
        if os.environ.get('DEL_SYNC_DRY_RUN') == '1':
            log.info('[del-sync][tv] DRY-RUN 将删除 115 剧集文件: %s', p115)
            return
        log.info('[del-sync][tv] 删除 115 剧集文件: %s', p115)
        try:
            os.remove(p115)
        except Exception as e:
            log.error('[del-sync][tv] 删除失败 %s: %s', p115, e)
        return
    log.info('[del-sync][tv] 未找到标题目录 %s (可能整部剧已删完或标题不匹配)', base)

def _handle_delete_events(lines):
    """解析 SmartStrm 日志删除行 -> 事件驱动清理 115 残留目录 (无全库扫描, 无风控)
    - '成功删除远程文件: [115] <路径>' 路径可能是:
        a) 目录本身: /sehuatang/thread_x   (Emby 删除目录条目)
        b) 子目录:   /sehuatang/thread_x/F071713
        c) 单文件:   /sehuatang/thread_x/FC2PPV-1/FC2PPV-1.mp4
      统一: 路径是目录则检查它, 否则检查文件所在目录
    - '收到 Emby 删除事件: ... thread_xxx' (SmartStrm 匹配失败的目录条目) -> 两个库都兜底
    - '收到 Emby 删除事件: ... 标题.SxxExx.(ext)' (剧集单集) -> _handle_tv_delete_event"""
    for ln in lines:
        m = DEL_SUCCESS_RE.search(ln)
        if m:
            p = m.group(1).strip()
            ok, isdir = _fs_ok(MOUNT_ROOT + p)
            if not ok:
                log.info('[del-sync] CD2 stat 不可靠, 跳过: %s', p)
                continue
            cand = p if isdir else os.path.dirname(p)
            _maybe_delete_115_dir(cand)
            continue
        m = DEL_EVENT_RE.search(ln)
        if m:
            name = m.group(1)
            _maybe_delete_115_dir('/sehuatang/' + name)
            _maybe_delete_115_dir('/sehuatang_tv/' + name)
            continue
        m = TV_DEL_EVENT_RE.search(ln)
        if m:
            _handle_tv_delete_event(m.group(1))
            continue

def main():
    log.info('===== monitor-ratelimit 启动 (interval=%ss, window=%ss, recover=%d) =====',
             CHECK_INTERVAL, RATE_WINDOW, RECOVER_CHECKS)
    st = _load_state()
    offset = 0
    limited = st.get('limited', False)
    since_ts = st.get('since')
    clean = st.get('clean', 0)
    # 探测冷却: 重启后不继承过长的旧冷却 (配置变更/进程重启后应尽快恢复探测),
    # 只保留"不超过一个冷却周期"的冷却时间
    next_probe_ts = st.get('next_probe_ts', 0)
    if next_probe_ts and next_probe_ts - time.time() > PROBE_BACKOFF_S + 60:
        next_probe_ts = 0
        log.info('重置过期的探测冷却(>%ds), 允许尽快探测', PROBE_BACKOFF_S)
    try:
        offset = os.path.getsize(SMARTSTRM_LOG)
    except OSError:
        pass
    while True:
        now = datetime.datetime.now()
        lines, offset = _tail_new_lines(SMARTSTRM_LOG, offset)
        hit = _has_ratelimit(lines, now, RATE_WINDOW)
        if hit:
            if not limited:
                limited = True
                since_ts = now.timestamp()
                log.warning('!!! 检测到 115 限频 (770004), 进入限频窗口, 开始时间 %s',
                            datetime.datetime.fromtimestamp(since_ts).strftime('%Y-%m-%d %H:%M:%S'))
            clean = 0
        else:
            if limited:
                clean += 1
                log.info('限频中, 连续 %d/%d 次检查无 770004', clean, RECOVER_CHECKS)
                if clean >= RECOVER_CHECKS:
                    if time.time() < next_probe_ts:
                        log.info('探测冷却中(上次探测失败后 %ds 内不重复), 继续等待',
                                 int(next_probe_ts - time.time()))
                        clean = 0
                    else:
                        # 主动探测: 确认 115 真的可用再重续, 避免假恢复
                        log.info('=== 疑似恢复, 主动探测 %s ===', PROBE_PATH)
                        if _probe_115():
                            log.info('=== 探测通过, 115 已恢复, 自动断点重续窗口内失败任务 ===')
                            if since_ts:
                                _resume_failed_tasks(since_ts)
                            limited = False
                            since_ts = None
                            clean = 0
                            next_probe_ts = 0
                        else:
                            clean = 0
                            next_probe_ts = time.time() + PROBE_BACKOFF_S
                            log.warning('探测失败: 115 仍在限频, 30 分钟内不再探测, 继续等待')
        st = {'limited': limited, 'since': since_ts, 'clean': clean,
              'last_weekly': st.get('last_weekly'), 'next_probe_ts': next_probe_ts}
        _save_state(st)
        # 删除事件处理: 限频窗口内跳过 (2026-08-13 修复: 批量删除事件风暴会再次触发限频;
        # 残留目录壳延后处理无害, SmartStrm 已删除 115 文件, 不处理只会留空目录)
        if not limited:
            try:
                _handle_delete_events(lines)
            except Exception as e:
                log.error('删除事件处理异常: %s', e)
        try:
            _weekly_scan(now, st)
        except Exception as e:
            log.error('周扫异常: %s', e)
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
