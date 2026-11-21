/* iVault 前端逻辑：真实 REST + SSE，无第三方依赖 */
'use strict';

/* ---------- 深浅色主题 ---------- */
(function () {
  const sun = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.3m0 14.4v2.3M2.5 12h2.3m14.4 0h2.3M5 5l1.6 1.6M17.4 17.4L19 19M19 5l-1.6 1.6M6.6 17.4L5 19"/></svg>';
  const moon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M20.4 14.2A8.3 8.3 0 019.8 3.6a8.3 8.3 0 1010.6 10.6z"/></svg>';
  const root = document.documentElement;
  const btn = document.getElementById('themeBtn');
  const lbl = document.getElementById('themeLabel');
  function apply(t) {
    root.dataset.theme = t;
    btn.innerHTML = t === 'dark' ? sun : moon;
    lbl.textContent = t === 'dark' ? '深色模式' : '浅色模式';
  }
  apply(localStorage.getItem('ivault-theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  btn.onclick = () => {
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('ivault-theme', next);
    apply(next);
  };
})();

/* ---------- 工具 ---------- */
const $ = id => document.getElementById(id);
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = '请求失败 (' + r.status + ')';
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) { /* ignore */ }
    const err = new Error(msg);
    err.status = r.status;    // 401 时触发登录浮层用
    throw err;
  }
  return r.json();
}

/* ---------- 登录（启用 IVAULT_PASSWORD 后，接口 401 时出现） ---------- */
function showAuth() {
  const ov = $('authOverlay');
  if (!ov || ov.style.display === 'flex') return;
  ov.style.display = 'flex';
  lockScroll();
  setTimeout(() => { const i = $('authPass'); if (i) i.focus(); }, 60);
}
async function doLogin() {
  const btn = $('authBtn'), err = $('authErr');
  const pw = $('authPass').value;
  if (!pw) { err.textContent = '请输入密码'; err.style.display = 'block'; return; }
  btn.disabled = true; btn.textContent = '验证中…'; err.style.display = 'none';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw })
    });
    if (r.ok) { location.reload(); return; }
    let msg = '登录失败 (' + r.status + ')';
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    err.textContent = msg; err.style.display = 'block';
  } catch (e) {
    err.textContent = '网络错误：' + e.message; err.style.display = 'block';
  }
  btn.disabled = false; btn.textContent = '登录';
}
async function doLogout() {
  try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  location.reload();
}
// 容量统一按 1024 进制（与后端 human_size 保持一致）。
// 之前这里用 1000 进制，而 NAS 的目录配额是按 1024 记的：512 GiB 的配额会被
// 显示成 549.8 GB，和后端诊断信息里的 512.0GB 对不上，看着像统计错了。
function fmtSize(b) {
  if (b == null || isNaN(b) || b === 0) return '—';
  const K = 1024;
  if (b >= K ** 4) return (b / K ** 4).toFixed(2) + ' TB';
  if (b >= K ** 3) return (b / K ** 3).toFixed(1) + ' GB';
  if (b >= K ** 2) return (b / K ** 2).toFixed(1) + ' MB';
  return Math.max(1, Math.round(b / K)) + ' KB';
}
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
  return (d.getMonth() + 1) + '-' + String(d.getDate()).padStart(2, '0') + ' ' + hm;
}
function fmtDur(sec) {
  if (sec == null || sec < 0) return '—';
  if (sec < 60) return Math.round(sec) + ' 秒';
  if (sec < 3600) return Math.round(sec / 60) + ' 分';
  return Math.floor(sec / 3600) + ' 时 ' + Math.round((sec % 3600) / 60) + ' 分';
}
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  /* 长提示（例如撤销信任失败的原因）给足阅读时间 */
  const dur = Math.min(9000, Math.max(3200, msg.length * 110));
  t._h = setTimeout(() => t.classList.remove('show'), dur);
}

/* ---------- 侧栏收起 ---------- */
/* 显式选择优先（localStorage）；没选过时按窗口宽度决定，和首屏脚本保持一致。
   手机上侧栏已经是底部标签栏，收起态没有意义，一律不收起 */
function sideShouldCollapse() {
  if (window.innerWidth <= 640) return false;
  const saved = localStorage.getItem('ivault-side');
  if (saved === '1' || saved === '0') return saved === '1';
  return window.innerWidth <= 1000;
}
function applySide() {
  const collapsed = sideShouldCollapse();
  document.documentElement.classList.toggle('side-collapsed', collapsed);
  const b = $('sideToggle');
  if (b) {
    b.title = collapsed ? '展开侧栏' : '收起侧栏';
    b.setAttribute('aria-label', b.title);
  }
}
function toggleSide() {
  const next = !sideShouldCollapse();
  localStorage.setItem('ivault-side', next ? '1' : '0');
  applySide();
}
window.addEventListener('resize', applySide);

/* ---------- 路由 ---------- */
function go(v) {
  document.querySelectorAll('.view').forEach(e => e.classList.remove('active'));
  $('view-' + v).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(e => e.classList.toggle('active', e.dataset.v === v));
  /* 首屏的 CSS 以 <html data-view> 为准，这里同步，否则样式表会把刚切到的页面继续藏起来 */
  document.documentElement.setAttribute('data-view', v);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  /* 记住当前页：刷新后回到原处，而不是每次都被踢回总览 */
  try { localStorage.setItem('ivault-view', v); } catch (e) { /* 隐私模式下写不了，忽略 */ }
}
document.querySelectorAll('.nav-item').forEach(e => { e.onclick = () => go(e.dataset.v); });

/* ---------- 全局状态 ---------- */
let state = { devices: [], backups: [], sources: [], status: {}, settings: {} };
let pairSource = null, jobSource = null, jobStartedAt = 0, logCount = 0, jobTick = null;

/* 终端日志的渲染节流。任务跑起来后 SSE 一秒能来几十条，以前是每条都
   insertAdjacentHTML + 强制滚动一次，主线程被拖死、页面直接卡住。
   现在先入队，一帧只渲染一次；并且只保留最近 MAX 行，DOM 不再无限增长。 */
const TERM_MAX_LINES = 300;   // 终端最多保留的行数
const TERM_LINE_MAX = 600;    // 单行最长字符数（超长输出截断，防止一条撑爆布局）
let logQueue = [];
let logRaf = 0;
/* 活动行：终端最后那一行，收到新状态就改文本而不是追加。
   idevicebackup2 本来就是用 \r 原地刷新，后端把那些帧转成 live 事件发过来，
   这里还原成"一行在走"的观感，日志行数也不会被进度帧撑爆。 */
let liveRow = null;
function queueLog(line) {
  if (line.length > TERM_LINE_MAX) line = line.slice(0, TERM_LINE_MAX) + ' …';
  logQueue.push(line);
  if (logRaf) return;
  logRaf = requestAnimationFrame(flushLog);
}
function flushLog() {
  logRaf = 0;
  const term = $('term');
  if (!term) { logQueue = []; return; }
  const time = new Date().toTimeString().slice(0, 8);
  const frag = document.createDocumentFragment();
  logQueue.forEach(line => {
    const row = document.createElement('div');
    row.className = 'ln';
    const t = document.createElement('span');
    t.className = 'dim';
    t.textContent = time + '  ';
    row.appendChild(t);
    /* 用 textContent 而不是拼 HTML：省掉每次 esc() 的开销，
       也天然免疫日志里的尖括号 */
    row.appendChild(document.createTextNode(line));
    frag.appendChild(row);
  });
  logQueue = [];
  /* 有活动行时插到它前面：活动行永远贴在底部，不会被后来的普通行顶到中间 */
  if (liveRow && liveRow.parentNode === term) term.insertBefore(frag, liveRow);
  else term.appendChild(frag);
  while (term.children.length > TERM_MAX_LINES) term.removeChild(term.firstChild);
  if (liveRow && !liveRow.isConnected) liveRow = null;   // 被行数上限裁掉了
  $('jSpeed').textContent = logCount + ' 行';
  term.scrollTop = term.scrollHeight;
}
/* 末行原地替换：没有活动行就新建一条，有就只改文本和它的时间戳 */
function renderLive(text) {
  const term = $('term');
  if (!term) return;
  if (text.length > TERM_LINE_MAX) text = text.slice(0, TERM_LINE_MAX) + ' …';
  if (!liveRow || !liveRow.isConnected) {
    liveRow = document.createElement('div');
    liveRow.className = 'ln live';
    const t = document.createElement('span');
    t.className = 'dim';
    liveRow.appendChild(t);
    liveRow.appendChild(document.createTextNode(''));
    term.appendChild(liveRow);
  }
  liveRow.firstChild.textContent = new Date().toTimeString().slice(0, 8) + '  ';
  liveRow.lastChild.textContent = text;
  while (term.children.length > TERM_MAX_LINES) term.removeChild(term.firstChild);
  term.scrollTop = term.scrollHeight;
}
function clearLog() {
  logQueue = [];
  liveRow = null;
  if (logRaf) { cancelAnimationFrame(logRaf); logRaf = 0; }
  const term = $('term');
  if (term) term.innerHTML = '';
}

/* 电量徽标：图标按百分比填充，充电中显示闪电并转绿，低电量未充电转琥珀色。
   取不到电量（离线 / 设备忙）就不渲染，不占位、不显示「—」。 */
/* showAge：详情页有空间，正文里写「· N 分钟前」；首页卡片紧凑，时间只放 title */
function battChip(b, showAge) {
  if (!b || b.level == null) return '';
  const level = Math.max(0, Math.min(100, b.level));
  const chg = !!(b.charging || b.external);
  const low = level < 20 && !chg;
  const w = Math.max(1.6, 14.2 * level / 100).toFixed(1);
  const bolt = chg ? '<svg class="bolt" viewBox="0 0 24 24" fill="currentColor">' +
    '<path d="M13.5 2 5 14h6l-1 8 9-12h-6l.5-8z"/></svg>' : '';
  /* 无线设备 10 分钟才采一次（USB 60 秒），旧读数要标出时间，别被当成实时值 */
  const mins = b.at ? Math.floor((Date.now() / 1000 - b.at) / 60) : 0;
  const age = mins >= 2 ? mins + ' 分钟前' : '';
  return '<span class="batt ' + (chg ? 'chg' : low ? 'low' : '') + '" title="' +
    (chg ? '充电中 · ' : '') + level + '%' + (age ? ' · ' + age + '读取' : '') + '">' + bolt +
    '<svg viewBox="0 0 24 24" fill="none">' +
      '<rect x="2" y="7" width="18" height="10" rx="2.6" stroke="currentColor" stroke-width="1.4"/>' +
      '<path d="M21.6 10.4v3.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>' +
      '<rect x="4" y="9" width="' + w + '" height="6" rx="1.2" fill="currentColor"/>' +
    '</svg><span>' + level + '%</span>' +
    (age && showAge ? '<span style="color:var(--txt3);font-weight:400">· ' + age + '</span>' : '') + '</span>';
}

/* 进度更新：设备管理页的卡片进度带 + 总览「我的设备」的细进度条与徽标，
   两处都由 SSE 直接驱动，不必等 6 秒轮询；找不到元素就安静跳过。 */
function updateDeviceProgress(udid, pct, phase, elapsed) {
  const p = pct == null ? null : Math.max(0, Math.min(100, pct));
  const box = document.getElementById('dp-' + udid);          // 设备管理页卡片
  if (box) {
    if (p != null) {
      const fill = box.querySelector('.bar i');
      if (fill) fill.style.width = p + '%';
      const num = box.querySelector('.dp-pct');
      if (num) num.textContent = p + '%';
    }
    if (phase) {
      const ph = box.querySelector('.dp-phase');
      if (ph && ph.textContent !== phase) ph.textContent = phase;
    }
    if (elapsed != null) {
      const el = box.querySelector('.dp-elapsed');
      if (el) el.textContent = ' · 已用 ' + elapsed;
    }
  }
  const mini = document.getElementById('dpm-' + udid);        // 总览紧凑行
  if (mini && p != null) {
    const fill = mini.firstElementChild;
    if (fill) fill.style.width = p + '%';
  }
  const pc = document.getElementById('dpc-' + udid);
  if (pc && p != null) pc.textContent = Math.round(p) + '%';
}
/* 当前跟踪的任务：取消按钮与断线重连都靠它，不能只依赖 EventSource.url ——
   连接一断 jobSource 就没了，那时候正是最需要「取消」的时候 */
let currentJob = null;
/* 任务卡的显示状态：idle / running / done / failed / canceled。
   refresh() 每 6 秒跑一次，靠它判断「卡片还在显示进行中，但后端已经没有活跃任务」，
   避免任务结束后卡片停在最后一个进度上。 */
let liveJobState = 'idle';
let lastUnpairUdid = null;

/* ---------- 热力图（最近 105 天，按完成日聚合） ---------- */
function renderHeat(backups) {
  const h = $('heat');
  h.innerHTML = '';
  const byDay = {};
  backups.forEach(s => {
    if (s.status === 'COMPLETE' && s.kind !== 'RESTORE' && s.finished_at) {
      const d = new Date(s.finished_at * 1000);
      const k = [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-');
      byDay[k] = (byDay[k] || 0) + 1;
    }
  });
  for (let i = 104; i >= 0; i--) {
    const d = new Date(); d.setDate(d.getDate() - i);
    const k = [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-');
    const el = document.createElement('i');
    const n = byDay[k] || 0;
    if (n >= 3) el.className = 'l4';
    else if (n === 2) el.className = 'l3';
    else if (n === 1) el.className = 'l2';
    h.appendChild(el);
  }
  const from = new Date(); from.setDate(from.getDate() - 104);
  $('heatFrom').textContent = (from.getMonth() + 1) + ' 月 ' + from.getDate() + ' 日';
}

function devName(udid) {
  const d = state.devices.find(x => x.udid === udid);
  return d ? (d.name || d.udid.slice(-8)) : udid.slice(-8);
}

/* ---------- 总览 ---------- */
/* 首页主操作：无设备时是「添加设备」入口，有设备才允许直接备份 */
function renderDashAction() {
  const b = $('dashAction');
  if (state.devices.length) {
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14m-7-7h14"/></svg>立即备份';
    b.onclick = quickBackup;
  } else {
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8.3v7.4m-3.7-3.7h7.4"/></svg>添加设备';
    b.onclick = openWizard;
  }
}

function renderDashboard() {
  const devs = state.devices, backups = state.backups;
  const total = devs.reduce((a, d) => a + (d.total_bytes || 0), 0);
  $('stTotal').textContent = fmtSize(total);
  /* 恢复任务也在 backups 里（kind=RESTORE），统计「备份」时要排除 */
  const okBackups = backups.filter(s => s.status === 'COMPLETE' && s.kind !== 'RESTORE');
  $('stTotalSub').textContent = devs.length + ' 台设备 · ' + okBackups.length + ' 条备份记录';
  $('stDevices').textContent = devs.length;
  const online = devs.filter(d => d.online).length;
  $('stDevicesSub').textContent = online + ' 台在线 · ' + (devs.length - online) + ' 台离线';
  const last = okBackups[0];
  $('stLast').textContent = last ? fmtTs(last.finished_at) : '尚无备份';
  $('stLastSub').textContent = last ? ((last.type === 'FULL' ? '全量' : '增量') + ' · ' + fmtSize(last.size_bytes)) : '完成首次配对后即可开始备份';
  $('stFree').textContent = fmtSize(state.status.disk_free_bytes);
  $('stFreeSub').textContent = state.status.backup_dir || '—';
  const hour = new Date().getHours();
  const greet = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
  $('dashHello').textContent = greet + ' · 服务已运行 ' + fmtDur(state.status.uptime_s);
  renderDashAction();

  const box = $('dashDevs');
  box.innerHTML = '';
  if (!devs.length) {
    box.innerHTML =
      '<div class="empty" style="border:1.5px dashed var(--stroke2);border-radius:14px;padding:34px 20px">' +
        '<div class="big-ico" style="width:64px;height:64px;border-radius:20px;margin:0 auto;opacity:.85">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="#00e59b" stroke-width="1.6"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
        '<p style="margin-top:16px"><b style="color:var(--txt);font-size:14px">还没有配对的设备</b><br>' +
          '用数据线把 iPhone 插到 NAS 的 USB 口，点下面的按钮开始配对</p>' +
        '<button class="btn primary" style="margin-top:16px" onclick="openWizard()">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14m-7-7h14"/></svg>添加设备</button>' +
      '</div>';
  }
  devs.slice(0, 4).forEach(d => {
    const days = d.last_backup_at ? Math.floor((Date.now() / 1000 - d.last_backup_at) / 86400) : null;
    const conn = d.connection || (d.online ? 'usb' : 'offline');
    const live = conn !== 'offline';
    const { own: job, src: srcJob } = devJobs(d.udid);
    const pct = Math.max(0, Math.min(100, (job && job.progress) || 0));
    box.insertAdjacentHTML('beforeend',
      '<div class="dev-card" onclick="go(\'devices\')">' +
        '<div class="dev-ico"><svg viewBox="0 0 24 24" fill="none" stroke="' + (live ? '#00e59b' : 'var(--txt3)') + '" stroke-width="1.7"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
        '<div class="info" style="flex:1;min-width:0"><b>' + esc(d.name || '设备') + '</b>' +
          jobChip(job, srcJob) +
          '<div class="meta">' +
          esc(d.ios_version ? 'iOS ' + d.ios_version : '') + ' · UDID ····' + esc(d.udid.slice(-4)) +
          ' · ' + (job ? esc(job.phase || '正在备份') : srcJob ? '正在用于恢复「' + esc(devName(srcJob.udid)) + '」' : days == null ? '未备份过' : days === 0 ? '今天已备份' : days + ' 天未备份') + '</div>' +
          /* 任务进行中：信息行下加一条进度（条 + 百分比），连接状态仍在右侧徽标里 */
          (job ? '<div class="dp-line"><div class="dp-mini" id="dpm-' + esc(d.udid) + '">' +
                   '<i style="width:' + pct + '%"></i></div>' +
                   '<b class="dp-pct" id="dpc-' + esc(d.udid) + '">' + pct + '%</b></div>' : '') +
        '</div>' +
        battChip(d.battery) +
        '<span class="badge ' + (live ? 'g' : 'a') + '"><i class="tag-ico"></i>' +
          (conn === 'network' ? 'Wi-Fi' : live ? 'USB' : '离线') + '</span>' +
        '</div>');
  });

  renderHeat(backups);
  const tl = $('timeline');
  tl.innerHTML = '';
  if (!backups.length) tl.innerHTML = '<div class="tl-item"><div class="t">暂无备份记录</div><div class="d">完成首次备份后，这里会显示动态</div></div>';
  backups.slice(0, 5).forEach(s => {
    const isR = s.kind === 'RESTORE', verb = isR ? '恢复' : '备份';
    const label = s.status === 'COMPLETE' ? verb + '完成' : s.status === 'FAILED' ? verb + '失败' : s.status === 'CANCELED' ? '已取消' : '进行中';
    const cls = s.status === 'COMPLETE' ? 'ok' : s.status === 'FAILED' ? 'warn' : '';
    tl.insertAdjacentHTML('beforeend',
      '<div class="tl-item ' + cls + '"><div class="t">' + esc(devName(s.udid)) + ' ' + label + '</div>' +
      '<div class="d">' + fmtTs(s.started_at) + ' · ' +
        (isR ? '来自「' + esc(devName(s.source_udid || s.udid)) + '」的备份'
             : (s.type === 'FULL' ? '全量' : '增量') + ' · ' + fmtSize(s.size_bytes)) + '</div></div>');
  });
}

/* 设备上的任务：own 是以它为目标的任务（备份/恢复，带进度）；
   src 是把它的备份拿去恢复另一台的任务——这时它本身也被占用（后端 busy_job 同样算忙），
   不能备份、改加密或取消配对，卡片上要看得出来 */
function devJobs(udid) {
  const jobs = (state.status && state.status.active_jobs) || [];
  const own = jobs.find(j => j.udid === udid) || null;
  const src = own ? null : (jobs.find(j => j.source_udid === udid) || null);
  return { own, src, busy: own || src };
}
function jobChip(own, src) {
  if (own) return '<span class="chip-run"><i></i>' + (own.kind === 'RESTORE' ? '恢复中' : '备份中') + '</span>';
  if (src) return '<span class="chip-run" title="这台设备的备份正在被用来恢复「' + esc(devName(src.udid)) + '」"><i></i>恢复来源</span>';
  return '';
}

/* ---------- 设备管理 ---------- */
const CONN_LABEL = { usb: 'USB 直连', network: '无线 Wi-Fi', offline: '未连接' };

function renderDevices() {
  const grid = $('devGrid');
  grid.innerHTML = '';
  state.devices.forEach(d => {
    const { own: job, src: srcJob, busy } = devJobs(d.udid);
    const conn = d.connection || (d.online ? 'usb' : 'offline');
    const live = conn !== 'offline';
    /* 徽标永远表示连接方式（Wi-Fi / USB / 离线）；「备份中」用名字旁的胶囊表达，
       两种状态都要看得见，不能互相顶掉 */
    const badge = conn === 'network' ? 'Wi-Fi' : live ? 'USB' : '离线';
    /* 加密开关不可用时必须说清原因：禁用按钮本身不会说话，
       而 title 属性在手机上根本不显示，用户只看到一个点不动的灰字 */
    const encNote = busy
      ? (srcJob ? '这台设备正在作为恢复来源，任务结束后可修改' : '任务进行中，结束后可修改')
      : live ? (d.encrypted ? '' : '加密备份才包含密码、钥匙串、健康数据')
             : '设备未连接，连上后可修改';
    grid.insertAdjacentHTML('beforeend',
      '<div class="card" style="padding:0;overflow:hidden">' +
        '<div style="padding:20px;display:flex;gap:15px;align-items:center">' +
          '<div class="dev-ico" style="width:52px;height:52px"><svg viewBox="0 0 24 24" fill="none" stroke="' + (live ? '#00e59b' : 'var(--txt3)') + '" stroke-width="1.7" style="width:26px;height:26px"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
          '<div style="flex:1"><b style="font-size:15px">' + esc(d.name || '设备') + '</b>' +
          jobChip(job, srcJob) +
          '<div style="font-size:11.5px;color:var(--txt3);margin-top:4px">' + esc(d.model || '') + ' · iOS ' + esc(d.ios_version || '?') + '</div></div>' +
          '<span class="badge ' + (live ? 'g' : 'a') + '"><i class="tag-ico"></i>' + badge + '</span>' +
        '</div>' +
        '<div style="padding:0 20px 20px;font-size:12.5px;color:var(--txt2);line-height:2.15">' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">连接方式</span><span>' + (CONN_LABEL[conn] || '未连接') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;align-items:center"><span style="color:var(--txt3)">电量</span>' +
            (d.battery ? battChip(d.battery, true)
              : '<span style="color:var(--txt3)">' + (d.battery_pending ? '读取中…' : '未取到') + '</span>') + '</div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">UDID</span><span class="num">···' + esc(d.udid.slice(-12)) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">配对时间</span><span>' + fmtTs(d.paired_at) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">上次备份</span><span>' + (d.last_backup_at ? fmtTs(d.last_backup_at) : '从未备份') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">记录数 / 占用</span><span class="num">' + (d.backup_count || 0) + ' 条 · ' + fmtSize(d.total_bytes) + '</span></div>' +
          /* 加密是设备侧开关：开着才备份密码、钥匙串、健康数据。修改要求设备在线且没有任务在跑 */
          '<div class="enc-row"><div class="enc-l"><span>备份加密</span>' +
            (d.encrypted
              ? '<span class="badge g"><i class="enc-dot"></i>已开启</span>'
              : '<span class="badge a"><i class="enc-dot"></i>未开启</span>') +
            '</div><button class="btn sm' + (d.encrypted ? '' : ' primary') + '"' +
            (live && !busy ? '' : ' disabled') +
            ' onclick="toggleEncryption(\'' + esc(d.udid) + '\')">' + (d.encrypted ? '关闭' : '开启') + '</button></div>' +
          (encNote ? '<div class="enc-hint' + (live && !busy ? '' : ' warn') + '">' + esc(encNote) + '</div>' : '') +
        '</div>' +
        (live || !state.status.wifi_enabled ? '' :
          '<div style="padding:0 20px 14px;font-size:11.5px;color:var(--txt3);line-height:1.75">' +
          'Wi-Fi 没有发现这台设备：确认已在电脑端勾选「在 Wi-Fi 上与此 iPhone 同步」，' +
          '或插回数据线备份。</div>') +
        /* 任务进行中才出现的进度带：高度固定，刷新时只改宽度和数字，卡片不抖 */
        (job ?
          '<div class="dev-prog" id="dp-' + esc(d.udid) + '">' +
            '<div class="dp-top">' +
              '<span><span class="dp-phase">' + esc(job.phase || '准备中') + '</span>' +
                '<span class="dp-elapsed"> · 已用 ' + fmtDur(Math.max(0, Date.now() / 1000 - (job.started_at || Date.now() / 1000))) + '</span></span>' +
              '<b class="dp-pct">' + (job.progress || 0) + '%</b></div>' +
            '<div class="bar"><i style="width:' + Math.max(0, Math.min(100, job.progress || 0)) + '%"></i></div>' +
          '</div>' : '') +
        '<div class="dev-actions">' +
          (busy
            /* 任务跑着的时候再点「立即备份」只会撞上「设备忙」，直接给进度入口 */
            ? '<button class="btn primary" style="flex:1;justify-content:center" onclick="go(\'tasks\')">' + (srcJob ? '恢复进度' : '查看进度') + '</button>'
            : '<button class="btn primary" style="flex:1;justify-content:center" onclick="startBackup(\'' + esc(d.udid) + '\')"' + (live ? '' : ' disabled') + '>立即备份</button>') +
          '<button class="btn" onclick="go(\'tasks\')">历史</button>' +
          '<button class="btn danger"' + (busy ? ' disabled title="任务进行中，取消配对会中断任务"' : '') + ' onclick="removeDevice(\'' + esc(d.udid) + '\')">取消配对</button>' +
        '</div></div>');
  });
  grid.insertAdjacentHTML('beforeend',
    '<div class="card" style="display:flex;flex-direction:column;align-items:center;justify-content:center;' +
    'border:1.5px dashed var(--stroke2);background:transparent;min-height:250px;cursor:pointer" onclick="openWizard()">' +
      '<div class="big-ico" style="opacity:.7"><svg viewBox="0 0 24 24" fill="none" stroke="#00e59b" stroke-width="1.8"><path d="M12 5v14m-7-7h14"/></svg></div>' +
      '<b style="margin-top:16px;font-size:14px">添加新设备</b>' +
      '<p style="font-size:12px;color:var(--txt3);margin-top:6px;text-align:center">用数据线把 iPhone 插到 NAS 的 USB 口</p></div>');
}

async function removeDevice(udid) {
  const d = state.devices.find(x => x.udid === udid);
  const online = !!(d && d.online);
  const tip = online
    ? '设备当前已连接：会同时通知 iPhone 撤销信任，之后需要重新点「信任」。'
    : '设备当前未连接：只能删除 NAS 上的配对证书，iPhone 里的信任会保留。\n' +
      '想彻底撤销，请先把数据线插上、解锁手机再取消。';
  if (!confirm('确定取消与「' + devName(udid) + '」的配对吗？\n\n' + tip + '\n\n已保存的备份不会被删除。')) return;
  try {
    const r = await api('/api/devices/' + encodeURIComponent(udid), { method: 'DELETE' });
    if (r.trust_revoked) {
      hideDevNotice();
      toast(r.message || '已解除配对并撤销信任');
    } else {
      // 撤销没真正生效是「需要用户动手」的事，用常驻提示条而不是一闪而过的 toast
      showDevNotice(r.message, udid);
      toast('已取消配对，但撤销信任未生效，请看上方说明');
    }
    refresh();
  } catch (e) { toast('操作失败：' + e.message); }
}

function showDevNotice(text, udid) {
  if (udid) lastUnpairUdid = udid;
  $('devNoticeText').textContent = text;
  $('devNotice').style.display = 'flex';
  go('devices');
}
function hideDevNotice() {
  $('devNotice').style.display = 'none';
  lastUnpairUdid = null;
}
/* 撤销没生效时，用户解锁手机后可以直接重试，不用重新配对再取消 */
async function retryUnpair() {
  if (!lastUnpairUdid) { toast('没有可重试的设备'); return; }
  const b = $('devNoticeRetry');
  b.disabled = true;
  b.textContent = '撤销中…';
  try {
    const r = await api('/api/devices/' + encodeURIComponent(lastUnpairUdid), { method: 'DELETE' });
    if (r.trust_revoked) {
      hideDevNotice();
      toast(r.message || '已解除配对并撤销信任');
    } else {
      $('devNoticeText').textContent = r.message;
      toast('仍未生效：请解锁 iPhone 后再试');
    }
    refresh();
  } catch (e) {
    toast('重试失败：' + e.message);
  } finally {
    b.disabled = false;
    b.textContent = '重试撤销';
  }
}

/* ---------- 配对向导（真实 SSE） ---------- */
function wizStep(n) {
  [1, 2, 3].forEach(i => {
    const s = $('st' + i);
    s.classList.toggle('on', i === n);
    s.classList.toggle('done', i < n);
  });
}
function wizBody(html, actions) {
  $('wizBody').innerHTML = html;
  $('wizActions').innerHTML = actions;
}
async function openWizard() {
  go('devices');
  $('wizard').style.display = 'flex';        // 浮层：不参与布局，不会再挤压卡片宽度
  lockScroll();                              // 锁住背景滚动
  resetWizScroll();
  if (pairSource) pairSource.close();
  wizStep(1);
  wizBody(
    '<div class="big-ico detect-ring"><svg viewBox="0 0 24 24" fill="none" stroke="#00e59b" stroke-width="1.6"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
    '<h3>请用数据线连接 iPhone 与 NAS</h3>' +
    '<p>将 USB 线插入 NAS 机身上的 USB-A 接口，手机保持解锁。正在监听 USB 总线…</p>' +
    '<p style="color:var(--txt3);font-size:12.5px;margin-top:8px">' +
    '首次配对只能用数据线（iPhone 不支持无线配对）。配对过一次之后，只要手机和 NAS 在同一 Wi-Fi、' +
    '且开启过「通过 Wi-Fi 同步」，备份就不必再插线。</p>',
    '<button class="btn ghost" onclick="closeWizard()">稍后再说</button>');
  try {
    const r = await api('/api/pair/begin', { method: 'POST' });
    pairSource = new EventSource('/api/pair/' + r.session + '/events');
    pairSource.onmessage = ev => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      onPairEvent(d);
    };
    pairSource.onerror = () => {
      pairSource.close(); pairSource = null;
    };
  } catch (e) {
    wizBody('<div class="big-ico"><svg viewBox="0 0 24 24" fill="none" stroke="#ff5d6c" stroke-width="1.8"><path d="M12 8v5m0 3h.01"/><circle cx="12" cy="12" r="9"/></svg></div><h3>无法开始配对</h3><p>' + esc(e.message) + '</p>',
      '<button class="btn" onclick="openWizard()">重试</button>');
  }
}
function onPairEvent(d) {
  if (d.stage === 'waiting') {
    // 插着的都是已配对设备时后端会带 message 说明，别让用户对着转圈干等
    if (d.message) {
      wizBody(
        '<div class="big-ico detect-ring"><svg viewBox="0 0 24 24" fill="none" stroke="#f5b13d" stroke-width="1.6">' +
        '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
        '<h3>这些设备已经配对过了</h3>' +
        '<p>' + esc(d.message) + '</p>' +
        '<p style="color:var(--txt3);font-size:12.5px;margin-top:8px">正在继续等待新设备接入…</p>',
        '<button class="btn ghost" onclick="closeWizard()">知道了</button>');
    }
    return;
  }
  if (d.stage === 'found' || d.stage === 'pairing') {
    wizStep(2);
    const attempt = d.attempt || 0;
    const alreadyHint = d.hint && d.hint.indexOf('已授权') >= 0;
    // 三种需要用户动手的状态：已授权读取中 / 已断线 / 被拒绝需重插
    let title = '在 iPhone 上点击「信任」', tone = '#00e59b';
    if (alreadyHint) { title = '设备已授权，正在读取信息'; }
    else if (d.unplugged) { title = '数据线已拔出，请重新插入'; tone = '#f5b13d'; }
    else if (d.replugged) { title = '已重新连接，正在请求弹窗'; }
    else if (d.denied) { title = '手机上没弹窗？拔掉数据线重插一次'; tone = '#f5b13d'; }
    else if (attempt > 0) { title = '仍等待 iPhone 上的「信任」确认'; }

    let detail;
    if (alreadyHint) {
      detail = '<p>' + esc(d.hint) + '</p>';
    } else if (d.message) {
      // 后端给出的针对性引导优先（拔插线 / 拒绝原因）
      detail = '<p>' + esc(d.message) + '</p>';
    } else if (d.unplugged) {
      detail = '<p>检测到数据线已断开。请把 iPhone 重新插回 NAS 的 USB 口，保持屏幕解锁，插上后会自动继续。</p>';
    } else {
      detail = '<p>已检测到设备 <b style="color:var(--green2)">' + esc(d.udid ? d.udid.slice(-8) : 'iOS 设备') + '</b>。' +
        '请解锁手机，在看到「要信任此电脑吗」时点 <b>「信任」</b> 并输入锁屏密码。' +
        '<br><span style="color:var(--txt3);font-size:12.5px">NAS 只保存配对证书，不读取你的锁屏密码。</span></p>';
    }

    const check = '<div class="phone-check">' +
      '<span class="' + (d.unplugged ? '' : 'hit') + '">① 设备' + (d.unplugged ? '已断开' : '已识别') + '</span>' +
      '<span class="' + (d.denied || d.unplugged ? '' : 'hit') + '">② 等待信任确认</span>' +
      '<span>③ 交换配对证书</span></div>';
    const footer = attempt > 0
      ? '<p style="margin-top:10px;font-size:12px;color:var(--txt3)">已重试 ' + attempt + ' 次' +
        (d.remaining != null ? ' · 剩余 ' + d.remaining + ' 秒' : '') +
        '，完成手机上的确认后会自动继续，无需刷新页面</p>'
      : '';

    wizBody(
      '<div class="big-ico detect-ring"><svg viewBox="0 0 24 24" fill="none" stroke="' + tone + '" stroke-width="1.7">' +
      (d.unplugged
        ? '<path d="M12 3v6m0 0a3 3 0 000 6v6"/><path d="M5 5l14 14"/>'
        : '<path d="M12 21s-7.5-4.6-9.5-9A5.4 5.4 0 0112 6.2 5.4 5.4 0 0121.5 12c-2 4.4-9.5 9-9.5 9z"/>') +
      '</svg></div>' +
      '<h3>' + title + '</h3>' + detail + check + footer,
      '<button class="btn ghost" onclick="closeWizard()">取消</button>');
    return;
  }
  if (d.stage === 'done') {
    if (pairSource) { pairSource.close(); pairSource = null; }
    wizStep(3);
    const already = d.already_paired;
    wizBody(
      '<div class="big-ico" style="background:linear-gradient(135deg,rgba(0,196,127,.3),rgba(0,229,155,.2));box-shadow:0 8px 30px rgba(0,196,127,.3)"><svg viewBox="0 0 24 24" fill="none" stroke="#00e59b" stroke-width="2.2"><path d="M4 12.5l5 5L20 6.5"/></svg></div>' +
      '<h3>' + (already ? '设备已授权 ✓' : '配对成功 🎉') + '</h3>' +
      '<p><b>' + esc(d.name || '设备') + '</b>（iOS ' + esc(d.ios_version || '?') + '）' +
      (already
        ? '此前已与本机建立信任，无需重新确认，可直接开始备份。'
        : '已与 NAS 建立永久信任。配对证书已持久化保存，之后插线或同局域网 Wi-Fi 均可直接备份。') + '</p>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center"><span class="badge g">✓ 证书持久化</span><span class="badge b">建议开启定时备份</span></div>',
      '<button class="btn" onclick="closeWizard()">完成</button><button class="btn primary" onclick="closeWizard();startBackup(\'' + esc(d.udid) + '\')">立即备份一次</button>');
    toast((already ? '设备已就绪：' : '配对成功：') + (d.name || '设备'));
    refresh();
    return;
  }
  if (d.stage === 'error') {
    if (pairSource) { pairSource.close(); pairSource = null; }
    const soft = d.retryable;   // 等信任超时属于可重试的引导态，不是硬失败
    wizBody(
      '<div class="big-ico"><svg viewBox="0 0 24 24" fill="none" stroke="' + (soft ? '#f5b13d' : '#ff5d6c') + '" stroke-width="1.8"><path d="M12 8v5m0 3h.01"/><circle cx="12" cy="12" r="9"/></svg></div>' +
      '<h3>' + (soft ? '还没收到信任确认' : '配对失败') + '</h3>' +
      '<p>' + esc(d.message || '请重新插线并在手机上点「信任」') + '</p>',
      '<button class="btn" onclick="closeWizard()">关闭</button>' +
      '<button class="btn primary" onclick="openWizard()">' + (soft ? '重新等待' : '重试') + '</button>');
  }
}
function closeWizard() {
  // 关 SSE 放在 try 里：任何异常都不能挡住浮层消失
  try { if (pairSource) { pairSource.close(); pairSource = null; } } catch (e) { /* ignore */ }
  $('wizard').style.display = 'none';
  unlockScroll();
}
/* 两个浮层共用 body 滚动锁：都关掉才解锁，否则关一个会让另一个下面的页面滚起来 */
function lockScroll() { document.body.style.overflow = 'hidden'; }
function unlockScroll() {
  const anyOpen = ['wizard', 'logOverlay', 'dlgOverlay'].some(id => {
    const el = $(id);
    return el && el.style.display !== 'none';
  });
  if (!anyOpen) document.body.style.overflow = '';
}
function resetWizScroll() {
  const box = document.querySelector('#wizard .wizard');
  if (box) box.scrollTop = 0;
}
/* Esc 关闭；点遮罩不关（配对进行中误触会中断会话） */
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const dlg = $('dlgOverlay');
  if (dlg && dlg.style.display !== 'none') { closeDlg(); return; }
  const logOv = $('logOverlay');
  if (logOv && logOv.style.display !== 'none') { closeLog(); return; }
  const ov = $('wizard');
  if (ov && ov.style.display !== 'none') closeWizard();
});

/* ---------- 备份记录日志（事后排查用） ---------- */
const _logCache = {};
async function openBackupLog(id) {
  $('logTitle').textContent = '备份记录 #' + String(id).padStart(4, '0') + ' 日志';
  $('logMeta').textContent = '';
  $('logStatus').textContent = '—';
  $('logStatus').className = 'badge b';
  $('logBody').textContent = '加载中…';
  $('logOverlay').style.display = 'flex';
  lockScroll();
  $('logBody').scrollTop = 0;
  try {
    const r = _logCache[id] || (await api('/api/backups/' + id + '/log'));
    _logCache[id] = r;
    const parts = [devName(r.udid), '开始 ' + fmtTs(r.started_at)];
    if (r.finished_at) parts.push('结束 ' + fmtTs(r.finished_at));
    if (r.error) parts.push('原因：' + r.error);
    $('logMeta').textContent = parts.join(' · ');
    const map = { COMPLETE: ['成功', 'g'], FAILED: ['失败', 'r'], CANCELED: ['已取消', 'a'] };
    const [txt, cls] = map[r.status] || ['进行中', 'b'];
    $('logStatus').textContent = txt;
    $('logStatus').className = 'badge ' + cls;
    $('logBody').textContent = r.log ||
      '这条记录没有保存日志。\n\n旧版本产生的任务不会落库；从本版本起，备份与恢复的输出都会存在备份记录里，随时可以回来查看。';
  } catch (e) {
    $('logBody').textContent = '读取失败：' + e.message;
  }
}
function closeLog() {
  $('logOverlay').style.display = 'none';
  unlockScroll();
}
function copyBackupLog(btn) {
  copyText($('logBody').textContent, btn, '复制日志');
}

/* ---------- 诊断信息 ---------- */
async function copyDiagnostics(btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '收集…';
  try {
    const r = await api('/api/diagnostics');
    copyText(r.text, btn, old);
  } catch (e) {
    toast('收集失败：' + e.message);
    btn.textContent = old;
  } finally {
    btn.disabled = false;
  }
}
/* 剪贴板：NAS 上用 http 访问，不是安全上下文，navigator.clipboard 可能不可用，
   所以保留 textarea + execCommand 的退路，否则点了会静默失败。 */
async function copyText(text, btn, restore) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { /* 走退路 */ }
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
  }
  toast(ok ? '已复制到剪贴板' : '复制失败，请手动选中文本复制');
  if (btn && restore) {
    btn.textContent = ok ? '已复制' : restore;
    setTimeout(() => { btn.textContent = restore; }, 1600);
  }
}

/* ---------- 备份任务（真实 SSE） ---------- */
function quickBackup() {
  const d = state.devices.find(x => x.connection === 'usb' || x.connection === 'network' || x.online);
  if (!d) {
    toast(state.status.wifi_enabled
      ? '当前没有可达设备：插上数据线，或让已配对 iPhone 与本机在同一 Wi-Fi 并解锁'
      : '当前没有在线设备，请先用数据线连接 iPhone');
    go('devices');
    return;
  }
  startBackup(d.udid);
}
async function startBackup(udid) {
  if (!udid) {
    const d = state.devices.find(x => x.connection === 'usb' || x.connection === 'network' || x.online);
    if (!d) { toast('没有在线设备，请先用数据线连接'); go('devices'); return; }
    udid = d.udid;
  }
  /* 未加密就开跑，拿到的是不含密码/钥匙串/健康数据的备份，而且事后补不回来
     （只能重新跑一次完整备份）。这信息挂在设备卡片上容易被忽略，
     用户真正动手的地方是这里，所以在动作前问一句，并给一条开启的捷径 */
  const dev = (state.devices || []).find(x => x.udid === udid);
  if (dev && !dev.encrypted) {
    openDlg({
      title: '「' + (dev.name || '这台设备') + '」还没开启备份加密',
      okText: '仍然备份',
      body:
        '<ul class="dlg-note">' +
          '<li>未加密的备份<b>不含</b>保存的密码、钥匙串、健康数据、Wi-Fi 配置</li>' +
          '<li>换机恢复到新手机后，这些要重新登录、重新设置</li>' +
          '<li>开启加密需要设备在线，且开启后下一次是<b>完整备份</b></li>' +
        '</ul>' +
        '<button class="btn" style="width:100%;justify-content:center"' +
          ' onclick="closeDlg();toggleEncryption(\'' + esc(udid) + '\')">先去开启加密</button>'
    }, async () => { await beginBackup(udid); });
    return;
  }
  await beginBackup(udid);
}
async function beginBackup(udid) {
  go('tasks');
  try {
    const r = await api('/api/backups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ udid })
    });
    attachJob(r.job_id, udid, null, 'BACKUP');
    refresh();   // 让总览里的设备卡片立刻出现进度带，不用等下一次轮询
  } catch (e) {
    toast('无法开始备份：' + e.message);
  }
}
function attachJob(jobId, udid, startedAt, kind) {
  if (jobSource) { jobSource.close(); jobSource = null; }
  resetLiveJob();                      // 清掉上一轮留下的终态（失败原因等）
  currentJob = { id: jobId, udid: udid, kind: kind || 'BACKUP' };
  liveJobState = 'running';
  $('liveJob').style.display = 'block';
  $('navJobBadge').textContent = '1';
  $('navJobBadge').style.display = 'block';
  $('jobTitle').textContent = (currentJob.kind === 'RESTORE' ? '正在恢复 · ' : '正在备份 · ') + devName(udid);
  $('term').innerHTML = '';
  clearLog();
  logCount = 0;
  /* 重连时用服务端给的任务开始时间，计时器不会从 0 重新数 */
  jobStartedAt = startedAt ? startedAt * 1000 : Date.now();
  $('prPct').textContent = '0%';
  $('prArc').style.strokeDashoffset = '402';
  $('prPhase').textContent = '准备中';
  $('jDone').textContent = '0% 完成';
  $('jSpeed').textContent = '0 行';
  $('jPhase').textContent = '准备中';
  $('jElapsed').textContent = '0 秒';

  jobSource = new EventSource('/api/jobs/' + jobId + '/events');
  jobSource.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.type === 'log') {
      logCount++;
      queueLog(d.line);
    } else if (d.type === 'live') {
      renderLive(d.text);           // 末行原地刷新，不占日志行数
    } else if (d.type === 'progress') {
      const p = Math.max(0, Math.min(100, d.progress || 0));
      if ($('prPct').textContent !== p + '%') {   // 值没变就不碰 DOM
        $('prPct').textContent = p + '%';
        $('prArc').style.strokeDashoffset = String(402 * (1 - p / 100));
        $('jDone').textContent = p + '% 完成';
      }
      const phase = d.phase || '正在传输';
      if ($('prPhase').textContent !== phase) {
        $('prPhase').textContent = phase;
        $('jPhase').textContent = phase;
      }
      if (currentJob) updateDeviceProgress(currentJob.udid, p, phase);
    } else if (d.type === 'done') {
      const isR = currentJob && currentJob.kind === 'RESTORE';
      finishJob('done', isR ? '✓ 恢复完成'
        : '✓ 备份完成 · 记录 #' + (d.backup_id || '?') + ' 已保存到 NAS');
    } else if (d.type === 'failed') {
      finishJob('failed', (currentJob && currentJob.kind === 'RESTORE' ? '恢复失败' : '备份失败'), d.error);
    } else if (d.type === 'canceled') {
      finishJob('canceled', '任务已取消');
    }
  };
  jobSource.onerror = () => {
    /* 流结束、网络抖动、后端重启都会触发 error。以前这里直接 close，
       等于放弃了 SSE 自带的重连：刷新页面或断一下网，进度和「取消」就再也
       回不来了。现在回查一次后端活跃任务表再决定：还在跑就重新接上，
       已经结束就按结束处理。 */
    if (jobSource) { jobSource.close(); jobSource = null; }
    setTimeout(() => resyncJob(jobId, udid, 0), 1200);
  };
  clearInterval(jobTick);
  jobTick = setInterval(() => {
    if (!jobSource) { clearInterval(jobTick); jobTick = null; return; }
    const el = fmtDur((Date.now() - jobStartedAt) / 1000);
    $('jElapsed').textContent = el;
    /* 卡片上的「已用」也跟着秒走，两处时间不会一个跳一个停 */
    if (currentJob) updateDeviceProgress(currentJob.udid, null, null, el);
  }, 1000);
}
/* 断线后与后端对齐：任务仍存在就重连，否则收尾。最多重试 10 次（约 30 秒），
   后端整体不可达时交给 refresh() 的状态灯去提示，不要无限重连。 */
async function resyncJob(jobId, udid, attempt) {
  let list;
  try {
    list = await api('/api/jobs/active');
  } catch (e) {
    if (attempt < 10) { setTimeout(() => resyncJob(jobId, udid, attempt + 1), 3000); return; }
    detachJob();
    return;
  }
  const j = (list || []).find(x => x.id === jobId);
  if (j) { attachJob(jobId, udid, j.started_at, j.kind); return; }
  /* 任务已经不在活跃表里：说明结束事件丢了（刷新页面/断网）。结果去历史记录里看，
     卡片直接收起，别让它停在最后一个进度上。 */
  detachJob();
  hideLiveJob();
  setTimeout(refresh, 300);
}
function detachJob() {
  if (jobSource) { jobSource.close(); jobSource = null; }
  currentJob = null;
  if (jobTick) { clearInterval(jobTick); jobTick = null; }
  $('navJobBadge').style.display = 'none';
}
/* 收尾三态：成功 → 展示 100% 两秒后收起；取消 → 立即收起；
   失败 → 保留卡片把原因写清楚，按钮变「关闭」。 */
function finishJob(kind, msg, error) {
  const udid = currentJob && currentJob.udid;
  detachJob();
  liveJobState = kind;
  const arc = $('prArc');
  if (kind === 'done') {
    $('prPct').textContent = '100%';
    arc.style.strokeDashoffset = '0';
    $('prPhase').textContent = '已完成';
    $('jPhase').textContent = '已完成';
    toast(msg);
    refresh();                       // 立刻刷新：历史表格与卡片状态不能有时差
    setTimeout(hideLiveJob, 2200);
    return;
  }
  if (kind === 'canceled') {
    toast(msg);
    hideLiveJob();                   // 取消不该留下「99%」这种半成品画面
    refresh();
    return;
  }
  // failed
  $('liveJob').classList.add('failed');
  $('jobTitle').textContent = msg + ' · ' + devName(udid);
  $('prPct').textContent = '✕';
  $('prPhase').textContent = '已失败';
  $('jPhase').textContent = '已失败';
  arc.style.opacity = '.28';
  const box = $('jobErr');
  box.style.display = 'block';
  box.textContent = error ? '失败原因：' + error : '任务失败，未返回具体原因';
  const btn = $('jobActBtn');
  btn.textContent = '关闭';
  btn.className = 'btn';
  btn.onclick = hideLiveJob;
  refresh();
}
/* 收起任务卡并复位，下一轮任务会重新走 resetLiveJob() */
function hideLiveJob() {
  $('liveJob').style.display = 'none';
  resetLiveJob();
}
function resetLiveJob() {
  liveJobState = 'idle';
  const box = $('jobErr');
  if (box) { box.style.display = 'none'; box.textContent = ''; }
  const btn = $('jobActBtn');
  if (btn) { btn.textContent = '取消'; btn.className = 'btn danger'; btn.onclick = cancelJob; }
  const arc = $('prArc');
  if (arc) arc.style.opacity = '';
  $('liveJob').classList.remove('failed');
}
async function cancelJob() {
  const id = currentJob && currentJob.id;
  if (!id) { toast('当前没有进行中的任务'); return; }
  try {
    await api('/api/jobs/' + id + '/cancel', { method: 'POST' });
    toast('已发送取消请求');
  } catch (e) { toast('取消失败：' + e.message); }
}

/* ---------- 任务历史 + 当前可恢复 ---------- */
/* 历史：每次任务一行，只增不改。恢复入口不在这里——磁盘上只有一份可恢复状态，
   挂在某条历史记录上会让人以为能选时间点。 */
function renderHistory() {
  const body = $('histBody');
  body.innerHTML = '';
  /* 运行中的任务由上面 active_jobs 那几行表达（带实时进度）。DB 里同一笔记录
     的状态也是 RUNNING，一起渲染就会出现两行「同一个任务」：一行有进度、
     一行只有「进行中」。所以这里滤掉，只留已收尾的。 */
  const backups = state.backups.filter(s => s.status !== 'RUNNING').slice(0, 12);
  const running = state.status.active_jobs || [];
  if (!backups.length && !running.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">暂无任务记录，完成首次备份后会显示在这里</td></tr>';
    return;
  }
  // 每个 td 带 data-label：窄屏下表格改成卡片时，字段名由 CSS 的 ::before 取出来
  running.forEach(j => {
    body.insertAdjacentHTML('beforeend',
      '<tr><td data-label="设备"><b>' + esc(devName(j.udid)) + '</b></td>' +
      '<td data-label="类型">' + (j.kind === 'RESTORE'
        ? '<span class="badge p">恢复</span>' : '<span class="badge b">备份</span>') + '</td>' +
      '<td data-label="开始时间" class="num">当前</td><td data-label="耗时" class="num">—</td>' +
      '<td data-label="进度" class="num">' + (j.progress || 0) + '%</td>' +
      '<td data-label="状态"><span class="badge g">运行中</span></td>' +
      '<td data-label="操作" style="text-align:right;font-size:12px;color:var(--txt3)">下方实时进度</td></tr>');
  });
  backups.forEach(s => {
    const dur = s.finished_at && s.started_at ? s.finished_at - s.started_at : null;
    const badge = s.status === 'COMPLETE' ? 'g' : s.status === 'FAILED' ? 'r' : 'a';
    const label = s.status === 'COMPLETE' ? '成功' : s.status === 'FAILED' ? '失败' : s.status === 'CANCELED' ? '已取消' : '进行中';
    // 失败的记录即使没日志也留入口：至少能看到失败原因
    const logBtn = (s.has_log || s.status === 'FAILED')
      ? '<button class="log-btn" onclick="event.stopPropagation();openBackupLog(' + s.id + ')" title="'
        + (s.error ? esc(s.error) : '查看这次任务的输出') + '">日志</button>' : '';
    const delBtn = s.status === 'RUNNING' ? ''
      : '<button class="btn ghost" style="font-size:12px;color:var(--txt3)" onclick="deleteBackup(' + s.id + ')" title="只删除这条记录，不动磁盘数据">删除</button>';
    body.insertAdjacentHTML('beforeend',
      '<tr><td data-label="设备"><b>' + esc(devName(s.udid)) + '</b><div style="font-size:11px;color:var(--txt3);margin-top:3px" class="num">' +
        (s.kind === 'RESTORE' ? 'RST-' : 'BKP-') + String(s.id).padStart(4, '0') + '</div></td>' +
      '<td data-label="类型">' + typeBadge(s) + '</td>' +
      '<td data-label="开始时间" class="num">' + fmtTs(s.started_at) + '</td>' +
      '<td data-label="耗时" class="num">' + fmtDur(dur) + '</td>' +
      /* 恢复不产生备份数据，数据量一栏没有意义 */
      '<td data-label="数据量" class="num">' + (s.kind === 'RESTORE' ? '—' : fmtSize(s.size_bytes)) + '</td>' +
      '<td data-label="状态"><span class="badge ' + badge + '">' + label + '</span></td>' +
      '<td data-label="操作" style="text-align:right;white-space:nowrap">' + logBtn + delBtn + '</td></tr>');
  });
}

/* 任务历史的「类型」列：备份分全量/增量；恢复是换机（来源≠目标）时注明来源 */
function typeBadge(s) {
  if (s.kind === 'RESTORE') {
    const cross = s.source_udid && s.source_udid !== s.udid;
    return '<span class="badge p">' + (cross ? '换机恢复' : '恢复') + '</span>' +
      (cross ? '<div style="font-size:11px;color:var(--txt3);margin-top:3px">来自「' + esc(devName(s.source_udid)) + '」</div>' : '');
  }
  return '<span class="badge ' + (s.type === 'FULL' ? 'a' : 'b') + '">' + (s.type === 'FULL' ? '全量' : '增量') + '</span>';
}

/* 当前可恢复：每份备份来源一行（含已取消配对的旧手机，换机时要用它）。
   恢复入口只在这里；点「恢复」后再选恢复到哪台设备 */
function renderRestore() {
  const box = $('restoreList');
  if (!box) return;
  const srcs = state.sources || [];
  if (!srcs.length) {
    box.innerHTML = '<div class="empty" style="padding:18px 0">还没有可恢复的备份，完成一次备份后会出现在这里</div>';
    return;
  }
  box.innerHTML = '';
  srcs.forEach(s => {
    box.insertAdjacentHTML('beforeend',
      '<div style="display:flex;align-items:center;gap:13px;padding:11px 0;border-top:1px solid var(--stroke)">' +
        '<div class="dev-ico" style="width:38px;height:38px"><svg viewBox="0 0 24 24" fill="none" stroke="' +
          (s.paired ? '#00e59b' : 'var(--txt3)') + '" stroke-width="1.7" style="width:20px;height:20px">' +
          '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
        '<div style="flex:1;min-width:0"><b style="font-size:13.5px">' + esc(s.name) + '</b>' +
          (s.paired ? '' : '<span style="font-size:11px;color:var(--txt3);margin-left:7px">已取消配对</span>') +
          '<div style="font-size:11.5px;color:var(--txt3);margin-top:3px">' +
            fmtTs(s.finished_at) + ' · ' + fmtSize(s.size_bytes) +
            (s.ios_version ? ' · iOS ' + esc(s.ios_version) : '') + ' · ' +
            (s.encrypted ? '<span style="color:var(--green2)">已加密</span>' : '未加密') + '</div></div>' +
        '<button class="btn" style="flex:none" onclick="restoreBackup(\'' + esc(s.udid) + '\')">恢复</button>' +
      '</div>');
  });
}

/* ---------- 通用表单浮层（恢复、备份加密共用） ---------- */
let _dlgOk = null;
function openDlg({ title, body, okText, danger }, onOk) {
  $('dlgTitle').textContent = title;
  $('dlgBody').innerHTML = body;
  $('dlgErr').style.display = 'none';
  const ok = $('dlgOk');
  ok.textContent = okText || '确定';
  ok.className = 'btn ' + (danger ? 'danger' : 'primary');
  ok.disabled = false;
  _dlgOk = onOk;
  $('dlgOverlay').style.display = 'flex';
  lockScroll();
  setTimeout(() => { const i = $('dlgBody').querySelector('input,select'); if (i) i.focus(); }, 60);
}
function closeDlg() {
  $('dlgOverlay').style.display = 'none';
  $('dlgBody').innerHTML = '';     /* 密码不要留在 DOM 里 */
  _dlgOk = null;
  unlockScroll();
}
function dlgError(msg) {
  const e = $('dlgErr');
  e.textContent = msg;
  e.style.display = 'block';
}
/* onOk 返回 false 或抛错时浮层保持打开，错误显示在按钮上方 */
async function dlgSubmit() {
  if (!_dlgOk) return;
  const ok = $('dlgOk'), old = ok.textContent;
  ok.disabled = true;
  $('dlgErr').style.display = 'none';
  try {
    const done = await _dlgOk(t => { ok.textContent = t; });
    if (done !== false) closeDlg();
  } catch (e) {
    dlgError(e.message || String(e));
  }
  ok.disabled = false;
  ok.textContent = old;
}

/* 恢复：source 是备份来源，目标默认是它自己（在线时；不在线则留空强制选择），也可以换成另一台已配对设备（换机） */
function restoreBackup(sourceUdid) {
  const src = (state.sources || []).find(s => s.udid === sourceUdid);
  if (!src) { toast('找不到这份备份，请刷新页面'); return; }
  const online = (state.devices || []).filter(d => d.online);
  if (!online.length) {
    toast('没有在线的设备：请把要恢复的手机插到 NAS 的 USB 口');
    return;
  }
  /* 来源本机在线才默认选它；不在线就留空让用户自己选，避免默认值悄悄变成「换机」 */
  const dflt = online.find(d => d.udid === src.udid) || null;
  const opts = (dflt ? '' : '<option value="" selected disabled>请选择要恢复的手机</option>') + online.map(d =>
    '<option value="' + esc(d.udid) + '"' + (dflt && d.udid === dflt.udid ? ' selected' : '') + '>' +
      esc(d.name || '设备') + (d.ios_version ? '（iOS ' + esc(d.ios_version) + '）' : '') +
      (d.udid === src.udid ? ' · 本机' : '') + '</option>').join('');
  openDlg({
    title: '从备份恢复',
    okText: '开始恢复',
    danger: true,
    body:
      '<div class="dlg-note warn"><b>恢复功能尚未经过真机验证，请谨慎使用。</b>' +
        '建议先用备用机测试，或确认目标手机上没有需要保留的数据。</div>' +
      '<div class="dlg-row"><span class="k">备份来源</span><span>' + esc(src.name) +
        ' · ' + fmtTs(src.finished_at) + (src.ios_version ? ' · iOS ' + esc(src.ios_version) : '') + '</span></div>' +
      '<label class="dlg-field"><span class="k">恢复到</span><select id="dlgTarget" class="dlg-input">' + opts + '</select></label>' +
      (src.encrypted
        ? '<label class="dlg-field"><span class="k">备份密码</span>' +
          '<input type="password" id="dlgPw" class="dlg-input" autocomplete="off" placeholder="开启加密时设置的密码"' +
          ' onkeydown="if(event.key===\'Enter\')dlgSubmit()"></label>'
        : '') +
      '<div id="dlgCross" class="dlg-note warn" style="display:none"></div>' +
      '<ul class="dlg-note">' +
        '<li>恢复会<b>覆盖</b>目标手机上的数据，手机会回到备份时的状态，之后新增的内容可能丢失</li>' +
        '<li>请先在目标手机上关闭「查找」（设置 → Apple 账户 → 查找），否则恢复会失败</li>' +
        '<li>建议插 USB 线进行：恢复完成后手机会重启，无线连接可能断开</li>' +
      '</ul>'
  }, async setBusy => {
    const target = $('dlgTarget').value;
    if (!target) throw new Error('请选择要恢复到哪台手机');
    const pw = src.encrypted ? $('dlgPw').value : '';
    if (src.encrypted && !pw) throw new Error('请输入备份密码');
    const name = devName(target);
    /* 一道确认讲清楚对象和风险（恢复尚未在真机验证过）；取消就回到浮层并给出提示 */
    if (!confirm('确认把「' + src.name + '」的备份恢复到「' + name + '」？\n\n' +
      '「' + name + '」上的全部数据会被覆盖，且无法撤销。\n\n' +
      '⚠ 恢复功能尚未经过真机验证，请谨慎使用。建议先用备用机测试，' +
      '或确认目标手机上没有需要保留的数据。')) { dlgError('已取消，未做任何改动'); return false; }
    setBusy('启动中…');
    const r = await api('/api/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ udid: target, source_udid: src.udid, password: pw || undefined })
    });
    toast('恢复任务已启动');
    attachJob(r.job_id, target, null, 'RESTORE');
  });
  /* 换机提示：目标不是来源本机时说明清楚；未加密备份换机会丢密码类数据 */
  const syncCross = () => {
    const t = $('dlgTarget').value, box = $('dlgCross');
    if (!t || t === src.udid) { box.style.display = 'none'; return; }
    box.innerHTML = '换机恢复：把「' + esc(src.name) + '」的备份写入「' + esc(devName(t)) + '」。' +
      (src.encrypted ? '' : '这份备份<b>未加密</b>，不包含密码、钥匙串、健康数据和 Wi-Fi 配置，换机后这些需要重新设置。');
    box.style.display = 'block';
  };
  $('dlgTarget').addEventListener('change', syncCross);
  syncCross();
}

/* 备份加密：iPhone 上的开关。开启后每份备份都由设备加密，备份时不需要密码；
   恢复和关闭时才需要。密码不保存在 NAS 上 */
function toggleEncryption(udid) {
  const d = (state.devices || []).find(x => x.udid === udid);
  if (!d) return;
  const enable = !d.encrypted;
  const pwField = (id, label, ph, ac) =>
    '<label class="dlg-field"><span class="k">' + label + '</span>' +
    '<input type="password" id="' + id + '" class="dlg-input" autocomplete="' + ac + '" placeholder="' + ph + '"' +
    ' onkeydown="if(event.key===\'Enter\')dlgSubmit()"></label>';
  openDlg({
    title: (enable ? '开启' : '关闭') + '备份加密 · ' + (d.name || '设备'),
    okText: enable ? '开启加密' : '关闭加密',
    danger: !enable,
    body: enable
      ? pwField('dlgPw', '设置备份密码', '恢复时需要这个密码', 'new-password') +
        pwField('dlgPw2', '再输入一次', '', 'new-password') +
        '<ul class="dlg-note">' +
          '<li>加密后的备份才包含密码、钥匙串、健康数据和 Wi-Fi 配置，换机必备</li>' +
          '<li><b>NAS 不会保存这个密码。</b>忘记密码就无法恢复加密备份，只能在手机上「还原所有设置」清除它后重新开启</li>' +
          '<li>开启时手机可能要求输入锁屏密码，请解锁手机并留意屏幕</li>' +
          '<li>开启后下一次备份通常会重新完整备份一次，耗时较长</li>' +
        '</ul>'
      : pwField('dlgPw', '当前备份密码', '开启加密时设置的密码', 'current-password') +
        '<ul class="dlg-note">' +
          '<li>关闭后的备份不再包含密码、钥匙串和健康数据</li>' +
          '<li>手机可能要求输入锁屏密码确认，请解锁手机并留意屏幕</li>' +
        '</ul>'
  }, async setBusy => {
    const pw = $('dlgPw').value;
    if (!pw) throw new Error('请输入备份密码');
    if (enable && pw !== $('dlgPw2').value) throw new Error('两次输入的密码不一致');
    setBusy('等待手机确认…');
    const r = await api('/api/devices/' + encodeURIComponent(udid) + '/encryption', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enable, password: pw })
    });
    toast(r.message || '已更新');
    refresh();
  });
}
async function deleteBackup(id) {
  if (!confirm('删除任务记录 #' + id + '？\n注意：磁盘上目前只保留最新一次可恢复状态，删除记录只是移除展示条目，不会释放磁盘空间。')) return;
  try {
    await api('/api/backups/' + id, { method: 'DELETE' });
    toast('记录已删除');
    refresh();
  } catch (e) { toast('删除失败：' + e.message); }
}

/* ---------- 设置 ---------- */
function toggleSwitch(el) {
  el.classList.toggle('on');
  syncTimeBox();
  saveSettings();
}
function syncTimeBox() {
  $('timeBox').classList.toggle('off', !$('swSched').classList.contains('on'));
}
async function saveSettings() {
  const payload = {
    schedule_enabled: $('swSched').classList.contains('on') ? 'true' : 'false',
    schedule_time: $('timeInput').value || '03:00',
    notify_webhook: $('whInput').value.trim(),
    notify_scope: document.querySelector('#notifyScope span.on')?.dataset.v || 'all',
    log_verbose: document.querySelector('#logMode span.on')?.dataset.v || 'false'
  };
  try {
    await api('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    toast('设置已保存');
    state.settings = payload;
    $('schedBadge').textContent = payload.schedule_enabled === 'true' ? '定时：每天 ' + payload.schedule_time : '定时：已关闭';
  } catch (e) { toast('保存失败：' + e.message); }
}
function renderSettings() {
  const s = state.settings || {};
  const on = s.schedule_enabled === 'true' || s.schedule_enabled === '1';
  const time = s.schedule_time || '03:00';
  $('swSched').classList.toggle('on', on);
  syncTimeBox();
  /* 每 6 秒的轮询刷新不要打断用户正在进行的输入 */
  if (document.activeElement !== $('timeInput')) $('timeInput').value = time;
  if (s.notify_webhook && document.activeElement !== $('whInput')) $('whInput').value = s.notify_webhook;
  const scope = s.notify_scope === 'failure' ? 'failure' : 'all';
  document.querySelectorAll('#notifyScope span').forEach(x => x.classList.toggle('on', x.dataset.v === scope));
  const lv = s.log_verbose === 'true' ? 'true' : 'false';
  document.querySelectorAll('#logMode span').forEach(x => x.classList.toggle('on', x.dataset.v === lv));
  $('schedBadge').textContent = on ? '定时：每天 ' + time : '定时：已关闭';
  /* 安全卡片：已启用显示会话与退出入口；未启用给出启用方法 */
  const auth = !!(state.status && state.status.auth);
  const authRow = $('authRow'), authHint = $('authHintRow');
  if (authRow) authRow.style.display = auth ? 'flex' : 'none';
  if (authHint) authHint.style.display = auth ? 'none' : 'flex';
  if (auth) $('authDesc').textContent = '已启用访问密码，登录会话 ' +
    (state.status.session_days || 30) + ' 天有效；修改密码会使所有会话立即失效';
  $('setMeta').textContent = state.status.data_dir || '—';
}
/* 测试通知：直接用输入框当前值发一条测试消息（先测通再保存也行），成功与否都如实反馈 */
async function testWebhook(btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '发送中…';
  try {
    const r = await api('/api/settings/test-webhook', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: $('whInput').value.trim() })
    });
    if (r.ok) toast('测试通知已发送：查收手机 / 群机器人');
    else toast('发送失败：' + (r.error || '未知原因'));
  } catch (e) { toast('测试失败：' + e.message); }
  btn.disabled = false; btn.textContent = old;
}
function wireSettings() {
  $('timeInput').addEventListener('change', saveSettings);
  $('whInput').addEventListener('change', saveSettings);
  document.querySelectorAll('#notifyScope span').forEach(sp => {
    sp.onclick = () => {
      document.querySelectorAll('#notifyScope span').forEach(x => x.classList.remove('on'));
      sp.classList.add('on');
      saveSettings();
    };
  });
  document.querySelectorAll('#logMode span').forEach(sp => {
    sp.onclick = () => {
      document.querySelectorAll('#logMode span').forEach(x => x.classList.remove('on'));
      sp.classList.add('on');
      saveSettings();
    };
  });
  syncTimeBox();
}

/* 侧栏底部：服务状态 + 容器版本号（不写死系统名） */
function renderSideFoot(offline) {
  const st = state.status || {};
  $('sideDot').classList.toggle('off', !!offline);
  $('sideState').textContent = offline ? '服务连接中断' : '服务运行中';
  $('sideVer').textContent = 'iVault' + (st.version ? ' v' + st.version : '');
}

/* ---------- 初始化 ---------- */
async function refresh() {
  try {
    const [st, devs, backups, settings, sources] = await Promise.all([
      api('/api/status'), api('/api/devices'), api('/api/backups'), api('/api/settings'),
      api('/api/restore/sources')
    ]);
    state.status = st; state.devices = devs; state.backups = backups; state.settings = settings;
    state.sources = sources;
    renderDashboard(); renderDevices(); renderHistory(); renderRestore(); renderSettings();
    renderSideFoot(false);
    /* 刷新页面 / 断开重开之后，后端可能还有任务在跑：自动重新接上，
       否则任务在跑却看不到进度，也没有「取消」入口。 */
    const live = (st.active_jobs || [])[0];
    if (live && !jobSource) attachJob(live.id, live.udid, live.started_at, live.kind);
    else if (!live && !jobSource) {
      currentJob = null;
      /* 后端已经没有任何活跃任务，但卡片还停在「进行中」：
         这是结束事件丢失的兜底（刷新页面、断网、后端重启）。
         直接收起——结果去历史记录里看，别留一个假的进度。 */
      if (liveJobState === 'running') hideLiveJob();
    }
    $('setPath').textContent = st.backup_dir || '—';
    $('aboutText').textContent = 'iVault v' + st.version + ' · 备份目录 ' + st.backup_dir;
    $('aboutOnline').textContent = (st.online_devices || []).length + ' 台在线 · ' +
      (st.active_jobs || []).length + ' 个进行中任务 · 无线通道' + (st.wifi_enabled ? '已启用' : '未启用（仅 USB）');
  } catch (e) {
    /* 启用了访问密码且未登录：所有接口都会 401，弹登录浮层并停住常规提示 */
    if (e && e.status === 401) { showAuth(); renderSideFoot(false); return; }
    renderSideFoot(true);
    toast('连接后端失败：' + e.message);
  }
}
renderSideFoot(true);   // 首屏在首个请求回来之前保持「未连接」的真实状态
/* 收起后只剩图标，补 title 便于识别 */
document.querySelectorAll('.nav-item').forEach(el => {
  const label = el.querySelector('span');
  if (label) el.title = label.textContent.trim();
});
applySide();

/* 刷新后回到上次所在的页。值可能是旧版本留下的（页面已不存在），
   校验一下再切，否则 go() 里取不到元素会抛错 */
try {
  const savedView = localStorage.getItem('ivault-view');
  if (savedView && savedView !== 'dashboard' && $('view-' + savedView)) go(savedView);
} catch (e) { /* 读不到就留在总览 */ }

wireSettings();
refresh();
setInterval(refresh, 6000);
