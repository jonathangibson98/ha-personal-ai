"""Routing, key rotation with failover, and the generic HTTP provider adapter."""
import asyncio
import collections
import json
import time
import uuid

from aiohttp import ClientError, ClientTimeout, web

from rotator import KeyPool, kid
from store import Store

MODEL_CACHE_S = 300


class UpstreamError(Exception):
    pass


def err(status: int, message: str, kind: str = "gateway_error"):
    return web.json_response({"error": {"message": message, "type": kind, "code": status}}, status=status)


# ------------------------------------------------------------ pure helpers (unit tested)

def dig(data, path: str):
    """Follow 'choices.0.message.content' through nested dicts/lists. Returns None if absent."""
    cur = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


GUESS_PATHS = ("choices.0.message.content", "choices.0.text", "message.content", "output", "response",
               "text", "result", "reply", "answer", "content", "data.text", "data.content")


def guess_text(data) -> str | None:
    for p in GUESS_PATHS:
        v = dig(data, p)
        if isinstance(v, str) and v:
            return v
    return None


def flatten(messages: list) -> dict:
    """Turn chat messages into the plain strings a simple web endpoint expects."""
    def text(m):
        c = m.get("content")
        if isinstance(c, list):
            return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return str(c or "")
    system = "\n".join(text(m) for m in messages if m.get("role") == "system")
    turns = [m for m in messages if m.get("role") != "system"]
    last_user = next((text(m) for m in reversed(turns) if m.get("role") == "user"), "")
    transcript = "\n".join(f"{'User' if m.get('role') == 'user' else 'Assistant'}: {text(m)}" for m in turns)
    return {"system": system, "last_user": last_user, "prompt": transcript + "\nAssistant:"}


def render(obj, v: dict):
    """Fill {{placeholders}} in a string / dict / list. A value that is exactly
    {{messages}}, {{temperature}} or {{max_tokens}} keeps its real type."""
    if isinstance(obj, str):
        whole = obj.strip()
        if whole in ("{{messages}}", "{{temperature}}", "{{max_tokens}}"):
            return v.get(whole[2:-2])
        for k, val in v.items():
            if isinstance(val, (str, int, float)):
                obj = obj.replace("{{" + k + "}}", str(val))
        return obj
    if isinstance(obj, dict):
        return {render(k, v): render(x, v) for k, x in obj.items()}
    if isinstance(obj, list):
        return [render(x, v) for x in obj]
    return obj


def completion(model: str, text: str) -> dict:
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def chunk(model: str, cid: str, text: str | None, done: bool = False) -> bytes:
    delta = {"role": "assistant", "content": text} if text is not None else {}
    obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if done else None}]}
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def sanitize(provider: str, kind: str, body: dict) -> dict:
    """Drop parameters that Home Assistant's OpenAI client sends but other providers reject."""
    b = dict(body)
    if kind != "openai" or provider != "openai":
        b.pop("prompt_cache_key", None)
    if not b.get("tools"):
        b.pop("parallel_tool_calls", None)
    return b


# ------------------------------------------------------------ gateway

class Gateway:
    def __init__(self, store: Store, pool: KeyPool, session):
        self.store, self.pool, self.session = store, pool, session
        self._models: dict[str, tuple[float, list[str]]] = {}
        self.recent = collections.deque(maxlen=60)

    # -- model listing / resolving
    def _headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def list_models(self, name: str, force: bool = False) -> list[str]:
        prov = self.store.providers().get(name)
        if not prov or not prov.get("enabled", True):
            return []
        cached = self._models.get(name)
        if cached and not force and time.time() - cached[0] < MODEL_CACHE_S:
            return cached[1]
        ids: list[str] = []
        if prov["kind"] == "http":
            ids = list(prov["recipe"].get("models") or [name])
        else:
            keys = self.store.keys(name) or ([""] if prov["kind"] == "ollama" else [])
            for key in keys[:3]:
                try:
                    async with self.session.get(self.store.base_url(name) + "/models", headers=self._headers(key),
                                                timeout=ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            ids = [str(m.get("id", "")).removeprefix("models/") for m in data.get("data", [])]
                            break
                except (ClientError, asyncio.TimeoutError, ValueError):
                    continue
        ids = sorted(m for m in ids if m and self.store.allowed(name, m))
        self._models[name] = (time.time(), ids)
        return ids

    async def all_models(self) -> list[dict]:
        names = [n for n, p in self.store.providers().items() if p.get("enabled", True)]
        lists = await asyncio.gather(*(self.list_models(n) for n in names))
        out = [{"id": f"{n}/{m}", "provider": n} for n, ms in zip(names, lists) for m in ms]
        out += [{"id": alias, "provider": "route"} for alias in self.store.data["routes"]]
        return out

    async def resolve(self, model: str) -> list[tuple[str, str]]:
        chain = self.store.data["routes"].get(model)
        items = chain if chain else [model]
        out: list[tuple[str, str]] = []
        for item in items:
            out += await self._resolve_one(item)
        return out

    async def _resolve_one(self, model: str) -> list[tuple[str, str]]:
        provs = {n: p for n, p in self.store.providers().items() if p.get("enabled", True)}
        if "/" in model:
            prefix, rest = model.split("/", 1)
            if prefix in provs:
                return [(prefix, rest)]
        for name in provs:  # a bare model name: first provider that lists it
            if model in await self.list_models(name):
                return [(name, model)]
        return []

    # -- request log
    def note(self, **kw) -> None:
        self.recent.appendleft({"t": time.time(), **kw})

    # -- the core: try keys, then fallbacks
    async def forward(self, request, path: str, model: str, build, stream: bool = False):
        """build(upstream_model, provider_name, kind) -> kwargs for session.request (json= or data=)."""
        chain = await self.resolve(model)
        if not chain:
            return err(404, f"Model '{model}' was not found. Use provider/model (see /v1/models), "
                            "or define a route with that name.", "model_not_found")
        t0 = time.time()
        problems: list[str] = []
        timeout = int(self.store.setting("timeout_s") or 120)
        self.pool.cooldowns = tuple(self.store.setting("cooldowns") or self.pool.cooldowns)

        for pname, umodel in chain:
            prov = self.store.providers()[pname]
            if prov["kind"] == "http":
                if path != "/chat/completions":
                    problems.append(f"{pname}: custom HTTP providers only do chat")
                    continue
                try:
                    resp = await self.http_provider(request, pname, prov, umodel, build(umodel, pname, "http")["json"], stream)
                    self.note(model=f"{pname}/{umodel}", provider=pname, key="-", status=200, ms=int((time.time() - t0) * 1000))
                    return resp
                except UpstreamError as e:
                    problems.append(f"{pname}: {e}")
                    self.note(model=f"{pname}/{umodel}", provider=pname, key="-", status=502, ms=int((time.time() - t0) * 1000))
                    continue

            keys = self.store.keys(pname) or ([""] if prov["kind"] == "ollama" else [])
            if not keys:
                problems.append(f"{pname}: no API key added")
                continue
            mode = prov.get("mode") or self.store.setting("rotation")
            tried: set[str] = set()
            while (key := self.pool.pick(pname, keys, mode, tried)) is not None:
                tried.add(key)
                url = self.store.base_url(pname) + path
                self.pool.begin(pname, key)
                try:
                    resp = await self.session.request(
                        "POST", url, headers=self._headers(key), **build(umodel, pname, prov["kind"]),
                        timeout=ClientTimeout(total=None, sock_connect=15, sock_read=timeout))
                except (ClientError, asyncio.TimeoutError) as e:
                    self.pool.end(pname, key)
                    wait = self.pool.failure(pname, key)
                    problems.append(f"{pname} key {kid(key)}: {type(e).__name__}, resting {wait:.0f}s")
                    continue

                status = resp.status
                ms = lambda: int((time.time() - t0) * 1000)  # noqa: E731
                if status == 200:
                    return await self._deliver(request, resp, pname, key, f"{pname}/{umodel}", stream, ms)
                raw = await resp.read()
                text = raw[:300].decode("utf-8", "replace")
                headers, ctype = resp.headers, resp.headers.get("Content-Type", "application/json")
                resp.release()
                self.pool.end(pname, key)
                self.note(model=f"{pname}/{umodel}", provider=pname, key=kid(key), status=status, ms=ms())
                if status in (401, 403):
                    self.pool.reject(pname, key)
                    problems.append(f"{pname} key {kid(key)}: rejected ({status})")
                elif status in (408, 429) or status >= 500:
                    try:
                        ra = float(headers.get("Retry-After", "")) if headers.get("Retry-After") else None
                    except ValueError:
                        ra = None
                    wait = self.pool.failure(pname, key, ra)
                    problems.append(f"{pname} key {kid(key)}: {status}, resting {wait:.0f}s")
                elif status == 404:
                    problems.append(f"{pname}: model not found there ({text[:80]})")
                    break  # not the key's fault; try the next provider in the chain
                else:  # 400/413/422: the request itself is wrong, retrying elsewhere won't help
                    return web.Response(body=raw, status=status, content_type=ctype.split(";")[0])
            else:
                if tried:
                    problems.append(f"{pname}: every key failed or is resting")
                else:
                    wait = self.pool.next_free_in(pname, keys)
                    problems.append(f"{pname}: all keys resting, next free in {wait:.0f}s")
        return err(503, "No provider could answer. " + " | ".join(problems), "all_providers_failed")

    async def _deliver(self, request, resp, pname, key, label, stream, ms):
        ctype = resp.headers.get("Content-Type", "application/json")
        if not stream or "text/event-stream" not in ctype:
            body = await resp.read()
            resp.release()
            ti = to = 0
            if "json" in ctype:
                try:
                    usage = json.loads(body).get("usage") or {}
                    ti, to = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                except (ValueError, AttributeError):
                    pass
            self.pool.end(pname, key)
            self.pool.success(pname, key, ti, to)
            self.note(model=label, provider=pname, key=kid(key), status=200, ms=ms())
            return web.Response(body=body, status=200, content_type=ctype.split(";")[0])
        out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                                      "X-Accel-Buffering": "no"})
        await out.prepare(request)
        ok = True
        try:
            async for part in resp.content.iter_any():
                await out.write(part)
        except (ClientError, asyncio.TimeoutError):
            ok = False
            try:
                await out.write(b'data: {"error": {"message": "The provider cut the stream short."}}\n\n')
            except ConnectionError:
                pass
        except ConnectionError:
            pass  # the client went away
        finally:
            resp.release()
            self.pool.end(pname, key)
        if ok:
            self.pool.success(pname, key)
        else:
            self.pool.failure(pname, key)
        self.note(model=label, provider=pname, key=kid(key), status=200 if ok else 502, ms=ms())
        await out.write_eof()
        return out

    # -- generic HTTP "recipe" provider
    async def http_provider(self, request, pname, prov, umodel, body, stream):
        r = prov["recipe"]
        messages = body.get("messages") or []
        v = {"model": umodel, "messages": messages, "temperature": body.get("temperature", 0.7),
             "max_tokens": body.get("max_tokens", 1024), **flatten(messages)}
        tpl = r.get("body")
        if isinstance(tpl, str) and tpl.strip():
            try:
                tpl = json.loads(tpl)
            except ValueError:
                pass
        payload = render(tpl, v) if tpl else None
        mode = r.get("stream", "none")
        method = str(r.get("method", "POST")).upper()
        kwargs = {"headers": render(r.get("headers") or {}, v)}
        if payload is not None:
            kwargs["json" if not isinstance(payload, str) else "data"] = payload
        if r.get("params"):
            kwargs["params"] = render(r["params"], v)
        try:
            resp = await self.session.request(method, render(r["url"], v), **kwargs,
                                              timeout=ClientTimeout(total=int(r.get("timeout", 120))))
        except (ClientError, asyncio.TimeoutError) as e:
            raise UpstreamError(f"{type(e).__name__}") from e
        if resp.status >= 400:
            text = (await resp.text())[:200]
            resp.release()
            raise UpstreamError(f"HTTP {resp.status}: {text}")

        spath = r.get("stream_path") or "choices.0.delta.content"
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]

        async def pieces():
            """Yield text pieces from a streaming response (sse / ndjson)."""
            buf = b""
            async for part in resp.content.iter_any():
                buf += part
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if mode == "sse":
                        if not line.startswith(b"data:"):
                            continue
                        line = line[5:].strip()
                    if not line or line == b"[DONE]":
                        continue
                    try:
                        t = dig(json.loads(line), spath)
                    except ValueError:
                        continue
                    if isinstance(t, str) and t:
                        yield t

        try:
            if mode in ("sse", "ndjson"):
                if stream:
                    out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                    await out.prepare(request)
                    await out.write(chunk(umodel, cid, ""))
                    async for t in pieces():
                        await out.write(chunk(umodel, cid, t))
                    await out.write(chunk(umodel, cid, None, done=True) + b"data: [DONE]\n\n")
                    await out.write_eof()
                    return out
                text = "".join([t async for t in pieces()])
            else:
                raw = await resp.read()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = raw.decode("utf-8", "replace")
                text = dig(data, r.get("response_path", "")) if r.get("response_path") else guess_text(data)
                if text is None:
                    raise UpstreamError("Could not find the reply text in the response. Set response_path.")
                text = text if isinstance(text, str) else json.dumps(text)
        except (ClientError, asyncio.TimeoutError) as e:
            raise UpstreamError(f"stream failed: {type(e).__name__}") from e
        finally:
            resp.release()
        if stream:
            out = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
            await out.prepare(request)
            await out.write(chunk(umodel, cid, text) + chunk(umodel, cid, None, done=True) + b"data: [DONE]\n\n")
            await out.write_eof()
            return out
        return web.json_response(completion(umodel, text))
