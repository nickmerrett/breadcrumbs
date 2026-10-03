"""
Entry point:
  BREADCRUMBS_KEYS="me:KEY1,code:KEY2" BREADCRUMBS_PUBLIC_URL=https://crumbs.example.com \
  BREADCRUMBS_TRUSTED_AGENTS="me,bob" \
    uvicorn main:app --port 8080
"""
import os

from app import create_app, parse_keys

_trusted_raw = os.getenv("BREADCRUMBS_TRUSTED_AGENTS", "")
_trusted = {a.strip() for a in _trusted_raw.split(",") if a.strip()}

app = create_app(os.getenv("BREADCRUMBS_DB", "breadcrumbs.db"),
                 parse_keys(os.getenv("BREADCRUMBS_KEYS", "")),
                 os.getenv("BREADCRUMBS_PUBLIC_URL", "http://localhost:8080"),
                 trusted_agents=_trusted or None)
