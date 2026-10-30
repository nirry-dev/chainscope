// Chainscope front-end: force-directed fund-flow graph over the local API.
// Buildless vanilla JS + SVG. Served same-origin by api.py, so API defaults to
// "" (same host); override with ?api=http://host:port if running the UI apart.
const API = new URLSearchParams(location.search).get("api")
  || localStorage.getItem("chainscope_api")
  || "";

const CAT_COLOR = {
  exchange: "#4da3ff", defi: "#22d3ee", mixer: "#ef4444", tumbler: "#ef4444",
  scam: "#fb923c", individual: "#94a3b8", contract: "#a78bfa", bridge: "#f59e0b",
  sanctioned: "#ef4444", darkmarket: "#ef4444", ransomware: "#ef4444", unknown: "#64748b",
};
const FLAGGED = new Set(["mixer", "tumbler", "scam", "sanctioned", "darkmarket", "ransomware"]);

let currentCase = null, chain = "eth";
let nodes = [], edges = [], nodeById = {};
let running = false, frozen = false, raf = null, heat = 1;
let view = { x: 0, y: 0, k: 1 };
let W = 800, H = 600, selected = null;

const svg = document.getElementById("graph");
const SVGNS = "http://www.w3.org/2000/svg";

// ---------- API helpers ----------
async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(API + path, opt);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json();
}
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 2200);
}

// ---------- bootstrap ----------
async function boot() {
  try { await api("/api/health"); setEngine(true); }
  catch { setEngine(false); return; }
  await loadCases();
  await loadSamples();
  await refreshAll();
  sizeSvg();
  window.addEventListener("resize", () => { sizeSvg(); draw(); });
}
function setEngine(ok) {
  const p = document.getElementById("apiState");
  p.textContent = ok ? "engine up" : "engine down";
  p.className = "pill " + (ok ? "pill-on" : "pill-off");
}
async function loadCases() {
  const { cases } = await api("/api/cases");
  const sel = document.getElementById("caseSelect");
  sel.innerHTML = "";
  if (!cases.length) {
    const { id } = await api("/api/cases", "POST", { name: "Investigation 1" });
    currentCase = id;
    const o = document.createElement("option"); o.value = id; o.textContent = "#" + id + " Investigation 1"; sel.appendChild(o);
    return;
  }
  for (const c of cases) {
    const o = document.createElement("option"); o.value = c.id; o.textContent = `#${c.id} ${c.name}`; sel.appendChild(o);
  }
  if (!cases.find(c => c.id === currentCase)) currentCase = cases[0].id;
  sel.value = currentCase;
}
async function loadSamples() {
  const { samples } = await api("/api/samples");
  const sel = document.getElementById("sampleSelect");
  sel.innerHTML = "";
  for (const s of samples) { const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o); }
}

// ---------- data load ----------
async function refreshAll() {
  const g = await api(`/api/cases/${currentCase}/graph?chain=${chain}`);
  ingestGraph(g);
  await loadFlags();
}
function ingestGraph(g) {
  const prev = {}; for (const n of nodes) prev[n.address] = n;
  nodeById = {};
  nodes = (g.nodes || []).map(n => {
    const old = prev[n.address] || {};
    const o = Object.assign({}, n);
    o.x = old.x ?? (W / 2 + (Math.random() - 0.5) * 240);
    o.y = old.y ?? (H / 2 + (Math.random() - 0.5) * 240);
    o.vx = 0; o.vy = 0; o.fx = old.fx; o.fy = old.fy;
    nodeById[o.address] = o; return o;
  });
  edges = (g.edges || []).filter(e => nodeById[e.src] && nodeById[e.dst]);
  computeScales();
  renderLegend();
  reheat();
  draw();
}
function computeScales() {
  let maxVol = 1, maxEdge = 1;
  for (const n of nodes) maxVol = Math.max(maxVol, (n.in_value || 0) + (n.out_value || 0));
  for (const e of edges) maxEdge = Math.max(maxEdge, e.value || 0);
  for (const n of nodes) {
    const v = (n.in_value || 0) + (n.out_value || 0);
    n.r = 8 + 20 * Math.sqrt(v / maxVol);
    n.color = (n.risk === "sanctioned" || FLAGGED.has(n.category)) ? CAT_COLOR.mixer
      : (CAT_COLOR[n.category] || CAT_COLOR.unknown);
  }
  for (const e of edges) {
    e.w = 1 + 5 * ((e.value || 0) / maxEdge);
    const d = nodeById[e.dst];
    e.flagged = d && (FLAGGED.has(d.category) || d.risk === "sanctioned");
  }
}

// ---------- force simulation ----------
function reheat() { if (frozen) return; running = true; heat = 1; if (!raf) raf = requestAnimationFrame(tick); }
function tick() {
  step(); draw(); heat *= 0.985;
  if (heat < 0.02 || frozen) { running = false; raf = null; return; }
  raf = requestAnimationFrame(tick);
}
function step() {
  const cx = W / 2, cy = H / 2;
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 0.01;
      const f = 2600 / d2, d = Math.sqrt(d2), ux = dx / d, uy = dy / d;
      a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
    }
    a.vx += (cx - a.x) * 0.002; a.vy += (cy - a.y) * 0.002;
  }
  for (const e of edges) {
    const a = nodeById[e.src], b = nodeById[e.dst];
    let dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - 120) * 0.02, ux = dx / d, uy = dy / d;
    a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
  }
  for (const n of nodes) {
    if (n.fx != null) { n.x = n.fx; n.y = n.fy; n.vx = 0; n.vy = 0; continue; }
    n.vx *= 0.82 * heat + 0.1; n.vy *= 0.82 * heat + 0.1;
    n.x += n.vx; n.y += n.vy;
  }
}

// ---------- render ----------
function sizeSvg() { const r = svg.getBoundingClientRect(); W = r.width || 800; H = r.height || 600; }
function el(tag, attrs) { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }

function draw() {
  svg.innerHTML = "";
  const defs = el("defs", {});
  for (const col of ["#35507a", "#f87171"]) {
    const m = el("marker", { id: "arw" + col.slice(1), markerWidth: 9, markerHeight: 9,
      refX: 8, refY: 3, orient: "auto", markerUnits: "userSpaceOnUse" });
    m.appendChild(el("path", { d: "M0,0 L8,3 L0,6 Z", fill: col }));
    defs.appendChild(m);
  }
  svg.appendChild(defs);
  const vp = el("g", { transform: `translate(${view.x},${view.y}) scale(${view.k})` });
  svg.appendChild(vp);

  const eg = el("g", {}); vp.appendChild(eg);
  for (const e of edges) {
    const a = nodeById[e.src], b = nodeById[e.dst];
    let dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 1;
    const ux = dx / d, uy = dy / d;
    const x1 = a.x + ux * (a.r + 1), y1 = a.y + uy * (a.r + 1);
    const x2 = b.x - ux * (b.r + 6), y2 = b.y - uy * (b.r + 6);
    const col = e.flagged ? "#f87171" : "#35507a";
    eg.appendChild(el("line", { x1, y1, x2, y2, class: "edge" + (e.flagged ? " flagged" : ""),
      "stroke-width": e.w, "marker-end": `url(#arw${col.slice(1)})` }));
    if (view.k > 0.75) {
      const t = el("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 2, class: "edge-label" });
      t.textContent = `${fmt(e.value)}${e.asset ? " " + e.asset : ""}`;
      eg.appendChild(t);
    }
  }
  const ng = el("g", {}); vp.appendChild(ng);
  for (const n of nodes) {
    const g = el("g", { class: "node" + (n.seed ? " seed" : ""), transform: `translate(${n.x},${n.y})` });
    if (FLAGGED.has(n.category) || ["high", "sanctioned", "medium"].includes(n.risk)) {
      g.appendChild(el("circle", { r: n.r + 3, class: "risk-ring",
        stroke: n.risk === "medium" ? "#fbbf24" : "#ef4444" }));
    }
    g.appendChild(el("circle", { r: n.r, fill: n.color }));
    const label = n.label || short(n.address);
    const t = el("text", { x: 0, y: n.r + 13, "text-anchor": "middle" });
    t.textContent = label.length > 22 ? label.slice(0, 21) + "…" : label;
    g.appendChild(t);
    if (selected === n.address) g.querySelector("circle:last-of-type").setAttribute("stroke", "#fff");
    attachNode(g, n);
    ng.appendChild(g);
  }
}
function renderLegend() {
  const cats = [...new Set(nodes.map(n => n.category || "unknown"))];
  const order = ["exchange", "defi", "mixer", "scam", "bridge", "contract", "individual", "unknown"];
  cats.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  document.getElementById("legend").innerHTML = cats.map(c =>
    `<div class="li"><span class="dot" style="background:${CAT_COLOR[c] || CAT_COLOR.unknown}"></span>${c}</div>`).join("");
}

// ---------- interaction ----------
let drag = null, pan = null;
function attachNode(g, n) {
  g.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); drag = { n, moved: false }; g.setPointerCapture(ev.pointerId); });
  g.addEventListener("pointermove", (ev) => {
    if (!drag || drag.n !== n) return;
    drag.moved = true; const p = toWorld(ev);
    n.fx = p.x; n.fy = p.y; n.x = p.x; n.y = p.y; reheat();
  });
  g.addEventListener("pointerup", () => {
    if (drag && drag.n === n) { if (!drag.moved) selectNode(n.address); n.fx = null; n.fy = null; }
    drag = null;
  });
}
svg.addEventListener("pointerdown", (ev) => { pan = { sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y }; svg.classList.add("panning"); });
svg.addEventListener("pointermove", (ev) => { if (!pan) return; view.x = pan.vx + (ev.clientX - pan.sx); view.y = pan.vy + (ev.clientY - pan.sy); draw(); });
window.addEventListener("pointerup", () => { pan = null; svg.classList.remove("panning"); });
svg.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const scale = ev.deltaY < 0 ? 1.1 : 0.9, m = svgPoint(ev);
  view.x = m.x - (m.x - view.x) * scale; view.y = m.y - (m.y - view.y) * scale; view.k *= scale; draw();
}, { passive: false });
function svgPoint(ev) { const r = svg.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }
function toWorld(ev) { const p = svgPoint(ev); return { x: (p.x - view.x) / view.k, y: (p.y - view.y) / view.k }; }

function fit() {
  if (!nodes.length) return;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of nodes) { minx = Math.min(minx, n.x); miny = Math.min(miny, n.y); maxx = Math.max(maxx, n.x); maxy = Math.max(maxy, n.y); }
  const pad = 70, gw = (maxx - minx) || 1, gh = (maxy - miny) || 1;
  view.k = Math.min((W - pad) / gw, (H - pad) / gh, 2);
  view.x = W / 2 - ((minx + maxx) / 2) * view.k;
  view.y = H / 2 - ((miny + maxy) / 2) * view.k;
  draw();
}

// ---------- inspector ----------
async function selectNode(address) {
  selected = address;
  document.getElementById("seedAddr").value = address;
  const info = await api(`/api/cases/${currentCase}/address?chain=${chain}&address=${encodeURIComponent(address)}`);
  renderInspector(info); draw();
}
function renderInspector(a) {
  const box = document.getElementById("inspector");
  if (!a || a.error) { box.innerHTML = `<div class="empty">${a && a.error ? a.error : "not found"}</div>`; return; }
  const m = a.metrics || {};
  const badges = [];
  if (a.category) badges.push(`<span class="badge">${a.category}</span>`);
  if (a.risk && a.risk !== "none") badges.push(`<span class="badge risk-${a.risk}">risk: ${a.risk}</span>`);
  if (a.entity) badges.push(`<span class="badge">${esc(a.entity)}</span>`);
  (a.tags || []).forEach(t => badges.push(`<span class="badge">#${esc(t)}</span>`));
  const cps = (a.counterparties || []).map(c => `
    <tr>
      <td><span class="cp-addr" data-a="${c.address}">${c.label ? esc(c.label) : short(c.address)}</span>
        ${c.risk && c.risk !== "none" ? `<span class="badge risk-${c.risk}">${c.risk}</span>` : ""}</td>
      <td class="num">${c.recv ? "+" + fmt(c.recv) : ""}</td>
      <td class="num">${c.sent ? "−" + fmt(c.sent) : ""}</td>
    </tr>`).join("");
  box.innerHTML = `
    <div class="insp-label">${a.label ? esc(a.label) : "Unlabeled wallet"}</div>
    <div class="insp-addr">${a.address}</div>
    <div style="margin-top:8px">${badges.join(" ")}</div>
    <div class="kpi-row">
      <div class="kpi"><div class="v">${fmt(a.balance)}</div><div class="k">balance</div></div>
      <div class="kpi"><div class="v">${m.tx_count ?? a.tx_count ?? 0}</div><div class="k">txns</div></div>
      <div class="kpi"><div class="v">${m.counterparties || 0}</div><div class="k">peers</div></div>
    </div>
    <div class="kpi-row">
      <div class="kpi"><div class="v" style="color:var(--ok)">+${fmt(m.in_value)}</div><div class="k">received</div></div>
      <div class="kpi"><div class="v" style="color:var(--warn)">−${fmt(m.out_value)}</div><div class="k">sent</div></div>
    </div>
    <h3>Counterparties</h3>
    <table class="cp-tbl"><thead><tr><th>address</th><th class="num">in</th><th class="num">out</th></tr></thead>
      <tbody>${cps || '<tr><td colspan="3" class="hint">none</td></tr>'}</tbody></table>
    <button id="traceFromBtn" class="btn primary full" style="margin-top:10px">Trace from this wallet →</button>`;
  box.querySelectorAll(".cp-addr").forEach(x => x.onclick = () => selectNode(x.dataset.a));
  document.getElementById("traceFromBtn").onclick = () => { document.getElementById("seedAddr").value = a.address; doTrace(); };
}

async function loadFlags() {
  const { flags } = await api(`/api/cases/${currentCase}/flags`);
  const box = document.getElementById("flagsList");
  if (!flags.length) { box.innerHTML = `<div class="hint">No risk flags yet — run a risk scan.</div>`; return; }
  box.innerHTML = flags.map(f => `
    <div class="finding sev-${f.severity}">
      <div class="ft">${esc(f.title)}</div>
      <div class="fm">${f.severity} · score ${Math.round(f.score || 0)}</div>
    </div>`).join("");
}

// ---------- actions ----------
async function doTrace() {
  const seed = document.getElementById("seedAddr").value.trim();
  if (!seed) return toast("enter a seed address");
  const hops = document.getElementById("traceHops").value;
  const dir = document.getElementById("traceDir").value;
  try {
    const g = await api(`/api/cases/${currentCase}/trace?chain=${chain}&seed=${encodeURIComponent(seed)}&hops=${hops}&direction=${dir}`);
    ingestGraph(g); setTimeout(fit, 400);
    toast(`traced ${g.nodes.length} wallets, ${g.edges.length} flows`);
  } catch (e) { toast("trace failed: " + e.message); }
}

function wire() {
  document.getElementById("caseSelect").onchange = async (e) => { currentCase = +e.target.value; await refreshAll(); setTimeout(fit, 400); };
  document.getElementById("newCaseBtn").onclick = async () => {
    const name = prompt("New investigation name:", "Investigation");
    if (!name) return;
    const { id } = await api("/api/cases", "POST", { name });
    currentCase = id; await loadCases(); await refreshAll(); toast("case #" + id + " created");
  };
  document.getElementById("chainSelect").onchange = async (e) => { chain = e.target.value; await refreshAll(); setTimeout(fit, 400); };
  document.getElementById("loadSampleBtn").onclick = async () => {
    const name = document.getElementById("sampleSelect").value;
    try { const r = await api(`/api/cases/${currentCase}/import`, "POST", { source: "sample", name });
      chain = r.chain; document.getElementById("chainSelect").value = chain;
      await refreshAll(); setTimeout(fit, 400); toast(`loaded ${r.transfers} transfers`);
    } catch (e) { toast("load failed: " + e.message); }
  };
  document.getElementById("liveSource").onchange = (e) => {
    const evm = e.target.value === "evm";
    document.getElementById("evmKey").style.display = evm ? "block" : "none";
    document.getElementById("evmBase").style.display = evm ? "block" : "none";
  };
  document.getElementById("liveBtn").onclick = async () => {
    const address = document.getElementById("liveAddr").value.trim();
    if (!address) return toast("enter an address");
    const source = document.getElementById("liveSource").value;
    const body = { source, address };
    if (source === "evm") { body.api_key = document.getElementById("evmKey").value; body.api_base = document.getElementById("evmBase").value; }
    toast("fetching…");
    try { const r = await api(`/api/cases/${currentCase}/import`, "POST", body);
      chain = r.chain; document.getElementById("chainSelect").value = chain;
      document.getElementById("seedAddr").value = address;
      await refreshAll(); setTimeout(fit, 400); toast(`imported ${r.transfers} transfers`);
    } catch (e) { toast("fetch failed: " + e.message); }
  };
  document.getElementById("traceBtn").onclick = doTrace;
  document.getElementById("showAllBtn").onclick = async () => { await refreshAll(); setTimeout(fit, 400); };
  document.getElementById("riskBtn").onclick = async () => {
    try { const r = await api(`/api/cases/${currentCase}/risk`, "POST", { chain });
      document.getElementById("riskMsg").textContent = `${r.new} new, ${r.merged} merged flags`;
      await loadFlags(); computeScales(); draw(); toast("risk scan complete");
    } catch (e) { toast("risk scan failed: " + e.message); }
  };
  document.getElementById("fitBtn").onclick = fit;
  document.getElementById("freezeBtn").onclick = (e) => {
    frozen = !frozen; e.target.textContent = frozen ? "▶ run" : "⏸ freeze";
    if (!frozen) reheat();
  };
}

// ---------- utils ----------
function short(a) { return a && a.length > 13 ? a.slice(0, 6) + "…" + a.slice(-4) : a; }
function fmt(v) { if (v == null) return "—"; const n = +v; if (!isFinite(n)) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M"; if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return (Math.round(n * 1000) / 1000).toString(); }
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

wire();
boot();
