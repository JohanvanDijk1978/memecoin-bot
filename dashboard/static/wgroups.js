/* memedash — Wallet Groups
 *
 * Loaded on demand by app.js (`import("./wgroups.js")`) and handed the shared
 * helper bundle, so this page can use the dashboard's formatting without
 * app.js and this file importing each other.
 *
 * The cards are treated as live alerts, not as a table: each token owns a DOM
 * node that is created once, updated in place every tick, and animated out
 * when the token stops qualifying. Sorting moves cards with CSS `order` rather
 * than re-inserting them, because re-inserting a node restarts its animation
 * and makes a quiet refresh look like a burst of new signals.
 */

/* Cache-busted by the `?v=` on app.js's import of this file — that is the
   fourth place the version has to be bumped, alongside main.py, app.js and
   the `?v=` in index.html. */
let MD = null;                 // helper bundle from app.js
let groups = [];
let active = +(localStorage.getItem("wg_group") || 0);
let sortKey = localStorage.getItem("wg_sort") || "wallets";
let minWallets = +(localStorage.getItem("wg_min") || 2);
let timer = null;
const cards = new Map();       // token address -> {el, data}
let lastPayload = null;

const SORTS = [
  ["wallets", "Most wallets holding"],
  ["wallets_asc", "Fewest wallets holding"],
  ["supply", "Highest combined supply %"],
  ["pnl", "Highest PnL"],
  ["pnl_asc", "Lowest PnL"],
  ["mc", "Highest market cap"],
  ["mc_asc", "Lowest market cap"],
  ["detected", "Recently detected"],
  ["position", "Largest combined position"],
];

/* ---------------- formatting ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const money = (v, digits) => {
  if (v == null || !isFinite(v)) return "—";
  const abs = Math.abs(v);
  const d = digits ?? (abs < 10 ? 2 : 0);
  return (v < 0 ? "-$" : "$") + abs.toLocaleString("en-US", { maximumFractionDigits: d });
};
const signed = (v) => {
  if (v == null || !isFinite(v)) return "—";
  return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toLocaleString("en-US",
    { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 0 });
};
const pctText = (v, dp = 2) => v == null || !isFinite(v) ? "—" : `${v.toFixed(dp)}%`;
const signedPct = (v) => v == null || !isFinite(v) ? "" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
const cls = (v) => v == null ? "" : v >= 0 ? "pos" : "neg";

const fmtPrice = (v) => {
  if (!v || !isFinite(v)) return "—";
  if (v >= 1) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  const d = Math.min(12, Math.max(2, 3 - Math.floor(Math.log10(v))));
  return "$" + v.toFixed(d);
};
const fmtAmt = (v) => {
  if (!v || !isFinite(v)) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
};
const shortCa = (a) => a.length > 12 ? `${a.slice(0, 5)}…${a.slice(-4)}` : a;

/* Where a number came from, said plainly. The page never shows a PnL it
   cannot stand behind — an unknown basis renders as "—", never as zero. */
const BASIS_NOTE = {
  chain: "Average entry from this wallet's full swap history",
  partial: "Average entry from swap history that is incomplete or priced at today's SOL/ETH price",
  observed: "Average entry from buys this dashboard watched happen",
  "pre-existing": "This position predates tracking and no swap history was readable — entry unknown",
  unknown: "A balance moved while the token had no price, so the basis is not trustworthy",
};
const BASIS_MARK = { chain: "", partial: "≈", observed: "◷", "pre-existing": "?", unknown: "?" };

/* ---------------- data ---------------- */
async function loadGroups() {
  const d = await MD.api("wgroups");
  groups = d.groups ?? [];
  if (!groups.some((g) => g.id === active)) active = groups[0]?.id ?? 0;
  return groups;
}

const sortValue = (t) => ({
  wallets: [t.holders_n, t.position_usd],
  wallets_asc: [-t.holders_n, -t.position_usd],
  supply: [t.supply_pct ?? -1],
  pnl: [t.pnl_usd ?? -Infinity],
  pnl_asc: [-(t.pnl_usd ?? Infinity)],
  mc: [t.mc ?? 0],
  mc_asc: [-(t.mc || Infinity)],
  detected: [t.detected_at ?? 0],
  position: [t.position_usd ?? 0],
}[sortKey] ?? [t.holders_n]);

/* ---------------- page ---------------- */
export async function page(view, helpers) {
  MD = helpers;
  cards.clear();
  await loadGroups();

  if (!groups.length) {
    view.innerHTML = `
      <div class="wg-empty panel">
        <div class="wg-empty-mark">◎</div>
        <h2>No wallet groups yet</h2>
        <p>Track a set of wallets together and this page shows only the memecoins
           <b>two or more of them hold at the same time</b> — who is in, how much
           supply each controls, and what each one is up or down.</p>
        <button class="wg-primary" id="wg-new">+ Create wallet group</button>
      </div>`;
    document.getElementById("wg-new").onclick = () => openEditor(null, view);
    return;
  }

  view.innerHTML = `
    <div id="wg-root">
      <div class="wg-bar">
        <select id="wg-pick">${groups.map((g) =>
          `<option value="${g.id}" ${g.id === active ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select>
        <button class="wg-ghost" id="wg-edit" title="Edit this group">Edit</button>
        <button class="wg-ghost" id="wg-del" title="Delete this group">Delete</button>
        <button class="wg-primary" id="wg-new">+ Create wallet group</button>
        <span class="wg-live" id="wg-live"><span class="dot"></span><span id="wg-live-t">connecting…</span></span>
      </div>
      <div class="wg-summary" id="wg-summary"></div>
      <div class="wg-notes" id="wg-notes"></div>
      <div class="wg-controls">
        <label class="wg-lab">Sort
          <select id="wg-sort">${SORTS.map(([k, l]) =>
            `<option value="${k}" ${k === sortKey ? "selected" : ""}>${l}</option>`).join("")}</select>
        </label>
        <span class="wg-lab">Held by at least</span>
        <div class="wg-pills" id="wg-min">${[2, 3, 4, 5].map((n) =>
          `<button data-n="${n}" class="${n === minWallets ? "on" : ""}">${n}${n === 5 ? "+" : ""}</button>`).join("")}</div>
      </div>
      <div class="wg-cards" id="wg-cards"><div class="loading">Scanning wallets…</div></div>
    </div>`;

  document.getElementById("wg-pick").onchange = (e) => {
    active = +e.target.value; localStorage.setItem("wg_group", active);
    cards.clear(); document.getElementById("wg-cards").innerHTML = `<div class="loading">Scanning wallets…</div>`;
    tick();
  };
  document.getElementById("wg-new").onclick = () => openEditor(null, view);
  document.getElementById("wg-edit").onclick = () =>
    openEditor(groups.find((g) => g.id === active), view);
  document.getElementById("wg-del").onclick = () => removeGroup(view);
  document.getElementById("wg-sort").onchange = (e) => {
    sortKey = e.target.value; localStorage.setItem("wg_sort", sortKey); applyOrder();
  };
  document.getElementById("wg-min").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    minWallets = +b.dataset.n; localStorage.setItem("wg_min", minWallets);
    document.querySelectorAll("#wg-min button").forEach((x) =>
      x.classList.toggle("on", +x.dataset.n === minWallets));
    draw(lastPayload);
  };

  clearInterval(timer);
  timer = setInterval(tick, 4000);
  MD.liveES?.addEventListener("message", onPush);
  tick();
}

function onPush(e) {
  if (e.data === "wg" && document.getElementById("wg-root")) tick();
}

async function tick() {
  const root = document.getElementById("wg-root");
  if (!root) {                       // navigated away — stop the page cleanly
    clearInterval(timer); timer = null;
    MD.liveES?.removeEventListener("message", onPush);
    return;
  }
  if (!active) return;
  try {
    const d = await MD.api(`wgroups/${active}/live`);
    lastPayload = d;
    draw(d);
  } catch (e) {
    const live = document.getElementById("wg-live-t");
    if (live) live.textContent = "API error";
  }
}

/* ---------------- rendering ---------------- */
function draw(d) {
  if (!d) return;
  const s = d.summary;
  const shown = (d.tokens ?? []).filter((t) => t.holders_n >= minWallets);

  document.getElementById("wg-summary").innerHTML = `
    <b>${s.wallets}</b> wallet${s.wallets === 1 ? "" : "s"} tracked
    <span class="sep">·</span> <b>${d.tokens.length}</b> shared memecoin${d.tokens.length === 1 ? "" : "s"} detected
    <span class="sep">·</span> <b class="${s.new_1h ? "pos" : ""}">${s.new_1h}</b> new in the last hour
    ${minWallets > 2 ? `<span class="sep">·</span> <span class="muted">${shown.length} shown at ${minWallets}+ wallets</span>` : ""}`;

  const notes = document.getElementById("wg-notes");
  notes.innerHTML = (s.notes ?? []).map((n) => `<div>ⓘ ${esc(n)}</div>`).join("")
    + (s.error ? `<div class="warn">last scan failed: ${esc(s.error)}</div>` : "");

  const live = document.getElementById("wg-live-t");
  live.textContent = s.scanning ? "scanning wallets…"
    : s.scanned_at ? `wallets checked ${MD.ago(s.scanned_at)} · every ${Math.round(s.interval)}s`
      : "first scan pending…";
  document.getElementById("wg-live").classList.toggle("busy", !!s.scanning);

  const wrap = document.getElementById("wg-cards");
  wrap.querySelector(".loading")?.remove();
  wrap.querySelector(".wg-none")?.remove();

  const keep = new Set(shown.map((t) => t.address));
  for (const [addr, entry] of [...cards]) {
    if (keep.has(addr)) continue;
    cards.delete(addr);
    entry.el.classList.add("wg-out");          // let it play out before it goes
    setTimeout(() => entry.el.remove(), 420);
  }
  for (const t of shown) {
    const entry = cards.get(t.address);
    if (entry) {
      paint(entry.el, t, entry.data);
      entry.data = t;
    } else {
      const el = document.createElement("article");
      el.className = "wg-card wg-in";
      el.dataset.addr = t.address;
      paint(el, t, null);
      wrap.append(el);
      cards.set(t.address, { el, data: t });
      setTimeout(() => el.classList.remove("wg-in"), 700);
    }
  }
  applyOrder();
  if (!shown.length && !wrap.querySelector(".wg-card")) {
    wrap.insertAdjacentHTML("beforeend", `<div class="wg-none">
      ${d.tokens.length ? `No token is held by ${minWallets}+ wallets right now.`
        : "No two tracked wallets are in the same memecoin right now."}
      <div class="muted">Cards appear here the moment a second wallet buys in.</div></div>`);
  }
}

function applyOrder() {
  const ranked = [...cards.values()].sort((a, b) => {
    const va = sortValue(a.data), vb = sortValue(b.data);
    for (let i = 0; i < Math.max(va.length, vb.length); i++) {
      const x = (vb[i] ?? 0) - (va[i] ?? 0);
      if (x) return x;
    }
    return 0;
  });
  ranked.forEach((entry, i) => { entry.el.style.order = i; });
}

function paint(el, t, prev) {
  const fresh = t.detected_at && Date.now() / 1000 - t.detected_at < 600;
  const known = t.pnl_usd != null;
  const partial = t.priced_n < t.holders_n;
  const seenIds = new Set((prev?.wallets ?? []).map((w) => w.wallet_id));

  el.innerHTML = `
    <header class="wg-head">
      ${t.image ? `<img class="wg-logo" loading="lazy" src="${esc(t.image)}" onerror="this.remove()">`
        : `<div class="wg-logo wg-logo-x">${esc((t.symbol || "?").slice(0, 2))}</div>`}
      <div class="wg-id">
        <div class="wg-title">
          <a href="${MD.padre(t.address, t.chain_id)}" target="_blank" rel="noopener">$${esc(t.symbol || "?")}</a>
          <span class="wg-sub">${esc(t.name || "")}</span>
          ${fresh ? `<span class="wg-new">NEW</span>` : ""}
        </div>
        <div class="wg-ca">
          <span class="mono" title="${esc(t.address)}">${esc(shortCa(t.address))}</span>
          <button class="wg-copy" data-ca="${esc(t.address)}" title="Copy contract address">⧉</button>
          ${t.chain_id ? `<span class="badge ${esc(t.chain_id)}">${esc(t.chain_id)}</span>` : ""}
          <a href="#/token/${esc(t.address)}" class="wg-info" title="Token page in memedash">ⓘ</a>
        </div>
      </div>
      <div class="wg-count" title="${t.holders_n} of ${t.wallets_total} tracked wallets hold this">
        <b>${t.holders_n}</b><span>/${t.wallets_total} wallets</span>
      </div>
    </header>

    <div class="wg-stats">
      <div><span class="k">Price</span><span class="v">${fmtPrice(t.price)}</span></div>
      <div><span class="k">Market cap</span><span class="v">${MD.fmtMc(t.mc)}</span></div>
      <div><span class="k">Supply held</span><span class="v accent">${pctText(t.supply_pct)}</span></div>
      <div><span class="k">Position</span><span class="v">${money(t.position_usd)}</span></div>
      <div><span class="k">Combined PnL</span><span class="v ${cls(t.pnl_usd)}">${
        known ? `${signed(t.pnl_usd)} <small>${signedPct(t.pnl_pct)}</small>` : "—"}</span></div>
    </div>

    <table class="wg-tbl">
      <thead><tr>
        <th>Wallet</th><th class="num">Supply held</th><th class="num">Amount</th>
        <th class="num">Position</th><th class="num">Avg entry</th><th class="num">PnL</th>
      </tr></thead>
      <tbody>${t.wallets.map((w) => `
        <tr class="${seenIds.size && !seenIds.has(w.wallet_id) ? "wg-row-in" : ""}">
          <td><span class="wg-wname">${esc(w.label)}</span>
              <span class="mono wg-waddr" title="${esc(w.address)}">${esc(w.short)}</span></td>
          <td class="num">${pctText(w.supply_pct)}</td>
          <td class="num">${fmtAmt(w.amount)}</td>
          <td class="num">${money(w.value_usd)}</td>
          <td class="num" title="${esc(BASIS_NOTE[w.basis] ?? "")}">${
            w.avg_entry ? fmtPrice(w.avg_entry) : "—"}<span class="wg-mark">${BASIS_MARK[w.basis] ?? ""}</span></td>
          <td class="num ${cls(w.pnl_usd)}">${w.pnl_usd != null
            ? `${signed(w.pnl_usd)} <small>${signedPct(w.pnl_pct)}</small>`
            : `<span class="wg-unknown" title="${esc(BASIS_NOTE[w.basis] ?? "")}">—</span>`}</td>
        </tr>`).join("")}</tbody>
    </table>

    <footer class="wg-foot">
      Combined: <b>${pctText(t.supply_pct)}</b> of supply held
      <span class="sep">·</span> <b>${money(t.position_usd)}</b> position value
      <span class="sep">·</span> <b class="${cls(t.pnl_usd)}">${known ? signed(t.pnl_usd) : "—"}</b> PnL
      ${known && partial ? `<span class="wg-caveat" title="Entry price is unknown for ${
        t.holders_n - t.priced_n} of these wallets, so they are not in the combined PnL">
        · ${t.priced_n}/${t.holders_n} wallets priced</span>` : ""}
      <span class="wg-when">${t.detected_at ? `detected ${MD.ago(t.detected_at)}` : ""}</span>
    </footer>`;

  el.querySelector(".wg-copy").onclick = (e) => {
    navigator.clipboard?.writeText(e.target.dataset.ca);
    e.target.textContent = "✓";
    setTimeout(() => { e.target.textContent = "⧉"; }, 1200);
  };
  if (prev && prev.position_usd !== t.position_usd) {   // a balance or price moved
    el.classList.add("wg-tick");
    setTimeout(() => el.classList.remove("wg-tick"), 900);
  }
}

/* ---------------- group editor ---------------- */
function openEditor(group, view) {
  const rows = group
    ? group.wallets.map((w) => ({ address: w.address, label: w.label }))
    : [{ address: "", label: "" }, { address: "", label: "" }];

  const overlay = document.createElement("div");
  overlay.className = "wg-modal";
  overlay.innerHTML = `
    <div class="wg-panel" role="dialog" aria-label="Wallet group">
      <h3>${group ? "Edit wallet group" : "New wallet group"}</h3>
      <label class="wg-field"><span>Group name</span>
        <input id="wg-f-name" type="text" maxlength="60" placeholder="Smart money"
               value="${esc(group?.name ?? "")}"></label>
      <div class="wg-field"><span>Wallets — a card appears when two of them hold the same token</span></div>
      <div id="wg-rows"></div>
      <button class="wg-ghost wg-add" id="wg-add">+ Add wallet</button>
      <div class="wg-err" id="wg-err"></div>
      <div class="wg-actions">
        <button class="wg-ghost" id="wg-cancel">Cancel</button>
        <button class="wg-primary" id="wg-save">${group ? "Save changes" : "Create group"}</button>
      </div>
    </div>`;
  document.body.append(overlay);

  const rowsEl = overlay.querySelector("#wg-rows");
  const read = () => [...rowsEl.querySelectorAll(".wg-row")].map((r) => ({
    address: r.querySelector(".wg-a").value.trim(),
    label: r.querySelector(".wg-l").value.trim(),
  }));
  const drawRows = (list) => {
    rowsEl.innerHTML = list.map((w, i) => `
      <div class="wg-row">
        <input class="wg-a mono" type="text" spellcheck="false" placeholder="Solana or 0x… address"
               value="${esc(w.address)}">
        <input class="wg-l" type="text" maxlength="40" placeholder="Name, e.g. Whale 3" value="${esc(w.label)}">
        <button class="wg-x" data-i="${i}" title="Remove">✕</button>
      </div>`).join("");
    rowsEl.querySelectorAll(".wg-x").forEach((b) => b.onclick = () => {
      const cur = read();
      cur.splice(+b.dataset.i, 1);
      drawRows(cur.length ? cur : [{ address: "", label: "" }]);
    });
  };
  drawRows(rows);

  const close = () => overlay.remove();
  overlay.querySelector("#wg-cancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  overlay.querySelector("#wg-add").onclick = () => drawRows([...read(), { address: "", label: "" }]);

  overlay.querySelector("#wg-save").onclick = async () => {
    const err = overlay.querySelector("#wg-err");
    const wallets = read().filter((w) => w.address);
    if (wallets.length < 2) {
      err.textContent = "A wallet group needs at least 2 wallets — that is what a shared holding means.";
      return;
    }
    const body = { name: overlay.querySelector("#wg-f-name").value.trim() || "Wallet group", wallets };
    err.textContent = "";
    overlay.querySelector("#wg-save").disabled = true;
    try {
      const r = await fetch(group ? `/api/wgroups/${group.id}` : "/api/wgroups", {
        method: group ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail ?? `save failed (${r.status})`);
      active = data.id;
      localStorage.setItem("wg_group", active);
      close();
      await page(view, MD);
    } catch (e) {
      err.textContent = e.message;
      overlay.querySelector("#wg-save").disabled = false;
    }
  };
  overlay.querySelector("#wg-f-name").focus();
}

async function removeGroup(view) {
  const btn = document.getElementById("wg-del");
  if (btn.dataset.armed !== "1") {           // inline confirm — no blocking dialog
    btn.dataset.armed = "1";
    btn.textContent = "Delete — sure?";
    btn.classList.add("danger");
    setTimeout(() => {
      if (btn.isConnected && btn.dataset.armed === "1") {
        btn.dataset.armed = ""; btn.textContent = "Delete"; btn.classList.remove("danger");
      }
    }, 4000);
    return;
  }
  await fetch(`/api/wgroups/${active}`, { method: "DELETE" });
  active = 0;
  localStorage.removeItem("wg_group");
  await page(view, MD);
}
