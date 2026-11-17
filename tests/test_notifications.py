import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import AuditLog, NotificationRoom, PanelSetting, RegistrationRequest, SessionLocal
from app.main import app
from app.matrix import MatrixClient, MatrixError
from app.notifications import SETTING_KEY, normalize_recipients, notify_registration
from test_registration import login, submit


@pytest.fixture
def matrix(monkeypatch):
    fake = Mock()
    fake.whoami.return_value = f"@Administrator:{settings.matrix_server_name}"
    fake.get_user.return_value = {"deactivated": False}
    fake.create_notification_room.return_value = "!notices:server"
    monkeypatch.setattr("app.notifications.MatrixClient", lambda: fake)
    return fake


def configure(recipients, enabled=True):
    with SessionLocal() as db:
        db.merge(PanelSetting(key=SETTING_KEY, value=json.dumps(dict(enabled=enabled, recipients=recipients))))
        db.commit()


def application():
    with SessionLocal() as db:
        req = RegistrationRequest(username="newuser", requester="private@example.com", password_enc="encrypted-secret")
        db.add(req)
        db.commit()
        return req.id


def test_settings_auth_and_csrf(client, monkeypatch, matrix):
    assert client.get('/settings', follow_redirects=False).status_code == 303
    assert client.post('/settings', data={}, follow_redirects=False).status_code == 303
    login(client, monkeypatch)
    with TestClient(app) as raw:
        raw.cookies.update(client.cookies)
        assert raw.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'}).status_code == 403
    assert matrix.whoami.call_count == 0


def test_settings_persist_and_show_existing_sender(client, monkeypatch, matrix):
    login(client, monkeypatch)
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya, ilya\nother'})
    assert response.status_code == 200
    assert matrix.whoami.return_value in response.text
    with SessionLocal() as db:
        values = json.loads(db.get(PanelSetting, SETTING_KEY).value)
    assert values == dict(enabled=True, recipients=[f'@ilya:{settings.matrix_server_name}', f'@other:{settings.matrix_server_name}'])
    matrix.create_notification_room.assert_not_called()
    matrix.send_notification.assert_not_called()


@pytest.mark.parametrize('value', ['@someone:other.example', '@:bad', 'bad!', 'a'*256])
def test_invalid_recipients(value):
    with pytest.raises(ValueError):
        normalize_recipients(value)


def test_invalid_settings_preserve_old_values(client, monkeypatch, matrix):
    login(client, monkeypatch)
    configure(['@old:server'])
    matrix.get_user.return_value = {'deactivated': True}
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 400
    with SessionLocal() as db:
        assert json.loads(db.get(PanelSetting, SETTING_KEY).value)['recipients'] == ['@old:server']
    matrix.whoami.side_effect = MatrixError('secret-token')
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 502
    assert 'secret-token' not in response.text
    # Disabling notifications must work even when Matrix is unavailable.
    assert client.post('/settings', data={'recipients': 'ilya'}, follow_redirects=False).status_code == 303


def test_reject_self_and_empty_list(client, monkeypatch, matrix):
    login(client, monkeypatch)
    for recipients in ['', matrix.whoami.return_value]:
        assert client.post('/settings', data={'enabled': 'true', 'recipients': recipients}).status_code == 400


def test_disabled_does_not_contact_matrix(matrix):
    notify_registration(application())
    matrix.whoami.assert_not_called()


def test_delivery_reuses_room_and_excludes_secrets(matrix):
    configure(['@ilya:server'])
    first, second = application(), application()
    notify_registration(first)
    notify_registration(second)
    matrix.create_notification_room.assert_called_once_with('@ilya:server')
    calls = matrix.send_notification.call_args_list
    assert len(calls) == 2
    assert calls[0].args[1] != calls[1].args[1]
    body = calls[0].args[2]
    assert f'/requests#request-{first}' in body and 'newuser' in body
    assert 'encrypted-secret' not in body and 'private@example.com' not in body
    with SessionLocal() as db:
        assert db.get(NotificationRoom, (matrix.whoami.return_value, '@ilya:server')) is not None


def test_one_failed_recipient_does_not_block_others(matrix):
    configure(['@broken:server', '@ilya:server'])
    matrix.create_notification_room.side_effect = [MatrixError('secret-token'), '!ok:server']
    notify_registration(application())
    matrix.send_notification.assert_called_once()
    with SessionLocal() as db:
        logs = db.query(AuditLog).all()
        assert [log.action for log in logs] == ['MATRIX_NOTICE_ERROR', 'MATRIX_NOTICE_SENT']
        assert all('secret-token' not in log.details for log in logs)


def test_application_saved_if_notification_fails(client, monkeypatch, matrix):
    configure(['@ilya:server'])
    fake = Mock()
    fake.user_exists.return_value = False
    monkeypatch.setattr('app.main.MatrixClient', lambda: fake)
    monkeypatch.setattr('app.main.send_moderation_notice', lambda *args: None)
    matrix.whoami.side_effect = MatrixError('secret-token')
    assert submit(client).status_code == 200
    with SessionLocal() as db:
        assert db.query(RegistrationRequest).one().status == 'PENDING'
        assert db.query(AuditLog).filter_by(action='MATRIX_NOTICE_ERROR').count() == 1


def test_matrix_transport_uses_configured_token(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        data = {'user_id': '@Administrator:server', 'room_id': '!room:server', 'event_id': '$event'}
        return httpx.Response(200, json=data)
    monkeypatch.setattr('httpx.request', request)
    matrix = MatrixClient()
    assert matrix.whoami() == '@Administrator:server'
    assert matrix.create_notification_room('@ilya:server') == '!room:server'
    matrix.send_notification('!room:server', 'transaction', 'test')
    assert calls[0][1].endswith('/account/whoami')
    assert calls[1][2]['json']['invite'] == ['@ilya:server']
    assert calls[1][2]['json']['preset'] == 'private_chat'
    assert calls[1][2]['json']['power_level_content_override']['invite'] == 100
    assert calls[2][0] == 'PUT'
    assert calls[2][1].endswith('/rooms/%21room%3Aserver/send/m.room.message/transaction')
    assert calls[2][2]['json'] == {'msgtype': 'm.text', 'body': 'test'}
    assert all(call[2]['headers']['Authorization'] == f'Bearer {settings.matrix_admin_token}' for call in calls)


def test_settings_allow_client_token_without_admin_api(client, monkeypatch, matrix):
    login(client, monkeypatch)
    matrix.get_user.side_effect = MatrixError('server admin required', 403)
    matrix.get_profile.return_value = {'displayname': 'Ilya'}
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 200
    matrix.get_profile.assert_called_once_with(f'@ilya:{settings.matrix_server_name}')
    with SessionLocal() as db:
        assert json.loads(db.get(PanelSetting, SETTING_KEY).value)['enabled'] is True


@pytest.mark.parametrize('status', [401, 500, None])
def test_recipient_error_identifies_failed_check(client, monkeypatch, matrix, status):
    login(client, monkeypatch)
    matrix.get_user.side_effect = MatrixError('secret-token', status)
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 502
    assert f'получателя @ilya:{settings.matrix_server_name}' in response.text
    assert (f'HTTP {status}' if status else 'нет соединения') in response.text
    assert 'secret-token' not in response.text
    matrix.get_profile.assert_not_called()


@pytest.mark.parametrize('profile_status', [None, 404, 403])
def test_profile_fallback_failure_does_not_save(client, monkeypatch, matrix, profile_status):
    login(client, monkeypatch)
    matrix.get_user.side_effect = MatrixError('forbidden', 403)
    matrix.get_profile.side_effect = MatrixError('private-response', profile_status)
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == (400 if profile_status == 404 else 502)
    assert 'private-response' not in response.text
    with SessionLocal() as db:
        assert db.get(PanelSetting, SETTING_KEY) is None


def test_missing_user_does_not_fall_back(client, monkeypatch, matrix):
    login(client, monkeypatch)
    matrix.get_user.side_effect = MatrixError('missing', 404)
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 400
    assert 'не найден' in response.text
    matrix.get_profile.assert_not_called()


def test_sender_failure_is_reported_once(client, monkeypatch, matrix):
    login(client, monkeypatch)
    matrix.whoami.side_effect = MatrixError('secret-token', 401)
    response = client.post('/settings', data={'enabled': 'true', 'recipients': 'ilya'})
    assert response.status_code == 502
    assert 'отправителя: HTTP 401' in response.text
    assert 'secret-token' not in response.text
    matrix.whoami.assert_called_once()
    matrix.get_user.assert_not_called()


def test_profile_request_encoding(monkeypatch):
    request = Mock(return_value=httpx.Response(200, json={}))
    monkeypatch.setattr('httpx.request', request)
    assert MatrixClient().get_profile('@ilya:server') == {}
    assert request.call_args.args[1].endswith('/_matrix/client/v3/profile/%40ilya%3Aserver')
