from unittest.mock import Mock

import httpx
import pytest

from app import updates
from app.matrix import MatrixClient, MatrixError
from test_registration import login


@pytest.fixture
def source(monkeypatch):
    updates._cache.clear()
    get = Mock(return_value=httpx.Response(200, request=httpx.Request('GET', updates.RELEASE_API),
                                         json={'tag_name': 'v1.100.0', 'prerelease': False, 'draft': False}))
    monkeypatch.setattr(updates.httpx, 'get', get)
    matrix = Mock()
    matrix.server_version.return_value = '1.99.0'
    monkeypatch.setattr(updates, 'MatrixClient', lambda: matrix)
    yield get, matrix
    updates._cache.clear()


@pytest.mark.parametrize('current,available', [('1.99.0', True), ('1.100.0rc1 (b=test, abc)', True),
                                             ('1.100.0', False), ('1.101.0', False)])
def test_numeric_version_comparison(source, current, available):
    source[1].server_version.return_value = current
    result = updates.synapse_update_status()
    assert result['update_available'] is available
    assert result['release_url'] == updates.RELEASE_PAGE + '/tag/v1.100.0'


def test_cache_expires_and_does_not_cache_installed_version(source, monkeypatch):
    clock = Mock(return_value=100)
    monkeypatch.setattr(updates.time, 'monotonic', clock)
    assert updates.synapse_update_status()['update_available'] is True
    source[1].server_version.return_value = '1.100.0'
    assert updates.synapse_update_status()['update_available'] is False
    source[0].assert_called_once()
    assert 'Authorization' not in source[0].call_args.kwargs['headers']
    clock.return_value = 3701
    updates.synapse_update_status()
    assert source[0].call_count == 2


def test_outage_preserves_last_success_without_claiming_current_status(source):
    first = updates.synapse_update_status()
    updates._cache['expires'] = 0
    source[0].side_effect = httpx.ConnectError('secret')
    result = updates.synapse_update_status()
    assert result['release_error'] is True
    assert result['latest'] == first['latest']
    assert result['checked_at'] == first['checked_at']
    assert result['update_available'] is None
    updates.synapse_update_status()
    assert source[0].call_count == 2


@pytest.mark.parametrize('payload', [{'tag_name': 'v1.101.0rc1'}, {'tag_name': 'bad'}, {},
                                     {'tag_name': 'v1.101.0', 'prerelease': True}])
def test_invalid_or_preview_release_cannot_announce_update(source, payload):
    source[0].return_value = httpx.Response(200, request=httpx.Request('GET', updates.RELEASE_API), json=payload)
    result = updates.synapse_update_status()
    assert result['release_error'] is True
    assert result['update_available'] is None


def test_current_version_unavailable(source):
    source[1].server_version.side_effect = MatrixError('token-secret')
    result = updates.synapse_update_status()
    assert result['current'] is None
    assert result['version_error'] is True
    assert result['update_available'] is None
    assert 'token-secret' not in str(result)


def test_dashboard_shows_update_and_requires_login(client, monkeypatch, source):
    check = Mock(side_effect=updates.synapse_update_status)
    monkeypatch.setattr('app.main.synapse_update_status', check)
    monkeypatch.setattr('app.main.MatrixClient', lambda: source[1])
    source[1].list_users.return_value = {'users': []}
    assert client.get('/', follow_redirects=False).status_code == 303
    check.assert_not_called()
    login(client, monkeypatch)
    response = client.get('/')
    assert response.status_code == 200
    assert 'Доступна новая версия Synapse' in response.text
    assert updates.RELEASE_PAGE + '/tag/v1.100.0' in response.text
    updates._cache['expires'] = 0
    source[0].side_effect = httpx.ConnectError('secret')
    response = client.get('/')
    assert 'Не удалось проверить обновления' in response.text
    assert 'Более новая стабильная версия не найдена' not in response.text
    assert 'secret' not in response.text


def test_server_version_endpoint(monkeypatch):
    request = Mock(return_value=httpx.Response(200, json={'server_version': '1.99.0'}))
    monkeypatch.setattr('httpx.request', request)
    assert MatrixClient().server_version() == '1.99.0'
    assert request.call_args.args[1].endswith('/_synapse/admin/v1/server_version')
