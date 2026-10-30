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

    # 2. addresses with balances / seed flags / tags
    for a in data.get("addresses", []):
        store.upsert_address(
            conn, case_id, chain, a["address"], balance=a.get("balance"),
            asset=asset, source=source, tags=a.get("tags"),
            seed=a.get("seed"), note=a.get("note"))

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

def address_metrics(conn, case_id, chain, address):
    """In/out value + counts + counterparties for one address."""
    out = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND src=?", (case_id, chain, address)).fetchone()
    inn = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM chain_transfers "
        "WHERE case_id=? AND chain=? AND dst=?", (case_id, chain, address)).fetchone()
    cps = conn.execute(
        "SELECT COUNT(DISTINCT cp) n FROM ("
        "  SELECT dst cp FROM chain_transfers WHERE case_id=? AND chain=? AND src=? "
        "  UNION SELECT src cp FROM chain_transfers WHERE case_id=? AND chain=? AND dst=?)",
        (case_id, chain, address, case_id, chain, address)).fetchone()
    return {"out_value": out["v"], "out_count": out["n"],
            "in_value": inn["v"], "in_count": inn["n"],
            "counterparties": cps["n"]}


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
    return a


# --- flow tracing --------------------------------------------------------

def trace_flow(conn, case_id, chain, seed, hops=3, direction="out", min_value=0.0):
    """Breadth-first 'follow the money' from a seed address.

    direction: 'out' (where funds went), 'in' (where they came from), 'both'.
    Returns a subgraph {nodes, edges} suitable for the flow-graph visualizer,
    each node carrying its hop distance from the seed and its label/risk."""
    edges_all = store.edges_aggregated(conn, case_id, chain)
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
        m = address_metrics(conn, case_id, chain, addr)
        nodes.append({
            "address": addr, "hop": h,
            "label": a.get("label"), "entity": a.get("entity"),
            "category": a.get("category") or "unknown",
            "risk": a.get("risk", "none"), "balance": a.get("balance"),
            "seed": bool(a.get("seed")) or addr == seed,
            "in_value": m["in_value"], "out_value": m["out_value"],
            "tx_count": a.get("tx_count", 0),
        })
    edges = [{"src": e["src"], "dst": e["dst"], "asset": e["asset"],
              "value": e["total"], "count": e["n"]} for e in kept_edges.values()]
    return {"seed": seed, "chain": chain, "hops": hops, "direction": direction,
            "nodes": nodes, "edges": edges}


def graph_view(conn, case_id, chain):
    """The full chain graph for a case (all nodes + aggregated edges), enriched
    with per-node metrics. The default visualizer view."""
    g = {"nodes": [], "edges": [], "chain": chain}
    for a in store.list_addresses(conn, case_id, chain):
        m = address_metrics(conn, case_id, chain, a["address"])
        g["nodes"].append({
            "address": a["address"], "hop": 0 if a.get("seed") else 1,
            "label": a.get("label"), "entity": a.get("entity"),
            "category": a.get("category") or "unknown",
            "risk": a.get("risk", "none"), "balance": a.get("balance"),
            "seed": bool(a.get("seed")),
            "in_value": m["in_value"], "out_value": m["out_value"],
            "tx_count": a.get("tx_count", 0)})
    for e in store.edges_aggregated(conn, case_id, chain):
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
