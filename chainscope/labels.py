"""Known-entity label knowledge base + resolver.

A small, curated set of *publicly documented* address attributions (the kind
every block explorer ships) so the analyzer can turn an opaque address into an
entity + category + risk tag. This is the attribution layer that makes a flow
graph readable: "funds went to a mixer, then to an exchange deposit address".

Entries here are public reference data. Imported datasets (and the analyst) can
add or override labels per-case in the DB; this KB is only the built-in
baseline. `category` drives visualization color; `risk` drives the risk engine.

Risk scale:  none < low < medium < high < sanctioned
"""

# category -> default risk when a dataset/KB doesn't state one
CATEGORY_RISK = {
    "exchange": "low",      # KYC chokepoint — useful, not inherently risky
    "defi": "low",
    "bridge": "medium",     # cross-chain hops obscure provenance
    "contract": "none",
    "individual": "none",
    "mixer": "high",        # privacy mixers / tumblers
    "tumbler": "high",
    "darkmarket": "sanctioned",
    "ransomware": "sanctioned",
    "scam": "high",
    "sanctioned": "sanctioned",
    "unknown": "none",
}

RISK_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "sanctioned": 4}

# Built-in public reference attributions. Keyed by (chain, normalized address).
# Kept intentionally small; the point is the mechanism, not a full dataset.
# The Tornado Cash router/pools below are OFAC-designated (public record,
# Aug 2022) and are the canonical "flagged mixer" example for on-chain tracing.
_KB = [
    # chain, address, label, entity, category, risk, source
    ("eth", "0x722122df12d4e14e13ac3b6895a86e84145b6967",
     "Tornado Cash: Router", "Tornado Cash", "mixer", "sanctioned", "OFAC SDN (public)"),
    ("eth", "0xa160cdab225685da1d56aa342ad8841c3b53f291",
     "Tornado Cash: 100 ETH Pool", "Tornado Cash", "mixer", "sanctioned", "OFAC SDN (public)"),
    ("eth", "0x8589427373d6d84e98730d7795d8f6f8731fda16",
     "Tornado Cash: Donation", "Tornado Cash", "mixer", "sanctioned", "OFAC SDN (public)"),
    # A couple of well-known public contract addresses for realism.
    ("eth", "0xdac17f958d2ee523a2206206994597c13d831ec7",
     "Tether: USDT Stablecoin", "Tether", "contract", "none", "public token contract"),
    ("eth", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
     "Circle: USDC Stablecoin", "Circle", "contract", "none", "public token contract"),
]

# address -> record
_BY_ADDR = {}
for _chain, _addr, _label, _entity, _cat, _risk, _src in _KB:
    _BY_ADDR[(_chain, _addr)] = {
        "label": _label, "entity": _entity, "category": _cat,
        "risk": _risk, "source": _src,
    }


def normalize(chain, address):
    """Canonical key form. EVM addresses are case-insensitive -> lowercase."""
    if not address:
        return address
    a = address.strip()
    if chain in ("eth", "evm", "bsc", "polygon", "arbitrum", "optimism", "base"):
        return a.lower()
    return a


def lookup(chain, address):
    """Return the built-in attribution for an address, or None."""
    return _BY_ADDR.get((chain, normalize(chain, address)))


def default_risk(category):
    return CATEGORY_RISK.get((category or "unknown").lower(), "none")


def risk_rank(risk):
    return RISK_RANK.get((risk or "none").lower(), 0)


def max_risk(*risks):
    best = "none"
    for r in risks:
        if risk_rank(r) > risk_rank(best):
            best = r
    return best


def catalog():
    """The KB as a list — for display / 'what does the tool know' views."""
    return [
        {"chain": c, "address": a, **rec}
        for (c, a), rec in _BY_ADDR.items()
    ]
