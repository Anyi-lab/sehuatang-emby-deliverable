# -*- coding: utf-8 -*-
"""index_page.py — 一键入库首页 (GET /)

2026-09-20 重写 (用户反馈「入库界面比较简陋 + 手动磁链没分类 + 最好支持多磁链」):
  - 批量提交: 多行文本一行一条, 支持 magnet:?xt=urn:btih:… / ed2k:// / 裸 40 位 BTIH
  - 分类下拉: 值来自后端 CATEGORY_MAP 单一来源 (渲染时 __CATS_JSON__ 注入), 不再前端硬编码
  - 行尾 "#分类key" 可单条覆盖本批默认分类 (混批场景)
  - 解析预览走 POST /api/import/batch?dry_run=1 (服务端同一套解析逻辑, 前端不重复实现)
  - 批次进度: 每条独立轮询 /api/import/status, 记录落 localStorage 刷新不丢
  - 最近入库 + 队列统计: /api/import/list (10s 自动刷新)

2026-09-22 防重 (用户实测: 面板连点两次 → 28 条并发建任务, 13 条白跑一轮):
  - 前端: 同一批链接 60s 内重复提交直接拦 (指纹=排序后的链接 key+类型+分类, 落 localStorage,
    关页面重开也拦); 全失败不记指纹, 允许立刻重试。
  - 服务端: POST /api/import/batch 按链接 hash 查重, 同 hash 已有 queued/running 任务则跳过
    (第二道兜底; 结果里回 dup_skipped + duplicates[] 明细)。
"""

import json

INDEX_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>一键入库</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:50}
.header h1{font-size:19px;color:#58a6ff}
.header .tag{font-size:12px;color:#8b949e;background:#21262d;padding:3px 10px;border-radius:12px}
.header .spacer{flex:1}
.container{max-width:1280px;margin:0 auto;padding:20px 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}
.card .lbl{font-size:12px;color:#8b949e}
.card .num{font-size:22px;font-weight:700;margin-top:3px}
.card.queued .num{color:#8b949e}.card.running .num{color:#58a6ff}
.card.pending_manual .num{color:#d29922}.card.done .num{color:#3fb950}
.card.failed .num{color:#f85149}.card.total .num{color:#58a6ff}
.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px 18px;margin-bottom:14px}
.panel h2{font-size:15px;color:#58a6ff;margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel h2 .sub{font-size:12px;color:#8b949e;font-weight:400}
.panel h2 .right{margin-left:auto;font-weight:400}
textarea#links{width:100%;min-height:150px;padding:12px 14px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#c9d1d9;font-size:13px;line-height:1.65;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;outline:none;resize:vertical}
textarea#links:focus{border-color:#58a6ff}
textarea#links::placeholder{color:#5b6672}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
.toolbar label{color:#8b949e;font-size:13px;white-space:nowrap}
select{padding:8px 11px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px;outline:none}
/* ===== 按钮统一规范 (2026-09-20)：所有页面同一套尺寸/配色，头部只放跨页导航 ===== */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:34px;padding:0 15px;border:1px solid transparent;border-radius:6px;font-family:inherit;font-size:13px;font-weight:600;line-height:1;cursor:pointer;white-space:nowrap;color:#c9d1d9;text-decoration:none;transition:background .15s,border-color .15s,color .15s}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.sm{height:30px;padding:0 11px;font-size:12.5px;font-weight:600}
.btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn.primary:hover:enabled{background:#388bfd;border-color:#388bfd}
.btn.ghost{background:#21262d;border-color:#30363d;color:#c9d1d9}
.btn.ghost:hover:enabled{background:#30363d;border-color:#8b949e}
.btn.ok{background:#238636;border-color:#2ea043;color:#fff}
.btn.ok:hover:enabled{background:#2ea043}
.btn.warn{background:#21262d;border-color:#9e6a03;color:#d29922}
.btn.warn:hover:enabled{background:#3d2e00}
.btn.danger{background:#21262d;border-color:#4d2c2c;color:#f85149}
.btn.danger:hover:enabled{background:#3d1418;border-color:#f85149}
.btn.purple{background:#6e40c9;border-color:#6e40c9;color:#fff}
.btn.purple:hover:enabled{background:#8957e5}
.nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* 非当前页导航必须显式声明 color/background：.btn 基础样式没有 color，锚点会回退成浏览器默认
   链接色(未访问 #0000ee / 已访问 #551a8b)，在 #161b22 上只有 1.8:1，看着就是糊在背景上的暗字 */
.nav a.btn{background:#21262d;border-color:#30363d;color:#c9d1d9}
.nav a.btn:hover{background:#30363d;border-color:#8b949e;color:#c9d1d9}
.nav a.btn.cur{background:#193656;border-color:#58a6ff;color:#58a6ff}
.nav a.btn.avdb.cur{background:#483600;border-color:#d29922;color:#d29922}
.plink{color:#58a6ff;font-size:12px;text-decoration:none}
.plink:hover{text-decoration:underline}
.panel-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid #21262d}
.live{margin-left:auto;font-size:12px;color:#8b949e;white-space:nowrap}
.live b{color:#c9d1d9}
.live .good{color:#3fb950}.live .warn{color:#d29922}.live .bad{color:#f85149}
.cathint{font-size:12px;color:#8b949e;margin-top:10px;line-height:1.8;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px 12px}
.cathint code{color:#79c0ff;background:#161b22;padding:1px 5px;border-radius:4px;font-size:11px}
.parse{margin-top:12px;border-top:1px solid #21262d;padding-top:10px;display:none}
.parse.show{display:block}
.ptable{width:100%;border-collapse:collapse;font-size:12px}
.ptable th{text-align:left;color:#8b949e;font-weight:600;padding:5px 8px;border-bottom:1px solid #21262d;white-space:nowrap}
.ptable td{padding:5px 8px;border-bottom:1px solid #161b22;vertical-align:top}
.ptable tr.bad td{background:#3d141822}
.link{font-family:ui-monospace,Menlo,Consolas,monospace;color:#8b949e;word-break:break-all}
.catpill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;white-space:nowrap}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap}
.badge.queued{background:#21262d;color:#8b949e;border:1px solid #30363d}
.badge.running{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb}
.badge.pending_manual{background:#d2992222;color:#d29922;border:1px solid #d29922}
.badge.done{background:#23863622;color:#3fb950;border:1px solid #2ea043}
.badge.failed{background:#f8514922;color:#f85149;border:1px solid #f85149}
.brow{display:flex;align-items:center;gap:10px;padding:8px 2px;border-top:1px solid #21262d;font-size:13px}
.brow:first-child{border-top:none}
.brow .idx{color:#8b949e;font-size:12px;min-width:26px}
.brow .lnk{flex:1;min-width:0;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#8b949e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.brow .msg{color:#8b949e;font-size:12px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.progress{width:90px;height:8px;background:#21262d;border-radius:4px;overflow:hidden;flex:none}
.progress .fill{height:100%;background:#58a6ff;border-radius:4px;transition:width .4s}
.progress .fill.done{background:#3fb950}
.progress .fill.failed{background:#f85149}
.pct{font-size:11px;color:#8b949e;width:32px;text-align:right;flex:none}
table.rt{width:100%;border-collapse:collapse;font-size:13px}
.tw{overflow-x:auto}
table.rt th{text-align:left;padding:8px 10px;background:#21262d;color:#8b949e;font-weight:600;font-size:12px;white-space:nowrap}
table.rt td{padding:8px 10px;border-top:1px solid #21262d;vertical-align:middle}
table.rt tr:hover td{background:#11161d}
.rt .ttl{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rt .m{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#8b949e}
.empty{text-align:center;color:#8b949e;padding:26px 0;font-size:13px}
.hint{font-size:12px;color:#8b949e;line-height:1.95}
.hint b{color:#c9d1d9}
.hint code{color:#79c0ff;background:#0d1117;padding:1px 5px;border-radius:4px}
.toast{position:fixed;top:18px;right:18px;background:#238636;color:#fff;padding:11px 18px;border-radius:8px;z-index:999;animation:fadein .25s;font-size:13px;max-width:420px}
.toast.err{background:#da3633}
.toast.warn{background:#9e6a03}
@keyframes fadein{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div class="header">
    <h1>🚀 一键入库</h1>
    <span class="tag">磁力/电驴 → 115 → strm → MDC刮削/油猴补充 → Emby</span>
    <div class="spacer"></div>
    <div class="nav">
        <a class="btn sm cur" href="/">🚀 一键入库</a>
        <a class="btn sm" href="/tasks">📋 任务监控</a>
        <a class="btn sm avdb" href="/avdb">🔗 avdb 连接器</a>
        <button class="btn sm purple" onclick="openLogin()">📱 115 扫码登录</button>
    </div>
</div>
<div class="container">
    <div class="cards" id="cards"></div>

    <div class="panel">
        <h2>批量入库 <span class="sub">一行一条; 支持 magnet / ed2k / 裸 40 位 BTIH; 行尾 <code>#分类</code> 可单条覆盖</span>
            <span class="right live" id="live"></span>
        </h2>
        <textarea id="links" spellcheck="false" placeholder="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567
ed2k://|file|影片名.mkv|1234567890|ABCDEF0123456789ABCDEF0123456789|/
0123456789abcdef0123456789abcdef01234567          ← 裸 BTIH 也能识别
magnet:?xt=urn:btih:...                          #fc2      ← 这一条单独放 FC2, 见下方说明"></textarea>
        <div class="toolbar">
            <label>类型</label>
            <select id="kind">
                <option value="fanhao">影片 (番号, MDC 刮削)</option>
                <option value="non_fanhao">剧集 (非番号, 油猴补充元数据)</option>
            </select>
            <label>分类</label>
            <select id="cat"></select>
            <button class="btn ghost" onclick="pasteFromClipboard()">📋 粘贴</button>
            <button class="btn ghost" onclick="clearAll()">清空</button>
            <button class="btn primary" id="btnSubmit" onclick="submitBatch()" disabled>批量入库</button>
        </div>
        <div class="cathint" id="catHint"></div>
        <div class="parse" id="parseBox"></div>
    </div>

    <div class="panel" id="batchPanel" style="display:none">
        <h2>本批提交 <span class="sub" id="batchSum"></span></h2>
        <div id="batchRows"></div>
        <div class="panel-foot"><button class="btn sm danger" onclick="clearBatch()">清空记录</button></div>
    </div>

    <div class="panel">
        <h2>最近入库 <span class="sub" id="recentSub">10s 自动刷新</span>
            <span class="right"><a class="plink" href="/tasks">查看全部 →</a></span>
        </h2>
        <div class="tw">
        <table class="rt">
            <thead><tr><th>时间</th><th>来源</th><th>标题 / 链接</th><th>类型</th><th>分类</th><th>状态</th><th>进度</th><th>消息</th></tr></thead>
            <tbody id="rtBody"></tbody>
        </table>
        </div>
        <div class="empty" id="rtEmpty">加载中…</div>
    </div>

    <div class="panel">
        <div class="hint">
            <b>配方选择</b><br>
            • <b>影片</b> (番号): 推磁力 → 等落地 → 清广告小文件 → strm → <b>MDC 刮削</b> → Emby 刷新 → 预热。MDC 没刮到就在帖子页用油猴补。<br>
            • <b>剧集</b> (非番号): 推磁力 → 等落地 (任务停在<b>待整理</b>) → 先在 115 手工整理视频 → 任务页点 <b>🔄整理</b> → 命名「原名.thread_x.S01E续集号」→ strm → 油猴补元数据 → Emby 刷新。<br>
            <b>分类</b>决定 115 落点目录名 / 本地 strm 的刮削监控目录 / 最终 <code>已刮削/&lt;分类&gt;</code> 输出。整批用下拉选择, 个别不同就在那一行行尾写 <code>#fc2</code> 这类标记。<br>
            <b>提交后可离开页面</b>: 任务在服务端排队执行, 这里只是显示进度; 刷新页面会从本机记录里恢复本批列表。要长期跟踪用 <a href="/tasks" style="color:#58a6ff">任务监控页</a>。
        </div>
    </div>
</div>

<!-- 115 扫码全局登录弹窗 -->
<div id="loginModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:999;align-items:center;justify-content:center">
    <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:340px;text-align:center">
        <h3 style="color:#58a6ff;margin-bottom:8px">115 扫码全局登录</h3>
        <div style="color:#8b949e;font-size:13px;margin-bottom:16px">用手机 115 App 扫一扫, 登录后凭证将自动分发</div>
        <div id="qrBox" style="background:#fff;border-radius:8px;padding:8px;margin:0 auto 12px;width:240px;height:240px;display:flex;align-items:center;justify-content:center;color:#8b949e;font-size:13px">生成中...</div>
        <div id="loginStatus" style="color:#c9d1d9;font-size:13px;min-height:20px;margin-bottom:12px">请稍候...</div>
        <div style="display:flex;gap:8px;justify-content:center">
            <button class="btn ghost" onclick="closeLogin()">关闭</button>
            <button class="btn purple" id="btnLoginAgain" onclick="startLogin()" style="display:none">重新生成二维码</button>
        </div>
    </div>
</div>

<script>
/* ===== 后端注入: 分类表 (来自 CATEGORY_MAP, 单一来源) ===== */
const CATS = __CATS_JSON__;
const DEFAULT_CAT = '__DEFAULT_CAT__';
const CAT_BY_KEY = {};
CATS.forEach(c => { CAT_BY_KEY[c.key] = c; });
const CAT_COLOR = {av:'#58a6ff', fc2:'#8957e5', sw:'#d29922', cn:'#f2555a', ea:'#3fb950', lf:'#a371f7'};

/* ===== 与 /tasks 页共用的状态词表 ===== */
const STATUS = {queued:'排队', running:'运行中', pending_manual:'待整理', done:'已完成', failed:'失败'};
const KIND_LBL = {fanhao:'影片', non_fanhao:'剧集'};
const STEP_PCT = {init:5, push:15, wait:40, scrape:60, nfo:80, strm:80, scan:92};
const STEP_LBL = {init:'创建', push:'推送', wait:'等待落地', wait_video:'等待落地', scrape:'刮削', nfo:'元数据', strm:'生成strm', scan:'扫库', move:'移动', error:'异常'};
const LS_KEY = 'sehuatang_batch_tasks';
const POLL_MS = 3000, LIST_MS = 10000;

let pollTimers = {};       // task_id -> interval
let batch = [];            // 本批记录 [{task_id, link, kind, cat, ts, ok, error}]
let previewItems = [], previewErrors = [], previewTimer = null, lastParsedText = '';
let submitBusy = false;
let lastSubmit = null;     // {fp, ts, n} 上一批成功提交的指纹 (前端防重, 见 dupGuard*)

/* ---------- 小工具 ---------- */
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmt(s){return s?String(s).slice(0,19).replace('T',' '):'';}
function shortLink(s,n){s=String(s||'');return s.length>(n||46)?s.slice(0,n||46)+'…':s;}
function badge(st){return '<span class="badge '+esc(st)+'">'+(STATUS[st]||esc(st))+'</span>';}
function pct(t){
  if(t.status==='done') return {p:100, cls:'done'};
  if(t.status==='failed') return {p:100, cls:'failed'};
  const p = STEP_PCT[t.step] || (t.status==='queued'?5:50);
  return {p:p, cls:''};
}
function catPill(key){
  if(!key) return '<span class="catpill">-</span>';
  const c = CAT_BY_KEY[key];
  const col = CAT_COLOR[key] || '#8b949e';
  return '<span class="catpill" style="color:'+col+';border-color:'+col+'55">'+esc(c?c.name:key)+'</span>';
}
function toast(msg, kind){
  const t = document.createElement('div');
  t.className = 'toast' + (kind==='err'?' err':(kind==='warn'?' warn':''));
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ t.remove(); }, kind==='err'?7000:4000);
}

/* ---------- 分类下拉 + 说明 ---------- */
function initCats(){
  const sel = document.getElementById('cat');
  sel.innerHTML = CATS.map(c=>'<option value="'+esc(c.key)+'"'+(c.key===DEFAULT_CAT?' selected':'')+'>'+esc(c.name)+' ('+esc(c.key)+')</option>').join('');
  sel.onchange = updateCatHint;
  updateCatHint();
}
function updateCatHint(){
  const key = document.getElementById('cat').value;
  const c = CAT_BY_KEY[key] || {};
  document.getElementById('catHint').innerHTML =
    '分类 <b style="color:#c9d1d9">' + esc(c.name||key) + '</b> · ' +
    '本地 strm 监控 <code>' + esc(c.strm||'-') + '</code> · ' +
    '刮削输出 <code>' + esc(c.out||'-') + '</code><br>' +
    '行尾写 <code>#' + esc(key) + '</code> 的单条会覆盖上面的整批分类; 可用分类: ' +
    CATS.map(x=>'<code>#'+esc(x.key)+'</code>='+esc(x.name)).join(' ');
}

/* ---------- 解析预览 (服务端 dry_run, 不重复实现解析) ---------- */
function schedulePreview(){
  const txt = document.getElementById('links').value;
  lightLive();
  if(previewTimer) clearTimeout(previewTimer);
  if(!txt.trim()){
    previewItems = []; previewErrors = [];
    document.getElementById('parseBox').className = 'parse';
    document.getElementById('btnSubmit').disabled = true;
    document.getElementById('btnSubmit').textContent = '批量入库';
    return;
  }
  previewTimer = setTimeout(doPreview, 450);
}
async function doPreview(){
  const txt = document.getElementById('links').value;
  const kind = document.getElementById('kind').value;
  const cat = document.getElementById('cat').value;
  let d;
  try{
    const res = await fetch('/api/import/batch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({links: txt, kind: kind, category: cat, dry_run: true})});
    d = await res.json();
  }catch(e){
    document.getElementById('live').innerHTML = '<span class="bad">解析请求失败: '+esc(e.message)+'</span>';
    return;
  }
  if(d.error){ toast(d.error, 'err'); return; }
  if(document.getElementById('links').value !== txt) return;   // 期间又改了, 丢弃
  lastParsedText = txt;
  previewItems = d.items || [];
  previewErrors = d.errors || [];
  renderPreview();
  const n = previewItems.length, bad = previewErrors.length;
  document.getElementById('btnSubmit').disabled = (n === 0 || submitBusy);
  document.getElementById('btnSubmit').textContent = n ? '批量入库 ('+n+')' : '批量入库';
  document.getElementById('live').innerHTML = n
    ? '识别到 <b class="good">'+n+'</b> 条' + (bad?' · <span class="warn">'+bad+' 行被跳过</span>':'')
    : '<span class="bad">没有识别到有效链接</span>';
}
function renderPreview(){
  const box = document.getElementById('parseBox');
  if(!previewItems.length && !previewErrors.length){ box.className='parse'; box.innerHTML=''; return; }
  let h = '<table class="ptable"><thead><tr><th>行</th><th>链接</th><th>类型</th><th>分类</th><th>状态</th></tr></thead><tbody>';
  previewItems.forEach(function(it){
    h += '<tr><td>'+it.line+'</td><td class="link">'+esc(shortLink(it.magnet, 64))+'</td><td>'+(KIND_LBL[it.kind]||esc(it.kind))+'</td><td>'+catPill(it.category)+'</td><td><span class="badge done">就绪</span></td></tr>';
  });
  previewErrors.forEach(function(er){
    h += '<tr class="bad"><td>'+er.line+'</td><td class="link">'+esc(shortLink(er.raw, 64))+'</td><td colspan="3"><span class="badge failed">跳过</span> <span style="color:#f85149;font-size:12px">'+esc(er.error)+'</span></td></tr>';
  });
  h += '</tbody></table>';
  if(previewErrors.length){
    h += '<div style="font-size:12px;color:#8b949e;margin-top:8px">被跳过的行不会提交; 想保留请修正后重新粘贴 —— 重复链接只取第一条。</div>';
  }
  box.className = 'parse show';
  box.innerHTML = h;
}
function lightLive(){   /* 极轻量即时计数(不解析语义), 权威结果以服务端 dry_run 为准 */
  const txt = document.getElementById('links').value;
  const n = txt.split('\n').filter(l=>l.trim() && !l.trim().startsWith('#')).length;
  if(!n){ document.getElementById('live').innerHTML = ''; return; }
  document.getElementById('live').innerHTML = '输入 <b>'+n+'</b> 行…';
}

/* ---------- 防重: 同一批链接 60 秒内重复提交直接拦 (2026-09-22) ----------
   背景: 面板连点两次「批量入库」→ 服务端把同一批链接建两遍任务; 同链接两条任务并发跑
   会互相删 115 离线任务 (实测 20:31 连点 → 28 条并发, 13 条白跑一轮 + MCP 会话被争抢)。
   服务端 /api/import/batch 已按 hash 查重兜底(第二道), 这里是第一道: 60s 窗口内同指纹
   直接在浏览器拦住, 请求都不发。指纹 = 排序后的链接规范化 key + 类型 + 分类。
   落 localStorage: 手滑关页面重开也拦得住。 */
const DUP_GUARD_MS = 60000;
const LS_LAST_SUBMIT = 'sehuatang_last_submit';
function linkDedupKey(line){
  const s = String(line||'').trim();
  let m = s.match(/btih:([0-9a-fA-F]{32,40})/);
  if(m) return 'bt:' + m[1].toLowerCase();
  if(/^[0-9a-fA-F]{40}$/.test(s)) return 'bt:' + s.toLowerCase();
  m = s.match(/ed2k:\/\/\|file\|[^|]*\|\d+\|([0-9a-fA-F]{32})\|/i);
  if(m) return 'ed2k:' + m[1].toLowerCase();
  m = s.match(/\|([0-9a-fA-F]{32})\|\/?$/);
  if(m) return 'ed2k:' + m[1].toLowerCase();
  return s;
}
function submitFingerprint(txt, kind, cat){
  const keys = String(txt||'').split('\n').map(function(l){ return l.trim(); })
    .filter(function(l){ return l && l.charAt(0) !== '#'; })
    .map(linkDedupKey).sort();
  return kind + '|' + cat + '|' + keys.join(';');
}
function dupGuardLeft(txt, kind, cat){
  if(!lastSubmit) return {left:0, ago:0};
  if(lastSubmit.fp !== submitFingerprint(txt, kind, cat)) return {left:0, ago:0};
  const elapsed = Date.now() - lastSubmit.ts;
  const left = DUP_GUARD_MS - elapsed;
  return left > 0 ? {left: Math.ceil(left/1000), ago: Math.round(elapsed/1000)} : {left:0, ago:0};
}
function dupGuardMark(txt, kind, cat, n){
  lastSubmit = {fp: submitFingerprint(txt, kind, cat), ts: Date.now(), n: n};
  try{ localStorage.setItem(LS_LAST_SUBMIT, JSON.stringify(lastSubmit)); }catch(e){}
}
function loadLastSubmit(){
  try{ lastSubmit = JSON.parse(localStorage.getItem(LS_LAST_SUBMIT) || 'null'); }catch(e){ lastSubmit = null; }
  if(!lastSubmit || typeof lastSubmit.fp !== 'string' || typeof lastSubmit.ts !== 'number') lastSubmit = null;
}

/* ---------- 提交 ---------- */
async function submitBatch(){
  const txt = document.getElementById('links').value;
  const kind = document.getElementById('kind').value;
  const cat = document.getElementById('cat').value;
  if(!txt.trim()){ toast('请先粘贴磁力/电驴链接', 'err'); return; }
  if(txt !== lastParsedText){ await doPreview(); }
  if(!previewItems.length){ toast('没有可提交的有效链接', 'err'); return; }
  const dup = dupGuardLeft(txt, kind, cat);
  if(dup.left){
    toast('这批 ' + lastSubmit.n + ' 条链接 ' + dup.ago + ' 秒前刚提交过, 已拦截防重复入库; 确实要再提交一次, 请等 ' + dup.left + ' 秒后再点。', 'warn');
    return;
  }
  submitBusy = true;
  const btn = document.getElementById('btnSubmit');
  btn.disabled = true; btn.textContent = '提交中…';
  let d;
  try{
    const res = await fetch('/api/import/batch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({links: txt, kind: kind, category: cat})});
    d = await res.json();
  }catch(e){
    submitBusy = false; btn.textContent = '批量入库'; btn.disabled = false;
    toast('提交失败: ' + e.message, 'err'); return;
  }
  submitBusy = false; btn.textContent = '批量入库'; btn.disabled = false;
  if(d.error){ toast(d.error, 'err'); return; }
  if((d.submitted || 0) > 0) dupGuardMark(txt, kind, cat, previewItems.length);   // 全失败不记, 允许立刻重试
  const items = d.items || [];
  const t0 = Date.now();
  items.forEach(function(it){
    if(it.ok){
      batch.unshift({task_id: it.task_id, link: it.magnet, kind: it.kind, cat: it.category, ts: t0, ok: true});
    }else{
      batch.unshift({task_id: '', link: it.magnet, kind: it.kind, cat: it.category, ts: t0, ok: false, error: it.error});
    }
  });
  saveBatch(); renderBatch(); resumePolls();
  const okN = d.submitted || 0, badN = d.failed || 0, skipN = d.skipped || 0, dupN = d.dup_skipped || 0;
  toast('已提交 ' + okN + ' 条' + (badN?', 失败 '+badN+' 条':'') + (skipN?', 解析跳过 '+skipN+' 行':'')
        + (dupN?', 查重跳过 '+dupN+' 条':''));
  if(skipN && (d.errors||[]).length){
    const first = d.errors[0];
    toast('第 '+first.line+' 行被跳过: '+first.error, 'warn');
  }
  if(dupN && (d.duplicates||[]).length){
    toast('第 '+d.duplicates[0].line+' 行: '+d.duplicates[0].error, 'warn');
  }
}

/* ---------- 本批进度 ---------- */
function saveBatch(){ try{ localStorage.setItem(LS_KEY, JSON.stringify(batch.slice(0, 60))); }catch(e){} }
function loadBatch(){
  try{ batch = JSON.parse(localStorage.getItem(LS_KEY) || '[]') || []; }catch(e){ batch = []; }
  if(!Array.isArray(batch)) batch = [];
}
function rowKey(b){ return (b.task_id || ('x'+b.ts)) + '-' + b.ts; }
function rowHtml(b){
  const st = b.status || (b.ok ? 'queued' : 'failed');
  const pp = pct(b);
  const msg = b.ok ? (b.msg || '等待执行…') : (b.error || '提交失败');
  const stepLbl = b.step ? (STEP_LBL[b.step] || b.step) : '';
  return '<span class="idx">'+(b.ok?'':'!')+'</span>'+
    '<span class="lnk" title="'+esc(b.link)+'">'+esc(shortLink(b.link, 60))+'</span>'+
    '<span>'+(KIND_LBL[b.kind]||esc(b.kind||''))+'</span>'+ catPill(b.cat)+ badge(st)+
    '<div class="progress"><div class="fill '+pp.cls+'" style="width:'+pp.p+'%"></div></div>'+
    '<span class="pct">'+pp.p+'%</span>'+
    '<span class="msg" title="'+esc(msg)+'">'+esc(stepLbl ? stepLbl+' · '+msg : msg)+'</span>';
}
function renderBatch(){
  const p = document.getElementById('batchPanel');
  if(!batch.length){ p.style.display = 'none'; return; }
  p.style.display = 'block';
  document.getElementById('batchSum').textContent =
    '共 ' + batch.length + ' 条 (记录在本机, 刷新不丢; 排队/运行中的会自动续查进度)';
  document.getElementById('batchRows').innerHTML = batch.map(function(b){
    return '<div class="brow" data-key="'+esc(rowKey(b))+'">'+rowHtml(b)+'</div>';
  }).join('');
}
function updateRow(b){
  const el = document.querySelector('[data-key="'+rowKey(b)+'"]');
  if(!el) return;
  el.innerHTML = rowHtml(b);
}
function resumePolls(){
  batch.forEach(function(b){
    if(!b.ok || !b.task_id) return;
    if(b.status === 'done' || b.status === 'failed') return;
    if(pollTimers[b.task_id]) return;
    pollTimers[b.task_id] = setInterval(function(){ pollOne(b.task_id); }, POLL_MS);
    pollOne(b.task_id);
  });
}
async function pollOne(taskId){
  let d;
  try{
    const res = await fetch('/api/import/status?task_id=' + encodeURIComponent(taskId));
    d = await res.json();
  }catch(e){ return; }
  const b = batch.find(x=>x.task_id === taskId);
  if(!b || d.error){
    if(pollTimers[taskId]){ clearInterval(pollTimers[taskId]); delete pollTimers[taskId]; }
    return;
  }
  b.status = d.status; b.step = d.step; b.msg = d.msg;
  updateRow(b);
  if(d.status === 'done' || d.status === 'failed'){
    if(pollTimers[taskId]){ clearInterval(pollTimers[taskId]); delete pollTimers[taskId]; }
    saveBatch();
    loadRecent();
  }
}
function clearBatch(){
  batch = []; saveBatch(); renderBatch();
  Object.keys(pollTimers).forEach(k=>{ clearInterval(pollTimers[k]); delete pollTimers[k]; });
}
function clearAll(){
  document.getElementById('links').value = '';
  schedulePreview();
  document.getElementById('live').innerHTML = '';
}
async function pasteFromClipboard(){
  try{
    const t = await navigator.clipboard.readText();
    if(!t){ toast('剪贴板是空的', 'warn'); return; }
    const box = document.getElementById('links');
    box.value = box.value.trim() ? (box.value.replace(/\s*$/,'') + '\n' + t.trim()) : t.trim();
    schedulePreview();
    toast('已粘贴 ' + t.split('\n').filter(x=>x.trim()).length + ' 行');
  }catch(e){
    toast('浏览器不给读剪贴板, 请手动 Ctrl+V 粘到输入框', 'warn');
  }
}

/* ---------- 队列统计 + 最近入库 ---------- */
async function loadRecent(){
  let d;
  try{
    const res = await fetch('/api/import/list?per_page=8');
    d = await res.json();
  }catch(e){ return; }
  if(!d || !d.tasks) return;
  const s = d.stats || {};
  const c = [['queued','排队',s.queued||0],['running','运行中',s.running||0],
             ['pending_manual','待整理',s.pending_manual||0],['done','已完成',s.done||0],
             ['failed','失败',s.failed||0],['total','总数',s.total||0]];
  document.getElementById('cards').innerHTML = c.map(x=>
    '<div class="card '+x[0]+'"><div class="lbl">'+x[1]+'</div><div class="num">'+x[2]+'</div></div>').join('');
  document.getElementById('recentSub').textContent = '最近 ' + d.tasks.length + ' 条 · 10s 自动刷新';
  document.getElementById('rtEmpty').style.display = d.tasks.length ? 'none' : 'block';
  if(!d.tasks.length){ document.getElementById('rtEmpty').textContent = '还没有入库记录'; }
  document.getElementById('rtBody').innerHTML = d.tasks.map(function(t){
    const pp = pct(t);
    const label = t.title || t.magnet || '-';
    const src = t.origin === 'avdb'
      ? '<span class="catpill" style="color:#d29922;border-color:#9e6a03">🔗 avdb</span>'
      : '<span class="catpill" style="color:#8b949e">🀄 色花堂</span>';
    return '<tr>'+
      '<td style="white-space:nowrap;color:#8b949e">'+fmt(t.created_at)+'</td>'+
      '<td>'+src+'</td>'+
      '<td><div class="ttl" title="'+esc(label)+'">'+esc(label)+'</div></td>'+
      '<td>'+(KIND_LBL[t.kind]||esc(t.kind||'-'))+'</td>'+
      '<td>'+catPill(t.category)+'</td>'+
      '<td>'+badge(t.status)+'</td>'+
      '<td><div style="display:flex;align-items:center;gap:6px"><div class="progress"><div class="fill '+pp.cls+'" style="width:'+pp.p+'%"></div></div><span class="pct">'+pp.p+'%</span></div></td>'+
      '<td><div class="m" title="'+esc(t.msg||'')+'">'+esc((t.msg||'-').replace(/\n/g,' ').slice(0,60))+'</div></td>'+
    '</tr>';
  }).join('');
}

/* ---------- 115 扫码登录 ---------- */
var loginTimer = null;
function openLogin(){
  document.getElementById('loginModal').style.display = 'flex';
  startLogin();
}
function closeLogin(){
  document.getElementById('loginModal').style.display = 'none';
  if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
}
function startLogin(){
  var box = document.getElementById('qrBox');
  var st = document.getElementById('loginStatus');
  box.innerHTML = '生成中...';
  st.textContent = '正在生成二维码...';
  document.getElementById('btnLoginAgain').style.display = 'none';
  fetch('/api/login/qrcode').then(function(r){return r.json()}).then(function(d){
    box.innerHTML = '<img src="/api/login/qrcode.png" style="width:220px;height:220px">';
    st.textContent = (d.msg || '请用 115 App 扫码确认') + ' (180秒内有效)';
  }).catch(function(e){ st.textContent = '生成失败: ' + e.message; });
  if(loginTimer) clearInterval(loginTimer);
  loginTimer = setInterval(pollLogin, 2500);
}
function pollLogin(){
  fetch('/api/login/status').then(function(r){return r.json()}).then(function(d){
    var st = document.getElementById('loginStatus');
    if(d.status === 'done'){
      st.innerHTML = '<span style="color:#7ee787">✅ ' + (d.msg || '全局登录成功') + '</span>';
      if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
    } else if(d.status === 'failed'){
      st.innerHTML = '<span style="color:#f85149">' + (d.msg || '登录失败') + '</span>';
      document.getElementById('btnLoginAgain').style.display = 'inline-block';
      if(loginTimer){ clearInterval(loginTimer); loginTimer = null; }
    } else if(d.status === 'running'){
      st.textContent = d.msg || '等待扫码...';
    } else {
      st.textContent = '状态: ' + d.status;
    }
  }).catch(function(e){});
}

/* ---------- 启动 ---------- */
initCats();
loadLastSubmit();
loadBatch();
renderBatch();
resumePolls();
document.getElementById('links').addEventListener('input', schedulePreview);
document.getElementById('kind').addEventListener('change', schedulePreview);
document.getElementById('cat').addEventListener('change', schedulePreview);
document.getElementById('links').addEventListener('paste', function(){ setTimeout(schedulePreview, 0); });
loadRecent();
setInterval(loadRecent, LIST_MS);
</script>
</body>
</html>"""


def render_index_page(category_map, default_category='av'):
    """把分类表注入模板 (单一来源: import_api.CATEGORY_MAP)"""
    cats = [{'key': k, 'name': v[2], 'strm': v[0], 'out': v[1]} for k, v in category_map.items()]
    return (INDEX_PAGE
            .replace('__CATS_JSON__', json.dumps(cats, ensure_ascii=False))
            .replace('__DEFAULT_CAT__', str(default_category)))
