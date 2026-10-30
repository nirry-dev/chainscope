"""On-chain analysis: ingest a dataset into the graph, compute wallet metrics,
trace fund flows across hops, and raise flags for risky exposure.

Read-only throughout — this inspects public transaction history, it never
constructs or sends a transaction.
"""
from . import store, labels

_SEV_SCORE = {"critical": 100, "high": 70, "medium": 45, "low": 20, "info": 8}


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


def counterparties(conn, case_id, chain, address):
    """Direct counterparties with net flow direction and volume — the
    address-inspector table."""
    rows = {}
    for e in store.edges_aggregated(conn, case_id, chain):
        if e["src"] == address:
            rows.setdefault(e["dst"], {"sent": 0, "recv": 0, "n": 0})
            rows[e["dst"]]["sent"] += e["total"] or 0
            rows[e["dst"]]["n"] += e["n"]
        elif e["dst"] == address:
            rows.setdefault(e["src"], {"sent": 0, "recv": 0, "n": 0})
            rows[e["src"]]["recv"] += e["total"] or 0
            rows[e["src"]]["n"] += e["n"]
    out = []
    for addr, agg in rows.items():
        a = store.get_address(conn, case_id, chain, addr) or {}
        out.append({"address": addr, "label": a.get("label"),
                    "entity": a.get("entity"), "category": a.get("category"),
                    "risk": a.get("risk", "none"), **agg,
                    "net": agg["recv"] - agg["sent"]})
    out.sort(key=lambda r: r["sent"] + r["recv"], reverse=True)
    return out


def inspect(conn, case_id, chain, address):
    """Everything the UI shows when you click a node."""
    a = store.get_address(conn, case_id, chain, address)
    if not a:
        return None
    a["metrics"] = address_metrics(conn, case_id, chain, address)
    a["counterparties"] = counterparties(conn, case_id, chain, address)
    a["holdings"] = store.holdings(conn, case_id, chain, address)
    a["by_asset"] = asset_breakdown(conn, case_id, chain, address)
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
            "taint_hops": a.get("taint_hops"),
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
            "taint_hops": a.get("taint_hops")})
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

    return store.record_flags(conn, case_id, items)


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
    new, _ = store.record_flags(conn, case_id, items)
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
            "on_path": True})
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
