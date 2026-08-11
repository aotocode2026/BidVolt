from __future__ import annotations


def test_demo_page_served(client):
    r = client.get("/demo/")
    assert r.status_code == 200
    assert "BidVolt" in r.text
    assert "app.js" in r.text

    js = client.get("/demo/app.js")
    assert js.status_code == 200
    assert "/auth/" in js.text
