"""Minimal EIS reachability probe for a fresh App Platform IP."""

import json
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ENDPOINTS = (
    ("eis-public", "https://zakupki.gov.ru/"),
    ("eis-documents", "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"),
)
RESULTS = {"state": "pending", "checked_at": None, "endpoints": []}


def check_endpoint(name, url, timeout=12):
    started_at = time.monotonic()
    request = Request(url, method="HEAD", headers={"User-Agent": "psadmin-eis-probe/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return {
                "name": name,
                "url": url,
                "reachable": True,
                "http_status": response.status,
                "duration_ms": round((time.monotonic() - started_at) * 1000),
            }
    except HTTPError as error:
        return {
            "name": name,
            "url": url,
            "reachable": True,
            "http_status": error.code,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }
    except (URLError, socket.timeout, ssl.SSLError, OSError) as error:
        return {
            "name": name,
            "url": url,
            "reachable": False,
            "error_type": type(error).__name__,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
        }


def run_probe():
    RESULTS["endpoints"] = [check_endpoint(name, url) for name, url in ENDPOINTS]
    RESULTS["checked_at"] = int(time.time())
    RESULTS["state"] = "complete"


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        body = json.dumps(RESULTS, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def main():
    threading.Thread(target=run_probe, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), ProbeHandler).serve_forever()


if __name__ == "__main__":
    main()
