import smtplib
from email.message import EmailMessage
from html import escape
from .config import settings

def send_moderation_notice(request_id, username, requester):
    msg = EmailMessage()
    msg["Subject"] = f"Новая заявка Matrix #{request_id}"
    msg["From"] = settings.smtp_user
    msg["To"] = settings.admin_email
    link = f"{settings.app_base_url.rstrip('/')}/requests"
    msg.set_content(f"Новая заявка #{request_id}\nПользователь: {username}\nЗаявитель: {requester}\n\nМодерация: {link}")
    html = f"<p>Новая заявка <b>#{request_id}</b></p><p>Пользователь: <b>{escape(username)}</b><br>Заявитель: {escape(requester)}</p><p><a href='{escape(link)}'>Открыть модерацию</a></p>"
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
        if settings.smtp_starttls: s.starttls()
        if settings.smtp_user: s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
