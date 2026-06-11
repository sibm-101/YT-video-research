from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from datetime import datetime

from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


def _quota_used_today() -> int:
    db = get_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    row = db.execute(
        "SELECT COALESCE(SUM(quota_used),0) as q FROM runs WHERE started_at LIKE ?",
        (today + "%",)
    ).fetchone()
    db.close()
    return row["q"] if row else 0


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, niche_id: int = None):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]

    active_niche = None
    last_run = None

    if niche_id:
        active_niche = db.execute("SELECT * FROM niches WHERE id=?", (niche_id,)).fetchone()
        if active_niche:
            active_niche = dict(active_niche)
    elif niches:
        active_niche = niches[0]

    if active_niche:
        run = db.execute(
            "SELECT * FROM runs WHERE niche_id=? ORDER BY started_at DESC LIMIT 1",
            (active_niche["id"],)
        ).fetchone()
        if run:
            last_run = dict(run)

    db.close()
    quota = _quota_used_today()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "niches": niches,
        "active_niche": active_niche,
        "last_run": last_run,
        "quota_used": quota,
        "quota_max": 10000,
    })


@router.post("/api/niches/create")
async def create_niche(name: str = Form(...)):
    name = name.strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Name cannot be empty."}, status_code=400)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO niches (name, created_at) VALUES (?,?)",
            (name, datetime.utcnow().isoformat())
        )
        db.commit()
        return JSONResponse({"ok": True, "id": cur.lastrowid, "name": name})
    except Exception as e:
        if "UNIQUE" in str(e):
            return JSONResponse({"ok": False, "message": f"A niche named '{name}' already exists."}, status_code=400)
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)
    finally:
        db.close()


@router.post("/api/niches/{niche_id}/rename")
async def rename_niche(niche_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Name cannot be empty."}, status_code=400)
    db = get_db()
    db.execute("UPDATE niches SET name=? WHERE id=?", (name, niche_id))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@router.delete("/api/niches/{niche_id}")
async def delete_niche(niche_id: int):
    db = get_db()
    db.execute("DELETE FROM niches WHERE id=?", (niche_id,))
    db.execute("DELETE FROM channels WHERE niche_id=?", (niche_id,))
    db.execute("DELETE FROM videos WHERE niche_id=?", (niche_id,))
    db.execute("DELETE FROM ideas WHERE niche_id=?", (niche_id,))
    db.execute("DELETE FROM patterns WHERE niche_id=?", (niche_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})
