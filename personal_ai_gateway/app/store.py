"""Settings, providers and routes: persisted in /data/gateway.json, merged with env/add-on options."""
import fnmatch
import json
import os
import re
import secrets
import shutil
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1"},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1"},
    "cerebras": {"label": "Cerebras", "base_url": "https://api.cerebras.ai/v1"},
    "nvidia": {"label": "NVIDIA NIM", "base_url": "https://integrate.api.nvidia.com/v1"},
    "together": {"label": "Together AI", "base_url": "https://api.together.xyz/v1"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    "anthropic": {"label": "Anthropic (OpenAI-compat layer)", "base_url": "https://api.anthropic.com/v1"},
}

DEFAULT_SETTINGS = {
    "rotation": "balanced",          # balanced | sequential
    "cooldowns": [10, 30, 60, 120],  # seconds, escalating per consecutive failure
    "timeout_s": 120,
    "ollama_url": "http://host.docker.internal:11434",
    "ollama_preload": "",
}

BUNDLED_URL = "http://127.0.0.1:11434"
LEGACY_ADDON_URL = "http://172.30.32.1:11434"  # old add-on default, ignored when Ollama is built in
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def load_addon_options() -> dict:
    p = DATA_DIR / "options.json"
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except ValueError:
        return {}


class Store:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = DATA_DIR / "gateway.json"
        self.options = load_addon_options()
        self.data = {"proxy_key": "", "settings": {}, "providers": {}, "routes": {}}
        if self.path.is_file():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except ValueError:
                pass
        self.env_keys = self._scan_env_keys()
        self._bootstrap()

    # -- bootstrap from env / add-on options
    def _scan_env_keys(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for name, value in sorted(os.environ.items()):
            m = re.fullmatch(r"([A-Z0-9]+)_API_KEY(?:_\d+)?", name)
            if m and value.strip() and m.group(1) not in ("PROXY", "ADMIN", "HA"):
                found.setdefault(m.group(1).lower(), []).append(value.strip())
        for item in self.options.get("api_keys") or []:
            prov, key = str(item.get("provider", "")).strip().lower(), str(item.get("key", "")).strip()
            if prov and key:
                found.setdefault(prov, []).append(key)
        return found

    def _opt(self, key: str, env: str, default: str = "") -> str:
        return str(os.environ.get(env) or self.options.get(key) or default).strip()

    def _bootstrap(self) -> None:
        if not self.data.get("proxy_key"):
            self.data["proxy_key"] = secrets.token_urlsafe(24)
            self.save()
        for name in self.env_keys:
            if name in PRESETS and name not in self.data["providers"]:
                self.data["providers"][name] = {"kind": "openai", "base_url": PRESETS[name]["base_url"],
                                                "keys": [], "enabled": True}
        if "ollama" not in self.data["providers"]:
            self.data["providers"]["ollama"] = {"kind": "ollama", "base_url": "", "keys": [], "enabled": True}

    # -- values
    @property
    def proxy_key(self) -> str:
        return self._opt("proxy_key", "PROXY_API_KEY") or self.data["proxy_key"]

    @property
    def admin_password(self) -> str:
        return self._opt("admin_password", "ADMIN_PASSWORD") or self.proxy_key

    def setting(self, key: str):
        """Environment variable > value saved in the page > add-on option > default."""
        env = {"ollama_url": "OLLAMA_URL", "ollama_preload": "OLLAMA_PRELOAD"}.get(key)
        if env and os.environ.get(env, "").strip():
            return os.environ[env].strip()
        if key in self.data["settings"]:
            return self.data["settings"][key]
        opt = str(self.options.get(key) or "").strip() if key in ("ollama_url", "ollama_preload") else ""
        if key == "ollama_url" and self.bundled_enabled() and opt in ("", LEGACY_ADDON_URL):
            return BUNDLED_URL
        if opt:
            return opt
        if key == "ollama_url" and os.environ.get("OLLAMA_DEFAULT_URL", "").strip():
            return os.environ["OLLAMA_DEFAULT_URL"].strip()  # a starting point; the page can still change it
        return DEFAULT_SETTINGS[key]

    def bundled_enabled(self) -> bool:
        """True in the add-on image, where Ollama is installed and the option isn't switched off."""
        if not shutil.which("ollama") or os.environ.get("BUILTIN_OLLAMA", "").strip() == "0":
            return False
        return self.options.get("builtin_ollama", True) is not False

    def settings(self) -> dict:
        return {k: self.setting(k) for k in DEFAULT_SETTINGS}

    def set_settings(self, new: dict) -> None:
        s = self.data["settings"]
        if new.get("rotation") in ("balanced", "sequential"):
            s["rotation"] = new["rotation"]
        if isinstance(new.get("cooldowns"), list):
            vals = [max(1, min(int(float(x)), 3600)) for x in new["cooldowns"] if str(x).strip()]
            if vals:
                s["cooldowns"] = vals[:8]
        if "timeout_s" in new:
            s["timeout_s"] = max(10, min(int(float(new["timeout_s"])), 1800))
        for k in ("ollama_url", "ollama_preload"):
            if k in new:
                s[k] = str(new[k]).strip()
        self.save()

    def ollama_url(self) -> str:
        return str(self.setting("ollama_url")).rstrip("/")

    # -- providers
    def providers(self) -> dict:
        return self.data["providers"]

    def keys(self, name: str) -> list[str]:
        p = self.data["providers"].get(name) or {}
        return list(dict.fromkeys((p.get("keys") or []) + self.env_keys.get(name, [])))

    def base_url(self, name: str) -> str:
        p = self.data["providers"][name]
        if p["kind"] == "ollama":
            return (p.get("base_url") or self.ollama_url()).rstrip("/") + "/v1"
        return p["base_url"].rstrip("/")

    def upsert_provider(self, name: str, spec: dict) -> None:
        name = name.strip().lower()
        if not NAME_RE.fullmatch(name):
            raise ValueError("Name: lowercase letters, digits, - and _ (max 32).")
        kind = spec.get("kind", "openai")
        if kind not in ("openai", "ollama", "http"):
            raise ValueError("Unknown provider kind.")
        cur = self.data["providers"].get(name, {})
        mode = spec.get("mode", cur.get("mode", ""))
        p = {"kind": kind, "enabled": spec.get("enabled", cur.get("enabled", True)),
             "mode": mode if mode in ("", "balanced", "sequential") else "",
             "keys": cur.get("keys", []), "ignore": spec.get("ignore", cur.get("ignore", [])),
             "whitelist": spec.get("whitelist", cur.get("whitelist", [])),
             "models": [str(m).strip() for m in spec.get("models", cur.get("models", [])) if str(m).strip()]}
        if kind == "openai":
            url = str(spec.get("base_url") or PRESETS.get(name, {}).get("base_url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("Base URL must start with http:// or https://")
            p["base_url"] = url
        elif kind == "ollama":
            p["base_url"] = str(spec.get("base_url") or "").strip()
        else:
            r = spec.get("recipe") or {}
            if not str(r.get("url", "")).startswith(("http://", "https://")):
                raise ValueError("Recipe URL must start with http:// or https://")
            p["recipe"] = r
        self.data["providers"][name] = p
        self.save()

    def delete_provider(self, name: str) -> None:
        if name == "ollama":
            raise ValueError("The local Ollama provider can't be removed (disable it instead).")
        self.data["providers"].pop(name, None)
        self.save()

    def add_key(self, name: str, key: str) -> None:
        p = self.data["providers"][name]
        if key and key not in p["keys"]:
            p["keys"].append(key)
            self.save()

    def remove_key(self, name: str, key_id: str) -> bool:
        from rotator import kid
        p = self.data["providers"][name]
        before = len(p["keys"])
        p["keys"] = [k for k in p["keys"] if kid(k) != key_id]
        self.save()
        return len(p["keys"]) < before

    def allowed(self, name: str, model: str) -> bool:
        p = self.data["providers"].get(name, {})
        if any(fnmatch.fnmatch(model, pat) for pat in p.get("whitelist") or []):
            return True
        if p.get("whitelist") and not p.get("ignore"):
            return False
        return not any(fnmatch.fnmatch(model, pat) for pat in p.get("ignore") or [])

    # -- routes (aliases with fallback chains)
    def set_routes(self, routes: dict) -> None:
        clean = {}
        for k, v in routes.items():
            k = str(k).strip()
            chain = [str(x).strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if str(x).strip()]
            if k and chain:
                clean[k] = chain
        self.data["routes"] = clean
        self.save()

    def regenerate_proxy_key(self) -> str:
        self.data["proxy_key"] = secrets.token_urlsafe(24)
        self.save()
        return self.data["proxy_key"]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
