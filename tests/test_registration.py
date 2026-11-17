from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
from argon2 import PasswordHasher
from conftest import CSRFClient

from app.config import settings
from app.db import AuditLog, RegistrationRequest, RegistrationReservation, SessionLocal
from app.main import app
from app.matrix import MatrixClient, MatrixError
from app.security import decrypt_secret

PASSWORD = "test1234"


@pytest.fixture
def matrix(monkeypatch):
    fake = Mock()
    fake.user_exists.return_value = False
    monkeypatch.setattr("app.main.MatrixClient", lambda: fake)
    monkeypatch.setattr("app.main.send_moderation_notice", lambda *args: None)
    return fake


def submit(client, **overrides):
    data = dict(username="tester", displayname="Tester", requester="user@example.com",
                password=PASSWORD, password_confirm=PASSWORD)
    data.update(overrides)
    return client.post("/request", data=data)


def login(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password_hash", PasswordHasher().hash("admin-test"))
    assert client.post("/login", data={"username": settings.admin_username,
                                      "password": "admin-test"}, follow_redirects=False).status_code == 303


def latest():
    with SessionLocal() as db:
        return db.query(RegistrationRequest).order_by(RegistrationRequest.id.desc()).first()


def test_chosen_password_is_encrypted_and_public_cannot_create_admin(client, matrix):
    response = submit(client, is_admin="true")
    assert response.status_code == 200
    req = latest()
    assert req.is_admin is False
    assert req.password_enc != PASSWORD
    assert decrypt_secret(req.password_enc) == PASSWORD
    assert PASSWORD not in response.text
    with SessionLocal() as db:
        assert all(PASSWORD not in log.details for log in db.query(AuditLog))


@pytest.mark.parametrize("fields,status", [
    ({"password_confirm": "different"}, 400),
    ({"password": "1234567", "password_confirm": "1234567"}, 400),
    ({"password": "x" * 129, "password_confirm": "x" * 129}, 400),
    ({"requester": "   "}, 400),
    ({"username": "bad/name"}, 400),
])
def test_invalid_request_creates_nothing(client, matrix, fields, status):
    response = submit(client, **fields)
    assert response.status_code == status
    assert latest() is None
    matrix.create_user.assert_not_called()
    assert PASSWORD not in response.text


def test_missing_password_rejected(client, matrix):
    assert client.post("/request", data={"username": "test", "requester": "test"}).status_code == 422
    assert latest() is None


def test_form_has_password_confirmation_and_configured_server(client):
    response = client.get("/request")
    assert 'name="password"' in response.text
    assert 'name="password_confirm"' in response.text
    assert settings.matrix_server_name in response.text
    assert "chat.iteko.su" not in response.text


def test_duplicate_and_existing_logins_rejected(client, matrix):
    assert submit(client).status_code == 200
    assert submit(client, username="@TESTER:ignored").status_code == 409
    matrix.user_exists.return_value = True
    assert submit(client, username="another").status_code == 409
    matrix.create_user.assert_not_called()


def test_synapse_lookup_failure_is_not_absence(client, matrix):
    matrix.user_exists.side_effect = MatrixError("HTTP 403")
    assert submit(client).status_code == 502
    assert latest() is None


def test_approval_passes_chosen_password_clears_secret_and_is_idempotent(client, matrix, monkeypatch):
    submit(client)
    rid = latest().id
    login(client, monkeypatch)
    assert client.post(f"/requests/{rid}/approve", follow_redirects=False).status_code == 303
    matrix.create_user.assert_called_once_with(f"@tester:{settings.matrix_server_name}", PASSWORD, "Tester", False, email="user@example.com")
    assert latest().status == "APPROVED"
    assert latest().password_enc is None
    client.post(f"/requests/{rid}/approve")
    assert matrix.create_user.call_count == 1


def test_invite_failure_can_retry_without_recreating_account(client, matrix, monkeypatch):
    submit(client)
    rid = latest().id
    login(client, monkeypatch)
    matrix.invite_to_room.side_effect = MatrixError(PASSWORD)
    response = client.post(f"/requests/{rid}/approve")
    assert latest().status == "APPROVED"
    assert latest().password_enc is None
    assert PASSWORD not in response.text
    assert "Повторить приглашение" in response.text
    matrix.invite_to_room.side_effect = None
    client.post(f"/requests/{rid}/invite")
    assert latest().error_message is None
    assert matrix.create_user.call_count == 1
    assert matrix.invite_to_room.call_count == 2


def test_creation_error_retains_secret_and_does_not_invite(client, matrix, monkeypatch):
    submit(client)
    rid = latest().id
    login(client, monkeypatch)
    matrix.create_user.side_effect = MatrixError(PASSWORD)
    response = client.post(f"/requests/{rid}/approve")
    assert latest().status == "ERROR"
    assert decrypt_secret(latest().password_enc) == PASSWORD
    assert PASSWORD not in response.text
    matrix.invite_to_room.assert_not_called()
    client.post(f"/requests/{rid}/reject")
    assert latest().password_enc is None
    assert latest().status == "REJECTED"
    with SessionLocal() as db:
        assert db.get(RegistrationReservation, "tester") is None
    assert submit(client).status_code == 200


def test_legacy_duplicate_cannot_be_approved(client, matrix, monkeypatch):
    submit(client)
    original = latest()
    with SessionLocal() as db:
        duplicate = RegistrationRequest(username="tester", requester="legacy", password_enc=original.password_enc)
        db.add(duplicate)
        db.commit()
        rid = duplicate.id
    login(client, monkeypatch)
    client.post(f"/requests/{rid}/approve")
    matrix.create_user.assert_not_called()
    assert "зарезервирован" in latest().error_message


def test_parallel_approval_creates_account_once(client, matrix, monkeypatch):
    submit(client)
    rid = latest().id
    login(client, monkeypatch)
    cookies = dict(client.cookies)
    entered, release = Event(), Event()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)

    matrix.create_user.side_effect = create
    def approve_once():
        with CSRFClient(app) as other:
            other.cookies.update(cookies)
            return other.post(f"/requests/{rid}/approve", follow_redirects=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve_once)
        try:
            assert entered.wait(5)
            assert approve_once().status_code == 303
        finally:
            release.set()
        assert first.result().status_code == 303
    assert matrix.create_user.call_count == 1


def test_unauthenticated_cannot_approve_or_invite(client, matrix):
    submit(client)
    rid = latest().id
    for action in ("approve", "invite", "reject"):
        response = client.post(f"/requests/{rid}/{action}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    matrix.create_user.assert_not_called()
    matrix.invite_to_room.assert_not_called()


@pytest.mark.parametrize("lookup_status,expected_puts", [(200, 0), (403, 0), (500, 0), (404, 1)])
def test_matrix_create_never_updates_known_users(monkeypatch, lookup_status, expected_puts):
    calls = []
    def request(method, url, **kwargs):
        calls.append(method)
        status = lookup_status if method == "GET" else 201
        return httpx.Response(status, json={"name": "@tester:example.invalid"})
    monkeypatch.setattr("app.matrix.httpx.request", request)
    matrix = MatrixClient()
    if expected_puts:
        matrix.create_user("@tester:example.invalid", PASSWORD)
    else:
        with pytest.raises(MatrixError):
            matrix.create_user("@tester:example.invalid", PASSWORD)
    assert calls.count("PUT") == expected_puts


def test_parallel_submissions_reserve_one_username(client, matrix):
    def send():
        with CSRFClient(app) as other:
            return submit(other).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert sorted(results) == [200, 409]
    with SessionLocal() as db:
        assert db.query(RegistrationRequest).count() == 1
        assert db.query(RegistrationReservation).count() == 1


def test_account_created_between_submission_and_approval_is_not_updated(client, matrix, monkeypatch):
    submit(client)
    rid = latest().id
    login(client, monkeypatch)
    real = MatrixClient()
    # Replace only the HTTP transport; exercise the real pre-create existence check.
    calls = []
    def request(method, url, **kwargs):
        calls.append(method)
        return httpx.Response(200, json={"name": f"@tester:{settings.matrix_server_name}"})
    monkeypatch.setattr("app.matrix.httpx.request", request)
    monkeypatch.setattr("app.main.MatrixClient", lambda: real)
    client.post(f"/requests/{rid}/approve")
    assert latest().status == "ERROR"
    assert calls == ["GET"]
