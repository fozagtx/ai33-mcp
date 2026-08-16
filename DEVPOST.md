<p align="center">
  <img src="docs/mediagen-logo.png" alt="mediagen logo" width="280" />
</p>

# Mediagen

Phone-ready media and content MCP for busy creators.

**Live MCP:** `https://pima5-ai33-mcp.hf.space/mcp`

## Inspiration

Busy people already live in Claude on their phones. What they do not have is time to jump between AI33, YouTube Studio, editors, and download tabs just to ship one piece of content.

We wanted a content machine that sits inside the chat they already use. Connect once. Ask from the phone. Keep moving between work blocks instead of babysitting five dashboards.

## What it does

Mediagen is a remote MCP server that turns Claude into a media and research workstation:

- Voice, dialogue, cloning, dubbing, SFX, music, and images through AI33 Pro
- Live YouTube niche research: search, saturation scans, channel outliers, video context, rising channels
- Streamable HTTP endpoint you can add as a Claude connector in about two minutes

No invented YouTube stats. Numbers come from the live YouTube Data API (or a short cache). Media jobs return real task IDs and downloadable asset URLs.

## How we built it

- Python FastMCP server over streamable HTTP (`server.py`)
- AI33 Pro API wired as MCP tools for the full media surface
- YouTube Data API v3 tools ported from AuspexIQ with the payment layer removed (`youtube_tools.py`)
- Docker Space on Hugging Face so the endpoint stays public and phone-reachable
- Keys resolved from request headers or Space secrets so shared deploys stay simple

Stack: Python 3.12, MCP, httpx, uvicorn, Docker, Hugging Face Spaces.

## Challenges we ran into

- Hugging Face OAuth can push code but cannot always restart a paused Space, so runtime recovery needed a manual restart
- Keeping the product honest: live API failures must surface as structured errors, never fake niche scores
- Making the README feel like a product for 9-to-5 creators instead of a tool dump
- Getting real phone screenshots into docs without breaking Space YAML frontmatter rules

## Accomplishments that we're proud of

- A public MCP URL that works from Claude on mobile
- One connector covering media generation and YouTube research
- Clean Docker deploy with secrets-based auth
- Product positioning that matches how people actually create: short windows, phone in hand

## What we learned

- MCP beats "skills" when the job needs live tools and real outputs
- Mobile Claude connectors are the distribution channel for creators who will not open another desktop app
- Hosting details matter: paused Spaces look like product bugs to users
- Clear who-it-helps copy beats long feature tables

## What's next for Mediagen

- Tighter mobile flows for common creator jobs (script to voice to post research)
- More packaging around the full content loop, including edit-side tools
- Better status and credit visibility inside the chat
- Optional one-tap connector installs and clearer onboarding for non-dev creators
