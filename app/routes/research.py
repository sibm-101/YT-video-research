import json
from fastapi import APIRouter, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import List, Optional

from app.database import get_db
from app.pipeline.runner import start_job

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/research", response_class=HTMLResponse)
async def research_page(request: Request, niche_id: int = None):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    db.close()
    return templates.TemplateResponse("research.html", {
        "request": request,
        "niches": niches,
        "active_niche_id": niche_id,
    })


@router.post("/api/research/analyze")
async def analyze_research(
    niche_id: int = Form(...),
    urls: str = Form(default=""),
    pasted_text: str = Form(default=""),
    files: List[UploadFile] = File(default=[]),
):
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    file_data = []
    for f in files:
        if f.filename:
            data = await f.read()
            file_data.append({"filename": f.filename, "data": data})

    if not url_list and not file_data and not pasted_text.strip():
        return JSONResponse({"ok": False, "message": "Please provide at least one URL, file, or pasted text."}, status_code=400)

    job_id = start_job("ingestion", niche_id,
                        urls=url_list, files=file_data, pasted_text=pasted_text)
    return JSONResponse({"ok": True, "job_id": job_id})
