from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, desc
from .config import settings
from .db import SessionLocal, init_db, RegistrationRequest, ManagedUser, AuditLog, audit
from .security import encrypt_secret, decrypt_secret, generate_password
from .matrix import MatrixClient, MatrixError
from .mail import send_moderation_notice

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.app_base_url.startswith("https://"), same_site="lax", max_age=28800)
templates = Jinja2Templates(directory="app/templates")
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
    if username == settings.admin_username and password == settings.admin_password:
        request.session.update({"admin": True, "username": username})
        with SessionLocal() as db: audit(db, username, "LOGIN", "admin", client_ip(request)); db.commit()
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401)

@app.get("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/login", status_code=303)

@app.get("/")
def dashboard(request: Request):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db:
        pending = db.scalar(select(RegistrationRequest).where(RegistrationRequest.status == "PENDING").count()) if False else len(db.scalars(select(RegistrationRequest).where(RegistrationRequest.status == "PENDING")).all())
        users = len(db.scalars(select(ManagedUser)).all())
    return templates.TemplateResponse("dashboard.html", {"request": request, "pending": pending, "users": users})

@app.get("/request")
def request_page(request: Request): return templates.TemplateResponse("request.html", {"request": request})

@app.post("/request")
def create_request(request: Request, username: str = Form(...), displayname: str = Form(""), requester: str = Form(...), is_admin: bool = Form(False)):
    username = username.strip().lower().lstrip("@").split(":")[0]
    if not username or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._=-" for c in username):
        return templates.TemplateResponse("request.html", {"request": request, "error": "Недопустимый Matrix username"}, status_code=400)
    password = generate_password()
    with SessionLocal() as db:
        req = RegistrationRequest(requester=requester.strip(), username=username, displayname=displayname.strip(), password_enc=encrypt_secret(password), is_admin=is_admin, status="PENDING")
        db.add(req); db.flush(); audit(db, requester, "REGISTRATION_REQUEST", f"request:{req.id}", client_ip(request), f"username={username}"); db.commit(); rid=req.id
    try: send_moderation_notice(rid, username, requester)
    except Exception: pass
    return templates.TemplateResponse("result.html", {"request": request, "message": f"Заявка #{rid} создана и отправлена на модерацию."})

@app.get("/requests")
def requests_page(request: Request):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db: rows = db.scalars(select(RegistrationRequest).order_by(desc(RegistrationRequest.created_at))).all()
    return templates.TemplateResponse("requests.html", {"request": request, "rows": rows})

@app.post("/requests/{rid}/approve")
def approve(request: Request, rid: int):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db:
        req=db.get(RegistrationRequest,rid)
        if not req: return RedirectResponse("/requests",303)
        if req.status not in ("PENDING","ERROR"): return RedirectResponse("/requests",303)
        req.status="CREATING"; req.error_message=None; db.commit()
        try:
            password=decrypt_secret(req.password_enc)
            uid=f"@{req.username}:{settings.matrix_server_name}"
            m=MatrixClient(); m.create_user(uid,password,req.displayname,req.is_admin)
            req.status="CREATED"; db.commit()
            # Password is no longer needed after successful user creation.
            req.password_enc=None
            managed=db.scalar(select(ManagedUser).where(ManagedUser.user_id==uid))
            if not managed: db.add(ManagedUser(user_id=uid,username=req.username,displayname=req.displayname,active=True))
            req.status="APPROVED"; req.decided_by=actor(request); req.decided_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)
            audit(db, actor(request), "APPROVE_REGISTRATION", uid, client_ip(request), f"request={rid}")
            db.commit()
        except Exception as e:
            req.status="ERROR"; req.error_message=str(e)[:1000]; audit(db, actor(request), "APPROVE_ERROR", f"request:{rid}", client_ip(request), str(e)[:1000]); db.commit()
    return RedirectResponse("/requests",303)

@app.post("/requests/{rid}/reject")
def reject(request: Request, rid: int, reason: str = Form("")):
    if not admin_required(request): return login_redirect()
    with SessionLocal() as db:
        req=db.get(RegistrationRequest,rid)
        if req and req.status=="PENDING":
            req.status="REJECTED"; req.decided_by=actor(request); req.reject_reason=reason; req.decided_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc); req.password_enc=None
            audit(db,actor(request),"REJECT_REGISTRATION",f"request:{rid}",client_ip(request),reason); db.commit()
    return RedirectResponse("/requests",303)

@app.get("/users")
def users_page(request: Request, q: str = ""):
    if not admin_required(request): return login_redirect()
    data=MatrixClient().list_users(1000).get("users",[])
    if q: data=[u for u in data if q.lower() in str(u).lower()]
    return templates.TemplateResponse("users.html", {"request": request, "users": data, "q": q})

@app.post("/users/{user_id:path}/password")
def reset_password(request: Request, user_id: str):
    if not admin_required(request): return login_redirect()
    pwd=generate_password()
    try:
        MatrixClient().update_user(user_id,password=pwd)
        with SessionLocal() as db: audit(db,actor(request),"RESET_PASSWORD",user_id,client_ip(request)); db.commit()
        return templates.TemplateResponse("result.html", {"request":request,"message":f"Пароль сброшен для {user_id}.","secret":pwd})
    except Exception as e: return templates.TemplateResponse("result.html", {"request":request,"message":f"Ошибка: {e}"},status_code=502)

@app.post("/users/{user_id:path}/deactivate")
def deactivate(request: Request,user_id:str):
    if not admin_required(request): return login_redirect()
    try: MatrixClient().update_user(user_id,deactivated=True); msg="деактивирован"; action="DEACTIVATE"
    except Exception as e: return templates.TemplateResponse("result.html", {"request":request,"message":str(e)},status_code=502)
    with SessionLocal() as db: audit(db,actor(request),action,user_id,client_ip(request)); db.commit()
    return RedirectResponse("/users",303)

@app.post("/users/{user_id:path}/activate")
def activate(request: Request,user_id:str):
    if not admin_required(request): return login_redirect()
    try: MatrixClient().update_user(user_id,deactivated=False); action="ACTIVATE"
    except Exception as e: return templates.TemplateResponse("result.html", {"request":request,"message":str(e)},status_code=502)
    with SessionLocal() as db: audit(db,actor(request),action,user_id,client_ip(request)); db.commit()
    return RedirectResponse("/users",303)

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
