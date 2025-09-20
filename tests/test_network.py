"""Тесты сетевых утилит CryptoPanic."""
from __future__ import annotations

from types import SimpleNamespace

from scripts.cryptopanic import network


class DummyResponse(SimpleNamespace):
    """Простая заглушка для requests.Response."""

    def __init__(self, status_code: int, url: str = "", headers: dict | None = None, text: str | None = None):
        super().__init__(status_code=status_code, url=url, headers=headers or {}, text=text or "")


def test_resolve_click_sync_eventual_success(monkeypatch):
    """Проверяем, что 429 приводит к повтору и в итоге возвращается финальный URL."""

    calls = []
    responses = [
        DummyResponse(429, headers={"Retry-After": "0"}),
        DummyResponse(200, url="https://example.com/article"),
    ]

    def fake_get(url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(network.requests, "get", fake_get)
    monkeypatch.setattr(network.time, "sleep", lambda *_: None)
    monkeypatch.setattr(network.random, "uniform", lambda *_, **__: 0)

    result = network._resolve_click_sync("123")

    assert result == "https://example.com/article"
    assert len(calls) == 2


def test_resolve_click_sync_gives_up_on_502(monkeypatch):
    """Если последовательность 502 превышает лимит, возвращается HTTP_502."""

    responses = [DummyResponse(502, url="https://example.com")] * (network.CLICK_502_MAX_TRIES + 1)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(network.requests, "get", fake_get)
    monkeypatch.setattr(network.time, "sleep", lambda *_: None)
    monkeypatch.setattr(network.random, "uniform", lambda *args, **kwargs: 0)

    result = network._resolve_click_sync("456")

    assert result.startswith("HTTP_502:")
    assert len(calls) == network.CLICK_502_MAX_TRIES
