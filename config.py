"""Environment configuration (read once at import)."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


CONFIG = {
    "CRAWL_TOKEN": os.environ.get("CRAWL_TOKEN", ""),
    "ROSTER_SHEET_URL": os.environ.get("ROSTER_SHEET_URL", ""),
    "GNEWS_API_KEY": os.environ.get("GNEWS_API_KEY", ""),
    "SENDGRID_API_KEY": os.environ.get("SENDGRID_API_KEY", ""),
    "EMAIL_FROM": os.environ.get("EMAIL_FROM", "alerts@thesqua.re"),
    "EMAIL_TO": os.environ.get("EMAIL_TO", ""),
    "EMAIL_MODE": os.environ.get("EMAIL_MODE", "changes-only"),
    "DETAIL_CAP": _int("DETAIL_CAP", 40),
    "DASHBOARD_URL": os.environ.get("DASHBOARD_URL", ""),
}
