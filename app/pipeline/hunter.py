"""Channel Hunter pipeline — 5 stages run as a background job."""
import json
import logging
from datetime import datetime, timedelta, timezone

from app import claude_client, youtube
from app.config import load_config
from app.database import get_db
from app.utils import parse_duration_iso

logger = logging.getLogger(__name__)


def estimate_quota(n_seeds: int) -> int:
    """Rough quota estimate: 100 per search + ~1 per channel batch + video fetches."""
    return n_seeds * 100 + n_seeds * 10


def run_hunt(job_id: int, hunt_id: int, seed_keywords: list[str], region: str,
             max_channel_age_days: int, min_breakout_views: int,
             include_shorts: bool, quota_tracker: dict):
    from app.pipeline.runner import update_job
    cfg = load_config()
    min_dur = cfg.get("longform_min_seconds", 180)
    db = get_db()

    def upd(msg, pct=None):
        update_job(job_id, message=msg, progress=pct)

    def check_cancelled() -> bool:
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row and row["status"] == "cancelled"

    try:
        # Compute the publishedAfter date (RFC 3339)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_channel_age_days)
        published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        total_seeds = len(seed_keywords)
        all_channel_ids: set[str] = set()

        # ── Stage 1: Seed search sweep ─────────────────────────────────────
        update_job(job_id, stage="Stage 1: Seed Search", progress=2)
        for idx, keyword in enumerate(seed_keywords):
            if check_cancelled():
                update_job(job_id, status="cancelled", message="Hunt cancelled.")
                db.close()
                return

            pct = 2 + int((idx / total_seeds) * 28)
            upd(f"Searching seed {idx+1} of {total_seeds}: '{keyword}'. "
                f"{len(all_channel_ids)} young channels found so far.", pct)

            channel_ids = youtube.search_recent_videos(
                query=keyword,
                published_after=published_after,
                region=region,
                max_results=50,
            )
            quota_tracker["used"] = quota_tracker.get("used", 0) + 100
            all_channel_ids.update(channel_ids)

        # ── Stage 2: Channel age filter ────────────────────────────────────
        update_job(job_id, stage="Stage 2: Age Filter", progress=30)
        upd(f"Checking ages for {len(all_channel_ids)} unique channels...", 30)

        # Split into: already cached vs needs API call
        unchecked = []
        young_channels_data = []

        for ch_id in all_channel_ids:
            row = db.execute(
                "SELECT created_at_yt FROM channel_age_cache WHERE yt_channel_id=?",
                (ch_id,)
            ).fetchone()
            if row and row["created_at_yt"]:
                created = _parse_dt(row["created_at_yt"])
                if created:
                    age = (datetime.utcnow() - created).days
                    if age <= max_channel_age_days:
                        unchecked.append(ch_id)  # young by cache — still need full details
                # If too old, skip silently
            else:
                unchecked.append(ch_id)

        # Batch fetch channel details for unchecked IDs
        upd(f"Fetching details for {len(unchecked)} channels...", 35)
        channel_details = youtube.batch_get_channel_details(unchecked)
        # Each batch of 50 costs 1 unit
        quota_tracker["used"] += max(1, len(unchecked) // 50 + 1)

        now = datetime.utcnow()
        for ch in channel_details:
            # Write to age cache
            db.execute(
                "INSERT OR REPLACE INTO channel_age_cache(yt_channel_id, created_at_yt, checked_at) VALUES(?,?,?)",
                (ch["yt_channel_id"], ch.get("created_at_yt", ""), now.isoformat())
            )
            created = _parse_dt(ch.get("created_at_yt", ""))
            if not created:
                continue
            age_days = (now - created).days
            if age_days <= max_channel_age_days:
                ch["age_days"] = age_days
                young_channels_data.append(ch)
        db.commit()

        update_job(job_id, stage="Stage 2: Age Filter", progress=45,
                   message=f"{len(young_channels_data)} channels ≤{max_channel_age_days} days old.")

        # ── Stage 3: Breakout check and top videos ─────────────────────────
        update_job(job_id, stage="Stage 3: Breakout Check", progress=47)
        finalists = []

        for idx, ch in enumerate(young_channels_data):
            if check_cancelled():
                update_job(job_id, status="cancelled", message="Hunt cancelled.")
                db.close()
                return

            pct = 47 + int((idx / max(len(young_channels_data), 1)) * 25)
            upd(f"Checking breakout: {ch['name']} ({idx+1}/{len(young_channels_data)})", pct)

            try:
                videos = youtube.get_channel_top_videos(ch["yt_channel_id"], max_videos=50)
                quota_tracker["used"] += 2  # playlist + videos.list

                if not include_shorts:
                    videos = [v for v in videos if v.get("duration_sec", 0) >= min_dur]

                if not videos:
                    continue

                best = max(videos, key=lambda v: v.get("views", 0))
                if best.get("views", 0) < min_breakout_views:
                    continue

                top3 = sorted(videos, key=lambda v: v.get("views", 0), reverse=True)[:3]
                ratio = best["views"] / max(ch.get("subs", 0), 1)

                ch["breakout_video_id"] = best.get("yt_video_id", "")
                ch["breakout_views"] = best.get("views", 0)
                ch["breakout_title"] = best.get("title", "")
                ch["views_to_subs_ratio"] = round(ratio, 1)
                ch["top_videos"] = top3
                ch["all_titles"] = [v.get("title", "") for v in videos]
                finalists.append(ch)
            except Exception as e:
                logger.warning("Breakout check failed for %s: %s", ch.get("name"), e)

        update_job(job_id, stage="Stage 3: Breakout Check", progress=72,
                   message=f"{len(finalists)} channels cleared the breakout bar.")

        # ── Stage 4: Faceless + niche judgment ────────────────────────────
        update_job(job_id, stage="Stage 4: Faceless Judgment", progress=74)
        upd(f"Judging faceless status for {len(finalists)} channels...", 74)

        if finalists:
            batch_input = [
                {
                    "yt_channel_id": ch["yt_channel_id"],
                    "name": ch.get("name", ""),
                    "description": ch.get("description", ""),
                    "video_titles": ch.get("all_titles", []),
                }
                for ch in finalists
            ]
            judgments = claude_client.judge_faceless_batch(batch_input)
            judgment_map = {j["yt_channel_id"]: j for j in judgments}
            for ch in finalists:
                j = judgment_map.get(ch["yt_channel_id"], {})
                ch["faceless_judgment"] = j.get("faceless_judgment", "unclear")
                ch["faceless_confidence"] = j.get("faceless_confidence", 0.5)
                ch["faceless_reason"] = j.get("faceless_reason", "")
                ch["niche_guess"] = j.get("niche_guess", "")

        # ── Stage 5: Save results ─────────────────────────────────────────
        update_job(job_id, stage="Stage 5: Saving", progress=90)
        saved = 0
        for ch in finalists:
            is_new = db.execute(
                "SELECT id FROM discovered_channels WHERE yt_channel_id=?",
                (ch["yt_channel_id"],)
            ).fetchone() is None

            db.execute("""
                INSERT INTO discovered_channels
                  (hunt_id, yt_channel_id, name, handle, url, created_at_yt, age_days,
                   subs, total_views, video_count, breakout_video_id, breakout_views,
                   views_to_subs_ratio, faceless_judgment, faceless_confidence,
                   faceless_reason, niche_guess, status, first_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(yt_channel_id) DO UPDATE SET
                  hunt_id=excluded.hunt_id, age_days=excluded.age_days,
                  subs=excluded.subs, total_views=excluded.total_views,
                  breakout_video_id=excluded.breakout_video_id,
                  breakout_views=excluded.breakout_views,
                  views_to_subs_ratio=excluded.views_to_subs_ratio,
                  faceless_judgment=excluded.faceless_judgment,
                  faceless_confidence=excluded.faceless_confidence,
                  faceless_reason=excluded.faceless_reason,
                  niche_guess=excluded.niche_guess
            """, (
                hunt_id, ch["yt_channel_id"], ch.get("name", ""),
                ch.get("handle", ""), ch.get("url", ""),
                ch.get("created_at_yt", ""), ch.get("age_days", 0),
                ch.get("subs", 0), ch.get("total_views", 0), ch.get("video_count", 0),
                ch.get("breakout_video_id", ""), ch.get("breakout_views", 0),
                ch.get("views_to_subs_ratio", 0),
                ch.get("faceless_judgment", "unclear"),
                ch.get("faceless_confidence", 0.5),
                ch.get("faceless_reason", ""),
                ch.get("niche_guess", ""),
                "new" if is_new else None,
                datetime.utcnow().isoformat() if is_new else None,
            ))
            dc_row = db.execute(
                "SELECT id FROM discovered_channels WHERE yt_channel_id=?",
                (ch["yt_channel_id"],)
            ).fetchone()
            dc_id = dc_row["id"] if dc_row else None

            # Save top 3 videos
            if dc_id:
                db.execute("DELETE FROM discovered_videos WHERE discovered_channel_id=?", (dc_id,))
                for rank, v in enumerate(ch.get("top_videos", []), 1):
                    db.execute("""
                        INSERT INTO discovered_videos
                          (discovered_channel_id, yt_video_id, title, views,
                           published_at, duration_sec, rank)
                        VALUES (?,?,?,?,?,?,?)
                    """, (dc_id, v.get("yt_video_id", ""), v.get("title", ""),
                          v.get("views", 0), v.get("published_at", ""),
                          v.get("duration_sec", 0), rank))
            saved += 1
        db.commit()

        # Update hunt record
        db.execute("""
            UPDATE hunts SET finished_at=?, quota_used=?, results_found=?, status='done'
            WHERE id=?
        """, (datetime.utcnow().isoformat(), quota_tracker.get("used", 0), saved, hunt_id))
        db.commit()

        update_job(job_id, status="done", stage="Complete", progress=100,
                   message=f"Hunt complete! Found {saved} channels ≤{max_channel_age_days} days old with a {min_breakout_views:,}+ view breakout.",
                   result_json=json.dumps({"hunt_id": hunt_id, "results_found": saved}))

    except Exception as e:
        logger.exception("Hunt job %d failed", job_id)
        db.execute("UPDATE hunts SET status='failed', finished_at=? WHERE id=?",
                   (datetime.utcnow().isoformat(), hunt_id))
        db.commit()
        update_job(job_id, status="failed", error=str(e),
                   message=f"Hunt failed: {str(e)[:200]}")
    finally:
        db.close()


def _parse_dt(s: str):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)
    except Exception:
        return None
