"""End-to-end demo of Chainscope (offline, no network / keys).

Proves: create a case -> load sample -> ingest into the graph -> wallet metrics
-> multi-hop flow trace -> risk flags.

    python demo.py
"""
import os
import tempfile

from chainscope import db, sources, analyze, store


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

    print("\nOK - chainscope works end to end, offline.")


if __name__ == "__main__":
    main()
