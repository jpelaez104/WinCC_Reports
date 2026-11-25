/* Gestor de Registros WinCC - interfaz (sin framework, sin compilacion). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const ROLE_LEVEL = { lector: 1, operador: 2, supervisor: 3, admin: 4, desarrollador: 4 };
const KIND_LABEL = {
  limite_alta: "Límite alto", limite_baja: "Límite bajo", fallo_senal: "Fuera de rango instrumento",
  dato_invalido: "Dato inválido (WinCC)", hueco: "Hueco sin registros", valor_congelado: "Valor congelado",
  atipico: "Valor atípico",
};
const SEV_LABEL = { alarma: "Alarma", pre_alarma: "Pre-alarma", info: "Info" };

const state = { user: null, projects: [], project: null, variables: [], selected: new Set(), charts: [] };

// ------------------------------------------------------------------ utilidades
// Los tiempos son "hora local de planta" codificados como epoch ms: se formatean con getters UTC.
const pad = (n) => String(n).padStart(2, "0");
function fmtDT(ms, sec = false) {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${pad(d.getUTCDate())}/${pad(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${sec ? ":" + pad(d.getUTCSeconds()) : ""}`;
}
function toInput(ms) {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
function fromInput(v) { return Date.parse(v + ":00Z"); }
function fmtDur(s) {
  if (s == null) return "—";
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}
const fmtNum = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString("es", { maximumFractionDigits: d }));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const varLabel = (v) => v.alias || v.var_name;
const hasRole = (r) => state.user && ROLE_LEVEL[state.user.role] >= ROLE_LEVEL[r];

function toast(msg, ms = 3500) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && !path.startsWith("/auth/login")) { showLogin(); throw new Error("Sesión expirada"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const d = data?.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Error ${res.status}`);
  }
  return data;
}

// ------------------------------------------------------------------- sesion
function showLogin() {
  $("#appView").classList.add("hidden");
  $("#loginView").classList.remove("hidden");
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#loginError").textContent = "";
  try {
    const r = await api("/auth/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    await startApp(r.user);
  } catch (err) { $("#loginError").textContent = err.message; }
});

$("#btnLogout").onclick = async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); showLogin(); };
$("#btnPw").onclick = () => openPwDialog("");

function openPwDialog(reason) {
  $("#pwReason").textContent = reason;
  $("#pwError").textContent = "";
  $("#pwCancel").classList.toggle("hidden", !!reason);
  $("#pwForm").reset();
  $("#pwDialog").showModal();
}
$("#pwForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const f = new FormData(e.target);
  if (f.get("new1") !== f.get("new2")) { $("#pwError").textContent = "Las contraseñas no coinciden"; return; }
  try {
    await api("/auth/change-password", { method: "POST", body: { old_password: f.get("old"), new_password: f.get("new1") } });
    $("#pwDialog").close();
    state.user.must_change_password = false;
    toast("Contraseña actualizada");
  } catch (err) { $("#pwError").textContent = err.message; }
});

async function startApp(user) {
  state.user = user;
  $("#loginView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#userName").textContent = user.display_name || user.username;
  $("#userRole").textContent = user.role;
  $$("[data-role]").forEach((el) => el.classList.toggle("hidden", !hasRole(el.dataset.role)));
  if (user.must_change_password) openPwDialog("Debe cambiar su contraseña inicial antes de continuar.");
  state.projects = await api("/projects");
  const sel = $("#projectSel");
  sel.innerHTML = state.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  const saved = safeGet("gestor.project");
  if (saved && state.projects.some((p) => p.id === saved)) sel.value = saved;
  await selectProject(sel.value);
}

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } }

$("#projectSel").onchange = (e) => selectProject(e.target.value);

async function selectProject(id) {
  state.project = state.projects.find((p) => p.id === id);
  safeSet("gestor.project", id);
  state.selected.clear();
  state.variables = await api(`/projects/${encodeURIComponent(id)}/variables`);
  // rango por defecto: ultimas 24 h con datos
  const last = state.project.last_t_ms || Date.now();
  const end = Math.ceil(last / 3600000) * 3600000;
  $("#dtEnd").value = toInput(end);
  $("#dtStart").value = toInput(end - 24 * 3600000);
  renderVarList();
  $("#cards").innerHTML = `<div class="panel empty">Seleccione el rango, el muestreo y las variables, y pulse <b>Analizar</b>.</div>`;
  refreshActiveTab();
}

// ---------------------------------------------------------------- pestanas
let activeTab = "analisis";
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  activeTab = b.dataset.tab;
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$("main>section").forEach((s) => s.classList.toggle("hidden", s.dataset.view !== activeTab));
  refreshActiveTab();
});
function refreshActiveTab() {
  const fn = { informes: loadReports, limites: loadLimits, alarmas: loadAlarms, audit: loadAudit, usuarios: loadUsers, ingesta: loadIngest }[activeTab];
  if (fn) fn().catch((e) => toast(e.message));
  if (activeTab === "analisis") state.charts.forEach((c) => c.resize());
}

// ---------------------------------------------------------- lista variables
function renderVarList() {
  const q = $("#varFilter").value.trim().toLowerCase();
  const groups = {};
  for (const v of state.variables) {
    if (!v.enabled) continue;
    const hay = `${v.var_name} ${v.alias || ""} ${v.tag || ""} ${v.group}`.toLowerCase();
    if (q && !hay.includes(q)) continue;
    (groups[`${v.category} · ${v.group}`] ||= []).push(v);
  }
  $("#varList").innerHTML = Object.entries(groups).map(([g, vs]) => `
    <details class="vgroup" open><summary><input type="checkbox" data-group="${esc(g)}"> ${esc(g)} <span class="muted">(${vs.length})</span></summary>
      ${vs.map((v) => `<label><input type="checkbox" value="${v.id}" ${state.selected.has(v.id) ? "checked" : ""}>
        <span>${esc(varLabel(v))}${v.unit ? ` <span class="muted">[${esc(v.unit)}]</span>` : ""}
        ${v.alias ? `<small class="mono">${esc(v.var_name)}</small>` : ""}${v.tag ? `<small>Tag ${esc(v.tag)}</small>` : ""}</span></label>`).join("")}
    </details>`).join("") || `<div class="empty">Sin variables</div>`;
  updateVarCount();
}
function updateVarCount() { $("#varCount").textContent = `${state.selected.size} seleccionada(s)`; }
$("#varFilter").addEventListener("input", renderVarList);
$("#varList").addEventListener("change", (e) => {
  const cb = e.target;
  if (cb.dataset.group) {
    $$("label input", cb.closest("details")).forEach((x) => { x.checked = cb.checked; toggleSel(+x.value, cb.checked); });
  } else toggleSel(+cb.value, cb.checked);
  updateVarCount();
});
$("#varList").addEventListener("click", (e) => { if (e.target.matches("summary input")) e.stopPropagation(); });
function toggleSel(id, on) { on ? state.selected.add(id) : state.selected.delete(id); }
$("#selAll").onclick = () => { $$("#varList label input").forEach((x) => { x.checked = true; toggleSel(+x.value, true); }); updateVarCount(); };
$("#selNone").onclick = () => { state.selected.clear(); $$("#varList input").forEach((x) => (x.checked = false)); updateVarCount(); };

$("#quickRanges").addEventListener("click", (e) => {
  const h = e.target.dataset.h;
  if (!h) return;
  const end = fromInput($("#dtEnd").value) || Date.now();
  $("#dtStart").value = toInput(end - h * 3600000);
});
$("#stepSel").onchange = (e) => $("#customStep").classList.toggle("hidden", e.target.value !== "custom");

function stepSeconds() {
  const v = $("#stepSel").value;
  if (v !== "custom") return +v;
  return Math.max(1, Math.round(+$("#customVal").value * +$("#customUnit").value));
}

// ------------------------------------------------------------------ analisis
$("#btnAnalyze").onclick = async () => {
  if (!state.selected.size) return toast("Seleccione al menos una variable");
  if (state.selected.size > 60) return toast("Máximo 60 variables por análisis");
  const body = { variable_ids: [...state.selected], start: $("#dtStart").value, end: $("#dtEnd").value, step_s: stepSeconds() };
  const btn = $("#btnAnalyze");
  btn.disabled = true; btn.textContent = "Analizando…";
  try {
    const r = await api("/analysis", { method: "POST", body });
    renderAnalysis(r);
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = "Analizar"; }
};

function renderAnalysis(r) {
  state.charts.forEach((c) => c.dispose());
  state.charts = [];
  const cards = $("#cards");
  cards.innerHTML = r.results.map((x, i) => cardHTML(x, i)).join("");
  r.results.forEach((x, i) => drawChart($(`#chart${i}`), x, r));
}

function cardHTML(x, i) {
  const v = x.variable, s = x.stats, sm = x.summary, u = v.unit ? ` ${esc(v.unit)}` : "";
  const kpi = (label, val, cls = "") => `<div class="kpi ${cls}"><span>${label}</span><b>${val}</b></div>`;
  const wa = x.wincc_alarms;
  return `<article class="panel card">
    <header><h2>${esc(v.alias || v.var_name)}</h2>
      <span class="muted mono">${esc(v.var_name)}</span>
      ${v.tag ? `<span class="badge">Tag ${esc(v.tag)}</span>` : `<span class="badge gray">sin tag</span>`}
      <span class="badge gray">${esc(v.group)}</span>
      ${s.nominal_period_s ? `<span class="muted">registro ~${fmtDur(s.nominal_period_s)}</span>` : ""}
    </header>
    <div class="kpis">
      ${kpi("Mínimo", s.n ? fmtNum(s.min) + u : "—")}
      ${kpi("Máximo", s.n ? fmtNum(s.max) + u : "—")}
      ${kpi("Media", s.n ? fmtNum(s.mean) + u : "—")}
      ${kpi("Mediana", s.n ? fmtNum(s.median) + u : "—")}
      ${kpi("Prom. temporal", s.n ? fmtNum(s.time_weighted_mean) + u : "—")}
      ${kpi("Desv. est.", s.n ? fmtNum(s.std, 3) : "—")}
      ${kpi("Datos válidos", s.pct_valid != null ? fmtNum(s.pct_valid, 1) + " %" : "—")}
      ${kpi("Alarmas", sm.alarma, sm.alarma ? "bad" : "")}
      ${kpi("Pre-alarmas", sm.pre_alarma, sm.pre_alarma ? "warn" : "")}
      ${kpi("Alarmas WinCC", wa.tag ? wa.activations : "—", wa.activations ? "bad" : "")}
    </div>
    ${x.notes.length ? `<div class="notes">${x.notes.map(esc).join("<br>")}</div>` : ""}
    <div class="chart" id="chart${i}"></div>
    <details class="eps"><summary>Eventos y anomalías (${x.episodes_total})${x.episodes_total > x.episodes.length ? ` · se muestran ${x.episodes.length}` : ""}</summary>
      <div class="tablewrap" style="max-height:320px">${episodesTable(x.episodes, u)}</div>
    </details>
  </article>`;
}

function episodesTable(eps, u) {
  if (!eps.length) return `<div class="empty">Sin eventos en el periodo</div>`;
  return `<table><thead><tr><th>Severidad</th><th>Tipo</th><th>Inicio</th><th>Fin</th><th>Duración</th><th class="num">Pico</th><th>Detalle</th></tr></thead><tbody>
    ${eps.map((e) => `<tr class="sev-${e.severity}"><td>${SEV_LABEL[e.severity]}</td><td>${KIND_LABEL[e.kind] || e.kind}</td>
      <td>${fmtDT(e.start_ms, true)}</td><td>${fmtDT(e.end_ms, true)}</td><td>${fmtDur(e.duration_s)}</td>
      <td class="num">${e.peak != null ? fmtNum(e.peak, 3) + u : ""}</td>
      <td>${e.level ? `${e.level} (límite ${fmtNum(e.limit, 3)})` : ""}${e.cause ? esc(e.cause) : ""}${e.z ? `z=${e.z}` : ""}</td></tr>`).join("")}
  </tbody></table>`;
}

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

function drawChart(el, x, r) {
  if (!window.echarts) { el.innerHTML = `<div class="empty">No se pudo cargar la librería de gráficas (vendor/echarts.min.js)</div>`; return; }
  const chart = echarts.init(el, null, { renderer: "canvas" });
  state.charts.push(chart);
  const fg = cssVar("--fg"), muted = cssVar("--muted"), line = cssVar("--line"), accent = cssVar("--accent");
  const bad = cssVar("--bad"), warn = cssVar("--warn");
  const ser = x.series || [];
  const mean = ser.map((p) => [p[0], p[1]]);
  const lo = ser.map((p) => [p[0], p[2]]);
  const band = ser.map((p) => [p[0], p[3] == null || p[2] == null ? null : p[3] - p[2]]);
  const lim = x.limits || {};
  const markLines = [];
  for (const [k, c, lbl] of [["hh", bad, "HH"], ["h", warn, "H"], ["l", warn, "L"], ["ll", bad, "LL"]]) {
    if (lim[k] != null) markLines.push({ yAxis: lim[k], name: lbl, lineStyle: { color: c, type: k.length === 2 ? "solid" : "dashed" }, label: { formatter: `${lbl} ${lim[k]}`, color: c } });
  }
  const areas = x.episodes.filter((e) => e.severity !== "info" || e.kind === "hueco").slice(0, 200).map((e) => [
    { xAxis: e.start_ms, itemStyle: { color: e.severity === "alarma" ? bad : e.severity === "pre_alarma" ? warn : muted, opacity: e.kind === "hueco" ? 0.08 : 0.15 } },
    { xAxis: Math.max(e.end_ms, e.start_ms + r.step_s * 1000) },
  ]);
  const u = x.variable.unit || "";
  chart.setOption({
    useUTC: true, animation: false,
    grid: { left: 56, right: 56, top: 24, bottom: 64 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const p = ser.find((s) => s[0] === ps[0].value[0]);
        if (!p) return "";
        return `${fmtDT(p[0])}<br>media <b>${fmtNum(p[1], 3)}</b> ${u}<br>mín ${fmtNum(p[2], 3)} · máx ${fmtNum(p[3], 3)}<br><span style="opacity:.7">n=${p[4]}</span>`;
      },
    },
    xAxis: { type: "time", min: r.start_ms, max: r.end_ms, axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted, hideOverlap: true } },
    yAxis: { type: "value", scale: true, name: u, nameTextStyle: { color: muted }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: line } } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 22, bottom: 12, textStyle: { color: muted } }],
    series: [
      { name: "min", type: "line", data: lo, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, silent: true },
      { name: "rango", type: "line", data: band, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: accent, opacity: 0.15 }, silent: true },
      { name: "media", type: "line", data: mean, symbol: "none", sampling: "lttb", lineStyle: { color: accent, width: 1.5 }, connectNulls: false,
        markLine: { symbol: "none", silent: true, data: markLines }, markArea: { silent: true, data: areas } },
    ],
    textStyle: { color: fg },
  });
}
window.addEventListener("resize", () => state.charts.forEach((c) => c.resize()));

// ------------------------------------------------------- informes firmados
// Solo se generan al pulsar "Generar reporte" (nunca automaticamente).
const STATUS_LABEL = { elaborado: "Elaborado", revisado: "Revisado", aprobado: "Aprobado" };
const DECLARATION = {
  Elaborado: "Declaro que elaboré este informe y que sus datos corresponden al periodo y variables indicados.",
  Revisado: "Declaro que revisé este informe y su contenido es correcto.",
  Aprobado: "Apruebo este informe.",
};
let reportCtx = null;

function fmtStep(s) {
  if (s % 86400 === 0) return `${s / 86400} día(s)`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}

function openReportDialog(ctx) {
  reportCtx = ctx;
  const f = $("#reportForm");
  f.reset();
  $("#repError").textContent = "";
  $("#repMeaning").textContent = ctx.meaning;
  $("#repSigner").textContent = `${state.user.display_name || state.user.username} (${state.user.username})`;
  $("#repDeclaration").textContent = DECLARATION[ctx.meaning];
  $("#repNewFields").classList.toggle("hidden", !ctx.create);
  $("#repDlgTitle").textContent = ctx.create ? "Generar reporte firmado" : `Firmar informe ${ctx.report.id}`;
  $("#repOk").textContent = ctx.create ? "Firmar y generar" : `Firmar como ${ctx.meaning}`;
  $("#repSummary").innerHTML = ctx.summary;
  if (ctx.create) f.title.value = `Informe de registros — ${state.project.name}`;
  $("#reportDialog").showModal();
  f.password.focus();
}

$("#btnReport").onclick = () => {
  if (!state.selected.size) return toast("Seleccione al menos una variable");
  if (state.selected.size > 60) return toast("Máximo 60 variables por informe");
  const start = $("#dtStart").value, end = $("#dtEnd").value, step = stepSeconds();
  if (!start || !end || fromInput(end) <= fromInput(start)) return toast("Revise las fechas de inicio y fin");
  const names = state.variables.filter((v) => state.selected.has(v.id)).map(varLabel);
  openReportDialog({
    create: true, meaning: "Elaborado",
    body: { project_id: state.project.id, variable_ids: [...state.selected], start, end, step_s: step },
    summary: `${esc(state.project.name)} · ${fmtDT(fromInput(start))} → ${fmtDT(fromInput(end))} · muestreo ${fmtStep(step)}<br>
      ${names.length} variable(s): ${esc(names.slice(0, 6).join(", "))}${names.length > 6 ? "…" : ""}<br>
      Incluye gráfica, estadística y eventos por variable, y al final los valores tabulados de todas.`,
  });
};

$("#reportForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const f = new FormData(e.target);
  const btn = $("#repOk");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = reportCtx.create ? "Generando PDF…" : "Firmando…";
  $("#repError").textContent = "";
  try {
    let rep;
    if (reportCtx.create) {
      rep = await api("/reports", { method: "POST", body: { ...reportCtx.body, title: f.get("title"), comment: f.get("comment"), password: f.get("password"), sign_comment: f.get("sign_comment") } });
    } else {
      rep = await api(`/reports/${encodeURIComponent(reportCtx.report.id)}/sign`, { method: "POST", body: { meaning: reportCtx.meaning, password: f.get("password"), comment: f.get("sign_comment") } });
    }
    $("#reportDialog").close();
    toast(`Informe ${rep.id} v${rep.version} firmado (${STATUS_LABEL[rep.status]})`);
    window.open(`/api/reports/${encodeURIComponent(rep.id)}/pdf`, "_blank");
    if (activeTab === "informes") loadReports().catch((x) => toast(x.message));
  } catch (err) { $("#repError").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = label; }
});

async function loadReports() {
  const list = await api(`/reports?project_id=${encodeURIComponent(state.project.id)}`);
  state.reports = list;
  const sig = (r, m) => {
    const s = r.signatures.find((x) => x.meaning === m);
    return s ? `<b>${esc(s.display_name || s.username)}</b><br><span class="muted">${fmtDT(s.signed_ms)}</span>` : `<span class="muted">pendiente</span>`;
  };
  $("#reportsTable").innerHTML = list.length ? `<thead><tr><th>Informe</th><th>Título</th><th>Periodo</th><th>Muestreo</th><th class="num">Var.</th><th>Estado</th>
      <th>Elaborado</th><th>Revisado</th><th>Aprobado</th><th></th></tr></thead><tbody>
    ${list.map((r) => `<tr data-id="${esc(r.id)}"><td class="mono">${esc(r.id)}<br><span class="muted">v${r.version}</span></td><td>${esc(r.title)}</td>
      <td>${fmtDT(r.start_ms)} →<br>${fmtDT(r.end_ms)}</td><td>${fmtStep(r.step_s)}</td><td class="num">${r.variable_ids.length}</td>
      <td><span class="badge ${r.status === "aprobado" ? "" : "gray"}">${STATUS_LABEL[r.status]}</span></td>
      <td>${sig(r, "Elaborado")}</td><td>${sig(r, "Revisado")}</td><td>${sig(r, "Aprobado")}</td>
      <td style="white-space:nowrap"><a class="btn small" target="_blank" href="/api/reports/${encodeURIComponent(r.id)}/pdf" title="Informe PDF con sello digital PAdES">PDF</a>
        <a class="btn small" href="/api/reports/${encodeURIComponent(r.id)}/xlsx" title="Informe en Excel (mismos datos que el PDF)">Excel</a>
        <a class="btn small" href="/api/reports/${encodeURIComponent(r.id)}/p7s" title="Sello digital del Excel (firma CMS .p7s): guárdelo junto al .xlsx">Sello .p7s</a>
        ${r.next_signature && r.next_signature !== "Elaborado" && hasRole("supervisor") ? `<button class="btn small primary" data-sign="${esc(r.next_signature)}">Firmar ${esc(r.next_signature)}</button>` : ""}</td></tr>`).join("")}
    </tbody>` : `<tbody><tr><td class="empty">Aún no hay informes. Genérelos desde la pestaña Análisis con el botón <b>Generar reporte</b>.</td></tr></tbody>`;
}

$("#reportsTable").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sign]");
  if (!b) return;
  const r = state.reports.find((x) => x.id === b.closest("tr").dataset.id);
  openReportDialog({
    create: false, meaning: b.dataset.sign, report: r,
    summary: `${esc(r.title)}<br>${fmtDT(r.start_ms)} → ${fmtDT(r.end_ms)} · muestreo ${fmtStep(r.step_s)} · ${r.variable_ids.length} variable(s)<br>
      Revise el PDF antes de firmar: <a target="_blank" href="/api/reports/${encodeURIComponent(r.id)}/pdf">abrir v${r.version}</a>`,
  });
});

$("#verifyFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const box = $("#verifyResult");
  box.classList.remove("hidden");
  box.textContent = "Calculando SHA-256…";
  try {
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    const sha = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
    const r = await api(`/reports/verify?sha256=${sha}`);
    const seal = r.seal ? `<br>Sello .p7s del Excel guardado en el gestor: ${r.seal.valid ? "válido" : "<b>NO VÁLIDO</b>"}${r.seal.valid && r.seal.trusted ? " (certificado del gestor)" : ""}` : "";
    const ok = r.found && r.data_intact && (!r.seal || r.seal.valid);
    box.innerHTML = `<div class="notes ${ok ? "" : "bad"}"><b>${esc(file.name)}</b>: ${esc(r.message)}
      ${r.found ? `<br>Firmas de esa versión: ${r.report.signatures.slice(0, r.version).map((s) => esc(s.meaning)).join(", ")} · datos ${r.data_intact ? "íntegros" : "<b>ALTERADOS</b>"}${seal}` : ""}
      <br><span class="mono muted">SHA-256 ${sha}</span></div>`;
  } catch (err) { box.textContent = err.message; }
});

// ------------------------------------------------------------------- limites
async function loadLimits() {
  const pid = state.project.id;
  const [vars, tags] = await Promise.all([api(`/projects/${pid}/variables`), api(`/projects/${pid}/alarm-tags`)]);
  state.variables = vars;
  const canEdit = hasRole("supervisor");
  const dis = canEdit ? "" : "disabled";
  const tagOpts = (cur) => `<option value=""></option>` + tags.map((t) => `<option ${t.tag === cur ? "selected" : ""}>${esc(t.tag)}</option>`).join("");
  const num = (n, v) => `<input type="number" step="any" name="${n}" value="${v ?? ""}" ${dis}>`;
  $("#limitsTable").innerHTML = `<thead><tr><th>Variable</th><th>Alias</th><th>Unidad</th><th>Tag alarma</th>
    <th>LL</th><th>L</th><th>H</th><th>HH</th><th>Rango mín</th><th>Rango máx</th><th>Estado</th><th></th></tr></thead><tbody>
    ${vars.map((v) => { const l = v.limits || {}; const configured = v.limits && ["ll", "l", "h", "hh"].some((k) => l[k] != null);
      return `<tr data-id="${v.id}">
      <td><span class="mono">${esc(v.var_name)}</span><br><small class="muted">${esc(v.group)}</small></td>
      <td><input type="text" class="wide" name="alias" value="${esc(v.alias || "")}" ${dis}></td>
      <td><input type="text" name="unit" value="${esc(v.unit || "")}" style="width:60px" ${dis}></td>
      <td><select name="tag" ${dis}>${tagOpts(v.tag)}</select></td>
      <td>${num("ll", l.ll)}</td><td>${num("l", l.l)}</td><td>${num("h", l.h)}</td><td>${num("hh", l.hh)}</td>
      <td>${num("range_min", l.range_min)}</td><td>${num("range_max", l.range_max)}</td>
      <td>${configured ? `<span class="badge ok">${esc(l.source)}</span><br><small class="muted">${esc(l.updated_by || "")} ${fmtDT(l.updated_ms)}</small>` : `<span class="badge warn">pendiente</span>`}</td>
      <td style="white-space:nowrap">${canEdit ? `<button class="btn small" data-act="suggest" ${v.tag ? "" : "disabled"}>Sugerir</button> <button class="btn small primary" data-act="save">Guardar</button>` : ""}</td></tr>`; }).join("")}
  </tbody>`;
  $("#tagsTable").innerHTML = `<thead><tr><th>Tag</th><th>Niveles</th><th class="num">Activaciones</th><th>Primera</th><th>Última</th><th>Variable asociada</th></tr></thead><tbody>
    ${tags.map((t) => `<tr><td class="mono">${esc(t.tag)}</td><td>${t.levels.map((l) => `<span class="badge ${l.length === 2 || l === "FALLO" ? "bad" : "warn"}">${l}</span>`).join(" ")}</td>
      <td class="num">${t.occurrences}</td><td>${fmtDT(t.first_t_ms)}</td><td>${fmtDT(t.last_t_ms)}</td>
      <td>${t.variable_ids.length ? t.variable_ids.map((id) => esc(varLabel(vars.find((v) => v.id === id) || { var_name: id }))).join(", ") : `<span class="badge gray">sin asociar</span>`}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No se detectaron alarmas analógicas (formato "TAG Alarma HH/H/L/LL") en este proyecto</td></tr>`}
  </tbody>`;
}

$("#limitsTable").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const tr = b.closest("tr"), id = +tr.dataset.id;
  const val = (n) => tr.querySelector(`[name=${n}]`).value;
  try {
    if (b.dataset.act === "suggest") {
      const s = await api(`/variables/${id}/limits/suggest`);
      let n = 0;
      for (const [lvl, d] of Object.entries(s.levels)) {
        const inp = tr.querySelector(`[name=${lvl.toLowerCase()}]`);
        if (inp && d.value != null && inp.value === "") { inp.value = d.value; inp.style.outline = `2px solid ${cssVar("--warn")}`; n++; }
      }
      toast(n ? `Sugeridos ${n} límites a partir de las activaciones de alarma. Revíselos y pulse Guardar.` : "No hay datos suficientes para sugerir (¿tag correcto? ¿hay activaciones con datos registrados?)", 6000);
      return;
    }
    await api(`/variables/${id}`, { method: "PATCH", body: { alias: val("alias"), unit: val("unit"), tag: val("tag") } });
    const body = {};
    for (const k of ["ll", "l", "h", "hh", "range_min", "range_max"]) body[k] = val(k) === "" ? null : +val(k);
    body.source = [...tr.querySelectorAll("input")].some((i) => i.style.outline) ? "sugerido_alarmas" : "manual";
    await api(`/variables/${id}/limits`, { method: "PUT", body });
    toast("Guardado");
    loadLimits();
  } catch (err) { toast(err.message, 6000); }
});

// ------------------------------------------------------------- alarmas/audit
function rangeQS() {
  return `start=${encodeURIComponent($("#dtStart").value)}&end=${encodeURIComponent($("#dtEnd").value)}`;
}
function rangeText() { return `${fmtDT(fromInput($("#dtStart").value))} → ${fmtDT(fromInput($("#dtEnd").value))} (rango de la pestaña Análisis)`; }

let alarmRows = [];
async function loadAlarms() {
  $("#alarmRange").textContent = rangeText();
  alarmRows = await api(`/projects/${state.project.id}/alarms?${rangeQS()}&only_active=${$("#onlyActive").checked}`);
  renderAlarms();
}
function renderAlarms() {
  const q = $("#alarmFilter").value.trim().toLowerCase();
  const rows = q ? alarmRows.filter((a) => a.text.toLowerCase().includes(q)) : alarmRows;
  const st = { 1: "Entra", 0: "Sale", 2: "Acuse", 3: "Acuse" };
  $("#alarmsTable").innerHTML = `<thead><tr><th>Fecha</th><th>Estado</th><th>Clase</th><th class="num">Nº</th><th>Texto</th><th>Fichero</th></tr></thead><tbody>
    ${rows.map((a) => `<tr class="${a.state_after === 1 ? "sev-alarma" : "sev-info"}"><td>${fmtDT(a.t_ms, true)}</td><td>${st[a.state_after] ?? a.state_after}</td><td>${a.msg_class}</td><td class="num">${a.msg_number}</td><td>${esc(a.text)}</td><td class="muted">${esc(a.group)}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">Sin alarmas en el rango</td></tr>`}</tbody>`;
}
$("#onlyActive").onchange = () => loadAlarms().catch((e) => toast(e.message));
$("#alarmFilter").addEventListener("input", renderAlarms);

async function loadAudit() {
  $("#auditRange").textContent = rangeText();
  const rows = await api(`/projects/${state.project.id}/wincc-audit?${rangeQS()}`);
  $("#winccAuditTable").innerHTML = `<thead><tr><th>Fecha</th><th>UTC</th><th>Usuario</th><th>Objeto</th><th>Descripción</th><th>Comentario</th></tr></thead><tbody>
    ${rows.map((a) => `<tr><td>${fmtDT(a.t_ms, true)}</td><td>${esc(a.delta_utc)}</td><td>${esc(a.user)}</td><td>${esc(a.object)}</td><td>${esc(a.description)}</td><td>${esc(a.comment)}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">Sin registros en el rango</td></tr>`}</tbody>`;
  if (hasRole("supervisor")) {
    const app = await api("/audit/app?limit=500");
    $("#chainState").innerHTML = app.chain.ok ? `<span class="badge ok">Cadena íntegra · ${app.chain.records} registros</span>` : `<span class="badge bad">Cadena ROTA en id ${app.chain.broken_at_id}</span>`;
    $("#appAuditTable").innerHTML = `<thead><tr><th>#</th><th>Fecha</th><th>Usuario</th><th>Acción</th><th>Entidad</th><th>Detalle</th></tr></thead><tbody>
      ${app.records.map((a) => `<tr><td class="num">${a.id}</td><td>${fmtDT(a.ts_ms, true)}</td><td>${esc(a.user)}</td><td>${esc(a.action)}</td><td class="mono">${esc(a.entity)}</td><td class="mono" style="font-size:11px">${esc(a.details ? JSON.stringify(a.details) : "")}</td></tr>`).join("")}</tbody>`;
  }
}

// ------------------------------------------------------------------ usuarios
async function loadUsers() {
  const users = await api("/users");
  $("#usersTable").innerHTML = `<thead><tr><th>Usuario</th><th>Nombre</th><th>Origen</th><th>Grupo WinCC</th><th>Rol</th><th>Estado</th><th>Último ingreso</th><th></th></tr></thead><tbody>
    ${users.map((u) => `<tr data-id="${u.id}"><td class="mono">${esc(u.username)}</td><td>${esc(u.display_name)}</td><td>${esc(u.source)}</td><td>${esc(u.wincc_group || "")}</td>
      <td><select data-act="role">${Object.keys(ROLE_LEVEL).map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td>${u.locked ? `<span class="badge bad">bloqueado</span>` : u.active ? `<span class="badge ok">activo</span>` : `<span class="badge gray">inactivo</span>`}
        ${!u.has_password ? `<span class="badge warn">sin contraseña</span>` : ""}</td>
      <td>${fmtDT(u.last_login_ms)}</td>
      <td style="white-space:nowrap"><button class="btn small" data-act="pw">Contraseña inicial</button>
        ${u.locked ? `<button class="btn small" data-act="unlock">Desbloquear</button>` : ""}
        <button class="btn small" data-act="toggle">${u.active ? "Desactivar" : "Activar"}</button></td></tr>`).join("")}</tbody>`;
}
$("#usersTable").addEventListener("change", async (e) => {
  if (e.target.dataset.act !== "role") return;
  const id = +e.target.closest("tr").dataset.id;
  try { await api(`/users/${id}`, { method: "PATCH", body: { role: e.target.value } }); toast("Rol actualizado"); }
  catch (err) { toast(err.message); loadUsers(); }
});
$("#usersTable").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const tr = b.closest("tr"), id = +tr.dataset.id;
  try {
    if (b.dataset.act === "pw") {
      const pw = prompt("Contraseña inicial (mín. 8 caracteres, letras y números). El usuario deberá cambiarla al ingresar:");
      if (!pw) return;
      await api(`/users/${id}`, { method: "PATCH", body: { new_password: pw } });
    } else if (b.dataset.act === "unlock") {
      await api(`/users/${id}`, { method: "PATCH", body: { unlock: true } });
    } else if (b.dataset.act === "toggle") {
      await api(`/users/${id}`, { method: "PATCH", body: { active: b.textContent.trim() === "Activar" } });
    }
    toast("Usuario actualizado");
    loadUsers();
  } catch (err) { toast(err.message, 6000); }
});
$("#btnSyncUsers").onclick = async () => {
  try { const r = await api("/users/sync-wincc", { method: "POST" }); toast(`Nuevos: ${r.created.length}. Grupo actualizado: ${r.updated.length}`); loadUsers(); }
  catch (e) { toast(e.message); }
};
$("#btnNewUser").onclick = () => { $("#userForm").reset(); $("#userError").textContent = ""; $("#userDialog").showModal(); };
$("#userForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const u = await api("/users", { method: "POST", body: { username: f.get("username"), display_name: f.get("display_name") || null, source: f.get("source"), role: f.get("role"), password: f.get("password") } });
    if (!f.get("active")) await api(`/users/${u.id}`, { method: "PATCH", body: { active: false } });
    $("#userDialog").close(); toast("Usuario creado"); loadUsers();
  } catch (err) { $("#userError").textContent = err.message; }
});

// ------------------------------------------------------------------- ingesta
async function loadIngest() {
  const st = await api("/ingest/status");
  const files = st.files.filter((f) => f.project_id === state.project.id);
  $("#ingestState").innerHTML = st.status ? `<span class="badge ${st.status === "ok" ? "ok" : "warn"}">${esc(st.status)}</span>` : "";
  $("#ingestSummary").innerHTML = st.started
    ? `Última ejecución: <b>${esc(st.started.replace("T", " "))}</b> (${esc(st.reason)}) · ${st.seconds} s · ficheros leídos ${st.collect?.files ?? "—"}, con cambios ${st.collect?.changed ?? "—"} · filas insertadas ${st.load?.inserted ?? "—"} · <b>lotes en cola: ${st.pending_batches}</b>
       ${[...(st.collect?.errors || []), ...(st.load?.errors || [])].map((x) => `<div class="error">${esc(x)}</div>`).join("")}`
    : "Aún no se ha ejecutado ninguna ingesta.";
  $("#filesTable").innerHTML = `<thead><tr><th>Fichero</th><th>Categoría</th><th>Cargado hasta</th><th class="num">Registros WinCC</th><th>Última carga</th></tr></thead><tbody>
    ${files.map((f) => `<tr><td class="mono">${esc(f.rel_path)}</td><td>${esc(f.category)}</td><td>${fmtDT(f.loaded_t_ms, true)}</td><td class="num">${f.rt_count ?? "—"}</td><td>${fmtDT(f.last_load_ms)}</td></tr>`).join("")}</tbody>`;
}
$("#btnIngest").onclick = async (e) => {
  e.target.disabled = true;
  try { const r = await api("/ingest/run", { method: "POST" }); toast(`Ingesta: ${r.status}`); await loadIngest(); }
  catch (err) { toast(err.message); }
  finally { e.target.disabled = false; }
};

// --------------------------------------------------------------------- inicio
(async () => {
  try { await startApp(await api("/auth/me")); }
  catch { showLogin(); }
})();
