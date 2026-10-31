"""SQLite storage for Chainscope.

One database file holds many investigations ("cases"). The on-chain graph is
the single source of truth: imports write into it, analysis reads from it.

  cases            = an investigation (scopes everything below)
  chain_addresses  = wallet / contract nodes
  chain_transfers  = value-movement edges
  flags            = risk findings raised by the analyzer

Standalone and self-contained — no external dependencies. Designed so the graph
model can later be re-exported into a larger platform as an addon.
"""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_addresses (
    id         INTEGER PRIMARY KEY,
    case_id    INTEGER NOT NULL,
    chain      TEXT NOT NULL,               -- btc | eth | ...
    address    TEXT NOT NULL,
    label      TEXT,                          -- human label ("NovaEx Hot Wallet")
    entity     TEXT,                          -- owning entity ("NovaEx")
    category   TEXT,                          -- exchange|mixer|defi|bridge|contract|individual|unknown
    risk       TEXT NOT NULL DEFAULT 'none',  -- none|low|medium|high|sanctioned
    balance    REAL,                          -- native-asset balance (best known)
    asset      TEXT,                          -- native asset symbol (BTC/ETH)
    tx_count   INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_seen  TEXT,
    source     TEXT,                          -- sample|esplora|evm|manual
    tags       TEXT,                          -- JSON list
    note       TEXT,
    seed       INTEGER NOT NULL DEFAULT 0,    -- 1 = an address the analyst pivoted from
    taint      REAL NOT NULL DEFAULT 0,       -- 0..1 propagated exposure to a flagged source
    taint_src  TEXT,                          -- label of the nearest flagged source
    taint_hops INTEGER,                        -- hops from that source
    cluster    TEXT,                           -- co-ownership cluster id (common-input / entity)
    created_at TEXT NOT NULL,
    UNIQUE(case_id, chain, address)
);

CREATE TABLE IF NOT EXISTS chain_transfers (
    id         INTEGER PRIMARY KEY,
    case_id    INTEGER NOT NULL,
    chain      TEXT NOT NULL,
    txid       TEXT NOT NULL,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    value      REAL NOT NULL DEFAULT 0,       -- amount in `asset` units
    asset      TEXT,
    ts         TEXT,                          -- ISO timestamp
    block      INTEGER,
    source     TEXT,
    UNIQUE(case_id, chain, txid, src, dst, asset)
);

CREATE INDEX IF NOT EXISTS ix_xfer_src ON chain_transfers(case_id, chain, src);
CREATE INDEX IF NOT EXISTS ix_xfer_dst ON chain_transfers(case_id, chain, dst);

CREATE TABLE IF NOT EXISTS chain_holdings (
    id       INTEGER PRIMARY KEY,
    case_id  INTEGER NOT NULL,
    chain    TEXT NOT NULL,
    address  TEXT NOT NULL,
    asset    TEXT NOT NULL,              -- ETH | USDT | USDC | ...
    balance  REAL NOT NULL DEFAULT 0,
    UNIQUE(case_id, chain, address, asset)
);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL,
    dedup_key   TEXT NOT NULL,
    chain       TEXT,                           -- which chain this finding is on
    title       TEXT NOT NULL,
    severity    TEXT,
    score       REAL,
    category    TEXT NOT NULL DEFAULT 'chain',
    technique   TEXT,
    description TEXT,
    remediation TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open|confirmed|false_positive|resolved
    created_at  TEXT NOT NULL,
    UNIQUE(case_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS chain_cospends (
    id       INTEGER PRIMARY KEY,
    case_id  INTEGER NOT NULL,
    chain    TEXT NOT NULL,
    a        TEXT NOT NULL,                 -- co-spend pair (common-input ownership heuristic)
    b        TEXT NOT NULL,
    UNIQUE(case_id, chain, a, b)
);

CREATE TABLE IF NOT EXISTS chain_prices (
    id       INTEGER PRIMARY KEY,
    case_id  INTEGER NOT NULL,
    asset    TEXT NOT NULL,                 -- USD price snapshot for valuation (no live feed)
    usd      REAL NOT NULL,
    UNIQUE(case_id, asset)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn):
    """Add columns introduced after a DB was first created (SQLite has no
    ADD COLUMN IF NOT EXISTS)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chain_addresses)")}
    for name, ddl in (("taint", "taint REAL NOT NULL DEFAULT 0"),
                      ("taint_src", "taint_src TEXT"),
                      ("taint_hops", "taint_hops INTEGER"),
                      ("cluster", "cluster TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE chain_addresses ADD COLUMN {ddl}")
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(flags)")}
    if "chain" not in fcols:
        conn.execute("ALTER TABLE flags ADD COLUMN chain TEXT")
        # backfill from the dedup_key, which is `chain:<chain>:...`
        for r in conn.execute("SELECT id, dedup_key FROM flags").fetchall():
            parts = (r["dedup_key"] or "").split(":")
            ch = parts[1] if len(parts) > 2 and parts[0] == "chain" else None
            if ch:
                conn.execute("UPDATE flags SET chain=? WHERE id=?", (ch, r["id"]))
    conn.commit()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


# --- cases ---------------------------------------------------------------

def create_case(conn, name, note=None):
    cur = conn.execute(
        "INSERT INTO cases (name, note, created_at) VALUES (?,?,?)",
        (name, note, now()))
    conn.commit()
    return cur.lastrowid


def get_case(conn, case_id):
    r = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    return dict(r) if r else None


def list_cases(conn):
    cur = conn.execute("SELECT * FROM cases ORDER BY id DESC")
    return [dict(r) for r in cur.fetchall()]


def update_case(conn, case_id, name=None, note=None):
    sets, vals = [], []
    if name is not None and name.strip():
        sets.append("name=?"); vals.append(name.strip())
    if note is not None:
        sets.append("note=?"); vals.append(note)
    if not sets:
        return
    vals.append(case_id)
    conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def delete_case(conn, case_id):
    """Delete a case and everything scoped to it."""
    for t in ("chain_holdings", "chain_transfers", "chain_addresses", "flags",
              "chain_cospends", "chain_prices"):
        conn.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))
    conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
    conn.commit()


def cases_overview(conn):
    """Every case with its counts and latest on-chain activity — for the
    investigations manager."""
    out = []
    for c in list_cases(conn):
        cid = c["id"]
        c["counts"] = {
            "addresses": conn.execute("SELECT COUNT(*) n FROM chain_addresses WHERE case_id=?", (cid,)).fetchone()["n"],
            "transfers": conn.execute("SELECT COUNT(*) n FROM chain_transfers WHERE case_id=?", (cid,)).fetchone()["n"],
            "flags": conn.execute("SELECT COUNT(*) n FROM flags WHERE case_id=?", (cid,)).fetchone()["n"],
        }
        c["last_activity"] = conn.execute(
            "SELECT MAX(ts) t FROM chain_transfers WHERE case_id=?", (cid,)).fetchone()["t"]
        out.append(c)
    return out
