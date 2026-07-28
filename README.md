---
title: AI33 Pro MCP Server
emoji: 🎙️
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: MCP server for the AI33 Pro media generation API
---

# AI33 Pro MCP Server

An MCP (Model Context Protocol) server exposing the [AI33 Pro](https://ai33.pro) media API as tools, served over streamable HTTP. Based on the [codexflows](https://github.com/fozagtx/codexflows) AI33 Pro skill.

## Endpoint

```
https://pima5-ai33-mcp.hf.space/mcp
```

## Tools

| Tool | Purpose |
|---|---|
| `ai33_health` | Provider health (ElevenLabs, MiniMax) |
| `ai33_get_credits` | Remaining credit balance |
| `ai33_list_voices` | Browse the v3 voice library (7 providers) |
| `ai33_text_to_speech` | TTS with any prefixed v3 voice |
| `ai33_create_dialogue` | Multi-speaker dialogue audio |
| `ai33_clone_voice` / `ai33_delete_cloned_voice` | Voice cloning from an audio URL |
| `ai33_voice_changer` / `ai33_voice_isolate` | Speech-to-speech voice transform, noise isolation |
| `ai33_speech_to_text` | Transcription (SRT + word-level JSON) |
| `ai33_dub_audio` | Dubbing into another language, optional replacement voice |
| `ai33_generate_sound_effect` | Sound effects from text |
| `ai33_generate_suno_music` | Suno song generation (simple + custom modes) |
| `ai33_generate_music` | Music generation (MiniMax) |
| `ai33_list_dictionaries` / `ai33_create_dictionary` / `ai33_update_dictionary` / `ai33_delete_dictionary` / `ai33_preview_dictionary` | Pronunciation dictionaries |
| `ai33_list_image_models` / `ai33_get_image_price` / `ai33_generate_image` | Image generation with reference-image support |
| `ai33_get_task` / `ai33_wait_for_task` / `ai33_list_tasks` / `ai33_delete_tasks` | Task management |

Generation tools return a `task_id` and can optionally wait for completion (`wait_seconds`). Completed tasks include an `asset_urls` list with downloadable outputs. Since the server runs remotely, file inputs (voice samples, audio to transcribe or dub, reference images) are passed as public URLs.

## Authentication

Every request needs an AI33 API key, resolved in this order:

1. `xi-api-key` request header (also accepts `x-ai33-api-key`, `x-api-key`, or `Authorization: Bearer`)
2. `AI33_API_KEY` environment variable (set as a Space secret)

If you duplicate this Space, add your own `AI33_API_KEY` secret in the Space settings so clients can connect without sending a header.

## Client configuration

For any MCP client that supports streamable HTTP:

```json
{
  "mcpServers": {
    "ai33": {
      "type": "http",
      "url": "https://pima5-ai33-mcp.hf.space/mcp",
      "headers": { "xi-api-key": "YOUR_AI33_API_KEY" }
    }
  }
}
```

## Run locally

```bash
pip install -r requirements.txt
export AI33_API_KEY="your-key"
python server.py
# MCP endpoint at http://localhost:7860/mcp
```

## Deploy your own

The whole server is four files: `server.py`, `Dockerfile`, `requirements.txt`, and this `README.md` (its frontmatter configures the Space).

1. [Duplicate the Space](https://huggingface.co/spaces/pima5/ai33-mcp?duplicate=true) or create a new Docker Space and upload the files.
2. Add an `AI33_API_KEY` secret in the Space settings (or skip it and require clients to send the `xi-api-key` header).
3. Your endpoint is `https://<user>-<space>.hf.space/mcp`.
