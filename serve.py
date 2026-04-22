#!/usr/bin/env python3
"""Local server with Keep/Remove persistence.

Serves docs/ as static files and exposes POST /api/status so that clicking
Keep or Remove in the browser actually updates the JSON database and
regenerates index.html immediately.

Usage:
    python3 serve.py           # serves on http://localhost:8080
    python3 serve.py --port 9000
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

DOCS_DIR         = Path("docs")
PAPERS_JSON      = DOCS_DIR / "papers.json"
INDUSTRY_JSON    = DOCS_DIR / "industry.json"
STARTUP_JSON     = DOCS_DIR / "startup.json"
USER_STATUS_JSON = DOCS_DIR / "user_status.json"


def _load(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write(path: Path, data: list) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, str(path))


def _update_user_status_file(url: str, status) -> None:
    """Atomically update user_status.json with the new url→status mapping."""
    us = _load(USER_STATUS_JSON) if USER_STATUS_JSON.exists() else {}
    if not isinstance(us, dict):
        us = {}
    if status is None:
        us.pop(url, None)
    else:
        us[url] = status
    _atomic_write(USER_STATUS_JSON, us)


def _update_status(url: str, status) -> bool:
    """Set or clear the status field on the item matching url.

    status="kept"|"removed" sets the field; status=None clears it.
    Also updates user_status.json so weekly_runner.py picks it up.
    Returns True if the item was found and updated.
    """
    for path in (PAPERS_JSON, INDUSTRY_JSON, STARTUP_JSON):
        items = _load(path)
        for item in items:
            if item.get("url") == url:
                if status is None:
                    item.pop("status", None)
                else:
                    item["status"] = status
                _atomic_write(path, items)
                _update_user_status_file(url, status)
                _regenerate_html()
                return True
    return False


def _regenerate_html() -> None:
    """Re-run generate_html() and overwrite docs/index.html."""
    try:
        from weekly_runner import generate_html, load_existing_papers
        from industry_feeds import load_industry_items

        papers   = load_existing_papers()
        industry = load_industry_items(str(INDUSTRY_JSON))
        startup  = load_industry_items(str(STARTUP_JSON))
        html     = generate_html(papers, industry, startup)

        index = DOCS_DIR / "index.html"
        with tempfile.NamedTemporaryFile(
            "w", dir=DOCS_DIR, delete=False, suffix=".tmp", encoding="utf-8"
        ) as f:
            f.write(html)
            tmp = f.name
        os.replace(tmp, str(index))
    except Exception as exc:
        print(f"[WARN] HTML regeneration failed: {exc}")


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DOCS_DIR), **kwargs)

    def do_POST(self):
        if self.path != "/api/status":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            url    = body.get("url", "").strip()
            status = body.get("status")          # "kept" | "removed" | null
            if not url:
                raise ValueError("missing url")
            if status not in ("kept", "removed", None):
                raise ValueError(f"invalid status: {status!r}")
            found = _update_status(url, status)
            self._json(200, {"ok": found})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Only log API calls, suppress static-file noise
        if "/api/" in (args[0] if args else ""):
            print(f"[API] {args[0]} → {args[1]}")


def main():
    parser = argparse.ArgumentParser(description="Local digest server with persistence.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = HTTPServer(("localhost", args.port), _Handler)
    url    = f"http://localhost:{args.port}"
    print(f"Serving digest at {url}  (Ctrl+C to stop)")
    print("Keep/Remove actions will update the JSON database and regenerate index.html.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
