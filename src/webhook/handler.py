"""Pure request-decision logic for the inbound vROps webhook — no sockets, so it is
unit-testable. server.py wraps this in a ThreadingHTTPServer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

MAX_BODY_BYTES = 1_000_000  # 1 MB cap on the inbound payload


@dataclass
class WebhookDecision:
    status: int                      # HTTP status to return
    message: str                     # short status/error text
    payload: Optional[dict] = None   # parsed alert payload when accepted (status 202)


def handle_webhook(method: str, path: str, headers: dict, body: bytes,
                   query_token: Optional[str], *, token: str,
                   expected_path: str) -> WebhookDecision:
    """Validate an inbound webhook request and parse its body.

    `headers` keys must be lowercased by the caller. status 202 means accept and
    dispatch `payload`; any other status is a rejection to return verbatim.
    """
    if path != expected_path:
        return WebhookDecision(404, "not found")
    if method.upper() != "POST":
        return WebhookDecision(405, "method not allowed")
    if len(body) > MAX_BODY_BYTES:
        return WebhookDecision(413, "payload too large")
    supplied = headers.get("x-webhook-token") or query_token
    if not token or supplied != token:
        return WebhookDecision(401, "unauthorized")
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return WebhookDecision(400, "invalid json")
    if not isinstance(data, dict):
        return WebhookDecision(400, "expected a json object")
    return WebhookDecision(202, "accepted", payload=data)
