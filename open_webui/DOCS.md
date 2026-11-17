# Open WebUI (AI Gateway chat)

[Open WebUI](https://github.com/open-webui/open-webui), a ChatGPT-style chat app, already connected to your **Personal AI Gateway** add-on. Every model and route in the gateway (cloud keys, signed-in Claude / ChatGPT / Gemini accounts, your Ollama) shows up in its model picker.

## Setup

1. Install and start **Personal AI Gateway** (2.0.0 or newer) first.
2. Install and start this add-on. It reads the gateway's address and a dedicated key from `/share/personal_ai_gateway`, so there's nothing to copy.
3. Open it: the gateway's **Chat** tab, or **Open Web UI** on this page (port 3000).
4. Create your account. **The first account becomes the admin.**

## Options

- **Require sign-in** (on): ask for an account before chatting. The chat is reachable from your network, so leave this on unless only you use that network. Once accounts exist it can't be switched off.
- **Gateway address / Gateway key**: only for a gateway that isn't an add-on on this Home Assistant (e.g. `http://192.168.1.10:8000/v1` and its proxy key).

## Good to know

- Chats, users and settings live in this add-on's data and are included in Home Assistant backups.
- Open WebUI takes the gateway connection on its **first** start. To change it later use Open WebUI → Admin panel → Settings → Connections.
- It needs around 1 GB of RAM and a few GB of disk.
- Inside Home Assistant on `https://`, browsers won't show an `http://` page inside another page, so the gateway's Chat tab opens it in a new tab instead.
