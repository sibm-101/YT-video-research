from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.database import get_db

router = APIRouter()


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not row:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(dict(row))


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    db = get_db()
    db.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','running')", (job_id,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})
