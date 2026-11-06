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

**Docker Compose** (any machine). The stack includes its own Ollama, so models download and run with nothing else to set up:

```bash
cd personal_ai_gateway
cp .env.example .env        # optional: API keys, PROXY_API_KEY
docker compose up -d
```
Open `http://<host>:8000`, sign in with the proxy key (shown in `docker compose logs gateway` and in Settings), then **Models → Find & download**. Models are stored in the `ollama_data` volume; the page's data in `./data`. Update with `docker compose pull && docker compose up -d` (or `up -d --build` if the image is private).

- **NVIDIA GPU:** uncomment the `deploy` block under `ollama` (needs the NVIDIA container toolkit); downloads and speed then use the GPU.
- **Your own Ollama instead:** set `OLLAMA_URL` in `.env` (or the address in Settings) and run `docker compose up -d --no-deps gateway`. An Ollama on the host must listen on the network (`OLLAMA_HOST=0.0.0.0`), or the gateway can't reach it from its container.
- **Check downloads:** `docker compose logs -f ollama`.

**Home Assistant add-on:** add the repository, install *Personal AI Gateway*, start it. **Ollama is built in**, so no other device is needed: open the add-on → Models → Find & download, pick a model, and it runs on this machine. (Settings → Ollama address should read `http://127.0.0.1:11434`; press *Use the built-in Ollama* if not.)

Things to know about the built-in Ollama:

- **CPU only.** Add-ons can't use a GPU, so replies are slower than on a GPU machine. On a Raspberry Pi or small mini-PC pick small models (1B to 4B parameters, e.g. `llama3.2:3b`, `qwen3:4b`, `gemma3:4b`). A 7B-8B model at the default 4-bit size needs about 5-6 GB of free RAM.
- **RAM and disk:** a model is loaded in RAM while in use (see *Keep models in memory for*) and each one takes 1-5 GB of disk in the add-on's data. Models are excluded from Home Assistant backups.
- **Use it elsewhere:** other apps can use `http://local-personal-ai-gateway:8000/v1` (the gateway, with your key). To let something talk to Ollama directly, set a host port for `11434/tcp` in the add-on's Network section; that port has no password, so keep it on your home network.
- **An Ollama on another device instead:** turn off *Built-in Ollama*, then set its address in Settings and press *Test connection*.

## Settings by environment / add-on options

`PROXY_API_KEY`, `ADMIN_PASSWORD`, `OLLAMA_URL`, `OLLAMA_PRELOAD`, and `<PROVIDER>_API_KEY_<N>` (e.g. `GEMINI_API_KEY_2`). Known provider names are created automatically; add any other OpenAI-compatible service from the Providers tab.

## Troubleshooting

- **Downloads fail / "Can't reach Ollama":** with the built-in Ollama the address must be `http://127.0.0.1:11434` (Settings → *Use the built-in Ollama*). For an Ollama elsewhere it must be that machine's LAN address. Use Settings → Test connection, and check the add-on log.
- **Out of memory / add-on restarts while chatting:** the model is too big for the free RAM. Pick a smaller one or a smaller quantisation.
- **A provider lists no models:** press *List models* on its card; the exact reason (bad key, HTTP 403, network) is shown, and written to the add-on log. Chat can still work: use the full name, e.g. `gemini/gemini-2.5-flash`, or add names under *Extra models* so apps' model pickers show them.
- **Test** with a model name typed in the box if listing is blocked for your key.

## Good to know

- Rotating several keys is meant for keys you own (several projects, a paid and a free key). Don't use it to get around a provider's per-person limits or terms.
- Sites without an API (for example Perchance generators) can't be used: the only ways in are driving a hidden browser and defeating the site's bot checks, which this gateway does not do. Use a provider that offers an API; many have free tiers (Gemini, Groq, OpenRouter free models, Cerebras) and rotate well here.
- Model search needs internet access from the gateway container. The Ollama library page has no official API, so if its layout changes that search may come back empty; Hugging Face search and "download by exact name" keep working.
- Hugging Face models are pulled by Ollama as `hf.co/user/repo:QUANT`. Check the size shown against your free RAM/VRAM (roughly file size plus 20%).
- Keep the port on your LAN; anyone with the proxy key can spend your provider keys.
