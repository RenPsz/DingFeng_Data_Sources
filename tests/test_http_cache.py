import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from cnfin import CNFin, SourceBlocked, SourceError
from cnfin import _Cache, _HttpClient, set_proxy


class StubResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self.payload = payload or {"ok": True}
        self.encoding = "utf-8"
        self.text = json.dumps(self.payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


def make_http(monkeypatch: pytest.MonkeyPatch, sessions: dict[bool, Mock], **kwargs) -> _HttpClient:
    client = _HttpClient(min_interval=0, **kwargs)
    monkeypatch.setattr(client, "_session", lambda trust_env: sessions[trust_env])
    monkeypatch.setattr(time, "sleep", lambda _: None)
    return client


def test_connection_error_falls_back_to_real_direct_session(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, direct = Mock(), Mock()
    proxy.request.side_effect = requests.exceptions.ConnectionError("proxy reset")
    direct.request.return_value = StubResponse()
    client = make_http(monkeypatch, {True: proxy, False: direct}, retries=0)

    response = client.request("https://example.test/data")

    assert response.status_code == 200
    assert proxy.request.call_count == 1
    assert direct.request.call_count == 1


def test_403_tries_direct_and_counts_toward_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy, direct = Mock(), Mock()
    proxy.request.return_value = StubResponse(403)
    direct.request.return_value = StubResponse()
    client = make_http(monkeypatch, {True: proxy, False: direct}, retries=0)

    assert client.request("https://example.test/data").status_code == 200
    assert direct.request.call_count == 1


def test_repeated_blocking_opens_circuit_without_single_failure_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct = Mock()
    direct.request.return_value = StubResponse(403)
    client = make_http(monkeypatch, {False: direct}, retries=4, trust_env=False)
    url = "https://example.test/data"

    with pytest.raises(SourceBlocked):
        client.request(url)
    assert direct.request.call_count == 1
    with pytest.raises(SourceBlocked):
        client.request(url)
    assert direct.request.call_count == 2
    with pytest.raises(SourceBlocked, match="冷却中"):
        client.request(url)
    assert direct.request.call_count == 2


def test_single_connection_failure_does_not_open_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    direct = Mock()
    direct.request.side_effect = requests.exceptions.ConnectionError("reset")
    client = make_http(monkeypatch, {False: direct}, retries=0, trust_env=False)
    url = "https://example.test/data"

    with pytest.raises(SourceError):
        client.request(url)
    direct.request.side_effect = None
    direct.request.return_value = StubResponse()
    assert client.request(url).status_code == 200
    assert direct.request.call_count == 2


def test_explicit_proxy_is_not_attached_to_direct_session() -> None:
    client = _HttpClient(proxy="http://127.0.0.1:7897")
    proxied = client._session(True)
    direct = client._session(False)

    assert proxied.proxies["https"] == "http://127.0.0.1:7897"
    assert direct.proxies == {}
    assert direct.trust_env is False


def test_proxy_configuration_survives_client_creation_and_instances_are_isolated() -> None:
    try:
        set_proxy("http://127.0.0.1:7897", trust_env=True)
        first = CNFin(cache=False, timeout=3)
        second = CNFin(cache=False, timeout=7)
        assert first.http is not second.http
        assert first.http.timeout == 3
        assert second.http.timeout == 7
        assert first.http._proxy == "http://127.0.0.1:7897"
        assert first.http._session(False).proxies == {}
    finally:
        set_proxy(None, trust_env=True)


def test_cache_roundtrip_uses_safe_json_and_preserves_metadata(tmp_path: Path) -> None:
    cache = _Cache(tmp_path)
    frame = pd.DataFrame({"date": ["2024-01-01"], "close": [1.25], "count": [2]})
    frame.attrs.update({"source": "tencent", "elapsed_ms": 3, "cached": False})

    cache.set("kline:" + "x" * 300, frame)
    restored = cache.get("kline:" + "x" * 300, ttl=60)

    assert restored is not None
    assert restored["close"].iloc[0] == 1.25
    assert restored["count"].iloc[0] == 2
    assert restored.attrs["source"] == "tencent"
    assert restored.attrs["cached"] is True
    assert all(path.suffix == ".json" for path in tmp_path.iterdir())


def test_cache_full_key_prevents_long_key_collision(tmp_path: Path) -> None:
    cache = _Cache(tmp_path)
    prefix = "k" * 200
    cache.set(prefix + "A", pd.DataFrame({"value": [1]}))
    cache.set(prefix + "B", pd.DataFrame({"value": [2]}))

    assert cache.get(prefix + "A", 60)["value"].iloc[0] == 1
    assert cache.get(prefix + "B", 60)["value"].iloc[0] == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_cache_corruption_and_expiry_are_clean_misses(tmp_path: Path) -> None:
    cache = _Cache(tmp_path)
    key = "quote_test"
    cache.set(key, pd.DataFrame({"value": [1]}))
    path = cache._file(key)
    path.write_text("not json", encoding="utf-8")
    assert cache.get(key, 60) is None

    cache.set(key, pd.DataFrame({"value": [1]}))
    old = time.time() - 120
    os.utime(path, (old, old))
    assert cache.get(key, 60) is None


def test_cache_concurrent_writers_never_expose_partial_json(tmp_path: Path) -> None:
    cache = _Cache(tmp_path)
    key = "shared"

    def write_and_read(i: int) -> int:
        for n in range(20):
            cache.set(key, pd.DataFrame({"value": [i * 100 + n]}))
            restored = cache.get(key, 60)
            if restored is not None:
                assert restored.columns.tolist() == ["value"]
                assert len(restored) == 1
        return i

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(write_and_read, range(8))) == 28
    assert not list(tmp_path.glob("*.tmp"))
    assert cache.get(key, 60) is not None


def test_cache_clear_only_removes_own_json_files(tmp_path: Path) -> None:
    cache = _Cache(tmp_path)
    cache.set("a", pd.DataFrame({"value": [1]}))
    unrelated = tmp_path / "keep.json"
    unrelated.write_text("{}", encoding="utf-8")

    assert cache.clear() == 1
    assert unrelated.exists()
