"""True end-to-end run: real HTTP server, real watcher.py CLI, real state file.

Serves four pages over real HTTP and drives the actual command line:
  /in-stock      - schema.org JSON-LD says InStock
  /out-of-stock  - schema.org JSON-LD says OutOfStock
  /no-json-ld    - plain HTML with an "Add to cart" button (LLM tier needed)
  /broken        - always 500 (failure alerting)

Run: python e2e_local.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PYTHON = sys.executable

IN_STOCK_PAGE = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"E2E Widget",
 "offers":{"@type":"Offer","availability":"https://schema.org/InStock"}}
</script></head><body><h1>E2E Widget</h1></body></html>"""

OUT_OF_STOCK_PAGE = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"E2E Widget",
 "offers":{"@type":"Offer","availability":"https://schema.org/OutOfStock"}}
</script></head><body><h1>E2E Widget</h1></body></html>"""

NO_JSON_LD_PAGE = """<!doctype html><html><body>
<h1>E2E Widget</h1><button class="add-to-cart">Add to cart</button>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/in-stock":
            self._ok(IN_STOCK_PAGE)
        elif self.path == "/out-of-stock":
            self._ok(OUT_OF_STOCK_PAGE)
        elif self.path == "/no-json-ld":
            self._ok(NO_JSON_LD_PAGE)
        elif self.path == "/slow-ok":
            time.sleep(0.2)
            self._ok(IN_STOCK_PAGE)
        else:
            self.send_response(500)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"boom!")

    def _ok(self, body: str):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep output readable
        pass


def run_cli(argv, env_extra=None):
    import os
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.run(
        [PYTHON, "watcher.py", *argv],
        cwd=HERE, capture_output=True, text=True, env=env, timeout=120,
    )
    return proc


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    workdir = HERE / "_e2e_tmp"
    workdir.mkdir(exist_ok=True)
    products_path = workdir / "products.json"
    state_path = workdir / "state.json"
    if state_path.exists():
        state_path.unlink()

    products = [
        {"id": "e2e_in", "store": "local", "name": "E2E Widget",
         "url": f"{base}/in-stock", "render": "http"},
        {"id": "e2e_out", "store": "local", "name": "E2E Widget",
         "url": f"{base}/out-of-stock", "render": "http"},
        {"id": "e2e_plain", "store": "local", "name": "E2E Widget",
         "url": f"{base}/no-json-ld", "render": "http"},
        {"id": "e2e_broken", "store": "local", "name": "E2E Widget",
         "url": f"{base}/always-500", "render": "http"},
    ]
    products_path.write_text(json.dumps(products, indent=2))

    base_args = [
        "--products-file", str(products_path),
        "--state-file", str(state_path),
    ]
    # No Telegram credentials: notifications are logged, not sent.
    env = {"LLM_PROVIDER": "anthropic"}  # no API key -> LLM tier fails soft

    failures = []

    def check(label, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {label}" + (f" - {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # ---- run 1: first check --------------------------------------------
    print("\n=== run 1: initial check ===")
    proc = run_cli([*base_args, "--json"], env)
    print(proc.stderr.strip())
    check("run 1 exit code 0", proc.returncode == 0, proc.stderr)
    results = {r["id"]: r for r in json.loads(proc.stdout)}
    check("run 1 reports 4 products", len(results) == 4, proc.stdout)
    check("json-ld in-stock detected", results["e2e_in"]["status"] == "in_stock",
          results["e2e_in"])
    check("json-ld out-of-stock detected",
          results["e2e_out"]["status"] == "out_of_stock", results["e2e_out"])
    check("500 page recorded as error", results["e2e_broken"]["status"] == "error",
          results["e2e_broken"])
    # no-json-ld page: with no API key the LLM tier cannot answer
    check("no-json-ld page handled without crash",
          results["e2e_plain"]["status"] in ("unknown", "in_stock", "out_of_stock"),
          results["e2e_plain"])
    # Telegram is intentionally unconfigured here, so the notifier logs the
    # message instead of sending - that log line is the signal we assert on.
    check("would-send logged for first in-stock",
          "would have sent: In stock at local: E2E Widget" in proc.stderr, proc.stderr)
    check("no notification for out-of-stock",
          results["e2e_out"]["notified"] is False, results["e2e_out"])
    check("would-have-sent message logged",
          "Telegram not configured" in proc.stderr, proc.stderr)

    state = json.loads(state_path.read_text())
    check("state file written", state_path.exists() and len(state) == 4)
    check("state stores statuses",
          state["e2e_in"]["last_status"] == "in_stock"
          and state["e2e_out"]["last_status"] == "out_of_stock", state)
    check("run_count persisted", state["e2e_in"]["run_count"] == 1, state)

    # ---- run 2: no repeat notifications --------------------------------
    print("\n=== run 2: repeat check (no repeat notifications) ===")
    proc = run_cli([*base_args, "--json"], env)
    results = {r["id"]: r for r in json.loads(proc.stdout)}
    check("run 2 exit code 0", proc.returncode == 0, proc.stderr)
    check("in-stock no longer notifies", results["e2e_in"]["notified"] is False)
    check("run_count incremented",
          json.loads(state_path.read_text())["e2e_in"]["run_count"] == 2)

    # ---- run 3: transition to in-stock notifies ------------------------
    print("\n=== run 3: restock transition ===")
    products[1]["url"] = f"{base}/in-stock"   # out-of-stock item comes back
    products_path.write_text(json.dumps(products, indent=2))
    proc = run_cli([*base_args, "--json"], env)
    results = {r["id"]: r for r in json.loads(proc.stdout)}
    check("run 3 exit code 0", proc.returncode == 0, proc.stderr)
    check("restock transition would notify",
          "would have sent: In stock at local: E2E Widget" in proc.stderr, proc.stderr)

    # ---- run 4: dry run writes no state --------------------------------
    print("\n=== run 4: dry run ===")
    dry_state = workdir / "dry_state.json"
    proc = run_cli([
        *base_args[:0],
        "--products-file", str(products_path),
        "--state-file", str(dry_state),
        "--dry-run", "--json",
    ], env)
    check("dry-run exit code 0", proc.returncode == 0, proc.stderr)
    check("dry-run produces no state file", not dry_state.exists())
    dry = {r["id"]: r for r in json.loads(proc.stdout)}
    check("dry-run still decides status", dry["e2e_in"]["status"] == "in_stock")
    check("dry-run never notifies", all(not r["notified"] for r in dry.values()))

    # ---- run 5: --product filter ---------------------------------------
    print("\n=== run 5: --product filter ===")
    proc = run_cli([*base_args, "--product", "e2e_out", "--json"], env)
    filtered = json.loads(proc.stdout)
    check("--product checks only the requested id",
          len(filtered) == 1 and filtered[0]["id"] == "e2e_out", proc.stdout)

    # ---- run 6: failure alerting ---------------------------------------
    print("\n=== run 6: failure alerting (3 consecutive failures) ===")
    alert_state = workdir / "alert_state.json"
    if alert_state.exists():
        alert_state.unlink()
    alert_products = [{"id": "e2e_broken2", "store": "local",
                       "name": "Always Broken", "url": f"{base}/always-500",
                       "render": "http"}]
    alert_products_path = workdir / "alert_products.json"
    alert_products_path.write_text(json.dumps(alert_products, indent=2))
    for i in range(3):
        proc = run_cli([
            "--products-file", str(alert_products_path),
            "--state-file", str(alert_state),
            "--json",
        ], {"ALERT_AFTER_FAILURES": "3", "FETCH_RETRIES": "1", **env})
        print(f"  attempt {i + 1}: exit={proc.returncode}")
    alerts = [line for line in proc.stderr.splitlines()
              if "consecutive failed checks" in line]
    check("failure alert raised after threshold", len(alerts) >= 1,
          proc.stderr)
    alert_state_data = json.loads(alert_state.read_text())
    check("failure streak tracked",
          alert_state_data["e2e_broken2"]["consecutive_failures"] == 3,
          alert_state_data)

    # ---- run 7: bad CLI input ------------------------------------------
    print("\n=== run 7: error paths ===")
    proc = run_cli(["--products-file", str(workdir / "absent.json"),
                    "--state-file", str(state_path)], env)
    check("missing products file exits 2", proc.returncode == 2, proc.stderr)
    proc = run_cli([*base_args, "--product", "nope"], env)
    check("unknown --product exits 2", proc.returncode == 2, proc.stderr)

    server.shutdown()
    print()
    if failures:
        print(f"E2E FAILED ({len(failures)}): {failures}")
        return 1
    print("E2E PASSED - all checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
