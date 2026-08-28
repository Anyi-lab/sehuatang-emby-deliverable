// ==UserScript==
// @name         色花堂链接级一键入库(逐条磁力/ed2k) + 元数据补充
// @namespace    sehuatang-import
// @version      1.9.6
// @updateURL    https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/src/userscript/sehuatang_import.user.js
// @downloadURL  https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/src/userscript/sehuatang_import.user.js
// @source       https://github.com/Anyi-lab/sehuatang-emby-deliverable
// @description  色花堂链接级一键入库(逐条磁力/ed2k)。每条链接手工选择类型: "番号"=影片(推→等→清小文件→strm→MDC刮削→Emby刷新→预热; MDC失败/不完整由油猴📤补充); "非番号"=剧集(推→等→重命名thread_xxx_S01E01..→SmartStrm生成strm→油猴📤补充元数据→Emby刷新+预热, 即使1个视频也是剧集)。右下角磁力导航面板列出全部链接可点击快速滚动定位。v1.6.3: 修复手机版磁力被 <wbr>/<br> 等空元素拆成多段文本节点导致的漏识别(全文本拼接兜底+iframe兜底); v1.7.0: 新增手动输入磁力/ed2k链接入库对话框(导航面板✏️按钮, 支持多条, 提交到当前thread); v1.8.0: 新增元数据补充——提取帖子标题/简介/图片, 由本机浏览器下载图片后上传服务器生成 Emby 海报与简介 (导航面板📤按钮); v1.8.1: 元数据图片抓取优化——只抓静态图片(jpg/png等, 排除gif/webp/svg/ico), 懒加载取真实地址(data-original/data-src), 像素尺寸过滤(太小的表情/图标/头像不抓, 超高清原图不抓); v1.8.2: 描述同步新流程(去MySQL, 剧集文件名 thread_xxx_S01E01, MDC失败/剧集均待油猴补充元数据); v1.8.3: API_BASE 改 http(未配https), 悬浮面板新增📋一键跳转任务监控页按钮; v1.8.4: 手机版图片识别兼容——Discuz 附件真实地址 file/zoomfile 属性、选择器抓不到时全量图片兜底、iframe 内正文跨框架收集、相对路径补全域名; v1.8.7: 简介提取终极兜底——按"含磁力/ed2k 链接的文本节点"定位正文容器(色花堂正文必含下载链接, 不受模板类名影响), 简介提取失败时面板显示具体原因。 v1.9.0: 图片改为用户手工选择(候选图网格点选, 支持动图gif/webp/avif, 最多9张), 已选支持📌海报/↑↓调序/✕移除, 上传保留原图格式(mime)不再强制jpg, 移动端触控优化。 v1.9.1: 候选图排除小尺寸表情/图标(像素<200或未加载时CSS尺寸<100x80), 恢复表情/笑脸区域跳过。 v1.9.2: 标题清洗——去掉发布者/来源前缀标记(自转/115ED2K等, 保留【Omar盘点】类系列名), 去掉体积/配额后缀【1.87G/25P+18V/1配额】, 去掉尾部分区与论坛后缀(- 综合讨论区 - 98堂[原色花堂])。 v1.9.5: 磁力导航面板每条磁力新增📋复制按钮(复制完整干净链接含&dn), 标签优先显示文件名; v1.9.6: 取消正文磁力链接旁"🎬入库"按钮(避免页面出现两个入库), 改为磁力导航面板每条磁力🚀一键入库(点🚀选类型: 番号/非番号)。
// @author       QwenPaw
// @match        *://sehuatang.net/*
// @match        *://sehuatang.org/*
// @match        *://sehuatang.net/*
// @match        *://sehuatang.org/*
// @match        *://www.sehuatang.org/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @connect      sehuatang.net
// @connect      *.sehuatang.net
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // ===== 可配置 =====
    const API_BASE = 'http://127.0.0.1:5081'; // 本机 import-api (WSL localhost 转发, 未配 https)
    const POLL_MS = 6000;                          // 状态轮询间隔
    const MAX_LABEL = 44;                          // 导航/按钮标签截断长度
    // ==================

    function getThreadId() {
        let m = location.pathname.match(/thread-(\d+)/);
        if (m) return m[1];
        m = location.search.match(/[?&]tid=(\d+)/);
        if (m) return m[1];
        return null;
    }

    // ===== 链接规范化 =====
    function normLink(s) {
        if (!s) return null;
        s = s.trim()
            .replace(/&amp;/g, '&')
            .replace(/&nbsp;/g, ' ')
            .replace(/[\u200b\u200c\u200d\ufeff]/g, '');
        if (s.startsWith('magnet:')) {
            const m = s.match(/^magnet:\?xt=urn:btih:([0-9a-fA-F]{32,40})/);
            return m ? 'magnet:?xt=urn:btih:' + m[1] : null;
        }
        if (s.startsWith('ed2k://')) {
            const m = s.match(/^ed2k:\/\/\|file\|[^|]*\|\d+\|[0-9a-fA-F]{32}\|\/?/);
            return m ? m[0] : null;
        }
        return null;
    }

    // ===== 收集链接与锚点 =====
    let items = [];
    const seenNorm = new Set();

    function labelFrom(s) {
        if (!s) return '链接';
        let t = s.replace(/\s+/g, ' ').trim();
        if (t.startsWith('magnet:')) {
            const dn = t.match(/[?&]dn=([^&\s🎬]+)/);
            if (dn) {
                const fn = decodeURIComponent(dn[1]);
                return fn.length > MAX_LABEL ? fn.slice(0, MAX_LABEL) + '…' : fn;
            }
            const bt = t.match(/btih:([0-9a-fA-F]{40})/);
            return bt ? '磁力 …' + bt[1].slice(-8) : '磁力链接';
        }
        if (t.startsWith('ed2k://')) {
            const m = t.match(/^\|file\|([^|]*)\|/);
            const fn = m ? m[1] : 'ed2k链接';
            return fn.length > MAX_LABEL ? fn.slice(0, MAX_LABEL) + '…' : fn;
        }
        return t.length > MAX_LABEL ? t.slice(0, MAX_LABEL) + '…' : t;
    }

    function addItem(norm, el, raw) {
        if (!norm || seenNorm.has(norm)) return;
        seenNorm.add(norm);
        // raw 存原始完整文本(清洗掉 🎬/入库 杂质, 保留 &dn 后缀)
        const clean = String(raw || norm).replace(/\s*🎬?\s*入库/g, '').trim();
        items.push({ norm: norm, el: el || null, label: labelFrom(clean || norm), raw: clean || norm });
    }

    function collectLinks() {
        items = [];
        seenNorm.clear();

        document.querySelectorAll('a[href]').forEach(a => {
            const n = normLink(a.getAttribute('href') || '');
            if (n) addItem(n, a, a.textContent || n);
        });

        document.querySelectorAll('input[type="text"], input[type="hidden"], textarea').forEach(el => {
            const n = normLink(el.value || '');
            if (n) addItem(n, el, n);
        });

        document.querySelectorAll('.blockcode li, code, pre').forEach(el => {
            const n = normLink(el.textContent || '');
            if (n) addItem(n, el, el.textContent || n);
        });

        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const t = node.nodeValue || '';
                if (!t) return NodeFilter.FILTER_REJECT;
                if (node.parentNode && node.parentNode.closest && node.parentNode.closest('#sht-import-wrap')) {
                    return NodeFilter.FILTER_REJECT;
                }
                return /magnet:\S+|ed2k:\/\/\|file\|/.test(t) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        });
        let node;
        while ((node = walker.nextNode())) {
            const n = normLink((node.nodeValue || '').match(/magnet:\S+|ed2k:\/\/\|file\|[^|\n]*\|\d+\|[0-9a-fA-F]{32}\|\/?/g)?.find(x => normLink(x)) || '');
            if (n) {
                const p = node.parentNode;
                if (p && p.nodeType === 1) addItem(n, p, node.nodeValue || n);
            }
        }

        try {
            const bodyText = document.body.textContent || '';
            if (bodyText && bodyText.length < 3000000) {
                const re = /magnet:\?xt=urn:btih:[0-9a-fA-F]{32,40}|ed2k:\/\/\|file\|[^|\n]*\|\d+\|[0-9a-fA-F]{32}\|\/?/g;
                let off = 0, curNode = null;
                const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                curNode = tw.nextNode();
                const advanceTo = (targetOff) => {
                    while (curNode) {
                        const len = (curNode.nodeValue || '').length;
                        if (off + len > targetOff) return curNode;
                        off += len;
                        curNode = tw.nextNode();
                    }
                    return null;
                };
                let m;
                while ((m = re.exec(bodyText)) !== null) {
                    const norm = normLink(m[0]);
                    if (!norm || seenNorm.has(norm)) continue;
                    let el = null;
                    try {
                        const tn = advanceTo(m.index);
                        if (tn && tn.parentNode && tn.parentNode.nodeType === 1 &&
                            !tn.parentNode.closest('script, style')) {
                            el = tn.parentNode;
                        }
                    } catch (e) {}
                    addItem(norm, el, m[0]);
                }
            }
        } catch (e) {}

        document.querySelectorAll('iframe').forEach(ifr => {
            try {
                const doc = ifr.contentDocument;
                if (!doc || !doc.body) return;
                const t = doc.body.textContent || '';
                if (!t || t.length > 3000000) return;
                const re = /magnet:\?xt=urn:btih:[0-9a-fA-F]{32,40}|ed2k:\/\/\|file\|[^|\n]*\|\d+\|[0-9a-fA-F]{32}\|\/?/g;
                let m;
                while ((m = re.exec(t)) !== null) {
                    const norm = normLink(m[0]);
                    if (norm) addItem(norm, ifr, m[0]);
                }
            } catch (e) {}
        });
        return items;
    }

    // ===== API =====
    function api(method, path, body) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: method,
                url: API_BASE + path,
                data: body ? JSON.stringify(body) : undefined,
                headers: { 'Content-Type': 'application/json' },
                timeout: 120000,
                onload: (res) => {
                    try { resolve(JSON.parse(res.responseText)); }
                    catch (e) { reject(new Error('响应解析失败: ' + String(res.responseText).slice(0, 120))); }
                },
                onerror: (res) => reject(new Error('网络错误: ' + (res.error || '无法连接 ' + API_BASE))),
                ontimeout: () => reject(new Error('请求超时'))
            });
        });
    }

    function stepLabel(s) {
        const st = s.status || '', step = s.step || '';
        if (st === 'done') return '✅ 完成';
        if (st === 'failed') return '❌ 失败';
        if (st === 'queued') return '排队中';
        if (step === 'push') return '🚀 推送115';
        if (step === 'wait') return '⏳ 等待下载';
        if (step === 'scrape') return '🪄 刮削中';
        if (step === 'strm' || step === 'nfo') return '🔗 生成strm/刮削';
        if (step === 'scan') return '📡 Emby扫库';
        return '处理中';
    }

    // ===== 提交入库(单条/全部/手动) + 轮询 =====
    async function submitImport(links, kind, btn, onFinal) {
        if (btn) { btn.disabled = true; btn.textContent = '提交中…'; }
        try {
            const body = {
                thread_id: getThreadId(),
                magnet: links.join('\n'),
                title: document.title,
                thread_url: location.href,
                kind: kind || undefined
            };
            const r = await api('POST', '/api/import', body);
            if (!r.task_id) throw new Error(r.error || '未返回 task_id');

            let final = null;
            for (let i = 0; i < 120; i++) {
                if (btn) btn.textContent = stepLabel({ status: 'queued', step: '' });
                await new Promise(res => setTimeout(res, POLL_MS));
                const s = await api('GET', '/api/import/status?task_id=' + r.task_id);
                if (s.status === 'done' || s.status === 'failed') { final = s; break; }
                if (btn) btn.textContent = stepLabel(s);
            }

            if (!final) {
                if (btn) { btn.textContent = '⏳ 进行中'; btn.disabled = false; }
                toast('12分钟内未结束, 可到 ' + API_BASE + '/tasks 查看', true);
                if (onFinal) onFinal(null);
                return;
            }
            if (final.status === 'done') {
                if (btn) { btn.textContent = '✅ 已入库'; btn.classList.add('sht-done'); }
                toast('入库完成: ' + (final.msg || '') + ' — 可点右下角📤上传元数据', false);
            } else {
                if (btn) { btn.textContent = '❌ 失败'; btn.classList.add('sht-fail'); btn.disabled = false; }
                toast('入库失败: ' + (final.msg || '未知错误'), true);
            }
            if (onFinal) onFinal(final);
        } catch (e) {
            if (btn) { btn.textContent = '❌ 失败'; btn.classList.add('sht-fail'); btn.disabled = false; }
            toast('请求失败: ' + e.message, true);
            if (onFinal) onFinal(null);
        }
    }

    // ===== 迷你 toast =====
    let toastBox = null;
    function toast(msg, isErr) {
        if (!toastBox) {
            toastBox = document.createElement('div');
            toastBox.id = 'sht-toast';
            document.head.appendChild(document.createElement('style')).textContent =
                '#sht-toast{position:fixed;top:16px;right:16px;z-index:2147483647;max-width:380px;background:rgba(15,23,42,.95);color:#e2e8f0;font-size:13px;line-height:1.5;padding:10px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.4);word-break:break-all;opacity:0;transform:translateY(-8px);transition:all .25s}';
            document.body.appendChild(toastBox);
        }
        toastBox.textContent = (isErr ? '❌ ' : '✅ ') + msg;
        toastBox.style.opacity = '1';
        toastBox.style.transform = 'translateY(0)';
        clearTimeout(toastBox._t);
        toastBox._t = setTimeout(() => { toastBox.style.opacity = '0'; toastBox.style.transform = 'translateY(-8px)'; }, 5000);
    }

    // ===== 高亮跳转 =====
    function flashTo(el) {
        if (!el) return;
        let target = el;
        while (target && target !== document.body && target.offsetParent === null) {
            target = target.parentNode;
        }
        if (!target || target === document.body) target = el;

        try { target.scrollIntoView({ behavior: 'auto', block: 'center' }); } catch (e) {}

        const imgs = [...document.images].filter(img => !img.complete);
        const wait = imgs.length
            ? Promise.all(imgs.map(img => new Promise(res => {
                img.addEventListener('load', res, { once: true });
                img.addEventListener('error', res, { once: true });
                setTimeout(res, 2500);
              })))
            : Promise.resolve();

        wait.then(() => {
            setTimeout(() => {
                const rect = target.getBoundingClientRect();
                const top = rect.top + (window.pageYOffset || document.documentElement.scrollTop || 0)
                    - window.innerHeight / 2 + rect.height / 2;
                window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
                setTimeout(() => {
                    const r2 = target.getBoundingClientRect();
                    const t2 = r2.top + (window.pageYOffset || document.documentElement.scrollTop || 0)
                        - window.innerHeight / 2 + r2.height / 2;
                    window.scrollTo({ top: Math.max(0, t2), behavior: 'smooth' });
                }, 120);
                target.style.outline = '3px solid #ffd166';
                target.style.outlineOffset = '2px';
                target.style.transition = 'outline .3s';
                setTimeout(() => { target.style.outline = ''; }, 2200);
            }, 50);
        });
    }

    // ===== 链接可见文本清洗: 只去掉 "🎬 入库" 等粘贴杂质, 保留完整链接(含 &dn= 后缀) =====
    // v1.9.4: 改为文本节点级清洗, 绝不整体替换父容器 textContent (避免拍平链接/图片/按钮结构)
    function cleanLinkText(el) {
        if (!el || el.nodeType !== 1) return;
        const href = (el.getAttribute && el.getAttribute('href')) || '';
        // a 链接: href 里的杂质也一并清掉(保留完整参数含 &dn)
        if (href && (href.startsWith('magnet:') || /^ed2k:\/\//.test(href))) {
            const h = href.replace(/\s*🎬?\s*入库/g, '').trim();
            if (h && h !== href) el.setAttribute('href', h);
            // 仅当 a 只有一个文本子节点时直接改文本; 复杂子结构交给文本节点级清洗
            if (el.childNodes.length === 1 && el.firstChild.nodeType === 3) {
                const t = (el.firstChild.nodeValue || '').replace(/\s*🎬?\s*入库/g, '').trim();
                if (t && t !== el.firstChild.nodeValue) el.firstChild.nodeValue = t;
            }
            return;
        }
        // 纯文本/容器: 只清洗其直接文本子节点, 不替换容器整体文本
        el.childNodes.forEach(cn => {
            if (cn.nodeType === 3) {
                const t = (cn.nodeValue || '').replace(/\s*🎬?\s*入库/g, '').trim();
                if (t && t !== cn.nodeValue) cn.nodeValue = t;
            }
        });
    }

    // ===== 全页面清洗: 只改文本节点, 保留所有 DOM 结构 =====
    function cleanPageMagnetTexts() {
        try {
            // 1) a 磁力/ed2k 链接: 清 href + 单一文本子节点
            document.querySelectorAll('a[href^="magnet:"], a[href^="ed2k://"]').forEach(a => {
                cleanLinkText(a);
            });
            // 2) 文本节点级清洗: 凡含磁力/ed2k 特征或含 🎬入库 杂质的文本节点, 逐节点清洗
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    const t = node.nodeValue || '';
                    if (!t) return NodeFilter.FILTER_REJECT;
                    const p = node.parentNode;
                    if (p && p.closest && p.closest('#sht-import-wrap')) return NodeFilter.FILTER_REJECT;
                    if (p && p.tagName === 'SCRIPT') return NodeFilter.FILTER_REJECT;
                    if (p && p.classList && p.classList.contains('sht-link-btn')) return NodeFilter.FILTER_REJECT;
                    // 命中: 磁力/ed2k 特征 | 🎬入库 按钮杂质
                    if (/magnet:\?xt=urn:btih:|ed2k:\/\/\|file\|/.test(t)) return NodeFilter.FILTER_ACCEPT;
                    if (/🎬/.test(t) && /入库/.test(t)) return NodeFilter.FILTER_ACCEPT;
                    return NodeFilter.FILTER_REJECT;
                }
            });
            const nodes = [];
            let n;
            while ((n = walker.nextNode())) nodes.push(n);
            nodes.forEach(node => {
                let t = node.nodeValue || '';
                let c = t.replace(/\s*🎬\s*入库/g, '');            // 删 🎬入库 按钮杂质
                if (/magnet:\?xt=urn:btih:|ed2k:\/\/\|file\|/.test(c)) {
                    c = c.replace(/\s*入库/g, '');                 // 磁力/ed2k 段里的 入库 杂质
                }
                if (c !== t) node.nodeValue = c;
            });
        } catch (e) {}
    }

    // ===== 链接清洗(不再在正文插入按钮, 入库统一走悬浮导航面板) =====
    let lastNavKey = '';
    function attachButtons() {
        items.forEach((it) => {
            const el = it.el;
            if (!el || el.nodeType !== 1) return;
            cleanLinkText(el);
        });
    }

    // ===== 单条入库类型选择弹层 =====
    let pop = null;
    function openKindPanel(it, btn, ev) {
        if (pop) pop.remove();
        pop = document.createElement('div');
        pop.className = 'sht-pop';
        const rect = (ev.target || btn).getBoundingClientRect();
        pop.innerHTML =
            '<div class="sht-pop-title">选择资源类型</div>' +
            '<button class="sht-pop-kind" data-kind="fanhao">番号(电影)</button>' +
            '<button class="sht-pop-kind" data-kind="non_fanhao">非番号(剧集)</button>' +
            '<div class="sht-pop-sub">' + it.label + '</div>';
        document.body.appendChild(pop);
        const pw = pop.offsetWidth || 180;
        let left = rect.left;
        let top = rect.bottom + 6;
        if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
        if (top + pop.offsetHeight > window.innerHeight - 8) top = rect.top - pop.offsetHeight - 6;
        pop.style.left = left + 'px';
        pop.style.top = top + 'px';
        pop.querySelectorAll('.sht-pop-kind').forEach(k => {
            k.addEventListener('click', () => {
                const kind = k.dataset.kind;
                pop.remove(); pop = null;
                submitImport([it.norm], kind, btn);
            });
        });
        setTimeout(() => {
            document.addEventListener('click', function h(e) {
                if (pop && !pop.contains(e.target)) { pop.remove(); pop = null; }
                document.removeEventListener('click', h);
            });
        }, 0);
    }

    // ===== 手动输入磁力/ed2k 入库对话框 (v1.7.0) =====
    function parseManualLinks(text) {
        if (!text) return [];
        const seen = new Set(), out = [];
        const re = /magnet:\?xt=urn:btih:[0-9a-fA-F]{32,40}|ed2k:\/\/\|file\|[^|\n]*\|\d+\|[0-9a-fA-F]{32}\|\/?/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            const n = normLink(m[0]);
            if (n && !seen.has(n)) { seen.add(n); out.push(n); }
        }
        return out;
    }

    let manualMask = null;
    function openManualDialog() {
        if (manualMask) return;
        manualMask = document.createElement('div');
        manualMask.id = 'sht-mask';
        manualMask.innerHTML =
            '<div id="sht-modal">' +
            '  <div id="sht-modal-title">✏️ 手动输入链接入库<span id="sht-modal-tid"></span><button id="sht-modal-close" title="关闭">✕</button></div>' +
            '  <div id="sht-modal-sub">粘贴磁力(<b>magnet:</b>)或电驴(<b>ed2k://</b>)链接，支持多条；提交后入库到<strong>当前主题 thread</strong> 目录下</div>' +
            '  <textarea id="sht-modal-input" rows="5" placeholder="magnet:?xt=urn:btih:...&#10;ed2k://|file|名称|大小|hash|/"></textarea>' +
            '  <div id="sht-modal-detect"></div>' +
            '  <div><span class="sht-modal-kind-label">资源类型：</span>' +
            '    <button class="sht-modal-kind active" data-kind="fanhao">番号(电影)</button>' +
            '    <button class="sht-modal-kind" data-kind="non_fanhao">非番号(剧集)</button>' +
            '  </div>' +
            '  <div id="sht-modal-status"></div>' +
            '  <div class="sht-modal-actions">' +
            '    <button id="sht-modal-cancel">取消</button>' +
            '    <button id="sht-modal-ok">🚀 提交入库</button>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(manualMask);

        const tidEl = manualMask.querySelector('#sht-modal-tid');
        tidEl.textContent = 'thread: ' + (getThreadId() || '?');

        const input = manualMask.querySelector('#sht-modal-input');
        const detect = manualMask.querySelector('#sht-modal-detect');
        const statusEl = manualMask.querySelector('#sht-modal-status');
        const okBtn = manualMask.querySelector('#sht-modal-ok');
        let kind = 'fanhao';

        const updateDetect = () => {
            const links = parseManualLinks(input.value);
            if (!links.length) {
                detect.textContent = input.value.trim() ? '⚠️ 未识别到有效磁力/ed2k 链接' : '';
                detect.className = input.value.trim() ? 'err' : '';
            } else {
                detect.textContent = '✅ 识别到 ' + links.length + ' 条链接' +
                    (links.length <= 3 ? ': ' + links.map(l => l.slice(0, 48) + (l.length > 48 ? '…' : '')).join(' | ') : '');
                detect.className = 'ok';
            }
        };
        input.addEventListener('input', updateDetect);
        updateDetect();

        manualMask.querySelectorAll('.sht-modal-kind').forEach(k => {
            k.addEventListener('click', () => {
                manualMask.querySelectorAll('.sht-modal-kind').forEach(x => x.classList.remove('active'));
                k.classList.add('active');
                kind = k.dataset.kind;
            });
        });

        const close = () => { if (manualMask) { manualMask.remove(); manualMask = null; } };
        manualMask.querySelector('#sht-modal-close').addEventListener('click', close);
        manualMask.querySelector('#sht-modal-cancel').addEventListener('click', close);
        manualMask.addEventListener('click', (e) => { if (e.target === manualMask) close(); });

        okBtn.addEventListener('click', async () => {
            if (okBtn.disabled) return;
            const links = parseManualLinks(input.value);
            if (!links.length) {
                statusEl.textContent = '⚠️ 未识别到有效磁力/ed2k 链接，请检查输入';
                return;
            }
            okBtn.disabled = true;
            statusEl.textContent = '';
            await submitImport(links, kind, statusEl, (final) => {
                if (final && final.status === 'done') {
                    close();
                } else if (final) {
                    statusEl.textContent = '❌ 入库失败: ' + (final.msg || '未知错误');
                    okBtn.disabled = false;
                } else {
                    okBtn.disabled = false;
                }
            });
            if (!manualMask) return;
            okBtn.disabled = false;
        });

        setTimeout(() => input.focus(), 50);
    }

    // ===== 元数据补充 (v1.8.0): 提取帖子信息 + 下载图片 + 上传服务器 =====
    // v1.8.4: 手机版兼容——Discuz 附件真实地址在 file/zoomfile 属性(PC 是 data-original);
    // 选择器抓不到时兜底全量图片; 帖子正文在 iframe 内时跨 iframe 收集; 相对路径补全域名
    function resolveUrl(u) {
        if (!u) return '';
        try { return new URL(u, location.href).href; } catch (e) { return u; }
    }

    // v1.9.2: 标题清洗 - 去掉发布者/来源前缀标记(自转/115ED2K等黑名单, 可多个连续),
    // 体积/配额后缀【1.87G/25P+18V/1配额】, 以及尾部分区与论坛后缀(- 综合讨论区 - 98堂[原色花堂])
    // 保留标题中间有意义内容(含【Omar盘点】这类系列名标记)
    function cleanThreadTitle(raw) {
        if (!raw) return '';
        let t = String(raw).trim();
        // 1) 尾部: 论坛/站点名 (Discuz / 98堂[原色花堂] / 色花堂 / sehuatang...)
        t = t.replace(/-\s*Powered\s+by\s+Discuz!.*$/i, '');
        t = t.replace(/-\s*(?:98堂|色花堂|原色花堂|sehuatang(?:\.net|\.org)?)\s*[^\]]*\]?\s*$/i, '');
        // 2) 尾部: 分区名 (综合讨论区 / 自拍分享区 / 中文字幕区 等, 以 区/版/频道 结尾)
        t = t.replace(/\s*-\s*[^\-]{1,30}?(?:讨论区|分享区|自拍区|字幕区|中字区|资源区|下载区|综合区|原创区|转帖区|求片区|回收站|频道|板块)\s*$/i, '');
        // 3) 尾部: 体积/配额信息 【1.87G/25P+18V/1配额】【3.2G/10P】【2V】 等
        t = t.replace(/\s*【[^】]*?(?:\d+(?:\.\d+)?\s*(?:G|GB|MB|KB|P|V|张|图|集|部)|配额|P\+|V\+)[^】]*?】\s*$/i, '');
        // 4) 开头: 连续删除黑名单发布者/来源标记 【自转】【115ED2K】...
        const MARK_RE = /^【[^】]{1,16}】/;
        const BAD_MARK = /^(自转|转载|转帖|转贴|原创|自译|自购|搬运|补档|重发|推荐|整理|收集|115ED2K|115|ED2K|磁力|合集)$/i;
        for (;;) {
            const mm = t.match(MARK_RE);
            if (!mm) break;
            const inner = mm[0].slice(1, -1).trim();
            if (!BAD_MARK.test(inner)) break;
            t = t.slice(mm[0].length).trim();
        }
        // 5) 清理空白
        return t.replace(/\s+/g, ' ').trim();
    }

    function collectMeta() {
        let title = '';
        // 1) 桌面版主题标题
        const t1 = document.querySelector('#thread_subject');
        if (t1) title = (t1.textContent || '').trim();
        // 2) 通用 h1/.ts/.xst (部分模板/手机版)
        if (!title) {
            const h1 = document.querySelector('h1.ts, .ts, .xst, h1');
            if (h1) title = (h1.textContent || '').trim();
        }
        // 3) og:title (部分模板)
        if (!title) {
            const og = document.querySelector('meta[property="og:title"]');
            if (og) title = (og.content || '').trim();
        }
        // 4) document.title: 只剥离站点后缀, 不再按第一个分隔符截断 (修复标题含 - 被截断)
        if (!title) {
            let t = document.title || '';
            t = t.replace(/-\s*Powered\s+by\s+Discuz!.*$/i, '');
            t = t.replace(/-\s*色花堂.*$/i, '');
            t = t.replace(/-\s*(手机版|移动版|触屏版).*$/i, '');
            title = t.trim();
        }

        let desc = '';
        // 正文容器: 桌面 td.t_f/.pcb, 手机版 postmessage_xxx 等
        let post = document.querySelector('#postlist .t_f, td.t_f, .pcb .t_f, #postlist .pcb, .postmessage, [id^="postmessage_"]');
        if (!post) {
            // 手机版正文可能在 iframe 内 (与图片收集一致)
            try {
                document.querySelectorAll('iframe').forEach(ifr => {
                    let doc = null;
                    try { doc = ifr.contentDocument; } catch (e) {}
                    if (doc && !post) {
                        post = doc.querySelector('[id^="postmessage_"], .t_f, td.t_f, .pcb .t_f, .postmessage');
                    }
                });
            } catch (e) {}
        }
        if (!post) {
            // 最终兜底: 找 class/id 含 t_f/postmessage 的候选里文本最长者 (正文最长)
            try {
                const cands = [...document.querySelectorAll('[id^="postmessage_"], [class*="t_f"], [class*="postmessage"]')];
                post = cands.sort((a, b) => (b.textContent || '').length - (a.textContent || '').length)[0] || null;
            } catch (e) { post = null; }
        }
        if (!post) {
            // v1.8.7 终极兜底: 色花堂正文必含磁力/ed2k 链接, 用含链接的文本节点定位正文容器
            try {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const nv = walker.currentNode.nodeValue || '';
                    if (/magnet:\S+|ed2k:\/\/\|file\|/.test(nv)) {
                        let el = walker.currentNode.parentElement;
                        // 向上找正文特征容器 (class/id 含 t_f/message/content/pcb/post), 找不到就用文本直接父元素
                        while (el && el !== document.body &&
                               !/(t_f|message|content|pcb|post)/i.test((el.className || '') + ' ' + (el.id || ''))) {
                            el = el.parentElement;
                        }
                        post = (el && el !== document.body) ? el : walker.currentNode.parentElement;
                        break;
                    }
                }
            } catch (e) { post = post || null; }
        }
        let descEmptyReason = '';
        if (post) {
            desc = post.innerText || post.textContent || '';
            desc = desc
                .replace(/magnet:\?xt=urn:btih:[0-9a-fA-F]{32,40}[\s\S]*?(?=\n|$)/g, ' ')
                .replace(/ed2k:\/\/\|file\|[^|\n]*\|\d+\|[0-9a-fA-F]{32}\|\/?/g, ' ')
                .replace(/https?:\/\/\S+/g, ' ')
                .replace(/[ \t]+/g, ' ')
                .replace(/\n{3,}/g, '\n\n')
                .trim()
                .slice(0, 800);
            if (!desc) descEmptyReason = '正文容器已找到但清理后为空';
        } else {
            descEmptyReason = '未找到正文容器(所有选择器+磁力定位均未命中)';
        }

        const imgs = [];
        const seen = new Set();
        const sel = '#postlist img, .pcb img, td.t_f img, .t_f img, .postmessage img';
        let nodes = [];
        try { nodes = [...document.querySelectorAll(sel)]; } catch (e) { nodes = []; }
        if (!nodes.length) {
            // 手机版/模板结构不同: 兜底全量图片 (后续用格式/尺寸/位置过滤)
            try { nodes = [...document.images]; } catch (e) { nodes = []; }
        }
        // 同域 iframe 兜底 (手机版正文可能渲染在 iframe 内)
        try {
            document.querySelectorAll('iframe').forEach(ifr => {
                let doc = null;
                try { doc = ifr.contentDocument; } catch (e) {}
                if (doc) {
                    const extra = doc.querySelectorAll('img');
                    extra.forEach(x => { if (!nodes.includes(x)) nodes.push(x); });
                }
            });
        } catch (e) {}

        nodes.forEach((img) => {
            if (imgs.length >= 30) return; // 候选图上限 (用户手工挑选, 放宽数量)
            // 跳过明确装饰区域(头像/签名/表情/图标/广告等)
            if (img.closest('.avatar, .signatures, .emoji, em, .smiley, .pls, .authi, .qq, .ad, .adv')) return;
            // 真实地址优先: Discuz 附件 file/zoomfile + 懒加载 data-* + 常规 (v1.8.4 手机版)
            const src = resolveUrl(
                img.getAttribute('file') || img.getAttribute('zoomfile') ||
                img.getAttribute('data-file') || img.getAttribute('data-url') ||
                img.getAttribute('data-original') || img.getAttribute('data-src') ||
                img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '');
            if (!src) return;
            // 不抓 base64 占位图
            if (src.startsWith('data:')) return;
            // 只排除矢量/图标格式 (动图 gif/webp/avif 保留, 交给用户选择)
            if (/\.(svg|ico)(\?|#|$)/i.test(src)) return;
            // 小尺寸过滤: 排除表情包/小图标 (像素尺寸; 未加载时用 CSS 渲染尺寸粗过滤)
            const nw = img.naturalWidth || img.width || 0;
            const nh = img.naturalHeight || img.height || 0;
            if (nw > 0 && nw < 200) return;
            if (nh > 0 && nh < 150) return;
            if (nw === 0) {
                const cw = img.clientWidth || 0, ch = img.clientHeight || 0;
                if (cw > 0 && (cw < 100 || ch < 80)) return;
            }
            if (seen.has(src)) return;
            seen.add(src);
            imgs.push({ src: src, poster: false });
        });
        // v1.9.2: 统一清洗标题(去标记/配额/分区/论坛后缀)
        title = cleanThreadTitle(title);
        return { title: title, desc: desc, descEmptyReason: descEmptyReason, images: imgs };
    }

    // v1.8.5: 手机端兼容 - blob 失败降级 arraybuffer, 显式 base64 转换
    // v1.9.0: 按 URL 推断图片 mime, 动图(gif/webp/avif)保留原格式, 不再一律转 jpeg
    function mimeForUrl(url) {
        const m = (url || '').match(/\.(gif|jpe?g|png|webp|bmp|avif)(\?|#|$)/i);
        if (m) {
            const ext = m[1].toLowerCase();
            return (ext === 'jpg' || ext === 'jpeg') ? 'image/jpeg' : 'image/' + ext;
        }
        return 'image/jpeg';
    }
    function dataUrlMime(dataUrl) {
        const m = String(dataUrl || '').match(/^data:([^;,]+)/);
        return m ? m[1] : 'image/jpeg';
    }
    function bufToBase64(buf, mime) {
        let bin = '';
        const CH = 0x8000;
        const u8 = new Uint8Array(buf);
        for (let i = 0; i < u8.byteLength; i += CH) {
            bin += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + CH, u8.byteLength)));
        }
        return 'data:' + (mime || 'image/jpeg') + ';base64,' + btoa(bin);
    }

    function downloadImgBase64(url) {
        return new Promise((resolve, reject) => {
            const mime = mimeForUrl(url);
            GM_xmlhttpRequest({
                method: 'GET',
                url: url,
                responseType: 'blob',
                headers: { 'Referer': location.origin + '/' },
                timeout: 30000,
                onload: (res) => {
                    try {
                        if (res.status >= 400) return reject(new Error('HTTP ' + res.status));
                        const blob = res.response;
                        // 优先 blob (桌面端 FileReader; readAsDataURL 自带正确 mime)
                        if (blob && typeof blob.size === 'number' && blob.size > 0) {
                            if (blob.size > 10 * 1024 * 1024) return reject(new Error('图片>10MB跳过'));
                            const fr = new FileReader();
                            fr.onload = () => resolve({ data: fr.result, mime: dataUrlMime(fr.result) });
                            fr.onerror = () => reject(new Error('Blob读取失败'));
                            fr.readAsDataURL(blob);
                            return;
                        }
                        // 降级: 手机端可能不支持 blob responseType, 响应是 ArrayBuffer
                        let ab = res.response;
                        if (!ab || typeof ab.byteLength !== 'number' || !ab.byteLength) {
                            // 文本响应(CF 拦截页/防盗链提示/HTML 错误页)
                            if (typeof res.responseText === 'string' && res.responseText) {
                                if (/^\s*</.test(res.responseText.slice(0, 200))) return reject(new Error('非图片响应(拦截页/防盗链)'));
                                return reject(new Error('响应非二进制'));
                            }
                            return reject(new Error('空响应'));
                        }
                        if (ab.byteLength > 10 * 1024 * 1024) return reject(new Error('图片>10MB跳过'));
                        resolve({ data: bufToBase64(ab, mime), mime: mime });
                    } catch (e) { reject(e); }
                },
                onerror: (res) => reject(new Error('下载失败: ' + (res.error || '网络错误'))),
                ontimeout: () => reject(new Error('下载超时'))
            });
        });
    }

    let metaMask = null;
    function openMetaPanel() {
        if (metaMask) return;
        const meta = collectMeta();
        metaMask = document.createElement('div');
        metaMask.id = 'sht-mask';
        metaMask.innerHTML =
            '<div id="sht-meta-modal">' +
            '  <div id="sht-modal-title">📤 上传元数据<span id="sht-meta-tid"></span><button id="sht-meta-close" title="关闭">✕</button></div>' +
            '  <div id="sht-modal-sub">提取帖子标题/简介/图片，由<strong>本机浏览器</strong>下载图片后上传服务器，生成 Emby 海报与简介（剧集自动匹配 emby_tv 目录）。图片为<strong>手工选择</strong>，支持动图与排序</div>' +
            '  <div class="sht-meta-label">标题</div><input id="sht-meta-title-input" type="text">' +
            '  <div class="sht-meta-label">简介（前500字自动提取，可编辑）</div><textarea id="sht-meta-desc-input" rows="3"></textarea>' +
            '  <div class="sht-meta-label">图片（点击候选图选择/取消，可多选，最多9张，支持动图）</div>' +
            '  <div id="sht-meta-picker"></div>' +
            '  <div class="sht-meta-label">已选图片（📌设为海报，↑↓调序，✕移除）</div>' +
            '  <div id="sht-meta-chosen"></div>' +
            '  <div id="sht-meta-status"></div>' +
            '  <div class="sht-modal-actions">' +
            '    <button id="sht-meta-cancel">取消</button>' +
            '    <button id="sht-meta-ok">🚀 上传元数据</button>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(metaMask);

        const tid = getThreadId() || '';
        metaMask.querySelector('#sht-meta-tid').textContent = 'thread: ' + (tid || '?');
        const titleInput = metaMask.querySelector('#sht-meta-title-input');
        const descInput = metaMask.querySelector('#sht-meta-desc-input');
        const pickerBox = metaMask.querySelector('#sht-meta-picker');
        const chosenBox = metaMask.querySelector('#sht-meta-chosen');
        const statusEl = metaMask.querySelector('#sht-meta-status');
        const okBtn = metaMask.querySelector('#sht-meta-ok');
        titleInput.value = meta.title;
        descInput.value = meta.desc;
        if (!meta.desc && meta.descEmptyReason) {
            statusEl.textContent = '⚠️ 简介未提取: ' + meta.descEmptyReason + '（可手动填写）';
            statusEl.className = '';
        }

        const MAX_PICK = 9;
        const metaImgs = meta.images.map((im) => ({ src: im.src, poster: false, b64: null }));
        const picked = []; // 已选图片 (按上传顺序, 与 metaImgs 元素同引用)

        // 候选图网格: 点击选中/取消
        const renderPicker = () => {
            pickerBox.innerHTML = '';
            if (!metaImgs.length) {
                pickerBox.innerHTML = '<div class="sht-meta-empty">⚠️ 未找到候选图片，可只上传标题/简介</div>';
                return;
            }
            metaImgs.forEach((im) => {
                const cell = document.createElement('div');
                const on = picked.indexOf(im) >= 0;
                cell.className = 'sht-meta-pick' + (on ? ' sel' : '');
                cell.innerHTML = '<img src="' + im.src + '" loading="lazy"><span class="sht-meta-pick-tick">✓</span>';
                cell.addEventListener('click', () => {
                    const i = picked.indexOf(im);
                    if (i >= 0) {
                        picked.splice(i, 1);
                        im.poster = false;
                        if (picked.length && !picked.some(x => x.poster)) picked[0].poster = true; // 海报被删则第一张顶上
                    } else {
                        if (picked.length >= MAX_PICK) {
                            statusEl.textContent = '⚠️ 最多选择 ' + MAX_PICK + ' 张图片';
                            return;
                        }
                        if (!picked.length) im.poster = true; // 第一张默认海报
                        picked.push(im);
                    }
                    renderPicker();
                    renderChosen();
                });
                pickerBox.appendChild(cell);
            });
        };

        // 已选列表: 📌海报 / ↑↓调序 / ✕移除
        const renderChosen = () => {
            chosenBox.innerHTML = '';
            if (!picked.length) {
                chosenBox.innerHTML = '<div class="sht-meta-empty">尚未选择图片，点上方候选图添加</div>';
                return;
            }
            picked.forEach((im, idx) => {
                const row = document.createElement('div');
                row.className = 'sht-meta-crow' + (im.poster ? ' poster' : '');
                row.innerHTML =
                    '<span class="sht-meta-cidx">' + (idx + 1) + '</span>' +
                    '<img class="sht-meta-cimg" src="' + im.src + '" loading="lazy">' +
                    '<div class="sht-meta-cbtns">' +
                    '  <button class="sht-meta-cbtn" data-act="poster" title="设为海报">' + (im.poster ? '⭐' : '📌') + '</button>' +
                    '  <button class="sht-meta-cbtn" data-act="up" title="上移">↑</button>' +
                    '  <button class="sht-meta-cbtn" data-act="down" title="下移">↓</button>' +
                    '  <button class="sht-meta-cbtn" data-act="del" title="移除">✕</button>' +
                    '</div>';
                row.querySelector('[data-act="poster"]').addEventListener('click', (e) => {
                    e.stopPropagation();
                    picked.forEach(x => x.poster = false);
                    im.poster = true;
                    renderChosen();
                });
                row.querySelector('[data-act="up"]').addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (idx > 0) { picked.splice(idx - 1, 0, picked.splice(idx, 1)[0]); renderChosen(); }
                });
                row.querySelector('[data-act="down"]').addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (idx < picked.length - 1) { picked.splice(idx + 1, 0, picked.splice(idx, 1)[0]); renderChosen(); }
                });
                row.querySelector('[data-act="del"]').addEventListener('click', (e) => {
                    e.stopPropagation();
                    picked.splice(idx, 1);
                    im.poster = false;
                    if (picked.length && !picked.some(x => x.poster)) picked[0].poster = true;
                    renderPicker();
                    renderChosen();
                });
                chosenBox.appendChild(row);
            });
        };
        renderPicker();
        renderChosen();

        const close = () => { if (metaMask) { metaMask.remove(); metaMask = null; } };
        metaMask.querySelector('#sht-meta-close').addEventListener('click', close);
        metaMask.querySelector('#sht-meta-cancel').addEventListener('click', close);
        metaMask.addEventListener('click', (e) => { if (e.target === metaMask) close(); });

        okBtn.addEventListener('click', async () => {
            if (okBtn.disabled) return;
            if (!tid) { statusEl.textContent = '⚠️ 未识别到 thread_id，无法定位媒体目录'; return; }
            okBtn.disabled = true;
            statusEl.textContent = '⏳ 正在下载图片… (' + picked.length + ' 张)';
            const results = await Promise.allSettled(picked.map(im => downloadImgBase64(im.src)));
            const images = [];
            const fails = [];
            results.forEach((r, idx) => {
                if (r.status === 'fulfilled' && r.value && r.value.data) {
                    const mime = r.value.mime || 'image/jpeg';
                    const ext = (mime.split('/')[1] === 'jpeg') ? 'jpg' : (mime.split('/')[1] || 'jpg');
                    images.push({ name: (picked[idx].poster ? 'poster' : 'img' + idx) + '.' + ext, data: r.value.data, poster: picked[idx].poster });
                } else {
                    fails.push('图' + (idx + 1) + ': ' + ((r.reason && r.reason.message) || r.reason || '失败'));
                }
            });
            if (!images.length) {
                statusEl.textContent = '⚠️ 没有图片上传成功: ' + fails.slice(0, 3).join('；') + '；仍可只传标题/简介';
            } else if (fails.length) {
                statusEl.textContent = '⏳ 已下载 ' + images.length + '/' + picked.length + ' 张图（失败: ' + fails.join('；') + '）';
            }
            statusEl.textContent = '⏳ 正在上传到服务器… (' + images.length + ' 张图)';
            const payload = {
                thread_id: tid,
                title: titleInput.value.trim(),
                desc: descInput.value.trim(),
                images: images
            };
            try {
                const r = await api('POST', '/api/metadata', payload);
                if (!r.ok) throw new Error(r.error || '服务器未返回 ok');
                statusEl.textContent = '✅ 已上传: ' + (r.files || []).join(', ') + (r.refreshed ? '，已触发 Emby 刷新' : '');
                statusEl.className = 'sht-done';
                setTimeout(close, 2500);
            } catch (e) {
                statusEl.textContent = '❌ 上传失败: ' + e.message;
                okBtn.disabled = false;
            }
        });

        setTimeout(() => titleInput.focus(), 50);
    }

    // ===== 悬浮导航面板 =====
    let nav = null, navList = null, navCollapsed = false;
    function buildNav() {
        if (nav) { nav.remove(); }
        nav = document.createElement('div');
        nav.id = 'sht-nav';
        nav.innerHTML =
            '<div id="sht-nav-head">' +
            '  <span id="sht-nav-title">📌 磁力导航</span>' +
            '  <span id="sht-nav-count"></span>' +
            '  <button id="sht-nav-tasks" title="打开任务监控页">📋</button>' +
            '  <button id="sht-nav-meta" title="上传元数据(海报/简介)">📤</button>' +
            '  <button id="sht-nav-manual" title="手动输入磁力/ed2k 链接入库">✏️</button>' +
            '  <button id="sht-nav-fold" title="折叠/展开">—</button>' +
            '</div>' +
            '<div id="sht-nav-body">' +
            '  <div id="sht-nav-list"></div>' +
            '</div>';
        document.body.appendChild(nav);
        navList = nav.querySelector('#sht-nav-list');

        nav.querySelector('#sht-nav-tasks').addEventListener('click', (e) => {
            e.stopPropagation();
            window.open(API_BASE + '/tasks', '_blank');
        });

        nav.querySelector('#sht-nav-meta').addEventListener('click', (e) => {
            e.stopPropagation();
            openMetaPanel();
        });

        nav.querySelector('#sht-nav-manual').addEventListener('click', (e) => {
            e.stopPropagation();
            openManualDialog();
        });

        nav.querySelector('#sht-nav-fold').addEventListener('click', (e) => {
            e.stopPropagation();
            navCollapsed = !navCollapsed;
            nav.querySelector('#sht-nav-body').style.display = navCollapsed ? 'none' : 'block';
            nav.querySelector('#sht-nav-fold').textContent = navCollapsed ? '+' : '—';
        });
        renderNavList();
    }

    function renderNavList() {
        const key = items.map(i => i.norm).join('|');
        if (key === lastNavKey) return;
        lastNavKey = key;
        navList.innerHTML = '';
        items.forEach((it, idx) => {
            const row = document.createElement('div');
            row.className = 'sht-nav-item';
            row.innerHTML = '<span class="sht-nav-idx">' + (idx + 1) + '</span><span class="sht-nav-label"></span><span class="sht-nav-import" title="入库此链接 (可先选类型)">🚀</span><span class="sht-nav-copy" title="复制完整磁力链接">📋</span><span class="sht-nav-go">↘</span>';
            row.querySelector('.sht-nav-label').textContent = it.label;
            row.title = it.raw || it.norm;
            row.querySelector('.sht-nav-copy').addEventListener('click', (e) => {
                e.stopPropagation();
                copyMagnet(it.raw || it.norm);
            });
            row.querySelector('.sht-nav-import').addEventListener('click', (e) => {
                e.stopPropagation();
                openKindPanel(it, row.querySelector('.sht-nav-import'), e);
            });
            row.addEventListener('click', () => {
                flashTo(it.el);
            });
            navList.appendChild(row);
        });
        nav.querySelector('#sht-nav-count').textContent = '(' + items.length + ')';
    }

    // ===== 复制完整磁力(含 &dn 后缀, 无杂质) =====
    function copyMagnet(text) {
        if (!text) return;
        const clean = String(text).replace(/\s*🎬?\s*入库/g, '').trim();
        const done = () => toast('✅ 已复制磁力链接');
        const fallback = (txt) => {
            const ta = document.createElement('textarea');
            ta.value = txt;
            ta.style.position = 'fixed'; ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch (e) {}
            document.body.removeChild(ta);
            if (ok) done(); else toast('复制失败, 请手动复制', true);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(clean).then(done, () => fallback(clean));
        } else {
            fallback(clean);
        }
    }

    // ===== 样式 =====
    function injectStyle() {
        const s = document.createElement('style');
        s.textContent = `
#sht-nav{position:fixed;right:16px;bottom:16px;z-index:2147483647;width:280px;max-height:70vh;display:flex;flex-direction:column;background:rgba(15,23,42,.94);border:1px solid #334155;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}
#sht-nav-head{display:flex;align-items:center;gap:6px;padding:8px 10px;background:linear-gradient(135deg,#e63946,#d90429);color:#fff;font-size:13px;font-weight:700;cursor:pointer;user-select:none}
#sht-nav-count{opacity:.85;font-weight:400}
#sht-nav-manual,#sht-nav-meta{margin-left:auto;padding:2px 7px;border:none;border-radius:6px;background:rgba(255,255,255,.2);color:#fff;font-size:13px;cursor:pointer}
#sht-nav-manual{margin-left:0}
#sht-nav-meta:hover,#sht-nav-manual:hover{background:rgba(255,255,255,.35)}
#sht-nav-fold{padding:2px 7px;border:none;border-radius:6px;background:rgba(255,255,255,.2);color:#fff;font-size:13px;cursor:pointer}
#sht-nav-body{overflow-y:auto;max-height:calc(70vh - 40px)}
#sht-nav-list{padding:4px}
.sht-nav-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;color:#e2e8f0;font-size:12px;cursor:pointer}
.sht-nav-item:hover{background:#1e293b}
.sht-nav-idx{flex:none;min-width:20px;text-align:center;background:#e63946;color:#fff;border-radius:4px;font-size:11px;font-weight:700;padding:1px 4px}
.sht-nav-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sht-nav-go{color:#64748b}
.sht-nav-import,.sht-nav-copy{flex:none;padding:0 4px;font-size:13px;cursor:pointer;border-radius:4px;min-width:26px;text-align:center}
.sht-nav-copy{color:#94a3b8}
.sht-nav-import{color:#4ade80}
.sht-nav-copy:hover{background:#334155;color:#fff}
.sht-nav-import:hover{background:#16a34a;color:#fff}
.sht-nav-import.sht-done{color:#2dc653;font-weight:700}
.sht-nav-import.sht-fail{color:#f87171}
.sht-link-btn{display:inline-block;margin:0 4px 2px 6px;padding:2px 10px;border:none;border-radius:12px;background:linear-gradient(135deg,#e63946,#d90429);color:#fff;font-size:12px;font-weight:600;cursor:pointer;vertical-align:middle;line-height:1.6;box-shadow:0 2px 6px rgba(217,4,41,.4)}
.sht-link-btn:hover{transform:translateY(-1px)}
.sht-link-btn:disabled{cursor:wait;opacity:.85}
.sht-link-btn.sht-done{background:linear-gradient(135deg,#2dc653,#1a7431)}
.sht-link-btn.sht-fail{background:linear-gradient(135deg,#6c757d,#343a40)}
.sht-pop{position:fixed;z-index:2147483648;width:180px;background:rgba(15,23,42,.97);border:1px solid #475569;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:10px;font-family:'Segoe UI',system-ui,sans-serif}
.sht-pop-title{color:#e2e8f0;font-size:12px;font-weight:700;margin-bottom:8px}
.sht-pop-kind{display:block;width:100%;margin:4px 0;padding:6px 8px;border:none;border-radius:6px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer;text-align:left}
.sht-pop-kind:hover{background:#e63946}
.sht-pop-sub{color:#94a3b8;font-size:11px;margin-top:6px;word-break:break-all;max-height:60px;overflow:hidden}
#sht-mask{position:fixed;inset:0;z-index:2147483649;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}
#sht-modal,#sht-meta-modal{width:min(560px,92vw);max-height:82vh;overflow:auto;background:#0f172a;border:1px solid #334155;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.6);padding:14px 16px;font-family:'Segoe UI',system-ui,sans-serif;display:flex;flex-direction:column;gap:10px}
#sht-modal-title{display:flex;align-items:center;gap:8px;color:#f8fafc;font-size:14px;font-weight:700}
#sht-meta-tid,#sht-modal-tid{margin-left:auto;color:#94a3b8;font-size:12px;font-weight:400}
#sht-meta-close,#sht-modal-close{padding:1px 8px;border:none;border-radius:6px;background:#1e293b;color:#94a3b8;font-size:14px;cursor:pointer}
#sht-meta-close:hover,#sht-modal-close:hover{background:#e63946;color:#fff}
#sht-modal-sub{color:#94a3b8;font-size:12px;line-height:1.5}
#sht-modal-input,#sht-meta-title-input,#sht-meta-desc-input{width:100%;box-sizing:border-box;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:13px;line-height:1.5;padding:8px 10px;resize:vertical;outline:none}
#sht-modal-input{min-height:96px}
#sht-meta-desc-input{min-height:64px}
#sht-modal-input:focus,#sht-meta-title-input:focus,#sht-meta-desc-input:focus{border-color:#e63946}
#sht-modal-detect{color:#64748b;font-size:12px;min-height:16px;word-break:break-all}
#sht-modal-detect.ok{color:#2dc653}
#sht-modal-detect.err{color:#ef4444}
.sht-meta-label{color:#94a3b8;font-size:12px;font-weight:600}
#sht-meta-picker{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:8px;max-height:300px;overflow-y:auto;padding:6px;border:1px solid #1e293b;border-radius:10px;background:#0b1220}
.sht-meta-pick{position:relative;aspect-ratio:1/1;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid #334155;background:#1e293b;opacity:.72;transition:opacity .15s,border-color .15s}
.sht-meta-pick img{width:100%;height:100%;object-fit:cover;display:block}
.sht-meta-pick:active{transform:scale(.96)}
.sht-meta-pick.sel{border-color:#e63946;opacity:1;box-shadow:0 0 0 2px rgba(230,57,70,.35)}
.sht-meta-pick-tick{position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;background:#e63946;color:#fff;font-size:14px;font-weight:700;display:none;align-items:center;justify-content:center}
.sht-meta-pick.sel .sht-meta-pick-tick{display:flex}
#sht-meta-chosen{display:flex;flex-direction:column;gap:6px}
.sht-meta-crow{display:flex;align-items:center;gap:8px;background:#1e293b;border:1px solid #334155;border-radius:10px;padding:6px 8px}
.sht-meta-crow.poster{border-color:#e63946;box-shadow:0 0 0 1px rgba(230,57,70,.4)}
.sht-meta-cidx{flex:none;min-width:22px;text-align:center;background:#e63946;color:#fff;border-radius:5px;font-size:11px;font-weight:700;padding:2px 0}
.sht-meta-cimg{width:48px;height:48px;object-fit:cover;border-radius:6px;flex:none;background:#0f172a}
.sht-meta-cbtns{display:flex;gap:6px;margin-left:auto}
.sht-meta-cbtn{min-width:34px;min-height:34px;padding:4px 8px;border:none;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:15px;line-height:1;cursor:pointer;touch-action:manipulation}
.sht-meta-cbtn:active{background:#e63946}
.sht-meta-empty{color:#64748b;font-size:12px;padding:8px;text-align:center}
@media (max-width:640px){
  #sht-meta-picker{grid-template-columns:repeat(auto-fill,minmax(70px,1fr));max-height:40vh}
  .sht-meta-cbtn{min-width:42px;min-height:42px;font-size:17px}
  .sht-meta-cimg{width:44px;height:44px}
  .sht-meta-pick-tick{width:26px;height:26px;font-size:16px}
}
#sht-meta-status{color:#e2e8f0;font-size:12px;min-height:18px;word-break:break-all}
#sht-meta-status.sht-done{color:#2dc653;font-weight:600}
.sht-modal-kind-label{color:#94a3b8;font-size:12px}
.sht-modal-kind{padding:5px 12px;border:1px solid #334155;border-radius:8px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer;margin-right:8px}
.sht-modal-kind:hover{border-color:#e63946}
.sht-modal-kind.active{background:#e63946;border-color:#e63946;color:#fff;font-weight:600}
#sht-modal-status{color:#e2e8f0;font-size:12px;min-height:18px;word-break:break-all}
#sht-modal-status.sht-done{color:#2dc653;font-weight:600}
#sht-modal-status.sht-fail{color:#ef4444}
.sht-modal-actions{display:flex;justify-content:flex-end;gap:8px}
#sht-meta-cancel,#sht-meta-ok,#sht-modal-cancel,#sht-modal-ok{padding:7px 16px;border:none;border-radius:8px;font-size:13px;cursor:pointer}
#sht-meta-cancel,#sht-modal-cancel{background:#1e293b;color:#94a3b8}
#sht-meta-cancel:hover,#sht-modal-cancel:hover{background:#334155;color:#e2e8f0}
#sht-meta-ok,#sht-modal-ok{background:linear-gradient(135deg,#e63946,#d90429);color:#fff;font-weight:600}
#sht-meta-ok:hover,#sht-modal-ok:hover{filter:brightness(1.1)}
#sht-meta-ok:disabled,#sht-modal-ok:disabled{cursor:wait;opacity:.75}
`;
        document.head.appendChild(s);
    }

    // ===== 扫描 + 挂载 =====
    const tid = getThreadId();
    if (!tid) return;

    injectStyle();

    function refresh() {
        cleanPageMagnetTexts();
        collectLinks();
        attachButtons();
        if (navList) renderNavList();
        else buildNav();
        const c = document.querySelector('#sht-nav-count');
        if (c) c.textContent = '(' + items.length + ')';
    }

    refresh();

    let debounceT = null;
    const mo = new MutationObserver(() => {
        clearTimeout(debounceT);
        debounceT = setTimeout(refresh, 400);
    });
    mo.observe(document.body, { childList: true, subtree: true });
})();
