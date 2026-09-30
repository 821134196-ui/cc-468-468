/* 防汛挡板前端：七视图单页应用，原生 JS，无依赖。 */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `请求失败 ${r.status}`);
  return d;
};
const upload = async (path, fields, file) => {
  const fd = new FormData();
  Object.entries(fields).forEach(([k, v]) => v != null && v !== "" && fd.append(k, v));
  if (file) fd.append("photo", file);
  const r = await fetch(path, { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `请求失败 ${r.status}`);
  return d;
};

const LEVEL_NAME = { NONE: "无预警", BLUE: "蓝警 IV", YELLOW: "黄警 III", ORANGE: "橙警 II", RED: "红警 I" };
const TASK_NAME = {
  generated: "清单已生成", picking: "领用中", installing: "分段安装", installed: "已封控",
  cleaning: "拆除待清洗", drying: "清洗晾干中", racking: "归架核对中", closed: "已结案",
};
const FLOW = ["generated", "picking", "installing", "installed", "cleaning", "drying", "racking", "closed"];
let state = { people: [], actor: null, view: "dashboard",
  entId: null, taskId: null, slot: 1, photoFile: null, dash: null };

function toast(msg, ok = true) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + (ok ? "ok" : "err");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 2600);
}
const actorId = () => parseInt($("#actor").value || state.actor || "1", 10);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const tag = (txt, cls = "gray") => `<span class="tag tag-${cls}">${esc(txt)}</span>`;

function taskStatusTag(s) {
  const map = { generated: "gray", picking: "blue", installing: "orange", installed: "green",
    cleaning: "blue", drying: "blue", racking: "orange", closed: "gray" };
  return tag(TASK_NAME[s] || s, map[s] || "gray");
}

// ----------------------------------------------------------------- 人员选择
async function loadPeople() {
  state.people = await api("/api/people");
  $("#actor").innerHTML = state.people.map(p =>
    `<option value="${p.id}">${esc(p.name)}（${{ supervisor: "主管", installer: "安装工", guard: "保安", observer: "巡查" }[p.role]}）</option>`).join("");
  $("#actor").onchange = () => { state.actor = actorId(); render(); };
}

// ----------------------------------------------------------------- 雨量趋势
function rainChart(rains) {
  const W = 640, H = 150, pad = { l: 34, r: 12, t: 14, b: 20 };
  if (!rains.length) return `<div class="muted">暂无雨量数据</div>`;
  const mm = rains.map(r => r.mm);
  const max = Math.max(55, ...mm) * 1.08;
  const x = i => pad.l + (rains.length === 1 ? 0 : i * (W - pad.l - pad.r) / (rains.length - 1));
  const y = v => H - pad.b - v * (H - pad.t - pad.b) / max;
  const pts = rains.map((r, i) => `${x(i).toFixed(1)},${y(r.mm).toFixed(1)}`).join(" ");
  const area = `${pad.l},${H - pad.b} ${pts} ${x(rains.length - 1)},${H - pad.b}`;
  let grid = "";
  [0, 20, 50].forEach(v => {
    if (v <= max) grid += `<line class="grid-line" x1="${pad.l}" y1="${y(v)}" x2="${W - pad.r}" y2="${y(v)}"/>
      <text x="4" y="${y(v) + 3}">${v}</text>`;
  });
  const th = `<line class="threshold" x1="${pad.l}" y1="${y(20)}" x2="${W - pad.r}" y2="${y(20)}"/>
    <text x="${W - pad.r - 96}" y="${y(20) - 3}">部署阈值 20mm</text>`;
  const dots = rains.map((r, i) =>
    `<circle class="pt" cx="${x(i)}" cy="${y(r.mm)}" r="2.4"><title>${esc(r.ts)} ${r.mm}mm/h</title></circle>`).join("");
  const last = rains[rains.length - 1];
  const lbl = `<text x="${Math.min(x(rains.length - 1) - 4, W - 46)}" y="${y(last.mm) - 6}">${last.mm}mm</text>`;
  return `<svg class="rain-box" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${grid}${th}
    <line class="axis" x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${H - pad.b}"/>
    <polygon class="area" points="${area}"/><polyline class="line" points="${pts}"/>
    ${dots}${lbl}
    <text x="${pad.l}" y="${H - 4}">雨量趋势（mm/h，简洁线形）</text></svg>`;
}

// ============================================================= 视图 1 看板
async function viewDashboard() {
  const d = state.dash = await api("/api/dashboard");
  const w = d.weather;
  const active = d.entrances.filter(e => e.task_id);
  const installed = d.entrances.filter(e => e.task_status === "installed").length;
  const openInc = d.incidents.length;
  $("#alert-pill").textContent = LEVEL_NAME[w.level] || w.level;
  $("#alert-pill").className = "pill pill-" + w.level;
  $("#view").innerHTML = `
   <div class="grid grid-3">
     <div class="stat"><div class="num">${w.rainfall ?? 0}<span style="font-size:14px"> mm/h</span></div><div class="lab">当前雨量</div></div>
     <div class="stat ${installed ? "warn" : ""}"><div class="num">${installed}</div><div class="lab">已封控入口</div></div>
     <div class="stat ${openInc ? "warn" : ""}"><div class="num">${openInc}</div><div class="lab">待处理异常</div></div>
   </div>

   <div class="card">
     <h2>雨量趋势与预警</h2>
     ${rainChart(d.rainfall)}
     <div class="weather-ctl" style="margin-top:8px">
       <input id="w-mm" type="number" placeholder="注入雨量 mm/h" min="0" max="120">
       <button class="btn" id="w-set">注入并复查</button>
       <button class="btn-ghost btn" id="w-tick">推进一个节拍</button>
       <button class="btn-gray btn" id="w-auto">恢复自动脚本</button>
       <span class="kv">阈值：≥8蓝 / ≥20黄(部署) / ≥30橙 / ≥50红；连续2拍低于20解除</span>
     </div>
   </div>

   <div class="card">
     <h2>车库入口（${active.length}/${d.entrances.length} 有任务）</h2>
     <table><thead><tr><th>入口</th><th>任务状态</th><th>预警</th><th>入口积水</th><th></th></tr></thead><tbody>
     ${d.entrances.map(e => `<tr class="clickable" data-ent="${e.id}">
       <td><b>${esc(e.name)}</b><div class="kv">${esc(e.code)}</div></td>
       <td>${e.task_id ? `#${e.task_id} ${taskStatusTag(e.task_status)}` : tag("待命", "gray")}</td>
       <td>${e.warning_cleared ? tag("已解除", "green") : tag("生效中", "orange")}</td>
       <td style="${e.water_cm >= 10 ? "color:var(--orange);font-weight:700" : ""}">${e.water_cm ?? 0} cm</td>
       <td>${e.task_id ? `<button class="btn small" data-task="${e.task_id}">进入任务</button>` : `<button class="btn-ghost btn small" data-entbtn="${e.id}">示意图</button>`}</td>
     </tr>`).join("")}
     </tbody></table>
   </div>

   <div class="card orange">
     <h2>待处理异常（缺件/破损/迟到/积水/放行/提前拆除）</h2>
     ${d.incidents.length ? `<table><tbody>${d.incidents.map(i => `<tr>
       <td>${tag({ wrong_segment: "错段", seal_damage: "密封条", late: "迟到", water: "积水",
         water_block: "积水阻拆", early_removal: "提前拆除", passage_open: "临时放行",
         shortage: "缺件", rack_missing: "归架缺件", rack_damaged: "归架破损" }[i.type] || i.type,
         i.type === "water" || i.type === "early_removal" ? "orange" : "red")}</td>
       <td>${esc(i.entrance_name || "")}</td><td>${esc(i.detail)}</td>
       <td class="kv">${esc(i.created_at)}</td></tr>`).join("")}</tbody></table>`
       : `<div class="muted">无待处理异常</div>`}
   </div>`;

  $("#w-set").onclick = async () => {
    const v = $("#w-mm").value;
    if (v === "") return toast("请输入雨量", false);
    const r = await api("/api/weather/set", { mm: parseFloat(v) });
    toast(`雨量 ${r.mm}mm/h → ${LEVEL_NAME[r.level]}`);
    viewDashboard();
  };
  $("#w-tick").onclick = async () => {
    const r = await api("/api/weather/tick", {});
    toast(`节拍推进：${r.mm}mm/h ${LEVEL_NAME[r.level]}`);
    viewDashboard();
  };
  $("#w-auto").onclick = () => api("/api/weather/auto", {}).then(() => toast("已恢复自动降雨脚本")).then(viewDashboard);
  $$("tr.clickable").forEach(tr => tr.onclick = () => { state.entId = +tr.dataset.ent; switchView("schematic"); });
  $$("[data-task]").forEach(b => b.onclick = e => { e.stopPropagation(); state.taskId = +b.dataset.task; switchView("install"); });
  $$("[data-entbtn]").forEach(b => b.onclick = e => { e.stopPropagation(); state.entId = +b.dataset.entbtn; switchView("schematic"); });
}

// ========================================================= 视图 2 入口示意图
async function viewSchematic() {
  const d = state.dash || await api("/api/dashboard");
  if (!state.entId) state.entId = d.entrances[0]?.id;
  const opts = d.entrances.map(e => `<option value="${e.id}" ${e.id === state.entId ? "selected" : ""}>${esc(e.name)}</option>`).join("");
  $("#view").innerHTML = `
   <div class="card">
     <h2>入口挡板拼接示意图</h2>
     <div class="row"><label class="kv">入口 <select id="ent-sel" style="width:auto">${opts}</select></label>
       <span class="kv">按挡板实际拼接顺序横向排列；缺段位置保留明显空槽</span></div>
     <div id="schema" class="schematic-wrap"></div>
     <div class="legend">
       <span><i style="background:var(--blue)"></i>已安装/锁定挡板</span>
       <span><i style="background:var(--orange-soft);border:1px dashed var(--orange)"></i>缺段空槽</span>
       <span><i style="background:#494f57"></i>立柱（深色=待装）</span>
       <span>圆点：连接 / 密封 / 封控确认</span>
     </div>
   </div>
   <div id="detail"></div>`;
  $("#ent-sel").onchange = e => { state.entId = +e.target.value; renderSchema(); };
  await renderSchema();
}

async function renderSchema() {
  const data = await api(`/api/entrance?id=${state.entId}`);
  const t = data.task;
  const slotMap = {};
  (t?.slots || []).forEach(s => (slotMap[s.slot_no] = s));
  const segLine = {};
  (t?.lines || []).forEach(l => { if (l.line_type === "segment") segLine[l.slot_no] = l; });
  let html = `<div class="schematic"><div class="post ${t ? "locked" : ""}" title="左端柱"></div>`;
  data.slots_spec.forEach((w, i) => {
    const no = i + 1;
    const s = slotMap[no], l = segLine[no];
    const installed = l?.item_id && t.status !== "generated";
    const missing = t && !l?.item_id;
    const checks = s ? ["conn_at", "seal_at", "ctrl_at"].map(k =>
      `<span class="chk ${s[k] ? "done" : ""}">✓</span>`).join("") : "";
    const opened = s?.opened ? " opened" : "";
    const damaged = s && s.seal_ok === 0 ? " damaged" : "";
    if (missing) {
      html += `<div class="seg missing" style="height:${70 + w * 18}px" title="设计宽 ${w}m"></div>`;
    } else {
      html += `<div class="seg${installed ? "" : ""}${opened}${damaged}" style="height:${70 + w * 18}px">
        ${checks ? `<div class="checks">${checks}</div>` : ""}
        <div class="sl">${no}号槽 ${w}m</div>
        <div class="cd">${esc(l?.item_code || "待领件")}</div>
      </div>`;
    }
    html += `<div class="post ${t ? "locked" : ""}" title="${no}号间柱"></div>`;
  });
  html += `</div><div class="ground"></div>`;
  $("#schema").innerHTML = html;

  let det = "";
  if (t) {
    det = `<div class="card"><h2>${esc(data.entrance.name)} · 任务 #${t.id} ${taskStatusTag(t.status)}</h2>
      <div class="flow">${FLOW.map(s => `<span class="step ${t.status === s ? "on" : ""}">${TASK_NAME[s]}</span>${s === "closed" ? "" : "→"}`).join("")}</div>
      <div class="hr"></div>
      <div class="kv">触发预警：<b>${LEVEL_NAME[t.level_triggered]}</b> ｜ 预警状态：<b>${t.warning_cleared ? "已解除可拆除" : "生效中"}</b></div>
      <div class="dialog-btns">
        <button class="btn" id="go-pick">器材领用</button>
        <button class="btn btn-orange" id="go-install">分段安装/拆除</button>
      </div></div>`;
  } else {
    det = `<div class="card muted">该入口暂无进行中任务；达到黄警阈值后调度自动生成部署清单。</div>`;
  }
  $("#detail").innerHTML = det;
  if (t) {
    state.taskId = t.id;
    $("#go-pick").onclick = () => switchView("pickup");
    $("#go-install").onclick = () => switchView("install");
  }
}

// ============================================================= 视图 3 领用
async function viewPickup() {
  const d = state.dash || await api("/api/dashboard");
  const tasks = d.entrances.filter(e => e.task_id && e.task_status !== "closed");
  if (!state.taskId || !tasks.find(t => t.task_id === state.taskId)) state.taskId = tasks[0]?.task_id;
  $("#view").innerHTML = `
   <div class="card">
     <h2>器材领用（扫码锁定，同件器材跨入口排他）</h2>
     <div class="row"><label class="kv">任务 <select id="task-sel" style="width:auto">
       ${tasks.map(t => `<option value="${t.task_id}" ${t.task_id === state.taskId ? "selected" : ""}>#${t.task_id} ${esc(t.name)} · ${TASK_NAME[t.task_status]}</option>`).join("")}
     </select></label></div>
   </div>
   <div id="pick-body"></div>
   <div class="card orange"><h2>缺件补充入库（主管/安装工）</h2>
     <div class="row">
       <select id="rp-kind" style="width:auto"><option value="segment">挡板</option><option value="seal">密封条</option><option value="post">立柱</option></select>
       <input id="rp-width" type="number" step="0.1" placeholder="挡板宽度m（如2）" style="width:140px">
       <input id="rp-qty" type="number" value="1" min="1" style="width:90px">
       <button class="btn btn-orange" id="rp-btn">补充入库</button>
     </div></div>`;
  $("#task-sel").onchange = e => { state.taskId = +e.target.value; renderPick(); };
  $("#rp-btn").onclick = async () => {
    try {
      const r = await api("/api/replenish", { kind: $("#rp-kind").value,
        width: parseFloat($("#rp-width").value) || null, qty: +$("#rp-qty").value || 1,
        person_id: actorId() });
      toast(`已入库：${r.codes.join("、")}（上存放架后立即可用）`);
      renderPick();
    } catch (e) { toast(e.message, false); }
  };
  if (state.taskId) await renderPick();
  else $("#pick-body").innerHTML = `<div class="card muted">暂无可领用任务</div>`;
}

async function renderPick() {
  const t = await api(`/api/task?id=${state.taskId}`);
  const canPick = ["generated", "picking", "installing"].includes(t.status);
  const pending = t.lines.filter(l => l.status === "pending");
  $("#pick-body").innerHTML = `
   <div class="card">
     <h2>#${t.id} ${esc(t.entrance.name)} ${taskStatusTag(t.status)}</h2>
     <div class="row" style="margin-bottom:8px">
       <button class="btn" id="pick-auto" ${canPick ? "" : "disabled"}>一键按规格领齐可用件</button>
       <button class="btn btn-orange" id="pick-done" ${["picking", "generated"].includes(t.status) ? "" : "disabled"}>齐套，进入安装</button>
       <span class="kv">操作人取当前下拉选择；扫码领错宽度将按"挡板错段"拒绝并记录</span>
     </div>
     <table><thead><tr><th>清单项</th><th>类型</th><th>状态</th><th>锁定器材</th><th>存放位</th><th>扫码领件</th></tr></thead><tbody>
     ${t.lines.map(l => `<tr>
       <td>${esc(l.label)}</td>
       <td>${tag({ segment: "挡板", post: "立柱", seal: "密封条" }[l.line_type], "blue")}</td>
       <td>${tag({ pending: "待领", locked: "已锁定", returned: "已归架" }[l.status], l.status === "pending" ? "orange" : "green")}</td>
       <td><b>${esc(l.item_code || "—")}</b></td>
       <td class="kv">${l.rack_code ? esc(l.rack_code + " " + (l.rack_pos || "")) : "—"}</td>
       <td>${l.status === "pending" && canPick ? `<div class="row"><input data-line="${l.id}" class="code-in" placeholder="扫/输器材条码" style="width:130px">
         <button class="btn small" data-pick="${l.id}">领</button></div>` : ""}</td>
     </tr>`).join("")}
     </tbody></table>
     ${pending.length ? `<div class="kv" style="margin-top:8px;color:var(--orange)">尚有 ${pending.length} 件待领（库存不足会列入缺件异常，补充入库后再领）</div>` : ""}
   </div>`;
  $$("[data-pick]").forEach(b => b.onclick = async () => {
    const inp = $(`.code-in[data-line="${b.dataset.pick}"]`);
    const code = inp.value.trim();
    if (!code) return toast("请扫描或输入器材条码", false);
    try {
      await api("/api/pick", { task_id: t.id, line_id: +b.dataset.pick, code, person_id: actorId() });
      toast(`已锁定 ${code}`); renderPick();
    } catch (e) { toast(e.message, false); }
  });
  $("#pick-auto").onclick = async () => {
    const r = await api("/api/pick-auto", { task_id: t.id, person_id: actorId() });
    toast(r.shortage.length ? `领 ${r.picked.length} 件；缺件：${r.shortage.join("、")}` : `已领齐 ${r.picked.length} 件`,
      !r.shortage.length);
    renderPick();
  };
  $("#pick-done").onclick = async () => {
    try { await api("/api/finish-picking", { task_id: t.id, person_id: actorId() }); toast("齐套，进入分段安装"); switchView("install"); }
    catch (e) { toast(e.message, false); }
  };
}

// ============================================================= 视图 4 安装
async function viewInstall() {
  const d = state.dash || await api("/api/dashboard");
  const tasks = d.entrances.filter(e => e.task_id && e.task_status !== "closed");
  if (!state.taskId || !tasks.find(t => t.task_id === state.taskId)) state.taskId = tasks[0]?.task_id;
  $("#view").innerHTML = `
   <div class="card"><div class="row"><label class="kv">任务 <select id="task-sel" style="width:auto">
     ${tasks.map(t => `<option value="${t.task_id}" ${t.task_id === state.taskId ? "selected" : ""}>#${t.task_id} ${esc(t.name)} · ${TASK_NAME[t.task_status]}</option>`).join("")}
   </select></label><span id="t-flow"></span></div></div>
   <div id="install-body"></div>`;
  $("#task-sel").onchange = e => { state.taskId = +e.target.value; renderInstall(); };
  if (state.taskId) await renderInstall();
  else $("#install-body").innerHTML = `<div class="card muted">暂无进行中任务</div>`;
}

async function renderInstall() {
  const t = await api(`/api/task?id=${state.taskId}`);
  state.slot = state.slot || 1;
  const photoOf = (s, k) => {
    const p = t.photos.find(x => x.slot_no === s.slot_no && x.kind === k);
    return p ? `<div class="thumb"><img src="/photos/${esc(p.path)}"></div>` : "";
  };
  const people = id => state.people.find(p => p.id === id)?.name || "—";
  $("#t-flow").innerHTML = `<span class="flow">${FLOW.map(s =>
    `<span class="step ${t.status === s ? "on" : ""}">${TASK_NAME[s]}</span>`).join("→")}</span>`;

  let assign = `<div class="card"><h2>现场人员与签到</h2><table><tbody>
    ${t.assignments.map(a => `<tr><td>${esc(a.person_name)}（${{ supervisor: "主管", installer: "安装工", guard: "保安", observer: "巡查" }[a.role]}）</td>
      <td>${esc(a.duty)}</td><td class="kv">应到 ${esc(a.due_at || "—")}</td>
      <td>${a.arrived_at ? tag("已签到" + (a.late ? "·迟到" : ""), a.late ? "orange" : "green") : tag("未到", "gray")}</td>
      <td>${a.arrived_at ? "" : `<button class="btn small" data-sign="${a.id}" data-me="${a.person_id}">本人签到</button>`}</td></tr>`).join("")}
    </tbody></table></div>`;

  let slots = "";
  if (t.status === "installing" || t.status === "installed") {
    slots = t.slots.map(s => `
      <div class="slot-card">
        <div class="slot-head"><span>${s.slot_no}号槽（设计 ${s.expected_width}m）${s.seg_code ? " · " + esc(s.seg_code) : ""}</span>
          <span>${s.opened ? tag("临时放行中", "orange") : s.seal_ok ? tag("密封条完好", "green") : tag("密封条破损", "red")}</span></div>
        <div class="check-grid">
          ${bigCheck(s, "conn", "连接确认", "安装工 · 段间/柱咬合", s.conn_at, people(s.conn_by), photoOf(s, "conn"))}
          ${bigCheck(s, "seal", "密封确认", "安装工 · 底部密封条", s.seal_at, people(s.seal_by), photoOf(s, "seal"))}
          ${bigCheck(s, "ctrl", "通道封控", "保安 · 禁止人车进入", s.ctrl_at, people(s.ctrl_by), photoOf(s, "ctrl"))}
        </div>
        <div class="dock-photo">
          <label class="small">固定拍照入口
            <input type="file" accept="image/*" capture="environment" data-photo="${s.slot_no}">
          </label>
          <button class="btn-gray btn small" data-damage="${s.slot_no}" ${s.seal_ok ? "" : "disabled"}>密封条破损登记</button>
          <button class="btn-gray btn small" data-replaced="${s.slot_no}" ${s.seal_ok ? "disabled" : ""}>更换完成验收</button>
          <button class="btn-gray btn small" data-open="${s.slot_no}" ${t.status === "installed" ? "" : "disabled"}>临时放行</button>
        </div>
      </div>`).join("");
  }

  const openPg = t.passages.filter(p => !p.restored_at);
  $("#install-body").innerHTML = `
   <div class="card orange">
     <h2>逐段确认（连接 → 密封 → 封控）</h2>
     <div class="kv">移动端：大勾选区操作；每个勾选随下方"固定拍照入口"上传的照片一同提交。</div>
   </div>
   ${slots || `<div class="card muted">${t.status === "picking" ? "尚未进入安装阶段" : "当前阶段无需分段确认"}</div>`}
   <div class="card">
     <div class="row end">
       ${openPg.length ? `<button class="btn btn-orange" id="restore-pg">复位放行通道（保安，${openPg.length}处）</button>` : ""}
       <button class="btn btn-orange" id="complete" ${t.status === "installing" ? "" : "disabled"}>安装完成 · 整体封控</button>
     </div>
     <div class="kv" style="margin-top:6px">须每段连接/密封/封控全部勾选、无开启通道、无破损未处理；并发完成只生效一次。</div>
   </div>
   ${assign}`;

  $$("[data-photo]").forEach(i => i.onchange = e => {
    state.photoFile = e.target.files[0];
    state.slot = +i.dataset.photo;
    toast(`已选取照片，将随 ${i.dataset.photo} 号槽下一次确认上传`);
  });
  $$("[data-ck]").forEach(b => b.onclick = async () => {
    const slot = +b.dataset.ck, kind = b.dataset.kind;
    try {
      const f = state.slot === slot ? state.photoFile : null;
      await upload("/api/confirm", { task_id: t.id, slot, kind, person_id: actorId() }, f);
      if (f) { state.photoFile = null; }
      toast(`${slot}号槽 ${ {conn:"连接",seal:"密封",ctrl:"封控"}[kind] }已确认`);
      renderInstall();
    } catch (e) { toast(e.message, false); }
  });
  $$("[data-damage]").forEach(b => b.onclick = async () => {
    const note = prompt("密封条破损情况说明：", "底部密封胶条撕裂");
    if (note === null) return;
    try { const r = await api("/api/seal-damage", { task_id: t.id, slot: +b.dataset.damage, person_id: actorId(), note });
      toast(`已下架破损条并换备用 ${r.spare}，请重新密封确认`); renderInstall();
    } catch (e) { toast(e.message, false); }
  });
  $$("[data-replaced]").forEach(b => b.onclick = async () => {
    try { await api("/api/seal-replaced", { task_id: t.id, slot: +b.dataset.replaced, person_id: actorId() });
      toast("更换验收通过，可重新密封确认"); renderInstall();
    } catch (e) { toast(e.message, false); }
  });
  $$("[data-open]").forEach(b => b.onclick = async () => {
    const plate = prompt("临时放行车辆车牌：", "沪A·00000");
    if (plate === null) return;
    try { await api("/api/passage-open", { task_id: t.id, slot: +b.dataset.open, person_id: actorId(), plate });
      toast("通道已开启，复位前连接/密封/封控全部失效"); renderInstall();
    } catch (e) { toast(e.message, false); }
  });
  const rp = $("#restore-pg");
  if (rp) rp.onclick = async () => {
    try { for (const p of openPg) await api("/api/passage-restore", { passage_id: p.id, person_id: actorId() });
      toast("通道已复位，请重新逐段确认"); renderInstall();
    } catch (e) { toast(e.message, false); }
  };
  $("#complete").onclick = async () => {
    try { await api("/api/complete", { task_id: t.id, person_id: actorId() }); toast("安装完成，入口封控"); renderInstall(); }
    catch (e) { toast(e.message, false); }
  };
  $$("[data-sign]").forEach(b => b.onclick = async () => {
    try { const r = await api("/api/signin", { assignment_id: +b.dataset.sign, person_id: +b.dataset.me });
      toast(r.late ? "签到成功，已登记迟到" : "签到成功"); renderInstall();
    } catch (e) { toast(e.message, false); }
  });
}

function bigCheck(s, kind, title, desc, at, by, photoHtml) {
  return `<button class="bigcheck ${at ? "done" : ""}" data-ck="${s.slot_no}" data-kind="${kind}">
    <span class="box">✓</span><span class="t">${title}</span>
    <span class="d">${desc}${at ? " · " + esc(by) : ""}</span>${photoHtml}</button>`;
}

// ============================================================= 视图 5 积水
async function viewWater() {
  const d = state.dash || await api("/api/dashboard");
  const opts = d.entrances.map(e => `<option value="${e.id}">${esc(e.name)}（当前 ${e.water_cm ?? 0}cm）</option>`).join("");
  $("#view").innerHTML = `
   <div class="card">
     <h2>入口积水观察</h2>
     <div class="row">
       <select id="w-ent" style="width:auto">${opts}</select>
       <input id="w-cm" type="number" step="0.5" placeholder="水位 cm" style="width:120px">
       <input id="w-note" placeholder="备注（排水/位置）" style="flex:1;min-width:140px">
       <button class="btn" id="w-add">记录观察</button>
     </div>
     <div class="kv" style="margin-top:8px">≥10cm 记入异常；≥15cm 禁止拆除挡板。监测设备数据由调度自动写入。</div>
   </div>
   <div class="card"><h2>各入口最近观察</h2><div id="w-list"></div></div>`;
  $("#w-add").onclick = async () => {
    const cm = parseFloat($("#w-cm").value);
    if (isNaN(cm)) return toast("请输入水位", false);
    try {
      const r = await api("/api/water", { entrance_id: +$("#w-ent").value, cm,
        person_id: actorId(), note: $("#w-note").value });
      toast(r.blocked ? `已记录 ${cm}cm：达到禁拆线` : `已记录 ${cm}cm`, !r.blocked);
      renderWaterList();
    } catch (e) { toast(e.message, false); }
  };
  renderWaterList();
}
async function renderWaterList() {
  const d = await api("/api/dashboard");
  state.dash = d;
  $("#w-list").innerHTML = (await Promise.all(d.entrances.map(async e => {
    const det = await api(`/api/entrance?id=${e.id}`);
    const ws = det.task?.waters || [];
    const all = await (async () => {
      // 用任务最近观察 + 看板水位展示即可
      return ws.slice(0, 5);
    })();
    return `<h3 style="margin:10px 0 4px">${esc(e.name)}</h3>
      ${all.length ? `<table><tbody>${all.map(w => `<tr><td style="width:90px"><b style="${w.level_cm>=10?'color:var(--orange)':''}">${w.level_cm} cm</b></td>
        <td class="kv">${esc(w.source)} · ${esc(w.observer||"")}</td><td class="kv">${esc(w.ts)}</td><td>${esc(w.note)}</td></tr>`).join("")}</tbody></table>`
        : `<div class="kv">暂无人工观察（当前监测 ${e.water_cm ?? 0}cm）</div>`}`;
  }))).join("");
}

// ====================================================== 视图 6 拆除与清洗
async function viewRemoval() {
  const d = state.dash || await api("/api/dashboard");
  const tasks = d.entrances.filter(e => e.task_id && ["installed", "cleaning", "drying", "racking"].includes(e.task_status));
  if (!state.taskId || !tasks.find(t => t.task_id === state.taskId)) state.taskId = tasks[0]?.task_id;
  $("#view").innerHTML = `
   <div class="card"><div class="row"><label class="kv">任务 <select id="task-sel" style="width:auto">
     ${tasks.map(t => `<option value="${t.task_id}" ${t.task_id === state.taskId ? "selected" : ""}>#${t.task_id} ${esc(t.name)} · ${TASK_NAME[t.task_status]}</option>`).join("")}
   </select></label></div></div>
   <div id="rem-body"></div>
   <div class="card orange"><h2>提前拆除审批（物业主管）</h2><div id="approvals"></div></div>`;
  $("#task-sel").onchange = e => { state.taskId = +e.target.value; renderRemoval(); };
  await renderRemoval();
  renderApprovals();
}

async function renderRemoval() {
  if (!state.taskId) { $("#rem-body").innerHTML = `<div class="card muted">暂无可拆除/清洗任务</div>`; return; }
  const t = await api(`/api/task?id=${state.taskId}`);
  const photoBtn = (apiPath, label, kind) => `
    <div class="row"><input type="file" accept="image/*" data-file="${kind}" style="flex:1">
      <button class="btn" data-stage="${apiPath}" data-kind="${kind}" ${kind === "dry" && t.status !== "drying" ? "disabled" : ""}
        ${kind === "clean" && t.status !== "cleaning" ? "disabled" : ""}>${label}</button></div>`;
  $("#rem-body").innerHTML = `
   <div class="card">
     <h2>#${t.id} ${esc(t.entrance.name)} ${taskStatusTag(t.status)}</h2>
     <div class="kv">预警：<b>${t.warning_cleared ? "已解除（可正常拆除）" : "未解除（拆除需主管批准）"}</b>
       ｜入口最新积水：<b id="rm-water">—</b></div>
     <div class="hr"></div>
     <div class="row">
       <button class="btn btn-orange" id="rm-req" ${t.status === "installed" ? "" : "disabled"}>申请提前拆除</button>
       <button class="btn" id="rm-do" ${t.status === "installed" ? "" : "disabled"}>执行拆除</button>
       <span class="kv">积水≥15cm 一律禁止拆除；未解除预警无批准则拒绝</span>
     </div>
     ${t.pending_approval ? `<div class="kv" style="margin-top:8px;color:var(--orange)">待审批：${esc(t.pending_approval.reason)}</div>` : ""}
   </div>
   <div class="card">
     <h2>拆除后处理：清洗 → 晾干 → 归架</h2>
     <div class="grid">
       <div><div class="kv" style="margin-bottom:4px">① 清洗挡板并拍照</div>${photoBtn("/api/clean", "确认清洗完成", "clean")}</div>
       <div><div class="kv" style="margin-bottom:4px">② 晾干后拍照</div>${photoBtn("/api/dry", "确认晾干，开始归架核对", "dry")}</div>
     </div>
     <div class="kv" style="margin-top:8px">拆除：${t.removed_at || "—"} ｜ 清洗：${t.cleaned_at || "—"} ｜ 晾干：${t.dried_at || "—"}</div>
   </div>`;
  const wdet = await api(`/api/entrance?id=${t.entrance.id}`);
  const water = wdet.task?.waters?.[0]?.level_cm ?? 0;
  $("#rm-water").textContent = water + " cm";
  $("#rm-water").style.color = water >= 15 ? "var(--red)" : water >= 10 ? "var(--orange)" : "";
  $("#rm-req").onclick = async () => {
    const reason = prompt("提前拆除原因：", "业主紧急出库");
    if (!reason) return;
    try { const r = await api("/api/removal-request", { task_id: t.id, person_id: actorId(), reason });
      toast(r.need_approval ? "已提交，等待物业主管审批" : "预警已解除，可直接拆除"); renderRemoval();
    } catch (e) { toast(e.message, false); }
  };
  $("#rm-do").onclick = async () => {
    try { await upload("/api/remove", { task_id: t.id, person_id: actorId() }, null);
      toast("已拆除，进入清洗"); renderRemoval();
    } catch (e) { toast(e.message, false); }
  };
  $$("[data-stage]").forEach(b => b.onclick = async () => {
    const inp = $(`[data-file="${b.dataset.kind}"]`);
    const f = inp.files[0];
    try { await upload(b.dataset.stage, { task_id: t.id, person_id: actorId() }, f);
      toast(b.dataset.kind === "clean" ? "清洗完成，进入晾干" : "晾干完成，开始归架核对");
      renderRemoval();
    } catch (e) { toast(e.message, false); }
  });
}

async function renderApprovals() {
  const inc = await api("/api/incidents");
  // 待审批来自各任务 pending_approval
  const d = await api("/api/dashboard");
  const pendings = [];
  for (const e of d.entrances.filter(x => x.task_status === "installed")) {
    const t = await api(`/api/task?id=${e.task_id}`);
    if (t.pending_approval) pendings.push({ e, t, ap: t.pending_approval });
  }
  $("#approvals").innerHTML = pendings.length ? `<table><thead><tr><th>入口</th><th>申请原因</th><th>时间</th><th>审批</th></tr></thead><tbody>
    ${pendings.map(p => `<tr><td>${esc(p.e.name)}</td><td>${esc(p.ap.reason)}</td><td class="kv">${esc(p.ap.created_at)}</td>
      <td class="row"><button class="btn btn-orange small" data-ap="${p.ap.id}" data-v="1">主管批准</button>
      <button class="btn-gray btn small" data-ap="${p.ap.id}" data-v="0">驳回</button></td></tr>`).join("")}
    </tbody></table>` : `<div class="muted">无待审批申请</div>`;
  $$("[data-ap]").forEach(b => b.onclick = async () => {
    const note = prompt(b.dataset.v === "1" ? "批准意见（可空）：" : "驳回原因：", "");
    if (note === null) return;
    try { await api("/api/removal-decide", { approval_id: +b.dataset.ap, person_id: actorId(),
      approve: b.dataset.v === "1", note });
      toast("已提交审批结果"); renderApprovals(); renderRemoval();
    } catch (e) { toast(e.message, false); }
  });
}

// ============================================================= 视图 7 归架
async function viewRack() {
  const d = state.dash || await api("/api/dashboard");
  const tasks = d.entrances.filter(e => e.task_id && !["closed"].includes(e.task_status));
  if (!state.taskId || !tasks.find(t => t.task_id === state.taskId)) state.taskId = tasks.find(t => t.task_status === "racking")?.task_id || tasks[0]?.task_id;
  $("#view").innerHTML = `
   <div class="card"><div class="row"><label class="kv">任务 <select id="task-sel" style="width:auto">
     ${tasks.map(t => `<option value="${t.task_id}" ${t.task_id === state.taskId ? "selected" : ""}>#${t.task_id} ${esc(t.name)} · ${TASK_NAME[t.task_status]}</option>`).join("")}
   </select></label><button class="btn" id="rk-start">建立归架盘点单</button></div></div>
   <div id="rk-body"></div>`;
  $("#task-sel").onchange = e => { state.taskId = +e.target.value; renderRack(); };
  $("#rk-start").onclick = async () => {
    try { const r = await api("/api/rack-start", { task_id: state.taskId, person_id: actorId() });
      toast(`盘点单已建立，应归 ${r.expected} 件`); renderRack();
    } catch (e) { toast(e.message, false); }
  };
  await renderRack();
}

async function renderRack() {
  if (!state.taskId) { $("#rk-body").innerHTML = `<div class="card muted">暂无任务</div>`; return; }
  const t = await api(`/api/task?id=${state.taskId}`);
  const rc = t.rack_check;
  let body = "";
  if (rc && !rc.finished_at) {
    const done = rc.scanned_codes.length, exp = rc.expected_codes.length;
    body = `<div class="card"><h2>归架扫码盘点（${done}/${exp}）</h2>
      <div class="row"><input id="rk-code" placeholder="扫/输器材条码" style="max-width:260px">
        <button class="btn" id="rk-scan">扫码归架</button>
        <button class="btn btn-orange" id="rk-finish">核对完成</button></div>
      <div class="grid grid-2" style="margin-top:10px">
        <div><div class="kv">应归清单</div><ul class="tight">${rc.expected_codes.map(c =>
          `<li style="${rc.scanned_codes.includes(c) ? "color:var(--green)" : "color:var(--orange)"}">${esc(c)} ${rc.scanned_codes.includes(c) ? "✓" : "未扫"}</li>`).join("")}</ul></div>
        <div><div class="kv">已扫码</div><ul class="tight">${rc.scanned_codes.map(c => `<li>${esc(c)} ✓</li>`).join("") || "<li class='muted'>无</li>"}</ul></div>
      </div></div>`;
  } else if (rc && rc.finished_at) {
    body = `<div class="card"><h2>盘点已结束</h2><div class="muted">见下方缺件/破损处理；全部处理后任务自动结案、器材重新可用。</div></div>`;
  } else {
    body = `<div class="card muted">${t.status === "racking" ? "请先建立盘点单" : "需完成拆除→清洗→晾干后进入归架"}</div>`;
  }
  const losses = (await api(`/api/losses?task_id=${t.id}`).catch(() => []));
  const lossHtml = losses.length ? `<div class="card orange"><h2>缺件/破损报损与补件（物业主管）</h2>
    <table><thead><tr><th>器材</th><th>类型</th><th>状态</th><th></th></tr></thead><tbody>
    ${losses.map(i => `<tr><td><b>${esc(i.code)}</b></td><td>${{segment:"挡板",post:"立柱",seal:"密封条"}[i.kind]}</td>
      <td>${tag(i.status === "damaged" ? "破损" : "缺归", "red")}</td>
      <td><button class="btn btn-orange small" data-loss="${esc(i.code)}">主管报损并同规格补件</button></td></tr>`).join("")}
    </tbody></table></div>` : "";
  $("#rk-body").innerHTML = body + lossHtml +
    `<div class="card"><h2>任务进展</h2><div class="flow">${FLOW.map(s =>
      `<span class="step ${t.status === s ? "on" : ""}">${TASK_NAME[s]}</span>`).join("→")}</div></div>`;

  const scan = $("#rk-scan"), code = $("#rk-code");
  if (scan) scan.onclick = async () => {
    const c = code.value.trim();
    if (!c) return toast("请输入条码", false);
    try { const r = await api("/api/rack-scan", { check_id: rc.id, code: c, person_id: actorId() });
      toast(`已扫 ${r.scanned}/${r.expected}${r.damaged ? "（该件破损，报损处理）" : ""}`);
      code.value = ""; renderRack();
    } catch (e) { toast(e.message, false); }
  };
  if (code) code.onkeydown = e => { if (e.key === "Enter") scan.click(); };
  const fin = $("#rk-finish");
  if (fin) fin.onclick = async () => {
    try { await api("/api/rack-finish", { check_id: rc.id, person_id: actorId() });
      toast("核对一致，器材归架重新可用"); renderRack();
    } catch (e) {
      if (!e.message.includes("归架差异")) return toast(e.message, false);
      if (!confirm(e.message + "\n\n确认承认差异并转主管报损补充？")) return;
      try { const r = await api("/api/rack-finish", { check_id: rc.id, person_id: actorId(), allow_diff: true });
        toast(`已登记差异：缺件${r.missing.length} 破损${r.damaged.length}，请主管报损补件`);
        renderRack();
      } catch (e2) { toast(e2.message, false); }
    }
  };
  $$("[data-loss]").forEach(b => b.onclick = async () => {
    const note = prompt("报损说明：", "归架盘点发现缺失/破损") || "";
    try { const r = await api("/api/loss-report", { task_id: t.id, item_code: b.dataset.loss,
      person_id: actorId(), note });
      toast(`已报损，补件 ${r.new_code} 就位可用`); renderRack();
    } catch (e) { toast(e.message, false); }
  });
}

// losses 走 GET 但定义为 POST，补一个 GET 支持
// ----------------------------------------------------------------- 路由
const VIEWS = { dashboard: viewDashboard, schematic: viewSchematic, pickup: viewPickup,
  install: viewInstall, water: viewWater, removal: viewRemoval, rack: viewRack };
function switchView(v) {
  state.view = v;
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  render();
}
async function render() {
  try { await VIEWS[state.view](); }
  catch (e) { $("#view").innerHTML = `<div class="card" style="color:var(--red)">加载失败：${esc(e.message)}</div>`; }
}
$("#tabs").addEventListener("click", e => {
  if (e.target.dataset.view) switchView(e.target.dataset.view);
});

(async function init() {
  await loadPeople();
  state.actor = actorId();
  await render();
  setInterval(async () => { if (state.view === "dashboard") await viewDashboard(); }, 6000);
})();
