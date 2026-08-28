# -*- coding: utf-8 -*-
"""
cd2grpc.py — CD2 gRPC 客户端 (本地版: 115-Desktop MCP 后端)
================================================================
替代原 CD2 gRPC 实现。接口与交付包 cd2grpc.py 兼容:
    MOUNT_PREFIX / CD2Client(auth/create_folder/walk_files/add_offline/
    remove_offline/get_offline_status/list_files)
底层全部走 115-Desktop MCP (add_offline_download / get_offline_tasks /
create_folder / list_files)。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp115 import MCP115, _OFFLINE_FINISHED

# 115 在 CD2 中的挂载路径前缀 (兼容旧调用方拼接; MCP 模式仅作路径标记, 实际按 115 根解析)
MOUNT_PREFIX = '/115open'

CD2_ADDR = 'unused-local-mcp'
CD2_USER = ''
CD2_PWD = ''


class CD2Client:
    def __init__(self, address=CD2_ADDR):
        self._m = MCP115()

    def close(self):
        pass

    def auth(self, username=CD2_USER, password=CD2_PWD):
        """MCP 无需认证, noop"""
        return True

    @staticmethod
    def _strip(remote):
        """去掉 '/115open' 前缀 -> 115 路径"""
        r = str(remote or '').strip()
        if r.startswith(MOUNT_PREFIX):
            r = r[len(MOUNT_PREFIX):]
        return r

    def create_folder(self, parent_path, name):
        """parent_path 为 115 路径(/115open 前缀可选); 递归建链, 返回 bool"""
        base = self._strip(parent_path)
        return self._m.mkdir(f'{base.rstrip("/")}/{name}')

    def walk_files(self, path, maxdepth=5, force_refresh=True):
        """返回 [(rel, name, is_dir, size)]"""
        base = self._strip(path)
        return self._m.walk(base, maxdepth=maxdepth)

    def list_files(self, path, maxdepth=5):
        base = self._strip(path)
        return self._m.walk(base, maxdepth=maxdepth)

    def add_offline(self, link, path):
        """path 含 /115open 前缀; 返回 SimpleNamespace(success, errorMessage)"""
        base = self._strip(path)
        ok, msg, _h = self._m.add_offline(link, base)
        return SimpleNamespace(success=ok, errorMessage='' if ok else msg)

    def remove_offline(self, hashes, delete_files=False):
        return self._m.remove_offline(hashes, delete_files=delete_files)

    def get_offline_status(self, info_hash, max_pages=1):
        """返回 (status, task); status=2 表示 FINISHED; 查不到返回 None"""
        return self._m.get_offline_status(info_hash, max_pages=max_pages)


if __name__ == '__main__':
    c = CD2Client()
    print('auth ok')
    print('exists /sehuatang:', c._m.exists('/sehuatang'))
    c.close()
