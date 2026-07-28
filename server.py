#!/usr/bin/env python3
"""AI33 Pro MCP server.

Exposes the AI33 Pro media API (https://api.ai33.pro) as MCP tools over
streamable HTTP: text-to-speech, multi-speaker dialogue, voice cloning,
speech-to-text, dubbing, sound effects, music generation, and image
generation, plus task management.

Authentication: each request needs an AI33 API key, resolved from (in order)
the `xi-api-key` / `x-ai33-api-key` / `x-api-key` request header, an
`Authorization: Bearer <key>` header, or the AI33_API_KEY environment
variable on the server.
"""

import asyncio
import os
import re
import time
import urllib.parse
from contextvars import ContextVar
from typing import Annotated, Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

API_BASE = os.environ.get("AI33_API_BASE", "https://api.ai33.pro").rstrip("/")
PORT = int(os.environ.get("PORT", "7860"))
MAX_WAIT_SECONDS = 300
POLL_INTERVAL_SECONDS = 3.0
VOICE_ID_PREFIXES = ("elevenlabs_", "minimax_", "clone_", "edge_", "kokoro_", "vbee_", "fishaudio_")

_REQUEST_HEADERS: ContextVar[dict] = ContextVar("ai33_request_headers", default={})

READ_ONLY = ToolAnnotations(readOnlyHint=True)
CREATE_TASK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

mcp = FastMCP(
    name="ai33-pro",
    instructions=(
        "Tools for the AI33 Pro media generation API: text-to-speech, dialogue, "
        "voice cloning, voice changing/isolation, speech-to-text, dubbing, sound "
        "effects, music (Suno and MiniMax), pronunciation dictionaries, and image "
        "generation.\n\n"
        "Typical flow: list voices with ai33_list_voices, create a generation task "
        "(e.g. ai33_text_to_speech), then read the result. Creation tools accept a "
        "wait_seconds parameter and, when the task finishes in time, return the "
        "completed task with an asset_urls list of downloadable outputs. If a task "
        "is still running, keep polling with ai33_wait_for_task or ai33_get_task.\n\n"
        "Every ai33_* call needs an AI33 API key: send an 'xi-api-key' request "
        "header, or rely on the AI33_API_KEY environment variable configured on "
        "the server. Check remaining credits with ai33_get_credits.\n\n"
        "The youtube_* tools provide live YouTube Data API v3 research: search, "
        "niche saturation scans with ENTER/CROWDED/AVOID verdicts, channel outlier "
        "audits, single-video performance context, and rising-channel radar. They "
        "need a free YouTube Data API key ('x-youtube-api-key' header or the "
        "YOUTUBE_API_KEY environment variable)."
    ),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)


class _HeaderCaptureMiddleware:
    """Stashes request headers in a context variable so tools can read the API key."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {}
        for raw_name, raw_value in scope.get("headers", []):
            headers[raw_name.decode("latin-1").lower()] = raw_value.decode("latin-1")
        token = _REQUEST_HEADERS.set(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_HEADERS.reset(token)


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": "ai33-mcp/1.0"},
        )
    return _client


def _resolve_api_key() -> str:
    headers = _REQUEST_HEADERS.get()
    for name in ("xi-api-key", "x-ai33-api-key", "x-api-key"):
        value = headers.get(name, "").strip()
        if value:
            return value
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        value = auth[7:].strip()
        if value:
            return value
    for env_name in ("AI33_API_KEY", "XI_API_KEY", "AI33_XI_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    raise ValueError(
        "No AI33 API key available. Send it as an 'xi-api-key' request header "
        "(recommended for shared deployments) or set the AI33_API_KEY environment "
        "variable / Space secret on the server. Get a key at https://ai33.pro."
    )


def _error_hint(status: int) -> str:
    hints = {
        400: "Validation error - check parameter values (for images, also @img reference count).",
        401: "Invalid API key or insufficient credits. Verify the key and check ai33_get_credits.",
        404: "Not found - check the ID and that the resource still exists.",
        422: "Model parameter error - check model_id, aspect_ratio, and resolution values.",
        429: "Rate limited or queue full - wait a moment and retry.",
    }
    return hints.get(status, "See the response body for details.")


async def _api(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    form: Optional[dict] = None,
    files: Optional[list] = None,
) -> dict:
    headers = {"xi-api-key": _resolve_api_key()}
    response = await _http().request(
        method,
        f"{API_BASE}{path}",
        params=params,
        json=json_body,
        data=form,
        files=files,
        headers=headers,
    )
    if response.status_code >= 400:
        body = response.text[:2000]
        raise ValueError(
            f"AI33 API error HTTP {response.status_code} on {method} {path}: {body}\n"
            f"Hint: {_error_hint(response.status_code)}"
        )
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {"raw": response.text[:5000]}
    if isinstance(data, dict):
        return data
    return {"data": data}


def _form_fields(fields: dict) -> dict:
    """Convert values to the string forms the multipart API expects, dropping Nones."""
    import json as _json

    prepared = {}
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            prepared[name] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            prepared[name] = _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            prepared[name] = str(value)
    return prepared


def _validate_voice_id(voice_id: str) -> str:
    if not voice_id.startswith(VOICE_ID_PREFIXES):
        raise ValueError(
            f"voice_id must start with one of: {', '.join(VOICE_ID_PREFIXES)}. "
            "Find valid IDs with ai33_list_voices."
        )
    return voice_id


async def _fetch_remote_file(url: str, *, max_mb: int, default_name: str) -> tuple:
    """Download a source file so it can be re-uploaded to AI33 as multipart data."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}'. Provide a public http(s) URL.")
    try:
        response = await _http().get(url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not download {url}: {exc}") from exc
    if response.status_code >= 400:
        raise ValueError(f"Could not download {url}: HTTP {response.status_code}")
    content = response.content
    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(f"File at {url} is {size_mb:.1f}MB which exceeds the {max_mb}MB limit.")
    name = os.path.basename(urllib.parse.unquote(parsed.path)) or default_name
    if "." not in name:
        name = default_name
    content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
    return name, content, content_type


def _extract_asset_urls(task: dict) -> list:
    """Collect downloadable output URLs from a completed task."""
    urls = []
    meta = task.get("metadata") or {}
    for key, kind in (
        ("audio_url", "audio"),
        ("replacement_audio_url", "audio"),
        ("srt_url", "captions"),
        ("json_url", "transcript"),
        ("output_uri", "audio"),
        ("cover_url", "cover"),
    ):
        if meta.get(key):
            urls.append({"kind": kind, "url": meta[key]})
    if task.get("output_uri"):
        urls.append({"kind": "audio", "url": task["output_uri"]})
    for item in meta.get("result_images") or []:
        url = item.get("imageUrl") or item.get("previewUrl")
        if url:
            urls.append({"kind": "image", "url": url})
    for item in ((meta.get("music_result") or {}).get("data")) or []:
        if item.get("audio_url"):
            urls.append({"kind": "music", "url": item["audio_url"], "title": item.get("title")})
        if item.get("cover_url"):
            urls.append({"kind": "cover", "url": item["cover_url"]})
    if meta.get("image_url"):
        urls.append({"kind": "cover", "url": meta["image_url"]})
    for url in meta.get("all_audio_urls") or []:
        urls.append({"kind": "music", "url": url})
    for clip in ((meta.get("suno_result") or {}).get("clips")) or []:
        if clip.get("audio_url"):
            urls.append(
                {
                    "kind": "music",
                    "url": clip["audio_url"],
                    "title": clip.get("title"),
                    "duration": clip.get("duration"),
                }
            )
        if clip.get("image_url"):
            urls.append({"kind": "cover", "url": clip["image_url"]})
    seen = set()
    deduped = []
    for entry in urls:
        if entry["url"] not in seen:
            deduped.append(entry)
            seen.add(entry["url"])
    return deduped


async def _wait_for_task(task_id: str, wait_seconds: float) -> dict:
    deadline = time.monotonic() + min(wait_seconds, MAX_WAIT_SECONDS)
    while True:
        try:
            task = await _api("GET", f"/v1/task/{task_id}")
        except ValueError as exc:
            message = str(exc)
            transient = any(
                marker in message for marker in ("HTTP 502", "HTTP 503", "HTTP 504", "server_busy")
            )
            if transient and time.monotonic() < deadline:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            raise
        status = task.get("status")
        if status == "done":
            task["asset_urls"] = _extract_asset_urls(task)
            return task
        if status == "error":
            task["error_hint"] = task.get("error_message") or "Task failed; check parameters and credits."
            return task
        if time.monotonic() >= deadline:
            task["note"] = (
                f"Task is still '{status}'. Poll again with ai33_wait_for_task "
                f"(task_id='{task_id}') until status is 'done'."
            )
            meta = task.get("metadata") or {}
            previews = []
            if meta.get("stream_url"):
                previews.append(meta["stream_url"])
            for clip in ((meta.get("suno_stream_result") or {}).get("clips")) or []:
                if clip.get("stream_url"):
                    previews.append(clip["stream_url"])
            if previews:
                task["preview_stream_urls"] = previews
            return task
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _finish_create(response: dict, wait_seconds: float) -> dict:
    task_id = response.get("task_id") or response.get("id")
    if task_id:
        response.setdefault("task_id", task_id)
    if not task_id or wait_seconds <= 0:
        if task_id:
            response.setdefault(
                "note", f"Task created. Poll with ai33_wait_for_task (task_id='{task_id}')."
            )
        return response
    task = await _wait_for_task(str(task_id), wait_seconds)
    task.setdefault("task_id", task_id)
    return task


# ---------------------------------------------------------------------------
# Account and status tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def ai33_health() -> dict:
    """Check AI33 provider health (ElevenLabs and MiniMax availability)."""
    return await _api("GET", "/v1/health-check")


@mcp.tool(annotations=READ_ONLY)
async def ai33_get_credits() -> dict:
    """Get the remaining AI33 credit balance for the current API key."""
    return await _api("GET", "/v1/credits")


@mcp.tool(annotations=READ_ONLY)
async def ai33_list_voices(
    provider: Annotated[
        Literal["elevenlabs", "minimax", "clone", "edge", "kokoro", "vbee", "fishaudio"],
        Field(description="Voice provider to browse. 'clone' lists your cloned voices."),
    ] = "elevenlabs",
    search: Annotated[
        Optional[str],
        Field(description="Search by name, description, language, gender, or tags."),
    ] = None,
    language: Optional[str] = None,
    locale: Optional[str] = None,
    gender: Optional[str] = None,
    accent: Optional[str] = None,
    category: Optional[str] = None,
    sort: Annotated[
        Optional[Literal["score", "task_count", "created_at", "trending"]],
        Field(description="Sort order (fishaudio provider only)."),
    ] = None,
    tag: Annotated[Optional[str], Field(description="Tag filter (fishaudio provider only).")] = None,
    voice_ownership: Annotated[
        Optional[Literal["community", "vbee", "all"]],
        Field(description="Ownership filter (vbee provider only)."),
    ] = None,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """List voices from the v3 voice library.

    The returned voice_id values are already prefixed (e.g. 'elevenlabs_...',
    'minimax_...') and can be used directly in ai33_text_to_speech and
    ai33_create_dialogue.
    """
    params = {"provider": provider, "page": page, "page_size": page_size}
    for name, value in (
        ("search", search),
        ("language", language),
        ("locale", locale),
        ("gender", gender),
        ("accent", accent),
        ("category", category),
        ("sort", sort),
        ("tag", tag),
        ("voice_ownership", voice_ownership),
    ):
        if value is not None:
            params[name] = value
    return await _api("GET", "/v3/voices", params=params)


# ---------------------------------------------------------------------------
# Speech generation tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=CREATE_TASK)
async def ai33_text_to_speech(
    text: Annotated[str, Field(description="Text to speak, up to 1,000,000 characters.", min_length=1)],
    voice_id: Annotated[
        str,
        Field(
            description=(
                "Prefixed v3 voice ID from ai33_list_voices, e.g. "
                "'elevenlabs_hpp4J3VqNfWAUOO0d1Us' or 'minimax_male-qn-qingse'."
            )
        ),
    ],
    speed: Annotated[float, Field(ge=0.5, le=1.5)] = 1.0,
    with_transcript: Annotated[
        bool, Field(description="Also produce SRT captions and word-level JSON transcript.")
    ] = False,
    file_name: Annotated[Optional[str], Field(description="Optional output file name.")] = None,
    pronunciation_dictionary_id: Annotated[
        Optional[int],
        Field(description="Apply a pronunciation dictionary (see ai33_list_dictionaries); only the audio changes."),
    ] = None,
    wait_seconds: Annotated[
        int,
        Field(ge=0, le=MAX_WAIT_SECONDS, description="How long to wait for completion. 0 returns the task_id immediately."),
    ] = 90,
) -> dict:
    """Generate speech from text (costs credits).

    Works with ElevenLabs, MiniMax, cloned, Edge, Kokoro, Vbee, and Fish Audio
    voices via their prefixed voice IDs. On completion the result includes
    asset_urls with the audio (and transcript files when requested).
    """
    form = _form_fields(
        {
            "text": text,
            "voice_id": _validate_voice_id(voice_id),
            "speed": speed,
            "with_transcript": with_transcript,
            "file_name": file_name,
            "pronunciation_dictionary_id": pronunciation_dictionary_id,
        }
    )
    response = await _api("POST", "/v3/text-to-speech", form=form)
    return await _finish_create(response, wait_seconds)


class DialogueSpeaker(BaseModel):
    voice_id: str = Field(
        description="Prefixed v3 voice ID. Speaker order maps to labels A>, B>, C> in the text."
    )
    speed: float = Field(1.0, ge=0.5, le=1.5)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_create_dialogue(
    text: Annotated[
        str,
        Field(
            description=(
                "Dialogue text with speaker labels, one line per turn, e.g. "
                "'A> Hello.\\nB> Hi there.' Labels map to the speakers list by order."
            ),
            min_length=1,
        ),
    ],
    speakers: Annotated[
        list[DialogueSpeaker],
        Field(min_length=2, description="At least two speakers, in label order (A, B, C...)."),
    ],
    delay: Annotated[float, Field(ge=0, le=5, description="Gap between speaker turns in seconds.")] = 0,
    with_transcript: bool = False,
    file_name: Optional[str] = None,
    pronunciation_dictionary_id: Annotated[
        Optional[int],
        Field(description="Apply a pronunciation dictionary to all lines; speaker labels are untouched."),
    ] = None,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Generate multi-speaker dialogue audio from labeled text (costs credits)."""
    speaker_payload = []
    for speaker in speakers:
        _validate_voice_id(speaker.voice_id)
        speaker_payload.append({"voice_id": speaker.voice_id, "speed": speaker.speed})
    form = _form_fields(
        {
            "text": text,
            "speakers": speaker_payload,
            "delay": delay,
            "with_transcript": with_transcript,
            "file_name": file_name,
            "pronunciation_dictionary_id": pronunciation_dictionary_id,
        }
    )
    response = await _api("POST", "/v3/text-to-speech/dialogue", form=form)
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_clone_voice(
    voice_name: Annotated[str, Field(description="Display name for the cloned voice.", min_length=1)],
    audio_url: Annotated[
        str,
        Field(description="Public http(s) URL of a clean voice sample (max 10MB, mp3/wav/m4a)."),
    ],
) -> dict:
    """Clone a voice from an audio sample. Only clone voices you have rights to use.

    The response includes prefixed_voice_id (e.g. 'clone_123') for use in
    ai33_text_to_speech and ai33_create_dialogue.
    """
    name, content, content_type = await _fetch_remote_file(
        audio_url, max_mb=10, default_name="sample.mp3"
    )
    response = await _api(
        "POST",
        "/v3/text-to-speech/voice-clone",
        form=_form_fields({"voice_name": voice_name}),
        files=[("audio_file", (name, content, content_type))],
    )
    data = response.get("data")
    if isinstance(data, dict) and data.get("voice_id") and "prefixed_voice_id" not in response:
        response["prefixed_voice_id"] = f"clone_{data['voice_id']}"
    return response


@mcp.tool(annotations=DESTRUCTIVE)
async def ai33_delete_cloned_voice(
    voice_clone_id: Annotated[str, Field(description="ID of the cloned voice to delete (without the 'clone_' prefix).")],
) -> dict:
    """Permanently delete a cloned voice."""
    return await _api("DELETE", f"/v3/text-to-speech/voice-clone/{urllib.parse.quote(voice_clone_id)}")


# ---------------------------------------------------------------------------
# Audio processing tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=CREATE_TASK)
async def ai33_speech_to_text(
    audio_url: Annotated[
        str,
        Field(
            description=(
                "Public http(s) URL of the audio to transcribe (max 200MB; "
                "mp3, aac, aiff, ogg, opus, wav, webm, flac, m4a)."
            )
        ),
    ],
    tag_audio_events: Annotated[
        bool, Field(description="Tag non-speech audio events such as laughter or music.")
    ] = True,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Transcribe audio to text (costs credits).

    On completion, asset_urls includes an SRT caption file and a word-level
    JSON transcript.
    """
    name, content, content_type = await _fetch_remote_file(
        audio_url, max_mb=200, default_name="audio.mp3"
    )
    response = await _api(
        "POST",
        "/v1/task/speech-to-text",
        form=_form_fields({"tag_audio_events": tag_audio_events}),
        files=[("file", (name, content, content_type))],
    )
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_dub_audio(
    audio_url: Annotated[
        str,
        Field(description="Public http(s) URL of the source audio (mp3/m4a, max 20MB or 5 minutes)."),
    ],
    target_lang: Annotated[str, Field(description="Target language code, e.g. 'es', 'fr', 'vi'.")],
    source_lang: Annotated[str, Field(description="Source language code or 'auto'.")] = "auto",
    num_speakers: Annotated[int, Field(ge=0, description="Number of speakers; 0 = auto-detect.")] = 0,
    allow_voice_cloning: Annotated[
        bool, Field(description="Clone the original voices into the target language.")
    ] = False,
    replacement_voice_id: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional prefixed v3 voice ID (elevenlabs_/minimax_/clone_/edge_/vbee_/fishaudio_; "
                "kokoro not supported). Synthesizes the dubbed SRT once more with this voice and "
                "returns it as replacement_audio_url (voice-only, no background audio). Adds 25% "
                "to the dubbing credit cost."
            )
        ),
    ] = None,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 0,
) -> dict:
    """Dub audio into another language (costs credits; can take several minutes).

    Defaults to returning the task_id immediately; poll with ai33_wait_for_task.
    """
    if replacement_voice_id is not None:
        _validate_voice_id(replacement_voice_id)
        if replacement_voice_id.startswith("kokoro_"):
            raise ValueError("Kokoro voices are not supported as dubbing replacement voices.")
    name, content, content_type = await _fetch_remote_file(
        audio_url, max_mb=20, default_name="audio.mp3"
    )
    response = await _api(
        "POST",
        "/v1/task/dubbing",
        form=_form_fields(
            {
                "num_speakers": num_speakers,
                "disable_voice_cloning": not allow_voice_cloning,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "voice_id": replacement_voice_id,
            }
        ),
        files=[("file", (name, content, content_type))],
    )
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_voice_changer(
    audio_url: Annotated[
        str,
        Field(description="Public http(s) URL of the audio to transform (mp3/m4a/wav, max 300MB or 5 hours)."),
    ],
    voice_id: Annotated[
        str,
        Field(description="Target ElevenLabs voice ID (raw, unprefixed), e.g. '21m00Tcm4TlvDq8ikWAM'."),
    ],
    model_id: str = "eleven_multilingual_sts_v2",
    stability: Annotated[float, Field(ge=0, le=1)] = 0.5,
    similarity_boost: Annotated[float, Field(ge=0, le=1)] = 0.75,
    style: Annotated[float, Field(ge=0, le=1, description="Expressiveness.")] = 0.2,
    use_speaker_boost: bool = True,
    remove_background_noise: bool = False,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Transform the voice in an audio file to another voice, speech-to-speech (costs credits).

    Only use on audio you have rights and consent to transform.
    """
    name, content, content_type = await _fetch_remote_file(
        audio_url, max_mb=300, default_name="audio.mp3"
    )
    settings = {
        "stability": stability,
        "similarity_boost": similarity_boost,
        "style": style,
        "use_speaker_boost": use_speaker_boost,
    }
    response = await _api(
        "POST",
        "/v1/task/voice-changer",
        form=_form_fields(
            {
                "voice_id": voice_id,
                "model_id": model_id,
                "voice_settings": settings,
                "remove_background_noise": remove_background_noise,
            }
        ),
        files=[("file", (name, content, content_type))],
    )
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_voice_isolate(
    audio_url: Annotated[
        str,
        Field(
            description=(
                "Public http(s) URL of the audio to clean (mp3/m4a/wav, max 300MB or "
                "5 hours, minimum 5 seconds)."
            )
        ),
    ],
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Isolate speech from background noise in an audio file (costs credits).

    Useful before transcription or dubbing when the source is noisy.
    """
    name, content, content_type = await _fetch_remote_file(
        audio_url, max_mb=300, default_name="audio.mp3"
    )
    response = await _api(
        "POST", "/v1/task/voice-isolate", files=[("file", (name, content, content_type))]
    )
    return await _finish_create(response, wait_seconds)


# ---------------------------------------------------------------------------
# Sound and music tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=CREATE_TASK)
async def ai33_generate_sound_effect(
    text: Annotated[
        str,
        Field(description="Description of the sound, e.g. 'Thunder rolling with heavy rain'.", max_length=450),
    ],
    duration_seconds: Annotated[
        Optional[float],
        Field(
            ge=0.5,
            le=30,
            description=(
                "Length in seconds (0.5-30). Omit for automatic length. "
                "Cost: 50 credits/second when set, 200 credits for auto."
            ),
        ),
    ] = None,
    prompt_influence: Annotated[float, Field(ge=0, le=1)] = 0.3,
    loop: Annotated[bool, Field(description="Generate a seamlessly loopable effect.")] = False,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 90,
) -> dict:
    """Generate a sound effect from a text description (costs credits)."""
    payload = {
        "text": text,
        "duration_seconds": duration_seconds,
        "prompt_influence": prompt_influence,
        "loop": loop,
        "model_id": "eleven_text_to_sound_v2",
    }
    response = await _api("POST", "/v1/task/sound-effect", json_body=payload)
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_generate_music(
    idea: Annotated[
        Optional[str],
        Field(
            max_length=2000,
            description="Style/mood description, e.g. 'Dreamy lo-fi jazz for a quiet evening'. Provide idea and/or lyrics.",
        ),
    ] = None,
    lyrics: Annotated[Optional[str], Field(max_length=3500)] = None,
    title: Annotated[Optional[str], Field(max_length=40)] = None,
    model: Literal["music-2.5+", "music-2.5", "music-2.0"] = "music-2.5+",
    n: Annotated[int, Field(ge=1, le=3, description="Number of tracks to generate.")] = 1,
    instrumental: Annotated[
        bool, Field(description="Generate without vocals (music-2.5+ only; lyrics are ignored).")
    ] = False,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 0,
) -> dict:
    """Generate music from an idea and/or lyrics (costs credits; takes minutes).

    Defaults to returning the task_id immediately; poll with ai33_wait_for_task.
    Completed tasks expose track URLs in asset_urls.
    """
    if instrumental:
        if model != "music-2.5+":
            raise ValueError("instrumental=true is only supported on model 'music-2.5+'.")
        lyrics = ""
    if not idea and not lyrics:
        raise ValueError("Provide 'idea' and/or 'lyrics'.")
    if model == "music-2.0" and idea and len(idea) < 20:
        raise ValueError("For model 'music-2.0' the idea must be at least 20 characters.")
    payload = {
        "title": title,
        "model": model,
        "generation_type": 1,
        "idea": idea or "",
        "lyrics": lyrics or "",
        "n": n,
        "rewrite_idea_switch": False,
        "instrumental": instrumental,
    }
    response = await _api("POST", "/v1m/task/music-generation", json_body=payload)
    return await _finish_create(response, wait_seconds)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_generate_suno_music(
    description: Annotated[
        Optional[str],
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Simple mode: short song description, e.g. 'Percussive indie pop song "
                "about the border between two lives'. Required unless lyrics or "
                "style_tags are provided."
            ),
        ),
    ] = None,
    make_instrumental: Annotated[
        bool, Field(description="Simple mode only: generate without vocals.")
    ] = False,
    title: Annotated[Optional[str], Field(max_length=80, description="Custom mode: song title.")] = None,
    lyrics: Annotated[
        Optional[str],
        Field(max_length=5000, description="Custom mode: full lyrics, e.g. '[Verse 1]\\n...'."),
    ] = None,
    style_tags: Annotated[
        Optional[str],
        Field(max_length=1000, description="Custom mode: style tags, e.g. 'indie pop, emotional, cinematic drums'."),
    ] = None,
    vocal_gender: Annotated[
        Optional[Literal["f", "m"]], Field(description="Custom mode: vocal gender.")
    ] = None,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 0,
) -> dict:
    """Generate songs with Suno (costs credits; takes minutes; returns 2 clips).

    Two modes, picked automatically: simple mode (just a description) or custom
    mode (lyrics and/or style_tags, plus optional title and vocal_gender).
    Defaults to returning the task_id immediately; poll with ai33_wait_for_task.
    While processing, preview_stream_urls may be available; final tracks appear
    in asset_urls when status is 'done'.
    """
    custom = bool(lyrics or style_tags or title or vocal_gender)
    if custom:
        if not lyrics and not style_tags:
            raise ValueError("Custom mode needs 'lyrics' and/or 'style_tags'.")
        payload = {
            "create_mode": "custom",
            "title": title or "",
            "lyrics": lyrics or "",
            "tags": style_tags or "",
        }
        if vocal_gender:
            payload["vocal_gender"] = vocal_gender
    else:
        if not description:
            raise ValueError(
                "Provide 'description' (simple mode) or 'lyrics'/'style_tags' (custom mode)."
            )
        payload = {
            "create_mode": "simple",
            "gpt_description_prompt": description,
            "make_instrumental": make_instrumental,
        }
    response = await _api("POST", "/v1s/task/music-generation", json_body=payload)
    return await _finish_create(response, wait_seconds)


# ---------------------------------------------------------------------------
# Image tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def ai33_list_image_models() -> dict:
    """List available image generation models and their parameters."""
    return await _api("GET", "/v1i/models")


@mcp.tool(annotations=READ_ONLY)
async def ai33_get_image_price(
    model_id: str = "bytedance-seedream-4.5",
    generations_count: Annotated[int, Field(ge=1)] = 1,
    aspect_ratio: str = "9:16",
    resolution: str = "2K",
    reference_image_count: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Get the credit price of an image generation before running it."""
    payload = {
        "model_id": model_id,
        "generations_count": generations_count,
        "model_parameters": {"aspect_ratio": aspect_ratio, "resolution": resolution},
        "assets": reference_image_count,
    }
    return await _api("POST", "/v1i/task/price", json_body=payload)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_generate_image(
    prompt: Annotated[
        str,
        Field(
            description=(
                "Image description. When using reference images, cite them as @img1, "
                "@img2, ... in the prompt; references must match reference_image_urls."
            ),
            min_length=1,
        ),
    ],
    model_id: str = "bytedance-seedream-4.5",
    generations_count: Annotated[int, Field(ge=1)] = 1,
    aspect_ratio: Annotated[str, Field(description="e.g. '16:9', '9:16', '1:1'.")] = "9:16",
    resolution: Annotated[str, Field(description="e.g. '1K', '2K', '4K'.")] = "2K",
    reference_image_urls: Annotated[
        Optional[list[str]],
        Field(description="Public http(s) URLs of reference images (max 5MB each)."),
    ] = None,
    wait_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Generate images from a text prompt, optionally guided by reference images (costs credits).

    Completed tasks expose image URLs in asset_urls. Use ai33_get_image_price
    first to check the cost.
    """
    reference_image_urls = reference_image_urls or []
    refs = {int(value) for value in re.findall(r"@img(\d+)", prompt)}
    if refs and max(refs) > len(reference_image_urls):
        raise ValueError(
            f"Prompt references @img{max(refs)} but only {len(reference_image_urls)} "
            "reference_image_urls were provided."
        )
    files = []
    for index, url in enumerate(reference_image_urls, start=1):
        name, content, content_type = await _fetch_remote_file(
            url, max_mb=5, default_name=f"reference-{index}.png"
        )
        files.append(("assets", (name, content, content_type)))
    form = _form_fields(
        {
            "prompt": prompt,
            "model_id": model_id,
            "generations_count": generations_count,
            "model_parameters": {"aspect_ratio": aspect_ratio, "resolution": resolution},
        }
    )
    response = await _api("POST", "/v1i/task/generate-image", form=form, files=files or None)
    return await _finish_create(response, wait_seconds)


# ---------------------------------------------------------------------------
# Pronunciation dictionary tools
# ---------------------------------------------------------------------------


class DictionaryRule(BaseModel):
    from_text: str = Field(description="Text to match, e.g. 'AI'.")
    to_text: str = Field(description="Replacement to speak instead, e.g. 'Ay Eye'.")
    match_type: Literal["word", "contains"] = Field(
        "word", description="'word' matches whole words; 'contains' matches substrings."
    )
    case_sensitive: bool = False

    def payload(self) -> dict:
        return {
            "from": self.from_text,
            "to": self.to_text,
            "matchType": self.match_type,
            "caseSensitive": self.case_sensitive,
        }


@mcp.tool(annotations=READ_ONLY)
async def ai33_list_dictionaries() -> dict:
    """List your pronunciation dictionaries.

    Use a dictionary's id as pronunciation_dictionary_id in ai33_text_to_speech
    or ai33_create_dialogue to fix how brand names, acronyms, or foreign words
    are spoken. Only the audio changes; the text stays as given.
    """
    return await _api("GET", "/v3/dictionaries")


@mcp.tool(annotations=CREATE_TASK)
async def ai33_create_dictionary(
    name: Annotated[str, Field(min_length=1, description="Dictionary name, e.g. 'Brand names'.")],
    rules: Annotated[list[DictionaryRule], Field(min_length=1)],
) -> dict:
    """Create a pronunciation dictionary of text-replacement rules."""
    payload = {"name": name, "rules": [rule.payload() for rule in rules]}
    return await _api("POST", "/v3/dictionaries", json_body=payload)


@mcp.tool(annotations=CREATE_TASK)
async def ai33_update_dictionary(
    dictionary_id: int,
    name: Optional[str] = None,
    rules: Optional[list[DictionaryRule]] = None,
) -> dict:
    """Update a pronunciation dictionary's name and/or rules."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if rules is not None:
        payload["rules"] = [rule.payload() for rule in rules]
    if not payload:
        raise ValueError("Provide 'name' and/or 'rules' to update.")
    return await _api("PUT", f"/v3/dictionaries/{dictionary_id}", json_body=payload)


@mcp.tool(annotations=DESTRUCTIVE)
async def ai33_delete_dictionary(dictionary_id: int) -> dict:
    """Delete a pronunciation dictionary."""
    return await _api("DELETE", f"/v3/dictionaries/{dictionary_id}")


@mcp.tool(annotations=READ_ONLY)
async def ai33_preview_dictionary(
    text: Annotated[str, Field(min_length=1, description="Text to run the rules against.")],
    rules: Annotated[list[DictionaryRule], Field(min_length=1)],
) -> dict:
    """Preview how dictionary rules would rewrite text before synthesis (no credits)."""
    payload = {"text": text, "rules": [rule.payload() for rule in rules]}
    return await _api("POST", "/v3/dictionaries/preview", json_body=payload)


# ---------------------------------------------------------------------------
# Task management tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def ai33_get_task(
    task_id: Annotated[str, Field(description="Task ID returned by a creation tool.")],
) -> dict:
    """Get the current status of a task without waiting.

    Status values: 'doing', 'done', 'error'. Completed tasks include asset_urls.
    """
    task = await _api("GET", f"/v1/task/{urllib.parse.quote(task_id)}")
    if task.get("status") == "done":
        task["asset_urls"] = _extract_asset_urls(task)
    return task


@mcp.tool(annotations=READ_ONLY)
async def ai33_wait_for_task(
    task_id: Annotated[str, Field(description="Task ID returned by a creation tool.")],
    timeout_seconds: Annotated[int, Field(ge=1, le=MAX_WAIT_SECONDS)] = 120,
) -> dict:
    """Poll a task until it finishes or the timeout elapses.

    If the task is still running at timeout, call this again with the same
    task_id. Completed tasks include asset_urls with downloadable outputs.
    """
    return await _wait_for_task(task_id, timeout_seconds)


@mcp.tool(annotations=READ_ONLY)
async def ai33_list_tasks(
    page: Annotated[int, Field(ge=1)] = 1,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    type: Annotated[
        Optional[str],
        Field(description="Optional task type filter, e.g. 'tts' or 'imagen2'."),
    ] = None,
) -> dict:
    """List recent tasks for the current API key."""
    params: dict[str, Any] = {"page": page, "limit": limit}
    if type:
        params["type"] = type
    return await _api("GET", "/v1/tasks", params=params)


@mcp.tool(annotations=DESTRUCTIVE)
async def ai33_delete_tasks(
    task_ids: Annotated[list[str], Field(min_length=1, description="Task IDs to delete or refund.")],
) -> dict:
    """Delete (or refund, where eligible) one or more tasks. This cannot be undone."""
    return await _api("POST", "/v1/task/delete", json_body={"task_ids": task_ids})


# ---------------------------------------------------------------------------
# YouTube tools (ported from AuspexIQ, payment layer removed)
# ---------------------------------------------------------------------------

import youtube_tools

youtube_tools.register(mcp, READ_ONLY, lambda: _REQUEST_HEADERS.get())


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
async def index(request):
    from starlette.responses import HTMLResponse

    host = request.headers.get("host", f"localhost:{PORT}")
    scheme = request.headers.get("x-forwarded-proto", "https" if "hf.space" in host else "http")
    endpoint = f"{scheme}://{host}/mcp"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AI33 Pro MCP Server</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:3rem auto;padding:0 1rem;line-height:1.6}}
code,pre{{background:#f4f4f5;border-radius:6px;padding:2px 6px}}pre{{padding:12px;overflow-x:auto}}</style></head>
<body>
<h1>AI33 Pro MCP Server</h1>
<p>MCP server for the <a href="https://ai33.pro">AI33 Pro</a> media API:
text-to-speech, dialogue, voice cloning, speech-to-text, dubbing, sound effects,
music, and image generation.</p>
<p><strong>Endpoint (streamable HTTP):</strong> <code>{endpoint}</code></p>
<p>Authenticate by sending your AI33 key as an <code>xi-api-key</code> header,
or configure the <code>AI33_API_KEY</code> secret on the server.</p>
<pre>{{
  "mcpServers": {{
    "ai33": {{
      "type": "http",
      "url": "{endpoint}",
      "headers": {{ "xi-api-key": "YOUR_AI33_API_KEY" }}
    }}
  }}
}}</pre>
</body></html>"""
    return HTMLResponse(html)


def main() -> None:
    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(_HeaderCaptureMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
