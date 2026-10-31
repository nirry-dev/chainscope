# Contributing to Chainscope

Thanks for your interest. Chainscope is a small, deliberately dependency-free
project — contributions that keep it that way are especially welcome.

## Principles

- **Standard library only** for the core (`chainscope/`, `api.py`). No runtime
  third-party Python packages. `pytest` is allowed as a dev/test dependency.
- **Buildless front end.** The UI is plain `index.html` / `app.js` /
  `styles.css` — no bundler, no framework, no transpile step.
- **Read-only.** Chainscope analyzes public data and must never sign, broadcast,
  or move funds, or require private keys.
- Match the surrounding code's style, comment density, and idioms.

## Getting started

```bash
git clone <your fork>
cd chainscope
python api.py          # run the app at http://127.0.0.1:8799
python demo.py         # offline end-to-end check
pip install pytest && pytest
```

## Before you open a PR

- Run `pytest` and `python demo.py`; both should pass.
- If you changed analysis logic, add or update a test in `tests/`.
- If you changed the UI, verify it in the browser and keep it working at phone
  width.
- Keep datasets synthetic. Do not add real individuals' addresses; public
  reference entities (e.g. a documented sanctioned contract) are fine when they
  serve attribution.
- Update `README.md` / `CHANGELOG.md` if you add a user-facing feature.

## Reporting bugs & ideas

Use the issue templates. For security issues, see [SECURITY.md](SECURITY.md) —
do **not** file those publicly.

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
