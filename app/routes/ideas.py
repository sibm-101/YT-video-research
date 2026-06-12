import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from datetime import datetime

from app.database import get_db
from app.pipeline.runner import start_job

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/ideas/run", response_class=HTMLResponse)
async def run_page(request: Request, niche_id: int = None):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    db.close()
    return templates.TemplateResponse("ideas_run.html", {
        "request": request,
        "niches": niches,
        "active_niche_id": niche_id,
    })


@router.post("/api/ideas/start")
async def start_ideas_run(
    niche_id: int = Form(...),
    n_ideas: int = Form(default=30),
    validate_top: int = Form(default=25),
):
    job_id = start_job("ideas", niche_id, n_ideas=n_ideas, validate_top=validate_top)
    return JSONResponse({"ok": True, "job_id": job_id})


@router.post("/api/ideas/validate-custom")
async def start_custom_validation(request: Request):
    data = await request.json()
    niche_id = data.get("niche_id")
    titles_raw = data.get("titles", "")
    titles = [t.strip() for t in titles_raw.splitlines() if t.strip()]
    if not niche_id:
        return JSONResponse({"ok": False, "message": "Please select a niche."}, status_code=400)
    if not titles:
        return JSONResponse({"ok": False, "message": "Please enter at least one title."}, status_code=400)
    job_id = start_job("validate_custom", int(niche_id), titles=titles)
    return JSONResponse({"ok": True, "job_id": job_id, "count": len(titles)})


@router.get("/ideas", response_class=HTMLResponse)
async def ideas_list(
    request: Request,
    niche_id: int = None,
    verdict: str = None,
    status: str = None,
    min_score: float = None,
    run_id: int = None,
):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]

    query = "SELECT i.*, n.name as niche_name FROM ideas i LEFT JOIN niches n ON i.niche_id=n.id WHERE 1=1"
    params = []
    if niche_id:
        query += " AND i.niche_id=?"
        params.append(niche_id)
    if verdict:
        query += " AND i.verdict=?"
        params.append(verdict)
    if status:
        query += " AND i.status=?"
        params.append(status)
    if min_score is not None:
        query += " AND i.final_score>=?"
        params.append(min_score)
    if run_id:
        query += " AND i.run_id=?"
        params.append(run_id)
    query += " ORDER BY i.final_score DESC LIMIT 200"

    ideas = [dict(r) for r in db.execute(query, params).fetchall()]
    for idea in ideas:
        try:
            idea["evidence"] = json.loads(idea.get("evidence_json") or "[]")
        except Exception:
            idea["evidence"] = []

    runs = [dict(r) for r in db.execute(
        "SELECT id, started_at, ideas_survived FROM runs ORDER BY started_at DESC LIMIT 20"
    ).fetchall()]

    db.close()
    return templates.TemplateResponse("ideas_list.html", {
        "request": request,
        "niches": niches,
        "ideas": ideas,
        "runs": runs,
        "active_niche_id": niche_id,
        "filter_verdict": verdict,
        "filter_status": status,
        "filter_min_score": min_score,
        "filter_run_id": run_id,
    })


@router.post("/api/ideas/{idea_id}/kill")
async def kill_idea(idea_id: int):
    db = get_db()
    db.execute("UPDATE ideas SET status='killed', verdict='KILL' WHERE id=?", (idea_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@router.post("/api/ideas/{idea_id}/ship")
async def ship_idea(idea_id: int, video_url: str = Form(...)):
    from app.youtube import get_video_by_id
    import re
    # Extract video ID from URL
    m = re.search(r"(?:v=|youtu\.be/)([\w-]+)", video_url)
    if not m:
        return JSONResponse({"ok": False, "message": "Invalid YouTube video URL."}, status_code=400)

    video_id = m.group(1)
    v = get_video_by_id(video_id)
    if not v:
        return JSONResponse({"ok": False, "message": "Could not fetch video stats. Check the URL and try again."}, status_code=400)

    db = get_db()
    idea = db.execute("SELECT niche_id FROM ideas WHERE id=?", (idea_id,)).fetchone()
    if not idea:
        db.close()
        return JSONResponse({"ok": False, "message": "Idea not found."}, status_code=404)

    # Get own channel median from DB
    ch = db.execute("""
        SELECT c.median_views FROM channels c
        JOIN videos vid ON vid.channel_id = c.id
        WHERE vid.yt_video_id=? LIMIT 1
    """, (video_id,)).fetchone()
    own_median = ch["median_views"] if ch else 1000
    own_outlier = v["views"] / max(own_median, 1)

    db.execute("""
        INSERT INTO shipped_results (idea_id, video_url, views, own_channel_median, own_outlier_score, logged_at)
        VALUES (?,?,?,?,?,?)
    """, (idea_id, video_url, v["views"], own_median, round(own_outlier, 2),
          datetime.utcnow().isoformat()))
    db.execute("UPDATE ideas SET status='shipped' WHERE id=?", (idea_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True, "views": v["views"], "outlier_score": round(own_outlier, 2)})
