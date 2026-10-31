"""Schema/migration, case lifecycle, flag chain-scoping, and report export."""
from chainscope import db, analyze, store, report


def test_schema_has_new_columns_and_tables(conn):
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(chain_addresses)")}
    assert {"taint", "taint_src", "taint_hops", "cluster"} <= acols
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(flags)")}
    assert "chain" in fcols
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chain_cospends", "chain_prices", "chain_holdings"} <= tables


def test_flags_are_chain_scoped(eth_case):
    conn, cid = eth_case
    analyze.risk_scan(conn, cid, "eth")
    assert len(store.list_flags(conn, cid, "eth")) > 0
    assert len(store.list_flags(conn, cid, "btc")) == 0    # none on btc
    assert len(store.list_flags(conn, cid)) == len(store.list_flags(conn, cid, "eth"))


def test_flag_triage_status(eth_case):
    conn, cid = eth_case
    analyze.risk_scan(conn, cid, "eth")
    flags = store.list_flags(conn, cid, "eth")
    fid = flags[0]["id"]
    assert (flags[0]["status"] or "open") == "open"
    assert store.set_flag_status(conn, cid, fid, "false_positive") is True
    assert store.set_flag_status(conn, cid, fid, "bogus") is False      # rejected
    assert store.set_flag_status(conn, cid, 999999, "confirmed") is False  # no such flag
    again = {f["id"]: f["status"] for f in store.list_flags(conn, cid, "eth")}
    assert again[fid] == "false_positive"
    assert store.set_flag_status(conn, cid, fid, "open") is True         # reopen
    md = report.build_markdown(conn, cid, "eth")
    assert "Status" in md   # status column present in the flags table


def test_delete_case_cascades(eth_case):
    conn, cid = eth_case
    analyze.risk_scan(conn, cid, "eth")
    db.delete_case(conn, cid)
    for t in ("chain_addresses", "chain_transfers", "chain_holdings", "flags"):
        n = conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE case_id=?", (cid,)).fetchone()["c"]
        assert n == 0
    assert db.get_case(conn, cid) is None


def test_report_json(eth_case):
    conn, cid = eth_case
    analyze.risk_scan(conn, cid, "eth")
    analyze.cluster(conn, cid, "eth")
    j = report.build_json(conn, cid, "eth")
    assert j["summary"]["wallets"] == 6
    assert j["summary"]["transactions"] == 8
    assert j["summary"]["usd_held"] > 0
    assert j["summary"]["clusters"] == 1
    assert j["prices"]["ETH"] == 3400.0


def test_report_markdown_has_diagram_and_sections(eth_case):
    conn, cid = eth_case
    analyze.risk_scan(conn, cid, "eth")
    analyze.cluster(conn, cid, "eth")
    md = report.build_markdown(conn, cid, "eth")
    assert md.startswith("# Chainscope evidence report")
    assert "```mermaid" in md
    assert "## Fund flows" in md
    assert "## Co-ownership clusters" in md
    assert "Holdings value" in md   # USD summary line
