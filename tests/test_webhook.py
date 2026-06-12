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
    assert _post(body=b'["a","b"]', headers={"x-webhook-token": _TOK}).status == 400


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


from src.webhook import alerts as A


def test_parse_alert_reads_common_keys():
    info = A.parse_alert({"alertId": "a1", "alertName": "High CPU",
                          "criticality": "critical", "resourceId": "r1",
                          "status": "ACTIVE"})
    assert info.alert_id == "a1"
    assert info.name == "High CPU"
    assert info.criticality == "CRITICAL"
    assert info.resource_id == "r1"
    assert info.raw["alertId"] == "a1"


def test_parse_alert_tolerates_alternate_keys_and_missing():
    info = A.parse_alert({"id": "x", "alertDefinitionName": "Mem", "alertLevel": "warning"})
    assert info.alert_id == "x"
    assert info.name == "Mem"
    assert info.criticality == "WARNING"
    assert info.resource_id is None


def test_passes_criticality_floor():
    crit = A.parse_alert({"criticality": "CRITICAL"})
    warn = A.parse_alert({"criticality": "WARNING"})
    assert A.passes_criticality(crit, "CRITICAL") is True
    assert A.passes_criticality(warn, "CRITICAL") is False
    assert A.passes_criticality(warn, "") is True
    assert A.passes_criticality(A.parse_alert({}), "CRITICAL") is True


def test_build_prompt_includes_key_facts():
    info = A.parse_alert({"alertName": "High CPU", "criticality": "CRITICAL", "resourceId": "r1"})
    prompt = A.build_prompt(info, {"resource_name": "vm-01", "resource_kind": "VirtualMachine"})
    assert "High CPU" in prompt
    assert "CRITICAL" in prompt
    assert "vm-01" in prompt
    assert "remediation" in prompt.lower()


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
        assert "remediation" in event.text.lower()
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


def test_process_alert_headline_uses_enriched_name(monkeypatch):
    # Payload has only resourceId; enrich resolves it to vm-01 -> title shows the name.
    async def fake_pipeline(event, memory, registry, llm_config):
        return "ok"
    monkeypatch.setattr(A, "run_pipeline", fake_pipeline)
    pub = _RecordingPublisher()
    A.process_alert({"alertId": "a1", "alertName": "High CPU", "criticality": "CRITICAL",
                     "resourceId": "r1"},
                    _FakeVrops(), memory=None, registry=None, llm_config=None, publisher=pub)
    assert "vm-01" in pub.published[0][0]   # title contains the resolved name


def test_process_alert_skips_below_criticality(monkeypatch):
    async def fake_pipeline(event, memory, registry, llm_config):
        return "should not run"
    monkeypatch.setattr(A, "run_pipeline", fake_pipeline)
    pub = _RecordingPublisher()
    A.process_alert({"alertId": "a1", "criticality": "WARNING"},
                    _FakeVrops(), memory=None, registry=None, llm_config=None,
                    publisher=pub, min_criticality="CRITICAL")
    assert pub.published == []


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
    assert c.webhook_path == "/vrops/alert"
    assert c.vrops_alert_channel == "#ops"
    assert c.webhook_min_criticality == "CRITICAL"


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
