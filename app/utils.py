import re
import difflib
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta


def parse_view_string(s: str) -> int | None:
    if not s:
        return None
    s = s.strip().replace(",", "").replace(" views", "").replace(" view", "")
    s = s.strip()
    m = re.match(r"([\d.]+)\s*([KkMmBb]?)", s)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        return int(num * 1_000)
    elif suffix == "M":
        return int(num * 1_000_000)
    elif suffix == "B":
        return int(num * 1_000_000_000)
    return int(num)


def parse_age_string(s: str) -> str | None:
    """Convert '10 months ago' → ISO date string (approx)."""
    if not s:
        return None
    s = s.lower().strip()
    now = datetime.utcnow()
    patterns = [
        (r"(\d+)\s+year", "years"),
        (r"(\d+)\s+month", "months"),
        (r"(\d+)\s+week", "weeks"),
        (r"(\d+)\s+day", "days"),
        (r"(\d+)\s+hour", "hours"),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, s)
        if m:
            n = int(m.group(1))
            if unit == "years":
                dt = now - relativedelta(years=n)
            elif unit == "months":
                dt = now - relativedelta(months=n)
            elif unit == "weeks":
                dt = now - timedelta(weeks=n)
            elif unit == "days":
                dt = now - timedelta(days=n)
            else:
                dt = now - timedelta(hours=n)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def fuzzy_match(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def is_duplicate(title: str, existing: list[str], threshold: float = 0.85) -> bool:
    for t in existing:
        if fuzzy_match(title, t) >= threshold:
            return True
    return False


def parse_duration_iso(iso: str) -> int:
    """Parse ISO 8601 duration (PT4M13S) → seconds."""
    if not iso:
        return 0
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def months_ago(date_str: str) -> float:
    if not date_str:
        return 999
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        delta = datetime.utcnow() - dt.replace(tzinfo=None)
        return delta.days / 30.44
    except Exception:
        return 999


def median(values: list) -> float:
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


def percentile(values: list, p: float) -> float:
    if not values:
        return 0
    s = sorted(values)
    idx = int(len(s) * p / 100)
    idx = min(idx, len(s) - 1)
    return s[idx]
