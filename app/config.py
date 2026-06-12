import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

DEFAULTS = {
    "models": {
        "parse_classify": "claude-haiku-4-5-20251001",
        "generate_reason": "claude-sonnet-4-6",
    },
    "outlier_threshold": 3.0,
    "longform_min_seconds": 180,
    "median_window_videos": 30,
    "ideas_per_run": 30,
    "validate_top": 25,
    "structural_min": 6,
    "staleness_months": 18,
    "burn_min_subs": 20000,
    "burn_fuzzy_threshold": 0.8,
    "max_searches_per_run": 60,
    "cache_days": 7,
    "wiki_min_monthly_views": 50000,
    "wiki_dead_monthly_views": 3000,
    "reddit": {
        "enabled": False,
        "subreddits": {
            "history": ["AskHistorians", "todayilearned"]
        },
    },
    "hunter": {
        "max_channel_age_days": 30,
        "min_breakout_views": 500000,
        "region": "US",
        "include_shorts": False,
        "max_seeds_per_hunt": 35,
        "seed_keywords": [
            "dark history", "scary stories", "true crime", "sleep sounds",
            "motivation speech", "AI explained", "space documentary",
            "geography facts", "war documentary", "reddit stories",
            "amazing facts", "psychology tricks", "make money online",
            "luxury lifestyle", "animal facts", "health tips",
            "gaming lore", "mysteries unsolved", "famous biographies",
            "how things work", "lost civilizations", "ocean mysteries",
            "cold case solved", "conspiracy theories", "survival stories",
            "ancient history", "criminal minds", "paranormal stories",
            "science explained", "world records", "financial freedom",
            "mind blowing facts", "serial killers", "space exploration",
            "natural disasters",
        ],
    },
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        # Deep merge with defaults
        return _deep_merge(DEFAULTS.copy(), data)
    return DEFAULTS.copy()


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# Invisible / formatting characters that frequently ride along when an API key
# is copied from a web page or PDF. These break HTTP header encoding downstream.
_INVISIBLE_CHARS = ("﻿", "​", "‌", "‍", "⁠", " ", " ")


def clean_key(key: str) -> str:
    """API keys are plain ASCII. Strip whitespace, surrounding quotes, and
    invisible copy-paste artifacts (BOM, zero-width spaces, non-breaking spaces)."""
    if not key:
        return ""
    for ch in _INVISIBLE_CHARS:
        key = key.replace(ch, "")
    return key.strip().strip('"').strip("'").strip()


def key_problem(key: str) -> str:
    """Return a plain-English problem with the key, or '' if it looks usable.
    Does NOT check validity with the provider — only local sanity."""
    cleaned = clean_key(key)
    if not cleaned:
        return "No key was entered."
    if not cleaned.isascii():
        return ("This key contains characters that don't belong in an API key. "
                "It was probably copied with hidden formatting — please delete it, "
                "re-copy the key directly from the provider, and paste it again.")
    return ""


def get_env(key: str) -> str:
    return os.environ.get(key, "")


def has_keys() -> bool:
    load_dotenv(ENV_PATH, override=True)
    return bool(get_env("ANTHROPIC_API_KEY") and get_env("YOUTUBE_API_KEY"))


def write_env(anthropic_key: str, youtube_key: str, reddit_id: str = "", reddit_secret: str = ""):
    anthropic_key = clean_key(anthropic_key)
    youtube_key = clean_key(youtube_key)
    reddit_id = clean_key(reddit_id)
    reddit_secret = clean_key(reddit_secret)
    lines = [f"ANTHROPIC_API_KEY={anthropic_key}", f"YOUTUBE_API_KEY={youtube_key}"]
    if reddit_id:
        lines.append(f"REDDIT_CLIENT_ID={reddit_id}")
    if reddit_secret:
        lines.append(f"REDDIT_CLIENT_SECRET={reddit_secret}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    load_dotenv(ENV_PATH, override=True)
