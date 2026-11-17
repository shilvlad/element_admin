import httpx
import pytest
from argon2 import PasswordHasher

from app.config import settings
from app.matrix import MatrixClient


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(settings, 'admin_password_hash', PasswordHasher().hash('test-password'))
    client.post('/login', data={'username': settings.admin_username, 'password': 'test-password'}, follow_redirects=False)
    return client


@pytest.mark.parametrize('query', ['', '   '])
def test_rooms_without_search_omit_empty_parameter(admin, monkeypatch, query):
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs['params'])
        if kwargs['params'].get('search_term') == '':
            return httpx.Response(400, json={'error': 'search_term cannot be an empty string'})
        return httpx.Response(200, json={'rooms': [{'room_id': '!room:example.com', 'name': 'Общая', 'joined_members': 1}], 'total_rooms': 1})
    monkeypatch.setattr('app.matrix.httpx.request', request)
    response = admin.get('/rooms', params={'q': query})
    assert response.status_code == 200
    assert calls == [{'from': '0', 'limit': 50}]
    assert 'Общая' in response.text
    assert 'text/html' in response.headers['content-type']
    assert response.encoding == 'utf-8'


def test_search_and_offset_are_preserved(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs['params'])
        return httpx.Response(200, json={'rooms': [], 'total_rooms': 0})
    monkeypatch.setattr('app.matrix.httpx.request', request)
    MatrixClient().list_rooms(offset='50', search=' Общая ')
    assert calls == [{'from': '50', 'limit': 50, 'search_term': 'Общая'}]


@pytest.mark.parametrize('status', [400, 403, 500])
def test_upstream_error_is_readable_html(admin, monkeypatch, status):
    monkeypatch.setattr('app.matrix.httpx.request', lambda *args, **kwargs:
                        httpx.Response(status, json={'error': 'sensitive-token'}))
    response = admin.get('/rooms')
    assert response.status_code == 502
    assert 'text/html' in response.headers['content-type']
    assert 'Повторить без поиска' in response.text
    assert 'sensitive-token' not in response.text
    assert 'Найдено:' not in response.text
