"""MCP servers that give AI apps access to only the Home Assistant devices you pick.

(Servers for computers, commands and other MCP servers live in machines.py; this file routes to them.)

Each server has its own address (http://<host>:8000/mcp/<key>) and its own device list.
Every device or entity is either "read" (can see it) or "control" (can see and use it).
Any MCP client works: Claude, ChatGPT, Gemini CLI, Cursor, the signed-in AIs inside this
gateway, and so on. Transport: MCP Streamable HTTP, JSON responses, no sessions needed.
"""
import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiohttp import ClientError, ClientTimeout, WSMsgType, web

log = logging.getLogger("gateway")

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "personal-ai-gateway-devices", "title": "Home Assistant devices", "version": "1.0.0"}
FORBIDDEN_DATA = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}
MODES = ("read", "control")
KINDS = ("ha", "computer", "command", "remote")


class HAError(Exception):
    pass


# ------------------------------------------------------------ Home Assistant connection

class HA:
    """Talks to Home Assistant: the Supervisor proxy inside the add-on, or HA_URL + HA_TOKEN elsewhere."""

    def __init__(self, store, session):
        self.store, self.session = store, session
        self._reg: tuple[float, dict] | None = None

    def conn(self) -> tuple[str, str, str]:
        sup = os.environ.get("SUPERVISOR_TOKEN", "")
        if sup:
            return "http://supervisor/core/api", "ws://supervisor/core/websocket", sup
        m = self.store.data.get("mcp", {})
        url = (os.environ.get("HA_URL") or m.get("ha_url") or "").strip().rstrip("/")
        tok = (os.environ.get("HA_TOKEN") or m.get("ha_token") or "").strip()
        if not url or not tok:
            return "", "", ""
        ws = ("wss://" + url[8:] if url.startswith("https://") else "ws://" + url.removeprefix("http://")) + "/api/websocket"
        return url + "/api", ws, tok

    def configured(self) -> bool:
        return bool(self.conn()[2])

    def locked(self) -> bool:
        """True when the connection comes from the add-on or environment, not the page."""
        return bool(os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_URL"))

    async def rest(self, method: str, path: str, body=None, params=None):
        base, _, tok = self.conn()
        if not tok:
            raise HAError("Home Assistant isn't connected. Set it up on the gateway's MCP tab.")
        try:
            async with self.session.request(method, base + path, json=body, params=params,
                                            headers={"Authorization": f"Bearer {tok}"},
                                            timeout=ClientTimeout(total=30)) as r:
                text = await r.text()
                if r.status >= 400:
                    raise HAError(f"Home Assistant said {r.status}: {text[:300]}")
                return json.loads(text) if text else None
        except (ClientError, asyncio.TimeoutError) as e:
            raise HAError(f"Can't reach Home Assistant: {type(e).__name__} {e}") from e

    async def ws(self, *commands: dict) -> list:
        _, url, tok = self.conn()
        if not tok:
            raise HAError("Home Assistant isn't connected. Set it up on the gateway's MCP tab.")
        results = []
        try:
            async with self.session.ws_connect(url, timeout=ClientTimeout(total=30), max_msg_size=64 * 1024 * 1024) as w:
                await w.receive_json()  # auth_required
                await w.send_json({"type": "auth", "access_token": tok})
                hello = await w.receive_json()
                if hello.get("type") != "auth_ok":
                    raise HAError("Home Assistant refused the access token.")
                for i, cmd in enumerate(commands, 1):
                    await w.send_json({"id": i, **cmd})
                    while True:
                        msg = await w.receive(timeout=60)
                        if msg.type != WSMsgType.TEXT:
                            raise HAError("Home Assistant closed the connection.")
                        data = json.loads(msg.data)
                        if data.get("id") == i and data.get("type") == "result":
                            if not data.get("success"):
                                raise HAError(str((data.get("error") or {}).get("message", "command failed")))
                            results.append(data.get("result"))
                            break
        except (ClientError, asyncio.TimeoutError) as e:
            raise HAError(f"Can't reach Home Assistant: {type(e).__name__} {e}") from e
        return results

    async def registry(self, fresh: bool = False) -> dict:
        if self._reg and not fresh and time.time() - self._reg[0] < 60:
            return self._reg[1]
        devices, entities, areas = await self.ws({"type": "config/device_registry/list"},
                                                 {"type": "config/entity_registry/list"},
                                                 {"type": "config/area_registry/list"})
        states = {s["entity_id"]: s for s in await self.rest("GET", "/states") or []}
        reg = {"devices": {d["id"]: d for d in devices or []},
               "entities": {e["entity_id"]: e for e in entities or []},
               "areas": {a["area_id"]: a.get("name", a["area_id"]) for a in areas or []},
               "states": states}
        self._reg = (time.time(), reg)
        return reg


# ------------------------------------------------------------ servers (config lives in the Store)

def new_server(name: str, kind: str = "ha") -> dict:
    srv = {"id": secrets.token_hex(4), "kind": kind, "name": name.strip() or "Devices", "enabled": True,
           "key": secrets.token_urlsafe(24), "created": int(time.time())}
    if kind == "ha":
        srv |= {"devices": {}, "entities": {}}
    return srv


class Devices:
    def __init__(self, store, ha: HA, on_change=None):
        self.store, self.ha, self.on_change = store, ha, on_change
        self.calls: list[dict] = []
        self.machines = None  # set by server.py (needs the accounts helper)

    # -- config
    @property
    def cfg(self) -> dict:
        m = self.store.data.setdefault("mcp", {})
        m.setdefault("servers", {})
        m.setdefault("computers", {})
        if "ai_servers" not in m:
            m["ai_servers"] = [m["ai_server"]] if m.get("ai_server") else []
        m["ai_servers"] = [x for x in m["ai_servers"] if x in m["servers"]]
        m["ai_server"] = m["ai_servers"][0] if m["ai_servers"] else ""
        for srv in m["servers"].values():
            srv.setdefault("kind", "ha")
        return m

    def servers(self) -> dict:
        return self.cfg["servers"]

    def by_key(self, key: str) -> dict | None:
        import hmac
        for s in self.servers().values():
            if s.get("enabled", True) and key and hmac.compare_digest(s["key"].encode(), key.encode()):
                return s
        return None

    def save_server(self, spec: dict) -> dict:
        sid = str(spec.get("id") or "")
        srv = self.servers().get(sid)
        if not srv:
            if sid:
                raise ValueError("That MCP server is gone.")
            kind = str(spec.get("kind") or "ha")
            if kind not in KINDS:
                raise ValueError("Unknown kind of MCP server.")
            srv = new_server(str(spec.get("name") or {"ha": "Devices", "computer": "Computer", "command": "Command",
                                                       "remote": "Remote"}[kind]), kind)
        if "name" in spec:
            srv["name"] = str(spec["name"]).strip()[:60] or srv["name"]
        if "enabled" in spec:
            srv["enabled"] = bool(spec["enabled"])
        if srv["kind"] == "ha":
            for k in ("devices", "entities"):
                if k in spec and isinstance(spec[k], dict):
                    srv[k] = {str(i): m for i, m in spec[k].items() if m in MODES}
            if "hidden_tools" in spec and isinstance(spec["hidden_tools"], list):
                srv["hidden_tools"] = [str(x) for x in spec["hidden_tools"]]
        else:
            self.machines.apply(srv, spec)
        self.servers()[srv["id"]] = srv
        self._saved()
        return srv

    def delete(self, sid: str) -> None:
        self.servers().pop(sid, None)
        if self.machines:
            self.machines.reset(sid)
        self._saved()

    def regenerate(self, sid: str) -> dict:
        srv = self.servers()[sid]
        srv["key"] = secrets.token_urlsafe(24)
        self._saved()
        return srv

    def set_settings(self, b: dict) -> None:
        if "ai_server" in b:  # older single choice (still used by the Home Assistant integration)
            b = {**b, "ai_servers": [b["ai_server"]] if b["ai_server"] else []}
        if "ai_servers" in b:
            ids = [str(x) for x in b["ai_servers"] or [] if str(x)]
            if any(x not in self.servers() for x in ids):
                raise ValueError("Unknown MCP server.")
            self.cfg["ai_servers"] = list(dict.fromkeys(ids))
            self.cfg["ai_server"] = ids[0] if ids else ""
        for k in ("ha_url", "ha_token"):
            if k in b and not self.ha.locked():
                self.cfg[k] = str(b[k]).strip()
        self.ha._reg = None
        self._saved()

    def _saved(self):
        self.store.save()
        if self.on_change:
            self.on_change()

    # -- what a server may touch
    async def allowed(self, srv: dict) -> dict[str, str]:
        reg = await self.ha.registry()
        out: dict[str, str] = {}
        for eid, e in reg["entities"].items():
            mode = srv["devices"].get(e.get("device_id") or "")
            if mode and not e.get("disabled_by"):
                out[eid] = mode
        for eid, mode in srv["entities"].items():
            out[eid] = mode
        return {e: m for e, m in out.items() if e in reg["states"]}

    async def catalog(self) -> dict:
        reg = await self.ha.registry(fresh=True)
        st = reg["states"]
        by_dev: dict[str, list] = {}
        for eid, e in reg["entities"].items():
            if e.get("device_id") and not e.get("disabled_by") and eid in st:
                by_dev.setdefault(e["device_id"], []).append(self._ent(eid, e, st[eid]))
        devices = []
        for did, d in reg["devices"].items():
            if d.get("disabled_by") or did not in by_dev:
                continue
            devices.append({"id": did, "name": d.get("name_by_user") or d.get("name") or did,
                            "area": reg["areas"].get(d.get("area_id") or "", ""),
                            "model": " ".join(x for x in (d.get("manufacturer"), d.get("model")) if x),
                            "entities": sorted(by_dev[did], key=lambda x: x["entity_id"])})
        loose = [self._ent(eid, reg["entities"].get(eid, {}), s) for eid, s in st.items()
                 if not (reg["entities"].get(eid) or {}).get("device_id")]
        devices.sort(key=lambda d: (d["area"] or "~", d["name"].lower()))
        return {"devices": devices, "entities": sorted(loose, key=lambda x: x["entity_id"])}

    @staticmethod
    def _ent(eid, e, s) -> dict:
        return {"entity_id": eid, "name": (s.get("attributes") or {}).get("friendly_name") or e.get("name") or eid,
                "state": s.get("state"), "category": e.get("entity_category") or ""}

    # -- MCP protocol
    async def handle(self, request: web.Request) -> web.StreamResponse:
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
                "Access-Control-Expose-Headers": "Mcp-Session-Id"}
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors)
        key = request.match_info.get("key", "")
        auth = request.headers.get("Authorization", "")
        if not key and auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        srv = self.by_key(key)
        if not srv:
            return web.json_response({"error": "Unknown or disabled MCP address. Copy it again from the gateway's MCP tab."},
                                     status=401, headers=cors)
        if request.method == "GET":
            return web.Response(status=405, headers={**cors, "Allow": "POST"}, text="This server only answers POST requests.")
        if request.method == "DELETE":
            return web.Response(status=200, headers=cors)
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response(rpc_error(None, -32700, "Parse error"), status=400, headers=cors)
        if isinstance(msg, list):
            replies = [r for r in [await self.route(srv, m) for m in msg] if r is not None]
            return web.json_response(replies, headers=cors) if replies else web.Response(status=202, headers=cors)
        reply = await self.route(srv, msg)
        if reply is None:
            return web.Response(status=202, headers=cors)
        return web.json_response(reply, headers=cors)

    async def route(self, srv: dict, m: dict):
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid request")
        if srv.get("kind", "ha") != "ha":
            return await self.machines.dispatch(srv, m, PROTOCOLS, SERVER_INFO)
        return await self.dispatch(srv, m)

    async def dispatch(self, srv: dict, m: dict):
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if mid is None:  # notification (initialized, cancelled ...) or a response to us
            return None
        try:
            if method == "initialize":
                want = str(params.get("protocolVersion") or "")
                return rpc_ok(mid, {
                    "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[1],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {**SERVER_INFO, "title": f"Home Assistant: {srv['name']}"},
                    "instructions": ("Access to a chosen set of Home Assistant devices. Start with list_devices to see "
                                     "what you may use. Devices marked can_control can be changed with call_action; "
                                     "the others are read-only.")})
            if method == "ping":
                return rpc_ok(mid, {})
            if method == "tools/list":
                hidden = set(srv.get("hidden_tools") or [])
                return rpc_ok(mid, {"tools": [t for t in TOOLS if t["name"] not in hidden]})
            if method in ("resources/list", "prompts/list"):
                return rpc_ok(mid, {method.split("/")[0]: []})
            if method == "resources/templates/list":
                return rpc_ok(mid, {"resourceTemplates": []})
            if method == "tools/call":
                return rpc_ok(mid, await self.call_tool(srv, str(params.get("name") or ""), params.get("arguments") or {}))
            return rpc_error(mid, -32601, f"Method not found: {method}")
        except HAError as e:
            return rpc_ok(mid, tool_text(str(e), error=True)) if method == "tools/call" else rpc_error(mid, -32000, str(e))

    async def call_tool(self, srv: dict, name: str, args: dict) -> dict:
        if name in (srv.get("hidden_tools") or []):
            return tool_text(f"{name} is turned off for this MCP server.", error=True)
        allowed = await self.allowed(srv)
        reg = self.ha._reg[1]
        try:
            if name == "list_devices":
                return tool_json(self._listing(srv, allowed, reg))
            eid = str(args.get("entity_id") or "").strip()
            if name in ("get_state", "get_history", "list_actions", "call_action") and eid not in allowed:
                return tool_text(f"{eid or 'That entity'} isn't shared with this MCP server. Use list_devices to see what is.", error=True)
            if name == "get_state":
                s = await self.ha.rest("GET", f"/states/{eid}") or reg["states"].get(eid, {})
                return tool_json(trim_state(s, allowed[eid]))
            if name == "get_history":
                hours = max(1, min(int(args.get("hours") or 24), 168))
                start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                data = await self.ha.rest("GET", f"/history/period/{start}",
                                          params={"filter_entity_id": eid, "minimal_response": "", "no_attributes": ""})
                pts = [{"state": p.get("state"), "at": p.get("last_changed")} for p in (data[0] if data else [])]
                return tool_json({"entity_id": eid, "hours": hours, "changes": len(pts),
                                  "history": pts[-200:]})
            if name == "list_actions":
                domain = eid.split(".", 1)[0]
                services = await self.ha.rest("GET", "/services") or []
                acts = next((s.get("services", {}) for s in services if s.get("domain") == domain), {})
                return tool_json({"entity_id": eid, "can_control": allowed[eid] == "control",
                                  "actions": {k: {"description": v.get("description", ""),
                                                  "fields": sorted((v.get("fields") or {}).keys())} for k, v in acts.items()}})
            if name == "call_action":
                if allowed[eid] != "control":
                    return tool_text(f"{eid} is shared as read-only. Change it to 'Can control' in the gateway to allow this.", error=True)
                domain = eid.split(".", 1)[0]
                action = str(args.get("action") or "").strip()
                svc_domain, _, svc = action.rpartition(".")
                svc_domain = svc_domain or domain
                if svc_domain not in (domain, "homeassistant") or not svc.replace("_", "").isalnum():
                    return tool_text(f"Use an action from the {domain} domain (see list_actions), for example {domain}.turn_on.", error=True)
                data = args.get("data") or {}
                if not isinstance(data, dict) or FORBIDDEN_DATA & set(data):
                    return tool_text("data must be an object and can't name other targets (entity_id, device_id, area_id...).", error=True)
                await self.ha.rest("POST", f"/services/{svc_domain}/{svc}", {**data, "entity_id": eid})
                self.calls.insert(0, {"t": time.time(), "server": srv["name"], "entity_id": eid, "action": f"{svc_domain}.{svc}"})
                del self.calls[40:]
                await asyncio.sleep(0.7)
                new = await self.ha.rest("GET", f"/states/{eid}")
                return tool_json({"ok": True, "called": f"{svc_domain}.{svc}", "entity_id": eid,
                                  "state_now": trim_state(new, "control") if new else None})
        except HAError as e:
            return tool_text(str(e), error=True)
        except (TypeError, ValueError) as e:
            return tool_text(f"Bad arguments: {e}", error=True)
        return tool_text(f"Unknown tool: {name}", error=True)

    @staticmethod
    def _listing(srv, allowed, reg) -> dict:
        groups: dict[str, dict] = {}
        for eid, mode in sorted(allowed.items()):
            e = reg["entities"].get(eid, {})
            did = e.get("device_id") or ""
            d = reg["devices"].get(did, {})
            key = did or "_other"
            g = groups.setdefault(key, {"device": d.get("name_by_user") or d.get("name") or "Other entities",
                                        "area": reg["areas"].get(d.get("area_id") or e.get("area_id") or "", ""),
                                        "entities": []})
            s = reg["states"].get(eid, {})
            a = s.get("attributes") or {}
            g["entities"].append({"entity_id": eid, "name": a.get("friendly_name", eid), "state": s.get("state"),
                                  "unit": a.get("unit_of_measurement"), "can_control": mode == "control"})
        return {"server": srv["name"], "devices": list(groups.values())}


def trim_state(s: dict, mode: str) -> dict:
    attrs = {k: v for k, v in (s.get("attributes") or {}).items()
             if k not in ("entity_picture", "supported_features") and len(json.dumps(v, default=str)) < 2000}
    return {"entity_id": s.get("entity_id"), "state": s.get("state"), "attributes": attrs,
            "last_changed": s.get("last_changed"), "can_control": mode == "control"}


def rpc_ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def tool_text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def tool_json(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str, ensure_ascii=False)}],
            "structuredContent": obj if isinstance(obj, dict) else {"result": obj}, "isError": False}


EID = {"type": "string", "description": "Entity id from list_devices, for example light.kitchen"}
TOOLS = [
    {"name": "list_devices", "title": "List shared devices",
     "description": "Every Home Assistant device and entity shared with you, with its current state and whether you may control it.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "get_state", "title": "Get state",
     "description": "Current state and attributes of one shared entity.",
     "inputSchema": {"type": "object", "properties": {"entity_id": EID}, "required": ["entity_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_history", "title": "Get history",
     "description": "How a shared entity's state changed over the last hours (1-168, default 24).",
     "inputSchema": {"type": "object", "properties": {"entity_id": EID, "hours": {"type": "integer", "minimum": 1, "maximum": 168}},
                     "required": ["entity_id"]}, "annotations": {"readOnlyHint": True}},
    {"name": "list_actions", "title": "List actions",
     "description": "The actions (services) available for a shared entity's domain and the data fields each takes.",
     "inputSchema": {"type": "object", "properties": {"entity_id": EID}, "required": ["entity_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "call_action", "title": "Control a device",
     "description": "Run a Home Assistant action on one shared entity marked can_control, e.g. action 'light.turn_on' with "
                    "data {'brightness_pct': 50}, 'switch.toggle', 'climate.set_temperature' with {'temperature': 72}.",
     "inputSchema": {"type": "object", "properties": {
         "entity_id": EID,
         "action": {"type": "string", "description": "domain.action or just the action, e.g. turn_on"},
         "data": {"type": "object", "description": "Extra fields for the action (optional)"}},
         "required": ["entity_id", "action"]},
     "annotations": {"destructiveHint": False, "readOnlyHint": False}},
]
