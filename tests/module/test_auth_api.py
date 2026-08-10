from __future__ import annotations


def _register(client, email="a@test.com", password="Abc12345", name="测试企业"):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "enterprise_name": name},
    )


def test_register_and_me(client):
    r = _register(client)
    assert r.status_code == 201
    data = r.json()
    assert data["access_token"] and data["refresh_token"]
    assert data["enterprise_id"] > 0

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "a@test.com"
    perms = set(body["permissions"])
    assert "quote.apply" in perms
    assert "review_provider.config" not in perms


def test_duplicate_email_conflict(client):
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409


def test_login_wrong_password(client):
    _register(client)
    r = client.post("/api/v1/auth/login", json={"email": "a@test.com", "password": "Wrong123"})
    assert r.status_code == 401


def test_password_policy_requires_digit_and_letter(client):
    r = _register(client, password="abcdefgh")  # 无数字
    assert r.status_code == 422
    r = _register(client, password="12345678")  # 无字母
    assert r.status_code == 422


def test_me_requires_auth(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_refresh_token_rotation(client):
    data = _register(client).json()
    first = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert first.status_code == 200
    assert first.json()["access_token"]
    # 旧 refresh token 已被吊销，不可复用
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert replay.status_code == 401
