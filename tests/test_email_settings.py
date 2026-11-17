from unittest.mock import Mock, MagicMock

from fastapi.testclient import TestClient

from app.config import settings
from app.db import PanelSetting, SessionLocal
from app.mail import EMAIL_NOTICE_KEY, email_notices_enabled, send_moderation_notice, send_password_notice
from app.main import app
from test_registration import login, submit


def test_email_settings_require_auth_and_csrf(client, monkeypatch):
    assert client.post('/settings/email', follow_redirects=False).status_code == 303
    login(client, monkeypatch)
    with TestClient(app) as raw:
        raw.cookies.update(client.cookies)
        assert raw.post('/settings/email').status_code == 403
    assert email_notices_enabled() is True


def test_toggle_persists_independently_of_matrix(client, monkeypatch):
    login(client, monkeypatch)
    matrix = Mock()
    monkeypatch.setattr('app.notifications.MatrixClient', lambda: matrix)
    for enabled in (False, True):
        data = {'enabled': 'true'} if enabled else {}
        assert client.post('/settings/email', data=data, follow_redirects=False).status_code == 303
        assert email_notices_enabled() is enabled
    matrix.whoami.assert_not_called()


def test_disabled_notice_never_contacts_smtp(monkeypatch):
    with SessionLocal() as db:
        db.add(PanelSetting(key=EMAIL_NOTICE_KEY, value='false'))
        db.commit()
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    send_moderation_notice(1, 'tester', 'user@example.com')
    smtp.assert_not_called()
    monkeypatch.setattr(settings, 'smtp_from', 'matrix@example.com')
    send_password_notice('user@example.com', '@tester:example.com', 'newpassword')
    smtp.assert_called_once()


def test_default_notice_sends_to_configured_admin(monkeypatch):
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    monkeypatch.setattr(settings, 'smtp_from', 'matrix@example.com')
    send_moderation_notice(1, 'tester', 'user@example.com')
    message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
    assert message['To'] == settings.admin_email


def test_settings_show_email_switch(client, monkeypatch):
    login(client, monkeypatch)
    monkeypatch.setattr('app.notifications.MatrixClient', Mock())
    page = client.get('/settings')
    assert page.status_code == 200
    assert 'action="/settings/email"' in page.text
    assert settings.admin_email in page.text


def test_registration_still_creates_request_when_mail_disabled(client, monkeypatch):
    with SessionLocal() as db:
        db.add(PanelSetting(key=EMAIL_NOTICE_KEY, value='false'))
        db.commit()
    matrix = Mock()
    matrix.user_exists.return_value = False
    monkeypatch.setattr('app.main.MatrixClient', lambda: matrix)
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    assert submit(client).status_code == 200
    smtp.assert_not_called()
