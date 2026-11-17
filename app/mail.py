import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from .config import settings
from .db import SessionLocal, PanelSetting

EMAIL_NOTICE_KEY = "email_moderation_notifications"


def email_notices_enabled():
    with SessionLocal() as db:
        row = db.get(PanelSetting, EMAIL_NOTICE_KEY)
        return row.value == "true" if row else True


def sender_address():
    sender = settings.smtp_from or settings.smtp_user
    if not sender:
        raise ValueError("Укажите SMTP_FROM или SMTP_USER")
    return sender


def send_moderation_notice(request_id, username, requester):
    if not email_notices_enabled():
        return
    msg = EmailMessage()
    msg["Subject"] = f"Новая заявка Matrix #{request_id}"
    msg["From"] = sender_address()
    msg["To"] = settings.admin_email
    link = f"{settings.app_base_url.rstrip('/')}/requests"
    msg.set_content(f"Новая заявка #{request_id}\nПользователь: {username}\nЗаявитель: {requester}\n\nМодерация: {link}")
    html = f"<p>Новая заявка <b>#{request_id}</b></p><p>Пользователь: <b>{escape(username)}</b><br>Заявитель: {escape(requester)}</p><p><a href='{escape(link)}'>Открыть модерацию</a></p>"
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
        if settings.smtp_starttls: s.starttls(context=ssl.create_default_context())
        if settings.smtp_user: s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def send_password_notice(address, user_id, password):
    msg = EmailMessage()
    msg['Subject'] = 'Новый пароль Matrix'
    msg['From'] = sender_address()
    msg['To'] = address
    msg.set_content(f'Учётная запись: {user_id}\nНовый пароль: {password}\n'
                    f'Сервер: {settings.matrix_homeserver}\nСмените пароль после входа.')
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
