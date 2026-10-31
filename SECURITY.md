# Security Policy

Chainscope is a **read-only** analysis tool: it never signs, broadcasts, or
moves funds, never handles private keys, and binds its local server to
`127.0.0.1` only. Still, we take issues seriously.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
vulnerability.

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  on this repository (Security → Report a vulnerability), or
- open a regular issue that says only "security report" with no details and ask
  for a private channel.

Please include what you can: affected version/commit, a description, and steps
to reproduce. We aim to acknowledge within a few days.

## Scope

In scope:
- Code execution, path traversal, or SSRF via the local API or static file
  server (`api.py`).
- Injection (SQL, HTML/JS) via imported datasets, annotations, or live-import
  responses.
- Anything that causes the tool to transact, leak local files, or make
  unintended outbound requests.

Out of scope:
- Accuracy of heuristic attributions, risk flags, or clustering — these are
  investigative signals, not guarantees (see
  [Responsible use](README.md#responsible-use)).
- Issues requiring a malicious dataset the user deliberately crafted and loaded
  outside normal use.
- The bundled sample datasets (synthetic) and the documented real reference
  addresses they contain.

## Supported versions

This is pre-1.0 software; only the latest `main` is supported.
