"""End-to-end demo of Chainscope (offline, no network / keys).

Proves: create a case -> load sample -> ingest into the graph -> wallet metrics
-> multi-hop flow trace -> risk flags.

    python demo.py
"""
import os
import tempfile

from chainscope import db, sources, analyze, store, report


def hr(t):
    print("\n" + t); print("-" * len(t))


def main():
    path = os.path.join(tempfile.gettempdir(), "chainscope_demo.db")
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)

    cid = db.create_case(conn, "Chain tracing demo", note="IR team")
    print(f"case #{cid}  (db={path})")

    hr("1. Ingest sample dataset")
    data = sources.load_sample("eth_theft_trace")
    res = analyze.ingest(conn, cid, data)
    print(f"   chain={res['chain']}  addresses={res['addresses']}  transfers={res['transfers']}")

    hr("2. Wallet metrics (the drained treasury)")
    seed = "0x1111111111111111111111111111111111111111"
    info = analyze.inspect(conn, cid, "eth", seed)
    m = info["metrics"]
    print(f"   {info.get('label')}  [{info.get('category')}] balance={info.get('balance')} ETH")
    print(f"   out={m['out_value']:g} ETH / {m['out_count']} tx   "
          f"in={m['in_value']:g} ETH / {m['in_count']} tx   peers={m['counterparties']}")

    hr("3. Trace the money (3 hops outward)")
    tr = analyze.trace_flow(conn, cid, "eth", seed, hops=3, direction="out")
    for n in sorted(tr["nodes"], key=lambda x: x["hop"]):
        tag = f" [!] {n['risk']}" if n["risk"] not in ("none", "low") else ""
        print(f"   hop {n['hop']}  {n['label'] or n['address']}  [{n['category']}]{tag}")
    print(f"   subgraph: {len(tr['nodes'])} nodes / {len(tr['edges'])} edges")

    hr("4. Risk scan -> flags")
    new, merged = analyze.risk_scan(conn, cid, "eth")
    print(f"   flags: {new} new, {merged} merged")
    for f in store.list_flags(conn, cid):
        print(f"   [{f['severity']:>8}] {f['title']}")

    hr("5. Taint propagation (multi-hop exposure)")
    tr = analyze.taint_scan(conn, cid, "eth")
    print(f"   sources={tr['sources']}  tainted downstream={tr['tainted']}  new flags={tr['new_flags']}")
    for a in sorted(store.list_addresses(conn, cid, "eth"), key=lambda x: -(x.get("taint") or 0)):
        if a.get("taint"):
            print(f"   {int(a['taint']*100):>3}%  {a.get('label') or a['address']}  "
                  f"({a.get('taint_hops')} hop(s) from {a.get('taint_src')})")

    hr("6. Path finder (victim -> exchange cashout)")
    p = analyze.find_paths(conn, cid, "eth",
                           "0x1111111111111111111111111111111111111111",
                           "0x5555555555555555555555555555555555555555")
    if p["found"]:
        chain_str = " -> ".join((store.get_address(conn, cid, "eth", a) or {}).get("label") or a[:10] for a in p["path"])
        print(f"   {p['hops']} hops, min value {p['min_value']} ETH")
        print(f"   {chain_str}")
    else:
        print("   no path")

    hr("7. Re-run is idempotent")
    analyze.ingest(conn, cid, data)
    new2, merged2 = analyze.risk_scan(conn, cid, "eth")
    tot = conn.execute("SELECT COUNT(*) c FROM chain_transfers WHERE case_id=?",
                       (cid,)).fetchone()["c"]
    print(f"   after re-run: {new2} new / {merged2} merged flags, {tot} transfers (no dupes)")

    hr("8. Multi-asset (ERC-20) token flows")
    cid2 = db.create_case(conn, "Token launder demo")
    analyze.ingest(conn, cid2, sources.load_sample("eth_token_launder"))
    print("   assets in case:", [f"{a['asset']}({a['n']})" for a in store.assets(conn, cid2, "eth")])
    ex = "0xb2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
    info = analyze.inspect(conn, cid2, "eth", ex)
    print("   exploiter holdings:", {h["asset"]: h["balance"] for h in info["holdings"]})
    print("   exploiter by-asset:",
          {b["asset"]: f"+{b['recv']:g}/-{b['sent']:g}" for b in info["by_asset"]})
    for asset in ("USDC", "ETH", "USDT"):
        tr = analyze.trace_flow(conn, cid2, "eth", ex, hops=3, direction="out", asset=asset)
        print(f"   {asset:>4} outflow trace: {len(tr['nodes'])} nodes / {len(tr['edges'])} edges")
    analyze.risk_scan(conn, cid2, "eth")
    print("   token-flow flags:")
    for f in store.list_flags(conn, cid2):
        print(f"     [{f['severity']:>8}] {f['title']}")

    hr("9. Timeline window (watch the flow unfold)")
    tl = store.timeline(conn, cid2, "eth")
    print(f"   {tl['count']} events  {tl['min']} .. {tl['max']}")
    early = analyze.graph_view(conn, cid2, "eth", before="2026-03-04T01:41:00Z")
    full = analyze.graph_view(conn, cid2, "eth")
    print(f"   graph before 01:41: {len(early['nodes'])} nodes / {len(early['edges'])} edges")
    print(f"   graph full:         {len(full['nodes'])} nodes / {len(full['edges'])} edges")

    hr("10. Per-asset counterparty nets (the exploiter EOA)")
    info = analyze.inspect(conn, cid2, "eth", ex)
    for c in info["counterparties"][:4]:
        nets = " ".join(f"{a['asset']} {a['net']:+g}" for a in c["assets"])
        print(f"   {(c['label'] or c['address'])[:22]:>22}: {nets}")

    hr("11. Address clustering (co-spend + shared entity)")
    cid3 = db.create_case(conn, "BTC peel + cluster demo")
    analyze.ingest(conn, cid3, sources.load_sample("btc_peel_cluster"))
    cl = analyze.cluster(conn, cid3, "btc")
    print(f"   {cl['count']} cluster(s), {cl['clustered']}/{cl['total']} addresses grouped")
    for c in cl["clusters"]:
        print(f"   [{c['id']}] {c['name']}  ({c['size']} addrs, risk={c['risk']})")
    # ETH launder case clusters by shared entity ("Unknown Actor")
    cle = analyze.cluster(conn, cid2, "eth")
    print(f"   eth case: {cle['count']} cluster(s) via shared entity")

    hr("12. Evidence report (export, with flow diagram + clusters)")
    analyze.taint_scan(conn, cid2, "eth")
    j = report.build_json(conn, cid2, "eth")
    md = report.build_markdown(conn, cid2, "eth")
    print(f"   json summary: {j['summary']['wallets']} wallets, {j['summary']['transactions']} txns, "
          f"{j['summary']['flags']} flags, {j['summary']['tainted_wallets']} tainted, "
          f"{j['summary']['clusters']} clusters")
    print(f"   markdown: {len(md)} chars / {md.count(chr(10)) + 1} lines; "
          f"mermaid diagram embedded: {'```mermaid' in md}")

    hr("13. USD valuation + chain-scoped flags")
    info = analyze.inspect(conn, cid2, "eth", ex)
    print(f"   exploiter holdings value: ${info['usd_held']:,.0f}")
    print(f"   prices: {info['prices']}")
    print(f"   eth flags: {len(store.list_flags(conn, cid2, 'eth'))}  "
          f"btc flags on this (eth) case: {len(store.list_flags(conn, cid2, 'btc'))}")

    hr("14. Entity rollup dashboard")
    ent = analyze.entity_summary(conn, cid2, "eth", analyze.cluster(conn, cid2, "eth")["clusters"][0]["id"])
    print(f"   entity '{ent['name']}': {ent['size']} wallets, ${ent['usd_held']:,.0f} held, risk={ent['risk']}")
    print(f"   net flows: " + ", ".join(f"{f['asset']} in {f['in']:g}/out {f['out']:g}" for f in ent["flows"]))
    print(f"   exchange: deposited ${ent['exchange_usd']['deposited']:,.0f}")
    print(f"   top external counterparties: " + ", ".join((c['label'] or c['address'][:10]) for c in ent["counterparties"][:3]))

    hr("15. Token / asset dashboard (USDC)")
    ad = analyze.asset_summary(conn, cid2, "eth", "USDC")
    print(f"   USDC moved {ad['moved']:g} (${ad['moved_usd']:,.0f}) across {ad['txns']} tx")
    print(f"   top holders: " + ", ".join(f"{(h['label'] or h['address'][:8])} {h['balance']:g}" for h in ad["holders"][:3]))
    print(f"   top receivers: " + ", ".join(f"{(r['label'] or r['address'][:8])}" for r in ad["top_receivers"][:3]))
    ov = analyze.asset_summary(conn, cid2, "eth")
    print(f"   overview assets: " + ", ".join(f"{a['asset']}(${(a['moved_usd'] or 0):,.0f})" for a in ov["assets"]))

    hr("16. Balance history (exploiter, USDC)")
    bs = analyze.balance_series(conn, cid2, "eth", ex, asset="USDC")
    print(f"   {len(bs)} points; USDC balance path: " + " -> ".join(f"{p['balance']:g}" for p in bs))

    hr("17. Transfers feed (filterable transaction list)")
    f_all = analyze.transfers_feed(conn, cid2, "eth", sort="value", limit=3)
    print(f"   all: {f_all['count']} of {f_all['total']}, largest first:")
    for t in f_all["transfers"]:
        print(f"     {(t['src']['label'] or t['src']['address'][:8])} -> "
              f"{(t['dst']['label'] or t['dst']['address'][:8])}  {t['value']:g} {t['asset']} (${(t['usd'] or 0):,.0f})")
    f_out = analyze.transfers_feed(conn, cid2, "eth", address=ex, direction="out")
    print(f"   exploiter outflows: {f_out['total']}  ({', '.join(t['counterparty']['label'] or t['counterparty']['address'][:8] for t in f_out['transfers'])})")
    f_usdc = analyze.transfers_feed(conn, cid2, "eth", asset="USDC", min_value=1000000)
    print(f"   USDC >= 1M: {f_usdc['total']} transfer(s)")

    hr("18. Flag triage (confirm / dismiss a finding)")
    flags = store.list_flags(conn, cid2, "eth")
    openc = sum(1 for f in flags if (f.get("status") or "open") == "open")
    print(f"   {len(flags)} flags, {openc} open")
    fp = flags[0]
    store.set_flag_status(conn, cid2, fp["id"], "false_positive")
    store.set_flag_status(conn, cid2, flags[1]["id"], "confirmed")
    flags2 = store.list_flags(conn, cid2, "eth")
    by_status = {}
    for f in flags2:
        by_status[f.get("status") or "open"] = by_status.get(f.get("status") or "open", 0) + 1
    print(f"   after triage: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())))
    store.set_flag_status(conn, cid2, fp["id"], "open")  # reopen the dismissed one
    print(f"   reopened flag #{fp['id']}: {fp['title'][:48]}")

    print("\nOK - chainscope works end to end, offline.")


if __name__ == "__main__":
    main()
