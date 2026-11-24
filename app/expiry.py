import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update

from .config import settings
from .db import PanelSetting, RegistrationRequest, RegistrationReservation, RegistrationOperation, SessionLocal, audit

router = APIRouter()
TTL_KEY = 'registration_ttl_hours'
DEFAULT_TTL = 72


def ttl_hours(db):
    row = db.get(PanelSetting, TTL_KEY)
    return int(row.value) if row else DEFAULT_TTL


def cutoff(db):
    return datetime.now(timezone.utc) - timedelta(hours=ttl_hours(db))


def expire_requests():
    with SessionLocal() as db:
        deadline = cutoff(db)
        ids = db.scalars(select(RegistrationRequest.id).where(
            RegistrationRequest.status == 'PENDING', RegistrationRequest.created_at <= deadline)).all()
        for rid in ids:
            claimed = db.execute(update(RegistrationRequest).where(
                RegistrationRequest.id == rid, RegistrationRequest.status == 'PENDING',
                RegistrationRequest.created_at <= deadline
            ).values(status='EXPIRED', password_enc=None, decided_by='system',
                     decided_at=datetime.now(timezone.utc), error_message='Срок действия заявки истёк. Можно подать новую заявку.'))
            if claimed.rowcount:
                db.execute(delete(RegistrationReservation).where(RegistrationReservation.request_id == rid))
                audit(db, 'system', 'EXPIRE_REGISTRATION', f'request:{rid}')
        # Uncertain creation/recovery must be reconciled, never free its username.
        # Do not erase a secret while a live operation may still be using it.
        grace = max(120, int(settings.matrix_timeout) + 60)
        no_live_operation = ~select(RegistrationOperation.request_id).where(
            RegistrationOperation.request_id == RegistrationRequest.id,
            RegistrationOperation.started_at > int(time.time()) - grace).exists()
        uncertain = ('ERROR', 'CREATING', 'CREATED', 'RECOVERING')
        errors = db.scalars(select(RegistrationRequest.id).where(
            RegistrationRequest.status.in_(uncertain), RegistrationRequest.password_enc.is_not(None),
            RegistrationRequest.created_at <= deadline, no_live_operation)).all()
        for rid in errors:
            claimed = db.execute(update(RegistrationRequest).where(
                RegistrationRequest.id == rid, RegistrationRequest.status.in_(uncertain),
                RegistrationRequest.password_enc.is_not(None), RegistrationRequest.created_at <= deadline, no_live_operation
            ).values(password_enc=None, error_message='Срок хранения пароля истёк. Сверьте аккаунт с Synapse; при его отсутствии отклоните заявку и создайте новую.'))
            if claimed.rowcount:
                audit(db, 'system', 'EXPIRE_REGISTRATION_SECRET', f'request:{rid}')
        db.commit()


async def expiry_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(expire_requests)
        except Exception:
            logging.getLogger(__name__).warning('Не удалось проверить срок действия заявок')


@router.post('/settings/registration')
def save_registration_settings(request: Request, hours: int = Form(...)):
    from .main import admin_required, actor, client_ip, templates
    if not admin_required(request):
        return RedirectResponse('/login', 303)
    if not 1 <= hours <= 720:
        return templates.TemplateResponse('result.html', {'request': request,
            'message': 'Срок должен быть от 1 до 720 часов.'}, status_code=400)
    with SessionLocal() as db:
        db.merge(PanelSetting(key=TTL_KEY, value=str(hours)))
        audit(db, actor(request), 'REGISTRATION_TTL_SETTINGS', 'settings', client_ip(request), details=f'hours={hours}')
        db.commit()
    expire_requests()
    return RedirectResponse('/settings?saved=1', 303)
