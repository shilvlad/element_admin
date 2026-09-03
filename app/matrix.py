import httpx
from .config import settings

def headers(): return {"Authorization": f"Bearer {settings.matrix_admin_token}"}
def user_id(username): return f"@{username}:{settings.matrix_server_name}"

async def get_user(uid):
    url=f"{settings.matrix_homeserver.rstrip('/')}/_synapse/admin/v2/users/{uid}"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.get(url,headers=headers())
        if r.status_code==404:return None
        r.raise_for_status(); return r.json()

async def get_users():
    url=f"{settings.matrix_homeserver.rstrip('/')}/_synapse/admin/v2/users?limit=1000"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.get(url,headers=headers()); r.raise_for_status(); return r.json().get("users",[])

async def create_user(username,password,displayname,admin=False):
    uid=user_id(username); url=f"{settings.matrix_homeserver.rstrip('/')}/_synapse/admin/v2/users/{uid}"
    payload={"password":password,"displayname":displayname,"admin":admin,"deactivated":False}
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.put(url,headers={**headers(),"Content-Type":"application/json"},json=payload); r.raise_for_status(); return r.json() if r.content else {}

async def set_password(uid,password):
    url=f"{settings.matrix_homeserver.rstrip('/')}/_synapse/admin/v2/users/{uid}"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.put(url,headers={**headers(),"Content-Type":"application/json"},json={"password":password}); r.raise_for_status()

async def set_deactivated(uid,value):
    url=f"{settings.matrix_homeserver.rstrip('/')}/_synapse/admin/v2/users/{uid}"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.put(url,headers={**headers(),"Content-Type":"application/json"},json={"deactivated":value}); r.raise_for_status()

async def invite_to_global(uid):
    if not settings.global_room_id: raise RuntimeError("GLOBAL_ROOM_ID is not configured")
    url=f"{settings.matrix_homeserver.rstrip('/')}/_matrix/client/v3/rooms/{settings.global_room_id}/invite"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.post(url,headers={**headers(),"Content-Type":"application/json"},json={"user_id":uid})
        if r.status_code not in (200,201,202,403): r.raise_for_status()
