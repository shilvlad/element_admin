from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, Form, Query, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select, desc

from .db import SessionLocal, AuditLog, audit
from .matrix import MatrixClient, MatrixError

router = APIRouter()


def context():
    # Imported lazily to keep the application and router initialization independent.
    from .main import templates, admin_required
    return templates, admin_required


def fmt_time(value):
    if not value:
        return 'Нет данных'
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')


def user_url(value):
    return '/users/' + quote(value, safe='') + '/details'


def room_url(value):
    return '/rooms/' + quote(value, safe='')


@router.get('/users/{user_id:path}/details')
def user_detail(request: Request, user_id: str):
    templates, allowed = context()
    if not allowed(request):
        return RedirectResponse('/login', 303)
    m = MatrixClient()
    try:
        user = m.get_user(user_id)
    except MatrixError as exc:
        raise HTTPException(404 if exc.status_code == 404 else 502, 'Не удалось получить пользователя')
    warnings = []
    def optional(call, fallback, name):
        try:
            return call()
        except MatrixError:
            warnings.append(f'Не удалось получить {name}')
            return fallback
    devices = optional(lambda: m.user_devices(user_id), {}, 'устройства').get('devices', [])
    rooms = optional(lambda: m.user_rooms(user_id), {}, 'комнаты').get('joined_rooms', [])
    sessions = optional(lambda: m.user_sessions(user_id), {}, 'подключения').get('devices', {})
    last_seen = [d.get('last_seen_ts') or 0 for d in devices]
    for device in sessions.values():
        last_seen.extend(c.get('last_seen') or 0 for session in device.get('sessions', [])
                         for c in session.get('connections', []))
    last_seen.append(user.get('last_seen_ts') or 0)
    with SessionLocal() as db:
        logs = db.scalars(select(AuditLog).where(AuditLog.target == user_id).order_by(desc(AuditLog.ts)).limit(100)).all()
    return templates.TemplateResponse('user_detail.html', dict(request=request, user=user, user_id=user_id,
        devices=devices, rooms=rooms, sessions=sessions, logs=logs, warnings=warnings,
        last_seen=fmt_time(max(last_seen)), fmt_time=fmt_time, room_url=room_url))


@router.get('/rooms')
def rooms_page(request: Request, q: str = '', offset: str = '0'):
    templates, allowed = context()
    if not allowed(request):
        return RedirectResponse('/login', 303)
    try:
        data = MatrixClient().list_rooms(offset=offset, search=q.strip())
    except MatrixError as exc:
        message = {
            403: 'Synapse отказал в доступе к комнатам. Проверьте права административного токена.',
            400: 'Synapse отклонил параметры поиска. Очистите поиск и повторите запрос.',
        }.get(exc.status_code, 'Не удалось получить список комнат. Проверьте доступность Synapse.')
        return templates.TemplateResponse('rooms.html', dict(request=request, data={}, q=q,
            room_url=room_url, error=message), status_code=502)
    return templates.TemplateResponse('rooms.html', dict(request=request, data=data, q=q, room_url=room_url))


@router.get('/rooms/{room_id:path}/media')
def room_media_page(request: Request, room_id: str, page: int = Query(1, ge=1)):
    templates, allowed = context()
    if not allowed(request):
        return RedirectResponse('/login', 303)
    m = MatrixClient()
    try:
        media = m.room_media(room_id)
    except MatrixError:
        raise HTTPException(502, 'Не удалось получить медиа комнаты')
    uris = list(dict.fromkeys(media.get('local', []) + media.get('remote', [])))
    pages = max(1, (len(uris) + 49) // 50)
    page = min(page, pages)
    rows = []
    for uri in uris[(page-1)*50:page*50]:
        parsed = urlsplit(uri)
        info = None
        if parsed.scheme == 'mxc' and parsed.netloc and parsed.path.strip('/'):
            try:
                info = m.media_info(parsed.netloc, parsed.path.lstrip('/')).get('media_info', {})
            except MatrixError:
                pass
        rows.append({'uri': uri, 'size': info.get('media_length') if info is not None else None})
    known = sum(r['size'] is not None for r in rows)
    return templates.TemplateResponse('room_media.html', dict(request=request, room_id=room_id, rows=rows,
        total=len(uris), page=page, pages=pages, known=known,
        size=sum(r['size'] or 0 for r in rows), room_url=room_url))


@router.post('/rooms/{room_id:path}/invite')
def room_invite(request: Request, room_id: str, user_id: str = Form(...)):
    templates, allowed = context()
    if not allowed(request):
        return RedirectResponse('/login', 303)
    user_id = user_id.strip()
    if not user_id.startswith('@') or ':' not in user_id or len(user_id) > 255:
        raise HTTPException(400, 'Укажите полный Matrix ID пользователя')
    try:
        MatrixClient().invite_to_room(room_id, user_id, request.session['username'])
    except MatrixError:
        return templates.TemplateResponse('result.html', {'request': request,
            'message': 'Приглашение не отправлено. Проверьте участие сервисного аккаунта в комнате и права приглашения.'}, status_code=502)
    with SessionLocal() as db:
        audit(db, request.session['username'], 'INVITE_USER', user_id,
              request.client.host if request.client else '-', f'room={room_id}')
        db.commit()
    return RedirectResponse(room_url(room_id), 303)


@router.get('/rooms/{room_id:path}')
def room_detail(request: Request, room_id: str):
    templates, allowed = context()
    if not allowed(request):
        return RedirectResponse('/login', 303)
    m = MatrixClient()
    try:
        room = m.get_room(room_id)
        members = m.room_members(room_id).get('members', [])
    except MatrixError as exc:
        raise HTTPException(404 if exc.status_code == 404 else 502, 'Не удалось получить комнату')
    return templates.TemplateResponse('room_detail.html', dict(request=request, room=room, members=members,
        room_id=room_id, user_url=user_url, room_url=room_url))
