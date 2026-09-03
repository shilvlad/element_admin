import smtplib
from email.message import EmailMessage
from .config import settings

def send_mail(subject, html):
    if not settings.smtp_host or not settings.admin_email: return
    m=EmailMessage(); m["Subject"]=subject; m["From"]=settings.smtp_user; m["To"]=settings.admin_email
    m.set_content("Откройте панель Synapse User Manager."); m.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_host,settings.smtp_port,timeout=20) as s:
        if settings.smtp_starttls:s.starttls()
        if settings.smtp_user:s.login(settings.smtp_user,settings.smtp_password)
        s.send_message(m)
