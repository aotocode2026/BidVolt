from __future__ import annotations

import io


def _headers(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "e@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload_txt(client, headers, name="营业执照.txt"):
    r = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", (name, io.BytesIO("统一社会信用代码 91110000XXXX".encode()), "text/plain"))],
        headers=headers,
    )
    return r.json()["files"][0]["file_id"]


def test_upload_creates_asset_and_ingest_classifies(client):
    h = _headers(client)
    _upload_txt(client, h)

    assets = client.get("/api/v1/enterprise/assets", headers=h)
    assert len(assets.json()) == 1
    asset_id = assets.json()[0]["asset_id"]
    assert assets.json()[0]["status"] == 1  # 待分类

    ingest = client.post("/api/v1/enterprise/ingest", json={"asset_ids": [asset_id]}, headers=h)
    assert ingest.status_code == 202
    assert ingest.json()["classified"][0]["category"] == "证照"

    detail = client.get(f"/api/v1/enterprise/assets/{asset_id}", headers=h)
    assert detail.json()["status"] == 2  # 待确认
    assert any(f["fact_key"] == "credit_code" for f in detail.json()["facts"])


def test_categories_and_correction(client):
    h = _headers(client)
    _upload_txt(client, h, name="随手记.txt")
    cats = client.get("/api/v1/enterprise/categories", headers=h)
    assert len(cats.json()) >= 7

    assets = client.get("/api/v1/enterprise/assets", headers=h)
    asset_id = assets.json()[0]["asset_id"]
    target_cat = next(c for c in cats.json() if c["name"] == "业绩")
    fix = client.patch(f"/api/v1/enterprise/assets/{asset_id}/category", json={"category_id": target_cat["category_id"]}, headers=h)
    assert fix.status_code == 200
    assert fix.json()["category_id"] == target_cat["category_id"]
