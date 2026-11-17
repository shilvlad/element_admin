import time
from html import unescape
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.config import settings
from app.db import (SessionLocal, AuditLog, RegistrationRequest, RegistrationOperation,
                    RegistrationReservation, ManagedUser)
from app.main import app
from app.matrix import MatrixError
from app.security import encrypt_secret

UID = '@tester:example.invalid'
ROOM = '!room:example.invalid'


@pytest.fixture
def matrix(monkeypatch):
    m = Mock()
    m.get_user.return_value = {'name': UID, 'displayname': '<Tester>', 'admin': False,
                               'deactivated': False, 'threepids': []}
    m.list_users.return_value = {'users': [{'name': UID, 'last_seen_ts': int(time.time()*1000)},
                                         {'name': '@old:example.invalid', 'deactivated': True, 'last_seen_ts': 1}]}
    m.user_devices.return_value = {'devices': [{'device_id': 'ABC', 'display_name': '<Phone>',
                                               'last_seen_ip': '1.2.3.4', 'last_seen_ts': 1000}]}
    m.user_rooms.return_value = {'joined_rooms': [ROOM]}
    m.user_sessions.return_value = {'devices': {'ABC': {'sessions': [{'connections': [
        {'ip': '1.2.3.4', 'last_seen': 2000, 'user_agent': '<Client>'}]}]}}}
    m.list_rooms.return_value = {'rooms': [{'room_id': ROOM, 'name': '<Room>', 'joined_members': 1}],
                                'total_rooms': 2, 'next_batch': 'cursor/+next'}
    m.get_room.return_value = {'room_id': ROOM, 'name': '<Room>', 'encryption': 'm.megolm.v1.aes-sha2'}
    m.room_members.return_value = {'members': [UID]}
    m.room_media.return_value = {'local': ['mxc://example.invalid/one'], 'remote': ['mxc://remote/two']}
    m.media_info.side_effect = [{'media_info': {'media_length': 123}}, MatrixError('not cached', 404)]
    monkeypatch.setattr('app.main.MatrixClient', lambda: m)
    monkeypatch.setattr('app.admin_views.MatrixClient', lambda: m)
    monkeypatch.setattr('app.recovery.MatrixClient', lambda: m)
    return m


@pytest.fixture
def admin(client, matrix, monkeypatch):
    monkeypatch.setattr(settings, 'admin_password_hash', PasswordHasher().hash('admin-test'))
    client.post('/login', data={'username': settings.admin_username, 'password': 'admin-test'}, follow_redirects=False)
    return client


def req(status='CREATING', operation=None):
    with SessionLocal() as db:
        row = RegistrationRequest(username='tester', requester='user@example.com', status=status,
                                  password_enc=encrypt_secret('test-password'))
        db.add(row)
        db.flush()
        db.add(RegistrationReservation(username='tester', request_id=row.id))
        if operation is not None:
            db.add(RegistrationOperation(request_id=row.id, started_at=operation))
        db.commit()
        return row.id


def read_req(rid):
    with SessionLocal() as db:
        return db.get(RegistrationRequest, rid)


def test_user_card_contains_devices_rooms_and_audit(admin, matrix):
    with SessionLocal() as db:
        db.add(AuditLog(actor='admin', action='RESET_PASSWORD', target=UID))
        db.commit()
    response = admin.get('/users/' + quote(UID, safe='') + '/details')
    assert response.status_code == 200
    assert '&lt;Phone&gt;' in response.text and '&lt;Client&gt;' in response.text
    assert '1.2.3.4' in response.text and 'RESET_PASSWORD' in response.text
    assert ROOM in unescape(response.text)
    assert '01.01.1970' in response.text


def test_user_card_handles_partial_failure(admin, matrix):
    matrix.user_devices.side_effect = MatrixError('secret')
    response = admin.get('/users/' + UID + '/details')
    assert response.status_code == 200
    assert 'Не удалось получить устройства' in response.text
    assert 'secret' not in response.text


def test_dashboard_counts_synapse_and_local_operations(admin, matrix):
    req('PENDING')
    with SessionLocal() as db:
        db.add(AuditLog(actor='admin', action='APPROVE_ERROR', target='request:1'))
        db.commit()
    response = admin.get('/')
    assert response.status_code == 200
    assert response.context['available'] is True
    assert response.context['total'] == 2
    assert response.context['deactivated'] == 1
    assert response.context['seen'] == 1
    assert response.context['statuses']['PENDING'] == 1
    assert response.context['errors'] == 1


def test_dashboard_outage_does_not_fake_zero_users(admin, matrix):
    matrix.list_users.side_effect = MatrixError('token-leak')
    response = admin.get('/')
    assert response.status_code == 200
    assert response.context['total'] is None
    assert response.context['available'] is False
    assert 'token-leak' not in response.text


def test_rooms_list_details_and_invite(admin, matrix):
    response = admin.get('/rooms?q=Room&offset=next')
    assert response.status_code == 200
    matrix.list_rooms.assert_called_once_with(offset='next', search='Room')
    assert '&lt;Room&gt;' in response.text
    assert 'cursor%2F%2Bnext' in response.text
    response = admin.get('/rooms/' + quote(ROOM, safe=''))
    assert response.status_code == 200
    assert UID in unescape(response.text)
    response = admin.post('/rooms/' + ROOM + '/invite', data={'user_id': UID}, follow_redirects=False)
    assert response.status_code == 303
    matrix.invite_to_room.assert_called_once_with(ROOM, UID, settings.admin_username)
    with SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action='INVITE_USER').one().target == UID


def test_room_invite_failure_and_validation(admin, matrix):
    assert admin.post('/rooms/' + ROOM + '/invite', data={'user_id': 'bad'}).status_code == 400
    matrix.invite_to_room.side_effect = MatrixError('sensitive')
    response = admin.post('/rooms/' + ROOM + '/invite', data={'user_id': UID})
    assert response.status_code == 502
    assert 'sensitive' not in response.text


def test_media_reports_partial_sizes_honestly(admin, matrix):
    response = admin.get('/rooms/' + ROOM + '/media')
    assert response.status_code == 200
    assert response.context['size'] == 123
    assert response.context['known'] == 1
    assert response.context['total'] == 2
    assert 'Размер недоступен' in response.text
    assert 'не полный размер' in response.text


@pytest.mark.parametrize('path', ['/rooms', '/rooms/' + ROOM, '/rooms/' + ROOM + '/media',
                                  '/users/' + UID + '/details'])
def test_new_pages_require_login(client, matrix, path):
    assert client.get(path, follow_redirects=False).headers['location'] == '/login'
    matrix.get_user.assert_not_called()
    matrix.get_room.assert_not_called()


def test_recovery_existing_requires_confirmation_and_never_changes_remote_password(admin, matrix):
    rid = req()
    response = admin.post(f'/requests/{rid}/recover')
    assert response.status_code == 409
    assert read_req(rid).status == 'CREATING'
    response = admin.post(f'/requests/{rid}/recover', data={'confirm_existing': 'true'}, follow_redirects=False)
    assert response.status_code == 303
    row = read_req(rid)
    assert row.status == 'APPROVED' and row.password_enc is None
    matrix.create_user.assert_not_called()
    matrix.update_user.assert_not_called()
    with SessionLocal() as db:
        assert db.query(ManagedUser).one().user_id == UID
        assert db.query(AuditLog).filter_by(action='RECOVER_REGISTRATION').count() == 1


def test_recovery_absent_can_retry_but_outage_cannot_complete(admin, matrix):
    rid = req()
    matrix.get_user.side_effect = MatrixError('outage', 500)
    admin.post(f'/requests/{rid}/recover')
    assert read_req(rid).status == 'CREATING'
    assert read_req(rid).password_enc
    matrix.get_user.side_effect = MatrixError('absent', 404)
    admin.post(f'/requests/{rid}/recover')
    assert read_req(rid).status == 'ERROR'
    assert read_req(rid).password_enc
    assert 'Можно повторить' in read_req(rid).error_message


def test_recovery_will_not_interrupt_inflight_creation(admin, matrix):
    rid = req(operation=int(time.time()))
    assert admin.post(f'/requests/{rid}/recover', data={'confirm_existing': 'true'}).status_code == 409
    matrix.get_user.assert_not_called()
    assert read_req(rid).status == 'CREATING'


def test_recovery_after_lease_expired(admin, matrix):
    rid = req('RECOVERING', operation=int(time.time())-600)
    admin.post(f'/requests/{rid}/recover', data={'confirm_existing': 'true'})
    assert read_req(rid).status == 'APPROVED'


@pytest.mark.parametrize('profile,smtp_failure,delivered', [
    ({'threepids': [{'medium': 'email', 'address': 'user@example.com'}]}, False, True),
    ({'threepids': [{'medium': 'email', 'address': 'user@example.com'}]}, True, False),
    ({'threepids': []}, False, False),
    ({'threepids': [{'medium': 'email', 'address': 'bad\n@example.com'}]}, False, False),
])
def test_password_delivery_uses_profile_and_has_manual_fallback(admin, matrix, monkeypatch, profile, smtp_failure, delivered):
    matrix.get_user.return_value = profile
    sender = Mock(side_effect=RuntimeError('smtp-password') if smtp_failure else None)
    monkeypatch.setattr('app.main.send_password_notice', sender)
    monkeypatch.setattr('app.main.generate_password', lambda: 'new-test-password')
    response = admin.post('/users/' + UID + '/password', data={'email': 'attacker@example.com'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    if delivered:
        sender.assert_called_once_with('user@example.com', UID, 'new-test-password')
        assert 'new-test-password' not in response.text
    else:
        assert 'new-test-password' in response.text
    assert 'attacker@example.com' not in response.text
    assert 'smtp-password' not in response.text
    with SessionLocal() as db:
        assert all('new-test-password' not in log.details for log in db.query(AuditLog))


def test_recovery_does_not_claim_another_requests_username(admin, matrix):
    rid = req('ERROR')
    with SessionLocal() as db:
        db.get(RegistrationReservation, 'tester').request_id = rid + 1
        db.commit()
    assert admin.post(f'/requests/{rid}/recover', data={'confirm_existing': 'true'}).status_code == 409
    matrix.get_user.assert_not_called()


def test_recovery_never_recreates_record_without_saved_password(admin, matrix):
    rid = req('CREATED')
    with SessionLocal() as db:
        db.get(RegistrationRequest, rid).password_enc = None
        db.commit()
    matrix.get_user.side_effect = MatrixError('absent', 404)
    admin.post(f'/requests/{rid}/recover')
    row = read_req(rid)
    assert row.status == 'ERROR' and row.password_enc is None
    assert 'ручная проверка' in row.error_message
    matrix.create_user.assert_not_called()
