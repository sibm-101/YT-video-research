from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, niche_id: int = None, tab: str = "channels"):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]

    channels = []
    outliers = []
    frameworks = []
    seeds = []

    if niche_id:
        channels = [dict(r) for r in db.execute("""
            SELECT c.*, COUNT(v.id) as video_count_actual
            FROM channels c LEFT JOIN videos v ON v.channel_id = c.id
            WHERE c.niche_id=?
            GROUP BY c.id ORDER BY c.subs DESC
        """, (niche_id,)).fetchall()]

        outliers = [dict(r) for r in db.execute("""
            SELECT v.*, c.name as channel_name, c.format_class
            FROM videos v JOIN channels c ON v.channel_id = c.id
            WHERE v.niche_id=? AND v.outlier_score IS NOT NULL
            ORDER BY v.outlier_score DESC LIMIT 100
        """, (niche_id,)).fetchall()]

        frameworks = [dict(r) for r in db.execute(
            "SELECT * FROM frameworks WHERE niche_id=? OR niche_id IS NULL ORDER BY id DESC",
            (niche_id,)
        ).fetchall()]

        seeds = [dict(r) for r in db.execute(
            "SELECT * FROM seeds WHERE niche_id=? ORDER BY score DESC LIMIT 50",
            (niche_id,)
        ).fetchall()]

    db.close()
    return templates.TemplateResponse("library.html", {
        "request": request,
        "niches": niches,
        "active_niche_id": niche_id,
        "channels": channels,
        "outliers": outliers,
        "frameworks": frameworks,
        "seeds": seeds,
        "tab": tab,
    })


@router.delete("/api/library/channels/{channel_id}")
async def delete_channel(channel_id: int):
    db = get_db()
    db.execute("DELETE FROM videos WHERE channel_id=?", (channel_id,))
    db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@router.post("/api/library/frameworks/{fw_id}/edit")
async def edit_framework(fw_id: int, name: str = Form(...), pattern: str = Form(...),
                          mechanics: str = Form(...), example: str = Form(default="")):
    db = get_db()
    db.execute("UPDATE frameworks SET name=?, pattern=?, mechanics=?, example=? WHERE id=?",
               (name, pattern, mechanics, example, fw_id))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@router.delete("/api/library/frameworks/{fw_id}")
async def delete_framework(fw_id: int):
    db = get_db()
    db.execute("DELETE FROM frameworks WHERE id=?", (fw_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})
