from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))
REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


@router.get("/reports", response_class=HTMLResponse)
async def reports_list(request: Request):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    runs = [dict(r) for r in db.execute("""
        SELECT r.*, n.name as niche_name
        FROM runs r LEFT JOIN niches n ON r.niche_id = n.id
        ORDER BY r.started_at DESC LIMIT 50
    """).fetchall()]
    db.close()
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "niches": niches,
        "runs": runs,
    })


@router.get("/reports/{run_id}", response_class=HTMLResponse)
async def view_report(request: Request, run_id: int):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    run = db.execute("""
        SELECT r.*, n.name as niche_name FROM runs r
        LEFT JOIN niches n ON r.niche_id = n.id WHERE r.id=?
    """, (run_id,)).fetchone()

    content = ""
    if run and run["report_path"] and Path(run["report_path"]).exists():
        content = Path(run["report_path"]).read_text()

    db.close()
    return templates.TemplateResponse("report_view.html", {
        "request": request,
        "niches": niches,
        "run": dict(run) if run else None,
        "content": content,
        "run_id": run_id,
    })


@router.get("/reports/{run_id}/download")
async def download_report(run_id: int):
    db = get_db()
    run = db.execute("SELECT report_path FROM runs WHERE id=?", (run_id,)).fetchone()
    db.close()
    if run and run["report_path"] and Path(run["report_path"]).exists():
        return FileResponse(run["report_path"], media_type="text/markdown",
                           filename=Path(run["report_path"]).name)
    return JSONResponse({"error": "Report not found."}, status_code=404)
