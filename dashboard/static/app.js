/* memedash frontend — no build step, ES modules + ECharts (CDN) */
const VERSION = "1.21"; // bump together with VERSION in main.py

const view = document.getElementById("view");
const $ = (id) => document.getElementById(id);

/* ---------------- state (persists across sessions) ---------------- */
const state = {
  days: localStorage.getItem("days") ?? "30",
  chain: localStorage.getItem("chain") ?? "",
};
// migrate pre-1.13 stored filter values (SOL/ETH -> dexscreener chain ids)
state.chain = { SOL: "solana", ETH: "ethereum" }[state.chain] ?? state.chain;
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
const padre = (a, cid) => {
  // real chain of the highest-liquidity pool when known (slug map mirrors utils.py)
  const slug = { solana: "solana", ethereum: "eth", bsc: "bsc", base: "base", robinhood: "robinhood" }[cid]
    || (a.startsWith("0x") ? "eth" : "solana");
  return `https://trade.padre.gg/trade/${slug}/${a}`;
};
const tokenLink = (t) =>
  `<a href="${padre(t.address, t.chain_id)}" target="_blank" rel="noopener"><b>${t.ticker ? "$" + esc(t.ticker) : t.address.slice(0, 6) + "…"}</b></a>` +
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
    fmt: (r) => r.best_call ? `${tokenLink(r.best_call)} ${multPeak(r.best_call.mult, r.best_call.peak_mc)}` : "—" },
  { key: "last_active", label: "Active", num: true, fmt: (r) => `<span style="color:var(--muted)">${ago(r.last_active)}</span>` },
];

/* ---------------- shared live stream (SSE) ---------------- */
let liveES = null;
try { liveES = new EventSource("/api/stream"); } catch { /* fallback polling covers it */ }
liveES?.addEventListener("message", () => ovFeedRefresh());
setInterval(() => ovFeedRefresh(), 30000);

const SRC_LABELS = { telegram: "TG", discord: "DC", dex_watcher: "DEX SOL", dex_watcher_evm: "DEX EVM" };

async function ovFeedRefresh() {
  const el = document.getElementById("ovfeed");
  if (!el) return;  // not on the overview page
  try {
    const d = await api("calls", { per: 25, chain: state.chain });
    el.innerHTML = "";
    el.append(table([
      { key: "called_at", label: "When", fmt: (r) => `<span style="color:var(--muted)">${ago(r.called_at)}</span>` },
      { key: "ticker", label: "Token", fmt: tokenLink },
      { key: "chain", label: "Chain", fmt: (r) => chainBadge(r.chain) },
      { key: "source", label: "Source", fmt: (r) => `<span class="badge">${SRC_LABELS[r.source] ?? esc(r.source)}</span>` },
      { key: "first_mc", label: "MC at post", num: true, fmt: (r) => fmtMc(r.first_mc) },
      { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.eff_peak) },
      { key: "caller", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
      { key: "group", label: "Group" },
    ], d.rows));
  } catch { /* keep last content */ }
}

/* ---------------- pages ---------------- */
const pages = {
  async overview() {
    const d = await api("overview", { days: state.days, chain: state.chain });
    view.innerHTML = kpis(d) + `
      <div class="panel" style="margin-bottom:18px"><h3>Live feed — group calls · dex watcher sol · dex watcher evm</h3>
        <div id="ovfeed"><div class="loading">Loading…</div></div></div>
      <div class="panel"><h3>Top movers — ${$("f-days").selectedOptions[0].text.toLowerCase()}</h3><div id="movers"></div></div>`;
    ovFeedRefresh();
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
    const t = d.token ?? {}, info = d.info ?? {};
    const links = Object.entries(d.links).map(([k, v]) => `<a href="${v}" target="_blank">${k} ↗</a>`).join("");
    const socials = [
      ...(info.websites ?? []).map((w) => ({ label: w.label || "web", url: w.url })),
      ...(info.socials ?? []).map((s) => ({ label: s.type || "link", url: s.url })),
    ].filter((s) => s.url);
    const bannerStyle = info.banner
      ? `style="background-image:linear-gradient(90deg,rgba(10,11,15,.93),rgba(10,11,15,.55)),url('${esc(info.banner)}');background-size:cover;background-position:center"`
      : "";
    const item = (k, v) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    view.innerHTML = `
      <div class="crumb"><a href="#/calls">Explorer</a> / token</div>
      <div class="cards">
        <div class="card" ${bannerStyle}>
          <div class="k">Token</div>
          <div class="v">${info.image ? `<img src="${esc(info.image)}" style="width:22px;height:22px;border-radius:50%;vertical-align:-4px;margin-right:6px">` : ""}${t.ticker ? "$" + esc(t.ticker) : addr.slice(0, 8)} ${chainBadge(t.chain ?? "?")}</div>
          <div class="socials">${socials.map((s) => `<a href="${esc(s.url)}" target="_blank">${esc(s.label)}</a>`).join(" · ") || `<span style="color:var(--dim)">no socials</span>`}</div>
        </div>
        <div class="card"><div class="k">Current MC <button id="mc-refresh" title="refresh from Dexscreener">↻</button></div>
          <div class="v">${t.dead ? `<span class="neg">dead</span>` : fmtMc(t.current_mc)}</div></div>
        ${item("Peak (observed)", fmtMc(Math.max(t.peak_mc_dash ?? 0, ...d.calls.map((c) => c.mc_at_call ?? 0))))}
        ${item("Groups called", d.calls.length)}
        ${item("First called", d.calls[0] ? ago(d.calls[0].called_at) : "—")}
      </div>
      <div class="panel" style="margin-bottom:18px;padding:0;overflow:hidden">
        <div class="tabs" style="padding:10px 12px 0">
          <button class="ctab active" data-c="live">Live chart</button>
          <button class="ctab" data-c="calls">Calls on chart 📍</button>
        </div>
        <div id="chart-live">${info.pair && info.chain_id
          ? `<iframe src="https://dexscreener.com/${esc(info.chain_id)}/${esc(info.pair)}?embed=1&theme=dark&trades=1&tabs=1&info=0" style="width:100%;height:720px;border:0;display:block" loading="lazy"></iframe>`
          : `<div class="empty">No live pool found — chart unavailable.</div>`}</div>
        <div id="chart-calls" style="display:none;padding:12px">
          <div id="cchart" style="height:320px"><div class="loading">Loading candles…</div></div>
        </div>
      </div>
      <div class="panel" style="margin-bottom:18px">
        <div class="tabs">
          <button class="tab active" data-tab="holders">Holders</button>
          <button class="tab" data-tab="traders">Top traders</button>
        </div>
        <div id="tabbody"><div class="loading">Loading…</div></div>
      </div>
      <div class="panel"><h3>Call timeline — earliest caller: <b style="color:var(--text)">${esc(d.earliest ?? "?")}</b>
        <span style="float:right" class="links">${links}</span></h3><div id="t"></div></div>`;
    $("mc-refresh").onclick = () => render();  // page reload re-fetches live from Dexscreener

    // one chart panel, two views: Dexscreener embed <-> calls candlestick
    let ccLoaded = false, doJump = null, pendingJump = null;
    const showChart = (which) => {
      view.querySelectorAll(".ctab").forEach((b) => b.classList.toggle("active", b.dataset.c === which));
      $("chart-live").style.display = which === "live" ? "" : "none";
      $("chart-calls").style.display = which === "calls" ? "" : "none";
      if (which === "calls") loadCallsChart();
    };
    view.querySelectorAll(".ctab").forEach((b) => b.onclick = () => showChart(b.dataset.c));
    window.__ccJump = (ts) => {
      showChart("calls");
      if (doJump) doJump(ts); else pendingJump = ts;
    };

    // candlestick with call markers (GeckoTerminal OHLCV via our API), lazy-built
    async function loadCallsChart() {
      if (ccLoaded) { charts.forEach((c) => c.resize()); return; }
      ccLoaded = true;
      const box = $("cchart");
      const first = d.calls[0]?.called_at ?? Date.now() / 1000 - 86400;
      const span = Date.now() / 1000 - first;
      const tf = span < 20 * 3600 ? "minute" : span < 40 * 86400 ? "hour" : "day";
      const o = await api(`token/${addr}/ohlcv`, { pair: info.pair ?? "", network: info.chain_id ?? "", tf });
      if (!o.candles?.length) {
        box.parentElement.innerHTML = `<div class="empty">No OHLCV history available for this pool.</div>`;
        return;
      }
      const times = o.candles.map((c) => c[0]);
      const idxFor = (ts) => {
        let best = 0;
        times.forEach((t, i) => { if (Math.abs(t - ts) < Math.abs(times[best] - ts)) best = i; });
        return best;
      };
      box.innerHTML = "";
      const ch = chart(box, {
        grid: { left: 60, right: 15, top: 15, bottom: 42 },
        xAxis: { type: "category", ...axis(),
          data: times.map((t) => new Date(t * 1000).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })) },
        yAxis: { type: "value", scale: true, ...axis() },
        dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 4,
          borderColor: "#232734", backgroundColor: "#101218",
          fillerColor: "rgba(124,108,255,.15)", textStyle: { color: "#5c6070" } }],
        series: [{ type: "candlestick",
          data: o.candles.map((c) => [c[1], c[4], c[3], c[2]]),  // o,c,l,h
          itemStyle: { color: "#3fdd8f", color0: "#ff5c6c", borderColor: "#3fdd8f", borderColor0: "#ff5c6c" },
          markPoint: { data: d.calls.map((c) => ({
            coord: [idxFor(c.called_at), o.candles[idxFor(c.called_at)][2]],
            name: c.caller, symbol: "pin", symbolSize: 26,
            itemStyle: { color: "#7c6cff" }, label: { show: false },
          })) } }],
      });
      doJump = (ts) => {
        const i = idxFor(ts);
        ch.dispatchAction({ type: "dataZoom", startValue: Math.max(0, i - 40), endValue: Math.min(times.length - 1, i + 40) });
        ch.dispatchAction({ type: "showTip", seriesIndex: 0, dataIndex: i });
      };
      if (pendingJump != null) { doJump(pendingJump); pendingJump = null; }
    }

    $("t").append(table([
      { key: "jump", label: "", fmt: (r) => `<button class="jbtn" title="show on calls chart" onclick="__ccJump && __ccJump(${r.called_at})">⌖</button>` },
      { key: "called_at", label: "When", fmt: (r) => new Date(r.called_at * 1000).toLocaleString() },
      { key: "caller", label: "Caller", fmt: (r) => `<a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a>` },
      { key: "group", label: "Group", fmt: (r) => `<a href="#/group/${encodeURIComponent(r.group)}">${esc(r.group)}</a>` },
      { key: "source", label: "Src", fmt: (r) => `<span class="badge">${esc(r.source)}</span>` },
      { key: "mc_at_call", label: "MC at call", num: true, fmt: (r) => fmtMc(r.mc_at_call) },
      { key: "mult", label: "Peak ×", num: true, fmt: (r) => multPeak(r.mult, r.mult * r.mc_at_call) },
      { key: "scan_count", label: "Scans", num: true },
    ], d.calls, { defaultSort: "called_at" }));

    const showTab = async (which) => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === which));
      const body = $("tabbody");
      if (which === "traders") {
        body.innerHTML = `<div class="empty">Per-wallet trader stats aren't in any keyless API — see
          <a href="${d.links.gmgn ?? "#"}" target="_blank" style="color:var(--accent)">GMGN ↗</a> or
          <a href="${d.links.axiom ?? "#"}" target="_blank" style="color:var(--accent)">Axiom ↗</a>.
          Add a Birdeye API key to embed them here.</div>`;
        return;
      }
      body.innerHTML = `<div class="loading">Loading…</div>`;
      const h = await api(`token/${addr}/holders`);
      if (h.unsupported) { body.innerHTML = `<div class="empty">${esc(h.reason ?? "unavailable")}</div>`; return; }
      body.innerHTML = "";
      body.append(table([
        { key: "rank", label: "#", num: true },
        { key: "address", label: "Token account", fmt: (r) => `<a class="mono" href="https://solscan.io/account/${r.address}" target="_blank">${r.address.slice(0, 4)}…${r.address.slice(-4)}</a>` },
        { key: "amount", label: "Amount", num: true, fmt: (r) => r.amount >= 1e6 ? (r.amount / 1e6).toFixed(2) + "M" : Math.round(r.amount).toLocaleString() },
        { key: "pct", label: "% supply", num: true, fmt: (r) => r.pct == null ? "—" : `<span class="${r.pct >= 5 ? "warn" : ""}">${r.pct}%</span>` },
      ], h.holders.map((r, i) => ({ ...r, rank: i + 1 })), { defaultSort: "amount" }));
    };
    document.querySelectorAll(".tab").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
    showTab("holders");
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
          <span class="who"><a href="#/caller/${encodeURIComponent(r.caller_key ?? r.caller)}">${esc(r.caller)}</a> · ${esc(r.group)}</span></div>`;
      }).join("") || `<div class="empty">No calls yet.</div>`;
      first = false;
    } catch { /* keep last content on transient errors */ }
  }
  refresh();
  setInterval(refresh, 30000);  // fallback safety net
  liveES?.addEventListener("message", () => refresh());  // instant server push
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
