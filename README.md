# Chainscope

A blockchain **transaction & wallet analyzer / visualizer** — an on-chain
OSINT and fund-tracing tool. It inspects *public* transaction history and turns
it into a readable entity graph: wallets and contracts become nodes, value
movements become edges. You can attribute known addresses, follow the money
across hops, and flag risky exposure (mixers, sanctioned addresses, tainted
inflows).

It is read-only. Nothing here signs, broadcasts, or moves funds — this is the
tracing / attribution / visualization layer only, for authorized
investigations, incident response, and research.

> Standalone prototype. The graph model is intentionally self-contained so it
> can later be offered as an addon to a larger security platform.

## Highlights

- **Interactive flow graph** — a buildless, dependency-free force-directed
  visualization (SVG + vanilla JS). Nodes are colored by entity category and
  sized by volume; edges are weighted by value and point in the direction funds
  moved; flows into flagged destinations are drawn in red.
- **Wallet inspector** — balance, transaction count, counterparties, total
  received / sent, and entity attribution for any address.
- **Follow-the-money tracing** — breadth-first flow trace from a seed address,
  outward (where funds went), inward (where they came from), or both, to a
  chosen hop depth.
- **Known-entity attribution** — a built-in label KB (exchanges, mixers,
  sanctioned addresses, token contracts) that makes an opaque graph legible.
- **Risk engine** — raises flags for laundering signals (outflow to a
  mixer/sanctioned address) and tainted inflows (funds from a flagged source).
- **Offline-first** — ships a sample dataset so the whole app is demoable with
  no network and no API keys; optional live import from public explorers
  (Bitcoin via esplora, keyless; EVM via an Etherscan-compatible endpoint).

## Architecture

```
chainscope/
  db.py        SQLite: cases + chain_addresses + chain_transfers + flags
  store.py     idempotent upserts, aggregated edges, flag records
  labels.py    built-in known-entity attribution KB + resolver
  sources.py   normalized datasets: bundled samples + live esplora / EVM fetch
  analyze.py   ingest, wallet metrics, flow tracing, risk engine
  cli.py       command-line interface
  samples/     bundled demo datasets
api.py         one stdlib process: JSON API (/api/*) + static UI
ui/            buildless web app (index.html / app.js / styles.css)
demo.py        offline end-to-end demo
```

Pure Python standard library + a vanilla-JS front end. No build step, no
third-party packages.

## Run

Start the app (serves the API and the UI from one process):

```bash
python api.py
```

Then open <http://127.0.0.1:8799> and click **Load** to import the sample
dataset, **Run risk scan**, and click any node to inspect it.

### CLI

```bash
python -m chainscope.cli case-create "Theft trace"
python -m chainscope.cli import 1 --sample eth_theft_trace
python -m chainscope.cli trace 1 0x1111111111111111111111111111111111111111 --hops 3
python -m chainscope.cli risk 1
python -m chainscope.cli flags 1
```

### Offline demo

```bash
python demo.py
```

## Status

Early prototype. The included scenario is synthetic (a DeFi treasury drain
laundered through a mixer and cashed out at an exchange); only the Tornado Cash
pool address is a real, publicly documented one, used to demonstrate
attribution. Live explorer import is best-effort and depends on network egress.

Roadmap: richer label/attribution data, address clustering, token/ERC-20 and
multi-asset flows, timeline view, saved investigations, and a packaged desktop
build.
