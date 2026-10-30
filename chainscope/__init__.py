"""Chainscope — blockchain transaction & wallet analyzer / visualizer.

A read-only on-chain OSINT / fund-tracing tool: it inspects public transaction
history and turns it into a readable entity graph (wallets = nodes, transfers =
edges), attributes known addresses, traces fund flows across hops, and flags
risky exposure (mixers, sanctioned addresses, tainted inflows).

Nothing here signs, broadcasts, or moves funds — this is the tracing /
attribution / visualization layer only.
"""
__version__ = "0.1.0"
