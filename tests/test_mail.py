from unittest.mock import MagicMock
from app.config import settings
from app.mail import send_password_notice


def test_password_email_has_only_the_intended_recipient(monkeypatch):
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    monkeypatch.setattr(settings, 'smtp_user', 'admin@example.com')
    send_password_notice('user@example.com', '@user:example.com', 'test-password')
    connection = smtp.return_value.__enter__.return_value
    assert connection.starttls.call_args.kwargs['context'].check_hostname is True
    message = connection.send_message.call_args.args[0]
    assert message['To'] == 'user@example.com'
    assert 'test-password' in message.get_content()


def test_local_smtp_sender_without_auth(monkeypatch):
    from app.mail import send_moderation_notice
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    monkeypatch.setattr(settings, 'smtp_from', 'matrix@chat.iteko.su')
    monkeypatch.setattr(settings, 'smtp_user', '')
    monkeypatch.setattr(settings, 'smtp_password', '')
    monkeypatch.setattr(settings, 'smtp_starttls', False)
    for send in (lambda: send_password_notice('user@example.com', '@user:example.com', 'test-password'),
                 lambda: send_moderation_notice(1, 'user', 'user@example.com')):
        send()
        connection = smtp.return_value.__enter__.return_value
        assert connection.send_message.call_args.args[0]['From'] == 'matrix@chat.iteko.su'
        connection.login.assert_not_called()
        connection.starttls.assert_not_called()


def test_from_can_differ_from_authenticated_login(monkeypatch):
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    monkeypatch.setattr(settings, 'smtp_from', 'matrix@example.com')
    monkeypatch.setattr(settings, 'smtp_user', 'smtp-login')
    monkeypatch.setattr(settings, 'smtp_password', 'test-secret')
    monkeypatch.setattr(settings, 'smtp_starttls', True)
    send_password_notice('user@example.com', '@user:example.com', 'test-password')
    connection = smtp.return_value.__enter__.return_value
    connection.login.assert_called_once_with('smtp-login', 'test-secret')
    assert connection.send_message.call_args.args[0]['From'] == 'matrix@example.com'


def test_empty_sender_fails_before_connecting(monkeypatch):
    import pytest
    smtp = MagicMock()
    monkeypatch.setattr('app.mail.smtplib.SMTP', smtp)
    monkeypatch.setattr(settings, 'smtp_from', '')
    monkeypatch.setattr(settings, 'smtp_user', '')
    with pytest.raises(ValueError, match='SMTP_FROM'):
        send_password_notice('user@example.com', '@user:example.com', 'test-password')
    smtp.assert_not_called()
