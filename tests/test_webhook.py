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
