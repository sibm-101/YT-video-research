from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import has_keys, write_env, get_env, clean_key
from app import claude_client
from app.youtube import test_youtube_key

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse("setup/wizard.html", {"request": request})


@router.post("/api/setup/test-anthropic")
async def test_anthropic(key: str = Form(...)):
    import os
    os.environ["ANTHROPIC_API_KEY"] = clean_key(key)
    ok, msg = claude_client.test_anthropic_key()
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/setup/test-youtube")
async def test_youtube(key: str = Form(...)):
    import os
    os.environ["YOUTUBE_API_KEY"] = clean_key(key)
    ok, msg = test_youtube_key()
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/setup/save")
async def save_setup(
    anthropic_key: str = Form(...),
    youtube_key: str = Form(...),
    reddit_id: str = Form(default=""),
    reddit_secret: str = Form(default=""),
):
    if not anthropic_key or not youtube_key:
        return JSONResponse({"ok": False, "message": "Both API keys are required."}, status_code=400)
    write_env(anthropic_key, youtube_key, reddit_id, reddit_secret)
    return JSONResponse({"ok": True, "message": "Keys saved successfully."})
