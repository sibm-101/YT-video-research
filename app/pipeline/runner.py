"""Full pipeline runner - orchestrates all stages in a background thread."""
import json
import logging
import threading
from datetime import datetime

from app.database import get_db
from app.config import load_config
from app.utils import is_duplicate
from app.pipeline import enrichment, outlier, gauntlet
from app import claude_client

logger = logging.getLogger(__name__)


def update_job(job_id: int, stage: str = None, progress: int = None,
               message: str = None, status: str = None, error: str = None,
               result_json: str = None):
    db = get_db()
    parts = []
    vals = []
    if stage:
        parts.append("stage=?"); vals.append(stage)
    if progress is not None:
        parts.append("progress_pct=?"); vals.append(progress)
    if message:
        parts.append("message=?"); vals.append(message)
    if status:
        parts.append("status=?"); vals.append(status)
    if error:
        parts.append("error=?"); vals.append(error)
    if result_json:
        parts.append("result_json=?"); vals.append(result_json)
    if status in ("done", "failed", "cancelled"):
        parts.append("finished_at=?"); vals.append(datetime.utcnow().isoformat())
    if parts:
        vals.append(job_id)
        db.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id=?", vals)
        db.commit()
    db.close()


def run_ingestion_job(job_id: int, niche_id: int, urls: list[str],
                      uploaded_files: list[dict], pasted_text: str):
    """Background task for Add Research ingestion."""
    from app.pipeline.ingestion import (
        ingest_channel_url, ingest_video_url, ingest_screenshot,
        ingest_text, ingest_pdf, parse_youtube_url
    )

    def upd(msg, pct=None):
        update_job(job_id, message=msg, progress=pct)

    try:
        update_job(job_id, status="running", stage="Ingestion", progress=0)
        results = []
        total_items = len(urls) + len(uploaded_files) + (1 if pasted_text.strip() else 0)
        done = 0

        # Process URLs
        for url in urls:
            url = url.strip()
            if not url:
                continue
            upd(f"Processing URL: {url[:60]}", int(done / max(total_items, 1) * 80))
            parsed = parse_youtube_url(url)
            if parsed:
                if parsed[0] == "channel":
                    r = ingest_channel_url(url, niche_id, upd)
                else:
                    r = ingest_video_url(url, niche_id)
                results.append({"url": url, "type": parsed[0], **r})
            done += 1

        # Process uploaded files
        for f in uploaded_files:
            fname = f.get("filename", "")
            fdata = f.get("data", b"")
            upd(f"Processing file: {fname}", int(done / max(total_items, 1) * 80))
            ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
            if ext in ("png", "jpg", "jpeg", "webp"):
                mt = f"image/{ext if ext != 'jpg' else 'jpeg'}"
                r = ingest_screenshot(fdata, mt, niche_id)
                results.append({"file": fname, "type": "screenshot", **r})
            elif ext == "pdf":
                r = ingest_pdf(fdata, fname, niche_id)
                results.append({"file": fname, "type": "pdf", **r})
            elif ext in ("txt", "md"):
                text = fdata.decode("utf-8", errors="replace")
                r = ingest_text(text, niche_id)
                results.append({"file": fname, "type": "text", **r})
            done += 1

        # Process pasted text
        if pasted_text.strip():
            upd("Processing pasted text...", 70)
            r = ingest_text(pasted_text, niche_id)
            results.append({"type": "pasted_text", **r})

        # Enrichment
        upd("Computing channel medians...", 80)
        enrichment.compute_channel_medians(niche_id, upd)

        upd("Classifying channel formats...", 85)
        enrichment.classify_channel_formats(niche_id, upd)

        upd("Computing outlier scores...", 90)
        outlier.compute_outlier_scores(niche_id, upd)

        update_job(job_id, status="done", stage="Complete", progress=100,
                   message="Research ingested successfully.",
                   result_json=json.dumps(results))

    except Exception as e:
        logger.exception("Ingestion job %d failed", job_id)
        update_job(job_id, status="failed", error=str(e),
                   message=f"Ingestion failed: {str(e)[:200]}")


def run_ideas_job(job_id: int, niche_id: int, n_ideas: int, validate_top: int):
    """Background task for Find New Ideas full pipeline."""
    def upd(msg, pct=None):
        update_job(job_id, message=msg, progress=pct)

    try:
        update_job(job_id, status="running", stage="Starting", progress=0)
        cfg = load_config()
        db = get_db()

        upd("Gathering niche data...", 5)

        niche = db.execute("SELECT * FROM niches WHERE id=?", (niche_id,)).fetchone()
        niche_name = niche["name"] if niche else "Unknown"

        # Stage 2: compute outliers
        upd("Mining outlier videos...", 10)
        outlier.compute_outlier_scores(niche_id, upd)

        outliers = outlier.get_outliers(niche_id, limit=80)
        averages = outlier.get_average_videos(niche_id, limit=60)

        # Stage 3: pattern extraction + rubric
        upd("Extracting patterns from outliers...", 20)
        pattern_result = claude_client.extract_patterns(outliers, averages, niche_name)
        patterns = pattern_result.get("patterns", [])

        # Store patterns
        db.execute("DELETE FROM patterns WHERE niche_id=?", (niche_id,))
        for p in patterns:
            db.execute("""
                INSERT INTO patterns (niche_id, topic, frame, mechanics_json, evidence_video_ids, created_at)
                VALUES (?,?,?,?,?,?)
            """, (niche_id, p.get("topic", ""), p.get("frame", ""),
                  json.dumps(p.get("mechanics", [])),
                  json.dumps(p.get("evidence_titles", [])),
                  datetime.utcnow().isoformat()))
        db.commit()

        upd("Deriving niche rubric...", 30)
        rubric_result = claude_client.derive_rubric(outliers, averages, niche_name)
        rubric = rubric_result.get("rubric", [])
        db.execute("UPDATE niches SET rubric_json=? WHERE id=?",
                   (json.dumps(rubric), niche_id))
        db.commit()

        # Stage 4: idea generation
        upd("Generating video ideas...", 40)
        frameworks = [dict(r) for r in db.execute(
            "SELECT * FROM frameworks WHERE niche_id=? OR niche_id IS NULL", (niche_id,)
        ).fetchall()]

        existing_titles = [r["title"] for r in db.execute(
            "SELECT title FROM ideas WHERE niche_id=?", (niche_id,)
        ).fetchall()]
        existing_titles += [r["title"] for r in db.execute(
            "SELECT title FROM videos WHERE niche_id=?", (niche_id,)
        ).fetchall()]

        shipped = db.execute("""
            SELECT i.title, s.own_outlier_score
            FROM shipped_results s JOIN ideas i ON s.idea_id = i.id
            WHERE i.niche_id=? ORDER BY s.logged_at DESC LIMIT 20
        """, (niche_id,)).fetchall()
        shipped_summary = ""
        if shipped:
            shipped_summary = "Shipped results:\n" + "\n".join(
                f"- {r['title']}: {r['own_outlier_score']:.1f}x" for r in shipped
            )

        seeds = [dict(r) for r in db.execute(
            "SELECT text, why_compelling, score FROM seeds WHERE niche_id=? ORDER BY score DESC LIMIT 20",
            (niche_id,)
        ).fetchall()]

        raw_ideas = claude_client.generate_ideas(
            rubric, patterns, frameworks, existing_titles,
            shipped_summary, seeds, n_ideas, niche_name
        )

        # Dedup generated ideas
        deduped = []
        seen_titles = list(existing_titles)
        for idea in raw_ideas:
            t = idea.get("title", "")
            if not is_duplicate(t, seen_titles, 0.85):
                deduped.append(idea)
                seen_titles.append(t)

        upd(f"Generated {len(deduped)} unique ideas, scoring...", 50)

        # Structural scoring (batched)
        if deduped and rubric:
            try:
                scores = claude_client.score_structural(deduped, rubric, niche_name)
                score_map = {s["title"]: s for s in scores if "title" in s}
                for idea in deduped:
                    s = score_map.get(idea["title"], {})
                    idea["structural_score"] = float(s.get("score", 5))
                    idea["structural_weakness"] = s.get("weakness", "")
            except Exception as e:
                logger.warning("Structural scoring failed: %s", e)
                for idea in deduped:
                    idea["structural_score"] = 5.0

        # Sort by structural score, take top validate_top
        deduped.sort(key=lambda x: x.get("structural_score", 0), reverse=True)
        to_validate = deduped[:validate_top]
        to_park = deduped[validate_top:]

        # Create a run record
        run_row = db.execute("""
            INSERT INTO runs (niche_id, started_at) VALUES (?,?)
        """, (niche_id, datetime.utcnow().isoformat()))
        db.commit()
        run_id = run_row.lastrowid

        quota_tracker = {"used": 0, "max": cfg.get("max_searches_per_run", 60) * 100}

        niche_videos = [dict(r) for r in db.execute(
            "SELECT title, views, outlier_score FROM videos WHERE niche_id=?", (niche_id,)
        ).fetchall()]

        all_idea_titles = [i.get("title", "") for i in deduped]

        survived = 0

        # Stage 5: gauntlet for top ideas
        for i, idea in enumerate(to_validate):
            pct = 55 + int((i / max(len(to_validate), 1)) * 35)
            upd(f"Stress-testing idea {i+1} of {len(to_validate)}: '{idea['title'][:50]}...'", pct)

            result = gauntlet.run_gauntlet(
                idea, niche_id, quota_tracker,
                all_idea_titles, niche_videos, rubric, niche_name, upd
            )

            if result["verdict"] not in ("KILL",):
                survived += 1

            db.execute("""
                INSERT INTO ideas
                (niche_id, run_id, title, one_line, frame_used, angle_note,
                 structural_score, demand_score, staleness_mult, freshness_gate,
                 final_score, verdict, evidence_json, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (niche_id, run_id, result["title"], result.get("one_line", ""),
                  result.get("frame_used", ""), result.get("angle_note", ""),
                  result.get("structural_score", 5), result.get("demand_score", 5),
                  result.get("staleness_mult", 1.0), result.get("freshness_gate", 1),
                  result.get("final_score", 0), result.get("verdict", "PARK"),
                  result.get("evidence_json", "[]"), "new",
                  datetime.utcnow().isoformat()))
            db.commit()

        # Park remaining ideas (not validated)
        for idea in to_park:
            db.execute("""
                INSERT INTO ideas
                (niche_id, run_id, title, one_line, frame_used, angle_note,
                 structural_score, demand_score, staleness_mult, freshness_gate,
                 final_score, verdict, evidence_json, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (niche_id, run_id, idea["title"], idea.get("one_line", ""),
                  idea.get("frame_used", ""), idea.get("angle_note", ""),
                  idea.get("structural_score", 5), 5.0, 1.0, 1,
                  idea.get("structural_score", 5) * 0.5, "PARK",
                  "[]", "new", datetime.utcnow().isoformat()))
        db.commit()

        # Update run stats
        db.execute("""
            UPDATE runs SET quota_used=?, ideas_generated=?, ideas_survived=?, finished_at=?
            WHERE id=?
        """, (quota_tracker["used"] // 100, len(deduped), survived,
              datetime.utcnow().isoformat(), run_id))
        db.commit()

        # Stage 6: generate report
        upd("Generating report...", 95)
        _generate_report(run_id, niche_id, niche_name, db, cfg)

        db.close()
        update_job(job_id, status="done", stage="Complete", progress=100,
                   message=f"Done! {survived} ideas ready to review.",
                   result_json=json.dumps({"run_id": run_id, "survived": survived}))

    except Exception as e:
        logger.exception("Ideas job %d failed", job_id)
        update_job(job_id, status="failed", error=str(e),
                   message=f"Run failed: {str(e)[:200]}")


def _generate_report(run_id: int, niche_id: int, niche_name: str, db, cfg: dict):
    from pathlib import Path
    ideas = db.execute("""
        SELECT * FROM ideas WHERE run_id=? AND verdict != 'KILL'
        ORDER BY final_score DESC LIMIT 10
    """, (run_id,)).fetchall()

    lines = [f"# Ideas Report: {niche_name}", f"**Run ID:** {run_id}",
             f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
             "", "---", ""]

    for i, idea in enumerate(ideas, 1):
        verdict_emoji = {"MAKE": "✅", "MAKE_WITH_REFRAME": "🔄", "PARK": "⏸️", "VIRGIN_GOLD": "🥇"}.get(idea["verdict"], "")
        lines.append(f"## {i}. {idea['title']}")
        lines.append(f"**Verdict:** {verdict_emoji} {idea['verdict']}  ")
        lines.append(f"**Score:** {idea['final_score']:.1f} (demand:{idea['demand_score']:.1f} × staleness:{idea['staleness_mult']} × structure:{idea['structural_score']:.1f})  ")
        lines.append(f"**One-liner:** {idea['one_line']}  ")
        if idea.get("angle_note"):
            lines.append(f"**Angle/Hook:** {idea['angle_note']}  ")
        lines.append("")

        try:
            evidence = json.loads(idea["evidence_json"] or "[]")
            if evidence:
                lines.append("| Title | Views | Rel. Perf | Age | Format Match | Type |")
                lines.append("|-------|-------|-----------|-----|--------------|------|")
                for e in evidence[:5]:
                    lines.append(
                        f"| {e.get('matched_title','')[:40]} | {e.get('views',0):,} | "
                        f"{e.get('relative_perf',0):.1f}x | {e.get('age_months',0):.0f}mo | "
                        f"{'Yes' if e.get('format_match') else 'No'} | {e.get('check_type','')} |"
                    )
                lines.append("")
        except Exception:
            pass
        lines.append("")

    report_dir = Path(__file__).parent.parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    safe_name = niche_name.lower().replace(" ", "_")
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    report_path = report_dir / f"{safe_name}_{date_str}_run{run_id}.md"
    report_path.write_text("\n".join(lines))

    db.execute("UPDATE runs SET report_path=? WHERE id=?", (str(report_path), run_id))
    db.commit()
    return str(report_path)


def start_job(job_type: str, niche_id: int, **kwargs) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO jobs (type, niche_id, status, started_at) VALUES (?,?,?,?)",
        (job_type, niche_id, "queued", datetime.utcnow().isoformat())
    )
    db.commit()
    job_id = cur.lastrowid
    db.close()

    if job_type == "ingestion":
        t = threading.Thread(
            target=run_ingestion_job,
            args=(job_id, niche_id, kwargs.get("urls", []),
                  kwargs.get("files", []), kwargs.get("pasted_text", "")),
            daemon=True
        )
    elif job_type == "ideas":
        t = threading.Thread(
            target=run_ideas_job,
            args=(job_id, niche_id, kwargs.get("n_ideas", 30),
                  kwargs.get("validate_top", 25)),
            daemon=True
        )
    else:
        raise ValueError(f"Unknown job type: {job_type}")

    t.start()
    return job_id
