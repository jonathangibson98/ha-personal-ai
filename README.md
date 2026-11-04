<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/personal_ai/brand/dark_logo@2x.png">
    <img alt="Personal AI" src="custom_components/personal_ai/brand/logo@2x.png" width="420">
  </picture>
</p>

# Personal AI for Home Assistant

A conversation agent, AI tasks, and **image generation** for Home Assistant, powered by any
OpenAI-compatible server: OpenAI itself, LM Studio, Ollama (`/v1`), LocalAI, llama.cpp,
Gemini-FastAPI, and so on.

## Features

- **Conversation agent** for Assist, with tool calling (it can control your devices), streaming, and a system prompt template.
- **Normal AI conversation**: ask it anything, not just about your house. Pick *Hybrid*, *Home control* or *Plain chat* per agent.
- **Google web search**: the AI searches Google (and can open the pages) when it needs current information, such as news, scores, prices, opening hours.
- **AI Task (data)** for `ai_task.generate_data`, including structured output and image attachments.
- **AI Image generator** for `ai_task.generate_image`, using the standard `/v1/images` endpoints. Pick the size, quality, and format, **choose the folder images are saved to**, and name files with a template.
- **AI voice (text-to-speech) entity** with **emotion**: `[happy]`, `[whisper]`, `[sad]`... tags change the delivery mid-message. Works with OpenAI (`gpt-4o-mini-tts`) or the **Personal AI Speech add-on** on your Pi.
- **AI transcription (speech-to-text) entity** for Assist, via any OpenAI-compatible `/audio/transcriptions` server (Whisper in the add-on, OpenAI, etc.).
- **TTS-friendly output**: strips emojis, Markdown emphasis, and LaTeX so announcements sound clean.
- **Real logo and icon** (Home Assistant 2026.3+ reads them from the `brand/` folder).

## Installation

**HACS:** HACS → ⋮ → *Custom repositories* → add this repo as *Integration* → download **Personal AI** → restart Home Assistant.

**Manual:** copy `custom_components/personal_ai` into `/config/custom_components/` and restart.

> Logos need Home Assistant 2026.3 or newer. On older versions everything still works, you just see the generic puzzle-piece icon.

## Setup

1. **Settings → Devices & Services → Add Integration → Personal AI.**
2. Fill in:
   - **Server Name**: anything, e.g. `OpenAI` or `My PC`.
   - **Server URL**: must end in `/v1`. Prefilled with `https://api.openai.com/v1`; for a local server use something like `http://192.168.1.50:1234/v1`.
   - **API Key**: required for OpenAI, usually blank for local servers.
3. On the new entry, click **Add conversation agent**, **Add AI task**, and/or **Add AI image generator**. One server can have as many of each as you want.

## Conversation modes

Each conversation agent has a **Conversation mode**:

| Mode | Behaviour |
| --- | --- |
| **Hybrid** (default) | Chats about anything from the model's own knowledge and uses your devices only when you ask it to control or check something. |
| **Home control only** | The classic Assist behaviour: a smart-home voice assistant. |
| **Plain chat** | A normal AI chatbot with no access to your entities or devices. |

If the **System Prompt** is left empty (or still holds the old stock Assist prompt), a built-in prompt for the chosen mode is used. Write your own prompt to override it. For Hybrid and Home control, **Tool Providers** still decides which Home Assistant tools (normally *Assist*) the agent may use.

## Voice: emotion TTS and STT

Add a second Personal AI server pointing at the **Personal AI Speech add-on** (`http://local-personal-ai-speech:8880/v1`, or `http://<pi-ip>:8880/v1`), then on that entry click **Add AI voice (TTS)** and **Add AI transcription (STT)**. Pick them in *Settings, Voice assistants*.

**Emotion:** write tags in the text, or turn on *Speak with emotion* on a conversation agent so the AI adds them itself.

```yaml
action: tts.speak
target:
  entity_id: tts.kokoro_82m_ai_voice
data:
  media_player_entity_id: media_player.kitchen
  message: "[excited] You got a package! [whisper] Don't tell anyone it's a gift."
  options:
    voice: af_heart
    emotion: calm   # used where there is no tag
```

Tags: neutral, happy, cheerful, excited, sad, angry, calm, gentle, whisper, shout, sarcastic, serious, nervous, curious, surprised, tired, playful, confident, empathetic, laughing. With OpenAI's `gpt-4o-mini-tts` each tag becomes a real acting instruction. With the local add-on the emotion is approximated (speed, volume, pauses), so it sounds natural but subtler.

## Web search

Turn on **Enable web search (Google)** on a conversation agent and pick a provider:

| Provider | Notes |
| --- | --- |
| **Serper** (default) | Real Google results through [serper.dev](https://serper.dev). Sign up, copy the API key, paste it into *Search API key*. Has a free tier. |
| **Google Custom Search** | Needs an API key **and** the search engine ID (`cx`). Google has closed this API to new customers and ends it on 1 January 2027, so use Serper unless you already have a key. |

The model decides when to search, so "what's the capital of France" is answered directly while "who won the match last night" triggers a search. With **Allow opening web pages** on, it can also read a result in full. Pages on private or local addresses are never opened, so a web page cannot make the AI poke at your home network.

Pick a model that supports tool calling. The prompt tells the AI to keep answers short and never read out web addresses, and the Voice Chat add-on strips any that slip through, so it works well by voice.

## AI image generator

| Setting | What it does |
| --- | --- |
| Image model | Model name on your server (`gpt-image-1`, `dall-e-3`, your Stable Diffusion model...) |
| Image size / Quality / File format | Sent to the server. `auto` sends nothing. |
| Response format | `b64_json` (default, works on most servers), `url` (Personal AI downloads it), or `omit` (for gpt-image-1 style servers that reject the setting) |
| Allow attached images | Lets you send a photo along with the prompt (image edits) |
| Save generated images to disk | Writes a copy of every image to the folder below |
| Save folder | Default `/config/www/personal_ai`, which is served at `/local/personal_ai/...`. Must be inside `/config/www`, `/media`, or listed in `allowlist_external_dirs` |
| Filename template | Placeholders `{date}` `{time}` `{timestamp}` `{slug}` `{name}` `{model}` plus strftime codes. The extension is added automatically and duplicates get `_1`, `_2`... |

```yaml
action: ai_task.generate_image
data:
  task_name: porch art
  entity_id: ai_task.gpt_image_1_ai_image   # use your entity's id
  instructions: A neon cat riding a motorcycle, synthwave style
```

Each saved image also fires a `personal_ai_image_saved` event (`path`, `url`, `prompt`, `model`, `mime_type`):

```yaml
triggers:
  - trigger: event
    event_type: personal_ai_image_saved
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: "New image: {{ trigger.event.data.prompt }}"
      data:
        image: "{{ trigger.event.data.url }}"
```

**Saving to a NAS or other folder:** add it to `configuration.yaml`, restart, then type the path into *Save folder*:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /mnt/nas/ai_images
```

## Troubleshooting

- **Logo missing:** needs HA 2026.3+. Restart Home Assistant and hard-refresh the browser, since brand images are cached.
- **"Cannot connect":** the Server URL must end in `/v1` and be reachable *from* Home Assistant (not just your PC).
- **"Not allowed to write to this folder":** use a folder under `/config/www` or `/media`, or add yours to `allowlist_external_dirs`.
- **Image call fails with an unknown-parameter error:** set *Response format* to `omit` and *Quality* to `auto`.
- Turn on debug logs: `logger: logs: custom_components.personal_ai: debug`.

## Credits

Built on [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm) by [@skye-harris](https://github.com/skye-harris) and the [fork by @luuquangvu](https://github.com/luuquangvu/hass_local_openai_llm). MIT licensed.
