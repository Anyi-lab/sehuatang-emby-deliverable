#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量入库防重 (2026-09-22) 离线桩测

不碰 115、不真建任务: stub 掉 import_api.start_import, 只验证
「同 hash 已有 queued/running 任务 → 这一条被跳过」以及"查重不该误伤"的边界。

跑法：python3 tests/batch_dupguard_test.py

副作用：往 import_log 临时插 4 行 task_id='__dup_test__*', 测完自己删;
      中断时手动 `DELETE FROM import_log WHERE task_id LIKE '__dup_test__%'` 即可。
"""
import os
import sys
import sqlite3
import logging

SRV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src', 'server')
if not os.path.isdir(SRV):
    SRV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'server')
sys.path.insert(0, SRV)
os.chdir(SRV)

import import_api as IA  # noqa: E402

FAIL = []
N = [0]


def ck(cond, label, extra=''):
    N[0] += 1
    print(('  PASS ' if cond else '  FAIL ') + label + (('  | ' + str(extra)) if extra else ''))
    if not cond:
        FAIL.append(label)


HASH_A = 'a1' * 20      # 40 hex
HASH_B = 'b2' * 20
HASH_C = 'c3' * 20
HASH_D = 'd4' * 20
MAG_A = 'magnet:?xt=urn:btih:' + HASH_A
MAG_B = 'magnet:?xt=urn:btih:' + HASH_B
MAG_C = 'magnet:?xt=urn:btih:' + HASH_C
MAG_D = 'magnet:?xt=urn:btih:' + HASH_D

TMP = []


def add_row(task_id, magnet, status, step='push', minutes_ago=0):
    c = sqlite3.connect(IA.LOG_DB)
    off = '-%d minutes' % minutes_ago
    c.execute(
        "INSERT OR REPLACE INTO import_log(task_id, magnet, status, step, msg, kind, category, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,datetime('now','localtime',?),"
        "datetime('now','localtime',?))",
        (task_id, magnet, status, step, 'dup test', 'fanhao', 'av', off, off))
    c.commit()
    c.close()
    TMP.append(task_id)


def cleanup():
    c = sqlite3.connect(IA.LOG_DB)
    for t in TMP:
        c.execute('DELETE FROM import_log WHERE task_id=?', (t,))
    c.commit()
    c.close()


print('=== 1. 解析阶段老行为不变 (重复行仍跳过) ===')
res = IA.batch_import(MAG_A + '\n' + MAG_A, 'fanhao', 'av', dry_run=True)
ck(res.get('skipped') == 1, '同批同链接重复行: skipped=1 (语义不变)', res.get('skipped'))
ck(res.get('dup_skipped') == 0, 'dry_run 预览不查台账 (dup_skipped=0)', res.get('dup_skipped'))
ck(bool(res['items'][0].get('ok')), 'dry_run 首条仍标记就绪')

print('=== 2. 同 hash 有活跃任务 → 跳过 ===')
add_row('__dup_test__a', MAG_A, 'running', 'push')
calls = []
IA.start_import = lambda **kw: (calls.append(kw), 'FAKEID')[1]
res = IA.batch_import(MAG_A + '\n' + MAG_B, 'fanhao', 'av')
ck(res.get('dup_skipped') == 1, '活跃同 hash: dup_skipped=1', res.get('dup_skipped'))
ck(res.get('submitted') == 1, '只有另一条被建任务 (submitted=1)', res.get('submitted'))
ck(res.get('failed') == 0, '查重跳过不计入 failed', res.get('failed'))
ck(len(calls) == 1, 'start_import 只被调 1 次', len(calls))
ck(res['duplicates'][0]['task_id'] == '__dup_test__a',
   'duplicates 指回已有任务 id', res['duplicates'][0]['task_id'])
ck(res['items'][0].get('skip_reason') == 'dup_active', 'items[0].skip_reason=dup_active')
ck('同链接已有任务在跑' in res['items'][0].get('error', ''), 'error 文案可读',
   res['items'][0].get('error'))
ck(res.get('skipped') == 0, 'skipped 只数解析错误, 不被查重污染', res.get('skipped'))

print('=== 3. done/failed 不算活跃 (不该挡重推) ===')
add_row('__dup_test__b', MAG_C, 'done')
res = IA.batch_import(MAG_C, 'fanhao', 'av')
ck(res.get('submitted') == 1, 'done 的同 hash 任务不挡提交', res.get('submitted'))

print('=== 4. 陈旧 running 超窗不挡 (防服务重启遗留脏状态锁死) ===')
add_row('__dup_test__c', MAG_D, 'running', 'wait', minutes_ago=180)
ck(IA._active_link_hashes().get(HASH_D) is None, '3 小时前的 running 不算活跃')
res = IA.batch_import(MAG_D, 'fanhao', 'av')
ck(res.get('submitted') == 1, '超窗 running 不挡提交', res.get('submitted'))

print('=== 5. 同 hash 换分类也挡 (有意的保守行为) ===')
res = IA.batch_import(MAG_A, 'fanhao', 'sw')
ck(res.get('dup_skipped') == 1, '同链接按另一分类提交也被跳过', res.get('dup_skipped'))

print('=== 6. 查询失败时放行 (不误挡) ===')
real = IA._active_link_hashes
IA._active_link_hashes = lambda *a, **k: {}
res = IA.batch_import(MAG_A, 'fanhao', 'av')
ck(res.get('submitted') == 1, '台账查询异常/空 → 照旧建任务', res.get('submitted'))
IA._active_link_hashes = real

print('=== 7. _fs_cache_sync: 本地版无缓存不再刷 warning ===')
logs = []


class _H(logging.Handler):
    def emit(self, rec):
        logs.append(rec.getMessage())


h = _H()
IA.log.addHandler(h)
from fs115 import _cache as _fs_cache  # noqa: E402
ck(_fs_cache() is None, 'fs115._cache() 本环境确实返回 None (刻意的降级占位)')
r1 = IA._fs_cache_sync('/sehuatang/x', tag='push 后')
r2 = IA._fs_cache_sync('/sehuatang/x')
IA.log.removeHandler(h)
ck(r1 is False and r2 is False, '无缓存 → 返回 False (no-op)', (r1, r2))
ck(not [m for m in logs if '缓存同步失败' in m], '不再产生 "缓存同步失败" warning', logs[:3])

print('=== 8. 桩掉的 start_import 参数齐全 ===')
ck(all(k in calls[0] for k in ('magnet', 'kind', 'category')), 'start_import 收到 magnet/kind/category')

cleanup()
print('\n%s (%d 项断言, %d 失败)' % ('ALL PASS' if not FAIL else 'FAILED: ' + ', '.join(FAIL), N[0], len(FAIL)))
sys.exit(1 if FAIL else 0)
