// v1.14.0 库存反查"页面上下文透传"集成桩测 —— 真实脚本源码 + jsdom。
// 验证: 裸磁链(无 &dn=)时 contexts 是否真的把番号所在文本送给服务端;
//       磁链原文有没有从上下文里剔掉; single 标志; 徽章三态仍按服务端答案渲染。
const fs = require('fs');
const { JSDOM } = require('jsdom');

// 运行 (需要 jsdom):  NODE_PATH=<含 jsdom 的 node_modules> node tests/inventory_context_jsdom_test.js
//   例: cd tests && npm i jsdom  然后  node inventory_context_jsdom_test.js
const SCRIPT = require('path').join(__dirname, '..', 'src', 'userscript', 'sehuatang_import.user.js');
const TID = '8123456';
const bh = (i) => '1'.repeat(38) + i.toString(16).padStart(2, '0');   // 40 位, 条条不同

function pageHtml(kind) {
    const head = '<h1 id="thread_subject">SNOS-403 授業中ですら… - 亚洲有码原创 - 98堂[原色花堂]</h1>';
    const title = '<title>SNOS-403 授業中ですら… - 亚洲有码原创 - 98堂[原色花堂] - Powered by Discuz!</title>';
    if (kind === 'one') {
        // 单链接页: 磁链在 blockcode>ol>li, 番号在同一帖的 t_f 里 (色花堂典型结构, 相隔 4 层)
        return '<!doctype html><html><head>' + title + '</head><body>' + head +
            '<table><tr><td class="t_f" id="postmessage_1"><div>SNOS-403 授業中ですら…</div>' +
            '<div class="blockcode"><ol><li>magnet:?xt=urn:btih:' + bh(0) + '</li></ol></div>' +
            '</td></tr></table></body></html>';
    }
    if (kind === 'shared') {
        // 合集帖: 12 条裸磁链同在一段文本里, 只有页面标题有番号
        const li = Array.from({ length: 12 },
            (_, i) => '<li>magnet:?xt=urn:btih:' + bh(i) + '</li>').join('');
        return '<!doctype html><html><head>' + title + '</head><body>' + head +
            '<table><tr><td class="t_f"><div>合集下载</div><div class="blockcode"><ol>' + li +
            '</ol></div></td></tr></table></body></html>';
    }
    // 'rows': 12 条各自一个帖子, 各带各的番号 (列表页/多帖形态)
    let out = '';
    for (let i = 0; i < 12; i++) {
        out += '<table><tr><td class="t_f" id="postmessage_' + i + '">' +
               '<h3>ABF-' + (380 + i) + ' 作品标题 ' + i + '</h3>' +
               '<div class="blockcode"><ol><li>magnet:?xt=urn:btih:' + bh(i) + '</li></ol></div>' +
               '</td></tr></table>';
    }
    return '<!doctype html><html><head>' + title + '</head><body>' + head + out + '</body></html>';
}

function boot(kind, resp) {
    const dom = new JSDOM(pageHtml(kind), {
        url: 'https://www.sehuatang.net/thread-' + TID + '-1-1.html',
        runScripts: 'outside-only', pretendToBeVisual: true
    });
    const w = dom.window;
    const seen = [];
    const logs = [];                       // v1.14.1: 抓控制台, 断言诊断行
    w.console = {
        log: (...a) => logs.push(a.map(x => (typeof x === 'string' ? x : '') ).join(' ')
                                      + ' ' + a.filter(x => typeof x !== 'string')
                                               .map(x => JSON.stringify(x)).join(' ')),
        warn: (...a) => logs.push('WARN ' + a.map(x => String(x)).join(' ')),
        error: () => {}, info: () => {}, debug: () => {}
    };
    w.GM_xmlhttpRequest = (o) => {
        if (String(o.url).indexOf('/api/import/lookup') >= 0) {
            seen.push(JSON.parse(o.data || '{}'));
            const body = resp ? resp(JSON.parse(o.data || '{}')) : { items: [], req_115: 0, elapsed_ms: 1, counts: {} };
            if (body === null) {                      // resp 显式返回 null → 模拟"查库存失败"
                setTimeout(() => o.onerror({ error: 'stub: 查库存失败' }), 1);
                return;
            }
            setTimeout(() => o.onload({ responseText: JSON.stringify(body) }), 1);
            return;
        }
        setTimeout(() => o.onerror({ error: 'stub: 其它接口不响应' }), 1);
    };
    w.GM_getValue = (k, d) => d;
    w.GM_setValue = () => {};
    w.GM_addStyle = () => {};
    w.eval(fs.readFileSync(SCRIPT, 'utf8'));
    return { dom, w, seen, logs };
}

let pass = 0, fail = 0;
function ok(name, cond, extra) {
    if (cond) { pass++; console.log('OK   ' + name); }
    else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '  -> ' + JSON.stringify(extra) : '')); }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    // ---------- 1. 单链接页: 番号必须从帖子正文送给服务端 ----------
    {
        const r = boot('one', (b) => ({
            items: [{ link: b.links[0], link_hash: bh(0), fanhao: 'SNOS-403', state: 'in',
                      checked: 'ledger_fanhao', src: 'ctx', count: 1, video: 1, dir: false }],
            req_115: 0, elapsed_ms: 3, counts: { in: 1, out: 0, unknown: 0 }
        }));
        await sleep(80);
        const [w, seen] = [r.w, r.seen];
        ok('发出了 /api/import/lookup', seen.length === 1, seen.length);
        const b = seen[0] || {};
        ok('links 1 条', (b.links || []).length === 1);
        ok('single === true', b.single === true, b.single);
        ok('title 带上了', /SNOS-403/.test(b.title || ''), b.title);
        ok('thread_url 带上了', /thread-8123456/.test(b.thread_url || ''), b.thread_url);
        const ctx = (b.contexts || [[]])[0] || [];
        ok('contexts[0] 非空', ctx.length > 0, ctx);
        ok('跨过 4 层 DOM 抠到了番号文本', ctx.some(s => /SNOS-403/.test(s)), ctx);
        ok('含番号的那层被排在第 1 位 (就近优先)', /SNOS-403/.test(ctx[0] || ''), ctx[0]);
        ok('contexts 剔掉了本条磁链原文', !ctx.some(s => /btih/.test(s)), ctx);
        ok('单层不超过 200 字符', ctx.every(s => s.length <= 200), ctx.map(s => s.length));
        ok('contexts 全是字符串', ctx.every(s => typeof s === 'string'));
        const row = w.document.querySelector('#sht-nav .sht-nav-item .sht-nav-inv');
        ok('徽章渲染成 ✅在库', !!row && /在库/.test(row.textContent), row && row.textContent);
        ok('徽章 title 说明来源是台账番号', !!row && /番号/.test(row.title || ''), row && row.title);
        // v1.14.1 诊断: 靠页面文本判定时必须把送出去的文本打出来
        ok('诊断行打出送出的页面文本', r.logs.some(l => /诊断/.test(l) && /SNOS-403/.test(l)),
           r.logs.filter(l => /诊断/.test(l)));
    }

    // ---------- 2. 合集帖: 12 条同一段文本, single=false ----------
    {
        const r = boot('shared');
        await sleep(80);
        const b = r.seen[0] || {};
        ok('12 条一次带走', (b.links || []).length === 12, (b.links || []).length);
        ok('single === false', b.single === false, b.single);
        ok('contexts 与 links 等长', (b.contexts || []).length === (b.links || []).length);
        ok('每条上下文都不含"自己"那条磁链原文',
           (b.contexts || []).every((c, i) => !c.some(s => s.indexOf(b.links[i]) >= 0)));
        ok('没有把页面标题塞进 contexts (多链接页)',
           (b.contexts || []).every(c => !c.some(s => /Powered by Discuz/.test(s))));
        ok('服务端没回条目时不打诊断行 (不刷屏)', !r.logs.some(l => /诊断/.test(l)),
           r.logs.filter(l => /诊断/.test(l)));
    }

    // ---------- 3. 12 条各带各的番号: 每条的最近文本层必须是自己那段 ----------
    {
        const r = boot('rows');
        await sleep(80);
        const b = r.seen[0] || {};
        const ctxs = b.contexts || [];
        ok('12 条上下文', ctxs.length === 12, ctxs.length);
        const fhOf = (c) => (String((c || [])[0] || '').match(/ABF-\d+/) || [''])[0];
        ok('第 1 条抠到自己的番号 ABF-380', fhOf(ctxs[0]) === 'ABF-380', ctxs[0]);
        ok('第 12 条抠到自己的番号 ABF-391', fhOf(ctxs[11]) === 'ABF-391', ctxs[11]);
        const all = ctxs.map(fhOf).filter(Boolean);
        ok('12 条各不相同的证据文本 (服务端防呆不会误杀)', new Set(all).size === 12, all);
    }

    // ---------- 4. 服务端说 unknown → 徽章必须是 ⚠️未校验 (不当成不在库) ----------
    {
        const r = boot('one', (b) => ({
            items: [{ link: b.links[0], link_hash: bh(0), fanhao: '', state: 'unknown',
                      reason: 'no_fanhao', src: '' }],
            req_115: 0, elapsed_ms: 1, counts: { in: 0, out: 0, unknown: 1 }
        }));
        await sleep(80);
        const row = r.w.document.querySelector('#sht-nav .sht-nav-item .sht-nav-inv');
        ok('⚠️未校验 渲染正确', !!row && /未校验/.test(row.textContent), row && row.textContent);
        ok('unknown 提示里带原因', !!row && /番号/.test(row.title || ''), row && row.title);
        ok('一条都没抠出来时也打诊断行 (便于核对 DOM)',
           r.logs.some(l => /诊断/.test(l) && /SNOS-403/.test(l)), r.logs.filter(l => /诊断/.test(l)));
    }

    // ---------- 5. 批量弹窗行内徽章 (v1.15.0): 在弹窗里就能看清哪条已在库 ----------
    {
        const r = boot('rows', (b) => ({
            items: b.links.map((l, i) => (i === 0
                ? { link: l, link_hash: bh(0), fanhao: 'ABF-380', state: 'in', checked: 'ledger',
                    src: 'ledger', count: 1, video: 1, dir: false }
                : (i === 1
                    ? { link: l, link_hash: bh(1), fanhao: 'ABF-381', state: 'out', checked: '115', src: 'ctx' }
                    : { link: l, link_hash: bh(i), fanhao: '', state: 'unknown', reason: 'mcp_down', src: '' }))),
            req_115: 1, elapsed_ms: 9, counts: { in: 1, out: 1, unknown: 10 }
        }));
        await sleep(80);
        const d = r.w.document;
        const navBadge = d.querySelector('#sht-nav .sht-nav-item .sht-nav-inv');
        ok('面板上先有徽章', !!navBadge && /在库/.test(navBadge.textContent), navBadge && navBadge.textContent);

        d.querySelector('#sht-nav-batch').click();
        await sleep(80);
        const rows = d.querySelectorAll('#sht-batch-list .sht-batch-row');
        const badges = d.querySelectorAll('#sht-batch-list .sht-batch-inv');
        ok('弹窗渲染出 12 行', rows.length === 12, rows.length);
        ok('每行都有徽章位', badges.length === 12, badges.length);
        ok('第 1 行 ✅在库', /在库/.test(badges[0].textContent), badges[0].textContent);
        ok('第 2 行 ❓不在库', /不在库/.test(badges[1].textContent), badges[1].textContent);
        ok('第 3 行 ⚠️未校验 (MCP 掉线不当成不在库)', /未校验/.test(badges[2].textContent), badges[2].textContent);
        ok('徽章三态 class 正确',
           badges[0].classList.contains('inv-in') && badges[1].classList.contains('inv-out') &&
           badges[2].classList.contains('inv-unk'),
           [badges[0].className, badges[1].className, badges[2].className]);
        ok('弹窗徽章带 tooltip (与面板同一口径)',
           /在库 —— /.test(badges[0].title || '') && badges[0].title === navBadge.title, badges[0].title);
        ok('打开弹窗不再重复查库存 (缓存命中)', r.seen.length === 1, r.seen.length);
    }

    // ---------- 6. 面板查库存失败 + 首开弹窗 → 弹窗自己补一次 (徽章不能永远空着) ----------
    {
        const r = boot('one', () => null);            // 桩: 查库存直接失败
        await sleep(80);
        r.w.document.querySelector('#sht-nav-batch').click();
        await sleep(80);
        ok('弹窗自己补查了一次', r.seen.length === 2, r.seen.length);
        const badge = r.w.document.querySelector('#sht-batch-list .sht-batch-inv');
        ok('查不到就留空 (不暗示"不在库")', !!badge && badge.textContent === '', badge && badge.textContent);
    }

    // ---------- 7. 「仅未在库」(v1.15.1): 跳过 ✅在库 的行 ----------
    // 旧按钮是「仅未提交」——刚打开面板时 batchTasks 是空的, 点下去等于全选, 等于没用。
    {
        const r = boot('rows', (b) => ({
            items: b.links.map((l, i) => (i === 0
                ? { link: l, link_hash: bh(0), fanhao: 'ABF-380', state: 'in', checked: 'ledger',
                    src: 'ledger', count: 1, video: 1, dir: false }
                : (i === 1
                    ? { link: l, link_hash: bh(1), fanhao: 'ABF-381', state: 'out', checked: '115', src: 'ctx' }
                    : { link: l, link_hash: bh(i), fanhao: '', state: 'unknown', reason: 'mcp_down', src: '' }))),
            req_115: 1, elapsed_ms: 9, counts: { in: 1, out: 1, unknown: 10 }
        }));
        await sleep(80);
        const d = r.w.document;
        d.querySelector('#sht-nav-batch').click();
        await sleep(80);
        const count = () => d.querySelector('#sht-batch-count').textContent;
        ok('计数行写出 ✅在库 几条', /✅在库 1/.test(count()), count());
        ok('没有"查库存中"残留 (结果已回全)', !/查库存中/.test(count()), count());

        const btn = d.querySelector('#sht-batch-list')
            && d.querySelector('.sht-batch-mini[data-act="notin"]');
        ok('按钮已改名「仅未在库」', !!btn && btn.textContent === '仅未在库', btn && btn.textContent);
        ok('旧的「仅未提交」按钮已移除', !d.querySelector('.sht-batch-mini[data-act="unsubmitted"]'));

        btn.click();
        const rows = d.querySelectorAll('#sht-batch-list .sht-batch-row');
        const cks = [...rows].map(x => x.querySelector('.sht-batch-ck').checked);
        ok('✅在库 的第 1 行被取消勾选', cks[0] === false, cks);
        ok('❓不在库 的第 2 行仍勾选', cks[1] === true, cks);
        ok('⚠️未校验 的第 3 行也勾选 (拿不准一律算未在库)', cks[2] === true, cks);
        ok('勾选数 = 12 - 1', /已选 11/.test(count()), count());
        ok('未勾选的行视觉上也变暗', rows[0].classList.contains('off'));
    }

    console.log('\n===== 前端桩测: ' + (fail ? ('失败 ' + fail + ' 项 / 共 ' + (pass + fail)) : ('全部通过 (' + pass + ')')) + ' =====');
    process.exit(fail ? 1 : 0);
})();
