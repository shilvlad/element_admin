from unittest.mock import Mock
from html import unescape

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import settings
from app.matrix import MatrixClient, MatrixError


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(settings, 'admin_password_hash', PasswordHasher().hash('admin-test'))
    client.post('/login', data={'username': settings.admin_username, 'password': 'admin-test'})
    return client


@pytest.fixture
def matrix(monkeypatch):
    matrix = Mock()
    matrix.list_users.return_value = {'users': [
        {'name': '@active:example.invalid', 'displayname': 'Иван', 'deactivated': False},
        {'name': '@disabled:example.invalid', 'displayname': 'Иван', 'deactivated': True},
        {'name': '@other:example.invalid', 'displayname': None, 'deactivated': True},
    ]}
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    return matrix


@pytest.mark.parametrize('status,expected,include', [
    ('active', ['@active:example.invalid'], False),
    ('deactivated', ['@disabled:example.invalid'], True),
    ('all', ['@active:example.invalid', '@disabled:example.invalid'], True),
])
def test_status_and_search(admin, matrix, status, expected, include):
    response = admin.get('/users', params={'status': status, 'q': ' иВаН '})
    assert response.status_code == 200
    assert [u['name'] for u in response.context['users']] == expected
    matrix.list_users.assert_called_once_with(include_deactivated=include)
    assert f'value="{status}" selected' in response.text


def test_actions_match_status(admin, matrix):
    response = admin.get('/users?status=all')
    assert '/users/@active:example.invalid/password' in response.text
    assert '/users/@active:example.invalid/deactivate' in response.text
    assert '/users/@active:example.invalid/activate' not in response.text
    assert '/users/@disabled:example.invalid/activate' in response.text
    assert '/users/@disabled:example.invalid/password' not in response.text
    assert '/reset_password' not in response.text
    assert 'Отключен' in response.text and 'Активен' in response.text


def test_ui_pagination_preserves_filters(admin, matrix):
    matrix.list_users.return_value = {'users': [
        {'name': f'@user{i}:example.invalid', 'deactivated': True} for i in range(101)
    ]}
    response = admin.get('/users?status=deactivated&q=user&page=2')
    assert len(response.context['users']) == 50
    assert response.context['users'][0]['name'] == '@user50:example.invalid'
    assert response.context['total'] == 101
    assert response.context['pages'] == 3
    assert 'status=deactivated&amp;q=user&amp;page=3' in response.text
    assert 'status=deactivated&amp;q=user&amp;page=1' in response.text
    response = admin.get('/users?status=deactivated&q=user&page=999')
    assert response.context['page'] == 3
    assert len(response.context['users']) == 1


def test_empty_search_and_failure(admin, matrix):
    response = admin.get('/users?q=absent')
    assert response.status_code == 200
    assert 'Пользователи не найдены' in response.text
    matrix.list_users.side_effect = MatrixError('sensitive upstream body')
    response = admin.get('/users?status=deactivated')
    assert response.status_code == 502
    assert 'Не удалось получить' in response.text
    assert 'sensitive upstream body' not in response.text


@pytest.mark.parametrize('query', ['status=invalid', 'page=0', 'page=-1'])
def test_invalid_filters(admin, matrix, query):
    assert admin.get('/users?' + query).status_code == 422
    matrix.list_users.assert_not_called()


def test_users_requires_login(client, matrix):
    assert client.get('/users', follow_redirects=False).headers['location'] == '/login'
    assert client.post('/users/@disabled:example.invalid/activate', follow_redirects=False).headers['location'] == '/login'
    matrix.list_users.assert_not_called()
    matrix.update_user.assert_not_called()


def test_activation_sets_new_password(admin, matrix):
    response = admin.post('/users/@disabled:example.invalid/activate')
    assert response.status_code == 200
    args, kwargs = matrix.update_user.call_args
    assert args == ('@disabled:example.invalid',)
    assert kwargs['deactivated'] is False
    assert len(kwargs['password']) >= 8
    assert kwargs['password'] in unescape(response.text)
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('include', [False, True])
def test_matrix_fetches_all_pages(monkeypatch, include):
    calls = []
    def request(method, url, **kwargs):
        calls.append(dict(kwargs['params']))
        if len(calls) == 1:
            return httpx.Response(200, json={'users': [{'name': f'user{i}'} for i in range(1000)],
                                            'next_token': 'opaque/+cursor', 'total': 1001})
        return httpx.Response(200, json={'users': [{'name': 'last', 'deactivated': True}], 'total': 1001})
    monkeypatch.setattr('app.matrix.httpx.request', request)
    result = MatrixClient().list_users(include_deactivated=include)
    assert len(result['users']) == 1001
    assert result['users'][-1]['name'] == 'last'
    assert calls == [
        {'limit': 1000, 'deactivated': str(include).lower()},
        {'limit': 1000, 'deactivated': str(include).lower(), 'from': 'opaque/+cursor'},
    ]


def test_repeated_cursor_fails_instead_of_looping(monkeypatch):
    monkeypatch.setattr('app.matrix.httpx.request', lambda *args, **kwargs:
                        httpx.Response(200, json={'users': [], 'next_token': 'same'}))
    with pytest.raises(MatrixError, match='повторный токен'):
        MatrixClient().list_users()


def test_activation_failure_hides_upstream_body(admin, matrix):
    matrix.update_user.side_effect = MatrixError('sensitive password')
    response = admin.post('/users/@disabled:example.invalid/activate')
    assert response.status_code == 502
    assert 'sensitive password' not in response.text
