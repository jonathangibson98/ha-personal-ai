"""Public HTTPS address for the MCP servers.

Apps like Claude only connect to MCP servers over https://. The gateway serves plain http on
the LAN, so this gives the MCP servers an https address in one of three ways:

  quick  a Cloudflare quick tunnel: instant, no account, but a new random
         https://<words>.trycloudflare.com address every time the add-on starts
  token  your own Cloudflare tunnel (free account): a fixed address on your domain
  url    you already have https in front of the gateway (Nginx Proxy Manager, DuckDNS,
         Tailscale Funnel, ...): the page just shows that address

Only the MCP endpoints go through the tunnel: cloudflared talks to a second listener on
127.0.0.1:MCP_ONLY_PORT that serves /mcp/<key> and nothing else (no admin page, no /v1 API).
"""
import asyncio
import logging
import os
import re
import shutil
import time

from aiohttp import web

log = logging.getLogger("tunnel")
MCP_ONLY_PORT = int(os.environ.get("MCP_PUBLIC_PORT", "8098"))
QUICK_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Tunnel:
    def __init__(self, store):
        self.store = store
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.url = ""          # the https address while a tunnel is up
        self.state = "off"     # off | starting | up | error
        self.error = ""
        self.log: list[str] = []
        self.since = 0.0

    # -- what the page shows
    def info(self) -> dict:
        mode = self.store.setting("public_mode")
        url = self.store.setting("public_url") if mode in ("url", "token") else self.url
        state = self.state
        if mode == "url":
            state = "up" if url else "error"
        return {"mode": mode, "url": (url or "").rstrip("/"), "state": state, "error": self.error,
                "log": self.log[-25:], "since": self.since, "available": bool(shutil.which("cloudflared")),
                "port": MCP_ONLY_PORT}

    # -- the MCP-only listener cloudflared forwards to
    @staticmethod
    async def start_listener(mcp_handler) -> web.AppRunner:
        app = web.Application(client_max_size=16 * 1024 * 1024)

        async def ok(_request):
            return web.json_response({"ok": True, "service": "Personal AI Gateway MCP"})

        app.add_routes([web.get("/", ok), web.route("*", "/mcp", mcp_handler), web.route("*", "/mcp/{key}", mcp_handler)])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", MCP_ONLY_PORT).start()
        return runner

    # -- cloudflared
    async def apply(self) -> None:
        """Start, restart or stop cloudflared to match the saved settings."""
        await self.stop()
        mode = self.store.setting("public_mode")
        self.error, self.url, self.log = "", "", []
        if mode not in ("quick", "token"):
            self.state = "off"
            return
        if not shutil.which("cloudflared"):
            self.state, self.error = "error", "cloudflared isn't in this image. Update the add-on."
            return
        if mode == "token" and not self.store.data["settings"].get("cf_token"):
            self.state, self.error = "error", "Paste your Cloudflare tunnel token first."
            return
        self.task = asyncio.create_task(self._run(mode))

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None
        await self._kill()
        self.state = "off"

    async def _kill(self) -> None:
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                p.kill()

    async def _run(self, mode: str) -> None:
        delay = 5
        while True:
            self.state, self.since = "starting", time.time()
            if mode == "quick":
                argv = ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{MCP_ONLY_PORT}"]
                env = dict(os.environ)
            else:
                argv = ["cloudflared", "tunnel", "--no-autoupdate", "run"]
                # The token goes in the environment so it never shows in the process list.
                env = {**os.environ, "TUNNEL_TOKEN": self.store.data["settings"].get("cf_token", "")}
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *argv, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            except OSError as e:
                self.state, self.error = "error", f"Couldn't start cloudflared: {e}"
                return
            log.info("Public HTTPS tunnel starting (%s)", mode)
            async for raw in self.proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                self.log.append(re.sub(r"(token|eyJ)[A-Za-z0-9._=-]{20,}", r"\1…", line)[:300])
                del self.log[:-200]
                m = QUICK_RE.search(line)
                if mode == "quick" and m and "api.trycloudflare.com" not in m.group(0):
                    self.url, self.state, self.error = m.group(0), "up", ""
                    log.info("Public HTTPS address for MCP: %s", self.url)
                elif mode == "token" and "Registered tunnel connection" in line:
                    self.state, self.error = "up", ""
                elif " ERR " in line and self.state != "up":
                    self.error = line.split(" ERR ", 1)[1][:200]
            code = await self.proc.wait()
            self.state, self.url = "error", ""
            self.error = self.error or f"cloudflared stopped (exit code {code})"
            log.warning("Public HTTPS tunnel stopped (%s); retrying in %s s", code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
