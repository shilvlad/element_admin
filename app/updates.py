from datetime import datetime, timezone
from threading import Lock
import time

import httpx
from packaging.version import Version

from .matrix import MatrixClient

RELEASE_API = "https://api.github.com/repos/element-hq/synapse/releases/latest"
RELEASE_PAGE = "https://github.com/element-hq/synapse/releases"
_cache = {}
_lock = Lock()


def latest_release():
    # Only the public GitHub request is cached. Never send the Matrix token there.
    with _lock:
        now = time.monotonic()
        if now < _cache.get("expires", 0):
            return dict(_cache["result"])
        previous = _cache.get("result", {})
        try:
            response = httpx.get(RELEASE_API, timeout=4.0, headers={
                "Accept": "application/vnd.github+json", "User-Agent": "element-admin"})
            response.raise_for_status()
            data = response.json()
            tag = data["tag_name"]
            version = Version(tag)
            if data.get("prerelease") or data.get("draft") or version.is_prerelease or version.is_devrelease:
                raise ValueError("Expected a stable release")
            result = dict(latest=str(version), release_url=RELEASE_PAGE + "/tag/" + tag,
                          checked_at=int(datetime.now(timezone.utc).timestamp() * 1000), release_error=False)
            ttl = 3600
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            result = dict(previous, release_error=True)
            ttl = 300
        _cache.update(result=result, expires=now + ttl)
        return dict(result)


def synapse_update_status():
    result = latest_release()
    result.update(current=None, update_available=None, version_error=False)
    try:
        raw = MatrixClient().server_version()
        # Synapse can append build metadata: "1.99.0rc1 (b=develop, abcdef)".
        current = Version(raw.split()[0])
        result["current"] = str(current)
    except Exception:
        result["version_error"] = True
        return result
    if result.get("latest") and not result.get("release_error"):
        result["update_available"] = current < Version(result["latest"])
    return result
