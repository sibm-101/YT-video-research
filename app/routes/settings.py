import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import load_config, save_config, get_env, write_env
from app import claude_client
from app.youtube import test_youtube_key
from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    db = get_db()
    niches = [dict(r) for r in db.execute("SELECT * FROM niches ORDER BY name").fetchall()]
    db.close()
    cfg = load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "niches": niches,
        "cfg": cfg,
        "anthropic_key_set": bool(get_env("ANTHROPIC_API_KEY")),
        "youtube_key_set": bool(get_env("YOUTUBE_API_KEY")),
    })


@router.post("/api/settings/save")
async def save_settings(request: Request):
    data = await request.json()
    cfg = load_config()

    # Update numeric fields
    numeric_fields = [
        "outlier_threshold", "longform_min_seconds", "median_window_videos",
        "ideas_per_run", "validate_top", "structural_min", "staleness_months",
        "burn_min_subs", "burn_fuzzy_threshold", "max_searches_per_run",
        "cache_days", "wiki_min_monthly_views", "wiki_dead_monthly_views"
    ]
    for field in numeric_fields:
        if field in data:
            try:
                val = data[field]
                cfg[field] = float(val) if "." in str(val) else int(val)
            except (ValueError, TypeError):
                pass

    # Models
    if "parse_classify" in data:
        cfg["models"]["parse_classify"] = data["parse_classify"]
    if "generate_reason" in data:
        cfg["models"]["generate_reason"] = data["generate_reason"]

    # Reddit toggle
    if "reddit_enabled" in data:
        cfg["reddit"]["enabled"] = bool(data["reddit_enabled"])

    # Channel Hunter settings
    if "hunter" not in cfg:
        cfg["hunter"] = {}
    hunter_num = {
        "hunter_max_age": "max_channel_age_days",
        "hunter_min_views": "min_breakout_views",
        "hunter_max_seeds": "max_seeds_per_hunt",
    }
    for form_key, cfg_key in hunter_num.items():
        if form_key in data:
            try:
                cfg["hunter"][cfg_key] = int(data[form_key])
            except (ValueError, TypeError):
                pass
    if "hunter_region" in data and data["hunter_region"]:
        cfg["hunter"]["region"] = data["hunter_region"].upper()[:2]
    if "hunter_seeds" in data:
        seeds = [s.strip() for s in data["hunter_seeds"].splitlines() if s.strip()]
        if seeds:
            cfg["hunter"]["seed_keywords"] = seeds

    save_config(cfg)
    return JSONResponse({"ok": True, "message": "Settings saved."})


@router.post("/api/settings/reset")
async def reset_settings():
    from app.config import DEFAULTS, CONFIG_PATH
    import yaml
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(DEFAULTS, f, default_flow_style=False)
    return JSONResponse({"ok": True, "message": "Settings reset to defaults."})


@router.post("/api/settings/test-anthropic")
async def test_anthropic_settings(key: str = Form(...)):
    os.environ["ANTHROPIC_API_KEY"] = key
    ok, msg = claude_client.test_anthropic_key()
    if ok:
        # Update .env
        yt_key = get_env("YOUTUBE_API_KEY")
        write_env(key, yt_key)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/api/settings/test-youtube")
async def test_youtube_settings(key: str = Form(...)):
    os.environ["YOUTUBE_API_KEY"] = key
    ok, msg = test_youtube_key()
    if ok:
        ant_key = get_env("ANTHROPIC_API_KEY")
        write_env(ant_key, key)
    return JSONResponse({"ok": ok, "message": msg})
