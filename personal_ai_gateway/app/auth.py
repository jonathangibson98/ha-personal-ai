"""Sign-in for the gateway page: a session cookie instead of typing the password every time.

* POST /api/login {password, remember} checks the admin password (the proxy key unless an
  ADMIN_PASSWORD is set) and sets an HttpOnly cookie. "remember" keeps it 90 days and renews it
  while you use it; otherwise it lasts until the browser closes (and at most 12 hours).
* Only a hash of each session token is stored (sessions.json), tied to the current password:
  changing the admin password or the proxy key it falls back to signs every browser out.
* Repeated wrong passwords from one address are slowed down (5 tries per 5 minutes).
"""
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

COOKIE = "gw_session"
REMEMBER_S = 90 * 86400
SHORT_S = 12 * 3600
RENEW_BELOW_S = 60 * 86400
MAX_TRIES, TRY_WINDOW_S = 5, 300


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Auth:
    def __init__(self, data_dir: Path, password):
        self.path = Path(data_dir) / "sessions.json"
        self.password = password          # callable: the current admin password
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}
        try:
            self.sessions = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.sessions = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sessions))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def _pw_tag(self) -> str:
        return _h("pw:" + (self.password() or ""))[:16]

    # -- brute-force brake
    def too_many(self, who: str) -> bool:
        now = time.time()
        tries = [t for t in self.fails.get(who, []) if now - t < TRY_WINDOW_S]
        self.fails[who] = tries
        return len(tries) >= MAX_TRIES

    def failed(self, who: str) -> None:
        self.fails.setdefault(who, []).append(time.time())

    # -- sessions
    def create(self, remember: bool, agent: str) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        life = REMEMBER_S if remember else SHORT_S
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v.get("exp", 0) > now}
            self.sessions[_h(token)] = {"exp": now + life, "remember": remember, "pw": self._pw_tag(),
                                        "created": now, "seen": now, "agent": agent[:120]}
            self._save()
        return token, life

    def check(self, token: str) -> dict | None:
        """The session for this cookie, or None. Renews remembered sessions that are being used."""
        if not token:
            return None
        key = _h(token)
        with self.lock:
            s = self.sessions.get(key)
            now = time.time()
            if not s or s.get("exp", 0) < now or s.get("pw") != self._pw_tag():
                return None
            changed = now - s.get("seen", 0) > 3600
            s["seen"] = now
            if s.get("remember") and s["exp"] - now < RENEW_BELOW_S:
                s["exp"], changed = now + REMEMBER_S, True
            if changed:
                self._save()
            return s

    def drop(self, token: str) -> None:
        with self.lock:
            if self.sessions.pop(_h(token or ""), None) is not None:
                self._save()

    def drop_all(self) -> int:
        with self.lock:
            n = len(self.sessions)
            self.sessions = {}
            self._save()
            return n

    def count(self) -> int:
        now = time.time()
        return sum(1 for v in self.sessions.values() if v.get("exp", 0) > now and v.get("pw") == self._pw_tag())
