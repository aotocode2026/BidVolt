from __future__ import annotations


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_info(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "BidVolt API"
