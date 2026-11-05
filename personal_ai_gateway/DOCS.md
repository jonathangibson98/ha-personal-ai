# Personal AI Gateway

One OpenAI-compatible address for every AI you use: your cloud keys **and** models you download and run yourself.

```
apps (Personal AI in Home Assistant, SillyTavern, Open WebUI...) → Gateway → Gemini / OpenAI / Groq / OpenRouter ... / your Ollama
```

## What it does

- **Key rotation and failover.** Add several keys per provider. A key that hits a rate limit rests (10 s, then 30, 60, 120), a key the provider rejects is parked for an hour, and the next key answers. Balanced or sequential mode.
- **Routes (fallback chains).** Make a model name of your own, e.g. `smart`, that tries `gemini/gemini-2.0-flash`, then `groq/llama-3.3-70b-versatile`, then `ollama/llama3.1:8b`.
- **Host the AI yourself.** The **Models** tab manages Ollama: see what's installed, free memory, delete, **search the Ollama library and Hugging Face (GGUF)**, pick a size or quantisation, and **download with a live progress meter**. List names under *Download on start* to fetch them automatically.
- **Custom HTTP providers.** For a service that isn't OpenAI-compatible but has a real endpoint you may use: describe the request (URL, headers, body template with `{{prompt}}`, `{{messages}}`...) and where the reply text is. The gateway presents it as a normal chat model, streaming included.
- Endpoints: `/v1/chat/completions` (streaming, tools), `/v1/embeddings`, `/v1/images/generations`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/models`.

Models are named `provider/model` (`gemini/gemini-2.0-flash`, `ollama/llama3.1:8b`), or by route name. Local Ollama models also work by bare name.

## Connect Personal AI (Home Assistant)

Settings → Devices & Services → Personal AI → add a server:

| Field | Value |
|---|---|
| Server URL | `http://<host>:8000/v1`, or with the add-on `http://local-personal-ai-gateway:8000/v1` |
| API key | the proxy key (page → Overview → Copy key) |

Then add a conversation agent and pick any model from the list. Personal AI 1.4 also adds two actions that work with a gateway: `personal_ai.search_models` and `personal_ai.pull_model` (they use the server's API key as the admin password, so they don't work if you set a separate `ADMIN_PASSWORD`).

## Install

**Docker Compose** (any machine):

```bash
cd personal_ai_gateway
cp .env.example .env        # keys, PROXY_API_KEY, OLLAMA_URL
docker compose up -d --build
# bundled Ollama too:  docker compose --profile bundled up -d --build   (OLLAMA_URL=http://ollama:11434)
```
Open `http://<host>:8000`, sign in with the proxy key (or `ADMIN_PASSWORD`). Everything you add in the page is saved in `./data`.

**Home Assistant add-on:** add the repository, install *Personal AI Gateway*, set the Ollama address (page → Settings → Ollama address → Test connection, or the `ollama_url` option): the LAN address of the machine running Ollama, e.g. `http://192.168.1.50:11434`, never localhost. Ollama must listen on the network (`OLLAMA_HOST=0.0.0.0`), optionally `api_keys`, start. Open it from the sidebar. An add-on can't run Ollama inside itself, so Ollama stays a separate container.

## Settings by environment / add-on options

`PROXY_API_KEY`, `ADMIN_PASSWORD`, `OLLAMA_URL`, `OLLAMA_PRELOAD`, and `<PROVIDER>_API_KEY_<N>` (e.g. `GEMINI_API_KEY_2`). Known provider names are created automatically; add any other OpenAI-compatible service from the Providers tab.

## Troubleshooting

- **Downloads fail / "Can't reach Ollama":** the address in Settings is wrong or Ollama isn't listening on the network. Use Settings → Test connection.
- **A provider lists no models:** press *List models* on its card; the exact reason (bad key, HTTP 403, network) is shown, and written to the add-on log. Chat can still work: use the full name, e.g. `gemini/gemini-2.5-flash`, or add names under *Extra models* so apps' model pickers show them.
- **Test** with a model name typed in the box if listing is blocked for your key.

## Good to know

- Rotating several keys is meant for keys you own (several projects, a paid and a free key). Don't use it to get around a provider's per-person limits or terms.
- Sites without an API (for example Perchance generators) can't be used: the only ways in are driving a hidden browser and defeating the site's bot checks, which this gateway does not do. Use a provider that offers an API; many have free tiers (Gemini, Groq, OpenRouter free models, Cerebras) and rotate well here.
- Model search needs internet access from the gateway container. The Ollama library page has no official API, so if its layout changes that search may come back empty; Hugging Face search and "download by exact name" keep working.
- Hugging Face models are pulled by Ollama as `hf.co/user/repo:QUANT`. Check the size shown against your free RAM/VRAM (roughly file size plus 20%).
- Keep the port on your LAN; anyone with the proxy key can spend your provider keys.
