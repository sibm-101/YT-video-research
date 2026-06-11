"""Stage 5: Stress-test gauntlet."""
import json
import logging
from datetime import datetime

import httpx

from app import claude_client, youtube
from app.database import get_db
from app.config import load_config
from app.utils import fuzzy_match, months_ago, percentile, is_duplicate

logger = logging.getLogger(__name__)

WIKI_UA = "ViralIdeaEngine/1.0 (research tool; contact@example.com)"


def _get_wiki_pageviews(entity: str, cfg: dict) -> float | None:
    """Returns average monthly pageviews for the entity or None."""
    try:
        # Step 1: Resolve article title
        search_url = "https://en.wikipedia.org/w/api.php"
        r = httpx.get(search_url, params={
            "action": "opensearch", "search": entity, "limit": 1, "format": "json"
        }, headers={"User-Agent": WIKI_UA}, timeout=10)
        data = r.json()
        if not data or len(data) < 2 or not data[1]:
            return None
        article = data[1][0].replace(" ", "_")

        # Step 2: Get 6 months of pageviews
        from datetime import timedelta
        end = datetime.utcnow()
        start = end - timedelta(days=180)
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")

        pv_url = (
            f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            f"en.wikipedia/all-access/user/{article}/monthly/{start_str}/{end_str}"
        )
        r2 = httpx.get(pv_url, headers={"User-Agent": WIKI_UA}, timeout=10)
        pv_data = r2.json()
        items = pv_data.get("items", [])
        if not items:
            return None
        total = sum(i.get("views", 0) for i in items)
        return total / len(items)
    except Exception as e:
        logger.debug("Wiki pageviews failed for %s: %s", entity, e)
        return None


def _get_or_compute_channel_median(channel_yt_id: str, db) -> int | None:
    """Get cached channel median or compute from recent videos."""
    ch = db.execute("SELECT median_views, subs FROM channels WHERE yt_channel_id=?",
                    (channel_yt_id,)).fetchone()
    if ch and ch["median_views"]:
        return ch["median_views"]
    # Proxy: 10% of subs
    if ch and ch["subs"]:
        return int(ch["subs"] * 0.1)
    return None


def run_gauntlet(idea: dict, niche_id: int, quota_tracker: dict,
                 all_idea_titles: list[str], niche_videos: list[dict],
                 rubric: list, niche_name: str, job_update=None) -> dict:
    """
    Run all stress-test checks on a single idea.
    Returns enriched idea dict with scores, verdict, evidence.
    """
    cfg = load_config()
    title = idea["title"]

    if job_update:
        job_update(f"Stress-testing: {title[:60]}...")

    # 1. Dedup (already filtered before this, but double-check)
    niche_titles = [v.get("title", "") for v in niche_videos]
    if is_duplicate(title, niche_titles + all_idea_titles, 0.85):
        return {**idea, "verdict": "KILL", "final_score": 0,
                "structural_score": 0, "demand_score": 0,
                "staleness_mult": 1.0, "freshness_gate": 0,
                "evidence_json": "[]", "kill_reason": "Duplicate of existing content"}

    # 2. Structural score (batched elsewhere, but handle single here)
    structural_score = idea.get("structural_score", 5.0)

    if structural_score < cfg.get("structural_min", 6):
        return {**idea, "verdict": "KILL", "final_score": 0,
                "structural_score": structural_score, "demand_score": 0,
                "staleness_mult": 1.0, "freshness_gate": 1,
                "evidence_json": "[]",
                "kill_reason": f"Low structural score ({structural_score:.1f})"}

    # 3. Live validation
    evidence = []
    demand_score = 5.0  # default neutral
    freshness_gate = 1
    staleness_mult = 1.0

    if quota_tracker.get("used", 0) < quota_tracker.get("max", 6000):
        db = get_db()
        try:
            core_entity = idea.get("core_entity", "")
            keywords = title.split()[:5]
            query_topic = " ".join(keywords)
            query_title = title

            for query in [query_topic, query_title]:
                if quota_tracker.get("used", 0) >= quota_tracker.get("max", 6000):
                    break
                try:
                    results = youtube.search_videos(query, max_results=10)
                    quota_tracker["used"] = quota_tracker.get("used", 0) + 100

                    for r in results:
                        ch_median = _get_or_compute_channel_median(r.get("channel_id_yt", ""), db)
                        if not ch_median:
                            ch_median = max(r.get("views", 0) // 10, 1000)

                        rel_perf = r["views"] / ch_median if ch_median else 1.0
                        age = months_ago(r.get("published_at", ""))

                        # Check format match
                        ch_row = db.execute(
                            "SELECT format_class FROM channels WHERE yt_channel_id=?",
                            (r.get("channel_id_yt", ""),)
                        ).fetchone()
                        ch_format = ch_row["format_class"] if ch_row else None
                        # Get niche's dominant format
                        niche_ch = db.execute(
                            "SELECT format_class FROM channels WHERE niche_id=? AND format_class IS NOT NULL LIMIT 1",
                            (niche_id,)
                        ).fetchone()
                        niche_format = niche_ch["format_class"] if niche_ch else None
                        fmt_match = 1 if (ch_format and niche_format and
                                         ch_format.lower() == niche_format.lower()) else 0

                        title_sim = fuzzy_match(r["title"], title)
                        check_type = "demand" if query == query_topic else "burn" if title_sim >= 0.8 else "demand"

                        evidence.append({
                            "yt_video_id": r.get("yt_video_id", ""),
                            "matched_title": r["title"],
                            "views": r["views"],
                            "channel_subs": 0,
                            "channel_median": ch_median,
                            "relative_perf": round(rel_perf, 2),
                            "format_match": fmt_match,
                            "age_months": round(age, 1),
                            "check_type": check_type,
                            "title_similarity": round(title_sim, 2),
                        })
                except Exception as e:
                    logger.warning("Search failed: %s", e)
        finally:
            db.close()

        # Compute scores from evidence
        demand_score, staleness_mult, freshness_gate = _compute_scores(evidence, cfg, niche_id)

        # External demand check (Wikipedia) when thin supply
        same_format_strong = [e for e in evidence if e.get("format_match") and e.get("relative_perf", 0) >= 2.0]
        if len(same_format_strong) < 2 and idea.get("core_entity"):
            avg_views = _get_wiki_pageviews(idea["core_entity"], cfg)
            if avg_views is not None:
                if avg_views >= cfg.get("wiki_min_monthly_views", 50000) and not same_format_strong:
                    idea["virgin_gold"] = True
                    demand_score = max(demand_score, 7.0)
                    idea["wiki_monthly_views"] = int(avg_views)
                elif avg_views < cfg.get("wiki_dead_monthly_views", 3000) and not same_format_strong:
                    demand_score = min(demand_score, 2.0)
                    idea["wiki_monthly_views"] = int(avg_views)

    # 4. Verdict
    try:
        verdict_result = claude_client.get_verdict(idea, structural_score, evidence)
        verdict = verdict_result.get("verdict", "PARK")
        reasoning = verdict_result.get("reasoning", "")
        reframe = verdict_result.get("reframe", "")
    except Exception as e:
        logger.warning("Verdict call failed: %s", e)
        verdict = "PARK"
        reasoning = "Verdict unavailable."
        reframe = ""

    if freshness_gate == 0:
        verdict = "KILL"

    # 5. Final score
    final_score = (demand_score * staleness_mult * structural_score * freshness_gate) / 10.0

    return {
        **idea,
        "structural_score": round(structural_score, 1),
        "demand_score": round(demand_score, 1),
        "staleness_mult": staleness_mult,
        "freshness_gate": freshness_gate,
        "final_score": round(final_score, 2),
        "verdict": verdict,
        "reasoning": reasoning,
        "reframe": reframe,
        "evidence_json": json.dumps(evidence),
    }


def _compute_scores(evidence: list, cfg: dict, niche_id: int) -> tuple[float, float, int]:
    """Returns (demand_score, staleness_mult, freshness_gate)."""
    demand_score = 3.0
    staleness_mult = 1.0
    freshness_gate = 1

    if not evidence:
        return demand_score, staleness_mult, freshness_gate

    best_rel_perf = max((e.get("relative_perf", 0) for e in evidence), default=0)
    all_views = [e.get("views", 0) for e in evidence]

    # Demand: any strong performer
    if best_rel_perf >= 3.0:
        demand_score = min(10.0, 5.0 + best_rel_perf)
    elif best_rel_perf >= 1.5:
        demand_score = 6.0
    else:
        demand_score = 3.0

    # Staleness
    strong_old = [e for e in evidence if e.get("relative_perf", 0) >= 2.0 and e.get("age_months", 0) > 18]
    strong_recent = [e for e in evidence if e.get("relative_perf", 0) >= 2.0 and e.get("age_months", 0) <= 12]

    if strong_old and not strong_recent:
        staleness_mult = 1.5
    elif strong_recent:
        staleness_mult = 0.7

    # Frame burn
    burn_min_subs = cfg.get("burn_min_subs", 20000)
    burn_fuzzy = cfg.get("burn_fuzzy_threshold", 0.8)
    for e in evidence:
        if (e.get("format_match") and
                e.get("title_similarity", 0) >= burn_fuzzy and
                e.get("channel_subs", 0) >= burn_min_subs and
                e.get("relative_perf", 1) < 0.7):
            freshness_gate = 0
            break

    # Saturation check
    same_format_strong_recent = [
        e for e in evidence
        if e.get("format_match") and e.get("relative_perf", 0) >= 2.0 and e.get("age_months", 0) <= 12
    ]
    if len(same_format_strong_recent) >= 3:
        demand_score *= 0.6

    return demand_score, staleness_mult, freshness_gate
