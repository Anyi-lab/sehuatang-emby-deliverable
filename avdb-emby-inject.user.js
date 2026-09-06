// ==UserScript==
// @name         AVdb → Emby 一键入库 (色花堂点单版)
// @namespace    sehuatang.emby.deliverable
// @version      0.2.0
// @updateURL    https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-emby-inject.user.js
// @downloadURL  https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/avdb-emby-inject.user.js
// @description  在 AVdb 文章卡片 + 在线资源(online-resources ranking/top/latest) 卡片上注入"→ Emby 入库"按钮。文章卡片直接取缓存 magnet; 在线资源卡片无 magnet, 点击时按番号反查本地库(优先)或拉取 JavDB 磁力, 再推给 import_api (localhost:5081) 全包入库。
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
          <label>分类 (可选, 空=回退 av)</label>
          <input type="text" id="avdb-emby-cat" placeholder="如: 3dh(3D) / fc2 / 剧情 / 无码 ... 留空自动" />
          <div class="row">
            <button class="cancel" id="avdb-emby-cancel">取消</button>
            <button class="go" id="avdb-emby-go">开始入库</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (v) => { overlay.remove(); resolve(v); };
      overlay.querySelector('#avdb-emby-cancel').addEventListener('click', () => close(null));
      overlay.querySelector('#avdb-emby-go').addEventListener('click', () => {
        close({
          kind: overlay.querySelector('#avdb-emby-kind').value,
          category: overlay.querySelector('#avdb-emby-cat').value.trim() || undefined,
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
        const cards = (root || document).querySelectorAll('[data-image-frame]');
        cards.forEach((card) => {
          // 已注入跳过; 在线资源页的订阅徽章卡是"订阅容器"不是资源卡, 同样跳过
          if (card.querySelector('.avdb-emby-btn')) return;
          const img = card.querySelector('img[src*="/articles/"], img[data-src*="/articles/"]');
          let tid = img ? tidFromUrl(img.currentSrc || img.src) : null;
          let cardNumber = null;
          if (!tid) {
            // 在线资源卡片: 封面是 javdb 外链, 从 DOM 找番号 (排除订阅徽章卡: 其无番号文本, 有 badge 元素)
            const isSubBadge = !!card.querySelector('.resource-card-top-left-badge, .resource-card-top-right-badge');
            if (!isSubBadge) cardNumber = numberFromCard(card);
            else card.classList.add('avdb-emby-skip');
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
          card.appendChild(btn);
        });
      } finally {
        injecting = false;
      }
    });
  }

  // ---------- 监听卡片容器变化 (虚拟滚动等) ----------
  const mo = new MutationObserver(() => injectButtons(document));
  mo.observe(document.body, { childList: true, subtree: true });

  // 初次注入
  setTimeout(() => injectButtons(document), 1200);
  console.log('[AVdb-Emby] userscript v0.2.0 loaded. IMPORT_API=' + IMPORT_API);
})();