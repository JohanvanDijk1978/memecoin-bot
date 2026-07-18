/* memedash frontend — no build step, ES modules + ECharts (CDN) */
const VERSION = "1.08"; // bump together with VERSION in main.py

const view = document.getElementById("view");
const $ = (id) => document.getElementById(id);

/* ---------------- state (persists across sessions) ---------------- */
const state = {
  days: localStorage.getItem("days") ?? "30",
  chain: localStorage.getItem("chain") ?? "",
};
$("f-days").value = state.days;
$("f-chain").value = state.chain;
$("f-days").onchange = (e) => { state.days = e.target.value; localStorage.setItem("days", state.days); render(); };
$("f-chain").onchange = (e) => { state.chain = e.target.value; localStorage.setItem("chain", state.chain); render(); };
$("f-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") location.hash = "#/calls?q=" + encodeURIComponent(e.target.value);
});

/* ---------------- helpers ---------------- */
async function api(path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  const r = await fetch(`/api/${path}?${qs}`);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtMc = (v) => !v ? "—" : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}K` : `$${Math.round(v)}`;
const fmtMult = (m) => m == null ? `<span class="mono" style="color:var(--dim)">—</span>`
  : `<span class="mult ${m >= 2 ? "pos" : m < 1 ? "neg" : ""}">${m.toFixed(2)}×</span>`;
const fmtPct = (p) => `<span class="${p >= 50 ? "pos" : p >= 25 ? "warn" : ""}">${p}%</span>`;
const multPeak = (m, peak) => fmtMult(m) +
  (m != null && peak ? ` <span style="color:var(--muted);font-size:11px">${fmtMc(peak)}</span>` : "");
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};
const chainBadge = (c) => `<span class="badge ${c.toLowerCase()}">${c}</span>`;
const padre = (a) => `https://trade.padre.gg/trade/${a.startsWith("0x") ? "eth" : "solana"}/${a}`;
const tokenLink = (t) =>
  `<a href="${padre(t.address)}" target="_blank" rel="noopener"><b>${t.ticker ? "$" + esc(t.ticker) : t.address.slice(0, 6) + "…"}</b></a>` +
  ` <a href="#/token/${t.address}" title="details" style="color:var(--dim)">ⓘ</a>`;

/* sortable table: cols = [{key,label,num,fmt,sortVal}] */
function table(cols, rows, { defaultSort, onRow } = {}) {
  let sortKey = defaultSort ?? cols[0].key, desc = true;
  const el = document.createElement("div");
  const draw = () => {
    const sorted = [...rows].sort((a, b) => {
      const col = cols.find((c) => c.key === sortKey);
      const va = col.sortVal ? col.sortVal(a) : a[sortKey], vb = col.sortVal ? col.sortVal(b) : b[sortKey];
      const cmp = typeof va === "string" ? va.localeCompare(vb) : (va ?? -1e18) - (vb ?? -1e18);
      return desc ? -cmp : cmp;
    });
    el.innerHTML = rows.length ? `<table><thead><tr>${cols.map((c) =>
      `<th class="${c.num ? "num" : ""} ${c.key === sortKey ? "sorted" : ""}" data-k="${c.key}">${c.label}${c.key === sortKey ? (desc ? " ↓" : " ↑") : ""}</th>`).join("")}</tr></thead>
      <tbody>${sorted.map((r, i) => `<tr class="${onRow ? "click" : ""}" data-i="${rows.indexOf(r)}">${
        cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.fmt ? c.fmt(r) : esc(r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      : `<div class="empty">No data for this filter.</div>`;
    el.querySelectorAll("th").forEach((th) => th.onclick = () => {
      const k = th.dataset.k;
      if (sortKey === k) desc = !desc; else { sortKey = k; desc = true; }
      draw();
    });
    if (onRow) el.querySelectorAll("tbody tr").forEach((tr) => tr.onclick = () => onRow(rows[+tr.dataset.i]));
  };
  draw();
  return el;
}

const charts = [];
function chart(el, option) {
  const c = echarts.init(el, null, { renderer: "canvas" });
  c.setOption({
    textStyle: { fontFamily: "Inter, sans-serif" },
    grid: { left: 45, right: 15, top: 25, bottom: 25 },
    tooltip: { backgroundColor: "#161923", borderColor: "#2e3342", textStyle: { color: "#e6e8ee" } },
    ...option,
  });
  charts.push(c);
  return c;
}
window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
const axis = (extra = {}) => ({ axisLine: { lineStyle: { color: "#232734" } }, axisLabel: { color: "#8b90a0", fontSize: 10 }, splitLine: { lineStyle: { color: "#161923" } }, ...extra });

function kpis(a) {
  const item = (k, v) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  return `<div class="cards">
    ${item("Calls", `${a.calls} <small>${a.unique_cas} CAs</small>`)}
    ${item("≥2× rate", fmtPct(a.hit2))}${item("≥5× rate", fmtPct(a.hit5))}${item("≥10× rate", fmtPct(a.hit10))}
    ${item("Avg peak", a.avg_mult + "×")}${item("Median peak", a.med_mult + "×")}${item("Best", a.best_mult + "×")}
  </div>`;
}

/* shared leaderboard columns */
const lbCols = (nameLabel, href) => [
  { key: "name", label: nameLabel, fmt: (r) => `<a href="${href}${encodeURIComponent(r.key ?? r.name)}"><b>${esc(r.name)}</b></a>` },
  { key: "calls", label: "Calls", num: true },
  { key: "hit2", label: "≥2×", num: true, fmt: (r) => fmtPct(r.hit2) },
  { key: "hit5", label: "≥5×", num: true, fmt: (r) => fmtPct(r.hit5) },
  { key: "hit10", label: "≥10×", num: true, fmt: (r) => fmtPct(r.hit10) },
  { key: "avg_mult", label: "Avg ×", num: true, fmt: (r) => fmtMult(r.avg_mult) },
  { key: "med_mult", label: "Med ×", num: true, fmt: (r) => fmtMult(r.med_mult) },
  { key: "consistency", label: "Score", num: true, fmt: (r) => `<b>${r.consistency.toFixed(2)}</b>` },
  { key: "best_call", label: "Best call", sortVal: (r) => r.best_call?.mult ?? 0,
    fmt: (r) => r.best_call ? `${tokenLink({ ticker: r.best_call.ticker, address: r.best_call.address })} ${multPeak(r.best_call.mult, r.best_call.peak_mc)}` : "—" },
  { key: "last_active", label: "Active", num: true, fmt: (r) => `<span style="color:var(--muted)">${ago(r.last_active)}</span>` },
];

/* ---------------- pages ---------------- */
const pages = {
  async overview() {
    const d = await api("overview", { days: state.days, chain: state.chain });
    view.innerHTML = kpis(d) + `
      <div class="grid2">
        <div class="panel"><h3>Calls per day</h3><div class="chart" id="c-day"></div></div>
        <div class="panel"><h3>Peak multiplier distribution</h3><div class="chart" id="c-hist"></div></div>
      </div>
      <div class="panel"><h3>Top movers — ${$("f-days").selectedOptions[0].text.toLowerCase()}</h3><div id="movers"></div></div>`;
    chart($("c-day"), {
      xAxis: { type: "category", data: d.per_day.map((x) => x[0].slice(5)), ...axis() },
      yAxis: { type: "value", ...axis() },
      series: [{ type: "line", data: d.per_day.map((x) => x[1]), smooth: true, symbol: "none",
        lineStyle: { color: "#7c6cff", width: 2 },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(124,108,255,.35)" }, { offset: 1, color: "rgba(124,108,255,0)" }] } } }],
    });
    chart($("c-hist"), {
      xAxis: { type: "category", data: d.histogram.map((h) => h.label), ...axis() },
      yAxis: { type: "value", ...axis() },
      series: [{ type: "bar", data: d.histogram.map((h, i) => ({ value: h.count, itemStyle: { color: ["#ff5c6c", "#8b90a0", "#3fdd8f", "#3fdd8f", "#7c6cff", "#ffb547"][i], borderRadius: [4, 4, 0, 0] } })), barWidth: "55%" }],
    });
    $("movers").append(table([
      { key: "ticker", label: "Token", fmt: tokenLink },
      { key: "chain", label: "Chain", fmt: (r) => chainBadge(r.chain) },
      { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.mult * r.first_mc) },
      { key: "first_mc", label: "Called at", num: true, fmt: (r) => fmtMc(r.first_mc) },
      { key: "current_mc", label: "Now", num: true, fmt: (r) => fmtMc(r.current_mc) },
      { key: "caller", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
      { key: "group", label: "Group" },
      { key: "called_at", label: "When", num: true, fmt: (r) => `<span style="color:var(--muted)">${ago(r.called_at)}</span>` },
    ], d.top_movers, { defaultSort: "mult" }));
  },

  async callers() {
    const rows = await api("callers", { days: state.days, chain: state.chain, min_calls: 2 });
    view.innerHTML = `<div class="panel"><h3>${rows.length} callers · min 2 calls · score = 2× hit-rate weighted by sample size</h3><div id="t"></div></div>`;
    $("t").append(table(lbCols("Caller", "#/caller/"), rows, { defaultSort: "consistency" }));
  },

  async groups() {
    const rows = await api("groups", { days: state.days, chain: state.chain });
    const cols = lbCols("Group", "#/group/");
    cols.splice(9, 0, { key: "active_callers", label: "Callers", num: true },
      { key: "top_caller", label: "Top caller", fmt: (r) => r.top_caller ? `<a href="#/caller/${encodeURIComponent(r.top_caller_key ?? r.top_caller)}">${esc(r.top_caller)}</a>` : "—" });
    view.innerHTML = `<div class="panel"><h3>${rows.length} groups</h3><div id="t"></div></div>`;
    $("t").append(table(cols, rows, { defaultSort: "consistency" }));
  },

  async sources() {
    const rows = await api("sources", { days: state.days });
    const cols = lbCols("Source", "#/calls?source=");
    cols.push({ key: "avg_hours_to_peak", label: "Avg h→peak", num: true,
      fmt: (r) => r.avg_hours_to_peak == null ? "—" : r.avg_hours_to_peak + "h" });
    view.innerHTML = `<div class="panel"><h3>Scan sources — where good tokens come from (h→peak observed since dashboard deploy)</h3><div id="t"></div></div>`;
    $("t").append(table(cols, rows, { defaultSort: "calls" }));
  },

  async calls(params) {
    const q = params.get("q") ?? "", minMult = params.get("min_mult") ?? "";
    let page = +(params.get("page") ?? 1);
    const load = () => api("calls", {
      days: state.days, chain: state.chain, q, page,
      caller: params.get("caller") ?? "", group: params.get("group") ?? "",
      source: params.get("source") ?? "", min_mult: minMult, sort: params.get("sort") ?? "called_at",
    });
    const d = await load();
    view.innerHTML = `<div class="panel">
      <h3>${d.total} calls ${q ? `matching “${esc(q)}”` : ""}
        · sort <select id="s-sort"><option value="called_at">newest</option><option value="mult">multiplier</option><option value="first_mc">mcap</option></select>
        · min × <input type="number" id="s-mult" style="width:70px" value="${esc(minMult)}" placeholder="0"></h3>
      <div id="t"></div>
      <div class="pager"><button id="prev">←</button> page ${d.page} / ${Math.max(1, Math.ceil(d.total / 50))} <button id="next">→</button></div>
    </div>`;
    $("s-sort").value = params.get("sort") ?? "called_at";
    const nav = (kv) => { const p = new URLSearchParams(params); Object.entries(kv).forEach(([k, v]) => v ? p.set(k, v) : p.delete(k)); location.hash = "#/calls?" + p; };
    $("s-sort").onchange = (e) => nav({ sort: e.target.value, page: "" });
    $("s-mult").onchange = (e) => nav({ min_mult: e.target.value, page: "" });
    $("prev").disabled = page <= 1; $("next").disabled = page * 50 >= d.total;
    $("prev").onclick = () => nav({ page: page - 1 });
    $("next").onclick = () => nav({ page: page + 1 });
    $("t").append(table([
      { key: "ticker", label: "Token", fmt: tokenLink },
      { key: "chain", label: "Chain", fmt: (r) => chainBadge(r.chain) },
      { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.eff_peak) },
      { key: "first_mc", label: "Called at", num: true, fmt: (r) => fmtMc(r.first_mc) },
      { key: "current_mc", label: "Now", num: true, fmt: (r) => r.dead ? `<span class="neg">dead</span>` : fmtMc(r.current_mc) },
      { key: "caller", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
      { key: "group", label: "Group", fmt: (r) => `<a href="#/group/${encodeURIComponent(r.group)}">${esc(r.group)}</a>` },
      { key: "source", label: "Src", fmt: (r) => `<span class="badge">${esc(r.source)}</span>` },
      { key: "scan_count", label: "Scans", num: true },
      { key: "called_at", label: "When", num: true, fmt: (r) => `<span style="color:var(--muted)">${ago(r.called_at)}</span>` },
    ], d.rows));
  },

  async token(_, addr) {
    const d = await api(`token/${addr}`);
    const t = d.token ?? {};
    const links = Object.entries(d.links).map(([k, v]) => `<a href="${v}" target="_blank">${k} ↗</a>`).join("");
    view.innerHTML = `
      <div class="crumb"><a href="#/calls">Explorer</a> / token</div>
      ${kpisToken(t, d)}
      <div class="panel"><h3>Call timeline — earliest caller: <b style="color:var(--text)">${esc(d.earliest ?? "?")}</b>
        <span style="float:right" class="links">${links}</span></h3><div id="t"></div></div>`;
    $("t").append(table([
      { key: "called_at", label: "When", fmt: (r) => new Date(r.called_at * 1000).toLocaleString() },
      { key: "caller", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
      { key: "group", label: "Group", fmt: (r) => `<a href="#/group/${encodeURIComponent(r.group)}">${esc(r.group)}</a>` },
      { key: "source", label: "Src", fmt: (r) => `<span class="badge">${esc(r.source)}</span>` },
      { key: "mc_at_call", label: "MC at call", num: true, fmt: (r) => fmtMc(r.mc_at_call) },
      { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.mult * r.mc_at_call) },
      { key: "scan_count", label: "Scans", num: true },
    ], d.calls, { defaultSort: "called_at" }));

    function kpisToken(t, d) {
      const item = (k, v) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
      return `<div class="cards">
        ${item("Token", `${t.ticker ? "$" + esc(t.ticker) : addr.slice(0, 8)} ${chainBadge(t.chain ?? "?")}`)}
        ${item("Current MC", t.dead ? `<span class="neg">dead</span>` : fmtMc(t.current_mc))}
        ${item("Peak (observed)", fmtMc(Math.max(t.peak_mc_dash ?? 0, ...d.calls.map((c) => c.mc_at_call ?? 0))))}
        ${item("Groups called", d.calls.length)}
        ${item("First called", d.calls[0] ? ago(d.calls[0].called_at) : "—")}
      </div>`;
    }
  },

  async caller(_, name) { await profilePage("caller", name); },
  async group(_, name) { await profilePage("group", name); },
};

async function profilePage(kind, name) {
  const d = await api(`${kind}/${encodeURIComponent(name)}`);
  view.innerHTML = `
    <div class="crumb"><a href="#/${kind}s">${kind}s</a> / profile</div>
    ${kpis(d.summary)}
    <div class="grid2">
      <div class="panel"><h3>Monthly calls & ≥2× rate</h3><div class="chart" id="c-month"></div></div>
      <div class="panel"><h3>${kind === "caller" ? "Groups posted in" : "Top callers"}</h3><div id="side"></div></div>
    </div>
    <div class="panel" style="margin-bottom:18px"><h3>Best calls</h3><div id="best"></div></div>
    <div class="panel"><h3>Recent</h3><div id="recent"></div></div>`;
  chart($("c-month"), {
    xAxis: { type: "category", data: d.monthly.map((m) => m.month), ...axis() },
    yAxis: [{ type: "value", ...axis() }, { type: "value", max: 100, ...axis({ splitLine: { show: false } }) }],
    series: [
      { type: "bar", data: d.monthly.map((m) => m.calls), itemStyle: { color: "#2e3342", borderRadius: [3, 3, 0, 0] }, barWidth: "50%" },
      { type: "line", yAxisIndex: 1, data: d.monthly.map((m) => m.hit2), lineStyle: { color: "#3fdd8f" }, itemStyle: { color: "#3fdd8f" }, symbolSize: 5 },
    ],
  });
  const callCols = [
    { key: "ticker", label: "Token", fmt: tokenLink },
    { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.peak_mc) },
    { key: kind === "caller" ? "group" : "caller", label: kind === "caller" ? "Group" : "Caller",
      fmt: (r) => kind === "caller"
        ? `<a href="#/group/${encodeURIComponent(r.group)}">${esc(r.group)}</a>`
        : `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
    { key: "called_at", label: "When", num: true, fmt: (r) => `<span style="color:var(--muted)">${ago(r.called_at)}</span>` },
  ];
  $("best").append(table(callCols, d.best, { defaultSort: "mult" }));
  $("recent").append(table(callCols, d.recent, { defaultSort: "called_at" }));
  if (kind === "caller") {
    $("side").append(table([
      { key: "0", label: "Group", fmt: (r) => esc(r[0]) },
      { key: "1", label: "Calls", num: true, fmt: (r) => r[1] },
    ], d.groups));
  } else {
    $("side").append(table([
      { key: "name", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.key ?? r.name)}">${esc(r.name)}</a>` },
      { key: "calls", label: "Calls", num: true },
      { key: "hit2", label: "≥2×", num: true, fmt: (r) => fmtPct(r.hit2) },
      { key: "avg_mult", label: "Avg ×", num: true, fmt: (r) => fmtMult(r.avg_mult) },
    ], d.top_callers, { defaultSort: "calls" }));
  }
}

/* ---------------- router ---------------- */
const titles = { overview: "Overview", callers: "Best callers", groups: "Best groups", calls: "Call explorer", sources: "Scan sources", token: "Token", caller: "Caller profile", group: "Group profile" };

async function render() {
  charts.forEach((c) => c.dispose()); charts.length = 0;
  const hash = location.hash.slice(2) || "";
  const [pathPart, queryPart] = hash.split("?");
  const [page, arg] = pathPart.split("/").map(decodeURIComponent);
  const name = page || "overview";
  const fn = pages[name] ?? pages.overview;
  $("title").textContent = titles[name] ?? "Overview";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name || (name === "overview" && a.dataset.page === "overview")));
  view.innerHTML = `<div class="loading">Loading…</div>`;
  try { await fn(new URLSearchParams(queryPart ?? ""), arg); }
  catch (e) { view.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", render);
render();

/* keyboard shortcuts */
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "/") { e.preventDefault(); $("f-search").focus(); }
  const map = { 1: "#/", 2: "#/callers", 3: "#/groups", 4: "#/calls", 5: "#/sources" };
  if (map[e.key]) location.hash = map[e.key];
});

/* ---------------- live CA feed: floating, draggable, resizable ---------------- */
(function liveFeed() {
  const ls = (k, v) => v === undefined
    ? JSON.parse(localStorage.getItem("live_" + k) ?? "null")
    : localStorage.setItem("live_" + k, JSON.stringify(v));
  const el = document.createElement("div");
  el.id = "live";
  el.innerHTML = `<div id="live-head"><span class="dot"></span>LIVE CA FEED
    <button id="live-collapse" title="collapse">—</button></div>
    <div id="live-body"><div class="empty">Loading…</div></div>`;
  document.body.append(el);
  const head = el.querySelector("#live-head"), body = el.querySelector("#live-body"),
    btn = el.querySelector("#live-collapse");

  const pos = ls("pos") ?? { x: innerWidth - 380, y: innerHeight - 330 };
  const size = ls("size") ?? { w: 360, h: 290 };
  let collapsed = ls("col") ?? false;
  const apply = () => {
    el.style.left = Math.max(0, Math.min(pos.x, innerWidth - 80)) + "px";
    el.style.top = Math.max(0, Math.min(pos.y, innerHeight - 36)) + "px";
    el.style.width = size.w + "px";
    el.style.height = collapsed ? "auto" : size.h + "px";
    el.style.resize = collapsed ? "none" : "both";
    body.style.display = collapsed ? "none" : "";
    btn.textContent = collapsed ? "+" : "—";
  };
  apply();
  btn.onclick = () => { collapsed = !collapsed; ls("col", collapsed); apply(); };

  head.addEventListener("pointerdown", (e) => {
    if (e.target === btn) return;
    e.preventDefault();
    const sx = e.clientX - pos.x, sy = e.clientY - pos.y;
    const move = (ev) => { pos.x = ev.clientX - sx; pos.y = ev.clientY - sy; apply(); };
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); ls("pos", pos); };
    addEventListener("pointermove", move); addEventListener("pointerup", up);
  });
  new ResizeObserver(() => {
    if (!collapsed && el.offsetWidth) { size.w = el.offsetWidth; size.h = el.offsetHeight; ls("size", size); }
  }).observe(el);

  const seen = new Set();
  let first = true;
  async function refresh() {
    try {
      const d = await api("calls", { per: 20 });
      const rows = d.rows ?? [];
      body.innerHTML = rows.map((r) => {
        const key = r.address + "|" + r.group + "|" + r.called_at;
        const isNew = !first && !seen.has(key);
        seen.add(key);
        return `<div class="live-row ${isNew ? "new" : ""}"><span class="t">${ago(r.called_at)}</span>
          ${tokenLink(r)} <span style="color:var(--muted)">${fmtMc(r.first_mc)}</span>
          <span class="who">${esc(r.caller)} · ${esc(r.group)}</span></div>`;
      }).join("") || `<div class="empty">No calls yet.</div>`;
      first = false;
    } catch { /* keep last content on transient errors */ }
  }
  refresh(); setInterval(refresh, 15000);
})();

/* health indicator */
async function health() {
  try {
    const h = await api("health");
    const ver = h.version === VERSION ? `v${VERSION}`
      : `<span class="warn">ui v${VERSION} / api v${h.version} — hard refresh</span>`;
    $("health").innerHTML = `<span class="dot"></span>${ver} · ${h.calls} calls · ${h.tokens} tokens · ingest ${h.ingest_lag_s}s ago`;
  } catch { $("health").innerHTML = `<span class="dot" style="background:var(--red)"></span>API down`; }
}
health(); setInterval(health, 30000);
