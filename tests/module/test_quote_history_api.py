from __future__ import annotations


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "qh@test.com", "password": "Abc12345", "enterprise_name": "报价企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()["project_id"]
    calc = client.post(
        "/api/v1/quotes/calculate",
        json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1, "project_id": pid},
        headers=headers,
    )
    assert calc.status_code == 200
    return headers, pid, calc.json()["calc_id"]


def test_calc_list_and_detail(client):
    h, pid, cid = _setup(client)

    lst = client.get(f"/api/v1/quotes?project_id={pid}", headers=h)
    assert lst.status_code == 200
    items = lst.json()["items"]
    assert any(i["calc_id"] == cid and i["project_id"] == pid for i in items)

    det = client.get(f"/api/v1/quotes/{cid}", headers=h)
    assert det.status_code == 200
    body = det.json()
    assert body["calc_id"] == cid
    assert body["result"]["suggested"] > 0
    assert body["samples"], "测算详情应返回冻结样本"
    assert body["samples"][0]["sample_id"]


def test_sample_detail_and_trend(client):
    h, _, cid = _setup(client)
    det = client.get(f"/api/v1/quotes/{cid}", headers=h).json()
    sid = det["samples"][0]["sample_id"]

    sd = client.get(f"/api/v1/quotes/history/samples/{sid}", headers=h)
    assert sd.status_code == 200
    sample = sd.json()
    assert sample["sample_id"] == sid
    assert isinstance(sample["win_price"], str) and float(sample["win_price"]) > 0  # 金额字符串契约（Issue #6）
    assert sample["win_date"]

    tr = client.get("/api/v1/quotes/history/CABLE-YJV-3x95/trend", headers=h)
    assert tr.status_code == 200
    trend = tr.json()
    assert trend["sample_count"] == 8
    assert trend["median_price"] and trend["max_price"] and trend["min_price"]
    assert trend["region_breakdown"]["华东"]["count"] == 8
    assert trend["readonly"] is True


def test_calc_cross_enterprise_denied(client):
    h, _, cid = _setup(client)
    r2 = client.post(
        "/api/v1/auth/register",
        json={"email": "qh2@test.com", "password": "Abc12345", "enterprise_name": "其他"},
    )
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get(f"/api/v1/quotes/{cid}", headers=h2).status_code == 404
    assert client.get(f"/api/v1/quotes?project_id={1}", headers=h2).json()["items"] == []
