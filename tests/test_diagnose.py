from src.actions.builtin.vrops.vrops_client import VropsClient


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _client():
    # __init__ does no network (only disables SSL warnings); we override _request.
    return VropsClient("host", "user", "pass")


def test_get_stat_series_extracts_ordered_samples():
    payload = {
        "values": [
            {"stat-list": {"stat": [
                {"statKey": {"key": "cpu|usage_average"},
                 "data": [10.0, None, 20.0, 30.0]},
            ]}}
        ]
    }
    client = _client()
    client._request = lambda method, path, **kw: _FakeResp(payload)
    series = client.get_stat_series("rid", ["cpu|usage_average"], hours_back=24)
    assert series == {"cpu|usage_average": [10.0, 20.0, 30.0]}


def test_get_stat_series_returns_empty_on_error_status():
    client = _client()
    client._request = lambda method, path, **kw: _FakeResp({}, status=500)
    assert client.get_stat_series("rid", ["cpu|usage_average"]) == {}
