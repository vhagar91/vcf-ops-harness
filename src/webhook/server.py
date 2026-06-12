"""Inbound HTTP listener for the vROps webhook, embedded in the bot process.

Thin glue over handler.handle_webhook (which holds the testable logic). Acks fast
(202) and dispatches alert processing to a daemon thread, since the pipeline takes
minutes and vROps webhooks time out quickly."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .handler import handle_webhook, MAX_BODY_BYTES
from ..utils.logger import info, error


def _safe_dispatch(dispatch, payload) -> None:
    try:
        dispatch(payload)
    except Exception as e:
        error("Webhook dispatch failed", error=str(e))


def start_webhook_server(port: int, token: str, path: str, dispatch) -> None:
    """Start the webhook listener in a daemon thread. `dispatch(payload)` is invoked
    (in its own daemon thread) for each accepted alert."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr access logging
            pass

        def _respond(self, status: int, message: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))

        def do_POST(self):
            parsed = urlparse(self.path)
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES + 1)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            query_token = (parse_qs(parsed.query).get("token") or [None])[0]
            decision = handle_webhook("POST", parsed.path, headers, body, query_token,
                                      token=token, expected_path=path)
            self._respond(decision.status, decision.message)
            if decision.status == 202 and decision.payload is not None:
                threading.Thread(
                    target=_safe_dispatch, args=(dispatch, decision.payload), daemon=True
                ).start()

        def do_GET(self):
            parsed = urlparse(self.path)
            self._respond(405 if parsed.path == path else 404, "method not allowed")

    def _serve() -> None:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        except Exception as e:
            error("Webhook listener failed to bind", port=port, error=str(e))
            return
        info("Webhook listener started", port=port, path=path)
        httpd.serve_forever()

    threading.Thread(target=_serve, name="vrops-webhook", daemon=True).start()
