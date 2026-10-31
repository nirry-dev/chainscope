# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Live EVM import now pulls **ERC-20 token transfers** (`tokentx`) alongside
  native ETH, valued per each token's decimals/symbol (best-effort — a failed
  token pass keeps the native results).
- **Flag triage**: confirm a finding or dismiss it as a false positive
  (`POST /api/cases/{id}/flags/{flag_id}`); dismissed flags drop out of the
  severity counts and sink in the list. Status is shown in the evidence report.

### Security
- Local server now **rejects cross-origin state-changing requests** and foreign
  `Host` headers (anti-CSRF / anti-DNS-rebinding), and no longer sends a wildcard
  `Access-Control-Allow-Origin`. Same-origin UI and the Tauri shell are
  unaffected.
- Front end now HTML-escapes all address interpolations (and `esc()` escapes
  single quotes), closing a self-XSS path from a crafted imported dataset.

### Fixed
- BTC esplora import sums multiple outputs to the same address within a tx
  (previously the duplicate collapsed on the unique key); docstring corrected to
  the representative-input model it actually uses.

## [0.1.0] - 2026-10-30

First tagged release.

### Added
- Interactive force-directed fund-flow graph (SVG + vanilla JS) with curved
  value-weighted edges, category colors, risk/taint rings, focus-dimming,
  search, pan/zoom/drag, and freeze.
- Wallet inspector: balance, USD value, counterparties with per-asset nets,
  assets held & moved, and a balance-history sparkline.
- Follow-the-money tracing (out/in/both, hop depth) and shortest-path finder.
- Taint propagation with decaying downstream exposure.
- Address clustering (common-input co-spend + shared-entity) with graph hulls
  and an entity rollup dashboard.
- Token/asset dashboard (top holders, receivers/senders, net exchange flow) and
  multi-asset / ERC-20 flows.
- USD valuation from an optional per-case price snapshot.
- Known-entity attribution KB, manual annotation, and a de-duplicating risk
  engine.
- Filterable transfers feed (scope, direction, asset, min value, search, sort).
- Timeline scrubber, saved investigations (multi-case), and Markdown/JSON
  evidence export with an embedded Mermaid flow diagram.
- Offline-first bundled sample datasets; optional live import (keyless BTC via
  esplora, verified against mainnet; EVM via an Etherscan-compatible endpoint).
- Single stdlib process serving both the JSON API and the static UI; a CLI; an
  offline end-to-end demo; and a packaged Tauri desktop build.
- pytest test suite covering the core analysis.

[Unreleased]: https://github.com/nirry-dev/chainscope/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nirry-dev/chainscope/releases/tag/v0.1.0
