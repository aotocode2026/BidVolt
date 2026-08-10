from __future__ import annotations

import io
import zipfile


def _headers(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "f@test.com", "password": "Abc12345", "enterprise_name": "测试企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload(
    client,
    headers,
    content: bytes = "招标公告内容".encode("utf-8"),
    name="材料.txt",
    target="enterprise",
    project_id=None,
):
    data = {"target": target}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post(
        "/api/v1/files/upload",
        data=data,
        files=[("files", (name, io.BytesIO(content), "application/octet-stream"))],
        headers=headers,
    )


def test_upload_and_download(client):
    h = _headers(client)
    r = _upload(client, h)
    assert r.status_code == 200
    entry = r.json()["files"][0]
    assert entry["status"] == 3  # parsed
    file_id = entry["file_id"]

    info = client.get(f"/api/v1/files/{file_id}/info", headers=h)
    assert info.json()["name"] == "材料.txt"

    dl = client.get(f"/api/v1/files/{file_id}/download", headers=h)
    assert dl.content == "招标公告内容".encode("utf-8")

    blocks = client.get(f"/api/v1/files/{file_id}/blocks", headers=h)
    assert blocks.json()["total"] >= 1


def test_upload_magic_mismatch_fails(client):
    h = _headers(client)
    r = _upload(client, h, content=b"this is text", name="fake.pdf")
    assert r.status_code == 200
    assert "error" in r.json()["files"][0]


def test_upload_to_project_sets_processing(client):
    h = _headers(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]
    r = _upload(client, h, name="招标文件.txt", target="project", project_id=pid)
    assert r.status_code == 200
    assert r.json()["files"][0]["status"] == 3

    project = client.get(f"/api/v1/projects/{pid}", headers=h).json()
    assert project["status"] == 2  # processing
    mats = client.get(f"/api/v1/files/projects/{pid}/materials", headers=h)
    assert len(mats.json()) == 1


def test_archive_rejects_traversal_zip(client):
    h = _headers(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    r = _upload(client, h, content=buf.getvalue(), name="bad.zip")
    assert r.status_code == 200
    file_id = r.json()["files"][0]["file_id"]

    ar = client.post("/api/v1/files/archive", json={"archive_file_id": file_id, "target": "enterprise"}, headers=h)
    assert ar.status_code == 422


def test_archive_normal_zip_imports_files(client):
    h = _headers(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "内容A")
        zf.writestr("b.txt", "内容B")
    r = _upload(client, h, content=buf.getvalue(), name="pkg.zip")
    file_id = r.json()["files"][0]["file_id"]

    ar = client.post("/api/v1/files/archive", json={"archive_file_id": file_id, "target": "enterprise"}, headers=h)
    assert ar.status_code == 200
    assert len(ar.json()["result"]["imported"]) == 2
