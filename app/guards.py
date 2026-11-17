import hashlib
import secrets
import time

from fastapi import HTTPException, Request
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import RateLimitBucket, SessionLocal


def csrf_token(request):
    if not request.session.get('csrf'):
        request.session['csrf'] = secrets.token_urlsafe(32)
    return request.session['csrf']


def consume_limit(key, limit, window):
    now = int(time.time())
    bucket = hashlib.sha256(key.encode()).hexdigest() + ':' + str(now // window)
    with SessionLocal() as db:
        db.execute(delete(RateLimitBucket).where(RateLimitBucket.expires_at < now))
        result = db.execute(update(RateLimitBucket).where(
            RateLimitBucket.key == bucket, RateLimitBucket.hits < limit
        ).values(hits=RateLimitBucket.hits + 1))
        if result.rowcount:
            db.commit()
            return
        if db.get(RateLimitBucket, bucket):
            raise HTTPException(429, 'Слишком много попыток. Попробуйте позже.',
                                headers={'Retry-After': str(window - now % window)})
        db.add(RateLimitBucket(key=bucket, hits=1, expires_at=now + window))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            result = db.execute(update(RateLimitBucket).where(
                RateLimitBucket.key == bucket, RateLimitBucket.hits < limit
            ).values(hits=RateLimitBucket.hits + 1))
            db.commit()
            if not result.rowcount:
                raise HTTPException(429, 'Слишком много попыток. Попробуйте позже.',
                                    headers={'Retry-After': str(window - now % window)})


async def protect_forms(request: Request):
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return
    form = await request.form()
    token = request.headers.get('X-CSRF-Token') or form.get('csrf_token')
    expected = request.session.get('csrf')
    if not isinstance(token, str) or not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(403, 'Форма устарела. Обновите страницу и повторите действие.')
    ip = request.client.host if request.client else '-'
    if request.url.path == '/login':
        consume_limit('login-ip:' + ip, settings.login_attempts, 60)
        username = str(form.get('username', '')).strip().casefold()
        consume_limit('login-user:' + username, settings.login_attempts, 60)
    elif request.url.path == '/request':
        consume_limit('registration:' + ip, settings.registration_attempts, 3600)
