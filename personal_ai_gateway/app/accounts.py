"""Sign in with your own AI account instead of an API key.

Uses each company's official command-line tool, signed in once with your normal account:
  * Claude Code  (@anthropic-ai/claude-code)  -> your Claude Pro / Max plan
  * Codex        (@openai/codex)              -> your ChatGPT plan
  * Gemini CLI   (@google/gemini-cli)         -> your Google account
The tools are installed on demand into /data/cli and run as a separate, unprivileged user
that can't read the gateway's own keys. Each chat request runs the tool once in
non-interactive mode and returns its answer as an OpenAI-style reply.
"""
import asyncio
import fcntl
import json
import logging
import os
import pwd
import re
import shutil
import signal
import struct
import tempfile
import termios
import time
import uuid
from pathlib import Path

from store import DATA_DIR

log = logging.getLogger("gateway")

CLI_DIR = DATA_DIR / "cli"
NPM_PREFIX = CLI_DIR / "npm"
HOME = CLI_DIR / "home"
WORK = CLI_DIR / "work"
CLI_USER = os.environ.get("CLI_USER", "aicli")
MCP_NAME = "home_assistant"
DEFAULT_SYSTEM = "You are a helpful assistant. Answer clearly and briefly."

ACCOUNTS = {
    "claude": {
        "label": "Claude", "plan": "Claude Pro or Max plan", "provider": "claude-account",
        "package": "@anthropic-ai/claude-code", "bin": "claude",
        "models": ["sonnet", "opus", "haiku"],
        "login": ["auth", "login", "--claudeai"], "logout": ["auth", "logout"],
    },
    "chatgpt": {
        "label": "ChatGPT", "plan": "ChatGPT Plus, Pro or Team plan (through Codex)", "provider": "chatgpt-account",
        "package": "@openai/codex", "bin": "codex",
        "models": ["default"],
        "login": ["login", "--device-auth"], "logout": ["logout"],
    },
    "gemini": {
        "label": "Gemini", "plan": "Gemini Code Assist Standard or Enterprise (work) account", "provider": "gemini-account",
        "package": "@google/gemini-cli", "bin": "gemini",
        "models": ["default", "gemini-3.5-flash", "gemini-3.1-pro-preview"],
        "notice": "Google stopped letting personal Google accounts (free or Google AI Pro/Ultra) sign in to Gemini CLI in June 2026; "
                  "they now have to use Google's Antigravity apps instead. For Home Assistant, use Gemini with a free API key: "
                  "get one at aistudio.google.com/apikey and add it under Providers as \"gemini\".",
        "login": [], "logout": None,
    },
}

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>78DEHMNOc]")
URL = re.compile(r"https://[^\s\x07\x1b\"'<>]+")


def strip_ansi(text: str) -> str:
    text = ANSI.sub("", text).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):  # keep what's left after carriage returns redraw a line
        lines.append(line.rsplit("\r", 1)[-1] if "\r" in line.rstrip("\r") else line.rstrip("\r"))
    return "\n".join(lines)


def find_links(raw: str) -> list[str]:
    links = re.findall(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)", raw)
    links += URL.findall(ANSI.sub(" ", raw))
    out = []
    for u in links:
        u = u.rstrip(".,)")
        if u not in out and len(u) > 12:
            out.append(u)
    return out[:6]


def transcript(messages: list) -> tuple[str, str]:
    """(system text, conversation text) from OpenAI chat messages."""
    def text(m):
        c = m.get("content")
        if isinstance(c, list):
            return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type", "text") == "text")
        return str(c or "")
    system = "\n\n".join(text(m) for m in messages if m.get("role") in ("system", "developer")).strip()
    turns = [m for m in messages if m.get("role") in ("user", "assistant") and text(m).strip()]
    if len(turns) == 1 and turns[0].get("role") == "user":
        return system, text(turns[0])
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {text(m)}" for m in turns]
    return system, "Conversation so far:\n\n" + "\n\n".join(lines) + "\n\nReply to the last User message as the Assistant."


class CliError(Exception):
    pass


# ------------------------------------------------------------ interactive sessions (install / sign in)

class Session:
    """A command running in a pseudo-terminal, readable and answerable from the web page."""

    def __init__(self, title: str, done_check=None):
        self.id = uuid.uuid4().hex[:12]
        self.title, self.done_check = title, done_check
        self.raw, self.status, self.code, self.started = "", "running", None, time.time()
        self.proc, self.fd = None, None

    async def start(self, argv, env, cwd, user_kw):
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))  # wide: long links stay on one line

        def ctty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, preexec_fn=ctty, **user_kw)
        os.close(slave)
        self.fd = master
        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._read)
        asyncio.create_task(self._watch())

    def _read(self):
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.fd)
            return
        self.raw = (self.raw + data.decode("utf-8", "replace"))[-200_000:]

    async def _watch(self):
        while self.proc.returncode is None:
            await asyncio.sleep(1)
            if self.done_check and await self.done_check():
                self.status = "done"
                await asyncio.sleep(2)  # let it finish writing its files
                self.kill()
                break
        await self.proc.wait()
        self.code = self.proc.returncode
        if self.status == "running":
            self.status = "done" if self.code == 0 or (self.done_check and await self.done_check()) else "failed"
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)
        except (OSError, ValueError):
            pass

    def send(self, text: str):
        if self.status != "running":
            raise ValueError("That window has already finished.")
        os.write(self.fd, text.encode() + b"\r")

    def kill(self):
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
        if self.status == "running":
            self.status = "cancelled"

    def view(self) -> dict:
        text = strip_ansi(self.raw)
        return {"id": self.id, "title": self.title, "status": self.status, "code": self.code,
                "output": "\n".join(text.split("\n")[-60:])[-8000:], "links": find_links(self.raw)}


# ------------------------------------------------------------ accounts

class Accounts:
    def __init__(self, store, port: int):
        self.store, self.port = store, port
        self.sessions: dict[str, Session] = {}
        self.sem = asyncio.Semaphore(int(os.environ.get("CLI_CONCURRENCY", "2")))
        self._status_cache: dict[str, tuple[float, bool]] = {}
        for d in (NPM_PREFIX, HOME, WORK):
            d.mkdir(parents=True, exist_ok=True)
        self.user_kw = self._user_kw()

    # -- plumbing
    def _user_kw(self) -> dict:
        if os.geteuid() != 0:
            return {}
        try:
            pw = pwd.getpwnam(CLI_USER)
        except KeyError:
            log.warning("User %s not found; the sign-in tools will run as root", CLI_USER)
            return {}
        for d in (CLI_DIR, NPM_PREFIX, HOME, WORK):
            try:
                if d.stat().st_uid != pw.pw_uid:
                    os.chown(d, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass
        return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}

    def env(self, **extra) -> dict:
        path = f"{NPM_PREFIX}/bin:/usr/local/bin:/usr/bin:/bin"
        env = {"HOME": str(HOME), "PATH": path, "LANG": "C.UTF-8", "TERM": "xterm-256color",
               "NO_BROWSER": "true", "BROWSER": "none", "DISABLE_AUTOUPDATER": "1",
               "NPM_CONFIG_PREFIX": str(NPM_PREFIX), "NPM_CONFIG_UPDATE_NOTIFIER": "false",
               "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "GEMINI_CLI_NO_RELAUNCH": "true"}
        for k in ("TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                  "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "NPM_CONFIG_CAFILE", "npm_config_cafile"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env.update(extra)
        return env

    def binary(self, acct: str) -> str | None:
        p = NPM_PREFIX / "bin" / ACCOUNTS[acct]["bin"]
        return str(p) if p.exists() else None

    async def run(self, argv, stdin: str = "", timeout: float = 60, env=None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env or self.env(), cwd=str(WORK), start_new_session=True, **self.user_kw)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            raise CliError(f"No answer within {int(timeout)} seconds.") from None
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # -- status
    async def signed_in(self, acct: str, fresh: bool = False) -> bool:
        b = self.binary(acct)
        if not b:
            return False
        if acct == "gemini":
            return (HOME / ".gemini" / "oauth_creds.json").is_file()
        cached = self._status_cache.get(acct)
        if cached and not fresh and time.time() - cached[0] < 30:
            return cached[1]
        try:
            if acct == "claude":
                code, out, _ = await self.run([b, "auth", "status"], timeout=20)
                try:
                    ok = bool(json.loads(out).get("loggedIn"))
                except ValueError:
                    ok = code == 0 and "logged in" in out.lower()
            else:
                code, out, err = await self.run([b, "login", "status"], timeout=20)
                ok = code == 0 and "not logged in" not in (out + err).lower()
        except (CliError, OSError):
            ok = False
        self._status_cache[acct] = (time.time(), ok)
        return ok

    async def version(self, acct: str) -> str:
        pkg = NPM_PREFIX / "lib" / "node_modules" / ACCOUNTS[acct]["package"] / "package.json"
        try:
            return json.loads(pkg.read_text()).get("version", "")
        except (OSError, ValueError):
            return ""

    async def status(self) -> list[dict]:
        out = []
        for acct, a in ACCOUNTS.items():
            installed = bool(self.binary(acct))
            out.append({"id": acct, "label": a["label"], "plan": a["plan"], "provider": a["provider"],
                        "installed": installed, "version": await self.version(acct) if installed else "",
                        "signed_in": await self.signed_in(acct) if installed else False,
                        "in_providers": a["provider"] in self.store.providers(),
                        "models": a["models"], "notice": a.get("notice", "")})
        return out

    # -- install / sign in / sign out
    def _new_session(self, title, done_check=None) -> Session:
        for sid, s in list(self.sessions.items()):  # forget old finished windows
            if s.status != "running" and time.time() - s.started > 3600:
                self.sessions.pop(sid, None)
        s = Session(title, done_check)
        self.sessions[s.id] = s
        return s

    async def install(self, acct: str) -> Session:
        if not shutil.which("npm"):
            raise ValueError("npm isn't in this image. Update the add-on or container to the latest version.")
        a = ACCOUNTS[acct]
        s = self._new_session(f"Installing {a['label']}")
        await s.start(["npm", "install", "-g", "--no-fund", "--no-audit", "--progress=false", f"{a['package']}@latest"],
                      self.env(), str(WORK), self.user_kw)

        async def after():
            await s.proc.wait()
            self._status_cache.pop(acct, None)
            if self.binary(acct) and a["provider"] not in self.store.providers():
                self.add_provider(acct)
        asyncio.create_task(after())
        return s

    async def login(self, acct: str) -> Session:
        b = self.binary(acct)
        if not b:
            raise ValueError("Install it first.")
        a = ACCOUNTS[acct]
        if acct == "gemini":
            self._gemini_settings()

        async def done():
            if acct == "gemini":
                return (HOME / ".gemini" / "oauth_creds.json").is_file()
            if acct == "claude":
                return (HOME / ".claude" / ".credentials.json").is_file()
            return (HOME / ".codex" / "auth.json").is_file()

        s = self._new_session(f"Sign in to {a['label']}", done)
        await s.start([b, *a["login"]], self.env(), str(WORK), self.user_kw)

        async def after():
            await s.proc.wait()
            self._status_cache.pop(acct, None)
            if a["provider"] not in self.store.providers():
                self.add_provider(acct)
            self.configure_mcp()
        asyncio.create_task(after())
        return s

    async def logout(self, acct: str) -> None:
        b = self.binary(acct)
        a = ACCOUNTS[acct]
        if acct == "gemini":
            for f in ("oauth_creds.json", "google_accounts.json"):
                (HOME / ".gemini" / f).unlink(missing_ok=True)
        elif b:
            await self.run([b, *a["logout"]], timeout=30)
        self._status_cache.pop(acct, None)

    def add_provider(self, acct: str) -> None:
        a = ACCOUNTS[acct]
        self.store.data["providers"][a["provider"]] = {
            "kind": "cli", "cli": acct, "enabled": True, "mode": "", "keys": [], "ignore": [], "whitelist": [], "models": []}
        self.store.save()

    # -- device access (MCP) for the signed-in tools
    def mcp_servers(self) -> dict[str, dict]:
        """name -> {url, label} for every MCP server picked under MCP > Signed-in AIs."""
        return {s["name"]: {"url": f"http://127.0.0.1:{self.port}/mcp/{s['key']}", "label": s["label"]}
                for s in self.store.mcp_ai_servers()}

    def _ours(self, url: str) -> bool:
        return str(url or "").startswith(f"http://127.0.0.1:{self.port}/mcp/")

    def _write_owned(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        if self.user_kw:
            os.chown(tmp, self.user_kw["user"], self.user_kw["group"])
            os.chown(path.parent, self.user_kw["user"], self.user_kw["group"])
        tmp.chmod(0o600)
        tmp.replace(path)

    def _gemini_settings(self) -> None:
        p = HOME / ".gemini" / "settings.json"
        try:
            s = json.loads(p.read_text()) if p.is_file() else {}
        except ValueError:
            s = {}
        sec = s.setdefault("security", {})
        sec.setdefault("auth", {})["selectedType"] = "oauth-personal"
        sec.setdefault("folderTrust", {})["enabled"] = False
        s["selectedAuthType"] = "oauth-personal"
        s.setdefault("ui", {})["hideBanner"] = True
        servers = s.setdefault("mcpServers", {})
        for name in [n for n, v in servers.items() if n == MCP_NAME or self._ours((v or {}).get("url"))]:
            servers.pop(name)
        for name, v in self.mcp_servers().items():
            servers[name] = {"type": "http", "url": v["url"], "trust": True}
        self._write_owned(p, json.dumps(s, indent=2))

    def _codex_config(self) -> None:
        p = HOME / ".codex" / "config.toml"
        text = p.read_text() if p.is_file() else ""
        text = re.sub(r"(?ms)^# >>> personal-ai-gateway.*?^# <<< personal-ai-gateway\n?", "", text).rstrip()
        servers = self.mcp_servers()
        if servers:
            text += "\n\n# >>> personal-ai-gateway (managed, don't edit)\n" + "".join(
                f"[mcp_servers.{n}]\nurl = \"{v['url']}\"\n" for n, v in servers.items()) + "# <<< personal-ai-gateway\n"
        self._write_owned(p, text.lstrip())

    def configure_mcp(self) -> None:
        """Point the signed-in tools at the MCP server picked in Devices (or remove it)."""
        try:
            self._gemini_settings()
            self._codex_config()
            self._write_owned(CLI_DIR / "claude-mcp.json", json.dumps(
                {"mcpServers": {n: {"type": "http", "url": v["url"]} for n, v in self.mcp_servers().items()}}))
        except OSError as e:
            log.warning("Couldn't write the AI tools' device settings: %s", e)

    # -- chat
    def models(self, acct: str) -> list[str]:
        return list(ACCOUNTS[acct]["models"])

    async def chat(self, acct: str, model: str, body: dict, timeout: float) -> str:
        b = self.binary(acct)
        if not b:
            raise CliError(f"{ACCOUNTS[acct]['label']} isn't installed yet (Providers > Sign in with an account).")
        system, prompt = transcript(body.get("messages") or [])
        system = system or DEFAULT_SYSTEM
        use_mcp = list(self.mcp_servers().items())
        if use_mcp:
            system += "\n\nYou have these tool sets (MCP servers) the user chose for you: " + "; ".join(
                f"{n} = {v['label']}" for n, v in use_mcp) + ". Use them when the user's request needs them."
            use_mcp = [n for n, _ in use_mcp]
        async with self.sem:
            if acct == "claude":
                return await self._claude(b, model, system, prompt, use_mcp, timeout)
            if acct == "chatgpt":
                return await self._codex(b, model, system, prompt, timeout)
            return await self._gemini(b, model, system, prompt, use_mcp, timeout)

    async def _claude(self, b, model, system, prompt, use_mcp, timeout) -> str:
        with tempfile.NamedTemporaryFile("w", dir=WORK, suffix=".txt", delete=False) as f:
            f.write(system)
        if self.user_kw:
            os.chown(f.name, self.user_kw["user"], self.user_kw["group"])
        argv = [b, "-p", "--output-format", "json", "--no-session-persistence", "--system-prompt-file", f.name,
                "--tools", "", "--strict-mcp-config"]
        if use_mcp:
            argv += ["--mcp-config", str(CLI_DIR / "claude-mcp.json"),
                     "--allowedTools", ",".join(f"mcp__{n}" for n in use_mcp)]
        if model and model != "default":
            argv += ["--model", model]
        try:
            code, out, err = await self.run(argv, prompt, timeout)
        finally:
            os.unlink(f.name)
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise CliError(self._problem("Claude", code, out, err)) from None
        if data.get("is_error"):
            raise CliError("Claude: " + str(data.get("result") or data.get("subtype") or "error")
                           + (" (sign in again in Providers)" if "auth" in str(data.get("result", "")).lower() else ""))
        return str(data.get("result") or "")

    async def _codex(self, b, model, system, prompt, timeout) -> str:
        out_file = WORK / f"codex-{uuid.uuid4().hex[:8]}.txt"
        argv = [b, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never", "-s", "read-only",
                "-c", 'approval_policy="never"', "-C", str(WORK), "-o", str(out_file)]
        if model and model != "default":
            argv += ["-m", model]
        argv.append("-")
        try:
            code, out, err = await self.run(argv, f"{system}\n\n{prompt}", timeout)
            text = out_file.read_text().strip() if out_file.is_file() else ""
        finally:
            out_file.unlink(missing_ok=True)
        if code != 0 or not text:
            raise CliError(self._problem("ChatGPT (Codex)", code, out, err))
        return text

    async def _gemini(self, b, model, system, prompt, use_mcp, timeout) -> str:
        argv = [b, "-o", "json", "--approval-mode", "default", "-p", "Follow the instructions and reply."]
        if model and model != "default":
            argv += ["-m", model]
        for n in use_mcp or []:
            argv += ["--allowed-mcp-server-names", n]
        code, out, err = await self.run(argv, f"{system}\n\n{prompt}", timeout)
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else {}
        except ValueError:
            data = {}
        if data.get("error"):
            e = data["error"]
            raise CliError(self._gemini_retired(str(e.get("message") if isinstance(e, dict) else e)) or "Gemini: " + str(e.get("message") if isinstance(e, dict) else e))
        text = data.get("response")
        if code != 0 or not isinstance(text, str):
            retired = self._gemini_retired(out + "\n" + err)
            if retired:
                raise CliError(retired)
            raise CliError(self._problem("Gemini", code, out, err))
        return text

    @staticmethod
    def _gemini_retired(text: str) -> str:
        """Google's 'migrate to Antigravity' refusal for personal accounts, in plain words."""
        if re.search(r"antigravity|IneligibleOrProjectId|UNSUPPORTED_CLIENT|not eligible for Gemini Code Assist", text or "", re.I):
            return ("Gemini account sign-in no longer works for personal Google accounts: Google moved them from Gemini CLI to its "
                    "Antigravity apps in June 2026. Use Gemini with a free API key instead (aistudio.google.com/apikey, then "
                    "Providers > add \"gemini\"), and remove gemini-account from your providers.")
        return ""

    @staticmethod
    def _problem(label, code, out, err) -> str:
        tail = strip_ansi((err or out).strip())[-400:]
        hint = " Sign in again in Providers." if re.search(r"auth|log ?in|credential|401", tail, re.I) else ""
        return f"{label} failed (exit {code}): {tail or 'no output'}.{hint}"
