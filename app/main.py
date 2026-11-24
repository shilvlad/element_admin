from .build import build_info
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import time
import asyncio
from contextlib import suppress
from email_validator import validate_email, EmailNotValidError
from fastapi import FastAPI, Request, Form, Query, Depends, BackgroundTasks
from typing import Literal
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, desc, update, delete, func
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import SessionLocal, init_db, RegistrationRequest, RegistrationReservation, RegistrationOperation, ManagedUser, AuditLog, audit
from .security import encrypt_secret, decrypt_secret, generate_password
from .matrix import MatrixClient, MatrixError
from .mail import send_moderation_notice, send_password_notice
from .guards import csrf_token, protect_forms
from .admin_views import router, fmt_time, user_url
from .recovery import router as recovery_router
from .notifications import router as settings_router, notify_registration
from .updates import synapse_update_status
from .expiry import router as expiry_router, expire_requests, expiry_loop, cutoff
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


@asynccontextmanager
async def lifespan(app):
    init_db()
    await asyncio.to_thread(expire_requests)
    task = asyncio.create_task(expiry_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

app = FastAPI(title=settings.app_name, lifespan=lifespan, dependencies=[Depends(protect_forms)])
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.app_base_url.startswith("https://"), same_site="lax", max_age=28800)
templates = Jinja2Templates(directory="app/templates")
templates.env.globals.update(matrix_homeserver=settings.matrix_homeserver.rstrip("/"), matrix_server_name=settings.matrix_server_name)
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["build_info"] = build_info
templates.env.globals.update(fmt_time=fmt_time, user_url=user_url)
app.include_router(router)
app.include_router(recovery_router)
app.include_router(settings_router)
app.include_router(expiry_router)
password_hasher = PasswordHasher()
app.mount("/static", StaticFiles(directory="app/static"), name="static")

def admin_required(request: Request):
    return request.session.get("admin") is True

def login_redirect(): return RedirectResponse("/login", status_code=303)
def actor(request): return request.session.get("username", "unknown")
def client_ip(request): return request.client.host if request.client else "-"

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request): return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == settings.admin_username:
        try:
            password_hasher.verify(settings.admin_password_hash, password)
            request.session.clear()
            request.session.update({"admin": True, "username": username})
            return RedirectResponse("/", status_code=303)
        except VerifyMismatchError:
            pass

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )

@app.post("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/login", status_code=303)

@app.get("/")
def dashboard(request: Request):
    if not admin_required(request): return login_redirect()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with SessionLocal() as db:
        statuses = dict(db.execute(select(RegistrationRequest.status, func.count()).group_by(RegistrationRequest.status)).all())
        errors = db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.ts >= cutoff, AuditLog.action.like("%ERROR")))
        recent = db.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(10)).all()
    available = False
    total = active = deactivated = seen = None
    try:
        users = MatrixClient().list_users(include_deactivated=True).get("users", [])
        available = True
        total = len(users)
        deactivated = sum(bool(u.get("deactivated")) for u in users)
        active = total - deactivated
        if any("last_seen_ts" in u for u in users):
            seen = sum((u.get("last_seen_ts") or 0) >= cutoff.timestamp()*1000 for u in users)
    except MatrixError:
        pass
    return templates.TemplateResponse("dashboard.html", dict(request=request, statuses=statuses, errors=errors,
        recent=recent, available=available, total=total, active=active, deactivated=deactivated, seen=seen,
        synapse_update=synapse_update_status()))

@app.get("/request")
def request_page(request: Request): return templates.TemplateResponse("request.html", {"request": request})

@app.post("/request")
def create_request(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), displayname: str = Form(""),
                   requester: str = Form(...), password: str = Form(...),
                   password_confirm: str = Form(...)):
    def invalid(message, code=400):
        return templates.TemplateResponse("request.html", {"request": request, "error": message}, status_code=code)

    username = username.strip().lower().lstrip("@").split(":")[0]
    if not username or len(username) > 200 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._=-" for c in username):
        return invalid("Недопустимый Matrix username")
    if password != password_confirm:
        return invalid("Пароли не совпадают")
    if not 8 <= len(password) <= 128:
        return invalid("Пароль должен содержать от 8 до 128 символов")
    if not requester.strip() or len(requester) > 255 or len(displayname) > 255:
        return invalid("Укажите контакт заявителя; поля имени и контакта — не более 255 символов")
    try:
        requester = validate_email(requester.strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return invalid("Укажите корректный email заявителя")
    expire_requests()
    uid = f"@{username}:{settings.matrix_server_name}"
    try:
        if MatrixClient().user_exists(uid):
            return invalid("Логин уже занят", 409)
    except MatrixError:
        return invalid("Не удалось проверить логин на сервере. Попробуйте позже", 502)
    with SessionLocal() as db:
        # Include old requests created before username reservations were introduced.
        existing = db.scalar(select(RegistrationRequest.id).where(
            RegistrationRequest.username == username,
            RegistrationRequest.status.notin_(("REJECTED", "EXPIRED"))))
        if existing is not None:
            return invalid("Заявка на этот логин уже существует", 409)
        req = RegistrationRequest(requester=requester.strip(), username=username,
                                  displayname=displayname.strip(), password_enc=encrypt_secret(password),
                                  is_admin=False, status="PENDING")
        try:
            db.add(req)
            db.flush()
            db.add(RegistrationReservation(username=username, request_id=req.id))
            audit(db, requester.strip(), "REGISTRATION_REQUEST", f"request:{req.id}", client_ip(request), f"username={username}")
            db.commit()
        except IntegrityError:
            db.rollback()
            return invalid("Заявка на этот логин уже существует", 409)
        rid = req.id
    try: send_moderation_notice(rid, username, requester)
    except Exception: pass
    background_tasks.add_task(notify_registration, rid)
    return templates.TemplateResponse("result.html", {"request": request, "message": f"Заявка #{rid} создана и отправлена на модерацию. После одобрения войдите с выбранным паролем. Статус уточните у администратора."})

@app.get("/requests")
def requests_page(request: Request):
    if not admin_required(request): return login_redirect()
    expire_requests()
    with SessionLocal() as db: rows = db.scalars(select(RegistrationRequest).order_by(desc(RegistrationRequest.created_at))).all()
    return templates.TemplateResponse("requests.html", {"request": request, "rows": rows})

@app.post("/requests/{rid}/approve")
def approve(request: Request, rid: int, is_admin: bool = Form(False), confirmed: bool = Form(False)):
    if not admin_required(request): return login_redirect()
    expire_requests()
    with SessionLocal() as db:
        req = db.get(RegistrationRequest, rid)
        if not req or req.status not in ("PENDING", "ERROR"):
            return RedirectResponse("/requests", 303)
        if not req.password_enc:
            return templates.TemplateResponse("result.html", {"request": request,
                "message": "Пароль заявки удалён. Сверьте аккаунт с Synapse; при его отсутствии отклоните заявку и создайте новую."}, status_code=409)
        if req.created_at.replace(tzinfo=timezone.utc) <= cutoff(db):
            db.close()
            expire_requests()
            return RedirectResponse("/requests", 303)
        if is_admin and not confirmed:
            return confirm_action(request, req.username, "Создать администратора Matrix",
                "Пользователь получит права управления сервером Synapse.", {"is_admin": "true"}, "/requests")
        reservation = db.get(RegistrationReservation, req.username)
        if reservation and reservation.request_id != rid:
            req.error_message = "Этот логин зарезервирован другой заявкой. Отклоните дубликат."
            db.commit()
            return RedirectResponse("/requests", 303)
        try:
            if not reservation:
                db.add(RegistrationReservation(username=req.username, request_id=rid))
                db.flush()
            claimed = db.execute(update(RegistrationRequest).where(
                RegistrationRequest.id == rid,
                RegistrationRequest.status.in_(("PENDING", "ERROR")),
                RegistrationRequest.password_enc.is_not(None), RegistrationRequest.created_at > cutoff(db)
            ).values(status="CREATING", error_message=None), execution_options={"synchronize_session": False})
            if claimed.rowcount == 1:
                operation = db.get(RegistrationOperation, rid)
                if operation:
                    operation.started_at = int(time.time())
                else:
                    db.add(RegistrationOperation(request_id=rid, started_at=int(time.time())))
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse("/requests", 303)
        if claimed.rowcount != 1:
            return RedirectResponse("/requests", 303)
        db.refresh(req)
        uid = f"@{req.username}:{settings.matrix_server_name}"
        m = MatrixClient()
        try:
            password = decrypt_secret(req.password_enc)
            try:
                email = validate_email(req.requester, check_deliverability=False).normalized
            except EmailNotValidError:
                email = None  # Legacy applications may contain a name instead of email.
            if email:
                m.create_user(uid, password, req.displayname, is_admin, email=email)
            else:
                m.create_user(uid, password, req.displayname, is_admin)
        except Exception:
            # Do not save upstream error bodies: they may contain credentials.
            req.status = "ERROR"
            req.error_message = "Создание не подтверждено. Проверьте наличие учётной записи в Synapse перед повтором; существующая запись не будет изменена."
            audit(db, actor(request), "APPROVE_ERROR", f"request:{rid}", client_ip(request))
            db.commit()
            return RedirectResponse("/requests", 303)
        # Commit account creation and secret deletion before attempting the invite.
        req.password_enc = None
        if not db.scalar(select(ManagedUser).where(ManagedUser.user_id == uid)):
            db.add(ManagedUser(user_id=uid, username=req.username, displayname=req.displayname, active=True))
        req.status = "APPROVED"
        req.is_admin = is_admin
        req.decided_by = actor(request)
        req.decided_at = datetime.now(timezone.utc)
        audit(db, actor(request), "APPROVE_REGISTRATION", uid, client_ip(request), f"request={rid}")
        db.commit()
        try:
            m.invite_to_room(settings.global_room_id, uid, actor(request))
        except Exception:
            req.error_message = "Учётная запись создана. Приглашение в Global не отправлено; можно повторить отдельно."
            audit(db, actor(request), "INVITE_ERROR", uid, client_ip(request))
            db.commit()
    return RedirectResponse("/requests", 303)


@app.post("/requests/{rid}/invite")
def retry_invite(request: Request, rid: int):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db:
        req = db.get(RegistrationRequest, rid)
        if not req or req.status != "APPROVED":
            return RedirectResponse("/requests", 303)
        uid = f"@{req.username}:{settings.matrix_server_name}"
        try:
            MatrixClient().invite_to_room(settings.global_room_id, uid, actor(request))
            req.error_message = None
            audit(db, actor(request), "INVITE_USER", uid, client_ip(request))
        except Exception:
            req.error_message = "Учётная запись создана. Приглашение в Global не отправлено; можно повторить отдельно."
            audit(db, actor(request), "INVITE_ERROR", uid, client_ip(request))
        db.commit()
    return RedirectResponse("/requests", 303)

@app.post("/requests/{rid}/reject")
def reject(request: Request, rid: int, reason: str = Form("")):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db:
        req=db.get(RegistrationRequest,rid)
        if req and req.status in ("PENDING", "ERROR"):
            claimed = db.execute(update(RegistrationRequest).where(
                RegistrationRequest.id == rid, RegistrationRequest.status.in_(("PENDING", "ERROR"))
            ).values(status="REJECTED"), execution_options={"synchronize_session": False})
            if claimed.rowcount != 1:
                db.rollback()
                return RedirectResponse("/requests", 303)
            db.execute(delete(RegistrationReservation).where(RegistrationReservation.request_id == rid))
            req.status="REJECTED"; req.decided_by=actor(request); req.reject_reason=reason; req.decided_at=datetime.now(timezone.utc); req.password_enc=None
            audit(db,actor(request),"REJECT_REGISTRATION",f"request:{rid}",client_ip(request),reason); db.commit()
    return RedirectResponse("/requests",303)

@app.get("/users")
def users_page(request: Request, q: str = "", status: Literal["active", "deactivated", "all"] = "active",
               page: int = Query(1, ge=1)):
    if not admin_required(request): return login_redirect()
    try:
        data = MatrixClient().list_users(include_deactivated=status != "active").get("users", [])
    except MatrixError:
        return templates.TemplateResponse("users.html", {"request": request, "users": [], "q": q,
            "status": status, "page": 1, "pages": 1, "total": 0,
            "error": "Не удалось получить пользователей из Synapse. Попробуйте позже."}, status_code=502)
    if status != "all":
        data = [u for u in data if bool(u.get("deactivated")) == (status == "deactivated")]
    if q.strip():
        query = q.strip().casefold()
        data = [u for u in data if query in u.get("name", "").casefold()
                or query in (u.get("displayname") or "").casefold()]
    total = len(data)
    pages = max(1, (total + 49) // 50)
    page = min(page, pages)
    return templates.TemplateResponse("users.html", {"request": request, "users": data[(page-1)*50:page*50],
        "q": q, "status": status, "page": page, "pages": pages, "total": total})

def password_result(request, matrix, user_id, password, message):
    address = None
    profile_failed = False
    try:
        profile = matrix.get_user(user_id)
        threepids = profile.get("threepids", [])
        if isinstance(threepids, list):
            for item in threepids:
                if item.get("medium") == "email":
                    try:
                        address = validate_email(item.get("address", ""), check_deliverability=False).normalized
                        break
                    except EmailNotValidError:
                        continue
    except MatrixError:
        profile_failed = True
    delivered = False
    if address:
        try:
            send_password_notice(address, user_id, password)
            delivered = True
            message += f" Новый пароль отправлен на {address}."
        except Exception:
            message += " Письмо не отправлено. Передайте пароль пользователю вручную."
    else:
        message += " Email недоступен. Передайте пароль пользователю вручную." if profile_failed else " Email не указан. Передайте пароль пользователю вручную."
    if address:
        with SessionLocal() as db:
            audit(db, actor(request), "PASSWORD_EMAIL_SENT" if delivered else "PASSWORD_EMAIL_ERROR", user_id, client_ip(request))
            db.commit()
    return templates.TemplateResponse("result.html", {"request": request, "message": message,
        "secret": None if delivered else password}, headers={"Cache-Control": "no-store"})


@app.post("/users/{user_id:path}/reset_password", include_in_schema=False)
@app.post("/users/{user_id:path}/password")
def reset_password(request: Request, user_id: str, confirmed: bool = Form(False)):
    if not admin_required(request): return login_redirect()
    if not confirmed:
        return confirm_action(request, user_id, "Сбросить пароль",
            "Старый пароль перестанет работать, активные сеансы будут завершены. Новый пароль будет отправлен на email профиля либо показан для ручной передачи.")
    password = generate_password()
    try:
        matrix = MatrixClient()
        matrix.reset_password(user_id, password)
    except MatrixError as exc:
        message = {
            403: "Synapse отказал в доступе. Проверьте права административного токена.",
            404: "Учётная запись не найдена либо API сброса пароля недоступен.",
            400: "Synapse отклонил сброс пароля. Проверьте настройки локальной авторизации и требования к паролю.",
        }.get(exc.status_code, "Не удалось подтвердить сброс пароля в Synapse. Обновите список пользователей и повторите попытку.")
        return templates.TemplateResponse("result.html", {"request": request, "message": message},
                                          status_code=502, headers={"Cache-Control": "no-store"})
    with SessionLocal() as db:
        audit(db, actor(request), "RESET_PASSWORD", user_id, client_ip(request))
        db.commit()
    return password_result(request, matrix, user_id, password, f"Пароль сброшен для {user_id}.")

@app.post("/users/{user_id:path}/deactivate")
def deactivate(request: Request, user_id: str, confirmed: bool = Form(False)):
    if not admin_required(request): return login_redirect()
    if not confirmed:
        return confirm_action(request, user_id, "Деактивировать пользователя",
            "Пользователь потеряет доступ к аккаунту. Это действие не является удалением всех его данных.")
    try: MatrixClient().update_user(user_id,deactivated=True); msg="деактивирован"; action="DEACTIVATE"
    except Exception as e: return templates.TemplateResponse("result.html", {"request":request,"message":str(e)},status_code=502)
    with SessionLocal() as db: audit(db,actor(request),action,user_id,client_ip(request)); db.commit()
    return RedirectResponse("/users",303)

@app.post("/users/{user_id:path}/activate")
def activate(request: Request,user_id:str):
    if not admin_required(request): return login_redirect()
    password = generate_password()
    try:
        matrix = MatrixClient()
        matrix.update_user(user_id, deactivated=False, password=password)
        action="ACTIVATE"
    except MatrixError: return templates.TemplateResponse("result.html", {"request": request, "message": "Не удалось активировать учётную запись. Проверьте её статус в Synapse перед повтором."}, status_code=502)
    with SessionLocal() as db: audit(db,actor(request),action,user_id,client_ip(request)); db.commit()
    return password_result(request, matrix, user_id, password, f"Учётная запись {user_id} активирована.")

@app.get("/audit")
def audit_page(request: Request):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db: rows=db.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(500)).all()
    return templates.TemplateResponse("audit.html", {"request":request,"rows":rows})

@app.get("/health")
def health(): return {"status":"ok","service":"synapse-user-manager"}

@app.get("/diagnostics")
def diagnostics(request: Request):
    if not admin_required(request): return login_redirect()
    if not settings.enable_diagnostics: return HTMLResponse("disabled", status_code=404)
    checks=[]
    try:
        MatrixClient().health()
        checks.append(("Synapse client API / TLS", True, "OK"))
    except Exception as e:
        checks.append(("Synapse client API / TLS", False, str(e)[:500]))
    try:
        MatrixClient().admin_health()
        checks.append(("Synapse Admin API + token", True, "OK"))
    except Exception as e:
        checks.append(("Synapse Admin API + token", False, str(e)[:500]))
    try:
        with SessionLocal() as db:
            db.execute(select(AuditLog).limit(1)).all()
        checks.append(("Database", True, "OK"))
    except Exception as e:
        checks.append(("Database", False, str(e)[:500]))
    return templates.TemplateResponse("diagnostics.html", {"request": request, "checks": checks})


@app.get("/about")
def about(request: Request):
    if not admin_required(request): return login_redirect()
    return templates.TemplateResponse("about.html", {"request": request, "build": build_info()})


@app.post("/users/{user_id:path}/invite-global")
def invite_global(request: Request, user_id: str):
    if not admin_required(request): return login_redirect()
    matrix = MatrixClient()
    code = 200
    action = "INVITE_GLOBAL"
    try:
        user = matrix.get_user(user_id)
        if user.get("deactivated"):
            return templates.TemplateResponse("result.html", {"request": request,
                "message": "Сначала активируйте учётную запись."}, status_code=400)
        rooms = matrix.user_rooms(user_id).get("joined_rooms", [])
        if settings.global_room_id in rooms:
            message = f"{user_id} уже состоит в Global."
            action = "INVITE_GLOBAL_SKIPPED"
        else:
            matrix.invite_to_room(settings.global_room_id, user_id, actor(request))
            message = f"Приглашение в Global отправлено для {user_id}. Пользователю нужно принять его в Element."
    except MatrixError:
        code = 502
        action = "INVITE_GLOBAL_ERROR"
        message = "Не удалось отправить приглашение в Global. Проверьте доступность Synapse и права отправителя."
    with SessionLocal() as db:
        audit(db, actor(request), action, user_id, client_ip(request))
        db.commit()
    return templates.TemplateResponse("result.html", {"request": request, "message": message}, status_code=code)


def confirm_action(request, target, title, consequence, fields=None, cancel_url="/users"):
    return templates.TemplateResponse("confirm.html", {"request": request, "target": target,
        "title": title, "consequence": consequence, "fields": fields or {}, "cancel_url": cancel_url},
        headers={"Cache-Control": "no-store"})
