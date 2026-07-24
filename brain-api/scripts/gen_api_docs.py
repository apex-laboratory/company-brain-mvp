"""Generate the API reference docs from the live FastAPI app.

Dumps the real OpenAPI spec (``app.openapi()``) to ``docs/openapi.json`` and
renders the self-contained ``docs/api.html`` from ``scripts/api_doc_template.html``
(the ``/*__SPEC__*/`` marker is replaced with the embedded spec). Run via
``make docs`` so the published docs never drift from the code.

Self-contained: seeds dummy settings (like the test conftest) so the app imports
without a real DB/Redis, and adds ``brain-api`` to ``sys.path`` so it runs from
any working directory.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_BRAIN_API = _SCRIPT_DIR.parent
_REPO_ROOT = _BRAIN_API.parent
_DOCS = _REPO_ROOT / "docs"
_TEMPLATE = _SCRIPT_DIR / "api_doc_template.html"
_OUT_JSON = _DOCS / "openapi.json"
_OUT_HTML = _DOCS / "api.html"

# Dummy env so pydantic Settings validates without a live stack (mirrors
# app/tests/conftest.py). These never touch a real service — we only import the
# app object and call app.openapi().
_ENV_DEFAULTS = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "REDIS_URL": "redis://localhost:6379",
    "JWT_ACCESS_SECRET": "docs-access-secret-at-least-32-characters-long",
    "JWT_REFRESH_SECRET": "docs-refresh-secret-at-least-32-characters-long",
    "ENCRYPTION_KEY": "0" * 64,
    "ALLOWED_ORIGINS": "http://localhost:3000",
    "AI_SERVICE_URL": "http://localhost:8000",
    "AI_SERVICE_TOKEN": "docs-ai-token",
    "RESEND_API_KEY": "docs-resend-key",
}


def main() -> int:
    for key, value in _ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(_BRAIN_API))

    from app.main import app  # imported after env + path setup

    spec = app.openapi()
    _DOCS.mkdir(parents=True, exist_ok=True)
    _OUT_JSON.write_text(json.dumps(spec, indent=2) + "\n")

    template = _TEMPLATE.read_text()
    if "/*__SPEC__*/" not in template:
        print(f"ERROR: marker /*__SPEC__*/ not found in {_TEMPLATE}", file=sys.stderr)
        return 1
    # Guard against a literal </script> inside the JSON breaking the inline block.
    embedded = json.dumps(spec).replace("</", "<\\/")
    _OUT_HTML.write_text(template.replace("/*__SPEC__*/", embedded))

    ops = sum(len(m) for m in spec.get("paths", {}).values())
    print(f"Wrote {_OUT_JSON.relative_to(_REPO_ROOT)} and "
          f"{_OUT_HTML.relative_to(_REPO_ROOT)} "
          f"({len(spec.get('paths', {}))} paths, {ops} operations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
