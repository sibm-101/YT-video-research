"""Channel Hunter routes — reuses YouTube client, jobs system, and ingestion pipeline."""
import json
from datetime import datetime
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import load_config, save_config
from app.database import get_db
from app.pipeline.runner import start_job
from app.pipeline.hunter import estimate_quota

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/hunter", response_class=HTMLResponse)
async def hunter_page(request: Request, hunt_id: int = None):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    cfg = load_config()
    hunter_cfg = cfg.get("hunter", {})

    # Load hunt history
    hunts = [dict(r) for r in db.execute(
        "SELECT * FROM hunts ORDER BY started_at DESC LIMIT 20"
    ).fetchall()]

    # Load results for selected hunt (or most recent completed)
    active_hunt = None
    results = []
    if hunt_id:
        active_hunt = db.execute("SELECT * FROM hunts WHERE id=?", (hunt_id,)).fetchone()
        if active_hunt:
            active_hunt = dict(active_hunt)
    elif hunts:
        done = [h for h in hunts if h["status"] == "done"]
        if done:
            active_hunt = done[0]
            hunt_id = active_hunt["id"]

    if hunt_id:
        rows = db.execute("""
            SELECT dc.*, GROUP_CONCAT(dv.yt_video_id || '|||' || dv.title || '|||' ||
              dv.views || '|||' || dv.published_at || '|||' || dv.rank, ';;;') as videos_raw
            FROM discovered_channels dc
            LEFT JOIN discovered_videos dv ON dv.discovered_channel_id = dc.id
            WHERE dc.hunt_id=?
            GROUP BY dc.id
            ORDER BY dc.views_to_subs_ratio DESC
        """, (hunt_id,)).fetchall()

        for row in rows:
            r = dict(row)
            r["videos"] = _parse_videos_raw(r.get("videos_raw", ""))
            results.append(r)

    db.close()
    return templates.TemplateResponse("channel_hunter.html", {
        "request": request,
        "niches": niches,
        "hunter_cfg": hunter_cfg,
        "hunts": hunts,
        "active_hunt": active_hunt,
        "active_hunt_id": hunt_id,
        "results": results,
        "seed_keywords_json": json.dumps(hunter_cfg.get("seed_keywords", [])),
    })


def _parse_videos_raw(raw: str) -> list[dict]:
    if not raw:
        return []
    videos = []
    for part in raw.split(";;;"):
        fields = part.split("|||")
        if len(fields) >= 5:
            videos.append({
                "yt_video_id": fields[0],
                "title": fields[1],
                "views": int(fields[2]) if fields[2].isdigit() else 0,
                "published_at": fields[3],
                "rank": int(fields[4]) if fields[4].isdigit() else 0,
            })
    return sorted(videos, key=lambda v: v.get("rank", 99))


@router.post("/api/hunter/start")
async def start_hunt(
    seed_keywords: str = Form(...),
    max_channel_age_days: int = Form(default=30),
    min_breakout_views: int = Form(default=500000),
    region: str = Form(default="US"),
    include_shorts: bool = Form(default=False),
):
    keywords = [k.strip() for k in seed_keywords.split("\n") if k.strip()]
    if not keywords:
        return JSONResponse({"ok": False, "message": "Please provide at least one seed keyword."}, status_code=400)

    cfg = load_config()
    max_seeds = cfg.get("hunter", {}).get("max_seeds_per_hunt", 35)
    keywords = keywords[:max_seeds]

    # Quota guard: refuse if estimate > 95% of daily limit
    est = estimate_quota(len(keywords))
    if est > 9500:
        return JSONResponse({
            "ok": False,
            "message": f"This hunt would use about {est:,} quota units, which is over the safe daily limit. "
                       f"Reduce to fewer than 95 seed keywords, or run again after midnight Pacific time."
        }, status_code=400)

    # Create hunt record
    db = get_db()
    cur = db.execute("""
        INSERT INTO hunts (started_at, seed_keywords, region, max_channel_age_days,
                           min_breakout_views, status)
        VALUES (?,?,?,?,?,?)
    """, (datetime.utcnow().isoformat(), json.dumps(keywords), region,
          max_channel_age_days, min_breakout_views, "running"))
    db.commit()
    hunt_id = cur.lastrowid
    db.close()

    job_id = start_job(
        "hunt", 0,
        hunt_id=hunt_id,
        seed_keywords=keywords,
        region=region,
        max_channel_age_days=max_channel_age_days,
        min_breakout_views=min_breakout_views,
        include_shorts=include_shorts,
    )
    return JSONResponse({"ok": True, "job_id": job_id, "hunt_id": hunt_id,
                         "estimated_quota": est, "seed_count": len(keywords)})


@router.get("/api/hunter/quota-estimate")
async def quota_estimate(seed_count: int = 5):
    est = estimate_quota(seed_count)
    pct = round(est / 10000 * 100, 1)
    return JSONResponse({
        "estimate": est,
        "percent": pct,
        "message": f"About {est:,} of today's 10,000 free units ({pct}%)",
        "safe": est < 9500,
    })


@router.post("/api/hunter/channels/{dc_id}/status")
async def update_channel_status(dc_id: int, status: str = Form(...)):
    valid = ("new", "saved", "dismissed", "sent_to_engine")
    if status not in valid:
        return JSONResponse({"ok": False, "message": "Invalid status."}, status_code=400)
    db = get_db()
    db.execute("UPDATE discovered_channels SET status=? WHERE id=?", (status, dc_id))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@router.post("/api/hunter/channels/{dc_id}/send-to-engine")
async def send_to_engine(dc_id: int, niche_id: int = Form(...)):
    """Send a discovered channel into the Idea Engine using the EXISTING ingestion pipeline."""
    db = get_db()
    ch = db.execute("SELECT * FROM discovered_channels WHERE id=?", (dc_id,)).fetchone()
    if not ch:
        db.close()
        return JSONResponse({"ok": False, "message": "Channel not found."}, status_code=404)

    channel_url = ch["url"] or f"https://youtube.com/channel/{ch['yt_channel_id']}"
    db.close()

    # Reuse the EXISTING ingestion job — do not duplicate it
    job_id = start_job("ingestion", niche_id,
                        urls=[channel_url], files=[], pasted_text="")

    # Mark as sent
    db = get_db()
    db.execute("UPDATE discovered_channels SET status='sent_to_engine' WHERE id=?", (dc_id,))
    db.commit()
    db.close()

    return JSONResponse({"ok": True, "job_id": job_id, "channel_url": channel_url})


@router.get("/api/hunter/results/{hunt_id}")
async def get_hunt_results(
    hunt_id: int,
    faceless_only: bool = False,
    min_ratio: float = 0.0,
    niche_filter: str = "",
    status_filter: str = "",
    sort_by: str = "ratio",
):
    db = get_db()
    rows = db.execute("""
        SELECT dc.*, GROUP_CONCAT(dv.yt_video_id || '|||' || dv.title || '|||' ||
          dv.views || '|||' || dv.published_at || '|||' || dv.rank, ';;;') as videos_raw
        FROM discovered_channels dc
        LEFT JOIN discovered_videos dv ON dv.discovered_channel_id = dc.id
        WHERE dc.hunt_id=?
        GROUP BY dc.id
    """, (hunt_id,)).fetchall()
    db.close()

    results = []
    for row in rows:
        r = dict(row)
        r["videos"] = _parse_videos_raw(r.get("videos_raw", ""))
        if faceless_only and r.get("faceless_judgment") != "likely":
            continue
        if r.get("views_to_subs_ratio", 0) < min_ratio:
            continue
        if niche_filter and niche_filter.lower() not in (r.get("niche_guess") or "").lower():
            continue
        if status_filter and r.get("status") != status_filter:
            continue
        results.append(r)

    if sort_by == "views":
        results.sort(key=lambda x: x.get("breakout_views", 0), reverse=True)
    elif sort_by == "age":
        results.sort(key=lambda x: x.get("age_days", 999))
    else:
        results.sort(key=lambda x: x.get("views_to_subs_ratio", 0), reverse=True)

    return JSONResponse(results)
