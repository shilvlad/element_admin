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
