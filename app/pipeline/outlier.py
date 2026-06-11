"""Stage 2: Outlier mining."""
import logging
from app.database import get_db
from app.config import load_config

logger = logging.getLogger(__name__)


def compute_outlier_scores(niche_id: int, job_update=None):
    """Compute outlier_score for all long-form videos with a known channel median."""
    cfg = load_config()
    min_dur = cfg.get("longform_min_seconds", 180)

    db = get_db()
    try:
        if job_update:
            job_update("Computing outlier scores...")

        rows = db.execute("""
            SELECT v.id, v.views, c.median_views, c.subs
            FROM videos v
            JOIN channels c ON v.channel_id = c.id
            WHERE v.niche_id=? AND v.is_short=0 AND v.duration_sec >= ?
        """, (niche_id, min_dur)).fetchall()

        for r in rows:
            if r["median_views"] and r["median_views"] > 0:
                score = r["views"] / r["median_views"]
            elif r["subs"] and r["subs"] > 0:
                score = r["views"] / (r["subs"] * 0.1)  # proxy: 10% of subs as expected views
            else:
                score = None

            if score is not None:
                db.execute("UPDATE videos SET outlier_score=? WHERE id=?", (round(score, 3), r["id"]))

        db.commit()

        total = db.execute("""
            SELECT COUNT(*) as n FROM videos
            WHERE niche_id=? AND outlier_score IS NOT NULL
        """, (niche_id,)).fetchone()["n"]

        threshold = cfg.get("outlier_threshold", 3.0)
        outliers = db.execute("""
            SELECT COUNT(*) as n FROM videos
            WHERE niche_id=? AND outlier_score >= ?
        """, (niche_id, threshold)).fetchone()["n"]

        logger.info("Niche %d: %d videos scored, %d outliers", niche_id, total, outliers)
        return {"total_scored": total, "outliers": outliers}
    finally:
        db.close()


def get_outliers(niche_id: int, limit: int = 100) -> list[dict]:
    cfg = load_config()
    threshold = cfg.get("outlier_threshold", 3.0)
    db = get_db()
    try:
        rows = db.execute("""
            SELECT v.id, v.title, v.views, v.outlier_score, v.published_at,
                   c.name as channel_name, c.format_class
            FROM videos v
            JOIN channels c ON v.channel_id = c.id
            WHERE v.niche_id=? AND v.outlier_score >= ?
            ORDER BY v.outlier_score DESC LIMIT ?
        """, (niche_id, threshold, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def get_average_videos(niche_id: int, limit: int = 100) -> list[dict]:
    cfg = load_config()
    threshold = cfg.get("outlier_threshold", 3.0)
    db = get_db()
    try:
        rows = db.execute("""
            SELECT v.title, v.views, v.outlier_score
            FROM videos v
            WHERE v.niche_id=? AND v.outlier_score IS NOT NULL AND v.outlier_score < ?
            ORDER BY RANDOM() LIMIT ?
        """, (niche_id, threshold, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()
