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


def get_env(key: str) -> str:
    return os.environ.get(key, "")


def has_keys() -> bool:
    load_dotenv(ENV_PATH, override=True)
    return bool(get_env("ANTHROPIC_API_KEY") and get_env("YOUTUBE_API_KEY"))


def write_env(anthropic_key: str, youtube_key: str, reddit_id: str = "", reddit_secret: str = ""):
    lines = [f"ANTHROPIC_API_KEY={anthropic_key}", f"YOUTUBE_API_KEY={youtube_key}"]
    if reddit_id:
        lines.append(f"REDDIT_CLIENT_ID={reddit_id}")
    if reddit_secret:
        lines.append(f"REDDIT_CLIENT_SECRET={reddit_secret}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    load_dotenv(ENV_PATH, override=True)
