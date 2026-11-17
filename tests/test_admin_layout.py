from unittest.mock import Mock

from test_registration import login


def test_public_request_keeps_original_styles(client, monkeypatch):
    for authenticated in (False, True):
        if authenticated:
            login(client, monkeypatch)
        response = client.get('/request')
        assert response.status_code == 200
        assert '/static/admin.css' not in response.text
        assert 'class="admin-layout"' not in response.text
        assert 'class="mobile-nav"' not in response.text


def test_admin_navigation_and_user_actions(client, monkeypatch):
    login(client, monkeypatch)
    matrix = Mock()
    matrix.list_users.return_value = {'users': [{'name': '@test:example.invalid', 'deactivated': False}]}
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    response = client.get('/users')
    assert response.status_code == 200
    assert '/static/admin.css' in response.text
    assert 'aria-label="Мобильное меню"' in response.text
    assert 'href="/users" aria-current="page"' in response.text
    assert '<details><summary' in response.text
    assert '/invite-global' in response.text
    assert '/password' in response.text
    assert '/deactivate' in response.text
    assert 'name="csrf_token"' in response.text
