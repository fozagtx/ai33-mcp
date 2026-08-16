---
title: AI33 MCP
emoji: ⚡
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: MCP server for AI33 Pro media + live YouTube research
tags:
  - mcp
  - agi
  - audio
  - youtube
---

# AI33 MCP

Remote [MCP](https://modelcontextprotocol.io) server for the [AI33 Pro](https://ai33.pro) media API (TTS, dialogue, clone, STT, dubbing, SFX, music, images) plus live YouTube Data API v3 research tools. Streamable HTTP.

**Live endpoint:** https://pima5-ai33-mcp.hf.space/mcp

## Features

- AI33 Pro media tools over streamable HTTP (`/mcp`)
- Live YouTube niche/outlier research (`youtube_*` tools)
- Optional server-side keys via Space secrets; clients can also send headers

## Prerequisites

- Python 3.12+
- An [AI33 Pro](https://ai33.pro) API key (for `ai33_*` tools)
- Optional: a free [YouTube Data API v3](https://console.cloud.google.com/) key (for `youtube_*` tools)

## Getting started

```bash
pip install -r requirements.txt
export AI33_API_KEY="your-key"
export YOUTUBE_API_KEY="your-youtube-key"   # optional
python server.py
# MCP: http://localhost:7860/mcp
```
Docker (same layout as the Hugging Face Space):

```bash
docker build -t ai33-mcp .
docker run --rm -p 7860:7860 -e AI33_API_KEY=your-key ai33-mcp
```

## MCP URL

```
https://pima5-ai33-mcp.hf.space/mcp
```

Transport: streamable HTTP (stateless).

## Add to Claude

### Claude.ai / Claude desktop (Connectors)

1. Open [Claude Settings → Connectors](https://claude.ai/settings/connectors) (desktop: **Settings → Connectors**).
2. Click **Add custom connector**.
3. Set:
   - **Name:** `ai33`
   - **URL:** `https://pima5-ai33-mcp.hf.space/mcp`
4. Save the connector.
5. In a chat, open the tools / search menu and enable the `ai33` connector.

The AI33 key is already configured as a Space secret on this deployment, so you usually do not need to paste a key in Claude.

### Claude Code (CLI)

```bash
claude mcp add --transport http ai33 https://pima5-ai33-mcp.hf.space/mcp
```

### Other MCP clients

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

`headers` are optional when the matching Space secrets are set.

Generation tools return a `task_id` (and can wait via `wait_seconds`). File inputs must be public URLs.

## Configuration

| Variable / header | Purpose |
|---|---|
| `AI33_API_KEY` or `xi-api-key` | AI33 media tools |
| `YOUTUBE_API_KEY` / `YT_API_KEY` or `x-youtube-api-key` | YouTube research tools |
| `PORT` | Listen port (default `7860`) |
| `AI33_API_BASE` | Override API host (default `https://api.ai33.pro`) |

Header aliases for AI33: `x-ai33-api-key`, `x-api-key`, or `Authorization: Bearer …`.

## Tools

**AI33:** `ai33_health`, `ai33_get_credits`, `ai33_list_voices`, `ai33_text_to_speech`, `ai33_create_dialogue`, `ai33_clone_voice`, `ai33_delete_cloned_voice`, `ai33_speech_to_text`, `ai33_dub_audio`, `ai33_voice_changer`, `ai33_voice_isolate`, `ai33_generate_sound_effect`, `ai33_generate_music`, `ai33_generate_suno_music`, pronunciation dictionary helpers, image helpers, task helpers.

**YouTube:** `youtube_search`, `youtube_scan_niche`, `youtube_channel_outliers`, `youtube_video_context`, `youtube_rising_channels` (live API data only; failures return structured errors).

## Deploy

This README’s YAML frontmatter configures the Hugging Face Docker Space.

1. [Duplicate the Space](https://huggingface.co/spaces/pima5/ai33-mcp?duplicate=true) or create a Docker Space and upload these files
2. Set `AI33_API_KEY` and optional `YOUTUBE_API_KEY` secrets
3. Endpoint: `https://<user>-<space>.hf.space/mcp`

## Troubleshooting

- **401 on AI33 tools** — missing/invalid key; check `ai33_get_credits`
- **YouTube tools fail with missing key** — set `YOUTUBE_API_KEY` or send `x-youtube-api-key`
- **Space returns 503** — Space is paused/sleeping; open the Space page and restart
- **`/mcp` returns 406 in a browser** — expected; use an MCP client with streamable HTTP
