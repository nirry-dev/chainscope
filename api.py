"""Chainscope local server.

A single stdlib HTTP process that serves BOTH the JSON API (/api/*) and the
static web UI (everything else), so the whole app runs from one command with no
build step and no dependencies:

    python api.py [--host 127.0.0.1] [--port 8799] [--db chainscope.db]

Security posture: binds to 127.0.0.1 only; read-only on-chain analysis — it
imports and analyzes public transaction data, it never signs or sends anything.
"""
import argparse
import json
import os
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from chainscope import db, store, analyze, labels, sources

DB_PATH = os.environ.get("CHAINSCOPE_DB", "chainscope.db")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

_CTYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
           ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
           ".png": "image/png", ".ico": "image/x-icon", ".map": "application/json"}


def case_summary(conn, case_id):
    c = db.get_case(conn, case_id)
    if not c:
        return None
    addr = conn.execute("SELECT COUNT(*) n FROM chain_addresses WHERE case_id=?", (case_id,)).fetchone()["n"]
    xfer = conn.execute("SELECT COUNT(*) n FROM chain_transfers WHERE case_id=?", (case_id,)).fetchone()["n"]
    flg = conn.execute("SELECT COUNT(*) n FROM flags WHERE case_id=?", (case_id,)).fetchone()["n"]
    c["counts"] = {"addresses": addr, "transfers": xfer, "flags": flg}
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "chainscope/0.1"

    # --- plumbing ---------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        try:
            if urlparse(self.path).path.startswith("/api/"):
                return self.api("GET")
            return self.static()
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        try:
            return self.api("POST")
        except Exception as e:
            self._json(500, {"error": str(e)})

    # --- static UI --------------------------------------------------------
    def static(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        fp = os.path.join(UI_DIR, rel)
        if not os.path.abspath(fp).startswith(UI_DIR) or not os.path.isfile(fp):
            self.send_response(404); self._cors(); self.end_headers()
            self.wfile.write(b"not found")
            return
        ctype = _CTYPES.get(os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # --- JSON API ---------------------------------------------------------
    def api(self, method):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        q = parse_qs(urlparse(self.path).query)
        conn = db.connect(DB_PATH)

        if parts == ["api", "health"]:
            return self._json(200, {"ok": True, "db": DB_PATH})
        if parts == ["api", "samples"]:
            return self._json(200, {"samples": sources.list_samples()})
        if parts == ["api", "labels"]:
            return self._json(200, {"labels": labels.catalog()})

        if parts == ["api", "cases"]:
            if method == "GET":
                return self._json(200, {"cases": db.list_cases(conn)})
            body = self._body()
            name = (body.get("name") or "").strip()
            if not name:
                return self._json(400, {"error": "name required"})
            cid = db.create_case(conn, name, body.get("note"))
            return self._json(201, {"id": cid})

        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "cases":
            try:
                cid = int(parts[2])
            except ValueError:
                return self._json(400, {"error": "bad case id"})
            if not db.get_case(conn, cid):
                return self._json(404, {"error": "case not found"})
            sub = parts[3:]
            chain = q.get("chain", ["eth"])[0]

            if sub == []:
                return self._json(200, case_summary(conn, cid))

            if sub == ["import"]:
                body = self._body()
                src = body.get("source", "sample")
                try:
                    if src == "sample":
                        data = sources.load_sample(body.get("name", ""))
                    elif src == "dataset":
                        data = sources.normalize_dataset(body.get("dataset") or {})
                    elif src == "esplora":
                        data = sources.fetch_esplora(
                            body["address"], base=body.get("base", "https://blockstream.info/api"))
                    elif src == "evm":
                        data = sources.fetch_evm(
                            body["address"], body["api_base"], body.get("api_key", ""))
                    else:
                        return self._json(400, {"error": f"unknown source {src}"})
                except Exception as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, analyze.ingest(conn, cid, data, source=src))

            if sub == ["graph"]:
                return self._json(200, analyze.graph_view(
                    conn, cid, chain, asset=q.get("asset", [None])[0] or None,
                    before=q.get("before", [None])[0] or None,
                    after=q.get("after", [None])[0] or None))

            if sub == ["assets"]:
                return self._json(200, {"assets": store.assets(conn, cid, chain)})

            if sub == ["timeline"]:
                return self._json(200, store.timeline(
                    conn, cid, chain, asset=q.get("asset", [None])[0] or None))

            if sub == ["trace"]:
                seed = q.get("seed", [None])[0]
                if not seed:
                    return self._json(400, {"error": "seed query param required"})
                return self._json(200, analyze.trace_flow(
                    conn, cid, chain, seed, hops=int(q.get("hops", ["3"])[0]),
                    direction=q.get("direction", ["out"])[0],
                    min_value=float(q.get("min", ["0"])[0]),
                    asset=q.get("asset", [None])[0] or None))

            if sub == ["address"]:
                addr = q.get("address", [None])[0]
                if not addr:
                    return self._json(400, {"error": "address query param required"})
                return self._json(200, analyze.inspect(conn, cid, chain, addr)
                                  or {"error": "address not found"})

            if sub == ["address", "annotate"]:
                body = self._body()
                addr = body.get("address")
                if not addr:
                    return self._json(400, {"error": "address required"})
                store.annotate_address(conn, cid, body.get("chain", "eth"), addr, {
                    k: body[k] for k in ("label", "entity", "category", "risk", "note", "tags")
                    if k in body})
                return self._json(200, {"ok": True})

            if sub == ["risk"]:
                ch = self._body().get("chain") or "eth"
                new, merged = analyze.risk_scan(conn, cid, ch)
                return self._json(200, {"new": new, "merged": merged})

            if sub == ["taint"]:
                body = self._body()
                return self._json(200, analyze.taint_scan(
                    conn, cid, body.get("chain", "eth"), max_hops=int(body.get("max_hops", 4))))

            if sub == ["path"]:
                src, dst = q.get("from", [None])[0], q.get("to", [None])[0]
                if not src or not dst:
                    return self._json(400, {"error": "from and to query params required"})
                return self._json(200, analyze.find_paths(
                    conn, cid, chain, src, dst,
                    directed=q.get("directed", ["1"])[0] not in ("0", "false")))

            if sub == ["flags"]:
                return self._json(200, {"flags": store.list_flags(conn, cid)})

        return self._json(404, {"error": "not found", "path": self.path})


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    DB_PATH = args.db
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"chainscope on http://{args.host}:{args.port}  (db={DB_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
