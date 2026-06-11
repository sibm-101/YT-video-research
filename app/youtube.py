"""YouTube Data API v3 wrapper with SQLite caching."""
import json
import hashlib
import logging
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_env, load_config
from app.database import get_db
from app.utils import parse_duration_iso

logger = logging.getLogger(__name__)


def _cache_key(method: str, params: dict) -> str:
    raw = json.dumps({"method": method, "params": params}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_cached(key: str, days: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT response_json, fetched_at FROM api_cache WHERE key=?", (key,)).fetchone()
    conn.close()
    if row:
        fetched = datetime.fromisoformat(row["fetched_at"])
        if datetime.utcnow() - fetched < timedelta(days=days):
            return json.loads(row["response_json"])
    return None


def _set_cache(key: str, data: dict):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO api_cache(key, response_json, fetched_at) VALUES(?,?,?)",
        (key, json.dumps(data), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def _build_yt():
    api_key = get_env("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YouTube API key not configured.")
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def resolve_channel(identifier: str) -> dict | None:
    """Resolve any channel identifier to channel metadata."""
    cfg = load_config()
    cache_days = cfg.get("cache_days", 7)

    key = _cache_key("resolve_channel", {"id": identifier})
    cached = _get_cached(key, cache_days)
    if cached:
        return cached

    yt = _build_yt()
    result = None

    # Try as channel ID directly
    if identifier.startswith("UC"):
        try:
            resp = yt.channels().list(part="snippet,statistics", id=identifier).execute()
            if resp.get("items"):
                result = _parse_channel(resp["items"][0])
        except HttpError:
            pass

    # Try as handle (@username)
    if not result:
        handle = identifier.lstrip("@")
        try:
            resp = yt.channels().list(part="snippet,statistics", forHandle=handle).execute()
            if resp.get("items"):
                result = _parse_channel(resp["items"][0])
        except HttpError:
            pass

    # Try as username
    if not result:
        try:
            resp = yt.channels().list(part="snippet,statistics", forUsername=identifier).execute()
            if resp.get("items"):
                result = _parse_channel(resp["items"][0])
        except HttpError:
            pass

    if result:
        _set_cache(key, result)
    return result


def _parse_channel(item: dict) -> dict:
    snip = item.get("snippet", {})
    stats = item.get("statistics", {})
    return {
        "yt_channel_id": item["id"],
        "name": snip.get("title", ""),
        "handle": snip.get("customUrl", ""),
        "description": snip.get("description", ""),
        "subs": int(stats.get("subscriberCount", 0) or 0),
        "video_count": int(stats.get("videoCount", 0) or 0),
    }


def get_channel_videos(channel_id: str, max_results: int = 500) -> list[dict]:
    """Fetch all videos from a channel's uploads playlist."""
    cfg = load_config()
    cache_days = cfg.get("cache_days", 7)

    key = _cache_key("channel_videos", {"channel_id": channel_id, "max": max_results})
    cached = _get_cached(key, cache_days)
    if cached:
        return cached

    yt = _build_yt()

    # Get uploads playlist ID
    ch_resp = yt.channels().list(part="contentDetails", id=channel_id).execute()
    if not ch_resp.get("items"):
        return []
    uploads_id = ch_resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # Paginate through playlist
    video_ids = []
    page_token = None
    while len(video_ids) < max_results:
        kwargs = dict(part="snippet", playlistId=uploads_id, maxResults=50)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = yt.playlistItems().list(**kwargs).execute()
        for item in resp.get("items", []):
            vid_id = item["snippet"]["resourceId"].get("videoId")
            if vid_id:
                video_ids.append(vid_id)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Batch fetch video details
    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        vresp = yt.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(batch)
        ).execute()
        for item in vresp.get("items", []):
            videos.append(_parse_video(item))

    _set_cache(key, videos)
    return videos


def _parse_video(item: dict) -> dict:
    snip = item.get("snippet", {})
    stats = item.get("statistics", {})
    cd = item.get("contentDetails", {})
    duration = parse_duration_iso(cd.get("duration", ""))
    title = snip.get("title", "")
    return {
        "yt_video_id": item["id"],
        "title": title,
        "views": int(stats.get("viewCount", 0) or 0),
        "published_at": snip.get("publishedAt", ""),
        "duration_sec": duration,
        "is_short": 1 if duration < 60 else 0,
        "channel_id_yt": snip.get("channelId", ""),
        "channel_name": snip.get("channelTitle", ""),
    }


def search_videos(query: str, max_results: int = 10) -> list[dict]:
    """Search YouTube and return enriched video data."""
    cfg = load_config()
    cache_days = cfg.get("cache_days", 7)

    key = _cache_key("search", {"q": query, "max": max_results})
    cached = _get_cached(key, cache_days)
    if cached:
        return cached

    yt = _build_yt()
    sresp = yt.search().list(
        part="snippet", q=query, type="video", maxResults=max_results, order="relevance"
    ).execute()

    video_ids = [i["id"]["videoId"] for i in sresp.get("items", []) if i["id"].get("videoId")]
    if not video_ids:
        return []

    vresp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(video_ids)).execute()
    videos = [_parse_video(i) for i in vresp.get("items", [])]

    _set_cache(key, videos)
    return videos


def get_video_by_id(video_id: str) -> dict | None:
    cfg = load_config()
    key = _cache_key("video", {"id": video_id})
    cached = _get_cached(key, cfg.get("cache_days", 7))
    if cached:
        return cached

    yt = _build_yt()
    resp = yt.videos().list(part="snippet,statistics,contentDetails", id=video_id).execute()
    if resp.get("items"):
        v = _parse_video(resp["items"][0])
        _set_cache(key, v)
        return v
    return None


def test_youtube_key() -> tuple[bool, str]:
    try:
        yt = _build_yt()
        yt.videos().list(part="snippet", id="dQw4w9WgXcQ").execute()
        return True, "Connected successfully."
    except HttpError as e:
        code = e.resp.status
        if code == 403:
            return False, "API key is invalid or YouTube Data API v3 is not enabled."
        return False, f"YouTube error {code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {str(e)}"
