from __future__ import annotations

import io
import zipfile


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
    up = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", ("营业执照.txt", io.BytesIO("统一社会信用代码 91110000XXXX".encode()), "text/plain"))],
        headers=h,
    )
    item = up.json()["files"][0]
    assert item["auto_ingest"] is True
    assert item["facts_extracted"] == 1  # 上传即自动入库：按文件名抽取初始事实

    assets = client.get("/api/v1/enterprise/assets", headers=h)
    assert len(assets.json()) == 1
    asset_id = assets.json()[0]["asset_id"]
    assert assets.json()[0]["status"] == 1  # 上传后先保持“待分类”（分类中信号）
    assert client.get("/api/v1/enterprise/classification-status", headers=h).json()["pending"] is True

    detail = client.get(f"/api/v1/enterprise/assets/{asset_id}", headers=h)
    assert detail.json()["status"] == 1
    credit_facts = [f for f in detail.json()["facts"] if f["fact_key"] == "credit_code"]
    assert len(credit_facts) == 1

    # 手动再跑 ingest：幂等（同名事实不重复插入），分类结果一致
    ingest = client.post("/api/v1/enterprise/ingest", json={"asset_ids": [asset_id]}, headers=h)
    assert ingest.status_code == 202
    assert ingest.json()["classified"][0]["category"] == "证照"
    assert client.get("/api/v1/enterprise/classification-status", headers=h).json()["pending"] is False
    detail2 = client.get(f"/api/v1/enterprise/assets/{asset_id}", headers=h)
    assert detail2.json()["status"] == 2
    assert sum(1 for f in detail2.json()["facts"] if f["fact_key"] == "credit_code") == 1


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


def test_zip_upload_is_source_archive_and_never_pending(client):
    """discussion #55：源压缩包归入“源文件”且无需业务分类，不计入待分类数量。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("材料.txt", "包内材料内容")
    h = _headers(client)
    up = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", ("资料包.zip", buf.getvalue(), "application/zip"))],
        headers=h,
    )
    assert up.status_code == 200

    cats = client.get("/api/v1/enterprise/categories", headers=h).json()
    src_cat = next(c for c in cats if c["name"] == "源文件")
    assets = client.get("/api/v1/enterprise/assets", headers=h).json()
    zip_asset = next(a for a in assets if a["asset_type"] == "源文件")
    assert zip_asset["status"] == 4
    assert zip_asset["category_id"] == src_cat["category_id"]

    # 子文件是普通业务文件：status=1 待分类，pending 只计它
    sub = next(a for a in assets if a["asset_type"] != "源文件")
    assert sub["status"] == 1
    st = client.get("/api/v1/enterprise/classification-status", headers=h).json()
    assert st["pending"] is True
    assert st["pending_asset_count"] == 1

    # ingest 跳过源文件，源文件身份与状态保持不变
    r = client.post(
        "/api/v1/enterprise/ingest",
        json={"asset_ids": [zip_asset["asset_id"]]},
        headers=h,
    )
    assert r.status_code == 202
    assert r.json()["classified"][0]["source"] == "source_archive"
    detail = client.get(f"/api/v1/enterprise/assets/{zip_asset['asset_id']}", headers=h).json()
    assert detail["status"] == 4
    assert detail["asset_type"] == "源文件"
