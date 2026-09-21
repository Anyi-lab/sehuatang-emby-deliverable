#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库存反查 /api/import/lookup 离线桩测 (2026-09-21)

stub 掉 MCP115.search_files，**不碰任何 115 真实数据**，只验证：三态判定 / 缓存与失效 /
115 请求计数 / 限频护栏 / 番号解析回退。

跑法：python3 tests/inventory_lookup_stub_test.py

副作用：会在 import_log 里临时插一行 task_id='__inv_test__'（用于验证入库成功后缓存失效），
测完自己删掉；若中途 Ctrl-C 中断，手动 `DELETE FROM import_log WHERE task_id='__inv_test__'` 即可。
"""
import os, sys, json, sqlite3

# 从本文件位置反推 src/server，别写死绝对路径
SRV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src', 'server')
if not os.path.isdir(SRV):
    SRV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'server')
sys.path.insert(0, SRV)
os.chdir(SRV)

import import_api as IA
import mcp115

FAIL = []

def ck(cond, label, extra=''):
    print(('  PASS ' if cond else '  FAIL ') + label + (('  | ' + str(extra)) if extra else ''))
    if not cond:
        FAIL.append(label)

# ---- 台账取样本 ----
c = sqlite3.connect('file:%s?mode=ro' % IA.LOG_DB, uri=True)
rows = c.execute("SELECT magnet, title, status FROM import_log").fetchall()
c.close()

def pick(status, need_fanhao=True):
    for mag, title, st in rows:
        if st != status or not mag or 'btih:' not in mag:
            continue
        # v1.14.0: 必须挑"title 恰好 1 个番号候选"的行 —— 多候选时 h2f 会退到链接文件名,
        # 与 _inv_extract_fanhao(title) 不一致, 断言会无意义地飘。
        if need_fanhao and len(IA._inv_fanhao_candidates(title)) != 1:
            continue
        return mag, title
    return None, None

done_mag, done_title = pick('done')
del_mag, del_title = pick('deleted')
assert done_mag and del_mag, '台账样本不足'
print('done  样本: %s | %s' % (IA._inv_hash(done_mag)[:12] + '…', (done_title or '')[:50]))
print('deleted 样本: %s | %s' % (IA._inv_hash(del_mag)[:12] + '…', (del_title or '')[:30]))

def magnet(h, dn=None):
    return 'magnet:?xt=urn:btih:' + h + (('&dn=' + dn) if dn else '')

# ---- stub ----
# v1.14.0 起 A(台账番号索引)会抢在 115 搜索之前直答, 所以"要验证搜索路径"的样本番号
# 必须选台账里【没有】的 (QA-90xx), 否则会被 A 0 请求吃掉、根本走不到 stub。
FRESH = ['QA-9001', 'QA-9002', 'QA-9003']
_h2f0, _done0, _gone0, _f2done0 = IA._inv_ledger_map()
_stolen = [f for f in FRESH if IA._fanhao_key(f) in _f2done0]
print('台账已含(不可用作搜索样本): %s' % (_stolen or '无'))

CALLS = []
def fake_search(self, keyword, limit=30, offset=0, file_type=None):
    CALLS.append(keyword)
    if keyword == 'QA-9001':
        return [
            {'file_id': '1', 'file_name': 'QA-9001', 'pick_code': 'pc1'},                       # 目录
            {'file_id': '2', 'file_name': '489155.com@QA-9001.mp4', 'sha1': 'A' * 40,
             'file_size': '7193920002', 'pick_code': 'pc2'},                                    # 视频
            {'file_id': '3', 'file_name': '489155.com@QA-9002.mp4', 'sha1': 'B' * 40,
             'file_size': '1', 'pick_code': 'pc3'},                                             # 干扰项
        ]
    if keyword == 'QA-9002':
        return []                                                                               # 干净 0 条
    if keyword == 'QA-9003':
        return None                                                                             # MCP 掉线
    return []

mcp115.MCP115.search_files = fake_search

LINKS = [
    done_mag,                                       # ① 台账 done → in/ledger, 0 请求
    magnet('a' * 40, 'QA-9001.mp4'),                # ② 搜到 → in (video=1 dir=True count=2)
    magnet('b' * 40, 'QA-9002.mp4'),                # ③ 干净 0 条 → out
    magnet('c' * 40),                               # ④ 无 dn 且台账没有 → unknown/no_fanhao
    magnet('d' * 40, 'QA-9003.mp4'),                # ⑤ MCP 返回 None → unknown/mcp_down
]

print('\n[1] 首次调用 (5 条代表样本)')
r1 = IA.inventory_lookup(LINKS)
items = r1['items']
for i, it in enumerate(items):
    print('   #%d %-14s state=%-8s fanhao=%-11s src=%-7s reason=%s count=%s video=%s dir=%s'
          % (i + 1, (it.get('link_hash') or '-')[:12], it.get('state'), it.get('fanhao') or '-',
             it.get('src') or '-', it.get('reason') or '-', it.get('count'), it.get('video'), it.get('dir')))
print('   req_115=%d  counts=%s' % (r1['req_115'], r1['counts']))

ck(items[0]['state'] == 'in' and items[0]['checked'] == 'ledger', '台账 done → in/ledger')
ck(items[0]['fanhao'] == IA._inv_extract_fanhao(done_title), '台账行能还原番号', items[0].get('fanhao'))
ck(items[1]['state'] == 'in' and items[1]['fanhao'] == 'QA-9001', '磁链 dn → 番号带横线', items[1].get('fanhao'))
ck(items[1]['video'] == 1 and items[1]['dir'] is True and items[1]['count'] == 2,
   '模糊命中被 _fanhao_key 过滤 (QA-9002 不算)', items[1].get('count'))
ck(items[2]['state'] == 'out' and items[2]['count'] == 0, '干净 0 条 → out')
ck(items[3]['state'] == 'unknown' and items[3]['reason'] == 'no_fanhao', '无番号 → unknown/no_fanhao')
ck(items[4]['state'] == 'unknown' and items[4]['reason'] == 'mcp_down', 'MCP 掉线 → unknown/mcp_down (绝不当 out)')
ck(r1['req_115'] == 3, 'req_115 == 3 (台账直答/无番号均不发请求)', r1['req_115'])
ck('QA9001' not in CALLS and 'QA-9001' in CALLS, '搜索词是带横线形式', CALLS)
ck(len(items) == len(LINKS), '出参与入参同序同长')

print('\n[2] deleted 的 hash 不吃台账直答 (台账不可信 → 回落实搜)')
CALLS.clear()
r2 = IA.inventory_lookup([del_mag])
ck(r2['items'][0]['checked'] != 'ledger', 'deleted 行未被当成"在库"', r2['items'][0].get('checked'))
ck(r2['items'][0]['state'] == 'out', '回落实搜后按 115 结果判 (stub 返回 []) → out', r2['items'][0])
ck(len(CALLS) == 1, '确实发了一次 115 请求', CALLS)

print('\n[3] 二次调用 (可缓存项必须 0 请求)')
CALLS.clear()
r3 = IA.inventory_lookup(LINKS[:4])          # 不含 mcp_down 那条
ck(r3['req_115'] == 0 and CALLS == [], 'req_115 == 0, stub 未被再次调用', r3['req_115'])
ck([i['state'] for i in r3['items']] == [i['state'] for i in items[:4]], '三态与首次一致')
ck(r3['items'][1].get('cached') is True, '缓存命中标记 cached=true')

print('\n[4] unknown 不落缓存 (瞬时故障不该被钉住 3 分钟)')
CALLS.clear()
r4 = IA.inventory_lookup([LINKS[4]])
ck(CALLS == ['QA-9003'], 'mcp_down 每次都会重试', CALLS)
ck(r4['items'][0]['state'] == 'unknown', '仍为 unknown')

print('\n[5] 限频窗口内一个请求都不发')
IA.RATELIMIT_STATE_FILE = '/tmp/_inv_fake_ratelimit.json'
with open('/tmp/_inv_fake_ratelimit.json', 'w') as f:
    json.dump({'limited': True}, f)
CALLS.clear()
r5 = IA.inventory_lookup([magnet('e' * 40, 'MIDE-999.mp4')])
ck(r5['req_115'] == 0 and CALLS == [], '限频窗口内不发请求', CALLS)
ck(r5['items'][0]['state'] == 'unknown' and r5['items'][0]['reason'] == 'ratelimit', '限频 → unknown/ratelimit')
os.remove('/tmp/_inv_fake_ratelimit.json')
IA.RATELIMIT_STATE_FILE = '/tmp/115push_ratelimit_state.json'

print('\n[6] 边界: 空 / 非数组 / 超长 / 空白')
ck(IA.inventory_lookup([])['items'] == [], '空数组 → 空 items')
ck('error' in IA.inventory_lookup('not-a-list'), '非数组 → error')
r6 = IA.inventory_lookup(['magnet:?xt=urn:btih:' + ('f' * 40)] * 100)
ck(len(r6['items']) <= IA.INV_MAX_LINKS, '超长被截断到 %d' % IA.INV_MAX_LINKS, len(r6['items']))
ck(IA.inventory_lookup(['   '])['items'] == [], '纯空白 → 空 items')
ck(IA.inventory_lookup([magnet('9' * 40, 'QA-9001.mp4')])['items'][0]['state'] == 'in', '重复番号共享缓存')

print('\n[7] 缓存 TTL 与失效')
ck(IA._inv_cache_get('MFYD-143') is None, '未缓存的 key 返回 None')
IA._INV_CACHE[IA._fanhao_key('MFYD-143')] = (0, {'state': 'out', 'fanhao': 'MFYD-143'})
ck(IA._inv_cache_get('MFYD-143') is None, '过期缓存自动不返回')
IA._INV_CACHE[IA._fanhao_key('MFYD-143')] = (IA.time.time(), {'state': 'out', 'fanhao': 'MFYD-143'})
ck(IA._inv_cache_get('MFYD-143') is not None, '新鲜缓存命中')
IA._inv_cache_put('MFYD-143', {'state': 'in', 'fanhao': 'MFYD-143'})
ck(IA._inv_cache_get('MFYD-143')['state'] == 'in', '写入覆盖生效')
IA.save_task('__inv_test__', title='MFYD-143 测试标题', magnet='magnet:?xt=urn:btih:' + '1' * 40,
             status='in_progress', step='test')
ck(IA._inv_cache_get('MFYD-143')['state'] == 'in', '非 done 状态不清缓存')
IA.save_task('__inv_test__', status='done', step='scan')
ck(IA._inv_cache_get('MFYD-143') is None, 'done 后该番号缓存被失效 (徽章立刻翻牌)')
cc = sqlite3.connect(IA.LOG_DB); cc.execute("DELETE FROM import_log WHERE task_id='__inv_test__'")
cc.execute("DELETE FROM import_log_history WHERE task_id='__inv_test__'"); cc.commit(); cc.close()

print('\n[8] 台账映射覆盖率')
h2f, done, gone, f2done = IA._inv_ledger_map()
ck(len(h2f) > 200, 'hash→番号 映射条数 > 200', len(h2f))
ck(len(done) > 200, '已 done 的 infohash > 200', len(done))
ck(len(gone) >= 1, 'deleted 的 infohash 已识别', sorted(gone)[:2])
ck(not (gone & set()) is None, 'gone 集合可用')
ck(all(v == IA._inv_search_form(v) for v in list(h2f.values())[:50]),
   '映射里番号全是带横线搜索形式', list(h2f.values())[:3])
m = [v for v in h2f.values() if not IA._inv_search_form(v)]
ck(m == [], '无非法形式番号', m[:3])
ck(len(f2done) > 200, 'A: 番号→已入库 索引条数 > 200', len(f2done))
ck(all(k == IA._fanhao_key(k) for k in list(f2done)[:50]), 'A: 键是 _fanhao_key 归一形式',
   list(f2done)[:3])

print('\n[9] 归一化/搜索词边界')
for raw, want in [('SNOS400', 'SNOS-400'), ('SNOS-400', 'SNOS-400'), ('snos-400', 'SNOS-400'),
                  ('VRKM01741', 'VRKM-01741'), ('400', ''), ('ABC-123456', 'ABC-123456'),
                  ('TOOLONG-123', 'TOOLONG-123'), ('', '')]:
    got = IA._inv_search_form(raw)
    ck(got == want, '_inv_search_form(%r) == %r' % (raw, want), got)
ck(IA._fanhao_key('VRKM-01741') == IA._fanhao_key('VRKM01741') == 'VRKM1741', '_fanhao_key 比对语义不变')
ck(IA._inv_hit_matches('489155.com@SNOS-400.mp4', 'SNOS400'), '带杂质文件名能匹配')
ck(not IA._inv_hit_matches('489155.com@SNOS-403.mp4', 'SNOS400'), '相邻番号不误判')
ck(not IA._inv_hit_matches('SNOS-406-U', 'SNOS400'), 'U 后缀变体不误判')
ck(IA._inv_fanhao_from_link('magnet:?xt=urn:btih:' + 'AB12' + 'C' * 36 + '&dn=SNOS-400.mp4') == 'SNOS-400',
   '只扫 dn, 不被 btih 尾巴干扰')
ck(IA._inv_fanhao_from_link('magnet:?xt=urn:btih:' + 'AB1234' + 'C' * 34) == '',
   '无 dn 的磁链不误抠番号', IA._inv_fanhao_from_link('magnet:?xt=urn:btih:' + 'AB1234' + 'C' * 34))

print('\n[10] A 方案: 台账番号索引 → 0 请求直答')
SOME = sorted(f2done)[0]                       # 台账里真实存在的番号(归一键)
SOME_FORM = IA._inv_search_form(SOME) or SOME
NEWH = '7' * 40                                # 台账里必然没有的假 hash
CALLS.clear()
rA = IA.inventory_lookup([magnet(NEWH, SOME_FORM + '.mp4')])
ck(rA['items'][0]['state'] == 'in' and rA['items'][0]['checked'] == 'ledger_fanhao',
   '番号命中台账 → in/ledger_fanhao', rA['items'][0].get('checked'))
ck(rA['items'][0]['src'] == 'magnet', 'src=magnet (番号来自链接)', rA['items'][0].get('src'))
ck(rA['req_115'] == 0 and CALLS == [], '0 请求 (根本没落到 115)', CALLS)

def _dead(*a, **k):
    raise RuntimeError('MCP 死透了')
_saved_search = mcp115.MCP115.search_files
mcp115.MCP115.search_files = _dead
rA2 = IA.inventory_lookup([magnet(NEWH, SOME_FORM + '.mp4')])
ck(rA2['items'][0]['state'] == 'in', 'MCP 死透了照样答 in —— A 的核心价值', rA2['items'][0].get('state'))
mcp115.MCP115.search_files = _saved_search

DEL_KEY = IA._fanhao_key(IA._inv_extract_fanhao(del_title) or 'nope')
ck(DEL_KEY not in f2done, 'deleted 的番号被排除在直答之外 (台账已不可信)', DEL_KEY)
# 上面 [2] 已拿这条 deleted 样本搜过一次 → 结果在缓存里, 会以"0 请求"作答, 掩盖本项断言
IA._INV_CACHE.pop(DEL_KEY, None)
CALLS.clear()
rAd = IA.inventory_lookup([magnet('5' * 40, (IA._inv_extract_fanhao(del_title) or 'X') + '.mp4')])
ck(rAd['items'][0]['state'] == 'out', '删过的番号不吃直答 → 回落实搜', rAd['items'][0].get('state'))
ck(len(CALLS) == 1, '确实发了 115 请求 (既没被直答, 也不是吃旧缓存)', CALLS)

print('\n[11] B 方案: 页面上下文透传 (裸磁链的唯一解)')
CTX1 = SOME_FORM + ' 授業中ですら… - 亚洲有码原创 - 98堂[原色花堂] - Powered by Discuz!'
rB = IA.inventory_lookup([magnet(NEWH)], title=CTX1, contexts=[[CTX1]])
ck(rB['items'][0]['fanhao'] == SOME_FORM, '从页面文本抠出番号', rB['items'][0].get('fanhao'))
ck(rB['items'][0]['src'] == 'ctx', 'src=ctx (番号来自上下文)', rB['items'][0].get('src'))
ck(rB['items'][0]['state'] == 'in' and rB['req_115'] == 0, 'B 抠番号 + A 直答 → 0 请求 (裸磁链的完整解)')

# 页面标题兜底: single 必须显式传 (分片后第二片只剩 1 条, 按长度猜会串味)
rB2 = IA.inventory_lookup([magnet('8' * 40)], title=CTX1, single=True)
ck(rB2['items'][0]['src'] == 'title' and rB2['items'][0]['state'] == 'in', 'single=True → 页面标题兜底生效')
rB3 = IA.inventory_lookup([magnet('8' * 40)], title=CTX1, single=False)
ck(rB3['items'][0]['state'] == 'unknown' and rB3['items'][0]['reason'] == 'no_fanhao',
   'single=False → 不用页面标题')
rB4 = IA.inventory_lookup([magnet('8' * 40)], title=CTX1)          # 老脚本兼容: 按长度猜
ck(rB4['items'][0]['src'] == 'title', 'single 不传 → 退回 len(links)==1 猜 (兼容 v1.13.0)')

# 合集帖防呆: 同一段文本供出 ≥2 条磁链 → 番号作废 (宁可 ⚠️ 不要假 ✅)
SHARED = [magnet('%040x' % (i * 7919 + 13)) for i in range(12)]
CALLS.clear()
rG = IA.inventory_lookup(SHARED, contexts=[[CTX1]] * 12)
ck([i['state'] for i in rG['items']] == ['unknown'] * 12, '12 条共用一段标题 → 全部退回 ⚠️',
   [i['state'] for i in rG['items']][:3])
ck(all(not i.get('fanhao') for i in rG['items']), '番号被清空, 不留一半')
ck(rG['req_115'] == 0 and CALLS == [], '0 请求 (不许拿假番号去搜)')
rG2 = IA.inventory_lookup(SHARED[:2], contexts=[[CTX1 + ' 第一行'], [CTX1 + ' 第二行']])
ck([i['state'] for i in rG2['items']] == ['in', 'in'], '各有各的证据文本 → 不误杀',
   [i['state'] for i in rG2['items']])

# 唯一候选规则: 一段文本里 2 个候选 → 不采信
rN = IA.inventory_lookup([magnet('4' * 40)], contexts=[['第一會所新片@SIS001@HNDS-079']])
ck(rN['items'][0]['state'] == 'unknown' and not rN['items'][0]['fanhao'], '多候选文本 → 不抠番号, 退回 ⚠️')

# 错位防线
rM = IA.inventory_lookup(SHARED[:3], contexts=[[CTX1]] * 2)
ck([i['state'] for i in rM['items']] == ['unknown'] * 3, 'contexts 长度不符 → 整批丢弃')
rS = IA.inventory_lookup(SHARED[:1], contexts=[{'oops': 1}])
ck(rS['items'][0]['state'] == 'unknown', '行不是 list → 整批丢弃')

print('\n[12] _inv_clean_ctxs 形状/截断/总量')
ck(IA._inv_clean_ctxs(None) is None and IA._inv_clean_ctxs([]) is None
   and IA._inv_clean_ctxs('abc') is None, 'None/[]/str → None')
ck(len(IA._inv_clean_ctxs([['x' * 500]])[0][0]) == 200, '单条截断到 200')
ck(IA._inv_clean_ctxs([[('y' * 200)] * 5] * 40) is not None, '40×5×200=40000 放行 (前端实际规模)')
ck(IA._inv_clean_ctxs([[('y' * 200)] * 8] * 40) is None, '40×8×200=64000 > 60000 → None')
ck(IA._inv_clean_ctxs([['a', 1, None, 'b']]) == [['a', 'b']], '非字符串元素被丢掉')

print('\n===== 结果: %s =====' % ('全部通过' if not FAIL else ('失败 %d 项: %s' % (len(FAIL), FAIL))))
sys.exit(1 if FAIL else 0)
