"""Stage 1: Enrichment - compute channel medians, enrich approx videos, classify formats."""
import logging
from datetime import datetime

from app import claude_client, youtube
from app.database import get_db
from app.config import load_config
from app.utils import median

logger = logging.getLogger(__name__)


def compute_channel_medians(niche_id: int, job_update=None):
    """Compute median views for all channels based on recent long-form uploads."""
    cfg = load_config()
    min_dur = cfg.get("longform_min_seconds", 180)
    window = cfg.get("median_window_videos", 30)

    db = get_db()
    try:
        channels = db.execute(
            "SELECT id, name, yt_channel_id FROM channels WHERE niche_id=?", (niche_id,)
        ).fetchall()

        for ch in channels:
            if job_update:
                job_update(f"Computing median for {ch['name']}...")

            rows = db.execute("""
                SELECT views FROM videos
                WHERE channel_id=? AND is_short=0 AND duration_sec >= ?
                ORDER BY published_at DESC LIMIT ?
            """, (ch["id"], min_dur, window)).fetchall()

            views_list = [r["views"] for r in rows if r["views"] is not None]
            if views_list:
                med = median(views_list)
                db.execute("UPDATE channels SET median_views=? WHERE id=?", (int(med), ch["id"]))

        db.commit()
    finally:
        db.close()


def classify_channel_formats(niche_id: int, job_update=None):
    """Classify format for channels that don't have one yet."""
    db = get_db()
    try:
        channels = db.execute(
            "SELECT id, name, yt_channel_id FROM channels WHERE niche_id=? AND format_class IS NULL",
            (niche_id,)
        ).fetchall()

        for ch in channels:
            if job_update:
                job_update(f"Classifying format: {ch['name']}...")
            try:
                titles = db.execute(
                    "SELECT title FROM videos WHERE channel_id=? ORDER BY published_at DESC LIMIT 15",
                    (ch["id"],)
                ).fetchall()
                title_list = [r["title"] for r in titles]
                result = claude_client.classify_format(ch["name"], "", title_list)
                fmt = result.get("format_class", "unknown")
                db.execute("UPDATE channels SET format_class=? WHERE id=?", (fmt, ch["id"]))
            except Exception as e:
                logger.warning("Format classify failed for %s: %s", ch["name"], e)

        db.commit()
    finally:
        db.close()


def enrich_approx_videos(niche_id: int, quota_tracker: dict, job_update=None):
    """Try to resolve approx-confidence videos via YouTube search."""
    db = get_db()
    try:
        approx_videos = db.execute("""
            SELECT v.id, v.title, c.name as channel_name
            FROM videos v
            LEFT JOIN channels c ON v.channel_id = c.id
            WHERE v.confidence='approx' AND v.niche_id=? AND v.yt_video_id IS NULL
            LIMIT 50
        """, (niche_id,)).fetchall()

        for v in approx_videos:
            if quota_tracker.get("used", 0) >= quota_tracker.get("max", 100):
                break

            query = v["title"]
            if v["channel_name"]:
                query += f" {v['channel_name']}"

            if job_update:
                job_update(f"Enriching: {v['title'][:50]}...")

            try:
                results = youtube.search_videos(query, max_results=5)
                quota_tracker["used"] = quota_tracker.get("used", 0) + 100

                for r in results:
                    from app.utils import fuzzy_match
                    if fuzzy_match(r["title"], v["title"]) >= 0.85:
                        db.execute("""
                            UPDATE videos SET yt_video_id=?, views=?, published_at=?,
                            duration_sec=?, confidence='exact', last_fetched=?
                            WHERE id=?
                        """, (r["yt_video_id"], r["views"], r["published_at"],
                              r["duration_sec"], datetime.utcnow().isoformat(), v["id"]))
                        break
            except Exception as e:
                logger.warning("Enrichment failed for video %d: %s", v["id"], e)

        db.commit()
    finally:
        db.close()
