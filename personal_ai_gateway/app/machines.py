"""MCP servers for things other than Home Assistant devices.

Three kinds, each with its own private address like the device servers:

* computer - another machine reached over SSH (a Raspberry Pi, a laptop, a NAS...). Built-in tools for
             system info, files and, if you allow it, running commands. You choose how much it may do
             and which folders it may touch.
* command  - any MCP server that runs as a command (stdio), e.g. `npx -y @modelcontextprotocol/server-...`.
             Runs inside the gateway (as the unprivileged tools user) or on one of your computers over SSH.
* remote   - another MCP server's URL, put behind this gateway's address so you manage it in one place.

For "command" and "remote" the gateway speaks MCP to the real server and passes requests through,
so every tool, resource and prompt that server offers works. You can hide tools you don't want used.
"""
import asyncio
import hashlib
import itertools
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
from collections import deque

from aiohttp import ClientError, ClientTimeout

from store import DATA_DIR

log = logging.getLogger("gateway")

SSH_DIR = DATA_DIR / "ssh"
SSH_KEY = SSH_DIR / "id_ed25519"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
CLIENT_INFO = {"name": "personal-ai-gateway", "version": "1.8.0"}
UPSTREAM_PROTOCOL = "2025-06-18"
ACCESS = ("read", "files", "full")
MAX_OUT = 100_000


class UpstreamError(Exception):
    pass


class RpcError(Exception):
    def __init__(self, error: dict):
        self.error = error if isinstance(error, dict) else {"code": -32000, "message": str(error)}
        super().__init__(self.error.get("message", "error"))


# ------------------------------------------------------------------ computers (SSH)

def slug_id() -> str:
    return secrets.token_hex(4)


class Computers:
    """Saved SSH connections. The gateway has one key pair; its public key goes in each computer's authorized_keys."""

    def __init__(self, store):
        self.store = store

    @property
    def all(self) -> dict:
        return self.store.data.setdefault("mcp", {}).setdefault("computers", {})

    # -- the gateway's key
    def ensure_key(self) -> str:
        if not SSH_KEY.is_file():
            SSH_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "personal-ai-gateway", "-f", str(SSH_KEY)],
                           check=True, capture_output=True, timeout=30)
        try:
            SSH_DIR.chmod(0o700)
            SSH_KEY.chmod(0o600)
        except OSError:
            pass
        return (SSH_KEY.with_suffix(".pub")).read_text().strip()

    def public_key(self) -> str:
        try:
            return self.ensure_key()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("Couldn't make the SSH key: %s", e)
            return ""

    # -- config
    def save(self, spec: dict) -> dict:
        cid = str(spec.get("id") or "")
        c = self.all.get(cid) or {"id": slug_id(), "name": "", "host": "", "port": 22, "user": ""}
        for k in ("name", "host", "user"):
            if k in spec:
                c[k] = str(spec[k]).strip()[:120]
        if "port" in spec:
            c["port"] = int(spec.get("port") or 22)
        if not c["host"] or not c["user"]:
            raise ValueError("A computer needs an address (host name or IP) and a user name.")
        if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", c["host"]) or not re.fullmatch(r"[A-Za-z0-9._-]+", c["user"]):
            raise ValueError("The address or user name has characters that aren't allowed.")
        if not 0 < c["port"] < 65536:
            raise ValueError("The port must be between 1 and 65535.")
        c["name"] = c["name"] or c["host"]
        self.all[c["id"]] = c
        self.store.save()
        return c

    def delete(self, cid: str, servers: dict) -> None:
        users = [s["name"] for s in servers.values() if cid in (s.get("computer"), s.get("run_on"))]
        if users:
            raise ValueError("These MCP servers use that computer, change or delete them first: " + ", ".join(users))
        self.all.pop(cid, None)
        self.store.save()

    def get(self, cid: str) -> dict:
        c = self.all.get(cid or "")
        if not c:
            raise UpstreamError("That computer isn't saved any more. Pick another one in the gateway.")
        return c

    # -- running things
    def argv(self, c: dict, remote: str, *, tty=False, batch=True) -> list[str]:
        opts = ["-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                "-o", "LogLevel=ERROR"]
        if batch:
            self.ensure_key()
            opts += ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", str(SSH_KEY),
                     "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/gwssh-%C", "-o", "ControlPersist=120"]
        else:
            opts += ["-o", "BatchMode=no", "-o", "PubkeyAuthentication=no", "-o", "ControlMaster=no",
                     "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1"]
        return ["ssh", "-T" if not tty else "-t", *opts, "-p", str(c.get("port") or 22), f"{c['user']}@{c['host']}", remote]

    async def run(self, c: dict, script: str, stdin: bytes = b"", timeout: float = 60) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *self.argv(c, login_shell(script)), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            kill_group(proc)
            await proc.wait()
            raise UpstreamError(f"{c['name']} didn't finish within {int(timeout)} seconds.") from None
        if proc.returncode == 255:
            raise UpstreamError(ssh_problem(c, err.decode("utf-8", "replace")))
        return proc.returncode, out, err

    async def test(self, c: dict) -> dict:
        t0 = time.time()
        try:
            code, out, err = await self.run(c, "uname -snrm; (uptime -p 2>/dev/null || uptime)", timeout=25)
        except UpstreamError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": code == 0, "reply": out.decode("utf-8", "replace").strip()[:400],
                "error": err.decode("utf-8", "replace").strip()[:400], "ms": int((time.time() - t0) * 1000)}

    async def install_key(self, c: dict, password: str) -> dict:
        """Log in once with a password and add the gateway's key to authorized_keys. The password isn't kept."""
        if not password:
            raise ValueError("Type the computer's password for that user.")
        pub = self.ensure_key()
        script = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                  f"(grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys)"
                  " && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo added")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SSHPASS": password}
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-e", *self.argv(c, script, batch=False), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
        except FileNotFoundError:
            raise ValueError("sshpass isn't installed in this image; add the key by hand (copy the command above).") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 40)
        except asyncio.TimeoutError:
            kill_group(proc)
            raise ValueError("The computer didn't answer in time.") from None
        if proc.returncode == 0 and b"added" in out:
            return await self.test(c)
        msg = err.decode("utf-8", "replace").strip()
        if proc.returncode == 5 or "denied" in msg.lower():
            return {"ok": False, "error": "Wrong password (or that user can't log in with a password)."}
        return {"ok": False, "error": ssh_problem(c, msg) if msg else f"Failed (exit {proc.returncode})."}

    def forget_host(self, c: dict) -> None:
        host = c["host"] if int(c.get("port") or 22) == 22 else f"[{c['host']}]:{c['port']}"
        if KNOWN_HOSTS.is_file():
            subprocess.run(["ssh-keygen", "-R", host, "-f", str(KNOWN_HOSTS)], capture_output=True, timeout=15)
        subprocess.run(["sh", "-c", "rm -f /tmp/gwssh-*"], capture_output=True, timeout=5)


def login_shell(script: str) -> str:
    """Run in a login shell so PATH has the user's tools (node, docker, ~/.local/bin...)."""
    q = shlex.quote(script)
    return f"if command -v bash >/dev/null 2>&1; then exec bash -lc {q}; else exec sh -lc {q}; fi"


def ssh_problem(c: dict, text: str) -> str:
    t = text.strip()
    low = t.lower()
    where = f"{c['user']}@{c['host']}:{c.get('port') or 22}"
    if "identification has changed" in low or "host key verification failed" in low:
        return (f"{c['name']}'s fingerprint changed (reinstalled or a different machine at that address). "
                "If that's expected press 'Forget fingerprint' on the computer and try again.")
    if "permission denied" in low:
        return (f"{c['name']} refused the gateway's key for {where}. Add the key: press 'Set up with password' "
                "or paste the command shown under Computers into that computer.")
    if "could not resolve" in low or "name or service not known" in low:
        return f"Can't find {c['host']}. Use its IP address (e.g. 192.168.1.20) or a name your network knows."
    if "connection refused" in low:
        return f"{where} refused the connection. Is SSH turned on there, and is the port right?"
    if "timed out" in low or "no route" in low:
        return f"Can't reach {where} (timed out). Is it on, and on the same network or Tailscale?"
    return f"SSH to {where} failed: {t[-400:] or 'no details'}"


def kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def clip(b: bytes, limit: int = MAX_OUT) -> str:
    s = b.decode("utf-8", "replace")
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n\n... [{len(s) - limit} characters cut] ...\n\n{s[-half:]}"


def shpath(p: str) -> str:
    p = (p or "").strip() or "~"
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def guard(srv: dict, expr: str, var: str = "p") -> str:
    """Shell that resolves a path (symlinks too) and stops unless it's inside the server's folders."""
    s = f'{var}=$(realpath -m -- {expr}) || exit 90; '
    folders = [f.strip() for f in srv.get("folders") or [] if f.strip()]
    if not folders or any(f.rstrip("/") == "" for f in folders):
        return s
    s += "ok=; for f in " + " ".join(f'"$(realpath -m -- {shpath(f)})"' for f in folders)
    s += f'; do case "${var}" in "$f"|"$f"/*) ok=1;; esac; done; [ -n "$ok" ] || {{ echo OUTSIDE >&2; exit 91; }}; '
    return s


# ------------------------------------------------------------------ computer servers: built-in tools

P = {"type": "string", "description": "Path on the computer. ~ is the home folder; relative paths start there."}
COMPUTER_TOOLS = [
    ("read", {"name": "system_info", "title": "System info",
              "description": "Host name, OS, uptime, CPU load and temperature, memory, disks, busiest processes and "
                             "Docker containers (if Docker is there and the user may use it).",
              "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}),
    ("read", {"name": "list_files", "title": "List a folder",
              "description": "Files and folders in one folder, with type, size and modified time.",
              "inputSchema": {"type": "object", "properties": {"path": P, "hidden": {"type": "boolean", "description": "Include dot files"}}},
              "annotations": {"readOnlyHint": True}}),
    ("read", {"name": "read_file", "title": "Read a file",
              "description": "The text of one file (up to max_kb kilobytes, default 256). Binary files are reported, not shown.",
              "inputSchema": {"type": "object", "properties": {"path": P, "max_kb": {"type": "integer", "minimum": 1, "maximum": 2048}},
                              "required": ["path"]}, "annotations": {"readOnlyHint": True}}),
    ("read", {"name": "find_files", "title": "Find files",
              "description": "Search under a folder for files whose name matches a pattern (e.g. *.yaml) and/or whose "
                             "text contains some words. Returns up to 200 paths.",
              "inputSchema": {"type": "object", "properties": {"path": P, "name": {"type": "string", "description": "Name pattern, e.g. *.log"},
                                                               "contains": {"type": "string", "description": "Text the file must contain"}}},
              "annotations": {"readOnlyHint": True}}),
    ("files", {"name": "write_file", "title": "Write a file",
               "description": "Create or replace a text file (folders are created). Set append to add to the end instead.",
               "inputSchema": {"type": "object", "properties": {"path": P, "content": {"type": "string"},
                                                                "append": {"type": "boolean"}}, "required": ["path", "content"]},
               "annotations": {"readOnlyHint": False, "destructiveHint": True}}),
    ("full", {"name": "run_command", "title": "Run a command",
              "description": "Run a shell command on the computer as the connected user and get its output and exit code. "
                             "timeout is in seconds (default 60, max 600). Commands that wait for input won't work.",
              "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": P,
                                                               "timeout": {"type": "integer", "minimum": 1, "maximum": 600}},
                              "required": ["command"]},
              "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}}),
]

SYSINFO = r"""
echo "== $(hostname) | $(uname -srm)"
[ -r /etc/os-release ] && . /etc/os-release && echo "OS: ${PRETTY_NAME:-}"
echo "Up: $(uptime -p 2>/dev/null || uptime)"
echo "Load: $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)  CPUs: $(nproc 2>/dev/null)"
t=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null) && echo "CPU temp: $((t/1000)) C"
echo; echo "== Memory"; free -h 2>/dev/null
echo; echo "== Disks"; df -h -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null | head -20
echo; echo "== Busiest processes"; ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu 2>/dev/null | head -9
if command -v docker >/dev/null 2>&1; then
  echo; echo "== Docker containers"
  docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1 | head -60
fi
"""


class ComputerTools:
    def __init__(self, computers: Computers, log_call):
        self.computers, self.log_call = computers, log_call

    def tools(self, srv: dict) -> list[dict]:
        level = ACCESS.index(srv.get("access") or "read") if (srv.get("access") or "read") in ACCESS else 0
        return [t for need, t in COMPUTER_TOOLS if ACCESS.index(need) <= level]

    async def call(self, srv: dict, name: str, a: dict) -> dict:
        allowed = {t["name"] for t in self.tools(srv)} - set(srv.get("hidden_tools") or [])
        if name not in {t["name"] for _, t in COMPUTER_TOOLS}:
            return tool_text(f"Unknown tool: {name}", error=True)
        if name not in allowed:
            return tool_text(f"{name} isn't allowed on this MCP server. Change what it may do in the gateway.", error=True)
        try:
            c = self.computers.get(srv.get("computer"))
            if name == "system_info":
                code, out, err = await self.computers.run(c, SYSINFO, timeout=30)
                return tool_text(clip(out + err))
            if name == "list_files":
                hidden = "" if a.get("hidden") else " ! -name '.*'"
                script = (guard(srv, shpath(a.get("path"))) + '[ -d "$p" ] || { echo NOTDIR >&2; exit 92; }; echo "$p"; '
                          f"find \"$p\" -mindepth 1 -maxdepth 1{hidden} -printf '%y\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%f\\n' | sort -t'\t' -k4 | head -n 1000")
                code, out, err = await self.computers.run(c, script)
                if code:
                    return self._fail(code, err, a.get("path"))
                lines = out.decode("utf-8", "replace").splitlines()
                kinds = {"d": "folder", "f": "file", "l": "link"}
                items = []
                for ln in lines[1:]:
                    parts = ln.split("\t", 3)
                    if len(parts) == 4:
                        items.append({"name": parts[3], "type": kinds.get(parts[0], parts[0]),
                                      "size": int(parts[1]) if parts[0] == "f" else None, "modified": parts[2]})
                return tool_json({"path": lines[0] if lines else "", "count": len(items), "items": items})
            if name == "read_file":
                limit = max(1, min(int(a.get("max_kb") or 256), 2048)) * 1024
                script = (guard(srv, shpath(a.get("path"))) + '[ -f "$p" ] || { echo NOTFILE >&2; exit 92; }; '
                          f'stat -c %s -- "$p"; head -c {limit} -- "$p"')
                code, out, err = await self.computers.run(c, script)
                if code:
                    return self._fail(code, err, a.get("path"))
                size_s, _, data = out.partition(b"\n")
                size = int(size_s or 0)
                if b"\0" in data[:8192]:
                    return tool_text(f"{a.get('path')} is a binary file ({size} bytes), not shown.")
                text = data.decode("utf-8", "replace")
                if size > limit:
                    text += f"\n\n[showing the first {limit // 1024} KB of {size // 1024} KB; ask for a bigger max_kb to see more]"
                return tool_text(text)
            if name == "find_files":
                patt, text = str(a.get("name") or "").strip(), str(a.get("contains") or "").strip()
                if not patt and not text:
                    return tool_text("Give a name pattern, some text to look for, or both.", error=True)
                base = guard(srv, shpath(a.get("path"))) + '[ -d "$p" ] || { echo NOTDIR >&2; exit 92; }; '
                find = "find \"$p\" -maxdepth 8 \\( -name .git -o -name node_modules -o -name proc \\) -prune -o -type f"
                if patt:
                    find += f" -name {shlex.quote(patt)}"
                find += " -print 2>/dev/null"
                if text:
                    find += f" | head -n 20000 | tr '\\n' '\\0' | xargs -0 -r grep -lIF -- {shlex.quote(text)} 2>/dev/null"
                code, out, err = await self.computers.run(c, base + find + " | head -n 200", timeout=90)
                if code and code in (90, 91, 92):
                    return self._fail(code, err, a.get("path"))
                found = out.decode("utf-8", "replace").splitlines()
                return tool_json({"found": len(found), "paths": found})
            if name == "write_file":
                content = str(a.get("content") if a.get("content") is not None else "")
                op = ">>" if a.get("append") else ">"
                script = (guard(srv, shpath(a.get("path"))) + '[ -d "$p" ] && { echo ISDIR >&2; exit 92; }; '
                          f'mkdir -p -- "$(dirname -- "$p")" && cat {op} "$p" && echo "$p"')
                code, out, err = await self.computers.run(c, script, stdin=content.encode())
                if code:
                    return self._fail(code, err, a.get("path"))
                self.log_call(srv, "write_file", out.decode().strip())
                return tool_json({"ok": True, "path": out.decode().strip(), "bytes": len(content.encode()),
                                  "appended": bool(a.get("append"))})
            if name == "run_command":
                cmd = str(a.get("command") or "").strip()
                if not cmd:
                    return tool_text("command is empty.", error=True)
                timeout = max(1, min(int(a.get("timeout") or 60), 600))
                cwd = str(a.get("cwd") or "").strip()
                q = shlex.quote(cmd)
                script = (guard(srv, shpath(cwd)) + 'cd "$p" || exit 93; ' if cwd else "") + (
                    f'if command -v timeout >/dev/null 2>&1; then exec timeout -k 5 {timeout} "${{BASH:-sh}}" -c {q}; '
                    f'else exec "${{BASH:-sh}}" -c {q}; fi')
                self.log_call(srv, "run_command", cmd[:120])
                code, out, err = await self.computers.run(c, script, stdin=b"", timeout=timeout + 15)
                if cwd and code in (90, 91, 93) and not out:
                    return self._fail(code, err, cwd)
                res = {"exit_code": code, "stdout": clip(out), "stderr": clip(err, 20_000)}
                if code in (124, 137):
                    res["note"] = f"Stopped after {timeout} seconds (timeout). Pass a bigger timeout (max 600) if it needs longer."
                return tool_json(res)
        except UpstreamError as e:
            return tool_text(str(e), error=True)
        except (TypeError, ValueError) as e:
            return tool_text(f"Bad arguments: {e}", error=True)
        return tool_text(f"Unknown tool: {name}", error=True)

    @staticmethod
    def _fail(code, err: bytes, path) -> dict:
        e = err.decode("utf-8", "replace").strip()
        msg = {91: f"{path} is outside the folders this MCP server may use.",
               92: {"NOTDIR": f"{path} isn't a folder.", "NOTFILE": f"{path} isn't a file (or doesn't exist).",
                    "ISDIR": f"{path} is a folder."}.get(e.splitlines()[-1] if e else "", e),
               93: f"Can't open the folder {path}."}.get(code) or e or f"Failed (exit {code})."
        return tool_text(msg, error=True)


# ------------------------------------------------------------------ MCP clients for command / remote servers

class StdioClient:
    """A long-running MCP server process; we talk JSON-RPC over its stdin/stdout."""

    def __init__(self, argv: list[str], label: str, **popen):
        self.argv, self.label, self.popen = argv, label, popen
        self.proc = None
        self.pending: dict[str, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.stderr: deque[str] = deque(maxlen=60)
        self.init: dict = {}
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, start_new_session=True, **self.popen)
        except (OSError, ValueError) as e:
            raise UpstreamError(f"Couldn't start {self.label}: {e}") from e
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._errors())]
        self.init = await self.request("initialize", {"protocolVersion": UPSTREAM_PROTOCOL, "capabilities": {},
                                                      "clientInfo": CLIENT_INFO}, timeout=180)
        await self.notify("notifications/initialized")

    async def _read(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.stderr.append("[stdout] " + line.decode("utf-8", "replace").rstrip()[:300])
                    continue
                await self._incoming(msg)
        except (asyncio.LimitOverrunError, ValueError) as e:
            self.stderr.append(f"[gateway] reading failed: {e}")
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(UpstreamError(self._died()))

    async def _incoming(self, msg):
        if not isinstance(msg, dict):
            return
        mid = msg.get("id")
        if "method" in msg:
            if mid is not None:  # the server asks us something
                method = msg["method"]
                if method == "ping":
                    await self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
                elif method == "roots/list":
                    await self._send({"jsonrpc": "2.0", "id": mid, "result": {"roots": []}})
                else:
                    await self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"{method} isn't supported by the gateway"}})
            return
        f = self.pending.pop(str(mid), None)
        if f and not f.done():
            f.set_result(msg)

    async def _errors(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            self.stderr.append(line.decode("utf-8", "replace").rstrip()[:500])

    def _died(self) -> str:
        code = self.proc.returncode if self.proc else None
        tail = "\n".join(list(self.stderr)[-8:])
        return f"{self.label} stopped" + (f" (exit {code})" if code is not None else "") + (f":\n{tail}" if tail else ".")

    async def _send(self, msg: dict):
        if not self.alive:
            raise UpstreamError(self._died())
        try:
            self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise UpstreamError(self._died()) from e

    async def notify(self, method: str, params: dict | None = None):
        await self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def request(self, method: str, params: dict | None = None, timeout: float = 120) -> dict:
        rid = f"gw-{next(self.ids)}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            try:
                await self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
            except UpstreamError:
                pass
            raise UpstreamError(f"{self.label} didn't answer {method} within {int(timeout)} seconds.") from None
        if "error" in msg:
            raise RpcError(msg["error"])
        return msg.get("result") or {}

    async def close(self):
        for t in self.tasks:
            t.cancel()
        if self.alive:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, OSError):
                kill_group(self.proc)
        if self.proc and self.proc.returncode is None:
            kill_group(self.proc)

    def logs(self) -> list[str]:
        return list(self.stderr)


class HttpClient:
    """Another MCP server reached by URL (Streamable HTTP, JSON or SSE replies)."""

    def __init__(self, session, url: str, headers: dict, label: str):
        self.session, self.url, self.headers, self.label = session, url, headers, label
        self.sid = ""
        self.ids = itertools.count(1)
        self.init: dict = {}
        self.stderr: deque[str] = deque(maxlen=30)
        self.alive = False

    async def start(self):
        self.init = await self.request("initialize", {"protocolVersion": UPSTREAM_PROTOCOL, "capabilities": {},
                                                      "clientInfo": CLIENT_INFO}, timeout=60, retry=False)
        await self.notify("notifications/initialized")
        self.alive = True

    def _headers(self) -> dict:
        h = {**self.headers, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        if self.init.get("protocolVersion"):
            h["MCP-Protocol-Version"] = self.init["protocolVersion"]
        return h

    async def _post(self, msg: dict, timeout: float):
        try:
            async with self.session.post(self.url, json=msg, headers=self._headers(),
                                         timeout=ClientTimeout(total=timeout)) as r:
                if r.headers.get("Mcp-Session-Id"):
                    self.sid = r.headers["Mcp-Session-Id"]
                if r.status == 404 and self.sid:
                    return "expired"
                if r.status >= 400:
                    text = (await r.text())[:300]
                    hint = " (check the URL and headers / token)" if r.status in (401, 403) else ""
                    raise UpstreamError(f"{self.label} answered {r.status}{hint}: {text}")
                if "id" not in msg:
                    return None
                if "text/event-stream" in r.headers.get("Content-Type", ""):
                    data: list[str] = []
                    async for raw in r.content:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            try:
                                ev = json.loads("\n".join(data))
                            except ValueError:
                                ev = None
                            data = []
                            if isinstance(ev, dict) and str(ev.get("id")) == str(msg["id"]) and "method" not in ev:
                                return ev
                    raise UpstreamError(f"{self.label} closed the reply without an answer.")
                text = await r.text()
                return json.loads(text) if text.strip() else None
        except (ClientError, asyncio.TimeoutError) as e:
            raise UpstreamError(f"Can't reach {self.label}: {type(e).__name__} {e}") from e
        except ValueError as e:
            raise UpstreamError(f"{self.label} sent something that isn't JSON.") from e

    async def notify(self, method: str, params: dict | None = None):
        await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 30)

    async def request(self, method: str, params: dict | None = None, timeout: float = 120, retry=True) -> dict:
        msg = {"jsonrpc": "2.0", "id": f"gw-{next(self.ids)}", "method": method,
               **({"params": params} if params is not None else {})}
        reply = await self._post(msg, timeout)
        if reply == "expired" and retry:
            self.sid, self.init = "", {}
            await self.start()
            reply = await self._post(msg, timeout)
        if isinstance(reply, list):
            reply = next((x for x in reply if str(x.get("id")) == msg["id"]), None)
        if not isinstance(reply, dict):
            raise UpstreamError(f"{self.label} gave no answer to {method}.")
        if "error" in reply:
            raise RpcError(reply["error"])
        return reply.get("result") or {}

    async def close(self):
        if self.sid:
            try:
                async with self.session.delete(self.url, headers=self._headers(), timeout=ClientTimeout(total=5)):
                    pass
            except (ClientError, asyncio.TimeoutError):
                pass
        self.alive = False

    def logs(self) -> list[str]:
        return list(self.stderr)


# ------------------------------------------------------------------ the manager

def parse_pairs(text_or_dict, sep: str) -> dict:
    if isinstance(text_or_dict, dict):
        return {str(k).strip(): str(v) for k, v in text_or_dict.items() if str(k).strip()}
    out = {}
    for line in str(text_or_dict or "").splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


class Machines:
    """Everything for computer / command / remote servers."""

    def __init__(self, store, session, accounts, calls: list):
        self.store, self.session, self.accounts, self.calls = store, session, accounts, calls
        self.computers = Computers(store)
        self.ctools = ComputerTools(self.computers, self.log_call)
        self.clients: dict[str, tuple[str, object]] = {}
        self.errors: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._closing: set = set()

    def log_call(self, srv: dict, action: str, target: str):
        self.calls.insert(0, {"t": time.time(), "server": srv["name"], "entity_id": target, "action": action})
        del self.calls[40:]

    # -- config fields per kind
    def apply(self, srv: dict, spec: dict) -> None:
        kind = srv["kind"]
        if "hidden_tools" in spec and isinstance(spec["hidden_tools"], list):
            srv["hidden_tools"] = [str(x) for x in spec["hidden_tools"]][:500]
        if kind == "computer":
            if "computer" in spec:
                srv["computer"] = str(spec["computer"] or "")
            if "access" in spec and spec["access"] in ACCESS:
                srv["access"] = spec["access"]
            if "folders" in spec:
                f = spec["folders"]
                srv["folders"] = [x.strip() for x in (f if isinstance(f, list) else str(f).splitlines()) if x.strip()][:50]
            srv.setdefault("access", "read")
            srv.setdefault("folders", [])
            srv.setdefault("computer", "")
        elif kind == "command":
            if "command" in spec:
                srv["command"] = str(spec["command"] or "").strip()[:4000]
            if "run_on" in spec:
                srv["run_on"] = str(spec["run_on"] or "gateway")
            if "env" in spec:
                srv["env"] = parse_pairs(spec["env"], "=")
            srv.setdefault("command", "")
            srv.setdefault("run_on", "gateway")
            srv.setdefault("env", {})
        elif kind == "remote":
            if "url" in spec:
                url = str(spec["url"] or "").strip()
                if url and not url.startswith(("http://", "https://")):
                    raise ValueError("The MCP server URL must start with http:// or https://")
                srv["url"] = url
            if "headers" in spec:
                srv["headers"] = parse_pairs(spec["headers"], ":")
            srv.setdefault("url", "")
            srv.setdefault("headers", {})
        self.reset(srv["id"], srv)

    @staticmethod
    def signature(srv: dict) -> str:
        keys = ("kind", "command", "run_on", "env", "url", "headers")
        return hashlib.sha1(json.dumps({k: srv.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()

    def reset(self, sid: str, srv: dict | None = None) -> None:
        cur = self.clients.get(sid)
        if cur and (srv is None or cur[0] != self.signature(srv)):
            self.clients.pop(sid, None)
            task = asyncio.ensure_future(cur[1].close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        self.errors.pop(sid, None)

    async def client(self, srv: dict):
        sid, sig = srv["id"], self.signature(srv)
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            cur = self.clients.get(sid)
            if cur and cur[0] == sig and cur[1].alive:
                return cur[1]
            if cur:
                await cur[1].close()
                self.clients.pop(sid, None)
            c = self._make(srv)
            try:
                await c.start()
            except (UpstreamError, RpcError) as e:
                await c.close()
                tail = "\n".join(c.logs()[-10:])
                msg = str(e) if not tail or tail in str(e) else f"{e}\n{tail}"
                self.errors[sid] = msg
                self.clients[sid] = (sig, c)  # keep its log for the page
                raise UpstreamError(msg) from e
            self.errors.pop(sid, None)
            self.clients[sid] = (sig, c)
            log.info("MCP %s: connected to %s", srv["name"], (c.init.get("serverInfo") or {}).get("name", "server"))
            return c

    def _make(self, srv: dict):
        label = f"'{srv['name']}'"
        if srv["kind"] == "remote":
            if not srv.get("url"):
                raise UpstreamError("Set the MCP server's URL first.")
            return HttpClient(self.session, srv["url"], srv.get("headers") or {}, label)
        cmd = (srv.get("command") or "").strip()
        if not cmd:
            raise UpstreamError("Set the command that starts the MCP server first.")
        env = srv.get("env") or {}
        if srv.get("run_on", "gateway") == "gateway":
            home = DATA_DIR / "cli" / "mcp-home"
            home.mkdir(parents=True, exist_ok=True)
            kw = dict(self.accounts.user_kw)
            if kw:
                try:
                    os.chown(home, kw["user"], kw["group"])
                except OSError:
                    pass
            return StdioClient(["/bin/sh", "-c", cmd], label, env=self.accounts.env(HOME=str(home), **env),
                               cwd=str(home), **kw)
        c = self.computers.get(srv["run_on"])
        exports = "".join(f"export {k}={shlex.quote(v)}; " for k, v in env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
        return StdioClient(self.computers.argv(c, login_shell(exports + cmd)), f"{label} on {c['name']}")

    # -- MCP
    def instructions(self, srv: dict) -> str:
        c = self.computers.all.get(srv.get("computer") or "", {})
        what = {"read": "look at system info and read files", "files": "read and write files",
                "full": "read and write files and run shell commands"}[srv.get("access") or "read"]
        where = f" inside {', '.join(srv['folders'])}" if srv.get("folders") else ""
        return (f"Access to the computer '{c.get('name', '?')}' ({c.get('user', '')}@{c.get('host', '')}) over SSH. "
                f"You may {what}{where}. Start with system_info to see what it is.")

    async def dispatch(self, srv: dict, m: dict, protocols: tuple, server_info: dict):
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if mid is None:
            return None
        hidden = set(srv.get("hidden_tools") or [])
        if srv["kind"] == "computer":
            if method == "initialize":
                want = str(params.get("protocolVersion") or "")
                return ok(mid, {"protocolVersion": want if want in protocols else protocols[1],
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {**server_info, "name": "personal-ai-gateway-computer",
                                               "title": f"Computer: {srv['name']}"},
                                "instructions": self.instructions(srv)})
            if method == "ping":
                return ok(mid, {})
            if method == "tools/list":
                return ok(mid, {"tools": [t for t in self.ctools.tools(srv) if t["name"] not in hidden]})
            if method in ("resources/list", "prompts/list"):
                return ok(mid, {method.split("/")[0]: []})
            if method == "resources/templates/list":
                return ok(mid, {"resourceTemplates": []})
            if method == "tools/call":
                return ok(mid, await self.ctools.call(srv, str(params.get("name") or ""), params.get("arguments") or {}))
            return err(mid, -32601, f"Method not found: {method}")

        # command / remote: pass through to the real server
        if method == "ping":
            return ok(mid, {})
        try:
            c = await self.client(srv)
            if method == "initialize":
                want = str(params.get("protocolVersion") or "")
                init = c.init
                info = dict(init.get("serverInfo") or {})
                info["title"] = f"{srv['name']}"
                return ok(mid, {"protocolVersion": want if want in protocols else init.get("protocolVersion") or protocols[1],
                                "capabilities": {k: v for k, v in (init.get("capabilities") or {}).items()
                                                 if k in ("tools", "resources", "prompts", "completions", "logging")},
                                "serverInfo": info, **({"instructions": init["instructions"]} if init.get("instructions") else {})})
            if method == "tools/list":
                res = await c.request("tools/list", params, timeout=60)
                res["tools"] = [t for t in res.get("tools") or [] if t.get("name") not in hidden]
                return ok(mid, res)
            if method == "tools/call":
                name = str(params.get("name") or "")
                if name in hidden:
                    return ok(mid, tool_text(f"{name} is turned off for this MCP server.", error=True))
                self.log_call(srv, name, json.dumps(params.get("arguments") or {}, default=str)[:100])
                return ok(mid, await c.request("tools/call", params, timeout=600))
            return ok(mid, await c.request(method, params, timeout=120))
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": e.error}
        except UpstreamError as e:
            if method == "tools/call":
                return ok(mid, tool_text(str(e), error=True))
            return err(mid, -32000, str(e))

    async def check(self, srv: dict) -> dict:
        """For the page: does it start, and which tools does it have?"""
        t0 = time.time()
        if srv["kind"] == "computer":
            c = self.computers.all.get(srv.get("computer") or "")
            if not c:
                return {"ok": False, "error": "Pick a computer for this server.", "tools": []}
            res = await self.computers.test(c)
            return {**res, "tools": [{"name": t["name"], "description": t["description"]} for t in self.ctools.tools(srv)]}
        try:
            c = await self.client(srv)
            tools, cursor = [], None
            for _ in range(20):
                res = await c.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
                tools += res.get("tools") or []
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            info = c.init.get("serverInfo") or {}
            return {"ok": True, "ms": int((time.time() - t0) * 1000),
                    "reply": f"{info.get('name', 'MCP server')} {info.get('version', '')}".strip(),
                    "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:300]} for t in tools],
                    "log": c.logs()[-15:]}
        except (UpstreamError, RpcError) as e:
            cur = self.clients.get(srv["id"])
            return {"ok": False, "error": str(e), "tools": [], "log": cur[1].logs()[-15:] if cur else []}

    def status(self, srv: dict) -> dict:
        cur = self.clients.get(srv["id"])
        return {"running": bool(cur and cur[1].alive), "error": self.errors.get(srv["id"], "")}

    async def close(self):
        for _, c in list(self.clients.values()):
            await c.close()
        self.clients.clear()


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def tool_text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def tool_json(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str, ensure_ascii=False)}],
            "structuredContent": obj if isinstance(obj, dict) else {"result": obj}, "isError": False}
