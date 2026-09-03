from urllib.parse import quote
import httpx
from .config import settings

class MatrixError(RuntimeError):
    pass

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
            raise MatrixError(f"HTTP {r.status_code}: {body}")
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

    def create_user(self, user_id, password, displayname="", admin=False):
        try:
            payload = {
                "password": password,
                "displayname": displayname,
                "admin": admin,
                "deactivated": False
            }
            self._request("PUT", f"/_synapse/admin/v2/users/{quote(user_id, safe='')}", json=payload)

            # Приглашаем нового пользователя в глобальную комнату
            # Если администратор не является участником комнаты, нужно использовать сервисного бота
            inviter_user_id = f"@{settings.admin_username}:{settings.matrix_server_name}"

            # Проверяем, что пользователь создан
            user_data = self.get_user(user_id)
            if user_data.get("name") == user_id:
                # Добавляем задержку, чтобы пользователь успел создаться
                import time
                time.sleep(1)

                # Отправляем приглашение
                self.invite_to_room(settings.global_room_id, user_id, inviter_user_id)

        except Exception as e:
            # Логируем ошибку, но не прерываем создание пользователя
            print(f"Warning: Could not invite user {user_id} to global room: {e}")
            # Можно также перевыбросить исключение, если это критично
            raise MatrixError(f"Failed to create user and invite to room: {e}")

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
