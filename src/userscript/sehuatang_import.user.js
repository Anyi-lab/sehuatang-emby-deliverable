// ==UserScript==
// @name         色花堂链接级一键入库(逐条磁力/ed2k) + 元数据补充
// @namespace    sehuatang-import
// @version      1.14.1
// @updateURL    https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/src/userscript/sehuatang_import.user.js
// @downloadURL  https://raw.githubusercontent.com/Anyi-lab/sehuatang-emby-deliverable/main/src/userscript/sehuatang_import.user.js
// @source       https://github.com/Anyi-lab/sehuatang-emby-deliverable
// @description  色花堂链接级一键入库(逐条磁力/ed2k)。每条链接手工选择类型: "番号"=影片(推→等→清小文件→strm→MDC刮削→Emby刷新→预热; MDC失败/不完整由油猴📤补充); "非番号"=剧集(推→等→重命名thread_xxx_S01E01..→SmartStrm生成strm→油猴📤补充元数据→Emby刷新+预热, 即使1个视频也是剧集)。左上角磁力导航面板列出全部链接可点击快速滚动定位。v1.6.3: 修复手机版磁力被 <wbr>/<br> 等空元素拆成多段文本节点导致的漏识别(全文本拼接兜底+iframe兜底); v1.7.0: 新增手动输入磁力/ed2k链接入库对话框(导航面板✏️按钮, 支持多条, 提交到当前thread); v1.8.0: 新增元数据补充——提取帖子标题/简介/图片, 由本机浏览器下载图片后上传服务器生成 Emby 海报与简介 (导航面板📤按钮); v1.8.1: 元数据图片抓取优化——只抓静态图片(jpg/png等, 排除gif/webp/svg/ico), 懒加载取真实地址(data-original/data-src), 像素尺寸过滤(太小的表情/图标/头像不抓, 超高清原图不抓); v1.8.2: 描述同步新流程(去MySQL, 剧集文件名 thread_xxx_S01E01, MDC失败/剧集均待油猴补充元数据); v1.8.3: API_BASE 改 http(未配https), 悬浮面板新增📋一键跳转任务监控页按钮; v1.8.4: 手机版图片识别兼容——Discuz 附件真实地址 file/zoomfile 属性、选择器抓不到时全量图片兜底、iframe 内正文跨框架收集、相对路径补全域名; v1.8.7: 简介提取终极兜底——按"含磁力/ed2k 链接的文本节点"定位正文容器(色花堂正文必含下载链接, 不受模板类名影响), 简介提取失败时面板显示具体原因。 v1.9.0: 图片改为用户手工选择(候选图网格点选, 支持动图gif/webp/avif, 最多9张), 已选支持📌海报/↑↓调序/✕移除, 上传保留原图格式(mime)不再强制jpg, 移动端触控优化。 v1.9.1: 候选图排除小尺寸表情/图标(像素<200或未加载时CSS尺寸<100x80), 恢复表情/笑脸区域跳过。 v1.9.2: 标题清洗——去掉发布者/来源前缀标记(自转/115ED2K等, 保留【Omar盘点】类系列名), 去掉体积/配额后缀【1.87G/25P+18V/1配额】, 去掉尾部分区与论坛后缀(- 综合讨论区 - 98堂[原色花堂])。 v1.9.5: 磁力导航面板每条磁力新增📋复制按钮(复制完整干净链接含&dn), 标签优先显示文件名; v1.9.6: 取消正文磁力链接旁"🎬入库"按钮(避免页面出现两个入库), 改为磁力导航面板每条磁力🚀一键入库(点🚀选类型: 番号/非番号); // ==UserScript==; v1.9.10: 磁力规则参考 JAV-FORUM——支持 32位 base32 磁力hash(原仅40位hex), ed2k 要求文件名非空(空文件名ed2k拒绝); v1.11.0: 新增批量入库(导航面板⚡按钮)——一次提交本页全部(或勾选)磁力/ed2k: 按行类型分组调 /api/import/batch(每组超100条自动分批), 支持👁dry-run预览、提交后并发轮询进度、面板关闭后头部保留进度徽章, 不再逐条点🚀逐条等, 顺带修掉面板/弹层内的磁力文本被 collectLinks 误收进导航列表; v1.11.1: 悬浮导航面板默认落位改到画面左上角(标题栏仍可拖动, 拖走后仅本次会话生效); v1.12.0: 面板头部重排为两行(标题行 + 工具条), 工具条四个按钮带文字均分宽, 标题不再被挤成两行、按钮不再顶出边界; 拖动后的落位用 GM 存储记住(刷新/翻页仍在原地, 越界自动收回画面内), 双击标题行复位到左上角; v1.13.0: 导航面板每条链接显示 115 库存徽章 ✅在库/❓不在库/⚠️未校验 (打开帖子页自动反查一次, 服务端缓存+分片, MCP 掉线一律显示"未校验"绝不当成"不在库"); v1.14.0: 库存反查带上帖子上下文(title/thread_id/contexts) —— 裸磁链(无 &dn=)也能从页面文本抠出番号, 不再一片⚠️; 服务端台账新增"番号→已入库"索引, 番号命中的 0 请求直答(MCP 掉线也答得出); 跨条查重防呆: 同一段文本供出≥2 条磁链(合集帖公共标题)则该番号作废, 宁缺勿错; 响应新增 src 字段说明番号来源, 控制台一行交代"115 请求数 + 番号来源分账" 【v1.14.1】库存反查: 当结果靠页面文本判定(或一条都没抠出番号)时, 控制台额外打印送给服务端的页面文本(由近到远), 用来核对抠出的番号是否为真。
// @author       QwenPaw
// @match        *://sehuatang.net/*
// @match        *://sehuatang.org/*
// @match        *://sehuatang.net/*
// @match        *://sehuatang.org/*
// @match        *://www.sehuatang.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
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
    // 刮削分类下拉(v1.11.0 抽成常量): 单条弹层/手动输入/批量入库三处共用, 避免选项各写一份漂移
    const CAT_OPTIONS = '<option value="av">AV(默认)</option>' +
        '<option value="fc2">FC2</option>' +
        '<option value="sw">丝袜</option>' +
        '<option value="cn">国产自拍</option>' +
        '<option value="ea">欧美</option>' +
        '<option value="lf">里番</option>';
    // ==================

    function getThreadId() {
        let m = location.pathname.match(/thread-(\d+)/);
        if (m) return m[1];
        m = location.search.match(/[?&]tid=(\d+)/);
        if (m) return m[1];
        return null;
    }

    // ===== 链接规范化(用于去重 + 提交) =====
    // 磁链: btih hash 统一小写; ed2k: hash 统一小写 + 结尾统一 (| 结尾, 去掉可选 /)
    function normLink(s) {
        if (!s) return null;
        s = s.trim()
            .replace(/&amp;/g, '&')
            .replace(/&nbsp;/g, ' ')
            .replace(/[\u200b\u200c\u200d\ufeff]/g, '');
        if (s.startsWith('magnet:')) {
            const m = s.match(/^magnet:\?xt=urn:btih:([a-zA-Z0-9]{32,40})/);
            return m ? 'magnet:?xt=urn:btih:' + m[1].toLowerCase() : null;
        }
        if (s.startsWith('ed2k://')) {
            const m = s.match(/^ed2k:\/\/\|file\|([^|]+)\|(\d+)\|([0-9a-fA-F]{32,40})\|\/?/);
            return m ? 'ed2k://|file|' + m[1] + '|' + m[2] + '|' + m[3].toLowerCase() + '|' : null;
        }
        return null;
    }

    // ===== UI 容器判定 (v1.11.0) =====
    // 面板/弹层里渲染的磁力文本(以及服务端回显的结果行)不能被 collectLinks 当成页面链接二次收集,
    // 否则每开一次批量面板就会把自己喂进导航列表(面板里的 link 文本 → 新的一行 → DOM 变化 → 再扫描)。
    // 原代码只排除 #sht-import-wrap(已废弃的旧容器), 这里统一成白名单式排除。
    const UI_SELECTOR = '#sht-nav,#sht-mask,#sht-toast,#sht-import-wrap';
    function inUi(el) {
        try { return !!(el && el.closest && el.closest(UI_SELECTOR)); } catch (e) { return false; }
    }

    // ===== 收集链接与锚点 =====
    let items = [];
    const seenNorm = new Set();
    let elOrder = new Map(); // 元素 -> 文档序编号(用于按文章出现顺序排列)

    function docOrderOf(el) {
        if (!el || el.nodeType !== 1) return 1e9;
        const o = elOrder.get(el);
        return o === undefined ? 1e9 : o;
    }

    function labelFrom(s) {
        if (!s) return '链接';
        let t = s.replace(/\s+/g, ' ').trim();
        if (t.startsWith('magnet:')) {
            const dn = t.match(/[?&]dn=([^&\s🎬]+)/);
            if (dn) {
                const fn = decodeURIComponent(dn[1]);
                return fn.length > MAX_LABEL ? fn.slice(0, MAX_LABEL) + '…' : fn;
            }
            const bt = t.match(/btih:([a-zA-Z0-9]{32,40})/);
            return bt ? '磁力 …' + bt[1].slice(-8) : '磁力链接';
        }
        if (t.startsWith('ed2k://')) {
            const m = t.match(/ed2k:\/\/\|file\|([^|]*)\|/);
            const fn = m ? m[1] : 'ed2k链接';
            return fn.length > MAX_LABEL ? fn.slice(0, MAX_LABEL) + '…' : fn;
        }
        return t.length > MAX_LABEL ? t.slice(0, MAX_LABEL) + '…' : t;
    }

    function addItem(norm, el, raw) {
        if (!norm) return;
        // raw 存原始完整文本(清洗掉 🎬/入库 杂质, 保留 &dn 后缀)
        const clean = String(raw || norm).replace(/\s*🎬?\s*入库/g, '').trim();
        if (seenNorm.has(norm)) {
            // 重复链接(磁链/ed2k 均去重): 若新位置在文档中更靠前, 更新为更早的出现位置
            const it = items.find(i => i.norm === norm);
            if (it && el && docOrderOf(el) < docOrderOf(it.el)) {
                it.el = el;
                it.raw = clean || norm;
                it.label = labelFrom(clean || norm);
            }
            return;
        }
        seenNorm.add(norm);
        items.push({ norm: norm, el: el || null, label: labelFrom(clean || norm), raw: clean || norm });
    }

    function collectLinks() {
        items = [];
        seenNorm.clear();

        // 给 body 内所有元素编号(文档序), 用于按文章出现顺序排列磁链/ed2k
        elOrder = new Map();
        let oi = 0;
        try {
            const ew = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let en;
            while ((en = ew.nextNode())) elOrder.set(en, oi++);
        } catch (e) {}

        document.querySelectorAll('a[href]').forEach(a => {
            if (inUi(a)) return;                       // 面板/弹层内的链接不算页面磁力 (v1.11.0)
            const n = normLink(a.getAttribute('href') || '');
            if (n) addItem(n, a, a.textContent || n);
        });

        document.querySelectorAll('input[type="text"], input[type="hidden"], textarea').forEach(el => {
            if (inUi(el)) return;                      // 手动/批量弹层输入框里的磁力不算页面磁力 (v1.11.0)
            const n = normLink(el.value || '');
            if (n) addItem(n, el, n);
        });

        document.querySelectorAll('.blockcode li, code, pre').forEach(el => {
            if (inUi(el)) return;                      // (v1.11.0) 同上, 面板内代码块不收集
            const n = normLink(el.textContent || '');
            if (n) addItem(n, el, el.textContent || n);
        });

        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const t = node.nodeValue || '';
                if (!t) return NodeFilter.FILTER_REJECT;
                if (inUi(node.parentNode)) {
                    return NodeFilter.FILTER_REJECT;
                }
                return /magnet:\S+|ed2k:\/\/\|file\|/.test(t) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        });
        let node;
        while ((node = walker.nextNode())) {
            const v = node.nodeValue || '';
            const re = /magnet:\S+|ed2k:\/\/\|file\|[^|\n]+\|\d+\|[0-9a-fA-F]{32,40}\|\/?/g;
            let mm;
            while ((mm = re.exec(v)) !== null) {
                const n = normLink(mm[0]);
                if (!n) continue;
                const p = node.parentNode;
                if (p && p.nodeType === 1) addItem(n, p, mm[0]);
            }
        }

        try {
            const bodyText = document.body.textContent || '';
            if (bodyText && bodyText.length < 3000000) {
                const re = /magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}|ed2k:\/\/\|file\|[^|\n]+\|\d+\|[0-9a-fA-F]{32,40}\|\/?/g;
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
                        if (inUi(tn && tn.parentNode)) continue;   // 命中面板/弹层内的文本, 跳过 (v1.11.0)
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
                const re = /magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}|ed2k:\/\/\|file\|[^|\n]+\|\d+\|[0-9a-fA-F]{32,40}\|\/?/g;
                let m;
                while ((m = re.exec(t)) !== null) {
                    const norm = normLink(m[0]);
                    if (norm) addItem(norm, ifr, m[0]);
                }
            } catch (e) {}
        });

        // 按文档出现顺序排序(磁链/ed2k 混合排列, 不分组)
        items.sort((a, b) => docOrderOf(a.el) - docOrderOf(b.el));
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
    async function submitImport(links, kind, btn, onFinal, category) {
        if (btn) { btn.disabled = true; btn.textContent = '提交中…'; }
        try {
            const body = {
                thread_id: getThreadId(),
                magnet: links.join('\n'),
                title: document.title,
                thread_url: location.href,
                kind: kind || undefined,
                category: category || undefined
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
                toast('入库完成: ' + (final.msg || '') + ' — 可点左上角📤上传元数据', false);
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
                    if (inUi(p)) return NodeFilter.FILTER_REJECT;   // 面板/弹层内的文本不清洗 (v1.11.0)
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
            '<div class="sht-pop-cat">' +
            '  <label>刮削分类</label>' +
            '  <select class="sht-pop-cat-select">' + CAT_OPTIONS + '</select>' +
            '</div>' +
            '<div class="sht-pop-sub">' + it.label + '</div>';
        document.body.appendChild(pop);
        const pw = pop.offsetWidth || 200;
        let left = rect.left;
        let top = rect.bottom + 6;
        if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
        if (top + pop.offsetHeight > window.innerHeight - 8) top = rect.top - pop.offsetHeight - 6;
        pop.style.left = left + 'px';
        pop.style.top = top + 'px';
        pop.querySelectorAll('.sht-pop-kind').forEach(k => {
            k.addEventListener('click', () => {
                const kind = k.dataset.kind;
                const category = pop.querySelector('.sht-pop-cat-select').value;
                pop.remove(); pop = null;
                submitImport([it.norm], kind, btn, null, category);
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
        const re = /magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}|ed2k:\/\/\|file\|[^|\n]+\|\d+\|[0-9a-fA-F]{32,40}\|\/?/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            const n = normLink(m[0]);
            if (n && !seen.has(n)) { seen.add(n); out.push(n); }
        }
        return out;
    }

    let manualMask = null;
    function openManualDialog() {
        closeOverlays('manual');          // 关掉批量/元数据弹层, 避免两个面板叠在一起 (v1.11.0)
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
            '  <div><span class="sht-modal-kind-label">刮削分类：</span>' +
            '    <select id="sht-modal-category">' + CAT_OPTIONS + '</select>' +
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
            }, manualMask.querySelector('#sht-modal-category').value);
            if (!manualMask) return;
            okBtn.disabled = false;
        });

        setTimeout(() => input.focus(), 50);
    }

    // ===== 批量入库 (v1.11.0) =====
    // 痛点: 一个帖子几十条磁力, 逐条点 🚀 要重复"选类型 → 等服务端 6 秒轮询 → 再点下一条"。
    // 后端早有 POST /api/import/batch(一次提交整批, 逐条建任务), 前端一直没用上 —— 这里补上入口。
    // 设计: kind 是整批共用的 → 前端按行类型分组, 每组超过 BATCH_MAX 再自动切片;
    //      提交后并发轮询所有 task_id(不再逐条阻塞等待), 面板关掉后进度留在导航头部徽章上。
    const BATCH_MAX = 100;         // 与后端 BATCH_MAX_ITEMS 默认值一致(超了后端直接报错, 前端先分批)
    const BATCH_POLL_CONC = 6;     // 每轮同时查询的任务数上限(几十条一起查会打满浏览器连接)
    let batchMask = null;          // 批量面板根节点
    let batchRows = [];            // [{it, pick, kind, ov, res, rowEl}] ov=行内改过类型(不再跟随顶部默认)
    let batchRender = null;        // 面板开着时由 openBatchPanel 注册的行重绘函数(页面变化后调用)
    let batchTasks = [];           // 本次批量建的任务 [{task_id, norm, label, kind, status, msg}]
    let batchPollTimer = null;
    const taskByNorm = new Map();  // norm -> task (导航行末尾显示 🕒/✅/❌)

    function taskStatusText(t) {
        if (!t) return '';
        if (t.status === 'done') return '✅ 已入库';
        if (t.status === 'failed') return '❌ 失败';
        return '🕒 ' + t.task_id;
    }

    // 关掉所有悬浮层(批量/手动输入/元数据/单条类型弹层), 同一时间只留一个, 避免叠一起时 z-index 混乱
    // except: 'manual' | 'meta' 时保留对应弹层(用户重复点同一个按钮时不清掉自己)
    function closeOverlays(except) {
        if (pop) { pop.remove(); pop = null; }
        if (manualMask && except !== 'manual') { manualMask.remove(); manualMask = null; }
        if (metaMask && except !== 'meta') { metaMask.remove(); metaMask = null; }
        if (batchMask && except !== 'batch') { batchMask.remove(); batchMask = null; batchRender = null; }
    }

    function batchDefaultKind() {
        const el = batchMask && batchMask.querySelector('input[name="sht-batch-kind"]:checked');
        return (el && el.value === 'non_fanhao') ? 'non_fanhao' : 'fanhao';
    }
    function batchDefaultCategory() {
        const el = batchMask && batchMask.querySelector('#sht-batch-category');
        return (el && el.value) || 'av';
    }

    // 纯函数(离线可测): 勾选行 → 按 kind 分组 → 每组按 BATCH_MAX 切片
    function batchGroups(rows) {
        const groups = [];
        ['fanhao', 'non_fanhao'].forEach(kind => {
            const picked = rows.filter(r => r.pick && r.kind === kind);
            for (let i = 0; i < picked.length; i += BATCH_MAX) {
                groups.push({ kind: kind, rows: picked.slice(i, i + BATCH_MAX) });
            }
        });
        return groups;
    }

    // 页面 DOM 变化后, 把面板里的行重新对齐到新收集出来的 items(按 norm 保留勾选/类型/结果)
    function remapBatchRows() {
        const prev = new Map(batchRows.map(r => [r.it.norm, r]));
        const submitted = new Set(batchTasks.map(t => t.norm));
        const dflt = batchMask ? batchDefaultKind() : 'fanhao';
        batchRows = items.map(it => {
            const old = prev.get(it.norm);
            if (old) { old.it = it; old.rowEl = null; return old; }
            return { it: it, pick: !submitted.has(it.norm), kind: dflt, ov: false,
                     res: taskStatusText(taskByNorm.get(it.norm)), rowEl: null };
        });
    }

    function navBadgeEl() { return nav && nav.querySelector('#sht-nav-badge'); }

    function refreshBatchBadge(finished) {
        const st = batchTasks;
        const badge = navBadgeEl();
        if (!st.length) { if (badge) badge.textContent = ''; return; }
        const done = st.filter(t => t.status === 'done').length;
        const fail = st.filter(t => t.status === 'failed').length;
        if (badge) {
            badge.textContent = '⚡' + done + '/' + st.length + (fail ? ' ✗' + fail : '');
            badge.title = '批量入库进度(点开查看): 完成 ' + done + ' / 失败 ' + fail + ' / 共 ' + st.length;
        }
        if (finished) {
            toast('批量入库结束: ' + done + ' 条完成' + (fail ? ', ' + fail + ' 条失败' : '') +
                  ' — 详情 ' + API_BASE + '/tasks', !!fail);
        }
    }

    function pollOneTask(t) {
        return api('GET', '/api/import/status?task_id=' + t.task_id).then(s => {
            const prev = (taskByNorm.get(t.norm) || {}).status;
            t.status = s.status || 'running';
            t.msg = s.msg || '';
            t.step = s.step || '';
            taskByNorm.set(t.norm, t);
            // v1.13.0: 入库成功 → 库存徽章立刻翻牌 (服务端也在 save_task(done) 时失效了自己的缓存)
            if (t.status === 'done' && prev !== 'done') invRefetchNorm(t.norm);
        }).catch(() => {});   // 单条查询失败不打断整轮, 下一轮再查
    }

    function stopBatchPolling() {
        if (batchPollTimer) { clearInterval(batchPollTimer); batchPollTimer = null; }
    }

    // 并发轮询: 只查还没结束的任务, 每轮最多 BATCH_POLL_CONC 个并发
    function startBatchPolling() {
        if (!batchTasks.length || batchPollTimer) return;
        const tick = async () => {
            const pend = batchTasks.filter(t => t.status !== 'done' && t.status !== 'failed');
            if (!pend.length) {
                stopBatchPolling();
                refreshBatchBadge(true);
                if (nav) renderNavList(true);
                return;
            }
            let idx = 0;
            const workers = Array.from({ length: Math.min(BATCH_POLL_CONC, pend.length) }, async () => {
                while (idx < pend.length) await pollOneTask(pend[idx++]);
            });
            await Promise.all(workers);
            refreshBatchBadge(false);
            if (nav) renderNavList(true);
            if (batchMask && batchRender) batchRender();
        };
        batchPollTimer = setInterval(tick, POLL_MS);
        tick();
    }

    function openBatchPanel() {
        closeOverlays();
        const submitted = new Set(batchTasks.map(t => t.norm));
        batchRows = items.map(it => ({
            it: it, pick: !submitted.has(it.norm), kind: 'fanhao', ov: false,
            res: taskStatusText(taskByNorm.get(it.norm)), rowEl: null
        }));
        batchMask = document.createElement('div');
        batchMask.id = 'sht-mask';
        batchMask.innerHTML =
            '<div id="sht-batch-modal">' +
            '  <div id="sht-modal-title">⚡ 批量入库<span id="sht-batch-tid"></span><button id="sht-batch-close" title="关闭">✕</button></div>' +
            '  <div id="sht-modal-sub">一次提交本页全部(或勾选)的磁力/ed2k。<strong>默认类型整批共用</strong>, 想单独走另一种就在行内点「番/剧」; 提交后服务端逐条建任务并按队列执行, <strong>可以关掉本面板继续浏览</strong>。</div>' +
            '  <div id="sht-batch-bar">' +
            '    <span class="sht-batch-lbl">默认类型</span>' +
            '    <button class="sht-batch-kind active" data-kind="fanhao">番号(电影)</button>' +
            '    <button class="sht-batch-kind" data-kind="non_fanhao">非番号(剧集)</button>' +
            '    <span class="sht-batch-lbl">分类</span>' +
            '    <select id="sht-batch-category">' + CAT_OPTIONS + '</select>' +
            '  </div>' +
            '  <div id="sht-batch-tools">' +
            '    <span id="sht-batch-count"></span>' +
            '    <button class="sht-batch-mini" data-act="all">全选</button>' +
            '    <button class="sht-batch-mini" data-act="none">全不选</button>' +
            '    <button class="sht-batch-mini" data-act="invert">反选</button>' +
            '    <button class="sht-batch-mini" data-act="unsubmitted">仅未提交</button>' +
            '  </div>' +
            '  <div id="sht-batch-list"></div>' +
            '  <div id="sht-batch-status"></div>' +
            '  <div class="sht-modal-actions">' +
            '    <button id="sht-batch-cancel">关闭</button>' +
            '    <button id="sht-batch-preview">👁 预览</button>' +
            '    <button id="sht-batch-ok">🚀 批量入库</button>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(batchMask);

        const tidEl = batchMask.querySelector('#sht-batch-tid');
        tidEl.textContent = 'thread: ' + (getThreadId() || '?') + ' / 共 ' + items.length + ' 条';
        const listBox = batchMask.querySelector('#sht-batch-list');
        const countEl = batchMask.querySelector('#sht-batch-count');
        const statusEl = batchMask.querySelector('#sht-batch-status');
        const okBtn = batchMask.querySelector('#sht-batch-ok');
        const previewBtn = batchMask.querySelector('#sht-batch-preview');

        const updateCount = () => {
            const picked = batchRows.filter(r => r.pick);
            const f = picked.filter(r => r.kind === 'fanhao').length;
            countEl.textContent = '共 ' + batchRows.length + ' 条, 已选 ' + picked.length +
                (picked.length ? '(番号 ' + f + ' / 剧集 ' + (picked.length - f) + ')' : '');
        };

        const renderRows = () => {
            listBox.innerHTML = '';
            if (!batchRows.length) {
                listBox.innerHTML = '<div class="sht-batch-empty">⚠️ 本页没识别到磁力/ed2k 链接</div>';
                return;
            }
            batchRows.forEach((r, idx) => {
                const row = document.createElement('div');
                row.className = 'sht-batch-row' + (r.pick ? '' : ' off') +
                    (r.res && r.res.indexOf('✅') === 0 ? ' done' : '') +
                    (r.res && r.res.indexOf('❌') === 0 ? ' fail' : '');
                row.innerHTML =
                    '<input type="checkbox" class="sht-batch-ck">' +
                    '<span class="sht-batch-idx">' + (idx + 1) + '</span>' +
                    '<span class="sht-batch-label" title="点击滚动定位到正文链接"></span>' +
                    '<button class="sht-batch-kbtn" data-kind="fanhao" title="本行按番号(电影)入库">番</button>' +
                    '<button class="sht-batch-kbtn" data-kind="non_fanhao" title="本行按非番号(剧集)入库">剧</button>' +
                    '<span class="sht-batch-res"></span>';
                const ck = row.querySelector('.sht-batch-ck');
                ck.checked = r.pick;
                const labelEl = row.querySelector('.sht-batch-label');
                labelEl.textContent = r.it.label;
                labelEl.addEventListener('click', () => flashTo(r.it.el));
                // 完整链接只放进 title 属性(不是文本节点), 这样 collectLinks 的文本扫描/清洗都不会碰它
                row.title = r.it.raw || r.it.norm;
                ck.addEventListener('change', () => {
                    r.pick = ck.checked;
                    if (!r.pick) r.ov = true;   // 手动取消勾选后不再跟随顶部默认类型(避免语义被覆盖)
                    row.classList.toggle('off', !r.pick);
                    updateCount();
                });
                const paintKind = () => {
                    row.querySelectorAll('.sht-batch-kbtn').forEach(b => {
                        b.classList.toggle('active', b.dataset.kind === r.kind);
                    });
                };
                row.querySelectorAll('.sht-batch-kbtn').forEach(b => {
                    b.addEventListener('click', (e) => {
                        e.stopPropagation();
                        r.kind = b.dataset.kind;
                        r.ov = true;
                        r.pick = true;
                        ck.checked = true;
                        row.classList.remove('off');
                        paintKind();
                        updateCount();
                    });
                });
                const resEl = row.querySelector('.sht-batch-res');
                resEl.textContent = r.res || '';
                resEl.className = 'sht-batch-res' + (r.res && r.res.indexOf('✅') === 0 ? ' ok'
                    : (r.res && r.res.indexOf('❌') === 0 ? ' bad' : ''));
                paintKind();
                r.rowEl = row;
                r.ck = ck;
                listBox.appendChild(row);
            });
        };

        // 服务端已建的这批任务, 把每行状态刷成 🕒/✅/❌
        const syncRows = () => {
            batchRows.forEach(r => {
                const t = taskByNorm.get(r.it.norm);
                if (!t || !r.rowEl) return;
                const resEl = r.rowEl.querySelector('.sht-batch-res');
                if (!resEl) return;
                resEl.textContent = taskStatusText(t);
                resEl.className = 'sht-batch-res' + (t.status === 'done' ? ' ok' : (t.status === 'failed' ? ' bad' : ''));
                if (t.status === 'done') r.ck.checked = false;
            });
        };

        const renderProgress = () => {
            if (!batchTasks.length) return;
            const done = batchTasks.filter(t => t.status === 'done').length;
            const fail = batchTasks.filter(t => t.status === 'failed').length;
            const run = batchTasks.length - done - fail;
            statusEl.className = fail ? 'sht-batch-status bad' : (run ? '' : 'sht-batch-status ok');
            statusEl.textContent = '⚡ 本次批量共 ' + batchTasks.length + ' 条: 完成 ' + done +
                ', 失败 ' + fail + ', 进行 ' + run +
                (run ? '(服务端按队列串行处理, 可关掉面板继续浏览)' : ' —— 已全部结束');
        };

        batchRender = () => { renderRows(); updateCount(); syncRows(); renderProgress(); };

        renderRows();
        updateCount();
        renderProgress();

        // 顶部默认类型/分类
        batchMask.querySelectorAll('.sht-batch-kind').forEach(k => {
            k.addEventListener('click', () => {
                batchMask.querySelectorAll('.sht-batch-kind').forEach(x => x.classList.remove('active'));
                k.classList.add('active');
                const kind = k.dataset.kind;
                // 只覆盖没有行内改过类型的行
                batchRows.forEach(r => { if (!r.ov) r.kind = kind; });
                renderRows();
                updateCount();
            });
        });
        // 恢复上次选的分类(没有则默认 av)
        try {
            const lastCat = GM_getValue('shtBatchCategory', 'av');
            if (lastCat) batchMask.querySelector('#sht-batch-category').value = lastCat;
        } catch (e) {}
        batchMask.querySelector('#sht-batch-category').addEventListener('change', (e) => {
            try { GM_setValue('shtBatchCategory', e.target.value); } catch (err) {}
        });

        // 快捷选择
        batchMask.querySelectorAll('.sht-batch-mini').forEach(b => {
            b.addEventListener('click', () => {
                const act = b.dataset.act;
                const submitted = new Set(batchTasks.map(t => t.norm));
                if (act === 'all') batchRows.forEach(r => { r.pick = true; });
                else if (act === 'none') batchRows.forEach(r => { r.pick = false; });
                else if (act === 'invert') batchRows.forEach(r => { r.pick = !r.pick; });
                else if (act === 'unsubmitted') batchRows.forEach(r => { r.pick = !submitted.has(r.it.norm); });
                renderRows();
                updateCount();
            });
        });

        const close = () => { if (batchMask) { batchMask.remove(); batchMask = null; batchRender = null; } };
        batchMask.querySelector('#sht-batch-close').addEventListener('click', close);
        batchMask.querySelector('#sht-batch-cancel').addEventListener('click', close);
        batchMask.addEventListener('click', (e) => { if (e.target === batchMask) close(); });

        // 预览(dry_run) / 提交
        const doSubmit = async (dryRun) => {
            const groups = batchGroups(batchRows);
            const total = groups.reduce((s, g) => s + g.rows.length, 0);
            if (!total) {
                statusEl.className = 'sht-batch-status bad';
                statusEl.textContent = '⚠️ 没有勾选任何链接';
                return;
            }
            okBtn.disabled = previewBtn.disabled = true;
            statusEl.className = '';
            statusEl.textContent = '⏳ ' + (dryRun ? '预览中' : '提交中') + ': ' + total + ' 条 / ' + groups.length + ' 批' +
                (total > BATCH_MAX ? '(超过单批 ' + BATCH_MAX + ' 条, 已自动分批提交)' : '') + '…';
            let okCount = 0, badCount = 0, skipCount = 0;
            const errs = [];
            for (const g of groups) {
                try {
                    const r = await api('POST', '/api/import/batch', {
                        links: g.rows.map(x => x.it.norm),
                        kind: g.kind,
                        category: batchDefaultCategory(),
                        thread_id: getThreadId() || undefined,
                        title: document.title,
                        thread_url: location.href,
                        dry_run: !!dryRun
                    });
                    if (r.error) throw new Error(r.error);
                    // 结果对号入座: 优先按 norm 匹配, 兜底按行号(服务端 items[].line 是提交顺序)
                    const byNorm = new Map();
                    (r.items || []).forEach(x => {
                        const n = normLink(x.magnet);
                        if (n) byNorm.set(n, x);
                    });
                    const byLine = new Map();
                    (r.items || []).forEach(x => byLine.set(x.line, x));
                    (r.errors || []).forEach(e => {
                        errs.push('第 ' + (e.line || '?') + ' 条: ' + (e.error || '无效'));
                    });
                    skipCount += r.skipped || 0;
                    g.rows.forEach((rr, i) => {
                        const x = byNorm.get(rr.it.norm) || byLine.get(i + 1) || {};
                        if (dryRun) {
                            if (x.ok === false || x.error) { rr.res = '❌ ' + (x.error || '不支持'); badCount++; }
                            else { rr.res = '👁 可提交'; okCount++; }
                        } else if (x.ok && x.task_id) {
                            rr.res = '✅ ' + x.task_id;
                            rr.pick = false;
                            okCount++;
                            const t = { task_id: x.task_id, norm: rr.it.norm, label: rr.it.label,
                                        kind: g.kind, status: 'queued', msg: '', step: '' };
                            batchTasks.push(t);
                            taskByNorm.set(rr.it.norm, t);
                        } else {
                            rr.res = '❌ ' + (x.error || '未建任务');
                            badCount++;
                        }
                    });
                } catch (e) {
                    g.rows.forEach(rr => { rr.res = '❌ ' + e.message; badCount++; });
                    errs.push(e.message);
                }
            }
            okBtn.disabled = previewBtn.disabled = false;
            renderRows();
            updateCount();
            if (dryRun) {
                statusEl.className = errs.length ? 'sht-batch-status bad' : 'sht-batch-status ok';
                statusEl.textContent = '👁 预览: ' + okCount + ' 条将被提交' +
                    (badCount ? ', ' + badCount + ' 条无效' : '') +
                    (skipCount ? ', ' + skipCount + ' 条被服务端跳过(重复等)' : '') +
                    (errs.length ? ' —— ' + errs.slice(0, 3).join(' / ') : '');
                return;
            }
            syncRows();
            startBatchPolling();
            refreshBatchBadge(false);
            if (nav) renderNavList(true);
            renderProgress();
            if (errs.length) {
                statusEl.textContent = '🚀 已提交 ' + okCount + ' 条' + (badCount ? ', ' + badCount + ' 条未建任务' : '') +
                    ' —— 忽略的: ' + errs.slice(0, 2).join(' / ');
            }
        };
        okBtn.addEventListener('click', () => { if (!okBtn.disabled) doSubmit(false); });
        previewBtn.addEventListener('click', () => { if (!previewBtn.disabled) doSubmit(true); });
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
                .replace(/magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}[\s\S]*?(?=\n|$)/g, ' ')
                .replace(/ed2k:\/\/\|file\|[^|\n]+\|\d+\|[0-9a-fA-F]{32,40}\|\/?/g, ' ')
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
        closeOverlays('meta');            // 关掉批量/手动输入弹层, 避免两个面板叠在一起 (v1.11.0)
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
    let nav = null, navList = null, navCollapsed = false, navResizeBound = false;

    // 落位持久化 (v1.12.0): 拖动后记住 left/top, 双击标题栏复位到左上角
    const NAV_POS_KEY = 'shtNavPos';
    const NAV_HOME = { left: 16, top: 16 };
    function applyNavPos(pos) {
        if (!nav || !pos) return;
        const w = nav.offsetWidth || 280;
        const maxLeft = Math.max(0, window.innerWidth - w);
        const maxTop = Math.max(0, window.innerHeight - 40);
        nav.style.left = Math.min(Math.max(0, Number(pos.left) || 0), maxLeft) + 'px';
        nav.style.top = Math.min(Math.max(0, Number(pos.top) || 0), maxTop) + 'px';
        nav.style.right = 'auto';
        nav.style.bottom = 'auto';
    }
    function restoreNavPos() {
        let pos = null;
        try { pos = JSON.parse(GM_getValue(NAV_POS_KEY, '') || 'null'); } catch (e) { pos = null; }
        applyNavPos(pos);
    }
    function saveNavPos() {
        if (!nav) return;
        const num = (v, fb) => { const n = parseFloat(v); return isFinite(n) ? n : fb; };
        const left = num(nav.style.left, nav.offsetLeft);
        const top = num(nav.style.top, nav.offsetTop);
        try { GM_setValue(NAV_POS_KEY, JSON.stringify({ left: left, top: top })); } catch (e) {}
    }
    function resetNavPos() {
        try { GM_setValue(NAV_POS_KEY, ''); } catch (e) {}
        nav.style.left = NAV_HOME.left + 'px';
        nav.style.top = NAV_HOME.top + 'px';
        nav.style.right = 'auto';
        nav.style.bottom = 'auto';
        toast('面板已复位到左上角');
    }

    function buildNav() {
        if (nav) { nav.remove(); }
        nav = document.createElement('div');
        nav.id = 'sht-nav';
        nav.innerHTML =
            '<div id="sht-nav-head">' +
            '  <div id="sht-nav-headrow">' +
            '    <span id="sht-nav-title">📌 磁力导航</span>' +
            '    <span id="sht-nav-count"></span>' +
            '    <span id="sht-nav-badge" title="批量入库进度(点击查看)"></span>' +
            '    <button id="sht-nav-fold" title="折叠/展开 (双击标题栏复位到左上角)">—</button>' +
            '  </div>' +
            '  <div id="sht-nav-toolbar">' +
            '    <button id="sht-nav-batch" title="批量入库: 一次提交本页全部(或勾选)磁力/ed2k">⚡ 批量</button>' +
            '    <button id="sht-nav-tasks" title="打开任务监控页">📋 任务</button>' +
            '    <button id="sht-nav-meta" title="上传元数据(海报/简介)">📤 元数据</button>' +
            '    <button id="sht-nav-manual" title="手动输入磁力/ed2k 链接入库">✏️ 手动</button>' +
            '  </div>' +
            '</div>' +
            '<div id="sht-nav-body">' +
            '  <div id="sht-nav-list"></div>' +
            '</div>';
        document.body.appendChild(nav);
        navList = nav.querySelector('#sht-nav-list');
        restoreNavPos();

        nav.querySelector('#sht-nav-tasks').addEventListener('click', (e) => {
            e.stopPropagation();
            window.open(API_BASE + '/tasks', '_blank');
        });

        // ⚡ 批量入库 (v1.11.0): 一次提交本页全部/勾选磁力
        nav.querySelector('#sht-nav-batch').addEventListener('click', (e) => {
            e.stopPropagation();
            openBatchPanel();
        });
        nav.querySelector('#sht-nav-badge').addEventListener('click', (e) => {
            e.stopPropagation();
            openBatchPanel();
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

        // v1.12.0: 窗口变小/放大后, 把面板收回可视范围(不改变用户存的位置, 只做边界收敛)
        if (!navResizeBound) {
            navResizeBound = true;
            window.addEventListener('resize', () => {
                if (nav) applyNavPos({ left: parseFloat(nav.style.left) || nav.offsetLeft, top: parseFloat(nav.style.top) || nav.offsetTop });
            });
        }

        // ===== 拖拽移动悬浮窗 (按住标题栏拖动) =====
        const head = nav.querySelector('#sht-nav-head');
        let drag = null;
        head.addEventListener('pointerdown', (e) => {
            if (e.target.closest('button')) return;   // 按钮区域不触发拖拽
            if (e.target.closest('#sht-nav-toolbar')) return;  // 工具条整片不拖拽
            e.preventDefault();
            const r = nav.getBoundingClientRect();
            drag = { sx: e.clientX, sy: e.clientY, ox: r.left, oy: r.top, moved: false };
            nav.style.left = drag.ox + 'px';
            nav.style.top = drag.oy + 'px';
            nav.style.right = 'auto';
            nav.style.bottom = 'auto';
            try { head.setPointerCapture && head.setPointerCapture(e.pointerId); } catch (err) {}
            head.classList.add('dragging');
        });
        head.addEventListener('pointermove', (e) => {
            if (!drag) return;
            const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
            if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
            nav.style.left = (drag.ox + dx) + 'px';
            nav.style.top = (drag.oy + dy) + 'px';
            nav.style.right = 'auto';
            nav.style.bottom = 'auto';
        });
        const endDrag = (e) => {
            if (!drag) return;
            const moved = drag.moved;
            drag = null;
            head.classList.remove('dragging');
            try { if (head.releasePointerCapture && e.pointerId) head.releasePointerCapture(e.pointerId); } catch (err) {}
            if (moved) saveNavPos();   // v1.12.0: 记住落位, 刷新后仍在原地
        };
        head.addEventListener('pointerup', endDrag);
        head.addEventListener('pointercancel', endDrag);

        // 双击标题行 → 复位到左上角默认落位
        nav.querySelector('#sht-nav-headrow').addEventListener('dblclick', (e) => {
            e.stopPropagation();
            resetNavPos();
        });

        renderNavList();
    }

    function renderNavList(force) {
        const key = items.map(i => i.norm).join('|');
        if (!force && key === lastNavKey) return;
        lastNavKey = key;
        navList.innerHTML = '';
        items.forEach((it, idx) => {
            const row = document.createElement('div');
            row.className = 'sht-nav-item';
            row.dataset.idx = idx;                                 // v1.13.0: 库存徽章按索引回填
            row.innerHTML = '<span class="sht-nav-idx">' + (idx + 1) + '</span><span class="sht-nav-label"></span><span class="sht-nav-inv"></span><span class="sht-nav-st"></span><span class="sht-nav-import" title="入库此链接 (可先选类型)">🚀</span><span class="sht-nav-copy" title="复制完整磁力链接">📋</span><span class="sht-nav-go">↘</span>';
            row.querySelector('.sht-nav-label').textContent = it.label;
            row.title = it.raw || it.norm;
            // v1.11.0: 批量入库建过任务的行, 在末尾显示 🕒/✅/❌ (点 🚀 可看任务详情, 不再重复提交)
            const t = taskByNorm.get(it.norm);
            if (t) {
                const st = row.querySelector('.sht-nav-st');
                st.textContent = t.status === 'done' ? '✅' : (t.status === 'failed' ? '❌' : '🕒');
                st.title = '批量任务 ' + t.task_id + (t.msg ? ': ' + t.msg : '');
            }
            row.querySelector('.sht-nav-copy').addEventListener('click', (e) => {
                e.stopPropagation();
                copyMagnet(it.raw || it.norm);
            });
            row.querySelector('.sht-nav-import').addEventListener('click', (e) => {
                e.stopPropagation();
                const cur = taskByNorm.get(it.norm);
                if (cur && cur.status === 'done') { toast('这条已入库: ' + cur.task_id, true); return; }
                if (cur && cur.status !== 'failed') { toast('这条已在队列里: ' + cur.task_id + ' (' + stepLabel(cur) + ')', true); return; }
                openKindPanel(it, row.querySelector('.sht-nav-import'), e);
            });
            row.addEventListener('click', () => {
                flashTo(it.el);
            });
            navList.appendChild(row);
        });
        nav.querySelector('#sht-nav-count').textContent = '(' + items.length + ')';
        invRender();          // v1.13.0: 先用缓存里的结果把已有的徽章画上
        invQuery(items.map(i => i.norm));   // 内部按缓存过滤: 已查过的一个请求都不发
    }

    // ===== 库存徽章 (v1.13.0, 2026-09-21): 这条磁链的东西在我 115 里有没有 =====
    // 服务端 POST /api/import/lookup 一次带走整页磁链, 返回三态:
    //   in 在库 / out 不在库 / unknown 未校验 (MCP 掉线·超时 / 限频窗口内 / 链接里没番号)
    // ⚠️ unknown 绝不渲染成"不在库" —— MCP 一掉线全页显示"不在库"会让人重复入库。
    // ⚠️ 只在 renderNavList 里 norm 列表真的变了之后查一次; 监听整个 DOM 变化 = 滚一次几十个请求。
    const INV_ON = true;        // 总开关: 嫌吵/出问题临时置 false 即可 (不用改服务端)
    const INV_TTL_MS = 10 * 60 * 1000;      // in/out 的缓存寿命
    const INV_TTL_UNK_MS = 60 * 1000;       // unknown 只压 1 分钟: MCP 掉线/限频是瞬时的,
                                            // 压 10 分钟会让这一页一直停在"未校验"不自己恢复
    const INV_MAX = 40;         // 与服务端 INV_MAX_LINKS 对齐
    const CTX_LEVELS = 5;       // 页面上下文往上爬几层 DOM (就近在最前, 与服务端"唯一候选"配合)
                                // 色花堂的磁链在 div.blockcode>ol>li 里, 番号在同一帖的 td.t_f 里
                                // —— li→ol→blockcode→t_f 已经是第 4 层, 所以至少要 4, 留 5 更稳
    const CTX_MAX = 200;        // 每层文本截断 (与服务端 _inv_clean_ctxs 的 max_len 对齐)
    const invCache = new Map(); // norm -> { item, ts }
    function invExpired(rec, now) {
        const ttl = (rec.item && rec.item.state === 'unknown') ? INV_TTL_UNK_MS : INV_TTL_MS;
        return (now - rec.ts) > ttl;
    }
    const INV_TXT = { in: '✅在库', out: '❓不在库', unknown: '⚠️未校验' };
    const INV_WHY = {
        no_fanhao: '链接里和页面文本里都没解析出番号, 无从反查',
        mcp_down: '115 MCP 无响应(掉线/超时), 本次未校验',
        ratelimit: '115 处于限频窗口, 本次未校验'
    };

    function invTitle(r) {
        const fh = r.fanhao ? ('番号 ' + r.fanhao) : '番号未解析出';
        if (r.state === 'in') {
            const where = r.checked === 'ledger'
                ? '入库台账: 这条磁链曾入库成功'
                : (r.checked === 'ledger_fanhao'
                    ? '入库台账: 这个番号已入库 (按番号匹配, 可能是另一条磁链推的)'
                    : '115 全盘搜索命中');
            const n = r.count
                ? ('命中 ' + r.count + ' 项' + (r.video ? (', 其中视频 ' + r.video + ' 个') : '') +
                   (r.dir ? ', 含同名目录' : ''))
                : '';
            return '✅ 在库 —— ' + where + '\n' + fh + (n ? '\n' + n : '');
        }
        if (r.state === 'out') {
            // 说清"搜不到"不等于"确定没有": 搜索是模糊匹配, 也可能文件被改名/挪走
            return '❓ 不在库 —— 115 搜索 ' + (r.fanhao || '') + ' 干净返回 0 条\n' + fh +
                   '\n(仅表示当前搜不到, 不等于确定没有)';
        }
        return '⚠️ 未校验 —— ' + (INV_WHY[r.reason] || r.reason || '未知原因') +
               (r.fanhao ? ('\n' + fh) : '') + '\n(点 🚀 仍可正常入库)';
    }

    function invRender() {
        if (!navList) return;
        const now = Date.now();
        navList.querySelectorAll('.sht-nav-item').forEach(row => {
            const el = row.querySelector('.sht-nav-inv');
            const it = items[Number(row.dataset.idx)];
            if (!el || !it) return;
            const rec = invCache.get(it.norm);
            if (rec && invExpired(rec, now)) { invCache.delete(it.norm); }
            const cur = invCache.get(it.norm);
            el.className = 'sht-nav-inv';
            if (!cur || !cur.item || !INV_TXT[cur.item.state]) {
                el.textContent = '';
                el.removeAttribute('title');
                return;
            }
            el.textContent = INV_TXT[cur.item.state];
            el.classList.add(cur.item.state === 'in' ? 'inv-in'
                           : (cur.item.state === 'out' ? 'inv-out' : 'inv-unk'));
            el.title = invTitle(cur.item);
        });
    }

    // 只查缓存里没有的; > 40 条按片串行发 (服务端自己还有并发/分片/限频护栏)
    // ===== 页面上下文 (v1.14.0, B 方案): 裸磁链没有 &dn=, 服务端抠不出番号 =====
    // 从这条磁链所在的 DOM 位置往外爬, 就近 → 逐层放宽, 交给服务端"只认唯一候选"。
    // 为什么必须剔掉自己: 磁链原文里的 btih hex 会被番号正则当成噪声, 混进候选就选不出唯一解。
    function contextFor(it) {
        const out = [];
        const el = (it && it.el) || null;
        if (!el || (el.isConnected === false)) return out;
        const self = String((it && it.raw) || '').replace(/\s+/g, ' ').trim();
        let node = el;
        for (let i = 0; i < CTX_LEVELS && node && node !== document.body; i++, node = node.parentElement) {
            let t = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
            if (self) t = t.split(self).join(' ');
            t = t.replace(/\s+/g, ' ').trim();
            if (t) out.push(t.slice(0, CTX_MAX));
        }
        // 页面级标题: 只在"本页只有这一条链接"时才敢带上 (12 条磁力共用一个标题必然串味,
        // 服务端会按"同一段文本供出 ≥2 条磁链"把它整个作废)
        if (items.length === 1) {
            const sub = document.querySelector('#thread_subject') || document.querySelector('h1.ts');
            const t2 = String((sub && (sub.innerText || sub.textContent)) || document.title || '')
                .replace(/\s+/g, ' ').trim();
            if (t2) out.push(t2.slice(0, CTX_MAX));
        }
        return out;
    }

    function invQuery(norms) {
        if (!INV_ON || !norms || !norms.length) return Promise.resolve();        const byNorm = new Map(items.map(i => [i.norm, i]));
        // 顺手把过期项摘掉 (invRender 也做, 但那条路径不保证先跑), unknown 1 分钟就过期,
        // 所以 MCP 掉线/限频之后重渲染一次就能自己恢复, 不会永远停在"未校验"
        const want = norms.filter(n => {
            const rec = invCache.get(n);
            if (rec && invExpired(rec, Date.now())) invCache.delete(n);
            return !invCache.has(n);
        });
        if (!want.length) return Promise.resolve();
        const run = (list) => {
            if (!list.length) return Promise.resolve();
            const chunk = list.slice(0, INV_MAX), rest = list.slice(INV_MAX);
            // 送 raw 而不是 norm: norm 丢掉了 &dn= 文件名, 服务端少了"从文件名抠番号"这条回退
            const links = chunk.map(n => ((byNorm.get(n) || {}).raw || n));
            const ctxs = chunk.map(n => contextFor(byNorm.get(n) || {}));
            // v1.14.0: 查库存和入库用同一份帖子上下文 —— 裸磁链的番号只能从页面文本抠。
            // single 必须显式传: 41 条以上的帖子分片后第二片只剩 1 条, 服务端按长度猜
            // 就会把整页标题套到它头上。
            return api('POST', '/api/import/lookup', {
                links: links,
                title: document.title,
                thread_id: getThreadId() || undefined,
                thread_url: location.href,
                contexts: ctxs,
                single: items.length === 1
            })
                .then(res => {
                    const arr = (res && res.items) || [];
                    // unknown 也缓存, 但只有 1 分钟寿命 (见 INV_TTL_UNK_MS): 不缓存的话
                    // 徽章永远画不出来 (invRender 只从缓存取数), 缓存太久又不会自愈。
                    chunk.forEach((n, i) => {
                        if (arr[i]) invCache.set(n, { item: arr[i], ts: Date.now() });
                    });
                    invRender();
                    // 每轮都打一行: 番号来源分账 (magnet=链接自带 / ctx=页面上下文 / title=页面标题 /
                    // ledger=台账 hash / ''=没抠出来)。核对"抠出来的是不是真番号"就看这里。
                    const bySrc = {};
                    arr.forEach(i => {
                        const s = (i && i.src) || '(无)';
                        (bySrc[s] = bySrc[s] || []).push((i && i.fanhao) || '-');
                    });
                    console.log('[sht] 库存反查: %d 条 | 115 请求 %d 次 | %dms | %o | 番号来源 %o',
                                arr.length, (res && res.req_115) || 0, (res && res.elapsed_ms) || 0,
                                (res && res.counts) || {}, bySrc);
                    // v1.14.1 诊断: 只要有人是靠"页面文本"判出来的(或一个都没抠出来), 就把送出去的
                    // 页面文本原样打出来 (由近到远的 DOM 层), 用来核对"抠出来的到底是不是真番号",
                    // 以及"是真没有番号, 还是我们爬错了层"。平时这行不出现。
                    if (ctxs && arr.length && (arr.some(i => (i && i.src) === 'ctx' || (i && i.src) === 'title') ||
                                 arr.every(i => !(i && i.fanhao)))) {
                        const probe = [];
                        for (let i = 0; i < Math.min(3, arr.length); i++) {
                            probe.push({ 番号: (arr[i] && arr[i].fanhao) || '-',
                                         来源: (arr[i] && arr[i].src) || '(无)',
                                         页面文本: (ctxs[i] || []) });
                        }
                        console.log('[sht] 诊断 · 送给服务端的页面文本 (由近到远): %o', probe);
                    }
                    return run(rest);
                })
                .catch(e => {
                    // 本地服务不通 / 报错 → 本次不画徽章。徽章是增强项, 不画 = 不暗示"不在库"。
                    console.warn('[sht] 库存反查失败, 本次不显示徽章:', (e && e.message) || e);
                });
        };
        return run(want);
    }

    // 某条入库成功后: 丢掉该条的缓存并只重查这一条 (徽章从 ❓/⚠️ 翻成 ✅)
    function invRefetchNorm(norm) {
        if (!INV_ON || !norm) return;
        invCache.delete(norm);
        invQuery([norm]);
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
#sht-nav{position:fixed;left:16px;top:16px;z-index:2147483647;width:280px;max-height:70vh;display:flex;flex-direction:column;background:rgba(15,23,42,.94);border:1px solid #334155;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}
#sht-nav-head{display:flex;flex-direction:column;gap:6px;padding:8px 10px;background:linear-gradient(135deg,#e63946,#d90429);color:#fff;font-size:13px;font-weight:700;cursor:grab;user-select:none;touch-action:none}
#sht-nav-head:active{cursor:grabbing}
#sht-nav-head.dragging{cursor:grabbing}
#sht-nav-headrow{display:flex;align-items:center;gap:6px;min-width:0}
#sht-nav-title{flex:none;white-space:nowrap}
#sht-nav-count{flex:none;opacity:.85;font-weight:400}
#sht-nav-toolbar{display:flex;gap:6px}
#sht-nav-toolbar button{flex:1 1 0;min-width:0;padding:3px 0;border:none;border-radius:6px;background:rgba(255,255,255,.18);color:#fff;font-size:11px;font-weight:600;font-family:inherit;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sht-nav-toolbar button:hover{background:rgba(255,255,255,.38)}
#sht-nav-toolbar button:active{background:rgba(255,255,255,.5)}
#sht-nav-fold{margin-left:auto;flex:none;padding:1px 8px;border:none;border-radius:6px;background:rgba(255,255,255,.2);color:#fff;font-size:13px;line-height:1.35;cursor:pointer}
#sht-nav-fold:hover{background:rgba(255,255,255,.38)}
#sht-nav-body{overflow-y:auto;max-height:calc(70vh - 78px)}
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
.sht-pop{position:fixed;z-index:2147483648;width:200px;background:rgba(15,23,42,.97);border:1px solid #475569;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:10px;font-family:'Segoe UI',system-ui,sans-serif}
.sht-pop-title{color:#e2e8f0;font-size:12px;font-weight:700;margin-bottom:8px}
.sht-pop-kind{display:block;width:100%;margin:4px 0;padding:6px 8px;border:none;border-radius:6px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer;text-align:left}
.sht-pop-kind:hover{background:#e63946}
.sht-pop-cat{margin:8px 0 2px;padding-top:6px;border-top:1px dashed #334155}
.sht-pop-cat label{display:block;color:#94a3b8;font-size:11px;margin-bottom:4px}
.sht-pop-cat-select{width:100%;padding:5px 6px;border:none;border-radius:6px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer}
.sht-pop-sub{color:#94a3b8;font-size:11px;margin-top:6px;word-break:break-all;max-height:60px;overflow:hidden}
#sht-mask{position:fixed;inset:0;z-index:2147483649;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}
#sht-modal,#sht-meta-modal,#sht-batch-modal{width:min(560px,92vw);max-height:82vh;overflow:auto;background:#0f172a;border:1px solid #334155;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.6);padding:14px 16px;font-family:'Segoe UI',system-ui,sans-serif;display:flex;flex-direction:column;gap:10px}
#sht-batch-modal{width:min(680px,94vw)}
#sht-modal-title{display:flex;align-items:center;gap:8px;color:#f8fafc;font-size:14px;font-weight:700}
#sht-meta-tid,#sht-modal-tid,#sht-batch-tid{margin-left:auto;color:#94a3b8;font-size:12px;font-weight:400}
#sht-meta-close,#sht-modal-close,#sht-batch-close{padding:1px 8px;border:none;border-radius:6px;background:#1e293b;color:#94a3b8;font-size:14px;cursor:pointer}
#sht-meta-close:hover,#sht-modal-close:hover,#sht-batch-close:hover{background:#e63946;color:#fff}
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
.sht-modal-kind{display:inline-block;padding:5px 12px;border:1px solid #334155;border-radius:8px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer;margin-right:8px}
.sht-modal-kind:hover,.sht-batch-kind:hover{border-color:#e63946}
.sht-modal-kind.active,.sht-batch-kind.active{background:#e63946;border-color:#e63946;color:#fff;font-weight:600}
#sht-modal-category,#sht-batch-category{padding:5px 10px;border:1px solid #334155;border-radius:8px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer}
#sht-modal-status{color:#e2e8f0;font-size:12px;min-height:18px;word-break:break-all}
#sht-modal-status.sht-done{color:#2dc653;font-weight:600}
#sht-modal-status.sht-fail{color:#ef4444}
.sht-modal-actions{display:flex;justify-content:flex-end;gap:8px}
#sht-meta-cancel,#sht-meta-ok,#sht-modal-cancel,#sht-modal-ok,#sht-batch-cancel,#sht-batch-preview,#sht-batch-ok{padding:7px 16px;border:none;border-radius:8px;font-size:13px;cursor:pointer}
#sht-meta-cancel,#sht-modal-cancel,#sht-batch-cancel,#sht-batch-preview{background:#1e293b;color:#94a3b8}
#sht-meta-cancel:hover,#sht-modal-cancel:hover,#sht-batch-cancel:hover,#sht-batch-preview:hover{background:#334155;color:#e2e8f0}
#sht-meta-ok,#sht-modal-ok,#sht-batch-ok{background:linear-gradient(135deg,#e63946,#d90429);color:#fff;font-weight:600}
#sht-meta-ok:hover,#sht-modal-ok:hover,#sht-batch-ok:hover{filter:brightness(1.1)}
#sht-meta-ok:disabled,#sht-modal-ok:disabled,#sht-batch-ok:disabled,#sht-batch-preview:disabled{cursor:wait;opacity:.75}
/* ===== 批量入库 (v1.11.0) ===== */
#sht-nav-badge{flex:none;color:#fff;font-size:11px;font-weight:700;background:rgba(0,0,0,.28);border-radius:8px;padding:1px 6px;cursor:pointer;white-space:nowrap}
#sht-nav-badge:empty{display:none}
.sht-nav-st{flex:none;font-size:11px;min-width:0;cursor:help}
/* ===== 库存徽章 (v1.13.0): 115 里有没有这条 ===== */
.sht-nav-inv{flex:none;font-size:11px;white-space:nowrap;cursor:help}
.sht-nav-inv:empty{display:none}
.sht-nav-inv.inv-in{color:#4ade80}
.sht-nav-inv.inv-out{color:#64748b}
.sht-nav-inv.inv-unk{color:#fbbf24}
#sht-batch-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;background:#0b1220;border:1px solid #1e293b;border-radius:10px}
.sht-batch-lbl{color:#94a3b8;font-size:12px}
.sht-batch-kind{display:inline-block;padding:5px 12px;border:1px solid #334155;border-radius:8px;background:#1e293b;color:#e2e8f0;font-size:12px;cursor:pointer}
#sht-batch-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:#94a3b8;font-size:12px}
#sht-batch-count{margin-right:auto}
.sht-batch-mini{padding:3px 10px;border:1px solid #334155;border-radius:7px;background:#1e293b;color:#cbd5e1;font-size:12px;cursor:pointer}
.sht-batch-mini:hover{border-color:#e63946;color:#fff}
#sht-batch-list{max-height:46vh;overflow-y:auto;border:1px solid #1e293b;border-radius:10px;background:#0b1220;padding:4px}
.sht-batch-row{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;color:#e2e8f0;font-size:12px}
.sht-batch-row:hover{background:#1e293b}
.sht-batch-row.off{opacity:.45}
.sht-batch-row.done .sht-batch-label{color:#2dc653}
.sht-batch-row.fail .sht-batch-label{color:#f87171}
.sht-batch-ck{flex:none;width:15px;height:15px;cursor:pointer;accent-color:#e63946}
.sht-batch-idx{flex:none;min-width:22px;text-align:center;background:#334155;color:#e2e8f0;border-radius:4px;font-size:11px;font-weight:700;padding:1px 4px}
.sht-batch-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.sht-batch-label:hover{color:#fff;text-decoration:underline}
.sht-batch-kbtn{flex:none;width:24px;padding:1px 0;border:1px solid #334155;border-radius:5px;background:#1e293b;color:#94a3b8;font-size:11px;cursor:pointer}
.sht-batch-kbtn:hover{border-color:#e63946;color:#fff}
.sht-batch-kbtn.active{background:#e63946;border-color:#e63946;color:#fff;font-weight:700}
.sht-batch-res{flex:none;min-width:74px;text-align:right;font-size:11px;color:#94a3b8;word-break:break-all}
.sht-batch-res.ok{color:#2dc653;font-weight:600}
.sht-batch-res.bad{color:#ef4444}
.sht-batch-empty{color:#64748b;font-size:12px;padding:14px;text-align:center}
#sht-batch-status{color:#e2e8f0;font-size:12px;min-height:18px;word-break:break-all}
#sht-batch-status.ok{color:#2dc653;font-weight:600}
#sht-batch-status.bad{color:#ef4444}
@media (max-width:640px){
  .sht-batch-res{min-width:0}
  #sht-batch-list{max-height:38vh}
}
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
        // v1.11.0: 批量面板开着时页面 DOM 一变(items 数组整体重建), 面板里的行要跟着重新对齐,
        // 否则行还指向已失效的旧 item 对象(标签/滚动定位就不准了)
        if (batchMask) {
            remapBatchRows();
            if (batchRender) batchRender();
        }
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
