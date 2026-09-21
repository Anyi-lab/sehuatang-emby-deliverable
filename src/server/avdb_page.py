# -*- coding: utf-8 -*-
"""avdb_page.py — avdb 连接器面板 (独立页面, 与色花堂入库任务页分开)
由 import_api.py 的 GET /avdb 提供。数据来自 /api/avdb/status (纯本地读取, 零网络)。"""

AVDB_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>avdb 连接器</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.header h1{font-size:20px;color:#d29922}
.header .tag{font-size:12px;color:#8b949e;background:#21262d;padding:3px 10px;border-radius:12px}
.header .spacer{flex:1}
/* ===== 按钮统一规范 (2026-09-20)：与 / 、/tasks 三页同一套尺寸/配色 ===== */
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
.nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* 非当前页导航必须显式声明 color/background：.btn 基础样式没有 color，锚点会回退成浏览器默认
   链接色(未访问 #0000ee / 已访问 #551a8b)，在 #161b22 上只有 1.8:1，看着就是糊在背景上的暗字 */
.nav a.btn{background:#21262d;border-color:#30363d;color:#c9d1d9}
.nav a.btn:hover{background:#30363d;border-color:#8b949e;color:#c9d1d9}
.nav a.btn.cur{background:#193656;border-color:#58a6ff;color:#58a6ff}
.nav a.btn.avdb.cur{background:#483600;border-color:#d29922;color:#d29922}
.plink{color:#58a6ff;font-size:12px;text-decoration:none}
.plink:hover{text-decoration:underline}
.container{max-width:1280px;margin:0 auto;padding:20px 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.card .lbl{font-size:12px;color:#8b949e}
.card .num{font-size:22px;font-weight:700;margin-top:4px;word-break:break-all}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}.dim{color:#8b949e}.blue{color:#58a6ff}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.tools{margin-left:auto;display:flex;gap:8px;align-items:center}
.hint{font-size:12px;color:#8b949e}
h2{font-size:15px;color:#58a6ff;margin:22px 0 10px;display:flex;align-items:center;gap:10px}
h2 .sub{font-size:12px;color:#8b949e;font-weight:400}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden;font-size:13px}
th{text-align:left;padding:9px 12px;background:#21262d;color:#8b949e;font-weight:600;font-size:12px;white-space:nowrap}
td{padding:9px 12px;border-top:1px solid #21262d;vertical-align:top}
tr:hover td{background:#11161d}
.pill{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;white-space:nowrap}
.p-submitted,.p-done,.p-adopted,.p-done_imported{background:#0f2d1b;color:#3fb950}
.p-missing,.p-not_on_115{background:#2d2a0f;color:#d29922}
.p-failed,.p-submit_failed,.p-adopt_failed,.p-no_number{background:#2d1214;color:#f85149}
.p-queued,.p-running{background:#0d2440;color:#58a6ff}
.p-ledger{background:#21262d;color:#8b949e}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#8b949e}
.empty{padding:16px;color:#8b949e;font-size:13px;background:#161b22;border:1px dashed #30363d;border-radius:8px}
.note{background:#161b22;border:1px solid #30363d;border-left:3px solid #d29922;border-radius:6px;padding:10px 14px;font-size:12px;color:#8b949e;margin-bottom:14px;line-height:1.7}
</style>
</head>
<body>
<div class="header">
  <h1>🔗 avdb 连接器</h1>
  <span class="tag">avdb 下载 → 自动入库（独立通道）</span>
  <span class="spacer"></span>
  <div class="nav">
    <a class="btn sm" href="/">🚀 一键入库</a>
    <a class="btn sm" href="/tasks">📋 任务监控</a>
    <a class="btn sm avdb cur" href="/avdb">🔗 avdb 连接器</a>
  </div>
</div>
<div class="container">
  <div class="note">
    这条通道只处理 <b>avdb 里下载的片</b>：守护每 60 秒读一次 avdb 本地库（零网络请求），发现新下载才去 115 查那一片的落点，搬进 <span class="mono">/sehuatang/AV/thread_&lt;番号&gt;</span> 后走标准入库（清广告 → strm → 刮削 → 扫库）。
    <b>色花堂网页/磁力入库的任务不在这个页面</b>，看 <a href="/tasks" style="color:#58a6ff">任务监控</a>。
  </div>

  <div class="cards" id="cards"></div>

  <div class="bar">
    <button class="btn primary" onclick="scan(true)">⚡ 补扫并全部入库</button>
    <button class="btn ghost" onclick="scan(false)">🔍 手动补扫</button>
    <button class="btn warn" id="pauseBtn" onclick="togglePause()">⏸ 暂停守护</button>
    <div class="tools">
      <span class="hint" id="hint"></span>
      <button class="btn ghost" onclick="load()">🔄 刷新</button>
    </div>
  </div>

  <div id="scanBox"></div>

  <h2>待接手 <span class="sub">avdb 有下载记录、守护还没处理（水位之后）</span></h2>
  <div id="pending"></div>

  <h2>连接器台账 <span class="sub">守护处理过的每一条 avdb 下载记录</span></h2>
  <div id="ledger"></div>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
var LBL={submitted:'已提交',done:'完成',adopted:'已提交',ledger:'台账已有',no_number:'无番号',
  not_on_115:'115 无落点',done_imported:'已在库里',candidate:'漏网待入库',adopt_failed:'提交失败',
  missing:'未找到',skipped:'跳过',failed:'失败',queued:'排队',running:'进行中'};
function pill(s){return '<span class="pill p-'+esc(s)+'" title="'+esc(s)+'">'+esc(LBL[s]||s||'-')+'</span>';}
var CUR=null;

function load(){
  fetch('/api/avdb/status').then(function(r){return r.json();}).then(function(d){
    CUR=d;
    var g=d.guard||{};
    var st = g.paused ? '<span class="warn">已暂停</span>' : (g.alive ? '<span class="ok">运行中</span>' : '<span class="bad">无心跳</span>');
    var age = (g.age==null) ? '—' : (g.age+'s 前');
    var c='';
    c+=card('守护状态', st, '心跳 '+age+' / '+g.interval+'s 一轮');
    c+=card('水位', (d.watermark==null?'未初始化':d.watermark), 'avdb 最大 id '+ (d.avdb_max_id==null?'—':d.avdb_max_id), 'dim');
    c+=card('待接手', d.pending_count||0, '等待守护处理', (d.pending_count?'warn':'dim'));
    var b=d.ledger.by_status||{};
    c+=card('已提交入库', b.submitted||0, '台账累计', 'ok');
    c+=card('跳过/未找到', (b.missing||0)+(b.skipped||0), '115 上已无落点', 'warn');
    c+=card('失败', b.submit_failed||0, '需人工看一眼', (b.submit_failed?'bad':'dim'));
    document.getElementById('cards').innerHTML=c;
    document.getElementById('pauseBtn').innerHTML = g.paused ? '▶ 恢复守护' : '⏸ 暂停守护';

    var p=d.pending||[];
    document.getElementById('pending').innerHTML = p.length ? (
      '<table><tr><th>id</th><th>番号</th><th>avdb 落点</th><th>记录时间</th><th>标题</th></tr>'+
      p.map(function(x){return '<tr><td class="mono">'+x.dl_id+'</td><td><b>'+esc(x.number)+'</b></td><td class="mono">/sehuatang/AVDB/'+esc(x.save_path)+'</td><td class="mono">'+esc(x.create_time)+'</td><td>'+esc(x.title)+'</td></tr>';}).join('')+
      '</table>') : '<div class="empty">没有待接手的记录 —— avdb 里新提交下载后，守护会在 60 秒内接手。</div>';

    var s=d.seen||[];
    document.getElementById('ledger').innerHTML = s.length ? (
      '<table><tr><th>id</th><th>番号</th><th>台账状态</th><th>入库任务</th><th>进度</th><th>说明</th><th>更新时间</th></tr>'+
      s.map(function(x){
        var t = x.task_id ? '<a href="/tasks?task_id='+esc(x.task_id)+'" style="color:#58a6ff" class="mono">'+esc(x.task_id)+'</a>' : '<span class="dim">—</span>';
        var stt = x.status;
        if(x.imported_task){ t = '<a href="/tasks?task_id='+esc(x.imported_task)+'" style="color:#3fb950" class="mono">'+esc(x.imported_task)+'</a>'; stt='done_imported'; }
        var prog = x.task_status ? pill(x.task_status)+' <span class="dim">'+esc(x.task_step)+'</span>' : '<span class="dim">—</span>';
        return '<tr><td class="mono">'+x.dl_id+'</td><td><b>'+esc(x.number)+'</b></td><td>'+pill(stt)+
               '</td><td>'+t+'</td><td>'+prog+'</td><td class="dim">'+esc(x.note||x.task_msg||'')+'</td><td class="mono">'+esc(x.updated_at)+'</td></tr>';
      }).join('')+'</table>') : '<div class="empty">台账还是空的。</div>';
  }).catch(function(e){document.getElementById('hint').innerHTML='<span class="bad">读取失败: '+esc(e)+'</span>';});
}

function card(lbl,val,sub,cls){
  return '<div class="card"><div class="lbl">'+esc(lbl)+'</div><div class="num '+(cls||'')+'">'+val+'</div><div class="lbl" style="margin-top:4px">'+esc(sub||'')+'</div></div>';
}

function scan(apply){
  var h=document.getElementById('hint');
  h.innerHTML = apply ? '补扫中并入库…（会列 115 目录，稍等）' : '补扫中…（会列 115 目录，稍等）';
  fetch('/api/avdb/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apply:!!apply,limit:40})})
  .then(function(r){return r.json();}).then(function(d){
    h.innerHTML = '补扫完成: 查了 '+d.scanned+' 条, 命中漏网 '+d.candidates+' 条'+(apply?', 已提交 '+d.adopted+' 条':', 未提交(预览)');
    var it=(d.items||[]).filter(function(x){return x.status!=='ledger';});
    document.getElementById('scanBox').innerHTML = it.length ? (
      '<h2>补扫结果 <span class="sub">不在台账里的 avdb 记录</span></h2><table>'+
      '<tr><th>id</th><th>番号</th><th>判定</th><th>115 落点</th><th>入库任务</th><th>说明</th></tr>'+
      it.map(function(x){return '<tr><td class="mono">'+x.dl_id+'</td><td><b>'+esc(x.number)+'</b></td><td>'+pill(x.status)+
        '</td><td class="mono">'+esc(x.dir_115||'')+'</td><td class="mono">'+esc(x.task_id||'—')+'</td><td class="dim">'+esc(x.error||x.resource_name||'')+'</td></tr>';}).join('')+
      '</table>') : '<h2>补扫结果 <span class="sub">没有漏网</span></h2><div class="empty">最近 '+d.scanned+' 条 avdb 记录都已进台账。</div>';
    load();
  }).catch(function(e){h.innerHTML='<span class="bad">补扫失败: '+esc(e)+'</span>';});
}

function togglePause(){
  var on = !(CUR && CUR.guard && CUR.guard.paused);
  fetch('/api/avdb/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:on})})
    .then(function(r){return r.json();}).then(function(d){
      document.getElementById('hint').textContent = d.paused ? '守护已暂停：不再自动接手新下载（手动补扫仍可用）' : '守护已恢复';
      load();
    });
}
load();setInterval(load,15000);
</script>
</body>
</html>
"""
