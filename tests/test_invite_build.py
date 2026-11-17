from unittest.mock import Mock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db import AuditLog, SessionLocal
from app.config import settings
from app.main import app
from app.matrix import MatrixError
from test_registration import login

UID = "@tester:example.invalid"


@pytest.fixture
def matrix(monkeypatch):
    matrix = Mock()
    matrix.get_user.return_value = {"deactivated": False}
    matrix.user_rooms.return_value = {"joined_rooms": []}
    monkeypatch.setattr("app.main.MatrixClient", lambda: matrix)
    return matrix


def test_invite_requires_login_and_csrf(client, matrix, monkeypatch):
    assert client.post("/users/" + UID + "/invite-global", follow_redirects=False).status_code == 303
    matrix.get_user.assert_not_called()
    login(client, monkeypatch)
    with TestClient(app) as raw:
        raw.cookies.update(client.cookies)
        assert raw.post("/users/" + UID + "/invite-global").status_code == 403
    matrix.invite_to_room.assert_not_called()


def test_invite_active_user_and_audit(client, matrix, monkeypatch):
    login(client, monkeypatch)
    response = client.post("/users/" + UID + "/invite-global")
    assert response.status_code == 200
    assert "Приглашение в Global отправлено" in response.text
    matrix.invite_to_room.assert_called_once_with(settings.global_room_id, UID, settings.admin_username)
    with SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action="INVITE_GLOBAL", target=UID).count() == 1


def test_already_joined_user_is_not_invited_again(client, matrix, monkeypatch):
    login(client, monkeypatch)
    matrix.user_rooms.return_value = {"joined_rooms": [settings.global_room_id]}
    response = client.post("/users/" + UID + "/invite-global")
    assert response.status_code == 200
    assert "уже состоит" in response.text
    matrix.invite_to_room.assert_not_called()


def test_deactivated_user_is_not_invited(client, matrix, monkeypatch):
    login(client, monkeypatch)
    matrix.get_user.return_value = {"deactivated": True}
    assert client.post("/users/" + UID + "/invite-global").status_code == 400
    matrix.invite_to_room.assert_not_called()


@pytest.mark.parametrize("method", ["get_user", "user_rooms", "invite_to_room"])
def test_invite_error_is_safe(client, matrix, monkeypatch, method):
    login(client, monkeypatch)
    getattr(matrix, method).side_effect = MatrixError("private-token")
    response = client.post("/users/" + UID + "/invite-global")
    assert response.status_code == 502
    assert "private-token" not in response.text
    with SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action="INVITE_GLOBAL_ERROR").count() == 1


def test_build_page_and_badge_are_admin_only(client, monkeypatch):
    assert client.get("/about", follow_redirects=False).status_code == 303
    assert 'class="build-badge"' not in client.get("/login").text
    login(client, monkeypatch)
    monkeypatch.setenv("BUILD_COMMIT", "abcdef0123456789")
    monkeypatch.setenv("BUILD_DATE", "2026-11-18T00:00:00Z")
    response = client.get("/about")
    assert response.status_code == 200
    from app.build import VERSION
    assert VERSION in response.text
    assert "abcdef0123456789" in response.text
    assert "abcdef01" in response.text
    assert "2026-11-18T00:00:00Z" in response.text
    assert "Светлая админка" in response.text


def test_unknown_build_is_honest(client, monkeypatch):
    login(client, monkeypatch)
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.delenv("BUILD_DATE", raising=False)
    response = client.get("/about")
    assert "не указан" in response.text


def test_users_show_invite_only_for_active_accounts(client, matrix, monkeypatch):
    login(client, monkeypatch)
    matrix.list_users.return_value = {"users": [
        {"name": UID, "deactivated": False},
        {"name": "@old:example.invalid", "deactivated": True}]}
    response = client.get("/users?status=all")
    assert response.status_code == 200
    assert "/users/" + quote(UID, safe="") + "/invite-global" in response.text
    assert "/users/" + quote("@old:example.invalid", safe="") + "/invite-global" not in response.text
