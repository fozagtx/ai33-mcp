---
title: AI33 MCP
emoji: ⚡
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Give Claude AI33 voice/media tools + live YouTube research
tags:
  - mcp
  - agi
  - audio
  - youtube
---

# AI33 MCP

Plug [AI33 Pro](https://ai33.pro) into Claude (or any MCP client) so your assistant can make voice, music, and images — and check YouTube niches with live data — without you leaving the chat.

**MCP URL:** `https://pima5-ai33-mcp.hf.space/mcp`

## Who this is for

- Creators and editors who already use Claude and want TTS, dubbing, voice clone, SFX, music, or images without hopping into another dashboard
- YouTube builders who want niche saturation, outlier videos, and rising channels pulled from the live API instead of guesswork
- Devs wiring the same tools into Cursor, Claude Code, or any MCP client

## What you get

Ask Claude (with this connector on) to things like:

- “Read this script in a MiniMax voice and give me the audio link”
- “Dub this clip into Spanish”
- “Is ‘sleep stories’ worth entering, or is it crowded?”
- “Which recent videos on @veritasium are outliers vs their usual baseline?”

Media jobs return a `task_id` and downloadable asset URLs. YouTube tools only return numbers from live API calls (or a short cache) — they don’t invent stats.

## Add it to Claude

### Claude.ai / Claude desktop

1. Open [Settings → Connectors](https://claude.ai/settings/connectors)
2. **Add custom connector**
3. Name: `ai33`
4. URL: `https://pima5-ai33-mcp.hf.space/mcp`
5. Save, then in a chat turn on the `ai33` connector from the tools menu

This hosted Space already has the AI33 key set as a secret, so you usually don’t paste a key in Claude.

### Claude Code

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

Headers are optional if those keys are set as Space secrets. File inputs (samples, audio, reference images) need public URLs.

## Run it yourself

Needs Python 3.12+, an [AI33](https://ai33.pro) key, and optionally a free [YouTube Data API v3](https://console.cloud.google.com/) key.

```bash
pip install -r requirements.txt
export AI33_API_KEY="your-key"
export YOUTUBE_API_KEY="your-youtube-key"   # optional
python server.py
# http://localhost:7860/mcp
```

```bash
docker build -t ai33-mcp .
docker run --rm -p 7860:7860 -e AI33_API_KEY=your-key ai33-mcp
```

| Key | Used for |
|---|---|
| `AI33_API_KEY` or header `xi-api-key` | Media tools |
| `YOUTUBE_API_KEY` / `YT_API_KEY` or header `x-youtube-api-key` | YouTube tools |
| `PORT` | Default `7860` |

## Tools (short list)

Media: voices, TTS, dialogue, clone, STT, dubbing, voice change/isolate, SFX, MiniMax + Suno music, images, pronunciation dictionaries, tasks.

YouTube: `youtube_search`, `youtube_scan_niche`, `youtube_channel_outliers`, `youtube_video_context`, `youtube_rising_channels`.

## Host your own copy

1. [Duplicate the Space](https://huggingface.co/spaces/pima5/ai33-mcp?duplicate=true)
2. Add your `AI33_API_KEY` (and optional `YOUTUBE_API_KEY`) secrets
3. Point your client at `https://<user>-<space>.hf.space/mcp`

## If something breaks

- **401 on media tools** — bad or missing AI33 key; try `ai33_get_credits`
- **YouTube tools complain about a key** — set `YOUTUBE_API_KEY` or send `x-youtube-api-key`
- **503 from the Space** — it’s paused; open the Space and hit Restart
- **Browser shows 406 on `/mcp`** — normal; use Claude or another MCP client, not the address bar
