import logging
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.config import has_keys
from app.routes import setup, dashboard, research, ideas, library, reports, settings, jobs, channel_hunter

# The Windows console defaults to a non-UTF-8 code page (cp1252), so any log line
# containing a Unicode character would otherwise crash the process. Switch stdout/
# stderr to UTF-8 with errors="replace" so logging can never raise on encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_log_handlers = [logging.FileHandler(str(Path(__file__).parent.parent / "app.log"), encoding="utf-8")]
try:
    _log_handlers.append(logging.StreamHandler())
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_log_handlers,
)

app = FastAPI(title="Viral Idea Engine", docs_url=None, redoc_url=None)

BASE_DIR = Path(__file__).parent.parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(setup.router)
app.include_router(dashboard.router)
app.include_router(research.router)
app.include_router(ideas.router)
app.include_router(library.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(jobs.router)
app.include_router(channel_hunter.router)


@app.middleware("http")
async def setup_redirect(request: Request, call_next):
    path = request.url.path
    # Always allow setup routes and API calls
    if path.startswith("/setup") or path.startswith("/api/setup") or path.startswith("/static"):
        return await call_next(request)
    if not has_keys():
        return RedirectResponse(url="/setup")
    return await call_next(request)


@app.on_event("startup")
async def startup():
    init_db()
    logging.info("Viral Idea Engine started.")
