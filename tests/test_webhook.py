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
