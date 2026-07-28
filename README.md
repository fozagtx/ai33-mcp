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

An MCP (Model Context Protocol) server exposing the [AI33 Pro](https://ai33.pro) media API as tools, served over streamable HTTP, plus live YouTube research tools (YouTube Data API v3). Based on the [codexflows](https://github.com/fozagtx/codexflows) AI33 Pro skill and the [AuspexIQ](https://github.com/fozagtx/AuspexIQ) analysis engine (payment layer removed — the YouTube key is free).

## Endpoint

```
https://pima5-ai33-mcp.hf.space/mcp
```

Transport: streamable HTTP (stateless). Works with any MCP client that supports remote HTTP servers.

## Connect from the Claude app (claude.ai / desktop)

1. Open **Settings → Connectors** (on claude.ai: `claude.ai/settings/connectors`).
2. Click **Add custom connector**.
3. Name: `ai33`, URL: `https://pima5-ai33-mcp.hf.space/mcp`.
4. Save, then enable the connector in a chat via the search & tools menu. The AI33 key is already configured server-side, so no key entry is needed.

## Connect from Claude Code (CLI)

```bash
claude mcp add --transport http ai33 https://pima5-ai33-mcp.hf.space/mcp
```

## Connect from any other MCP client (JSON config)

```json
{
  "mcpServers": {
    "ai33": {
      "type": "http",
      "url": "https://pima5-ai33-mcp.hf.space/mcp",
      "headers": {
        "xi-api-key": "YOUR_AI33_API_KEY",
        "x-youtube-api-key": "YOUR_YOUTUBE_API_KEY"
      }
    }
  }
}
```

The `headers` block is optional — keys configured as Space secrets are used when a header is absent.

## AI33 media tools

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

## YouTube research tools

Live YouTube Data API v3 analysis — every number comes from a live API call (or a short-TTL cache). Failures return structured `{"ok": false, "error": {...}}` objects, never fabricated data.

| Tool | What it does | Quota cost (uncached) |
|---|---|---|
| `youtube_search` | Search videos; compact results with live view/like/comment counts | ~101 units |
| `youtube_scan_niche` | Niche assessment: saturation score, outlier videos vs each channel's own baseline, ENTER/CROWDED/AVOID verdict | ~122 units |
| `youtube_channel_outliers` | Which of a channel's recent videos overperformed its own baseline (accepts `UC...` ID, `@handle`, or channel URL) | ~5 units |
| `youtube_video_context` | Explain one video's performance: outlier multiple, percentile, views/day velocity, MEGA_OUTLIER→UNDERPERFORMER classification | ~4 units |
| `youtube_rising_channels` | Fastest-rising channels in a niche by recent-median vs lifetime-average momentum | ~132 units |

## Authentication

**AI33 tools** — an AI33 API key, resolved in this order:

1. `xi-api-key` request header (also accepts `x-ai33-api-key`, `x-api-key`, or `Authorization: Bearer`)
2. `AI33_API_KEY` environment variable (set as a Space secret)

**YouTube tools** — a free YouTube Data API v3 key (Google Cloud Console → enable "YouTube Data API v3" → Credentials → Create API key; 10,000 quota units/day, no billing required):

1. `x-youtube-api-key` request header
2. `YOUTUBE_API_KEY` (or `YT_API_KEY`) environment variable / Space secret

If you duplicate this Space, add your own secrets in the Space settings so clients can connect without sending headers.

## Run locally

```bash
pip install -r requirements.txt
export AI33_API_KEY="your-key"
export YOUTUBE_API_KEY="your-youtube-key"   # optional, for the youtube_* tools
python server.py
# MCP endpoint at http://localhost:7860/mcp
```

## Deploy your own

The whole server is five files: `server.py`, `youtube_tools.py`, `Dockerfile`, `requirements.txt`, and this `README.md` (its frontmatter configures the Space).

1. [Duplicate the Space](https://huggingface.co/spaces/pima5/ai33-mcp?duplicate=true) or create a new Docker Space and upload the files.
2. Add `AI33_API_KEY` and `YOUTUBE_API_KEY` secrets in the Space settings (or skip them and require clients to send the keys as headers).
3. Your endpoint is `https://<user>-<space>.hf.space/mcp`.
