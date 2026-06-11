"""Stage 0: Ingestion and normalization."""
import base64
import logging
import re
from datetime import datetime

from app import claude_client, youtube
from app.database import get_db
from app.utils import parse_view_string, parse_age_string, parse_duration_iso, fuzzy_match

logger = logging.getLogger(__name__)


def parse_youtube_url(url: str) -> tuple[str, str] | None:
    """Returns (type, identifier) where type is 'channel' or 'video'."""
    url = url.strip()
    # Video URL
    vm = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)", url)
    if vm:
        return ("video", vm.group(1))
    # Channel handle
    hm = re.search(r"youtube\.com/@([\w.-]+)", url)
    if hm:
        return ("channel", f"@{hm.group(1)}")
    # Channel URL formats
    cm = re.search(r"youtube\.com/(?:channel/|c/)([\w-]+)", url)
    if cm:
        return ("channel", cm.group(1))
    return None


def ingest_channel_url(url: str, niche_id: int, job_update=None) -> dict:
    """Pull full channel catalog and store in DB."""
    parsed = parse_youtube_url(url)
    if not parsed or parsed[0] != "channel":
        return {"error": f"Not a valid channel URL: {url}"}

    identifier = parsed[1]
    if job_update:
        job_update(f"Resolving channel: {identifier}")

    ch_data = youtube.resolve_channel(identifier)
    if not ch_data:
        return {"error": f"Could not resolve channel: {identifier}"}

    db = get_db()
    try:
        # Upsert channel
        db.execute("""
            INSERT INTO channels (yt_channel_id, handle, name, subs, video_count, niche_id, data_source, last_fetched)
            VALUES (?, ?, ?, ?, ?, ?, 'api', ?)
            ON CONFLICT(yt_channel_id) DO UPDATE SET
              name=excluded.name, subs=excluded.subs, video_count=excluded.video_count,
              last_fetched=excluded.last_fetched
        """, (ch_data["yt_channel_id"], ch_data.get("handle", ""), ch_data["name"],
              ch_data["subs"], ch_data["video_count"], niche_id, datetime.utcnow().isoformat()))
        db.commit()

        channel_row = db.execute("SELECT id FROM channels WHERE yt_channel_id=?",
                                  (ch_data["yt_channel_id"],)).fetchone()
        channel_db_id = channel_row["id"]

        if job_update:
            job_update(f"Fetching videos for {ch_data['name']}...")

        videos = youtube.get_channel_videos(ch_data["yt_channel_id"])

        inserted = 0
        for v in videos:
            try:
                db.execute("""
                    INSERT OR IGNORE INTO videos
                    (yt_video_id, channel_id, title, views, published_at, duration_sec,
                     is_short, data_source, confidence, niche_id, last_fetched)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (v["yt_video_id"], channel_db_id, v["title"], v["views"],
                      v["published_at"], v["duration_sec"], v["is_short"],
                      "api", "exact", niche_id, datetime.utcnow().isoformat()))
                inserted += 1
            except Exception:
                pass
        db.commit()

        return {
            "channel": ch_data,
            "channel_db_id": channel_db_id,
            "videos_fetched": len(videos),
            "videos_inserted": inserted,
        }
    finally:
        db.close()


def ingest_video_url(url: str, niche_id: int) -> dict:
    parsed = parse_youtube_url(url)
    if not parsed or parsed[0] != "video":
        return {"error": f"Not a valid video URL: {url}"}

    video_id = parsed[1]
    v = youtube.get_video_by_id(video_id)
    if not v:
        return {"error": f"Video not found: {video_id}"}

    # Also ingest its parent channel
    ch_data = youtube.resolve_channel(v["channel_id_yt"])
    db = get_db()
    try:
        if ch_data:
            db.execute("""
                INSERT INTO channels (yt_channel_id, handle, name, subs, niche_id, data_source, last_fetched)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(yt_channel_id) DO UPDATE SET name=excluded.name, subs=excluded.subs
            """, (ch_data["yt_channel_id"], ch_data.get("handle", ""), ch_data["name"],
                  ch_data["subs"], niche_id, "api", datetime.utcnow().isoformat()))
            db.commit()

        ch_row = db.execute("SELECT id FROM channels WHERE yt_channel_id=?",
                             (v["channel_id_yt"],)).fetchone()
        channel_db_id = ch_row["id"] if ch_row else None

        db.execute("""
            INSERT OR IGNORE INTO videos
            (yt_video_id, channel_id, title, views, published_at, duration_sec,
             is_short, data_source, confidence, niche_id, last_fetched)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (v["yt_video_id"], channel_db_id, v["title"], v["views"], v["published_at"],
              v["duration_sec"], v["is_short"], "api", "exact", niche_id, datetime.utcnow().isoformat()))
        db.commit()

        return {"video": v, "channel": ch_data}
    finally:
        db.close()


def ingest_screenshot(file_bytes: bytes, media_type: str, niche_id: int) -> dict:
    b64 = base64.b64encode(file_bytes).decode()
    result = claude_client.extract_screenshot(b64, media_type)

    videos_data = result.get("videos", [])
    page_channel = result.get("page_channel")

    db = get_db()
    channel_db_id = None

    try:
        if page_channel and page_channel.get("name"):
            subs = parse_view_string(page_channel.get("subs_string", "0")) or 0
            db.execute("""
                INSERT INTO channels (name, handle, subs, niche_id, data_source, last_fetched)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT DO NOTHING
            """, (page_channel["name"], page_channel.get("handle", ""),
                  subs, niche_id, "screenshot", datetime.utcnow().isoformat()))
            db.commit()
            ch_row = db.execute("SELECT id FROM channels WHERE name=?", (page_channel["name"],)).fetchone()
            if ch_row:
                channel_db_id = ch_row["id"]

        normalized = []
        for v in videos_data:
            title = v.get("title", "").strip()
            if not title:
                continue
            views = parse_view_string(v.get("view_string", ""))
            pub_at = parse_age_string(v.get("age_string", ""))
            dur = parse_duration_iso(v.get("duration_string", ""))
            normalized.append({
                "title": title,
                "views": views or 0,
                "published_at": pub_at or "",
                "duration_sec": dur,
                "is_short": 1 if dur and dur < 60 else 0,
                "channel_name": v.get("channel_name", ""),
            })

        inserted = 0
        for v in normalized:
            try:
                db.execute("""
                    INSERT OR IGNORE INTO videos
                    (channel_id, title, views, published_at, duration_sec, is_short,
                     data_source, confidence, niche_id, last_fetched)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (channel_db_id, v["title"], v["views"], v["published_at"],
                      v["duration_sec"], v["is_short"], "screenshot", "approx",
                      niche_id, datetime.utcnow().isoformat()))
                inserted += 1
            except Exception:
                pass
        db.commit()

        return {
            "page_channel": page_channel,
            "videos": normalized,
            "inserted": inserted,
        }
    finally:
        db.close()


def ingest_text(text: str, niche_id: int) -> dict:
    result = claude_client.parse_text(text)
    videos_data = result.get("videos", [])

    db = get_db()
    try:
        inserted = 0
        normalized = []
        for v in videos_data:
            title = v.get("title", "").strip()
            if not title:
                continue
            views = parse_view_string(v.get("view_string", ""))
            pub_at = parse_age_string(v.get("age_string", ""))
            normalized.append({
                "title": title,
                "views": views or 0,
                "published_at": pub_at or "",
                "channel_name": v.get("channel_name", ""),
            })
            try:
                db.execute("""
                    INSERT OR IGNORE INTO videos
                    (title, views, published_at, data_source, confidence, niche_id, last_fetched)
                    VALUES (?,?,?,?,?,?,?)
                """, (title, views or 0, pub_at or "", "text", "approx",
                      niche_id, datetime.utcnow().isoformat()))
                inserted += 1
            except Exception:
                pass
        db.commit()
        return {"videos": normalized, "inserted": inserted}
    finally:
        db.close()


def ingest_pdf(file_bytes: bytes, filename: str, niche_id: int) -> dict:
    import pdfplumber
    import io
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"

    classification = claude_client.classify_document(text)
    cls = classification.get("classification", "VIDEO_DATA")
    frameworks = classification.get("frameworks", [])

    db = get_db()
    try:
        fw_inserted = 0
        for fw in frameworks:
            db.execute("""
                INSERT INTO frameworks (name, pattern, mechanics, example, source_doc, niche_id)
                VALUES (?,?,?,?,?,?)
            """, (fw.get("name", ""), fw.get("pattern", ""), fw.get("mechanics", ""),
                  fw.get("example", ""), filename, None))
            fw_inserted += 1
        db.commit()

        video_result = {"videos": [], "inserted": 0}
        if cls in ("VIDEO_DATA", "BOTH"):
            video_result = ingest_text(text, niche_id)

        return {
            "classification": cls,
            "frameworks_extracted": fw_inserted,
            **video_result,
        }
    finally:
        db.close()
