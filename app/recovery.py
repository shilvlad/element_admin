import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import (SessionLocal, RegistrationRequest, RegistrationOperation,
                 RegistrationReservation, ManagedUser, audit)
from .matrix import MatrixClient, MatrixError

router = APIRouter()
RECOVERABLE = ('CREATING', 'CREATED', 'ERROR', 'RECOVERING')


@router.post('/requests/{rid}/recover')
def recover_request(request: Request, rid: int, confirm_existing: bool = Form(False)):
    from .main import admin_required, templates, actor, client_ip
    if not admin_required(request):
        return RedirectResponse('/login', 303)

    def result(message, status=200):
        return templates.TemplateResponse('result.html', {'request': request, 'message': message}, status_code=status)

    now = int(time.time())
    with SessionLocal() as db:
        req = db.get(RegistrationRequest, rid)
        if not req or req.status not in RECOVERABLE:
            return RedirectResponse('/requests', 303)
        operation = db.get(RegistrationOperation, rid)
        # Allow the outstanding Synapse request to finish before reconciling state.
        grace = max(120, int(settings.matrix_timeout) + 60)
        if operation and now - operation.started_at < grace:
            return result('Операция ещё может выполняться. Повторите сверку через несколько минут.', 409)
        reservation = db.get(RegistrationReservation, req.username)
        if reservation and reservation.request_id != rid:
            return result('Логин принадлежит другой заявке. Требуется ручная проверка.', 409)
        old_status = req.status
        claimed = db.execute(update(RegistrationRequest).where(
            RegistrationRequest.id == rid, RegistrationRequest.status == old_status
        ).values(status='RECOVERING'), execution_options={'synchronize_session': False})
        if claimed.rowcount != 1:
            db.rollback()
            return RedirectResponse('/requests', 303)
        if operation:
            # Claim the lease as well as the status: another recovery may already be running.
            leased = db.execute(update(RegistrationOperation).where(
                RegistrationOperation.request_id == rid,
                RegistrationOperation.started_at == operation.started_at
            ).values(started_at=now), execution_options={'synchronize_session': False})
            if leased.rowcount != 1:
                db.rollback()
                return result('Сверка уже выполняется.', 409)
        else:
            db.add(RegistrationOperation(request_id=rid, started_at=now))
        if not reservation:
            db.add(RegistrationReservation(username=req.username, request_id=rid))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return result('Заявку уже обрабатывают. Обновите страницу.', 409)
        db.refresh(req)
        uid = f'@{req.username}:{settings.matrix_server_name}'
        try:
            user = MatrixClient().get_user(uid)
        except MatrixError as exc:
            if exc.status_code != 404:
                req.status = old_status
                req.error_message = 'Сверка с Synapse не выполнена. Изменения учётной записи не производились.'
            elif old_status == 'CREATED' or not req.password_enc:
                req.status = 'ERROR'
                req.error_message = 'Synapse не нашёл ранее созданную запись. Требуется ручная проверка; пароль не восстановлен.'
            else:
                req.status = 'ERROR'
                req.error_message = 'Synapse не нашёл учётную запись. Можно повторить одобрение заявки.'
            db.delete(db.get(RegistrationOperation, rid))
            audit(db, actor(request), 'RECOVER_ERROR', f'request:{rid}', client_ip(request))
            db.commit()
            return RedirectResponse('/requests', 303)
        if not confirm_existing:
            req.status = old_status
            db.delete(db.get(RegistrationOperation, rid))
            db.commit()
            return result(f'В Synapse существует {uid}. Убедитесь, что это учётная запись данной заявки, '
                          'затем отметьте подтверждение на странице заявок и повторите сверку. Пароль и права не изменены.', 409)
        managed = db.scalar(select(ManagedUser).where(ManagedUser.user_id == uid))
        if not managed:
            managed = ManagedUser(user_id=uid, username=req.username)
            db.add(managed)
        managed.displayname = user.get('displayname') or ''
        managed.active = not user.get('deactivated', False)
        req.status = 'APPROVED'
        req.password_enc = None
        req.is_admin = bool(user.get('admin'))
        req.decided_by = actor(request)
        req.decided_at = datetime.now(timezone.utc)
        req.error_message = 'Учётная запись найдена при сверке. При необходимости повторите приглашение в Global.'
        db.delete(db.get(RegistrationOperation, rid))
        audit(db, actor(request), 'RECOVER_REGISTRATION', uid, client_ip(request), f'request={rid}')
        db.commit()
    return RedirectResponse('/requests', 303)
