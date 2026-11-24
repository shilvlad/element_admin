import hashlib
import json
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import NotificationRoom, PanelSetting, RegistrationRequest, SessionLocal, audit
from .matrix import MatrixClient, MatrixError
from .mail import EMAIL_NOTICE_KEY, email_notices_enabled
from .expiry import ttl_hours

router = APIRouter()
SETTING_KEY = "matrix_registration_notifications"


def load_settings(db):
    row = db.get(PanelSetting, SETTING_KEY)
    return json.loads(row.value) if row else {"enabled": False, "recipients": []}


def normalize_recipients(value):
    if len(value) > 6000:
        raise ValueError("Слишком длинный список получателей")
    result = []
    for item in re.split(r"[\s,;]+", value.strip()):
        if not item:
            continue
        uid = item if item.startswith("@") else f"@{item}:{settings.matrix_server_name}"
        local, separator, server = uid[1:].partition(":")
        if (not separator or server != settings.matrix_server_name or not local or len(uid) > 255
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._=-/" for c in local)):
            raise ValueError("Укажите логины или Matrix ID пользователей этого сервера")
        if uid not in result:
            result.append(uid)
        if len(result) > 20:
            raise ValueError("Можно указать не более 20 получателей")
    return result


def settings_page(request, values, error=None, status_code=200, sender=None):
    from .main import templates
    if sender is None:
        try:
            sender = MatrixClient().whoami()
        except Exception:
            sender = "Не удалось определить: проверьте Matrix-токен"
    with SessionLocal() as db:
        registration_hours = ttl_hours(db)
    return templates.TemplateResponse("settings.html", {
        "request": request, "enabled": values["enabled"],
        "recipients": "\n".join(values["recipients"]), "sender": sender,
        "error": error, "saved": request.query_params.get("saved") == "1",
        "registration_hours": registration_hours, "email_enabled": email_notices_enabled(), "admin_email": settings.admin_email,
    }, status_code=status_code)


@router.get("/settings")
def get_settings(request: Request):
    from .main import admin_required
    if not admin_required(request):
        return RedirectResponse("/login", 303)
    with SessionLocal() as db:
        values = load_settings(db)
    return settings_page(request, values)


def matrix_check_error(subject, exc):
    status = f"HTTP {exc.status_code}" if exc.status_code else "нет соединения с Matrix"
    return f"Не удалось проверить {subject}: {status}. Настройки не изменены."


def validate_recipient(matrix, uid):
    try:
        user = matrix.get_user(uid)
    except MatrixError as exc:
        if exc.status_code == 404:
            raise ValueError(f"Пользователь {uid} не найден") from None
        if exc.status_code != 403:
            raise
        # Sending a message does not require Synapse server-admin permissions.
        # A regular account can check the recipient through the Client API.
        try:
            matrix.get_profile(uid)
        except MatrixError as profile_error:
            if profile_error.status_code == 404:
                raise ValueError(f"Пользователь {uid} не найден или его профиль недоступен") from None
            raise
        return
    if user.get("deactivated"):
        raise ValueError(f"Пользователь {uid} деактивирован")


@router.post("/settings")
def save_settings(request: Request, recipients: str = Form(""), enabled: bool = Form(False)):
    from .main import admin_required, actor, client_ip
    if not admin_required(request):
        return RedirectResponse("/login", 303)
    values = {"enabled": enabled, "recipients": []}
    sender = None
    try:
        values["recipients"] = normalize_recipients(recipients)
        if enabled and not values["recipients"]:
            raise ValueError("Укажите хотя бы одного получателя")
    except ValueError as exc:
        return settings_page(request, values, str(exc), 400)
    if enabled:
        matrix = MatrixClient()
        try:
            sender = matrix.whoami()
        except MatrixError as exc:
            return settings_page(request, values, matrix_check_error("отправителя", exc), 502,
                                 sender="Не удалось определить")
        for uid in values["recipients"]:
            if uid == sender:
                return settings_page(request, values, "Отправитель не может получать собственные уведомления", 400, sender)
            try:
                validate_recipient(matrix, uid)
            except ValueError as exc:
                return settings_page(request, values, str(exc), 400, sender)
            except MatrixError as exc:
                return settings_page(request, values, matrix_check_error(f"получателя {uid}", exc), 502, sender)
    with SessionLocal() as db:
        db.merge(PanelSetting(key=SETTING_KEY, value=json.dumps(values)))
        audit(db, actor(request), "MATRIX_NOTIFICATIONS_SETTINGS", "settings", client_ip(request))
        db.commit()
    return RedirectResponse("/settings?saved=1", 303)


def notification_room(matrix, sender, recipient):
    with SessionLocal() as db:
        row = db.get(NotificationRoom, (sender, recipient))
        if row:
            return row.room_id
    room_id = matrix.create_notification_room(recipient)
    with SessionLocal() as db:
        db.add(NotificationRoom(sender=sender, recipient=recipient, room_id=room_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return db.get(NotificationRoom, (sender, recipient)).room_id
    return room_id


def record_delivery(rid, recipient, failed=False):
    with SessionLocal() as db:
        audit(db, "system", "MATRIX_NOTICE_ERROR" if failed else "MATRIX_NOTICE_SENT",
              f"request:{rid}", details=f"recipient={recipient}")
        db.commit()


def notify_registration(rid):
    # Background failures must not turn a saved application into an HTTP error.
    try:
        with SessionLocal() as db:
            values = load_settings(db)
            if not values["enabled"]:
                return
            req = db.get(RegistrationRequest, rid)
            if req is None:
                return
            body = (f"Новая заявка #{rid}\nЛогин: {req.username}\n"
                    f"{settings.app_base_url.rstrip('/')}/requests#request-{rid}")
        matrix = MatrixClient()
        sender = matrix.whoami()
        for recipient in values["recipients"]:
            try:
                room_id = notification_room(matrix, sender, recipient)
                transaction = hashlib.sha256(f"{sender}|{recipient}|{rid}".encode()).hexdigest()
                matrix.send_notification(room_id, transaction, body)
                record_delivery(rid, recipient)
            except Exception:
                record_delivery(rid, recipient, failed=True)
    except Exception:
        # Do not include exception text: Matrix responses may contain secrets.
        try:
            record_delivery(rid, "-", failed=True)
        except Exception:
            pass


@router.post("/settings/email")
def save_email_settings(request: Request, enabled: bool = Form(False)):
    from .main import admin_required, actor, client_ip
    if not admin_required(request):
        return RedirectResponse("/login", 303)
    with SessionLocal() as db:
        db.merge(PanelSetting(key=EMAIL_NOTICE_KEY, value="true" if enabled else "false"))
        audit(db, actor(request), "EMAIL_NOTIFICATIONS_SETTINGS", "settings", client_ip(request),
              details=f"enabled={enabled}")
        db.commit()
    return RedirectResponse("/settings?saved=1", 303)
