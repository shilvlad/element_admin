from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import time

import pytest

from app.db import (SessionLocal, RegistrationRequest, RegistrationReservation,
                    RegistrationOperation, PanelSetting, AuditLog)
from app.expiry import expire_requests, TTL_KEY
from test_registration import login, submit


def make(status='PENDING', hours=80, secret='encrypted-secret', operation=False):
    with SessionLocal() as db:
        row = RegistrationRequest(username='expired', requester='user@example.com', status=status,
            password_enc=secret, created_at=datetime.now(timezone.utc)-timedelta(hours=hours))
        db.add(row)
        db.flush()
        db.add(RegistrationReservation(username=row.username, request_id=row.id))
        if operation:
            db.add(RegistrationOperation(request_id=row.id, started_at=int(time.time())))
        db.commit()
        return row.id


def test_pending_expiry_clears_secret_releases_name_and_audits_once():
    rid = make()
    expire_requests()
    expire_requests()
    with SessionLocal() as db:
        row = db.get(RegistrationRequest, rid)
        assert row.status == 'EXPIRED' and row.password_enc is None
        assert db.get(RegistrationReservation, 'expired') is None
        assert db.query(AuditLog).filter_by(action='EXPIRE_REGISTRATION').count() == 1


@pytest.mark.parametrize('status', ['CREATING', 'CREATED', 'RECOVERING', 'APPROVED'])
def test_inflight_and_completed_accounts_are_not_expired(status):
    rid = make(status, operation=True)
    expire_requests()
    with SessionLocal() as db:
        row = db.get(RegistrationRequest, rid)
        assert row.status == status and row.password_enc == 'encrypted-secret'
        assert db.get(RegistrationReservation, 'expired') is not None


def test_error_loses_secret_but_keeps_reservation():
    rid = make('ERROR')
    expire_requests()
    with SessionLocal() as db:
        row = db.get(RegistrationRequest, rid)
        assert row.status == 'ERROR' and row.password_enc is None
        assert db.get(RegistrationReservation, 'expired').request_id == rid


def test_settings_expire_existing_requests_and_validate_range(client, monkeypatch):
    rid = make(hours=2)
    login(client, monkeypatch)
    assert client.post('/settings/registration', data={'hours': 0}).status_code == 400
    assert client.post('/settings/registration', data={'hours': 721}).status_code == 400
    assert client.post('/settings/registration', data={'hours': 1}, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.get(PanelSetting, TTL_KEY).value == '1'
        assert db.get(RegistrationRequest, rid).status == 'EXPIRED'


def test_expired_request_cannot_be_approved_and_can_be_resubmitted(client, monkeypatch):
    rid = make()
    matrix = Mock()
    matrix.user_exists.return_value = False
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    monkeypatch.setattr('app.main.send_moderation_notice', lambda *args: None)
    login(client, monkeypatch)
    client.post(f'/requests/{rid}/approve', follow_redirects=False)
    matrix.create_user.assert_not_called()
    assert submit(client, username='expired').status_code == 200
    with SessionLocal() as db:
        assert db.query(RegistrationRequest).filter_by(username='expired', status='PENDING').count() == 1


def test_error_without_secret_cannot_retry_approval(client, monkeypatch):
    rid = make('ERROR')
    matrix = Mock()
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    login(client, monkeypatch)
    assert client.post(f'/requests/{rid}/approve').status_code == 409
    matrix.create_user.assert_not_called()


def test_recent_request_is_not_expired():
    rid = make(hours=1)
    expire_requests()
    with SessionLocal() as db:
        assert db.get(RegistrationRequest, rid).status == 'PENDING'


def test_expiry_settings_need_auth_and_csrf(client, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    assert client.post('/settings/registration', data={'hours': 1}, follow_redirects=False).status_code == 303
    login(client, monkeypatch)
    with TestClient(app) as raw:
        raw.cookies.update(client.cookies)
        assert raw.post('/settings/registration', data={'hours': 1}).status_code == 403


@pytest.mark.parametrize('status', ['CREATING', 'CREATED', 'RECOVERING'])
def test_abandoned_operation_loses_secret_without_freeing_username(status):
    rid = make(status, operation=True)
    with SessionLocal() as db:
        db.get(RegistrationOperation, rid).started_at = int(time.time()) - 600
        db.commit()
    expire_requests()
    with SessionLocal() as db:
        row = db.get(RegistrationRequest, rid)
        assert row.status == status and row.password_enc is None
        assert db.get(RegistrationReservation, 'expired') is not None


def test_periodic_expiry_runs_without_page_visits(monkeypatch):
    import asyncio
    import app.expiry as expiry
    calls = []
    async def sleep(seconds):
        calls.append(seconds)
        if len(calls) > 1:
            raise asyncio.CancelledError()
    sweep = Mock()
    monkeypatch.setattr(expiry.asyncio, 'sleep', sleep)
    monkeypatch.setattr(expiry, 'expire_requests', sweep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(expiry.expiry_loop())
    sweep.assert_called_once()
    assert calls == [60, 60]
