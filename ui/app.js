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
let showHulls = false;  // draw co-ownership cluster hulls behind nodes
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
  if (currentCase == null) {
    const stored = +(localStorage.getItem("chainscope_case") || 0);
    currentCase = cases.find(c => c.id === stored) ? stored : cases[0].id;
  } else if (!cases.find(c => c.id === currentCase)) {
    currentCase = cases[0].id;
  }
  sel.value = currentCase;
  persistCase();
}
function persistCase() { try { localStorage.setItem("chainscope_case", String(currentCase)); } catch (_) {} }

// ---------- investigations manager ----------
async function openCaseManager() {
  const { cases } = await api("/api/cases");
  renderCaseList(cases);
  document.getElementById("caseModal").classList.remove("hidden");
}
function closeCaseManager() { document.getElementById("caseModal").classList.add("hidden"); }
function renderCaseList(cases) {
  const box = document.getElementById("caseList");
  if (!cases.length) { box.innerHTML = `<div class="hint">No investigations yet — create one above.</div>`; return; }
  box.innerHTML = cases.map(c => {
    const cu = c.counts || {};
    const la = c.last_activity ? ` · last ${c.last_activity.slice(0, 10)}` : "";
    return `<div class="case-card ${c.id === currentCase ? "current" : ""}">
      <div class="cc-top"><span class="cc-name">${esc(c.name)}</span>${c.id === currentCase ? '<span class="cc-cur">open</span>' : ""}</div>
      ${c.note ? `<div class="cc-note">${esc(c.note)}</div>` : ""}
      <div class="cc-meta">
        <span class="cc-chip"><b>${cu.addresses || 0}</b> wallets</span>
        <span class="cc-chip"><b>${cu.transfers || 0}</b> transfers</span>
        <span class="cc-chip flags"><b>${cu.flags || 0}</b> flags</span>
        <span class="cc-chip">${(c.created_at || "").slice(0, 10)}${la}</span>
      </div>
      <div class="cc-actions">
        <button class="btn primary" data-act="open">Open</button>
        <button class="btn ghost" data-act="rename">Rename</button>
        <button class="btn ghost" data-act="note">Note</button>
        <button class="btn ghost cc-del" data-act="delete">Delete</button>
      </div></div>`;
  }).join("");
  const cards = box.querySelectorAll(".case-card");
  cases.forEach((c, i) => cards[i].querySelectorAll("[data-act]").forEach(b => {
    if (b.dataset.act === "delete") b.onclick = () => confirmDelete(b, c);
    else b.onclick = () => caseAction(b.dataset.act, c);
  }));
}
async function caseAction(act, c) {
  if (act === "open") return openCase(c.id);
  if (act === "rename") {
    const n = prompt("Rename investigation:", c.name);
    if (!n || !n.trim()) return;
    await api(`/api/cases/${c.id}`, "POST", { name: n.trim() });
    await loadCases(); openCaseManager();
  } else if (act === "note") {
    const n = prompt("Note:", c.note || "");
    if (n === null) return;
    await api(`/api/cases/${c.id}`, "POST", { note: n });
    openCaseManager();
  }
}
function confirmDelete(btn, c) {           // two-click inline confirm (no native dialog)
  if (btn.dataset.armed === "1") return deleteCase(c);
  btn.dataset.armed = "1"; btn.textContent = "Confirm?"; btn.classList.add("armed");
  setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = "Delete"; btn.classList.remove("armed"); } }, 3000);
}
async function deleteCase(c) {
  await api(`/api/cases/${c.id}/delete`, "POST");
  if (c.id === currentCase) currentCase = null;
  await loadCases(); await refreshAll(); setTimeout(fit, 400);
  openCaseManager();
}
async function openCase(id) {
  currentCase = id; persistCase(); asset = ""; selected = null;
  document.getElementById("caseSelect").value = id;
  closeCaseManager();
  await refreshAll(); setTimeout(fit, 400);
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
  const cl = document.getElementById("clusterList"); if (cl) cl.innerHTML = "";
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
  showHulls = nodes.some(n => n.cluster);
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

  // cluster hulls (behind everything) — co-ownership grouping
  if (showHulls) drawHulls(vp);

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

// ---------- cluster hulls ----------
function clusterHue(cid) {            // deterministic color per cluster id
  let h = 0; for (let i = 0; i < cid.length; i++) h = (h * 31 + cid.charCodeAt(i)) >>> 0;
  return h % 360;
}
function clusterCss(cid) { return `hsl(${clusterHue(cid)} 72% 62%)`; }
function convexHull(pts) {
  if (pts.length < 3) return pts.slice();
  pts = pts.slice().sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lo = [], hi = [];
  for (const p of pts) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); }
  for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (hi.length >= 2 && cross(hi[hi.length - 2], hi[hi.length - 1], p) <= 0) hi.pop(); hi.push(p); }
  lo.pop(); hi.pop(); return lo.concat(hi);
}
function drawHulls(vp) {
  const groups = {};
  for (const n of nodes) if (n.cluster) (groups[n.cluster] = groups[n.cluster] || []).push(n);
  const hg = el("g", { class: "hulls" }); vp.appendChild(hg);
  for (const cid in groups) {
    const members = groups[cid];
    if (members.length < 2) continue;
    const col = clusterCss(cid);
    const cx = members.reduce((s, m) => s + m.x, 0) / members.length;
    const cy = members.reduce((s, m) => s + m.y, 0) / members.length;
    const pad = Math.max(...members.map(m => m.r)) + 22;
    const hull = convexHull(members.map(m => ({ x: m.x, y: m.y })));
    const padded = hull.map(p => { const dx = p.x - cx, dy = p.y - cy, d = Math.hypot(dx, dy) || 1; return { x: p.x + dx / d * pad, y: p.y + dy / d * pad }; });
    if (padded.length >= 3) {
      const d = "M" + padded.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" L") + " Z";
      hg.appendChild(el("path", { d, class: "hull", fill: col, stroke: col }));
    } else if (padded.length === 2) {
      hg.appendChild(el("line", { x1: padded[0].x, y1: padded[0].y, x2: padded[1].x, y2: padded[1].y,
        class: "hull-line", stroke: col, "stroke-width": pad * 1.3 }));
    }
    let minY = Infinity, lx = cx; for (const p of padded) if (p.y < minY) { minY = p.y; lx = p.x; }
    const t = el("text", { x: cx, y: minY - 5, class: "hull-label", fill: col, "text-anchor": "middle" });
    t.textContent = cid; hg.appendChild(t);
  }
}
async function doCluster() {
  try {
    const res = await api(`/api/cases/${currentCase}/cluster`, "POST", { chain });
    await refreshAll();                 // re-fetch nodes carrying cluster ids
    renderClusterList(res);
    toast(res.count ? `${res.count} cluster(s), ${res.clustered}/${res.total} grouped` : "no multi-address clusters");
  } catch (e) { toast("cluster failed: " + e.message); }
}
function renderClusterList(res) {
  const box = document.getElementById("clusterList");
  if (!box) return;
  if (!res || !res.clusters || !res.clusters.length) { box.innerHTML = `<div class="hint">No multi-address clusters found.</div>`; return; }
  box.innerHTML = res.clusters.map(c => `
    <div class="cl-item" data-cid="${c.id}" data-addrs='${esc(JSON.stringify(c.addresses))}'>
      <span class="cl-dot" style="background:${clusterCss(c.id)}"></span>
      <span class="cl-name">${esc(c.name)}</span>
      <span class="cl-size">${c.size}</span>
      ${c.risk && c.risk !== "none" ? `<span class="badge risk-${c.risk}">${c.risk}</span>` : ""}
    </div>`).join("");
  box.querySelectorAll(".cl-item").forEach(it => it.onclick = () => { focusCluster(JSON.parse(it.dataset.addrs)); openEntity(it.dataset.cid); });
}
function focusCluster(addrs) {
  const ns = addrs.map(a => nodeById[a]).filter(Boolean);
  if (!ns.length) return;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of ns) { minx = Math.min(minx, n.x); miny = Math.min(miny, n.y); maxx = Math.max(maxx, n.x); maxy = Math.max(maxy, n.y); }
  const pad = 130, gw = (maxx - minx) || 1, gh = (maxy - miny) || 1;
  view.k = Math.min((W - pad) / gw, (H - pad) / gh, 2.4);
  view.x = W / 2 - ((minx + maxx) / 2) * view.k;
  view.y = H / 2 - ((miny + maxy) / 2) * view.k;
  draw();
}

// ---------- entity & token dashboards ----------
async function openEntity(cid) {
  try {
    const e = await api(`/api/cases/${currentCase}/entity?chain=${chain}&cluster=${encodeURIComponent(cid)}`);
    if (e.error) return toast(e.error);
    renderEntity(e);
  } catch (err) { toast("entity failed: " + err.message); }
}
async function openAssetDash() {
  try {
    const d = await api(`/api/cases/${currentCase}/asset?chain=${chain}&asset=${encodeURIComponent(asset)}`);
    renderAssetDash(d);
  } catch (err) { toast("asset dashboard failed: " + err.message); }
}
function wireAddrClicks(box) {
  box.querySelectorAll(".cp-addr, .ent-mem, .asset-row").forEach(x => x.onclick = () => {
    if (x.dataset.s != null) { asset = x.dataset.s; document.getElementById("assetSelect").value = asset; refreshAll().then(openAssetDash); return; }
    selectNode(x.dataset.a); const n = nodeById[x.dataset.a]; if (n) centerOn(n);
  });
}
function renderEntity(e) {
  const box = document.getElementById("inspector");
  box.className = "inspector insp";
  const holdRows = (e.holdings || []).map(h => `<tr><td><b>${esc(h.asset)}</b></td><td class="num">${fmt(h.balance)}</td><td class="num muted-usd">${h.usd != null ? usd(h.usd) : ""}</td></tr>`).join("");
  const flowRows = (e.flows || []).map(f => `<tr><td><b>${esc(f.asset)}</b></td><td class="num recv">${f.in ? "+" + fmt(f.in) : ""}</td><td class="num sent">${f.out ? "−" + fmt(f.out) : ""}</td></tr>`).join("");
  const cps = (e.counterparties || []).map(c => {
    const cc = (c.risk === "sanctioned" || FLAGGED.has(c.category)) ? CAT_COLOR.mixer : (CAT_COLOR[c.category] || CAT_COLOR.unknown);
    const chips = (c.assets || []).map(x => `<span class="cp-as ${x.net >= 0 ? "pos" : "neg"}">${esc(x.asset)} ${x.net >= 0 ? "+" : "−"}${fmt(Math.abs(x.net))}</span>`).join("");
    return `<tr><td><span class="cp-addr" data-a="${c.address}"><span class="cp-dot" style="background:${cc}"></span><span class="cp-name">${c.label ? esc(c.label) : short(c.address)}</span></span>${c.risk && c.risk !== "none" ? `<span class="badge risk-${c.risk}">${c.risk}</span>` : ""}<div class="cp-assets">${chips}</div></td></tr>`;
  }).join("");
  const members = (e.members || []).map(m => {
    const cc = (m.risk === "sanctioned" || FLAGGED.has(m.category)) ? CAT_COLOR.mixer : (CAT_COLOR[m.category] || CAT_COLOR.unknown);
    return `<div class="ent-mem" data-a="${m.address}"><span class="cp-dot" style="background:${cc}"></span><span class="cp-name">${m.label ? esc(m.label) : short(m.address)}</span><span class="ent-tx">${m.tx_count || 0} tx</span></div>`;
  }).join("");
  box.innerHTML = `
    <div class="insp-head">
      <div class="monogram" style="background:${clusterCss(e.id)}">${esc(e.id)}</div>
      <div class="insp-title"><div class="insp-label">${esc(e.name)}</div>
        <div class="insp-sub">entity · ${e.size} wallets</div></div>
    </div>
    <div class="badges">
      ${e.risk && e.risk !== "none" ? `<span class="badge risk-${e.risk}">risk: ${e.risk}</span>` : ""}
      ${e.flagged_members ? `<span class="badge risk-high">${e.flagged_members} flagged</span>` : ""}
      ${e.max_taint >= 0.15 ? `<span class="badge taint">taint ${Math.round(e.max_taint * 100)}%</span>` : ""}
    </div>
    <div class="kpi-row">
      <div class="kpi"><div class="v">${usd(e.usd_held)}</div><div class="k">holdings</div></div>
      <div class="kpi"><div class="v">${usd(e.exchange_usd.deposited)}</div><div class="k">to exch</div></div>
      <div class="kpi"><div class="v">${e.size}</div><div class="k">wallets</div></div>
    </div>
    <h4>Combined holdings</h4>
    <table class="cp-tbl"><thead><tr><th>asset</th><th class="num">amount</th><th class="num">value</th></tr></thead><tbody>${holdRows || '<tr><td colspan="3" class="hint">none</td></tr>'}</tbody></table>
    <h4>Net external flows</h4>
    <table class="cp-tbl"><thead><tr><th>asset</th><th class="num">in</th><th class="num">out</th></tr></thead><tbody>${flowRows || '<tr><td colspan="3" class="hint">none</td></tr>'}</tbody></table>
    <h4>Top counterparties</h4>
    <table class="cp-tbl"><tbody>${cps || '<tr><td class="hint">none</td></tr>'}</tbody></table>
    <h4>Member wallets</h4>
    <div class="ent-members">${members}</div>`;
  wireAddrClicks(box);
}
function rowAddr(h) {
  const cc = (h.risk === "sanctioned" || FLAGGED.has(h.category)) ? CAT_COLOR.mixer : (CAT_COLOR[h.category] || CAT_COLOR.unknown);
  const amt = h.balance != null ? fmt(h.balance) : fmt(h.value);
  return `<tr><td><span class="cp-addr" data-a="${h.address}"><span class="cp-dot" style="background:${cc}"></span><span class="cp-name">${h.label ? esc(h.label) : short(h.address)}</span></span></td><td class="num">${amt}</td><td class="num muted-usd">${h.usd != null ? usd(h.usd) : ""}</td></tr>`;
}
function renderAssetDash(d) {
  const box = document.getElementById("inspector");
  box.className = "inspector insp";
  if (d.overview) {
    const rows = (d.assets || []).map(a => `<tr class="asset-row" data-s="${esc(a.asset)}"><td><b>${esc(a.asset)}</b></td><td class="num">${fmt(a.moved)}</td><td class="num muted-usd">${a.moved_usd != null ? usd(a.moved_usd) : ""}</td><td class="num">${a.holders}</td></tr>`).join("");
    box.innerHTML = `<div class="insp-head"><div class="monogram" style="background:#1f6feb">Σ</div><div class="insp-title"><div class="insp-label">Assets</div><div class="insp-sub">${(d.assets || []).length} tokens in case</div></div></div>
      <p class="hint">Click an asset for its holders &amp; flows.</p>
      <table class="cp-tbl"><thead><tr><th>asset</th><th class="num">moved</th><th class="num">value</th><th class="num">holders</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="hint">none</td></tr>'}</tbody></table>`;
    wireAddrClicks(box);
    return;
  }
  const ef = d.exchange_flow || { in: 0, out: 0 };
  box.innerHTML = `
    <div class="insp-head"><div class="monogram" style="background:#1f6feb">${esc(d.asset.slice(0, 3))}</div>
      <div class="insp-title"><div class="insp-label">${esc(d.asset)}</div><div class="insp-sub">${d.price ? usd(d.price) + " · " : ""}token dashboard</div></div></div>
    <div class="kpi-row">
      <div class="kpi"><div class="v">${fmt(d.moved)}</div><div class="k">moved</div></div>
      <div class="kpi"><div class="v">${usd(d.moved_usd)}</div><div class="k">value</div></div>
      <div class="kpi"><div class="v">${d.txns}</div><div class="k">txns</div></div>
    </div>
    <h4>Exchange flow</h4>
    <table class="cp-tbl"><tbody>
      <tr><td>into exchanges</td><td class="num recv">${fmt(ef.in)}</td><td class="num muted-usd">${ef.in_usd != null ? usd(ef.in_usd) : ""}</td></tr>
      <tr><td>out of exchanges</td><td class="num sent">${fmt(ef.out)}</td><td class="num muted-usd">${ef.out_usd != null ? usd(ef.out_usd) : ""}</td></tr></tbody></table>
    <h4>Top holders</h4>
    <table class="cp-tbl"><tbody>${(d.holders || []).map(rowAddr).join("") || '<tr><td class="hint">none</td></tr>'}</tbody></table>
    <h4>Top receivers</h4>
    <table class="cp-tbl"><tbody>${(d.top_receivers || []).map(rowAddr).join("") || '<tr><td class="hint">none</td></tr>'}</tbody></table>
    <h4>Top senders</h4>
    <table class="cp-tbl"><tbody>${(d.top_senders || []).map(rowAddr).join("") || '<tr><td class="hint">none</td></tr>'}</tbody></table>`;
  wireAddrClicks(box);
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
  const info = await api(`/api/cases/${currentCase}/address?chain=${chain}&address=${encodeURIComponent(address)}&asset=${encodeURIComponent(asset)}`);
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
    const assets = c.assets || [];
    // Multiple assets can't be summed into one in/out (2M USDC vs 30 ETH), so
    // show a signed per-asset net chip row and blank the aggregate columns.
    const multi = assets.length > 1;
    const chips = multi ? `<div class="cp-assets">` + assets.map(x =>
      `<span class="cp-as ${x.net >= 0 ? "pos" : "neg"}">${esc(x.asset)} ${x.net >= 0 ? "+" : "−"}${fmt(Math.abs(x.net))}</span>`).join("") + `</div>` : "";
    return `<tr>
      <td><span class="cp-addr" data-a="${c.address}"><span class="cp-dot" style="background:${col}"></span>
        <span class="cp-name">${c.label ? esc(c.label) : short(c.address)}</span></span>
        ${c.risk && c.risk !== "none" ? `<span class="badge risk-${c.risk}">${c.risk}</span>` : ""}${chips}</td>
      <td class="num recv">${!multi && c.recv ? "+" + fmt(c.recv) : ""}</td>
      <td class="num sent">${!multi && c.sent ? "−" + fmt(c.sent) : ""}</td></tr>`;
  }).join("");
  // merge per-asset holdings + in/out flows into one table
  const am = {};
  (a.holdings || []).forEach(h => { const o = am[h.asset] = am[h.asset] || {}; o.held = h.balance; o.heldUsd = h.usd; });
  (a.by_asset || []).forEach(b => { const o = am[b.asset] = am[b.asset] || {}; o.recv = b.recv; o.sent = b.sent; });
  const assetRows = Object.keys(am).sort().map(sym => {
    const v = am[sym];
    return `<tr><td><b>${esc(sym)}</b></td>
      <td class="num">${v.held != null ? fmt(v.held) : "—"}</td>
      <td class="num muted-usd">${v.heldUsd != null ? usd(v.heldUsd) : ""}</td>
      <td class="num recv">${v.recv ? "+" + fmt(v.recv) : ""}</td>
      <td class="num sent">${v.sent ? "−" + fmt(v.sent) : ""}</td></tr>`;
  }).join("");
  const sparkAsset = asset || a.asset || "";
  const sparkSection = (a.balance_series && a.balance_series.length > 1)
    ? `<h4>Balance history${sparkAsset ? " · " + esc(sparkAsset) : ""}</h4>${sparkline(a.balance_series)}` : "";

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
      ${a.usd_held ? `<div class="kpi"><div class="v">${usd(a.usd_held)}</div><div class="k">value</div></div>` : ""}
      <div class="kpi"><div class="v">${m.tx_count ?? a.tx_count ?? 0}</div><div class="k">txns</div></div>
      <div class="kpi"><div class="v">${m.counterparties || 0}</div><div class="k">peers</div></div>
    </div>
    ${sparkSection}
    <h4>Assets held &amp; moved</h4>
    <table class="cp-tbl"><thead><tr><th>asset</th><th class="num">held</th><th class="num">value</th><th class="num">in</th><th class="num">out</th></tr></thead>
      <tbody>${assetRows || '<tr><td colspan="5" class="hint">none</td></tr>'}</tbody></table>
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
  const { flags } = await api(`/api/cases/${currentCase}/flags?chain=${chain}`);
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
async function exportReport(fmt) {
  try {
    const data = await api(`/api/cases/${currentCase}/report?chain=${chain}&format=${fmt}`);
    const content = fmt === "json" ? JSON.stringify(data, null, 2) : data.markdown;
    const mime = fmt === "json" ? "application/json" : "text/markdown";
    downloadFile(`chainscope-case${currentCase}.${fmt === "json" ? "json" : "md"}`, content, mime);
    toast("report exported");
  } catch (e) { toast("export failed: " + e.message); }
}
function downloadFile(name, content, mime) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([content], { type: mime }));
  a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
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
  document.getElementById("caseSelect").onchange = async (e) => { currentCase = +e.target.value; persistCase(); selected = null; asset = ""; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("newCaseBtn").onclick = openCaseManager;
  document.querySelectorAll("#caseModal [data-close]").forEach(b => b.onclick = closeCaseManager);
  document.getElementById("newCaseForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("ncName").value.trim();
    if (!name) return;
    const note = document.getElementById("ncNote").value.trim();
    const { id } = await api("/api/cases", "POST", { name, note: note || null });
    document.getElementById("ncName").value = ""; document.getElementById("ncNote").value = "";
    await loadCases(); await openCase(id); toast("investigation created");
  });
  document.getElementById("chainSelect").onchange = async (e) => { chain = e.target.value; asset = ""; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("assetSelect").onchange = async (e) => { asset = e.target.value; await refreshAll(); setTimeout(fit, 450); };
  document.getElementById("assetDashBtn").onclick = openAssetDash;
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
  document.getElementById("clusterBtn").onclick = doCluster;
  document.getElementById("findPathBtn").onclick = doFindPath;
  document.getElementById("search").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  document.getElementById("zoomInBtn").onclick = () => zoomAround(W / 2, H / 2, 1.2);
  document.getElementById("zoomOutBtn").onclick = () => zoomAround(W / 2, H / 2, 0.83);
  document.getElementById("exportMdBtn").onclick = () => exportReport("md");
  document.getElementById("exportJsonBtn").onclick = () => exportReport("json");
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
function usd(v) { if (v == null || v === "" || !isFinite(+v)) return "—"; const n = +v, s = n < 0 ? "−" : "", a = Math.abs(n);
  if (a >= 1e9) return s + "$" + (a / 1e9).toFixed(2) + "B"; if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return s + "$" + (a / 1e3).toFixed(1) + "k"; return s + "$" + a.toFixed(2); }
function sparkline(series, w = 236, h = 42) {
  if (!series || series.length < 2) return `<div class="hint">not enough history</div>`;
  const vals = series.map(p => p.balance); let mn = Math.min(0, ...vals), mx = Math.max(0, ...vals);
  if (mx === mn) mx = mn + 1; const n = series.length;
  const X = i => (i / (n - 1)) * (w - 4) + 2, Y = v => h - 3 - ((v - mn) / (mx - mn)) * (h - 6);
  const pts = series.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.balance).toFixed(1)}`).join(" ");
  const area = `M${X(0).toFixed(1)},${(h - 3).toFixed(1)} ` + series.map((p, i) => `L${X(i).toFixed(1)},${Y(p.balance).toFixed(1)}`).join(" ") + ` L${X(n - 1).toFixed(1)},${(h - 3).toFixed(1)} Z`;
  const zero = (mn < 0 && mx > 0) ? `<line class="spark-zero" x1="2" y1="${Y(0).toFixed(1)}" x2="${w - 2}" y2="${Y(0).toFixed(1)}"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${zero}<path class="spark-area" d="${area}"/><path class="spark-line" d="${pts}"/></svg>`;
}
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

wire();
boot();
