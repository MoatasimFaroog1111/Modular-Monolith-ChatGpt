def test_identity_requires_authentication_and_returns_only_caller(client):
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"X-User": "missing"}).status_code == 401
    response = client.get("/api/v1/me", headers={"X-User": "fin1"})
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "fin1"
    assert data["role"] == "finance"
    assert data["id"] > 0
    assert set(data) == {"id", "username", "full_name", "role"}
