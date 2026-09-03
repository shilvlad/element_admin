import secrets,string
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI,Request,Form
from fastapi.responses import HTMLResponse,RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select,desc
from .config import settings
from .db import SessionLocal,init_db,RegistrationRequest,ManagedUser,AuditLog,audit
from .matrix import create_user,invite_to_global,get_users,get_user,set_password,set_deactivated,user_id
from .mail import send_mail

app=FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware,secret_key=settings.secret_key,session_cookie="sum_session",same_site="lax")
app.mount("/static",StaticFiles(directory=Path(__file__).parent/"static"),name="static")
templates=Jinja2Templates(directory=Path(__file__).parent/"templates")
@app.on_event("startup")
def startup(): init_db()
def pwd(): return ''.join(secrets.choice(string.ascii_letters+string.digits+"!@#$%&*?") for _ in range(16))
def auth(r): return r.session.get("user")
def guard(r): return None if auth(r) else RedirectResponse("/login",303)
def render(r,t,**x): return templates.TemplateResponse(t,{"request":r,**x})
def ip(r): return r.client.host if r.client else ""

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request): return render(request,"login.html",error=None)
@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    if secrets.compare_digest(username,settings.admin_username) and secrets.compare_digest(password,settings.admin_password):
        request.session["user"]=username; return RedirectResponse("/",303)
    return render(request,"login.html",error="Неверный логин или пароль")
@app.get("/logout")
def logout(request:Request): request.session.clear(); return RedirectResponse("/login",303)

@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request):
    if (x:=guard(request)): return x
    with SessionLocal() as db:
        pending=len(db.scalars(select(RegistrationRequest).where(RegistrationRequest.status=="PENDING")).all())
        users=len(db.scalars(select(ManagedUser)).all()); logs=db.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(10)).all()
    return render(request,"dashboard.html",pending=pending,users=users,logs=logs)

@app.get("/request",response_class=HTMLResponse)
def request_page(request:Request):
    if (x:=guard(request)): return x
    return render(request,"request.html",error=None,generated=pwd())
@app.post("/request")
async def request_create(request:Request,username:str=Form(...),displayname:str=Form(...),requester:str=Form(...),new_password:str=Form(...),is_admin:bool=Form(False)):
    if (x:=guard(request)): return x
    username=username.strip().lower().lstrip("@")
    with SessionLocal() as db:
        existing=db.scalar(select(RegistrationRequest).where(RegistrationRequest.username==username,RegistrationRequest.status=="PENDING"))
        if existing:return render(request,"request.html",error="Для этого логина уже есть заявка.",generated=new_password)
        if await get_user(user_id(username)):return render(request,"request.html",error="Такой пользователь уже существует в Synapse.",generated=new_password)
        db.add(RegistrationRequest(username=username,displayname=displayname.strip(),requester=requester.strip(),password=new_password,is_admin=is_admin))
        audit(db,auth(request),"CREATE_REQUEST",user_id(username),ip(request),"Registration request created");db.commit()
    send_mail("[Matrix] Требуется подтверждение регистрации",f"<h2>Новая заявка Matrix</h2><p><b>Логин:</b> @{username}:{settings.matrix_server_name}</p><p><b>ФИО:</b> {displayname}</p><p><b>Инициатор:</b> {requester}</p><p><a href='{settings.matrix_homeserver}/requests'>Открыть модерацию</a></p>")
    return RedirectResponse("/requests",303)

@app.get("/requests",response_class=HTMLResponse)
def requests_page(request:Request):
    if (x:=guard(request)): return x
    with SessionLocal() as db: rows=db.scalars(select(RegistrationRequest).order_by(desc(RegistrationRequest.created_at))).all()
    return render(request,"requests.html",rows=rows)
@app.post("/requests/{rid}/approve")
async def approve(request:Request,rid:int):
    if (x:=guard(request)): return x
    with SessionLocal() as db:
        row=db.get(RegistrationRequest,rid)
        if not row or row.status!="PENDING":return RedirectResponse("/requests",303)
        uid=user_id(row.username)
        try:
            await create_user(row.username,row.password,row.displayname,row.is_admin); await invite_to_global(uid)
            row.status="APPROVED";row.decided_by=auth(request);row.decided_at=datetime.now(timezone.utc)
            db.add(ManagedUser(user_id=uid,username=row.username,displayname=row.displayname));audit(db,auth(request),"APPROVE_USER",uid,ip(request),"Created and invited to #Global")
        except Exception as e:
            row.status="ERROR";audit(db,auth(request),"APPROVE_USER_ERROR",uid,ip(request),str(e))
        db.commit()
    return RedirectResponse("/requests",303)
@app.post("/requests/{rid}/reject")
def reject(request:Request,rid:int,reason:str=Form("")):
    if (x:=guard(request)): return x
    with SessionLocal() as db:
        row=db.get(RegistrationRequest,rid)
        if row and row.status=="PENDING":
            row.status="REJECTED";row.reject_reason=reason;row.decided_by=auth(request);row.decided_at=datetime.now(timezone.utc);audit(db,auth(request),"REJECT_REQUEST",user_id(row.username),ip(request),reason);db.commit()
    return RedirectResponse("/requests",303)

@app.get("/users",response_class=HTMLResponse)
async def users_page(request:Request,q:str=""):
    if (x:=guard(request)): return x
    data=await get_users();q=q.lower().strip()
    if q:data=[u for u in data if q in str(u.get("name","")).lower() or q in str(u.get("displayname","")).lower()]
    return render(request,"users.html",users=data,q=q)
@app.post("/users/{uid}/reset-password")
async def reset_password(request:Request,uid:str,new_password:str=Form("")):
    if (x:=guard(request)): return x
    new_password=new_password or pwd();await set_password(uid,new_password)
    with SessionLocal() as db:audit(db,auth(request),"RESET_PASSWORD",uid,ip(request),"Password reset");db.commit()
    return render(request,"result.html",title="Пароль сброшен",message=f"Новый пароль: {new_password}")
@app.post("/users/{uid}/deactivate")
async def deactivate(request:Request,uid:str):
    if (x:=guard(request)): return x
    await set_deactivated(uid,True)
    with SessionLocal() as db:
        u=db.scalar(select(ManagedUser).where(ManagedUser.user_id==uid));
        if u:u.active=False
        audit(db,auth(request),"DEACTIVATE_USER",uid,ip(request),"User deactivated");db.commit()
    return RedirectResponse("/users",303)
@app.post("/users/{uid}/activate")
async def activate(request:Request,uid:str):
    if (x:=guard(request)): return x
    await set_deactivated(uid,False)
    with SessionLocal() as db:
        u=db.scalar(select(ManagedUser).where(ManagedUser.user_id==uid));
        if u:u.active=True
        audit(db,auth(request),"ACTIVATE_USER",uid,ip(request),"User activated");db.commit()
    return RedirectResponse("/users",303)
@app.get("/audit",response_class=HTMLResponse)
def audit_page(request:Request):
    if (x:=guard(request)): return x
    with SessionLocal() as db:logs=db.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(500)).all()
    return render(request,"audit.html",logs=logs)
