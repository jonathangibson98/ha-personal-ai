"""API key pool: rotation, escalating cooldowns, lockouts and usage counters."""
import hashlib
import json
import time
from pathlib import Path

DEFAULT_COOLDOWNS = (10, 30, 60, 120)
LOCK_SECONDS = 3600  # a key the provider rejected (401/403) rests this long


def kid(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def mask(key: str) -> str:
    if not key:
        return "(no key)"
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "••••"


class KeyPool:
    def __init__(self, path: Path | None = None, cooldowns=DEFAULT_COOLDOWNS):
        self.path = path
        self.cooldowns = tuple(cooldowns) or DEFAULT_COOLDOWNS
        self.state: dict[str, dict] = {}
        self.dirty = False
        if path and path.is_file():
            try:
                for k, v in json.loads(path.read_text()).items():
                    self.state[k] = {**self._blank(), **{x: v[x] for x in ("used", "errors", "tokens_in", "tokens_out", "last") if x in v}}
            except (ValueError, OSError, AttributeError):
                pass

    @staticmethod
    def _blank() -> dict:
        return {"fails": 0, "until": 0.0, "locked": False, "inflight": 0, "used": 0,
                "errors": 0, "tokens_in": 0, "tokens_out": 0, "last": 0.0}

    def _s(self, provider: str, key: str) -> dict:
        return self.state.setdefault(f"{provider}:{kid(key)}", self._blank())

    # -- selection
    def available(self, provider: str, key: str, now: float | None = None) -> bool:
        return self._s(provider, key)["until"] <= (now or time.time())

    def pick(self, provider: str, keys: list[str], mode: str = "balanced",
             exclude: set[str] | None = None) -> str | None:
        """Choose a usable key, or None if every key is cooling down or already tried."""
        now = time.time()
        exclude = exclude or set()
        usable = [k for k in keys if k not in exclude and self.available(provider, k, now)]
        if not usable:
            return None
        if mode == "sequential":
            return usable[0]
        return min(usable, key=lambda k: (self._s(provider, k)["inflight"], self._s(provider, k)["used"]))

    def next_free_in(self, provider: str, keys: list[str]) -> float:
        now = time.time()
        waits = [max(0.0, self._s(provider, k)["until"] - now) for k in keys]
        return min(waits) if waits else 0.0

    # -- outcomes
    def begin(self, provider: str, key: str) -> None:
        self._s(provider, key)["inflight"] += 1

    def end(self, provider: str, key: str) -> None:
        s = self._s(provider, key)
        s["inflight"] = max(0, s["inflight"] - 1)

    def success(self, provider: str, key: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        s = self._s(provider, key)
        s.update(fails=0, until=0.0, locked=False, last=time.time())
        s["used"] += 1
        s["tokens_in"] += int(tokens_in or 0)
        s["tokens_out"] += int(tokens_out or 0)
        self.dirty = True

    def failure(self, provider: str, key: str, retry_after: float | None = None) -> float:
        s = self._s(provider, key)
        s["fails"] += 1
        s["errors"] += 1
        wait = self.cooldowns[min(s["fails"] - 1, len(self.cooldowns) - 1)]
        if retry_after:
            wait = max(wait, min(float(retry_after), 300.0))
        s["until"] = time.time() + wait
        s["last"] = time.time()
        self.dirty = True
        return wait

    def reject(self, provider: str, key: str) -> None:
        """Provider said the key is invalid or unauthorised."""
        s = self._s(provider, key)
        s.update(locked=True, until=time.time() + LOCK_SECONDS, last=time.time())
        s["errors"] += 1
        self.dirty = True

    def reset(self, provider: str, key: str) -> None:
        self._s(provider, key).update(fails=0, until=0.0, locked=False)

    # -- reporting
    def info(self, provider: str, key: str) -> dict:
        s = self._s(provider, key)
        now = time.time()
        cooling = max(0.0, s["until"] - now)
        status = "locked" if s["locked"] and cooling else ("cooling" if cooling else "ready")
        return {"id": kid(key), "masked": mask(key), "status": status, "cooldown_s": round(cooling),
                "used": s["used"], "errors": s["errors"], "busy": s["inflight"],
                "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"], "last": s["last"]}

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        self.dirty = False
        try:
            self.path.write_text(json.dumps(self.state))
        except OSError:
            pass
