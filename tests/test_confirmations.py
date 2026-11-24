from unittest.mock import Mock

import pytest

from app.db import SessionLocal, RegistrationRequest
from app.security import encrypt_secret
from test_registration import login


@pytest.mark.parametrize('action', ['password', 'deactivate'])
def test_user_action_waits_for_confirmation(client, monkeypatch, action):
    login(client, monkeypatch)
    matrix = Mock()
    matrix.get_user.return_value = {'threepids': []}
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    url = '/users/@test:example.invalid/' + action
    response = client.post(url, data={'confirmed': 'false'})
    assert response.status_code == 200
    assert 'Подтвердить' in response.text and 'Отмена' in response.text
    assert '@test:example.invalid' in response.text
    matrix.reset_password.assert_not_called()
    matrix.update_user.assert_not_called()
    response = client.post(url, data={'confirmed': 'true'}, follow_redirects=False)
    if action == 'password':
        matrix.reset_password.assert_called_once()
    else:
        matrix.update_user.assert_called_once()


def test_admin_grant_requires_confirmation_and_preserves_selected_role(client, monkeypatch):
    login(client, monkeypatch)
    matrix = Mock()
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    with SessionLocal() as db:
        row = RegistrationRequest(username='admin_test', requester='user@example.com',
                                  password_enc=encrypt_secret('password123'))
        db.add(row)
        db.commit()
        rid = row.id
    url = f'/requests/{rid}/approve'
    response = client.post(url, data={'is_admin': 'true', 'confirmed': 'false'})
    assert response.status_code == 200 and 'Создать администратора Matrix' in response.text
    assert 'name="is_admin" value="true"' in response.text
    matrix.create_user.assert_not_called()
    with SessionLocal() as db:
        assert db.get(RegistrationRequest, rid).status == 'PENDING'
    client.post(url, data={'is_admin': 'true', 'confirmed': 'true'}, follow_redirects=False)
    assert matrix.create_user.call_args.args[3] is True
