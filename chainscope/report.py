"""Case export — turn an investigation into a shareable evidence report.

build_json  -> the full structured case (meta, assets, wallets+holdings, edges,
               transactions, flags) for machine use / archival.
build_markdown -> a readable forensic write-up rendered from the same data.

Read-only; both are snapshots of the graph at export time.
"""
from . import db, store, labels

_FLAGGED = {"mixer", "tumbler", "darkmarket", "ransomware", "scam", "sanctioned"}
_SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _is_flagged(a):
    return (a.get("category") or "").lower() in _FLAGGED or labels.risk_rank(a.get("risk")) >= 3


def build_json(conn, case_id, chain="eth"):
    from .analyze import usd_of
    case = db.get_case(conn, case_id) or {"id": case_id}
    prices = store.prices(conn, case_id)
    addresses = store.list_addresses(conn, case_id, chain)
    usd_held = 0.0
    for a in addresses:
        a["holdings"] = store.holdings(conn, case_id, chain, a["address"])
        for h in a["holdings"]:
            h["usd"] = usd_of(prices, h["asset"], h["balance"])
            usd_held += h["usd"] or 0
    transfers = store.list_transfers(conn, case_id, chain, limit=1000000)
    flags = store.list_flags(conn, case_id, chain)
    assets = store.assets(conn, case_id, chain)
    for a in assets:
        a["usd"] = usd_of(prices, a["asset"], a["total"])
    edges = store.edges_aggregated(conn, case_id, chain)
    tl = store.timeline(conn, case_id, chain)

    sev_counts = {}
    for f in flags:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
    tainted = [a for a in addresses if (a.get("taint") or 0) >= 0.15]
    max_taint = max((a.get("taint") or 0 for a in addresses), default=0)
    clusters = _clusters_from(addresses)

    return {
        "generated_at": db.now(), "chain": chain, "case": case,
        "summary": {
            "wallets": len(addresses), "transactions": len(transfers),
            "assets": assets, "flags": len(flags), "flags_by_severity": sev_counts,
            "flagged_entities": sum(1 for a in addresses if _is_flagged(a)),
            "tainted_wallets": len(tainted), "max_taint": round(max_taint, 3),
            "clusters": len(clusters), "usd_held": round(usd_held, 2),
            "window": {"start": tl["min"], "end": tl["max"]},
        },
        "prices": prices,
        "assets": assets, "addresses": addresses, "edges": edges,
        "transactions": transfers, "flags": flags, "clusters": clusters,
    }


def _clusters_from(addresses):
    """Summarize the co-ownership clusters already recorded on the addresses
    (set by the last clustering run). Read-only — the report reflects current
    state and never recomputes."""
    groups = {}
    for a in addresses:
        cid = a.get("cluster")
        if cid:
            groups.setdefault(cid, []).append(a)
    out = []
    for cid, members in groups.items():
        if len(members) < 2:
            continue
        ent = next((m.get("entity") for m in members if m.get("entity")), None)
        lab = next((m.get("label") for m in members if m.get("label")), None)
        risk = max((m.get("risk", "none") for m in members), key=labels.risk_rank)
        out.append({"id": cid, "name": ent or lab or (members[0]["address"][:10] + "…"),
                    "size": len(members), "risk": risk,
                    "addresses": [m["address"] for m in members]})
    out.sort(key=lambda c: c["size"], reverse=True)
    return out


def _tbl(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _usd(v):
    if not v:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1e9:
        return f"${n/1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"${n/1e6:.2f}M"
    if abs(n) >= 1e3:
        return f"${n/1e3:.1f}k"
    return f"${n:,.2f}"


def _g(v):
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}k"
    return f"{n:g}"


def _mnode(label):
    """Mermaid-safe node label: strip quotes/newlines, cap length."""
    s = str(label).replace('"', "'").replace("\n", " ").strip()
    return s[:26] + "…" if len(s) > 27 else s


def _flow_diagram(edges, lab, addresses, top=16):
    """A Mermaid flowchart of the largest flows — renders inline on GitHub /
    VS Code / any Mermaid-aware viewer, with flagged destinations highlighted.
    ASCII-only so it also prints safely to a Windows console."""
    es = sorted(edges, key=lambda e: e["total"] or 0, reverse=True)[:top]
    if not es:
        return ""
    risky = {a["address"] for a in addresses if _is_flagged(a)}
    nid, lines = {}, ["```mermaid", "flowchart LR"]
    for addr in {e["src"] for e in es} | {e["dst"] for e in es}:
        nid[addr] = f"n{len(nid)}"
        lines.append(f'  {nid[addr]}["{_mnode(lab.get(addr, addr))}"]')
    for e in es:
        val = f'{_g(e["total"])} {e["asset"] or ""}'.strip()
        lines.append(f'  {nid[e["src"]]} -- "{val}" --> {nid[e["dst"]]}')
    flagged = [nid[a] for a in risky if a in nid]
    if flagged:
        lines.append("  classDef flagged stroke:#ef4444,stroke-width:2px,fill:#3a1414,color:#fff;")
        lines.append("  class " + ",".join(flagged) + " flagged;")
    lines.append("```")
    return "\n".join(lines)


def build_markdown(conn, case_id, chain="eth"):
    d = build_json(conn, case_id, chain)
    s, case = d["summary"], d["case"]
    L = []
    L.append(f"# Chainscope evidence report — {case.get('name', 'case ' + str(case_id))}")
    L.append("")
    L.append(f"**Case:** #{case.get('id', case_id)} · {case.get('name', '')}  ")
    L.append(f"**Chain:** {chain}  ")
    L.append(f"**Generated:** {d['generated_at']}  ")
    if s["window"]["start"]:
        L.append(f"**Activity window:** {s['window']['start']} → {s['window']['end']}  ")
    if case.get("note"):
        L.append(f"**Note:** {case['note']}  ")
    L.append("")
    L.append("> Read-only analysis of public on-chain data. Attributions marked "
             "*(public)* are from a built-in reference set; others were supplied "
             "by the imported dataset or the analyst.")
    L.append("")

    # summary
    L.append("## Summary")
    L.append("")
    assets_str = ", ".join(
        f"{a['asset']} {_g(a['total'])}" + (f" ({_usd(a.get('usd'))})" if a.get("usd") else "")
        for a in d["assets"]) or "—"
    sev = s["flags_by_severity"]
    sev_str = ", ".join(f"{k} {sev[k]}" for k in _SEV_ORDER if sev.get(k)) or "none"
    L.append(f"- **Wallets:** {s['wallets']}")
    if s.get("usd_held"):
        L.append(f"- **Holdings value:** {_usd(s['usd_held'])} (price snapshot)")
    L.append(f"- **Transactions:** {s['transactions']}")
    L.append(f"- **Assets moved:** {assets_str}")
    L.append(f"- **Risk flags:** {s['flags']} ({sev_str})")
    L.append(f"- **Flagged entities:** {s['flagged_entities']}")
    L.append(f"- **Tainted wallets:** {s['tainted_wallets']} (max {int(s['max_taint']*100)}% exposure)")
    if s.get("clusters"):
        L.append(f"- **Co-ownership clusters:** {s['clusters']}")
    L.append("")

    # flags
    if d["flags"]:
        L.append("## Risk flags")
        L.append("")
        L.append(_tbl(["Severity", "Finding", "Score", "Status"],
                      [[f["severity"], f["title"], round(f.get("score") or 0),
                        (f.get("status") or "open").replace("_", " ")] for f in d["flags"]]))
        L.append("")

    # flagged / notable entities
    notable = [a for a in d["addresses"]
               if _is_flagged(a) or a.get("label") or (a.get("taint") or 0) >= 0.15]
    if notable:
        L.append("## Flagged & notable entities")
        L.append("")
        rows = []
        for a in notable:
            taint = f"{int((a.get('taint') or 0)*100)}%" if (a.get("taint") or 0) >= 0.15 else "—"
            rows.append([a["address"], a.get("label") or "—", a.get("entity") or "—",
                         a.get("category") or "—", a.get("risk") or "none", taint])
        L.append(_tbl(["Address", "Label", "Entity", "Category", "Risk", "Taint"], rows))
        L.append("")

    # fund flows — grouped by asset, each largest-first
    if d["edges"]:
        lab = {a["address"]: (a.get("label") or a["address"][:10] + "…") for a in d["addresses"]}
        L.append("## Fund flows")
        L.append("")
        L.append(_flow_diagram(d["edges"], lab, d["addresses"]))
        L.append("")
        by_asset = {}
        for e in d["edges"]:
            by_asset.setdefault(e["asset"] or "—", []).append(e)
        for sym in sorted(by_asset):
            es = sorted(by_asset[sym], key=lambda e: e["total"] or 0, reverse=True)
            total = sum(e["total"] or 0 for e in es)
            L.append(f"### {sym} — {_g(total)} across {len(es)} flow(s)")
            L.append("")
            rows = [[lab.get(e["src"], e["src"]), lab.get(e["dst"], e["dst"]),
                     _g(e["total"]), e["n"]] for e in es]
            L.append(_tbl(["From", "To", "Value", "Txns"], rows))
            L.append("")

    # co-ownership clusters
    if d.get("clusters"):
        L.append("## Co-ownership clusters")
        L.append("")
        L.append("> Addresses grouped by common-input ownership and shared entity "
                 "attribution — presumed to be controlled by one operator.")
        L.append("")
        rows = [[c["id"], c["name"], c["size"], c["risk"]] for c in d["clusters"]]
        L.append(_tbl(["Cluster", "Attribution", "Addresses", "Risk"], rows))
        L.append("")

    # appendix: transactions
    if d["transactions"]:
        L.append("## Appendix — transactions")
        L.append("")
        rows = [[t.get("txid", ""), t["src"], t["dst"], t.get("asset", ""),
                 _g(t.get("value")), t.get("ts") or "—", t.get("block") or "—"]
                for t in d["transactions"]]
        L.append(_tbl(["Txid", "From", "To", "Asset", "Value", "Time", "Block"], rows))
        L.append("")

    L.append("---")
    L.append(f"*Generated by Chainscope — {d['generated_at']}.*")
    return "\n".join(L)
