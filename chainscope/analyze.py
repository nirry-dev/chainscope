"""On-chain analysis: ingest a dataset into the graph, compute wallet metrics,
trace fund flows across hops, and raise flags for risky exposure.

Read-only throughout — this inspects public transaction history, it never
constructs or sends a transaction.
"""
from . import store, labels

_SEV_SCORE = {"critical": 100, "high": 70, "medium": 45, "low": 20, "info": 8}
_EXCHANGE_CATS = {"exchange", "cex"}


def usd_of(prices, asset, amount):
    """USD value of `amount` of `asset` under the case price snapshot, or None
    when that asset has no price (so the UI can show '—' rather than $0)."""
    if amount is None or asset not in prices:
        return None
    try:
        return float(amount) * prices[asset]
    except (TypeError, ValueError):
        return None


# --- ingest --------------------------------------------------------------

def ingest(conn, case_id, data, source="sample"):
    """Load a normalized dataset (see sources.py) into the chain graph.

    Order: label-bearing entities first, then addresses (balances/seed/tags),
    then transfers (auto-creating any endpoint node not yet present). Built-in
    KB attributions fill in labels/risk for anything still unlabeled."""
    chain = data["chain"]
    asset = data.get("asset")

    # 1. explicit entity labels from the dataset
    for e in data.get("entities", []):
        cat = e.get("category")
        risk = e.get("risk") or labels.default_risk(cat)
        store.upsert_address(
            conn, case_id, chain, e["address"], label=e.get("label"),
            entity=e.get("entity"), category=cat, risk=risk, source=source)

    # 2. addresses with balances / seed flags / tags. `holdings` is a per-asset
    #    map {ASSET: balance}; a bare `balance` is treated as the native asset.
    for a in data.get("addresses", []):
        hold = a.get("holdings") or {}
        native_bal = a.get("balance")
        if native_bal is None and asset in hold:
            native_bal = hold[asset]
        store.upsert_address(
            conn, case_id, chain, a["address"], balance=native_bal,
            asset=asset, source=source, tags=a.get("tags"),
            seed=a.get("seed"), note=a.get("note"))
        if hold:
            for asym, bal in hold.items():
                store.set_holding(conn, case_id, chain, a["address"], asym, bal)
        elif native_bal is not None:
            store.set_holding(conn, case_id, chain, a["address"], asset, native_bal)

    # 3. transfers (auto-create endpoints we haven't seen)
    seen = {r["address"] for r in store.list_addresses(conn, case_id, chain)}
    n_xfer = 0
    for t in data.get("transfers", []):
        for addr in (t["src"], t["dst"]):
            if addr not in seen:
                store.upsert_address(conn, case_id, chain, addr, asset=asset, source=source)
                seen.add(addr)
        store.upsert_transfer(
            conn, case_id, chain, t["txid"], t["src"], t["dst"], t["value"],
            asset=t.get("asset") or asset, ts=t.get("ts"),
            block=t.get("block"), source=source)
        n_xfer += 1

    # 3b. common-input co-spend groups (ownership clustering signal)
    for group in data.get("cospends", []):
        store.add_cospend_group(conn, case_id, chain, group)

    # 3c. USD price snapshot (optional, static — powers valuation/dashboards)
    for sym, usd in (data.get("prices") or {}).items():
        store.set_price(conn, case_id, sym, usd)

    # 4. built-in KB attribution for anything still unlabeled
    for row in store.list_addresses(conn, case_id, chain):
        kb = labels.lookup(chain, row["address"])
        if kb and not row.get("label"):
            store.upsert_address(
                conn, case_id, chain, row["address"], label=kb["label"],
                entity=kb["entity"], category=kb["category"], risk=kb["risk"])

    store.refresh_tx_counts(conn, case_id, chain)
    return {"chain": chain, "transfers": n_xfer, "addresses": len(seen)}


# --- metrics -------------------------------------------------------------

def address_metrics(conn, case_id, chain, address, asset=None, before=None, after=None):
    """In/out value + counts + counterparties for one address. Pass `asset` to
    restrict to a single token/coin and `before`/`after` (ISO ts) to a window."""
    af, ap = "", []
    if asset:
        af += " AND asset=?"; ap.append(asset)
    if before:
        af += " AND ts IS NOT NULL AND ts<=?"; ap.append(before)
    if after:
        af += " AND ts IS NOT NULL AND ts>=?"; ap.append(after)
    out = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND src=?" + af, [case_id, chain, address] + ap).fetchone()
    inn = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND dst=?" + af, [case_id, chain, address] + ap).fetchone()
    cps = conn.execute(
        "SELECT COUNT(DISTINCT cp) n FROM ("
        "  SELECT dst cp FROM chain_transfers WHERE case_id=? AND chain=? AND src=? "
        "  UNION SELECT src cp FROM chain_transfers WHERE case_id=? AND chain=? AND dst=?)",
        (case_id, chain, address, case_id, chain, address)).fetchone()
    return {"out_value": out["v"], "out_count": out["n"],
            "in_value": inn["v"], "in_count": inn["n"],
            "counterparties": cps["n"]}


def asset_breakdown(conn, case_id, chain, address):
    """Per-asset received/sent totals for one address."""
    rows = {}
    for r in conn.execute(
            "SELECT asset, COALESCE(SUM(value),0) v, COUNT(*) n FROM chain_transfers "
            "WHERE case_id=? AND chain=? AND dst=? GROUP BY asset", (case_id, chain, address)):
        rows.setdefault(r["asset"], {"recv": 0, "sent": 0})["recv"] = r["v"]
    for r in conn.execute(
            "SELECT asset, COALESCE(SUM(value),0) v, COUNT(*) n FROM chain_transfers "
            "WHERE case_id=? AND chain=? AND src=? GROUP BY asset", (case_id, chain, address)):
        rows.setdefault(r["asset"], {"recv": 0, "sent": 0})["sent"] = r["v"]
    return [{"asset": a, **v} for a, v in rows.items()]


def counterparties(conn, case_id, chain, address, asset=None):
    """Direct counterparties with net flow direction and volume — the
    address-inspector table.

    Each counterparty carries an aggregate recv/sent/net plus a per-asset
    breakdown (`assets`: [{asset, recv, sent, net}]), so a mixed-asset
    relationship (e.g. +2M USDC but −30 ETH) reads correctly instead of summing
    non-comparable units. Pass `asset` to restrict to a single token/coin."""
    rows = {}  # counterparty -> {assets: {sym: {recv, sent}}, n}
    for e in store.edges_aggregated(conn, case_id, chain, asset=asset):
        if e["src"] == address:
            cp, key = e["dst"], "sent"
        elif e["dst"] == address:
            cp, key = e["src"], "recv"
        else:
            continue
        r = rows.setdefault(cp, {"assets": {}, "n": 0})
        a = r["assets"].setdefault(e["asset"], {"recv": 0, "sent": 0})
        a[key] += e["total"] or 0
        r["n"] += e["n"]
    out = []
    for addr, agg in rows.items():
        a = store.get_address(conn, case_id, chain, addr) or {}
        per_asset = [{"asset": sym, "recv": v["recv"], "sent": v["sent"],
                      "net": v["recv"] - v["sent"]}
                     for sym, v in sorted(agg["assets"].items())]
        recv = sum(v["recv"] for v in agg["assets"].values())
        sent = sum(v["sent"] for v in agg["assets"].values())
        out.append({"address": addr, "label": a.get("label"),
                    "entity": a.get("entity"), "category": a.get("category"),
                    "risk": a.get("risk", "none"), "cluster": a.get("cluster"),
                    "n": agg["n"], "recv": recv, "sent": sent,
                    "net": recv - sent, "assets": per_asset})
    out.sort(key=lambda r: r["sent"] + r["recv"], reverse=True)
    return out


def balance_series(conn, case_id, chain, address, asset=None):
    """Running cumulative net balance (in − out) over time for an address, as
    ordered points — the data behind a balance-history sparkline. This is net
    flow since the first observed transfer (no opening balance / fees), so it is
    a provenance view of accumulation, not a custody statement."""
    af, ap = "", []
    if asset:
        af = " AND asset=?"; ap = [asset]
    rows = conn.execute(
        "SELECT ts, value, src, dst FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND (src=? OR dst=?) AND ts IS NOT NULL" + af +
        " ORDER BY ts", [case_id, chain, address, address] + ap).fetchall()
    series, bal = [], 0.0
    for r in rows:
        bal += (r["value"] or 0) * (1 if r["dst"] == address else -1)
        series.append({"ts": r["ts"], "balance": round(bal, 6)})
    return series


def inspect(conn, case_id, chain, address, asset=None):
    """Everything the UI shows when you click a node. `asset` restricts the
    metrics and counterparty nets to a single token/coin (the active filter)."""
    a = store.get_address(conn, case_id, chain, address)
    if not a:
        return None
    prices = store.prices(conn, case_id)
    a["metrics"] = address_metrics(conn, case_id, chain, address, asset=asset)
    a["counterparties"] = counterparties(conn, case_id, chain, address, asset=asset)
    holds = store.holdings(conn, case_id, chain, address)
    for h in holds:
        h["usd"] = usd_of(prices, h["asset"], h["balance"])
    a["holdings"] = holds
    a["usd_held"] = round(sum(h["usd"] for h in holds if h["usd"]), 2) if holds else 0
    a["by_asset"] = asset_breakdown(conn, case_id, chain, address)
    a["balance_series"] = balance_series(conn, case_id, chain, address,
                                         asset=asset or a.get("asset"))
    a["prices"] = prices
    return a


# --- flow tracing --------------------------------------------------------

def trace_flow(conn, case_id, chain, seed, hops=3, direction="out", min_value=0.0,
               asset=None):
    """Breadth-first 'follow the money' from a seed address.

    direction: 'out' (where funds went), 'in' (where they came from), 'both'.
    asset: restrict the trace to a single token/coin.
    Returns a subgraph {nodes, edges} suitable for the flow-graph visualizer,
    each node carrying its hop distance from the seed and its label/risk."""
    edges_all = store.edges_aggregated(conn, case_id, chain, asset=asset)
    fwd, rev = {}, {}
    for e in edges_all:
        if (e["total"] or 0) < min_value:
            continue
        fwd.setdefault(e["src"], []).append(e)
        rev.setdefault(e["dst"], []).append(e)

    hop = {seed: 0}
    frontier = [seed]
    kept_edges = {}
    for depth in range(hops):
        nxt = []
        for node in frontier:
            outbound = []
            if direction in ("out", "both"):
                outbound += [(e, e["dst"]) for e in fwd.get(node, [])]
            if direction in ("in", "both"):
                outbound += [(e, e["src"]) for e in rev.get(node, [])]
            for e, other in outbound:
                key = (e["src"], e["dst"], e["asset"])
                kept_edges[key] = e
                if other not in hop:
                    hop[other] = depth + 1
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break

    nodes = []
    for addr, h in hop.items():
        a = store.get_address(conn, case_id, chain, addr) or {"address": addr}
        m = address_metrics(conn, case_id, chain, addr, asset=asset)
        nodes.append({
            "address": addr, "hop": h,
            "label": a.get("label"), "entity": a.get("entity"),
            "category": a.get("category") or "unknown",
            "risk": a.get("risk", "none"), "balance": a.get("balance"),
            "seed": bool(a.get("seed")) or addr == seed,
            "in_value": m["in_value"], "out_value": m["out_value"],
            "tx_count": a.get("tx_count", 0),
            "taint": a.get("taint", 0), "taint_src": a.get("taint_src"),
            "taint_hops": a.get("taint_hops"), "cluster": a.get("cluster"),
        })
    edges = [{"src": e["src"], "dst": e["dst"], "asset": e["asset"],
              "value": e["total"], "count": e["n"]} for e in kept_edges.values()]
    return {"seed": seed, "chain": chain, "hops": hops, "direction": direction,
            "asset": asset, "assets": store.assets(conn, case_id, chain),
            "nodes": nodes, "edges": edges}


def graph_view(conn, case_id, chain, asset=None, before=None, after=None):
    """The full chain graph for a case (nodes + aggregated edges), enriched with
    per-node metrics. `asset` restricts to one token/coin; `before`/`after` (ISO
    ts) restrict to a time window — and when a window is set, only wallets that
    have transacted within it are shown (so the network grows as the timeline
    scrubber advances)."""
    edges_raw = store.edges_aggregated(conn, case_id, chain, asset=asset,
                                       before=before, after=after)
    g = {"nodes": [], "edges": [], "chain": chain, "asset": asset,
         "assets": store.assets(conn, case_id, chain)}
    if before or after:
        live = set()
        for e in edges_raw:
            live.add(e["src"]); live.add(e["dst"])
        rows = [a for a in store.list_addresses(conn, case_id, chain)
                if a["address"] in live or a.get("seed")]
    else:
        rows = store.list_addresses(conn, case_id, chain)
    for a in rows:
        m = address_metrics(conn, case_id, chain, a["address"], asset=asset,
                            before=before, after=after)
        g["nodes"].append({
            "address": a["address"], "hop": 0 if a.get("seed") else 1,
            "label": a.get("label"), "entity": a.get("entity"),
            "category": a.get("category") or "unknown",
            "risk": a.get("risk", "none"), "balance": a.get("balance"),
            "seed": bool(a.get("seed")),
            "in_value": m["in_value"], "out_value": m["out_value"],
            "tx_count": a.get("tx_count", 0),
            "taint": a.get("taint", 0), "taint_src": a.get("taint_src"),
            "taint_hops": a.get("taint_hops"), "cluster": a.get("cluster")})
    for e in edges_raw:
        g["edges"].append({"src": e["src"], "dst": e["dst"],
                           "asset": e["asset"], "value": e["total"],
                           "count": e["n"]})
    return g


# --- risk engine ---------------------------------------------------------

_FLAGGED = {"mixer", "tumbler", "darkmarket", "ransomware", "scam", "sanctioned"}


def risk_scan(conn, case_id, chain):
    """Raise flags for risky exposure in the chain graph:

      * direct transfer INTO a mixer / sanctioned address  (laundering)
      * funds RECEIVED FROM a flagged (scam/sanctioned) address (tainted inflow)

    Idempotent: stable dedup keys mean re-running merges instead of duplicating.
    Returns (new, merged)."""
    by_addr = {a["address"]: a for a in store.list_addresses(conn, case_id, chain)}
    items = []
    for e in store.edges_aggregated(conn, case_id, chain):
        src = by_addr.get(e["src"], {})
        dst = by_addr.get(e["dst"], {})
        vol = e["total"] or 0
        dst_cat = (dst.get("category") or "").lower()
        dst_risk = dst.get("risk", "none")
        src_cat = (src.get("category") or "").lower()
        src_risk = src.get("risk", "none")

        # outflow to a mixer / sanctioned destination
        if dst_cat in _FLAGGED or labels.risk_rank(dst_risk) >= 3:
            sev = "critical" if dst_risk == "sanctioned" else "high"
            dname = dst.get("label") or e["dst"]
            items.append({
                "dedup_key": f"chain:{chain}:launder:{e['src']}:{e['dst']}",
                "title": f"{vol:g} {e['asset']} sent to flagged address ({dname})",
                "severity": sev, "score": float(_SEV_SCORE[sev]),
                "category": "chain", "technique": f"mixer-exposure/{dst_cat or 'flagged'}",
                "description": (
                    f"Address {e['src']} sent {vol:g} {e['asset']} across "
                    f"{e['n']} transfer(s) to {dname} [{dst_cat or 'flagged'}, "
                    f"risk={dst_risk}]. Movement into a mixer/sanctioned "
                    f"destination is a strong laundering/obfuscation signal and "
                    f"breaks naive provenance tracing at this hop."),
                "remediation": (
                    "Flag the sending address and all upstream sources for "
                    "investigation; preserve the transaction hashes as evidence; "
                    "where sanctions apply, treat downstream commingled funds as "
                    "tainted. Pivot the trace to the mixer's known withdrawal "
                    "clusters rather than following the on-chain edge directly."),
            })

        # tainted inflow from a scam / sanctioned source
        if src_cat in _FLAGGED or labels.risk_rank(src_risk) >= 3:
            sev = "high" if src_risk == "sanctioned" else "medium"
            sname = src.get("label") or e["src"]
            items.append({
                "dedup_key": f"chain:{chain}:taint:{e['dst']}:{e['src']}",
                "title": f"{vol:g} {e['asset']} received from flagged address ({sname})",
                "severity": sev, "score": float(_SEV_SCORE[sev]),
                "category": "chain", "technique": f"tainted-inflow/{src_cat or 'flagged'}",
                "description": (
                    f"Address {e['dst']} received {vol:g} {e['asset']} across "
                    f"{e['n']} transfer(s) from {sname} [{src_cat or 'flagged'}, "
                    f"risk={src_risk}]. Direct inflow from a flagged source taints "
                    f"this address's balance for compliance/attribution purposes."),
                "remediation": (
                    "Attribute and document the receiving address; if it is an "
                    "exchange deposit address, it is a candidate KYC chokepoint — "
                    "preserve evidence for a subpoena/exchange request."),
            })

    return store.record_flags(conn, case_id, items, chain=chain)


# --- taint propagation ---------------------------------------------------

TAINT_DECAY = 0.6   # taint multiplier per hop downstream of a flagged source
TAINT_MIN = 0.15    # below this, an address is considered clean


def taint_scan(conn, case_id, chain, max_hops=4):
    """Propagate 'taint' downstream from every flagged source (scam / mixer /
    sanctioned) along the direction funds moved, decaying each hop. An address
    that received dirty funds several hops away still carries a (smaller) share
    of exposure — the thing direct-edge risk scanning misses.

    Writes a 0..1 taint score + nearest-source label/hops onto each address,
    and raises a flag for tainted non-source wallets. Returns
    (tainted, sources, new_flags)."""
    store.reset_taint(conn, case_id, chain)
    by_addr = {a["address"]: a for a in store.list_addresses(conn, case_id, chain)}
    out_adj = {}
    for e in store.edges_aggregated(conn, case_id, chain):
        out_adj.setdefault(e["src"], []).append(e["dst"])

    def is_source(a):
        return (a.get("category") or "").lower() in _FLAGGED or labels.risk_rank(a.get("risk")) >= 3

    sources = [a["address"] for a in by_addr.values() if is_source(a)]
    best = {}  # address -> (taint, src_label, hops)
    for s in sources:
        slabel = by_addr[s].get("label") or s
        # BFS downstream; taint at hop h = DECAY**h (source itself = 1.0)
        seen = {s: 0}
        frontier = [s]
        for h in range(max_hops):
            nxt = []
            for node in frontier:
                for dst in out_adj.get(node, []):
                    if dst not in seen:
                        seen[dst] = h + 1
                        nxt.append(dst)
            frontier = nxt
            if not frontier:
                break
        for addr, hops in seen.items():
            t = TAINT_DECAY ** hops if hops > 0 else 1.0
            if addr not in best or t > best[addr][0]:
                best[addr] = (t, slabel, hops)

    items, tainted = [], 0
    for addr, (t, slabel, hops) in best.items():
        store.set_taint(conn, case_id, chain, addr, round(t, 3), slabel, hops)
        if hops == 0:
            continue  # the source itself
        tainted += 1
        if t < TAINT_MIN:
            continue
        a = by_addr.get(addr, {})
        if is_source(a):
            continue  # already flagged on its own merits
        sev = "high" if t >= 0.5 else ("medium" if t >= 0.3 else "low")
        name = a.get("label") or addr
        items.append({
            "dedup_key": f"chain:{chain}:tainted:{addr}",
            "title": f"{name} carries {int(t*100)}% taint ({hops} hop(s) from {slabel})",
            "severity": sev, "score": float(_SEV_SCORE[sev]),
            "category": "chain", "technique": f"taint-propagation/{hops}hop",
            "description": (
                f"Address {addr} is {hops} hop(s) downstream of flagged source "
                f"{slabel}; {int(t*100)}% of a direct transfer's taint reaches it "
                f"under a {TAINT_DECAY} per-hop decay model. Its balance is "
                f"commingled with funds traceable to that source."),
            "remediation": (
                "Treat the commingled balance as tainted for attribution/compliance; "
                "trace the specific inbound transfers to confirm the proportion, and "
                "document the full path back to the flagged source as evidence."),
        })
    new, _ = store.record_flags(conn, case_id, items, chain=chain)
    return {"tainted": tainted, "sources": len(sources), "new_flags": new}


# --- path finding --------------------------------------------------------

def find_paths(conn, case_id, chain, src, dst, max_hops=6, directed=True):
    """Shortest connecting path from src to dst over the transfer graph
    (directed = following the direction funds moved; else undirected). Returns a
    subgraph of just the path plus the ordered address list, for the visualizer
    to render the A→B connection."""
    edges = store.edges_aggregated(conn, case_id, chain)
    emap = {}
    adj = {}
    for e in edges:
        emap[(e["src"], e["dst"])] = e
        adj.setdefault(e["src"], []).append(e["dst"])
        if not directed:
            adj.setdefault(e["dst"], []).append(e["src"])

    # BFS shortest path with parent reconstruction
    prev = {src: None}
    frontier = [src]
    found = src == dst
    for _ in range(max_hops):
        if found:
            break
        nxt = []
        for node in frontier:
            for other in adj.get(node, []):
                if other not in prev:
                    prev[other] = node
                    if other == dst:
                        found = True
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break

    if dst not in prev:
        return {"from": src, "to": dst, "found": False, "path": [],
                "nodes": [], "edges": [], "chain": chain}

    # reconstruct
    path = []
    cur = dst
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    path.reverse()

    nodes = []
    for i, addr in enumerate(path):
        a = store.get_address(conn, case_id, chain, addr) or {"address": addr}
        m = address_metrics(conn, case_id, chain, addr)
        nodes.append({
            "address": addr, "hop": i,
            "label": a.get("label"), "entity": a.get("entity"),
            "category": a.get("category") or "unknown",
            "risk": a.get("risk", "none"), "balance": a.get("balance"),
            "seed": i == 0, "in_value": m["in_value"], "out_value": m["out_value"],
            "tx_count": a.get("tx_count", 0), "taint": a.get("taint", 0),
            "taint_src": a.get("taint_src"), "taint_hops": a.get("taint_hops"),
            "cluster": a.get("cluster"), "on_path": True})
    out_edges = []
    values = []
    for a, b in zip(path, path[1:]):
        e = emap.get((a, b)) or emap.get((b, a))
        if e:
            out_edges.append({"src": e["src"], "dst": e["dst"], "asset": e["asset"],
                              "value": e["total"], "count": e["n"], "on_path": True})
            values.append(e["total"] or 0)
    return {"from": src, "to": dst, "found": True, "path": path,
            "hops": len(path) - 1, "min_value": (min(values) if values else None),
            "directed": directed, "nodes": nodes, "edges": out_edges, "chain": chain}


# --- address clustering --------------------------------------------------

def cluster(conn, case_id, chain):
    """Group addresses into co-ownership clusters (the "who controls this
    wallet" grouping Arkham-style tools surface) with union-find over two
    heuristics:

      * common-input ownership — addresses seen together as inputs to one
        transaction are presumed co-owned (the classic Bitcoin heuristic;
        captured at import time into chain_cospends).
      * shared entity attribution — addresses carrying the same entity label
        belong to the same operator.

    Writes a stable cluster id onto every multi-address cluster's members and
    returns the cluster summaries (singletons are left unclustered)."""
    store.reset_clusters(conn, case_id, chain)
    addrs = store.list_addresses(conn, case_id, chain)
    parent = {a["address"]: a["address"] for a in addrs}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:       # path compression
            parent[x], x = root, parent[x]
        return root

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)   # deterministic root

    # 1. common-input co-spend pairs
    for a, b in store.cospend_pairs(conn, case_id, chain):
        union(a, b)
    # 2. shared entity attribution
    by_entity = {}
    for a in addrs:
        ent = (a.get("entity") or "").strip()
        if ent:
            by_entity.setdefault(ent.lower(), []).append(a["address"])
    for members in by_entity.values():
        for m in members[1:]:
            union(members[0], m)

    groups = {}
    for a in addrs:
        groups.setdefault(find(a["address"]), []).append(a)

    by_addr = {a["address"]: a for a in addrs}
    clusters, n = [], 0
    for root, members in groups.items():
        if len(members) < 2:
            continue
        n += 1
        cid = f"C{n}"
        # representative: prefer an entity name, else the richest label
        ent = next((m.get("entity") for m in members if m.get("entity")), None)
        lab = next((m.get("label") for m in members if m.get("label")), None)
        name = ent or lab or (members[0]["address"][:10] + "…")
        risk = max((m.get("risk", "none") for m in members), key=labels.risk_rank)
        for m in members:
            store.set_cluster(conn, case_id, chain, m["address"], cid)
        clusters.append({
            "id": cid, "name": name, "entity": ent, "size": len(members),
            "risk": risk,
            "addresses": [m["address"] for m in members],
            "members": [{"address": m["address"], "label": m.get("label"),
                         "category": m.get("category"), "risk": m.get("risk", "none")}
                        for m in sorted(members, key=lambda m: -(m.get("tx_count") or 0))],
        })
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return {"clusters": clusters, "count": len(clusters),
            "clustered": sum(c["size"] for c in clusters), "total": len(addrs)}


# --- entity rollup dashboard (Arkham-style "entity page") ----------------

def entity_summary(conn, case_id, chain, cluster_id):
    """Roll a co-ownership cluster up into one entity view: combined holdings
    (+USD), net external flows per asset (intra-cluster edges collapsed), the
    entity's top external counterparties, exchange usage, and risk — the
    "entity page" an investigator reads instead of 20 separate address pages."""
    members = [a for a in store.list_addresses(conn, case_id, chain)
               if a.get("cluster") == cluster_id]
    if not members:
        return None
    mset = {m["address"] for m in members}
    prices = store.prices(conn, case_id)

    # combined holdings per asset
    held = {}
    for m in members:
        for h in store.holdings(conn, case_id, chain, m["address"]):
            held[h["asset"]] = held.get(h["asset"], 0) + (h["balance"] or 0)
    holdings = [{"asset": s, "balance": v, "usd": usd_of(prices, s, v)}
                for s, v in sorted(held.items(), key=lambda kv: -(usd_of(prices, kv[0], kv[1]) or kv[1]))]
    usd_held = round(sum(h["usd"] for h in holdings if h["usd"]), 2)

    # external flows + counterparties (collapse edges within the cluster)
    ext = {}            # external addr -> {assets:{sym:{recv,sent}}, n}
    flow = {}           # asset -> {in, out}
    internal = {}       # asset -> volume moved inside the cluster
    for e in store.edges_aggregated(conn, case_id, chain):
        s_in, d_in = e["src"] in mset, e["dst"] in mset
        vol = e["total"] or 0
        if s_in and d_in:
            internal[e["asset"]] = internal.get(e["asset"], 0) + vol
            continue
        if not (s_in or d_in):
            continue
        other, key = (e["dst"], "out") if s_in else (e["src"], "in")
        flow.setdefault(e["asset"], {"in": 0, "out": 0})[key] += vol
        r = ext.setdefault(other, {"assets": {}, "n": 0})
        ra = r["assets"].setdefault(e["asset"], {"recv": 0, "sent": 0})
        ra["recv" if key == "in" else "sent"] += vol
        r["n"] += e["n"]

    counterpart = []
    exch_in = exch_out = 0.0
    for addr, agg in ext.items():
        a = store.get_address(conn, case_id, chain, addr) or {}
        recv = sum(v["recv"] for v in agg["assets"].values())
        sent = sum(v["sent"] for v in agg["assets"].values())
        cat = (a.get("category") or "").lower()
        per = [{"asset": s, "recv": v["recv"], "sent": v["sent"], "net": v["recv"] - v["sent"]}
               for s, v in sorted(agg["assets"].items())]
        if cat in _EXCHANGE_CATS:
            for s, v in agg["assets"].items():
                exch_out += usd_of(prices, s, v["sent"]) or 0   # entity → exchange
                exch_in += usd_of(prices, s, v["recv"]) or 0
        counterpart.append({"address": addr, "label": a.get("label"),
                            "entity": a.get("entity"), "category": a.get("category"),
                            "risk": a.get("risk", "none"), "n": agg["n"],
                            "recv": recv, "sent": sent, "assets": per})
    counterpart.sort(key=lambda c: sum((usd_of(prices, x["asset"], x["recv"] + x["sent"]) or (x["recv"] + x["sent"])) for x in c["assets"]), reverse=True)

    flows = [{"asset": s, "in": v["in"], "out": v["out"],
              "in_usd": usd_of(prices, s, v["in"]), "out_usd": usd_of(prices, s, v["out"]),
              "internal": internal.get(s, 0)} for s, v in sorted(flow.items())]
    name = next((m.get("entity") for m in members if m.get("entity")), None) \
        or next((m.get("label") for m in members if m.get("label")), None) \
        or (members[0]["address"][:10] + "…")
    risk = max((m.get("risk", "none") for m in members), key=labels.risk_rank)
    return {
        "id": cluster_id, "name": name, "chain": chain, "size": len(members),
        "risk": risk, "max_taint": round(max((m.get("taint") or 0 for m in members), default=0), 3),
        "flagged_members": sum(1 for m in members if labels.risk_rank(m.get("risk")) >= 3
                               or (m.get("category") or "").lower() in _FLAGGED),
        "usd_held": usd_held, "holdings": holdings, "flows": flows,
        "exchange_usd": {"deposited": round(exch_out, 2), "withdrawn": round(exch_in, 2)},
        "counterparties": counterpart[:12],
        "members": [{"address": m["address"], "label": m.get("label"),
                     "category": m.get("category"), "risk": m.get("risk", "none"),
                     "tx_count": m.get("tx_count", 0), "taint": m.get("taint", 0)}
                    for m in sorted(members, key=lambda m: -(m.get("tx_count") or 0))],
    }


# --- token / asset dashboard (Arkham-style "token page") -----------------

def asset_summary(conn, case_id, chain, asset=None):
    """Token page. With `asset`: total moved, top holders, top receivers/senders
    by volume, and net exchange flow for that token. Without `asset`: a per-asset
    overview (totals, holders, top holder) across the case."""
    prices = store.prices(conn, case_id)
    if not asset:
        out = []
        for a in store.assets(conn, case_id, chain):
            sym = a["asset"]
            holders = conn.execute(
                "SELECT COUNT(*) n FROM chain_holdings WHERE case_id=? AND chain=? "
                "AND asset=? AND balance>0", (case_id, chain, sym)).fetchone()["n"]
            top = conn.execute(
                "SELECT address, balance FROM chain_holdings WHERE case_id=? AND chain=? "
                "AND asset=? ORDER BY balance DESC LIMIT 1", (case_id, chain, sym)).fetchone()
            ta = store.get_address(conn, case_id, chain, top["address"]) if top else None
            out.append({"asset": sym, "moved": a["total"], "txns": a["n"],
                        "moved_usd": usd_of(prices, sym, a["total"]), "holders": holders,
                        "top_holder": (ta.get("label") or top["address"]) if ta else None})
        return {"overview": True, "assets": out}

    def _rank(col):
        col = "dst" if col == "dst" else "src"   # whitelist (not user-supplied)
        rows = conn.execute(
            f"SELECT {col} AS a, SUM(value) v, COUNT(*) n FROM chain_transfers "
            f"WHERE case_id=? AND chain=? AND asset=? GROUP BY {col} "
            "ORDER BY v DESC LIMIT 8", (case_id, chain, asset)).fetchall()
        res = []
        for r in rows:
            a = store.get_address(conn, case_id, chain, r["a"]) or {}
            res.append({"address": r["a"], "label": a.get("label"),
                        "category": a.get("category"), "risk": a.get("risk", "none"),
                        "value": r["v"], "n": r["n"], "usd": usd_of(prices, asset, r["v"])})
        return res

    holders = []
    for r in conn.execute(
            "SELECT address, balance FROM chain_holdings WHERE case_id=? AND chain=? "
            "AND asset=? AND balance>0 ORDER BY balance DESC LIMIT 10",
            (case_id, chain, asset)).fetchall():
        a = store.get_address(conn, case_id, chain, r["address"]) or {}
        holders.append({"address": r["address"], "label": a.get("label"),
                        "category": a.get("category"), "risk": a.get("risk", "none"),
                        "balance": r["balance"], "usd": usd_of(prices, asset, r["balance"])})

    tot = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND asset=?", (case_id, chain, asset)).fetchone()
    # net exchange flow for this asset
    ex_in = ex_out = 0.0
    for e in store.edges_aggregated(conn, case_id, chain, asset=asset):
        d = store.get_address(conn, case_id, chain, e["dst"]) or {}
        s = store.get_address(conn, case_id, chain, e["src"]) or {}
        if (d.get("category") or "").lower() in _EXCHANGE_CATS:
            ex_in += e["total"] or 0
        if (s.get("category") or "").lower() in _EXCHANGE_CATS:
            ex_out += e["total"] or 0
    return {"overview": False, "asset": asset, "chain": chain,
            "moved": tot["v"], "txns": tot["n"], "moved_usd": usd_of(prices, asset, tot["v"]),
            "price": prices.get(asset), "holders": holders,
            "top_receivers": _rank("dst"), "top_senders": _rank("src"),
            "exchange_flow": {"in": round(ex_in, 6), "out": round(ex_out, 6),
                              "in_usd": usd_of(prices, asset, ex_in),
                              "out_usd": usd_of(prices, asset, ex_out)}}
