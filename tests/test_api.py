"""Tests for the routes that must not depend on VictoriaMetrics."""

import json
import time

import pytest

from app.main import app


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    return app.test_client()


def test_api_time_returns_a_plausible_timestamp(client):
    before = time.time()
    r = client.get("/api/time")
    after = time.time()
    assert r.status_code == 200
    t = json.loads(r.data)["t"]
    assert before <= t <= after


def test_api_time_forbids_caching(client):
    """A cached timestamp would tell the visitor their clock is off by the cache age."""
    r = client.get("/api/time")
    cache = r.headers["Cache-Control"]
    assert "no-store" in cache
    assert "max-age=0" in cache


def test_api_time_never_touches_the_metrics_store(client, monkeypatch):
    """It is called several times a visit and its latency is the measurement error."""

    def explode(*args, **kwargs):
        raise AssertionError("/api/time must not query VictoriaMetrics")

    monkeypatch.setattr("app.vm._query", explode)
    monkeypatch.setattr("app.vm.query_range", explode)
    assert client.get("/api/time").status_code == 200


def test_healthz_never_touches_the_metrics_store(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("liveness must not depend on another service")

    monkeypatch.setattr("app.vm._query", explode)
    assert client.get("/healthz").status_code == 200


def test_readyz_reports_503_when_the_store_is_unreachable(client, monkeypatch):
    def fail(*args, **kwargs):
        raise __import__("app.vm", fromlist=["vm"]).VictoriaMetricsError("nope")

    monkeypatch.setattr("app.vm._query", fail)
    assert client.get("/readyz").status_code == 503


def test_history_rejects_an_unknown_range(client):
    assert client.get("/api/history?range=nope").status_code == 400
