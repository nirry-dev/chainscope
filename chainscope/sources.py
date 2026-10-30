"""Data sources for on-chain data.

Offline-first: a bundled sample dataset makes the whole analyzer demoable with
no network and no API keys, and an optional live fetch pulls a real address's
transactions from a public explorer when egress is available.

  * sample  -> chainscope/samples/*.json       (always available)
  * esplora -> blockstream-style Bitcoin REST   (keyless, best-effort)
  * evm     -> Etherscan-compatible explorer     (needs an API base + key)

Every source returns the SAME normalized dataset dict so the ingest path does
not care where the data came from:

    {
      "chain":    "eth"|"btc"|...,
      "asset":    "ETH"|"BTC"|...,
      "entities": [{address,label,entity,category,risk}, ...],  # optional labels
      "addresses":[{address,balance,seed,tags}, ...],
      "transfers":[{txid,src,dst,value,asset,ts,block}, ...],
    }
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "samples")

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# BTC: legacy/P2SH base58 (1/3…) or bech32 (bc1…); loose but catches junk.
_BTC_RE = re.compile(r"^(bc1[0-9a-z]{6,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,39})$")


def validate_address(address, chain):
    """Reject obviously-malformed input before spending a network round-trip.
    Returns the cleaned address or raises ValueError."""
    a = (address or "").strip()
    if not a:
        raise ValueError("address is required")
    if chain == "eth":
        if not _EVM_RE.match(a):
            raise ValueError("not a valid EVM address (expected 0x + 40 hex chars)")
        return a.lower()
    if chain == "btc":
        if not _BTC_RE.match(a):
            raise ValueError("not a valid Bitcoin address")
        return a
    return a


def list_samples():
    if not os.path.isdir(SAMPLE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SAMPLE_DIR) if f.endswith(".json"))


def load_sample(name):
    """Load a bundled dataset by name (no extension). Validated to the
    normalized shape; raises ValueError on anything malformed."""
    safe = os.path.basename(name)
    path = os.path.join(SAMPLE_DIR, safe + ".json")
    if not os.path.isfile(path):
        raise ValueError(f"unknown sample '{name}' (have: {', '.join(list_samples())})")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return normalize_dataset(data)


def normalize_dataset(data):
    """Coerce an arbitrary dataset dict into the normalized shape, dropping
    anything that isn't a well-formed transfer. Keeps ingest defensive against
    hand-edited or third-party JSON."""
    if not isinstance(data, dict):
        raise ValueError("dataset must be a JSON object")
    chain = (data.get("chain") or "eth").lower()
    asset = data.get("asset") or ("BTC" if chain == "btc" else "ETH")
    transfers = []
    for t in data.get("transfers", []):
        if not (t.get("txid") and t.get("src") and t.get("dst")):
            continue
        try:
            value = float(t.get("value", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        transfers.append({
            "txid": str(t["txid"]), "src": str(t["src"]), "dst": str(t["dst"]),
            "value": value, "asset": t.get("asset") or asset,
            "ts": t.get("ts"), "block": t.get("block"),
        })
    cospends = []
    for g in data.get("cospends", []):
        members = [str(m) for m in g if m]
        if len(members) >= 2:
            cospends.append(members)
    return {
        "chain": chain, "asset": asset,
        "entities": [e for e in data.get("entities", []) if e.get("address")],
        "addresses": [a for a in data.get("addresses", []) if a.get("address")],
        "transfers": transfers,
        "cospends": cospends,
        "note": data.get("note"),
    }


# --- live fetch (best-effort; requires network egress) -------------------

def _get_json(url, timeout=15, retries=1):
    """GET + parse JSON with clean, user-facing errors. Retries once on a
    timeout/transient network error so a single hiccup doesn't abort an import."""
    req = urllib.request.Request(url, headers={"User-Agent": "chainscope/0.1"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except ValueError:
                raise ValueError("explorer returned a non-JSON response "
                                 "(wrong base URL or a rate-limit page?)")
        except urllib.error.HTTPError as e:
            # 4xx/5xx are not worth retrying (bad request / not found / blocked)
            raise ValueError(f"explorer HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e  # transient: DNS, refused, timeout — retry once
    raise ValueError(f"could not reach explorer ({getattr(last, 'reason', last)})")


def _clamp_txs(max_txs, hard=500):
    try:
        return max(1, min(int(max_txs), hard))
    except (TypeError, ValueError):
        return 25


def fetch_esplora(address, base="https://blockstream.info/api", max_txs=50):
    """Pull a Bitcoin address + recent transactions from a Blockstream/esplora
    REST endpoint (keyless). Returns the normalized dataset. BTC values are in
    satoshis on the wire; converted to BTC here.

    Each tx is reduced to edges: every input address funds every output address
    it is not also an input of, weighted by the output value — the simple "all
    inputs fund all outputs" heuristic esplora-based tracers use. The input set
    of each tx is also recorded as a co-spend group (common-input ownership) to
    feed address clustering."""
    SAT = 1e8
    address = validate_address(address, "btc")
    base = (base or "https://blockstream.info/api").rstrip("/")
    max_txs = _clamp_txs(max_txs)
    stats = _get_json(f"{base}/address/{address}")
    if not isinstance(stats, dict) or "chain_stats" not in stats:
        raise ValueError("unexpected esplora response (check the base URL)")
    chain_stats = stats.get("chain_stats", {})
    funded = chain_stats.get("funded_txo_sum", 0)
    spent = chain_stats.get("spent_txo_sum", 0)
    balance = (funded - spent) / SAT
    tx_count = chain_stats.get("tx_count", 0)

    # esplora pages 25 txs at a time; page via /txs/chain/<last_seen_txid>.
    txs, last_txid = [], None
    while len(txs) < max_txs:
        url = f"{base}/address/{address}/txs"
        if last_txid:
            url += f"/chain/{last_txid}"
        page = _get_json(url)
        if not isinstance(page, list) or not page:
            break
        txs.extend(page)
        last_txid = page[-1].get("txid")
        if len(page) < 25:
            break
    txs = txs[:max_txs]

    transfers, cospends = [], []
    for tx in txs:
        txid = tx.get("txid")
        ts = tx.get("status", {}).get("block_time")
        iso = (datetime.fromtimestamp(ts, timezone.utc).isoformat()
               if ts else None)
        block = tx.get("status", {}).get("block_height")
        ins = [vin.get("prevout", {}).get("scriptpubkey_address")
               for vin in tx.get("vin", [])]
        ins = [a for a in ins if a]
        in_set = set(ins)
        if len(in_set) >= 2:
            cospends.append(sorted(in_set))   # common-input ownership heuristic
        for vout in tx.get("vout", []):
            dst = vout.get("scriptpubkey_address")
            if not dst or dst in in_set:
                continue  # skip change back to an input
            val = vout.get("value", 0) / SAT
            src = ins[0] if ins else "coinbase"
            transfers.append({"txid": txid, "src": src, "dst": dst,
                              "value": val, "asset": "BTC", "ts": iso, "block": block})

    return normalize_dataset({
        "chain": "btc", "asset": "BTC",
        "addresses": [{"address": address, "balance": balance,
                       "tx_count": tx_count, "seed": True}],
        "transfers": transfers, "cospends": cospends,
    })


def fetch_evm(address, api_base, api_key, max_txs=50):
    """Pull an EVM address's native-value transfers from an Etherscan-compatible
    explorer (module=account&action=txlist). Requires an API base + key."""
    WEI = 1e18
    address = validate_address(address, "eth")
    api_base = (api_base or "").strip().rstrip("/")
    if not api_base:
        raise ValueError("EVM explorer API base URL is required")
    if not (api_key or "").strip():
        raise ValueError("EVM explorer API key is required")
    max_txs = _clamp_txs(max_txs, hard=1000)
    url = (f"{api_base}?module=account&action=txlist&address={address}"
           f"&startblock=0&endblock=99999999&sort=desc&page=1&offset={max_txs}"
           f"&apikey={api_key.strip()}")
    data = _get_json(url)
    if isinstance(data, dict) and str(data.get("status")) == "0":
        # Etherscan reports errors/empty with status "0"; "No transactions found"
        # is benign, anything else (rate limit, bad key) should surface.
        msg = (data.get("message") or "").strip()
        res = data.get("result")
        if "No transactions found" not in msg and not isinstance(res, list):
            raise ValueError(f"explorer error: {msg or res or 'rejected request'}")
    rows = data.get("result", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise ValueError(f"explorer error: {rows}")
    transfers = []
    for tx in rows:
        try:
            val = int(tx.get("value", "0")) / WEI
        except (TypeError, ValueError):
            val = 0.0
        ts = tx.get("timeStamp")
        try:
            iso = (datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
                   if ts else None)
        except (TypeError, ValueError):
            iso = None
        transfers.append({"txid": tx.get("hash"), "src": tx.get("from"),
                          "dst": tx.get("to"), "value": val, "asset": "ETH",
                          "ts": iso, "block": tx.get("blockNumber")})
    return normalize_dataset({
        "chain": "eth", "asset": "ETH",
        "addresses": [{"address": address, "seed": True}],
        "transfers": transfers,
    })
