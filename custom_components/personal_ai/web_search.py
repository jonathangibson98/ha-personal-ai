"""Web search (Google results) and page reading, exposed to the model as tools.

Registered as a Home Assistant LLM API per conversation agent, so the model can
decide for itself when it needs fresh information from the internet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    import voluptuous as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from .const import (
    DEFAULT_SEARCH_RESULTS,
    LOGGER,
    SEARCH_PROVIDER_GOOGLE_CSE,
    SEARCH_PROVIDER_SERPER,
)

SERPER_URL = "https://google.serper.dev/search"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"

SEARCH_TIMEOUT = ClientTimeout(total=15)
PAGE_TIMEOUT = ClientTimeout(total=15)
MAX_PAGE_BYTES = 1_500_000
MAX_PAGE_CHARS = 6000
MAX_REDIRECTS = 4
USER_AGENT = "Mozilla/5.0 (compatible; HomeAssistant-PersonalAI/1.1; +https://www.home-assistant.io)"

API_PROMPT_SEARCH = (
    "You can search the web with Google using the web_search tool. Use it for anything "
    "current or that you are not sure about: news, sports, weather away from home, prices, "
    "opening hours, recent events, or facts that may have changed. Do not search for things "
    "you already know well or for questions about the home itself. After searching, answer "
    "in your own words and briefly name the source when it helps. Never read out URLs."
)
API_PROMPT_READ = (
    " When the search snippets are not enough, open the best result with read_webpage."
)


@dataclass(frozen=True, slots=True)
class SearchSettings:
    """Everything needed to talk to the search provider."""

    provider: str
    api_key: str
    engine_id: str = ""
    max_results: int = DEFAULT_SEARCH_RESULTS
    read_webpages: bool = True


# ---------------------------------------------------------------- providers


def _clip(text: object, limit: int = 400) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def parse_serper(data: dict, limit: int) -> JsonObjectType:
    """Turn a Serper response into the compact structure given to the model."""
    out: dict[str, object] = {}
    box = data.get("answerBox") or {}
    if answer := box.get("answer") or box.get("snippet"):
        out["direct_answer"] = _clip(answer, 500)
    graph = data.get("knowledgeGraph") or {}
    if graph.get("description"):
        out["knowledge_panel"] = _clip(f"{graph.get('title', '')}: {graph['description']}", 500)
    out["results"] = [
        {
            "title": _clip(item.get("title"), 160),
            "url": item.get("link"),
            "snippet": _clip(item.get("snippet")),
            **({"date": item["date"]} if item.get("date") else {}),
        }
        for item in (data.get("organic") or [])[:limit]
    ]
    return out  # type: ignore[return-value]


def parse_google_cse(data: dict, limit: int) -> JsonObjectType:
    """Turn a Google Custom Search response into the compact structure."""
    return {
        "results": [
            {
                "title": _clip(item.get("title"), 160),
                "url": item.get("link"),
                "snippet": _clip(item.get("snippet")),
            }
            for item in (data.get("items") or [])[:limit]
        ]
    }


async def run_search(
    hass: HomeAssistant, settings: SearchSettings, query: str
) -> JsonObjectType:
    """Run one search and return results (or an error the model can relay)."""
    session = async_get_clientsession(hass)
    limit = max(1, min(int(settings.max_results), 10))
    language = (hass.config.language or "en").split("-")[0]
    country = (hass.config.country or "").lower()
    try:
        if settings.provider == SEARCH_PROVIDER_GOOGLE_CSE:
            params = {
                "key": settings.api_key,
                "cx": settings.engine_id,
                "q": query,
                "num": limit,
                "hl": language,
            }
            async with session.get(GOOGLE_CSE_URL, params=params, timeout=SEARCH_TIMEOUT) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    message = (data.get("error") or {}).get("message", f"HTTP {resp.status}")
                    LOGGER.warning("Google search failed: %s", message)
                    return {"error": f"Google search failed: {_clip(message, 200)}"}
            return parse_google_cse(data, limit)

        payload: dict[str, object] = {"q": query, "num": limit, "hl": language}
        if country:
            payload["gl"] = country
        headers = {"X-API-KEY": settings.api_key, "Content-Type": "application/json"}
        async with session.post(
            SERPER_URL, json=payload, headers=headers, timeout=SEARCH_TIMEOUT
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                message = data.get("message", f"HTTP {resp.status}") if isinstance(data, dict) else ""
                LOGGER.warning("Serper search failed: %s", message or resp.status)
                return {"error": f"Search failed: {_clip(message or resp.status, 200)}"}
        return parse_serper(data, limit)
    except (ClientError, asyncio.TimeoutError, ValueError) as err:
        LOGGER.warning("Web search request failed: %s", err)
        return {"error": "The search service could not be reached."}


# ---------------------------------------------------------------- page reader


class _TextExtractor(HTMLParser):
    """Very small HTML to text converter."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP and tag != "head":
            self._skip_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and tag != "head" and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, readable text) for an HTML document."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return parser.title.strip(), text


async def _is_public_host(host: str, port: int) -> bool:
    """True only if every address the host resolves to is a public internet address."""
    try:
        literal = ipaddress.ip_address(host)
        return literal.is_global
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


async def read_page(hass: HomeAssistant, url: str) -> JsonObjectType:
    """Download a public web page and return its readable text.

    Local and private addresses are refused so a prompt-injected page cannot make
    the model probe the home network. Redirects are followed manually so every hop
    is checked.
    """
    session = async_get_clientsession(hass)
    current = url.strip()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return {"error": "Only http and https links can be opened."}
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not await _is_public_host(parsed.hostname, port):
                return {"error": "That address is not a public website, so it was not opened."}
            async with session.get(
                current,
                allow_redirects=False,
                timeout=PAGE_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    current = urljoin(current, resp.headers["Location"])
                    continue
                if resp.status >= 400:
                    return {"error": f"The page returned HTTP {resp.status}."}
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype and not any(t in ctype for t in ("text/", "xml", "json")):
                    return {"error": "That link is not a readable web page."}
                raw = await resp.content.read(MAX_PAGE_BYTES)
                charset = resp.charset or "utf-8"
            body = raw.decode(charset, errors="replace")
            if "html" in ctype or body.lstrip()[:1] == "<":
                title, text = html_to_text(body)
            else:
                title, text = "", body.strip()
            if not text:
                return {"error": "The page had no readable text."}
            return {
                "url": current,
                "title": _clip(title, 160),
                "text": text[:MAX_PAGE_CHARS],
                "truncated": len(text) > MAX_PAGE_CHARS,
            }
        return {"error": "Too many redirects."}
    except (ClientError, asyncio.TimeoutError, LookupError) as err:
        LOGGER.warning("Could not read %s: %s", url, err)
        return {"error": "The page could not be loaded."}


# ---------------------------------------------------------------- LLM API


class WebSearchTool(llm.Tool):
    """Search Google for current information."""

    name = "web_search"
    description = (
        "Search the web with Google and return the top results (title, url, snippet) plus a "
        "direct answer when Google has one. Use it for current events, facts that change, "
        "or anything you are unsure about."
    )
    parameters = vol.Schema(
        {vol.Required("query", description="What to search for, as you would type into Google"): str}
    )

    def __init__(self, settings: SearchSettings) -> None:
        """Initialize the tool."""
        self._settings = settings

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the search."""
        query = str(tool_input.tool_args.get("query", "")).strip()
        if not query:
            return {"error": "No search query was given."}
        LOGGER.debug("web_search: %s", query)
        return await run_search(hass, self._settings, query)


class ReadWebpageTool(llm.Tool):
    """Open a web page and return its text."""

    name = "read_webpage"
    description = (
        "Open a public web page (for example a result from web_search) and return its "
        "readable text, to get details the search snippet does not have."
    )
    parameters = vol.Schema({vol.Required("url", description="Full http(s) address of the page"): str})

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Fetch the page."""
        url = str(tool_input.tool_args.get("url", "")).strip()
        LOGGER.debug("read_webpage: %s", url)
        return await read_page(hass, url)


class WebSearchAPI(llm.API):
    """An LLM API that gives the model web search (and optionally page reading)."""

    def __init__(self, hass: HomeAssistant, api_id: str, name: str, settings: SearchSettings) -> None:
        """Initialize the API."""
        super().__init__(hass=hass, id=api_id, name=name)
        self._settings = settings

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        """Return the tools and prompt for one conversation."""
        tools: list[llm.Tool] = [WebSearchTool(self._settings)]
        prompt = API_PROMPT_SEARCH
        if self._settings.read_webpages:
            tools.append(ReadWebpageTool())
            prompt += API_PROMPT_READ
        return llm.APIInstance(api=self, api_prompt=prompt, llm_context=llm_context, tools=tools)
