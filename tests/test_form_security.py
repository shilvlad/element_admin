import re

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, RegistrationRequest
from app.main import app


def token(client):
    response = client.get('/login')
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_missing_wrong_and_other_session_csrf_rejected():
    with TestClient(app) as one, TestClient(app) as other:
        own = token(one)
        foreign = token(other)
        for value in (None, 'wrong', foreign):
            headers = {'X-CSRF-Token': value} if value else {}
            assert one.post('/login', data={'username': 'x', 'password': 'y'}, headers=headers).status_code == 403
        assert one.post('/login', data={'username': 'x', 'password': 'y', 'csrf_token': 'токен'}).status_code == 403
        assert one.post('/login', data={'username': 'x', 'password': 'y', 'csrf_token': own}).status_code == 401


def test_get_logout_does_not_change_session(client):
    assert client.get('/logout').status_code == 405


def test_login_rate_limit_and_expiration(monkeypatch):
    monkeypatch.setattr(settings, 'login_attempts', 2)
    monkeypatch.setattr('app.guards.time.time', lambda: 120)
    with TestClient(app) as client:
        csrf = token(client)
        for _ in range(2):
            assert client.post('/login', data={'username': 'no', 'password': 'bad'},
                               headers={'X-CSRF-Token': csrf}).status_code == 401
        response = client.post('/login', data={'username': 'no', 'password': 'bad'},
                               headers={'X-CSRF-Token': csrf, 'X-Forwarded-For': 'new-ip'})
        assert response.status_code == 429
        assert int(response.headers['retry-after']) > 0
        monkeypatch.setattr('app.guards.time.time', lambda: 181)
        assert client.post('/login', data={'username': 'no', 'password': 'bad'},
                           headers={'X-CSRF-Token': csrf}).status_code == 401


def test_registration_rate_limit_precedes_synapse(client, monkeypatch):
    monkeypatch.setattr(settings, 'registration_attempts', 1)
    exists = []
    monkeypatch.setattr('app.main.MatrixClient.user_exists', lambda *args: exists.append(1) or False)
    monkeypatch.setattr('app.main.send_moderation_notice', lambda *args: None)
    data = {'username': 'tester', 'requester': 'user@example.com',
            'password': 'test1234', 'password_confirm': 'test1234'}
    assert client.post('/request', data=data).status_code == 200
    assert client.post('/request', data=data).status_code == 429
    assert exists == [1]


def test_email_required_and_attached_to_synapse(client, monkeypatch):
    monkeypatch.setattr('app.main.MatrixClient.user_exists', lambda *args: False)
    monkeypatch.setattr('app.main.send_moderation_notice', lambda *args: None)
    data = {'username': 'tester', 'requester': 'Иван Иванов',
            'password': 'test1234', 'password_confirm': 'test1234'}
    assert client.post('/request', data=data).status_code == 400
    with SessionLocal() as db:
        assert db.query(RegistrationRequest).count() == 0
    data['requester'] = 'user@example.com'
    assert client.post('/request', data=data).status_code == 200
