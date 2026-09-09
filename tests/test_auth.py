from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


client = TestClient(app)


def test_admin_login_success(monkeypatch):
    password = "test-password"
    password_hash = PasswordHasher().hash(password)

    monkeypatch.setattr(settings, "admin_username", "test-admin")
    monkeypatch.setattr(settings, "admin_password_hash", password_hash)

    response = client.post(
        "/login",
        data={
            "username": "test-admin",
            "password": password,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    response = client.get("/")

    assert response.status_code == 200


def test_admin_login_wrong_password(monkeypatch):
    password_hash = PasswordHasher().hash("correct-password")

    monkeypatch.setattr(settings, "admin_username", "test-admin")
    monkeypatch.setattr(settings, "admin_password_hash", password_hash)

    response = client.post(
        "/login",
        data={
            "username": "test-admin",
            "password": "wrong-password",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Неверный логин или пароль" in response.text