// Chainscope front-end: force-directed fund-flow graph over the local API.
// In a browser the page is served same-origin by api.py, so API = "". In the
// Tauri desktop shell the page loads from tauri://, so point at the local
// engine the shell spawned on 127.0.0.1:8799. Override with ?api= or localStorage.
const API = new URLSearchParams(location.search).get("api")
  || localStorage.getItem("chainscope_api")
  || (/^https?:$/.test(location.protocol) ? "" : "http://127.0.0.1:8799");

const CAT_COLOR = {
  exchange: "#4da3ff", defi: "#22d3ee", mixer: "#ef4444", tumbler: "#ef4444",
  scam: "#fb923c", individual: "#94a3b8", contract: "#a78bfa", bridge: "#f59e0b",
  sanctioned: "#ef4444", darkmarket: "#ef4444", ransomware: "#ef4444", unknown: "#64748b",
};
const FLAGGED = new Set(["mixer", "tumbler", "scam", "sanctioned", "darkmarket", "ransomware"]);
const SEV_COLOR = { critical: "#ef4444", high: "#f87171", medium: "#fbbf24", low: "#4da3ff" };

let currentCase = null, chain = "eth", asset = "";  // asset "" = all assets
let nodes = [], edges = [], nodeById = {}, adj = {};
let running = false, frozen = false, raf = null, heat = 1;
let view = { x: 0, y: 0, k: 1 };
let W = 800, H = 600, selected = null, hoverAddr = null;
// timeline scrubber state (ms since epoch)
let tlEvents = [], tlMin = 0, tlMax = 0, tlCursor = 0, tlPlaying = false, tlTimer = null, tlFetch = null, tlScrub = false;

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
  p.className = "status " + (ok ? "status-on" : "status-off");
  p.querySelector(".status-t").textContent = ok ? "engine up" : "engine down";
}
async function loadCases() {
  const { cases } = await api("/api/cases");
  const sel = document.getElementById("caseSelect");
  sel.innerHTML = "";
  if (!cases.length) {
    const { id } = await api("/api/cases", "POST", { name: "Investigation 1" });
    currentCase = id;
    sel.appendChild(opt(id, "#" + id + " Investigation 1")); return;
  }
  for (const c of cases) sel.appendChild(opt(c.id, `#${c.id} ${c.name}`));
  if (!cases.find(c => c.id === currentCase)) currentCase = cases[0].id;
  sel.value = currentCase;
}
async function loadSamples() {
  const { samples } = await api("/api/samples");
  const sel = document.getElementById("sampleSelect");
  sel.innerHTML = "";
  for (const s of samples) sel.appendChild(opt(s, s));
}
function opt(v, t) { const o = document.createElement("option"); o.value = v; o.textContent = t; return o; }

// ---------- data load ----------
async function refreshAll() {
  const g = await api(`/api/cases/${currentCase}/graph?chain=${chain}&asset=${encodeURIComponent(asset)}`);
  populateAssets(g.assets || []);
  ingestGraph(g);
  await loadFlags();
  await loadTimeline();
}
function populateAssets(assets) {
  const sel = document.getElementById("assetSelect");
  const cur = asset;
  sel.innerHTML = '<option value="">all assets</option>' +
    assets.map(a => `<option value="${esc(a.asset)}">${esc(a.asset)} (${a.n})</option>`).join("");
  if (assets.find(a => a.asset === cur) || cur === "") sel.value = cur; else { asset = ""; sel.value = ""; }
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
  adj = {};
  for (const e of edges) {
    (adj[e.src] = adj[e.src] || new Set()).add(e.dst);
    (adj[e.dst] = adj[e.dst] || new Set()).add(e.src);
  }
  if (selected && !nodeById[selected]) { selected = null; renderInspectorEmpty(); }
  computeScales();
  renderLegend();
  reheat();
  draw();
}
function computeScales() {
  // With a single asset selected, scale by value; across all assets values
  // aren't comparable (2M USDC vs 200 ETH), so scale by transfer count instead.
  const byValue = !!asset;
  let maxNode = 1, maxEdge = 1;
  for (const n of nodes) maxNode = Math.max(maxNode, byValue ? (n.in_value || 0) + (n.out_value || 0) : (n.tx_count || 0));
  for (const e of edges) maxEdge = Math.max(maxEdge, byValue ? (e.value || 0) : (e.count || 0));
  for (const n of nodes) {
    const v = byValue ? (n.in_value || 0) + (n.out_value || 0) : (n.tx_count || 0);
    n.r = 9 + 20 * Math.sqrt(v / maxNode);
    n.color = (n.risk === "sanctioned" || FLAGGED.has(n.category)) ? CAT_COLOR.mixer
      : (CAT_COLOR[n.category] || CAT_COLOR.unknown);
  }
  for (const e of edges) {
    e.w = 1.2 + 5 * ((byValue ? (e.value || 0) : (e.count || 0)) / maxEdge);
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
      const f = 2800 / d2, d = Math.sqrt(d2), ux = dx / d, uy = dy / d;
      a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
    }
    a.vx += (cx - a.x) * 0.002; a.vy += (cy - a.y) * 0.002;
  }
  for (const e of edges) {
    const a = nodeById[e.src], b = nodeById[e.dst];
    let dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - 130) * 0.02, ux = dx / d, uy = dy / d;
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

function focusSet() {
  const f = hoverAddr || selected;
  if (!f || !nodeById[f]) return null;
  const keep = new Set([f]);
  if (adj[f]) for (const n of adj[f]) keep.add(n);
  return { f, keep };
}

function draw() {
  const foc = focusSet();
  svg.innerHTML = "";
  const defs = el("defs", {});
  for (const col of ["#38507a", "#f87171"]) {
    const m = el("marker", { id: "arw" + col.slice(1), markerWidth: 8, markerHeight: 8,
      refX: 7, refY: 3, orient: "auto", markerUnits: "userSpaceOnUse" });
    m.appendChild(el("path", { d: "M0,0 L7,3 L0,6 Z", fill: col }));
    defs.appendChild(m);
  }
  svg.appendChild(defs);
  const vp = el("g", { transform: `translate(${view.x},${view.y}) scale(${view.k})` });
  svg.appendChild(vp);

  // edges (curved quadratic beziers)
  const eg = el("g", {}); vp.appendChild(eg);
  for (const e of edges) {
    const a = nodeById[e.src], b = nodeById[e.dst];
    let dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 1;
    const ux = dx / d, uy = dy / d, px = -uy, py = ux;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const curv = Math.min(d * 0.16, 46);
    const qx = mx + px * curv, qy = my + py * curv;
    const sdx = qx - a.x, sdy = qy - a.y, sl = Math.hypot(sdx, sdy) || 1;
    const edx = qx - b.x, edy = qy - b.y, elen = Math.hypot(edx, edy) || 1;
    const x1 = a.x + (sdx / sl) * (a.r + 1), y1 = a.y + (sdy / sl) * (a.r + 1);
    const x2 = b.x + (edx / elen) * (b.r + 6), y2 = b.y + (edy / elen) * (b.r + 6);
    const col = e.flagged ? "#f87171" : "#38507a";
    let cls = "edge" + (e.flagged ? " flagged" : "");
    if (e.on_path) cls += " path";
    else if (foc) cls += (e.src === foc.f || e.dst === foc.f) ? " hot" : " dim";
    const path = el("path", { d: `M${x1},${y1} Q${qx},${qy} ${x2},${y2}`,
      class: cls, "stroke-width": e.w, "marker-end": `url(#arw${col.slice(1)})` });
    eg.appendChild(path);
    const showLbl = (foc && (e.src === foc.f || e.dst === foc.f)) || (!foc && view.k > 1.15);
    if (showLbl) {
      const t = el("text", { x: qx, y: qy - 3, class: "edge-label", "text-anchor": "middle" });
      t.textContent = `${fmt(e.value)}${e.asset ? " " + e.asset : ""}`;
      eg.appendChild(t);
    }
  }

  // nodes
  const ng = el("g", {}); vp.appendChild(ng);
  for (const n of nodes) {
    let cls = "node" + (n.seed ? " seed" : "") + (selected === n.address ? " sel" : "") + (n.on_path ? " path" : "");
    if (foc && !foc.keep.has(n.address)) cls += " dim";
    const g = el("g", { class: cls, transform: `translate(${n.x},${n.y})` });
    g.appendChild(el("circle", { class: "halo", r: n.r + 9, fill: n.color }));
    const isSrc = FLAGGED.has(n.category) || n.risk === "sanctioned" || n.risk === "high";
    if (FLAGGED.has(n.category) || ["high", "sanctioned", "medium"].includes(n.risk)) {
      g.appendChild(el("circle", { r: n.r + 3.5, class: "risk-ring",
        stroke: n.risk === "medium" ? "#fbbf24" : "#ef4444" }));
    }
    if ((n.taint || 0) >= 0.15 && !isSrc) {
      g.appendChild(el("circle", { r: n.r + 6.5, class: "taint-ring", opacity: 0.4 + 0.6 * n.taint }));
    }
    g.appendChild(el("circle", { class: "body", r: n.r, fill: n.color }));
    const label = n.label || short(n.address);
    const t = el("text", { x: 0, y: n.r + 14, "text-anchor": "middle" });
    t.textContent = label.length > 24 ? label.slice(0, 23) + "…" : label;
    g.appendChild(t);
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
const tip = document.getElementById("tooltip");
function attachNode(g, n) {
  g.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); drag = { n, moved: false };
    try { g.setPointerCapture(ev.pointerId); } catch (_) {} });
  g.addEventListener("pointermove", (ev) => {
    if (drag && drag.n === n) {
      drag.moved = true; const p = toWorld(ev);
      n.fx = p.x; n.fy = p.y; n.x = p.x; n.y = p.y; reheat();
    } else { moveTip(ev); }
  });
  g.addEventListener("pointerup", () => {
    if (drag && drag.n === n) { if (!drag.moved) selectNode(n.address); n.fx = null; n.fy = null; }
    drag = null;
  });
  g.addEventListener("pointerenter", (ev) => {
    if (drag) return;
    hoverAddr = n.address; showTip(n, ev); if (!running) draw();
  });
  g.addEventListener("pointerleave", () => {
    if (hoverAddr === n.address) { hoverAddr = null; hideTip(); if (!running) draw(); }
  });
}
function showTip(n, ev) {
  const risk = n.risk && n.risk !== "none" ? ` · risk ${n.risk}` : "";
  const taint = (n.taint || 0) >= 0.15 ? `<br><span style="color:#fb923c">taint ${Math.round(n.taint * 100)}% · ${n.taint_hops}h from ${esc(n.taint_src || "?")}</span>` : "";
  tip.innerHTML = `<div class="tt-label">${esc(n.label || "Unlabeled wallet")}</div>
    <div class="tt-addr">${short(n.address)}</div>
    <div class="tt-meta">${esc(n.category || "unknown")}${risk}<br>
      bal ${fmt(n.balance)} · in ${fmt(n.in_value)} · out ${fmt(n.out_value)}${taint}</div>`;
  tip.classList.add("show"); moveTip(ev);
}
function moveTip(ev) {
  if (!tip.classList.contains("show")) return;
  const r = svg.getBoundingClientRect();
  let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
  if (x + 260 > W) x -= 288; if (y + 90 > H) y -= 100;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.classList.remove("show"); }

svg.addEventListener("pointerleave", () => { if (hoverAddr) { hoverAddr = null; if (!running) draw(); } hideTip(); });
svg.addEventListener("pointerdown", (ev) => { hideTip(); pan = { sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y }; svg.classList.add("panning"); });
svg.addEventListener("pointermove", (ev) => { if (!pan) return; view.x = pan.vx + (ev.clientX - pan.sx); view.y = pan.vy + (ev.clientY - pan.sy); draw(); });
window.addEventListener("pointerup", () => { pan = null; svg.classList.remove("panning"); });
svg.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const m = svgPoint(ev); zoomAround(m.x, m.y, ev.deltaY < 0 ? 1.1 : 0.9);
}, { passive: false });
function svgPoint(ev) { const r = svg.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }
function toWorld(ev) { const p = svgPoint(ev); return { x: (p.x - view.x) / view.k, y: (p.y - view.y) / view.k }; }
function zoomAround(cx, cy, s) {
  const nk = Math.max(0.15, Math.min(6, view.k * s));
  const r = nk / view.k;                 // actual scale after clamping
  view.x = cx - (cx - view.x) * r;
  view.y = cy - (cy - view.y) * r;
  view.k = nk; draw();
}

function fit() {
  if (!nodes.length) return;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of nodes) { minx = Math.min(minx, n.x); miny = Math.min(miny, n.y); maxx = Math.max(maxx, n.x); maxy = Math.max(maxy, n.y); }
  const pad = 90, gw = (maxx - minx) || 1, gh = (maxy - miny) || 1;
  view.k = Math.min((W - pad) / gw, (H - pad) / gh, 2);
  view.x = W / 2 - ((minx + maxx) / 2) * view.k;
  view.y = H / 2 - ((miny + maxy) / 2) * view.k;
  draw();
}
function centerOn(n) {
  view.x = W / 2 - n.x * view.k; view.y = H / 2 - n.y * view.k; draw();
}

// ---------- inspector ----------
async function selectNode(address) {
  selected = address;
  hoverAddr = null; hideTip();
  document.getElementById("seedAddr").value = address;
  draw();
  const info = await api(`/api/cases/${currentCase}/address?chain=${chain}&address=${encodeURIComponent(address)}`);
  renderInspector(info);
}
function renderInspectorEmpty() {
  document.getElementById("inspector").innerHTML =
    `<div class="insp-empty"><div class="insp-empty-art">◈</div>
      <p>Load a dataset, then click any wallet to trace it.</p>
      <button class="btn primary js-load">Load sample</button></div>`;
  wireEmpty();
}
function monoColor(a) { return (a.risk === "sanctioned" || FLAGGED.has(a.category)) ? CAT_COLOR.mixer : (CAT_COLOR[a.category] || CAT_COLOR.unknown); }
function initials(a) {
  const s = (a.entity || a.label || a.address || "?");
  if (s.startsWith("0x") || s.startsWith("bc1") || s.startsWith("1") || s.startsWith("3")) return s.replace(/^0x/, "").slice(0, 2).toUpperCase();
  const p = s.split(/\s+/).filter(Boolean);
  return ((p[0][0] || "") + (p[1] ? p[1][0] : "")).toUpperCase();
}
function renderInspector(a) {
  const box = document.getElementById("inspector");
  if (!a || a.error) { box.innerHTML = `<div class="insp-empty"><p>${a && a.error ? a.error : "not found"}</p></div>`; return; }
  const m = a.metrics || {};
  const badges = [];
  if (a.category) badges.push(`<span class="badge">${esc(a.category)}</span>`);
  if (a.risk && a.risk !== "none") badges.push(`<span class="badge risk-${a.risk}">risk: ${a.risk}</span>`);
  if ((a.taint || 0) >= 0.15) badges.push(`<span class="badge taint">taint ${Math.round(a.taint * 100)}% · ${a.taint_hops}h</span>`);
  if (a.entity) badges.push(`<span class="badge">${esc(a.entity)}</span>`);
  (a.tags || []).forEach(t => badges.push(`<span class="badge">#${esc(t)}</span>`));
  const cps = (a.counterparties || []).map(c => {
    const col = (c.risk === "sanctioned" || FLAGGED.has(c.category)) ? CAT_COLOR.mixer : (CAT_COLOR[c.category] || CAT_COLOR.unknown);
    return `<tr>
      <td><span class="cp-addr" data-a="${c.address}"><span class="cp-dot" style="background:${col}"></span>
        <span class="cp-name">${c.label ? esc(c.label) : short(c.address)}</span></span>
        ${c.risk && c.risk !== "none" ? `<span class="badge risk-${c.risk}">${c.risk}</span>` : ""}</td>
      <td class="num recv">${c.recv ? "+" + fmt(c.recv) : ""}</td>
      <td class="num sent">${c.sent ? "−" + fmt(c.sent) : ""}</td></tr>`;
  }).join("");
  // merge per-asset holdings + in/out flows into one table
  const am = {};
  (a.holdings || []).forEach(h => (am[h.asset] = am[h.asset] || {}).held = h.balance);
  (a.by_asset || []).forEach(b => { const o = am[b.asset] = am[b.asset] || {}; o.recv = b.recv; o.sent = b.sent; });
  const assetRows = Object.keys(am).sort().map(sym => {
    const v = am[sym];
    return `<tr><td><b>${esc(sym)}</b></td>
      <td class="num">${v.held != null ? fmt(v.held) : "—"}</td>
      <td class="num recv">${v.recv ? "+" + fmt(v.recv) : ""}</td>
      <td class="num sent">${v.sent ? "−" + fmt(v.sent) : ""}</td></tr>`;
  }).join("");

  box.className = "inspector insp";
  box.innerHTML = `
    <div class="insp-head">
      <div class="monogram" style="background:${monoColor(a)}">${initials(a)}</div>
      <div class="insp-title">
        <div class="insp-label">${a.label ? esc(a.label) : "Unlabeled wallet"}</div>
        <div class="insp-addr"><span class="a">${a.address}</span>
          <button class="copy" title="Copy address" data-copy="${a.address}">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4">
              <rect x="5" y="5" width="8" height="8" rx="1.5"/><path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2H3.5A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5"/></svg>
          </button></div>
      </div>
    </div>
    <div class="badges">${badges.join("")}</div>
    <div class="kpi-row">
      <div class="kpi"><div class="v">${fmt(a.balance)}</div><div class="k">balance</div></div>
      <div class="kpi"><div class="v">${m.tx_count ?? a.tx_count ?? 0}</div><div class="k">txns</div></div>
      <div class="kpi"><div class="v">${m.counterparties || 0}</div><div class="k">peers</div></div>
    </div>
    <h4>Assets held &amp; moved</h4>
    <table class="cp-tbl"><thead><tr><th>asset</th><th class="num">held</th><th class="num">in</th><th class="num">out</th></tr></thead>
      <tbody>${assetRows || '<tr><td colspan="4" class="hint">none</td></tr>'}</tbody></table>
    <h4>Counterparties</h4>
    <table class="cp-tbl"><thead><tr><th>address</th><th class="num">in</th><th class="num">out</th></tr></thead>
      <tbody>${cps || '<tr><td colspan="3" class="hint">none</td></tr>'}</tbody></table>
    <button id="traceFromBtn" class="btn primary full" style="margin-top:14px">Trace from this wallet →</button>
    <div class="insp-actions">
      <button id="setABtn" class="btn ghost grow" title="Use as path start">Path A ↦</button>
      <button id="setBBtn" class="btn ghost grow" title="Use as path end">↦ Path B</button>
    </div>
    <details class="annotate">
      <summary>✎ Annotate wallet</summary>
      <label>Label</label><input id="anLabel" value="${esc(a.label || "")}" />
      <label>Entity</label><input id="anEntity" value="${esc(a.entity || "")}" />
      <label>Category</label>
      <select id="anCategory">${["", "exchange", "defi", "bridge", "contract", "individual", "mixer", "scam", "sanctioned", "unknown"]
        .map(c => `<option${c === (a.category || "") ? " selected" : ""}>${c}</option>`).join("")}</select>
      <label>Risk</label>
      <select id="anRisk">${["none", "low", "medium", "high", "sanctioned"]
        .map(r => `<option${r === (a.risk || "none") ? " selected" : ""}>${r}</option>`).join("")}</select>
      <label>Tags (comma-separated)</label><input id="anTags" value="${esc((a.tags || []).join(", "))}" />
      <label>Note</label><textarea id="anNote">${esc(a.note || "")}</textarea>
      <button id="anSave" class="btn primary full mt">Save annotation</button>
    </details>`;
  box.querySelectorAll(".cp-addr").forEach(x => x.onclick = () => { selectNode(x.dataset.a); const n = nodeById[x.dataset.a]; if (n) centerOn(n); });
  const cp = box.querySelector(".copy");
  cp.onclick = () => { navigator.clipboard?.writeText(cp.dataset.copy); toast("address copied"); };
  document.getElementById("traceFromBtn").onclick = () => { document.getElementById("seedAddr").value = a.address; doTrace(); };
  document.getElementById("setABtn").onclick = () => { document.getElementById("pathFrom").value = a.address; toast("path start set"); };
  document.getElementById("setBBtn").onclick = () => { document.getElementById("pathTo").value = a.address; toast("path end set"); };
  document.getElementById("anSave").onclick = () => saveAnnotation(a.address);
}

async function saveAnnotation(address) {
  const tags = document.getElementById("anTags").value.split(",").map(s => s.trim()).filter(Boolean);
  const body = {
    address, chain,
    label: document.getElementById("anLabel").value.trim(),
    entity: document.getElementById("anEntity").value.trim(),
    category: document.getElementById("anCategory").value || null,
    risk: document.getElementById("anRisk").value,
    note: document.getElementById("anNote").value,
    tags,
  };
  try {
    await api(`/api/cases/${currentCase}/address/annotate`, "POST", body);
    await refreshAll();
    await selectNode(address);
    toast("annotation saved");
  } catch (e) { toast("save failed: " + e.message); }
}

async function loadFlags() {
  const { flags } = await api(`/api/cases/${currentCase}/flags`);
  const sum = document.getElementById("riskSummary");
  const box = document.getElementById("flagsList");
  const counts = {};
  for (const f of flags) counts[f.severity] = (counts[f.severity] || 0) + 1;
  sum.innerHTML = ["critical", "high", "medium", "low"].filter(s => counts[s])
    .map(s => `<span class="rchip ${s}"><span class="d"></span><b>${counts[s]}</b> ${s}</span>`).join("");
  if (!flags.length) { box.innerHTML = `<div class="hint empty-flags">No risk flags yet — run a scan.</div>`; return; }
  box.innerHTML = flags.map(f => `
    <div class="flag sev-${f.severity}">
      <div class="ft">${esc(f.title)}</div>
      <div class="fm"><span class="sev-pill">${f.severity}</span> score ${Math.round(f.score || 0)}</div>
    </div>`).join("");
}

// ---------- actions ----------
async function doTrace() {
  const seed = document.getElementById("seedAddr").value.trim();
  if (!seed) return toast("enter a seed address");
  const hops = document.getElementById("traceHops").value;
  const dir = document.getElementById("traceDir").value;
  try {
    const g = await api(`/api/cases/${currentCase}/trace?chain=${chain}&seed=${encodeURIComponent(seed)}&hops=${hops}&direction=${dir}&asset=${encodeURIComponent(asset)}`);
    selected = seed; populateAssets(g.assets || []); ingestGraph(g); setTimeout(fit, 450);
    toast(`traced ${g.nodes.length} wallets, ${g.edges.length} flows`);
  } catch (e) { toast("trace failed: " + e.message); }
}
async function loadSample() {
  const name = document.getElementById("sampleSelect").value;
  try {
    const r = await api(`/api/cases/${currentCase}/import`, "POST", { source: "sample", name });
    chain = r.chain; document.getElementById("chainSelect").value = chain;
    await refreshAll(); setTimeout(fit, 450); toast(`loaded ${r.transfers} transfers`);
  } catch (e) { toast("load failed: " + e.message); }
}
async function doTaint() {
  try {
    const r = await api(`/api/cases/${currentCase}/taint`, "POST", { chain });
    await refreshAll();
    toast(`taint: ${r.tainted} wallets exposed from ${r.sources} source(s), ${r.new_flags} flags`);
  } catch (e) { toast("taint scan failed: " + e.message); }
}
async function doFindPath() {
  const from = document.getElementById("pathFrom").value.trim();
  const to = document.getElementById("pathTo").value.trim();
  const msg = document.getElementById("pathMsg");
  if (!from || !to) { msg.textContent = "enter both addresses"; return; }
  const directed = document.getElementById("pathDirected").checked ? 1 : 0;
  try {
    const r = await api(`/api/cases/${currentCase}/path?chain=${chain}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&directed=${directed}`);
    if (!r.found) { msg.textContent = "no connecting path found"; toast("no path"); return; }
    selected = from; ingestGraph(r); setTimeout(fit, 450);
    msg.innerHTML = `<span style="color:var(--accent-2)">${r.hops} hop(s)</span> · min flow ${fmt(r.min_value)}`;
    toast(`path found: ${r.hops} hops`);
  } catch (e) { msg.textContent = e.message; }
}
function doSearch() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  if (!q) return;
  const hit = nodes.find(n => n.address.toLowerCase().includes(q)
    || (n.label || "").toLowerCase().includes(q) || (n.entity || "").toLowerCase().includes(q));
  if (!hit) return toast("no match");
  selectNode(hit.address); centerOn(hit);
}

function wireEmpty() {
  document.querySelectorAll(".js-load").forEach(b => b.onclick = loadSample);
  const e = document.getElementById("emptyLoadBtn"); if (e) e.onclick = loadSample;
}
// ---------- timeline scrubber ----------
async function loadTimeline() {
  tlStop();
  const box = document.getElementById("timeline");
  let tl;
  try { tl = await api(`/api/cases/${currentCase}/timeline?chain=${chain}&asset=${encodeURIComponent(asset)}`); }
  catch { box.classList.add("hidden"); return; }
  tlEvents = (tl.events || []).map(e => ({ t: Date.parse(e.ts), v: e.value, asset: e.asset }));
  if (!tlEvents.length || !tl.min) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  tlMin = Date.parse(tl.min); tlMax = Date.parse(tl.max);
  if (tlMax <= tlMin) tlMax = tlMin + 1;
  tlCursor = tlMax;
  renderTimeline();
}
function renderTimeline() {
  const svg = document.getElementById("tlHist");
  const w = svg.clientWidth || 600, h = svg.clientHeight || 34, B = 48, span = tlMax - tlMin;
  const buckets = new Array(B).fill(0);
  for (const e of tlEvents) {
    let f = (e.t - tlMin) / span; f = Math.max(0, Math.min(0.999, f));
    buckets[Math.floor(f * B)]++;
  }
  const maxB = Math.max(1, ...buckets), bw = w / B, cf = (tlCursor - tlMin) / span;
  svg.innerHTML = buckets.map((c, i) => {
    const bh = c ? Math.max(2, (c / maxB) * (h - 4)) : 0;
    const on = (i + 0.5) / B <= cf;
    return `<rect class="tl-bar ${on ? "on" : ""}" x="${(i * bw + 1).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(1, bw - 1.5).toFixed(1)}" height="${bh.toFixed(1)}" rx="1"/>`;
  }).join("");
  document.getElementById("tlCursor").style.left = (Math.max(0, Math.min(1, cf)) * 100) + "%";
  const n = tlEvents.filter(e => e.t <= tlCursor).length, atMax = tlCursor >= tlMax;
  document.getElementById("tlReadout").innerHTML =
    `<b>${n}</b>/${tlEvents.length} · ${atMax ? "all" : new Date(tlCursor).toISOString().slice(0, 16).replace("T", " ")}`;
}
function setCursor(ms) {
  tlCursor = Math.max(tlMin, Math.min(tlMax, ms));
  renderTimeline();
  clearTimeout(tlFetch);
  tlFetch = setTimeout(async () => {
    const before = tlCursor >= tlMax ? "" : new Date(tlCursor).toISOString();
    try {
      const g = await api(`/api/cases/${currentCase}/graph?chain=${chain}&asset=${encodeURIComponent(asset)}${before ? `&before=${encodeURIComponent(before)}` : ""}`);
      ingestGraph(g);  // keeps node positions; no fit, so the network grows in place
    } catch (e) { /* ignore transient */ }
  }, 90);
}
function tlStop() { tlPlaying = false; clearInterval(tlTimer); tlTimer = null; const b = document.getElementById("tlPlay"); if (b) b.textContent = "▶"; }
function tlToggle() {
  if (tlPlaying) return tlStop();
  if (!tlEvents.length) return;
  tlPlaying = true; document.getElementById("tlPlay").textContent = "❚❚";
  if (tlCursor >= tlMax) setCursor(tlMin);
  tlTimer = setInterval(() => {
    const next = tlEvents.map(e => e.t).filter(t => t > tlCursor + 1).sort((a, b) => a - b)[0];
    if (next == null) { setCursor(tlMax); tlStop(); return; }
    setCursor(next);
  }, 750);
}

function wire() {
  document.getElementById("caseSelect").onchange = async (e) => { currentCase = +e.target.value; selected = null; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("newCaseBtn").onclick = async () => {
    const name = prompt("New investigation name:", "Investigation");
    if (!name) return;
    const { id } = await api("/api/cases", "POST", { name });
    currentCase = id; selected = null; await loadCases(); await refreshAll(); toast("case #" + id + " created");
  };
  document.getElementById("chainSelect").onchange = async (e) => { chain = e.target.value; asset = ""; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("assetSelect").onchange = async (e) => { asset = e.target.value; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("loadSampleBtn").onclick = loadSample;
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
    try {
      const r = await api(`/api/cases/${currentCase}/import`, "POST", body);
      chain = r.chain; document.getElementById("chainSelect").value = chain;
      document.getElementById("seedAddr").value = address;
      await refreshAll(); setTimeout(fit, 450); toast(`imported ${r.transfers} transfers`);
    } catch (e) { toast("fetch failed: " + e.message); }
  };
  document.getElementById("traceBtn").onclick = doTrace;
  document.getElementById("showAllBtn").onclick = async () => { await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("riskBtn").onclick = async () => {
    try {
      const r = await api(`/api/cases/${currentCase}/risk`, "POST", { chain });
      await loadFlags(); computeScales(); draw();
      toast(r.new + r.merged > 0 ? `${r.new} new, ${r.merged} merged flags` : "no risk flags found");
    } catch (e) { toast("risk scan failed: " + e.message); }
  };
  document.getElementById("taintBtn").onclick = doTaint;
  document.getElementById("findPathBtn").onclick = doFindPath;
  document.getElementById("search").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  document.getElementById("zoomInBtn").onclick = () => zoomAround(W / 2, H / 2, 1.2);
  document.getElementById("zoomOutBtn").onclick = () => zoomAround(W / 2, H / 2, 0.83);
  document.getElementById("tlPlay").onclick = tlToggle;
  const track = document.getElementById("tlTrack");
  const curFromX = (ev) => { const r = track.getBoundingClientRect(); let f = (ev.clientX - r.left) / r.width; f = Math.max(0, Math.min(1, f)); setCursor(tlMin + f * (tlMax - tlMin)); };
  track.addEventListener("pointerdown", (ev) => { tlStop(); tlScrub = true; try { track.setPointerCapture(ev.pointerId); } catch (_) {} curFromX(ev); });
  track.addEventListener("pointermove", (ev) => { if (tlScrub) curFromX(ev); });
  track.addEventListener("pointerup", () => { tlScrub = false; });
  document.getElementById("fitBtn").onclick = fit;
  document.getElementById("freezeBtn").onclick = (e) => {
    frozen = !frozen; e.target.classList.toggle("active", frozen);
    e.target.textContent = frozen ? "▶" : "❚❚";
    if (!frozen) reheat();
  };
  wireEmpty();
}

// ---------- utils ----------
function short(a) { return a && a.length > 15 ? a.slice(0, 7) + "…" + a.slice(-5) : a; }
function fmt(v) { if (v == null) return "—"; const n = +v; if (!isFinite(n)) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M"; if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return (Math.round(n * 1000) / 1000).toString(); }
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

wire();
boot();
