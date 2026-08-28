# -*- coding: utf-8 -*-
"""
fs115.py — 115 文件操作 (本地版: 115-Desktop MCP 后端)
================================================================
替代原 CD2 挂载文件系统实现。路径约定与交付包一致:
    '/sehuatang/thread_x'  <->  115 网盘 /sehuatang/thread_x
接口与交付包 fs115.py 完全兼容: exists/mkdir/listdir/walk/rename/delete/upload/
list_local_strm/delete_local_strm/sync_strm_with_115 + _cache()。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp115 import MCP115, MEDIA_EXTS

ROOT = ''  # MCP 模式无本地挂载根, 路径直接是 115 路径

# 本地 strm 目录根 (MDCng watch 目录: /media/待看/sehuatang <-> G:\srtm\待看\sehuatang)
LOCAL_STRM_ROOT = '/mnt/g/srtm/待看/sehuatang'

# ---- 本地缓存: 本环境无 fs_cache 模块, 安全降级为无缓存 ----
_CACHE = None


def _cache():
    return _CACHE


class FS115:
    def __init__(self, root=ROOT):
        self.root = root
        self.use_cache = True
        self._m = MCP115()

    def close(self):
        pass

    # ---------- 路径 ----------
    def _norm(self, remote):
        return remote

    def exists(self, remote, use_cache=True):
        return self._m.exists(remote)

    def mkdir(self, remote):
        return self._m.mkdir(remote)

    def listdir(self, remote, use_cache=True):
        return self._m.listdir(remote)

    def walk(self, remote, maxdepth=5, use_cache=True):
        return self._m.walk(remote, maxdepth=maxdepth)

    def rename(self, src, dst):
        return self._m.rename(src, dst)

    def delete(self, remote):
        return self._m.delete(remote)

    def upload(self, local_path, remote_dir, fn=None):
        """本地元数据上传 115 — MCP 无上传工具, 本地方案 115 只存视频, 返回 False"""
        return False

    # ---------- 本地 strm 目录同步 ----------
    def _local_strm_dir(self, remote_savepath):
        """'/sehuatang/thread_x' -> LOCAL_STRM_ROOT/thread_x"""
        rel = remote_savepath.strip('/')
        if rel.startswith('sehuatang_tv/'):
            rel = rel[len('sehuatang_tv/'):]
        elif rel.startswith('sehuatang/'):
            rel = rel[len('sehuatang/'):]
        return os.path.join(LOCAL_STRM_ROOT, rel) if rel else LOCAL_STRM_ROOT

    def list_local_strm(self, remote_savepath):
        """列出本地 strm 目录下的 .strm 相对路径"""
        p = self._local_strm_dir(remote_savepath)
        out = []
        if not os.path.isdir(p):
            return out
        for dp, _dns, fns in os.walk(p):
            for n in fns:
                if n.endswith('.strm'):
                    rel = os.path.relpath(os.path.join(dp, n), p).replace(os.sep, '/')
                    out.append(rel)
        return sorted(out)

    def delete_local_strm(self, remote_savepath, rel):
        p = os.path.join(self._local_strm_dir(remote_savepath), rel)
        try:
            if os.path.exists(p):
                os.remove(p)
            return True
        except Exception as e:
            print(f'[fs115.del-local-strm] 失败 {p}: {str(e)[:150]}')
            return False

    def sync_strm_with_115(self, remote_savepath):
        """对照 115 现状, 删除本地 strm 目录中对应文件已不存在的 .strm"""
        rels = self.list_local_strm(remote_savepath)
        removed = []
        for rel in rels:
            base = rel[:-5] if rel.endswith('.strm') else rel  # 去 .strm
            m = re.search(r'\.\((\w+)\)$', base)              # xxx.(mp4) -> .mp4
            if not m:
                continue
            ext = m.group(1)
            rel115 = base[:m.start()] + '.' + ext
            if not self.exists(f'{remote_savepath}/{rel115}'):
                if self.delete_local_strm(remote_savepath, rel):
                    print(f'[sync-strm] 删本地残留: {rel} (115 已不存在 {rel115})')
                    removed.append(rel)
        return removed


if __name__ == '__main__':
    fs = FS115()
    print('exists /sehuatang:', fs.exists('/sehuatang'))
    print('listdir /sehuatang:', [(n, d) for n, d in fs.listdir('/sehuatang')][:10])
    fs.close()
