"""YouTube Data API v3 tools (ported from AuspexIQ, payment layer removed) — 2026-08-16.

Live YouTube niche analysis: search, niche saturation scans, channel outlier
audits, single-video context, and rising-channel radar. Every number comes from
the YouTube Data API v3 at request time or a short-TTL cache of a previous live
response; failures surface as structured {"ok": false, "error": ...} objects.

Authentication: a free YouTube Data API v3 key (Google Cloud Console, no
billing required; 10,000 quota units/day), resolved from the
`x-youtube-api-key` request header or the YOUTUBE_API_KEY / YT_API_KEY
environment variable.
"""

import asyncio
import math
import os
import re
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Annotated, Optional
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
_PACIFIC = ZoneInfo("America/Los_Angeles")
_QUOTA_UPSTREAM_REASONS = {"quotaExceeded", "dailyLimitExceeded"}

UPSTREAM_TIMEOUT_S = 10
DAILY_UNIT_BUDGET = int(os.environ.get("YT_DAILY_UNIT_BUDGET", "9000"))
COST_SEARCH = 100
COST_LIST = 1

SHORTS_MAX_SECONDS = 62
BASELINE_RECENT_UPLOADS = 30
BASELINE_DEEP_CHANNELS = 10
BASELINE_MIN_VIDEOS = 5
BASELINE_FLOOR = 100
BASELINE_MIN_AGE_DAYS = 7
BASELINE_CONCURRENCY = 5

SCAN_OUTLIER_MULTIPLE = 3.0
SCAN_OUTLIERS_MAX = 15
SMALL_CHANNEL_SUBS = 100_000

FRESH_WINDOW_DAYS = 90
OPENNESS_W_DIVERSITY = 40
OPENNESS_W_CONCENTRATION = 25
OPENNESS_W_FRESHNESS = 20
OPENNESS_W_SMALL_OUTLIERS = 15
SMALL_OUTLIERS_CAP = 5
VERDICT_ENTER_MAX_SATURATION = 45
VERDICT_ENTER_MIN_SMALL_OUTLIERS = 2
VERDICT_AVOID_MIN_SATURATION = 70
VERDICT_AVOID_MAX_FRESH_SHARE = 0.10

VIDEO_MEGA_OUTLIER_MULTIPLE = 10.0
VIDEO_ABOVE_BASELINE_MULTIPLE = 1.5
VIDEO_TYPICAL_MULTIPLE = 0.5
VIDEO_CONTEXT_WORST_CASE_UNITS = 4 * COST_LIST

RADAR_SEARCH_RESULTS = 50
RADAR_DEEP_CHANNELS = 15
RADAR_MAX_CHANNELS = 10
RADAR_MIN_MOMENTUM = 1.0
RADAR_WORST_CASE_UNITS = COST_SEARCH + 2 * COST_LIST + RADAR_DEEP_CHANNELS * 2 * COST_LIST

SCAN_WORST_CASE_UNITS = COST_SEARCH + 2 * COST_LIST + BASELINE_DEEP_CHANNELS * 2 * COST_LIST
SEARCH_WORST_CASE_UNITS = COST_SEARCH + COST_LIST

SCAN_CACHE_TTL_S = 6 * 3600
CHANNEL_CACHE_TTL_S = 3 * 3600
SEARCH_CACHE_TTL_S = 15 * 60
CACHE_MAX_ENTRIES = 500

_get_request_headers = lambda: {}


class ToolFault(Exception):
    """Structured tool failure, returned to the client as {"ok": false, "error": {...}}."""

    def __init__(self, code, message, retryable):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_response(self):
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message, "retryable": self.retryable},
        }


class RequestMeter:
    def __init__(self):
        self.units = 0


class QuotaGuard:
    """Tracks units spent per Google quota day (resets at midnight Pacific)."""

    def __init__(self, budget):
        self.budget = budget
        self._day = None
        self._spent = 0

    def _roll(self):
        today = datetime.now(_PACIFIC).date()
        if today != self._day:
            self._day = today
            self._spent = 0

    @property
    def remaining(self):
        self._roll()
        return max(0, self.budget - self._spent)

    def next_reset_iso(self):
        now = datetime.now(_PACIFIC)
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    def precheck(self, worst_case_units):
        self._roll()
        if worst_case_units > self.remaining:
            raise ToolFault(
                "QUOTA_EXHAUSTED",
                f"Daily YouTube quota budget is spent ({self._spent}/{self.budget} units used "
                f"today; this call needs up to {worst_case_units}). The budget resets at "
                f"midnight Pacific: {self.next_reset_iso()}. Retry after that.",
                True,
            )

    def spend(self, units, meter):
        self._roll()
        self._spent += units
        meter.units += units


quota = QuotaGuard(DAILY_UNIT_BUDGET)


class TTLCache:
    """In-memory TTL cache of real fetched responses; evicts oldest first."""

    def __init__(self, max_entries):
        self.max_entries = max_entries
        self._entries = OrderedDict()

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, payload, fetched_at = entry
        if time.monotonic() >= expires_at:
            del self._entries[key]
            return None
        return payload, fetched_at

    def put(self, key, payload, ttl_s, fetched_at):
        self._entries[key] = (time.monotonic() + ttl_s, payload, fetched_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


cache = TTLCache(CACHE_MAX_ENTRIES)

_yt_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _yt_client
    if _yt_client is None:
        _yt_client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S)
    return _yt_client


def _yt_api_key():
    headers = _get_request_headers()
    for name in ("x-youtube-api-key", "x-yt-api-key"):
        value = (headers.get(name) or "").strip()
        if value:
            return value
    for env_name in ("YOUTUBE_API_KEY", "YT_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    raise ToolFault(
        "MISSING_API_KEY",
        "No YouTube API key available. Send it as an 'x-youtube-api-key' request header "
        "or set the YOUTUBE_API_KEY environment variable / Space secret. Keys are free: "
        "Google Cloud Console -> create a project -> enable 'YouTube Data API v3' -> "
        "Credentials -> Create API key (10,000 quota units/day, no billing needed).",
        False,
    )


def _sanitize(text, key):
    return text.replace(key, "***") if key else text


def _error_details(response):
    try:
        err = response.json().get("error", {})
        errors = err.get("errors") or [{}]
        return errors[0].get("reason", "unknown"), err.get("message", response.text[:200])
    except Exception:
        return "unknown", response.text[:200]


async def _yt_get(resource, params, cost, meter):
    key = _yt_api_key()
    quota.spend(cost, meter)
    try:
        response = await _http().get(f"{YT_API_BASE}/{resource}", params={**params, "key": key})
    except httpx.TimeoutException:
        raise ToolFault(
            "UPSTREAM_TIMEOUT",
            f"YouTube API '{resource}' call exceeded {UPSTREAM_TIMEOUT_S}s. Retry shortly.",
            True,
        )
    except httpx.HTTPError as exc:
        raise ToolFault(
            "YT_API_ERROR",
            _sanitize(f"YouTube API '{resource}' request failed: {exc}. Retry may help.", key),
            True,
        )
    if response.status_code == 200:
        return response.json()
    reason, message = _error_details(response)
    if reason in _QUOTA_UPSTREAM_REASONS:
        raise ToolFault(
            "QUOTA_EXHAUSTED",
            f"YouTube reports the API key's daily quota is exhausted. Quota resets at "
            f"midnight Pacific: {quota.next_reset_iso()}. Retry after that.",
            True,
        )
    raise ToolFault(
        "YT_API_ERROR",
        _sanitize(
            f"YouTube API '{resource}' returned HTTP {response.status_code} "
            f"({reason}): {message}. Retry may help.",
            key,
        ),
        True,
    )


async def search_videos(query, region_code, published_after_iso, max_results, meter, order="relevance"):
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "order": order,
        "maxResults": max_results,
        "regionCode": region_code,
    }
    if published_after_iso:
        params["publishedAfter"] = published_after_iso
    return await _yt_get("search", params, COST_SEARCH, meter)


async def list_videos(video_ids, meter):
    return await _yt_get(
        "videos",
        {"part": "snippet,statistics,contentDetails,liveStreamingDetails", "id": ",".join(video_ids)},
        COST_LIST,
        meter,
    )


async def list_channels(channel_ids, meter):
    return await _yt_get(
        "channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(channel_ids)}, COST_LIST, meter
    )


async def channel_by_handle(handle, meter):
    return await _yt_get(
        "channels", {"part": "snippet,statistics,contentDetails", "forHandle": handle}, COST_LIST, meter
    )


async def playlist_page(playlist_id, max_results, page_token, meter):
    params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token
    return await _yt_get("playlistItems", params, COST_LIST, meter)


# ---------------------------------------------------------------------------
# Pure analysis helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def duration_seconds(iso_duration):
    if not iso_duration:
        return 0
    match = _DURATION_RE.match(iso_duration)
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_short(seconds):
    return seconds <= SHORTS_MAX_SECONDS


def lifetime_average(view_count, video_count):
    if not video_count:
        return 0.0
    return view_count / video_count


def recent_median_baseline(uploads, now):
    """Median views of qualifying recent uploads; None when too few qualify."""
    cutoff = now - timedelta(days=BASELINE_MIN_AGE_DAYS)
    qualifying = [
        v["views"]
        for v in uploads
        if v["views"] is not None
        and v["published_at"] is not None
        and v["published_at"] < cutoff
        and not is_short(v["seconds"])
        and not v.get("stream")  # cumulative stream views would poison the median
    ]
    if len(qualifying) < BASELINE_MIN_VIDEOS:
        return None
    return float(median(qualifying))


def outlier_multiple(views, baseline):
    if baseline < BASELINE_FLOOR:
        return None
    return views / baseline


def views_per_day(views, published_at, now):
    age_days = max((now - published_at).total_seconds() / 86400, 1.0)
    return views / age_days


def classify_video(multiple):
    if multiple is None:
        return "INSUFFICIENT_BASELINE"
    if multiple >= VIDEO_MEGA_OUTLIER_MULTIPLE:
        return "MEGA_OUTLIER"
    if multiple >= SCAN_OUTLIER_MULTIPLE:
        return "OUTLIER"
    if multiple >= VIDEO_ABOVE_BASELINE_MULTIPLE:
        return "ABOVE_BASELINE"
    if multiple >= VIDEO_TYPICAL_MULTIPLE:
        return "TYPICAL"
    return "UNDERPERFORMER"


def normalize_video(item):
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    seconds = duration_seconds(item.get("contentDetails", {}).get("duration"))
    published = snippet.get("publishedAt")
    return {
        "id": item.get("id", ""),
        "title": snippet.get("title", "").strip(),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", "").strip(),
        "published_at": parse_timestamp(published) if published else None,
        "views": int(stats["viewCount"]) if "viewCount" in stats else None,
        "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
        "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
        "seconds": seconds,
        "stream": snippet.get("liveBroadcastContent") in ("live", "upcoming")
        or "liveStreamingDetails" in item,
    }


def normalize_channel(item):
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    hidden = stats.get("hiddenSubscriberCount", False)
    return {
        "id": item.get("id", ""),
        "title": snippet.get("title", "").strip(),
        "subs": None if hidden else int(stats.get("subscriberCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "uploads": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
    }


def assess_niche(videos, outlier_records, now):
    """Signals, saturation score, verdict, and reasons over the analyzed set."""
    n = len(videos)
    if n == 0:
        return (
            None,
            "NO_DATA",
            [
                "no qualifying long-form videos were found for this query in the recency "
                "window, so no saturation score or entry verdict can be computed; try a "
                "broader query or a longer recency window"
            ],
            {
                "channel_diversity": None,
                "top3_view_concentration": None,
                "fresh_share_90d": None,
                "small_channel_outliers": 0,
            },
        )

    unique_channels = {v["channel_id"] for v in videos}
    u = len(unique_channels)
    diversity = u / n

    views_by_channel = defaultdict(int)
    for v in videos:
        views_by_channel[v["channel_id"]] += v["views"]
    total_views = sum(views_by_channel.values())
    top3_views = sum(sorted(views_by_channel.values(), reverse=True)[:3])
    c3 = top3_views / total_views if total_views else 0.0

    fresh_cutoff = now - timedelta(days=FRESH_WINDOW_DAYS)
    fresh_count = sum(1 for v in videos if v["published_at"] >= fresh_cutoff)
    f90 = fresh_count / n if n else 0.0

    sw = sum(1 for r in outlier_records if r["subs"] is not None and r["subs"] < SMALL_CHANNEL_SUBS)

    openness = (
        OPENNESS_W_DIVERSITY * diversity
        + OPENNESS_W_CONCENTRATION * (1 - c3)
        + OPENNESS_W_FRESHNESS * f90
        + OPENNESS_W_SMALL_OUTLIERS * min(sw, SMALL_OUTLIERS_CAP) / SMALL_OUTLIERS_CAP
    )
    saturation = round(100 - openness)

    if saturation <= VERDICT_ENTER_MAX_SATURATION and sw >= VERDICT_ENTER_MIN_SMALL_OUTLIERS:
        verdict = "ENTER"
        lead = (
            f"saturation {saturation}/100 is at or below {VERDICT_ENTER_MAX_SATURATION} "
            f"with {sw} small-channel breakout(s); there is room for a new entrant"
        )
    elif saturation > VERDICT_AVOID_MIN_SATURATION or f90 < VERDICT_AVOID_MAX_FRESH_SHARE:
        verdict = "AVOID"
        parts = []
        if saturation > VERDICT_AVOID_MIN_SATURATION:
            parts.append(f"saturation {saturation}/100 exceeds {VERDICT_AVOID_MIN_SATURATION}")
        if f90 < VERDICT_AVOID_MAX_FRESH_SHARE:
            parts.append(
                f"only {round(f90 * 100)}% of analyzed videos are from the last "
                f"{FRESH_WINDOW_DAYS} days; the niche looks stale"
            )
        lead = " and ".join(parts)
    else:
        verdict = "CROWDED"
        if saturation <= VERDICT_ENTER_MAX_SATURATION:
            lead = (
                f"saturation {saturation}/100 is moderate, but only {sw} small-channel "
                f"breakout(s) cleared the bar; ENTER needs at least "
                f"{VERDICT_ENTER_MIN_SMALL_OUTLIERS}"
            )
        else:
            lead = (
                f"saturation {saturation}/100 sits between {VERDICT_ENTER_MAX_SATURATION} "
                f"and {VERDICT_AVOID_MIN_SATURATION}; established channels dominate but "
                f"the niche is not closed"
            )

    reasons = [
        lead,
        f"{u} unique channels across {n} analyzed videos",
        f"top 3 channels hold {round(c3 * 100)}% of the views in the result set",
        f"{round(f90 * 100)}% of analyzed videos were published in the last {FRESH_WINDOW_DAYS} days",
        f"{sw} outlier video(s) came from channels under {SMALL_CHANNEL_SUBS:,} subscribers",
    ]
    signals = {
        "channel_diversity": round(diversity, 3),
        "top3_view_concentration": round(c3, 3),
        "fresh_share_90d": round(f90, 3),
        "small_channel_outliers": sw,
    }
    return saturation, verdict, reasons, signals


# ---------------------------------------------------------------------------
# Shared pipeline plumbing
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _watch_url(video_id):
    return "https://www.youtube.com/watch?" + urlencode({"v": video_id})


def _dedupe(values):
    return list(dict.fromkeys(values))


def _chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _meta(cache_state, fetched_at_iso, units_spent):
    return {
        "cache": cache_state,
        "fetched_at": fetched_at_iso,
        "quota_units_spent": units_spent,
        "quota_units_remaining_today": quota.remaining,
    }


async def _execute(cache_key, ttl_s, worst_case_units, run):
    """Cache lookup, quota precheck, pipeline, cache store, structured errors."""
    meter = RequestMeter()
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            payload, fetched_at = cached
            return {**payload, "meta": _meta("hit", fetched_at, 0)}
        quota.precheck(worst_case_units)
        payload = await run(meter)
        fetched_at = _iso(_now())
        cache.put(cache_key, payload, ttl_s, fetched_at)
        return {**payload, "meta": _meta("miss", fetched_at, meter.units)}
    except ToolFault as fault:
        return fault.to_response()
    except Exception as exc:  # a tool must never raise to the client
        return ToolFault("YT_API_ERROR", f"Unexpected server error: {exc}. Retry may help.", True).to_response()


def _video_format(record):
    if record.get("stream"):
        return "livestream"
    if is_short(record.get("seconds", 0)):
        return "short"
    return "video"


def _format_outlier(record):
    method_label = "recent median" if record["baseline_method"] == "recent_median" else "lifetime average"
    return {
        "title": record["title"],
        "url": _watch_url(record["id"]),
        "channel": record["channel_title"],
        "channel_id": record["channel_id"],
        "channel_subs": record["subs"] if record["subs"] is not None else 0,
        "views": record["views"],
        "published_at": _iso(record["published_at"]),
        "format": _video_format(record),
        "channel_baseline": round(record["baseline"]),
        "baseline_method": record["baseline_method"],
        "outlier_multiple": round(record["multiple"], 2),
        "why": f"{record['multiple']:.1f}x this channel's {method_label}",
    }


async def _recent_uploads(channel, meter):
    if not channel["uploads"]:
        return []
    page = await playlist_page(channel["uploads"], BASELINE_RECENT_UPLOADS, None, meter)
    upload_ids = [
        item["contentDetails"]["videoId"]
        for item in page.get("items", [])
        if item.get("contentDetails", {}).get("videoId")
    ]
    if not upload_ids:
        return []
    data = await list_videos(upload_ids, meter)
    return [normalize_video(item) for item in data.get("items", [])]


async def _deep_baseline(channel, now, meter):
    fallback = (lifetime_average(channel["view_count"], channel["video_count"]), "lifetime_avg")
    try:
        uploads = await _recent_uploads(channel, meter)
        baseline = recent_median_baseline(uploads, now)
        if baseline is None:
            return fallback
        return baseline, "recent_median"
    except ToolFault as fault:
        if fault.code in ("QUOTA_EXHAUSTED", "MISSING_API_KEY"):
            raise
        return fallback


def _validate_region(region_code):
    region = (region_code or "US").strip().upper()
    if len(region) != 2 or not region.isalpha():
        raise ToolFault(
            "INVALID_INPUT", "'region_code' must be an ISO 3166-1 alpha-2 code such as 'US' or 'GB'.", False
        )
    return region


async def _resolve_channel(reference, meter):
    ref = reference.strip()
    channel_id = None
    handle = None
    if ref.startswith("UC") and len(ref) == 24 and " " not in ref:
        channel_id = ref
    elif ref.startswith(("http://", "https://")) or "youtube.com" in ref or "youtu.be" in ref:
        parsed = urlparse(ref if "://" in ref else "https://" + ref)
        segments = [s for s in parsed.path.split("/") if s]
        host = (parsed.hostname or "").lower()
        if host.endswith("youtu.be") or (segments and segments[0] in ("watch", "shorts", "embed", "live")):
            raise ToolFault(
                "INVALID_INPUT",
                "That looks like a video URL, not a channel. Pass a channel ID (UC...), an "
                "@handle, or a channel URL - or use youtube_video_context for a single video.",
                False,
            )
        if segments and segments[0] == "channel" and len(segments) > 1:
            channel_id = segments[1]
        elif segments:
            at_segments = [s for s in segments if s.startswith("@")]
            handle = at_segments[0][1:] if at_segments else segments[-1]
    elif ref.startswith("@"):
        handle = ref[1:]
    else:
        handle = ref
    if channel_id:
        data = await list_channels([channel_id], meter)
    elif handle:
        data = await channel_by_handle(handle, meter)
    else:
        data = {}
    items = data.get("items") or []
    if not items:
        raise ToolFault(
            "CHANNEL_NOT_FOUND",
            f"Could not resolve '{reference}' to a YouTube channel. Pass a channel ID "
            f"(UC...), an @handle, or a full youtube.com channel URL.",
            False,
        )
    return normalize_channel(items[0])


def _parse_video_ref(reference):
    ref = (reference or "").strip()
    if not ref:
        raise ToolFault("INVALID_INPUT", "'video' is required: a YouTube video URL or video ID.", False)
    if "/" not in ref and "?" not in ref and len(ref) == 11:
        return ref
    parsed = urlparse(ref if "://" in ref else "https://" + ref)
    host = (parsed.hostname or "").lower()
    segments = [s for s in parsed.path.split("/") if s]
    candidate = None
    if host.endswith("youtu.be") and segments:
        candidate = segments[0]
    elif "youtube.com" in host:
        query = parse_qs(parsed.query)
        if query.get("v"):
            candidate = query["v"][0]
        elif segments and segments[0] in ("shorts", "embed", "live") and len(segments) > 1:
            candidate = segments[1]
    if candidate and len(candidate) == 11:
        return candidate
    raise ToolFault(
        "INVALID_INPUT",
        f"Could not extract a video ID from '{reference}'. Pass a YouTube video URL or "
        f"the 11-character video ID.",
        False,
    )


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


async def _search_pipeline(query, region, published_after, max_results, order, meter):
    search = await search_videos(query, region, published_after, max_results, meter, order=order)
    video_ids = _dedupe(
        item["id"]["videoId"] for item in search.get("items", []) if item.get("id", {}).get("videoId")
    )
    results = []
    if video_ids:
        data = await list_videos(video_ids, meter)
        for item in data.get("items", []):
            video = normalize_video(item)
            results.append(
                {
                    "title": video["title"],
                    "url": _watch_url(video["id"]),
                    "video_id": video["id"],
                    "channel": video["channel_title"],
                    "channel_id": video["channel_id"],
                    "published_at": _iso(video["published_at"]) if video["published_at"] else None,
                    "views": video["views"],
                    "likes": video["like_count"],
                    "comments": video["comment_count"],
                    "duration_seconds": video["seconds"],
                    "format": _video_format(video),
                }
            )
    return {"ok": True, "query": query, "region_code": region, "results": results}


async def _scan_pipeline(query, region, recency_days, max_results, meter):
    now = _now()
    published_after = _iso(now - timedelta(days=recency_days))

    search = await search_videos(query, region, published_after, max_results, meter)
    video_ids = _dedupe(
        item["id"]["videoId"] for item in search.get("items", []) if item.get("id", {}).get("videoId")
    )

    videos = []
    streams_filtered = 0
    if video_ids:
        data = await list_videos(video_ids, meter)
        for item in data.get("items", []):
            video = normalize_video(item)
            if video["views"] is None or video["published_at"] is None:
                continue  # hidden view count or missing metadata
            if is_short(video["seconds"]):
                continue
            if video["stream"]:
                streams_filtered += 1  # cumulative stream views skew comparisons
                continue
            if not video["channel_id"]:
                continue
            videos.append(video)

    channels_by_id = {}
    channel_ids = _dedupe(v["channel_id"] for v in videos)
    if channel_ids:
        data = await list_channels(channel_ids, meter)
        channels_by_id = {
            channel["id"]: channel for channel in (normalize_channel(item) for item in data.get("items", []))
        }
    videos = [v for v in videos if v["channel_id"] in channels_by_id]

    result_counts = Counter(v["channel_id"] for v in videos)
    views_in_set = defaultdict(int)
    for v in videos:
        views_in_set[v["channel_id"]] += v["views"]
    ranked = sorted(result_counts, key=lambda cid: (-result_counts[cid], -views_in_set[cid], cid))
    deep_ids = ranked[:BASELINE_DEEP_CHANNELS]

    baselines = {}
    semaphore = asyncio.Semaphore(BASELINE_CONCURRENCY)

    async def deep(channel_id):
        async with semaphore:
            baselines[channel_id] = await _deep_baseline(channels_by_id[channel_id], now, meter)

    await asyncio.gather(*(deep(cid) for cid in deep_ids))
    for cid in result_counts:
        if cid not in baselines:
            channel = channels_by_id[cid]
            baselines[cid] = (lifetime_average(channel["view_count"], channel["video_count"]), "lifetime_avg")

    outlier_records = []
    for v in videos:
        baseline, method = baselines[v["channel_id"]]
        multiple = outlier_multiple(v["views"], baseline)
        if multiple is not None and multiple >= SCAN_OUTLIER_MULTIPLE:
            outlier_records.append(
                {
                    **v,
                    "baseline": baseline,
                    "baseline_method": method,
                    "multiple": multiple,
                    "subs": channels_by_id[v["channel_id"]]["subs"],
                }
            )
    outlier_records.sort(key=lambda r: -r["multiple"])

    saturation, verdict, reasons, signals = assess_niche(videos, outlier_records, now)

    return {
        "ok": True,
        "query": query,
        "region_code": region,
        "analyzed": {
            "videos": len(videos),
            "channels": len(result_counts),
            "deep_baseline_channels": len(deep_ids),
            "livestreams_filtered": streams_filtered,
        },
        "saturation_score": saturation,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "signals": signals,
        "outliers": [_format_outlier(r) for r in outlier_records[:SCAN_OUTLIERS_MAX]],
    }


async def _channel_pipeline(channel_ref, lookback, min_multiple, meter):
    now = _now()
    channel = await _resolve_channel(channel_ref, meter)

    upload_ids = []
    if channel["uploads"]:
        page_token = None
        while len(upload_ids) < lookback:
            page_size = min(50, lookback - len(upload_ids))
            try:
                page = await playlist_page(channel["uploads"], page_size, page_token, meter)
            except ToolFault as fault:
                # a brand-new channel's uploads playlist can 404: honest zero uploads
                if fault.code == "YT_API_ERROR" and "playlistNotFound" in fault.message:
                    break
                raise
            upload_ids.extend(
                item["contentDetails"]["videoId"]
                for item in page.get("items", [])
                if item.get("contentDetails", {}).get("videoId")
            )
            page_token = page.get("nextPageToken")
            if not page_token:
                break
    upload_ids = _dedupe(upload_ids)[:lookback]

    videos = []
    for batch in _chunks(upload_ids, 50):
        data = await list_videos(batch, meter)
        for item in data.get("items", []):
            video = normalize_video(item)
            if video["views"] is None or video["published_at"] is None:
                continue
            videos.append(video)

    baseline = recent_median_baseline(videos, now)
    if baseline is None:
        baseline = lifetime_average(channel["view_count"], channel["video_count"])
        method = "lifetime_avg"
    else:
        method = "recent_median"

    outlier_records = []
    for v in videos:
        multiple = outlier_multiple(v["views"], baseline)
        if multiple is not None and multiple >= min_multiple:
            outlier_records.append(
                {
                    **v,
                    "channel_title": channel["title"],
                    "baseline": baseline,
                    "baseline_method": method,
                    "multiple": multiple,
                    "subs": channel["subs"],
                }
            )
    outlier_records.sort(key=lambda r: -r["multiple"])

    return {
        "ok": True,
        "channel": {
            "id": channel["id"],
            "title": channel["title"],
            "subscribers": channel["subs"] if channel["subs"] is not None else 0,
            "url": "https://www.youtube.com/channel/" + channel["id"],
        },
        "baseline": round(baseline),
        "baseline_method": method,
        "videos_considered": len(videos),
        "outliers": [_format_outlier(r) for r in outlier_records],
    }


async def _video_context_pipeline(video_id, meter):
    now = _now()
    data = await list_videos([video_id], meter)
    items = data.get("items") or []
    if not items:
        raise ToolFault("VIDEO_NOT_FOUND", f"No YouTube video exists for id '{video_id}'. Check the URL/ID.", False)
    video = normalize_video(items[0])
    if video["views"] is None or video["published_at"] is None:
        raise ToolFault(
            "VIDEO_NOT_FOUND",
            "This video's view count is hidden or metadata is unavailable, so it cannot be analyzed.",
            False,
        )

    channel_data = await list_channels([video["channel_id"]], meter)
    channel_items = channel_data.get("items") or []
    if not channel_items:
        raise ToolFault("CHANNEL_NOT_FOUND", "The video's channel could not be fetched.", False)
    channel = normalize_channel(channel_items[0])

    percentile = None
    velocity_multiple = None
    channel_median_vpd = None
    try:
        uploads = await _recent_uploads(channel, meter)
    except ToolFault as fault:
        if fault.code in ("QUOTA_EXHAUSTED", "MISSING_API_KEY"):
            raise
        uploads = []
    baseline = recent_median_baseline(uploads, now)
    if baseline is None:
        baseline = lifetime_average(channel["view_count"], channel["video_count"])
        method = "lifetime_avg"
    else:
        method = "recent_median"

    peers = [
        u for u in uploads if u["views"] is not None and u["published_at"] is not None and u["id"] != video_id
    ]
    if peers:
        percentile = round(100 * sum(1 for u in peers if u["views"] < video["views"]) / len(peers))
        peer_vpd = sorted(views_per_day(u["views"], u["published_at"], now) for u in peers)
        channel_median_vpd = peer_vpd[len(peer_vpd) // 2]
        if channel_median_vpd > 0:
            velocity_multiple = views_per_day(video["views"], video["published_at"], now) / channel_median_vpd

    multiple = outlier_multiple(video["views"], baseline)
    classification = classify_video(multiple)
    age_days = max((now - video["published_at"]).total_seconds() / 86400, 1.0)

    video_format = _video_format(video)
    why = []
    if multiple is not None:
        why.append(
            f"{multiple:.1f}x the channel's "
            f"{'recent median' if method == 'recent_median' else 'lifetime average'} "
            f"of {round(baseline):,} views"
        )
    else:
        why.append(
            f"channel baseline is below {BASELINE_FLOOR} views, too small to compute a meaningful multiple"
        )
    if video_format != "video":
        why.append(
            f"note: this is a {video_format}, compared against the channel's long-form "
            f"baseline (streams and Shorts accumulate views differently)"
        )
    if percentile is not None:
        why.append(f"beats {percentile}% of the channel's recent uploads")
    if velocity_multiple is not None:
        why.append(
            f"has averaged {velocity_multiple:.1f}x more views per day over its life "
            f"than the channel's recent uploads"
        )

    return {
        "ok": True,
        "video": {
            "title": video["title"],
            "url": _watch_url(video_id),
            "views": video["views"],
            "published_at": _iso(video["published_at"]),
            "age_days": round(age_days, 1),
            "format": video_format,
        },
        "channel": {
            "id": channel["id"],
            "title": channel["title"],
            "subscribers": channel["subs"] if channel["subs"] is not None else 0,
            "baseline": round(baseline),
            "baseline_method": method,
        },
        "outlier_multiple": round(multiple, 2) if multiple is not None else None,
        "percentile_vs_recent_uploads": percentile,
        "views_per_day": round(views_per_day(video["views"], video["published_at"], now)),
        "channel_median_views_per_day": round(channel_median_vpd) if channel_median_vpd else None,
        "velocity_multiple": round(velocity_multiple, 2) if velocity_multiple else None,
        "classification": classification,
        "why": why,
    }


async def _radar_pipeline(niche, region, recency_days, max_subs, meter):
    now = _now()
    published_after = _iso(now - timedelta(days=recency_days))

    search = await search_videos(niche, region, published_after, RADAR_SEARCH_RESULTS, meter)
    video_ids = _dedupe(
        item["id"]["videoId"] for item in search.get("items", []) if item.get("id", {}).get("videoId")
    )
    videos = []
    if video_ids:
        data = await list_videos(video_ids, meter)
        for item in data.get("items", []):
            video = normalize_video(item)
            if video["views"] is None or video["published_at"] is None:
                continue
            if is_short(video["seconds"]) or not video["channel_id"]:
                continue
            videos.append(video)

    channels_by_id = {}
    channel_ids = _dedupe(v["channel_id"] for v in videos)
    if channel_ids:
        data = await list_channels(channel_ids, meter)
        channels_by_id = {
            channel["id"]: channel for channel in (normalize_channel(item) for item in data.get("items", []))
        }
    videos = [v for v in videos if v["channel_id"] in channels_by_id]

    candidates = []
    for cid, channel in channels_by_id.items():
        if channel["subs"] is None or channel["subs"] > max_subs:
            continue
        if lifetime_average(channel["view_count"], channel["video_count"]) < BASELINE_FLOOR:
            continue
        candidates.append(cid)

    result_counts = Counter(v["channel_id"] for v in videos)
    views_in_set = defaultdict(int)
    for v in videos:
        views_in_set[v["channel_id"]] += v["views"]
    candidates.sort(key=lambda cid: (-result_counts[cid], -views_in_set[cid], cid))
    deep_ids = candidates[:RADAR_DEEP_CHANNELS]

    momentum_by_id = {}
    semaphore = asyncio.Semaphore(BASELINE_CONCURRENCY)

    async def measure(channel_id):
        channel = channels_by_id[channel_id]
        async with semaphore:
            try:
                uploads = await _recent_uploads(channel, meter)
            except ToolFault as fault:
                if fault.code in ("QUOTA_EXHAUSTED", "MISSING_API_KEY"):
                    raise
                return
        recent = recent_median_baseline(uploads, now)
        if recent is None:
            return  # not enough qualifying uploads to measure momentum honestly
        lifetime = lifetime_average(channel["view_count"], channel["video_count"])
        momentum_by_id[channel_id] = (recent, lifetime, recent / lifetime)

    await asyncio.gather(*(measure(cid) for cid in deep_ids))

    rising = []
    cooling_count = 0
    for cid, (recent, lifetime, momentum) in momentum_by_id.items():
        if momentum < RADAR_MIN_MOMENTUM:
            cooling_count += 1  # declining channels are never sold as "rising"
            continue
        channel = channels_by_id[cid]
        top_video = max((v for v in videos if v["channel_id"] == cid), key=lambda v: v["views"])
        sample_multiple = outlier_multiple(top_video["views"], recent)
        rising.append(
            {
                "channel": channel["title"],
                "channel_id": cid,
                "url": "https://www.youtube.com/channel/" + cid,
                "subscribers": channel["subs"],
                "videos_in_results": result_counts[cid],
                "recent_median_views": round(recent),
                "lifetime_avg_views": round(lifetime),
                "momentum_multiple": round(momentum, 2),
                "top_video_in_results": {
                    "title": top_video["title"],
                    "url": _watch_url(top_video["id"]),
                    "views": top_video["views"],
                    "outlier_multiple": round(sample_multiple, 2) if sample_multiple is not None else None,
                },
            }
        )
    rising.sort(key=lambda r: -r["momentum_multiple"])

    measured = len(momentum_by_id)
    if measured == 0:
        note = (
            "no channels in this niche could be measured for momentum (too few qualifying "
            "recent uploads); try a broader niche or a higher max_subs"
        )
    elif not rising:
        note = (
            f"none of the {measured} measured channels are genuinely rising - every recent "
            f"median sits below its lifetime average; this niche is cooling"
        )
    else:
        note = (
            f"{len(rising)} of {measured} measured channels have recent-median views above "
            f"their lifetime average ({cooling_count} cooling channels excluded)"
        )

    return {
        "ok": True,
        "niche": niche,
        "region_code": region,
        "analyzed": {
            "videos": len(videos),
            "channels": len(channels_by_id),
            "candidates_measured": measured,
            "cooling_excluded": cooling_count,
        },
        "note": note,
        "rising": rising[:RADAR_MAX_CHANNELS],
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register(mcp, read_only_annotations, get_request_headers):
    """Register the YouTube tools on the shared FastMCP instance."""
    global _get_request_headers
    _get_request_headers = get_request_headers

    @mcp.tool(annotations=read_only_annotations)
    async def youtube_search(
        query: Annotated[str, Field(min_length=1, max_length=100, description="Search terms.")],
        region_code: Annotated[str, Field(description="ISO 3166-1 alpha-2 country code.")] = "US",
        order: Annotated[
            str,
            Field(description="Sort order: relevance, date, viewCount, or rating."),
        ] = "relevance",
        published_within_days: Annotated[
            Optional[int],
            Field(ge=1, le=3650, description="Only return videos published in the last N days."),
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict:
        """Search YouTube videos and return compact results with live view counts.

        Costs ~101 YouTube quota units per uncached call (the free daily key
        quota is 10,000 units).
        """
        if order not in ("relevance", "date", "viewCount", "rating"):
            return ToolFault(
                "INVALID_INPUT", "'order' must be relevance, date, viewCount, or rating.", False
            ).to_response()
        try:
            region = _validate_region(region_code)
        except ToolFault as fault:
            return fault.to_response()
        published_after = (
            _iso(_now() - timedelta(days=published_within_days)) if published_within_days else None
        )
        cache_key = ("search", query.strip().lower(), region, order, published_within_days, max_results)
        return await _execute(
            cache_key,
            SEARCH_CACHE_TTL_S,
            SEARCH_WORST_CASE_UNITS,
            lambda meter: _search_pipeline(query.strip(), region, published_after, max_results, order, meter),
        )

    @mcp.tool(annotations=read_only_annotations)
    async def youtube_scan_niche(
        query: Annotated[str, Field(min_length=2, max_length=80, description="Niche keyword, e.g. a topic phrase.")],
        region_code: str = "US",
        recency_days: Annotated[
            int, Field(ge=30, le=1825, description="Only consider videos published in this window.")
        ] = 365,
        max_results: Annotated[int, Field(ge=10, le=50, description="Search results to analyze.")] = 50,
    ) -> dict:
        """Assess a YouTube niche with live data: who ranks, concentration, outlier
        videos vs each channel's own baseline, a saturation score, and an
        ENTER / CROWDED / AVOID verdict for a new entrant.

        Costs up to ~122 YouTube quota units per uncached call.
        """
        try:
            region = _validate_region(region_code)
        except ToolFault as fault:
            return fault.to_response()
        cache_key = ("scan_niche", query.strip().lower(), region, recency_days, max_results)
        return await _execute(
            cache_key,
            SCAN_CACHE_TTL_S,
            SCAN_WORST_CASE_UNITS,
            lambda meter: _scan_pipeline(query.strip(), region, recency_days, max_results, meter),
        )

    @mcp.tool(annotations=read_only_annotations)
    async def youtube_channel_outliers(
        channel: Annotated[
            str,
            Field(min_length=1, description="A channel ID (UC...), an @handle, or a full youtube.com channel URL."),
        ],
        lookback_videos: Annotated[int, Field(ge=10, le=100, description="Recent uploads to analyze.")] = 30,
        min_multiple: Annotated[
            float, Field(ge=1.5, le=10.0, description="views/baseline threshold to count as an outlier.")
        ] = 2.5,
    ) -> dict:
        """Reveal which of a YouTube channel's recent videos overperformed the
        channel's own baseline (median views of recent long-form uploads)."""
        cache_key = ("channel_outliers", channel.strip().lower(), lookback_videos, min_multiple)
        worst_case = COST_LIST + 2 * math.ceil(lookback_videos / 50) * COST_LIST
        return await _execute(
            cache_key,
            CHANNEL_CACHE_TTL_S,
            worst_case,
            lambda meter: _channel_pipeline(channel.strip(), lookback_videos, min_multiple, meter),
        )

    @mcp.tool(annotations=read_only_annotations)
    async def youtube_video_context(
        video: Annotated[str, Field(description="A YouTube video URL or the 11-character video ID.")],
    ) -> dict:
        """Explain a YouTube video's performance relative to its own channel:
        outlier multiple vs the channel's recent-median baseline, percentile among
        recent uploads, views-per-day velocity, and a classification from
        MEGA_OUTLIER to UNDERPERFORMER."""
        try:
            video_id = _parse_video_ref(video)
        except ToolFault as fault:
            return fault.to_response()
        return await _execute(
            ("video_context", video_id),
            CHANNEL_CACHE_TTL_S,
            VIDEO_CONTEXT_WORST_CASE_UNITS,
            lambda meter: _video_context_pipeline(video_id, meter),
        )

    @mcp.tool(annotations=read_only_annotations)
    async def youtube_rising_channels(
        niche: Annotated[str, Field(min_length=2, max_length=80, description="Niche keyword.")],
        region_code: str = "US",
        recency_days: Annotated[int, Field(ge=30, le=1825)] = 365,
        max_subs: Annotated[
            int,
            Field(ge=1_000, le=10_000_000, description="Only return channels at or below this subscriber count."),
        ] = 500_000,
    ) -> dict:
        """Find the fastest-rising channels in a YouTube niche: channels whose
        recent-median views far exceed their lifetime average. Useful for sponsor
        scouting, collab targeting, and competitor detection.

        Costs up to ~132 YouTube quota units per uncached call.
        """
        try:
            region = _validate_region(region_code)
        except ToolFault as fault:
            return fault.to_response()
        cache_key = ("rising_channels", niche.strip().lower(), region, recency_days, max_subs)
        return await _execute(
            cache_key,
            SCAN_CACHE_TTL_S,
            RADAR_WORST_CASE_UNITS,
            lambda meter: _radar_pipeline(niche.strip(), region, recency_days, max_subs, meter),
        )
