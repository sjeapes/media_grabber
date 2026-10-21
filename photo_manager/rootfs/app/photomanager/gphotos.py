"""Google Photos upload (Library API, 'append only' access: it can add photos, never read,
change or delete anything in your Google Photos).

Sign-in is a one-off copy-and-paste step, because Google doesn't allow this permission on
devices without a browser: you open a Google link, approve, and paste back the address of the
page you land on. The refresh token is kept in the add-on's private data folder.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("photomanager.google")

SCOPE = "https://www.googleapis.com/auth/photoslibrary.appendonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
REDIRECT = "http://localhost"
MAX_UPLOAD = 190 * 1024 * 1024


class GoogleError(Exception):
    """kind: 'auth' (sign in again), 'rate' (quota/limits — stop for now) or 'error'."""

    def __init__(self, msg, kind="error"):
        super().__init__(msg)
        self.kind = kind


def http(method: str, url: str, headers=None, data: bytes | None = None, timeout=300):
    """(status, body). Network failures raise GoogleError. Replaced in tests."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(f"Couldn't reach Google: {exc}") from None


def _json(body: bytes) -> dict:
    try:
        v = json.loads(body or b"{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


class GooglePhotos:
    def __init__(self, cfg):
        self.cfg = cfg
        self.file = cfg.data_dir / "google.json"
        self._state = None
        self._lock = threading.Lock()
        self._access = None            # (token, expires_at)

    # --- credentials ---------------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return str(self.cfg.options.get("google_client_id") or "").strip()

    @property
    def client_secret(self) -> str:
        return str(self.cfg.options.get("google_client_secret") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _load(self) -> dict:
        try:
            v = json.loads(self.file.read_text())
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict):
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.file)

    @property
    def connected(self) -> bool:
        d = self._load()
        return bool(d.get("refresh_token")) and d.get("client_id") == self.client_id

    def status(self) -> dict:
        d = self._load()
        return {"configured": self.configured, "connected": self.connected,
                "connected_at": d.get("connected_at") if self.connected else None,
                "needs_reconnect": bool(d.get("needs_reconnect")) and self.connected}

    # --- signing in ------------------------------------------------------------------------
    def auth_url(self) -> str:
        if not self.configured:
            raise GoogleError("Add your Google client ID and secret in the add-on's Configuration first")
        self._state = secrets.token_urlsafe(16)
        q = {"client_id": self.client_id, "redirect_uri": REDIRECT, "response_type": "code",
             "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": self._state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def connect(self, pasted: str):
        """pasted: the address of the page Google sent you to (or just its code=… value)."""
        if not self.configured:
            raise GoogleError("Add your Google client ID and secret in the add-on's Configuration first")
        pasted = (pasted or "").strip()
        if not pasted:
            raise GoogleError("Paste the address of the page you ended up on")
        if "://" in pasted or "code=" in pasted or "error=" in pasted:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query
                                      if "://" in pasted else pasted.lstrip("?"))
            if q.get("error"):
                raise GoogleError(f"Google said: {q['error'][0]}")
            code = (q.get("code") or [""])[0]
            state = (q.get("state") or [""])[0]
            if not code:
                raise GoogleError("That address doesn't contain a sign-in code")
            if not self._state or state != self._state:
                raise GoogleError("That address is from a different sign-in attempt — press "
                                  "“Connect Google Photos” again and use the new link")
        else:
            code = pasted
        status, body = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                            urllib.parse.urlencode({
                                "grant_type": "authorization_code", "code": code,
                                "redirect_uri": REDIRECT, "client_id": self.client_id,
                                "client_secret": self.client_secret}).encode())
        res = _json(body)
        if status != 200 or not res.get("refresh_token"):
            why = res.get("error_description") or res.get("error") or f"HTTP {status}"
            raise GoogleError(f"Google didn't accept that sign-in ({why}). Start again with a new link.")
        if SCOPE not in (res.get("scope") or "").split():
            raise GoogleError("Google Photos permission wasn't granted — tick it when approving")
        self._state = None
        self._access = (res.get("access_token"), time.time() + int(res.get("expires_in", 3000)) - 60)
        self._save({"refresh_token": res["refresh_token"], "client_id": self.client_id,
                    "connected_at": time.time()})

    def disconnect(self):
        self._access = None
        try:
            self.file.unlink()
        except OSError:
            pass

    def _token(self, force=False) -> str:
        with self._lock:
            if not force and self._access and self._access[1] > time.time() and self._access[0]:
                return self._access[0]
            d = self._load()
            if not d.get("refresh_token") or d.get("client_id") != self.client_id:
                raise GoogleError("Google Photos isn't connected", "auth")
            status, body = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                urllib.parse.urlencode({
                                    "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                    "client_id": self.client_id, "client_secret": self.client_secret}).encode())
            res = _json(body)
            if status != 200 or not res.get("access_token"):
                if res.get("error") == "invalid_grant":
                    d["needs_reconnect"] = True
                    self._save(d)
                    raise GoogleError("Google sign-in has expired — connect Google Photos again", "auth")
                raise GoogleError(f"Couldn't refresh the Google sign-in ({res.get('error') or status})",
                                  "rate" if status == 429 else "error")
            if d.pop("needs_reconnect", None):
                self._save(d)
            self._access = (res["access_token"], time.time() + int(res.get("expires_in", 3000)) - 60)
            return self._access[0]

    # --- uploading -----------------------------------------------------------------------------
    def _call(self, method, url, headers, data):
        for attempt in (1, 2):
            h = {"Authorization": f"Bearer {self._token(force=attempt == 2)}", **headers}
            status, body = http(method, url, h, data)
            if status == 401 and attempt == 1:
                continue
            break
        if status == 401:
            raise GoogleError("Google refused the sign-in — connect Google Photos again", "auth")
        if status == 403:
            msg = (_json(body).get("error") or {}).get("message") or "permission denied"
            raise GoogleError(f"Google refused the upload: {msg}", "auth")
        if status == 429:
            raise GoogleError("Google's upload limit was reached — it will try again later", "rate")
        if status >= 400:
            msg = (_json(body).get("error") or {}).get("message") or f"HTTP {status}"
            raise GoogleError(f"Google said: {msg}")
        return body

    def upload(self, path: Path, name: str) -> str:
        """Add one JPEG to Google Photos (no album). Returns the new media item's id."""
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise GoogleError("The JPEG is too big for Google Photos (200 MB limit)")
        data = Path(path).read_bytes()
        token = self._call("POST", UPLOAD_URL, {
            "Content-Type": "application/octet-stream", "X-Goog-Upload-Content-Type": "image/jpeg",
            "X-Goog-Upload-Protocol": "raw"}, data).decode("ascii", "ignore").strip()
        if not token:
            raise GoogleError("Google didn't accept the file")
        body = self._call("POST", CREATE_URL, {"Content-Type": "application/json"}, json.dumps({
            "newMediaItems": [{"simpleMediaItem": {"fileName": name, "uploadToken": token}}]}).encode())
        results = _json(body).get("newMediaItemResults") or [{}]
        item = results[0].get("mediaItem")
        if not item or not item.get("id"):
            msg = (results[0].get("status") or {}).get("message") or "no result"
            raise GoogleError(f"Google couldn't add the photo: {msg}")
        return item["id"]
