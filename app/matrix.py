from urllib.parse import quote
import httpx
from .config import settings

class MatrixError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code

class MatrixClient:
    def __init__(self):
        self.base = settings.matrix_homeserver.rstrip("/")
        self.headers = {"Authorization": f"Bearer {settings.matrix_admin_token}"}
        self.timeout = settings.matrix_timeout

    def _request(self, method, path, **kwargs):
        try:
            r = httpx.request(method, self.base + path, headers=self.headers, timeout=self.timeout, **kwargs)
        except httpx.HTTPError as e:
            raise MatrixError(f"connection error: {e}") from e
        if r.status_code >= 400:
            try: body = r.json()
            except Exception: body = r.text[:500]
            raise MatrixError(f"HTTP {r.status_code}: {body}", r.status_code)
        return r

    def health(self):
        r = httpx.get(self.base + "/_matrix/client/versions", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def admin_health(self):
        r = self._request("GET", "/_synapse/admin/v2/users?limit=1")
        return r.json()

    def get_user(self, user_id):
        return self._request("GET", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}").json()

    def list_users(self, limit=1000):
        return self._request("GET", f"/_synapse/admin/v2/users?limit={limit}").json()

    def user_exists(self, user_id):
        try:
            self.get_user(user_id)
            return True
        except MatrixError as exc:
            if exc.status_code == 404:
                return False
            raise

    def create_user(self, user_id, password, displayname="", admin=False):
        # Synapse's PUT also updates users. Never call it for a known account.
        if self.user_exists(user_id):
            raise MatrixError("Логин уже занят. Существующая учётная запись не изменена.")
        self._request("PUT", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}", json={
            "password": password,
            "displayname": displayname,
            "admin": admin,
            "deactivated": False,
        })

    def reset_password(self, user_id, password):
        self._request("POST", f"/_synapse/admin/v1/reset_password/{quote(user_id, safe='')}",
                      json={"new_password": password, "logout_devices": True})

    def update_user(self, user_id, **payload):
        self._request("PUT", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}", json=payload)

    def invite_to_room(self, room_id, user_id, inviter_user_id):
        # Requires a valid Matrix client access token for the inviter. The current admin token
        # may be accepted by Synapse only if it is also a normal client token.
        url = self.base + f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/invite"
        body = {"user_id": user_id}
        try:
            r = httpx.post(url, headers=self.headers, json=body, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise MatrixError(f"invite connection error: {e}") from e
        if r.status_code >= 400:
            try: detail = r.json()
            except Exception: detail = r.text[:500]
            raise MatrixError(f"invite HTTP {r.status_code}: {detail}")

    def room_health(self):
        # Admin API does not expose a generic "room exists" check with this token.
        # A real client token is required for membership/invite operations.
        return {"room_id": settings.global_room_id, "note": "membership check requires client-capable token"}
