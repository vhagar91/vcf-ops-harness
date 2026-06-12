# Proactive Alert Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Receive vROps alert webhooks in an embedded HTTP listener, enrich + summarize each alert via the agentic pipeline, and publish an executive summary + remediation steps to Slack.

**Architecture:** A new `src/webhook/` package started in a daemon thread by `create_and_start`, alongside the Slack Socket Mode connection. Pure request-decision logic (`handler.py`) is wrapped by a thin `ThreadingHTTPServer` (`server.py`) that acks `202` fast and dispatches processing to a background thread. `alerts.py` parses → enriches (light, via the existing `VropsClient`) → runs `run_pipeline` on a synthetic event → publishes through a pluggable `Publisher` (`publisher.py`, v1 = Slack).

**Tech Stack:** Python 3.13, stdlib `http.server` (no new dependency), slack_bolt `app.client`, pytest (pure logic + fake client/publisher + monkeypatched `run_pipeline`).

**Spec:** `docs/superpowers/specs/2026-06-12-proactive-alert-notification-design.md`

---

## File Structure

**Create:**
- `src/webhook/__init__.py` — empty package marker.
- `src/webhook/handler.py` — `WebhookDecision` + pure `handle_webhook(...)` (validation/parse, no sockets).
- `src/webhook/publisher.py` — `Publisher` protocol + `SlackPublisher`.
- `src/webhook/alerts.py` — `AlertInfo`, `parse_alert`, `passes_criticality`, `enrich`, `build_prompt`, `process_alert`.
- `src/webhook/server.py` — `start_webhook_server(...)` (thin `ThreadingHTTPServer` glue; not unit-tested).
- `tests/test_webhook.py` — tests for the pure/injected layers.

**Modify:**
- `src/config/settings.py` — six `webhook_*` / `vrops_alert_channel` config fields + `load_config`.
- `src/slack/bot.py` — `_maybe_start_webhook(...)` helper, called from `create_and_start`.
- `.env.example` — document the new vars.

**Reuses (no change):** `run_pipeline` (orchestrator), `ConversationMemory`, `PipelineEvent`, `VropsClient.get_resource_names` / `.get_alert`, `_build_client` (actions), `app.client` (bolt WebClient).

---

## Task 1: Pure request decision (`handler.py`)

**Files:**
- Create: `src/webhook/__init__.py` (empty), `src/webhook/handler.py`
- Test: `tests/test_webhook.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `src/webhook/__init__.py` as an empty file, then create `tests/test_webhook.py`:

```python
"""Tests for the proactive alert webhook (no network required)."""

from __future__ import annotations

from src.webhook.handler import handle_webhook, WebhookDecision, MAX_BODY_BYTES

_PATH = "/vrops/alert"
_TOK = "secret"


def _post(body=b'{"alertId":"a1"}', headers=None, query_token=None, path=_PATH, method="POST"):
    return handle_webhook(method, path, headers or {}, body, query_token,
                          token=_TOK, expected_path=_PATH)


def test_accepts_valid_token_in_header():
    d = _post(headers={"x-webhook-token": _TOK})
    assert d.status == 202
    assert d.payload == {"alertId": "a1"}


def test_accepts_valid_token_in_query():
    d = _post(query_token=_TOK)
    assert d.status == 202


def test_rejects_bad_or_missing_token():
    assert _post(headers={"x-webhook-token": "nope"}).status == 401
    assert _post().status == 401


def test_rejects_wrong_path_and_method():
    assert _post(path="/other", headers={"x-webhook-token": _TOK}).status == 404
    assert _post(method="GET", headers={"x-webhook-token": _TOK}).status == 405


def test_rejects_oversized_and_bad_json():
    big = b"x" * (MAX_BODY_BYTES + 1)
    assert _post(body=big, headers={"x-webhook-token": _TOK}).status == 413
    assert _post(body=b"not json", headers={"x-webhook-token": _TOK}).status == 400
    assert _post(body=b'["a","b"]', headers={"x-webhook-token": _TOK}).status == 400  # not an object
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.webhook.handler'`.

- [ ] **Step 3: Implement `handler.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/webhook/__init__.py src/webhook/handler.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): pure request-decision logic for vROps alert webhook

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Publisher (`publisher.py`)

**Files:**
- Create: `src/webhook/publisher.py`
- Test: `tests/test_webhook.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook.py`:

```python
from src.webhook.publisher import SlackPublisher


class _FakeSlackClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def chat_postMessage(self, channel, text):
        if self.fail:
            raise RuntimeError("slack down")
        self.calls.append({"channel": channel, "text": text})


def test_slack_publisher_posts_title_and_body():
    c = _FakeSlackClient()
    SlackPublisher(c, "#ops").publish("HEADLINE", "the body")
    assert c.calls[0]["channel"] == "#ops"
    assert "HEADLINE" in c.calls[0]["text"]
    assert "the body" in c.calls[0]["text"]


def test_slack_publisher_swallows_errors():
    c = _FakeSlackClient(fail=True)
    # Must not raise — publishing is best-effort.
    SlackPublisher(c, "#ops").publish("H", "B")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k slack_publisher -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.webhook.publisher'`.

- [ ] **Step 3: Implement `publisher.py`**

```python
"""Output publishers for proactive notifications. v1 posts to Slack; the Publisher
protocol lets Teams/ticketing adapters be added without touching the alert core."""

from __future__ import annotations

from typing import Protocol

from ..utils.logger import error


class Publisher(Protocol):
    def publish(self, title: str, body: str) -> None: ...


class SlackPublisher:
    """Posts to a Slack channel via the bolt App's WebClient (`app.client`)."""

    def __init__(self, client, channel: str):
        self._client = client
        self._channel = channel

    def publish(self, title: str, body: str) -> None:
        text = f"*{title}*\n{body}" if title else body
        try:
            self._client.chat_postMessage(channel=self._channel, text=text)
        except Exception as e:
            error("Failed to publish Slack notification", error=str(e))
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k slack_publisher -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/webhook/publisher.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): pluggable Publisher + SlackPublisher

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Alert parsing, filter, prompt (`alerts.py` — pure parts)

**Files:**
- Create: `src/webhook/alerts.py`
- Test: `tests/test_webhook.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook.py`:

```python
from src.webhook import alerts as A


def test_parse_alert_reads_common_keys():
    info = A.parse_alert({"alertId": "a1", "alertName": "High CPU",
                          "criticality": "critical", "resourceId": "r1",
                          "status": "ACTIVE"})
    assert info.alert_id == "a1"
    assert info.name == "High CPU"
    assert info.criticality == "CRITICAL"   # normalized upper
    assert info.resource_id == "r1"
    assert info.raw["alertId"] == "a1"


def test_parse_alert_tolerates_alternate_keys_and_missing():
    info = A.parse_alert({"id": "x", "alertDefinitionName": "Mem", "alertLevel": "warning"})
    assert info.alert_id == "x"
    assert info.name == "Mem"
    assert info.criticality == "WARNING"
    assert info.resource_id is None  # absent, tolerated


def test_passes_criticality_floor():
    crit = A.parse_alert({"criticality": "CRITICAL"})
    warn = A.parse_alert({"criticality": "WARNING"})
    assert A.passes_criticality(crit, "CRITICAL") is True
    assert A.passes_criticality(warn, "CRITICAL") is False
    assert A.passes_criticality(warn, "") is True          # no floor -> all pass
    assert A.passes_criticality(A.parse_alert({}), "CRITICAL") is True  # unknown -> don't drop


def test_build_prompt_includes_key_facts():
    info = A.parse_alert({"alertName": "High CPU", "criticality": "CRITICAL", "resourceId": "r1"})
    prompt = A.build_prompt(info, {"resource_name": "vm-01", "resource_kind": "VirtualMachine"})
    assert "High CPU" in prompt
    assert "CRITICAL" in prompt
    assert "vm-01" in prompt
    assert "remediation" in prompt.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k "parse_alert or criticality or build_prompt" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.webhook.alerts'`.

- [ ] **Step 3: Implement the pure parts of `alerts.py`**

```python
"""vROps alert webhook -> enriched prompt -> agentic pipeline -> publish."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

from ..config.types import PipelineEvent
from ..pipeline.orchestrator import run_pipeline
from ..utils.logger import info, error
from .publisher import Publisher

# Criticality ordering for the optional floor filter (least -> most severe).
_CRIT_ORDER = ["INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"]


@dataclass
class AlertInfo:
    alert_id: Optional[str]
    name: Optional[str]
    criticality: Optional[str]
    status: Optional[str]
    resource_id: Optional[str]
    resource_name: Optional[str]
    start_time: Optional[object]
    raw: dict = field(default_factory=dict)


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_alert(payload: dict) -> AlertInfo:
    """Extract alert fields from a (user-defined) vROps webhook payload, tolerantly."""
    crit = _first(payload, "criticality", "alertLevel", "alertCriticality", "status")
    return AlertInfo(
        alert_id=_first(payload, "alertId", "alert_id", "id"),
        name=_first(payload, "alertName", "alertDefinitionName", "name"),
        criticality=str(crit).upper() if crit is not None else None,
        status=_first(payload, "status", "alertStatus"),
        resource_id=_first(payload, "resourceId", "resource_id", "entityId"),
        resource_name=_first(payload, "resourceName", "resource_name", "entityName"),
        start_time=_first(payload, "startTimeUTC", "startDate", "startTime"),
        raw=payload,
    )


def passes_criticality(alert: AlertInfo, min_criticality: str) -> bool:
    """True if the alert meets the configured floor. Empty floor -> always True;
    an unknown criticality is never silently dropped."""
    if not min_criticality:
        return True
    floor = min_criticality.upper()
    try:
        return _CRIT_ORDER.index(alert.criticality or "") >= _CRIT_ORDER.index(floor)
    except ValueError:
        return True


def build_prompt(alert: AlertInfo, context: dict) -> str:
    """Build the message fed to the agentic pipeline."""
    name = alert.resource_name or context.get("resource_name") or alert.resource_id or "unknown object"
    kind = context.get("resource_kind") or ""
    detail = context.get("alert_detail")
    lines = [
        "A vROps alert fired. Respond for on-call operators with EXACTLY: "
        "1) a one-line executive summary, 2) the affected object, 3) three concrete "
        "remediation steps. Be specific and technical; never invent values.",
        "",
        f"Alert: {alert.name or '(unnamed)'} (criticality {alert.criticality or 'UNKNOWN'})",
        f"Affected object: {name} {('[' + kind + ']') if kind else ''}".strip(),
        f"Status: {alert.status or 'UNKNOWN'}  Started: {alert.start_time or 'n/a'}",
    ]
    if detail:
        lines.append(f"Alert detail: {json.dumps(detail)[:1500]}")
    lines.append(f"Raw payload: {json.dumps(alert.raw)[:1500]}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k "parse_alert or criticality or build_prompt" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/webhook/alerts.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): alert parsing, criticality filter, prompt builder

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Enrichment + orchestration (`alerts.py` — `enrich`, `process_alert`)

**Files:**
- Modify: `src/webhook/alerts.py` (append)
- Test: `tests/test_webhook.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook.py`:

```python
class _FakeVrops:
    def __init__(self):
        self.calls = []

    def get_resource_names(self, ids, chunk_size=100):
        self.calls.append(("names", ids))
        return {ids[0]: {"name": "vm-01", "kind": "VirtualMachine"}}

    def get_alert(self, alert_id):
        self.calls.append(("alert", alert_id))
        return {"alertId": alert_id, "symptoms": ["cpu>90"]}


class _RecordingPublisher:
    def __init__(self):
        self.published = []

    def publish(self, title, body):
        self.published.append((title, body))


def test_enrich_resolves_name_and_alert_detail():
    a = A.parse_alert({"alertId": "a1", "resourceId": "r1"})
    ctx = A.enrich(_FakeVrops(), a)
    assert ctx["resource_name"] == "vm-01"
    assert ctx["resource_kind"] == "VirtualMachine"
    assert ctx["alert_detail"]["symptoms"] == ["cpu>90"]


def test_enrich_handles_none_client():
    a = A.parse_alert({"alertId": "a1", "resourceId": "r1"})
    assert A.enrich(None, a) == {}


def test_process_alert_publishes_pipeline_reply(monkeypatch):
    async def fake_pipeline(event, memory, registry, llm_config):
        assert "remediation" in event.text.lower()  # got the built prompt
        return "1) summary 2) vm-01 3) steps"
    monkeypatch.setattr(A, "run_pipeline", fake_pipeline)
    pub = _RecordingPublisher()
    A.process_alert({"alertId": "a1", "alertName": "High CPU", "criticality": "CRITICAL",
                     "resourceId": "r1"},
                    _FakeVrops(), memory=None, registry=None, llm_config=None, publisher=pub)
    assert len(pub.published) == 1
    title, body = pub.published[0]
    assert "High CPU" in title
    assert "summary" in body


def test_process_alert_publishes_fallback_on_pipeline_error(monkeypatch):
    async def boom(event, memory, registry, llm_config):
        raise RuntimeError("llm down")
    monkeypatch.setattr(A, "run_pipeline", boom)
    pub = _RecordingPublisher()
    A.process_alert({"alertId": "a1", "alertName": "High CPU", "criticality": "CRITICAL"},
                    _FakeVrops(), memory=None, registry=None, llm_config=None, publisher=pub)
    assert len(pub.published) == 1
    assert "unavailable" in pub.published[0][1].lower()


def test_process_alert_skips_below_criticality(monkeypatch):
    async def fake_pipeline(event, memory, registry, llm_config):
        return "should not run"
    monkeypatch.setattr(A, "run_pipeline", fake_pipeline)
    pub = _RecordingPublisher()
    A.process_alert({"alertId": "a1", "criticality": "WARNING"},
                    _FakeVrops(), memory=None, registry=None, llm_config=None,
                    publisher=pub, min_criticality="CRITICAL")
    assert pub.published == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k "enrich or process_alert" -v`
Expected: FAIL with `AttributeError: module 'src.webhook.alerts' has no attribute 'enrich'`.

- [ ] **Step 3: Implement `enrich`, `_alert_headline`, `process_alert`**

Append to `src/webhook/alerts.py`:

```python
def enrich(client, alert: AlertInfo) -> dict:
    """Light enrichment: resolve the resource name (when absent) and fetch the alert's
    full detail. Tolerates a missing client (no creds) by returning what it can."""
    context: dict = {}
    if client is None:
        return context
    try:
        if not alert.resource_name and alert.resource_id:
            names = client.get_resource_names([alert.resource_id]) or {}
            entry = names.get(alert.resource_id) or {}
            context["resource_name"] = entry.get("name")
            context["resource_kind"] = entry.get("kind")
        if alert.alert_id:
            context["alert_detail"] = client.get_alert(alert.alert_id)
    except Exception as e:
        error("Alert enrichment failed (continuing)", error=str(e))
    return context


def _alert_headline(alert: AlertInfo) -> str:
    return (f"{alert.criticality or 'ALERT'}: {alert.name or '(unnamed alert)'} "
            f"on {alert.resource_name or alert.resource_id or 'unknown object'}")


def process_alert(payload: dict, client, memory, registry, llm_config,
                  publisher: Publisher, min_criticality: str = "") -> None:
    """Parse -> filter -> enrich -> run the agentic pipeline -> publish. Never raises;
    on failure publishes a minimal fallback so the alert is not silently lost."""
    alert = parse_alert(payload)
    if not passes_criticality(alert, min_criticality):
        info("Alert below criticality floor; skipping", criticality=alert.criticality)
        return
    headline = _alert_headline(alert)
    try:
        context = enrich(client, alert)
        prompt = build_prompt(alert, context)
        event = PipelineEvent(channel="vrops-webhook", user_id="vrops",
                              text=prompt, thread_ts=alert.alert_id or "alert")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            reply = loop.run_until_complete(run_pipeline(event, memory, registry, llm_config))
        finally:
            loop.close()
        publisher.publish(f"🚨 {headline}", reply or "(no summary generated)")
    except Exception as e:
        error("Alert processing failed", error=str(e), type=type(e).__name__)
        publisher.publish(f"🚨 {headline}",
                          "⚠️ Automated summary unavailable; review the alert in vROps.")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -v`
Expected: PASS (all webhook tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/webhook/alerts.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): light enrichment + process_alert orchestration

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: HTTP listener (`server.py`)

Thin glue over `handle_webhook`; not unit-tested (consistent with the Slack and `vrops_client` HTTP code). Verified by import smoke here and a localhost curl in Task 7.

**Files:**
- Create: `src/webhook/server.py`

- [ ] **Step 1: Implement `server.py`**

```python
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
```

- [ ] **Step 2: Verify it imports**

Run: `.venv/bin/python -c "from src.webhook.server import start_webhook_server; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/webhook/server.py
git commit -m "$(cat <<'EOF'
feat(webhook): embedded ThreadingHTTPServer listener (ack-fast + dispatch)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Configuration (`settings.py`)

**Files:**
- Modify: `src/config/settings.py`
- Test: `tests/test_webhook.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_webhook.py`:

```python
def test_config_loads_webhook_fields(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "y")
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_PORT", "9099")
    monkeypatch.setenv("WEBHOOK_TOKEN", "tok")
    monkeypatch.setenv("VROPS_ALERT_CHANNEL", "#ops")
    monkeypatch.setenv("WEBHOOK_MIN_CRITICALITY", "CRITICAL")
    from src.config.settings import load_config
    c = load_config()
    assert c.webhook_enabled is True
    assert c.webhook_port == 9099
    assert c.webhook_token == "tok"
    assert c.webhook_path == "/vrops/alert"     # default
    assert c.vrops_alert_channel == "#ops"
    assert c.webhook_min_criticality == "CRITICAL"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k config_loads_webhook -v`
Expected: FAIL with `AttributeError: 'HarnessConfig' object has no attribute 'webhook_enabled'`.

- [ ] **Step 3: Add the fields**

In `src/config/settings.py`, in `class HarnessConfig`, after the `vrops_site_map_file` field, add:

```python
    # Proactive alert webhook (off unless WEBHOOK_ENABLED=true)
    webhook_enabled: bool = False
    webhook_port: int = 8088
    webhook_token: str = ""
    webhook_path: str = "/vrops/alert"
    vrops_alert_channel: str = ""
    webhook_min_criticality: str = ""
```

In `load_config()`, after the `vrops_site_map_file=...` line, add:

```python
        webhook_enabled=os.environ.get("WEBHOOK_ENABLED", "false").lower() == "true",
        webhook_port=int(os.environ.get("WEBHOOK_PORT", "8088")),
        webhook_token=os.environ.get("WEBHOOK_TOKEN", ""),
        webhook_path=os.environ.get("WEBHOOK_PATH", "/vrops/alert"),
        vrops_alert_channel=os.environ.get("VROPS_ALERT_CHANNEL", ""),
        webhook_min_criticality=os.environ.get("WEBHOOK_MIN_CRITICALITY", ""),
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k config_loads_webhook -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/config/settings.py tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(config): webhook + alert-channel settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire into the bot (`bot.py`) + `.env.example`

**Files:**
- Modify: `src/slack/bot.py`, `.env.example`
- Test: `tests/test_webhook.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook.py`:

```python
import types

from src.slack import bot as botmod


def _cfg(**over):
    base = dict(webhook_enabled=True, webhook_token="tok", vrops_alert_channel="#ops",
                webhook_port=8088, webhook_path="/vrops/alert", webhook_min_criticality="")
    base.update(over)
    return types.SimpleNamespace(**base)


def _fake_app():
    return types.SimpleNamespace(client=object())


def test_maybe_start_webhook_starts_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "start_webhook_server",
                        lambda port, token, path, dispatch: calls.append((port, token, path)))
    botmod._maybe_start_webhook(_fake_app(), _cfg(), memory=None, registry=None, llm_config=None)
    assert calls == [(8088, "tok", "/vrops/alert")]


def test_maybe_start_webhook_skips_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "start_webhook_server", lambda *a, **k: calls.append(a))
    botmod._maybe_start_webhook(_fake_app(), _cfg(webhook_enabled=False),
                                memory=None, registry=None, llm_config=None)
    assert calls == []


def test_maybe_start_webhook_refuses_without_token_or_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "start_webhook_server", lambda *a, **k: calls.append(a))
    botmod._maybe_start_webhook(_fake_app(), _cfg(webhook_token=""),
                                memory=None, registry=None, llm_config=None)
    botmod._maybe_start_webhook(_fake_app(), _cfg(vrops_alert_channel=""),
                                memory=None, registry=None, llm_config=None)
    assert calls == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -k maybe_start_webhook -v`
Expected: FAIL with `AttributeError: module 'src.slack.bot' has no attribute '_maybe_start_webhook'`.

- [ ] **Step 3: Implement the wiring in `bot.py`**

Add these imports near the top of `src/slack/bot.py` (after the existing `from ..ai.llm import LlmConfig` import):

```python
from ..webhook.server import start_webhook_server
from ..webhook.publisher import SlackPublisher
from ..webhook.alerts import process_alert
from ..actions.builtin.vrops.actions import _build_client
```

Add this module-level function (e.g. right after the imports / before `create_and_start`):

```python
def _maybe_start_webhook(app, config, memory, registry, llm_config) -> None:
    """Start the proactive vROps alert webhook listener, if configured. Fail-safe:
    never starts without a token (would be an open endpoint) or an alert channel
    (nowhere to publish)."""
    if not config.webhook_enabled:
        return
    if not config.webhook_token:
        error("WEBHOOK_ENABLED but WEBHOOK_TOKEN is empty; not starting webhook listener")
        return
    if not config.vrops_alert_channel:
        error("WEBHOOK_ENABLED but VROPS_ALERT_CHANNEL is empty; not starting webhook listener")
        return

    publisher = SlackPublisher(app.client, config.vrops_alert_channel)

    def _dispatch(payload):
        try:
            client = _build_client({})
        except Exception:
            client = None  # no vROps creds -> summarize from the raw payload only
        process_alert(payload, client, memory, registry, llm_config, publisher,
                      config.webhook_min_criticality)

    start_webhook_server(config.webhook_port, config.webhook_token,
                         config.webhook_path, _dispatch)
```

Then, inside `create_and_start`, immediately before the `is_socket_mode = bool(config.slack_app_token)` line, add:

```python
    # Optional: proactive vROps alert webhook (embedded listener).
    _maybe_start_webhook(app, config, memory, registry, llm_config)
```

- [ ] **Step 4: Update `.env.example`**

Add a documented block near the `VROPS_*` entries in `.env.example`:

```bash
# --- Proactive alert webhook (vROps Webhook Outbound -> LLM -> Slack) ---
# Off unless WEBHOOK_ENABLED=true. Requires WEBHOOK_TOKEN and VROPS_ALERT_CHANNEL.
WEBHOOK_ENABLED=false
WEBHOOK_PORT=8088
# Shared secret vROps must send as the X-Webhook-Token header or ?token= query param.
WEBHOOK_TOKEN=
WEBHOOK_PATH=/vrops/alert
# Slack channel id/name to publish alert summaries to (e.g. #vrops-alerts).
VROPS_ALERT_CHANNEL=
# Optional floor: INFORMATION | WARNING | IMMEDIATE | CRITICAL (empty = all).
WEBHOOK_MIN_CRITICALITY=
```

- [ ] **Step 5: Run tests + import smoke**

Run: `.venv/bin/python -m pytest tests/test_webhook.py -v` (all pass) and
`.venv/bin/python -m pytest tests/ -q` (no regressions) and
`.venv/bin/python -c "import src.main; print('main OK')"`.

- [ ] **Step 6: Commit**

```bash
git add src/slack/bot.py .env.example tests/test_webhook.py
git commit -m "$(cat <<'EOF'
feat(webhook): start embedded alert listener from the bot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: End-to-end localhost smoke

Manual verification of the running listener (no live vROps needed).

- [ ] **Step 1: Start a bot with the webhook enabled (a fake Slack channel is fine for a 202 check)**

In a scratch shell, run a minimal driver that starts only the listener with a fake dispatch:
```bash
.venv/bin/python -c "
import time, json
from src.webhook.server import start_webhook_server
seen = []
start_webhook_server(8088, 'tok', '/vrops/alert', lambda p: seen.append(p))
time.sleep(0.5)
import urllib.request
def post(tokhdr, body):
    req = urllib.request.Request('http://127.0.0.1:8088/vrops/alert', data=body,
        method='POST', headers={'X-Webhook-Token': tokhdr})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code
print('valid token ->', post('tok', b'{\"alertId\":\"a1\",\"criticality\":\"CRITICAL\"}'))
print('bad token   ->', post('nope', b'{}'))
time.sleep(0.3)
print('dispatched   ->', seen)
"
```
Expected: `valid token -> 202`, `bad token -> 401`, `dispatched -> [{'alertId': 'a1', ...}]`.

- [ ] **Step 2: (with live vROps + Slack) Configure vROps Webhook Outbound**

Point a vROps Webhook Outbound notification at `http://<bot-host>:8088/vrops/alert` with header `X-Webhook-Token: <WEBHOOK_TOKEN>`, trigger a test alert, and confirm a summary posts to `VROPS_ALERT_CHANNEL`. If fields are missing in the summary, adjust the vROps payload template or extend `parse_alert`'s key fallbacks.

---

## Self-Review

**Spec coverage:**
- Embedded listener in a daemon thread, ack-fast + dispatch → Task 5 (`server.py`), Task 7 (wiring). ✓
- Pure validation (token header/query, path, method, size, JSON) → Task 1 (`handle_webhook`). ✓
- Reuse agentic pipeline via synthetic `PipelineEvent` (channel `vrops-webhook`, thread=alertId) → Task 4 (`process_alert`). ✓
- Light enrichment (resource name + alert detail), tolerant of missing client → Task 4 (`enrich`). ✓
- Pluggable publisher, Slack v1 → Task 2. ✓
- Tolerant `parse_alert` + criticality filter → Task 3. ✓
- Fallback publish on failure → Task 4 (`process_alert` except). ✓
- Config + fail-safe gating (no token/channel → refuse) → Task 6 (fields), Task 7 (`_maybe_start_webhook`). ✓
- Security (401/404/405/413/400, token header or query) → Task 1. ✓
- Tests for each pure/injected layer + localhost smoke → Tasks 1–8. ✓
- Out-of-scope respected (no Teams/ticket adapters, no dedup, no persistence, no write-back). ✓

**Placeholder scan:** No TBD/TODO; every code step is complete and runnable. The vROps-template note in Task 8 is a manual verification instruction, not a placeholder.

**Type consistency:** `handle_webhook(...) -> WebhookDecision{status,message,payload}`; `server.py` reads `decision.status`/`decision.payload`. `parse_alert -> AlertInfo`; `enrich(client, AlertInfo) -> dict`; `build_prompt(AlertInfo, dict)`; `process_alert(payload, client, memory, registry, llm_config, publisher, min_criticality="")` — signature matches the Task 4 tests and the Task 7 `_dispatch` call. `SlackPublisher(client, channel).publish(title, body)` matches Task 2 tests and `process_alert`'s usage. `start_webhook_server(port, token, path, dispatch)` matches Task 5 def and Task 7 call/test. `run_pipeline` monkeypatched on `src.webhook.alerts` (it's imported there) — matches Task 4 tests.
