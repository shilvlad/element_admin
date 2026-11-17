import hashlib
import json
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import NotificationRoom, PanelSetting, RegistrationRequest, SessionLocal, audit
from .matrix import MatrixClient, MatrixError

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
    return templates.TemplateResponse("settings.html", {
        "request": request, "enabled": values["enabled"],
        "recipients": "\n".join(values["recipients"]), "sender": sender,
        "error": error, "saved": request.query_params.get("saved") == "1",
    }, status_code=status_code)


@router.get("/settings")
def get_settings(request: Request):
    from .main import admin_required
    if not admin_required(request):
        return RedirectResponse("/login", 303)
    with SessionLocal() as db:
        values = load_settings(db)
    return settings_page(request, values)


@router.post("/settings")
def save_settings(request: Request, recipients: str = Form(""), enabled: bool = Form(False)):
    from .main import admin_required, actor, client_ip
    if not admin_required(request):
        return RedirectResponse("/login", 303)
    values = {"enabled": enabled, "recipients": []}
    try:
        values["recipients"] = normalize_recipients(recipients)
        if enabled and not values["recipients"]:
            raise ValueError("Укажите хотя бы одного получателя")
        if enabled:
            matrix = MatrixClient()
            sender = matrix.whoami()
            for uid in values["recipients"]:
                if uid == sender:
                    raise ValueError("Отправитель не может получать собственные уведомления")
                if matrix.get_user(uid).get("deactivated"):
                    raise ValueError(f"Пользователь {uid} деактивирован")
    except ValueError as exc:
        return settings_page(request, values, str(exc), 400)
    except MatrixError:
        return settings_page(request, values, "Не удалось проверить отправителя или получателей. Проверьте Matrix ID и токен.", 502)
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
