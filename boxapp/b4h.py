"""
Minimal synchronous client for the MegCube B4H box WebAPI.

Login flow:
  1. GET  /auth/login/challenge?username=...  -> session_id, salt, challenge
  2. POST /auth/login  {session_id, username, password=sha256(pwd+salt+challenge)}
  3. Every request sends the header  Cookie: sessionID=<session_id>
The box drops idle sessions after ~30 s (code 512); call() then logs in again and retries once.
The box can't run two API queries at once on a session (code 1073741825 "general"),
so call() sends them one at a time (a lock shared by all threads of this process).
"""
import hashlib
import json
import logging
import threading
from typing import Any

import httpx

log = logging.getLogger("b4h")

SESSION_LOST = 512


class B4HError(RuntimeError):
    def __init__(self, code: Any, message: Any, path: str):
        super().__init__(f"{path}: code={code} message={message}")
        self.code, self.message, self.path = code, message, path


class B4HClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.username = username
        self.password = password
        self.session_id: str | None = None
        self._http = httpx.Client(base_url=base_url.rstrip("/"), verify=False, timeout=15)
        self._login_lock = threading.Lock()
        self._call_lock = threading.Lock()

    @staticmethod
    def _json(r: httpx.Response, path: str) -> dict:
        """Parse a login reply; a non-JSON answer means B4H_BASE_URL points at something that is not the box."""
        try:
            return r.json()
        except ValueError:
            raise B4HError(r.status_code, f"non-JSON reply (HTTP {r.status_code}); check B4H_BASE_URL", path)

    def login(self) -> None:
        with self._login_lock:
            body = self._json(self._http.get("/auth/login/challenge", params={"username": self.username}),
                              "/auth/login/challenge")
            if body.get("code") != 0:
                raise B4HError(body.get("code"), body.get("message"), "/auth/login/challenge")
            d = body["data"]
            pwd_hash = hashlib.sha256((self.password + d["salt"] + d["challenge"]).encode()).hexdigest()
            body = self._json(self._http.post(
                "/auth/login",
                json={"session_id": d["session_id"], "username": self.username, "password": pwd_hash},
                headers={"Cookie": f"sessionID={d['session_id']}"},
            ), "/auth/login")
            if body.get("code") != 0:
                # careful: 5 wrong passwords in a row locks the account on the box
                raise B4HError(body.get("code"), body.get("message"), "/auth/login")
            self.session_id = (body.get("data") or {}).get("session_id", d["session_id"])
            log.info("B4H login OK")

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Cookie": f"sessionID={self.session_id}"}

    def call(self, method: str, path: str, body: Any = None, _retry: bool = True) -> Any:
        """Call a WebAPI endpoint. Returns `data` on code 0, raises B4HError otherwise."""
        if not self.session_id:
            self.login()
        with self._call_lock:
            r = self._http.request(
                method, path, headers=self._headers(),
                content=json.dumps(body) if body is not None else None,
            )
        try:
            payload = r.json()
        except ValueError:
            raise B4HError(r.status_code, f"non-JSON reply (HTTP {r.status_code})", path)
        if payload.get("code") == SESSION_LOST and _retry:
            self.login()
            return self.call(method, path, body, _retry=False)
        if payload.get("code") != 0:
            raise B4HError(payload.get("code"), payload.get("message"), path)
        return payload.get("data")

    def get_bytes(self, path: str, params: dict | None = None, _retry: bool = True) -> tuple[bytes, str]:
        """Binary download, e.g. a record image. Logs in again once if the session expired."""
        if not self.session_id:
            self.login()
        r = self._http.get(path, params=params, headers=self._headers())
        ctype = r.headers.get("content-type", "application/octet-stream")
        if _retry and (r.status_code in (401, 403) or self._is_session_lost(r, ctype)):
            self.login()
            return self.get_bytes(path, params, _retry=False)
        r.raise_for_status()
        return r.content, ctype

    @staticmethod
    def _is_session_lost(r: httpx.Response, ctype: str) -> bool:
        if "json" not in ctype:
            return False
        try:
            return r.json().get("code") == SESSION_LOST
        except ValueError:
            return False

    def close(self) -> None:
        self._http.close()
