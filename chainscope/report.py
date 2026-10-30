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
    case = db.get_case(conn, case_id) or {"id": case_id}
    addresses = store.list_addresses(conn, case_id, chain)
    for a in addresses:
        a["holdings"] = store.holdings(conn, case_id, chain, a["address"])
    transfers = store.list_transfers(conn, case_id, chain, limit=1000000)
    flags = store.list_flags(conn, case_id)
    assets = store.assets(conn, case_id, chain)
    edges = store.edges_aggregated(conn, case_id, chain)
    tl = store.timeline(conn, case_id, chain)

    sev_counts = {}
    for f in flags:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
    tainted = [a for a in addresses if (a.get("taint") or 0) >= 0.15]
    max_taint = max((a.get("taint") or 0 for a in addresses), default=0)

    return {
        "generated_at": db.now(), "chain": chain, "case": case,
        "summary": {
            "wallets": len(addresses), "transactions": len(transfers),
            "assets": assets, "flags": len(flags), "flags_by_severity": sev_counts,
            "flagged_entities": sum(1 for a in addresses if _is_flagged(a)),
            "tainted_wallets": len(tainted), "max_taint": round(max_taint, 3),
            "window": {"start": tl["min"], "end": tl["max"]},
        },
        "assets": assets, "addresses": addresses, "edges": edges,
        "transactions": transfers, "flags": flags,
    }


def _tbl(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


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
    assets_str = ", ".join(f"{a['asset']} {_g(a['total'])}" for a in d["assets"]) or "—"
    sev = s["flags_by_severity"]
    sev_str = ", ".join(f"{k} {sev[k]}" for k in _SEV_ORDER if sev.get(k)) or "none"
    L.append(f"- **Wallets:** {s['wallets']}")
    L.append(f"- **Transactions:** {s['transactions']}")
    L.append(f"- **Assets moved:** {assets_str}")
    L.append(f"- **Risk flags:** {s['flags']} ({sev_str})")
    L.append(f"- **Flagged entities:** {s['flagged_entities']}")
    L.append(f"- **Tainted wallets:** {s['tainted_wallets']} (max {int(s['max_taint']*100)}% exposure)")
    L.append("")

    # flags
    if d["flags"]:
        L.append("## Risk flags")
        L.append("")
        L.append(_tbl(["Severity", "Finding", "Score"],
                      [[f["severity"], f["title"], round(f.get("score") or 0)] for f in d["flags"]]))
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

    # fund flows
    if d["edges"]:
        L.append("## Fund flows (aggregated)")
        L.append("")
        lab = {a["address"]: (a.get("label") or a["address"][:10] + "…") for a in d["addresses"]}
        rows = sorted(([lab.get(e["src"], e["src"]), lab.get(e["dst"], e["dst"]),
                        e["asset"], _g(e["total"]), e["n"]] for e in d["edges"]),
                      key=lambda r: r[2])
        L.append(_tbl(["From", "To", "Asset", "Value", "Txns"], rows))
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
