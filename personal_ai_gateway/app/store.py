"""Settings, providers and routes: persisted in /data/gateway.json, merged with env/add-on options."""
import fnmatch
import json
import os
import re
import secrets
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
    "ollama_url": "",                # optional: an Ollama you run somewhere. Empty = no Ollama
    "ollama_preload": "",
    "public_mode": "off",            # off | quick | token | url  (HTTPS address for the MCP servers)
    "public_url": "",                # your own https:// address (url mode, or the hostname of a token tunnel)
    "cf_token": "",                  # Cloudflare tunnel token (token mode)
}
SECRET_SETTINGS = ("cf_token",)
# Old defaults from when Ollama was built in or assumed; they mean "no Ollama" now.
OLD_OLLAMA_DEFAULTS = ("http://127.0.0.1:11434", "http://172.30.32.1:11434", "http://host.docker.internal:11434",
                       "http://localhost:11434")

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
        self.data = {"proxy_key": "", "settings": {}, "providers": {}, "routes": {}, "mcp": {"servers": {}, "ai_server": ""}}
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
        if not self.data.get("openwebui_key"):
            self.data["openwebui_key"] = "owui-" + secrets.token_urlsafe(24)
            self.save()
        saved = self.data.get("settings", {})
        if str(saved.get("ollama_url", "")).rstrip("/") in OLD_OLLAMA_DEFAULTS:
            saved.pop("ollama_url", None)  # Ollama isn't built in any more
            self.save()
        prov = self.data["providers"].get("ollama")
        if self.ollama_configured():
            if not prov:
                self.data["providers"]["ollama"] = {"kind": "ollama", "base_url": "", "keys": [], "enabled": True}
        elif prov and not prov.get("base_url"):
            del self.data["providers"]["ollama"]  # nothing to point it at
            self.save()

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
        if key == "ollama_url" and opt.rstrip("/") in OLD_OLLAMA_DEFAULTS:
            opt = ""
        if opt:
            return opt
        if key == "ollama_url" and os.environ.get("OLLAMA_DEFAULT_URL", "").strip():
            return os.environ["OLLAMA_DEFAULT_URL"].strip()  # a starting point; the page can still change it
        return DEFAULT_SETTINGS[key]

    def bundled_enabled(self) -> bool:
        """Ollama used to be built in. It isn't any more; use your own Ollama if you want one."""
        return False

    def ollama_configured(self) -> bool:
        return bool(self.ollama_url())

    def settings(self) -> dict:
        out = {k: self.setting(k) for k in DEFAULT_SETTINGS}
        for k in SECRET_SETTINGS:
            out[k + "_set"] = bool(out.pop(k))
        return out

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
        if "ollama_url" in new:
            url = s["ollama_url"]
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("The Ollama address must start with http:// or https://")
            if url and "ollama" not in self.data["providers"]:
                self.data["providers"]["ollama"] = {"kind": "ollama", "base_url": "", "keys": [], "enabled": True}
            if not url and not (self.data["providers"].get("ollama") or {}).get("base_url"):
                self.data["providers"].pop("ollama", None)
        if new.get("public_mode") in ("off", "quick", "token", "url"):
            s["public_mode"] = new["public_mode"]
        if "public_url" in new:
            u = str(new["public_url"] or "").strip().rstrip("/")
            if u and not u.startswith("http"):
                u = "https://" + u
            if u.startswith("http://"):
                raise ValueError("The public address must be https:// (that's the point of it).")
            s["public_url"] = u
        if new.get("cf_token"):
            s["cf_token"] = str(new["cf_token"]).strip()
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
        if kind not in ("openai", "ollama", "http", "cli"):
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
        elif kind == "cli":
            p["cli"] = cur.get("cli") or str(spec.get("cli") or "")
            if not p["cli"]:
                raise ValueError("Account providers are added from Providers > Sign in with an account.")
        else:
            r = spec.get("recipe") or {}
            if not str(r.get("url", "")).startswith(("http://", "https://")):
                raise ValueError("Recipe URL must start with http:// or https://")
            p["recipe"] = r
        self.data["providers"][name] = p
        self.save()

    def delete_provider(self, name: str) -> None:
        if name == "ollama" and self.ollama_configured():
            raise ValueError("Clear the Ollama address in Settings first (or disable this provider).")
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

    def mcp_ai_servers(self) -> list[dict]:
        """The enabled MCP servers the signed-in AIs may use: [{name, key, label}], names unique and tool-safe."""
        m = self.data.get("mcp") or {}
        ids = m.get("ai_servers") if "ai_servers" in m else ([m["ai_server"]] if m.get("ai_server") else [])
        out, used = [], set()
        for sid in ids:
            srv = (m.get("servers") or {}).get(sid)
            if not srv or not srv.get("enabled", True):
                continue
            kind = srv.get("kind", "ha")
            base = "home_assistant" if kind == "ha" and "home_assistant" not in used else \
                (re.sub(r"[^a-z0-9]+", "_", srv["name"].lower()).strip("_") or kind)[:30]
            name, n = base, 2
            while name in used:
                name, n = f"{base}_{n}", n + 1
            used.add(name)
            label = {"ha": "Home Assistant devices", "computer": "the computer", "command": "tools",
                     "remote": "tools"}.get(kind, "tools")
            out.append({"name": name, "key": srv["key"], "kind": kind, "label": f"{label} '{srv['name']}'"})
        return out

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
