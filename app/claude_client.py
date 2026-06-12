"""Claude API wrapper."""
import json
import logging
import re
from pathlib import Path
from anthropic import Anthropic, APIError

from app.config import get_env, load_config, clean_key, key_problem

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text()
    raise FileNotFoundError(f"Prompt file not found: {name}")


def _client() -> Anthropic:
    key = clean_key(get_env("ANTHROPIC_API_KEY"))
    if not key:
        raise ValueError("Anthropic API key not configured.")
    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
    return Anthropic(api_key=key)


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _call(model: str, system: str, user: str, max_tokens: int = 4096) -> dict | list:
    client = _client()
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = resp.content[0].text
            cleaned = _clean_json(raw)
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            if attempt == 0:
                logger.warning("JSON decode failed, retrying: %s", e)
                continue
            raise ValueError(f"Claude returned invalid JSON: {raw[:200]}")
        except APIError as e:
            raise RuntimeError(f"Claude API error: {e}")


def test_anthropic_key() -> tuple[bool, str]:
    # Local sanity check first — catches copy-paste junk before any network call
    problem = key_problem(get_env("ANTHROPIC_API_KEY"))
    if problem:
        return False, problem
    try:
        client = _client()
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, "Connected successfully."
    except ValueError as e:
        return False, str(e)
    except APIError as e:
        status = getattr(e, "status_code", None)
        if status in (401, 403):
            return False, "This Anthropic API key was rejected. Check it is correct and active at console.anthropic.com."
        return False, f"Anthropic API error: {str(e)[:160]}"
    except Exception as e:
        return False, f"Could not reach Anthropic. Check your internet connection. ({str(e)[:120]})"


def extract_screenshot(image_b64: str, media_type: str = "image/png") -> dict:
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt_text = _load_prompt("screenshot_extract.txt")
    client = _client()
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                        {"type": "text", "text": prompt_text},
                    ],
                }],
            )
            raw = resp.content[0].text
            return json.loads(_clean_json(raw))
        except json.JSONDecodeError:
            if attempt == 0:
                continue
            raise


def classify_document(text: str) -> dict:
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt = _load_prompt("doc_classify.txt")
    return _call(model, prompt, text[:12000])


def parse_text(text: str) -> dict:
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt = _load_prompt("text_parse.txt")
    return _call(model, prompt, text[:12000])


def classify_format(channel_name: str, description: str, recent_titles: list[str]) -> dict:
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt = _load_prompt("format_classify.txt")
    user = f"Channel: {channel_name}\nDescription: {description[:500]}\nRecent titles:\n" + "\n".join(recent_titles[:15])
    return _call(model, prompt, user)


def extract_patterns(outliers: list[dict], averages: list[dict], niche_name: str) -> dict:
    cfg = load_config()
    model = cfg["models"]["generate_reason"]
    prompt = _load_prompt("pattern_extract.txt")
    user = f"Niche: {niche_name}\n\nOUTLIER VIDEOS:\n"
    for v in outliers[:60]:
        user += f"- {v['title']} ({v.get('outlier_score', 0):.1f}x)\n"
    user += "\nAVERAGE VIDEOS:\n"
    for v in averages[:40]:
        user += f"- {v['title']}\n"
    return _call(model, prompt, user, max_tokens=6000)


def derive_rubric(outliers: list[dict], averages: list[dict], niche_name: str) -> dict:
    cfg = load_config()
    model = cfg["models"]["generate_reason"]
    prompt = _load_prompt("rubric_derive.txt")
    user = f"Niche: {niche_name}\n\nOUTLIERS:\n"
    for v in outliers[:40]:
        user += f"- {v['title']}\n"
    user += "\nAVERAGE:\n"
    for v in averages[:30]:
        user += f"- {v['title']}\n"
    return _call(model, prompt, user, max_tokens=4096)


def generate_ideas(rubric: list, patterns: list, frameworks: list, existing_titles: list,
                   shipped_summary: str, seeds: list, n: int, niche_name: str) -> list:
    cfg = load_config()
    model = cfg["models"]["generate_reason"]
    prompt = _load_prompt("idea_generate.txt").replace("{n}", str(n))
    user = f"Niche: {niche_name}\n\nRUBRIC:\n{json.dumps(rubric, indent=2)}\n\n"
    user += f"PATTERNS:\n{json.dumps(patterns[:20], indent=2)}\n\n"
    user += f"FRAMEWORKS:\n{json.dumps(frameworks[:20], indent=2)}\n\n"
    if shipped_summary:
        user += f"SHIPPED RESULTS:\n{shipped_summary}\n\n"
    if seeds:
        user += f"EXTERNAL SEEDS:\n{json.dumps(seeds[:20], indent=2)}\n\n"
    user += f"EXISTING TITLES (do not duplicate):\n" + "\n".join(existing_titles[:200])
    result = _call(model, prompt, user, max_tokens=8000)
    if isinstance(result, dict):
        return result.get("ideas", result.get("candidates", []))
    return result


def score_structural(titles_with_rubric: list[dict], rubric: list, niche_name: str) -> list:
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt = _load_prompt("structural_score.txt")
    user = f"Niche: {niche_name}\n\nRUBRIC:\n{json.dumps(rubric, indent=2)}\n\nCANDIDATES:\n"
    for item in titles_with_rubric:
        user += f"- {item['title']}\n"
    result = _call(model, prompt, user, max_tokens=6000)
    if isinstance(result, dict):
        return result.get("scores", result.get("results", []))
    return result


def get_verdict(idea: dict, structural_score: float, evidence: list) -> dict:
    cfg = load_config()
    model = cfg["models"]["generate_reason"]
    prompt = _load_prompt("verdict.txt")
    user = f"IDEA: {idea['title']}\nOne-liner: {idea.get('one_line', '')}\n"
    user += f"Structural score: {structural_score}/10\n\n"
    user += f"EVIDENCE:\n{json.dumps(evidence[:20], indent=2)}"
    return _call(model, prompt, user, max_tokens=1024)


def judge_faceless_batch(channels: list[dict]) -> list[dict]:
    """
    Batch-judge a list of channels for faceless status.
    Each channel dict must have: name, description, video_titles (list), yt_channel_id.
    Returns list of {yt_channel_id, faceless_judgment, faceless_confidence, faceless_reason, niche_guess}.
    Uses the cheap parse_classify model (Haiku) as specified.
    """
    cfg = load_config()
    model = cfg["models"]["parse_classify"]
    prompt = _load_prompt("faceless_judge.txt")
    results = []
    # Process in batches of 5 to stay within token limits
    for i in range(0, len(channels), 5):
        batch = channels[i:i+5]
        user = "Judge each of these channels:\n\n"
        for ch in batch:
            user += f"---\nChannel ID: {ch['yt_channel_id']}\n"
            user += f"Name: {ch.get('name', '')}\n"
            user += f"Description: {ch.get('description', '')[:400]}\n"
            titles = ch.get("video_titles", [])
            user += f"Video titles: {json.dumps(titles[:20])}\n"
        user += f"\nReturn a JSON array with one object per channel in the same order."
        try:
            raw = _call(model, prompt, user, max_tokens=2048)
            if isinstance(raw, list):
                for j, ch in enumerate(batch):
                    entry = raw[j] if j < len(raw) else {}
                    results.append({
                        "yt_channel_id": ch["yt_channel_id"],
                        "faceless_judgment": entry.get("faceless", "unclear"),
                        "faceless_confidence": float(entry.get("confidence", 0.5)),
                        "faceless_reason": entry.get("reason", ""),
                        "niche_guess": entry.get("niche_guess", ""),
                    })
            elif isinstance(raw, dict):
                # Single channel response
                for ch in batch:
                    results.append({
                        "yt_channel_id": ch["yt_channel_id"],
                        "faceless_judgment": raw.get("faceless", "unclear"),
                        "faceless_confidence": float(raw.get("confidence", 0.5)),
                        "faceless_reason": raw.get("reason", ""),
                        "niche_guess": raw.get("niche_guess", ""),
                    })
        except Exception as e:
            logger.warning("faceless_judge batch failed: %s", e)
            for ch in batch:
                results.append({
                    "yt_channel_id": ch["yt_channel_id"],
                    "faceless_judgment": "unclear",
                    "faceless_confidence": 0.0,
                    "faceless_reason": "Judgment unavailable.",
                    "niche_guess": "",
                })
    return results
