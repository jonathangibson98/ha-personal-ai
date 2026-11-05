"""Hosting the AI yourself: manage Ollama, search the model libraries, download with progress."""
import asyncio
import html
import json
import re
import time
import uuid

from aiohttp import ClientError, ClientTimeout

UA = {"User-Agent": "Mozilla/5.0 (compatible; PersonalAI-Gateway/1.0)"}
QUANT_RE = re.compile(r"(?<![A-Za-z0-9])((?:IQ|Q)\d(?:_[A-Z0-9]+)*|BF16|F16|F32)(?![A-Za-z0-9])", re.I)
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,200}")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ------------------------------------------------------------ search parsing (unit tested)

def parse_ollama_search(page: str) -> list[dict]:
    """Read ollama.com/search HTML. Returns [] if the page layout changes (callers fall back)."""
    out = []
    for block in re.split(r"<li[^>]*x-test-model", page)[1:]:
        m = re.search(r'href="/(?:library/)?([^"]+)"', block)
        if not m:
            continue
        name = m.group(1)
        title = re.search(r"x-test-search-response-title[^>]*>([^<]+)<", block)
        desc = re.search(r"<p[^>]*>\s*([^<]{8,400}?)\s*</p>", block)
        sizes = re.findall(r"x-test-size[^>]*>\s*([^<\s]+)\s*<", block)
        caps = re.findall(r"x-test-capability[^>]*>\s*([^<\s]+)\s*<", block)
        pulls = re.search(r"x-test-pull-count[^>]*>\s*([^<\s]+)\s*<", block)
        updated = re.search(r"x-test-updated[^>]*>\s*([^<]+?)\s*<", block)
        out.append({
            "source": "ollama", "id": name, "title": html.unescape(title.group(1).strip()) if title else name,
            "description": html.unescape(desc.group(1)) if desc else "",
            "sizes": sizes, "capabilities": caps, "downloads": pulls.group(1) if pulls else "",
            "updated": updated.group(1) if updated else "",
            "pull": [f"{name}:{s}" for s in sizes] or [name],
        })
    return out


def quants_from_files(siblings: list[dict]) -> list[dict]:
    """Group a Hugging Face repo's .gguf files by quantisation, summing split-file sizes."""
    groups: dict[str, int] = {}
    for f in siblings or []:
        name = f.get("rfilename", "")
        if not name.lower().endswith(".gguf") or "mmproj" in name.lower():
            continue
        m = QUANT_RE.search(name.rsplit("/", 1)[-1])
        if m:
            groups[m.group(1).upper()] = groups.get(m.group(1).upper(), 0) + int(f.get("size") or 0)
    return [{"quant": q, "size": s, "size_h": human(s) if s else "?"}
            for q, s in sorted(groups.items(), key=lambda kv: kv[1] or 10**15)]


# ------------------------------------------------------------ Ollama client

class OllamaError(Exception):
    pass


class Hub:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def base(self) -> str:
        return self.store.ollama_url()

    async def _json(self, method: str, path: str, **kw):
        try:
            async with self.session.request(method, self.base + path, timeout=ClientTimeout(total=kw.pop("t", 20)), **kw) as r:
                text = await r.text()
                if r.status >= 400:
                    raise OllamaError(f"Ollama {r.status}: {text[:200]}")
                return json.loads(text) if text else {}
        except (ClientError, asyncio.TimeoutError) as e:
            raise OllamaError(f"Can't reach Ollama at {self.base} ({type(e).__name__}). Check the Ollama URL in Settings.") from e

    async def status(self) -> dict:
        try:
            v = await self._json("GET", "/api/version", t=5)
            return {"ok": True, "url": self.base, "version": v.get("version", "")}
        except OllamaError as e:
            return {"ok": False, "url": self.base, "error": str(e)}

    async def installed(self) -> list[dict]:
        data = await self._json("GET", "/api/tags")
        loaded = {m["name"] for m in (await self._json("GET", "/api/ps")).get("models", [])}
        return sorted(({"name": m["name"], "size": m.get("size", 0), "size_h": human(m.get("size", 0)),
                        "modified": m.get("modified_at", ""), "loaded": m["name"] in loaded,
                        "family": (m.get("details") or {}).get("family", ""),
                        "params": (m.get("details") or {}).get("parameter_size", ""),
                        "quant": (m.get("details") or {}).get("quantization_level", "")}
                       for m in data.get("models", [])), key=lambda m: m["name"])

    async def delete(self, name: str) -> None:
        await self._json("DELETE", "/api/delete", json={"model": name, "name": name})

    async def unload(self, name: str) -> None:
        await self._json("POST", "/api/generate", json={"model": name, "keep_alive": 0}, t=60)

    # -- downloads
    def start_pull(self, name: str) -> dict:
        name = name.strip()
        if not MODEL_NAME_RE.fullmatch(name):
            raise ValueError("That doesn't look like a model name.")
        for j in self.jobs.values():
            if j["name"] == name and j["status"] in ("queued", "pulling"):
                return j
        job = {"id": uuid.uuid4().hex[:10], "name": name, "status": "queued", "completed": 0, "total": 0,
               "message": "Waiting to start", "started": time.time(), "updated": time.time()}
        self.jobs[job["id"]] = job
        self._tasks[job["id"]] = asyncio.create_task(self._pull(job))
        for old in sorted(self.jobs.values(), key=lambda j: j["started"])[:-30]:
            if old["status"] not in ("queued", "pulling"):
                self.jobs.pop(old["id"], None)
        return job

    async def _pull(self, job: dict) -> None:
        layers: dict[str, list[int]] = {}
        job["status"] = "pulling"
        try:
            async with self.session.post(self.base + "/api/pull", json={"model": job["name"], "name": job["name"], "stream": True},
                                         timeout=ClientTimeout(total=None, sock_connect=15, sock_read=600)) as r:
                if r.status >= 400:
                    raise OllamaError((await r.text())[:200])
                async for line in r.content:
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    if ev.get("error"):
                        raise OllamaError(ev["error"])
                    if ev.get("digest") and ev.get("total"):
                        layers[ev["digest"]] = [ev.get("completed", 0), ev["total"]]
                    job["completed"] = sum(c for c, _ in layers.values())
                    job["total"] = sum(t for _, t in layers.values())
                    job["message"] = ev.get("status", "")
                    job["updated"] = time.time()
            job.update(status="done", message="Ready to use", completed=job["total"], updated=time.time())
        except asyncio.CancelledError:
            job.update(status="cancelled", message="Cancelled", updated=time.time())
            raise
        except (OllamaError, ClientError, asyncio.TimeoutError, ValueError) as e:
            job.update(status="error", message=str(e) or type(e).__name__, updated=time.time())

    def cancel(self, job_id: str) -> bool:
        t = self._tasks.get(job_id)
        if t and not t.done():
            t.cancel()
            return True
        return False

    async def preload(self) -> None:
        """Download the models named in ollama_preload that aren't installed yet."""
        wanted = [m.strip() for m in re.split(r"[,\n]", str(self.store.setting("ollama_preload"))) if m.strip()]
        if not wanted:
            return
        for _ in range(30):  # Ollama may still be starting
            if (await self.status())["ok"]:
                break
            await asyncio.sleep(10)
        else:
            return
        have = {m["name"] for m in await self.installed()}
        for name in wanted:
            if name not in have and f"{name}:latest" not in have:
                self.start_pull(name)

    # -- search
    async def search(self, q: str, source: str = "all") -> dict:
        q = q.strip()
        results, notes = [], []
        tasks = []
        if source in ("all", "ollama"):
            tasks.append(("ollama", self._search_ollama(q)))
        if source in ("all", "hf") and q:
            tasks.append(("hf", self._search_hf(q)))
        done = await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        for (src, _), res in zip(tasks, done):
            if isinstance(res, Exception):
                notes.append(f"{'Ollama library' if src == 'ollama' else 'Hugging Face'} search failed: {type(res).__name__}")
            else:
                results += res
        if source in ("all", "hf") and not q:
            notes.append("Type a name to search Hugging Face.")
        return {"results": results, "notes": notes}

    async def _search_ollama(self, q: str) -> list[dict]:
        async with self.session.get("https://ollama.com/search", params={"q": q}, headers=UA,
                                    timeout=ClientTimeout(total=15)) as r:
            r.raise_for_status()
            found = parse_ollama_search(await r.text())
        if not found and q:  # layout changed or no hits: let the user pull the name as typed
            found = []
        return found[:30]

    async def _search_hf(self, q: str) -> list[dict]:
        params = {"search": q, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": "20"}
        async with self.session.get("https://huggingface.co/api/models", params=params, headers=UA,
                                    timeout=ClientTimeout(total=15)) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        return [{"source": "hf", "id": m["id"], "title": m["id"], "description": ", ".join(
                    t for t in (m.get("tags") or []) if t in ("conversational", "text-generation", "code", "vision", "tool-use"))[:80],
                 "downloads": f"{m.get('downloads', 0):,}", "likes": m.get("likes", 0),
                 "sizes": [], "capabilities": [], "updated": (m.get("lastModified") or "")[:10], "pull": [f"hf.co/{m['id']}"]}
                for m in data if m.get("id")]

    async def hf_quants(self, repo: str) -> list[dict]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            raise ValueError("Bad repository name.")
        async with self.session.get(f"https://huggingface.co/api/models/{repo}", params={"blobs": "true"}, headers=UA,
                                    timeout=ClientTimeout(total=15)) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        return [{**q, "pull": f"hf.co/{repo}:{q['quant']}"} for q in quants_from_files(data.get("siblings"))]
