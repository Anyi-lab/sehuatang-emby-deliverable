# -*- coding: utf-8 -*-
"""
mcp115.py — 本地版 115 操作适配层 (115-Desktop MCP 后端)
================================================================
替代交付包原 cd2grpc.py + fs115.py 的 CD2 挂载后端。
所有 115 操作走 115-Desktop MCP (WSL 127.0.0.1:11522 -> Windows 11502):
  - 离线下载:  add_offline_download (磁力/ed2k)
  - 任务查询:  get_offline_tasks
  - 文件操作:  list_files (条目含 pickcode pc) / create_folder / rename_file / move_files / delete_files
路径约定: 与交付包一致, 115 绝对路径如 '/sehuatang/thread_x', 根为 ''。
strm 302 直链: http://192.168.2.238:11500/d/<pickcode>/<url编码文件名> (115-Desktop strm 服务现场签发 CDN 302)
"""
import os
import re
import sys
import json
import time
import threading
import unicodedata
from types import SimpleNamespace

sys.path.insert(0, '/root/clacky_workspace')
from mcp_lib_115 import McpClient, find_entries, is_file_entry, pick, nfc, norm_name

MEDIA_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.m4v', '.ts', '.wmv', '.rmvb', '.rm', '.webm'}
STRM_HOST = 'http://192.168.2.238:11500'   # 115-Desktop strm 服务 (局域网, 302 直链 115 CDN)
PAGE = 200
_OFFLINE_FINISHED = 2

# ---- 全局共享单个 MCP 会话 (115-Desktop MCP 服务端 session 有限, 避免每实例建会话) ----
_LOCK = threading.Lock()
_GLOBAL_CLIENT = None


def _get_client():
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None:
        _GLOBAL_CLIENT = McpClient('import-api')
    return _GLOBAL_CLIENT


class MCP115:
    """115 操作适配 (目录解析/文件操作/离线下载/strm 生成)"""

    def __init__(self):
        self.c = _get_client()
        self._cid = {}  # path -> cid

    # ---------- 路径/目录 ----------
    @staticmethod
    def _segs(path):
        return [s for s in str(path or '').replace('\\', '/').split('/') if s]

    def list_entries(self, cid, limit_pages=20):
        """枚举文件夹全部条目 -> [dict(fid,fn,is_dir,size,pc)] (分页拉全)"""
        out, offset = [], 0
        for _ in range(limit_pages):
            with _LOCK:
                data = self.c.list_files(str(cid), offset, PAGE)
            if data is None:
                break
            ents = find_entries(data)
            if not ents:
                break
            for e in ents:
                fid = pick(e, ('fid', 'file_id', 'id', 'cid'))
                nm = pick(e, ('fn', 'file_name', 'name', 'filename'))
                if not fid or not nm:
                    continue
                is_dir = not is_file_entry(e)
                out.append({'fid': str(fid), 'fn': nfc(str(nm)), 'is_dir': is_dir,
                            'size': int(pick(e, ('fs', 'file_size', 'size', 'size_byte')) or 0),
                            'pc': str(pick(e, ('pc', 'pick_code')) or '')})
            if len(ents) < PAGE:
                break
            offset += PAGE
            time.sleep(0.05)
        return out

    def _find_child(self, cid, name):
        want = norm_name(name)
        for e in self.list_entries(cid):
            if norm_name(e['fn']) == want:
                return e
        return None

    def resolve_cid(self, path):
        """115 路径 -> 文件夹 cid; 不存在返回 None; 根/'' -> '0'"""
        path = (path or '').strip()
        if path in ('', '/'):
            return '0'
        if path in self._cid:
            return self._cid[path]
        cid, cur = '0', ''
        for s in self._segs(path):
            cur = cur + '/' + s
            if cur in self._cid:
                cid = self._cid[cur]
                continue
            e = self._find_child(cid, s)
            if e is None or not e['is_dir']:
                return None
            cid = e['fid']
            self._cid[cur] = cid
        self._cid[path] = cid
        return cid

    def exists(self, path):
        if (path or '').strip() in ('', '/'):
            return True
        segs = self._segs(path)
        cid, cur = '0', ''
        for s in segs:
            cur = cur + '/' + s
            if cur in self._cid:
                cid = self._cid[cur]
                continue
            e = self._find_child(cid, s)
            if e is None:
                return False
            if not e['is_dir']:
                return True
            cid = e['fid']
            self._cid[cur] = cid
        return True

    def mkdir(self, path):
        """递归建目录; 已存在/成功 -> True"""
        segs = self._segs(path)
        if not segs:
            return True
        cid, cur = '0', ''
        for s in segs:
            cur = cur + '/' + s
            if cur in self._cid:
                cid = self._cid[cur]
                continue
            e = self._find_child(cid, s)
            if e is not None:
                if not e['is_dir']:
                    return False
                cid = e['fid']
                self._cid[cur] = cid
                continue
            with _LOCK:
                r = self.c.create_folder(cid, s)
            new_cid = None
            if isinstance(r, dict):
                new_cid = str(pick(r, ('cid', 'file_id', 'fid', 'id')) or '')
            elif isinstance(r, (str, int)):
                new_cid = str(r)
            if not new_cid:
                e2 = self._find_child(cid, s)
                if e2 is None or not e2['is_dir']:
                    return False
                new_cid = e2['fid']
            cid = new_cid
            self._cid[cur] = cid
        return True

    # ---------- 文件操作 ----------
    def listdir(self, path):
        cid = self.resolve_cid(path)
        if cid is None:
            return []
        return [(e['fn'], e['is_dir'], e['size']) for e in self.list_entries(cid)]

    def walk(self, path, maxdepth=5):
        out = []

        def rec(cid, rel, depth):
            if depth > maxdepth:
                return
            for e in self.list_entries(cid):
                r = (rel + '/' + e['fn']) if rel else e['fn']
                out.append([r, e['fn'], e['is_dir'], e['size']])
                if e['is_dir']:
                    rec(e['fid'], r, depth + 1)

        cid = self.resolve_cid(path)
        if cid is not None:
            rec(cid, '', 1)
        return out

    def rename(self, src, dst):
        """同父目录重命名 / 跨目录移动, 返回 bool"""
        try:
            sp = os.path.split(src.rstrip('/'))
            dp = os.path.split(dst.rstrip('/'))
            sp_cid = self.resolve_cid(sp[0] or '/')
            if sp_cid is None:
                return False
            e = self._find_child(sp_cid, sp[1])
            if e is None:
                return False
            ok = False
            if (sp[0] or '/').rstrip('/') == (dp[0] or '/').rstrip('/'):
                with _LOCK:
                    r = self.c.rename_file(e['fid'], dp[1])
                ok = r is not None
            else:
                dp_cid = self.resolve_cid(dp[0] or '/')
                if dp_cid is None:
                    if not self.mkdir(dp[0]):
                        return False
                    dp_cid = self.resolve_cid(dp[0])
                if dp_cid is None:
                    return False
                with _LOCK:
                    r1 = self.c.move_files([e['fid']], dp_cid)
                ok = r1 is not None
                if ok and dp[1] != e['fn']:
                    e2 = self._find_child(dp_cid, e['fn'])
                    if e2 is not None:
                        with _LOCK:
                            self.c.rename_file(e2['fid'], dp[1])
            self._cid.pop(src.rstrip('/'), None)
            return ok
        except Exception:
            return False

    def delete(self, path):
        """删除文件/目录 (按 fid); 已不存在视为成功"""
        parent, name = os.path.split((path or '').rstrip('/'))
        if not name:
            return False
        cid = self.resolve_cid(parent or '/') if parent else '0'
        if cid is None:
            return False
        e = self._find_child(cid, name)
        if e is None:
            return True
        with _LOCK:
            r = self.c.delete_files([e['fid']])
        self._cid.pop(path.rstrip('/'), None)
        return r is not None

    def list_videos(self, savepath, maxdepth=4):
        return [r for r, n, is_dir, _sz in self.walk(savepath, maxdepth)
                if not is_dir and os.path.splitext(n)[1].lower() in MEDIA_EXTS]

    # ---------- 离线下载 ----------
    @staticmethod
    def link_hash(link):
        m = re.search(r'btih:([0-9a-fA-F]{32,40})', link or '')
        if m:
            return m.group(1).lower()
        m = re.search(r'\|([0-9a-fA-F]{32})\|', link or '')  # ed2k://|file|name|size|hash|/
        if m:
            return m.group(1).lower()
        return ''

    def add_offline(self, link, savepath):
        """推磁力/ed2k 到 115 离线下载, 返回 (success, msg, info_hash)"""
        savepath = savepath.strip()
        if not self.mkdir(savepath):
            return False, '建目录失败(MCP)', None
        cid = self.resolve_cid(savepath)
        if cid is None:
            return False, '解析 115 目录失败', None
        with _LOCK:
            r = self.c.call('add_offline_download', {'urls': [link], 'save_dir_id': cid}, retry=3)
        h = self.link_hash(link)
        if r is None:
            return False, 'MCP add_offline_download 无响应(115-Desktop 未运行?)', h
        if isinstance(r, dict):
            st = r.get('state')
            if st is False or (isinstance(st, str) and st.lower() in ('fail', 'failed', 'error', 'false')):
                return False, str(r.get('msg') or r.get('message') or json.dumps(r, ensure_ascii=False))[:200], h
            if r.get('success') is False:
                return False, json.dumps(r, ensure_ascii=False)[:200], h
        return True, 'ok', h

    def get_offline_task(self, info_hash, max_pages=3):
        """按 info_hash 查离线任务 (get_offline_tasks 从 page 0 起, 新任务在前)"""
        ih = str(info_hash or '').lower()
        for page in range(max_pages):
            with _LOCK:
                r = self.c.call('get_offline_tasks', {'page': page}, retry=2)
            if not isinstance(r, dict):
                continue
            for t in r.get('tasks', []) or []:
                if str(t.get('info_hash', '')).lower() == ih:
                    return t
        return None

    def get_offline_status(self, info_hash, max_pages=1):
        """返回 (status, task); 2=FINISHED; 查不到返回 None"""
        t = self.get_offline_task(info_hash, max_pages)
        if t is None:
            return None
        try:
            st = int(t.get('status', 0) or 0)
        except Exception:
            st = 0
        return st, t

    def remove_offline(self, info_hashes, delete_files=False):
        """删除离线任务 (按 info_hash)"""
        ok = True
        for h in info_hashes or []:
            try:
                with _LOCK:
                    r = self.c.call('delete_offline_task', {'info_hash': h}, retry=2)
                if r is None:
                    ok = False
            except Exception:
                ok = False
            time.sleep(0.3)
        return SimpleNamespace(success=ok, errorMessage='')

    # ---------- strm 302 直链生成 ----------
    @staticmethod
    def strm_url(pc, name):
        import urllib.parse
        return f'{STRM_HOST}/d/{pc}/{urllib.parse.quote(name)}'

    def gen_strm(self, savepath, local_root):
        """为 115 目录 savepath 生成 strm 到本地 local_root 对应子目录 (递归子目录)。
        返回生成数量。strm 命名: 视频文件名去扩展名 + '.strm'。"""
        cid = self.resolve_cid(savepath)
        if cid is None:
            return 0
        rel = savepath.strip('/')
        if rel.startswith('sehuatang_tv/'):
            rel = rel[len('sehuatang_tv/'):]
        elif rel.startswith('sehuatang/'):
            rel = rel[len('sehuatang/'):]
        local_dir = os.path.join(local_root, rel) if rel else local_root
        cnt = 0
        for e in self.list_entries(cid):
            if e['is_dir']:
                cnt += self.gen_strm(f'{savepath.rstrip("/")}/{e["fn"]}', local_root)
                continue
            if os.path.splitext(e['fn'])[1].lower() not in MEDIA_EXTS:
                continue
            if not e['pc']:
                continue
            os.makedirs(local_dir, exist_ok=True)
            stem = os.path.splitext(e['fn'])[0]
            sp = os.path.join(local_dir, stem + '.strm')
            url = self.strm_url(e['pc'], e['fn'])
            try:
                with open(sp, 'w', encoding='utf-8') as f:
                    f.write(url)
                cnt += 1
            except Exception as ex:
                print(f'[mcp115] 写 strm 失败 {sp}: {ex}', file=sys.stderr)
        return cnt
