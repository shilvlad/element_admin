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

    def whoami(self):
        return self._request("GET", "/_matrix/client/v3/account/whoami").json()["user_id"]

    def create_notification_room(self, recipient):
        return self._request("POST", "/_matrix/client/v3/createRoom", json={
            "preset": "private_chat", "is_direct": True, "invite": [recipient],
            "name": "Заявки element-admin",
            "power_level_content_override": {"invite": 100, "state_default": 100},
        }).json()["room_id"]

    def send_notification(self, room_id, transaction_id, body):
        return self._request("PUT", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{quote(transaction_id, safe='')}",
                             json={"msgtype": "m.text", "body": body}).json()

    def health(self):
        r = httpx.get(self.base + "/_matrix/client/versions", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def admin_health(self):
        r = self._request("GET", "/_synapse/admin/v2/users?limit=1")
        return r.json()

    def get_user(self, user_id):
        return self._request("GET", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}").json()

    def list_users(self, limit=1000, include_deactivated=False):
        users = []
        params = {"limit": limit, "deactivated": str(include_deactivated).lower()}
        seen_tokens = set()
        while True:
            data = self._request("GET", "/_synapse/admin/v2/users", params=params).json()
            users.extend(data.get("users", []))
            token = data.get("next_token")
            if token is None:
                return {"users": users, "total": len(users)}
            token = str(token)
            if token in seen_tokens:
                raise MatrixError("Synapse вернул повторный токен страницы пользователей")
            seen_tokens.add(token)
            params["from"] = token

    def user_exists(self, user_id):
        try:
            self.get_user(user_id)
            return True
        except MatrixError as exc:
            if exc.status_code == 404:
                return False
            raise

    def create_user(self, user_id, password, displayname="", admin=False, email=None):
        # Synapse's PUT also updates users. Never call it for a known account.
        if self.user_exists(user_id):
            raise MatrixError("Логин уже занят. Существующая учётная запись не изменена.")
        payload = {"password": password, "displayname": displayname, "admin": admin, "deactivated": False}
        if email:
            payload["threepids"] = [{"medium": "email", "address": email}]
        self._request("PUT", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}", json=payload)

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

    def user_devices(self, user_id):
        return self._request("GET", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}/devices").json()

    def user_rooms(self, user_id):
        return self._request("GET", f"/_synapse/admin/v1/users/{quote(user_id, safe='')}/joined_rooms").json()

    def user_sessions(self, user_id):
        return self._request("GET", f"/_synapse/admin/v1/whois/{quote(user_id, safe='')}").json()

    def list_rooms(self, offset="0", search=""):
        params = {"from": offset, "limit": 50}
        if search.strip():
            params["search_term"] = search.strip()
        return self._request("GET", "/_synapse/admin/v1/rooms", params=params).json()

    def get_room(self, room_id):
        return self._request("GET", f"/_synapse/admin/v1/rooms/{quote(room_id, safe='')}").json()

    def room_members(self, room_id):
        return self._request("GET", f"/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/members").json()

    def room_media(self, room_id):
        return self._request("GET", f"/_synapse/admin/v1/room/{quote(room_id, safe='')}/media").json()

    def media_info(self, origin, media_id):
        return self._request("GET", f"/_synapse/admin/v1/media/{quote(origin, safe='')}/{quote(media_id, safe='')}").json()
