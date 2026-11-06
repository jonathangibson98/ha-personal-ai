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

from hub import Hub, OllamaError, run_bundled
from rotator import KeyPool, kid
from store import DATA_DIR, PRESETS, Store
from upstream import Gateway, err, sanitize

VERSION = "1.1.2"
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
            if not secret_ok(sent, STORE.proxy_key):
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
    except (OllamaError, ValueError, KeyError) as e:
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
            "kind": p["kind"], "enabled": p.get("enabled", True), "mode": p.get("mode", ""),
            "base_url": p.get("base_url") or "",
            "recipe": p.get("recipe"), "ignore": p.get("ignore", []), "whitelist": p.get("whitelist", []),
            "keys": [{**POOL.info(name, k), "source": "saved" if k in saved else "env"} for k in STORE.keys(name)],
        }
    return web.json_response({
        "version": VERSION, "uptime_s": int(time.time() - START), "proxy_key": STORE.proxy_key,
        "providers": providers, "routes": STORE.data["routes"], "settings": STORE.settings(),
        "presets": {k: v for k, v in PRESETS.items()}, "recent": list(GW.recent)[:30],
        "ollama": await HUB.status(), "addon": bool(INGRESS_IP), "bundled": STORE.bundled_enabled(),
        "disk_free": shutil.disk_usage(DATA_DIR).free if STORE.bundled_enabled() else None,
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
    if STORE.bundled_enabled():
        tasks.append(asyncio.create_task(run_bundled(STORE)))
    yield
    for t in tasks:
        t.cancel()
    POOL.save()
    await app["session"].close()


async def make_app() -> web.Application:
    global STORE, POOL, GW, HUB
    STORE = Store()
    POOL = KeyPool(DATA_DIR / "usage.json", STORE.setting("cooldowns"))
    session = ClientSession(timeout=ClientTimeout(total=None))
    GW, HUB = Gateway(STORE, POOL, session), Hub(STORE, session)
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
    ])
    return app


def main():
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(make_app(), host="0.0.0.0", port=port, print=lambda *_: None)


if __name__ == "__main__":
    log.info("Personal AI Gateway %s starting", VERSION)
    main()
