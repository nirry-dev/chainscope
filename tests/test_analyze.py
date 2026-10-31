"""Core analysis: ingest, metrics, tracing, taint, clustering, dashboards."""
from chainscope import analyze, store

from conftest import EXPLOITER, INTERMEDIARY, DEX_ROUTER, LUMEN, ZENITH


def test_ingest_counts(eth_case):
    conn, cid = eth_case
    assert len(store.list_addresses(conn, cid, "eth")) == 6
    assert store.edges_aggregated(conn, cid, "eth")  # non-empty
    n = conn.execute("SELECT COUNT(*) c FROM chain_transfers WHERE case_id=?",
                     (cid,)).fetchone()["c"]
    assert n == 8


def test_ingest_idempotent(eth_case):
    conn, cid = eth_case
    before = conn.execute("SELECT COUNT(*) c FROM chain_transfers WHERE case_id=?",
                          (cid,)).fetchone()["c"]
    from chainscope import sources
    analyze.ingest(conn, cid, sources.load_sample("eth_token_launder"))
    after = conn.execute("SELECT COUNT(*) c FROM chain_transfers WHERE case_id=?",
                         (cid,)).fetchone()["c"]
    assert after == before  # re-import must not duplicate


def test_usd_valuation(eth_case):
    conn, cid = eth_case
    info = analyze.inspect(conn, cid, "eth", EXPLOITER)
    # 300k USDC * 1 + 100 ETH * 3400 = 640,000
    assert info["usd_held"] == 640000.0
    assert info["prices"]["ETH"] == 3400.0


def test_per_asset_counterparties(eth_case):
    conn, cid = eth_case
    cps = {c["address"]: c for c in analyze.counterparties(conn, cid, "eth", EXPLOITER)}
    dex = cps[DEX_ROUTER]
    nets = {a["asset"]: a["net"] for a in dex["assets"]}
    assert nets["ETH"] == 300        # received 300 ETH
    assert nets["USDC"] == -800000   # sent 800k USDC


def test_balance_series(eth_case):
    conn, cid = eth_case
    bs = analyze.balance_series(conn, cid, "eth", EXPLOITER, asset="USDC")
    assert [p["balance"] for p in bs] == [2000000, 1200000, 300000]


def test_risk_scan_flags_mixer(eth_case):
    conn, cid = eth_case
    new, merged = analyze.risk_scan(conn, cid, "eth")
    assert new > 0
    flags = store.list_flags(conn, cid, "eth")
    assert any(f["severity"] == "critical" for f in flags)
    # re-run merges, does not duplicate
    new2, merged2 = analyze.risk_scan(conn, cid, "eth")
    assert new2 == 0 and merged2 == merged + new


def test_taint_propagation(eth_case):
    conn, cid = eth_case
    res = analyze.taint_scan(conn, cid, "eth")
    assert res["sources"] >= 1
    assert res["tainted"] >= 1
    tainted = [a for a in store.list_addresses(conn, cid, "eth") if (a.get("taint") or 0) > 0]
    assert tainted


def test_path_finder(eth_case):
    conn, cid = eth_case
    p = analyze.find_paths(conn, cid, "eth", LUMEN, ZENITH)
    assert p["found"] is True
    assert p["path"][0] == LUMEN and p["path"][-1] == ZENITH
    assert p["hops"] >= 1


def test_cluster_eth_shared_entity(eth_case):
    conn, cid = eth_case
    res = analyze.cluster(conn, cid, "eth")
    assert res["count"] == 1
    c = res["clusters"][0]
    assert c["size"] == 2
    assert set(c["addresses"]) == {EXPLOITER, INTERMEDIARY}


def test_cluster_btc_cospend(btc_case):
    conn, cid = btc_case
    res = analyze.cluster(conn, cid, "btc")
    # 3 co-spent actor wallets + 2 shared-entity exchange wallets
    assert res["count"] == 2
    assert res["clustered"] == 5
    sizes = sorted(c["size"] for c in res["clusters"])
    assert sizes == [2, 3]


def test_entity_summary(eth_case):
    conn, cid = eth_case
    analyze.cluster(conn, cid, "eth")
    ent = analyze.entity_summary(conn, cid, "eth", "C1")
    assert ent["size"] == 2
    assert ent["usd_held"] == 660000.0          # 640k exploiter + 20k intermediary USDC
    assert ent["exchange_usd"]["deposited"] == 430000.0
    assert ent["risk"] == "high"


def test_asset_summary(eth_case):
    conn, cid = eth_case
    d = analyze.asset_summary(conn, cid, "eth", "USDC")
    assert d["moved"] == 4580000.0
    assert d["moved_usd"] == 4580000.0
    assert d["holders"][0]["balance"] == 3000000  # Lumen Lending Pool
    assert d["top_receivers"] and d["top_senders"]


def test_asset_overview(eth_case):
    conn, cid = eth_case
    d = analyze.asset_summary(conn, cid, "eth")
    assert d["overview"] is True
    syms = {a["asset"] for a in d["assets"]}
    assert syms == {"USDC", "ETH", "USDT"}


def test_transfers_feed_filters(eth_case):
    conn, cid = eth_case
    allf = analyze.transfers_feed(conn, cid, "eth")
    assert allf["total"] == 8
    out = analyze.transfers_feed(conn, cid, "eth", address=EXPLOITER, direction="out")
    assert out["total"] == 4
    assert all(t["direction"] == "out" for t in out["transfers"])
    big = analyze.transfers_feed(conn, cid, "eth", asset="USDC", min_value=1000000)
    assert big["total"] == 1
    hit = analyze.transfers_feed(conn, cid, "eth", query="0xe5e5")
    assert hit["total"] == 2  # two transfers involve Zenith


def test_transfers_feed_sort_by_value(eth_case):
    conn, cid = eth_case
    d = analyze.transfers_feed(conn, cid, "eth", sort="value")
    vals = [t["value"] for t in d["transfers"]]
    assert vals == sorted(vals, reverse=True)
