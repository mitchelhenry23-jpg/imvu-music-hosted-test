"""Adaptive direct, relay, and MP3 provider for IMVU Music Next v0.6.3."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from difflib import SequenceMatcher
import json
import html
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


CONFIGURED_RESOLVER = os.environ.get("IMVU_YOUTUBE_RESOLVER", "").strip()
script_path = Path(__file__).resolve()
deep_output_resolver = (
    script_path.parents[2] / "outputs" / "youtube_resolver"
    if len(script_path.parents) > 2
    else None
)
RESOLVER_DIRS = tuple(path for path in (
    Path(CONFIGURED_RESOLVER) if CONFIGURED_RESOLVER else None,
    script_path.with_name("youtube_resolver"),
    deep_output_resolver,
    script_path.parent.parent / "youtube_resolver",
) if path is not None)
for resolver_dir in RESOLVER_DIRS:
    if resolver_dir.is_dir():
        sys.path.insert(0, str(resolver_dir))
        break
try:
    from yt_dlp import YoutubeDL
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:
    YoutubeDL = None
    get_ffmpeg_exe = None


HOST = os.environ.get(
    "IMVU_MUSIC_HOST",
    "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
)
PORT = int(
    os.environ.get(
        "PORT",
        os.environ.get("IMVU_MUSIC_PORT", "8765")
    )
)
USER_AGENT = "IMVU-Music-Next/0.6.3"
BASE_URL = (
    os.environ.get("IMVU_MUSIC_BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or f"http://{HOST}:{PORT}"
).rstrip("/")
SERVICE_VERSION = "0.6.3"
DELIVERY_TIERS = ("direct", "relay", "mp3")
FLASH_POLICY = b'''<?xml version="1.0"?>
<!DOCTYPE cross-domain-policy SYSTEM "http://www.adobe.com/xml/dtds/cross-domain-policy.dtd">
<cross-domain-policy>
  <site-control permitted-cross-domain-policies="master-only"/>
  <allow-access-from domain="*" secure="false"/>
</cross-domain-policy>'''
YOUTUBE_AUDIO_STRATEGIES = (
    {
        "name": "standard_m4a",
        "player_client": None,
        "format": "bestaudio[protocol^=https][ext=m4a]",
    },
    {
        "name": "standard_webm",
        "player_client": None,
        "format": "bestaudio[protocol^=https][ext=webm]",
    },
    {
        "name": "tv_m4a",
        "player_client": "tv",
        "format": "bestaudio[protocol^=https][ext=m4a]",
    },
    {
        "name": "tv_webm",
        "player_client": "tv",
        "format": "bestaudio[protocol^=https][ext=webm]",
    },
    {
        "name": "embedded_m4a",
        "player_client": "web_embedded",
        "format": "bestaudio[protocol^=https][ext=m4a]",
    },
    {
        "name": "embedded_webm",
        "player_client": "web_embedded",
        "format": "bestaudio[protocol^=https][ext=webm]",
    },
    {
        "name": "android_vr_hls",
        "player_client": "android_vr",
        "format": "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]",
    },
    {
        "name": "web_safari_hls",
        "player_client": "web_safari",
        "format": "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]",
    },
    {
        "name": "mweb_hls",
        "player_client": "mweb",
        "format": "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]",
    },
    {
        "name": "tv_simply_hls",
        "player_client": "tv_simply",
        "format": "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]",
    },
)
YOUTUBE_SEARCH_FALLBACKS = {}
YOUTUBE_STREAM_METADATA = {}
YOUTUBE_SEARCH_CACHE = {}
YOUTUBE_VIDEO_STRATEGY = {}
YOUTUBE_BAD_UNTIL = {}
MUSIC_IDENTITY_CACHE = {}
PREFERRED_AUDIO_STRATEGY = None
LAST_PLAYBACK = {}
YOUTUBE_DIRECT_CACHE = {}


class QuietResolverLogger:
    """Keep expected yt-dlp fallback misses out of the user-facing window."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


RESOLVER_LOGGER = QuietResolverLogger()


def youtube_api_key():
    configured = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if configured:
        return configured
    key_file = Path(__file__).resolve().with_name("youtube_api_key.txt")
    try:
        value = key_file.read_text(encoding="utf-8").strip()
        if value and not value.startswith("#"):
            return value
    except OSError:
        pass
    return ""


def fetch_json(url, timeout=20):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def clean_youtube_query(value):
    if not re.match(r"^https?://(?:(?:www|music|m)\.)?(?:youtube\.com/|youtu\.be/)", value, re.I):
        return value[:160]
    try:
        endpoint = "https://www.youtube.com/oembed?" + urlencode({
            "url": value,
            "format": "json",
        })
        title = str(fetch_json(endpoint).get("title") or value)
        title = re.sub(
            r"\s*[\[(][^\])]*(?:official|video|lyrics?|audio|4k|remaster)[^\])]*[\])]",
            "", title, flags=re.I,
        )
        title = re.sub(r"\s+(?:official\s+)?(?:music\s+)?video\s*$", "", title, flags=re.I)
        return re.sub(r"\s{2,}", " ", title).strip()[:160]
    except Exception:
        return value[:160]


def is_youtube_url(value):
    return bool(re.match(
        r"^https?://(?:(?:www|music|m)\.)?(?:youtube\.com/|youtu\.be/)",
        str(value), re.I,
    ))


def youtube_video_id(value):
    try:
        parsed = urlparse(value)
        host = parsed.netloc.lower().split(":", 1)[0]
        host = re.sub(r"^(?:www|music|m)\.", "", host)
        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/", 1)[0]
        elif host == "youtube.com":
            parts = parsed.path.strip("/").split("/")
            if parsed.path.rstrip("/") == "/watch":
                candidate = str((parse_qs(parsed.query).get("v") or [""])[0])
            elif parts and parts[0] in ("shorts", "embed", "live") and len(parts) > 1:
                candidate = parts[1]
            else:
                candidate = ""
        else:
            candidate = ""
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else ""
    except Exception:
        return ""


def youtube_reference(value):
    """Resolve watch, share, Music, playlist, and browse links to one video."""
    if not is_youtube_url(value):
        return None
    direct_id = youtube_video_id(value)
    if direct_id:
        canonical = "https://www.youtube.com/watch?v=" + direct_id
        title = clean_youtube_query(canonical)
        author = ""
        source_title = title
        try:
            body = fetch_json("https://www.youtube.com/oembed?" + urlencode({
                "url": canonical,
                "format": "json",
            }))
            source_title = str(body.get("title") or title).strip()
            author = str(body.get("author_name") or "").strip()
        except Exception:
            pass
        return {
            "id": direct_id,
            "title": title,
            "sourceTitle": source_title,
            "sourceChannel": author,
        }
    if not youtube_playback_available():
        return None
    options = {
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 25,
        "extract_flat": "in_playlist",
        "playlistend": 1,
    }
    options.update(resolver_runtime_options())
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(value, download=False)
    entry = info or {}
    entries = entry.get("entries") or []
    if entries:
        entry = next((item for item in entries if item), {})
    video_id = str(entry.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        video_id = youtube_video_id(str(entry.get("url") or ""))
    if not video_id:
        return None
    source_title = str(
        entry.get("title") or info.get("title") or "YouTube Music"
    )[:160]
    return {
        "id": video_id,
        "title": source_title,
        "sourceTitle": source_title,
        "sourceChannel": str(
            entry.get("channel") or entry.get("uploader")
            or info.get("channel") or info.get("uploader") or ""
        )[:160],
    }


def youtube_playback_available():
    return YoutubeDL is not None and get_ffmpeg_exe is not None


def resolver_runtime_options():
    options = {
        "remote_components": ["ejs:github"],
        "logger": RESOLVER_LOGGER,
    }
    configured = os.environ.get("YTDLP_NODE_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(sys.executable).resolve().parent.parent / "node" / "bin" / "node.exe",
    ]
    node_path = next((path for path in candidates if path and path.is_file()), None)
    if node_path:
        options["js_runtimes"] = {"node": {"path": str(node_path)}}
    return options


def resolve_youtube_audio(video_id, strategy):
    if not youtube_playback_available():
        raise RuntimeError("youtube_resolver_not_installed")
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 20,
        "format": strategy["format"],
    }
    player_client = strategy.get("player_client")
    if player_client:
        options["extractor_args"] = {
            "youtube": {"player_client": [player_client]},
        }
    options.update(resolver_runtime_options())
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            "https://www.youtube.com/watch?v=" + video_id,
            download=False,
        )
    stream_url = str((info or {}).get("url") or "")
    if not stream_url.startswith("https://"):
        raise RuntimeError("youtube_audio_not_found")
    headers = {
        str(key): str(value)
        for key, value in ((info or {}).get("http_headers") or {}).items()
        if key and value
    }
    return stream_url, headers, str((info or {}).get("protocol") or "unknown")


def direct_url_expiry(stream_url):
    value = str((parse_qs(urlparse(stream_url).query).get("expire") or ["0"])[0])
    return int(value) if value.isdigit() else 0


def resolve_youtube_format18(video_id):
    """Return yt-dlp's original CDN URL and request headers for format 18.

    This is the DIRECT tier: the resolved URL is handed to the Flash client,
    which calls NetStream.play() against it directly (Google CDN to client,
    no bytes touch this service). Validated live in IMVU Classic as v0.6.2
    Test C. Do NOT rewrite the hostname to redirector.googlevideo.com --
    that rewrite is what broke v0.6.1 Test A (NetStream.Play.StreamNotFound).
    """
    if YoutubeDL is None:
        raise RuntimeError("youtube_resolver_not_installed")
    cached = YOUTUBE_DIRECT_CACHE.get(video_id)
    if cached and float(cached.get("expires") or 0) - time.time() > 600:
        return dict(cached)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 25,
        "format": "18",
    }
    options.update(resolver_runtime_options())
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            "https://www.youtube.com/watch?v=" + video_id,
            download=False,
        )
    stream_url = str((info or {}).get("url") or "")
    host = str(urlparse(stream_url).hostname or "").lower()
    if not stream_url.startswith("https://") or not host.endswith("googlevideo.com"):
        raise RuntimeError("youtube_format18_not_found")
    headers = {
        str(key): str(value)
        for key, value in ((info or {}).get("http_headers") or {}).items()
        if key and value
    }
    entry = {
        "url": stream_url,
        "headers": headers,
        "expires": direct_url_expiry(stream_url) or int(time.time()) + 1800,
    }
    if len(YOUTUBE_DIRECT_CACHE) > 500:
        YOUTUBE_DIRECT_CACHE.clear()
    YOUTUBE_DIRECT_CACHE[video_id] = dict(entry)
    return entry


def record_bridge_error(video_id, detail):
    try:
        log_path = Path(__file__).resolve().with_name("bridge-errors.log")
        cleaned = re.sub(r"[\r\n]+", " | ", str(detail))[:4000]
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat()} youtube:{video_id} {cleaned}\n")
    except OSError:
        pass


def compact_media_error(detail):
    cleaned = re.sub(r"https?://\S+", "<media-url>", str(detail))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:260] or "no audio bytes returned"


def youtube_video_temporarily_bad(video_id):
    expires = float(YOUTUBE_BAD_UNTIL.get(video_id) or 0)
    if expires <= time.time():
        YOUTUBE_BAD_UNTIL.pop(video_id, None)
        return False
    return True


def search_cache_get(key):
    cached = YOUTUBE_SEARCH_CACHE.get(key)
    if not cached or time.time() - cached["saved"] > 900:
        YOUTUBE_SEARCH_CACHE.pop(key, None)
        return None
    filtered = [
        dict(item) for item in cached["results"]
        if not youtube_video_temporarily_bad(
            str(item.get("id") or "").removeprefix("youtube:")
        )
    ]
    if cached["results"] and not filtered:
        YOUTUBE_SEARCH_CACHE.pop(key, None)
        return None
    return filtered


def search_cache_put(key, results):
    if len(YOUTUBE_SEARCH_CACHE) > 200:
        YOUTUBE_SEARCH_CACHE.clear()
    YOUTUBE_SEARCH_CACHE[key] = {
        "saved": time.time(),
        "results": [dict(item) for item in results],
    }


def ordered_audio_strategies(candidate_ids=None):
    strategies = list(YOUTUBE_AUDIO_STRATEGIES)
    learned = next((
        YOUTUBE_VIDEO_STRATEGY.get(video_id)
        for video_id in (candidate_ids or [])
        if YOUTUBE_VIDEO_STRATEGY.get(video_id)
    ), None)
    preferred = learned or PREFERRED_AUDIO_STRATEGY
    if preferred:
        strategies.sort(
            key=lambda item: item["name"] != preferred
        )
    return strategies


def estimated_intro_skip(video_id):
    metadata = YOUTUBE_STREAM_METADATA.get(video_id) or {}
    duration = int(metadata.get("duration") or 0)
    expected = int(metadata.get("expected_duration") or 0)
    extra = duration - expected
    # Keep normal music-video intros and outros. Compensate only when the
    # excess is large enough to indicate a clearly cinematic lead-in.
    if expected >= 60 and 75 <= extra <= 240:
        return max(0, extra - 3)
    return 0


def youtube_result(video_id, title, artist="", score=1.0, match=None,
                   duration=0, expected_duration=0):
    video_url = "https://www.youtube.com/watch?v=" + video_id
    return {
        "id": "youtube:" + video_id,
        "provider": "youtube",
        "type": "track",
        "title": title,
        "artist": artist,
        # Tier 3 (final fallback): server-side MP3 conversion. Kept as "url"
        # for backward compatibility with any client still expecting a single
        # playable address.
        "url": BASE_URL + "/v1/stream/youtube/" + video_id + ".mp3",
        # Tier 1: ask this endpoint for a short-lived direct Google CDN URL;
        # the client NetStream.play()s that URL itself, bytes never touch
        # this service. Falls through to tier 2 on any NetStream error.
        "resolveUrl": BASE_URL + "/v1/resolve/youtube/" + video_id,
        # Tier 2: same format-18 bytes, unchanged, relayed through this
        # service over plain local HTTP. Use if tier 1's URL fails for this
        # listener (e.g. cross-network signed-URL edge cases).
        "relayUrl": BASE_URL + "/v1/relay/youtube/" + video_id,
        "tiers": list(DELIVERY_TIERS),
        "externalUrl": video_url,
        "playable": youtube_playback_available(),
        "finite": True,
        "preview": False,
        "match": match or match_label(score),
        "score": score,
        "duration": int(duration or 0),
        "expectedDuration": int(expected_duration or 0),
    }


def remember_youtube_fallbacks(results):
    if len(YOUTUBE_STREAM_METADATA) > 1000:
        YOUTUBE_STREAM_METADATA.clear()
    exact_groups = {}
    for item in results:
        if str(item.get("provider") or "").lower() != "youtube":
            continue
        if str(item.get("match") or "").lower() != "exact":
            continue
        video_id = str(item.get("id") or "").removeprefix("youtube:")
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            YOUTUBE_STREAM_METADATA[video_id] = {
                "duration": int(item.get("duration") or 0),
                "expected_duration": int(item.get("expectedDuration") or 0),
            }
            identity_key = normalized_words(
                f'{item.get("artist", "")} {item.get("title", "")}'
            )
            group = exact_groups.setdefault(identity_key, [])
            if video_id not in group:
                group.append(video_id)
    if not exact_groups:
        return
    if len(YOUTUBE_SEARCH_FALLBACKS) > 250:
        YOUTUBE_SEARCH_FALLBACKS.clear()
    for exact_ids in exact_groups.values():
        for video_id in exact_ids:
            YOUTUBE_SEARCH_FALLBACKS[video_id] = [
                candidate for candidate in exact_ids
                if candidate != video_id
            ][:3]


def normalized_words(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


SEARCH_NOISE_WORDS = frozenset((
    "official", "music", "video", "videos", "audio", "hd", "hq", "4k",
    "lyric", "lyrics", "visualizer", "visualiser", "remaster", "remastered",
    "version", "feat", "featuring", "ft", "a", "an",
))


def significant_tokens(value):
    tokens = normalized_words(value).split()
    return {
        token for token in tokens
        if token not in SEARCH_NOISE_WORDS
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def youtube_channel_tokens(value):
    channel = normalized_words(value)
    channel = re.sub(r"vevo\b", "", channel)
    return significant_tokens(channel)


def match_metrics(query, title, artist=""):
    wanted_tokens = significant_tokens(query)
    candidate_tokens = significant_tokens(f"{artist} {title}")
    if not wanted_tokens or not candidate_tokens:
        return 0.0, 0.0, 0.0
    exact = wanted_tokens & candidate_tokens
    unmatched_wanted = wanted_tokens - exact
    unmatched_candidates = candidate_tokens - exact
    fuzzy_quality = 0.0
    for wanted in sorted(unmatched_wanted, key=len, reverse=True):
        best = None
        best_ratio = 0.0
        for candidate in unmatched_candidates:
            ratio = SequenceMatcher(None, wanted, candidate).ratio()
            if ratio > best_ratio:
                best = candidate
                best_ratio = ratio
        if best is not None and best_ratio >= 0.82:
            fuzzy_quality += best_ratio
            unmatched_candidates.remove(best)
    matched_quality = len(exact) + fuzzy_quality
    coverage = matched_quality / max(1, len(wanted_tokens))
    precision = matched_quality / max(1, len(candidate_tokens))
    penalty = 0.0
    for marker in ("remix", "cover", "type beat", "sped up", "slowed", "instrumental", "game"):
        if marker in normalized_words(f"{artist} {title}") and marker not in normalized_words(query):
            penalty += 0.12
    score = (coverage * 0.78) + (precision * 0.22) - penalty
    return score, coverage, precision


def match_score(query, title, artist=""):
    return match_metrics(query, title, artist)[0]


def match_label(score):
    if score >= 0.90:
        return "exact"
    if score >= 0.64:
        return "close"
    return "alternate"


def unwanted_version(query, title):
    wanted = normalized_words(query)
    candidate = normalized_words(title)
    markers = (
        "cover", "remix", "mix", "edit", "mashup", "bootleg", "karaoke",
        "nightcore", "sped up", "slowed", "instrumental", "type beat",
        "rework", "game cover", "live", "acoustic", "piano", "pianoforte",
        "tribute", "re recorded", "rerecorded", "demo",
        "extended version", "alternate version",
        "reaction", "review", "tutorial", "how to play", "lesson",
        "interview", "behind the scenes", "making of", "fan made",
        "fanmade", "unofficial", "bass boosted", "8d audio", "8d version",
        "clean version", "censored", "vocals only", "isolated vocals",
        "drumless", "full album", "1 hour", "10 hours", "looped",
        "dance practice", "dance video", "shorts",
        "ai generated", "generated by ai", "suno ai", "udio ai",
        "ai cover", "ai song", "ai music",
    )
    return any(marker in candidate and marker not in wanted for marker in markers)


VERSION_MARKERS = (
    "live", "acoustic", "remix", "instrumental", "karaoke", "demo",
    "sped up", "slowed", "nightcore", "clean", "radio edit", "remaster",
)


def requested_version_words(query):
    normalized = normalized_words(query)
    return [marker for marker in VERSION_MARKERS if marker in normalized]


def identity_candidate(query, title, artist, duration, source, index):
    title = str(title or "").strip()
    artist = str(artist or "").strip()
    if not title or not artist or unwanted_version(query, title):
        return None
    full_score = match_score(query, title, artist)
    remembered_title_score = match_score(query, title) - 0.03
    return {
        "artist": artist,
        "title": title,
        "duration": int(duration or 0),
        "score": max(full_score, remembered_title_score) - (index * 0.004),
        "source": source,
    }


def deezer_identity_candidates(query):
    endpoint = "https://api.deezer.com/search?" + urlencode({
        "q": query,
        "limit": 8,
    })
    body = fetch_json(endpoint)
    candidates = []
    for index, track in enumerate((body.get("data") or [])[:8]):
        candidate = identity_candidate(
            query,
            track.get("title_short") or track.get("title"),
            (track.get("artist") or {}).get("name"),
            track.get("duration"), "deezer", index,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def apple_identity_candidates(query):
    endpoint = "https://itunes.apple.com/search?" + urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": 8,
    })
    body = fetch_json(endpoint)
    candidates = []
    for index, track in enumerate((body.get("results") or [])[:8]):
        if str(track.get("kind") or "song").lower() != "song":
            continue
        candidate = identity_candidate(
            query, track.get("trackName"), track.get("artistName"),
            round(float(track.get("trackTimeMillis") or 0) / 1000.0),
            "apple", index,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def same_music_identity(first, second):
    first_artist = normalized_words(first.get("artist"))
    second_artist = normalized_words(second.get("artist"))
    first_title = normalized_words(first.get("title"))
    second_title = normalized_words(second.get("title"))
    return (
        SequenceMatcher(None, first_artist, second_artist).ratio() >= 0.86
        and SequenceMatcher(None, first_title, second_title).ratio() >= 0.86
    )


def resolve_music_identity(query):
    cache_key = normalized_words(query)
    if not cache_key:
        return None
    if cache_key in MUSIC_IDENTITY_CACHE:
        return MUSIC_IDENTITY_CACHE[cache_key]
    candidates = []
    for provider in (deezer_identity_candidates, apple_identity_candidates):
        try:
            candidates.extend(provider(query))
        except Exception:
            pass
    for candidate in candidates:
        matches = [
            other for other in candidates
            if other is not candidate and same_music_identity(candidate, other)
        ]
        independent_sources = {
            item.get("source") for item in [candidate] + matches
        }
        candidate["score"] += 0.12 * (len(independent_sources) - 1)
        durations = sorted(
            int(item.get("duration") or 0)
            for item in [candidate] + matches
            if int(item.get("duration") or 0) > 0
        )
        if durations:
            candidate["duration"] = durations[len(durations) // 2]
        candidate["sources"] = sorted(independent_sources)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    distinct = []
    version_words = requested_version_words(query)
    for candidate in candidates:
        if candidate["score"] < 0.55:
            continue
        if any(same_music_identity(candidate, existing) for existing in distinct):
            continue
        hypothesis = dict(candidate)
        canonical = f'{hypothesis["artist"]} {hypothesis["title"]}'
        if version_words:
            canonical += " " + " ".join(version_words)
        hypothesis["query"] = canonical
        distinct.append(hypothesis)
        if len(distinct) >= 5:
            break
    identity = dict(distinct[0]) if distinct and distinct[0]["score"] >= 0.68 else None
    if identity:
        identity["alternatives"] = [dict(item) for item in distinct]
        identity["ambiguous"] = (
            len(distinct) > 1
            and identity["score"] - distinct[1]["score"] < 0.30
        )
    if len(MUSIC_IDENTITY_CACHE) > 250:
        MUSIC_IDENTITY_CACHE.clear()
    MUSIC_IDENTITY_CACHE[cache_key] = identity
    return identity


def iso_duration_seconds(value):
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        str(value or ""), re.I,
    )
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return (days * 86400) + (hours * 3600) + (minutes * 60) + seconds


def youtube_video_details(video_ids, key):
    if not video_ids:
        return {}
    endpoint = "https://www.googleapis.com/youtube/v3/videos?" + urlencode({
        "part": "contentDetails,status,statistics",
        "id": ",".join(video_ids[:50]),
        "key": key,
    })
    body = fetch_json(endpoint)
    details = {}
    for item in body.get("items") or []:
        video_id = str(item.get("id") or "")
        if not video_id:
            continue
        details[video_id] = {
            "duration": iso_duration_seconds(
                (item.get("contentDetails") or {}).get("duration")
            ),
            "licensed": (item.get("contentDetails") or {}).get("licensedContent") is True,
            "views": int((item.get("statistics") or {}).get("viewCount") or 0),
            "embeddable": (item.get("status") or {}).get("embeddable") is not False,
        }
    return details


def search_deezer(query):
    endpoint = "https://api.deezer.com/search?" + urlencode({
        "q": query,
        "limit": 6,
    })
    body = fetch_json(endpoint)
    output = []
    for track in (body.get("data") or [])[:6]:
        track_id = str(track.get("id") or "")
        preview = str(track.get("preview") or "")
        if not track_id or not preview:
            continue
        title = str(track.get("title") or "Deezer track")
        if unwanted_version(query, title):
            continue
        artist = str((track.get("artist") or {}).get("name") or "")
        score = match_score(query, title, artist)
        output.append({
            "id": "deezer:" + track_id,
            "provider": "deezer",
            "type": "track",
            "title": title + " â€¢ 30s preview",
            "artist": artist,
            "url": BASE_URL + "/v1/stream/deezer/" + quote(track_id, safe=""),
            "externalUrl": str(track.get("link") or ""),
            "playable": True,
            "finite": True,
            "preview": True,
            "duration": 30,
            "match": match_label(score),
            "score": score,
        })
    output.sort(key=lambda item: item["score"], reverse=True)
    return output


def youtube_candidate_score(query, title, artist, duration=0,
                            expected_duration=0, licensed=False,
                            view_count=0):
    if unwanted_version(query, title):
        return None
    score, coverage, precision = match_metrics(query, title, artist)
    channel = normalized_words(artist)
    title_words = normalized_words(title)
    wanted_tokens = significant_tokens(query)
    title_tokens = significant_tokens(title)
    channel_tokens = youtube_channel_tokens(artist)
    channel_matches_query = bool(wanted_tokens & channel_tokens)
    verified_channel = (
        "vevo" in channel or channel.endswith(" topic") or "official" in channel
    )
    if channel_matches_query:
        score += 0.22
    elif verified_channel:
        score += 0.14
    elif (wanted_tokens and wanted_tokens.issubset(title_tokens)
          and "official" not in title_words):
        score -= 0.12
    if "official" in title_words:
        score += 0.08
    if "lyrics" in title_words or "lyric" in title_words:
        score -= 0.32
    if ("remaster" in title_words or "remastered" in title_words) and "remaster" not in normalized_words(query):
        score -= 0.05
    if "visualizer" in title_words or "visualiser" in title_words:
        score -= 0.03
    if licensed:
        score += 0.10
    if view_count:
        popularity = (math.log10(max(1, view_count)) - 3.0) * 0.05
        score += max(-0.08, min(0.18, popularity))
    if duration and expected_duration:
        difference = abs(duration - expected_duration)
        if difference <= 5:
            score += 0.16
        elif difference <= 15:
            score += 0.12
        elif difference <= 30:
            score += 0.06
        elif difference > 180:
            return None
        elif difference > 90:
            score -= 0.24
        elif difference > 45:
            score -= 0.10
    return score


def youtube_candidate_label(query, title, artist, score):
    _, coverage, _ = match_metrics(query, title, artist)
    wanted_tokens = significant_tokens(query)
    title_tokens = significant_tokens(title)
    remembered_phrase = (
        len(title_tokens) >= 2
        and title_tokens.issubset(wanted_tokens)
        and len(wanted_tokens - title_tokens) <= 2
    )
    if score >= 0.90 and (coverage >= 0.84 or remembered_phrase):
        return "exact"
    if score >= 0.64 and coverage >= 0.58:
        return "close"
    return "alternate"


def youtube_identity_hypotheses(identity):
    if not identity:
        return []
    hypotheses = identity.get("alternatives") or [identity]
    return [item for item in hypotheses if item and item.get("query")]


def artist_named_in_query(query, hypothesis):
    """True only when the user's own words identify this hypothesis' artist."""
    artist = str((hypothesis or {}).get("artist") or "").strip()
    if not artist:
        return False
    wanted_artist = normalized_words(artist)
    raw_query = normalized_words(query)
    if wanted_artist and wanted_artist in raw_query:
        return True
    _, coverage, _ = match_metrics(artist, query)
    return coverage >= 0.92


def ranked_youtube_result(video_id, raw_query, identity, source_title,
                          source_channel, duration=0, licensed=False,
                          view_count=0):
    hypotheses = youtube_identity_hypotheses(identity)
    explicitly_named = [
        item for item in hypotheses
        if artist_named_in_query(raw_query, item)
    ]
    if explicitly_named:
        hypotheses = explicitly_named
    possibilities = [(raw_query, 0, None)]
    possibilities.extend(
        (str(item.get("query")), int(item.get("duration") or 0), item)
        for item in hypotheses
    )
    ranked = []
    for target_query, expected_duration, hypothesis in possibilities:
        score = youtube_candidate_score(
            target_query, source_title, source_channel, duration,
            expected_duration, licensed, view_count,
        )
        if score is None:
            continue
        ranked.append((
            score,
            youtube_candidate_label(
                target_query, source_title, source_channel, score,
            ),
            hypothesis,
            expected_duration,
        ))
    if not ranked:
        return None
    score, label, hypothesis, expected_duration = max(
        ranked, key=lambda item: item[0]
    )
    result = youtube_result(
        video_id, source_title, source_channel, score, label,
        duration, expected_duration,
    )
    result["sourceTitle"] = source_title
    result["sourceChannel"] = source_channel
    result["licensed"] = bool(licensed)
    result["views"] = int(view_count or 0)
    if hypothesis:
        result["title"] = str(hypothesis.get("title") or source_title)
        result["artist"] = str(hypothesis.get("artist") or source_channel)
    return result


def search_youtube_resolver(query, identity=None):
    if not youtube_playback_available():
        return []
    lookup_query = query
    options = {
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 25,
        "extract_flat": "in_playlist",
        "playlistend": 20,
    }
    options.update(resolver_runtime_options())
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info("ytsearch20:" + lookup_query + " official", download=False)
    output = []
    for item in (info.get("entries") or [])[:20]:
        if not item:
            continue
        video_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            continue
        if youtube_video_temporarily_bad(video_id):
            continue
        title = str(item.get("title") or "YouTube video")
        artist = str(item.get("channel") or item.get("uploader") or "")
        duration = int(item.get("duration") or 0)
        result = ranked_youtube_result(
            video_id, query, identity, title, artist, duration,
            False, int(item.get("view_count") or 0),
        )
        if result:
            output.append(result)
    output.sort(key=lambda result: result["score"], reverse=True)
    return output[:8]


def search_youtube(query, identity=None):
    lookup_query = query
    cache_key = normalized_words(lookup_query)
    cached = search_cache_get(cache_key)
    if cached is not None:
        return cached
    key = youtube_api_key()
    if not key:
        output = search_youtube_resolver(query, identity)
        search_cache_put(cache_key, output)
        return output
    endpoint = "https://www.googleapis.com/youtube/v3/search?" + urlencode({
        "part": "snippet",
        "type": "video",
        "videoCategoryId": "10",
        "maxResults": 20,
        "order": "relevance",
        "q": lookup_query + " official",
        "key": key,
    })
    try:
        body = fetch_json(endpoint)
    except Exception:
        output = search_youtube_resolver(query, identity)
        search_cache_put(cache_key, output)
        return output
    video_ids = [
        str((item.get("id") or {}).get("videoId") or "")
        for item in (body.get("items") or [])[:20]
    ]
    try:
        details = youtube_video_details(
            [video_id for video_id in video_ids if video_id], key,
        )
    except Exception:
        details = {}
    output = []
    for item in (body.get("items") or [])[:20]:
        video_id = str((item.get("id") or {}).get("videoId") or "")
        if not video_id:
            continue
        if youtube_video_temporarily_bad(video_id):
            continue
        snippet = item.get("snippet") or {}
        title = html.unescape(str(snippet.get("title") or "YouTube video"))
        artist = html.unescape(str(snippet.get("channelTitle") or ""))
        live_state = str(snippet.get("liveBroadcastContent") or "none").lower()
        if live_state != "none" and "live" not in normalized_words(lookup_query):
            continue
        detail = details.get(video_id) or {}
        result = ranked_youtube_result(
            video_id, query, identity, title, artist,
            int(detail.get("duration") or 0),
            bool(detail.get("licensed")), int(detail.get("views") or 0),
        )
        if result:
            output.append(result)
    output.sort(key=lambda result: result["score"], reverse=True)
    search_cache_put(cache_key, output)
    return output


def search_audius(query):
    endpoint = "https://api.audius.co/v1/tracks/search?" + urlencode({
        "query": query,
        "app_name": "imvu-music-next",
    })
    body = fetch_json(endpoint)
    output = []
    for track in (body.get("data") or [])[:6]:
        track_id = str(track.get("id") or "")
        if not track_id:
            continue
        permalink = str(track.get("permalink") or "")
        title = str(track.get("title") or "Audius track")
        artist = str((track.get("user") or {}).get("name") or "")
        if unwanted_version(query, title):
            continue
        score = match_score(query, title, artist)
        output.append({
            "id": "audius:" + track_id,
            "provider": "audius",
            "type": "track",
            "title": title,
            "artist": artist,
            "url": BASE_URL + "/v1/stream/audius/" + quote(track_id, safe=""),
            "externalUrl": "https://audius.co" + permalink if permalink else "",
            "playable": True,
            "finite": True,
            "duration": track.get("duration") or 0,
            "preview": False,
            "match": match_label(score),
            "score": score,
        })
    output.sort(key=lambda item: item["score"], reverse=True)
    return output


def search_radio(query):
    endpoint = "https://de1.api.radio-browser.info/json/stations/search?" + urlencode({
        "name": query,
        "limit": 6,
        "hidebroken": "true",
        "order": "clickcount",
        "reverse": "true",
    })
    rows = fetch_json(endpoint)
    output = []
    for row in rows:
        stream_url = row.get("url_resolved") or row.get("url")
        if str(row.get("codec") or "").lower() != "mp3" or not stream_url:
            continue
        output.append({
            "id": "radio:" + str(row.get("stationuuid") or ""),
            "provider": "radio",
            "type": "radio",
            "title": str(row.get("name") or "Radio station"),
            "artist": " â€¢ ".join(filter(None, [row.get("country"), row.get("language")])),
            "url": str(stream_url),
            "externalUrl": str(row.get("homepage") or ""),
            "playable": True,
            "finite": False,
        })
    return output


def logical_youtube_results(results, identity=None):
    """Present songs to Flash, while keeping duplicate uploads backstage."""
    if not results:
        return []
    logical = []
    seen = set()
    for item in results:
        key = normalized_words(f'{item.get("artist", "")} {item.get("title", "")}')
        if not key or key in seen:
            continue
        logical.append(item)
        seen.add(key)
        if len(logical) >= 8:
            break
    return logical


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def common_headers(self, content_type, content_length=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Connection", "close")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.common_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers("application/json; charset=utf-8", len(body))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.common_headers("text/plain", 0)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/crossdomain.xml":
            self.send_bytes(200, "text/x-cross-domain-policy", FLASH_POLICY)
            return
        if parsed.path == "/health":
            self.send_json({
                "ok": True,
                "service": "imvu-music-next-local",
                "version": SERVICE_VERSION,
                "search": "apple-deezer-youtube-consensus",
                "streamMethods": len(YOUTUBE_AUDIO_STRATEGIES),
                "deliveryTiers": list(DELIVERY_TIERS),
                "lastPlayback": dict(LAST_PLAYBACK),
            })
            return
        if parsed.path == "/v1/search":
            self.handle_search(parsed)
            return
        match = re.fullmatch(r"/v1/stream/audius/([^/]+)", parsed.path)
        if match:
            self.proxy_audius(match.group(1))
            return
        match = re.fullmatch(r"/v1/stream/deezer/(\d+)", parsed.path)
        if match:
            self.proxy_deezer(match.group(1))
            return
        match = re.fullmatch(r"/v1/resolve/youtube/([A-Za-z0-9_-]{11})", parsed.path)
        if match:
            self.handle_resolve_youtube(match.group(1))
            return
        match = re.fullmatch(r"/v1/relay/youtube/([A-Za-z0-9_-]{11})", parsed.path)
        if match:
            self.handle_relay_youtube(match.group(1))
            return
        match = re.fullmatch(r"/v1/stream/youtube/([A-Za-z0-9_-]{11})\.mp3", parsed.path)
        if match:
            self.proxy_youtube(match.group(1))
            return
        self.send_json({"error": "not_found"}, 404)

    def handle_search(self, parsed):
        params = parse_qs(parsed.query)
        original = str((params.get("q") or [""])[0]).strip()[:300]
        mode = str((params.get("mode") or ["all"])[0]).lower()
        if not original:
            self.send_json({"error": "missing_query", "results": []}, 400)
            return
        batches = []
        failures = []
        reference = None
        if mode != "radio" and is_youtube_url(original):
            try:
                reference = youtube_reference(original)
            except Exception as error:
                failures.append("youtube_link:" + type(error).__name__)
        query = str(reference.get("title")) if reference else clean_youtube_query(original)
        identity = None
        if mode != "radio":
            try:
                identity = resolve_music_identity(query)
            except Exception as error:
                failures.append("music_identity:" + type(error).__name__)
        resolved_query = str((identity or {}).get("query") or query)
        reference_result = None
        if mode != "radio" and reference:
            reference_result = ranked_youtube_result(
                str(reference.get("id")), query, identity,
                str(reference.get("sourceTitle") or query),
                str(reference.get("sourceChannel") or "YouTube Music"),
            )
            # A pasted link is an intentional request, so keep it playable even
            # when its metadata is unclear.  Do not assign a guessed canonical
            # identity in that case; this isolates it from unrelated fallbacks.
            if (not reference_result
                    or str(reference_result.get("match") or "") != "exact"):
                reference_result = youtube_result(
                    str(reference.get("id")),
                    str(reference.get("sourceTitle") or query),
                    str(reference.get("sourceChannel") or "YouTube Music"),
                    1.0, "exact",
                )
        try:
            if mode != "radio":
                youtube_matches = search_youtube(query, identity)
                if reference:
                    reference_id = "youtube:" + str(reference.get("id"))
                    youtube_matches = [
                        item for item in youtube_matches
                        if str(item.get("id") or "") != reference_id
                    ]
                raw_youtube_matches = (
                    ([reference_result] if reference_result else [])
                    + youtube_matches
                )
                remember_youtube_fallbacks(raw_youtube_matches)
                if reference_result:
                    batches.append([reference_result])
                elif youtube_matches:
                    batches.append(logical_youtube_results(
                        youtube_matches, identity,
                    ))
        except Exception as error:
            failures.append("youtube:" + type(error).__name__)
            if reference_result:
                batches.append([reference_result])
        if mode == "all":
            try:
                batches.append(search_deezer(query))
            except Exception as error:
                failures.append("deezer:" + type(error).__name__)
            try:
                batches.append(search_audius(query))
            except Exception as error:
                failures.append("audius:" + type(error).__name__)
        if mode == "radio" or mode == "all":
            try:
                batches.append(search_radio(query))
            except Exception as error:
                failures.append("radio:" + type(error).__name__)
        results = []
        for index in range(6):
            for batch in batches:
                if index < len(batch):
                    results.append(batch[index])
                if len(results) >= 12:
                    break
        self.send_json({
            "query": original,
            "resolvedQuery": resolved_query,
            "identity": {
                "artist": str((identity or {}).get("artist") or ""),
                "title": str((identity or {}).get("title") or ""),
                "duration": int((identity or {}).get("duration") or 0),
                "ambiguous": bool((identity or {}).get("ambiguous")),
                "alternatives": [
                    {
                        "artist": str(item.get("artist") or ""),
                        "title": str(item.get("title") or ""),
                        "duration": int(item.get("duration") or 0),
                        "score": round(float(item.get("score") or 0), 3),
                    }
                    for item in ((identity or {}).get("alternatives") or [])
                ],
            },
            "mode": mode,
            "results": results[:12],
            "failures": failures,
        })

    def handle_resolve_youtube(self, video_id):
        """DIRECT tier. Returns a short-lived Google CDN URL for the client
        to NetStream.play() itself -- bytes never pass through this service.
        The client is expected to fall back to relayUrl, then to the .mp3
        url, on any NetStream.onStatus error for this address."""
        try:
            entry = resolve_youtube_format18(video_id)
        except Exception as error:
            self.send_json({
                "ok": False,
                "tier": "direct",
                "video": video_id,
                "error": type(error).__name__,
                "relayUrl": BASE_URL + "/v1/relay/youtube/" + video_id,
                "mp3Url": BASE_URL + "/v1/stream/youtube/" + video_id + ".mp3",
            }, 502)
            return
        self.send_json({
            "ok": True,
            "tier": "direct",
            "video": video_id,
            "url": "http://127.0.0.1:1/forced-direct-failure",
            "expiresAt": entry["expires"],
            "relayUrl": BASE_URL + "/v1/relay/youtube/" + video_id,
            "mp3Url": BASE_URL + "/v1/stream/youtube/" + video_id + ".mp3",
        })

    def handle_relay_youtube(self, video_id):
        """RELAY tier (fallback #1). Same format-18 bytes as the direct
        tier, unchanged (no transcode/remux), served over plain local HTTP
        so a listener whose network rejects the signed Google URL directly
        can still play the identical media through this service instead.
        Forwards Range so Flash's normal buffering/seek-probing behavior
        still works against it."""
        try:
            entry = resolve_youtube_format18(video_id)
        except Exception as error:
            self.send_json({
                "error": "relay_resolve_failed",
                "detail": type(error).__name__,
            }, 502)
            return
        headers = dict(entry.get("headers") or {})
        headers.setdefault("User-Agent", USER_AGENT)
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        request = Request(entry["url"], headers=headers)
        try:
            upstream = urlopen(request, timeout=30)
        except HTTPError as error:
            self.send_response(error.code)
            self.common_headers("text/plain", 0)
            self.send_header("X-IMVU-Music-Tier", "relay")
            self.end_headers()
            return
        except Exception as error:
            self.send_json({
                "error": "relay_fetch_failed",
                "detail": type(error).__name__,
            }, 502)
            return
        status = getattr(upstream, "status", 200) or 200
        try:
            self.send_response(status)
            self.common_headers(
                upstream.headers.get("Content-Type") or "video/mp4",
                upstream.headers.get("Content-Length"),
            )
            for header_name in ("Content-Range", "Accept-Ranges"):
                value = upstream.headers.get(header_name)
                if value:
                    self.send_header(header_name, value)
            self.send_header("X-IMVU-Music-Tier", "relay")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                block = upstream.read(64 * 1024)
                if not block:
                    break
                self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def proxy_audius(self, track_id):
        endpoint = "https://api.audius.co/v1/tracks/" + quote(track_id, safe="") + "/stream?" + urlencode({
            "app_name": "imvu-music-next",
        })
        self.proxy_audio(endpoint)

    def proxy_deezer(self, track_id):
        try:
            track = fetch_json("https://api.deezer.com/track/" + quote(track_id, safe=""))
            preview = str(track.get("preview") or "")
            if not preview:
                self.send_json({"error": "preview_not_found"}, 404)
                return
            self.proxy_audio(preview)
        except Exception as error:
            self.send_json({"error": "preview_lookup_failed", "detail": type(error).__name__}, 502)

    def proxy_youtube(self, video_id):
        """MP3 tier (final fallback). Unchanged from v0.6.0 other than the
        added X-IMVU-Music-Tier diagnostic header."""
        global PREFERRED_AUDIO_STRATEGY
        failures = []
        candidate_ids = [video_id]
        for candidate_id in YOUTUBE_SEARCH_FALLBACKS.get(video_id, []):
            if candidate_id not in candidate_ids:
                candidate_ids.append(candidate_id)
        for strategy in ordered_audio_strategies(candidate_ids):
            for candidate_id in candidate_ids:
                process = None
                strategy_name = strategy["name"]
                try:
                    stream_url, source_headers, protocol = resolve_youtube_audio(candidate_id, strategy)
                    user_agent = source_headers.get("User-Agent") or USER_AGENT
                    referer = source_headers.get("Referer") or "https://www.youtube.com/"
                    header_lines = []
                    for key, value in source_headers.items():
                        if key.lower() in ("accept-encoding", "connection", "content-length", "host", "user-agent", "referer"):
                            continue
                        safe_key = re.sub(r"[^A-Za-z0-9-]", "", key)
                        safe_value = re.sub(r"[\r\n]+", " ", value)
                        if safe_key and safe_value:
                            header_lines.append(f"{safe_key}: {safe_value}")
                    ffmpeg_args = [
                        get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-rw_timeout", "15000000", "-reconnect", "1",
                        "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                        "-user_agent", user_agent, "-referer", referer,
                    ]
                    if header_lines:
                        ffmpeg_args.extend(["-headers", "\r\n".join(header_lines) + "\r\n"])
                    intro_skip = estimated_intro_skip(candidate_id)
                    if intro_skip:
                        ffmpeg_args.extend(["-ss", str(intro_skip)])
                    ffmpeg_args.extend([
                        "-i", stream_url, "-vn", "-acodec", "libmp3lame",
                        "-b:a", "160k", "-f", "mp3", "pipe:1",
                    ])
                    process = subprocess.Popen(
                        ffmpeg_args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    first_block = process.stdout.read(32 * 1024)
                    if not first_block:
                        detail = process.stderr.read(64 * 1024).decode("utf-8", "replace")
                        failures.append(
                            f"{candidate_id}/{strategy_name} ({protocol}): "
                            + compact_media_error(detail)
                        )
                        continue

                    PREFERRED_AUDIO_STRATEGY = strategy_name
                    YOUTUBE_VIDEO_STRATEGY[candidate_id] = strategy_name
                    YOUTUBE_BAD_UNTIL.pop(candidate_id, None)
                    LAST_PLAYBACK.clear()
                    LAST_PLAYBACK.update({
                        "ok": True,
                        "tier": "mp3",
                        "requestedVideo": video_id,
                        "playedVideo": candidate_id,
                        "strategy": strategy_name,
                        "introSkip": intro_skip,
                        "at": datetime.now().isoformat(timespec="seconds"),
                    })
                    self.send_response(200)
                    self.common_headers("audio/mpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-IMVU-Music-Tier", "mp3")
                    self.send_header("X-IMVU-Music-Strategy", strategy_name)
                    self.send_header("X-IMVU-Music-Video", candidate_id)
                    self.send_header("X-IMVU-Music-Intro-Skip", str(intro_skip))
                    self.end_headers()
                    self.wfile.write(first_block)
                    while True:
                        block = process.stdout.read(64 * 1024)
                        if not block:
                            break
                        self.wfile.write(block)
                    return
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as error:
                    failures.append(
                        f"{candidate_id}/{strategy_name}: {type(error).__name__}: "
                        + compact_media_error(error)
                    )
                finally:
                    if process and process.poll() is None:
                        process.terminate()
                    if process:
                        try:
                            process.wait(timeout=3)
                        except Exception:
                            process.kill()

        for candidate_id in candidate_ids:
            YOUTUBE_BAD_UNTIL[candidate_id] = time.time() + 900
        LAST_PLAYBACK.clear()
        LAST_PLAYBACK.update({
            "ok": False,
            "tier": "mp3",
            "requestedVideo": video_id,
            "attemptedUploads": len(candidate_ids),
            "attemptedMethods": len(YOUTUBE_AUDIO_STRATEGIES),
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        record_bridge_error(video_id, " || ".join(failures) or "all YouTube strategies failed")
        if not self.wfile.closed:
            try:
                self.send_json({
                    "error": "youtube_stream_failed",
                    "attempts": len(YOUTUBE_AUDIO_STRATEGIES) * len(candidate_ids),
                }, 502)
            except Exception:
                pass

    def proxy_audio(self, endpoint):
        try:
            request = Request(endpoint, headers={"User-Agent": USER_AGENT})
            upstream = urlopen(request, timeout=30)
            self.send_response(200)
            self.common_headers(upstream.headers.get("Content-Type") or "audio/mpeg",
                                upstream.headers.get("Content-Length"))
            self.end_headers()
            while True:
                block = upstream.read(64 * 1024)
                if not block:
                    break
                self.wfile.write(block)
            upstream.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except HTTPError as error:
            self.send_json({"error": "audio_upstream_failed", "status": error.code}, 502)
        except Exception as error:
            self.send_json({"error": "audio_proxy_failed", "detail": type(error).__name__}, 502)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()



