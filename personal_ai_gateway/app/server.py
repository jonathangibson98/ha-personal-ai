#!/usr/bin/env python3
"""Personal AI Gateway: one OpenAI-compatible endpoint for every model you can reach.

* Many providers and API keys, with rotation, cooldowns and fallback routes
* Your own models: manage Ollama, search the model libraries, download with progress
* Custom HTTP providers for services that aren't OpenAI-compatible
Runs as a Home Assistant add-on (ingress) or as a plain Docker container.
"""
import asyncio
import hmac
import json
import logging
import os
import shutil
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, FormData, web

from accounts import ACCOUNTS, Accounts
import socket

from hub import Hub, OllamaError
from machines import Machines, UpstreamError
from mcp import HA, Devices, HAError
from rotator import KeyPool, kid
from store import DATA_DIR, PRESETS, Store
from tunnel import Tunnel
from upstream import Gateway, err, sanitize

VERSION = "2.0.1"
TEST_MODELS = {"gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini", "groq": "llama-3.3-70b-versatile",
               "mistral": "mistral-small-latest", "deepseek": "deepseek-chat", "cerebras": "llama3.1-8b"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")
APP_DIR = Path(__file__).parent
INGRESS_IP = "172.30.32.2" if os.environ.get("SUPERVISOR_TOKEN") else ""

STORE: Store
POOL: KeyPool
GW: Gateway
HUB: Hub
ACC: Accounts
DEV: Devices
TUN: Tunnel
START = time.time()


def secret_ok(sent: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(sent.encode(), expected.encode())


@web.middleware
async def middleware(request, handler):
    path = request.path
    if path.startswith("/v1"):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            auth = request.headers.get("Authorization", "")
            sent = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
            if not (secret_ok(sent, STORE.proxy_key) or secret_ok(sent, STORE.data.get("openwebui_key", ""))):
                resp = err(401, "Invalid API key. Use the proxy key shown in the gateway's Settings.", "invalid_api_key")
            else:
                try:
                    resp = await handler(request)
                except web.HTTPException as e:
                    resp = err(e.status, e.reason)
                except Exception as e:  # noqa: BLE001
                    log.exception("Unhandled error")
                    resp = err(500, f"Gateway error: {e}")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, x-api-key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp
    if path.startswith("/api/") and request.remote != INGRESS_IP:
        if not secret_ok(request.headers.get("X-Admin-Token", ""), STORE.admin_password):
            return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (OllamaError, HAError, UpstreamError, ValueError, KeyError) as e:
        return web.json_response({"error": str(e) or type(e).__name__}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("Unhandled error")
        return web.json_response({"error": f"Unexpected error: {e}"}, status=500)


# ------------------------------------------------------------------ OpenAI-compatible API

def json_proxy(path: str):
    async def handler(request):
        try:
            body = await request.json()
        except ValueError:
            return err(400, "Body must be JSON.", "invalid_request_error")
        model = str(body.get("model") or "")
        if not model:
            return err(400, "'model' is required.", "invalid_request_error")
        chat = path == "/chat/completions"

        def build(umodel, pname, kind):
            b = {**body, "model": umodel}
            return {"json": sanitize(pname, kind, b) if chat else b}

        return await GW.forward(request, path, model, build, stream=bool(body.get("stream")) and chat)
    return handler


async def transcriptions(request):
    form = await request.post()
    model = str(form.get("model") or "")
    if not model:
        return err(400, "'model' is required.", "invalid_request_error")
    fields = list(form.items())

    def build(umodel, pname, kind):
        fd = FormData()
        for k, v in fields:
            if k == "model":
                fd.add_field("model", umodel)
            elif hasattr(v, "file"):
                v.file.seek(0)
                fd.add_field(k, v.file.read(), filename=v.filename, content_type=v.content_type)
            else:
                fd.add_field(k, str(v))
        return {"data": fd}

    return await GW.forward(request, "/audio/transcriptions", model, build)


async def v1_models(request):
    items = await GW.all_models()
    return web.json_response({"object": "list", "data": [
        {"id": m["id"], "object": "model", "created": 0, "owned_by": m["provider"]} for m in items]})


async def v1_model(request):
    mid = request.match_info["id"]
    for m in await GW.all_models():
        if m["id"] == mid:
            return web.json_response({"id": mid, "object": "model", "created": 0, "owned_by": m["provider"]})
    return err(404, f"Model '{mid}' not found.", "model_not_found")


# ------------------------------------------------------------------ admin API

async def api_state(request):
    providers = {}
    for name, p in STORE.providers().items():
        saved = set(p.get("keys") or [])
        providers[name] = {
            "kind": p["kind"], "cli": p.get("cli", ""), "enabled": p.get("enabled", True), "mode": p.get("mode", ""),
            "base_url": p.get("base_url") or "",
            "recipe": p.get("recipe"), "ignore": p.get("ignore", []), "whitelist": p.get("whitelist", []),
            "keys": [{**POOL.info(name, k), "source": "saved" if k in saved else "env"} for k in STORE.keys(name)],
        }
    return web.json_response({
        "version": VERSION, "uptime_s": int(time.time() - START), "proxy_key": STORE.proxy_key,
        "providers": providers, "routes": STORE.data["routes"], "settings": STORE.settings(),
        "presets": {k: v for k, v in PRESETS.items()}, "recent": list(GW.recent)[:30],
        "ollama": await HUB.status(), "addon": bool(INGRESS_IP), "bundled": False, "disk_free": None,
        "public": TUN.info(), "open_webui": await open_webui_info(request.app["session"]),
        "env_locked": [k for k, e in (("ollama_url", "OLLAMA_URL"), ("ollama_preload", "OLLAMA_PRELOAD"))
                       if os.environ.get(e, "").strip()],
    })


async def api_provider_save(request):
    b = await request.json()
    STORE.upsert_provider(str(b.get("name", "")), b)
    name = str(b["name"]).strip().lower()
    if b.get("key"):
        STORE.add_key(name, str(b["key"]).strip())
    GW._models.pop(name, None)
    return web.json_response({"ok": True})


async def api_provider_delete(request):
    STORE.delete_provider(request.match_info["name"])
    return web.json_response({"ok": True})


async def api_key_add(request):
    name = request.match_info["name"]
    key = str((await request.json()).get("key", "")).strip()
    if not key:
        raise ValueError("Paste a key first.")
    STORE.add_key(name, key)
    GW._models.pop(name, None)
    return web.json_response({"ok": True})


async def api_key_delete(request):
    ok = STORE.remove_key(request.match_info["name"], request.match_info["kid"])
    if not ok:
        raise ValueError("That key comes from the add-on options or environment; remove it there.")
    return web.json_response({"ok": True})


async def api_key_reset(request):
    name = request.match_info["name"]
    for k in STORE.keys(name):
        if kid(k) == request.match_info["kid"]:
            POOL.reset(name, k)
    return web.json_response({"ok": True})


async def api_provider_models(request):
    name = request.match_info["name"]
    models = await GW.list_models(name, force=True)
    return web.json_response({"models": models, "error": GW.list_errors.get(name, "")})


async def api_provider_test(request):
    name = request.match_info["name"]
    b = await request.json() if request.can_read_body else {}
    prov = STORE.providers().get(name)
    if not prov:
        raise ValueError("Unknown provider.")
    model = str(b.get("model") or "").strip()
    if not model:
        models = await GW.list_models(name, force=True)
        model = models[0] if models else TEST_MODELS.get(name, "")
        if not model:
            return web.json_response({"ok": False, "error": "Couldn't list models: " + (GW.list_errors.get(name) or "none returned")
                                      + ". Type a model name in the box next to Test."})
    body = {"model": f"{name}/{model}", "max_tokens": 30,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}
    t0 = time.time()
    resp = await GW.forward(None, "/chat/completions", body["model"],
                            lambda um, pn, kind: {"json": sanitize(pn, kind, {**body, "model": um})})
    raw = json.loads(resp.body or b"{}")
    ms = int((time.time() - t0) * 1000)
    if resp.status == 200:
        reply = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return web.json_response({"ok": True, "model": model, "reply": str(reply)[:200], "ms": ms})
    return web.json_response({"ok": False, "model": model, "error": (raw.get("error") or {}).get("message", str(raw))[:400], "ms": ms})


async def api_ollama_test(request):
    url = str((await request.json()).get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("The address must start with http:// or https://")
    return web.json_response(await HUB.status(url or None))


async def api_routes(request):
    STORE.set_routes(await request.json())
    return web.json_response({"ok": True})


async def api_settings(request):
    STORE.set_settings(await request.json())
    return web.json_response({"ok": True})


async def api_regen(request):
    return web.json_response({"proxy_key": STORE.regenerate_proxy_key()})


async def api_all_models(request):
    return web.json_response({"models": await GW.all_models()})


async def api_playground(request):
    return await json_proxy("/chat/completions")(request)


# -- local models
async def api_installed(request):
    return web.json_response({"models": await HUB.installed()})


async def api_pull(request):
    b = await request.json()
    return web.json_response(await HUB.start_pull(str(b.get("name", ""))))


async def api_model_delete(request):
    await HUB.delete(str((await request.json()).get("name", "")))
    GW._models.pop("ollama", None)
    return web.json_response({"ok": True})


async def api_model_unload(request):
    await HUB.unload(str((await request.json()).get("name", "")))
    return web.json_response({"ok": True})


async def api_search(request):
    q = request.query.get("q", "")
    src = request.query.get("source", "all")
    return web.json_response(await HUB.search(q, src if src in ("all", "ollama", "hf") else "all"))


async def api_quants(request):
    return web.json_response({"quants": await HUB.hf_quants(request.query.get("repo", ""))})


async def api_jobs(request):
    for j in HUB.jobs.values():  # a finished download changes what Ollama lists
        if j["status"] == "done" and not j.get("cache_cleared"):
            j["cache_cleared"] = True
            GW._models.pop("ollama", None)
    return web.json_response({"jobs": sorted(HUB.jobs.values(), key=lambda j: -j["started"])})


async def api_job_cancel(request):
    return web.json_response({"ok": HUB.cancel(request.match_info["id"])})


# -- sign in with an account
def _acct(request) -> str:
    a = request.match_info["acct"]
    if a not in ACCOUNTS:
        raise ValueError("Unknown account type.")
    return a


async def api_accounts(request):
    return web.json_response({"accounts": await ACC.status(), "sessions": [s.view() for s in ACC.sessions.values()
                                                                          if s.status == "running"]})


async def api_account_install(request):
    return web.json_response((await ACC.install(_acct(request))).view())


async def api_account_login(request):
    return web.json_response((await ACC.login(_acct(request))).view())


async def api_account_logout(request):
    await ACC.logout(_acct(request))
    return web.json_response({"ok": True})


async def api_account_provider(request):
    ACC.add_provider(_acct(request))
    return web.json_response({"ok": True})


async def api_session(request):
    s = ACC.sessions.get(request.match_info["sid"])
    if not s:
        raise ValueError("That window is gone. Start again.")
    return web.json_response(s.view())


async def api_session_input(request):
    s = ACC.sessions.get(request.match_info["sid"])
    if not s:
        raise ValueError("That window is gone. Start again.")
    s.send(str((await request.json()).get("text", "")))
    return web.json_response(s.view())


async def api_session_cancel(request):
    s = ACC.sessions.get(request.match_info["sid"])
    if s:
        s.kill()
    return web.json_response({"ok": True})


# -- devices (MCP servers)
async def api_mcp(request):
    servers = [{**s, **({"status": DEV.machines.status(s)} if s.get("kind", "ha") != "ha" else {})}
               for s in DEV.servers().values()]
    return web.json_response({
        "servers": servers, "ai_server": DEV.cfg["ai_server"], "ai_servers": DEV.cfg["ai_servers"],
        "computers": list(DEV.cfg["computers"].values()), "public_key": DEV.machines.computers.public_key(),
        "ha_connected": DEV.ha.configured(),
        "ha_locked": DEV.ha.locked(), "ha_url": "" if DEV.ha.locked() else DEV.cfg.get("ha_url", ""),
        "ha_token_set": bool(DEV.cfg.get("ha_token")), "calls": DEV.calls[:15], "port": int(os.environ.get("PORT", "8000"))})


async def api_mcp_save(request):
    srv = DEV.save_server(await request.json())
    return web.json_response(srv)


async def api_mcp_delete(request):
    DEV.delete(request.match_info["sid"])
    return web.json_response({"ok": True})


async def api_mcp_regen(request):
    return web.json_response(DEV.regenerate(request.match_info["sid"]))


async def api_mcp_settings(request):
    DEV.set_settings(await request.json())
    return web.json_response({"ok": True})


async def api_mcp_check(request):
    srv = DEV.servers().get(request.match_info["sid"])
    if not srv:
        raise ValueError("That MCP server is gone.")
    if srv.get("kind", "ha") == "ha":
        from mcp import TOOLS
        return web.json_response({"ok": DEV.ha.configured(), "tools": [{"name": t["name"], "description": t["description"]} for t in TOOLS]})
    return web.json_response(await DEV.machines.check(srv))


def _computer(request) -> dict:
    c = DEV.cfg["computers"].get(request.match_info["cid"])
    if not c:
        raise ValueError("That computer is gone.")
    return c


async def api_computer_save(request):
    return web.json_response(DEV.machines.computers.save(await request.json()))


async def api_computer_delete(request):
    DEV.machines.computers.delete(request.match_info["cid"], DEV.servers())
    return web.json_response({"ok": True})


async def api_computer_test(request):
    return web.json_response(await DEV.machines.computers.test(_computer(request)))


async def api_computer_key(request):
    pw = str((await request.json()).get("password") or "")
    return web.json_response(await DEV.machines.computers.install_key(_computer(request), pw))


async def api_computer_forget(request):
    DEV.machines.computers.forget_host(_computer(request))
    return web.json_response({"ok": True})


async def api_mcp_catalog(request):
    return web.json_response(await DEV.catalog())


async def mcp_endpoint(request):
    return await DEV.handle(request)


async def api_public(request):
    b = await request.json()
    STORE.set_settings({k: b[k] for k in ("public_mode", "public_url", "cf_token") if k in b})
    await TUN.apply()
    await asyncio.sleep(0.2)
    return web.json_response(TUN.info())


async def api_public_status(request):
    return web.json_response(TUN.info())


# -- Open WebUI (the chat add-on next to this one)
SHARE = Path("/share/personal_ai_gateway")
_owui = {"t": 0.0, "v": None}


def write_connection() -> None:
    """Tell the Open WebUI add-on how to reach this gateway (it maps /share read-only)."""
    if not Path("/share").is_dir():
        return
    try:
        SHARE.mkdir(parents=True, exist_ok=True)
        f = SHARE / "connection.json"
        f.write_text(json.dumps({
            "base_url": f"http://{socket.gethostname()}:{int(os.environ.get('PORT', '8000'))}/v1",
            "api_key": STORE.data["openwebui_key"], "written_by": f"Personal AI Gateway {VERSION}"}, indent=2))
        os.chmod(f, 0o600)
    except OSError as e:
        log.warning("Couldn't write the Open WebUI connection file: %s", e)


async def open_webui_info(session) -> dict:
    """Is the Open WebUI add-on installed/running, and on which port?"""
    url = os.environ.get("OPEN_WEBUI_URL", "").strip()
    port = os.environ.get("OPEN_WEBUI_PORT", "").strip()
    if url or port.isdigit():  # Docker: set by docker-compose next to the open-webui service
        return {"installed": True, "running": True, "url": url, "port": int(port) if port.isdigit() else None}
    tok = os.environ.get("SUPERVISOR_TOKEN")
    if not tok:
        return {"installed": False}
    if _owui["v"] is not None and time.time() - _owui["t"] < 20:
        return _owui["v"]
    out = {"installed": False}
    hdr = {"Authorization": f"Bearer {tok}"}
    try:
        async with session.get("http://supervisor/addons/self/info", headers=hdr, timeout=ClientTimeout(total=5)) as r:
            me = (await r.json()).get("data") or {}
        sibling = str(me.get("slug", "")).rsplit("personal_ai_gateway", 1)[0] + "open_webui"
        async with session.get(f"http://supervisor/addons/{sibling}/info", headers=hdr, timeout=ClientTimeout(total=5)) as r:
            if r.status == 200:
                d = (await r.json()).get("data") or {}
                if d.get("version"):
                    port = (d.get("network") or {}).get("8080/tcp")
                    out = {"installed": True, "running": d.get("state") == "started", "port": port, "url": "",
                           "slug": sibling, "version": d.get("version")}
                else:
                    out = {"installed": False, "available": True, "slug": sibling}
    except Exception as e:  # noqa: BLE001
        out = {"installed": False, "error": str(e)[:120]}
    _owui.update(t=time.time(), v=out)
    return out


async def index(request):
    return web.FileResponse(APP_DIR / "ui" / "index.html", headers={"Cache-Control": "no-store"})


async def health(request):
    return web.json_response({"ok": True, "version": VERSION})


# ------------------------------------------------------------------ startup

async def background(app):
    async def saver():
        while True:
            await asyncio.sleep(30)
            POOL.save()
    tasks = [asyncio.create_task(saver()), asyncio.create_task(HUB.preload())]
    old = DATA_DIR / "ollama"  # models from the built-in Ollama that this add-on no longer has
    if old.is_dir():
        def wipe():
            size = sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
            shutil.rmtree(old, ignore_errors=True)
            log.info("Removed the old built-in Ollama folder (%s freed)", f"{size / 1e9:.1f} GB")
        tasks.append(asyncio.create_task(asyncio.to_thread(wipe)))
    write_connection()
    try:
        mcp_runner = await Tunnel.start_listener(mcp_endpoint)
    except OSError as e:  # the HTTPS tunnel can't work, but everything else still should
        mcp_runner = None
        log.error("MCP-only listener for the HTTPS tunnel failed to start: %s", e)
    await TUN.apply()
    yield
    await TUN.stop()
    if mcp_runner:
        await mcp_runner.cleanup()
    for t in tasks:
        t.cancel()
    await DEV.machines.close()
    POOL.save()
    await app["session"].close()


async def make_app() -> web.Application:
    global STORE, POOL, GW, HUB, ACC, DEV
    STORE = Store()
    POOL = KeyPool(DATA_DIR / "usage.json", STORE.setting("cooldowns"))
    session = ClientSession(timeout=ClientTimeout(total=None))
    GW, HUB = Gateway(STORE, POOL, session), Hub(STORE, session)
    global TUN
    TUN = Tunnel(STORE)
    ACC = Accounts(STORE, int(os.environ.get("PORT", "8000")))
    GW.accounts = ACC
    DEV = Devices(STORE, HA(STORE, session), on_change=ACC.configure_mcp)
    DEV.machines = Machines(STORE, session, ACC, DEV.calls)
    DEV.cfg  # migrate older settings (single ai_server, servers without a kind)
    ACC.configure_mcp()
    app = web.Application(middlewares=[middleware], client_max_size=64 * 1024 * 1024)
    app["session"] = session
    app.cleanup_ctx.append(background)
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.route("*", "/v1/chat/completions", json_proxy("/chat/completions")),
        web.route("*", "/v1/embeddings", json_proxy("/embeddings")),
        web.route("*", "/v1/images/generations", json_proxy("/images/generations")),
        web.route("*", "/v1/audio/speech", json_proxy("/audio/speech")),
        web.route("*", "/v1/audio/transcriptions", transcriptions),
        web.get("/v1/models", v1_models), web.get("/v1/models/{id:.+}", v1_model),
        web.route("OPTIONS", "/v1/{tail:.*}", health),
        web.get("/api/state", api_state),
        web.post("/api/providers", api_provider_save),
        web.delete("/api/providers/{name}", api_provider_delete),
        web.post("/api/providers/{name}/keys", api_key_add),
        web.delete("/api/providers/{name}/keys/{kid}", api_key_delete),
        web.post("/api/providers/{name}/keys/{kid}/reset", api_key_reset),
        web.get("/api/providers/{name}/models", api_provider_models),
        web.post("/api/providers/{name}/test", api_provider_test),
        web.post("/api/ollama/test", api_ollama_test),
        web.put("/api/routes", api_routes), web.put("/api/settings", api_settings),
        web.post("/api/proxy-key/regenerate", api_regen),
        web.get("/api/all-models", api_all_models),
        web.post("/api/playground", api_playground),
        web.get("/api/models/installed", api_installed),
        web.post("/api/models/pull", api_pull),
        web.post("/api/models/delete", api_model_delete),
        web.post("/api/models/unload", api_model_unload),
        web.get("/api/models/search", api_search),
        web.get("/api/models/quants", api_quants),
        web.get("/api/jobs", api_jobs),
        web.post("/api/jobs/{id}/cancel", api_job_cancel),
        web.get("/api/accounts", api_accounts),
        web.post("/api/accounts/{acct}/install", api_account_install),
        web.post("/api/accounts/{acct}/login", api_account_login),
        web.post("/api/accounts/{acct}/logout", api_account_logout),
        web.post("/api/accounts/{acct}/provider", api_account_provider),
        web.get("/api/sessions/{sid}", api_session),
        web.post("/api/sessions/{sid}/input", api_session_input),
        web.post("/api/sessions/{sid}/cancel", api_session_cancel),
        web.get("/api/mcp", api_mcp),
        web.post("/api/mcp/servers", api_mcp_save),
        web.delete("/api/mcp/servers/{sid}", api_mcp_delete),
        web.post("/api/mcp/servers/{sid}/regenerate", api_mcp_regen),
        web.put("/api/mcp/settings", api_mcp_settings),
        web.get("/api/mcp/catalog", api_mcp_catalog),
        web.post("/api/mcp/servers/{sid}/check", api_mcp_check),
        web.post("/api/mcp/computers", api_computer_save),
        web.delete("/api/mcp/computers/{cid}", api_computer_delete),
        web.post("/api/mcp/computers/{cid}/test", api_computer_test),
        web.post("/api/mcp/computers/{cid}/install-key", api_computer_key),
        web.post("/api/mcp/computers/{cid}/forget-host", api_computer_forget),
        web.put("/api/public", api_public), web.get("/api/public", api_public_status),
        web.route("*", "/mcp", mcp_endpoint),
        web.route("*", "/mcp/{key}", mcp_endpoint),
    ])
    return app


def main():
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(make_app(), host="0.0.0.0", port=port, print=lambda *_: None)


if __name__ == "__main__":
    log.info("Personal AI Gateway %s starting", VERSION)
    main()
