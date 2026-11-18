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
    throw new Error(msg);
  }
  return r.json();
}
function fmtSize(b) {
  if (b == null || isNaN(b) || b === 0) return '—';
  if (b >= 1e12) return (b / 1e12).toFixed(2) + ' TB';
  if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
  return Math.max(1, Math.round(b / 1e3)) + ' KB';
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

/* ---------- 路由 ---------- */
function go(v) {
  document.querySelectorAll('.view').forEach(e => e.classList.remove('active'));
  $('view-' + v).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(e => e.classList.toggle('active', e.dataset.v === v));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
document.querySelectorAll('.nav-item').forEach(e => { e.onclick = () => go(e.dataset.v); });

/* ---------- 全局状态 ---------- */
let state = { devices: [], snapshots: [], status: {}, settings: {} };
let pairSource = null, jobSource = null, jobStartedAt = 0, logCount = 0, jobTick = null;
let lastUnpairUdid = null;

/* ---------- 热力图（最近 105 天，按完成日聚合） ---------- */
function renderHeat(snaps) {
  const h = $('heat');
  h.innerHTML = '';
  const byDay = {};
  snaps.forEach(s => {
    if (s.status === 'COMPLETE' && s.finished_at) {
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

function healthScore(d) {
  if (!d.last_backup_at) return 45;
  const days = (Date.now() / 1000 - d.last_backup_at) / 86400;
  return Math.max(20, Math.round(100 - days * 4));
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
  const devs = state.devices, snaps = state.snapshots;
  const total = devs.reduce((a, d) => a + (d.total_bytes || 0), 0);
  $('stTotal').textContent = fmtSize(total);
  $('stTotalSub').textContent = devs.length + ' 台设备 · ' + snaps.filter(s => s.status === 'COMPLETE').length + ' 份快照';
  $('stDevices').textContent = devs.length;
  const online = devs.filter(d => d.online).length;
  $('stDevicesSub').textContent = online + ' 台在线 · ' + (devs.length - online) + ' 台离线';
  const last = snaps.find(s => s.status === 'COMPLETE');
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
    const score = healthScore(d);
    const col = score >= 80 ? '#00c47f' : score >= 50 ? '#f5b13d' : '#ff5d6c';
    const off = (132 * (1 - score / 100)).toFixed(0);
    const days = d.last_backup_at ? Math.floor((Date.now() / 1000 - d.last_backup_at) / 86400) : null;
    const conn = d.connection || (d.online ? 'usb' : 'offline');
    const live = conn !== 'offline';
    box.insertAdjacentHTML('beforeend',
      '<div class="dev-card" onclick="go(\'devices\')">' +
        '<div class="dev-ico"><svg viewBox="0 0 24 24" fill="none" stroke="' + (live ? '#00e59b' : 'var(--txt3)') + '" stroke-width="1.7"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
        '<div class="info"><b>' + esc(d.name || '设备') + '</b><div class="meta">' +
          esc(d.ios_version ? 'iOS ' + d.ios_version : '') + ' · UDID ····' + esc(d.udid.slice(-4)) +
          ' · ' + (days == null ? '未备份过' : days === 0 ? '今天已备份' : days + ' 天未备份') + '</div></div>' +
        '<span class="badge ' + (live ? 'g' : 'a') + '"><i class="tag-ico"></i>' + (conn === 'network' ? 'Wi-Fi' : live ? 'USB' : '离线') + '</span>' +
        '<div class="ring"><svg width="52" height="52"><circle cx="26" cy="26" r="21" fill="none" stroke="var(--ringtrack)" stroke-width="4"/>' +
        '<circle cx="26" cy="26" r="21" fill="none" stroke="' + col + '" stroke-width="4" stroke-linecap="round" stroke-dasharray="132" stroke-dashoffset="' + off + '"/></svg>' +
        '<div class="num">' + score + '</div></div></div>');
  });

  renderHeat(snaps);
  const tl = $('timeline');
  tl.innerHTML = '';
  if (!snaps.length) tl.innerHTML = '<div class="tl-item"><div class="t">暂无备份记录</div><div class="d">完成首次备份后，这里会显示动态</div></div>';
  snaps.slice(0, 5).forEach(s => {
    const label = s.status === 'COMPLETE' ? '备份完成' : s.status === 'FAILED' ? '备份失败' : s.status === 'CANCELED' ? '已取消' : '进行中';
    const cls = s.status === 'COMPLETE' ? 'ok' : s.status === 'FAILED' ? 'warn' : '';
    tl.insertAdjacentHTML('beforeend',
      '<div class="tl-item ' + cls + '"><div class="t">' + esc(devName(s.udid)) + ' ' + label + '</div>' +
      '<div class="d">' + fmtTs(s.started_at) + ' · ' + (s.type === 'FULL' ? '全量' : '增量') + ' · ' + fmtSize(s.size_bytes) + '</div></div>');
  });
}

/* ---------- 设备管理 ---------- */
const CONN_LABEL = { usb: 'USB 直连', network: '无线 Wi-Fi', offline: '未连接' };

function renderDevices() {
  const grid = $('devGrid');
  grid.innerHTML = '';
  state.devices.forEach(d => {
    const score = healthScore(d);
    const job = (state.status.active_jobs || []).find(j => j.udid === d.udid);
    const conn = d.connection || (d.online ? 'usb' : 'offline');
    const live = conn !== 'offline';
    const badge = job ? '备份中' : conn === 'network' ? 'Wi-Fi' : live ? 'USB' : '离线';
    grid.insertAdjacentHTML('beforeend',
      '<div class="card" style="padding:0;overflow:hidden">' +
        '<div style="padding:20px;display:flex;gap:15px;align-items:center">' +
          '<div class="dev-ico" style="width:52px;height:52px"><svg viewBox="0 0 24 24" fill="none" stroke="' + (live ? '#00e59b' : 'var(--txt3)') + '" stroke-width="1.7" style="width:26px;height:26px"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg></div>' +
          '<div style="flex:1"><b style="font-size:15px">' + esc(d.name || '设备') + '</b>' +
          '<div style="font-size:11.5px;color:var(--txt3);margin-top:4px">' + esc(d.model || '') + ' · iOS ' + esc(d.ios_version || '?') + '</div></div>' +
          '<span class="badge ' + (live || job ? 'g' : 'a') + '"><i class="tag-ico"></i>' + badge + '</span>' +
        '</div>' +
        '<div style="padding:0 20px 20px;font-size:12.5px;color:var(--txt2);line-height:2.15">' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">连接方式</span><span>' + (CONN_LABEL[conn] || '未连接') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">UDID</span><span class="num">···' + esc(d.udid.slice(-12)) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">配对时间</span><span>' + fmtTs(d.paired_at) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">上次备份</span><span>' + (d.last_backup_at ? fmtTs(d.last_backup_at) : '从未备份') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">健康分</span><span style="color:var(--green2)">' + score + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:var(--txt3)">快照数 / 占用</span><span class="num">' + (d.snapshot_count || 0) + ' 份 · ' + fmtSize(d.total_bytes) + '</span></div>' +
        '</div>' +
        '<div style="padding:14px 20px;border-top:1px solid var(--stroke);display:flex;gap:9px">' +
          '<button class="btn primary" style="flex:1;justify-content:center" onclick="startBackup(\'' + d.udid + '\')"' + (live ? '' : ' disabled') + '>立即备份</button>' +
          '<button class="btn" onclick="go(\'library\')">快照</button>' +
          '<button class="btn danger" onclick="removeDevice(\'' + d.udid + '\')">取消配对</button>' +
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
  $('wizard').style.display = 'block';
  $('wizard').scrollIntoView({ behavior: 'smooth', block: 'center' });
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
  if (pairSource) { pairSource.close(); pairSource = null; }
  $('wizard').style.display = 'none';
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
  go('tasks');
  try {
    const r = await api('/api/backups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ udid })
    });
    attachJob(r.job_id, udid);
  } catch (e) {
    toast('无法开始备份：' + e.message);
  }
}
function attachJob(jobId, udid) {
  if (jobSource) jobSource.close();
  $('liveJob').style.display = 'block';
  $('navJobBadge').textContent = '1';
  $('navJobBadge').style.display = 'block';
  $('jobTitle').textContent = '正在备份 · ' + devName(udid);
  $('term').innerHTML = '';
  logCount = 0;
  jobStartedAt = Date.now();
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
      const term = $('term');
      const time = new Date().toTimeString().slice(0, 8);
      term.insertAdjacentHTML('beforeend', '<span class="dim">' + time + '</span>  ' + esc(d.line) + '\n');
      term.scrollTop = term.scrollHeight;
      $('jSpeed').textContent = logCount + ' 行';
    } else if (d.type === 'progress') {
      const p = Math.max(0, Math.min(100, d.progress || 0));
      $('prPct').textContent = p + '%';
      $('prArc').style.strokeDashoffset = String(402 * (1 - p / 100));
      $('jDone').textContent = p + '% 完成';
      const phase = d.phase || '正在传输';
      $('prPhase').textContent = phase;
      $('jPhase').textContent = phase;
    } else if (d.type === 'done') {
      finishJob('✓ 备份完成 · 快照 #' + (d.snapshot || '?') + ' 已保存到 NAS');
    } else if (d.type === 'failed') {
      finishJob('备份失败：' + (d.error || '未知错误'));
    } else if (d.type === 'canceled') {
      finishJob('任务已取消');
    }
  };
  jobSource.onerror = () => {
    // 后端流结束（任务已收尾）时浏览器会触发 error，此时刷新数据即可
    if (jobSource) { jobSource.close(); jobSource = null; }
    setTimeout(refresh, 800);
  };
  clearInterval(jobTick);
  jobTick = setInterval(() => {
    if (!jobSource) { clearInterval(jobTick); jobTick = null; return; }
    $('jElapsed').textContent = fmtDur((Date.now() - jobStartedAt) / 1000);
  }, 1000);
}
function finishJob(msg) {
  if (jobSource) { jobSource.close(); jobSource = null; }
  $('prPhase').textContent = '已结束';
  $('navJobBadge').style.display = 'none';
  toast(msg);
  setTimeout(refresh, 1200);
}
async function cancelJob() {
  if (!jobSource) { toast('当前没有进行中的任务'); return; }
  const id = jobSource.url.split('/').slice(-2)[0];
  try {
    await api('/api/jobs/' + id + '/cancel', { method: 'POST' });
    toast('已发送取消请求');
  } catch (e) { toast('取消失败：' + e.message); }
}

/* ---------- 历史 / 备份库 ---------- */
function renderHistory() {
  const body = $('histBody');
  body.innerHTML = '';
  const snaps = state.snapshots.slice(0, 12);
  if (!snaps.length) { body.innerHTML = '<tr><td colspan="6" class="empty">暂无备份记录，完成首次备份后会显示在这里</td></tr>'; return; }
  (state.status.active_jobs || []).forEach(j => {
    body.insertAdjacentHTML('beforeend',
      '<tr><td><b>' + esc(devName(j.udid)) + '</b></td><td><span class="badge b">进行中</span></td><td class="num">当前</td><td class="num">—</td><td class="num">' + (j.progress || 0) + '%</td><td><span class="badge g">运行中</span></td></tr>');
  });
  snaps.forEach(s => {
    const dur = s.finished_at && s.started_at ? s.finished_at - s.started_at : null;
    const badge = s.status === 'COMPLETE' ? 'g' : s.status === 'FAILED' ? 'r' : 'a';
    body.insertAdjacentHTML('beforeend',
      '<tr><td><b>' + esc(devName(s.udid)) + '</b></td>' +
      '<td><span class="badge ' + (s.type === 'FULL' ? 'a' : 'b') + '">' + (s.type === 'FULL' ? '全量' : '增量') + '</span></td>' +
      '<td class="num">' + fmtTs(s.started_at) + '</td><td class="num">' + fmtDur(dur) + '</td>' +
      '<td class="num">' + fmtSize(s.size_bytes) + '</td>' +
      '<td><span class="badge ' + badge + '">' + esc(s.status === 'COMPLETE' ? '成功' : s.status === 'FAILED' ? '失败' : s.status === 'CANCELED' ? '已取消' : '进行中') + '</span></td></tr>');
  });
}

function renderLibrary() {
  const body = $('snapBody');
  body.innerHTML = '';
  if (!state.snapshots.length) { body.innerHTML = '<tr><td colspan="6" class="empty">备份库为空</td></tr>'; return; }
  state.snapshots.slice(0, 30).forEach(s => {
    const badge = s.status === 'COMPLETE' ? 'g' : s.status === 'FAILED' ? 'r' : 'a';
    const label = s.status === 'COMPLETE' ? '完整' : s.status === 'FAILED' ? '失败' : s.status === 'CANCELED' ? '已取消' : '进行中';
    body.insertAdjacentHTML('beforeend',
      '<tr><td><b>' + fmtTs(s.started_at) + '</b><div style="font-size:11px;color:var(--txt3);margin-top:3px" class="num">SNAP-' + String(s.id).padStart(4, '0') + '</div></td>' +
      '<td>' + esc(devName(s.udid)) + '</td>' +
      '<td><span class="badge ' + (s.type === 'FULL' ? 'a' : 'b') + '">' + (s.type === 'FULL' ? '全量' : '增量') + '</span></td>' +
      '<td class="num">' + fmtSize(s.size_bytes) + '</td>' +
      '<td><span class="badge ' + badge + '">' + label + '</span></td>' +
      '<td style="text-align:right">' +
        '<button class="btn ghost" style="font-size:12px;color:var(--green2)" onclick="restoreSnap(\'' + s.udid + '\')">恢复</button>' +
        '<button class="btn ghost" style="font-size:12px;color:var(--red)" onclick="deleteSnap(' + s.id + ')">删除</button></td></tr>');
  });
}
async function restoreSnap(udid) {
  if (!confirm('将最近一次成功备份恢复到「' + devName(udid) + '」？\n恢复会覆盖设备当前数据，请确保设备已插线连接。')) return;
  try {
    const r = await api('/api/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ udid }) });
    toast('恢复任务已启动');
    attachJob(r.job_id, udid);
  } catch (e) { toast('恢复失败：' + e.message); }
}
async function deleteSnap(id) {
  if (!confirm('删除快照记录 #' + id + '？\n注意：增量快照共享基线目录，物理数据不会立即删除。')) return;
  try {
    await api('/api/snapshots/' + id, { method: 'DELETE' });
    toast('快照记录已删除');
    refresh();
  } catch (e) { toast('删除失败：' + e.message); }
}

/* ---------- 设置 ---------- */
let retDragging = false;

function toggleSwitch(el) {
  el.classList.toggle('on');
  syncTimeBox();
  saveSettings();
}
function syncTimeBox() {
  $('timeBox').classList.toggle('off', !$('swSched').classList.contains('on'));
}
function onRetInput() {
  const v = Math.max(1, Math.min(10, parseInt($('retRange').value, 10) || 5));
  $('retVal').textContent = v + ' 份';
  $('retRange').style.setProperty('--fill', ((v - 1) / 9 * 100).toFixed(1) + '%');
}
async function saveSettings() {
  const payload = {
    schedule_enabled: $('swSched').classList.contains('on') ? 'true' : 'false',
    schedule_time: $('timeInput').value || '03:00',
    retention: String(Math.max(1, Math.min(10, parseInt($('retRange').value, 10) || 5))),
    notify_webhook: $('whInput').value.trim()
  };
  try {
    await api('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    toast('设置已保存');
    state.settings = payload;
    $('schedBadge').textContent = payload.schedule_enabled === 'true' ? '定时：每天 ' + payload.schedule_time : '定时：已关闭';
    $('retBadge').textContent = '保留策略：最近 ' + payload.retention + ' 份 / 设备';
  } catch (e) { toast('保存失败：' + e.message); }
}
function renderSettings() {
  const s = state.settings || {};
  const on = s.schedule_enabled === 'true' || s.schedule_enabled === '1';
  const ret = Math.max(1, Math.min(10, parseInt(s.retention, 10) || 5));
  const time = s.schedule_time || '03:00';
  $('swSched').classList.toggle('on', on);
  syncTimeBox();
  /* 每 6 秒的轮询刷新不要打断用户正在进行的输入 */
  if (document.activeElement !== $('timeInput')) $('timeInput').value = time;
  if (!retDragging && document.activeElement !== $('retRange')) {
    $('retRange').value = ret;
    onRetInput();
  }
  if (s.notify_webhook && document.activeElement !== $('whInput')) $('whInput').value = s.notify_webhook;
  $('schedBadge').textContent = on ? '定时：每天 ' + time : '定时：已关闭';
  $('retBadge').textContent = '保留策略：最近 ' + ret + ' 份 / 设备';
}
function wireSettings() {
  const r = $('retRange');
  r.addEventListener('pointerdown', () => { retDragging = true; });
  ['pointerup', 'pointercancel'].forEach(ev => r.addEventListener(ev, () => { retDragging = false; }));
  r.addEventListener('input', onRetInput);                                   // 拖动时只更新显示
  r.addEventListener('change', () => { retDragging = false; saveSettings(); }); // 松手才提交
  $('timeInput').addEventListener('change', saveSettings);
  $('whInput').addEventListener('change', saveSettings);
  onRetInput();
  syncTimeBox();
}

/* 侧栏底部：服务状态 + 真实系统版本 + 版本号（都不写死） */
function renderSideFoot(offline) {
  const st = state.status || {};
  $('sideDot').classList.toggle('off', !!offline);
  $('sideState').textContent = offline ? '服务连接中断' : '服务运行中';
  $('sideOs').textContent = !offline && st.os_name ? ' · ' + st.os_name : '';
  $('sideVer').textContent = 'iVault' + (st.version ? ' v' + st.version : '');
}

/* ---------- 初始化 ---------- */
async function refresh() {
  try {
    const [st, devs, snaps, settings] = await Promise.all([
      api('/api/status'), api('/api/devices'), api('/api/snapshots'), api('/api/settings')
    ]);
    state.status = st; state.devices = devs; state.snapshots = snaps; state.settings = settings;
    renderDashboard(); renderDevices(); renderHistory(); renderLibrary(); renderSettings();
    renderSideFoot(false);
    $('setPath').textContent = st.backup_dir || '—';
    $('aboutText').textContent = 'iVault v' + st.version + (st.os_name ? ' · ' + st.os_name : '') + ' · 备份目录 ' + st.backup_dir;
    $('aboutOnline').textContent = (st.online_devices || []).length + ' 台在线 · ' +
      (st.active_jobs || []).length + ' 个进行中任务 · 无线通道' + (st.wifi_enabled ? '已启用' : '未启用');
  } catch (e) {
    renderSideFoot(true);
    toast('连接后端失败：' + e.message);
  }
}
renderSideFoot(true);   // 首屏在首个请求回来之前保持「未连接」的真实状态
wireSettings();
refresh();
setInterval(refresh, 6000);
