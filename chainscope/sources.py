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
import urllib.request
from datetime import datetime, timezone

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "samples")


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
    return {
        "chain": chain, "asset": asset,
        "entities": [e for e in data.get("entities", []) if e.get("address")],
        "addresses": [a for a in data.get("addresses", []) if a.get("address")],
        "transfers": transfers,
        "note": data.get("note"),
    }


# --- live fetch (best-effort; requires network egress) -------------------

def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "chainscope/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_esplora(address, base="https://blockstream.info/api", max_txs=25):
    """Pull a Bitcoin address + recent transactions from a Blockstream/esplora
    REST endpoint (keyless). Returns the normalized dataset. BTC values are in
    satoshis on the wire; converted to BTC here.

    Each tx is reduced to edges: every input address funds every output address
    it is not also an input of, weighted by the output value — the simple "all
    inputs fund all outputs" heuristic esplora-based tracers use."""
    SAT = 1e8
    stats = _get_json(f"{base}/address/{address}")
    txs = _get_json(f"{base}/address/{address}/txs")[:max_txs]
    chain_stats = stats.get("chain_stats", {})
    funded = chain_stats.get("funded_txo_sum", 0)
    spent = chain_stats.get("spent_txo_sum", 0)
    balance = (funded - spent) / SAT
    tx_count = chain_stats.get("tx_count", len(txs))

    transfers = []
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
        "transfers": transfers,
    })


def fetch_evm(address, api_base, api_key, max_txs=25):
    """Pull an EVM address's native-value transfers from an Etherscan-compatible
    explorer (module=account&action=txlist). Requires an API base + key."""
    WEI = 1e18
    url = (f"{api_base}?module=account&action=txlist&address={address}"
           f"&startblock=0&endblock=99999999&sort=desc&page=1&offset={max_txs}"
           f"&apikey={api_key}")
    data = _get_json(url)
    rows = data.get("result", []) if isinstance(data, dict) else []
    transfers = []
    for tx in rows if isinstance(rows, list) else []:
        try:
            val = int(tx.get("value", "0")) / WEI
        except (TypeError, ValueError):
            val = 0.0
        ts = tx.get("timeStamp")
        iso = (datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
               if ts else None)
        transfers.append({"txid": tx.get("hash"), "src": tx.get("from"),
                          "dst": tx.get("to"), "value": val, "asset": "ETH",
                          "ts": iso, "block": tx.get("blockNumber")})
    return normalize_dataset({
        "chain": "eth", "asset": "ETH",
        "addresses": [{"address": address, "seed": True}],
        "transfers": transfers,
    })
