"""Chainscope command-line interface.

    python -m chainscope.cli cases
    python -m chainscope.cli case-create "Theft trace"
    python -m chainscope.cli import   <case_id> --sample eth_theft_trace
    python -m chainscope.cli import   <case_id> --esplora <btc_address>
    python -m chainscope.cli trace    <case_id> <seed> [--hops 3] [--dir out]
    python -m chainscope.cli address  <case_id> <address>
    python -m chainscope.cli risk     <case_id>
    python -m chainscope.cli flags    <case_id>
"""
import argparse
import os

from . import db, store, analyze, sources

DB_PATH = os.environ.get("CHAINSCOPE_DB", "chainscope.db")


def _conn():
    return db.connect(DB_PATH)


def main(argv=None):
    global DB_PATH
    ap = argparse.ArgumentParser(prog="chainscope")
    ap.add_argument("--db", default=DB_PATH)
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("cases")
    p = sp.add_parser("case-create"); p.add_argument("name"); p.add_argument("--note")

    p = sp.add_parser("import"); p.add_argument("case_id", type=int)
    p.add_argument("--chain", default="eth")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample"); g.add_argument("--esplora"); g.add_argument("--evm")
    p.add_argument("--api-base"); p.add_argument("--api-key")

    p = sp.add_parser("trace"); p.add_argument("case_id", type=int); p.add_argument("seed")
    p.add_argument("--chain", default="eth"); p.add_argument("--hops", type=int, default=3)
    p.add_argument("--dir", default="out", choices=["out", "in", "both"])

    p = sp.add_parser("address"); p.add_argument("case_id", type=int); p.add_argument("address")
    p.add_argument("--chain", default="eth")

    p = sp.add_parser("risk"); p.add_argument("case_id", type=int); p.add_argument("--chain", default="eth")
    p = sp.add_parser("flags"); p.add_argument("case_id", type=int)

    args = ap.parse_args(argv)
    DB_PATH = args.db
    conn = _conn()

    if args.cmd == "cases":
        for c in db.list_cases(conn):
            print(f"#{c['id']:>3}  {c['name']}  ({c['created_at']})")

    elif args.cmd == "case-create":
        cid = db.create_case(conn, args.name, args.note)
        print(f"created case #{cid}")

    elif args.cmd == "import":
        if args.sample:
            data = sources.load_sample(args.sample)
        elif args.esplora:
            data = sources.fetch_esplora(args.esplora)
        else:
            data = sources.fetch_evm(args.evm, args.api_base, args.api_key or "")
        res = analyze.ingest(conn, args.case_id, data)
        print(f"imported {res['transfers']} transfers / {res['addresses']} addresses "
              f"on {res['chain']}")

    elif args.cmd == "trace":
        tr = analyze.trace_flow(conn, args.case_id, args.chain, args.seed,
                                hops=args.hops, direction=args.dir)
        for n in sorted(tr["nodes"], key=lambda x: x["hop"]):
            flag = f"  [!] {n['risk']}" if n["risk"] not in ("none", "low") else ""
            print(f"  hop {n['hop']}  {n['label'] or n['address']}  [{n['category']}]{flag}")
        print(f"{len(tr['nodes'])} nodes / {len(tr['edges'])} edges")

    elif args.cmd == "address":
        info = analyze.inspect(conn, args.case_id, args.chain, args.address)
        if not info:
            print("not found"); return
        m = info["metrics"]
        print(f"{info.get('label') or info['address']}  [{info.get('category')}] "
              f"risk={info.get('risk')}")
        print(f"  balance={info.get('balance')}  in={m['in_value']:g}/{m['in_count']}  "
              f"out={m['out_value']:g}/{m['out_count']}  peers={m['counterparties']}")

    elif args.cmd == "risk":
        new, merged = analyze.risk_scan(conn, args.case_id, args.chain)
        print(f"{new} new / {merged} merged flags")

    elif args.cmd == "flags":
        for f in store.list_flags(conn, args.case_id):
            print(f"  [{f['severity']:>8}] {f['title']}")


if __name__ == "__main__":
    main()
