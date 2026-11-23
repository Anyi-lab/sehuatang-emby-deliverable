// ==UserScript==
// @name         AVdb + Jasna-LTX → Emby 一键入库（二合一）
// @namespace    yg.emby.deliverable
// @version      1.8.1
// @updateURL    https://gh-proxy.com/https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-ltx-emby-inject.user.js
// @downloadURL  https://gh-proxy.com/https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-ltx-emby-inject.user.js
// @description  一份脚本管两个站：AVdb(localhost:8200) 卡片/磁力详情页注入「→ Emby」按钮；Jasna-LTX(ltxlib.dpdns.org) 右下角面板按番号一键入库。AVdb/LTX 单条走 /api/import(带 thread_id+title+thread_url 保住帖子上下文)，批量框走 /api/import/batch，共用 import_api(5081) 的「推115 → 落地 → 刮削 → strm → Emby」链路。不依赖任何站方脚本，不需要网页登录 115。
// @author       YG
// @match        http://localhost:8200/*
// @match        http://127.0.0.1:8200/*
// @match        http://192.168.2.238:8200/*
// @match        https://ltxlib.dpdns.org/*
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      localhost
// @connect      127.0.0.1
// @connect      192.168.2.238
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  // ============================================================
  //  运行环境判定
  // ============================================================
  const IS_LTX = /(^|\.)ltxlib\.dpdns\.org$/i.test(location.hostname);
  const MODE = IS_LTX ? 'ltx' : 'avdb';

  // ============================================================
  //  公共配置
  // ============================================================
  // import_api 地址。默认本机 5081；改过端口就执行一次:
  //   localStorage.setItem('emby-import-api', 'http://192.168.2.238:5081')
  const IMPORT_API_DEFAULT = 'http://localhost:5081';

  // 分类表 —— 直接对齐 import_api 的 CATEGORY_MAP(单一来源), 决定 strm 落地/已刮削目录分流
  const CATEGORIES = [
    { key: 'av',   name: 'AV' },
    { key: 'fc2',  name: 'FC2' },
    { key: 'sw',   name: '丝袜' },
    { key: 'cn',   name: '国产自拍' },
    { key: 'ea',   name: '欧美' },
    { key: 'lf',   name: '里番' },
    { key: 'uc',   name: '无码' },
    { key: 'misc', name: '杂项' },
    { key: 'self', name: '自存' },
  ];

  const POLL_MS = 4000;
  const POLL_DEADLINE = 3 * 3600e3;

  function importApiBase() {
    try {
      const v = localStorage.getItem('emby-import-api');
      if (v && /^https?:\/\//.test(v)) return v.replace(/\/+$/, '');
    } catch (e) { /* ignore */ }
    // AVdb 若用局域网 IP 访问, 同 host 推断 5081
    if (!IS_LTX && /^(\d{1,3}\.){3}\d{1,3}$/.test(location.hostname)) {
      return 'http://' + location.hostname + ':5081';
    }
    return IMPORT_API_DEFAULT;
  }

  // ============================================================
  //  公共: 调 import_api
  //  ⚠ 5081 上没有 /api/import 单条接口(404), 只有 /api/import/batch
  //    请求 { links: "多行 ed2k/磁力", kind, category }
  //    响应 { submitted, failed, skipped, dup_skipped, items:[{ok,task_id,magnet,error}] }
  // ============================================================
  function gmJson(method, url, body, timeout) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        data: body ? JSON.stringify(body) : undefined,
        timeout: timeout || 20000,
        onload: (r) => {
          let j = null;
          try { j = JSON.parse(r.responseText); }
          catch (e) { j = { error: String(r.responseText || '').slice(0, 300) }; }
          resolve({ status: r.status, data: j });
        },
        onerror: () => reject(new Error('连不上 import_api (' + url + ')')),
        ontimeout: () => reject(new Error('import_api 请求超时')),
      });
    });
  }

  // ---------- 单条入库: POST /api/import ----------
  // 源码 import_api.py:5908 —— 接受 thread_id / magnet / title / thread_url / kind / category
  // thread_id + title + thread_url 必须带上: 否则落点退化成 manual_<hash8>,
  // 非番号(non_fanhao)也不会走剧集模式(见 import_api.py:5939 注释)。
  async function submitOne(o) {
    const body = {
      thread_id: o.thread_id != null ? String(o.thread_id) : '',
      magnet: o.magnet,
      title: o.title || '',
      thread_url: o.thread_url || '',
      kind: o.kind || 'fanhao',
    };
    if (o.category) body.category = o.category;

    const r = await gmJson('POST', importApiBase() + '/api/import', body);
    if (r.status !== 200) {
      const msg = (r.data && r.data.error) || ('HTTP ' + r.status);
      if (/重复|已存在|已在库|在库|duplicate|exists/i.test(msg)) return { taskId: '', dup: true, raw: r.data };
      throw new Error(msg);
    }
    if (r.data && r.data.task_id) return { taskId: r.data.task_id, dup: false, raw: r.data };
    if (r.data && r.data.error) throw new Error(r.data.error);
    throw new Error('import_api 未返回 task_id');
  }

  // ---------- 批量入库: POST /api/import/batch ----------
  async function submitLinks(links, kind, category, ctx) {
    const body = {
      links: Array.isArray(links) ? links.join('\n') : String(links),
      kind: kind || 'fanhao',
    };
    if (category) body.category = category;
    if (ctx && ctx.thread_id != null) body.thread_id = String(ctx.thread_id);
    if (ctx && ctx.title) body.title = ctx.title;
    if (ctx && ctx.thread_url) body.thread_url = ctx.thread_url;

    const r = await gmJson('POST', importApiBase() + '/api/import/batch', body);
    if (r.status !== 200) throw new Error((r.data && r.data.error) || ('HTTP ' + r.status));
    if (r.data && r.data.error) throw new Error(r.data.error);

    const items = Array.isArray(r.data && r.data.items) ? r.data.items : [];
    const okItem = items.find((x) => x && x.ok && x.task_id);
    if (okItem) return { taskId: okItem.task_id, raw: r.data, dup: false };

    // 先判重复: 服务端查重命中走 dup_skipped / duplicates, 不该报成失败
    const dupSkipped = (r.data && r.data.dup_skipped) || 0;
    const dupList = (r.data && Array.isArray(r.data.duplicates)) ? r.data.duplicates : [];
    if (dupSkipped > 0 || dupList.length) return { taskId: '', raw: r.data, dup: true };

    if (items.length && items.every((x) => x && !x.ok)) {
      const err = items.map((x) => x.error).filter(Boolean)[0]
        || ((r.data && Array.isArray(r.data.errors) && r.data.errors[0] && r.data.errors[0].error) || '')
        || '提交被拒';
      if (/重复|已存在|duplicate|exists/i.test(err)) return { taskId: '', raw: r.data, dup: true };
      throw new Error(err);
    }
    return { taskId: '', raw: r.data, dup: false };
  }

  async function pollStatus(taskId) {
    const deadline = Date.now() + POLL_DEADLINE;
    let nullStreak = 0;
    for (;;) {
      const res = await gmJson('GET', importApiBase() + '/api/import/status?task_id=' + encodeURIComponent(taskId), null, 10000)
        .catch(() => null);
      if (!res || res.status !== 200) {
        if (++nullStreak >= 10) return { done: false, data: { msg: '状态查询无响应 (import_api 不可达)' } };
        await sleep(3000);
        continue;
      }
      nullStreak = 0;
      const d = (res.data && res.data.data) ? res.data.data : (res.data || {});
      const status = String(d.status || '');
      if (/^(done|success|ok|完成|成功)$/i.test(status)) return { done: true, data: d };
      if (/^(fail|failed|error|失败)/i.test(status)) return { done: false, data: d };
      if (Date.now() > deadline) return { done: null, data: d };
      await sleep(POLL_MS);
    }
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // ============================================================
  //  公共: 落地去重(同一番号+链接不重复推)
  // ============================================================
  const SENT_KEY = 'emby-sent';
  const sentSet = (() => {
    try { const a = JSON.parse(localStorage.getItem(SENT_KEY) || '[]'); return new Set(Array.isArray(a) ? a : []); }
    catch (e) { return new Set(); }
  })();
  function hashOf(a, b) {
    let h = 5381;
    const s = String(a || '').toUpperCase() + '|' + String(b || '').slice(0, 200);
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }
  function markSent(h) {
    sentSet.add(h);
    try { localStorage.setItem(SENT_KEY, JSON.stringify([...sentSet].slice(-2000))); } catch (e) { /* ignore */ }
  }

  // ============================================================
  //  公共: 样式
  // ============================================================
  GM_addStyle(`
    /* ---------- AVdb 卡片按钮 ---------- */
    .avdb-emby-btn {
      position:absolute; right:8px; bottom:8px; z-index:60;
      display:inline-flex; align-items:center; gap:4px;
      padding:5px 10px; border-radius:8px; border:none; cursor:pointer;
      font-size:12px; font-weight:600; color:#fff;
      background:linear-gradient(135deg,#10b981,#059669);
      box-shadow:0 2px 6px rgba(0,0,0,.35);
      opacity:.92; transition:opacity .15s, transform .1s;
    }
    .avdb-emby-btn:hover { opacity:1; transform:translateY(-1px); }
    .avdb-emby-btn:disabled { opacity:.5; cursor:wait; }
    .avdb-emby-btn.avdb-emby-ok { background:linear-gradient(135deg,#22c55e,#16a34a); }
    .avdb-emby-btn.avdb-emby-busy { background:linear-gradient(135deg,#f59e0b,#d97706); }
    .avdb-emby-btn.avdb-emby-fail { background:linear-gradient(135deg,#ef4444,#dc2626); }
    .avdb-emby-btn.avdb-emby-inline {
      position:static; margin-left:auto; padding:3px 8px; border-radius:6px;
      font-size:11px; box-shadow:none; opacity:1; flex:0 0 auto;
      align-self:center; white-space:nowrap; line-height:1.35;
    }
    .avdb-emby-btn.avdb-emby-inline:hover { transform:none; filter:brightness(1.1); }
    .avdb-emby-btn.avdb-emby-tail { margin:6px 0 0; display:inline-flex; }

    /* ---------- 公共 toast ---------- */
    .avdb-emby-toast {
      position:fixed; left:50%; bottom:36px; transform:translateX(-50%);
      z-index:2147483001; max-width:560px; padding:10px 16px; border-radius:10px;
      font-size:13px; line-height:1.5; color:#fff; background:rgba(17,24,39,.94);
      box-shadow:0 6px 24px rgba(0,0,0,.45); white-space:pre-wrap; word-break:break-all;
    }
    .avdb-emby-toast.ok { border:1px solid #22c55e; }
    .avdb-emby-toast.err { border:1px solid #ef4444; }
    .avdb-emby-toast.warn { border:1px solid #f59e0b; }

    /* ---------- AVdb 入库弹窗 ---------- */
    .avdb-emby-modal-overlay {
      position:fixed; inset:0; z-index:99998; background:rgba(0,0,0,.55);
      display:flex; align-items:center; justify-content:center;
    }
    .avdb-emby-modal {
      width:440px; max-width:92vw; background:#1f2937; color:#e5e7eb;
      border-radius:14px; padding:20px 22px; box-shadow:0 12px 40px rgba(0,0,0,.6);
      font-size:14px;
    }
    .avdb-emby-modal h3 { margin:0 0 6px; font-size:16px; color:#fff; }
    .avdb-emby-modal .sub { color:#9ca3af; font-size:12px; margin-bottom:14px; word-break:break-all; }
    .avdb-emby-modal .src { color:#6b7280; font-size:11px; margin-bottom:8px; word-break:break-all; }
    .avdb-emby-modal label { display:block; margin:10px 0 4px; color:#d1d5db; font-size:13px; }
    .avdb-emby-modal select, .avdb-emby-modal input[type=text] {
      width:100%; box-sizing:border-box; padding:7px 9px; border-radius:8px;
      border:1px solid #4b5563; background:#111827; color:#e5e7eb; font-size:13px;
    }
    .avdb-emby-modal .hidden { display:none; }
    .avdb-emby-modal .row { display:flex; gap:10px; justify-content:flex-end; margin-top:18px; }
    .avdb-emby-modal button {
      padding:7px 16px; border-radius:8px; border:none; cursor:pointer; font-size:13px; font-weight:600;
    }
    .avdb-emby-modal .cancel { background:#374151; color:#d1d5db; }
    .avdb-emby-modal .go { background:#10b981; color:#fff; }

    /* ---------- LTX 面板 ---------- */
    #ltxe-panel {
      position:fixed; right:16px; bottom:16px; z-index:2147483000;
      width:420px; max-width:94vw; background:#161b22; color:#c9d1d9;
      border:1px solid #30363d; border-radius:12px; overflow:hidden;
      box-shadow:0 10px 32px rgba(0,0,0,.55);
      font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    }
    #ltxe-panel.ltxe-collapsed { width:210px; }
    #ltxe-panel.ltxe-collapsed .ltxe-body { display:none; }
    .ltxe-head {
      display:flex; align-items:center; gap:8px; padding:9px 12px;
      background:#1f2630; border-bottom:1px solid #30363d; cursor:pointer; user-select:none;
    }
    .ltxe-head b { color:#58a6ff; font-size:13px; font-weight:600; }
    .ltxe-head .ltxe-count { color:#8b949e; font-size:11px; margin-left:auto; }
    .ltxe-head .ltxe-caret { color:#8b949e; font-size:11px; }
    .ltxe-body { padding:10px 12px 12px; }
    .ltxe-row { display:flex; gap:6px; margin-bottom:8px; }
    .ltxe-row select, .ltxe-row input {
      flex:1; min-width:0; padding:5px 7px; border-radius:6px;
      border:1px solid #30363d; background:#0d1117; color:#c9d1d9; font-size:12px;
    }
    .ltxe-list { max-height:260px; overflow-y:auto; border:1px solid #21262d; border-radius:8px; }
    .ltxe-item {
      display:flex; align-items:center; gap:6px; padding:5px 7px;
      border-bottom:1px solid #21262d;
    }
    .ltxe-item:last-child { border-bottom:none; }
    .ltxe-item:hover { background:#1b2028; }
    .ltxe-code { color:#e6edf3; font-weight:600; font-size:12px; white-space:nowrap; }
    .ltxe-title { color:#8b949e; font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
    .ltxe-tag { font-size:10px; color:#8b949e; border:1px solid #30363d; border-radius:4px; padding:0 4px; }
    .ltxe-btn {
      padding:3px 8px; border-radius:6px; border:none; cursor:pointer;
      font-size:11px; font-weight:600; color:#fff;
      background:linear-gradient(135deg,#10b981,#059669); white-space:nowrap;
    }
    .ltxe-btn:hover { filter:brightness(1.12); }
    .ltxe-btn:disabled { opacity:.45; cursor:wait; }
    .ltxe-btn.ok  { background:linear-gradient(135deg,#22c55e,#16a34a); }
    .ltxe-btn.err { background:linear-gradient(135deg,#ef4444,#dc2626); }
    .ltxe-btn.busy{ background:linear-gradient(135deg,#f59e0b,#d97706); }
    .ltxe-btn.ghost { background:#30363d; color:#c9d1d9; }
    .ltxe-btn.ghost:hover { background:#3d444d; }
    .ltxe-foot { display:flex; gap:6px; align-items:center; margin-top:8px; }
    .ltxe-empty { padding:14px; text-align:center; color:#6e7681; font-size:12px; }
    .ltxe-stat { margin-top:6px; font-size:10px; color:#4b5563; line-height:1.4; word-break:break-all; white-space:pre-line; }
    /* 卡片内按钮: 跟站点那两个按钮同宽同高 */
    .ltxe-cardbtn {
      display:block; width:100%; margin-top:6px; padding:7px 10px;
      border-radius:8px; border:1px solid rgba(16,185,129,.45);
      background:linear-gradient(135deg,#10b981,#059669); color:#fff;
      font-size:12px; font-weight:600; line-height:1.35; cursor:pointer;
      text-align:center; box-sizing:border-box;
    }
    .ltxe-cardbtn:hover { filter:brightness(1.1); }
    .ltxe-cardbtn:disabled { opacity:.55; cursor:wait; }
    .ltxe-cardbtn.ok  { background:linear-gradient(135deg,#22c55e,#16a34a); border-color:#22c55e; }
    .ltxe-cardbtn.err { background:linear-gradient(135deg,#ef4444,#dc2626); border-color:#ef4444; }
    .ltxe-cardbtn.busy{ background:linear-gradient(135deg,#f59e0b,#d97706); border-color:#f59e0b; }
    .ltxe-ta {
      width:100%; box-sizing:border-box; min-height:56px; resize:vertical; margin-top:8px;
      padding:6px 8px; border-radius:6px; border:1px solid #30363d;
      background:#0d1117; color:#c9d1d9; font:11px/1.5 ui-monospace,Consolas,monospace;
    }
  `);

  function toast(msg, type) {
    const el = document.createElement('div');
    el.className = 'avdb-emby-toast ' + (type || '');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), type === 'err' ? 9000 : 7000);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ============================================================
  //  分类下拉 HTML (两个站共用; AVdb 额外带「自定义…」)
  // ============================================================
  function categoryOptionsHtml(withCustom) {
    let h = CATEGORIES.map((c) =>
      `<option value="${c.key}">${escapeHtml(c.name)}</option>`).join('');
    if (withCustom) h += '<option value="__custom__">自定义…</option>';
    return h;
  }

  // ============================================================
  //  =======================  AVdb 分支  =======================
  // ============================================================
  function initAVDB() {

    const JAVDB_MOVIE_MAX_RANK_CACHE = 1000;
    const articleMap = new Map();   // tid -> article
    const movieMap = new Map();     // movie_id -> movie
    const numberIndex = new Map();  // 番号 -> {source, tid|movie_id}

    // ---------- 拦截页面自身 XHR ----------
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__m = (method || '').toUpperCase();
      this.__u = String(url || '');
      return origOpen.apply(this, [method, url, ...rest]);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      this.addEventListener('load', function () {
        try {
          if (this.__m === 'POST' && /\/articles\/search(\?|$)/.test(this.__u)) {
            const data = JSON.parse(this.responseText);
            const items = (data && data.data && data.data.items) || [];
            items.forEach((it) => {
              const tid = it && it.tid;
              if (tid != null) {
                articleMap.set(String(tid), it);
                if (it.number) numberIndex.set(String(it.number).trim().toUpperCase(), { source: 'article', tid: String(tid) });
              }
            });
            if (items.length) console.log('[Emby] cached', items.length, 'articles, total:', articleMap.size);
          } else if (this.__m === 'GET' && /\/javdb\//.test(this.__u)) {
            const data = JSON.parse(this.responseText);
            let movies = null;
            if (data && data.data && Array.isArray(data.data.movies)) movies = data.data.movies;
            else if (data && data.data && Array.isArray(data.data.items)) movies = data.data.items;
            else if (Array.isArray(data && data.data)) movies = data.data;
            if (movies && movies.length) {
              movies.forEach((mv) => {
                if (!mv || !mv.id) return;
                movieMap.set(String(mv.id), mv);
                const num = String(mv.number || mv.code || '').trim().toUpperCase();
                if (num) numberIndex.set(num, { source: 'movie', movie_id: String(mv.id) });
              });
              if (movieMap.size > JAVDB_MOVIE_MAX_RANK_CACHE) {
                const keys = [...movieMap.keys()].slice(0, movieMap.size - JAVDB_MOVIE_MAX_RANK_CACHE);
                keys.forEach((k) => {
                  const mv = movieMap.get(k);
                  if (mv && mv.number) numberIndex.delete(String(mv.number).trim().toUpperCase());
                  movieMap.delete(k);
                });
              }
              console.log('[Emby] cached', movies.length, 'javdb movies, total:', movieMap.size);
            }
          }
        } catch (e) { /* ignore */ }
      });
      return origSend.apply(this, args);
    };

    // ---------- 工具 ----------
    function tidFromUrl(src) {
      const m = (src || '').match(/\/articles\/(\d+)\/image/);
      return m ? m[1] : null;
    }
    function getJwtToken() {
      const m = document.cookie.match(/(?:^|;\s*)jwt_token=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    }
    function authFetch(url, opts) {
      const headers = Object.assign({}, (opts && opts.headers) || {});
      const tok = getJwtToken();
      if (tok) headers['Authorization'] = 'Bearer ' + tok;
      if (opts && opts.body) headers['Content-Type'] = 'application/json';
      return fetch(url, Object.assign({ credentials: 'include' }, opts, { headers }));
    }

    // ---------- 番号解析 ----------
    const NUMBER_RE = /^([A-Za-z0-9]{1,12})-(\d{2,8})$/;

    function collectNumbers(card) {
      const list = [];
      const push = (raw, tier) => {
        const t = String(raw || '').trim();
        if (!t || t.length > 24) return;
        if (!NUMBER_RE.test(t)) return;
        const i = list.findIndex((x) => x.n === t);
        if (i === -1) list.push({ n: t, tier });
        else if (tier < list[i].tier) list[i].tier = tier;
      };
      try {
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT, null);
        let n;
        while ((n = walker.nextNode())) push(n.nodeValue, 1);
      } catch (e) { /* ignore */ }
      card.querySelectorAll('span, div, p, a, b, strong, em').forEach((el) => {
        if (el.children && el.children.length) return;
        push(el.textContent, 2);
      });
      const loose = /\b([A-Za-z0-9]{1,12}-\d{2,8})\b/g;
      const text = String(card.textContent || '');
      let m;
      while ((m = loose.exec(text))) push(m[1], 3);
      return list;
    }

    function numberVariants(num) {
      const k = String(num || '').trim().toUpperCase();
      const out = [];
      if (!k) return out;
      out.push(k);
      const m = k.match(NUMBER_RE);
      if (m) {
        const head = m[1];
        const maxStrip = Math.min(4, head.length - 3);
        for (let i = 1; i <= maxStrip; i++) out.push(head.slice(i) + '-' + m[2]);
      }
      return out;
    }

    function normText(s) {
      return String(s || '').replace(/[\s\u3000….,。、（）()【】\[\]!?！？:：;；'"’”\-_/|~～·]+/g, '').toLowerCase();
    }
    function titleLooksSame(cardText, movieTitle) {
      const a = normText(cardText);
      const b = normText(movieTitle);
      if (!a || !b) return false;
      const n = 8;
      if (b.length <= n) return a.includes(b);
      for (let i = 0; i + n <= b.length; i += 2) {
        if (a.includes(b.slice(i, i + n))) return true;
      }
      return false;
    }

    function numberFromCard(card) {
      const list = collectNumbers(card);
      if (!list.length) return null;
      const sorted = list.slice().sort((a, b) => {
        if (a.tier !== b.tier) return a.tier - b.tier;
        const pa = (a.n.match(NUMBER_RE) || [])[1] || '';
        const pb = (b.n.match(NUMBER_RE) || [])[1] || '';
        return pa.length - pb.length || a.n.length - b.n.length;
      });
      for (const c of sorted) {
        for (const v of numberVariants(c.n)) {
          if (numberIndex.has(v)) return v;
        }
      }
      return sorted[0].n;
    }

    function findCachedByNumber(num) {
      for (const v of numberVariants(num)) {
        const hit = numberIndex.get(v);
        if (hit) return hit;
      }
      return null;
    }

    function tidFromCard(card) {
      const imgs = card.querySelectorAll('img');
      for (const im of imgs) {
        const cands = [im.currentSrc, im.getAttribute('src'), im.getAttribute('data-src'), im.getAttribute('srcset')];
        for (const c of cands) {
          const t = tidFromUrl(String(c || ''));
          if (t) return t;
        }
      }
      const m = String(card.innerHTML || '').match(/\/articles\/(\d+)\/(?:image|images|javdb-gallery)/);
      return m ? m[1] : null;
    }

    function movieIdFromCard(card) {
      const links = card.querySelectorAll('a[href]');
      for (const a of links) {
        const href = a.getAttribute('href') || '';
        const m = href.match(/javdb\.com\/v\/([A-Za-z0-9]+)/);
        if (m) return m[1];
        try {
          const u = new URL(href, location.origin);
          const v = u.searchParams.get('movie');
          if (v) return v;
        } catch (e) { /* ignore */ }
      }
      return null;
    }

    async function resolveMovieByNumber(num, cardText) {
      const exact = String(num || '').trim().toUpperCase();
      let fallback = null;
      const numOf = (m) => String((m && m.number) || '').trim().toUpperCase();
      const titleOk = (m) => !cardText || titleLooksSame(cardText, (m && (m.title || m.origin_title)) || '');
      for (const q of numberVariants(num)) {
        let movies = [];
        try {
          const res = await authFetch('/api/v1/javdb/search?type=movie&limit=10&q=' + encodeURIComponent(q));
          const d = await res.json();
          movies = (d && d.data && d.data.movies) || [];
        } catch (e) { continue; }
        if (!movies.length) continue;
        const eqHit = movies.find((m) => numOf(m) === q);
        if (eqHit) {
          if (q === exact || titleOk(eqHit)) return eqHit;
          if (!fallback) fallback = eqHit;
          continue;
        }
        if (movies.length === 1) {
          if (titleOk(movies[0])) return movies[0];
          if (!fallback) fallback = movies[0];
        }
      }
      return fallback;
    }

    async function resolveMovieById(movieId) {
      try {
        const res = await authFetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId));
        const d = await res.json();
        const mv = (d && d.data && d.data.movie) || null;
        if (mv) {
          movieMap.set(String(mv.id), mv);
          const k = String(mv.number || '').trim().toUpperCase();
          if (k) numberIndex.set(k, { source: 'movie', movie_id: String(mv.id) });
        }
        return mv;
      } catch (e) { return null; }
    }

    // ---------- 磁力解析 ----------
    async function resolveMagnet(article, movie) {
      if (article && article.magnet) return { magnet: article.magnet, source: 'article' };

      const num = (movie && (movie.number || movie.code)) || (article && article.number) || '';
      if (num) {
        const hit = findCachedByNumber(num);
        if (hit && hit.source === 'article') {
          const art = articleMap.get(hit.tid);
          if (art && art.magnet) return { magnet: art.magnet, source: 'article' };
        }
      }

      if (movie && movie.id) {
        try {
          const res = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movie.id) + '/magnets', { credentials: 'include' });
          const d = await res.json();
          const magnets = (d && d.data && d.data.magnets) || (d && d.data && d.data.items) || [];
          const url = (x) => String((x && (x.magnet_url || x.magnet || x.link || x.url)) || '');
          const isMag = (x) => /^(magnet:|ed2k:)/i.test(url(x));
          const key = String(num || '').trim().toUpperCase();
          const named = key ? magnets.find((x) => isMag(x) && String(x.name || '').toUpperCase().indexOf(key) >= 0) : null;
          const m = named || magnets.find((x) => isMag(x) && /^magnet:/i.test(url(x))) || magnets.find(isMag);
          const mlink = url(m);
          if (mlink) return { magnet: mlink, source: 'javdb' };
        } catch (e) { /* fallthrough */ }
      }
      return null;
    }

    // ---------- 弹窗 ----------
    function askKind(payload) {
      return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'avdb-emby-modal-overlay';
        const autoKind = /^[a-z0-9]{1,12}-\d{2,8}/i.test(String(payload.number || '').trim()) ? 'fanhao' : 'non_fanhao';
        overlay.innerHTML = `
          <div class="avdb-emby-modal">
            <h3>推送到 Emby 入库</h3>
            <div class="src">数据来源: ${escapeHtml(payload.source || '')}</div>
            <div class="sub">${escapeHtml(String(payload.title || ''))}<br/>${payload.number ? '番号 ' + escapeHtml(payload.number) + ' · ' : ''}${payload.thread_id ? 'thread_id=' + escapeHtml(payload.thread_id) + ' · ' : ''}${payload.magnet ? '磁力已就绪' : '磁力获取中'}</div>
            <label>类型 (import_api 必填)</label>
            <select id="avdb-emby-kind">
              <option value="fanhao" ${autoKind === 'fanhao' ? 'selected' : ''}>fanhao (影片 / 番号)</option>
              <option value="non_fanhao" ${autoKind === 'non_fanhao' ? 'selected' : ''}>non_fanhao (剧集 / 非番号)</option>
            </select>
            <label>分类 (决定落地到已刮削哪个目录)</label>
            <select id="avdb-emby-cat">${categoryOptionsHtml(true)}</select>
            <input type="text" id="avdb-emby-cat-custom" class="hidden" placeholder="输入自定义分类 key, 如: 剧情 / fc2 / 无码" />
            <div class="row">
              <button class="cancel" id="avdb-emby-cancel">取消</button>
              <button class="go" id="avdb-emby-go">开始入库</button>
            </div>
          </div>`;
        document.body.appendChild(overlay);
        const close = (v) => { overlay.remove(); resolve(v); };
        overlay.querySelector('#avdb-emby-cancel').addEventListener('click', () => close(null));
        const catSel = overlay.querySelector('#avdb-emby-cat');
        const catCustom = overlay.querySelector('#avdb-emby-cat-custom');
        catSel.addEventListener('change', () => {
          catCustom.classList.toggle('hidden', catSel.value !== '__custom__');
          if (catSel.value !== '__custom__') catCustom.value = '';
        });
        overlay.querySelector('#avdb-emby-go').addEventListener('click', () => {
          let catVal = catSel.value;
          if (catVal === '__custom__') catVal = catCustom.value.trim();
          close({
            kind: overlay.querySelector('#avdb-emby-kind').value,
            category: catVal || undefined,
          });
        });
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
      });
    }

    // ---------- 按钮挂载 ----------
    function badgeRowOf(card) {
      const cands = [];
      const divs = card.querySelectorAll('div');
      for (const d of divs) {
        const c = String(d.className || '');
        if (!/(^|\s|\b)flex(\b|\s|$)/.test(c)) continue;
        if (!/(^|\s)text-xs(\s|$)/.test(c)) continue;
        if (d.querySelector('img') || d.querySelector('[data-image-frame]')) continue;
        if (d.querySelector('.avdb-emby-btn')) continue;
        const t = (d.textContent || '').trim();
        if (!t || t.length > 80) continue;
        cands.push(d);
      }
      cands.sort((a, b) => b.querySelectorAll('div').length - a.querySelectorAll('div').length);
      return cands[0] || null;
    }
    function mountBtn(card, frame, btn) {
      const row = badgeRowOf(card);
      if (row) {
        btn.classList.add('avdb-emby-inline');
        row.appendChild(btn);
        return;
      }
      btn.classList.add('avdb-emby-inline', 'avdb-emby-tail');
      (card !== frame ? card : (frame.parentElement || frame)).appendChild(btn);
    }

    // ---------- 卡片注入 ----------
    let injecting = false;
    function injectButtons(root) {
      if (injecting) return;
      injecting = true;
      requestAnimationFrame(() => {
        try {
          const frames = (root || document).querySelectorAll('[data-image-frame]');
          frames.forEach((frame) => {
            const card = (frame.closest && frame.closest('[data-slot="card"]')) || frame;
            if (card.querySelector('.avdb-emby-btn')) return;
            const tid = tidFromCard(card);
            const cardMovieId = movieIdFromCard(card);
            const cardNumber = tid ? null : numberFromCard(card);
            if (!tid && !cardMovieId && !cardNumber) return;

            const btn = document.createElement('button');
            btn.className = 'avdb-emby-btn';
            btn.textContent = '→ Emby';
            btn.addEventListener('click', async (ev) => {
              ev.stopPropagation();
              ev.preventDefault();
              if (btn.disabled) return;

              let article = tid ? articleMap.get(String(tid)) : null;
              let movie = cardMovieId ? movieMap.get(String(cardMovieId)) : null;
              if (!article && !movie && cardNumber) {
                const hit = findCachedByNumber(cardNumber);
                if (hit && hit.source === 'article') article = articleMap.get(hit.tid);
                if (hit && hit.source === 'movie') movie = movieMap.get(hit.movie_id);
              }

              if (!article && !movie && (cardMovieId || cardNumber)) {
                btn.disabled = true;
                btn.textContent = '查番号…';
                btn.classList.add('avdb-emby-busy');
                try {
                  if (cardMovieId) movie = await resolveMovieById(cardMovieId);
                  if (!movie && cardNumber) movie = await resolveMovieByNumber(cardNumber, card.textContent || '');
                } catch (e) { /* ignore */ }
                btn.disabled = false;
                btn.classList.remove('avdb-emby-busy');
                btn.textContent = '→ Emby';
              }

              if (!article && !movie) {
                btn.textContent = '无数据';
                btn.classList.add('avdb-emby-fail');
                toast((tid ? '没有缓存到文章数据 (tid=' + tid + ')'
                           : 'javdb 里查不到这个番号 (卡片解析为 ' + (cardNumber || '(空)') + ', 已试 ' + numberVariants(cardNumber).length + ' 种写法)')
                      + ', 请刷新页面重试; 仍不行就是上游 javdb 没收录', 'err');
                setTimeout(() => { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-fail'); }, 3000);
                return;
              }

              btn.textContent = '拉磁力…';
              btn.disabled = true;
              btn.classList.add('avdb-emby-busy');
              let magnetInfo = null;
              try { magnetInfo = await resolveMagnet(article, movie); } catch (e) { /* ignore */ }
              btn.disabled = false;

              if (!magnetInfo) {
                btn.textContent = '✗ 无磁力';
                btn.classList.remove('avdb-emby-busy');
                btn.classList.add('avdb-emby-fail');
                toast('入库失败: ' + (movie ? ('没有取到这部电影的磁力 (movie_id=' + movie.id + ')') : '该文章没有磁力链接')
                      + '. 在线资源请确认已登录 JavDB 账户(设置→个人→在线账户)', 'err');
                setTimeout(() => { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-fail'); }, 5000);
                return;
              }

              const number = (article && article.number) || (movie && (movie.number || movie.code)) || '';
              const payload = {
                source: magnetInfo.source,
                number: number,
                thread_id: (article && String(article.tid)) || (movie ? String(movie.id) : ''),
                title: magnetInfo.source === 'article' ? (article.title || '') : (movie.title || movie.origin_title || ''),
                thread_url: (article && article.detail_url) || (movie ? 'https://javdb.com/v/' + movie.id : ''),
                magnet: magnetInfo.magnet,
              };

              const opts = await askKind(payload);
              if (!opts) { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-busy'); return; }

              btn.disabled = true;
              btn.textContent = '提交中…';
              btn.classList.add('avdb-emby-busy');
              try {
                const { taskId, dup } = await submitOne({
                  magnet: payload.magnet,
                  thread_id: payload.thread_id,
                  title: payload.title,
                  thread_url: payload.thread_url,
                  kind: opts.kind,
                  category: opts.category,
                });
                if (dup || !taskId) {
                  btn.textContent = '已在库';
                  btn.classList.remove('avdb-emby-busy');
                  btn.classList.add('avdb-emby-ok');
                  toast('已在库中, 跳过: ' + (number || payload.magnet.slice(0, 48)), 'warn');
                  return;
                }
                btn.textContent = '推送中 ' + taskId.slice(0, 8) + '…';
                toast('已提交入库, task_id=' + taskId + '\n链路: 推115 → 落地 → 刮削 → strm → Emby\n可在 ' + importApiBase() + ' 实时看进度', 'ok');

                const r = await pollStatus(taskId);
                if (r && r.done) {
                  btn.textContent = '✓ 已入库';
                  btn.classList.remove('avdb-emby-busy');
                  btn.classList.add('avdb-emby-ok');
                  toast('入库完成! task_id=' + taskId + (r.data && r.data.msg ? '\n' + r.data.msg : ''), 'ok');
                } else if (r && r.done === false) {
                  btn.textContent = '✗ 失败';
                  btn.classList.remove('avdb-emby-busy');
                  btn.classList.add('avdb-emby-fail');
                  toast('入库失败: task_id=' + taskId + (r.data && r.data.msg ? '\n' + r.data.msg : ''), 'err');
                } else {
                  btn.textContent = '… 轮询超时';
                  btn.classList.remove('avdb-emby-busy');
                  toast('仍在入库中, 请到 ' + importApiBase() + ' 查看 task_id=' + taskId, 'ok');
                }
              } catch (e) {
                btn.textContent = '✗ 失败';
                btn.classList.add('avdb-emby-fail');
                toast('提交失败: ' + e.message, 'err');
              } finally {
                btn.disabled = false;
                setTimeout(() => {
                  btn.textContent = '→ Emby';
                  btn.classList.remove('avdb-emby-ok', 'avdb-emby-fail', 'avdb-emby-busy');
                }, 4000);
              }
            });
            mountBtn(card, frame, btn);
          });
        } finally {
          injecting = false;
        }
      });
    }

    // ==========================================================
    //  磁力详情页 (online-resources?movie=xxx)
    // ==========================================================
    function isMovieDetail() {
      return /\/online-resources(\/|$)/.test(location.pathname) && /[?&]movie=/.test(location.search);
    }
    function movieIdFromUrl() {
      const m = location.search.match(/[?&]movie=([^&]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    }

    const detailState = { movieId: null, number: '', articles: [], magnets: [], resources: [] };

    async function loadDetailData(movieId) {
      const st = { movieId, number: '', articles: [], magnets: [], resources: [] };
      try {
        const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId), { credentials: 'include' });
        const d = await r.json();
        if (d && d.data && d.data.movie) st.number = d.data.movie.number || '';
      } catch (e) { /* ignore */ }
      try {
        const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId) + '/magnets', { credentials: 'include' });
        const d = await r.json();
        const mags = (d && d.data && (d.data.magnets || d.data.items)) || [];
        if (Array.isArray(mags)) st.magnets = mags;
      } catch (e) { /* ignore */ }
      try {
        const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId) + '/comment-resources', { credentials: 'include' });
        const d = await r.json();
        const res = (d && d.data && (d.data.resources || d.data.items)) || [];
        if (Array.isArray(res)) st.resources = res;
      } catch (e) { /* ignore */ }
      if (st.number) {
        try {
          const r = await authFetch('/api/v1/articles/search', {
            method: 'POST',
            body: JSON.stringify({ keyword: st.number, page: 1, page_size: 50 }),
          });
          const d = await r.json();
          const items = (d && d.data && d.data.items) || [];
          if (Array.isArray(items)) st.articles = items;
        } catch (e) { /* ignore */ }
      }
      return st;
    }

    function onlineMagnetCandidates(st) {
      const out = [];
      (st.magnets || []).forEach((m) => {
        const u = m && (m.magnet_url || m.magnet || m.link || m.url || '');
        if (u && /^(magnet:|ed2k:)/i.test(u)) out.push({ href: u, name: m.name || m.title || '', key: (m.hash || m.btih || '').toLowerCase() });
      });
      (st.resources || []).forEach((r) => {
        const u = r && (r.resource_url || r.url || '');
        if (u && /^(magnet:|ed2k:)/i.test(u)) out.push({ href: u, name: r.name || '', key: String(r.hash || '').toLowerCase() });
      });
      return out;
    }

    function matchOnlineMagnetByCard(card, candidates, fallbackIndex) {
      const text = (card.textContent || '').trim().toLowerCase();
      for (const c of candidates) {
        if (c.key && text.includes(c.key)) return c;
      }
      for (const c of candidates) {
        const n = String(c.name || '').toLowerCase().replace(/\s+/g, '');
        if (n && n.length > 4 && text.replace(/\s+/g, '').includes(n)) return c;
      }
      return candidates.length ? candidates[fallbackIndex % candidates.length] : null;
    }

    function magnetCardSelector() { return 'div.rounded-xl.border'; }

    function sectionByTitle(titleText) {
      const secs = [...document.querySelectorAll('section.space-y-3')];
      return secs.find((s) => (s.textContent || '').trim().startsWith(titleText));
    }

    function cardsInSection(sec) {
      if (!sec) return [];
      return [...sec.querySelectorAll(magnetCardSelector())]
        .filter((c) => {
          const cls = (c.className || '').toString();
          return cls.includes('bg-muted/20') && cls.includes('p-3');
        })
        .filter((c) => !c.querySelector('.avdb-emby-btn'));
    }

    function makeDetailBtn(card, getMagnet) {
      const btn = document.createElement('button');
      btn.className = 'avdb-emby-btn';
      btn.textContent = '→ Emby';
      btn.style.position = 'static';
      btn.style.display = 'inline-flex';
      const actionsWrap = card.querySelector('.grid.w-full.grid-cols-2.gap-2, div[class*="gap-2"]');
      if (actionsWrap) {
        btn.style.marginLeft = '4px';
        actionsWrap.appendChild(btn);
      } else {
        card.appendChild(btn);
      }
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        if (btn.disabled) return;
        btn.textContent = '拉磁力…';
        btn.disabled = true;
        btn.classList.add('avdb-emby-busy');
        const magnetInfo = await getMagnet();
        btn.disabled = false;
        if (!magnetInfo || !magnetInfo.magnet) {
          btn.textContent = '✗ 无磁力';
          btn.classList.add('avdb-emby-fail');
          toast('入库失败: 该条磁力获取不到链接', 'err');
          setTimeout(() => { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-fail'); }, 5000);
          return;
        }
        const payload = {
          source: magnetInfo.source,
          number: detailState.number,
          thread_id: magnetInfo.tid ? String(magnetInfo.tid) : (movieIdFromUrl() || ''),
          title: magnetInfo.title || '',
          thread_url: magnetInfo.url || ('https://javdb.com/v/' + movieIdFromUrl()),
          magnet: magnetInfo.magnet,
        };
        const opts = await askKind(payload);
        if (!opts) { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-busy'); return; }

        btn.disabled = true;
        btn.textContent = '提交中…';
        btn.classList.add('avdb-emby-busy');
        try {
          const { taskId, dup } = await submitOne({
            magnet: payload.magnet,
            thread_id: payload.thread_id,
            title: payload.title,
            thread_url: payload.thread_url,
            kind: opts.kind,
            category: opts.category,
          });
          if (dup || !taskId) {
            btn.textContent = '已在库';
            btn.classList.replace('avdb-emby-busy', 'avdb-emby-ok');
            toast('已在库中, 跳过', 'warn');
            return;
          }
          btn.textContent = '推送中 ' + taskId.slice(0, 8) + '…';
          toast('已提交入库, task_id=' + taskId + '\n链路: 推115 → 落地 → 刮削 → strm → Emby', 'ok');
          const rr = await pollStatus(taskId);
          if (rr && rr.done) {
            btn.textContent = '✓ 已入库'; btn.classList.replace('avdb-emby-busy', 'avdb-emby-ok');
          } else if (rr && rr.done === false) {
            btn.textContent = '✗ 失败'; btn.classList.replace('avdb-emby-busy', 'avdb-emby-fail');
            toast('入库失败: ' + (rr.data && rr.data.msg || ''), 'err');
          } else {
            btn.textContent = '… 轮询超时';
            toast('仍在入库中, task_id=' + taskId, 'ok');
          }
        } catch (e) {
          btn.textContent = '✗ 失败'; btn.classList.add('avdb-emby-fail');
          toast('提交失败: ' + e.message, 'err');
        } finally {
          btn.disabled = false;
          setTimeout(() => {
            btn.textContent = '→ Emby';
            btn.classList.remove('avdb-emby-ok', 'avdb-emby-fail', 'avdb-emby-busy');
          }, 4000);
        }
      });
    }

    let detailLoadPromise = null;
    async function ensureDetailData(movieId) {
      if (detailState.movieId !== movieId || !detailLoadPromise) {
        detailState.movieId = movieId;
        detailLoadPromise = loadDetailData(movieId);
      }
      const fresh = await detailLoadPromise;
      if (detailState.movieId !== movieId) return ensureDetailData(movieId);
      Object.assign(detailState, fresh);
      return detailState;
    }

    async function injectDetailButtons() {
      if (!isMovieDetail()) return;
      const movieId = movieIdFromUrl();
      if (!movieId) return;

      await ensureDetailData(movieId);

      const btnInCard = (card) => !!card.querySelector('.avdb-emby-btn');

      const libSec = sectionByTitle('资源库磁力');
      if (libSec) {
        const cards = cardsInSection(libSec);
        cards.forEach((card, i) => {
          if (btnInCard(card)) return;
          makeDetailBtn(card, async () => {
            const art = (await ensureDetailData(movieId)).articles[i];
            const magnet = art && art.magnet;
            if (!art || !magnet) return null;
            return { source: 'article', magnet, tid: art.tid, title: art.title || art.number || '', url: art.detail_url };
          });
        });
      }

      const onlSec = sectionByTitle('在线磁链');
      if (onlSec) {
        const cards = cardsInSection(onlSec);
        cards.forEach((card, i) => {
          if (btnInCard(card)) return;
          makeDetailBtn(card, async () => {
            const st2 = await ensureDetailData(movieId);
            const cand = matchOnlineMagnetByCard(card, onlineMagnetCandidates(st2), i);
            if (!cand) return null;
            return { source: 'javdb', magnet: cand.href, tid: null, title: cand.name || st2.number || '', url: null };
          });
        });
      }
    }

    // ---------- 监听 ----------
    // MutationObserver 在虚拟滚动/频繁重排下会疯狂触发, 每次都全量 querySelectorAll 很费。
    // 合并成 200ms 一次的尾触发, 注入本身仍是幂等的(有 .avdb-emby-btn 就跳过)。
    let injectTimer = null;
    function scheduleInject() {
      if (injectTimer) return;
      injectTimer = setTimeout(() => {
        injectTimer = null;
        try { injectButtons(document); } catch (e) { /* ignore */ }
        if (isMovieDetail()) {
          injectDetailButtons().catch(() => {});
        }
      }, 200);
    }

    const mo = new MutationObserver(scheduleInject);
    mo.observe(document.body, { childList: true, subtree: true });

    setTimeout(() => {
      injectButtons(document);
      injectDetailButtons();
    }, 1200);

    console.log('[Emby] AVdb 模式已加载 v1.8.1, import_api=' + importApiBase());
  }

  // ============================================================
  //  =======================  LTX 分支  ========================
  // ============================================================
  function initLTX() {

    const MAX_ITEMS = 8000;
    const itemMap = new Map();   // code -> item

    function normCode(s) { return String(s || '').trim().toUpperCase(); }

    function pickLink(item) {
      const links = (item.links || []).concat(item.pendingLinks || []);
      for (const l of links) if (/^ed2k:\/\//i.test(l)) return l;
      for (const l of links) if (/^magnet:\?/i.test(l)) return l;
      return '';
    }

    // 链接字段兼容: 站点快照是 links:["ed2k://..."] 字符串数组,
    // 但实时接口可能给对象数组或单个 link/magnet 字段, 全都收
    function toLinkArray(arr) {
      const out = [];
      if (!Array.isArray(arr)) return out;
      for (const x of arr) {
        if (typeof x === 'string') {
          if (x) out.push(x);
        } else if (x && typeof x === 'object') {
          const u = x.url || x.magnet || x.magnet_url || x.link || x.href || x.src || x.uri;
          if (typeof u === 'string' && u) out.push(u);
        }
      }
      return out;
    }

    function ingestItem(raw) {
      if (!raw || typeof raw !== 'object') return false;
      const code = normCode(raw.code || raw.number || raw.id);
      if (!code) return false;

      const links = toLinkArray(raw.links);
      const pending = toLinkArray(raw.pending_links || raw.pendingLinks);
      // 单值字段也收: 快照里有 link: "ed2k://..." 这种写法
      for (const k of ['link', 'magnet', 'magnet_url', 'ed2k']) {
        if (typeof raw[k] === 'string' && raw[k]) links.push(raw[k]);
      }
      if (!links.length && !pending.length) return false;

      const prev = itemMap.get(code) || {};
      itemMap.set(code, {
        code,
        title: raw.title || prev.title || '',
        titleZh: raw.title_zh || raw.titleZh || prev.titleZh || '',
        links,
        pendingLinks: pending,
        url: raw.url || prev.url || '',
        cover: raw.cover || prev.cover || '',
      });
      if (itemMap.size > MAX_ITEMS) {
        const drop = itemMap.size - MAX_ITEMS;
        let i = 0;
        for (const k of itemMap.keys()) { if (i++ >= drop) break; itemMap.delete(k); }
      }
      return true;
    }

    function ingestResponse(body) {
      if (!body) return 0;
      let arr = null;
      if (Array.isArray(body)) arr = body;
      else if (Array.isArray(body.items)) arr = body.items;
      else if (body.data && Array.isArray(body.data.items)) arr = body.data.items;
      else if (body.data && Array.isArray(body.data)) arr = body.data;
      if (!arr || !arr.length) return 0;
      let n = 0;
      for (const it of arr) if (ingestItem(it)) n++;
      if (n) scheduleRender();
      return n;
    }

    // ---------- 数据捕获 ----------
    // 站点的接口响应是加密的: 响应头 application/vnd.ltx.encrypted+json,
    // 请求还要带 X-LTX-Session + 签名, 裸调 /api/items 直接 428。
    // 它自己的 Us() 里这么解:
    //     let c = Ns.decode(s), l = JSON.parse(c);
    // 明文一定过 JSON.parse, 所以在解析层截, 不用复刻它的 ECDH 握手。
    //
    // ⚠ 关键坑: 本脚本带 @grant, Tampermonkey 会把它放进隔离沙箱执行。
    //   沙箱里的 JSON 不是页面的那个 JSON, 改 window.JSON.parse 对页面毫无影响
    //   (v1.3.x 面板一条都抓不到就是因为这个)。必须改 unsafeWindow 上的 JSON。
    //   注: XMLHttpRequest 这类 DOM 接口在沙箱里是透明的, 所以 AVdb 分支没事。
    const pageWin = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
    const pageJSON = pageWin.JSON;
    const CAP_KEY = '__ltxEmbyKap';
    const HOOK_FLAG = '__ltxEmbyParseHook';
    const RESP_FLAG = '__ltxEmbyRespHook';

    // 诊断计数: 面板上直接显示, 免得每次都要开控制台
    const capStat = { resp: 0, parse: 0, hits: 0, items: 0, fallback: 0, shape: '' };

    function looksLikeItems(v) {
      if (!v || typeof v !== 'object') return false;
      if (Array.isArray(v)) {
        return v.length > 0 && v[0] && typeof v[0] === 'object' && 'code' in v[0];
      }
      if (Array.isArray(v.items)) {
        return v.items.length > 0 && v.items[0] && typeof v.items[0] === 'object' && 'code' in v.items[0];
      }
      if (v.data && Array.isArray(v.data.items)) {
        return v.data.items.length > 0 && v.data.items[0] && typeof v.data.items[0] === 'object' && 'code' in v.data.items[0];
      }
      return false;
    }

    function firstItemOf(v) {
      if (!v || typeof v !== 'object') return null;
      if (Array.isArray(v)) return v[0] || null;
      if (Array.isArray(v.items)) return v.items[0] || null;
      if (v.data && Array.isArray(v.data.items)) return v.data.items[0] || null;
      if (v.data && Array.isArray(v.data)) return v.data[0] || null;
      return null;
    }

    // 统一入口: 先直接吃(同 world 通常可读);
    // 只有"读对象时抛异常"才走字符串回传 —— 解析结果为 0 属于正常(没链接的条目),
    // 绝不能触发回传, 否则 postMessage -> JSON.parse -> 再进来 会形成回环刷屏。
    function handlePayload(v) {
      try {
        if (!looksLikeItems(v)) return;
        capStat.hits++;
        let n = 0;
        let threw = null;
        try { n = ingestResponse(v) || 0; } catch (e) { threw = e; }

        if (n) {
          capStat.items += n;
        } else if (threw && capStat.fallback < 3) {
          capStat.fallback++;
          try { pageWin.postMessage({ [CAP_KEY]: pageJSON.stringify(v) }, '*'); } catch (e) { /* ignore */ }
        } else {
          const first = firstItemOf(v);
          if (first) {
            try { capStat.shape = Object.keys(first).join(','); } catch (e) { /* ignore */ }
          }
        }
        updateStat();
      } catch (e) { /* 绝不打断页面 */ }
    }

    // 跨 world 直接读对象在部分环境会拿不到属性, 留一条字符串回传通道
    window.addEventListener('message', function (ev) {
      const d = ev.data;
      if (!d || typeof d !== 'object' || typeof d[CAP_KEY] !== 'string') return;
      try {
        const n = ingestResponse(JSON.parse(d[CAP_KEY])) || 0;
        if (n) { capStat.items += n; updateStat(); }
      } catch (e) { /* ignore */ }
    });

    // ---- 钩子 1: Response.prototype.json ----
    // 站点 pc() 里是 `return e.json()`, 这是浏览器原生实现,
    // 不会去调 JS 层的 JSON.parse —— 只挂 JSON.parse 会漏掉全部 /api/items 数据。
    try {
      const RP = pageWin.Response && pageWin.Response.prototype;
      if (RP && typeof RP.json === 'function' && !RP[RESP_FLAG]) {
        const origJson = RP.json;
        RP.json = function () {
          capStat.resp++;
          let p;
          try { p = origJson.call(this); } catch (e) { throw e; }
          return p.then(function (v) {
            handlePayload(v);
            return v;
          });
        };
        RP[RESP_FLAG] = true;
        console.log('[Emby] Response.json 钩子已装入页面上下文');
      } else if (RP && RP[RESP_FLAG]) {
        console.log('[Emby] Response.json 钩子已存在');
      }
    } catch (e) {
      console.warn('[Emby] Response.json 钩子安装失败:', e);
    }

    // ---- 钩子 2: JSON.parse (兜底, 覆盖 text() 后再 parse 的路径) ----
    try {
      if (!pageJSON || typeof pageJSON.parse !== 'function') {
        console.warn('[Emby] 拿不到页面的 JSON 对象');
      } else if (pageJSON[HOOK_FLAG]) {
        console.log('[Emby] JSON.parse 钩子已存在');
      } else {
        const origParse = pageJSON.parse;
        pageJSON.parse = function (text, reviver) {
          const v = origParse.call(this, text, reviver);
          capStat.parse++;
          handlePayload(v);
          return v;
        };
        pageJSON[HOOK_FLAG] = true;
        console.log('[Emby] JSON.parse 钩子已装入页面上下文');
      }
    } catch (e) {
      console.warn('[Emby] JSON.parse 钩子安装失败:', e);
    }

    function updateStat() {
      const el = document.getElementById('ltxe-stat');
      if (!el) return;
      let s = '钩子: json×' + capStat.resp + ' parse×' + capStat.parse +
        ' 命中×' + capStat.hits + ' · 条目 ' + itemMap.size;
      if (!itemMap.size && capStat.shape) s += '\n首条字段: ' + capStat.shape;
      el.textContent = s;
    }

    // ---------- 面板 ----------
    const PREFS_KEY = 'ltx-emby-prefs';
    const state = { filter: '', kind: 'fanhao', category: 'av' };
    try {
      const p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
      if (p.kind === 'fanhao' || p.kind === 'non_fanhao') state.kind = p.kind;
      if (p.category && CATEGORIES.some((c) => c.key === p.category)) state.category = p.category;
    } catch (e) { /* ignore */ }
    function savePrefs() {
      try { localStorage.setItem(PREFS_KEY, JSON.stringify({ kind: state.kind, category: state.category })); } catch (e) { /* ignore */ }
    }

    let panel, listEl, countEl, filterEl, kindEl, catEl, taEl, batchBtn;

    function buildPanel() {
      panel = document.createElement('div');
      panel.id = 'ltxe-panel';
      panel.innerHTML = `
        <div class="ltxe-head" id="ltxe-head">
          <b>LTX → Emby</b>
          <span class="ltxe-count" id="ltxe-count">0 条</span>
          <span class="ltxe-caret" id="ltxe-caret">收起</span>
        </div>
        <div class="ltxe-body">
          <div class="ltxe-row">
            <select id="ltxe-kind">
              <option value="fanhao">fanhao (影片/番号)</option>
              <option value="non_fanhao">non_fanhao (剧集)</option>
            </select>
            <select id="ltxe-cat"></select>
          </div>
          <div class="ltxe-row">
            <input id="ltxe-filter" type="text" placeholder="过滤番号 / 标题 (回车 = 入库第一条)" />
          </div>
          <div class="ltxe-list" id="ltxe-list"></div>
          <div class="ltxe-stat" id="ltxe-stat"></div>
          <div class="ltxe-foot">
            <button class="ltxe-btn ghost" id="ltxe-clear">清空已提交标记</button>
            <button class="ltxe-btn ghost" id="ltxe-pull">重载数据</button>
            <button class="ltxe-btn ghost" id="ltxe-open" style="margin-left:auto">打开 5081</button>
          </div>
          <textarea class="ltxe-ta" id="ltxe-ta"
            placeholder="批量: 一行一个 ed2k:// 或 magnet:? 链接, 直接整段粘进来"></textarea>
          <div class="ltxe-foot">
            <button class="ltxe-btn" id="ltxe-batch">批量入库</button>
            <span style="color:#6e7681;font-size:11px">链接可从站点「复制链接」直接粘</span>
          </div>
        </div>`;
      document.body.appendChild(panel);

      listEl = panel.querySelector('#ltxe-list');
      countEl = panel.querySelector('#ltxe-count');
      filterEl = panel.querySelector('#ltxe-filter');
      kindEl = panel.querySelector('#ltxe-kind');
      catEl = panel.querySelector('#ltxe-cat');
      taEl = panel.querySelector('#ltxe-ta');
      batchBtn = panel.querySelector('#ltxe-batch');

      catEl.innerHTML = categoryOptionsHtml(false);
      catEl.value = state.category;
      kindEl.value = state.kind;

      panel.querySelector('#ltxe-head').addEventListener('click', () => {
        panel.classList.toggle('ltxe-collapsed');
        panel.querySelector('#ltxe-caret').textContent =
          panel.classList.contains('ltxe-collapsed') ? '展开' : '收起';
      });

      kindEl.addEventListener('change', () => { state.kind = kindEl.value; savePrefs(); });
      catEl.addEventListener('change', () => { state.category = catEl.value; savePrefs(); });
      filterEl.addEventListener('input', () => { state.filter = filterEl.value.trim().toUpperCase(); renderList(); });
      filterEl.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        const first = filteredItems()[0];
        if (!first) { toast('没有匹配的条目', 'warn'); return; }
        const row = listEl.querySelector('.ltxe-item');
        if (row) doImport(first, row.querySelector('.ltxe-btn'));
      });

      panel.querySelector('#ltxe-clear').addEventListener('click', () => {
        sentSet.clear();
        try { localStorage.removeItem(SENT_KEY); } catch (e) { /* ignore */ }
        renderList();
        toast('已清空「已提交」标记', 'ok');
      });

      panel.querySelector('#ltxe-open').addEventListener('click', () => {
        window.open(importApiBase(), '_blank', 'noopener');
      });


      // 重载数据: 站点响应是加密的, 脚本没法自己再要一遍(裸调 /api/items 会 428)。
      // 数据全靠钩子在页面自己解析时截获, 所以"重新抓一次"= 重新加载页面。
      panel.querySelector('#ltxe-pull').addEventListener('click', () => {
        toast('正在重新加载页面以重新捕获数据…', 'ok');
        setTimeout(() => {
          try { (pageWin.location || location).reload(); return; } catch (e) { /* 落到下面的兜底 */ }
          try { location.href = location.href; } catch (e) { /* ignore */ }
          try { pageWin.location.href = pageWin.location.href; } catch (e) { /* ignore */ }
        }, 250);
      });

      batchBtn.addEventListener('click', doBatch);
      renderList();
    }

    function filteredItems() {
      const all = [...itemMap.values()].reverse();
      if (!state.filter) return all;
      return all.filter((it) => {
        if (it.code.indexOf(state.filter) >= 0) return true;
        const t = ((it.title || '') + ' ' + (it.titleZh || '')).toUpperCase();
        return t.indexOf(state.filter) >= 0;
      });
    }

    let renderTimer = null;
    function scheduleRender() {
      if (renderTimer) return;
      renderTimer = setTimeout(() => { renderTimer = null; renderList(); }, 250);
    }

    function renderList() {
      if (!listEl) return;
      const items = filteredItems();
      countEl.textContent = itemMap.size + ' 条';
      updateStat();
      if (!items.length) {
        listEl.innerHTML = '<div class="ltxe-empty">' +
          (itemMap.size ? '没有匹配的条目' : '还没捕获到数据<br/>在站点里翻一下列表/搜索一次就自动进来了') +
          '</div>';
        return;
      }
      const shown = items.slice(0, 200);
      const frag = document.createDocumentFragment();
      for (const it of shown) {
        const link = pickLink(it);
        const h = hashOf(it.code, link);
        const sent = sentSet.has(h);

        const row = document.createElement('div');
        row.className = 'ltxe-item';
        row.dataset.code = it.code;

        const code = document.createElement('span');
        code.className = 'ltxe-code';
        code.textContent = it.code;

        const tag = document.createElement('span');
        tag.className = 'ltxe-tag';
        tag.textContent = /^ed2k:/i.test(link) ? 'ed2k' : (/^magnet:/i.test(link) ? '磁力' : '—');

        const title = document.createElement('span');
        title.className = 'ltxe-title';
        title.textContent = it.title || it.titleZh || '';
        title.title = title.textContent;

        const btn = document.createElement('button');
        btn.className = 'ltxe-btn' + (sent ? ' ok' : '');
        btn.textContent = sent ? '已提交' : '→ Emby';
        btn.disabled = !link || sent;
        btn.addEventListener('click', (ev) => { ev.stopPropagation(); doImport(it, btn); });

        row.append(code, tag, title, btn);
        frag.appendChild(row);
      }
      listEl.innerHTML = '';
      listEl.appendChild(frag);
      if (items.length > shown.length) {
        const more = document.createElement('div');
        more.className = 'ltxe-empty';
        more.textContent = '还有 ' + (items.length - shown.length) + ' 条, 用过滤框缩小范围';
        listEl.appendChild(more);
      }
    }

    async function doImport(item, btn) {
      const link = pickLink(item);
      if (!link) { toast(item.code + ' 没有 ed2k/磁力链接', 'warn'); return; }
      const h = hashOf(item.code, link);

      if (btn) { btn.disabled = true; btn.classList.add('busy'); btn.textContent = '提交中…'; }

      try {
        // 带上帖子上下文: thread_id + title + thread_url, 否则落点退化成 manual_<hash8>
        // thread_id 会变成 115 目录 thread_<id> (import_api.py:413) 并用于同帖去重(:1307),
        // 所以这里用番号当身份 —— 比 javdb 短码可读, 且一部番号就是一个"帖"
        const { taskId, dup } = await submitOne({
          magnet: link,
          thread_id: item.code,
          title: item.title || item.titleZh || item.code,
          thread_url: item.url || '',
          kind: state.kind,
          category: state.category,
        });
        markSent(h);
        if (dup || !taskId) {
          if (btn) { btn.classList.remove('busy'); btn.classList.add('ok'); btn.textContent = '已在库'; }
          toast(item.code + ' 已在库中, 跳过', 'warn');
          return;
        }
        if (btn) btn.textContent = '推送 ' + taskId.slice(0, 8) + '…';
        toast(item.code + ' 已提交\ntask_id=' + taskId + '\n链路: 推115 → 落地 → 刮削 → strm → Emby', 'ok');

        const r = await pollStatus(taskId);
        if (!btn) return;
        btn.classList.remove('busy');
        if (r.done) {
          btn.classList.add('ok'); btn.textContent = '✓ 已入库';
          toast(item.code + ' 入库完成' + (r.data && r.data.msg ? '\n' + r.data.msg : ''), 'ok');
        } else if (r.done === false) {
          btn.classList.add('err'); btn.textContent = '✗ 失败'; btn.disabled = false;
          toast(item.code + ' 入库失败' + (r.data && r.data.msg ? ': ' + r.data.msg : ''), 'err');
        } else {
          btn.textContent = '… 处理中';
          toast(item.code + ' 仍在处理, 去 ' + importApiBase() + ' 看进度', 'ok');
        }
      } catch (e) {
        if (btn) { btn.classList.remove('busy'); btn.classList.add('err'); btn.textContent = '✗ 失败'; btn.disabled = false; }
        toast('提交失败: ' + e.message, 'err');
      }
    }

    async function doBatch() {
      const txt = String(taEl.value || '').trim();
      if (!txt) { toast('先把 ed2k/磁力 贴进下面的框', 'warn'); return; }
      const lines = txt.split('\n').map((s) => s.trim()).filter((s) => /^(ed2k:\/\/|magnet:\?)/i.test(s));
      if (!lines.length) { toast('框里没有可识别的 ed2k/磁力 链接', 'err'); return; }

      batchBtn.disabled = true;
      const old = batchBtn.textContent;
      batchBtn.textContent = '提交中…';
      try {
        const { taskId, raw } = await submitLinks(lines, state.kind, state.category);
        const okN = (raw && raw.submitted) || 0;
        const badN = (raw && raw.failed) || 0;
        const dupN = (raw && raw.dup_skipped) || 0;
        const skipN = (raw && raw.skipped) || 0;
        toast('已提交 ' + okN + ' 条' + (badN ? ', 失败 ' + badN : '') +
              (dupN ? ', 重复跳过 ' + dupN : '') + (skipN ? ', 解析跳过 ' + skipN : '') +
              (taskId ? '\ntask_id=' + taskId : '') + '\n进度见 ' + importApiBase(), okN ? 'ok' : 'err');
        if (okN) taEl.value = '';
      } catch (e) {
        toast('批量提交失败: ' + e.message, 'err');
      } finally {
        batchBtn.disabled = false;
        batchBtn.textContent = old;
      }
    }

    // ---------- 卡片内注入「→ Emby」按钮 ----------
    // 站点卡片底部有「复制 ed2k」/「添加到下载」两行按钮, 我们把按钮插进同一个容器。
    // 链接不碰 DOM(拿不到), 而是按卡片上的番号回查 itemMap —— 数据已经由钩子收好了。
    const CARD_BTN_CLS = 'ltxe-cardbtn';
    const handledEls = new WeakSet();

    // 番号前缀必须以字母开头 —— 否则会把日期 "2019-03-07" 里的 "2019-03" 当成番号
    const CARD_CODE_RE = /\b([A-Za-z][A-Za-z0-9]{1,11})-(\d{2,6})\b/g;

    // 从按钮往上找"卡片": 逐层看, 优先返回"已在 itemMap 里"的番号
    function codeFromEl(el) {
      let n = el;
      let fallback = '';
      for (let i = 0; i < 8 && n && n !== document.body; i++, n = n.parentElement) {
        const raw = String(n.textContent || '');
        if (raw.length > 4000) break;         // 再往上就是整个列表了
        const m = raw.match(CARD_CODE_RE);
        if (!m) continue;
        const uniq = [...new Set(m.map((x) => x.toUpperCase()))];
        const inMap = uniq.filter((c) => itemMap.has(c));
        if (inMap.length === 1) return inMap[0];      // 唯一命中, 最可信
        if (inMap.length > 1) {                        // 多个命中 = 已越过卡片
          if (!fallback) fallback = inMap[0];
          break;
        }
        if (uniq.length === 1) {                       // 只有一个候选但还没捕获
          if (!fallback) fallback = uniq[0];
          continue;
        }
        break;                                          // 多个番号, 越过卡片边界
      }
      return fallback;
    }

    function makeCardBtn(code) {
      const b = document.createElement('button');
      b.className = CARD_BTN_CLS;
      b.textContent = '→ Emby';
      b.title = code;
      b.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        if (b.disabled) return;
        // 点击时重新识别一次: 注入可能发生在数据到达之前
        const key = codeFromEl(b) || code;
        const item = itemMap.get(key);
        if (!item) { toast(key + ' 还没捕获到数据, 滚动到它那页再试', 'warn'); return; }
        await doImport(item, b);
      });
      return b;
    }

    let cardTimer = null;
    function scheduleCardInject() {
      if (cardTimer) return;
      cardTimer = setTimeout(() => {
        cardTimer = null;
        try { injectCardButtons(); } catch (e) { /* ignore */ }
      }, 400);
    }

    function injectCardButtons() {
      const cands = document.querySelectorAll('button, [role="button"]');
      for (const el of cands) {
        if (handledEls.has(el)) continue;
        if (el.closest && el.closest('#ltxe-panel')) continue;
        const t = (el.textContent || '').trim();
        if (!t || t.length > 20) continue;
        if (!/^(复制\s*(ed2k|磁力|链接)|添加到下载)/.test(t)) continue;

        handledEls.add(el);
        const code = codeFromEl(el);
        if (!code) continue;

        const stack = el.parentElement;
        if (!stack) continue;
        if (stack.querySelector('.' + CARD_BTN_CLS)) continue;   // 这个卡片已经加过
        stack.appendChild(makeCardBtn(code));
      }
    }

    function boot() {
      if (document.getElementById('ltxe-panel')) return;
      buildPanel();
      injectCardButtons();
      console.log('[Emby] LTX 模式已加载 v1.8.1, import_api=' + importApiBase());
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 800));
    } else {
      setTimeout(boot, 800);
    }

    const mo = new MutationObserver(() => {
      if (!document.getElementById('ltxe-panel')) boot();
      scheduleCardInject();
    });
    try { mo.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) { /* ignore */ }
  }

  // ============================================================
  //  分派
  //  @run-at document-start: LTX 的 JSON.parse 钩子必须赶在站点首屏请求之前装上,
  //  否则第一批数据(常常就是全量目录)会被漏掉。
  //  AVdb 分支要 mo.observe(document.body), document-start 时 body 还不存在, 等 DOM 就绪。
  // ============================================================
  if (IS_LTX) {
    initLTX();
  } else if (document.body) {
    initAVDB();
  } else {
    document.addEventListener('DOMContentLoaded', initAVDB, { once: true });
  }

})();
