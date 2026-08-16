---
title: AI33 MCP
emoji: ⚡
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Phone-ready media & content MCP for busy creators
tags:
  - mcp
  - media
  - content
  - youtube
  - audio
---

# AI33 MCP — a content machine in your pocket

This is not a “generic skill.” It’s a **media & content MCP** you connect once, then run from Claude on your phone: voice, music, images, YouTube research, and the rest of the content workflow — without opening five other sites between meetings.

Built for busy 9-to-5 people who still want to ship content. You ask. The tools run. You keep moving.

**MCP URL:** `https://pima5-ai33-mcp.hf.space/mcp`

## Who it helps

People who create after work, on the commute, or between Slack pings — and don’t have time to bounce between AI33, YouTube Studio, editors, and download tabs.

If Claude is already open on your phone, that’s enough.

## What you can do

Once it’s connected, you can:

- Generate speech, dialogue, clones, dubs, SFX, music, and images
- Research niches live on YouTube (saturation, outliers, rising channels)
- Stay in one chat instead of multitasking across dashboards

<p align="center">
  <img src="docs/media-gen.png" alt="media-gen tools on phone" width="300" />
</p>

<p align="center"><em>On your phone: open Claude, pick a tool, tap Ask — no extra site.</em></p>

## Add it to Claude (2 minutes)

### Claude.ai / Claude desktop

1. Open [Settings → Connectors](https://claude.ai/settings/connectors)
2. **Add custom connector**
3. Name: `ai33` (or `media-gen` if you prefer)
4. URL: `https://pima5-ai33-mcp.hf.space/mcp`
5. Save, then enable it in a chat from the tools menu

This Space already has the AI33 key set as a secret, so you usually don’t paste a key.

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

Headers are optional when those keys are Space secrets. For file inputs (voice samples, audio, reference images), use public URLs.

## Why this beats a normal skill

A skill is a prompt. This is a **live tool server**:

- Real AI33 Pro media jobs with downloadable outputs
- Real YouTube Data API numbers (no invented view counts)
- Works over streamable HTTP so Claude on mobile can call it like any other connector

You’re not “remembering steps.” You’re pressing Ask and getting assets/research back.

## Run your own

Python 3.12+, an [AI33](https://ai33.pro) key, optional free [YouTube Data API v3](https://console.cloud.google.com/) key.

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

| Key | For |
|---|---|
| `AI33_API_KEY` / `xi-api-key` | Media generation |
| `YOUTUBE_API_KEY` / `x-youtube-api-key` | YouTube research |
| `PORT` | Default `7860` |

## Tool map

**Media (AI33):** voices, TTS, dialogue, clone, STT, dubbing, voice change/isolate, SFX, MiniMax + Suno music, images, pronunciation dictionaries, tasks.

**YouTube research:** `youtube_search`, `youtube_scan_niche`, `youtube_channel_outliers`, `youtube_video_context`, `youtube_rising_channels`.

## Host a copy

1. [Duplicate the Space](https://huggingface.co/spaces/pima5/ai33-mcp?duplicate=true)
2. Add `AI33_API_KEY` (+ optional `YOUTUBE_API_KEY`)
3. Point Claude at `https://<user>-<space>.hf.space/mcp`

## Stuck?

- **401 on media** — check the AI33 key / `ai33_get_credits`
- **YouTube key missing** — set `YOUTUBE_API_KEY` or send `x-youtube-api-key`
- **503** — Space is paused; open it and Restart
- **Browser 406 on `/mcp`** — use Claude, not the address bar
