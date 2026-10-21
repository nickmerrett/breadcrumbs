"""
Entry point:
  BREADCRUMBS_DIR=/data BREADCRUMBS_KEYS="me:KEY1,code:KEY2" \
  BREADCRUMBS_PUBLIC_URL=https://crumbs.example.com \
  BREADCRUMBS_TRUSTED_AGENTS="me,bob" \
    uvicorn main:app --host 0.0.0.0 --port 8080

Data layout inside BREADCRUMBS_DIR:
  entries/          one .md file per record (source of truth)
  index.sqlite      derived FTS + vector index (disposable, rebuildable)
"""
import os

from app import create_app, parse_keys

_trusted_raw = os.getenv("BREADCRUMBS_TRUSTED_AGENTS", "")
_trusted = {a.strip() for a in _trusted_raw.split(",") if a.strip()}

app = create_app(
    data_dir=os.getenv("BREADCRUMBS_DIR", "./data"),
    keys=parse_keys(os.getenv("BREADCRUMBS_KEYS", "")),
    public_url=os.getenv("BREADCRUMBS_PUBLIC_URL", "http://localhost:8080"),
    trusted_agents=_trusted or None,
)
