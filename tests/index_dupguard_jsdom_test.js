#!/usr/bin/env node
/* 批量面板「同批 60 秒内重复提交拦截」jsdom 桩测 (2026-09-22)
 *
 * 把 index_page.render_index_page() 真渲染出的 HTML 丢进 jsdom 执行, stub 掉 fetch,
 * 断言: 指纹口径 / 连点两次被拦(且不发请求) / 换一批放行 / 全失败不记指纹 /
 *       60s 窗口过期放行 / 服务端查重结果会被提示。
 *
 * 跑法: cd tests && npm test   (或 node index_dupguard_jsdom_test.js)
 */
const path = require('path');
const { execFileSync } = require('child_process');
const { JSDOM } = require('jsdom');

const SRV = path.join(__dirname, '..', 'src', 'server');
let FAIL = 0, N = 0;

function ck(cond, label, extra) {
  N++;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra === undefined ? '' : '  | ' + extra));
  if (!cond) FAIL++;
}

function renderIndex() {
  const py = [
    'import sys, os',
    'sys.path.insert(0, r"' + SRV + '")',
    'os.chdir(r"' + SRV + '")',
    'import index_page, import_api',
    'sys.stdout.write(index_page.render_index_page(import_api.CATEGORY_MAP))',
  ].join('; ');
  return execFileSync('python3', ['-c', py], { encoding: 'utf8', maxBuffer: 1 << 24 });
}

const HTML = renderIndex();

const MAG_A = 'magnet:?xt=urn:btih:' + 'a1'.repeat(20);
const MAG_B = 'magnet:?xt=urn:btih:' + 'b2'.repeat(20);
const LINKS = MAG_A + '\n' + MAG_B;

function makeDom(opts) {
  opts = opts || {};
  const posts = [];
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:5081/',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = function (url, init) {
        const u = String(url);
        const body = init && init.body ? JSON.parse(init.body) : null;
        if (u.indexOf('/api/import/list') === 0) {
          return Promise.resolve({ json: () => Promise.resolve({ tasks: [], stats: {} }) });
        }
        if (u.indexOf('/api/import/batch') === 0) {
          posts.push(body);
          if (body && body.dry_run) {
            const items = String(body.links).split('\n').filter(l => l.trim()).map((l, i) => ({
              line: i + 1, magnet: l.trim(), kind: body.kind, category: body.category, ok: true, task_id: '',
            }));
            return Promise.resolve({ json: () => Promise.resolve({ dry_run: true, items, errors: [], submitted: 0, failed: 0, skipped: 0, dup_skipped: 0 }) });
          }
          const r = opts.submitResult || { submitted: 2, failed: 0, skipped: 0, dup_skipped: 0, duplicates: [] };
          return Promise.resolve({ json: () => Promise.resolve(Object.assign({
            items: [{ line: 1, magnet: MAG_A, kind: body.kind, category: body.category, ok: true, task_id: 'T1' },
                    { line: 2, magnet: MAG_B, kind: body.kind, category: body.category, ok: true, task_id: 'T2' }],
            errors: [],
          }, r)) });
        }
        if (u.indexOf('/api/import/status') === 0) {
          return Promise.resolve({ json: () => Promise.resolve({ status: 'running', step: 'push', msg: 'x' }) });
        }
        return Promise.resolve({ json: () => Promise.resolve({}) });
      };
      window.navigator.clipboard = { readText: () => Promise.resolve('') };
    },
  });
  return { dom, posts };
}

function setText(w, txt) { w.document.getElementById('links').value = txt; }
function toasts(w) { return Array.from(w.document.querySelectorAll('.toast')).map(x => x.textContent); }

(async () => {
  console.log('=== 1. 指纹口径 ===');
  {
    const { dom } = makeDom();
    const w = dom.window;
    ck(w.eval('typeof submitFingerprint') === 'function', 'submitFingerprint 已注入');
    const f1 = w.eval('submitFingerprint(' + JSON.stringify(MAG_A + '\n' + MAG_B) + ',"fanhao","av")');
    const f2 = w.eval('submitFingerprint(' + JSON.stringify(MAG_B + '\n' + MAG_A) + ',"fanhao","av")');
    ck(f1 === f2, '同批换行顺序 → 同指纹');
    const upper = MAG_A.toUpperCase().replace('MAGNET', 'magnet').replace('XT=URN:BTIH:', 'xt=urn:btih:');
    const f3 = w.eval('submitFingerprint(' + JSON.stringify(upper) + ',"fanhao","av")');
    ck(f3 === w.eval('submitFingerprint(' + JSON.stringify(MAG_A) + ',"fanhao","av")'),
       'infohash 大小写 → 同指纹');
    ck(w.eval('submitFingerprint(' + JSON.stringify(LINKS) + ',"fanhao","av")') !==
       w.eval('submitFingerprint(' + JSON.stringify(LINKS) + ',"non_fanhao","av")'),
       '换类型 → 指纹不同');
    ck(w.eval('submitFingerprint(' + JSON.stringify(LINKS) + ',"fanhao","av")') !==
       w.eval('submitFingerprint(' + JSON.stringify(LINKS) + ',"fanhao","sw")'),
       '换分类 → 指纹不同');
    dom.window.close();
  }

  console.log('=== 2. 连点两次: 第二次被拦且不发请求 ===');
  {
    const { dom, posts } = makeDom();
    const w = dom.window;
    setText(w, LINKS);
    await w.submitBatch();
    const after1 = posts.length;
    await w.submitBatch();
    const txt = toasts(w).join(' | ');
    ck(after1 >= 1, '第一次: 发了 batch 请求', after1);
    ck(posts.length === after1, '第二次: 没有新增 batch 请求 (被浏览器拦住)', posts.length);
    ck(/刚提交过/.test(txt) && /已拦截/.test(txt), '提示文案说明被拦截', txt.slice(0, 120));
    ck(!!w.localStorage.getItem('sehuatang_last_submit'), '指纹已落 localStorage');
    dom.window.close();
  }

  console.log('=== 3. 换一批链接 → 照常放行 ===');
  {
    const { dom, posts } = makeDom();
    const w = dom.window;
    setText(w, LINKS);
    await w.submitBatch();
    const after1 = posts.length;
    setText(w, MAG_A + '\nmagnet:?xt=urn:btih:' + 'c3'.repeat(20));
    await w.submitBatch();
    ck(posts.length > after1, '不同指纹: 正常提交', posts.length);
    dom.window.close();
  }

  console.log('=== 4. 全部失败时不记指纹 (允许立刻重试) ===');
  {
    const { dom, posts } = makeDom({ submitResult: { submitted: 0, failed: 2 } });
    const w = dom.window;
    setText(w, LINKS);
    await w.submitBatch();
    const after1 = posts.length;
    await w.submitBatch();
    ck(posts.length > after1, '0 成功 → 第二次仍可提交 (不误挡重试)', posts.length);
    ck(!w.localStorage.getItem('sehuatang_last_submit'), '0 成功 → 不写指纹');
    dom.window.close();
  }

  console.log('=== 5. 60 秒窗口过期 → 放行 ===');
  {
    const { dom, posts } = makeDom();
    const w = dom.window;
    setText(w, LINKS);
    await w.submitBatch();
    const after1 = posts.length;
    const stale = JSON.parse(w.localStorage.getItem('sehuatang_last_submit'));
    stale.ts = Date.now() - 61000;
    w.localStorage.setItem('sehuatang_last_submit', JSON.stringify(stale));
    w.eval('loadLastSubmit()');
    const left = w.eval('dupGuardLeft(' + JSON.stringify(LINKS) + ',"fanhao","av").left');
    ck(left === 0, '61 秒前提交过 → 剩余拦截时间 0', left);
    await w.submitBatch();
    ck(posts.length > after1, '窗口过期: 可以再提交', posts.length);
    dom.window.close();
  }

  console.log('=== 6. 服务端查重结果会提示到界面 ===');
  {
    const { dom } = makeDom({
      submitResult: {
        submitted: 1, failed: 0, skipped: 0, dup_skipped: 1,
        duplicates: [{ line: 2, raw: MAG_B, error: '同链接已有任务在跑(running/push, abc123), 已跳过', task_id: 'abc123' }],
      },
    });
    const w = dom.window;
    setText(w, LINKS);
    await w.submitBatch();
    const txt = toasts(w).join(' | ');
    ck(/查重跳过 1 条/.test(txt), '汇总 toast 带查重跳过数', txt.slice(0, 140));
    ck(/同链接已有任务在跑/.test(txt), '明细 toast 带原因', txt.slice(0, 200));
    dom.window.close();
  }

  console.log('\n' + (FAIL ? 'FAILED: ' + FAIL + '/' + N : 'ALL PASS') + ' (' + N + ' 项断言, ' + FAIL + ' 失败)');
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
