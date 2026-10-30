"""Graph operations over the on-chain store.

Thin helpers over the DB. All writes are idempotent upserts keyed by UNIQUE
constraints, so re-importing the same wallet or transaction never duplicates a
node or an edge.
"""
import json

from .db import now


# --- addresses -----------------------------------------------------------

def upsert_address(conn, case_id, chain, address, label=None, entity=None,
                   category=None, risk=None, balance=None, asset=None,
                   first_seen=None, last_seen=None, source=None, tags=None,
                   note=None, seed=None):
    """Insert or enrich an address node. Only non-None fields overwrite, so a
    later, richer observation never erases an earlier one. Risk only ratchets
    up (a later 'none' never clears a recorded risk)."""
    tags_json = json.dumps(tags) if tags is not None else None
    conn.execute(
        "INSERT INTO chain_addresses "
        "(case_id, chain, address, label, entity, category, risk, balance, "
        " asset, first_seen, last_seen, source, tags, note, seed, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(case_id, chain, address) DO UPDATE SET "
        "  label=COALESCE(excluded.label, chain_addresses.label), "
        "  entity=COALESCE(excluded.entity, chain_addresses.entity), "
        "  category=COALESCE(excluded.category, chain_addresses.category), "
        "  risk=CASE WHEN excluded.risk IS NOT NULL AND excluded.risk!='none' "
        "            THEN excluded.risk ELSE chain_addresses.risk END, "
        "  balance=COALESCE(excluded.balance, chain_addresses.balance), "
        "  asset=COALESCE(excluded.asset, chain_addresses.asset), "
        "  first_seen=COALESCE(excluded.first_seen, chain_addresses.first_seen), "
        "  last_seen=COALESCE(excluded.last_seen, chain_addresses.last_seen), "
        "  tags=COALESCE(excluded.tags, chain_addresses.tags), "
        "  note=COALESCE(excluded.note, chain_addresses.note), "
        "  seed=MAX(excluded.seed, chain_addresses.seed)",
        (case_id, chain, address, label, entity, category, risk or "none", balance,
         asset, first_seen, last_seen, source, tags_json, note,
         1 if seed else 0, now()))
    conn.commit()
    r = conn.execute(
        "SELECT id FROM chain_addresses WHERE case_id=? AND chain=? AND address=?",
        (case_id, chain, address)).fetchone()
    return r["id"]


def get_address(conn, case_id, chain, address):
    r = conn.execute(
        "SELECT * FROM chain_addresses WHERE case_id=? AND chain=? AND address=?",
        (case_id, chain, address)).fetchone()
    return _row_addr(r) if r else None


def list_addresses(conn, case_id, chain=None):
    if chain:
        cur = conn.execute(
            "SELECT * FROM chain_addresses WHERE case_id=? AND chain=? "
            "ORDER BY tx_count DESC, address", (case_id, chain))
    else:
        cur = conn.execute(
            "SELECT * FROM chain_addresses WHERE case_id=? "
            "ORDER BY tx_count DESC, address", (case_id,))
    return [_row_addr(r) for r in cur.fetchall()]


def set_risk(conn, case_id, chain, address, risk):
    conn.execute(
        "UPDATE chain_addresses SET risk=? WHERE case_id=? AND chain=? AND address=?",
        (risk, case_id, chain, address))
    conn.commit()


def refresh_tx_counts(conn, case_id, chain):
    """Recompute tx_count / first_seen / last_seen for every address from the
    transfers actually stored. Cheap, idempotent, called after an import."""
    rows = conn.execute(
        "SELECT address FROM chain_addresses WHERE case_id=? AND chain=?",
        (case_id, chain)).fetchall()
    for r in rows:
        a = r["address"]
        agg = conn.execute(
            "SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx FROM chain_transfers "
            "WHERE case_id=? AND chain=? AND (src=? OR dst=?)",
            (case_id, chain, a, a)).fetchone()
        conn.execute(
            "UPDATE chain_addresses SET tx_count=?, "
            "first_seen=COALESCE(first_seen, ?), last_seen=COALESCE(?, last_seen) "
            "WHERE case_id=? AND chain=? AND address=?",
            (agg["n"], agg["mn"], agg["mx"], case_id, chain, a))
    conn.commit()


# --- transfers -----------------------------------------------------------

def upsert_transfer(conn, case_id, chain, txid, src, dst, value, asset=None,
                    ts=None, block=None, source=None):
    conn.execute(
        "INSERT INTO chain_transfers "
        "(case_id, chain, txid, src, dst, value, asset, ts, block, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(case_id, chain, txid, src, dst, asset) DO UPDATE SET "
        "  value=excluded.value, ts=COALESCE(excluded.ts, chain_transfers.ts), "
        "  block=COALESCE(excluded.block, chain_transfers.block)",
        (case_id, chain, txid, src, dst, value, asset, ts, block, source))
    conn.commit()


def list_transfers(conn, case_id, chain, address=None, limit=500):
    if address:
        cur = conn.execute(
            "SELECT * FROM chain_transfers WHERE case_id=? AND chain=? "
            "AND (src=? OR dst=?) ORDER BY ts DESC LIMIT ?",
            (case_id, chain, address, address, limit))
    else:
        cur = conn.execute(
            "SELECT * FROM chain_transfers WHERE case_id=? AND chain=? "
            "ORDER BY ts DESC LIMIT ?", (case_id, chain, limit))
    return [dict(r) for r in cur.fetchall()]


def edges_aggregated(conn, case_id, chain):
    """Collapse raw transfers into directed (src,dst) edges with total value and
    a transfer count — the shape the flow-graph visualizer draws."""
    cur = conn.execute(
        "SELECT src, dst, asset, COUNT(*) n, SUM(value) total, "
        "       MIN(ts) first_ts, MAX(ts) last_ts "
        "FROM chain_transfers WHERE case_id=? AND chain=? "
        "GROUP BY src, dst, asset", (case_id, chain))
    return [dict(r) for r in cur.fetchall()]


# --- flags (risk findings) ----------------------------------------------

def record_flags(conn, case_id, items):
    """Insert/update risk flags. Each item carries a stable dedup_key so a
    re-scan merges instead of duplicating. Returns (new, merged)."""
    new, merged = 0, 0
    for it in items:
        existing = conn.execute(
            "SELECT id FROM flags WHERE case_id=? AND dedup_key=?",
            (case_id, it["dedup_key"])).fetchone()
        if existing:
            merged += 1
            continue
        conn.execute(
            "INSERT INTO flags (case_id, dedup_key, title, severity, score, "
            "category, technique, description, remediation, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, it["dedup_key"], it["title"], it.get("severity"),
             it.get("score"), it.get("category", "chain"), it.get("technique"),
             it.get("description"), it.get("remediation"), "open", now()))
        new += 1
    conn.commit()
    return new, merged


def list_flags(conn, case_id):
    cur = conn.execute(
        "SELECT * FROM flags WHERE case_id=? ORDER BY score DESC, id", (case_id,))
    return [dict(r) for r in cur.fetchall()]


def _row_addr(r):
    d = dict(r)
    try:
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
    except Exception:
        d["tags"] = []
    return d
