from html import unescape
import re

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import settings
from app.db import AuditLog, SessionLocal


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(settings, 'admin_password_hash', PasswordHasher().hash('admin-test'))
    client.post('/login', data={'username': settings.admin_username, 'password': 'admin-test'})
    return client


@pytest.fixture
def synapse(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == 'GET':
            return httpx.Response(200, json={'users': [{'name': '@tester:example.invalid', 'displayname': 'Tester', 'deactivated': False}]})
        return httpx.Response(200, json={})
    monkeypatch.setattr('app.matrix.httpx.request', request)
    return calls


def test_reset_from_rendered_button(admin, synapse):
    response = admin.get('/users')
    action = re.search(r'action="([^"]+)"\s*>\s*<button[^>]*>Сбросить пароль', response.text).group(1)
    response = admin.post(unescape(action))
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    method, url, kwargs = synapse[-1]
    assert method == 'POST'
    assert url.endswith('/_synapse/admin/v1/reset_password/%40tester%3Aexample.invalid')
    password = kwargs['json']['new_password']
    assert kwargs['json'] == {'new_password': password, 'logout_devices': True}
    assert len(password) == 24
    assert password in unescape(response.text)
    assert not any(method == 'PUT' for method, _, _ in synapse)
    with SessionLocal() as db:
        log = db.query(AuditLog).filter_by(action='RESET_PASSWORD').one()
        assert log.target == '@tester:example.invalid'
        assert password not in log.details


def test_old_button_url_still_works(admin, synapse):
    assert admin.post('/users/@tester:example.invalid/reset_password').status_code == 200
    assert synapse[-1][0] == 'POST'


@pytest.mark.parametrize('suffix', ['password', 'reset_password'])
def test_reset_requires_admin(client, synapse, suffix):
    response = client.post('/users/@tester:example.invalid/' + suffix, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/login'
    assert not synapse


@pytest.mark.parametrize('status', [400, 403, 404, 500])
def test_failed_reset_does_not_report_success_or_expose_secrets(admin, monkeypatch, status):
    secret = 'temporary-test-password'
    monkeypatch.setattr('app.main.generate_password', lambda: secret)
    monkeypatch.setattr('app.matrix.httpx.request', lambda *args, **kwargs:
                        httpx.Response(status, json={'error': secret}))
    response = admin.post('/users/@tester:example.invalid/password')
    assert response.status_code == 502
    assert secret not in response.text
    assert 'Пароль сброшен' not in response.text
    assert 'Временный пароль' not in response.text
    with SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action='RESET_PASSWORD').count() == 0


def test_connection_failure_is_shown_without_password(admin, monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError('unavailable')
    monkeypatch.setattr('app.matrix.httpx.request', unavailable)
    response = admin.post('/users/@tester:example.invalid/password')
    assert response.status_code == 502
    assert 'Не удалось подтвердить' in response.text
    assert 'Временный пароль' not in response.text
