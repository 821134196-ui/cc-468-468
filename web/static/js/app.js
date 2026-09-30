/* ===== 车库防汛挡板系统 前端 ===== */
'use strict';

const API = {
  async get(u) { const r = await fetch(u); return r.json(); },
  async post(u, body) {
    const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}) });
    return r.json();
  },
  async upload(file) {
    const r = await fetch('/api/photos', { method: 'POST',
      headers: { 'X-Photo-Name': file.name || 'photo.png' }, body: file });
    return r.json();
  }
};

const state = {
  staffId: Number(localStorage.getItem('staffId')) || 3,
  staff: [], dash: null,
  pendingPhoto: null, // 等待随确认上传的照片 {path, url}
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg; el.className = 'toast show ' + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 2600);
}

async function apiPost(url, body) {
  const res = await API.post(url, body);
  if (res.ok === false) { toast(res.error || '操作失败', 'err'); return null; }
  return res;
}

const LEVEL_PILL = { blue: 'pill-blue', yellow: 'pill-yellow', orange: 'pill-orange', red: 'pill-red' };
const KIND_LABEL = { board: '挡板', post: '立柱', seal: '密封条' };
const STATUS_LABEL = {
  available: '可用', locked: '已锁定', deployed: '已部署', cleaning: '清洗中',
  drying: '晾干中', returned: '已归架待核', lost: '已报损', repair: '维修中'
};
const ROLE_LABEL = { guard: '保安', worker: '防汛员', supervisor: '物业主管' };

/* ---------- 顶栏 ---------- */
async function loadStaff() {
  state.staff = await API.get('/api/staff');
  if (!state.staff.find(s => s.id === state.staffId)) state.staffId = state.staff[0]?.id;
  $('#staffPick').innerHTML = state.staff.map(s =>
    `<option value="${s.id}" ${s.id === state.staffId ? 'selected' : ''}>${esc(s.name)}（${ROLE_LABEL[s.role]}）</option>`).join('');
  $('#staffPick').onchange = e => {
    state.staffId = Number(e.target.value);
    localStorage.setItem('staffId', state.staffId);
    render();
  };
}
function currentStaff() { return state.staff.find(s => s.id === state.staffId); }

function roundPill(rnd, label) {
  if (!rnd) return `<span class="pill pill-gray"><span class="dot"></span>无预警</span>`;
  if (rnd.status === 'lifted') return `<span class="pill pill-gray"><span class="dot"></span>预警已解除</span>`;
  return `<span class="pill ${LEVEL_PILL[rnd.level] || 'pill-blue'}"><span class="dot"></span>${esc(label) || rnd.level}预警</span>`;
}

/* ---------- 紧凑雨量趋势 SVG（单系列，雨水蓝） ---------- */
function rainSparkline(trend) {
  const W = 220, H = 56, P = 6;
  const pts = (trend || []).slice(-16).map(p => p.rain_rate ?? 0);
  if (pts.length < 2) {
    return `<div class="trend-wrap"><svg viewBox="0 0 ${W} ${H}">
      <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="#D6DCE0" stroke-width="1"/>
      <text x="${W / 2}" y="${H / 2}" text-anchor="middle" font-size="11" fill="#68757E">等待雨量数据…</text></svg></div>`;
  }
  const maxV = Math.max(25, ...pts) * 1.1;
  const x = i => P + i * (W - 2 * P) / (pts.length - 1);
  const y = v => H - P - (v / maxV) * (H - 2 * P);
  const line = pts.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${H - P} L${P},${H - P} Z`;
  const last = pts[pts.length - 1];
  return `<div class="trend-wrap">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="雨强趋势">
      <path d="${area}" fill="#2B6CA3" opacity="0.10"/>
      <path d="${line}" fill="none" stroke="#2B6CA3" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="3.5" fill="#E87722" stroke="#fff" stroke-width="1.5"/>
    </svg>
    <div class="trend-caption"><span>雨强 mm/h</span><span>最新 ${last}</span></div>
  </div>`;
}

/* ---------- 视图 1：预警看板 ---------- */
function viewDashboard(d) {
  const rnd = d.round;
  const lv = rnd && rnd.status === 'active' ? `lv-${rnd.level}` : (rnd ? 'lv-lifted' : 'lv-blue');
  const title = !rnd ? '当前无预警' : (rnd.status === 'lifted'
    ? `预警已解除（${d.level_labels[rnd.lift_kind === 'early' ? 'lifted' : rnd.level] || ''}）`
    : `${d.round_label}预警进行中`);
  const entrances = d.overview.map(o => {
    const t = o.task;
    const status = t
      ? `<span class="pill ${t.status === 'done' ? 'pill-green' : t.status === 'watch' ? 'pill-blue' : 'pill-orange'}">${t.status_label}</span>`
      : '<span class="pill pill-gray">无任务</span>';
    const prog = t ? `封控${t.filled}/${t.total}段 · 连接${t.installed}/${t.total} · 密封${t.sealed}/${t.total}` : '暂无部署清单';
    return `<tr>
      <td><b>${esc(o.name)}</b><div class="muted">${esc(o.code)} · ${esc(o.ramp_desc)}</div></td>
      <td>${o.width_slots} 段 / ${o.post_count} 柱</td>
      <td>${status}<div class="muted" style="margin-top:3px">${prog}</div></td>
      <td>${t ? `<a href="#/install?task=${t.id}"><button class="btn-sm">进入处理</button></a>` : ''}</td>
    </tr>`;
  }).join('');

  const orders = d.orders.length ? d.orders.map(o => `
    <tr><td><span class="badge badge-orange">${KIND_LABEL[o.kind]}</span> ${esc(o.equipment_code)}</td>
    <td>${o.status === 'reported' ? '已报损待采购' : '采购中'}</td>
    <td>${esc(o.reported_by_name || '')}</td>
    <td class="muted">${esc(o.reason)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">暂无待补充器材</td></tr>';

  const events = d.events.map(ev => {
    const cls = /alert|damage|late|loss|wrong|早|破损|积水|缺/.test(ev.detail) ? 'warn'
      : /封控|完成|入库|解除/.test(ev.detail) ? 'ok' : '';
    return `<li class="${cls}"><span class="t-time">${esc(ev.at.slice(5, 16))}</span>${esc(ev.detail)}</li>`;
  }).join('');

  return `
  <div class="alert-banner ${lv}">
    <div><div class="muted" style="color:rgba(255,255,255,.75)">防汛指挥</div><div class="big">${esc(title)}</div></div>
    <div class="spacer"></div>
    ${rainSparkline(d.trend)}
    <div class="metric">${rnd ? `<b>${rnd.rain_rate}</b> mm/h 雨强<br><b>${rnd.water_level}</b> cm 水位` : '—'}</div>
  </div>

  <div class="grid grid-3" style="margin-bottom:16px">
    <div class="stat blue"><div class="num">${d.overview.filter(o => o.task).length}</div><div class="lbl">已生成部署清单入口</div></div>
    <div class="stat orange"><div class="num">${d.overview.filter(o => o.task && !['done', 'stocktaking', 'drying', 'cleaning', 'removed_early'].includes(o.task.status)).length}</div><div class="lbl">处置中入口</div></div>
    <div class="stat red"><div class="num">${d.orders.length}</div><div class="lbl">待补充器材</div></div>
  </div>

  <div class="card"><h2>🌦 天气模拟与预警推进</h2>
    <div class="row">
      <label class="field">天气脚本
        <select id="scenarioSel">
          <option value="yellow" ${d.scenario === 'yellow' ? 'selected' : ''}>黄色持续（部署演示）</option>
          <option value="storm" ${d.scenario === 'storm' ? 'selected' : ''}>完整风暴（升降级全过程）</option>
          <option value="recede" ${d.scenario === 'recede' ? 'selected' : ''}>雨停回落（解除拆除）</option>
        </select></label>
      <button id="btnScenario" class="btn-ghost">切换并重置时间轴</button>
      <button id="btnPoll">立即复查一次</button>
      <button id="btnAdvance" class="btn-orange">快进 90 秒</button>
      <span class="muted">后台每 15 秒自动复查，连续 3 次安全读数解除预警</span>
    </div>
    ${rnd && rnd.status === 'active' ? `<div class="row" style="margin-top:10px">
      <button id="btnLift" class="btn-ghost">正常解除（天气回落）</button>
      <button id="btnEarly" class="btn-orange">主管提前解除</button>
    </div>` : ''}
  </div>

  <div class="card"><h2>🚧 各入口处置进度</h2>
    <table><thead><tr><th>入口</th><th>规格</th><th>任务状态</th><th></th></tr></thead><tbody>${entrances}</tbody></table>
  </div>

  <div class="grid grid-2">
    <div class="card"><h2>🛠 缺件补充单</h2>
      <table><thead><tr><th>器材</th><th>状态</th><th>主管</th><th>原因</th></tr></thead><tbody>${orders}</tbody></table>
    </div>
    <div class="card"><h2>📜 事件流水</h2><ul class="timeline">${events}</ul></div>
  </div>`;
}

function bindDashboard(d) {
  $('#scenarioSel') && ($('#scenarioSel').onchange = null);
  $('#btnScenario') && ($('#btnScenario').onclick = async () => {
    const r = await apiPost('/api/weather/scenario', { scenario: $('#scenarioSel').value });
    if (r) { toast('天气脚本已切换', 'ok'); render(); }
  });
  $('#btnPoll') && ($('#btnPoll').onclick = async () => {
    const r = await apiPost('/api/weather/poll', {});
    if (r) { toast(r.lifted ? '复查通过，预警已解除' : r.changed ? `预警升级：${r.level}` : `已复查（${r.reading.rain_rate}mm/h）`, r.lifted ? 'ok' : ''); render(); }
  });
  $('#btnAdvance') && ($('#btnAdvance').onclick = async () => {
    const r = await apiPost('/api/weather/scenario', { scenario: d.scenario, advance: 90 });
    if (r) { const p = await apiPost('/api/weather/poll', {}); if (p) render(); }
  });
  $('#btnLift') && ($('#btnLift').onclick = async () => {
    const r = await apiPost('/api/rounds/lift', { round_id: d.round.id, kind: 'normal' });
    if (r) { toast('预警正常解除，可以拆除', 'ok'); render(); }
  });
  $('#btnEarly') && ($('#btnEarly').onclick = async () => {
    const reason = prompt('请物业主管填写提前解除原因：');
    if (!reason) return;
    const sup = state.staff.find(s => s.role === 'supervisor');
    const r = await apiPost('/api/rounds/lift', { round_id: d.round.id, kind: 'early', approver_id: sup.id, reason });
    if (r) { toast('主管已批准提前解除', 'ok'); render(); }
  });
}

/* ---------- 通用：入口横向挡板拼接图 ---------- */
function baffleMap(o, boards, posts) {
  const postAt = slot => (posts || []).find(p => p.slot === slot);
  const segs = [];
  for (let slot = 0; slot < o.width_slots; slot++) {
    const b = boards.find(x => x.slot === slot);
    const lp = postAt(slot);
    segs.push(`<div class="post ${lp && lp.post_id ? '' : 'placeholder'}" title="${lp && lp.post_id ? esc(lp.post_code) : '待立立柱'}"></div>`);
    if (b && b.board_id) {
      const cls = b.conn_confirmed && b.seal_confirmed ? '' : 'locked';
      const photo = b.photo_path ? 'has-photo' : '';
      segs.push(`<div class="seg ${cls} ${photo}">
        <div class="code">${esc(b.board_code)}</div>
        <div class="muted" style="color:rgba(255,255,255,.8)">${esc(b.seal_code || '无密封条')}</div>
        <div class="checks">
          <span class="${b.conn_confirmed ? 'chk-ok' : 'chk-no'}">${b.conn_confirmed ? '✓连接' : '○连接'}</span>
          <span class="${b.seal_confirmed ? 'chk-ok' : 'chk-no'}">${b.seal_confirmed ? '✓密封' : '○密封'}</span>
        </div></div>`);
    } else {
      segs.push(`<div class="seg empty"><div class="slot-tag">缺段空槽</div><div class="muted">第 ${slot + 1} 段未装</div></div>`);
    }
    if (slot === o.width_slots - 1) {
      const ep = postAt(o.width_slots);
      segs.push(`<div class="post ${ep && ep.post_id ? '' : 'placeholder'}" title="${ep && ep.post_id ? esc(ep.post_code) : '待立末端立柱'}"></div>`);
    }
  }
  return segs.join('');
}

/* ---------- 视图 2：入口示意图 ---------- */
async function viewDiagram() {
  const [ents, tasks] = await Promise.all([API.get('/api/entrances'), API.get('/api/tasks?all=1')]);
  const html = [];
  for (const ent of ents) {
    const task = tasks.find(t => t.entrance_id === ent.id && t.round_status === 'active')
      || tasks.find(t => t.entrance_id === ent.id);
    let line = '', info = '<span class="badge">未部署</span>', water = '';
    if (task) {
      const full = await API.get(`/api/tasks/${task.id}`);
      line = `<div class="baffle-line">${baffleMap(ent, full.boards, full.posts)}</div>`;
      const obs = full.observations[0];
      if (obs) water = `<div class="waterline"><i style="width:${Math.min(100, obs.water_level_cm / 35 * 100)}%"></i></div>
        <div class="muted">最近积水：${obs.water_level_cm}cm（${esc(obs.at.slice(5, 16))}）</div>`;
      info = `<span class="pill ${task.status === 'watch' ? 'pill-blue' : 'pill-orange'}">${STATUS_TASK(task.status)}</span>
        <a href="#/install?task=${task.id}"><button class="btn-sm btn-ghost">详情</button></a>`;
    } else {
      line = `<div class="baffle-line">${Array.from({ length: ent.width_slots }, (_, i) =>
        `<div class="post placeholder"></div><div class="seg empty"><div class="slot-tag">空槽 ${i + 1}</div></div>`).join('')}
        <div class="post placeholder"></div></div>`;
    }
    html.push(`<div class="ramp">
      <div class="ramp-head"><div class="name">${esc(ent.name)}</div>
        <span class="muted">${esc(ent.code)} · ${ent.width_slots}段${ent.post_count}柱</span>
        <div class="spacer"></div>${info}</div>
      <div class="muted" style="margin-bottom:8px">${esc(ent.ramp_desc)}</div>
      ${line}${water}
    </div>`);
  }
  return `<div class="card"><h2>🗺️ 入口挡板拼接示意图</h2>
    <p class="muted">按挡板实际拼接顺序自左向右排列；橙色虚线为缺段空槽，斜纹立柱表示尚未立起。横向可滑动查看。</p>
    <div class="legend">
      <span><i style="background:#2B6CA3"></i>已装并确认</span>
      <span><i style="background:#617788"></i>已装待确认</span>
      <span><i style="background:repeating-linear-gradient(-45deg,#FFF6EE,#FFF6EE 4px,#FDEBDC 4px,#FDEBDC 8px);border:1px dashed #E87722"></i>缺段空槽</span>
    </div></div>
  <div class="diagram-scroll card"><div class="entrance-map">${html.join('')}</div></div>`;
}
function STATUS_TASK(s) {
  return ({ planned: '待领件', claimed: '已领件', installing: '安装中', sealed: '已封控', watch: '守看中',
    cleaning: '待清洗', removed_early: '提前拆除', drying: '晾干中', stocktaking: '归架盘点', done: '已完成' })[s] || s;
}

/* ---------- 视图 3：器材领用 ---------- */
async function viewClaim() {
  const [tasks, eq] = await Promise.all([API.get('/api/tasks?all=1'), API.get('/api/equipment')]);
  const active = tasks.filter(t => ['planned', 'claimed', 'installing'].includes(t.status));
  const taskOpts = active.map(t => `<option value="${t.id}">#${t.id} ${esc(t.entrance_name)}（${STATUS_TASK(t.status)}）</option>`).join('');
  const groups = ['board', 'post', 'seal'].map(k => {
    const rows = eq.filter(e => e.kind === k).map(e => `
      <tr><td><b>${esc(e.code)}</b></td><td>${e.length_cm}cm</td>
      <td><span class="badge ${e.status === 'available' ? 'badge-green' : e.status === 'locked' || e.status === 'deployed' ? 'badge-orange' : ''}">${STATUS_LABEL[e.status] || e.status}</span></td>
      <td>${esc(e.rack_name || '-')}</td>
      <td class="muted">${e.active_task_id ? `任务 #${e.active_task_id}` : ''}${esc(e.damage_note ? ' · ' + e.damage_note : '')}</td></tr>`).join('');
    return `<div class="card"><h2>🧰 ${KIND_LABEL[k]}库存</h2>
      <table><thead><tr><th>编号</th><th>规格</th><th>状态</th><th>存放架</th><th>占用</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }).join('');
  return `
  <div class="card"><h2>📋 按入口领件锁定器材</h2>
    <p class="muted">预警达阈值后按入口生成部署清单；领件时锁定对应挡板、立柱与密封条，<b>同一器材不能被两个入口同时领取</b>。</p>
    <div class="row">
      <label class="field">部署任务 <select id="claimTask">${taskOpts || '<option value="">无待领件任务</option>'}</select></label>
      <button id="btnClaim" class="btn-orange" style="margin-top:14px">确认领件并锁定器材</button>
    </div>
    <div id="claimMsg" class="muted" style="margin-top:8px"></div>
  </div>${groups}`;
}
function bindClaim() {
  $('#btnClaim') && ($('#btnClaim').onclick = async () => {
    const tid = Number($('#claimTask').value);
    if (!tid) return;
    const r = await apiPost(`/api/tasks/${tid}/claim`, { staff_id: state.staffId });
    if (r) { toast(`已锁定 ${r.locked} 件器材`, 'ok'); render(); }
  });
}

/* ---------- 流程步骤条 ---------- */
function flowBar(status) {
  const steps = ['planned', 'claimed', 'installing', 'watch', 'cleaning', 'drying', 'stocktaking', 'done'];
  const labels = { planned: '清单', claimed: '领件', installing: '安装', watch: '封控守看', cleaning: '清洗', drying: '晾干', stocktaking: '归架', done: '完成' };
  const cur = status === 'removed_early' ? 'cleaning' : status;
  const ci = steps.indexOf(cur);
  return `<div class="flow-steps">${steps.map((s, i) =>
    `<span class="step ${i === ci ? 'active' : ''} ${i < ci ? 'done' : ''}">${labels[s]}</span>${i < steps.length - 1 ? '<span class="arrow">›</span>' : ''}`).join('')}</div>`;
}

/* ---------- 任务选择器（多页面共用） ---------- */
async function taskSelector(allowed, currentId) {
  const tasks = await API.get('/api/tasks?all=1');
  const list = tasks.filter(t => allowed.includes(t.status));
  const opts = list.map(t =>
    `<option value="${t.id}" ${t.id === Number(currentId) ? 'selected' : ''}>#${t.id} ${esc(t.entrance_name)} · ${STATUS_TASK(t.status)}</option>`).join('');
  return { opts: opts || '<option value="">（无）</option>', count: list.length, first: list[0]?.id };
}

/* ---------- 视图 4：分段安装（移动端大勾选 + 固定拍照） ---------- */
async function viewInstall(params) {
  const sel = await taskSelector(['claimed', 'installing', 'sealed', 'watch'], params.get('task'));
  return `
  <div class="card"><h2>🔩 分段安装确认</h2>
    <div class="row"><label class="field">选择任务 <select id="installTask" style="min-width:240px">${sel.opts}</select></label>
    <button id="btnLoadInstall" class="btn-ghost">打开</button></div></div>
  <div id="installBody"></div>`;
}

async function renderInstallBody(tid) {
  const root = $('#installBody');
  const t = await API.get(`/api/tasks/${tid}`);
  if (!t.detail) { root.innerHTML = ''; return; }
  const d = t.detail;
  const staff = currentStaff();
  const canInstall = staff.role === 'worker' || staff.role === 'supervisor';
  const canGuard = staff.role === 'guard' || staff.role === 'supervisor';

  const segCards = t.boards.map((b, i) => {
    if (!b.board_id) {
      return `<div class="segment-card empty-slot"><b>第 ${i + 1} 段 · <span style="color:var(--alert-orange-dark)">缺段空槽</span></b>
        <p class="muted">该槽位尚未安装挡板。可领备用挡板后由安装工纠正装回。</p></div>`;
    }
    const done = b.conn_confirmed && b.seal_confirmed;
    return `<div class="segment-card ${done ? 'done' : ''}" data-slot="${b.slot}">
      <div class="row"><b>第 ${i + 1} 段</b>
        <span class="badge badge-blue">${esc(b.board_code)}</span>
        <span class="badge">密封条 ${esc(b.seal_code || '未配')}</span>
        ${b.photo_path ? `<img class="photo-thumb small" src="/photos/${esc(b.photo_path)}">` : ''}
        <div class="spacer"></div>
        <button class="btn-sm btn-ghost act-fix" data-slot="${b.slot}">挡板错段？更换</button>
        <button class="btn-sm btn-danger act-seal-damage" data-slot="${b.slot}">密封条破损</button>
      </div>
      <button class="big-check ${b.conn_confirmed ? 'checked' : ''} act-conn" data-slot="${b.slot}" ${canInstall ? '' : 'disabled'}>
        <span class="box">✓</span><span>连接确认：卡槽 / 螺栓 / 立柱咬合牢固</span></button>
      <button class="big-check orange ${b.seal_confirmed ? 'checked' : ''} act-seal" data-slot="${b.slot}" ${canInstall ? '' : 'disabled'}>
        <span class="box">✓</span><span>密封确认：底部密封条贴合、无缝隙渗漏</span></button>
      ${b.note ? `<div class="muted">备注：${esc(b.note)}</div>` : ''}
    </div>`;
  }).join('');

  const passages = t.passages.map(p => `<tr>
    <td><b>${esc(p.plate)}</b></td><td>${esc(p.staff_name || '')}</td><td>${esc(p.opened_at.slice(5, 16))}</td>
    <td>${esc(p.reason)}</td><td>${p.resealed ? '<span class="badge badge-green">已重新封控</span>' : '<span class="badge badge-orange">通道开启中</span>'}</td>
    <td>${p.resealed ? '' : `<button class="btn-sm act-reseal" data-pid="${p.id}">通过后重新封控</button>`}</td></tr>`).join('');

  root.innerHTML = `
  <div class="card">
    ${flowBar(d.status)}
    <div class="row"><h2 style="margin:0">${esc(d.entrance_name)}</h2>
      <span class="pill ${d.status === 'watch' ? 'pill-blue' : 'pill-orange'}">${d.status_label}</span>
      <span class="muted">轮次 ${d.round_label}预警 · 领件人 ${esc(d.claimed_by_name || '-')}</span>
      <div class="spacer"></div>
      ${d.status === 'claimed' ? `<button id="btnBegin" class="btn-orange" ${canInstall ? '' : 'disabled'}>开始安装</button>` : ''}
    </div>
    ${!canInstall ? '<p class="muted" style="margin-top:8px">当前岗位为保安，连接/密封确认由防汛员或主管操作；保安可执行封控与放行。</p>' : ''}
  </div>

  <div class="card"><h2>逐段确认（每段连接 + 密封）</h2>${segCards}</div>

  <div class="card"><h2>🚧 通道封控与临时放行</h2>
    <p class="muted">全部 ${d.width_slots} 段连接、密封确认后，<b>保安可执行封控</b>。</p>
    <div class="row">
      <button id="btnClosure" class="btn-orange" ${canGuard && ['claimed', 'installing', 'watch'].includes(d.status) ? '' : 'disabled'}>确认通道封控（含拍照）</button>
      <span class="muted">封控后进入积水守看</span>
    </div>
    <h3>临时车辆放行</h3>
    <div class="row">
      <input id="plate" placeholder="车牌号 如 沪A12345" style="width:150px">
      <input id="passReason" placeholder="放行原因（可选）" style="flex:1;min-width:160px">
      <button id="btnPass" class="btn-ghost" ${canGuard && d.status === 'watch' ? '' : 'disabled'}>登记放行</button>
    </div>
    <table style="margin-top:10px"><thead><tr><th>车牌</th><th>保安</th><th>时间</th><th>原因</th><th>状态</th><th></th></tr></thead>
      <tbody>${passages || '<tr><td colspan="6" class="muted">暂无放行记录</td></tr>'}</tbody></table>
  </div>`;

  const photo = () => state.pendingPhoto?.path || '';
  $('#btnBegin') && ($('#btnBegin').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/begin`, { staff_id: state.staffId });
    if (r) { toast('已开工', 'ok'); clearPhoto(); renderInstallBody(tid); }
  });
  root.querySelectorAll('.act-conn').forEach(el => el.onclick = async () => {
    const r = await apiPost(`/api/segments/${tid}/${el.dataset.slot}/connection`,
      { staff_id: state.staffId, photo: photo(), note: '现场确认' });
    if (r) { toast('连接已确认', 'ok'); clearPhoto(); renderInstallBody(tid); }
  });
  root.querySelectorAll('.act-seal').forEach(el => el.onclick = async () => {
    const r = await apiPost(`/api/segments/${tid}/${el.dataset.slot}/seal`,
      { staff_id: state.staffId, photo: photo() });
    if (r) { toast('密封已确认', 'ok'); clearPhoto(); renderInstallBody(tid); }
  });
  root.querySelectorAll('.act-fix').forEach(el => el.onclick = async () => {
    const code = prompt('输入正确的备用挡板编号（如 DB-09）：');
    if (!code) return;
    const r = await apiPost(`/api/segments/${tid}/${el.dataset.slot}/fix-segment`,
      { board_code: code.toUpperCase(), staff_id: state.staffId });
    if (r) { toast('错段已纠正，请重新确认连接与密封', 'ok'); renderInstallBody(tid); }
  });
  root.querySelectorAll('.act-seal-damage').forEach(el => el.onclick = async () => {
    const r = await apiPost(`/api/segments/${tid}/${el.dataset.slot}/seal-damage`,
      { staff_id: state.staffId, note: '密封条破损更换' });
    if (r) { toast(r.message, ''); renderInstallBody(tid); }
  });
  $('#btnClosure').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/closure`, { staff_id: state.staffId, photo: photo() });
    if (r) { toast('通道已封控，转入守看', 'ok'); clearPhoto(); renderInstallBody(tid); }
  };
  $('#btnPass').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/passages`,
      { plate: $('#plate').value, reason: $('#passReason').value, staff_id: state.staffId });
    if (r) { toast('放行已登记，车辆通过后须重新封控', ''); renderInstallBody(tid); }
  };
  root.querySelectorAll('.act-reseal').forEach(el => el.onclick = async () => {
    const r = await apiPost(`/api/passages/${el.dataset.pid}/reseal`,
      { staff_id: state.staffId, photo: photo() });
    if (r) { toast('已重新封控并确认密封', 'ok'); clearPhoto(); renderInstallBody(tid); }
  });
  showPhotoBar();
}

function bindInstall() {
  const sel = $('#installTask');
  $('#btnLoadInstall') && ($('#btnLoadInstall').onclick = () => {
    if (sel.value) { location.hash = `#/install?task=${sel.value}`; renderInstallBody(Number(sel.value)); }
  });
  const tid = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('task'));
  if (tid && sel) renderInstallBody(tid);
}

/* ---------- 视图 5：积水观察 ---------- */
async function viewWater(params) {
  const sel = await taskSelector(['sealed', 'watch'], params.get('task'));
  return `<div class="card"><h2>💧 入口积水观察</h2>
    <div class="row"><label class="field">守看中的任务 <select id="waterTask" style="min-width:240px">${sel.opts}</select></label>
    <button id="btnLoadWater" class="btn-ghost">打开</button></div></div>
  <div id="waterBody"></div>`;
}
async function renderWaterBody(tid) {
  const root = $('#waterBody');
  const t = await API.get(`/api/tasks/${tid}`);
  if (!t.detail) { root.innerHTML = ''; return; }
  const d = t.detail;
  const rows = t.observations.map(o => `<tr>
    <td>${esc(o.at.slice(5, 16))}</td><td><b>${o.water_level_cm} cm</b>
    ${o.water_level_cm >= 10 ? '<span class="badge badge-red">超警戒 10cm</span>' : '<span class="badge badge-green">可控</span>'}</td>
    <td>${esc(o.staff_name || '')}</td><td>${esc(o.note)}</td>
    <td>${o.photo_path ? `<img class="photo-thumb small" src="/photos/${esc(o.photo_path)}">` : ''}</td></tr>`).join('');
  root.innerHTML = `<div class="card">
    ${flowBar(d.status)}
    <div class="row"><h2 style="margin:0">${esc(d.entrance_name)}</h2><div class="spacer"></div>
      <span class="pill pill-blue">${d.status_label}</span></div>
    <div class="grid grid-3" style="margin:12px 0">
      <label class="field">水位（厘米）<input id="wl" type="number" step="0.5" value="5"></label>
      <label class="field" style="grid-column: span 2">情况说明 <input id="wnote" placeholder="如：截水流正常、坡道无明显上涨"></label>
    </div>
    <div class="row"><button id="btnObs" class="btn-orange">记录积水观察（含拍照）</button>
      <span class="muted">≥10cm 自动标记警戒并写入事件流水</span></div>
  </div>
  <div class="card"><h2>观察记录</h2>
    <table><thead><tr><th>时间</th><th>水位</th><th>记录人</th><th>说明</th><th>照片</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted">暂无记录</td></tr>'}</tbody></table>
  </div>
  <div class="card"><h2>📜 任务事件</h2><ul class="timeline">${t.events.slice(0, 12).map(ev =>
    `<li class="${/alert|积水|警戒/.test(ev.detail) ? 'warn' : ''}"><span class="t-time">${esc(ev.at.slice(5, 16))}</span>${esc(ev.detail)}</li>`).join('')}</ul></div>`;
  $('#btnObs').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/observations`, {
      water_level_cm: Number($('#wl').value), note: $('#wnote').value,
      staff_id: state.staffId, photo: state.pendingPhoto?.path || '' });
    if (r) { toast(r.alert ? '已标记积水警戒！' : '观察已记录', r.alert ? 'err' : 'ok'); clearPhoto(); renderWaterBody(tid); }
  };
  showPhotoBar();
}
function bindWater() {
  const sel = $('#waterTask');
  $('#btnLoadWater') && ($('#btnLoadWater').onclick = () => {
    if (sel.value) { location.hash = `#/water?task=${sel.value}`; renderWaterBody(Number(sel.value)); }
  });
  const tid = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('task'));
  if (tid && sel) renderWaterBody(tid);
}

/* ---------- 视图 6：拆除清洗 ---------- */
async function viewDismantle(params) {
  const sel = await taskSelector(['watch', 'sealed', 'cleaning', 'removed_early', 'drying'], params.get('task'));
  return `<div class="card"><h2>🧽 拆除 · 清洗 · 晾干</h2>
    <p class="muted"><b>预警解除后才能拆除</b>；特殊情况须由<b>物业主管决定并审批提前拆除</b>。拆除后依次清洗、晾干、上存放架。</p>
    <div class="row"><label class="field">任务 <select id="dmTask" style="min-width:240px">${sel.opts}</select></label>
    <button id="btnLoadDm" class="btn-ghost">打开</button></div></div>
  <div id="dmBody"></div>`;
}
async function renderDmBody(tid) {
  const root = $('#dmBody');
  const t = await API.get(`/api/tasks/${tid}`);
  if (!t.detail) { root.innerHTML = ''; return; }
  const d = t.detail;
  const staff = currentStaff();
  const isSup = staff.role === 'supervisor';
  const approved = t.approvals.length > 0;
  const lifted = d.round_status === 'lifted';
  const dm = t.dismantle || {};

  root.innerHTML = `<div class="card">
    ${flowBar(d.status)}
    <div class="row"><h2 style="margin:0">${esc(d.entrance_name)}</h2><div class="spacer"></div>
      <span class="pill ${d.status === 'watch' ? 'pill-blue' : 'pill-orange'}">${d.status_label}</span></div>

    <h3>① 拆除条件</h3>
    <div class="row">
      ${lifted ? '<span class="badge badge-green">✓ 预警已解除（正常拆除）</span>'
        : approved ? '<span class="badge badge-orange">✓ 物业主管已批准提前拆除</span>'
        : '<span class="badge badge-red">✗ 预警未解除，禁止拆除</span>'}
      ${!lifted && !approved ? `<button id="btnApprove" class="btn-orange" ${isSup ? '' : 'disabled'}>物业主管审批提前拆除</button>
        <span class="muted">${isSup ? '' : '（仅物业主管账号可审批，可在右上角切换岗位）'}</span>` : ''}
    </div>
    ${approved && !lifted ? `<div class="muted" style="margin-top:6px">审批：${esc(t.approvals[0].staff_name)} — ${esc(t.approvals[0].reason)}</div>` : ''}

    <h3>② 拆除 / ③ 清洗 / ④ 晾干</h3>
    <div class="grid grid-3">
      <div class="stat ${dm.dismantled_at ? 'blue' : ''}"><div class="num">${dm.dismantled_at ? '✓' : '…'}</div><div class="lbl">拆除${dm.dismantled_at ? ' ' + esc(dm.dismantled_at.slice(5, 16)) : ''}</div></div>
      <div class="stat ${dm.cleaned_at ? 'blue' : ''}"><div class="num">${dm.cleaned_at ? '✓' : '…'}</div><div class="lbl">清洗${dm.cleaned_at ? ' ' + esc(dm.cleaned_at.slice(5, 16)) : ''}</div></div>
      <div class="stat ${dm.dried_at ? 'blue' : ''}"><div class="num">${dm.dried_at ? '✓' : '…'}</div><div class="lbl">晾干${dm.dried_at ? ' ' + esc(dm.dried_at.slice(5, 16)) : ''}</div></div>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="btnDm" ${d.status === 'watch' || d.status === 'sealed' ? '' : 'disabled'}>执行拆除</button>
      <button id="btnClean" class="btn-ghost" ${['cleaning', 'removed_early'].includes(d.status) ? '' : 'disabled'}>登记清洗完成</button>
      <button id="btnDry" class="btn-orange" ${d.status === 'drying' ? '' : 'disabled'}>登记晾干完成，上存放架</button>
      <a href="#/stocktake?task=${tid}"><button class="btn-ghost" ${d.status === 'stocktaking' || d.status === 'done' ? '' : 'disabled'}>前往归架盘点 →</button></a>
    </div>
  </div>`;

  $('#btnApprove') && ($('#btnApprove').onclick = async () => {
    const reason = prompt('提前拆除原因（主管审批）：');
    if (!reason) return;
    const r = await apiPost(`/api/tasks/${tid}/early-approve`,
      { supervisor_id: state.staffId, reason });
    if (r) { toast('提前拆除已批准', 'ok'); renderDmBody(tid); }
  });
  $('#btnDm').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/dismantle`, { staff_id: state.staffId });
    if (r) { toast('已拆除，进入清洗', 'ok'); renderDmBody(tid); }
  };
  $('#btnClean').onclick = async () => {
    const note = prompt('清洗备注（可留空）：', '') ?? '';
    const r = await apiPost(`/api/tasks/${tid}/cleaned`, { staff_id: state.staffId, note });
    if (r) { toast('清洗完成，等待晾干', 'ok'); renderDmBody(tid); }
  };
  $('#btnDry').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/dried`, { staff_id: state.staffId });
    if (r) { toast('晾干完成，已上存放架', 'ok'); renderDmBody(tid); }
  };
}
function bindDismantle() {
  const sel = $('#dmTask');
  $('#btnLoadDm') && ($('#btnLoadDm').onclick = () => {
    if (sel.value) { location.hash = `#/dismantle?task=${sel.value}`; renderDmBody(Number(sel.value)); }
  });
  const tid = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('task'));
  if (tid && sel) renderDmBody(tid);
}

/* ---------- 视图 7：归架盘点 ---------- */
async function viewStock(params) {
  const sel = await taskSelector(['stocktaking', 'done'], params.get('task'));
  return `<div class="card"><h2>📦 归架盘点与缺件报损</h2>
    <p class="muted">逐件核对挡板 / 立柱 / 密封条是否归架、有无损坏；<b>差异件由物业主管决定报损并开补充单</b>，补充入库后器材重新可用。</p>
    <div class="row"><label class="field">盘点任务 <select id="stTask" style="min-width:240px">${sel.opts}</select></label>
    <button id="btnLoadSt" class="btn-ghost">打开</button></div></div>
  <div id="stBody"></div>
  <div class="card"><h2>🛠 缺件补充单</h2><div id="ordersBody"></div></div>`;
}
async function renderStockBody(tid) {
  const root = $('#stBody');
  const [t, orders] = await Promise.all([API.get(`/api/tasks/${tid}`), API.get('/api/orders')]);
  if (!t.detail) { root.innerHTML = ''; return; }
  const d = t.detail;
  const staff = currentStaff();
  const isSup = staff.role === 'supervisor';
  const present = t.stocktake.filter(i => i.state === 'present' && i.checked).length;
  const diffs = t.stocktake.filter(i => i.state !== 'present');

  const rows = t.stocktake.map(i => {
    const hasOrder = orders.some(o => o.equipment_id === i.equipment_id && o.status !== 'received');
    const diffCls = i.state !== 'present' ? 'diff-row' : '';
    const stateBtns = ['present', 'missing', 'damaged'].map(st => {
      const cls = st === i.state ? `on ${st === 'missing' ? 'miss' : st === 'damaged' ? 'dmg' : ''}` : '';
      const lbl = { present: '在位', missing: '缺失', damaged: '损坏' }[st];
      return `<button class="${cls}" data-item="${i.id}" data-state="${st}">${lbl}</button>`;
    }).join('');
    return `<tr class="${diffCls}">
      <td><span class="badge badge-${i.kind === 'board' ? 'blue' : ''}">${KIND_LABEL[i.kind]}</span> <b>${esc(i.code)}</b></td>
      <td>${esc(i.rack_name || '-')}</td>
      <td><span class="state-toggle">${stateBtns}</span></td>
      <td class="muted">${esc(i.note)}</td>
      <td class="diff-actions">
        ${i.state !== 'present' && !hasOrder
          ? (isSup ? `<button class="btn-sm btn-orange act-loss" data-item="${i.id}">主管报损</button>`
                   : '<span class="muted">待物业主管报损</span>')
          : i.state !== 'present' ? '<span class="badge badge-orange">补充流程中</span>' : ''}
      </td></tr>`;
  }).join('');

  root.innerHTML = `<div class="card">
    ${flowBar(d.status)}
    <div class="row"><h2 style="margin:0">${esc(d.entrance_name)}</h2><div class="spacer"></div>
      <span class="pill ${d.status === 'done' ? 'pill-green' : 'pill-orange'}">${d.status_label}</span></div>
    <div class="grid grid-3" style="margin:12px 0">
      <div class="stat blue"><div class="num">${t.stocktake.length}</div><div class="lbl">应归架器材</div></div>
      <div class="stat ${present === t.stocktake.length ? 'blue' : ''}"><div class="num">${present}</div><div class="lbl">已核在位</div></div>
      <div class="stat ${diffs.length ? 'red' : ''}"><div class="num">${diffs.length}</div><div class="lbl">差异件</div></div>
    </div>
    <table><thead><tr><th>器材</th><th>存放架</th><th>盘点结果</th><th>备注</th><th>处理</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="row" style="margin-top:14px">
      <button id="btnFinalize" class="btn-orange" ${d.status === 'stocktaking' ? '' : 'disabled'}>
        ${d.status === 'done' ? '已完成归架核对' : '完成归架核对，器材重新可用'}</button>
      <span class="muted">存在差异时须全部报损开单后才能完成；并发完成仅一次生效</span>
    </div>
  </div>`;

  root.querySelectorAll('.state-toggle button').forEach(btn => btn.onclick = async () => {
    const note = btn.dataset.state !== 'present' ? (prompt('差异说明：', btn.dataset.state === 'missing' ? '归架未找到' : '检查发现损坏') || '') : '';
    if (note === null) return;
    const r = await apiPost(`/api/stock/${btn.dataset.item}/check`,
      { staff_id: state.staffId, state: btn.dataset.state, note });
    if (r) renderStockBody(tid);
  });
  root.querySelectorAll('.act-loss').forEach(btn => btn.onclick = async () => {
    const reason = prompt('报损原因（物业主管）：');
    if (!reason) return;
    const r = await apiPost(`/api/stock/${btn.dataset.item}/loss`,
      { supervisor_id: state.staffId, reason });
    if (r) { toast('已报损并开补充采购单', 'ok'); renderStockBody(tid); }
  });
  $('#btnFinalize').onclick = async () => {
    const r = await apiPost(`/api/tasks/${tid}/finalize`, { staff_id: state.staffId });
    if (r) { toast(`归架完成：在位 ${r.total - r.differences}/${r.total}，差异 ${r.differences} 件已开单`, 'ok'); renderStockBody(tid); }
  };
  renderOrders();
}
async function renderOrders() {
  const orders = await API.get('/api/orders');
  const isSup = currentStaff().role === 'supervisor';
  const rows = orders.map(o => `<tr>
    <td><span class="badge badge-orange">${KIND_LABEL[o.kind]}</span> ${esc(o.equipment_code)}</td>
    <td>${o.status === 'received' ? '<span class="badge badge-green">已入库</span>' : '<span class="badge">已报损/采购中</span>'}</td>
    <td>${esc(o.reported_by_name || '')}</td><td class="muted">${esc(o.reason)}</td>
    <td>${o.status === 'received' ? esc(o.new_code)
      : (isSup ? `<input class="newcode" data-oid="${o.id}" placeholder="新编号 如 DB-13" style="width:120px"> <button class="btn-sm act-receive" data-oid="${o.id}">确认入库</button>` : '<span class="muted">待主管入库</span>')}</td>
  </tr>`).join('');
  $('#ordersBody').innerHTML = `<table><thead><tr><th>器材</th><th>状态</th><th>主管</th><th>原因</th><th>补充入库</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted">暂无补充单</td></tr>'}</tbody></table>`;
  $('#ordersBody').querySelectorAll('.act-receive').forEach(btn => btn.onclick = async () => {
    const code = btn.parentElement.querySelector('.newcode').value.trim().toUpperCase();
    if (!code) return toast('请填写新器材编号', 'err');
    const r = await apiPost(`/api/orders/${btn.dataset.oid}/receive`,
      { new_code: code, supervisor_id: state.staffId });
    if (r) { toast(`${code} 已入库，恢复可用`, 'ok'); renderOrders(); }
  });
}
function bindStock() {
  const sel = $('#stTask');
  $('#btnLoadSt') && ($('#btnLoadSt').onclick = () => {
    if (sel.value) { location.hash = `#/stocktake?task=${sel.value}`; renderStockBody(Number(sel.value)); }
  });
  const tid = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('task'));
  if (tid && sel) renderStockBody(tid);
  renderOrders();
}

/* ---------- 固定拍照条 ---------- */
function showPhotoBar() { $('#photobar').classList.remove('hidden'); }
function hidePhotoBar() { $('#photobar').classList.add('hidden'); }
function clearPhoto() {
  state.pendingPhoto = null; $('#photoPreview').innerHTML = '';
}
$('#photoInput').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  const res = await API.upload(file);
  if (res.path) {
    state.pendingPhoto = { path: res.path };
    $('#photoPreview').innerHTML = `<img src="/photos/${res.path}">`;
    toast('照片就绪，点击确认项时一并上传', 'ok');
  }
  e.target.value = '';
});

/* ---------- 路由 ---------- */
const routes = {
  dashboard: { title: '预警看板', render: async () => viewDashboard(await API.get('/api/dashboard')), bind: bindDashboard },
  diagram: { title: '入口示意图', render: viewDiagram, bind: () => {} },
  claim: { title: '器材领用', render: viewClaim, bind: bindClaim },
  install: { title: '分段安装', render: viewInstall, bind: bindInstall },
  water: { title: '积水观察', render: viewWater, bind: bindWater },
  dismantle: { title: '拆除清洗', render: viewDismantle, bind: bindDismantle },
  stocktake: { title: '归架盘点', render: viewStock, bind: bindStock },
};

async function render() {
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [name, qs] = hash.split('?');
  const params = new URLSearchParams(qs || '');
  const route = routes[name] || routes.dashboard;
  document.querySelectorAll('.sidenav a').forEach(a =>
    a.classList.toggle('active', a.getAttribute('href') === `#/${name}`));
  try {
    $('#view').innerHTML = await route.render(params);
    route.bind(params);
  } catch (e) {
    $('#view').innerHTML = `<div class="card"><h2>页面加载失败</h2><p class="muted">${esc(e.message)}</p></div>`;
  }
  if (!['install', 'water'].includes(name)) { hidePhotoBar(); clearPhoto(); }
  // 刷新顶栏预警药丸
  API.get('/api/dashboard').then(d => {
    const pill = document.getElementById('roundPill');
    if (pill) {
      const wrap = document.createElement('span');
      wrap.innerHTML = roundPill(d.round, d.round_label);
      const fresh = wrap.firstElementChild;
      fresh.id = 'roundPill';
      pill.replaceWith(fresh);
    }
  });
}

window.addEventListener('hashchange', render);
(async function init() {
  await loadStaff();
  render();
})();
