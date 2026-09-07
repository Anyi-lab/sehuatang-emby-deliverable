// ==UserScript==
// @name         AVdb → Emby 一键入库 (色花堂点单版)
// @namespace    sehuatang.emby.deliverable
// @version      0.3.3
// @updateURL    https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-emby-inject.user.js
// @downloadURL  https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-emby-inject.user.js
// @description  在 AVdb 文章卡片 + 在线资源(online-resources ranking/top/latest)卡片 + 磁力详情页(online-resources?movie=)上注入"→ Emby 入库"按钮。文章卡片直接取缓存 magnet; 在线资源卡片按番号反查本地库(优先)或拉取 JavDB 磁力; 磁力详情页对资源库磁力(色花堂)、在线磁链(javdb magnets)与评论区资源(javdb comment-resources, 含磁力/ED2K)逐条注入, 每条一键入库。再推给 import_api (localhost:5081) 全包入库; 入库时从下拉选落地分类(AV/FC2/丝袜/国产自拍/欧美/里番, 可选自定义)。
// @author       clacky
// @match        http://localhost:8200/*
// @match        http://127.0.0.1:8200/*
// @match        http://192.168.2.238:8200/*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // ---------- 配置 ----------
  const IMPORT_API = 'http://localhost:5081';   // import_api 地址 (浏览器侧可达)
  const JAVDB_MOVIE_MAX_RANK_CACHE = 1000;     // 在线资源 movie 缓存上限

  // 入库分类选项 (与 import_api CATEGORY_MAP 保持一致, 决定 strm 落地/已刮削目录分流)
  // key -> 显示名; norm_category 会把非法/空回退到 av
  const CATEGORY_OPTIONS = [
    { key: 'av',  name: 'AV (默认)' },
    { key: 'fc2', name: 'FC2' },
    { key: 'sw',  name: '丝袜' },
    { key: 'cn',  name: '国产自拍' },
    { key: 'ea',  name: '欧美' },
    { key: 'lf',  name: '里番' },
    { key: '__custom__', name: '自定义…' },
  ];

  // ---------- 数据缓存 ----------
  // articleMap: tid -> article (来自 POST /articles/search)
  const articleMap = new Map();
  // movieMap: movie_id -> movie (来自 javdb 列表响应 rankings/top/latest/tags 等)
  const movieMap = new Map();
  // numberIndex: number -> movie (番号反查索引, 用于在线资源卡片)
  const numberIndex = new Map();

  // ---------- 拦截页面自身 XHR (axios 底层) ----------
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
              // 也建番号索引 (在线资源页反查本地库用)
              if (it.number) numberIndex.set(String(it.number).trim().toUpperCase(), { source: 'article', tid: String(tid) });
            }
          });
          if (items.length) console.log('[AVdb-Emby] cached', items.length, 'articles, total:', articleMap.size);
        } else if (this.__m === 'GET' && /\/javdb\//.test(this.__u)) {
          // 拦截 javdb 在线资源列表响应 (rankings/top/latest/tags/movies 等)
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
            console.log('[AVdb-Emby] cached', movies.length, 'javdb movies, total:', movieMap.size);
          }
        }
      } catch (e) { /* ignore */ }
    });
    return origSend.apply(this, args);
  };

  // ---------- 样式 ----------
  GM_addStyle(`
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
    .avdb-emby-toast {
      position:fixed; left:50%; bottom:36px; transform:translateX(-50%);
      z-index:99999; max-width:560px; padding:10px 16px; border-radius:10px;
      font-size:13px; line-height:1.5; color:#fff; background:rgba(17,24,39,.94);
      box-shadow:0 6px 24px rgba(0,0,0,.45); white-space:pre-wrap; word-break:break-all;
    }
    .avdb-emby-toast.ok { border:1px solid #22c55e; }
    .avdb-emby-toast.err { border:1px solid #ef4444; }
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
  `);

  // ---------- 工具 ----------
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

  // 从封面图 URL 提取本地文章 tid: /api/v1/articles/{tid}/image
  function tidFromUrl(src) {
    const m = (src || '').match(/\/articles\/(\d+)\/image/);
    return m ? m[1] : null;
  }

  // 从 cookie 读 JWT (前端 axios 同样用它加 Authorization: Bearer, 用于 /api/v1/articles/* 鉴权)
  function getJwtToken() {
    const m = document.cookie.match(/(?:^|;\s*)jwt_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // 卡片内找番号文本 (在线资源卡片没有本地 tid, 从 DOM 文本提取番号)
  function numberFromCard(card) {
    const re = /^\s*([a-z0-9]{1,12}-\d{2,8})\s*$/i;
    const els = card.querySelectorAll('span, div, p, a');
    for (const el of els) {
      const t = (el.textContent || '').trim();
      if (t && t.length <= 20 && re.test(t)) return t;
    }
    return null;
  }

  // ---------- 异步拉取 magnet ----------
  // 场景1: 本地文章 (articleMap 有数据) -> 直接用 article.magnet
  // 场景2: 在线资源 movie -> 优先番号反查本地库(avdb search, 走同源 cookie), 失败则拉 javdb movies/{id}/magnets
  async function resolveMagnet(article, movie) {
    // 已有本地文章 magnet
    if (article && article.magnet) return { magnet: article.magnet, source: 'article' };

    const num = (movie && (movie.number || movie.code)) || (article && article.number) || '';
    // 本地番号索引命中? (页面已加载过该番号的文章列表时)
    if (num) {
      const hit = numberIndex.get(String(num).trim().toUpperCase());
      if (hit && hit.source === 'article') {
        const art = articleMap.get(hit.tid);
        if (art && art.magnet) return { magnet: art.magnet, source: 'article' };
      }
    }

    // 拉 JavDB 磁力 (movie_id 已知): GET /api/v1/javdb/movies/{id}/magnets
    // avdb 的 api_key_or_jwt 支持 cookie(jwt_token) 鉴权, 同源 fetch 带 credentials 即可
    if (movie && movie.id) {
      try {
        const res = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movie.id) + '/magnets', { credentials: 'include' });
        const d = await res.json();
        const magnets = (d && d.data && d.data.magnets) || (d && d.data && d.data.items) || [];
        const m = magnets.find((x) => x && /^(magnet:)/.test(x.magnet_url || x.magnet || x.link || x.url || '')) ||
                  magnets.find((x) => x && /^(magnet:|ed2k:)/.test(String(x.magnet_url || x.magnet || x.link || x.url || '')));
        const mlink = m && (m.magnet_url || m.magnet || m.link || m.url);
        if (mlink) return { magnet: mlink, source: 'javdb' };
      } catch (e) { /* fallthrough */ }
    }
    return null;
  }

  // ---------- 弹窗: 确认 kind + 可选 category ----------
  function askKind(payload) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'avdb-emby-modal-overlay';
      const autoKind = /^[a-z0-9]{1,12}-\d{2,8}/i.test(String(payload.number || '').trim()) ? 'fanhao' : 'non_fanhao';
      const catOptionsHtml = CATEGORY_OPTIONS.map((o) =>
        `<option value="${o.key}">${escapeHtml(o.name)}</option>`
      ).join('');
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
          <select id="avdb-emby-cat">${catOptionsHtml}</select>
          <input type="text" id="avdb-emby-cat-custom" class="hidden" placeholder="输入自定义分类 key, 如: 剧情 / fc2 / 无码" />
          <div class="row">
            <button class="cancel" id="avdb-emby-cancel">取消</button>
            <button class="go" id="avdb-emby-go">开始入库</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (v) => { overlay.remove(); resolve(v); };
      overlay.querySelector('#avdb-emby-cancel').addEventListener('click', () => close(null));
      // 选"自定义…"时显示输入框, 方便输入任意分类 key
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

  // ---------- 调 import_api ----------
  async function callImport(body) {
    const res = await new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: IMPORT_API + '/api/import',
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(body),
        timeout: 15000,
        onload: (r) => {
          try { resolve({ status: r.status, data: JSON.parse(r.responseText) }); }
          catch (e) { resolve({ status: r.status, data: { error: r.responseText.slice(0, 200) } }); }
        },
        onerror: () => reject(new Error('网络错误: 无法连接 import_api (' + IMPORT_API + ')')),
        ontimeout: () => reject(new Error('请求超时 (15s)')),
      });
    });
    if (res.data && res.data.task_id) return { taskId: res.data.task_id, data: res.data };
    if (res.data && res.data.error) throw new Error(res.data.error);
    throw new Error('import_api 未返回 task_id');
  }

  async function pollStatus(taskId) {
    const deadline = Date.now() + 3 * 60 * 60 * 1000;
    for (;;) {
      const res = await new Promise((resolve) => {
        GM_xmlhttpRequest({
          method: 'GET',
          url: IMPORT_API + '/api/import/status?task_id=' + encodeURIComponent(taskId),
          timeout: 10000,
          onload: (r) => { try { resolve(JSON.parse(r.responseText)); } catch (e) { resolve(null); } },
          onerror: () => resolve(null),
          ontimeout: () => resolve(null),
        });
      });
      if (!res) { await new Promise(r => setTimeout(r, 3000)); continue; }
      const d = res.data || res;
      const status = String(d.status || '');
      if (/^(done|success|ok|完成|成功)$/i.test(status)) return { done: true, data: d };
      if (/^(fail|failed|error|失败)/i.test(status)) return { done: false, data: d };
      await new Promise(r => setTimeout(r, 4000));
      if (Date.now() > deadline) return { done: null, data: d };
    }
  }

  // ---------- 注入按钮 ----------
  let injecting = false;
  function injectButtons(root) {
    if (injecting) return;
    injecting = true;
    requestAnimationFrame(() => {
      try {
        const frames = (root || document).querySelectorAll('[data-image-frame]');
        frames.forEach((frame) => {
          // frame 只是封面容器; 实际卡片是最近的 data-slot="card" 父 (番号/标题信息区在 frame 外部)
          const card = (frame.closest && frame.closest('[data-slot="card"]')) || frame;
          // 已注入跳过 (订阅徽章按钮 .resource-card-subscription-badge 是普通影片卡的常驻角标, 不是订阅容器)
          if (card.querySelector('.avdb-emby-btn')) return;
          const img = card.querySelector('img[src*="/articles/"], img[data-src*="/articles/"]');
          let tid = img ? tidFromUrl(img.currentSrc || img.src) : null;
          let cardNumber = null;
          if (!tid) {
            // 在线资源卡片: 封面是 javdb 外链 (img-proxy), 无本地 tid, 从整卡 DOM 找番号文本
            cardNumber = numberFromCard(card);
          }
          if (!tid && !cardNumber) return;

          const btn = document.createElement('button');
          btn.className = 'avdb-emby-btn';
          btn.textContent = '→ Emby';
          btn.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            ev.preventDefault();
            if (btn.disabled) return;

            let article = tid ? articleMap.get(String(tid)) : null;
            let movie = null;
            if (!article && cardNumber) {
              const hit = numberIndex.get(String(cardNumber).trim().toUpperCase());
              if (hit && hit.source === 'article') article = articleMap.get(hit.tid);
              if (hit && hit.source === 'movie') movie = movieMap.get(hit.movie_id);
            }

            if (!article && !movie) {
              btn.textContent = '无数据';
              btn.classList.add('avdb-emby-fail');
              toast((tid ? '没有缓存到文章数据 (tid=' + tid + ')' : '没有缓存到在线资源数据 (番号 ' + cardNumber + ')') + ', 请刷新页面重试', 'err');
              setTimeout(() => { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-fail'); }, 3000);
              return;
            }

            // 解析 magnet (在线资源卡片需异步拉取)
            btn.textContent = '拉磁力…';
            btn.disabled = true;
            btn.classList.add('avdb-emby-busy');
            let magnetInfo = null;
            try {
              magnetInfo = await resolveMagnet(article, movie);
            } catch (e) { /* ignore, below will show no magnet */ }
            btn.disabled = false;

            if (!magnetInfo) {
              // 显示弹窗但仍允许? 不 — import_api 需要磁力才能推. 失败.
              const noMagnetSrc = movie ? ('没有取到这部电影的磁力 (movie_id=' + movie.id + ')') : '该文章没有磁力链接';
              btn.textContent = '✗ 无磁力';
              btn.classList.remove('avdb-emby-busy');
              btn.classList.add('avdb-emby-fail');
              toast('入库失败: ' + noMagnetSrc + '. 在线资源请确认已登录 JavDB 账户(设置→个人→在线账户)', 'err');
              setTimeout(() => { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-fail'); }, 5000);
              return;
            }

            // 组装 import payload
            const number = (article && article.number) || (movie && (movie.number || movie.code)) || '';
            const threadId = (article && String(article.tid)) || (movie ? String(movie.id) : '');
            const payload = {
              source: magnetInfo.source,
              number: number,
              thread_id: threadId,
              title: magnetInfo.source === 'article' ? (article.title || '') : (movie.title || movie.origin_title || ''),
              magnet: magnetInfo.magnet,
            };

            const opts = await askKind(payload);
            if (!opts) { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-busy'); return; }

            btn.disabled = true;
            btn.textContent = '提交中…';
            btn.classList.add('avdb-emby-busy');
            try {
              const body = {
                thread_id: payload.thread_id,
                magnet: payload.magnet,
                title: payload.title,
                thread_url: (article && article.detail_url) || (movie ? 'https://javdb.com/v/' + movie.id : ''),
                kind: opts.kind,
              };
              if (opts.category) body.category = opts.category;
              const { taskId } = await callImport(body);
              btn.textContent = '推送中 ' + taskId.slice(0, 8) + '…';
              toast('已提交入库, task_id=' + taskId + '\n链路: 推115 → 落地 → 刮削 → strm → Emby\n可在 import_api 页面(5081) 实时看进度', 'ok');
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
                toast('仍在入库中, 请到 import_api 页面(5081)查看 task_id=' + taskId, 'ok');
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
          frame.appendChild(btn);
        });
      } finally {
        injecting = false;
      }
    });
  }

  // ==========================================================
  //  在线资源「磁力详情页」注入 (online-resources?movie=xxx)
  //  两类磁力逐条注入: 资源库磁力(色花堂/articles) + 在线磁链(javdb/magnets)
  // ==========================================================
  function isMovieDetail() {
    return /\/online-resources(\/|$)/.test(location.pathname) && /[?&]movie=/.test(location.search);
  }
  function movieIdFromUrl() {
    const m = location.search.match(/[?&]movie=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // 同源 fetch 公共头 (夹 JWT)
  function authFetch(url, opts) {
    const headers = Object.assign({}, (opts && opts.headers) || {});
    const tok = getJwtToken();
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    if (opts && opts.body) headers['Content-Type'] = 'application/json';
    return fetch(url, Object.assign({ credentials: 'include' }, opts, { headers }));
  }

  // ---------- 详情页数据加载 ----------
  const detailState = { movieId: null, number: '', articles: [], magnets: [], resources: [] };
  async function loadDetailData(movieId) {
    const st = {
      movieId,
      number: '',
      articles: [],   // 资源库磁力 (本地 article, 含 magnet/tid/category)
      magnets: [],    // 在线磁链 (javdb 标准磁力区, 含 magnet_url)
      resources: [],  // 评论区资源 (javdb comment-resources, 含 magnet/ed2k: resource_url/name)
    };
    // 1) javdb movie -> 番号
    try {
      const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId), { credentials: 'include' });
      const d = await r.json();
      if (d && d.data && d.data.movie) st.number = d.data.movie.number || '';
    } catch (e) { /* ignore */ }
    // 2a) 在线磁链: GET /javdb/movies/{id}/magnets (标准磁力区)
    try {
      const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId) + '/magnets', { credentials: 'include' });
      const d = await r.json();
      const mags = (d && d.data && (d.data.magnets || d.data.items)) || [];
      if (Array.isArray(mags)) st.magnets = mags;
    } catch (e) { /* ignore */ }
    // 2b) 评论区资源: GET /javdb/movies/{id}/comment-resources (playback 播放页等展示的评论区磁力/ed2k)
    try {
      const r = await fetch('/api/v1/javdb/movies/' + encodeURIComponent(movieId) + '/comment-resources', { credentials: 'include' });
      const d = await r.json();
      const res = (d && d.data && (d.data.resources || d.data.items)) || [];
      if (Array.isArray(res)) st.resources = res;
    } catch (e) { /* ignore */ }
    // 3) 资源库磁力: POST /articles/search {keyword:番号}
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

  // 在线磁链候选: 标准磁力区 + 评论区资源, 统一成 { href, name } 列表 (优先标准磁力, 其次评论区)
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

  // 用卡片文本找匹配的在线磁力 (评论资源名/哈希会显示在卡上; 匹配不上 fallback 按序)
  function matchOnlineMagnetByCard(card, candidates, fallbackIndex) {
    const text = (card.textContent || '').trim().toLowerCase();
    // 1) 文本里出现磁力/ed2k 哈希 (btih 40hex 或 ed2k 32hex)
    for (const c of candidates) {
      if (c.key && text.includes(c.key)) return c;
    }
    // 2) 文件名包含匹配 (卡片显示资源名)
    for (const c of candidates) {
      const n = String(c.name || '').toLowerCase().replace(/\s+/g, '');
      if (n && n.length > 4 && text.replace(/\s+/g, '').includes(n)) return c;
    }
    // 3) 按序 fallback
    return candidates[fallbackIndex] || null;
  }

  // ---------- 详情页: 磁力卡片容器 ----------
  // 磁力区 section: "资源库磁力资源" / "在线磁链资源"
  function magnetCardSelector() {
    return 'div.rounded-xl.border';
  }
  function sectionByTitle(titleText) {
    const secs = [...document.querySelectorAll('section.space-y-3')];
    return secs.find((s) => (s.textContent || '').trim().startsWith(titleText));
  }
  // 卡片顺序 (与数据数组按序对应)
  function cardsInSection(sec) {
    if (!sec) return [];
    return [...sec.querySelectorAll(magnetCardSelector())]
      .filter((c) => {
        const cls = (c.className || '').toString();
        return cls.includes('bg-muted/20') && cls.includes('p-3');
      })
      .filter((c) => !c.querySelector('.avdb-emby-btn'));
  }

  // ---------- 详情页按钮逻辑 (逐条) ----------
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
        magnet: magnetInfo.magnet,
      };
      const opts = await askKind(payload);
      if (!opts) { btn.textContent = '→ Emby'; btn.classList.remove('avdb-emby-busy'); return; }

      btn.disabled = true;
      btn.textContent = '提交中…';
      btn.classList.add('avdb-emby-busy');
      try {
        const body = {
          thread_id: payload.thread_id,
          magnet: payload.magnet,
          title: payload.title,
          thread_url: magnetInfo.url || ('https://javdb.com/v/' + movieIdFromUrl()),
          kind: opts.kind,
        };
        if (opts.category) body.category = opts.category;
        const { taskId } = await callImport(body);
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

  // ---------- 详情页: 扫描并注入 ----------
  // 数据加载 promise 去重: 防止 MutationObserver 多次触发时,
  // detailState.movieId 已被赋值但数据还在 await 中, 用空 detailState 注入按钮
  // (导致按钮闭包捕获 cand=null, 点击必报"该条磁力获取不到链接")。
  let detailLoadPromise = null;
  async function ensureDetailData(movieId) {
    if (detailState.movieId !== movieId || !detailLoadPromise) {
      detailState.movieId = movieId;
      detailLoadPromise = loadDetailData(movieId);
    }
    const fresh = await detailLoadPromise;   // 并发触发者共用同一个 promise
    Object.assign(detailState, fresh);
    return detailState;
  }

  async function injectDetailButtons() {
    if (!isMovieDetail()) return;
    const movieId = movieIdFromUrl();
    if (!movieId) return;

    // 数据就绪后再注入 (await 去重 promise)
    const st = await ensureDetailData(movieId);

    const btnInCard = (card) => !!card.querySelector('.avdb-emby-btn');

    // 资源库磁力 section (点击时实时取 articles, 不捕获注入时的快照)
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

    // 在线磁链 section (标准磁力区 + 评论区磁力/ed2k, 点击时实时匹配取磁力)
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

  // ---------- 监听卡片容器变化 (虚拟滚动等) ----------
  const mo = new MutationObserver(() => {
    injectButtons(document);
    if (isMovieDetail()) injectDetailButtons();
  });
  mo.observe(document.body, { childList: true, subtree: true });

  // 初次注入
  setTimeout(() => {
    injectButtons(document);
    injectDetailButtons();
  }, 1200);
  console.log('[AVdb-Emby] userscript v0.3.3 loaded. IMPORT_API=' + IMPORT_API);
})();